#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""
Dusky Kernel Compiler — 2026.08 Production Grade
Target: Arch Linux rolling, Kernel 7.1.x+, systemd 261+, Python 3.14+
Toolchain: LLVM/Clang + lld (with ThinLTO) or GCC fallback, rustc/bindgen (with rustavailable probe)
Methodology: pacman -T Provides resolution, modprobed-db hardware profiling + systemd service,
             kernel.org SHA-256 verification, interactive release picker (mainline default),
             LSMOD + expanded LMC_KEEP localmodconfig, vmlinux BTF preservation (enabling
             sched_ext), pacman-pkg with isolated PKGDEST.
"""
from __future__ import annotations

import argparse
import atexit
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

# --- Preflight Checks ---
if sys.version_info < (3, 14):
    sys.exit(f"Fatal: Python 3.14+ required. Found Python {sys.version.split()[0]}")
if os.geteuid() == 0:
    sys.exit("Fatal: Do not run as root. makepkg refuses root execution. Run as standard user.")

try:
    import rich  # noqa: F401
except ImportError:
    print(":: Missing 'python-rich'. Install: sudo pacman -S --needed python-rich")
    sys.exit(1)

from rich import box
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()

# --- Global Paths & Constants ---
USER_AGENT = "dusky-kernel/2026.08"

DEPENDENCIES = [
    "base-devel",
    "bc",
    "cpio",
    "gettext",
    "libelf",
    "pahole",
    "perl",
    "tar",
    "xz",
    "zstd",
    "kmod",
    "openssl",
    "ncurses",
    "rust",
    "rust-src",
    "rust-bindgen",
    "clang",
    "llvm",
    "lld",
    "git",
    "rsync",
    "python",
]

MODPROBED_DB_AUR = "modprobed-db"
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DB_FILE = XDG_CONFIG / "modprobed.db"
BUILD_DIR = Path.home() / "dusky_build"
PACKAGES_DIR = BUILD_DIR / "packages"
DUSKY_DIR = XDG_CONFIG / "dusky" / "settings" / "dusky_kernel"
DUSKY_STATE_FILE = DUSKY_DIR / "state.json"
DUSKY_SAVED_CONFIG = DUSKY_DIR / "kernel.config"

# Expanded LMC_KEEP for modern 2026 laptops & desktops (USB4/TB, NVMe, Wi-Fi 7, GPU, sched_ext, BPF)
LMC_KEEP_PREFIXES = (
    "drivers/usb:drivers/gpu:fs:drivers/input:drivers/nvme:"
    "drivers/scsi:drivers/hid:drivers/block:drivers/md:"
    "drivers/acpi:drivers/firmware:drivers/platform:fs/nls:"
    "kernel/power:drivers/net:drivers/char:drivers/thunderbolt:"
    "drivers/accel:drivers/pci:drivers/media:drivers/i2c:drivers/spi:"
    "kernel/sched:kernel/bpf:net/sched"
)


class SystemAction(StrEnum):
    INIT = "1"
    MONITOR = "2"
    COMPILE = "3"
    CONFIG = "4"
    VERIFY = "5"
    EXIT = "6"


@dataclass
class DuskyState:
    use_imported_config: bool = True
    prefer_llvm: bool = True
    enable_rust: bool = True
    enable_sched_ext: bool = True
    enable_thin_lto: bool = True

    _FIELDS = (
        "use_imported_config",
        "prefer_llvm",
        "enable_rust",
        "enable_sched_ext",
        "enable_thin_lto",
    )

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    @classmethod
    def load(cls) -> DuskyState:
        defaults = cls()
        try:
            if DUSKY_STATE_FILE.exists():
                with open(DUSKY_STATE_FILE, "r") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return defaults
                return cls(
                    **{
                        name: cls._as_bool(data.get(name), getattr(defaults, name))
                        for name in cls._FIELDS
                    }
                )
        except (OSError, json.JSONDecodeError):
            pass
        return defaults

    def save(self) -> None:
        DUSKY_DIR.mkdir(parents=True, exist_ok=True)
        payload = {name: getattr(self, name) for name in self._FIELDS}
        tmp_file = DUSKY_STATE_FILE.with_suffix(".json.tmp")
        with open(tmp_file, "w") as f:
            json.dump(payload, f, indent=4)
        os.replace(tmp_file, DUSKY_STATE_FILE)


# --- Sudo Keepalive Daemon ---
_sudo_stop = threading.Event()
_sudo_thread: threading.Thread | None = None


def _sudo_keepalive_loop() -> None:
    while not _sudo_stop.wait(60):
        r = subprocess.run(["sudo", "-n", "-v"], capture_output=True)
        if r.returncode != 0:
            break


def stop_sudo_keepalive() -> None:
    _sudo_stop.set()


def ensure_sudo() -> None:
    """Authenticate sudo and maintain background keepalive loop."""
    global _sudo_thread
    console.print("[dim]Authenticating sudo...[/dim]")
    subprocess.run(["sudo", "-v"], check=True)
    if _sudo_thread is None or not _sudo_thread.is_alive():
        _sudo_stop.clear()
        _sudo_thread = threading.Thread(
            target=_sudo_keepalive_loop, name="sudo-keepalive", daemon=True
        )
        _sudo_thread.start()
        atexit.register(stop_sudo_keepalive)


def get_username() -> str:
    for var in ("LOGNAME", "USER"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        return os.getlogin()
    except OSError:
        return Path.home().name


# --- Toolchain Probing ---
def is_tool_available(tool: str) -> bool:
    return shutil.which(tool) is not None


def check_llvm_available() -> bool:
    return is_tool_available("clang") and is_tool_available("llvm-ar") and is_tool_available("lld")


def probe_rust_support(kernel_dir: Path, use_llvm: bool) -> tuple[bool, str]:
    """Check rustc, bindgen, rust-src and the kernel 'rustavailable' probe.

    Returns (ok, reason). Reason explains the failure for user feedback.
    """
    if not is_tool_available("rustc"):
        return False, "rustc not found in PATH"
    if not is_tool_available("bindgen"):
        return False, "bindgen not found in PATH"

    try:
        r = subprocess.run(
            ["rustc", "--print", "sysroot"], capture_output=True, text=True, check=True
        )
        rust_src = Path(r.stdout.strip()) / "lib" / "rustlib" / "src" / "rust"
        if not rust_src.exists():
            return False, f"rust-src missing at {rust_src}"
    except (OSError, subprocess.CalledProcessError):
        return False, "failed to query rustc sysroot"

    cmd = ["make"]
    if use_llvm:
        cmd.extend(["LLVM=1", "LLVM_IAS=1"])
    cmd.append("rustavailable")
    try:
        res = subprocess.run(cmd, cwd=kernel_dir, capture_output=True, text=True)
        if res.returncode != 0:
            detail = (res.stdout + res.stderr).strip().splitlines()
            reason = detail[-1] if detail else f"exit code {res.returncode}"
            return False, f"kernel reports toolchain incompatible ({reason})"
    except OSError as e:
        return False, str(e)
    return True, "available"


# --- Dependency & Package Resolution ---
def missing_packages(pkgs: list[str]) -> list[str]:
    """Use pacman -T to evaluate package satisfaction including Provides."""
    r = subprocess.run(["pacman", "-T"] + pkgs, capture_output=True, text=True)
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def install_dependencies() -> None:
    to_install = missing_packages(DEPENDENCIES)
    if not to_install:
        console.print("[green]::[/green] All build dependencies already satisfied.")
        return
    console.print(f"[cyan]::[/cyan] Installing dependencies: {', '.join(to_install)}")
    subprocess.run(["sudo", "pacman", "-S", "--needed", "--noconfirm"] + to_install, check=True)


def check_aur_helper() -> str | None:
    for helper in ("paru", "yay"):
        if shutil.which(helper):
            return helper
    return None


def install_aur_package(pkg_name: str) -> None:
    if subprocess.run(["pacman", "-Qq", pkg_name], capture_output=True).returncode == 0:
        return
    helper = check_aur_helper()
    if helper:
        console.print(f"[cyan]::[/cyan] Using [bold]{helper}[/bold] to install {pkg_name}...")
        subprocess.run([helper, "-S", "--noconfirm", "--needed", pkg_name], check=True)
    else:
        console.print(f"[yellow]::[/yellow] No AUR helper found. Building {pkg_name} via makepkg...")
        build_dir = Path("/tmp") / f"{pkg_name}-{os.getpid()}"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        try:
            subprocess.run(
                ["git", "clone", f"https://aur.archlinux.org/{pkg_name}.git", str(build_dir)],
                check=True,
            )
            subprocess.run(["makepkg", "-si", "--noconfirm"], cwd=build_dir, check=True)
        finally:
            if build_dir.exists():
                shutil.rmtree(build_dir, ignore_errors=True)


# --- kernel.org Release Discovery & Cryptographic Verification ---
def version_key(version: str) -> tuple[int, ...] | None:
    """Sort key for kernel versions. '7.2' < '7.2.1' < '7.3-rc1' < '7.3'.

    Release candidates sort below the final release of the same series.
    Returns None for non-numeric versions (e.g. 'next-20260820').
    """
    m = re.match(r"^(\d+(?:\.\d+)*)(?:-rc(\d+))?$", version.strip())
    if not m:
        return None
    nums = tuple(int(part) for part in m.group(1).split("."))
    rc = m.group(2)
    return (*nums, -int(rc)) if rc else (*nums, 0)


def normalize_releases(data: dict) -> list[dict]:
    """Filter kernel.org releases.json to mainline + non-EOL stables, best first."""
    priority = {"mainline": 0, "stable": 1}
    candidates: list[dict] = []
    seen: set[str] = set()
    for release in data.get("releases", []):
        moniker = release.get("moniker")
        version = release.get("version")
        url = release.get("source")
        if moniker not in priority or not version or not url:
            continue
        if release.get("iseol"):
            continue
        key = version_key(version)
        if key is None or version in seen:
            continue
        seen.add(version)
        candidates.append({"version": version, "url": url, "moniker": moniker, "key": key})
    candidates.sort(key=lambda c: (priority[c["moniker"]], [-v for v in c["key"]]))
    return candidates


def fetch_releases() -> list[dict]:
    """Fetch and normalize available kernel.org releases. Raises on total failure."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                "https://www.kernel.org/releases.json",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode())
            candidates = normalize_releases(data)
            if candidates:
                return candidates
            raise ValueError("no usable mainline/stable releases in kernel.org JSON")
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"kernel.org API failed after retries: {last_error}")


def pick_release(candidates: list[dict]) -> tuple[str, str]:
    """Interactive release selection. Default is entry #1 (current mainline)."""
    table = Table(title="Available kernel.org Releases", box=box.SIMPLE_HEAVY)
    table.add_column("#", style="bold green", justify="right")
    table.add_column("Moniker", style="cyan")
    table.add_column("Version", style="bold white")
    table.add_column("Source", style="dim")
    for idx, cand in enumerate(candidates, start=1):
        table.add_row(str(idx), cand["moniker"], cand["version"], cand["url"])
    console.print(table)

    choice = Prompt.ask(
        "\n[bold cyan]Select kernel version to compile[/bold cyan]",
        choices=[str(i) for i in range(1, len(candidates) + 1)],
        default="1",
    )
    selected = candidates[int(choice) - 1]
    return selected["version"], selected["url"]


def sha256sums_urls(version: str) -> list[str]:
    """Official sha256sums.asc locations for a given kernel version.

    Final releases live in v<major>.x; release candidates additionally live
    in v<major>.x/testing.
    """
    m = re.match(r"^(\d+)\.", version)
    major = m.group(1) if m else "7"
    base = f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x"
    urls = []
    if "-rc" in version:
        urls.append(f"{base}/testing/sha256sums.asc")
    urls.append(f"{base}/sha256sums.asc")
    return urls


def _http_get_bytes(url: str, timeout: int, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
    raise last_error if last_error else RuntimeError(f"fetch failed: {url}")


def get_sha256_for_tarball(tarball_name: str, version: str) -> str | None:
    """Fetch official sha256sums.asc from kernel.org and locate hash for target tarball."""
    for url in sha256sums_urls(version):
        try:
            content = _http_get_bytes(url, timeout=15).decode("utf-8")
            for line in content.splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1] == tarball_name:
                    return parts[0]
        except Exception as e:
            console.print(f"[dim]Note: {url} lookup failed ({e})[/dim]")
    return None


def hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def download_and_verify_file(url: str, dest: Path, expected_sha256: str | None) -> None:
    """Download source archive; verify SHA-256 when an official hash is available."""
    tarball_name = dest.name
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            total_str = response.headers.get("Content-Length")
            total_size = int(total_str) if total_str and total_str.isdigit() else None
            columns = [SpinnerColumn(), TextColumn("[cyan]{task.description}")]
            if total_size:
                columns.extend([BarColumn(), DownloadColumn(), TransferSpeedColumn()])
            else:
                columns.extend([TextColumn("[cyan]{task.completed} bytes"), TransferSpeedColumn()])

            hasher = hashlib.sha256()
            with Progress(*columns, console=console) as progress:
                task = progress.add_task(f"Downloading {tarball_name}...", total=total_size)
                with open(dest, "wb") as out_file:
                    while True:
                        buf = response.read(1024 * 256)
                        if not buf:
                            break
                        out_file.write(buf)
                        hasher.update(buf)
                        progress.advance(task, advance=len(buf))

        digest = hasher.hexdigest()
        if expected_sha256:
            if digest.lower() != expected_sha256.lower():
                raise ValueError(
                    f"Checksum mismatch! Expected {expected_sha256}, got {digest}"
                )
            console.print("[bold green]::[/bold green] SHA-256 Checksum Verified Successfully.")
        else:
            console.print(
                f"[bold yellow]::[/bold yellow] WARNING: Proceeding WITHOUT checksum "
                f"verification. Downloaded SHA-256: {digest}"
            )
    except BaseException:
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise


def _confirm_unverified_download() -> None:
    console.print(
        "[bold red]::[/bold red] Official sha256sums.asc could not be retrieved from "
        "kernel.org. The download cannot be verified."
    )
    if not Confirm.ask(
        "[bold yellow]Download and install this kernel UNVERIFIED?[/bold yellow]", default=False
    ):
        raise RuntimeError("Aborted: unwilling to proceed without checksum verification.")


def ensure_tarball(version: str, url: str, tarball: Path) -> None:
    """Guarantee a present (and verifiably intact, when possible) source tarball.

    Reuses an existing download only after re-verifying its SHA-256 against
    kernel.org; corrupt/truncated leftovers are discarded and re-downloaded.
    Consent is requested before anything unverified happens, and the existing
    file is never destroyed unless a replacement download is actually approved.
    """
    expected = get_sha256_for_tarball(tarball.name, version)
    unverified_accepted = False

    if tarball.exists() and tarball.stat().st_size > 0:
        if expected:
            actual = hash_file(tarball)
            if actual.lower() == expected.lower():
                console.print(f"[green]::[/green] Reusing verified tarball {tarball.name}.")
                return
            console.print(
                f"[yellow]::[/yellow] Existing tarball failed verification "
                f"(expected {expected}, got {actual}). Re-downloading..."
            )
            tarball.unlink()
        else:
            _confirm_unverified_download()
            unverified_accepted = True
            tarball.unlink()

    if expected is None and not unverified_accepted:
        _confirm_unverified_download()

    download_and_verify_file(url, tarball, expected)


def tarball_name_from_url(version: str, url: str) -> str:
    path = urlparse(url).path
    base = Path(path).name
    return base if base else f"linux-{version}.tar.xz"


def is_valid_kernel_tree(kernel_dir: Path) -> bool:
    makefile = kernel_dir / "Makefile"
    if not makefile.is_file():
        return False
    try:
        head = makefile.read_text(errors="replace")[:2000]
    except OSError:
        return False
    return "VERSION" in head and (kernel_dir / "scripts").is_dir()


def count_db_modules() -> int:
    if not DB_FILE.exists():
        return 0
    try:
        with open(DB_FILE, "r") as f:
            return sum(1 for line in f if line.strip() and not line.startswith("#"))
    except OSError:
        return 0


def export_active_config(target_file: Path) -> bool:
    try:
        if Path("/proc/config.gz").exists():
            with gzip.open("/proc/config.gz", "rt") as f_in, open(target_file, "w") as f_out:
                f_out.write(f_in.read())
            return True
    except Exception:
        pass
    try:
        rel = os.uname().release
        candidates = [
            Path(f"/boot/config-{rel}"),
            Path(f"/usr/lib/modules/{rel}/config"),
            Path(f"/lib/modules/{rel}/config"),
        ]
        for cand in candidates:
            if cand.exists():
                shutil.copy(cand, target_file)
                return True
    except Exception as e:
        console.print(f"[dim]Config fallback export failed: {e}[/dim]")
    return False


def is_plausible_kernel_config(path: Path) -> bool:
    """Reject empty/garbage config files before they are injected into a build."""
    try:
        if path.stat().st_size < 1000:
            return False
        with open(path, "r", errors="replace") as f:
            head = "".join(f.readline() for _ in range(200))
        return bool(re.search(r'^CONFIG_\w+=', head, re.MULTILINE))
    except OSError:
        return False


def find_built_packages(pkg_dir: Path) -> list[Path]:
    """Locate finished .pkg.tar.zst packages in isolated PKGDEST directory."""
    if not pkg_dir.is_dir():
        return []
    pkgs = [p for p in pkg_dir.glob("*.pkg.tar.zst") if "-debug" not in p.name]
    return sorted(pkgs, key=lambda x: x.name)


# --- System Actions ---
def initialize_tracking() -> None:
    ensure_sudo()
    console.print("\n[bold cyan]::[/bold cyan] Syncing Arch build toolchains...")
    install_dependencies()

    console.print("[bold cyan]::[/bold cyan] Resolving hardware profiler (modprobed-db)...")
    install_aur_package(MODPROBED_DB_AUR)

    console.print("[bold cyan]::[/bold cyan] Initializing local modprobed database...")
    subprocess.run(["modprobed-db", "store"], capture_output=True, check=False)

    console.print("[bold cyan]::[/bold cyan] Enabling systemd user daemon & timer...")
    r_service = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "modprobed-db.service"],
        capture_output=True,
        text=True,
    )
    if r_service.returncode != 0:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "modprobed-db.timer"],
            capture_output=True,
            check=False,
        )

    subprocess.run(["sudo", "loginctl", "enable-linger", get_username()], check=False)

    console.print(
        Panel(
            "[bold green]Daemon Initialization Complete![/bold green]\n\n"
            "modprobed-db service + timer track hardware modules automatically.\n"
            "Use your hardware (USB drives, Wi-Fi, audio, Bluetooth) to populate DB.",
            border_style="green",
            padding=(1, 2),
        )
    )


def monitor_modules() -> None:
    console.clear()
    console.print("[bold yellow]Press Ctrl+C to return to main menu.[/bold yellow]\n")
    try:
        with Live(console=console, refresh_per_second=2) as live:
            while True:
                subprocess.run(["modprobed-db", "store"], capture_output=True, check=False)
                panel = Panel(
                    Align.center(
                        f"[bold white]Unique Drivers Mapped:[/bold white] "
                        f"[bold green]{count_db_modules()}[/bold green]"
                    ),
                    title="Live Hardware Profiling Telemetry",
                    border_style="cyan",
                    padding=(2, 5),
                )
                live.update(panel)
                time.sleep(2)
    except KeyboardInterrupt:
        pass


def manage_dusky_state() -> None:
    state = DuskyState.load()
    while True:
        console.clear()
        config_status = "ACTIVE" if state.use_imported_config else "INACTIVE"
        config_color = "green" if state.use_imported_config else "yellow"
        llvm_status = "ENABLED (ThinLTO)" if state.prefer_llvm else "GCC DEFAULT"
        rust_status = "ENABLED" if state.enable_rust else "DISABLED"

        info_text = (
            f"[bold white]Config Directory:[/bold white] {DUSKY_DIR}\n"
            f"[bold white]Auto-Import Config:[/bold white] [bold {config_color}]{config_status}[/bold {config_color}]\n"
            f"[bold white]LLVM/Clang Mode:[/bold white] [cyan]{llvm_status}[/cyan]\n"
            f"[bold white]Rust Kernel Support:[/bold white] [cyan]{rust_status}[/cyan]\n"
            f"[dim]Backup Config:[/dim] {'Present' if DUSKY_SAVED_CONFIG.exists() else 'Missing'}\n"
        )
        console.print(
            Panel(
                Align.center(info_text),
                title="[bold cyan]Dusky Configuration Manager[/bold cyan]",
                border_style="blue",
            )
        )
        table = Table(show_header=False, box=box.SIMPLE)
        table.add_column("Option", style="bold green", justify="right")
        table.add_column("Description", style="white")
        table.add_row("1.", "Export Live System Config to Dusky Directory")
        table.add_row("2.", "Toggle Config Auto-Import")
        table.add_row("3.", "Toggle LLVM/Clang Toolchain (ThinLTO)")
        table.add_row("4.", "Toggle Rust Kernel Abstractions")
        table.add_row("5.", "Back to Main Menu")
        console.print(table)

        choice = Prompt.ask("\n[bold cyan]Select[/bold cyan]", choices=["1", "2", "3", "4", "5"], default="5")
        if choice == "1":
            DUSKY_DIR.mkdir(parents=True, exist_ok=True)
            if export_active_config(DUSKY_SAVED_CONFIG):
                console.print(f"\n[bold green]Success:[/bold green] Exported config to {DUSKY_SAVED_CONFIG}")
            else:
                console.print("\n[bold red]Error:[/bold red] Could not locate valid active config.")
            Prompt.ask("\n[dim]Press Enter to continue...[/dim]")
        elif choice == "2":
            if not DUSKY_SAVED_CONFIG.exists():
                console.print("\n[bold red]Error: No exported config found. Run option 1 first.[/bold red]")
            elif not is_plausible_kernel_config(DUSKY_SAVED_CONFIG):
                console.print(
                    "\n[bold red]Error: Saved config exists but looks invalid/corrupt. "
                    "Re-export it via option 1.[/bold red]"
                )
            else:
                state.use_imported_config = not state.use_imported_config
                state.save()
                console.print("\n[bold green]Config Auto-Import updated.[/bold green]")
            Prompt.ask("\n[dim]Press Enter to continue...[/dim]")
        elif choice == "3":
            state.prefer_llvm = not state.prefer_llvm
            state.save()
            console.print(f"\n[bold green]LLVM Mode set to {state.prefer_llvm}.[/bold green]")
            Prompt.ask("\n[dim]Press Enter to continue...[/dim]")
        elif choice == "4":
            state.enable_rust = not state.enable_rust
            state.save()
            console.print(f"\n[bold green]Rust kernel support set to {state.enable_rust}.[/bold green]")
            Prompt.ask("\n[dim]Press Enter to continue...[/dim]")
        else:
            break


def run_empirical_diagnostics() -> None:
    console.clear()
    console.print(Panel("[bold cyan]Dusky System Empirical Diagnostics[/bold cyan]", border_style="blue"))

    # 1. System Info
    console.print(f"[bold white]Host Kernel:[/bold white] {os.uname().release}")
    console.print(f"[bold white]Python Runtime:[/bold white] {sys.version.split()[0]}")

    # 2. Toolchain
    llvm_ok = check_llvm_available()
    console.print(f"[bold white]LLVM/Clang Toolchain (clang, llvm-ar, lld):[/bold white] {'[green]OK[/green]' if llvm_ok else '[yellow]Missing[/yellow]'}")
    console.print(f"[bold white]GCC Compiler:[/bold white] {'[green]OK[/green]' if is_tool_available('gcc') else '[red]Missing[/red]'}")
    console.print(f"[bold white]Rustc Compiler:[/bold white] {'[green]OK[/green]' if is_tool_available('rustc') else '[yellow]Missing[/yellow]'}")
    console.print(f"[bold white]Rust Bindgen:[/bold white] {'[green]OK[/green]' if is_tool_available('bindgen') else '[yellow]Missing[/yellow]'}")

    # 3. Telemetry & Units
    db_count = count_db_modules()
    console.print(f"[bold white]modprobed-db Drivers Mapped:[/bold white] [green]{db_count}[/green]")

    r_unit = subprocess.run(["systemctl", "--user", "is-enabled", "modprobed-db.service"], capture_output=True, text=True)
    unit_enabled = r_unit.stdout.strip() if r_unit.returncode == 0 else "disabled/missing"
    console.print(f"[bold white]modprobed-db systemd unit:[/bold white] [cyan]{unit_enabled}[/cyan]")

    r_timer = subprocess.run(["systemctl", "--user", "is-active", "modprobed-db.timer"], capture_output=True, text=True)
    timer_active = r_timer.stdout.strip() if r_timer.returncode == 0 else "inactive/missing"
    console.print(f"[bold white]modprobed-db systemd timer:[/bold white] [cyan]{timer_active}[/cyan]")

    r_linger = subprocess.run(["loginctl", "show-user", get_username(), "-p", "Linger"], capture_output=True, text=True)
    linger_val = r_linger.stdout.strip() if r_linger.returncode == 0 else "Linger=no"
    console.print(f"[bold white]User Session Linger:[/bold white] [cyan]{linger_val}[/cyan]")

    # 4. Storage & Saved Config
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(str(BUILD_DIR)).free / (1024**3)
    console.print(f"[bold white]Build Dir Free Space ({BUILD_DIR}):[/bold white] [cyan]{free_gb:.1f} GB[/cyan]")
    if DUSKY_SAVED_CONFIG.exists():
        size_kb = DUSKY_SAVED_CONFIG.stat().st_size / 1024
        ok = is_plausible_kernel_config(DUSKY_SAVED_CONFIG)
        status = "[green]valid[/green]" if ok else "[red]INVALID[/red]"
        console.print(f"[bold white]Saved Config:[/bold white] [cyan]{size_kb:.0f} KB, {status}[/cyan]")
    else:
        console.print("[bold white]Saved Config:[/bold white] [yellow]Missing[/yellow]")

    # 5. Kernel Config Capabilities (sched_ext, BTF, etc.)
    if Path("/proc/config.gz").exists():
        try:
            with gzip.open("/proc/config.gz", "rt") as f:
                cfg = f.read()
                has_btf = "CONFIG_DEBUG_INFO_BTF=y" in cfg
                has_scx = "CONFIG_SCHED_CLASS_EXT=y" in cfg
                console.print(f"[bold white]Active Kernel BTF Support:[/bold white] {'[green]YES[/green]' if has_btf else '[red]NO[/red]'}")
                console.print(f"[bold white]Active Kernel sched_ext Support:[/bold white] {'[green]YES[/green]' if has_scx else '[red]NO[/red]'}")
        except Exception:
            pass

    Prompt.ask("\n[dim]Press Enter to return to main menu...[/dim]")


# --- Kconfig Matrix (pure builder, unit-testable) ---
def build_config_matrix(use_llvm: bool, enable_thin_lto: bool) -> list[str]:
    """Assemble scripts/config arguments for hardening & performance policy."""
    cfg_args = [
        # 1. BTF & sched_ext preservation (CRITICAL)
        # Keep CONFIG_DEBUG_INFO_BTF=y so CONFIG_SCHED_CLASS_EXT (sched_ext) works!
        "-e", "DEBUG_INFO",
        "-e", "DEBUG_INFO_DWARF5",
        "-e", "DEBUG_INFO_BTF",
        "-d", "DEBUG_INFO_BTF_MODULES",  # Disable per-module BTF to save build time
        "-d", "DEBUG_INFO_DWARF4",
        "-d", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
        "-e", "DEBUG_INFO_COMPRESSED_NONE",
        "-d", "DEBUG_INFO_NONE",

        # 2. Keyring cleanup (prevent build error on missing local certs)
        "--set-str", "SYSTEM_TRUSTED_KEYS", "",
        "--set-str", "SYSTEM_REVOCATION_KEYS", "",

        # 3. Performance & Scheduler Optimizations
        "-e", "SCHED_CLASS_EXT",
        "-e", "CC_OPTIMIZE_FOR_PERFORMANCE",
        "-e", "TCP_CONG_ADVANCED",
        "-e", "TCP_CONG_BBR",
        "--set-str", "DEFAULT_TCP_CONG", "bbr",
        "-e", "KERNEL_ZSTD",
        "-e", "MODULE_COMPRESS_ZSTD",
        "-e", "HZ_1000",
    ]

    if use_llvm and enable_thin_lto:
        cfg_args.extend(["-e", "LTO_CLANG_THIN", "-d", "LTO_NONE"])

    if os.uname().machine == "x86_64":
        cfg_args.extend(["-e", "X86_NATIVE_CPU"])

    return cfg_args


def apply_config_matrix(kernel_dir: Path, cfg_args: list[str]) -> None:
    scripts_cfg = str(kernel_dir / "scripts" / "config")
    subprocess.run([scripts_cfg] + cfg_args, cwd=kernel_dir, check=True)


# --- Build Process Safety Helpers ---
def terminate_process_group(process: subprocess.Popen | None) -> None:
    """Terminate a build process and its whole session, then reap it."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        console.print("[red]Warning: build process did not terminate cleanly.[/red]")


# --- Main Compilation Pipeline ---
def compile_kernel() -> None:
    if count_db_modules() < 100:
        console.print(
            Panel(
                f"[bold red]Hardware profile at {DB_FILE} is sparse (<100 drivers).[/bold red]\n"
                "Please run option 1 (Init) and option 2 (Telemetry) to populate hardware database first.",
                border_style="red",
            )
        )
        return

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(str(BUILD_DIR)).free / (1024**3)
    if free_gb < 25.0:
        if not Confirm.ask(
            f"\n[bold yellow]Only {free_gb:.1f} GB free space in {BUILD_DIR}. Kernel compilation needs ~25-30 GB. Continue?[/bold yellow]",
            default=False,
        ):
            return

    ensure_sudo()
    install_dependencies()

    state = DuskyState.load()
    use_llvm = state.prefer_llvm and check_llvm_available()
    if state.prefer_llvm and not use_llvm:
        console.print("[yellow]:: LLVM toolchain requested but incomplete. Falling back to GCC.[/yellow]")

    console.print("[bold cyan]::[/bold cyan] Querying kernel.org releases...")
    candidates = fetch_releases()
    version, url = pick_release(candidates)

    tarball_name = tarball_name_from_url(version, url)
    tarball = BUILD_DIR / tarball_name
    kernel_dir = BUILD_DIR / f"linux-{version}"
    isolated_pkg_dir = PACKAGES_DIR / f"linux-{version}"

    build_proc: subprocess.Popen | None = None
    log_lines: deque[str] = deque(maxlen=20)
    try:
        # Check source tree sanity
        if kernel_dir.exists() and not is_valid_kernel_tree(kernel_dir):
            console.print(f"[yellow]:: Incomplete tree at {kernel_dir}, removing...[/yellow]")
            shutil.rmtree(kernel_dir, ignore_errors=True)

        if not is_valid_kernel_tree(kernel_dir):
            console.print(f"\n[bold cyan]::[/bold cyan] Fetching Linux kernel source [bold]linux-{version}[/bold]...")
            ensure_tarball(version, url, tarball)

            if kernel_dir.exists():
                shutil.rmtree(kernel_dir, ignore_errors=True)

            with console.status("[bold yellow]Unpacking source archive...[/bold yellow]"):
                subprocess.run(["tar", "-xf", str(tarball)], cwd=BUILD_DIR, check=True)

            if not is_valid_kernel_tree(kernel_dir):
                console.print(f"[bold red]Fatal:[/bold red] Extracted tree at {kernel_dir} is invalid.")
                return
        else:
            console.print(f"\n[bold cyan]::[/bold cyan] Found existing valid source tree at linux-{version}.")

        isolated_pkg_dir.mkdir(parents=True, exist_ok=True)

        # Base Make command definition
        make_base = ["make"]
        if use_llvm:
            make_base.extend(["LLVM=1", "LLVM_IAS=1"])

        # --- Config Injection ---
        injected = False
        if state.use_imported_config and DUSKY_SAVED_CONFIG.exists():
            if is_plausible_kernel_config(DUSKY_SAVED_CONFIG):
                console.print("[bold green]::[/bold green] Injecting saved Dusky kernel config...")
                shutil.copy(DUSKY_SAVED_CONFIG, kernel_dir / ".config")
                injected = True
            else:
                console.print(
                    "[yellow]:: Saved Dusky config is corrupt/invalid; falling back to live system config.[/yellow]"
                )
        if not injected:
            console.print("[bold cyan]::[/bold cyan] Cloning live host kernel config...")
            if not Path("/proc/config.gz").exists():
                subprocess.run(["sudo", "modprobe", "configs"], check=False)
            if not export_active_config(kernel_dir / ".config"):
                subprocess.run(make_base + ["defconfig"], cwd=kernel_dir, check=True)

        # --- localmodconfig Pruning ---
        console.print("[bold cyan]::[/bold cyan] Pruning kernel config with localmodconfig + modprobed-db...")
        env = os.environ.copy()
        if DB_FILE.exists() and DB_FILE.stat().st_size > 0:
            env["LSMOD"] = str(DB_FILE)
        else:
            console.print("[dim]:: modprobed.db not present; localmodconfig reading live system drivers from /proc/modules[/dim]")
        env["LMC_KEEP"] = LMC_KEEP_PREFIXES

        subprocess.run(
            make_base + ["localmodconfig"],
            cwd=kernel_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )

        # --- Host Scripts / Tooling ---
        console.print("[bold cyan]::[/bold cyan] Building host kconfig tooling...")
        subprocess.run(make_base + ["scripts"], cwd=kernel_dir, stdout=subprocess.DEVNULL, check=True)

        # --- Hardening & BTF Preservation Matrix ---
        console.print("[bold cyan]::[/bold cyan] Applying Arch 2026 Kernel Hardening & Performance Matrix...")
        apply_config_matrix(kernel_dir, build_config_matrix(use_llvm, state.enable_thin_lto))

        # Check Rust kernel support
        if state.enable_rust:
            rust_ok, rust_reason = probe_rust_support(kernel_dir, use_llvm)
            if rust_ok:
                console.print("[bold green]::[/bold green] Enabling in-tree Rust driver support (CONFIG_RUST=y)...")
                subprocess.run(
                    [str(kernel_dir / "scripts" / "config"), "-e", "RUST"],
                    cwd=kernel_dir,
                    check=True,
                )
            else:
                console.print(
                    f"[yellow]:: Rust support requested but unavailable: {rust_reason}. Building without CONFIG_RUST.[/yellow]"
                )
                subprocess.run(
                    [str(kernel_dir / "scripts" / "config"), "-d", "RUST"],
                    cwd=kernel_dir,
                    check=False,
                )
        else:
            subprocess.run(
                [str(kernel_dir / "scripts" / "config"), "-d", "RUST"],
                cwd=kernel_dir,
                check=False,
            )

        (kernel_dir / "localversion").write_text("-dusky")

        # Resolve config dependencies cleanly
        subprocess.run(make_base + ["olddefconfig"], cwd=kernel_dir, stdout=subprocess.DEVNULL, check=True)

        if Confirm.ask("\n[bold yellow]Edit configuration manually via nconfig?[/bold yellow]", default=False):
            subprocess.run(make_base + ["nconfig"], cwd=kernel_dir, check=True)
            subprocess.run(make_base + ["olddefconfig"], cwd=kernel_dir, stdout=subprocess.DEVNULL, check=True)

        # Save active config back to Dusky state
        DUSKY_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(kernel_dir / ".config", DUSKY_SAVED_CONFIG)
        state.use_imported_config = True
        state.save()

        cores = os.cpu_count() or 4
        toolchain_name = "LLVM/Clang (ThinLTO)" if use_llvm else "GCC"
        console.print(f"\n[bold green]Building linux-{version}-dusky using {toolchain_name} with {cores} threads...[/bold green]\n")

        build_cmd = make_base + [
            f"-j{cores}",
            "PACMAN_PKGBASE=linux-dusky",
            "PACMAN_EXTRAPACKAGES=headers",
            "pacman-pkg",
        ]

        build_env = os.environ.copy()
        build_env["PKGDEST"] = str(isolated_pkg_dir)

        # Run process in its own session for clean signal handling
        build_proc = subprocess.Popen(
            build_cmd,
            cwd=kernel_dir,
            env=build_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        try:
            with Live(console=console, auto_refresh=True, refresh_per_second=8) as live:
                if build_proc.stdout is None:
                    raise RuntimeError("Failed to capture build output stream")
                for line in iter(build_proc.stdout.readline, ""):
                    clean = line.strip()
                    if not clean:
                        continue
                    log_lines.append(clean)
                    live.update(
                        Panel(
                            "\n".join(log_lines),
                            title=f"[bold cyan]Compiling linux-{version} ({toolchain_name})[/bold cyan]",
                            border_style="blue",
                            padding=(0, 2),
                        )
                    )
                build_proc.stdout.close()
            build_proc.wait()
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Compilation interrupted by user. Terminating process group...[/bold yellow]")
            terminate_process_group(build_proc)
            return

        if build_proc.returncode != 0:
            console.print(f"\n[bold red]Fatal:[/bold red] Kernel compilation failed (exit {build_proc.returncode}). Config preserved.")
            console.print("[dim]--- Last build output ---[/dim]")
            for ln in list(log_lines)[-12:]:
                console.print(f"[dim]  {ln}[/dim]")
            return

        console.print("\n[bold cyan]::[/bold cyan] Resolving generated Arch packages...")
        valid_pkgs = find_built_packages(isolated_pkg_dir)
        if not valid_pkgs:
            console.print(f"[bold red]No valid packages found in {isolated_pkg_dir}![/bold red]")
            return

        ensure_sudo()
        console.print(f"[bold cyan]::[/bold cyan] Installing {len(valid_pkgs)} package(s)...")
        for p in valid_pkgs:
            console.print(f"  [dim]{p.name}[/dim]")

        subprocess.run(
            ["sudo", "pacman", "-U", "--needed", "--noconfirm"] + [str(p) for p in valid_pkgs],
            check=True,
        )

        console.print(
            Panel(
                f"[bold green]Mission Accomplished![/bold green]\n\n"
                f"Dusky Kernel [bold]linux-{version}-dusky[/bold] installed successfully.\n"
                "initramfs generation ran automatically via pacman hooks.\n"
                "[dim]Note: systemd-boot detects new kernels automatically; GRUB users may need 'grub-mkconfig -o /boot/grub/grub.cfg'. Verify your boot entry before rebooting.[/dim]",
                border_style="green",
                padding=(1, 2),
            )
        )

    except KeyboardInterrupt:
        terminate_process_group(build_proc)
        console.print("\n[bold yellow]Interrupted.[/bold yellow]")
    except subprocess.CalledProcessError as e:
        terminate_process_group(build_proc)
        console.print(f"\n[bold red]Subprocess failed:[/bold red] {e}")
        if e.stderr:
            err = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
            console.print(f"[dim]{err[-2000:]}[/dim]")
    except Exception as e:
        terminate_process_group(build_proc)
        console.print(f"\n[bold red]Error:[/bold red] [{type(e).__name__}] {e}")


# --- Main Menu & CLI Routing ---
def main_menu() -> None:
    while True:
        console.clear()
        state = DuskyState.load()
        config_status = (
            "[bold green]IMPORTED[/bold green]"
            if state.use_imported_config and DUSKY_SAVED_CONFIG.exists()
            else "[dim]LIVE[/dim]"
        )
        llvm_info = "[cyan]LLVM/ThinLTO[/cyan]" if state.prefer_llvm and check_llvm_available() else "[yellow]GCC[/yellow]"

        console.print(
            Panel(
                Align.center(
                    f"[bold cyan]Dusky Kernel Compiler[/bold cyan] [dim]- 2026.08 Production[/dim]\n"
                    f"[dim]Arch Linux • Kernel 7.1+ • localmodconfig + LMC_KEEP • pacman-pkg[/dim]\n"
                    f"[dim]Toolchain: {llvm_info} • Config: {config_status}[/dim]"
                ),
                box=box.DOUBLE,
                border_style="blue",
            )
        )
        table = Table(show_header=False, box=box.SIMPLE)
        table.add_column("Option", style="bold green", justify="right")
        table.add_column("Description", style="white")
        table.add_row("1.", "Install Toolchains & Init Hardware Profiler")
        table.add_row("2.", "View Live Hardware Telemetry")
        table.add_row("3.", "Compile & Install Kernel")
        table.add_row("4.", "Config Manager & Toolchain Settings")
        table.add_row("5.", "Run System Empirical Diagnostics")
        table.add_row("6.", "Exit")
        console.print(table)

        try:
            choice = Prompt.ask("\n[bold cyan]Select[/bold cyan]", choices=["1", "2", "3", "4", "5", "6"], default="6")
        except EOFError:
            console.print("\n[bold cyan]Input stream closed. Exiting Dusky Kernel Compiler.[/bold cyan]\n")
            break
        if choice == SystemAction.EXIT:
            console.print("\n[bold cyan]Exiting Dusky Kernel Compiler. May your uptime be long![/bold cyan]\n")
            break
        try:
            if choice == SystemAction.INIT:
                initialize_tracking()
                Prompt.ask("\n[dim]Press Enter to return to menu...[/dim]")
            elif choice == SystemAction.MONITOR:
                monitor_modules()
            elif choice == SystemAction.COMPILE:
                compile_kernel()
                Prompt.ask("\n[dim]Press Enter to return to menu...[/dim]")
            elif choice == SystemAction.CONFIG:
                manage_dusky_state()
            elif choice == SystemAction.VERIFY:
                run_empirical_diagnostics()
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Action cancelled by user.[/bold yellow]")
        except Exception as e:
            console.print(f"\n[bold red]Action failed:[/bold red] [{type(e).__name__}] {e}")
            Prompt.ask("\n[dim]Press Enter to return to menu...[/dim]")


def parse_cli_args() -> None:
    parser = argparse.ArgumentParser(description="Dusky Kernel Compiler 2026.08 Engine")
    parser.add_argument("--verify", action="store_true", help="Run empirical diagnostics and exit")
    parser.add_argument("--check-latest", action="store_true", help="Check latest kernel.org versions and exit")
    args = parser.parse_args()

    if args.verify:
        run_empirical_diagnostics()
        sys.exit(0)
    elif args.check_latest:
        try:
            candidates = fetch_releases()
        except Exception as e:
            console.print(f"[bold red]Fatal:[/bold red] {e}")
            sys.exit(1)
        for cand in candidates[:5]:
            console.print(
                f"[cyan]{cand['moniker']}[/cyan]: [bold green]{cand['version']}[/bold green] — {cand['url']}"
            )
        sys.exit(0)


def _sigterm_to_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    parse_cli_args()
    try:
        main_menu()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Force quit.[/bold yellow]\n")
        sys.exit(0)
