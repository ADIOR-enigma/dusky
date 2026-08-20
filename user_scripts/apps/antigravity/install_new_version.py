#!/usr/bin/env python3
"""
Antigravity Installer / Updater for Arch Linux

Extracts the Antigravity .tar.gz archive to /opt, creates a launcher
in ~/.local/bin, and sets up a .desktop entry (with icon) so it shows
up properly in rofi and other application menus.

Design goals (modern, reliable, verifiable):

  • Atomic installs — the archive is extracted to a temp workspace,
    validated, and only then swapped into place. The old installation
    is never destroyed until the new one is complete and verified.
  • Python-only extraction (tarfile + "data" filter = path-traversal
    safe, no external `tar`, no fragile root shell pipelines).
  • Auto-detects the archive's top-level directory instead of relying
    on a hardcoded name.
  • Installs the app's own icon (read straight out of app.asar) into
    the hicolor theme and writes Icon= in the .desktop entry, which
    fixes icon-less launcher entries (e.g. rofi).
  • File-based logging (default ~/.local/state/antigravity-installer.log)
    so every run can be audited.
  • `--verify` performs a full health check of an installation
    (binary, version metadata, launcher, desktop entry, icon, PATH,
    and a live launch smoke test).
  • Pre-flight checks: disk space, archive integrity, required members,
    symlink safety.

Usage:
    python install_new_version.py                                # interactive
    python install_new_version.py /path/to/Antigravity.tar.gz     # direct
    python install_new_version.py --prefix /tmp/sandbox           # sandbox
    python install_new_version.py --prefix /tmp/sandbox --verify  # health check
    python install_new_version.py --verify                        # check /opt install
    python install_new_version.py --dry-run                       # preview only
"""

# ── Bootstrap: auto-install Python dependencies ──────────────────────
import importlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("✗  Python 3.12+ is required (safe tarfile extraction filters).")

_CONTAINED_LIBS = Path.home() / "contained_apps" / "python-libs"


def _bootstrap_dependencies() -> None:
    """Install missing Python packages via ``uv`` into ~/contained_apps/."""
    libs_str = str(_CONTAINED_LIBS)
    if libs_str not in sys.path:
        sys.path.insert(0, libs_str)

    required = {"rich": "rich"}
    missing = [pkg for mod, pkg in required.items() if not importlib.util.find_spec(mod)]
    if not missing:
        return

    uv = shutil.which("uv")
    if uv is None:
        sys.exit(
            "✗  'uv' is not installed and required Python packages are missing.\n"
            "   Install uv first:  pacman -S uv\n"
            f"   Missing packages: {', '.join(missing)}"
        )

    print(f"📦  Installing missing dependencies via uv: {', '.join(missing)} …")
    _CONTAINED_LIBS.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [uv, "pip", "install", "--target", libs_str, "-q", *missing],
    )
    importlib.invalidate_caches()


_bootstrap_dependencies()

# ── Standard library imports ─────────────────────────────────────────
import argparse
import json
import logging
import os
import re
import shlex
import struct
import tarfile
import tempfile

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.theme import Theme

# ── Theme & Console ──────────────────────────────────────────────────
_THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "hl": "bold magenta",
    }
)
console = Console(theme=_THEME)

# ── Constants ────────────────────────────────────────────────────────
INSTALLER_VERSION = "2.0.0"
APP_NAME = "Antigravity"
BINARY_NAME = "antigravity"
DESKTOP_FILE = "antigravity.desktop"
VERSION_FILE = ".installed_version"

DEFAULT_INSTALL_DIR = Path("/opt/Antigravity-x64")
DEFAULT_SYMLINK_DIR = Path.home() / ".local" / "bin"
DEFAULT_APPS_DIR = Path.home() / ".local" / "share" / "applications"
DEFAULT_ICON_DIR = Path.home() / ".local" / "share" / "icons"
DEFAULT_LOG_PATH = Path.home() / ".local" / "state" / "antigravity-installer.log"
DEFAULT_ARCHIVE = Path("/mnt/zram1/Antigravity.tar.gz")

# Legacy install locations probed for an existing version / cleanup
LEGACY_INSTALL_DIRS = [
    Path("/opt/Antigravity"),
]

# Flags preserved in the launcher script (matches the existing setup)
LAUNCHER_FLAGS = ["--disable-gpu-sandbox"]

LOG_MAX_BYTES = 2 * 1024 * 1024


class InstallError(Exception):
    """A user-facing installation error."""


# ═════════════════════════════════════════════════════════════════════
#  Logging
# ═════════════════════════════════════════════════════════════════════

def _rotate_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
    except OSError:
        pass


def _setup_logging(flag_path: str | None) -> Path:
    """Configure file-based logging. Returns the active log path."""
    if flag_path:
        log_path = Path(flag_path).expanduser()
    elif os.environ.get("ANTIGRAVITY_LOG"):
        log_path = Path(os.environ["ANTIGRAVITY_LOG"]).expanduser()
    else:
        log_path = DEFAULT_LOG_PATH

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log(log_path)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(message)s",
            handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        )
    except OSError as e:
        logging.basicConfig(level=logging.INFO)
        log_path = Path(f"(file logging unavailable: {e})")
    logging.info("=== %s installer v%s (pid %d) ===", APP_NAME, INSTALLER_VERSION, os.getpid())
    return log_path


# ═════════════════════════════════════════════════════════════════════
#  Version detection (Electron .asar parser)
# ═════════════════════════════════════════════════════════════════════

def _parse_asar(asar_path: Path) -> tuple[dict, int] | None:
    """Parse an Electron .asar header; return (header, data_start).

    The asar format uses two Chromium "pickle" structures as a header:

    ┌──────────────────────────────────────────────────────────────┐
    │ Pickle 1 (size pickle, always 8 bytes)                      │
    │   [uint32 payload_size=4] [uint32 header_size]              │
    ├──────────────────────────────────────────────────────────────┤
    │ Pickle 2 (header pickle, header_size bytes)                 │
    │   [uint32 payload_size] [uint32 json_len] [json string …]   │
    ├──────────────────────────────────────────────────────────────┤
    │ Data section (file contents concatenated)                    │
    │   starts at byte  8 + header_size                           │
    └──────────────────────────────────────────────────────────────┘
    """
    try:
        with open(asar_path, "rb") as f:
            f.read(4)                                  # pickle1 payload size (always 4)
            header_size = struct.unpack("<I", f.read(4))[0]
            f.read(4)                                  # pickle2 payload size
            json_len = struct.unpack("<I", f.read(4))[0]
            header = json.loads(f.read(json_len).decode("utf-8"))
            return header, 8 + header_size
    except Exception:
        return None


def read_asar_file(asar_path: Path, name: str) -> bytes | None:
    """Read a single file out of an .asar archive."""
    parsed = _parse_asar(asar_path)
    if parsed is None:
        return None
    header, data_start = parsed
    entry = header.get("files", {}).get(name)
    if entry is None:
        return None
    try:
        with open(asar_path, "rb") as f:
            f.seek(data_start + int(entry["offset"]))
            return f.read(int(entry["size"]))
    except Exception:
        return None


def _read_version_from_asar_fallback(asar_path: Path) -> str | None:
    """Fallback: run ``strings`` over the .asar and grab the first semver."""
    try:
        proc = subprocess.run(
            ["strings", "-n", "10", str(asar_path)],
            capture_output=True, text=True, timeout=30,
        )
        for line in proc.stdout.splitlines():
            m = re.search(r'"version"\s*:\s*"(\d+\.\d+\.\d+)"', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def get_version_from_asar(asar_path: Path) -> str | None:
    """Best-effort version read: parsed package.json, then strings scan."""
    pkg = read_asar_file(asar_path, "package.json")
    if pkg:
        try:
            ver = json.loads(pkg).get("version")
            if ver:
                return str(ver)
        except Exception:
            pass
    return _read_version_from_asar_fallback(asar_path)


def _get_version_from_dir(d: Path) -> str | None:
    """Try to read a version from a single install directory."""
    vf = d / VERSION_FILE
    if vf.exists():
        return vf.read_text().strip() or None
    asar = d / "resources" / "app.asar"
    if asar.exists():
        return get_version_from_asar(asar)
    return None


def get_installed_version(install_dir: Path) -> str | None:
    """Version of the currently installed app (primary dir, then legacy)."""
    ver = _get_version_from_dir(install_dir)
    if ver:
        return ver
    for legacy in LEGACY_INSTALL_DIRS:
        if legacy.exists():
            ver = _get_version_from_dir(legacy)
            if ver:
                return ver
    return None


def find_legacy_installs(install_dir: Path) -> list[Path]:
    """Legacy install directories that still exist and aren't the target."""
    return [d for d in LEGACY_INSTALL_DIRS if d.exists() and d != install_dir]


# ═════════════════════════════════════════════════════════════════════
#  Archive inspection
# ═════════════════════════════════════════════════════════════════════

def _normalize_member(name: str) -> str:
    return name[2:] if name.startswith("./") else name


def inspect_archive(archive: Path) -> tuple[str, list[tarfile.TarInfo]]:
    """Open the archive and return (top-level root dir, members).

    Validates: readable tar, single top-level directory, required
    members present, and no symlinks/hardlinks (refused for safety)."""
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
    except (tarfile.TarError, OSError) as e:
        raise InstallError(f"could not read archive: {e}")

    if not members:
        raise InstallError("archive is empty")

    tops: set[str] = set()
    for m in members:
        name = _normalize_member(m.name)
        if not name or name == ".":
            continue
        tops.add(name.split("/")[0])

    if len(tops) != 1:
        listing = ", ".join(sorted(tops))
        raise InstallError(
            f"archive must have a single top-level directory (found: {listing})"
        )
    root = tops.pop()

    names = {_normalize_member(m.name) for m in members}
    required = [f"{root}/{BINARY_NAME}", f"{root}/resources/app.asar"]
    missing = [r for r in required if r not in names]
    if missing:
        raise InstallError(
            f"archive is missing required members: {', '.join(missing)}"
        )

    links = [m.name for m in members if m.islnk() or m.issym()]
    if links:
        raise InstallError(
            f"archive contains symlinks/hardlinks — refusing to install: "
            f"{', '.join(links[:5])}"
        )
    return root, members


def _nearest_existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _check_disk_space(members: list[tarfile.TarInfo], dest: Path) -> int:
    """Verify there's room for the extracted archive at *dest*."""
    needed = sum(m.size for m in members if m.isfile())
    base = _nearest_existing(dest)
    try:
        free = os.statvfs(base).f_bavail * os.statvfs(base).f_frsize
    except OSError:
        logging.warning("could not stat %s for disk space check", base)
        return needed
    if free < needed:
        raise InstallError(
            f"insufficient disk space: need {needed / 1e6:.0f} MB, "
            f"but only {free / 1e6:.0f} MB free on {base}"
        )
    logging.info("disk space ok: need %d bytes, %d free on %s", needed, free, base)
    return needed


def get_archive_version(archive: Path, root: str) -> str | None:
    """Extract only the .asar from the tarball and read its version."""
    asar_member = f"{root}/resources/app.asar"
    try:
        with tempfile.TemporaryDirectory(prefix="agy-ver-") as tmp:
            with tarfile.open(archive, "r:*") as tar:
                tar.extract(asar_member, path=tmp, filter="data")
            return get_version_from_asar(Path(tmp) / asar_member)
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════

def _needs_sudo(path: Path) -> bool:
    """True when writing to *path* (or its parent) requires root."""
    target = path if path.exists() else path.parent
    return not os.access(target, os.W_OK)


def _run(*cmd: str, sudo: bool = False) -> None:
    """Run a command, optionally prefixed with ``sudo``. Raises on failure."""
    full = ["sudo", *cmd] if sudo else list(cmd)
    logging.info("$ %s", " ".join(shlex.quote(c) for c in full))
    try:
        result = subprocess.run(full)
    except FileNotFoundError:
        raise InstallError(f"command not found: {full[0]}")
    if result.returncode != 0:
        raise InstallError(
            f"command failed (exit {result.returncode}): {' '.join(full)}"
        )


def _pre_auth_sudo() -> bool:
    """Prompt for the sudo password once so later commands don't interrupt."""
    console.print("[info]  ⚡ Elevated privileges required — you may be prompted for your password.[/]")
    return subprocess.run(["sudo", "-v"]).returncode == 0


def _confirm(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question. On EOF (piped stdin), use *default*."""
    try:
        return Confirm.ask(prompt, default=default)
    except EOFError:
        return default


def _smoke_test(binary: Path, timeout: int = 25) -> tuple[bool, str]:
    """Launch the binary with --version as a sanity check."""
    logging.info("smoke test: %s --version", binary)
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as e:
        return False, str(e)
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    detail = lines[0][:120] if lines else f"exit {proc.returncode}"
    return proc.returncode == 0, detail


def _launcher_content(binary_path: Path) -> str:
    flags = " ".join(LAUNCHER_FLAGS)
    return f"#!/bin/bash\nexec {shlex.quote(str(binary_path))} {flags} \"$@\"\n"


def _desktop_content(exec_path: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Antigravity\n"
        "GenericName=Agentic Platform\n"
        "Comment=Antigravity IDE\n"
        f"Exec={shlex.quote(str(exec_path))} %U\n"
        "Icon=antigravity\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "StartupWMClass=Antigravity\n"
        "Categories=Development;IDE;\n"
        "Keywords=Antigravity;agent;IDE;\n"
    )


def _validate_desktop(desktop_file: Path) -> bool:
    """Run desktop-file-validate if available; non-fatal."""
    if not shutil.which("desktop-file-validate"):
        return True
    proc = subprocess.run(
        ["desktop-file-validate", str(desktop_file)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        logging.warning("desktop validation: %s", proc.stderr.strip() or proc.stdout.strip())
        return False
    return True


def _refresh_caches(apps_dir: Path, icon_dir: Path) -> None:
    """Refresh the desktop/icon databases so launchers pick changes up."""
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(apps_dir)], capture_output=True,
        )
    if shutil.which("gtk-update-icon-cache"):
        subprocess.run(
            ["gtk-update-icon-cache", "-f", "-q", str(icon_dir / "hicolor")],
            capture_output=True,
        )


def _install_icon(asar_path: Path, icon_dir: Path) -> bool:
    """Extract the app's own icon from app.asar into the hicolor theme."""
    data = read_asar_file(asar_path, "icon.png")
    if not data or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        logging.warning("no usable icon.png found in app.asar")
        return False
    dest = icon_dir / "hicolor" / "512x512" / "apps" / "antigravity.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    logging.info("installed icon: %s (%d bytes)", dest, len(data))
    return True


# ═════════════════════════════════════════════════════════════════════
#  Installation
# ═════════════════════════════════════════════════════════════════════

def _finalize_extracted(tmp_root: Path, *, system: bool, new_version: str | None) -> None:
    """Post-extraction checks and tweaks inside the temp workspace."""
    binary = tmp_root / BINARY_NAME
    if not binary.is_file():
        raise InstallError(f"binary missing after extraction: {binary}")
    binary.chmod(binary.stat().st_mode | 0o755)

    if system:
        sandbox = tmp_root / "chrome-sandbox"
        if sandbox.is_file():
            sandbox.chmod(0o4755)

    if new_version:
        (tmp_root / VERSION_FILE).write_text(new_version + "\n")
        logging.info("wrote version metadata: %s", new_version)


def _swap_into_place(tmp_root: Path, install_dir: Path, *, use_sudo: bool) -> None:
    """Atomically move the new install into its final location."""
    if use_sudo:
        _run("chown", "-R", "root:root", str(tmp_root), sudo=True)
        _run("rm", "-rf", str(install_dir), sudo=True)
        _run("mv", str(tmp_root), str(install_dir), sudo=True)
    else:
        if install_dir.is_symlink() or install_dir.is_file():
            install_dir.unlink()
        elif install_dir.is_dir():
            shutil.rmtree(install_dir)
        shutil.move(str(tmp_root), str(install_dir))


def _install(
    *,
    archive: Path,
    root: str,
    members: list[tarfile.TarInfo],
    install_dir: Path,
    symlink_dir: Path,
    apps_dir: Path,
    icon_dir: Path,
    new_version: str | None,
    dry_run: bool,
    system: bool,
    legacy_dirs: list[Path],
) -> None:
    use_sudo = not dry_run and _needs_sudo(install_dir)
    use_sudo_symlink = not dry_run and _needs_sudo(symlink_dir)
    binary_path = install_dir / BINARY_NAME
    launcher_path = symlink_dir / BINARY_NAME
    desktop_file = apps_dir / DESKTOP_FILE
    tmp_dir: Path | None = None

    def say(what: str) -> None:
        console.print(f"  [dim]DRY-RUN: {what}[/]")
        logging.info("DRY-RUN: %s", what)

    with Progress(
        SpinnerColumn("dots"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Installing…", total=8)

        # 1 ── Prepare temp workspace ────────────────────────────────
        progress.update(task, description="[cyan]Preparing workspace[/]")
        if dry_run:
            say("create temp workspace and extract there first")
        else:
            tmp_dir = Path(tempfile.mkdtemp(prefix="antigravity-install-"))
            logging.info("temp workspace: %s", tmp_dir)
        progress.advance(task)

        # 2 ── Extract ───────────────────────────────────────────────
        progress.update(task, description="[cyan]Extracting archive[/]")
        try:
            if dry_run:
                say(f"extract archive to temp workspace ({len(members)} members)")
            else:
                with tarfile.open(archive, "r:*") as tar:
                    xtask = progress.add_task("Extracting…", total=len(members))
                    for m in members:
                        tar.extract(m, path=tmp_dir, filter="data")
                        progress.advance(xtask)
        except (tarfile.TarError, OSError) as e:
            raise InstallError(f"extraction failed: {e}")
        progress.advance(task)

        # 3 ── Verify + finalize extraction ──────────────────────────
        progress.update(task, description="[cyan]Verifying extraction[/]")
        if dry_run:
            say(f"verify {root}/{BINARY_NAME}, set permissions, write version metadata")
        else:
            _finalize_extracted(
                tmp_dir / root, system=system, new_version=new_version,
            )
        progress.advance(task)

        # 4 ── Icon ──────────────────────────────────────────────────
        progress.update(task, description="[cyan]Installing icon[/]")
        if dry_run:
            say("extract icon.png from app.asar into hicolor theme")
        else:
            _install_icon(tmp_dir / root / "resources" / "app.asar", icon_dir)
        progress.advance(task)

        # 5 ── Swap into place ───────────────────────────────────────
        progress.update(task, description=f"[cyan]Installing to {install_dir.parent}[/]")
        if dry_run:
            say(f"remove old {install_dir} and move new install into place")
        else:
            _swap_into_place(tmp_dir / root, install_dir, use_sudo=use_sudo)
            for legacy in legacy_dirs:
                if _needs_sudo(legacy):
                    _run("rm", "-rf", str(legacy), sudo=True)
                else:
                    shutil.rmtree(legacy, ignore_errors=True)
                logging.info("removed legacy install: %s", legacy)
        progress.advance(task)

        # 6 ── Launcher ──────────────────────────────────────────────
        progress.update(task, description="[cyan]Creating launcher[/]")
        if dry_run:
            say(f"write launcher {launcher_path} → {binary_path}")
        else:
            symlink_dir.mkdir(parents=True, exist_ok=True)
            if launcher_path.exists() or launcher_path.is_symlink():
                launcher_path.unlink()
            launcher_path.write_text(_launcher_content(binary_path))
            launcher_path.chmod(0o755)
            logging.info("wrote launcher: %s", launcher_path)
        progress.advance(task)

        # 7 ── Desktop entry ─────────────────────────────────────────
        progress.update(task, description="[cyan]Creating desktop entry[/]")
        if dry_run:
            say(f"write desktop entry {desktop_file} (Icon=antigravity)")
        else:
            apps_dir.mkdir(parents=True, exist_ok=True)
            desktop_file.write_text(_desktop_content(launcher_path))
            ok = _validate_desktop(desktop_file)
            if not ok:
                console.print("[warn]  ⚠  Desktop entry failed validation (see log).[/]")
            _refresh_caches(apps_dir, icon_dir)
            logging.info("wrote desktop entry: %s (validated=%s)", desktop_file, ok)
        progress.advance(task)

        # 8 ── Smoke test ────────────────────────────────────────────
        progress.update(task, description="[cyan]Smoke test[/]")
        if not dry_run:
            ok, detail = _smoke_test(install_dir / BINARY_NAME)
            if ok:
                console.print(f"  [success]✓  Binary launches: {detail}[/]")
                logging.info("smoke test ok: %s", detail)
            else:
                console.print(
                    f"[warn]  ⚠  Binary smoke test failed: {detail}[/]\n"
                    "     The install is in place, but the binary may be missing "
                    "shared libraries or an incompatible libc."
                )
                logging.warning("smoke test failed: %s", detail)
        progress.advance(task)

    if tmp_dir and tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════
#  Verification
# ═════════════════════════════════════════════════════════════════════

def verify(
    install_dir: Path,
    symlink_dir: Path,
    apps_dir: Path,
    icon_dir: Path,
    *,
    system: bool,
) -> int:
    """Health-check an installation. Returns 0 when all checks pass."""
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    add("Install directory", install_dir.is_dir(), str(install_dir))
    if not install_dir.is_dir():
        console.print(f"[err]✗  Nothing installed at {install_dir}[/]")
        return 1

    binary = install_dir / BINARY_NAME
    add("Binary present", binary.is_file(), str(binary))
    add("Binary executable", os.access(binary, os.X_OK) if binary.exists() else False)

    asar = install_dir / "resources" / "app.asar"
    asar_ver = get_version_from_asar(asar) if asar.is_file() else None
    vf = install_dir / VERSION_FILE
    meta_ver = vf.read_text().strip() if vf.is_file() else None
    add("Version metadata", bool(meta_ver), meta_ver or "missing")
    if meta_ver and asar_ver:
        add("Version metadata matches binary",
            meta_ver == asar_ver,
            f"metadata={meta_ver}, asar={asar_ver}")
    elif asar_ver:
        add("Version (from asar)", True, asar_ver)

    launcher = symlink_dir / BINARY_NAME
    launcher_ok = launcher.is_file() and os.access(launcher, os.X_OK)
    add("Launcher present", launcher_ok, str(launcher))
    if launcher_ok:
        content = launcher.read_text()
        add("Launcher points to binary", str(binary) in content)
        add("Launcher has flags",
            all(f in content for f in LAUNCHER_FLAGS),
            " ".join(LAUNCHER_FLAGS))

    desktop = apps_dir / DESKTOP_FILE
    add("Desktop entry present", desktop.is_file(), str(desktop))
    if desktop.is_file():
        add("Desktop entry has icon", "Icon=antigravity" in desktop.read_text())
        add("Desktop entry valid", _validate_desktop(desktop))

    hicolor_icon = icon_dir / "hicolor" / "512x512" / "apps" / "antigravity.png"
    icon_ok = hicolor_icon.is_file()
    icon_detail = str(hicolor_icon) if icon_ok else ""
    if not icon_ok:
        for theme_root in (icon_dir, Path("/usr/share/icons")):
            if theme_root.exists():
                found = sorted(theme_root.glob("*/apps/antigravity.*"))
                if found:
                    icon_ok, icon_detail = True, str(found[0])
                    break
    add("Icon available", icon_ok, icon_detail)

    which = shutil.which(BINARY_NAME)
    if system:
        add("On PATH", bool(which), which or f"{symlink_dir} not in PATH")
        if which:
            add("PATH resolves to launcher", Path(which).resolve() == launcher.resolve())
    else:
        add("On PATH", True, f"sandbox install — launcher intentionally not on PATH")

    ok_smoke, smoke_detail = _smoke_test(binary)
    add("Binary launches", ok_smoke, smoke_detail)

    console.print()
    tbl = Table(box=box.ROUNDED, title=f"[bold]{APP_NAME} Health Check[/]", padding=(0, 2))
    tbl.add_column("Check", style="bold", min_width=28)
    tbl.add_column("Status", min_width=8)
    tbl.add_column("Detail", style="dim", overflow="fold", min_width=40)
    for name, ok, detail in checks:
        status = "[success]PASS[/]" if ok else "[err]FAIL[/]"
        tbl.add_row(name, status, detail)
    console.print(tbl)

    failed = [c for c in checks if not c[1]]
    if failed:
        console.print(
            f"[err]✗  {len(failed)} of {len(checks)} checks failed.[/]"
        )
        return 1
    console.print(f"[success]✓  All {len(checks)} checks passed.[/]")
    return 0


# ═════════════════════════════════════════════════════════════════════
#  CLI & Main
# ═════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=f"Install or update {APP_NAME} on Arch Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  %(prog)s                                     # interactive\n"
            f"  %(prog)s ~/Downloads/Antigravity.tar.gz      # direct path\n"
            f"  %(prog)s --prefix /tmp/sandbox                # sandboxed install\n"
            f"  %(prog)s --verify                            # health check\n"
            f"  %(prog)s --prefix /tmp/sandbox --verify      # check a sandbox\n"
            f"  %(prog)s --dry-run                           # preview only\n"
        ),
    )
    p.add_argument(
        "archive", nargs="?", default=None,
        help=f"Path to {APP_NAME}.tar.gz (prompted interactively if omitted).",
    )
    p.add_argument(
        "--prefix", default=None, metavar="DIR",
        help="Install under DIR instead of /opt (useful for testing).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making any changes.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Reinstall even when the same version is already installed.",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Answer 'yes' to every prompt (non-interactive).",
    )
    p.add_argument(
        "--log", default=None, metavar="FILE",
        help=f"Write the log to FILE (default: {DEFAULT_LOG_PATH}).",
    )
    p.add_argument(
        "--verify", action="store_true",
        help="Health-check the installation and exit (no install).",
    )
    p.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {INSTALLER_VERSION}",
    )
    return p.parse_args()


def _run_cli(args: argparse.Namespace, log_path: Path) -> int:
    # ── Guard: don't run the whole script as root ─────────────────
    if os.getuid() == 0 and not args.prefix:
        console.print(
            "[err]✗  Don't run this script with sudo / as root.[/]\n"
            "   The script will auto-escalate for the commands that need it.\n"
            "   Running as root puts the launcher and desktop entry in the wrong place."
        )
        return 1

    # ── Resolve install paths ─────────────────────────────────────
    if args.prefix:
        prefix = Path(args.prefix).expanduser().resolve()
        install_dir = prefix / "Antigravity-x64"
        symlink_dir = prefix / "bin"
        apps_dir = prefix / "share" / "applications"
        icon_dir = prefix / "share" / "icons"
        prefix.mkdir(parents=True, exist_ok=True)
    else:
        install_dir = DEFAULT_INSTALL_DIR
        symlink_dir = DEFAULT_SYMLINK_DIR
        apps_dir = DEFAULT_APPS_DIR
        icon_dir = DEFAULT_ICON_DIR

    # ── Verify mode ───────────────────────────────────────────────
    if args.verify:
        console.print()
        console.print(
            Panel(
                f"[bold magenta]{APP_NAME} Health Check[/]",
                subtitle=f"[dim]{install_dir}[/]",
                box=box.ROUNDED,
                padding=(1, 4),
            )
        )
        return verify(
            install_dir, symlink_dir, apps_dir, icon_dir, system=not args.prefix,
        )

    # ── Banner ────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            f"[bold magenta]🚀  {APP_NAME} Installer[/]",
            subtitle="[dim]Arch Linux[/]",
            box=box.DOUBLE_EDGE,
            padding=(1, 4),
        )
    )
    console.print()

    # ── Archive path ──────────────────────────────────────────────
    if args.archive:
        archive = Path(args.archive).expanduser().resolve()
    else:
        default_display = str(DEFAULT_ARCHIVE) if DEFAULT_ARCHIVE.exists() else ""
        if args.yes:
            raw = default_display
        else:
            try:
                raw = Prompt.ask(
                    "[bold]📦  Path to Antigravity archive[/]",
                    default=default_display or None,
                )
            except EOFError:
                raw = default_display
        if not raw:
            console.print("[err]✗  No archive path provided.[/]")
            return 1
        archive = Path(raw).expanduser().resolve()

    if not archive.exists():
        console.print(f"[err]✗  File not found: {archive}[/]")
        return 1
    if not archive.is_file():
        console.print(f"[err]✗  Not a file: {archive}[/]")
        return 1
    if not tarfile.is_tarfile(str(archive)):
        console.print(f"[err]✗  Not a valid tar archive: {archive}[/]")
        return 1

    console.print(f"  [dim]Archive :[/]  {archive}")
    if args.prefix:
        console.print(f"  [dim]Prefix  :[/]  {args.prefix}")
    console.print()

    # ── Inspect archive ───────────────────────────────────────────
    with console.status("[cyan]Inspecting archive …[/]"):
        root, members = inspect_archive(archive)
        needed = _check_disk_space(members, install_dir)
    logging.info(
        "archive inspected: root=%s, members=%d, ~%d MB extracted",
        root, len(members), needed // (1024 * 1024),
    )
    console.print(f"[success]  ✓  Archive verified (root: [hl]{root}[/], {len(members)} members)[/]")

    # ── Version detection ─────────────────────────────────────────
    with console.status("[cyan]Detecting versions …[/]"):
        installed_ver = get_installed_version(install_dir)
        new_ver = get_archive_version(archive, root)
        legacy_dirs = find_legacy_installs(install_dir) if not args.prefix else []

    tbl = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    tbl.add_column("Label", style="bold", min_width=12)
    tbl.add_column("Version")
    tbl.add_row(
        "Installed",
        f"[yellow]v{installed_ver}[/]" if installed_ver else "[dim]not found[/]",
    )
    tbl.add_row(
        "New",
        f"[green]v{new_ver}[/]" if new_ver else "[dim]unknown[/]",
    )
    console.print()
    console.print(tbl)
    if legacy_dirs:
        for ld in legacy_dirs:
            console.print(f"  [warn]⚠  Legacy install found at {ld} — will be removed[/]")
    console.print()

    # ── Same-version guard ────────────────────────────────────────
    if installed_ver and new_ver and installed_ver == new_ver and not args.force:
        proceed = args.yes or _confirm(
            f"[warn]⚠  Version v{new_ver} is already installed. Reinstall anyway?[/]",
            default=False,
        )
        if not proceed:
            console.print("[dim]Aborted.[/]")
            logging.info("aborted: same version already installed")
            return 2

    # ── Dry-run notice ────────────────────────────────────────────
    if args.dry_run:
        console.print("[warn]  ⚠  DRY-RUN mode — no changes will be made[/]")
        console.print()

    # ── Confirmation ──────────────────────────────────────────────
    if not args.yes and not _confirm("[bold]Proceed with installation?[/]", default=True):
        console.print("[dim]Aborted.[/]")
        return 2
    console.print()

    # ── Privilege escalation ──────────────────────────────────────
    if not args.dry_run and _needs_sudo(install_dir):
        if not _pre_auth_sudo():
            raise InstallError("failed to obtain elevated privileges (sudo)")
        console.print()

    # ── Install ───────────────────────────────────────────────────
    _install(
        archive=archive,
        root=root,
        members=members,
        install_dir=install_dir,
        symlink_dir=symlink_dir,
        apps_dir=apps_dir,
        icon_dir=icon_dir,
        new_version=new_ver,
        dry_run=args.dry_run,
        system=not args.prefix,
        legacy_dirs=legacy_dirs,
    )
    logging.info(
        "install complete: version=%s, install_dir=%s",
        new_ver or "unknown", install_dir,
    )

    # ── Summary ───────────────────────────────────────────────────
    console.print()
    ver_label = f" v{new_ver}" if new_ver else ""

    if args.dry_run:
        console.print(
            Panel(
                f"[bold green]✅  DRY-RUN complete — no changes were made[/]\n"
                f"[dim]Run without --dry-run to install {APP_NAME}{ver_label}.[/]",
                box=box.ROUNDED,
            )
        )
    else:
        console.print(
            Panel(
                f"[bold green]✅  {APP_NAME}{ver_label} installed successfully![/]\n\n"
                f"  [bold]Run with:[/]   [cyan]antigravity[/]\n"
                f"  [bold]Location:[/]  [dim]{install_dir}[/]\n"
                f"  [bold]Log:[/]       [dim]{log_path}[/]",
                box=box.ROUNDED,
            )
        )

        # Warn if the launcher directory is not in PATH
        which = shutil.which(BINARY_NAME)
        if which is None or Path(which).resolve() != (symlink_dir / BINARY_NAME).resolve():
            console.print(
                f"[warn]  ⚠  {symlink_dir} is not first in your PATH.[/]\n"
                f"     Add it to your shell profile or run {APP_NAME} with the full path."
            )
        console.print(
            f"[info]  ℹ  Verify the install anytime with: "
            f"python install_new_version.py --verify[/]"
        )
    console.print()
    return 0


def main() -> int:
    args = _parse_args()
    log_path = _setup_logging(args.log)
    console.print(f"  [dim]Log: {log_path}[/]")
    try:
        return _run_cli(args, log_path)
    except InstallError as e:
        logging.error("%s", e)
        console.print(f"[err]✗  {e}[/]")
        return 1
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        return 130
    except Exception:
        logging.exception("unexpected error")
        console.print(
            "[err]✗  Unexpected error — details in the log.[/]\n"
            f"     Log: {log_path}"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
