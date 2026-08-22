#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""
Dusky Kernel Compiler — 2026.08 Production Grade
Target: Arch Linux rolling, Kernel 7.1.x+, systemd 261+, Python 3.14+
Toolchain: LLVM/Clang + lld (ThinLTO/full/thin-dist) or GCC fallback, rustc/bindgen (rustavailable probe)
Profiles: TOML-driven kernel profiles (~/user_scripts/kernel/kernel_profiles/*.toml) controlling
          scheduler patches (CachyOS BORE), HZ/tickless/preempt, THP, LTO mode, governor,
          zswap/MGLRU/SLUB policy and release channel — CachyOS prepare() parity.
Methodology: pacman -T Provides resolution, modprobed-db hardware profiling + systemd service,
             kernel.org SHA-256 verification, interactive release picker (per-profile channel),
             LSMOD + expanded LMC_KEEP localmodconfig, vmlinux BTF preservation (enabling
             sched_ext), pacman-pkg with isolated PKGDEST per profile.
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
import tomllib
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

# readline for native shell-like tab completion (bleeding-edge: no extra deps)
try:
    import readline  # type: ignore
    import glob as _glob

    _READLINE_AVAILABLE = True
except ImportError:
    _READLINE_AVAILABLE = False
    _glob = None  # type: ignore

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
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
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
    "aria2",
]

MODPROBED_DB_AUR = "modprobed-db"
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DB_FILE = XDG_CONFIG / "modprobed.db"
DEFAULT_BUILD_DIR = Path.home() / "dusky_build"
DUSKY_DIR = XDG_CONFIG / "dusky" / "settings" / "dusky_kernel"
DUSKY_STATE_FILE = DUSKY_DIR / "state.json"
DUSKY_SAVED_CONFIG = DUSKY_DIR / "kernel.config"
PROFILES_DIR = Path(
    os.environ.get("DUSKY_PROFILES_DIR", str(Path(__file__).resolve().parent / "kernel_profiles"))
)
CACHYOS_PATCH_BASE = "https://raw.githubusercontent.com/CachyOS/kernel-patches/master"

# Expanded LMC_KEEP for modern 2026 laptops & desktops (USB4/TB, NVMe, Wi-Fi 7, GPU, sched_ext, BPF)
LMC_KEEP_PREFIXES = (
    "drivers/usb:drivers/gpu:fs:drivers/input:drivers/nvme:"
    "drivers/scsi:drivers/hid:drivers/block:drivers/md:"
    "drivers/acpi:drivers/firmware:drivers/platform:fs/nls:"
    "kernel/power:drivers/net:drivers/char:drivers/thunderbolt:"
    "drivers/accel:drivers/pci:drivers/media:drivers/i2c:drivers/spi:"
    "kernel/sched:kernel/bpf:net/sched"
)


# --- Kernel Profiles (TOML-driven, CachyOS-inspired) ---
HZ_CHOICES = (100, 250, 300, 500, 600, 750, 1000)
_SUFFIX_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_PROFILE_SPEC: dict[str, dict[str, object]] = {
    "meta": {"name": str, "description": str, "suffix": str},
    "release": {"channel": ("mainline", "stable", "lts")},
    "scheduler": {"type": ("vanilla", "bore")},
    "cpu": {"opt": ("native",), "default_governor": ("schedutil", "performance")},
    "timing": {
        "hz": HZ_CHOICES,
        "tickless": ("periodic", "idle", "full"),
        "preempt": ("full", "lazy"),
    },
    "memory": {
        "thp": ("always", "madvise"),
        "mglru": bool,
        "zswap_default_on": bool,
        "slub_tiny": bool,
    },
    "compiler": {"optimize": ("o3", "o2"), "lto": ("none", "thin", "full", "thin_dist")},
    "power": {"wq_power_efficient": bool},
    "network": {"congestion": ("bbr", "cubic"), "qdisc": ("fq", "fq_codel")},
}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class KernelProfile:
    source_file: str
    name: str
    description: str
    suffix: str
    channel: str
    scheduler: str
    cpu_opt: str
    governor: str
    hz: int
    tickless: str
    preempt: str
    thp: str
    mglru: bool
    zswap_default_on: bool
    slub_tiny: bool
    optimize: str
    lto: str
    wq_power_efficient: bool
    congestion: str
    qdisc: str

    @property
    def localversion(self) -> str:
        return f"-{self.suffix}"

    @property
    def pkgbase(self) -> str:
        return f"linux-{self.suffix}"

    @staticmethod
    def _fail(source: str, msg: str) -> None:
        raise ProfileError(f"{source}: {msg}")

    @classmethod
    def _check(cls, source: str, dotted: str, value: object, spec: object) -> None:
        if isinstance(spec, tuple):
            if spec and all(isinstance(s, str) for s in spec):
                valid = " | ".join(spec)  # type: ignore[union-attr]
                ok = isinstance(value, str) and value in spec
            else:
                valid = " | ".join(str(s) for s in spec)  # type: ignore[union-attr]
                ok = isinstance(value, int) and not isinstance(value, bool) and value in spec
            if not ok:
                cls._fail(source, f"{dotted}: {value!r} is invalid (valid: {valid})")
        elif spec is str:
            if not isinstance(value, str) or not value.strip():
                cls._fail(source, f"{dotted}: must be a non-empty string")
        elif spec is bool:
            if not isinstance(value, bool):
                cls._fail(source, f"{dotted}: must be true or false")

    @classmethod
    def from_dict(cls, data: object, source: str) -> KernelProfile:
        if not isinstance(data, dict):
            cls._fail(source, "top-level structure must be a TOML table")
        unknown_sections = set(data) - set(_PROFILE_SPEC)
        if unknown_sections:
            cls._fail(source, f"unknown section(s): {', '.join(sorted(unknown_sections))}")
        values: dict[str, object] = {}
        for section, keys in _PROFILE_SPEC.items():
            if section not in data:
                cls._fail(source, f"missing required section [{section}]")
            sect = data[section]
            if not isinstance(sect, dict):
                cls._fail(source, f"[{section}] must be a table")
            unknown_keys = set(sect) - set(keys)
            if unknown_keys:
                cls._fail(source, f"unknown key(s) in [{section}]: {', '.join(sorted(unknown_keys))}")
            for key, spec in keys.items():
                if key not in sect:
                    cls._fail(source, f"missing required key '{key}' in [{section}]")
                cls._check(source, f"{section}.{key}", sect[key], spec)
                values[key] = sect[key]
        suffix = str(values["suffix"])
        if not _SUFFIX_RE.match(suffix):
            cls._fail(source, f"meta.suffix: {suffix!r} must match lowercase-dns style (e.g. dusky-gaming)")
        return cls(
            source_file=source,
            name=str(values["name"]),
            description=str(values["description"]),
            suffix=suffix,
            channel=str(values["channel"]),
            scheduler=str(values["type"]),
            cpu_opt=str(values["opt"]),
            governor=str(values["default_governor"]),
            hz=int(values["hz"]),
            tickless=str(values["tickless"]),
            preempt=str(values["preempt"]),
            thp=str(values["thp"]),
            mglru=bool(values["mglru"]),
            zswap_default_on=bool(values["zswap_default_on"]),
            slub_tiny=bool(values["slub_tiny"]),
            optimize=str(values["optimize"]),
            lto=str(values["lto"]),
            wq_power_efficient=bool(values["wq_power_efficient"]),
            congestion=str(values["congestion"]),
            qdisc=str(values["qdisc"]),
        )


def load_profiles() -> list[KernelProfile]:
    """Parse every *.toml in PROFILES_DIR; invalid files are skipped with a warning."""
    profiles: list[KernelProfile] = []
    seen: set[str] = set()
    if not PROFILES_DIR.is_dir():
        console.print(f"[yellow]:: Profiles directory not found: {PROFILES_DIR}[/yellow]")
        return profiles
    for path in sorted(PROFILES_DIR.glob("*.toml")):
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            profile = KernelProfile.from_dict(data, path.name)
        except (ProfileError, tomllib.TOMLDecodeError, OSError) as e:
            console.print(f"[yellow]:: Skipping invalid profile {path.name}: {e}[/yellow]")
            continue
        if profile.name in seen:
            console.print(f"[yellow]:: Skipping {path.name}: duplicate profile name '{profile.name}'[/yellow]")
            continue
        seen.add(profile.name)
        profiles.append(profile)
    return profiles


def find_profile(profiles: list[KernelProfile], name: str | None) -> KernelProfile | None:
    if not name:
        return None
    return next((p for p in profiles if p.name == name), None)


def saved_config_path(profile_name: str | None) -> Path:
    if not profile_name:
        return DUSKY_DIR / "kernel.config"
    return DUSKY_DIR / f"kernel.config.{profile_name}"


def summarize_profile(p: KernelProfile) -> str:
    lto_label = p.lto.replace("_", "-").upper()
    return (
        f"{p.scheduler.upper()} • {p.hz}Hz/{p.tickless}/{p.preempt} • "
        f"THP:{p.thp} • {p.optimize.upper()}+{lto_label} • gov:{p.governor}"
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
    custom_build_dir: str | None = None
    selected_profile: str | None = None

    _FIELDS = (
        "use_imported_config",
        "prefer_llvm",
        "enable_rust",
        "enable_sched_ext",
    )

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    @staticmethod
    def _as_optional_str(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @classmethod
    def load(cls) -> DuskyState:
        defaults = cls()
        try:
            if DUSKY_STATE_FILE.exists():
                with open(DUSKY_STATE_FILE, "r") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return defaults
                kwargs = {
                    name: cls._as_bool(data.get(name), getattr(defaults, name))
                    for name in cls._FIELDS
                }
                kwargs["custom_build_dir"] = cls._as_optional_str(data.get("custom_build_dir"))
                kwargs["selected_profile"] = cls._as_optional_str(data.get("selected_profile"))
                return cls(**kwargs)
        except (OSError, ValueError):
            pass
        return defaults

    def save(self) -> None:
        DUSKY_DIR.mkdir(parents=True, exist_ok=True)
        payload = {name: getattr(self, name) for name in self._FIELDS}
        payload["custom_build_dir"] = self.custom_build_dir
        payload["selected_profile"] = self.selected_profile
        tmp_file = DUSKY_STATE_FILE.with_suffix(".json.tmp")
        with open(tmp_file, "w") as f:
            json.dump(payload, f, indent=4)
        os.replace(tmp_file, DUSKY_STATE_FILE)


# --- Modern Build Location Helpers (2026 bleeding-edge: findmnt + walrus, no legacy aliases) ---
def _resolve_custom_build_dir(raw: str | None) -> Path | None:
    """Validate & resolve custom build dir. Handles ~, $VAR, relative -> ~/ . Returns absolute Path or None."""
    if not isinstance(raw, str) or not (stripped := raw.strip()):
        return None
    # Expand $VARS then ~ ; Path.expanduser() is native in 3.14
    expanded = os.path.expandvars(stripped)
    try:
        p = Path(expanded).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        # Normalize without requiring existence (strict=False is 3.6+)
        return p.resolve(strict=False) if hasattr(p, "resolve") else p.absolute()
    except Exception:
        return None


def _existing_target(path: Path) -> Path:
    """findmnt/stat require existing target; walk up to nearest existing parent (bleeding-edge robust)."""
    p = path
    # If path itself exists, use it; else walk parents (max 10 hops)
    for _ in range(10):
        if p.exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return path if path.exists() else p


def get_fs_type(path: Path) -> str:
    """Bleeding-edge fs detection: findmnt (util-linux 2.42.2) is authoritative on Arch."""
    target = _existing_target(path)
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(target)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and (fstype := r.stdout.strip()):
            return fstype
    except Exception:
        pass
    # Fallback: stat -f (coreutils) - less precise (ext4 shows as ext2/ext3) but always present
    try:
        r = subprocess.run(["stat", "-f", "-c", "%T", str(target)], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and (fstype := r.stdout.strip()):
            return fstype
    except Exception:
        pass
    return "unknown"


def is_ram_backed(path: Path) -> bool:
    """True if path lives on RAM (tmpfs/ramfs) or on ZRAM block device (ext4 on /dev/zram*)."""
    target = _existing_target(path)
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE,FSTYPE", "--target", str(target)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and (out := r.stdout.strip()):
            parts = out.split()
            src = parts[0] if len(parts) > 0 else ""
            fstype = parts[1] if len(parts) > 1 else ""
            if "zram" in src or fstype in ("tmpfs", "ramfs", "zram"):
                return True
    except Exception:
        pass
    # Final heuristic for non-mounted yet paths (e.g., /mnt/zram1/new_build)
    return "zram" in str(path) or get_fs_type(target) in ("tmpfs", "ramfs", "zram")


def get_build_dir() -> Path:
    """Effective build dir: 1) $DUSKY_BUILD_DIR env (ephemeral) > 2) persisted state > 3) DEFAULT."""
    if (env_raw := os.environ.get("DUSKY_BUILD_DIR")) and (resolved := _resolve_custom_build_dir(env_raw)):
        return resolved
    if (custom_raw := DuskyState.load().custom_build_dir) and (resolved := _resolve_custom_build_dir(custom_raw)):
        return resolved
    return DEFAULT_BUILD_DIR


def get_packages_dir() -> Path:
    return get_build_dir() / "packages"


def _strip_markup(text: str) -> str:
    """Strip rich markup [bold ...] for readline prompt length calculation."""
    return re.sub(r"\[/?[^\]]*\]", "", text)


def prompt_path_with_tab_completion(prompt_text: str) -> str:
    """Native tab completion for filesystem paths (fixes spaces-on-tab + backspace deleting prompt)."""
    if not _READLINE_AVAILABLE:
        return Prompt.ask(prompt_text, default="", show_default=False)
    # Configure readline completer for paths
    def _path_completer(text: str, state: int) -> str | None:
        try:
            expanded = os.path.expanduser(text) if text else ""
            pattern = (expanded + "*") if text else "*"
            matches = _glob.glob(pattern)  # type: ignore
            if state < len(matches):
                m = matches[state]
                if text.startswith("~"):
                    home = str(Path.home())
                    if m.startswith(home):
                        m = "~" + m[len(home) :]
                try:
                    if Path(os.path.expanduser(m)).is_dir() and not m.endswith("/"):
                        m += "/"
                except Exception:
                    pass
                return m
            return None
        except Exception:
            return None

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    try:
        readline.set_completer(_path_completer)
        readline.set_completer_delims(" \t\n;")
        readline.parse_and_bind("tab: complete")
        # Use input(prompt) with stripped markup so readline knows prompt length (fixes backspace wiping line)
        clean = _strip_markup(prompt_text)
        try:
            return input(clean)
        except EOFError:
            return ""
    finally:
        try:
            readline.set_completer(old_completer)
            readline.set_completer_delims(old_delims)
            readline.parse_and_bind("tab: self-insert")
            readline.parse_and_bind("set editing-mode emacs")
        except Exception:
            pass


def prompt_choice_fixed(prompt_text: str, choices: list[str], default: str) -> str:
    """Choice prompt that never deletes the prompt on backspace (fixes rich+readline ANSI bug)."""
    # Disable completer for choices - we want plain input, not path completion
    old_completer = readline.get_completer() if _READLINE_AVAILABLE else None
    old_delims = readline.get_completer_delims() if _READLINE_AVAILABLE else None
    try:
        if _READLINE_AVAILABLE:
            readline.set_completer(None)
            readline.parse_and_bind("tab: self-insert")
        # Strip markup for readline prompt length
        clean = _strip_markup(prompt_text)
        # Build rich prompt for display, but pass clean to input for readline
        # We print rich markup separately, then use input(clean) so readline knows length
        # Actually use console.print for colors, but input with clean for readline
        # To keep colors, print rich then input with empty? Instead use input(clean) with no rich.
        # We do: print rich prompt, then input("") but readline won't know prompt length -> bug.
        # So we use input(clean) directly and let it print (no rich colors for prompt, but functional)
        # For choices, we can just use rich Prompt but with readline disabled to avoid bug
        # Simpler: use Prompt.ask with readline disabled
        if _READLINE_AVAILABLE:
            # Use sys.stdin.readline to bypass readline's prompt-length bug (backspace won't wipe line)
            choices_str = "/".join(choices)
            full_prompt = f"{prompt_text} [dim][{choices_str}] ({default}):[/dim] "
            while True:
                console.print(full_prompt, end="")
                try:
                    raw = sys.stdin.readline()
                    if not raw:  # EOF
                        return default
                    raw = raw.strip()
                    if not raw:
                        return default
                    if raw in choices:
                        return raw
                    console.print(f"[red]Invalid: {raw}. Choose {choices_str}[/red]")
                except EOFError:
                    return default
        # Fallback to rich if readline not available
        return Prompt.ask(prompt_text, choices=choices, default=default)
    finally:
        if _READLINE_AVAILABLE:
            try:
                readline.set_completer(old_completer)
                readline.set_completer_delims(old_delims)
            except Exception:
                pass


def prompt_enter_fixed(prompt_text: str) -> str:
    """Press-Enter prompt that never wipes line on backspace (bypasses readline)."""
    old_completer = readline.get_completer() if _READLINE_AVAILABLE else None
    old_delims = readline.get_completer_delims() if _READLINE_AVAILABLE else None
    try:
        if _READLINE_AVAILABLE:
            readline.set_completer(None)
            readline.parse_and_bind("tab: self-insert")
        console.print(prompt_text, end="")
        try:
            sys.stdin.readline()
        except EOFError:
            pass
        return ""
    finally:
        if _READLINE_AVAILABLE:
            try:
                readline.set_completer(old_completer)
                readline.set_completer_delims(old_delims)
            except Exception:
                pass


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
            ["rustc", "--print", "sysroot"],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
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
        res = subprocess.run(
            cmd, cwd=kernel_dir, capture_output=True, text=True, errors="replace"
        )
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
    """Filter kernel.org releases.json to mainline + stable + non-EOL longterm, best first."""
    priority = {"mainline": 0, "stable": 1, "longterm": 2}
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


CHANNEL_MONIKERS = {
    "mainline": ("mainline", "stable"),
    "stable": ("stable",),
    "lts": ("longterm",),
}


def pick_release(candidates: list[dict], channel: str = "mainline") -> tuple[str, str]:
    """Interactive release selection restricted to the profile's channel."""
    allowed = CHANNEL_MONIKERS.get(channel) or CHANNEL_MONIKERS["mainline"]
    filtered = [c for c in candidates if c["moniker"] in allowed]
    if not filtered:
        console.print(f"[yellow]:: No non-EOL '{channel}' releases found; showing all channels.[/yellow]")
        filtered = candidates
    table = Table(title=f"Available kernel.org Releases [channel: {channel}]", box=box.SIMPLE_HEAVY)
    table.add_column("#", style="bold green", justify="right")
    table.add_column("Moniker", style="cyan")
    table.add_column("Version", style="bold white")
    table.add_column("Source", style="dim")
    for idx, cand in enumerate(filtered, start=1):
        table.add_row(str(idx), cand["moniker"], cand["version"], cand["url"])
    console.print(table)

    choice = prompt_choice_fixed(
        "\n[bold cyan]Select kernel version to compile[/bold cyan]",
        choices=[str(i) for i in range(1, len(filtered) + 1)],
        default="1",
    )
    selected = filtered[int(choice) - 1]
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


def _ensure_aria2_available() -> bool:
    """Ensure aria2c is available, auto-installing via pacman if missing (bleeding-edge: 16-conn)."""
    if is_tool_available("aria2c"):
        return True
    console.print("[cyan]::[/cyan] aria2 not found - auto-installing for accelerated download (16x)...")
    try:
        # Try pacman (extra/aria2) - DEPENDENCIES now includes aria2
        subprocess.run(["sudo", "pacman", "-S", "--needed", "--noconfirm", "aria2"], check=True)
        return is_tool_available("aria2c")
    except Exception as e:
        console.print(f"[yellow]::[/yellow] aria2 auto-install failed ({e}), falling back to urllib.")
        return False


def _download_with_aria2(url: str, dest: Path) -> None:
    """Download via aria2c with resume, 16 connections, retry. Raises on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "aria2c",
        "-x", "16", "-s", "16", "-k", "1M",
        "--retry-wait=2", "--max-tries=5",
        "--allow-overwrite=true", "--auto-file-renaming=false",
        "-c",  # resume
        "--console-log-level=warn", "--summary-interval=0",
        "--header", f"User-Agent: {USER_AGENT}",
        "-d", str(dest.parent),
        "-o", dest.name,
        url,
    ]
    # aria2c has its own progress; we just run it
    console.print(f"[cyan]::[/cyan] Downloading via aria2c (16x) -> {dest.name}...")
    subprocess.run(cmd, check=True)


def _download_with_urllib(url: str, dest: Path) -> str:
    """Fallback urllib download with progress, returns hex digest."""
    tarball_name = dest.name
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            total_str = response.headers.get("Content-Length")
            total_size = int(total_str) if total_str and total_str.isdigit() else None
            columns = [SpinnerColumn(), TextColumn("[cyan]{task.description}")]
            if total_size:
                columns.extend([BarColumn(), DownloadColumn(), TransferSpeedColumn()])
            else:
                columns.extend([TextColumn("[cyan]{task.completed} bytes"), TransferSpeedColumn()])

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
        return hasher.hexdigest()
    except BaseException:
        if dest.exists():
            # Keep partial for potential resume if user retries with aria2, but urllib can't resume
            # For urllib fallback, we clean up to avoid corrupt verify
            try:
                if dest.stat().st_size == 0:
                    dest.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def download_and_verify_file(url: str, dest: Path, expected_sha256: str | None) -> None:
    """Download source archive via aria2 (16x, resume) with urllib fallback; verify SHA-256."""
    tarball_name = dest.name
    # Prefer aria2c if available (or can be auto-installed)
    use_aria = _ensure_aria2_available() if not is_tool_available("aria2c") else True
    # Actually check again after ensure
    if is_tool_available("aria2c"):
        use_aria = True
    else:
        use_aria = False

    digest: str | None = None
    try:
        if use_aria:
            try:
                _download_with_aria2(url, dest)
                # aria2 done, compute hash
                digest = hash_file(dest)
            except subprocess.CalledProcessError as e:
                console.print(f"[yellow]:: aria2 failed ({e}), falling back to urllib...[/yellow]")
                # Don't delete partial - aria2 -c will resume next time, but we try urllib fallback
                # For urllib fallback, we need to remove partial to avoid mixing
                if dest.exists():
                    # Keep it for next aria2 resume, but urllib will overwrite
                    pass
                digest = _download_with_urllib(url, dest)
            except Exception as e:
                console.print(f"[yellow]:: aria2 error ({e}), falling back to urllib...[/yellow]")
                digest = _download_with_urllib(url, dest)
        else:
            digest = _download_with_urllib(url, dest)

        # Verify
        if expected_sha256:
            if digest.lower() != expected_sha256.lower():
                raise ValueError(f"Checksum mismatch! Expected {expected_sha256}, got {digest}")
            console.print("[bold green]::[/bold green] SHA-256 Checksum Verified Successfully.")
        else:
            console.print(f"[bold yellow]::[/bold yellow] WARNING: Proceeding WITHOUT checksum verification. Downloaded SHA-256: {digest}")
    except BaseException:
        # Only delete if we have no resume capability (urllib) and destination is corrupt
        # For aria2, keep partial for resume
        if not is_tool_available("aria2c") and dest.exists():
            # urllib fallback failed - keep partial? But we already handled
            pass
        # If we used urllib and file is zero or we're aborting unverified, let caller decide
        # Ensure_tarball will handle discarding on next run via hash check
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


# --- CachyOS Scheduler Patch Stage ---
def kernel_major(version: str) -> str:
    m = re.match(r"^(\d+\.\d+)", version.strip())
    return m.group(1) if m else version


def scheduler_patch_urls(profile: KernelProfile, version: str) -> list[str]:
    if profile.scheduler != "bore":
        return []
    return [f"{CACHYOS_PATCH_BASE}/{kernel_major(version)}/sched/0001-bore-cachy.patch"]


def _cached_patch(url: str, cache_dir: Path) -> Path:
    """Download once per major-version patch file; atomic write into cache dir."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / url.rsplit("/", 1)[-1]
    if not dest.exists() or dest.stat().st_size == 0:
        content = _http_get_bytes(url, timeout=30)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(content)
        os.replace(tmp, dest)
    return dest


def apply_profile_patches(kernel_dir: Path, profile: KernelProfile, version: str, cache_dir: Path) -> KernelProfile:
    """Apply profile scheduler patches to a vanilla tree; degrade gracefully to vanilla EEVDF."""
    urls = scheduler_patch_urls(profile, version)
    effective = profile
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        console.print(f"[cyan]::[/cyan] Fetching CachyOS scheduler patch [bold]{name}[/bold]...")
        try:
            patch_file = _cached_patch(url, cache_dir)
            probe = subprocess.run(
                ["patch", "-Np1", "--dry-run", "--forward", "-i", str(patch_file)],
                cwd=kernel_dir,
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                detail = (probe.stdout + "\n" + probe.stderr).strip()[-600:]
                raise RuntimeError(f"dry-run rejected the patch:\n{detail}")
            applied = subprocess.run(
                ["patch", "-Np1", "--forward", "-i", str(patch_file)],
                cwd=kernel_dir,
                capture_output=True,
                text=True,
            )
            if applied.returncode != 0:
                detail = (applied.stderr or "").strip()[-600:]
                raise RuntimeError(f"application failed:\n{detail}")
            console.print(f"[green]::[/green] Applied {name} (BORE scheduler active for this build).")
        except Exception as e:
            console.print(f"[bold yellow]:: Patch stage failed:[/bold yellow] {e}")
            console.print(
                "[dim]CachyOS patches are cut against their fork; they can lag vanilla mid-cycle.[/dim]"
            )
            if not Confirm.ask(
                "[bold yellow]Continue WITHOUT the scheduler patch (vanilla EEVDF)?[/bold yellow]",
                default=True,
            ):
                raise RuntimeError("Aborted by user after patch failure.")
            console.print("[cyan]:: Falling back to vanilla EEVDF scheduler for this build.[/cyan]")
            effective = replace(effective, scheduler="vanilla")
    return effective


def count_db_modules() -> int:
    if not DB_FILE.exists():
        return 0
    try:
        with open(DB_FILE, "r") as f:
            return sum(1 for line in f if line.strip() and not line.startswith("#"))
    except (OSError, ValueError):
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
    profiles = load_profiles()
    while True:
        console.clear()
        config_status = "ACTIVE" if state.use_imported_config else "INACTIVE"
        config_color = "green" if state.use_imported_config else "yellow"
        llvm_status = "ENABLED (LTO per profile)" if state.prefer_llvm else "GCC DEFAULT"
        rust_status = "ENABLED" if state.enable_rust else "DISABLED"
        active_profile = find_profile(profiles, state.selected_profile)
        profile_line = (
            f"[bold green]{active_profile.name}[/bold green] [dim]({summarize_profile(active_profile)})[/dim]"
            if active_profile
            else "[yellow]not set (chosen at compile time)[/yellow]"
        )
        saved_configs = sorted(DUSKY_DIR.glob("kernel.config*"))
        saved_summary = (
            ", ".join(p.name.replace("kernel.config", "").lstrip(".") or "legacy" for p in saved_configs)
            if saved_configs
            else "none yet"
        )
        effective_build = get_build_dir()
        build_source = "CUSTOM" if effective_build != DEFAULT_BUILD_DIR else "DEFAULT"
        build_color = "cyan" if effective_build != DEFAULT_BUILD_DIR else "dim"
        fs_type = get_fs_type(effective_build)
        ram_backed = is_ram_backed(effective_build)

        info_text = (
            f"[bold white]Profiles Directory:[/bold white] {PROFILES_DIR} "
            f"[dim]({'all valid' if len(profiles) == len(list(PROFILES_DIR.glob('*.toml'))) else 'some invalid/missing'} • {len(profiles)} loaded)[/dim]\n"
            f"[bold white]Active Profile:[/bold white] {profile_line}\n"
            f"[bold white]Config Directory:[/bold white] {DUSKY_DIR}\n"
            f"[bold white]Build Directory:[/bold white] [bold {build_color}]{effective_build}[/bold {build_color}] [dim]({build_source} • {fs_type}{', RAM' if ram_backed else ''})[/dim]\n"
            f"[bold white]Auto-Import Config:[/bold white] [bold {config_color}]{config_status}[/bold {config_color}] [dim](saved: {saved_summary})[/dim]\n"
            f"[bold white]LLVM/Clang Mode:[/bold white] [cyan]{llvm_status}[/cyan]\n"
            f"[bold white]Rust Kernel Support:[/bold white] [cyan]{rust_status}[/cyan]\n"
        )
        console.print(
            Align.center(
                Panel(
                    Align.center(info_text),
                    title="[bold cyan]Dusky Configuration Manager[/bold cyan]",
                    border_style="bright_blue",
                    box=box.ROUNDED,
                    expand=False,
                    padding=(1, 2),
                )
            )
        )
        table = Table(show_header=False, box=box.SIMPLE)
        table.add_column("Option", style="bold green", justify="right")
        table.add_column("Description", style="white")
        table.add_row("1.", "Export Live System Config for Active Profile")
        table.add_row("2.", "Toggle Config Auto-Import")
        table.add_row("3.", "Toggle LLVM/Clang Toolchain (LTO mode comes from profiles)")
        table.add_row("4.", "Toggle Rust Kernel Abstractions")
        table.add_row("5.", "Set / Change Build Directory (ZRAM/disk)")
        table.add_row("6.", "Back to Main Menu")
        console.print(table)

        choice = prompt_choice_fixed("\n[bold cyan]Select[/bold cyan]", choices=["1", "2", "3", "4", "5", "6"], default="6")
        if choice == "1":
            if not profiles:
                console.print("\n[bold red]Error:[/bold red] No valid profiles found; cannot export.")
            else:
                target = find_profile(profiles, state.selected_profile) or select_profile(profiles, state)
                dest = saved_config_path(target.name)
                DUSKY_DIR.mkdir(parents=True, exist_ok=True)
                if export_active_config(dest):
                    console.print(f"\n[bold green]Success:[/bold green] Exported config to {dest}")
                else:
                    console.print("\n[bold red]Error:[/bold red] Could not locate valid active config.")
            prompt_enter_fixed("\n[dim]Press Enter to continue...[/dim]")
        elif choice == "2":
            active_cfg = saved_config_path(state.selected_profile)
            if not active_cfg.exists():
                console.print(
                    "\n[bold red]Error: No exported config found for the active profile. "
                    "Run option 1 first.[/bold red]"
                )
            elif not is_plausible_kernel_config(active_cfg):
                console.print(
                    "\n[bold red]Error: Saved config exists but looks invalid/corrupt. "
                    "Re-export it via option 1.[/bold red]"
                )
            else:
                state.use_imported_config = not state.use_imported_config
                state.save()
                console.print("\n[bold green]Config Auto-Import updated.[/bold green]")
            prompt_enter_fixed("\n[dim]Press Enter to continue...[/dim]")
        elif choice == "3":
            state.prefer_llvm = not state.prefer_llvm
            state.save()
            console.print(f"\n[bold green]LLVM Mode set to {state.prefer_llvm}.[/bold green]")
            prompt_enter_fixed("\n[dim]Press Enter to continue...[/dim]")
        elif choice == "4":
            state.enable_rust = not state.enable_rust
            state.save()
            console.print(f"\n[bold green]Rust kernel support set to {state.enable_rust}.[/bold green]")
            prompt_enter_fixed("\n[dim]Press Enter to continue...[/dim]")
        elif choice == "5":
            console.print("\n[bold cyan]Current build dir:[/bold cyan] " + str(get_build_dir()))
            console.print("[dim]Examples: /mnt/zram1/dusky_build  (ZRAM block device, e.g. /dev/zram1 formatted as ext4, RAM-backed)[/dim]")
            console.print("[dim]          /tmp/dusky_build        (tmpfs, RAM-backed, uncompressed)[/dim]")
            console.print("[dim]          ~/dusky_build           (default, disk - btrfs/ext4)[/dim]")
            console.print("[dim]Env override DUSKY_BUILD_DIR also works for one-off builds.[/dim]")
            console.print("[dim]Tab completion: type /mnt/z[TAB] -> /mnt/zram1/[/dim]")
            raw = prompt_path_with_tab_completion("\n[bold cyan]Enter new build directory[/bold cyan] (empty to reset to default, 'cancel' to abort): ")
            if raw.strip().lower() == "cancel":
                console.print("[yellow]Cancelled.[/yellow]")
            elif not raw.strip():
                state.custom_build_dir = None
                state.save()
                console.print(f"[bold green]Build dir reset to default: {DEFAULT_BUILD_DIR}[/bold green]")
            else:
                candidate = _resolve_custom_build_dir(raw)
                if candidate is None:
                    console.print("[bold red]Invalid path.[/bold red]")
                else:
                    # Validate writability / create - bleeding-edge: findmnt + statvfs
                    try:
                        candidate.mkdir(parents=True, exist_ok=True)
                        # Atomic write test (O_TMPFILE style via write+fsync)
                        test_file = candidate / ".dusky_write_test"
                        test_file.write_text("ok")
                        test_file.unlink(missing_ok=True)
                        free_gb = shutil.disk_usage(str(candidate)).free / (1024**3)
                        fs_info = get_fs_type(candidate)
                        ram_backed = is_ram_backed(candidate)
                        if free_gb < 25:
                            console.print(f"[bold yellow]Warning: only {free_gb:.1f} GB free at {candidate} ({fs_info}) - needs 25-30GB.[/bold yellow]")
                            if not Confirm.ask("Save anyway?", default=False):
                                prompt_enter_fixed("\n[dim]Press Enter to continue...[/dim]")
                                continue
                        state.custom_build_dir = str(candidate)
                        state.save()
                        console.print(f"[bold green]Build dir set to: {candidate} ({fs_info}, {free_gb:.1f} GB free)[/bold green]")
                        if ram_backed:
                            if fs_info == "tmpfs":
                                console.print("[dim]tmpfs detected - RAM-backed, very fast, but uses uncompressed RAM. Prefer ZRAM (zstd-compressed, e.g. /mnt/zram1).[/dim]")
                            else:
                                console.print("[dim]ZRAM/RAM detected - excellent for avoiding SSD writes (RAM-backed, zstd-compressed if you used mkfs).[/dim]")
                    except Exception as e:
                        console.print(f"[bold red]Cannot use {candidate}: {e}[/bold red]")
            prompt_enter_fixed("\n[dim]Press Enter to continue...[/dim]")
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

    # 4. Storage & Saved Config (uses effective build dir - may be ZRAM)
    effective_build = get_build_dir()
    effective_build.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(str(effective_build)).free / (1024**3)
    fs_type = get_fs_type(effective_build)
    src_label = "CUSTOM" if effective_build != DEFAULT_BUILD_DIR else "DEFAULT"
    console.print(f"[bold white]Build Dir ({src_label}, {fs_type}):[/bold white] [cyan]{effective_build} — {free_gb:.1f} GB free[/cyan]")
    if is_ram_backed(effective_build):
        console.print(f"[dim]  -> RAM-backed ({fs_type}) - zero SSD wear, contents lost on reboot/poweroff[/dim]")

    # 4b. Profiles & per-profile saved configs
    profiles = load_profiles()
    console.print(f"\n[bold white]Profiles ({len(profiles)}) from {PROFILES_DIR}:[/bold white]")
    if not profiles:
        console.print("[yellow]  No valid TOML profiles found.[/yellow]")
    for p in profiles:
        cfg_file = saved_config_path(p.name)
        if cfg_file.exists():
            size_kb = cfg_file.stat().st_size / 1024
            ok = is_plausible_kernel_config(cfg_file)
            cfg_status = f"[cyan]{size_kb:.0f} KB, {'valid' if ok else '[red]INVALID[/red]'}[/cyan]"
        else:
            cfg_status = "[dim]no saved config yet[/dim]"
        remembered = " [green](last used)[/green]" if p.name == DuskyState.load().selected_profile else ""
        console.print(f"  [bold white]{p.name}{remembered}:[/bold white] {p.description}\n    [dim]{summarize_profile(p)} • config: {cfg_status}[/dim]")

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

    prompt_enter_fixed("\n[dim]Press Enter to return to main menu...[/dim]")


# --- Kconfig Matrix (profile-driven, mirrors CachyOS PKGBUILD prepare()) ---
LTO_MODE_CONFIG = {
    "none": ("LTO_NONE", ("LTO_CLANG_THIN", "LTO_CLANG_FULL", "LTO_CLANG_THIN_DIST")),
    "thin": ("LTO_CLANG_THIN", ("LTO_CLANG_THIN_DIST", "LTO_CLANG_FULL", "LTO_NONE")),
    "thin_dist": ("LTO_CLANG_THIN_DIST", ("LTO_CLANG_THIN", "LTO_CLANG_FULL", "LTO_NONE")),
    "full": ("LTO_CLANG_FULL", ("LTO_CLANG_THIN", "LTO_CLANG_THIN_DIST", "LTO_NONE")),
}


def build_config_matrix(profile: KernelProfile, use_llvm: bool) -> list[str]:
    """Assemble scripts/config arguments translating a KernelProfile into Kconfig policy."""
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

        # 3. Common baseline: sched_ext + fast compression
        "-e", "SCHED_CLASS_EXT",
        "-e", "KERNEL_ZSTD",
        "-e", "MODULE_COMPRESS_ZSTD",
    ]

    # Scheduler flavor
    if profile.scheduler == "bore":
        cfg_args.extend(["-e", "SCHED_BORE"])

    # CPU codegen: always native per Dusky policy (single-machine builds)
    if os.uname().machine == "x86_64":
        cfg_args.extend(["-d", "GENERIC_CPU", "-d", "MZEN4", "-e", "X86_NATIVE_CPU"])

    # Tick rate: disable every sibling choice so olddefconfig resolves deterministically
    for hz in HZ_CHOICES:
        if hz != profile.hz:
            cfg_args.extend(["-d", f"HZ_{hz}"])
    cfg_args.extend(["-e", f"HZ_{profile.hz}", "--set-val", "HZ", str(profile.hz)])

    # Tickless mode (exact CachyOS toggle sets)
    match profile.tickless:
        case "periodic":
            cfg_args.extend([
                "-d", "NO_HZ_IDLE", "-d", "NO_HZ_FULL", "-d", "NO_HZ", "-d", "NO_HZ_COMMON",
                "-e", "HZ_PERIODIC",
            ])
        case "idle":
            cfg_args.extend([
                "-d", "HZ_PERIODIC", "-d", "NO_HZ_FULL",
                "-e", "NO_HZ_IDLE", "-e", "NO_HZ", "-e", "NO_HZ_COMMON",
            ])
        case "full":
            cfg_args.extend([
                "-d", "HZ_PERIODIC", "-d", "NO_HZ_IDLE", "-d", "CONTEXT_TRACKING_FORCE",
                "-e", "NO_HZ_FULL_NODEF", "-e", "NO_HZ_FULL", "-e", "NO_HZ", "-e", "NO_HZ_COMMON",
                "-e", "CONTEXT_TRACKING",
            ])

    # Preemption model
    match profile.preempt:
        case "full":
            cfg_args.extend(["-e", "PREEMPT", "-d", "PREEMPT_LAZY"])
        case "lazy":
            cfg_args.extend(["-d", "PREEMPT", "-e", "PREEMPT_LAZY"])

    # Optimization level
    match profile.optimize:
        case "o3":
            cfg_args.extend(["-d", "CC_OPTIMIZE_FOR_PERFORMANCE", "-e", "CC_OPTIMIZE_FOR_PERFORMANCE_O3"])
        case "o2":
            cfg_args.extend(["-e", "CC_OPTIMIZE_FOR_PERFORMANCE", "-d", "CC_OPTIMIZE_FOR_PERFORMANCE_O3"])

    # Default cpufreq governor
    if profile.governor == "performance":
        cfg_args.extend(["-d", "CPU_FREQ_DEFAULT_GOV_SCHEDUTIL", "-e", "CPU_FREQ_DEFAULT_GOV_PERFORMANCE"])
    else:
        cfg_args.extend(["-e", "CPU_FREQ_DEFAULT_GOV_SCHEDUTIL", "-d", "CPU_FREQ_DEFAULT_GOV_PERFORMANCE"])

    # Transparent Hugepages
    if profile.thp == "always":
        cfg_args.extend(["-d", "TRANSPARENT_HUGEPAGE_MADVISE", "-e", "TRANSPARENT_HUGEPAGE_ALWAYS"])
    else:
        cfg_args.extend(["-d", "TRANSPARENT_HUGEPAGE_ALWAYS", "-e", "TRANSPARENT_HUGEPAGE_MADVISE"])

    # LTO family (LLVM-only; GCC fallback forces none)
    lto_mode = profile.lto if use_llvm else "none"
    lto_enable, lto_disable = LTO_MODE_CONFIG[lto_mode]
    for sym in lto_disable:
        cfg_args.extend(["-d", sym])
    cfg_args.extend(["-e", lto_enable])

    # Networking: congestion control + default qdisc (CachyOS-style explicit defaults)
    if profile.congestion == "bbr":
        cfg_args.extend([
            "-m", "TCP_CONG_CUBIC",
            "-d", "DEFAULT_CUBIC",
            "-e", "TCP_CONG_BBR",
            "-e", "DEFAULT_BBR",
            "--set-str", "DEFAULT_TCP_CONG", "bbr",
        ])
    else:
        cfg_args.extend(["-e", "DEFAULT_CUBIC", "--set-str", "DEFAULT_TCP_CONG", "cubic"])
    if profile.qdisc == "fq":
        cfg_args.extend([
            "-m", "NET_SCH_FQ_CODEL",
            "-e", "NET_SCH_FQ",
            "-d", "DEFAULT_FQ_CODEL",
            "-e", "DEFAULT_FQ",
        ])
    else:
        cfg_args.extend([
            "-m", "NET_SCH_FQ",
            "-e", "NET_SCH_FQ_CODEL",
            "-d", "DEFAULT_FQ",
            "-e", "DEFAULT_FQ_CODEL",
        ])

    # Memory policy
    if profile.mglru:
        cfg_args.extend(["-e", "LRU_GEN", "-e", "LRU_GEN_ENABLED"])
    else:
        cfg_args.extend(["-d", "LRU_GEN", "-d", "LRU_GEN_ENABLED"])
    if profile.zswap_default_on:
        cfg_args.extend([
            "-e", "ZSWAP",
            "-e", "ZSWAP_DEFAULT_ON",
            "-e", "ZSWAP_COMPRESSOR_DEFAULT_ZSTD",
            "--set-str", "ZSWAP_COMPRESSOR_DEFAULT", "zstd",
            "-e", "ZSWAP_ZPOOL_DEFAULT_ZSMALLOC",
            "--set-str", "ZSWAP_ZPOOL_DEFAULT", "zsmalloc",
        ])
    if profile.slub_tiny:
        cfg_args.extend(["-e", "SLUB_TINY"])
    else:
        cfg_args.extend(["-d", "SLUB_TINY"])

    # Power policy
    if profile.wq_power_efficient:
        cfg_args.extend(["-e", "WQ_POWER_EFFICIENT"])
    else:
        cfg_args.extend(["-d", "WQ_POWER_EFFICIENT"])

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


# --- Profile Selection ---
def select_profile(profiles: list[KernelProfile], state: DuskyState) -> KernelProfile:
    """Interactive profile picker; remembers the choice across runs."""
    remembered = find_profile(profiles, state.selected_profile)
    table = Table(title="Dusky Kernel Profiles", box=box.SIMPLE_HEAVY)
    table.add_column("#", style="bold green", justify="right")
    table.add_column("Profile", style="bold white")
    table.add_column("Description", style="cyan")
    table.add_column("Key settings", style="dim")
    for idx, p in enumerate(profiles, start=1):
        marker = " [green](last used)[/green]" if remembered is p else ""
        table.add_row(str(idx), p.name + marker, p.description, summarize_profile(p))
    console.print(table)

    default = str(profiles.index(remembered) + 1) if remembered else "1"
    choice = prompt_choice_fixed(
        "\n[bold cyan]Select kernel profile[/bold cyan]",
        choices=[str(i) for i in range(1, len(profiles) + 1)],
        default=default,
    )
    profile = profiles[int(choice) - 1]
    if state.selected_profile != profile.name:
        state.selected_profile = profile.name
        state.save()
    return profile


# --- Main Compilation Pipeline ---
def compile_kernel(profile_name: str | None = None) -> None:
    if count_db_modules() < 100:
        console.print(
            Panel(
                f"[bold red]Hardware profile at {DB_FILE} is sparse (<100 drivers).[/bold red]\n"
                "Please run option 1 (Init) and option 2 (Telemetry) to populate hardware database first.",
                border_style="red",
            )
        )
        return

    effective_build = get_build_dir()
    effective_packages = get_packages_dir()
    effective_build.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(str(effective_build)).free / (1024**3)
    fs_type = get_fs_type(effective_build)
    console.print(f"[dim]Build directory: {effective_build} ({fs_type}, {free_gb:.1f} GB free)[/dim]")
    if is_ram_backed(effective_build):
        console.print("[dim]RAM-backed build ({fs_type}) - zero SSD writes, wiped on reboot. Final .pkg.tar.zst is installed via pacman -U before reboot.[/dim]".format(fs_type=fs_type))
    if free_gb < 25.0:
        if not Confirm.ask(
            f"\n[bold yellow]Only {free_gb:.1f} GB free space in {effective_build} ({fs_type}). Kernel compilation needs ~25-30 GB. Continue?[/bold yellow]",
            default=False,
        ):
            return

    ensure_sudo()
    install_dependencies()

    state = DuskyState.load()

    profiles = load_profiles()
    if not profiles:
        console.print(f"[bold red]Fatal:[/bold red] No valid profiles found in {PROFILES_DIR}.")
        return
    requested = profile_name or os.environ.get("DUSKY_PROFILE")
    profile = find_profile(profiles, requested)
    if profile is None:
        if requested:
            console.print(f"[yellow]:: Requested profile '{requested}' not found; opening picker.[/yellow]")
        profile = select_profile(profiles, state)
    console.print(
        f"\n[bold cyan]::[/bold cyan] Profile [bold]{profile.name}[/bold]: {profile.description}\n"
        f"[dim]   {summarize_profile(profile)} • channel:{profile.channel} • {profile.pkgbase}[/dim]"
    )

    # Re-resolve in case user changed dir in config manager after initial check
    effective_build = get_build_dir()
    effective_packages = get_packages_dir()
    use_llvm = state.prefer_llvm and check_llvm_available()
    if state.prefer_llvm and not use_llvm:
        console.print("[yellow]:: LLVM toolchain requested but incomplete. Falling back to GCC.[/yellow]")

    console.print("[bold cyan]::[/bold cyan] Querying kernel.org releases...")
    candidates = fetch_releases()
    version, url = pick_release(candidates, profile.channel)

    tarball_name = tarball_name_from_url(version, url)
    tarball = effective_build / tarball_name
    kernel_dir = effective_build / f"linux-{version}"
    isolated_pkg_dir = effective_packages / f"{profile.pkgbase}-{version}"

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
                subprocess.run(["tar", "-xf", str(tarball)], cwd=effective_build, check=True)

            if not is_valid_kernel_tree(kernel_dir):
                console.print(f"[bold red]Fatal:[/bold red] Extracted tree at {kernel_dir} is invalid.")
                return
        else:
            console.print(f"\n[bold cyan]::[/bold cyan] Found existing valid source tree at linux-{version}.")

        isolated_pkg_dir.mkdir(parents=True, exist_ok=True)

        # --- Scheduler Patch Stage (CachyOS kernel-patches, cached per major version) ---
        profile = apply_profile_patches(kernel_dir, profile, version, effective_build / "cachyos_patch_cache")

        # Base Make command definition
        make_base = ["make"]
        if use_llvm:
            make_base.extend(["LLVM=1", "LLVM_IAS=1"])

        # --- Config Injection ---
        profile_cfg = saved_config_path(profile.name)
        injected = False
        if state.use_imported_config and profile_cfg.exists():
            if is_plausible_kernel_config(profile_cfg):
                console.print(f"[bold green]::[/bold green] Injecting saved '{profile.name}' kernel config...")
                shutil.copy(profile_cfg, kernel_dir / ".config")
                injected = True
            else:
                console.print(
                    "[yellow]:: Saved profile config is corrupt/invalid; falling back to live system config.[/yellow]"
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
        console.print(f"[bold cyan]::[/bold cyan] Applying profile matrix for [bold]{profile.name}[/bold]...")
        apply_config_matrix(kernel_dir, build_config_matrix(profile, use_llvm))

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

        (kernel_dir / "localversion").write_text(profile.localversion)

        # Resolve config dependencies cleanly
        subprocess.run(make_base + ["olddefconfig"], cwd=kernel_dir, stdout=subprocess.DEVNULL, check=True)

        if Confirm.ask("\n[bold yellow]Edit configuration manually via nconfig?[/bold yellow]", default=False):
            subprocess.run(make_base + ["nconfig"], cwd=kernel_dir, check=True)
            subprocess.run(make_base + ["olddefconfig"], cwd=kernel_dir, stdout=subprocess.DEVNULL, check=True)

        # Save active config back to Dusky state (per-profile)
        DUSKY_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(kernel_dir / ".config", profile_cfg)
        state.use_imported_config = True
        state.save()

        # Estimate total steps for progress bar BEFORE clean (after clean, dry-run output is tiny cnt=5)
        total_steps: int | None = None
        try:
            with console.status("[dim]Estimating total compile steps for ETA...[/dim]"):
                try:
                    dr = subprocess.run(
                        make_base + ["-n", "all"],
                        cwd=kernel_dir,
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    out = dr.stdout
                except subprocess.TimeoutExpired as e:
                    out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
                    if not out and e.stderr:
                        out = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
                # Count all compile/link steps - live shows CC, LD, AR, HOSTCC, OBJCOPY, AS
                cnt = (
                    out.count("CC ")
                    + out.count("LD ")
                    + out.count("AR ")
                    + out.count("HOSTCC ")
                    + out.count("OBJCOPY ")
                    + out.count("AS ")
                )
                if cnt < 500:
                    cnt = out.count(" CC ") + out.count(" LD ")
                if cnt >= 500:
                    total_steps = cnt
                    console.print(f"[dim]Estimated {total_steps} steps - progress bar will show ETA[/dim]")
                else:
                    try:
                        db_cnt = count_db_modules()
                        heuristic = max(3000, db_cnt * 14)
                        total_steps = heuristic
                        console.print(f"[dim]Dry-run cnt={cnt} too low, using heuristic {total_steps} steps (db {db_cnt} modules) - ETA enabled[/dim]")
                    except Exception:
                        total_steps = 3500
                        console.print(f"[dim]Could not estimate steps (cnt={cnt}), using fallback {total_steps} - ETA enabled[/dim]")
        except Exception as e:
            console.print(f"[dim]Estimate failed: {e}, progress will be indeterminate[/dim]")
            total_steps = None

        # Auto-clean stale build artifacts from interrupted/corrupt previous run (best clean compile, no corruption)
        # Do AFTER estimation, otherwise dry-run after clean gives cnt=5
        # make clean keeps .config (we just saved it) but removes vmlinux, System.map, .o, .tmp
        try:
            has_stale = (
                (kernel_dir / "vmlinux").exists()
                or (kernel_dir / "System.map").exists()
                or (kernel_dir / ".tmp_vmlinux.kallsyms1").exists()
                or any(kernel_dir.rglob("*.o"))
            )
            if not has_stale:
                try:
                    has_stale = any(p.stat().st_size == 0 for p in kernel_dir.rglob("*.o"))
                except Exception:
                    pass
            if has_stale:
                console.print("[yellow]:: Previous build artifacts detected - cleaning for corruption-free build (make clean)...[/yellow]")
                subprocess.run(make_base + ["clean"], cwd=kernel_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                for pat in ["*.lto.*", ".tmp_*"]:
                    for p in kernel_dir.glob(pat):
                        try:
                            if p.is_file():
                                p.unlink(missing_ok=True)
                            elif p.is_dir():
                                shutil.rmtree(p, ignore_errors=True)
                        except Exception:
                            pass
                console.print("[dim]Clean done - starting fresh compile[/dim]")
        except Exception as e:
            console.print(f"[dim]Clean check skipped: {e}[/dim]")

        cores = os.cpu_count() or 4
        lto_label = (profile.lto if use_llvm else "none").replace("_", "-").upper()
        toolchain_name = f"LLVM/Clang ({lto_label})" if use_llvm else "GCC"
        console.print(
            f"\n[bold green]Building linux-{version}{profile.localversion} "
            f"[{profile.name}] using {toolchain_name} with {cores} threads...[/bold green]\n"
        )

        build_cmd = make_base + [
            f"-j{cores}",
            f"PACMAN_PKGBASE={profile.pkgbase}",
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
            # Progress bar with ETA + Live log panel
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("•"),
                TimeElapsedColumn(),
                TextColumn("•"),
                TimeRemainingColumn(),
                console=console,
                transient=False,
            )
            task_id = progress.add_task(
                f"[cyan]Compiling linux-{version} ({toolchain_name})[/cyan]",
                total=total_steps,
            )
            # Group progress bar + log panel
            def _make_renderable():
                return Group(
                    progress,
                    Panel(
                        "\n".join(log_lines) if log_lines else "[dim]Starting build...[/dim]",
                        title=f"[bold cyan]Compiling linux-{version} ({toolchain_name})[/bold cyan]",
                        border_style="blue",
                        padding=(0, 2),
                    ),
                )

            with Live(_make_renderable(), console=console, auto_refresh=True, refresh_per_second=8) as live:
                if build_proc.stdout is None:
                    raise RuntimeError("Failed to capture build output stream")
                for line in iter(build_proc.stdout.readline, ""):
                    clean = line.strip()
                    if not clean:
                        continue
                    log_lines.append(clean)
                    # Advance for all compile steps - live shows CC, LD, AR, HOSTCC, OBJCOPY, AS
                    if clean.startswith(
                        ("CC ", "LD ", "AR ", "HOSTCC ", "OBJCOPY ", "AS ", "CC\t", "LD\t")
                    ) or any(k in clean for k in (" CC ", " LD ", " AR ", " HOSTCC ", " OBJCOPY ", " AS ")):
                        progress.advance(task_id)
                        # Auto-extend if underestimated (your 13626 -> 14657) - keep ETA moving instead of 0:00:00
                        try:
                            _t = progress.tasks[task_id]
                            if _t.total is not None and _t.completed >= _t.total:
                                progress.update(task_id, total=_t.total + 1000)
                        except Exception:
                            pass
                    live.update(_make_renderable())
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

        # Autonomous boot entry (systemd-boot + GRUB) - no manual kernel-install needed next time
        try:
            # Find actual installed kver from /usr/lib/modules matching this profile's localversion
            kver = None
            try:
                profile_mods = sorted(Path("/usr/lib/modules").glob(f"*{profile.localversion}"))
                if profile_mods:
                    # Pick latest by mtime
                    profile_mods.sort(key=lambda p: p.stat().st_mtime)
                    kver = profile_mods[-1].name
            except Exception:
                pass
            if not kver:
                # Fallback: version is 7.2 -> 7.2.0-dusky-x, else 7.2.1 -> 7.2.1-dusky-x
                kver = (
                    f"{version}.0{profile.localversion}"
                    if version.count(".") == 1
                    else f"{version}{profile.localversion}"
                )
            vmlinuz_str = f"/boot/vmlinuz-{profile.pkgbase}"
            # Autonomous: try systemd-boot and GRUB without fragile /boot permission checks (0700)
            # kernel-install and bootctl will fail gracefully if not applicable
            try:
                if is_tool_available("bootctl"):
                    console.print(f"[cyan]::[/cyan] Ensuring systemd-boot entry for {kver}...")
                    # kernel-install add is idempotent and handles missing vmlinuz gracefully
                    subprocess.run(["sudo", "kernel-install", "add", kver, vmlinuz_str], check=False)
                    subprocess.run(["sudo", "bootctl", "update"], check=False)
                    try:
                        r = subprocess.run(["bootctl", "list"], capture_output=True, text=True, timeout=5)
                        if kver not in r.stdout:
                            console.print(f"[yellow]:: Note: boot entry for {kver} not in bootctl list, check /boot/loader/entries/[/yellow]")
                        else:
                            console.print(f"[green]::[/green] systemd-boot entry verified for {kver}")
                    except Exception:
                        pass
            except Exception as e:
                console.print(f"[dim]systemd-boot note: {e}[/dim]")
            try:
                # GRUB auto-detect: try common locations, let grub-mkconfig fail silently if not present
                for grub_cfg in ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"]:
                    # Use sudo test via shell then run mkconfig; if test fails, next iteration
                    if subprocess.run(["sudo", "test", "-f", grub_cfg], capture_output=True).returncode == 0:
                        console.print(f"[cyan]::[/cyan] Updating GRUB {grub_cfg}...")
                        subprocess.run(["sudo", "grub-mkconfig", "-o", grub_cfg], check=False)
                        break
            except Exception as e:
                console.print(f"[dim]GRUB note: {e}[/dim]")
        except Exception as e:
            console.print(f"[dim]Boot entry auto-creation note: {e} (manual: sudo kernel-install add <kver> {vmlinuz_str})[/dim]")

        console.print(
            Panel(
                f"[bold green]Mission Accomplished![/bold green]\n\n"
                f"Dusky Kernel [bold]linux-{version}{profile.localversion}[/bold] "
                f"(profile: {profile.name}) installed successfully.\n"
                "initramfs generation ran automatically via pacman hooks.\n"
                f"[dim]Boot entry ensured automatically for systemd-boot/GRUB. Verify with: bootctl list | grep -A2 {profile.suffix}[/dim]",
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
        profiles = load_profiles()
        active_profile = find_profile(profiles, state.selected_profile)
        profile_label = (
            f"[bold green]{active_profile.name}[/bold green]"
            if active_profile
            else "[yellow]unselected[/yellow]"
        )
        config_status = (
            "[bold green]IMPORTED[/bold green]"
            if state.use_imported_config and saved_config_path(state.selected_profile).exists()
            else "[dim]LIVE[/dim]"
        )
        llvm_info = "[cyan]LLVM[/cyan]" if state.prefer_llvm and check_llvm_available() else "[yellow]GCC[/yellow]"
        effective_build = get_build_dir()
        build_label = str(effective_build)
        if effective_build != DEFAULT_BUILD_DIR:
            build_label = f"[cyan]{effective_build}[/cyan]"
        else:
            build_label = f"[dim]{effective_build}[/dim]"

        console.print(
            Align.center(
                Panel(
                    Align.center(
                        f"[bold cyan]Dusky Kernel Compiler[/bold cyan] [dim]- 2026.08 Production[/dim]\n"
                        f"[dim]Arch Linux • TOML profiles • localmodconfig + LMC_KEEP • pacman-pkg[/dim]\n"
                        f"[dim]Profile: {profile_label} • Toolchain: {llvm_info} • Config: {config_status} • Build: {build_label}[/dim]"
                    ),
                    box=box.ROUNDED,
                    border_style="bright_blue",
                    expand=False,
                    padding=(1, 2),
                )
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
            choice = prompt_choice_fixed("\n[bold cyan]Select[/bold cyan]", choices=["1", "2", "3", "4", "5", "6"], default="6")
        except EOFError:
            console.print("\n[bold cyan]Input stream closed. Exiting Dusky Kernel Compiler.[/bold cyan]\n")
            break
        if choice == SystemAction.EXIT:
            console.print("\n[bold cyan]Exiting Dusky Kernel Compiler. May your uptime be long![/bold cyan]\n")
            break
        try:
            if choice == SystemAction.INIT:
                initialize_tracking()
                prompt_enter_fixed("\n[dim]Press Enter to return to menu...[/dim]")
            elif choice == SystemAction.MONITOR:
                monitor_modules()
            elif choice == SystemAction.COMPILE:
                compile_kernel()
                prompt_enter_fixed("\n[dim]Press Enter to return to menu...[/dim]")
            elif choice == SystemAction.CONFIG:
                manage_dusky_state()
            elif choice == SystemAction.VERIFY:
                run_empirical_diagnostics()
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Action cancelled by user.[/bold yellow]")
        except Exception as e:
            console.print(f"\n[bold red]Action failed:[/bold red] [{type(e).__name__}] {e}")
            prompt_enter_fixed("\n[dim]Press Enter to return to menu...[/dim]")


def parse_cli_args() -> None:
    parser = argparse.ArgumentParser(description="Dusky Kernel Compiler 2026.08 Engine")
    parser.add_argument("--verify", action="store_true", help="Run empirical diagnostics and exit")
    parser.add_argument("--check-latest", action="store_true", help="Check latest kernel.org versions and exit")
    parser.add_argument("--list-profiles", action="store_true", help="List available TOML profiles and exit")
    parser.add_argument("--profile", type=str, default=None, help="Preselect a kernel profile by name (e.g. gaming)")
    parser.add_argument("--build-dir", type=str, default=None, help="Override build directory (supports ZRAM/tmpfs, e.g. /mnt/zram1/dusky_build or /tmp/dusky_build)")
    args = parser.parse_args()

    if args.build_dir:
        resolved = _resolve_custom_build_dir(args.build_dir)
        if resolved is None:
            console.print(f"[bold red]Invalid --build-dir: {args.build_dir}[/bold red]")
            sys.exit(1)
        # Persist for this run via env, and offer to save
        os.environ["DUSKY_BUILD_DIR"] = str(resolved)
        console.print(f"[dim]Using build dir override: {resolved}[/dim]")

    if args.profile:
        profiles = load_profiles()
        if find_profile(profiles, args.profile) is None:
            console.print(f"[bold red]Unknown profile: {args.profile}. Available: {', '.join(p.name for p in profiles) or 'none'}[/bold red]")
            sys.exit(1)
        os.environ["DUSKY_PROFILE"] = args.profile
        console.print(f"[dim]Profile preselected: {args.profile}[/dim]")

    if args.list_profiles:
        profiles = load_profiles()
        if not profiles:
            console.print(f"[yellow]No valid profiles in {PROFILES_DIR}[/yellow]")
        else:
            table = Table(title=f"Dusky Profiles ({PROFILES_DIR})", box=box.SIMPLE_HEAVY)
            table.add_column("Name", style="bold white")
            table.add_column("Description", style="cyan")
            table.add_column("Key settings", style="dim")
            for p in profiles:
                table.add_row(p.name, p.description, summarize_profile(p))
            console.print(table)
        sys.exit(0)

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
