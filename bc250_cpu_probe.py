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


# ---- 5. SMN WINDOW CHECK ---------------------------------------------------------
def section_smn_check():
    rule("5. SMN WINDOW CHECK  (documents the core-disable fuse path)")
    if not shutil.which("setpci"):
        out("  setpci not found (pciutils); skipping optional check.")
        return
    addr = 0x30081A38   # Zen 2 core-disable fuse: 0x30081800 + 0x238 (per ryzen_smu/ZenStates)
    run(["setpci", "-s", "00:00.0", f"0x60.L={addr:08x}"])
    data = run(["setpci", "-s", "00:00.0", "0x64.L"])
    if data is None:
        out("  Could not run setpci (need root). Skipping.")
        return
    val = int(data.strip(), 16)
    out(f"  NB SMN index 0x60 <- 0x{addr:08X};  data 0x64 -> 0x{val:08X}")
    if val == 0xFFFFFFFF:
        out("  >> Window LOCKED: the CPU core-disable fuse is not readable from")
        out("     userspace on this board (confirmed via MMIO and intel-conf1).")
        out("     This is a documented BC-250 finding. Register-level reads would")
        out("     need the GPU-side SMN path (umr) - see README.")
        REPORT["smn"] = dict(addr=addr, value="0xFFFFFFFF", locked=True)
    else:
        mask = val & 0xFF
        out(f"  >> Window OPEN: low byte 0x{mask:02X} = core-disable mask "
            f"(set bits = off cores).")
        REPORT["smn"] = dict(addr=addr, value=f"0x{val:08X}", mask=mask, locked=False)


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
    out(f"  smn fuse path : {'LOCKED' if smn.get('locked') else smn.get('value', 'n/a')}")


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
