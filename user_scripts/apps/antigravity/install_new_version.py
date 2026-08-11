#!/usr/bin/env python3
"""
Antigravity Installer / Updater for Arch Linux

Extracts the Antigravity .tar.gz archive to /opt, creates a symlink
in /usr/local/bin, and sets up a .desktop launcher entry.

Features:
  • Rich terminal UI with progress indicators
  • Detects currently-installed and incoming versions
  • Interactive prompts when run without arguments
  • Auto-installs Python dependencies (rich)
  • Auto-escalates privileges via sudo when needed
  • --prefix flag for sandboxed / local installs
  • --dry-run flag to preview changes

Usage:
    python install_new_version.py                           # interactive
    python install_new_version.py /path/to/Antigravity.tar.gz
    python install_new_version.py --prefix /tmp/test-dir    # sandbox
    python install_new_version.py --dry-run                 # preview only
"""

# ── Bootstrap: auto-install Python dependencies ──────────────────────
import importlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

_CONTAINED_LIBS = Path.home() / "contained_apps" / "python-libs"


def _bootstrap_dependencies() -> None:
    """Install missing Python packages via ``uv`` into ~/contained_apps/."""
    # Make the contained-libs dir importable regardless
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

# ── Imports ──────────────────────────────────────────────────────────
import argparse
import json
import os
import re
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

# ── Default paths ────────────────────────────────────────────────────
DEFAULT_INSTALL_DIR = Path("/opt/Antigravity-x64")
DEFAULT_SYMLINK_DIR = Path.home() / ".local" / "bin"
DEFAULT_ARCHIVE = Path("/mnt/zram1/downloads/Antigravity.tar.gz")

# Legacy install locations to probe for an existing version and clean up
LEGACY_INSTALL_DIRS = [
    Path("/opt/Antigravity"),
]

ARCHIVE_ROOT = "Antigravity-x64"
BINARY_NAME = "antigravity"
VERSION_FILE = ".installed_version"

# Flags to preserve in the launcher script (match the user's existing setup)
LAUNCHER_FLAGS = ["--disable-gpu-sandbox"]


# ═════════════════════════════════════════════════════════════════════
#  Version detection
# ═════════════════════════════════════════════════════════════════════

def _read_version_from_asar(asar_path: Path) -> str | None:
    """Parse an Electron .asar archive and read the app version from its
    embedded ``package.json``.

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
            _pickle1_payload = struct.unpack("<I", f.read(4))[0]     # always 4
            header_size      = struct.unpack("<I", f.read(4))[0]     # H
            _pickle2_payload = struct.unpack("<I", f.read(4))[0]
            json_len         = struct.unpack("<I", f.read(4))[0]

            header = json.loads(f.read(json_len).decode("utf-8"))
            data_start = 8 + header_size

            pkg_entry = header.get("files", {}).get("package.json")
            if pkg_entry is None:
                return None

            f.seek(data_start + int(pkg_entry["offset"]))
            pkg = json.loads(f.read(pkg_entry["size"]))
            return pkg.get("version")
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
    return _read_version_from_asar(asar_path) or _read_version_from_asar_fallback(asar_path)


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
    """Return the version of the currently installed Antigravity.

    Checks the primary install dir first, then probes legacy locations."""
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
    """Return a list of legacy install directories that still exist."""
    return [d for d in LEGACY_INSTALL_DIRS if d.exists() and d != install_dir]


def get_archive_version(archive: Path) -> str | None:
    """Extract only the .asar from the tarball into a temp dir and read its version."""
    asar_member = f"{ARCHIVE_ROOT}/resources/app.asar"
    try:
        with tempfile.TemporaryDirectory(prefix="agy-ver-") as tmp:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extract(asar_member, path=tmp, filter="data")
            return get_version_from_asar(Path(tmp) / asar_member)
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════

def _needs_sudo(path: Path) -> bool:
    """Return True when writing to *path* (or its parent) requires root."""
    target = path if path.exists() else path.parent
    return not os.access(target, os.W_OK)


def _run(*cmd: str, sudo: bool = False) -> None:
    """Run a command, optionally prefixed with ``sudo``."""
    full = ["sudo", *cmd] if sudo else list(cmd)
    result = subprocess.run(full)
    if result.returncode != 0:
        console.print(f"[err]✗  Command failed (exit {result.returncode}): {' '.join(full)}[/]")
        sys.exit(1)


def _validate_archive(archive: Path) -> bool:
    """Verify the tarball contains the expected Antigravity binary."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            return f"{ARCHIVE_ROOT}/{BINARY_NAME}" in tar.getnames()
    except Exception:
        return False


def _pre_auth_sudo() -> bool:
    """Prompt the user for their sudo password once so later commands don't
    interrupt the progress display."""
    console.print("[info]  ⚡ Elevated privileges required — you may be prompted for your password.[/]")
    return subprocess.run(["sudo", "-v"]).returncode == 0


def _confirm(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question. On EOF (piped stdin exhausted), use *default*."""
    try:
        return Confirm.ask(prompt, default=default)
    except EOFError:
        return default


# ═════════════════════════════════════════════════════════════════════
#  Installation
# ═════════════════════════════════════════════════════════════════════

_STEPS = [
    "Cleaning up legacy installs",
    "Removing old installation",
    "Extracting archive",
    "Setting permissions",
    "Creating launcher",
    "Creating desktop entry",
    "Saving version metadata",
]


def _install(
    archive: Path,
    install_dir: Path,
    symlink_dir: Path,
    new_version: str | None,
    dry_run: bool,
    use_prefix: bool = False,
    legacy_dirs: list[Path] | None = None,
) -> None:
    use_sudo_install = _needs_sudo(install_dir) and not dry_run
    use_sudo_symlink = _needs_sudo(symlink_dir) and not dry_run
    binary_path = install_dir / BINARY_NAME
    legacy_dirs = legacy_dirs or []

    # Build the launcher script content
    flags = " ".join(LAUNCHER_FLAGS)
    launcher_content = (
        "#!/bin/bash\n"
        f'exec "{binary_path}" {flags} "$@"\n'
    )

    with Progress(
        SpinnerColumn("dots"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Installing…", total=len(_STEPS))

        # 1 ── Clean up legacy installs ────────────────────────────
        progress.update(task, description=f"[cyan]{_STEPS[0]}[/]")
        for legacy in legacy_dirs:
            if dry_run:
                console.print(f"  [dim]DRY-RUN: would remove legacy {legacy}[/]")
            else:
                use_sudo_legacy = _needs_sudo(legacy)
                _run("rm", "-rf", str(legacy), sudo=use_sudo_legacy)
        progress.advance(task)

        # 2 ── Remove old install (same-name dir) ──────────────────
        progress.update(task, description=f"[cyan]{_STEPS[1]}[/]")
        if install_dir.exists():
            if dry_run:
                console.print(f"  [dim]DRY-RUN: would remove {install_dir}[/]")
            else:
                _run("rm", "-rf", str(install_dir), sudo=use_sudo_install)
        progress.advance(task)

        # 3 ── Extract ─────────────────────────────────────────────
        progress.update(task, description=f"[cyan]{_STEPS[2]}[/]")
        if dry_run:
            console.print(f"  [dim]DRY-RUN: would extract to {install_dir.parent}[/]")
        else:
            _run("tar", "-xzf", str(archive), "-C", str(install_dir.parent),
                 sudo=use_sudo_install)
        progress.advance(task)

        # 4 ── Permissions ─────────────────────────────────────────
        progress.update(task, description=f"[cyan]{_STEPS[3]}[/]")
        if not dry_run and binary_path.exists():
            _run("chmod", "+x", str(binary_path), sudo=use_sudo_install)
        progress.advance(task)

        # 5 ── Launcher script ─────────────────────────────────────
        progress.update(task, description=f"[cyan]{_STEPS[4]}[/]")
        launcher_path = symlink_dir / BINARY_NAME
        if dry_run:
            console.print(f"  [dim]DRY-RUN: would write launcher {launcher_path}[/]")
        else:
            symlink_dir.mkdir(parents=True, exist_ok=True)
            # Remove any existing file or symlink first to avoid
            # write_text() following a symlink and corrupting its target
            if launcher_path.exists() or launcher_path.is_symlink():
                launcher_path.unlink()
            launcher_path.write_text(launcher_content)
            launcher_path.chmod(0o755)
        progress.advance(task)

        # 6 ── Desktop entry ───────────────────────────────────────
        progress.update(task, description=f"[cyan]{_STEPS[5]}[/]")
        if use_prefix:
            desktop_dir = install_dir.parent / "share" / "applications"
        else:
            desktop_dir = Path.home() / ".local/share/applications"
        desktop_file = desktop_dir / "antigravity.desktop"
        exec_cmd = f"{binary_path} {flags} %U"
        desktop_content = (
            "[Desktop Entry]\n"
            "Name=Antigravity\n"
            "Comment=Antigravity IDE\n"
            f"Exec={exec_cmd}\n"
            "Terminal=false\n"
            "Type=Application\n"
            "Categories=Development;IDE;\n"
            "StartupWMClass=Antigravity\n"
            "StartupNotify=true\n"
        )
        if dry_run:
            console.print(f"  [dim]DRY-RUN: would write {desktop_file}[/]")
        else:
            desktop_dir.mkdir(parents=True, exist_ok=True)
            desktop_file.write_text(desktop_content)
        progress.advance(task)

        # 7 ── Version metadata ────────────────────────────────────
        progress.update(task, description=f"[cyan]{_STEPS[6]}[/]")
        if new_version and not dry_run:
            vf = install_dir / VERSION_FILE
            # Write version via tee to support sudo without shell injection risk
            proc = subprocess.run(
                ["sudo", "tee", str(vf)] if use_sudo_install else ["tee", str(vf)],
                input=new_version, text=True,
                stdout=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                console.print("[warn]  ⚠  Could not save version metadata[/]")
        progress.advance(task)


# ═════════════════════════════════════════════════════════════════════
#  CLI & Main
# ═════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Install or update Antigravity on Arch Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                                     # interactive\n"
            "  %(prog)s ~/Downloads/Antigravity.tar.gz      # direct path\n"
            "  %(prog)s --prefix /tmp/sandbox                # sandboxed install\n"
            "  %(prog)s --dry-run                            # preview only\n"
        ),
    )
    p.add_argument(
        "archive", nargs="?", default=None,
        help="Path to Antigravity.tar.gz (prompted interactively if omitted).",
    )
    p.add_argument(
        "--prefix", default=None, metavar="DIR",
        help="Install under DIR instead of /opt (useful for testing).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making any changes.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

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
        prefix = Path(args.prefix).resolve()
        install_dir = prefix / ARCHIVE_ROOT
        symlink_dir = prefix / "bin"
        prefix.mkdir(parents=True, exist_ok=True)
        symlink_dir.mkdir(parents=True, exist_ok=True)
    else:
        install_dir = DEFAULT_INSTALL_DIR
        symlink_dir = DEFAULT_SYMLINK_DIR

    # ── Banner ────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            "[bold magenta]🚀  Antigravity Installer[/]",
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
        try:
            raw = Prompt.ask(
                "[bold]📦  Path to Antigravity archive[/]",
                default=default_display or None,
            )
        except EOFError:
            if default_display:
                raw = default_display
            else:
                console.print("[err]✗  No archive path provided.[/]")
                return 1
        archive = Path(raw).expanduser().resolve()

    if not archive.exists():
        console.print(f"[err]✗  File not found: {archive}[/]")
        return 1
    if not tarfile.is_tarfile(str(archive)):
        console.print(f"[err]✗  Not a valid tar archive: {archive}[/]")
        return 1

    console.print(f"  [dim]Archive :[/]  {archive}")
    if args.prefix:
        console.print(f"  [dim]Prefix  :[/]  {args.prefix}")
    console.print()

    # ── Validate archive contents ─────────────────────────────────
    with console.status("[cyan]Verifying archive contents …[/]"):
        valid = _validate_archive(archive)
    if not valid:
        console.print(
            f"[err]✗  Archive does not contain '{ARCHIVE_ROOT}/{BINARY_NAME}'. "
            f"Wrong file?[/]"
        )
        return 1
    console.print("[success]  ✓  Archive verified[/]")

    # ── Version detection ─────────────────────────────────────────
    with console.status("[cyan]Detecting versions …[/]"):
        installed_ver = get_installed_version(install_dir)
        new_ver = get_archive_version(archive)
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
    if installed_ver and new_ver and installed_ver == new_ver:
        if not _confirm(
            f"[warn]⚠  Version v{new_ver} is already installed. Reinstall?[/]",
            default=False,
        ):
            console.print("[dim]Aborted.[/]")
            return 0

    # ── Dry-run notice ────────────────────────────────────────────
    if args.dry_run:
        console.print("[warn]  ⚠  DRY-RUN mode — no changes will be made[/]")
        console.print()

    # ── Confirmation ──────────────────────────────────────────────
    if not _confirm("[bold]Proceed with installation?[/]", default=True):
        console.print("[dim]Aborted.[/]")
        return 0
    console.print()

    # ── Privilege escalation ──────────────────────────────────────
    if (not args.dry_run
            and (_needs_sudo(install_dir) or _needs_sudo(symlink_dir))):
        if not _pre_auth_sudo():
            console.print("[err]✗  Failed to obtain elevated privileges.[/]")
            return 1
        console.print()

    # ── Install ───────────────────────────────────────────────────
    _install(
        archive=archive,
        install_dir=install_dir,
        symlink_dir=symlink_dir,
        new_version=new_ver,
        dry_run=args.dry_run,
        use_prefix=bool(args.prefix),
        legacy_dirs=legacy_dirs,
    )

    # ── Summary ───────────────────────────────────────────────────
    console.print()
    ver_label = f" v{new_ver}" if new_ver else ""

    if args.dry_run:
        console.print(
            Panel(
                f"[bold green]✅  DRY-RUN complete — no changes were made[/]\n"
                f"[dim]Run without --dry-run to install Antigravity{ver_label}.[/]",
                box=box.ROUNDED,
            )
        )
    else:
        console.print(
            Panel(
                f"[bold green]✅  Antigravity{ver_label} installed successfully![/]\n\n"
                f"  [bold]Run with:[/]   [cyan]antigravity[/]\n"
                f"  [bold]Location:[/]  [dim]{install_dir}[/]",
                box=box.ROUNDED,
            )
        )

        # Warn if the launcher directory is not in PATH
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        if str(symlink_dir) not in path_dirs:
            console.print(
                f"[warn]  ⚠  {symlink_dir} is not in your PATH.[/]\n"
                f"     Add it to your shell profile or run Antigravity with the full path."
            )
    console.print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
