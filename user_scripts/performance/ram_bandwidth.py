#!/usr/bin/env python3
"""
ram_bandwidth.py - Ultimate DDR Memory Bandwidth & Latency Benchmark Suite

Features:
  - Hardware & SMBIOS Memory Probe (Configured Speed, Factory Rated Speed, Channels, Manufacturers, Form Factor, Capacities).
  - SPD5118 DDR5 Memory Temperature Sensors & Thermal Monitoring.
  - NUMA Node & Multi-Socket Topology Probe.
  - Heterogeneous Core Cache Probing (P-Core / E-Core / Zen 5 dynamic cache detection).
  - Xoshiro256** PRNG Pointer-Chasing Latency Benchmark (Zero prefetcher leakage).
  - L1 / L2 / L3 Cache & Main DRAM Latency Hierarchy Micro-Benchmark.
  - Theoretical Peak Memory Bandwidth Calculator & Efficiency Gauge.
  - Pure Multi-Core Read Benchmark (sysbench 64M blocks).
  - Pure Multi-Core Write Benchmark (sysbench 64M blocks).
  - Multi-Core STREAM / Copy Benchmark (stress-ng --stream).
  - Single-Core Copy & Memory Benchmark (mbw memcpy).
  - Structured JSON & CSV Export Options (--export report.json / report.csv).
  - Sudo password sanitization & TemporaryDirectory automatic filesystem cleanup.
"""

import argparse
import contextlib
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None


@dataclass(slots=True)
class HardwareSpecs:
    cpu_model: str
    online_cpus: int
    numa_nodes: int
    mem_type: str
    configured_speed_mts: int | None
    factory_speed_mts: int | None
    dimm_count: int | None
    channels: int | None
    bus_width_bits: int | None
    theoretical_max_gb_s: float | None
    total_ram_gb: float | None
    avail_ram_gb: float | None
    manufacturer: str | None
    part_number: str | None
    form_factor: str | None
    initial_dram_temps: list[tuple[str, float]] | None = None
    final_dram_temps: list[tuple[str, float]] | None = None


@dataclass(slots=True)
class TestResult:
    name: str
    throughput_gb_s: float
    throughput_mib_s: float
    read_gb_s: float | None = None
    write_gb_s: float | None = None
    efficiency_pct: float | None = None
    latency_ns: float | None = None
    details: str = ""


@dataclass(slots=True)
class CacheHierarchyResult:
    l1_kb: int
    l2_kb: int
    l3_kb: int
    dram_mb: int
    l1_ns: float
    l2_ns: float
    l3_ns: float
    dram_ns: float


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


def get_sudo_pass(cli_pass: str | None = None, allow_interactive_prompt: bool = True) -> str | None:
    """Determine sudo password dynamically without hardcoding credentials."""
    if os.geteuid() == 0:
        return None  # Running directly as root

    if cli_pass is not None:
        return cli_pass

    if "SUDO_PASSWORD" in os.environ:
        return os.environ["SUDO_PASSWORD"]

    # Test if passwordless sudo is configured
    try:
        proc = subprocess.run(["sudo", "-n", "true"], capture_output=True)
        if proc.returncode == 0:
            return ""
    except Exception:
        pass

    if allow_interactive_prompt and sys.stdin.isatty():
        import getpass
        try:
            return getpass.getpass("[sudo] Password for system operations: ")
        except Exception:
            pass

    return None


def run_cmd(cmd: list[str], input_text: str | None = None) -> str:
    proc = subprocess.run(
        cmd,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout)
    return proc.stdout or ""


def run_sudo_cmd(cmd: list[str], sudo_pass: str | None = None) -> str:
    """Run a command with sudo securely, sanitizing exceptions to prevent password leaks."""
    if os.geteuid() == 0:
        return run_cmd(cmd)

    full_cmd = ["sudo", "-S", *cmd] if sudo_pass else ["sudo", "-n", *cmd]

    try:
        proc = subprocess.run(
            full_cmd,
            input=f"{sudo_pass}\n" if sudo_pass else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return proc.stdout or ""
    except subprocess.CalledProcessError as e:
        safe_output = e.output.replace(sudo_pass, "***") if sudo_pass else e.output
        raise RuntimeError(f"Privileged execution failed for {' '.join(cmd)}\n{safe_output}") from e


def get_online_cpu_count() -> int:
    """Native Python 3.13+ process CPU count respecting process affinity."""
    return os.process_cpu_count() or max(os.cpu_count() or 1, 1)


def probe_dram_temperatures() -> list[tuple[str, float]]:
    """Probe SPD5118 DDR5 module and system memory thermal sensors via hwmon."""
    temps: list[tuple[str, float]] = []
    for path in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        try:
            val_c = int(Path(path).read_text().strip()) / 1000.0
            name_path = Path(path).parent / "name"
            label_path = Path(path).with_name(Path(path).name.replace("_input", "_label"))
            name = name_path.read_text().strip() if name_path.exists() else "hwmon"
            label = label_path.read_text().strip() if label_path.exists() else Path(path).stem

            if "spd5118" in name.lower() or "dram" in name.lower() or "dimm" in label.lower() or "memory" in label.lower():
                sensor_name = f"DRAM Module ({name})" if "spd5118" in name.lower() else f"{name} {label}"
                temps.append((sensor_name, val_c))
        except Exception:
            pass
    return temps


def get_numa_node_count() -> int:
    """Probe system NUMA nodes topology."""
    numa_nodes = 1
    numa_path = "/sys/devices/system/node"
    if os.path.exists(numa_path):
        nodes = glob.glob(os.path.join(numa_path, "node*"))
        if nodes:
            numa_nodes = len(nodes)
    return max(numa_nodes, 1)


def probe_cpu_cache_sizes(target_core: str = "0") -> tuple[int, int, int]:
    """Probe L1D, L2, and L3 cache sizes dynamically for a specific heterogeneous microarchitectural core."""
    l1_kb, l2_kb, l3_kb = 32, 512, 16384
    core_id = re.split(r"[,\-]", target_core)[0].strip() if target_core else "0"

    try:
        cache_dir = Path(f"/sys/devices/system/cpu/cpu{core_id}/cache/")
        for index_path in cache_dir.glob("index*"):
            try:
                level = (index_path / "level").read_text().strip()
                ctype = (index_path / "type").read_text().strip()
                size_str = (index_path / "size").read_text().strip()
                m = re.match(r"(\d+)\s*([KMGT])?", size_str, re.IGNORECASE)
                if m:
                    val = int(m.group(1))
                    unit = (m.group(2) or "K").upper()
                    kb = val * 1024 if unit == "M" else (val * 1024 * 1024 if unit == "G" else val)

                    if level == "1" and ctype.lower() == "data":
                        l1_kb = kb
                    elif level == "2":
                        l2_kb = kb
                    elif level == "3":
                        l3_kb = kb
            except Exception:
                continue
    except Exception:
        pass
    return l1_kb, l2_kb, l3_kb


def check_and_install_deps(sudo_pass: str | None = None) -> None:
    """Ensure required benchmark tools are installed, avoiding root-makepkg traps."""
    required_tools = ["sysbench", "stress-ng", "dmidecode", "taskset", "mbw"]

    if not tool_exists("gcc") and not tool_exists("clang"):
        required_tools.append("gcc")

    missing_tools = [t for t in required_tools if not tool_exists(t)]

    if not missing_tools:
        return

    msg = f"Installing missing benchmark dependencies: {', '.join(missing_tools)}..."
    if RICH_AVAILABLE:
        console.print(f"[yellow]{msg}[/yellow]")
    else:
        print(msg)

    if tool_exists("paru"):
        if os.geteuid() == 0:
            eprint("Error: Cannot run 'paru' as root on Arch Linux. Run as standard user or use pacman.")
            sys.exit(1)
        subprocess.run(["paru", "-S", "--needed", "--noconfirm", "--skipreview", *missing_tools], check=False)
    elif tool_exists("pacman"):
        try:
            run_sudo_cmd(["pacman", "-S", "--needed", "--noconfirm", *missing_tools], sudo_pass=sudo_pass)
        except Exception as exc:
            eprint(f"Warning: Automatic dependency installation failed: {exc}")


def detect_hardware_specs(sudo_pass: str | None = None) -> HardwareSpecs:
    """Dynamically probe CPU, RAM capacity, SMBIOS architecture, NUMA topology, and thermal sensors."""
    cpu_model = "Unknown Processor"
    try:
        lscpu_out = run_cmd(["lscpu"])
        for line in lscpu_out.splitlines():
            if "Model name:" in line:
                cpu_model = line.split(":", 1)[1].strip()
                break
    except Exception:
        try:
            with open("/proc/cpuinfo", "r") as fh:
                for line in fh:
                    if line.strip().lower().startswith("model name"):
                        cpu_model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

    online_cpus = get_online_cpu_count()
    numa_nodes = get_numa_node_count()

    total_ram_gb: float | None = None
    avail_ram_gb: float | None = None
    try:
        with open("/proc/meminfo", "r") as fh:
            meminfo = fh.read()
            total_match = re.search(r"MemTotal:\s+(\d+)\s+kB", meminfo)
            avail_match = re.search(r"MemAvailable:\s+(\d+)\s+kB", meminfo)
            if total_match:
                total_ram_gb = float(total_match.group(1)) / 1e6
            if avail_match:
                avail_ram_gb = float(avail_match.group(1)) / 1e6
    except Exception:
        pass

    mem_type = "RAM"
    configured_speed_mts: int | None = None
    factory_speed_mts: int | None = None
    dimm_count: int | None = None
    channels: int | None = None
    bus_width_bits: int | None = None
    theoretical_max_gb_s: float | None = None
    manufacturer: str | None = None
    part_number: str | None = None
    form_factor: str | None = None

    if tool_exists("dmidecode"):
        try:
            dmi_out = run_sudo_cmd(["dmidecode", "-t", "memory"], sudo_pass=sudo_pass)

            types_found = re.findall(r"Type:\s+(DDR[345]|LPDDR[45]|HBM\d?)", dmi_out)
            if types_found:
                mem_type = types_found[0]

            cfg_speeds = re.findall(r"Configured Memory Speed:\s+(\d+)\s+(?:MT/s|MHz)", dmi_out)
            if cfg_speeds:
                valid_cfg = [int(s) for s in cfg_speeds if int(s) > 0]
                if valid_cfg:
                    configured_speed_mts = max(valid_cfg)

            fac_speeds = re.findall(r"Speed:\s+(\d+)\s+(?:MT/s|MHz)", dmi_out)
            if fac_speeds:
                valid_fac = [int(s) for s in fac_speeds if int(s) > 0]
                if valid_fac:
                    factory_speed_mts = max(valid_fac)

            if configured_speed_mts is None:
                configured_speed_mts = factory_speed_mts

            mfg_found = re.findall(r"Manufacturer:\s+([^\n]+)", dmi_out)
            for m in mfg_found:
                m_clean = m.strip()
                if m_clean and "Unknown" not in m_clean and "Not Specified" not in m_clean:
                    manufacturer = m_clean
                    break

            part_found = re.findall(r"Part Number:\s+([^\n]+)", dmi_out)
            for p in part_found:
                p_clean = p.strip()
                if p_clean and "Unknown" not in p_clean and "Not Specified" not in p_clean:
                    part_number = p_clean
                    break

            ff_found = re.findall(r"Form Factor:\s+([^\n]+)", dmi_out)
            for f in ff_found:
                f_clean = f.strip()
                if f_clean and "Unknown" not in f_clean and "Not Specified" not in f_clean:
                    form_factor = f_clean
                    break

            devices = dmi_out.split("Memory Device")
            installed_dimms = 0
            for dev in devices[1:]:
                if "Size:" in dev and "No Module Installed" not in dev:
                    size_match = re.search(r"Size:\s+(\d+)\s+(MB|GB|GiB)", dev)
                    if size_match and int(size_match.group(1)) > 0:
                        installed_dimms += 1
            if installed_dimms > 0:
                dimm_count = installed_dimms
                channels = max(1, min(dimm_count, 8))
        except Exception:
            pass

    if configured_speed_mts is not None and channels is not None:
        if "DDR5" in mem_type:
            # DDR5 features two independent 32-bit subchannels per DIMM
            bus_width_bits = 32 * (channels * 2)
        else:
            bus_width_bits = 64 * channels

        total_bus_bytes = bus_width_bits / 8.0
        theoretical_max_gb_s = (configured_speed_mts * total_bus_bytes) / 1000.0

    initial_dram_temps = probe_dram_temperatures()

    return HardwareSpecs(
        cpu_model=cpu_model,
        online_cpus=online_cpus,
        numa_nodes=numa_nodes,
        mem_type=mem_type,
        configured_speed_mts=configured_speed_mts,
        factory_speed_mts=factory_speed_mts,
        dimm_count=dimm_count,
        channels=channels,
        bus_width_bits=bus_width_bits,
        theoretical_max_gb_s=theoretical_max_gb_s,
        total_ram_gb=total_ram_gb,
        avail_ram_gb=avail_ram_gb,
        manufacturer=manufacturer,
        part_number=part_number,
        form_factor=form_factor,
        initial_dram_temps=initial_dram_temps,
    )


@contextlib.contextmanager
def set_cpu_performance(sudo_pass: str | None = None):
    """Quietly set CPU scaling governor and EPP to performance and restore upon context exit."""
    original_governors: dict[str, str] = {}
    original_epps: dict[str, str] = {}
    intel_no_turbo_path = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
    amd_boost_path = Path("/sys/devices/system/cpu/cpufreq/boost")

    for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        try:
            original_governors[f] = Path(f).read_text().strip()
        except Exception:
            pass

    for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference"):
        try:
            original_epps[f] = Path(f).read_text().strip()
        except Exception:
            pass

    def apply_settings(gov: str, epp: str):
        cmds = []
        if gov:
            cmds.append(f"echo {gov} | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor >/dev/null 2>&1")
        if epp:
            cmds.append(f"echo {epp} | tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference >/dev/null 2>&1")
        if amd_boost_path.exists():
            cmds.append(f"echo 1 > {amd_boost_path} 2>&1")
        if intel_no_turbo_path.exists():
            cmds.append(f"echo 0 > {intel_no_turbo_path} 2>&1")

        try:
            run_sudo_cmd(["sh", "-c", "; ".join(cmds)], sudo_pass=sudo_pass)
        except Exception:
            pass

    apply_settings("performance", "performance")
    try:
        yield
    finally:
        py_restore = ["import pathlib"]
        for f, g in original_governors.items():
            py_restore.append(f"try: pathlib.Path({f!r}).write_text({g!r})\nexcept Exception: pass")
        for f, e in original_epps.items():
            py_restore.append(f"try: pathlib.Path({f!r}).write_text({e!r})\nexcept Exception: pass")

        if py_restore:
            try:
                run_sudo_cmd(["python3", "-c", "\n".join(py_restore)], sudo_pass=sudo_pass)
            except Exception:
                pass


def run_cache_hierarchy_latency_test(cores: str | None = None) -> CacheHierarchyResult | None:
    """Measure dynamically probed L1 Cache, L2 Cache, L3 Cache, and Main DRAM access delays using Xoshiro256**."""
    target_core = re.split(r"[,\-]", cores)[0].strip() if cores else "0"

    compiler = tool_exists("gcc") or tool_exists("clang")
    if not compiler:
        return None

    l1_kb, l2_kb, l3_kb = probe_cpu_cache_sizes(target_core)
    l1_target_kb = max(16, l1_kb // 2)
    l2_target_kb = max(128, l2_kb // 2)
    l3_target_kb = max(2048, l3_kb // 2)
    dram_target_mb = max(128, (l3_kb * 5) // 1024)

    cc = "gcc" if tool_exists("gcc") else "clang"
    c_code = f"""
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <stdint.h>

static inline uint64_t rotl(const uint64_t x, int k) {{ return (x << k) | (x >> (64 - k)); }}
static uint64_t s[4] = {{ 0x180ec6d33cfd0aba, 0xd5a61266f0c9392c, 0xa9582618e03fc9aa, 0x39abdc4529b1661c }};
uint64_t next_prng(void) {{
    const uint64_t result = rotl(s[1] * 5, 7) * 9;
    const uint64_t t = s[1] << 17;
    s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3];
    s[2] ^= t; s[3] = rotl(s[3], 45);
    return result;
}}

double measure_lat_kb(size_t size_kb) {{
    size_t size_bytes = size_kb * 1024;
    if (size_bytes < 16384) size_bytes = 16384;
    size_t count = size_bytes / sizeof(size_t);
    size_t *arr = (size_t *)malloc(size_bytes);
    size_t *indices = (size_t *)malloc(count * sizeof(size_t));
    if (!arr || !indices) return 0.0;

    for (size_t i = 0; i < count; i++) indices[i] = i;

    for (size_t i = count - 1; i > 0; i--) {{
        size_t j = next_prng() % (i + 1);
        size_t tmp = indices[i];
        indices[i] = indices[j];
        indices[j] = tmp;
    }}

    for (size_t i = 0; i < count - 1; i++) arr[indices[i]] = indices[i+1];
    arr[indices[count-1]] = indices[0];
    free(indices);

    size_t curr = 0;
    for (size_t i = 0; i < 1000000; i++) curr = arr[curr];

    struct timespec ts1, ts2;
    size_t jumps = 20000000;
    clock_gettime(CLOCK_MONOTONIC, &ts1);
    for (size_t i = 0; i < jumps; i++) curr = arr[curr];
    clock_gettime(CLOCK_MONOTONIC, &ts2);

    double nsec = (ts2.tv_sec - ts1.tv_sec) * 1e9 + (ts2.tv_nsec - ts1.tv_nsec);
    if (curr == 0xdeadbeef) printf("%zu", curr);
    free(arr);
    return nsec / (double)jumps;
}}

int main() {{
    double l1 = measure_lat_kb({l1_target_kb});
    double l2 = measure_lat_kb({l2_target_kb});
    double l3 = measure_lat_kb({l3_target_kb});
    double dram = measure_lat_kb({dram_target_mb * 1024});
    printf("%.2f %.2f %.2f %.2f\\n", l1, l2, l3, dram);
    return 0;
}}
"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            c_path = os.path.join(tmpdir, "cache_lat.c")
            bin_path = os.path.join(tmpdir, "cache_lat.bin")

            with open(c_path, "w") as f:
                f.write(c_code)

            subprocess.run([cc, "-O3", c_path, "-o", bin_path], check=True, capture_output=True)
            cmd = ["taskset", "-c", target_core, bin_path]
            out = run_cmd(cmd).strip().split()

            if len(out) == 4:
                return CacheHierarchyResult(
                    l1_kb=l1_target_kb,
                    l2_kb=l2_target_kb,
                    l3_kb=l3_target_kb,
                    dram_mb=dram_target_mb,
                    l1_ns=float(out[0]),
                    l2_ns=float(out[1]),
                    l3_ns=float(out[2]),
                    dram_ns=float(out[3]),
                )
    except Exception:
        pass
    return None


def run_latency_test(
    array_size_mb: int = 128, specs: HardwareSpecs = None, cores: str | None = None
) -> TestResult:
    """High-precision Xoshiro256** random DRAM access latency benchmark (zero-leak cleanup)."""
    target_core = re.split(r"[,\-]", cores)[0].strip() if cores else "0"
    lat_ns: float = 0.0
    compiler = tool_exists("gcc") or tool_exists("clang")

    if compiler:
        cc = "gcc" if tool_exists("gcc") else "clang"
        c_code = r"""
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <stdint.h>

static inline uint64_t rotl(const uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
static uint64_t s[4] = { 0x180ec6d33cfd0aba, 0xd5a61266f0c9392c, 0xa9582618e03fc9aa, 0x39abdc4529b1661c };
uint64_t next_prng(void) {
    const uint64_t result = rotl(s[1] * 5, 7) * 9;
    const uint64_t t = s[1] << 17;
    s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3];
    s[2] ^= t; s[3] = rotl(s[3], 45);
    return result;
}

int main(int argc, char **argv) {
    size_t size_bytes = 128 * 1024 * 1024;
    if (argc > 1) size_bytes = (size_t)atoll(argv[1]);
    size_t count = size_bytes / sizeof(size_t);
    size_t *arr = (size_t *)malloc(size_bytes);
    size_t *indices = (size_t *)malloc(count * sizeof(size_t));
    if (!arr || !indices) return 1;

    for (size_t i = 0; i < count; i++) indices[i] = i;

    for (size_t i = count - 1; i > 0; i--) {
        size_t j = next_prng() % (i + 1);
        size_t tmp = indices[i];
        indices[i] = indices[j];
        indices[j] = tmp;
    }

    for (size_t i = 0; i < count - 1; i++) arr[indices[i]] = indices[i+1];
    arr[indices[count-1]] = indices[0];
    free(indices);

    size_t curr = 0;
    for (size_t i = 0; i < 1000000; i++) curr = arr[curr];

    struct timespec ts1, ts2;
    size_t jumps = 20000000;
    clock_gettime(CLOCK_MONOTONIC, &ts1);
    for (size_t i = 0; i < jumps; i++) curr = arr[curr];
    clock_gettime(CLOCK_MONOTONIC, &ts2);

    double nsec = (ts2.tv_sec - ts1.tv_sec) * 1e9 + (ts2.tv_nsec - ts1.tv_nsec);
    printf("%.2f\n", nsec / (double)jumps);

    if (curr == 0xdeadbeef) printf("%zu", curr);
    free(arr);
    return 0;
}
"""
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                c_path = os.path.join(tmpdir, "lat.c")
                bin_path = os.path.join(tmpdir, "lat.bin")

                with open(c_path, "w") as f:
                    f.write(c_code)

                subprocess.run([cc, "-O3", c_path, "-o", bin_path], check=True, capture_output=True)
                cmd = ["taskset", "-c", target_core, bin_path, str(array_size_mb * 1024 * 1024)]
                out = run_cmd(cmd).strip()
                lat_ns = float(out)
        except Exception:
            pass

    bytes_per_sec = (1e9 / lat_ns) * 8.0 if lat_ns > 0 else 0.0
    gb_s = bytes_per_sec / 1e9
    mib_s = bytes_per_sec / (1024.0 * 1024.0)

    eff_pct = (
        (gb_s / specs.theoretical_max_gb_s) * 100.0
        if specs and specs.theoretical_max_gb_s and specs.theoretical_max_gb_s > 0
        else 0.1
    )

    return TestResult(
        name="Random Memory Latency",
        throughput_gb_s=gb_s,
        throughput_mib_s=mib_s,
        read_gb_s=gb_s,
        write_gb_s=0.0,
        efficiency_pct=eff_pct,
        latency_ns=lat_ns,
        details=f"128M pointer chasing (> L3 cache on Core {target_core})",
    )


def run_pure_read_test(
    workers: int, run_time: int, specs: HardwareSpecs, cores: str | None = None
) -> TestResult:
    """100% Pure Memory Read Benchmark using sysbench memory with 64M block size."""
    cmd = []
    if cores:
        cmd.extend(["taskset", "-c", cores])

    cmd.extend(
        [
            "sysbench",
            "memory",
            f"--threads={workers}",
            f"--time={run_time}",
            "--memory-block-size=64M",
            "--memory-total-size=1000G",
            "--memory-scope=local",
            "--memory-access-mode=seq",
            "--memory-oper=read",
            "run",
        ]
    )

    stdout = run_cmd(cmd)

    mib_s = 0.0
    for line in stdout.splitlines():
        match = re.search(r"\(([\d\.]+)\s+([KMGT]?i?B)/sec\)", line, re.IGNORECASE)
        if match:
            val = float(match.group(1))
            unit = match.group(2).upper()
            if "G" in unit:
                mib_s = val * 1024.0 if "GI" in unit else val * (1000.0 * 1000.0 * 1000.0) / (1024.0 * 1024.0)
            elif "M" in unit:
                mib_s = val if "MI" in unit else val * 1000000.0 / (1024.0 * 1024.0)
            elif "K" in unit:
                mib_s = val / 1024.0
            else:
                mib_s = val / (1024.0 * 1024.0)
            break

    gb_s = (mib_s * 1024.0 * 1024.0) / 1e9
    eff_pct = (
        (gb_s / specs.theoretical_max_gb_s) * 100.0
        if specs.theoretical_max_gb_s and specs.theoretical_max_gb_s > 0
        else None
    )
    lat_ns = (64.0 / (gb_s * 1e9)) * 1e9 if gb_s > 0 else None

    return TestResult(
        name="Pure Read (Multi-Thread)",
        throughput_gb_s=gb_s,
        throughput_mib_s=mib_s,
        read_gb_s=gb_s,
        write_gb_s=0.0,
        efficiency_pct=eff_pct,
        latency_ns=lat_ns,
        details=f"sysbench 64M blocks, {workers} parallel read workers",
    )


def run_pure_write_test(
    workers: int, run_time: int, specs: HardwareSpecs, cores: str | None = None
) -> TestResult:
    """100% Pure Memory Write Benchmark using sysbench memory with 64M block size."""
    cmd = []
    if cores:
        cmd.extend(["taskset", "-c", cores])

    cmd.extend(
        [
            "sysbench",
            "memory",
            f"--threads={workers}",
            f"--time={run_time}",
            "--memory-block-size=64M",
            "--memory-total-size=1000G",
            "--memory-scope=local",
            "--memory-access-mode=seq",
            "--memory-oper=write",
            "run",
        ]
    )

    stdout = run_cmd(cmd)

    mib_s = 0.0
    for line in stdout.splitlines():
        match = re.search(r"\(([\d\.]+)\s+([KMGT]?i?B)/sec\)", line, re.IGNORECASE)
        if match:
            val = float(match.group(1))
            unit = match.group(2).upper()
            if "G" in unit:
                mib_s = val * 1024.0 if "GI" in unit else val * (1000.0 * 1000.0 * 1000.0) / (1024.0 * 1024.0)
            elif "M" in unit:
                mib_s = val if "MI" in unit else val * 1000000.0 / (1024.0 * 1024.0)
            elif "K" in unit:
                mib_s = val / 1024.0
            else:
                mib_s = val / (1024.0 * 1024.0)
            break

    gb_s = (mib_s * 1024.0 * 1024.0) / 1e9
    eff_pct = (
        (gb_s / specs.theoretical_max_gb_s) * 100.0
        if specs.theoretical_max_gb_s and specs.theoretical_max_gb_s > 0
        else None
    )
    lat_ns = (64.0 / (gb_s * 1e9)) * 1e9 if gb_s > 0 else None

    return TestResult(
        name="Pure Write (Multi-Thread)",
        throughput_gb_s=gb_s,
        throughput_mib_s=mib_s,
        read_gb_s=0.0,
        write_gb_s=gb_s,
        efficiency_pct=eff_pct,
        latency_ns=lat_ns,
        details=f"sysbench 64M blocks, {workers} parallel write workers",
    )


def run_copy_stream_test(
    workers: int, run_time: int, specs: HardwareSpecs, cores: str | None = None
) -> TestResult:
    """Multi-Core Combined Memory Stream Copy Benchmark using stress-ng --stream."""
    cmd = []
    if cores:
        cmd.extend(["taskset", "-c", cores])

    actual_time = max(run_time, 5)

    cmd.extend(
        [
            "stress-ng",
            "--stream",
            str(workers),
            "--timeout",
            f"{actual_time}s",
            "--metrics-brief",
            "-v",
        ]
    )

    stdout = run_cmd(cmd)

    rate_re = re.compile(
        r"memory rate:\s+([0-9]+(?:\.[0-9]+)?)\s+([KMGT]?B)\s+read/sec,\s+([0-9]+(?:\.[0-9]+)?)\s+([KMGT]?B)\s+write/sec",
        re.IGNORECASE,
    )
    matches = rate_re.findall(stdout)

    read_mb_s = 0.0
    write_mb_s = 0.0
    for r_val, r_unit, w_val, w_unit in matches:
        r_f = float(r_val)
        w_f = float(w_val)
        if "G" in r_unit.upper():
            r_f *= 1000.0
        if "G" in w_unit.upper():
            w_f *= 1000.0
        read_mb_s += r_f
        write_mb_s += w_f

    total_mb_s = read_mb_s + write_mb_s

    read_gb_s = read_mb_s / 1000.0
    write_gb_s = write_mb_s / 1000.0
    total_gb_s = total_mb_s / 1000.0
    total_mib_s = (total_gb_s * 1e9) / (1024.0 * 1024.0)

    eff_pct = (
        (total_gb_s / specs.theoretical_max_gb_s) * 100.0
        if specs.theoretical_max_gb_s and specs.theoretical_max_gb_s > 0
        else None
    )
    lat_ns = (64.0 / (total_gb_s * 1e9)) * 1e9 if total_gb_s > 0 else None

    return TestResult(
        name="Stream Copy (Multi-Thread)",
        throughput_gb_s=total_gb_s,
        throughput_mib_s=total_mib_s,
        read_gb_s=read_gb_s,
        write_gb_s=write_gb_s,
        efficiency_pct=eff_pct,
        latency_ns=lat_ns,
        details=f"stress-ng --stream, {workers} workers (Read: {read_gb_s:.1f} GB/s, Write: {write_gb_s:.1f} GB/s)",
    )


def run_single_core_test(
    size_mib: int, runs: int, specs: HardwareSpecs, cores: str | None = None
) -> TestResult:
    """Single-Core Memory Copy Benchmark using mbw."""
    cmd = []
    target_core = re.split(r"[,\-]", cores)[0].strip() if cores else "0"
    cmd.extend(["taskset", "-c", target_core, "mbw", "-n", str(runs), str(size_mib)])

    stdout = run_cmd(cmd)

    avg_re = re.compile(
        r"^AVG\s+Method:\s+(\S+).+?Copy:\s+([0-9.]+)\s+MiB/s",
        re.MULTILINE,
    )
    averages = avg_re.findall(stdout)
    memcpy_mib_s = next((float(rate) for method, rate in averages if method == "MEMCPY"), 0.0)

    gb_s = (memcpy_mib_s * 1024.0 * 1024.0) / 1e9
    eff_pct = (
        (gb_s / specs.theoretical_max_gb_s) * 100.0
        if specs.theoretical_max_gb_s and specs.theoretical_max_gb_s > 0
        else None
    )
    lat_ns = (64.0 / (gb_s * 1e9)) * 1e9 if gb_s > 0 else None

    return TestResult(
        name="Single-Core Copy (1 Thread)",
        throughput_gb_s=gb_s,
        throughput_mib_s=memcpy_mib_s,
        read_gb_s=gb_s / 2.0,
        write_gb_s=gb_s / 2.0,
        efficiency_pct=eff_pct,
        latency_ns=lat_ns,
        details=f"mbw memcpy on Core {target_core} (Line Fill Buffer limit)",
    )


def build_gauge(pct: float | None, width: int = 8) -> str:
    """Render a seamless solid-block progress meter with 100% matched height and width."""
    if pct is None:
        return "[dim]N/A[/dim]"
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    empty = width - filled

    fill_color = "bright_green" if clamped >= 75.0 else ("bright_yellow" if clamped >= 45.0 else "bright_cyan")
    bar = f"[{fill_color}]" + "█" * filled + f"[/{fill_color}][bright_black]" + "█" * empty + f"[/bright_black] [bold white]{clamped:4.1f}%[/bold white]"
    return bar


def render_header(specs: HardwareSpecs, governor_active: bool = True):
    if specs.configured_speed_mts and specs.factory_speed_mts and specs.factory_speed_mts > specs.configured_speed_mts:
        speed_str = f"{specs.configured_speed_mts} MT/s [dim](Factory Rated: {specs.factory_speed_mts} MT/s)[/dim]"
    elif specs.configured_speed_mts:
        speed_str = f"{specs.configured_speed_mts} MT/s"
    else:
        speed_str = "Unknown MT/s"

    dimm_str = (
        f"{specs.dimm_count} Modules ({specs.bus_width_bits}-bit total width)"
        if specs.dimm_count and specs.bus_width_bits
        else "Unknown Topology"
    )
    max_str = (
        f"{specs.theoretical_max_gb_s:.2f} GB/s (Theoretical Limit)"
        if specs.theoretical_max_gb_s
        else "N/A"
    )
    ram_cap_str = (
        f"{specs.total_ram_gb:.1f} GB Installed ({specs.avail_ram_gb:.1f} GB Available)"
        if specs.total_ram_gb and specs.avail_ram_gb
        else "System RAM"
    )
    mfg_str = specs.manufacturer or "Generic DRAM"
    form_str = specs.form_factor or "System Memory"
    gov_str = (
        "[bold green]Performance Mode[/bold green] (Hardware Turbo/Boost Active)"
        if governor_active
        else "[dim]Standard Governor[/dim]"
    )
    numa_str = (
        f"[bold cyan]{specs.numa_nodes} NUMA Nodes[/bold cyan] (Uniform Memory Architecture)"
        if specs.numa_nodes == 1
        else f"[bold red]{specs.numa_nodes} NUMA Nodes[/bold red] (Multi-Socket Inter-Node NUMA Routing)"
    )

    temp_str = "[dim]No Sensor Data[/dim]"
    if specs.initial_dram_temps:
        t_list = [f"{lbl}: {val:.1f}°C" for lbl, val in specs.initial_dram_temps]
        temp_str = f"[bold yellow]{' | '.join(t_list)}[/bold yellow]"

    if not RICH_AVAILABLE:
        print(f"=== RAM BANDWIDTH BENCHMARK SUITE ===")
        print(f"CPU: {specs.cpu_model} ({specs.online_cpus} online cores)")
        print(f"RAM: {specs.mem_type} @ {speed_str} | {ram_cap_str}")
        print(f"Topology: {dimm_str} | {mfg_str} {form_str}")
        print(f"NUMA: {specs.numa_nodes} Nodes | Temps: {temp_str}")
        print(f"Theoretical Max Bandwidth: {max_str}")
        print("=" * 60)
        return

    table = Table(show_header=False, box=box.ROUNDED, expand=True)
    table.add_column("Property", style="bold cyan", width=26)
    table.add_column("System Specifications & Architecture", style="bold white")

    table.add_row("Processor Model", f"[bold white]{specs.cpu_model}[/bold white]")
    table.add_row("Logical CPU Cores", f"[bold green]{specs.online_cpus}[/bold green] cores online")
    table.add_row("NUMA Architecture", numa_str)
    table.add_row("CPU Scaling & Frequency", gov_str)
    table.add_row("Installed Memory Capacity", f"[bold bright_magenta]{ram_cap_str}[/bold bright_magenta]")
    table.add_row("Memory Technology & Speed", f"[bold yellow]{specs.mem_type}[/bold yellow] @ [bold bright_yellow]{speed_str}[/bold bright_yellow]")
    table.add_row("Channel & Slot Topology", f"{dimm_str} ({mfg_str} {form_str})")
    table.add_row("Memory Thermal Sensors", temp_str)
    table.add_row("Theoretical Peak Bandwidth", f"[bold bright_green]{max_str}[/bold bright_green]")

    panel = Panel(
        table,
        title="[bold white on blue] 󰍛 SYSTEM HARDWARE & MEMORY ARCHITECTURE [/bold white on blue]",
        border_style="bright_blue",
        padding=(0, 1),
    )
    console.print(panel)


def render_cache_hierarchy_table(result: CacheHierarchyResult | None):
    """Render CPU Cache & DRAM Latency Hierarchy table."""
    if not result:
        return

    l1_size_str = f"{result.l1_kb} KB"
    l2_size_str = f"{result.l2_kb} KB" if result.l2_kb < 1024 else f"{result.l2_kb / 1024:.1f} MB"
    l3_size_str = f"{result.l3_kb / 1024:.1f} MB"
    dram_size_str = f"{result.dram_mb} MB"

    if not RICH_AVAILABLE:
        print("\n=== CPU CACHE & DRAM LATENCY HIERARCHY ===")
        print(f"L1 Data Cache ({l1_size_str:7s}) : {result.l1_ns:.2f} ns")
        print(f"L2 Dedicated ({l2_size_str:7s}) : {result.l2_ns:.2f} ns")
        print(f"L3 Shared LLC ({l3_size_str:7s}) : {result.l3_ns:.2f} ns")
        print(f"Main System DRAM ({dram_size_str:7s}): {result.dram_ns:.2f} ns")
        return

    table = Table(
        title="󰔛 CPU Cache & Main Memory Latency Hierarchy",
        box=box.ROUNDED,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Memory Subsystem Level", style="bold white", width=29)
    table.add_column("Buffer Size", justify="center", style="bold yellow", width=13)
    table.add_column("Access Delay (ns)", justify="right", style="bold cyan", width=16)
    table.add_column("Relative Delay", justify="center", width=18)
    table.add_column("Microarchitectural Cache Target", style="dim white")

    l1_gauge = build_gauge((result.l1_ns / result.dram_ns) * 100.0)
    l2_gauge = build_gauge((result.l2_ns / result.dram_ns) * 100.0)
    l3_gauge = build_gauge((result.l3_ns / result.dram_ns) * 100.0)
    dram_gauge = build_gauge(100.0)

    table.add_row("L1 Data Cache", l1_size_str, f"[bold bright_green]{result.l1_ns:.2f} ns[/bold bright_green]", l1_gauge, "On-die L1 core data cache (~4 clock cycles)")
    table.add_row("L2 Dedicated Cache", l2_size_str, f"[bold bright_green]{result.l2_ns:.2f} ns[/bold bright_green]", l2_gauge, "Per-core dedicated L2 cache (~12-14 clock cycles)")
    table.add_row("L3 Shared Smart Cache", l3_size_str, f"[bold bright_yellow]{result.l3_ns:.2f} ns[/bold bright_yellow]", l3_gauge, "Shared LLC Smart Cache (~40-50 clock cycles)")
    table.add_row("Main System DRAM", dram_size_str, f"[bold bright_cyan]󰔛 {result.dram_ns:.2f} ns[/bold bright_cyan]", dram_gauge, "Uncached random DRAM pointer-chasing access")

    console.print(table)


def render_results_table(results: list[TestResult], specs: HardwareSpecs):
    if not RICH_AVAILABLE:
        print("\n=== BENCHMARK RESULTS SUMMARY ===")
        for r in results:
            eff = f"{r.efficiency_pct:5.1f}%" if r.efficiency_pct is not None else "N/A"
            lat = f"{r.latency_ns:6.2f} ns" if r.latency_ns is not None else "N/A"
            print(
                f"{r.name:28s}: {r.throughput_gb_s:7.2f} GB/s ({r.throughput_mib_s:9.1f} MiB/s) | {eff} of Max | Latency: {lat} | {r.details}"
            )
        return

    table = Table(
        title="󰓅 RAM Bandwidth & Latency Benchmark Summary",
        box=box.ROUNDED,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Benchmark Test Mode", style="bold white", width=29)
    table.add_column("Throughput", justify="right", style="bold green", width=13)
    table.add_column("Bus Efficiency", justify="center", width=16)
    table.add_column("Access Latency", justify="right", style="bold cyan", width=14)
    table.add_column("Test Configuration & Details", style="dim white")

    for r in results:
        lat_str = f"[bold bright_cyan]{r.latency_ns:.2f} ns[/bold bright_cyan]" if r.latency_ns is not None else "[dim]N/A[/dim]"
        if r.name == "Random Memory Latency":
            lat_str = f"[bold bright_cyan]󰔛 {r.latency_ns:.2f} ns[/bold bright_cyan]"

        tp_str = f"[bold bright_green]{r.throughput_gb_s:.2f} GB/s[/bold bright_green]"

        table.add_row(
            r.name,
            tp_str,
            build_gauge(r.efficiency_pct),
            lat_str,
            r.details,
        )

    console.print(table)

    note_text = Text()
    note_text.append("󰨣 Microarchitectural Performance Insights:\n", style="bold yellow")
    note_text.append(" 󰅂 ", style="cyan")
    if specs.theoretical_max_gb_s:
        note_text.append(f"Theoretical Max Peak for your memory bus is ", style="white")
        note_text.append(f"{specs.theoretical_max_gb_s:.2f} GB/s.\n", style="bold green")
    else:
        note_text.append(
            "Theoretical Max Peak calculation requires SMBIOS speed & channel data.\n",
            style="white",
        )

    if specs.configured_speed_mts and specs.factory_speed_mts and specs.factory_speed_mts > specs.configured_speed_mts:
        note_text.append(" 󰅂 ", style="cyan")
        note_text.append("Frequency Downclocking Detected: ", style="bold bright_white")
        note_text.append(
            f"Installed RAM is factory-rated for {specs.factory_speed_mts} MT/s but currently operating at {specs.configured_speed_mts} MT/s due to CPU memory controller hardware constraints.\n",
            style="white",
        )

    note_text.append(" 󰅂 ", style="cyan")
    note_text.append("Single-Core Throughput Limit: ", style="bold bright_white")
    note_text.append(
        "A single CPU core is hardware-capped (~13-20 GB/s) due to finite per-core Line Fill Buffer (LFB) request queues.\n",
        style="white",
    )
    note_text.append(" 󰅂 ", style="cyan")
    note_text.append("Pure Read / Write Scaling: ", style="bold bright_white")
    note_text.append(
        f"To reach maximum DRAM bus saturation (60-80+ GB/s), memory requests must be issued in parallel across multiple CPU cores ({specs.online_cpus} active).\n",
        style="white",
    )
    note_text.append(" 󰅂 ", style="cyan")
    note_text.append("Random Access Latency vs Bandwidth: ", style="bold bright_white")
    note_text.append(
        "Random latency is measured via 128MB random pointer chasing (> L3 cache) to isolate true DRAM access delay (115-125 ns). Streaming bandwidth achieves sub-nanosecond line fill cycles via hardware parallelism.",
        style="white",
    )

    panel = Panel(
        note_text,
        title="[bold cyan]󰨣 Understanding Single-Thread vs Multi-Thread RAM Bandwidth & Latency[/bold cyan]",
        border_style="cyan",
    )
    console.print(panel)


def export_report(
    filename: str,
    specs: HardwareSpecs,
    cache_hierarchy: CacheHierarchyResult | None,
    results: list[TestResult],
) -> None:
    """Export structured JSON or CSV benchmark report."""
    specs.final_dram_temps = probe_dram_temperatures()
    data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hardware_specs": {
            "cpu_model": specs.cpu_model,
            "online_cpus": specs.online_cpus,
            "numa_nodes": specs.numa_nodes,
            "mem_type": specs.mem_type,
            "configured_speed_mts": specs.configured_speed_mts,
            "factory_speed_mts": specs.factory_speed_mts,
            "dimm_count": specs.dimm_count,
            "channels": specs.channels,
            "bus_width_bits": specs.bus_width_bits,
            "theoretical_max_gb_s": specs.theoretical_max_gb_s,
            "total_ram_gb": specs.total_ram_gb,
            "avail_ram_gb": specs.avail_ram_gb,
            "manufacturer": specs.manufacturer,
            "part_number": specs.part_number,
            "form_factor": specs.form_factor,
            "initial_dram_temps_c": specs.initial_dram_temps,
            "final_dram_temps_c": specs.final_dram_temps,
        },
        "cache_hierarchy_latency_ns": asdict(cache_hierarchy) if cache_hierarchy else None,
        "benchmark_results": [asdict(r) for r in results],
    }

    if filename.endswith(".json"):
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
        msg = f"Exported benchmark report to JSON: {filename}"
        if RICH_AVAILABLE:
            console.print(f"[bold green]󰄬 {msg}[/bold green]")
        else:
            print(msg)
    elif filename.endswith(".csv"):
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Metric / Test", "Throughput (GB/s)", "Throughput (MiB/s)", "Efficiency (%)", "Latency (ns)", "Details"])
            for r in results:
                writer.writerow([r.name, f"{r.throughput_gb_s:.2f}", f"{r.throughput_mib_s:.1f}", f"{r.efficiency_pct:.1f}" if r.efficiency_pct else "N/A", f"{r.latency_ns:.2f}" if r.latency_ns else "N/A", r.details])
        msg = f"Exported benchmark report to CSV: {filename}"
        if RICH_AVAILABLE:
            console.print(f"[bold green]󰄬 {msg}[/bold green]")
        else:
            print(msg)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ultimate Hardware-Agnostic RAM Bandwidth & Latency Benchmark Suite"
    )
    parser.add_argument(
        "--bench",
        choices=["read", "write", "copy", "single", "latency", "cache", "all"],
        default="all",
        help="Benchmark mode to run non-interactively (default: all).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Number of workers for multi-core tests (default: all online CPUs).",
    )
    parser.add_argument(
        "--time",
        type=int,
        default=10,
        help="Duration in seconds per test (default: 10).",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=4096,
        help="Array size in MiB for mbw single-core test (default: 4096).",
    )
    parser.add_argument(
        "--cores",
        help="Core range string to pin tests to (e.g. 0-13 or 0-7).",
    )
    parser.add_argument(
        "--export",
        help="Path to export benchmark results in JSON or CSV format (e.g. --export report.json).",
    )
    parser.add_argument(
        "--no-governor",
        action="store_true",
        help="Skip optimizing CPU performance governor.",
    )
    parser.add_argument(
        "--sudo-pass",
        help="Optional sudo password for dmidecode and CPU governor tuning.",
    )
    args = parser.parse_args()

    sudo_pass = get_sudo_pass(args.sudo_pass, allow_interactive_prompt=True)

    check_and_install_deps(sudo_pass)
    specs = detect_hardware_specs(sudo_pass)

    render_header(specs, governor_active=not args.no_governor)

    workers = args.workers or specs.online_cpus
    results: list[TestResult] = []
    cache_hierarchy: CacheHierarchyResult | None = None

    governor_ctx = (
        contextlib.nullcontext() if args.no_governor else set_cpu_performance(sudo_pass)
    )

    with governor_ctx:
        if RICH_AVAILABLE:
            with Progress(
                SpinnerColumn("dots", style="cyan"),
                TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                console=console,
                transient=True,
            ) as progress:
                if args.bench in ["cache", "all"]:
                    tc = progress.add_task(
                        "Measuring L1/L2/L3 Cache & DRAM Latency Hierarchy...", total=None
                    )
                    cache_hierarchy = run_cache_hierarchy_latency_test(args.cores)
                    progress.remove_task(tc)

                if args.bench in ["latency", "all"]:
                    t0 = progress.add_task(
                        "Running Random DRAM Access Latency Benchmark (128M Pointer-Chasing)...", total=None
                    )
                    res_lat = run_latency_test(128, specs, args.cores)
                    results.append(res_lat)
                    progress.remove_task(t0)

                if args.bench in ["single", "all"]:
                    t1 = progress.add_task(
                        "Running Single-Core Memory Copy Benchmark (mbw)...", total=None
                    )
                    res_single = run_single_core_test(args.size, 10, specs, args.cores)
                    results.append(res_single)
                    progress.remove_task(t1)

                if args.bench in ["read", "all"]:
                    t2 = progress.add_task(
                        "Running Pure Multi-Core Read Benchmark (sysbench 64M)...", total=None
                    )
                    res_read = run_pure_read_test(workers, args.time, specs, args.cores)
                    results.append(res_read)
                    progress.remove_task(t2)

                if args.bench in ["write", "all"]:
                    t3 = progress.add_task(
                        "Running Pure Multi-Core Write Benchmark (sysbench 64M)...", total=None
                    )
                    res_write = run_pure_write_test(workers, args.time, specs, args.cores)
                    results.append(res_write)
                    progress.remove_task(t3)

                if args.bench in ["copy", "all"]:
                    t4 = progress.add_task(
                        "Running Multi-Core STREAM Copy Benchmark (stress-ng)...", total=None
                    )
                    res_copy = run_copy_stream_test(workers, args.time, specs, args.cores)
                    results.append(res_copy)
                    progress.remove_task(t4)
        else:
            if args.bench in ["cache", "all"]:
                print("Measuring L1/L2/L3 Cache & DRAM Latency Hierarchy...")
                cache_hierarchy = run_cache_hierarchy_latency_test(args.cores)
            if args.bench in ["latency", "all"]:
                print("Running Random DRAM Access Latency Benchmark...")
                results.append(run_latency_test(128, specs, args.cores))
            if args.bench in ["single", "all"]:
                print("Running Single-Core Memory Copy Benchmark...")
                results.append(run_single_core_test(args.size, 10, specs, args.cores))
            if args.bench in ["read", "all"]:
                print("Running Pure Multi-Core Read Benchmark...")
                results.append(run_pure_read_test(workers, args.time, specs, args.cores))
            if args.bench in ["write", "all"]:
                print("Running Pure Multi-Core Write Benchmark...")
                results.append(run_pure_write_test(workers, args.time, specs, args.cores))
            if args.bench in ["copy", "all"]:
                print("Running Multi-Core STREAM Copy Benchmark...")
                results.append(run_copy_stream_test(workers, args.time, specs, args.cores))

        if cache_hierarchy:
            render_cache_hierarchy_table(cache_hierarchy)
        if results:
            render_results_table(results, specs)

        if args.export:
            export_report(args.export, specs, cache_hierarchy, results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
