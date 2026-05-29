#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bc250_cpu_probe.py  -  AMD BC-250 (Cyan Skillfish / harvested PS5 APU) CPU & core map

Read-only. Establishes the physical CPU core layout of the BC-250 from information
that is actually reachable on this board, and documents the one thing that is not.

What it reports:
  1. OS-visible CPU        (/proc/cpuinfo, lscpu)
  2. Physical core map     (CCX-aware: which of the 8 die positions are enabled/off)
  3. CPUID enumeration     (leaves 0x1, 0x80000008, 0x8000001E, 0x8000001D)
  4. P-state table         (MSRs 0xC0010064..0xC001006B -> freq/voltage per state)
  5. SMN window check      (documents that the CPU core-disable fuse is NOT readable
                            via the NB SMN window on this board - a finding, not a bug)

Background: the register-level core-disable fuse lives in SMN space. On retail Ryzen
it is read via the NB window (PCI 00:00.0 reg 0x60 index / 0x64 data). On the BC-250
that window returns 0xFFFFFFFF for any address, via both MMIO and intel-conf1 access -
the data port never answers. So the physical core layout here is derived from CPU
topology (core ids + APIC IDs + L3/CCX structure), which is unambiguous on its own.

Usage:
    sudo modprobe cpuid msr        # enables the CPUID + P-state sections
    sudo python3 bc250_cpu_probe.py

Requires root for CPUID/MSR. Degrades gracefully if anything is unavailable.
Saves bc250_cpu_report_<timestamp>.txt and .json next to where you run it.
"""

import os
import re
import time
import json
import struct
import shutil
import platform
import subprocess

CORES_PER_CCX = 4          # Zen / Zen 2 CCX width
PSTATE_MSRS = [0xC0010064 + i for i in range(8)]

# Core-disable fuse SMN addresses by codename, from ZenStates-Core Cpu.cs.
# APUs relocate the fuse out of the desktop 0x30081xxx region into 0x5Dxxx.
# Cyan Skillfish (BC-250) is a Zen 2 APU, so Renoir's address is the prime suspect.
FUSE_CANDIDATES = [
    (0x0005D3E8, "Renoir (Zen 2 APU)"),
    (0x0005D449, "Cezanne (Zen 3 APU)"),
    (0x0005D254, "Picasso/Raven (Zen/Zen+ APU)"),
    (0x0005D4DC, "Rembrandt"),
    (0x30081A38, "Matisse (desktop Zen 2)"),   # what retail tools assume; empty on APU
]
SCAN_LO, SCAN_HI = 0x0005D200, 0x0005D600      # APU fuse neighborhood (bounded fallback)

REPORT = {}
LINES = []


def out(s=""):
    print(s)
    LINES.append(s)


def rule(title):
    out("")
    out("=" * 74)
    out(title)
    out("=" * 74)


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


# ---- 1. OS-VISIBLE CPU -----------------------------------------------------------
def section_os():
    rule("1. OS-VISIBLE CPU  (only ever the ENABLED cores)")
    cpuinfo = ""
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
    except Exception:
        pass

    def first(field):
        m = re.search(rf"^{re.escape(field)}\s*:\s*(.+)$", cpuinfo, re.M)
        return m.group(1).strip() if m else None

    n_logical = len(re.findall(r"^processor\s*:", cpuinfo, re.M))
    apicids = [int(x) for x in re.findall(r"^apicid\s*:\s*(\d+)", cpuinfo, re.M)]
    core_ids = sorted(set(int(x) for x in re.findall(r"^core id\s*:\s*(\d+)", cpuinfo, re.M)))
    mhz = [float(x) for x in re.findall(r"^cpu MHz\s*:\s*([\d.]+)", cpuinfo, re.M)]

    info = dict(model=first("model name"), vendor=first("vendor_id"),
                family=first("cpu family"), model_n=first("model"),
                stepping=first("stepping"), microcode=first("microcode"),
                n_logical=n_logical, core_ids_list=core_ids, apicids=apicids)

    out(f"  Model            : {info['model']}")
    out(f"  Vendor           : {info['vendor']}")
    out(f"  Family/Model/Step: {info['family']} / {info['model_n']} / {info['stepping']}")
    out(f"  Microcode        : {info['microcode']}")
    out(f"  Logical CPUs     : {n_logical}")
    out(f"  Core ids present : {core_ids}")
    if mhz:
        out(f"  Current MHz range: {min(mhz):.0f} - {max(mhz):.0f}")

    ls = run(["lscpu"]) or ""
    l3 = re.search(r"^L3 cache:\s*(.+)$", ls, re.M)
    if l3:
        out(f"  L3 cache         : {l3.group(1).strip()}")
        inst = re.search(r"\((\d+)\s*instance", l3.group(1))
        info["l3_instances"] = int(inst.group(1)) if inst else None
    for key in ("Thread(s) per core", "Core(s) per socket", "Socket(s)", "NUMA node(s)"):
        m = re.search(rf"^{re.escape(key)}:\s*(.+)$", ls, re.M)
        if m:
            out(f"  {key:<18}: {m.group(1).strip()}")

    REPORT["os"] = info


# ---- 2. PHYSICAL CORE MAP --------------------------------------------------------
def section_coremap():
    rule("2. PHYSICAL CORE MAP  (what's on the die, derived from topology)")
    osd = REPORT.get("os", {})
    enabled = osd.get("core_ids_list") or []
    if not enabled:
        out("  No core-id data available; cannot build map.")
        return

    n_ccx = osd.get("l3_instances") or (max(enabled) // CORES_PER_CCX + 1)
    total = n_ccx * CORES_PER_CCX
    disabled = [c for c in range(total) if c not in enabled]

    out(f"  Assuming Zen 2 layout: {CORES_PER_CCX} cores/CCX x {n_ccx} CCX = {total} physical positions")
    out("")
    splits = []
    for ccx in range(n_ccx):
        positions = range(ccx * CORES_PER_CCX, (ccx + 1) * CORES_PER_CCX)
        cells = "".join("\u25a0" if p in enabled else "\u25a1" for p in positions)
        en = [p for p in positions if p in enabled]
        dis = [p for p in positions if p not in enabled]
        splits.append(len(en))
        out(f"  CCX{ccx}:  {cells}   cores {list(positions)}   enabled {en}   off {dis}")

    out("")
    out(f"  >> Physical cores on die : {total}  (\u25a0 enabled  \u25a1 disabled)")
    out(f"  >> Enabled               : {enabled}  ({len(enabled)} cores)")
    out(f"  >> Disabled positions    : {disabled}")
    out(f"  >> CCX split             : {' + '.join(str(s) for s in splits)}")

    # APIC corroboration
    apicids = sorted(set(i >> 1 for i in osd.get("apicids", [])))
    if apicids:
        out(f"  >> Per-core APIC indices : {apicids}  (corroborates the core-id map)")

    REPORT["coremap"] = dict(total=total, n_ccx=n_ccx, enabled=enabled,
                             disabled=disabled, split=splits)


# ---- CPUID helper ----------------------------------------------------------------
def cpuid(leaf, subleaf=0, cpu=0):
    try:
        with open(f"/dev/cpu/{cpu}/cpuid", "rb") as f:
            f.seek((subleaf << 32) | leaf)
            data = f.read(16)
        if len(data) == 16:
            return struct.unpack("<4I", data)
    except Exception:
        pass
    return None


def section_cpuid():
    rule("3. CPUID ENUMERATION")
    if not os.path.exists("/dev/cpu/0/cpuid"):
        run(["modprobe", "cpuid"])
    if not os.path.exists("/dev/cpu/0/cpuid"):
        out("  /dev/cpu/0/cpuid unavailable (sudo modprobe cpuid). Skipping.")
        return
    info = {}
    r = cpuid(0x80000008)
    if r:
        out(f"  CPUID 8000_0008 ECX : 0x{r[2]:08X}  (NC field -> {(r[2] & 0xFF) + 1} enumerated)")
    r = cpuid(0x8000001E)
    if r:
        out(f"  CPUID 8000_001E EBX : 0x{r[1]:08X}  threads/CU -> {((r[1] >> 8) & 0xFF) + 1}")
    r = cpuid(0x1)
    if r:
        eax = r[0]
        fam = ((eax >> 8) & 0xF) + ((eax >> 20) & 0xFF)
        mod = ((eax >> 4) & 0xF) | ((eax >> 12) & 0xF0)
        out(f"  CPUID 0000_0001 EAX : 0x{eax:08X}  family 0x{fam:X} model 0x{mod:X} step {eax & 0xF}")
        info["family"] = fam
    i = 0
    while i < 8:
        r = cpuid(0x8000001D, i)
        if not r or (r[0] & 0x1F) == 0:
            break
        if ((r[0] >> 5) & 0x7) == 3:
            out(f"  L3 shared by {((r[0] >> 14) & 0xFFF) + 1} logical processors per instance")
        i += 1
    REPORT["cpuid"] = info


# ---- 4. P-STATE TABLE ------------------------------------------------------------
def rdmsr(msr, cpu=0):
    try:
        with open(f"/dev/cpu/{cpu}/msr", "rb") as f:
            f.seek(msr)
            data = f.read(8)
        if len(data) == 8:
            return struct.unpack("<Q", data)[0]
    except Exception:
        pass
    return None


def section_pstates():
    rule("4. P-STATE TABLE  (per-state frequency / voltage, read-only)")
    if not os.path.exists("/dev/cpu/0/msr"):
        run(["modprobe", "msr"])
    if not os.path.exists("/dev/cpu/0/msr"):
        out("  /dev/cpu/0/msr unavailable (sudo modprobe msr). Skipping.")
        return
    states = []
    out("  Pn   raw                FID  DID   ~MHz    VID   ~Volts  enabled")
    out("  ---  -----------------  ---  ---  ------  ----  ------  -------")
    for n, msr in enumerate(PSTATE_MSRS):
        val = rdmsr(msr)
        if val is None:
            continue
        fid, did, vid = val & 0xFF, (val >> 8) & 0x3F, (val >> 14) & 0xFF
        mhz = (fid / did) * 200.0 if did else 0.0
        volts = 1.55 - vid * 0.00625
        out(f"  P{n}   0x{val:016X}  {fid:>3}  {did:>3}  {mhz:6.0f}  0x{vid:02X}  {volts:5.3f}   {bool((val >> 63) & 1)}")
        states.append(dict(n=n, mhz=round(mhz), volts=round(volts, 3)))
    REPORT["pstates"] = states


# ---- 5. SMN CORE-DISABLE FUSE (APU-aware) ----------------------------------------
def _smn_once(addr):
    # One setpci call: write index, read data, re-read index. Returns (data, index).
    o = run(["setpci", "-s", "00:00.0", f"0x60.L={addr:08x}", "0x64.L", "0x60.L"])
    if not o:
        return None, None
    toks = o.split()
    if len(toks) < 2:
        return None, None
    try:
        return int(toks[0], 16), int(toks[1], 16)
    except ValueError:
        return None, None


def _smn_verified(addr, samples=5):
    # Trust data only when the index survived the read. Stable = all samples agree.
    vals, races = [], 0
    for _ in range(samples):
        data, idx = _smn_once(addr)
        if data is None:
            return None, False, races
        if idx != addr:
            races += 1
            continue
        vals.append(data)
    return (vals[0] if vals else None), (len(vals) > 0 and len(set(vals)) == 1), races


def _decode(val):
    mask = val & 0xFF
    disabled = [i for i in range(8) if mask & (1 << i)]
    enabled = [i for i in range(8) if not (mask & (1 << i))]
    return mask, enabled, disabled


def section_smn_check():
    rule("5. SMN CORE-DISABLE FUSE  (APU-aware probe)")
    if not shutil.which("setpci"):
        out("  setpci not found (pciutils); skipping.")
        return
    if os.geteuid() != 0:
        out("  Need root for SMN access; skipping.")
        return
    out("  NB SMN window (00:00.0 0x60 index / 0x64 data) is SHARED with the GPU")
    out("  SMU/governor. Stop it first for clean reads:")
    out("      sudo systemctl stop cyan-skillfish-governor-smu")
    out("  (restart afterwards with: sudo systemctl start cyan-skillfish-governor-smu)")
    out("")

    topo_dis = set(REPORT.get("coremap", {}).get("disabled", []))
    topo_en = set(REPORT.get("coremap", {}).get("enabled", []))
    target_en = len(topo_en) or 6

    out("  Known core-fuse addresses (APUs live in 0x5Dxxx, not 0x30081xxx):")
    candidates = []        # stable, plausibly-decoded hits
    for addr, name in FUSE_CANDIDATES:
        val, stable, races = _smn_verified(addr)
        if val is None:
            out(f"    0x{addr:08X}  {name:<27} read failed (need root / no setpci)")
            continue
        if val == 0xFFFFFFFF:
            out(f"    0x{addr:08X}  {name:<27} 0xFFFFFFFF (unmapped on this silicon)")
        elif not stable:
            out(f"    0x{addr:08X}  {name:<27} unstable (races={races}; telemetry, not fuse)")
        else:
            mask, en, dis = _decode(val)
            flag = "  <== plausible" if len(en) == target_en else ""
            out(f"    0x{addr:08X}  {name:<27} 0x{val:08X} mask=0x{mask:02X} off={dis}{flag}")
            if len(en) == target_en:
                candidates.append((addr, name, val, mask, en, dis))

    # Bounded scan only if no named address produced a plausible mask.
    if not candidates:
        out("")
        out(f"  No named address matched. Bounded scan 0x{SCAN_LO:05X}-0x{SCAN_HI:05X}")
        out("  for a stable register leaving exactly {0} enabled cores (~30s)...".format(target_en))
        for addr in range(SCAN_LO, SCAN_HI, 4):
            val, stable, _ = _smn_verified(addr, samples=3)
            if val is None or not stable or val in (0x0, 0xFFFFFFFF):
                continue
            mask, en, dis = _decode(val)
            if len(en) == target_en:
                out(f"    0x{addr:08X} = 0x{val:08X}  mask=0x{mask:02X}  off={dis}  <== candidate")
                candidates.append((addr, "scan", val, mask, en, dis))

    out("")
    # Prefer the candidate whose disabled set exactly matches topology.
    hit = None
    for c in candidates:
        if set(c[5]) == topo_dis and topo_dis:
            hit = c
            break
    if hit is None and candidates:
        hit = candidates[0]

    if hit:
        addr, name, val, mask, en, dis = hit
        out(f"  >> Core-disable fuse @ 0x{addr:08X} = 0x{val:08X}   ({name})")
        out(f"     mask 0x{mask:02X}  ->  enabled {en}   disabled {dis}")
        factory_fused = (set(dis) == topo_dis and bool(topo_dis))
        if factory_fused:
            out(f"     Matches topology (cores {sorted(topo_dis)} off). The FUSE reports")
            out("     these cores disabled => FACTORY-FUSED. Hard lock; the PSP enforces")
            out("     fuse state at boot, so an OS-side unlock is almost certainly out.")
        elif not dis and topo_dis:
            out(f"     Fuse shows NO cores disabled, yet the OS sees only {len(topo_en)}.")
            out("     => CONFIG / downcore-disabled, not fused. This is the softer case;")
            out("     a downcore/CCD config path may exist (still PSP-gated - verify).")
        else:
            out(f"     Fuse-off {dis} vs topology-off {sorted(topo_dis)}: investigate the")
            out("     mismatch (bit ordering or wrong register) before trusting it.")
        REPORT["smn"] = dict(fuse_addr=f"0x{addr:08X}", source=name, value=f"0x{val:08X}",
                             mask=f"0x{mask:02X}", enabled=en, disabled=dis,
                             factory_fused=factory_fused)
    else:
        out("  >> No plausible core-disable fuse at known APU addresses or in the scan.")
        out("     Stop the governor and re-run; if still nothing, widen SCAN_LO/SCAN_HI.")
        REPORT["smn"] = dict(found=False)


def section_summary():
    rule("PASTE-BACK SUMMARY  (copy this block when reporting your board)")
    osd = REPORT.get("os", {})
    cm = REPORT.get("coremap", {})
    smn = REPORT.get("smn", {})
    out(f"  model         : {osd.get('model')}")
    out(f"  microcode     : {osd.get('microcode')}")
    out(f"  logical cpus  : {osd.get('n_logical')}")
    if cm:
        out(f"  physical cores: {cm.get('total')}  enabled {cm.get('enabled')}  off {cm.get('disabled')}")
        out(f"  ccx split     : {' + '.join(str(s) for s in cm.get('split', []))}")
    smn = REPORT.get("smn", {})
    if smn.get("fuse_addr"):
        out(f"  core fuse     : {smn['fuse_addr']} = {smn['value']}  mask {smn['mask']}  off {smn['disabled']}")
        out(f"  fuse verdict  : {'FACTORY-FUSED (hard lock)' if smn.get('factory_fused') else 'see report (possible config-disable)'}")
    else:
        out("  core fuse     : not found at known APU addresses (stop governor + re-run)")


def main():
    out("BC-250 CPU & CORE MAP")
    out(f"Host: {platform.node()}   Kernel: {platform.release()}   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if os.geteuid() != 0:
        out("\n  !! Not root. CPUID/MSR/SMN sections will be limited. Re-run with sudo.\n")
    section_os()
    section_coremap()
    section_cpuid()
    section_pstates()
    section_smn_check()
    section_summary()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        with open(f"bc250_cpu_report_{stamp}.txt", "w") as f:
            f.write("\n".join(LINES) + "\n")
        with open(f"bc250_cpu_report_{stamp}.json", "w") as f:
            json.dump(REPORT, f, indent=2, default=str)
        out(f"\nSaved: bc250_cpu_report_{stamp}.txt")
        out(f"Saved: bc250_cpu_report_{stamp}.json")
    except Exception as e:
        out(f"\n(could not write report files: {e})")


if __name__ == "__main__":
    main()
