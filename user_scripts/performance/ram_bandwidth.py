#!/usr/bin/env python3
"""
ram_bandwidth.py - Ultimate DDR Memory Bandwidth Benchmark Suite & Visualizer

Features:
  - Hardware & SMBIOS Memory Probe (Speed MT/s, Channels, Manufacturers, Form Factor, Capacities).
  - Theoretical Peak Memory Bandwidth Calculator & Efficiency Gauge.
  - Pure Multi-Core Read Benchmark (sysbench 64M blocks).
  - Pure Multi-Core Write Benchmark (sysbench 64M blocks).
  - Multi-Core STREAM / Copy Benchmark (stress-ng --stream).
  - Single-Core Copy & Memory Benchmark (mbw memcpy).
  - Rich TUI/CLI interface with dynamic progress spinners, colorful tables, visual gauges, and educational notes.
  - Non-blocking CPU scaling governor optimization (performance mode + turbo boost).
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
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
    mem_type: str
    configured_speed_mts: int | None
    dimm_count: int | None
    channels: int | None
    bus_width_bits: int | None
    theoretical_max_gb_s: float | None
    total_ram_gb: float | None
    avail_ram_gb: float | None
    manufacturer: str | None
    part_number: str | None
    form_factor: str | None


@dataclass(slots=True)
class TestResult:
    name: str
    throughput_gb_s: float
    throughput_mib_s: float
    read_gb_s: float | None = None
    write_gb_s: float | None = None
    efficiency_pct: float | None = None
    details: str = ""


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
    """Run a command with sudo if not root, supporting both password and interactive TTY execution."""
    if os.geteuid() == 0:
        return run_cmd(cmd)

    if sudo_pass == "":
        full_cmd = ["sudo", "-n", *cmd]
        return run_cmd(full_cmd)
    elif sudo_pass is not None:
        full_cmd = ["sudo", "-S", *cmd]
        return run_cmd(full_cmd, input_text=f"{sudo_pass}\n")
    elif sys.stdin.isatty():
        # Let sudo prompt for password natively in an interactive terminal session
        proc = subprocess.run(["sudo", *cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, ["sudo", *cmd], output=proc.stdout)
        return proc.stdout or ""
    else:
        full_cmd = ["sudo", "-n", *cmd]
        try:
            return run_cmd(full_cmd)
        except Exception:
            return run_cmd(cmd)


def get_online_cpu_count() -> int:
    if tool_exists("nproc"):
        try:
            return max(int(run_cmd(["nproc"]).strip()), 1)
        except Exception:
            pass
    return max(os.cpu_count() or 1, 1)


def check_and_install_deps(sudo_pass: str | None = None) -> None:
    """Ensure required benchmark tools are installed via pacman/paru."""
    required_tools = ["sysbench", "stress-ng", "dmidecode"]
    missing_tools = [t for t in required_tools if not tool_exists(t)]

    if not tool_exists("mbw"):
        missing_tools.append("mbw")

    if not missing_tools:
        return

    msg = f"Installing missing benchmark dependencies: {', '.join(missing_tools)}..."
    if RICH_AVAILABLE:
        console.print(f"[yellow]{msg}[/yellow]")
    else:
        print(msg)

    if tool_exists("paru"):
        cmd = ["paru", "-S", "--needed", "--noconfirm", "--skipreview", *missing_tools]
        subprocess.run(cmd, check=False)
    elif tool_exists("pacman"):
        cmd = ["pacman", "-S", "--needed", "--noconfirm", *missing_tools]
        try:
            run_sudo_cmd(cmd, sudo_pass=sudo_pass)
        except Exception as exc:
            eprint(f"Warning: Automatic dependency installation failed: {exc}")


def detect_hardware_specs(sudo_pass: str | None = None) -> HardwareSpecs:
    """Dynamically probe CPU, RAM capacity, and SMBIOS memory architecture."""
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

            speeds = re.findall(r"Configured Memory Speed:\s+(\d+)\s+MT/s", dmi_out)
            if not speeds:
                speeds = re.findall(r"Speed:\s+(\d+)\s+MT/s", dmi_out)
            if not speeds:
                speeds = re.findall(r"Configured Clock Speed:\s+(\d+)\s+MHz", dmi_out)
            if not speeds:
                speeds = re.findall(r"Speed:\s+(\d+)\s+MHz", dmi_out)
            if speeds:
                valid_speeds = [int(s) for s in speeds if int(s) > 0]
                if valid_speeds:
                    configured_speed_mts = max(valid_speeds)

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
        bus_width_bits = 64 * channels
        total_bus_bytes = bus_width_bits / 8.0
        theoretical_max_gb_s = (configured_speed_mts * total_bus_bytes) / 1000.0

    return HardwareSpecs(
        cpu_model=cpu_model,
        online_cpus=online_cpus,
        mem_type=mem_type,
        configured_speed_mts=configured_speed_mts,
        dimm_count=dimm_count,
        channels=channels,
        bus_width_bits=bus_width_bits,
        theoretical_max_gb_s=theoretical_max_gb_s,
        total_ram_gb=total_ram_gb,
        avail_ram_gb=avail_ram_gb,
        manufacturer=manufacturer,
        part_number=part_number,
        form_factor=form_factor,
    )


@contextlib.contextmanager
def set_cpu_performance(sudo_pass: str | None = None):
    """Set CPU scaling governor to performance and enable hardware boost on Intel/AMD."""
    original_governors: dict[str, str] = {}
    original_intel_no_turbo: str | None = None
    original_amd_boost: str | None = None

    intel_no_turbo_path = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
    amd_boost_path = Path("/sys/devices/system/cpu/cpufreq/boost")

    gov_files = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    for f in gov_files:
        try:
            original_governors[f] = Path(f).read_text().strip()
        except Exception:
            pass

    if intel_no_turbo_path.exists():
        try:
            original_intel_no_turbo = intel_no_turbo_path.read_text().strip()
        except Exception:
            pass

    if amd_boost_path.exists():
        try:
            original_amd_boost = amd_boost_path.read_text().strip()
        except Exception:
            pass

    def apply_settings(gov: str, intel_turbo: str | None, amd_boost: str | None):
        py_cmds = ["import glob, os, pathlib"]
        if gov:
            py_cmds.append(
                f"for f in glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'):\n"
                f"    try: pathlib.Path(f).write_text('{gov}')\n"
                f"    except Exception: pass"
            )
        if intel_turbo is not None and intel_no_turbo_path.exists():
            py_cmds.append(
                f"try: pathlib.Path('{intel_no_turbo_path}').write_text('{intel_turbo}')\n"
                f"except Exception: pass"
            )
        if amd_boost is not None and amd_boost_path.exists():
            py_cmds.append(
                f"try: pathlib.Path('{amd_boost_path}').write_text('{amd_boost}')\n"
                f"except Exception: pass"
            )
        script = "\n".join(py_cmds)
        try:
            run_sudo_cmd(["python3", "-c", script], sudo_pass=sudo_pass)
        except Exception:
            pass

    if RICH_AVAILABLE:
        console.print(
            "[bold yellow]⚡ Setting CPU governor to 'performance' and enabling hardware Turbo/Boost...[/bold yellow]"
        )
    else:
        print("Setting CPU governor to 'performance' and enabling hardware Turbo/Boost...")

    apply_settings("performance", "0", "1")
    try:
        yield
    finally:
        if RICH_AVAILABLE:
            console.print("[dim]Restoring original CPU governor settings...[/dim]")
        py_restore = ["import pathlib"]
        for f, g in original_governors.items():
            py_restore.append(
                f"try: pathlib.Path({f!r}).write_text({g!r})\n"
                f"except Exception: pass"
            )
        if original_intel_no_turbo is not None:
            py_restore.append(
                f"try: pathlib.Path({str(intel_no_turbo_path)!r}).write_text({original_intel_no_turbo!r})\n"
                f"except Exception: pass"
            )
        if original_amd_boost is not None:
            py_restore.append(
                f"try: pathlib.Path({str(amd_boost_path)!r}).write_text({original_amd_boost!r})\n"
                f"except Exception: pass"
            )
        if py_restore:
            try:
                run_sudo_cmd(["python3", "-c", "\n".join(py_restore)], sudo_pass=sudo_pass)
            except Exception:
                pass


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

    return TestResult(
        name="Pure Read (Multi-Thread)",
        throughput_gb_s=gb_s,
        throughput_mib_s=mib_s,
        read_gb_s=gb_s,
        write_gb_s=0.0,
        efficiency_pct=eff_pct,
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

    return TestResult(
        name="Pure Write (Multi-Thread)",
        throughput_gb_s=gb_s,
        throughput_mib_s=mib_s,
        read_gb_s=0.0,
        write_gb_s=gb_s,
        efficiency_pct=eff_pct,
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

    return TestResult(
        name="Copy & Stream (Multi-Thread)",
        throughput_gb_s=total_gb_s,
        throughput_mib_s=total_mib_s,
        read_gb_s=read_gb_s,
        write_gb_s=write_gb_s,
        efficiency_pct=eff_pct,
        details=f"stress-ng --stream, {workers} workers (Read: {read_gb_s:.2f} GB/s, Write: {write_gb_s:.2f} GB/s)",
    )


def run_single_core_test(
    size_mib: int, runs: int, specs: HardwareSpecs, cores: str | None = None
) -> TestResult:
    """Single-Core Memory Copy Benchmark using mbw."""
    cmd = []
    target_core = "0"
    if cores:
        target_core = re.split(r"[,\-]", cores)[0].strip()
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

    return TestResult(
        name="Single-Core Copy (1 Thread)",
        throughput_gb_s=gb_s,
        throughput_mib_s=memcpy_mib_s,
        read_gb_s=gb_s / 2.0,
        write_gb_s=gb_s / 2.0,
        efficiency_pct=eff_pct,
        details=f"mbw memcpy on single core (Core {target_core}) - Single-core Line Fill Buffer limit",
    )


def build_gauge(pct: float | None, width: int = 15) -> str:
    if pct is None:
        return "[dim]N/A[/dim]"
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    empty = width - filled
    
    color = "green" if clamped >= 75.0 else ("yellow" if clamped >= 45.0 else "cyan")
    bar = f"[{color}]" + "█" * filled + "░" * empty + f" {clamped:.1f}%[/{color}]"
    return bar


def render_header(specs: HardwareSpecs):
    speed_str = (
        f"{specs.configured_speed_mts} MT/s" if specs.configured_speed_mts else "Unknown MT/s"
    )
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

    if not RICH_AVAILABLE:
        print(f"=== RAM BANDWIDTH BENCHMARK SUITE ===")
        print(f"CPU: {specs.cpu_model} ({specs.online_cpus} online cores)")
        print(f"RAM: {specs.mem_type} @ {speed_str} | {ram_cap_str}")
        print(f"Topology: {dimm_str} | {mfg_str} {form_str}")
        print(f"Theoretical Max Bandwidth: {max_str}")
        print("=" * 60)
        return

    table = Table(show_header=False, box=box.ROUNDED, expand=True)
    table.add_column("Property", style="bold cyan", width=26)
    table.add_column("System Specifications & Architecture", style="bold white")

    table.add_row("Processor Model", f"[bold white]{specs.cpu_model}[/bold white]")
    table.add_row("Logical CPU Cores", f"[bold green]{specs.online_cpus}[/bold green] cores online")
    table.add_row("Installed Memory Capacity", f"[bold bright_magenta]{ram_cap_str}[/bold bright_magenta]")
    table.add_row("Memory Technology & Speed", f"[bold yellow]{specs.mem_type}[/bold yellow] @ [bold bright_yellow]{speed_str}[/bold bright_yellow]")
    table.add_row("Channel & Slot Topology", f"{dimm_str} ({mfg_str} {form_str})")
    table.add_row("Theoretical Peak Bandwidth", f"[bold bright_green]{max_str}[/bold bright_green]")

    panel = Panel(
        table,
        title="[bold white on blue] 🚀 SYSTEM HARDWARE & MEMORY ARCHITECTURE [/bold white on blue]",
        border_style="bright_blue",
        padding=(0, 1),
    )
    console.print(panel)


def render_results_table(results: list[TestResult], specs: HardwareSpecs):
    if not RICH_AVAILABLE:
        print("\n=== BENCHMARK RESULTS SUMMARY ===")
        for r in results:
            eff = f"{r.efficiency_pct:5.1f}%" if r.efficiency_pct is not None else "N/A"
            print(
                f"{r.name:30s}: {r.throughput_gb_s:7.2f} GB/s ({r.throughput_mib_s:9.1f} MiB/s) | {eff} of Max | {r.details}"
            )
        return

    table = Table(
        title="📊 RAM Bandwidth Benchmark Summary",
        box=box.ROUNDED,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Benchmark Test Mode", style="bold white", width=28)
    table.add_column("Throughput (GB/s)", justify="right", style="bold green", width=18)
    table.add_column("Throughput (MiB/s)", justify="right", style="bold yellow", width=18)
    table.add_column("Efficiency Meter (% of Max)", justify="center", width=25)
    table.add_column("Test Configuration & Details", style="dim white")

    for r in results:
        table.add_row(
            r.name,
            f"[bold bright_green]{r.throughput_gb_s:.2f} GB/s[/bold bright_green]",
            f"{r.throughput_mib_s:,.1f} MiB/s",
            build_gauge(r.efficiency_pct),
            r.details,
        )

    console.print(table)

    note_text = Text()
    note_text.append("💡 Microarchitectural Performance Insights:\n", style="bold yellow")
    note_text.append(" • ", style="cyan")
    if specs.theoretical_max_gb_s:
        note_text.append(f"Theoretical Max Peak for your memory bus is ", style="white")
        note_text.append(f"{specs.theoretical_max_gb_s:.2f} GB/s.\n", style="bold green")
    else:
        note_text.append(
            "Theoretical Max Peak calculation requires SMBIOS speed & channel data.\n",
            style="white",
        )

    note_text.append(" • ", style="cyan")
    note_text.append("Single-Core Throughput Limit: ", style="bold bright_white")
    note_text.append(
        "A single CPU core is hardware-capped (~13-20 GB/s) due to finite per-core Line Fill Buffer (LFB) request queues.\n",
        style="white",
    )
    note_text.append(" • ", style="cyan")
    note_text.append("Pure Read / Write Scaling: ", style="bold bright_white")
    note_text.append(
        f"To reach maximum DRAM bus saturation (60-80+ GB/s), memory requests must be issued in parallel across multiple CPU cores ({specs.online_cpus} active).",
        style="white",
    )

    panel = Panel(
        note_text,
        title="[bold cyan]Understanding Single-Thread vs Multi-Thread RAM Bandwidth[/bold cyan]",
        border_style="cyan",
    )
    console.print(panel)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ultimate Hardware-Agnostic RAM Bandwidth Benchmark Suite & Visualizer"
    )
    parser.add_argument(
        "--bench",
        choices=["read", "write", "copy", "single", "all"],
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
        "--no-governor",
        action="store_true",
        help="Skip optimizing CPU performance governor.",
    )
    parser.add_argument(
        "--sudo-pass",
        help="Optional sudo password for dmidecode and CPU governor tuning.",
    )
    args = parser.parse_args()

    # Pass allow_interactive_prompt=True so get_sudo_pass prompts when run interactively in a TTY!
    sudo_pass = get_sudo_pass(args.sudo_pass, allow_interactive_prompt=True)

    check_and_install_deps(sudo_pass)
    specs = detect_hardware_specs(sudo_pass)

    render_header(specs)

    workers = args.workers or specs.online_cpus
    results: list[TestResult] = []

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

    render_results_table(results, specs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
