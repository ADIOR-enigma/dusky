# Dusky Kernel Compiler — Visual Map

> **One sentence:** You pick a *recipe* (TOML profile) → the compiler *tailors* a kernel to your hardware like a bespoke suit, borrowing only what you need.

---

## 1. The Big Analogy: Bespoke Tailor Shop

| Kernel World | Tailor Shop Analogy |
|---|---|
| **TOML profiles** `kernel_profiles/*.toml` | **Recipe cards** on the wall - "Gaming cut", "Battery saver cut", "Mint factory uniform (v3)" |
| **Live system** `/proc/config.gz` + `modprobed.db` | **Your body measurements** taken while you wear the current suit |
| `localmodconfig strict` | **Slim-fit:** only fabric for *your* measurements, tiny closet, fast to sew |
| `localmodconfig expanded` + `LMC_KEEP` | **Regular-fit:** slim + extra pockets for common tools (USB4, NVMe, GPU) - safe if you buy new shoes |
| **CPU arch** `native / generic_v3 / v4 / znver4 / generic` | **Fabric cut:** `native` = cut to *your* shoulders (fastest), `generic_v3` = one-size-fits-most (2013+ machines), `generic_v4` = modern factory line |
| `hz / tickless / preempt` | **Heartbeat:** 1000Hz full-tickless = drummer hitting every millisecond (gaming snappy), 300Hz idle = drummer rests when quiet (battery) |
| **Build dir** `~/dusky_build` or `/mnt/zram1` | **Workbench:** garage table (`~/dusky_build`) vs RAM table that vanishes on reboot but doesn't scratch the floor (ZRAM) |
| **pacman-pkg** | **Boxing the suit** with label `linux-dusky-gaming-7.2-1` so `pacman -U` can install/remove cleanly |

---

## 2. Where Everything Lives (Source of Truth)

```
~/user_scripts/kernel/
├── dusky_kernal_compile.py       # The tailor (engine)
├── kernel_profiles/              # Recipe drawer - YOU edit these, code reads them
│   ├── 01_gaming.toml            #   BORE + 1000Hz + native + expanded
│   ├── 05_battery.toml           #   vanilla + 300Hz + expanded
│   ├── 06_distributable_v3.toml  #   generic_v3 - share with friends
│   ├── 08_minimal_strict.toml    #   strict - only your hardware
│   └── 10_throughput_bmq.toml    #   BMQ scheduler
└── DUSKY_KERNEL_VISUAL_MAP.md    # this file

~/.config/dusky/settings/dusky_kernel/
├── state.json                    # Remembers last profile, build dir, LLVM preference
└── kernel.config.<profile>       # Last successful .config per profile (your perfect pattern)
```

> **Rule:** TOML is king. Code never hard-codes a tuning knob. You add `11_my_experiment.toml`, it appears instantly in `--list-profiles` and the menu.

**A profile is 11 tiny drawers:**

```toml
[meta]      name, description, suffix       # label on the box
[release]   channel = mainline/stable/lts  # which kernel.org shelf to pick from
[scheduler] type = vanilla/bore/bmq        # brain: bore=snappy, bmq=throughput, vanilla=EEVDF
[dusky]     enhanced = true/false          # extra Dusky tweaks
[cpu]       arch = native/generic_v3/... + governor  # fabric cut
[timing]    hz / tickless / preempt        # heartbeat
[memory]    thp/mglru/numa/zswap/slub      # closet organization
[compiler]  optimize o3/o2/size + lto + kcfi  # scissors sharpness
[power]     wq_power_efficient
[network]   congestion bbr/bbr3/cubic + qdisc
[modules]   mode strict/expanded           # slim vs regular fit
```

---

## 3. The Journey: From Menu to Boot (Visual Flow)

```mermaid
flowchart TD
    A[You run dusky_kernal_compile.py] --> B{Profiles found?}
    B -->|no| Z[Error: no TOML]
    B -->|yes| C[Table: gaming | low_ram | ... | bmq]
    C --> D[You pick #3 maximum_performance]
    D --> E[Ephemeral overrides?]
    E -->|"CPU arch? keep/native/v3/v4/znver4"| F[ Effective arch = generic_v3? ]
    E -->|"Modules? keep/strict/expanded"| G[ Effective mode = strict? ]
    F & G --> H[Effective profile = TOML + overrides\n(TOML file untouched)]
    H --> I[Fetch kernel.org/releases.json\nmainline/stable/lts]
    I --> J[Pick version: 7.2 (#1) / 7.1.9 / 6.18.45]
    J --> K[Download linux-7.2.tar.xz\naria2 16x + sha256sums.asc verify]
    K --> L[Unpack to ~/dusky_build/linux-7.2]
    L --> M[Patch stage: bore/bmq patch\nif missing → fallback vanilla]
    M --> N[Inject config:\n saved kernel.config.<profile> ? that : live /proc/config.gz]
    N --> O[Prune: make localmodconfig\nLSMOD=modprobed.db + LMC_KEEP?\nstrict→LMC_KEEP="" | expanded→LMC_KEEP=313 long]
    O --> P[make scripts]
    P --> Q[Matrix: scripts/config\nH Z + tickless + preempt + dusky + CFI + arch + LTO + BBR + ...]
    Q --> R[Rust? probe rustavailable → -e RUST : -d RUST]
    R --> S[localversion = -dusky-xxx\nmake prepare; yes \"\"|make config; olddefconfig]
    S --> T[[Dry-run estimate total steps\nfor ETA bar]]
    T --> U{Stale .o/vmlinux?}
    U -->|yes| V[make clean]
    U -->|no| W
    V --> W[Build: make -j$(nproc) LLVM=1 CC=clang pacman-pkg\nPKGDEST=packages/linux-7.2-dusky-xxx]
    W --> X[Live progress bar + last 20 log lines]
    X --> Y{Success?}
    Y -->|no| Y1[Show last 12 lines, keep .config]
    Y -->|yes| Z1[Find *.pkg.tar.zst in PKGDEST]
    Z1 --> Z2[sudo pacman -U *.pkg.tar.zst]
    Z2 --> Z3[kernel-install add / bootctl update / grub-mkconfig]
    Z3 --> AA[Mission Accomplished! Reboot → bootctl list]
```

**Intuition:** Step `O→Q` is like the tailor chalking the pattern after measuring you, then cutting away 70% of the factory's giant pattern.

---

## 4. Decision Points & What Changes What

### 4.1 CPU Arch (the fabric)
```
native      → -d GENERIC_CPU -d MZEN4 -e X86_NATIVE_CPU
              Your CPU flags only. Fastest, not portable.

generic     → -e GENERIC_CPU -d MZEN4 -d X86_NATIVE_CPU --set-val X86_64_VERSION 1
              Baseline x86-64 (2003+). Share with anyone.

generic_v2  → X86_64_VERSION 2 (SSE4.2)   | v3 → AVX2 (Haswell 2013+)
generic_v4  → X86_64_VERSION 4 (AVX-512)  | znver4 → AMD Zen4 tuned (-e MZEN4)
```
*Override at build time keeps TOML pristine:* `--cpu-arch generic_v3` or menu `CPU arch?`

### 4.2 Module Mode (the closet)
```
expanded (safe)   LMC_KEEP = drivers/usb:drivers/gpu:fs:...:kernel/sched:kernel/bpf  (313 chars)
                  Keeps common friends even if not plugged in today.
                  Use when you *might* plug a new USB stick tomorrow.

strict (tiny)     LMC_KEEP = ""
                  Only modules seen in modprobed.db / LSMOD + boot essentials.
                  Kernel 30-50% smaller, compile faster, but needs recompile after new hardware.
                  Run `modprobed-db store` after using new gear, then recompile.
```

### 4.3 Interactive Flow (what you actually see)

```
Profile: gaming (BORE • native:expanded • 1000Hz/full/full • ...)
Effective: BORE • native:expanded • 1000Hz/full/full • ...

Profile CPU arch: native (native (this CPU))  Modules: expanded (safety net)
[CPU architecture for this build? [keep/native/generic/.../znver4] (keep): keep
[Module pruning mode? [keep/strict/expanded] (keep): strict
→ Effective build deviates from TOML: native/strict
```

*Env for automation:* `DUSKY_CPU_ARCH=generic_v3 DUSKY_MODULES_MODE=strict python dusky_kernal_compile.py --profile gaming`

---

## 5. Under the Hood: 3 Layers

1. **Discovery & Trust**
   * `releases.json` → `sha256sums.asc` per `vMAJOR.x` → `hash_file` verify → `aria2c -x16 -c` resume → reuse verified tarball next run.

2. **Configuration Surgery**
   * `export_active_config` → `/proc/config.gz` else `/boot/config-*` → `localmodconfig` (LSMOD) → `scripts` → `build_config_matrix(profile)` → `scripts/config -e/-d/--set-val` (30-40 knobs) → `RUST` probe → `prepare + olddefconfig`.

3. **Build & Ship**
   * Estimate `make -n all | CC/LD/AR` count → `progress + Live` panel (auto-extends if underestimate) → `make pacman-pkg -j$(nproc)` in its own `setsid` → `PKGDEST` isolated per `profile+version` → `pacman -U --needed` → `kernel-install + bootctl/grub`.

All steps respect `DUSKY_BUILD_DIR` (findmnt `tmpfs/zram` detection), `sudo -v` keepalive, `terminate_process_group` on `Ctrl+C`, per-profile `kernel.config.<name>`.

---

## 6. How to Invent Your Own Profile (30 seconds)

1. Copy: `cp kernel_profiles/01_gaming.toml kernel_profiles/11_my_lab.toml`
2. Edit:
   ```toml
   [meta]
   name = "my_lab"
   suffix = "dusky-lab"
   [cpu]
   arch = "generic_v3"   # share lab machines
   default_governor = "schedutil"
   [modules]
   mode = "strict"      # tiny
   [dusky]
   enhanced = true
   ```
3. Run: `python dusky_kernal_compile.py --list-profiles` → see `my_lab`
4. Pick `11` in menu → answer `keep` for overrides → builds `linux-my_lab`.

---

## 7. Troubleshooting Cheat-Sheet

| Symptom | Fix |
|---|---|
| `<100 drivers` warning | `1) Install Toolchains & Init` → `2) View Telemetry` → use USB/WiFi/audio → count rises |
| `Only 10GB free` | `4) Config Manager → Build Directory` → `/mnt/zram1/dusky_build` (ZRAM) or `/tmp` |
| `patch stage failed` | Normal if major (e.g., 8.x) ahead of patch set → choose `vanilla` when asked |
| `Invalid: ... Choose ...` | Menu expects numbers `1-6` or `keep/native/...` literals |
| Want to share kernel | Build with `generic_v3` (or add `06_distributable` profile) |

---

**Glossary in one breath:** `HZ` tick, `NO_HZ` tickless, `PREEMPT` low-latency, `THP` hugepages, `LTO` link-time optimization, `BBR` TCP, `MGLRU/NUMA/ZSWAP` memory, `SLUB_TINY` allocator, `WQ` power, `BORE/BMQ` schedulers, `KCFI` security, `BTF` pretty `bpftool` types.

*Tip:* Run `--verify` for empirical diagnostics (BTF, sched_ext, free space, profiles) without compiling.
