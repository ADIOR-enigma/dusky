# Dusky Kernel Compiler — Profile Schema & Configuration Guide
# ==============================================================================
# This template documents every configurable setting, every allowed option,
# and what each knob does in plain English.
#
# You can copy this file, rename it to 'XX_myprofile.toml', and tune it.
# ==============================================================================

[meta]
# Unique internal identifier for the profile (referenced with: --profile my_name)
name = "my_custom_kernel"

# Brief summary shown in the interactive menu and profile list table
description = "Custom build optimized for my daily workflow"

# Suffix added to the kernel release and package name.
# Resulting Arch packages: 'linux-dusky-custom.pkg.tar.zst' & 'linux-dusky-custom-headers-*.pkg.tar.zst'
suffix = "dusky-custom"

# Sort order in the interactive menu (1 = top of list, 100 = bottom)
priority = 50

# Free-form label tags for categorization
tags = ["desktop", "gaming", "custom"]


[release]
# Which upstream kernel.org branch to track:
#   - "mainline" : Absolute bleeding-edge release (or release candidate if allow_rc = true)
#   - "stable"   : Latest stable point release (Recommended for daily drivers)
#   - "longterm" : Long-Term Support branch (Maximum stability)
channel = "stable"

# Exact version pin (e.g. "7.2.1"). Leave empty ("") to always pick the newest available in channel.
pin = ""

# Set to true to allow building -rc (release candidate) tarballs when on the mainline channel
allow_rc = false

# Minimum allowable version floor (e.g. "7.1"). Empty ("") means no restriction.
min_version = ""


[scheduler]
# The core process scheduler:
#   - "eevdf" : Pristine Linux upstream Earliest Eligible Virtual Deadline First scheduler
#   - "bore"  : Burst-Oriented Response Enhancer (CachyOS patch: best desktop/gaming latency)
#   - "bmq"   : BitMap Queue alternative scheduler (Project C: tuned for batch throughput)
type = "bore"

# CONFIG_SCHED_AUTOGROUP: Automatic per-session task grouping (improves desktop smoothness under heavy load)
autogroup = true

# CONFIG_RT_GROUP_SCHED: Real-time task cgroup bandwidth control (leave false unless using RT cgroups)
rt_group = false

# If the scheduler patch (e.g., BORE) cannot apply to the kernel version, seamlessly continue on EEVDF
allow_vanilla_fallback = true


[dusky]
# Enables opinionated desktop latency heuristics and umbrella tuning
enhanced = true

# Identifiers baked into kernel build metadata for reproducibility
hostname = "dusky"
user = "dusky"

# Fixes build timestamp to tarball release date (SOURCE_DATE_EPOCH) for bit-for-bit reproducible builds
reproducible = true

# Escape hatch: inject arbitrary raw Kconfig symbols (e.g. { "CONFIG_NVME_CORE" = true, "CONFIG_NR_CPUS" = 64 })
extra_config = {}


[cpu]
# Target CPU instruction set architecture:
#   - "native"         : Matches the exact CPU running the compiler (Fastest, uses all CPU instructions)
#   - "generic_v3"     : x86-64-v3 (AVX2, BMI2, FMA, Haswell 2013+) - Sweet spot for sharing with friends
#   - "generic_v4"     : x86-64-v4 (AVX-512, Zen4+ / modern Intel Xeon)
#   - "generic_v2"     : x86-64-v2 (SSE4.2 / POPCNT)
#   - "generic"        : Baseline x86-64 (Maximum compatibility with 2003+ PCs)
#   - Specific family  : "znver2", "znver3", "znver4", "znver5", "skylake", "alderlake", "raptorlake"
arch = "native"

# Default boot-time CPU frequency scaling governor:
#   - "performance" : Locks CPU to maximum clock speed (Best for gaming / desktop responsiveness)
#   - "schedutil"   : Dynamically adjusts clock speed based on scheduler load (Balanced)
#   - "powersave"   : Forces lowest power states (Used with Intel/AMD hardware autonomous EPP)
#   - "ondemand"    : Legacy dynamic scaling
governor = "performance"

# AMD P-State driver mode (for AMD Ryzen processors):
#   - "active"    : Full hardware autonomous frequency scaling (Energy Performance Preference EPP)
#   - "passive"   : Kernel controls frequency targets
#   - "guided"    : Kernel provides minimum/maximum range; CPU hardware selects exact clock
#   - "undefined" : Auto-detect default
amd_pstate = "undefined"

# Hardware vulnerability mitigations (Spectre, Meltdown, Retbleed):
#   - true  : Full security mitigations enabled (Safe)
#   - false : Bakes in 'mitigations=off' for maximum raw instruction speed
mitigations = true

# Maximum number of CPU cores the kernel supports (2 to 8192). Lower values save slight memory.
nr_cpus = 512

# SMT / Hyper-Threading awareness in the task scheduler
smt = true

# Machine Check Exception hardware error logging
mce = true


[timing]
# Timer interrupt tick frequency in Hz:
#   - 1000 : Highest timer resolution, 1ms ticks (Best for gaming and competitive latency)
#   - 500  : Balanced high-responsiveness
#   - 300  : Ideal for audio workstations and video playback
#   - 250  : Traditional server / battery-saving balance
#   - 100  : Minimal idle wakeups for headless servers
hz = 1000

# Tickless idle mode:
#   - "full"     : NO_HZ_FULL — stops timer ticks on running CPUs during single-task loads (Lowest latency)
#   - "idle"     : NO_HZ_IDLE — stops timer ticks only when CPU cores are idle (Standard desktop)
#   - "periodic" : Timer tick runs continuously (Legacy)
tickless = "idle"

# Kernel preemption model (Latency vs. Throughput trade-off):
#   - "full"      : PREEMPT — Kernel code is immediately interruptible (Snappiest desktop & gaming)
#   - "lazy"      : PREEMPT_LAZY — Hybrid throughput/latency preemption model (Modern Linux 6.12+)
#   - "voluntary" : Balanced throughput for workstations
#   - "none"      : Pure batch throughput (Traditional servers)
#   - "rt"        : Hard Real-Time PREEMPT_RT (For audio synthesis and robotics)
preempt = "full"

# CONFIG_PREEMPT_DYNAMIC: Allows overriding preemption model at boot via 'preempt=full|lazy|none'
preempt_dynamic = true

# Offload RCU callback processing to background housekeeping threads
hz_periodic_rcu = false


[memory]
# Transparent Hugepages (THP) memory allocation policy:
#   - "always"  : Aggressively uses 2MB hugepages (Best for gaming and high-performance computation)
#   - "madvise" : Allocates hugepages only when applications explicitly request them (Saves RAM)
#   - "never"   : Disables hugepages
thp = "madvise"

# Multi-Gen LRU (MGLRU): Modern, efficient memory page reclamation (Reduces lag under high RAM pressure)
mglru = true

# Enables compressed RAM swap cache (Zswap) automatically at boot
zswap_default_on = false

# Compression algorithm for Zswap: "zstd" (Best ratio), "lz4" (Fastest), "lzo", "deflate"
zswap_compressor = "zstd"

# CONFIG_SLUB_TINY: Micro-allocator footprint for low-RAM machines (<= 8GB). Sacrifices some throughput.
slub_tiny = false

# Non-Uniform Memory Access (NUMA) multi-socket/multi-die memory awareness (Keep true for modern CPUs)
numa = true

# Automatically migrates memory pages closer to the executing CPU core
numa_balancing = false

# Kernel Samepage Merging (deduplicates identical memory pages across Virtual Machines)
ksm = true

# Data Access Monitoring subsystem
damon = false

# Reports free memory pages back to virtualization hypervisors
page_reporting = true


[compiler]
# Toolchain compiler suite:
#   - "llvm" : Clang + LLVM + LLD linker (Enables ThinLTO, AutoFDO, and modern optimizations)
#   - "gcc"  : GNU Compiler Collection (GCC) + BFD linker
toolchain = "llvm"

# Compiler optimization level:
#   - "o2"   : Standard production optimization
#   - "o3"   : Aggressive high-performance optimization (-O3)
#   - "size" : Optimize for smallest binary size (-Os)
optimize = "o2"

# Link-Time Optimization (LTO) — LLVM only:
#   - "thin"      : Multi-threaded LTO (Fast compile, ~95% of full LTO performance gains)
#   - "full"      : Single-threaded whole-program optimization (Slowest build, maximum runtime speed)
#   - "thin_dist" : Distributed ThinLTO model
#   - "none"      : Disables LTO for fastest compile times
lto = "thin"

# Kernel Control Flow Integrity security (Requires LLVM + LTO)
kcfi = false

# Clang AutoFDO profiling consumption
autofdo = false

# Post-link Propeller block layout optimization
propeller = false

# ZSTD compression level for the final kernel image and initramfs (1 = fastest, 19 = smallest file)
zstd_clevel = 19

# On-disk kernel module compression: "zstd", "xz", "gzip", "none"
module_compress = "zstd"

# Debug information level:
#   - "none"    : Fastest compile, smallest kernel. Preserves core vmlinux BTF for BPF/sched_ext!
#   - "reduced" : Minimal DWARF symbols for basic stack traces
#   - "full"    : Full DWARF debug sections (Very large build size)
debug_info = "none"

# Parallel compilation jobs ('make -j N'). Set to 0 to auto-detect all CPU threads.
jobs = 0

# Enables Rust kernel infrastructure if 'rustc' and 'bindgen' are detected on your system
rust = true


[power]
# Schedules workqueues to power-efficient CPU cores by default
wq_power_efficient = false

# CPU idle governor:
#   - "menu"   : Standard tickless idle governor
#   - "teo"    : Timer Events Oriented governor (Optimized for laptops and battery endurance)
#   - "ladder" : Stepped idle state governor
cpu_idle_governor = "menu"

# Batches non-urgent RCU callbacks to keep CPU cores in deep C-state sleep longer
rcu_lazy = false

# Energy-Aware Scheduling (EAS) model for hybrid Intel (P/E-core) or AMD architectures
energy_model = false

# Power management sleep and hibernate support
suspend = true


[network]
# TCP congestion control algorithm:
#   - "bbr"      : Google BBR (Highest throughput, lowest bufferbloat on Wi-Fi / broadband)
#   - "bbr3"     : Out-of-tree BBRv3 (Falls back to BBR if kernel tree lacks v3)
#   - "cubic"    : Standard Linux default
#   - "reno"     : Traditional TCP
#   - "westwood" : Optimized for lossy wireless networks
congestion = "bbr"

# Default queuing discipline:
#   - "fq"       : Fair Queuing (Required for optimal BBR performance)
#   - "cake"     : Common Applications Kept Enhanced (Best anti-bufferbloat for gaming routers)
#   - "fq_codel" : Fair Queuing Controlled Delay
#   - "fq_pie"   : Proportional Integral controller Enhanced
#   - "pfifo_fast": Traditional FIFO queue
qdisc = "fq"

# Multipath TCP support (allows bonding Wi-Fi + Ethernet concurrently)
mptcp = true

# Legacy /proc/net/ip_conntrack exposure
nf_conntrack_procfs = false


[modules]
# LocalModConfig module pruning strategy:
#   - "strict"   : Prunes ALL modules not recorded in ~/.config/modprobed.db.
#                  -> Produces the smallest, fastest-compiling kernel (~300 modules).
#   - "expanded" : Uses modprobed-db PLUS an intelligent safety net (LMC_KEEP).
#                  -> Preserves USB, GPU, sound, NVMe, Wi-Fi, and filesystems even if unplugged.
#                  -> Best balance of small size and hot-plug safety (~1,200 modules).
mode = "expanded"

# Use ~/.config/modprobed.db as the hardware profile for module pruning
modprobed_db = true

# Extra subsystem directories to always preserve during 'expanded' mode pruning
lmc_keep_extra = []

# Automatically installs and enables the hourly systemd timer to update modprobed.db
manage_service = true

# Force cryptographic signing on all kernel modules (Leave false to allow DKMS drivers like NVIDIA/ZFS)
sig_force = false
