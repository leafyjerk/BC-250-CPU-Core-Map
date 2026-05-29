# BC-250 CPU Core Map

A small read-only diagnostic that establishes the **physical CPU core layout** of the
AMD BC-250 (Cyan Skillfish / harvested PS5 APU), and documents why the core-disable
fuse is *not* readable from userspace on this board the way it is on retail Ryzen.

## TL;DR finding

The BC-250's CPU is a physically **8-core Zen 2 die with 2 cores disabled**, arranged
as a symmetric **3 + 3** split across its two CCX:

```
CCX0:  ■■■□     cores 0,1,2 enabled   ·  core 3 off
CCX1:  ■■■□     cores 4,5,6 enabled   ·  core 7 off
              ■ enabled   □ disabled
8 physical positions · 6 enabled · disabled = {3, 7}
```

This is derived unambiguously from CPU topology and confirmed by three independent
signals (Linux `core id`s, per-core APIC IDs, and the two-instance L3/CCX structure).
It matches the widely-reported "harvested PS5 die, 6 of 8 cores" story at the
silicon-layout level.

## What it does *not* answer (yet)

Whether cores 3 and 7 are **factory-fused off** (a hard lock) or **config-disabled**
(potentially recoverable) cannot be determined from topology alone. On retail Ryzen
that distinction comes from reading the core-disable fuse over the SMN bus — and on
the BC-250 that read does not work (see below). So this tool proves the cores are
*physically present*; it does not claim they are unlockable. Given the PSP enforces
fuse state at boot and this is salvaged console silicon, treat unlock prospects as
unconfirmed and probably hard.

## The SMN wall (a documented negative result)

On retail Ryzen the core-disable fuse is read via the northbridge SMN window:
write the SMN address to PCI `00:00.0` register `0x60` (index), read the value from
`0x64` (data). The Zen 2 core-disable fuse lives at SMN `0x30081800 + 0x238`
(`0x30081A38`), per the `ryzen_smu` and `ZenStates-Core` projects.

On the BC-250 this window is **dead**:

- The index write *sticks* — writing `0x30081a38` to `0x60` and reading it back returns `0x30081a38`.
- The data port `0x64` returns `0xFFFFFFFF` for every address, both via memory-mapped
  config access and via legacy port I/O (`setpci -A intel-conf1`).
- `ryzen_smu` does not help: it uses the *same* `0x60`/`0x64` window, and separately its
  CPU-detection / PCI-match table doesn't recognize this APU (family `0x17` model `0x47`),
  so it loads but never attaches.

The CPU northbridge SMN path is locked on this firmware. The remaining avenue for a
register-level read is the **GPU-side SMN path** — the same one `umr` uses, and that
the 40-CU work relied on — reaching SMN through the amdgpu device rather than the CPU
NB window. Whether a GPU-originated SMN read can reach a CPU-domain fuse on this
unified APU is an open question; if you have `umr` working on a BC-250, that's the
experiment to run.

## What the tool reads

1. **OS-visible CPU** — model, family/model/stepping, microcode, logical CPUs, core ids, L3.
2. **Physical core map** — CCX-aware reconstruction of the 8 die positions and which are off.
3. **CPUID enumeration** — leaves `0x1`, `0x80000008`, `0x8000001E`, `0x8000001D`.
4. **P-state table** — decodes MSRs `0xC0010064..0xC001006B` to per-state frequency/voltage.
5. **SMN window check** — performs the fuse read and reports the locked window as a finding.

It saves a `.txt` and `.json` report and prints a compact paste-back block for
fleet data collection.

## Requirements

Pure Python 3 standard library — no pip packages. The CPUID and P-state sections use
the `cpuid` and `msr` kernel modules; the SMN check uses `setpci` from `pciutils`
(already present on most systems).

## Usage

```bash
sudo modprobe cpuid msr      # enables the CPUID + P-state sections
sudo python3 bc250_cpu_probe.py
```

Everything is **read-only**. The SMN check writes only the SMN *index* register to
address a read; nothing touches a fuse, an SMU command port, or any core-config register.

## Reporting your board

Run it and paste the "PASTE-BACK SUMMARY" block (or attach the `.json`). The interesting
questions for the fleet: do all BC-250s disable the same two positions (3 and 7), is the
3+3 split universal, and does any board's firmware leave the SMN window open?

## Credits

This stands entirely on the BC-250 community's reverse-engineering work:

- **duggasco** — 40-CU unlock and the UMR register-dump methodology; the ■/□ harvest-map
  style is borrowed from that project.
- **bc250collective** — SMU API reverse-engineering that underpins SMU access on this SoC.
- **mrfrakes** and **dantistnfs** — `bc250_smu_oc`, the SMU-based CPU OC/UV work.
- **filippor**, **mothenjoyer69 / TuxThePenguin0** — `cyan-skillfish-governor`
  (smu/tt branches), `oberon-governor`, and the `ignore_cu_harvest` patch.
- **elektricM** and contributors — the community `amd-bc250-docs`.
- **irusanov** (`ZenStates-Core`) and **leogx9r** / **amkillam** (`ryzen_smu`) — the
  Zen 2 core-disable fuse register offsets and decode logic used by the SMN check.
- **Chester Lam / Chips and Cheese** — the deep-dive on the PS5 Zen 2 cores used in
  the BC-250 (background reference).
- The **BC-250 Discord** community — collective testing and guidance.
- Claude - probe tooling

## License

GPL 2.0
