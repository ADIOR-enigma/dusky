#!/usr/bin/env python3
"""
===============================================================================
Unified Systemd & DBus Service Manager
===============================================================================
Context: Arch Linux (Bleeding-Edge) / Python 3.14+
Description: Installs, symlinks, and manages user and system-level Systemd
             services and DBus activation files. Replaces legacy bash scripts
             130, 131, and 132 with a single, highly configurable tool.

Features:
  - Declarative configuration blocks at top of script.
  - Native Python 3.14+ (pathlib, dataclasses, typing).
  - Rich terminal presentation (styled logs, panels, progress tables).
  - Multi-mode support: --all, --user, --dbus, --system.
  - Non-interactive mode (--default / -y).
  - Dry-run mode (--dry-run / -n).
  - Live unit inspection (--status / -st).
  - Complete uninstall/reversion mode (--uninstall) with O(1) batching.
  - Split Execution Architecture (unprivileged user execution + isolated sudo sub-process).
  - Fast, clean direct file copying (shutil.copy2) without backup clutter or temp files.
  - Direct symlink creation and updates.
  - Robust JSON / JSONL state parsing with LoadState tracking.
===============================================================================
"""

import argparse
import datetime
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# --- Rich UI Imports ---
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

# Initialize Rich Consoles
console = Console()
error_console = Console(stderr=True)

# ==============================================================================
# CONFIGURATION SECTION
# ==============================================================================

@dataclass(frozen=True)
class ServiceConfig:
    """Configuration model for a Systemd service/timer file."""
    source_path: str
    default_action: Literal["enable", "disable"] = "disable"


@dataclass(frozen=True)
class SymlinkConfig:
    """Configuration model for DBus or User Symlinks."""
    source_path: str
    target_path: str


# ------------------------------------------------------------------------------
# 1. Systemd User Services (Target: ~/.config/systemd/user/)
# ------------------------------------------------------------------------------
USER_SERVICES: list[ServiceConfig] = [
    # Network Meter (Default: Enable)
    ServiceConfig("$HOME/user_scripts/waybar/network/network_meter.service", "enable"),
    # Dusky Control Center Daemon (Default: Disable)
    ServiceConfig("$HOME/user_scripts/dusky_system/control_center/service/dusky.service", "disable"),
    # Dusky Update Checker (Default: Disable)
    ServiceConfig("$HOME/user_scripts/update_dusky/update_checker/service/update_checker.service", "disable"),
    ServiceConfig("$HOME/user_scripts/update_dusky/update_checker/service/update_checker.timer", "disable"),
    # Dusky Quickpanel (Default: Disable)
    ServiceConfig("$HOME/user_scripts/dusky_system/quickpanal/service/dusky_quickpanal.service", "disable"),
    # Dusky OSD Router (Default: Disable)
    ServiceConfig("$HOME/user_scripts/mako_osd/osd_router/osd_lock.service", "disable"),
    # Dusky RAM Monitor (Default: Enable)
    ServiceConfig("$HOME/user_scripts/performance/swap_and_ram_monitor_service/dusky_ram_monitor.service", "enable"),
    # Dusky Audio Visualizer (Default: Disable)
    ServiceConfig("$HOME/user_scripts/way_layers/visualizer/dusky_visualizer.service", "disable"),
    # Dusky Screentime Tracker (Default: Disable)
    ServiceConfig("$HOME/user_scripts/screentime/dusky_screentime.service", "disable"),
    # Dusky TUI Pre-warming Daemon (Default: Enable)
    ServiceConfig("$HOME/user_scripts/dusky_tui/python/service/dusky_tui.service", "enable"),
]

# ------------------------------------------------------------------------------
# 2. DBus & User Service Symlinks
# ------------------------------------------------------------------------------
DBUS_SYMLINKS: list[SymlinkConfig] = [
    # Dusky Control Center DBus Activation
    SymlinkConfig(
        "$HOME/user_scripts/dusky_system/control_center/service/com.github.dusky.controlcenter.service",
        "$XDG_DATA_HOME/dbus-1/services/com.github.dusky.controlcenter.service",
    ),
    # Dusky Quickpanel DBus Activation
    SymlinkConfig(
        "$HOME/user_scripts/dusky_system/quickpanal/service/org.dusky.quickpanal.service",
        "$XDG_DATA_HOME/dbus-1/services/org.dusky.quickpanal.service",
    ),
]

# ------------------------------------------------------------------------------
# 3. Systemd System Services (Target: /etc/systemd/system/ - Requires Root)
# ------------------------------------------------------------------------------
SYSTEM_SERVICES: list[ServiceConfig] = [
    # RAPL CPU Energy Permissions Setter (Default: Enable)
    ServiceConfig("$HOME/user_scripts/mako_osd/dusky_glance/services/glance_cpu_pkg_watt.service", "enable"),
    # Dusky CPU Core and Power Limiter Restorer (Default: Enable)
    ServiceConfig("$HOME/user_scripts/performance/cpu/service/dusky_cpu.service", "enable"),
    # Disable NumLock on TTYs on Boot (Default: Disable)
    ServiceConfig("$HOME/user_scripts/hypr/input/service/numlock_disable.service", "disable"),
]

SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")

# ==============================================================================
# HELPER & UTILITY FUNCTIONS
# ==============================================================================

@dataclass(frozen=True)
class UserContext:
    username: str
    home: Path
    uid: int
    gid: int
    is_root: bool


def get_user_context() -> UserContext:
    """Resolves real non-root user details prioritizing active privilege escalation context (headless safe)."""
    is_root = os.geteuid() == 0
    real_uid = os.getuid()

    if is_root:
        # 1. Immediate Privilege Escalation Environment Variables MUST take precedence
        escalation_uid = os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID")
        if escalation_uid and escalation_uid.isdigit():
            real_uid = int(escalation_uid)
        elif "DOAS_USER" in os.environ:
            try:
                real_uid = pwd.getpwnam(os.environ["DOAS_USER"]).pw_uid
            except KeyError:
                pass
        else:
            # 2. Fallback to Absolute Truth via PAM / logind
            try:
                loginuid_raw = Path("/proc/self/loginuid").read_text(encoding="utf-8").strip()
                loginuid = int(loginuid_raw)
                if loginuid != 4294967295:  # (unsigned -1) means unset
                    real_uid = loginuid
            except (FileNotFoundError, ValueError, OSError):
                pass

    try:
        pw = pwd.getpwuid(real_uid)
    except KeyError:
        error_console.print(f"[bold red][ERROR][/bold red] Fatal: Resolved UID {real_uid} does not map to a valid system user.")
        sys.exit(1)

    return UserContext(
        username=pw.pw_name,
        home=Path(pw.pw_dir),
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        is_root=is_root,
    )


def get_user_config_dir(ctx: UserContext) -> Path:
    """Strictly enforces standard XDG config home."""
    if not ctx.is_root:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config and Path(xdg_config).is_absolute():
            return Path(xdg_config)
    return ctx.home / ".config"


def get_user_data_dir(ctx: UserContext) -> Path:
    """Strictly enforces standard XDG data home for DBus configurations."""
    if not ctx.is_root:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data and Path(xdg_data).is_absolute():
            return Path(xdg_data)
    return ctx.home / ".local" / "share"


def expand_path(raw_path: str, ctx: UserContext) -> Path:
    """Deterministically expands paths without executing arbitrary env injections."""
    replacements = {
        "$XDG_DATA_HOME": str(get_user_data_dir(ctx)),
        "${XDG_DATA_HOME}": str(get_user_data_dir(ctx)),
        "$XDG_CONFIG_HOME": str(get_user_config_dir(ctx)),
        "${XDG_CONFIG_HOME}": str(get_user_config_dir(ctx)),
        "$HOME": str(ctx.home),
        "${HOME}": str(ctx.home),
        "~/": f"{ctx.home}/",
    }

    path_str = raw_path
    for var, val in replacements.items():
        if var in path_str:
            path_str = path_str.replace(var, val)

    if path_str.startswith("/root/") and ctx.username != "root":
        path_str = os.path.join(str(ctx.home), path_str[6:])

    final_path = Path(os.path.normpath(os.path.expanduser(path_str)))
    if not ctx.is_root and not final_path.is_relative_to(ctx.home):
        log_warn(f"Warning: Path {final_path} resolves outside designated user boundaries ({ctx.home}).")

    return final_path


def safe_mkdir(target_dir: Path) -> bool:
    """Standard directory creation enforcing exact 0755 permissions. Returns success state."""
    try:
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        return True
    except Exception as e:
        log_error(f"Failed to create directory {target_dir}: {e}")
        return False


def get_user_ipc_env(ctx: UserContext) -> dict[str, str]:
    """Constructs a sterile whitelist environment mapping to prevent root environment leakage."""
    runtime_dir = Path(f"/run/user/{ctx.uid}")
    if not runtime_dir.exists():
        log_warn(f"Runtime dir {runtime_dir} missing! Is user logged in or linger enabled?")

    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/bin"),
        "USER": ctx.username,
        "HOME": str(ctx.home),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
    }

    for xdg_var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        if xdg_var in os.environ:
            env[xdg_var] = os.environ[xdg_var]

    return env


def log_info(msg: str) -> None:
    console.print(f"[bold blue][INFO][/bold blue] {msg}")


def log_success(msg: str) -> None:
    console.print(f"[bold green][OK][/bold green]   {msg}")


def log_warn(msg: str) -> None:
    console.print(f"[bold yellow][WARN][/bold yellow] {msg}")


def log_error(msg: str) -> None:
    error_console.print(f"[bold red][ERROR][/bold red] {msg}")


def suggest_missing(source_path: Path) -> None:
    """Provide fuzzy suggestions if source unit file does not exist."""
    parent = source_path.parent
    if parent.exists() and parent.is_dir():
        log_warn("Did you name it something else? Found in directory:")
        matching = [f.name for f in parent.glob("*.service")] + [f.name for f in parent.glob("*.timer")]
        if matching:
            for item in matching:
                console.print(f"  - {item}")
        else:
            console.print("  (No .service or .timer files found)")


def run_systemctl(args: list[str], is_user: bool, dry_run: bool, ctx: UserContext) -> bool:
    """Executes systemctl with mandatory timeout parameters."""
    base = ["systemctl", "--user"] if is_user else ["systemctl"]
    cmd = base + args
    cmd_str = " ".join(cmd)

    if dry_run:
        log_info(f"[Dry-Run] Would execute: [cyan]{cmd_str}[/cyan]")
        return True

    env = get_user_ipc_env(ctx) if is_user else None
    try:
        res = subprocess.run(cmd, capture_output=True, env=env, text=True, timeout=20)
        if res.returncode != 0:
            err_msg = res.stderr.strip()
            log_error(f"Systemctl failed ({cmd_str}):\n{err_msg}" if err_msg else f"Systemctl returned non-zero ({cmd_str})")
            return False
        return True
    except subprocess.TimeoutExpired:
        log_error(f"Systemctl execution timed out ({cmd_str}). IPC bus may be hung.")
        return False
    except Exception as e:
        log_error(f"Execution error ({cmd_str}): {e}")
        return False


def reload_dbus(dry_run: bool, ctx: UserContext) -> bool:
    """Modern DBus reload via busctl for dbus-broker compatibility."""
    log_info("Triggering DBus configuration reload via busctl...")
    cmd = [
        "busctl", "--user", "call",
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "ReloadConfig"
    ]
    if dry_run:
        log_info(f"[Dry-Run] Would execute: {' '.join(cmd)}")
        return True

    env = get_user_ipc_env(ctx)
    try:
        res = subprocess.run(cmd, capture_output=True, env=env, text=True, timeout=10)
        if res.returncode != 0:
            log_warn(f"DBus reload failed (is DBus running?): {res.stderr.strip()}")
            return False
        log_success("DBus reloaded successfully.")
        return True
    except Exception as e:
        log_error(f"Failed to reload DBus: {e}")
        return False


# ==============================================================================
# CORE WORKFLOW PHASES
# ==============================================================================

def process_service_batch(
    configs: list[ServiceConfig],
    target_dir: Path,
    is_user: bool,
    use_defaults: bool,
    dry_run: bool,
    ctx: UserContext,
) -> None:
    """Installs and manages systemd service units cleanly using fast direct file copy (shutil.copy2)."""
    scope = "User" if is_user else "System"
    console.print(f"\n[bold magenta]--- Processing {scope} Services ---[/bold magenta]")

    if not dry_run:
        if not safe_mkdir(target_dir):
            log_error(f"Aborting batch due to directory creation failure: {target_dir}")
            return

    installed_units: list[ServiceConfig] = []
    valid_extensions = {".service", ".timer", ".socket", ".target"}

    # Phase 1: Installation (Fast Direct Copy)
    for cfg in configs:
        src_path = expand_path(cfg.source_path, ctx)
        service_name = src_path.name
        target_file = target_dir / service_name

        console.print("-" * 50)
        log_info(f"Processing: [bold cyan]{service_name}[/bold cyan]")

        if src_path.suffix not in valid_extensions:
            log_error(f"Invalid unit extension '{src_path.suffix}'. Skipping.")
            continue

        if not src_path.exists():
            log_error(f"Source file not found: {src_path}")
            suggest_missing(src_path)
            continue

        log_info(f"Installing to {target_file}...")
        if dry_run:
            log_info(f"[Dry-Run] Copy {src_path} -> {target_file} (mode=0644)")
        else:
            try:
                shutil.copy2(src_path, target_file)
                target_file.chmod(0o644)
                log_success(f"Installed {service_name}")
            except Exception as e:
                log_error(f"Failed to install {service_name}: {e}")
                continue

        installed_units.append(cfg)

    if not installed_units:
        return

    # Phase 2: SINGLE Daemon Reload
    console.print("-" * 50)
    log_info(f"Reloading {scope.lower()} systemd daemon...")
    run_systemctl(["daemon-reload"], is_user=is_user, dry_run=dry_run, ctx=ctx)

    # Phase 3: Action Assessment
    enable_units: list[str] = []
    disable_units: list[str] = []

    for cfg in installed_units:
        service_name = Path(cfg.source_path).name
        action_str = cfg.default_action.strip().lower()
        if action_str not in ("enable", "disable"):
            action_str = "disable"

        default_is_enable = (action_str == "enable")

        if use_defaults or not sys.stdin.isatty():
            user_wants_enable = default_is_enable
            log_info(f"Auto-applying default ({action_str}) for {service_name}...")
        else:
            try:
                user_wants_enable = Confirm.ask(
                    f"Enable and Start {service_name}?",
                    default=default_is_enable,
                    console=console,
                )
            except (EOFError, OSError):
                user_wants_enable = default_is_enable

        if user_wants_enable:
            enable_units.append(service_name)
        else:
            disable_units.append(service_name)

    # Phase 4: Bulk Systemd Execution
    if enable_units:
        log_info(f"Enabling & Starting ({len(enable_units)} units)...")
        if run_systemctl(["enable", "--now"] + enable_units, is_user=is_user, dry_run=dry_run, ctx=ctx):
            log_success(f"Successfully activated: {', '.join(enable_units)}")

    if disable_units:
        log_info(f"Disabling & Stopping ({len(disable_units)} units)...")
        if run_systemctl(["disable", "--now"] + disable_units, is_user=is_user, dry_run=dry_run, ctx=ctx):
            log_success(f"Successfully deactivated: {', '.join(disable_units)}")


def process_symlinks(
    configs: list[SymlinkConfig],
    dry_run: bool,
    ctx: UserContext,
) -> None:
    """Manages DBus and User service symlinks cleanly via direct symlink creation without backup files."""
    console.print("\n[bold magenta]--- Processing DBus & Service Symlinks ---[/bold magenta]")

    need_user_daemon_reload = False
    need_dbus_reload = False

    for cfg in configs:
        src_path = expand_path(cfg.source_path, ctx)
        target_path = expand_path(cfg.target_path, ctx)
        target_dir = target_path.parent

        console.print("-" * 50)
        log_info(f"Target: [cyan]{target_path.name}[/cyan]")

        if not src_path.exists():
            log_error(f"Source missing: {src_path}")
            suggest_missing(src_path)
            continue

        if not dry_run:
            if not safe_mkdir(target_dir):
                continue

        if target_path.exists() and target_path.is_dir() and not target_path.is_symlink():
            log_error(f"Target path {target_path} is a directory! Cannot replace.")
            continue

        if target_path.is_symlink():
            try:
                if target_path.resolve() == src_path.resolve():
                    log_success("Symlink already exists and points to correct source.")
                    continue
                log_warn(f"Updating existing link (was pointing to {target_path.resolve()})")
            except Exception:
                log_warn("Updating broken or invalid existing symlink.")

        log_info(f"Linking {src_path} -> {target_path}")
        if dry_run:
            log_info(f"[Dry-Run] Would symlink {src_path} -> {target_path}")
        else:
            try:
                target_path.unlink(missing_ok=True)
                target_path.symlink_to(src_path)
                log_success("Link mapped successfully.")
            except Exception as e:
                log_error(f"Failed to create symlink {target_path}: {e}")
                continue

        # Robust Trigger Detection
        if target_dir.name == "services" and target_dir.parent.name == "dbus-1":
            need_dbus_reload = True
        elif "systemd/user" in target_path.as_posix():
            need_user_daemon_reload = True

    console.print("-" * 50)
    if need_user_daemon_reload:
        log_info("Systemd user units changed. Reloading daemon...")
        run_systemctl(["daemon-reload"], is_user=True, dry_run=dry_run, ctx=ctx)
        log_success("Systemd user daemon reloaded.")

    if need_dbus_reload:
        reload_dbus(dry_run, ctx=ctx)


def display_status(ctx: UserContext) -> None:
    """Renders a comprehensive Rich Table utilizing systemd JSON / JSONL state parsing."""
    table = Table(title="Systemd & DBus Services Status Overview", title_style="bold cyan")
    table.add_column("Unit / Service", style="bold white")
    table.add_column("Type", style="magenta")
    table.add_column("Source Status", style="yellow")
    table.add_column("Target Installed", style="blue")
    table.add_column("Active State", style="green")
    table.add_column("Enabled State", style="cyan")

    def get_bulk_states(units: list[str], is_user: bool) -> dict[str, tuple[str, str]]:
        if not units:
            return {}
        cmd = ["systemctl", "show", "--output=json", "--property=Id,ActiveState,UnitFileState,LoadState"] + units
        if is_user:
            cmd.insert(1, "--user")

        env = get_user_ipc_env(ctx) if is_user else None
        try:
            res = subprocess.run(cmd, capture_output=True, env=env, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return {}

        states: dict[str, tuple[str, str]] = {}
        out = res.stdout.strip()
        if not out:
            return states

        # Parse JSON Array or JSON Lines (JSONL)
        lines = out.splitlines() if not out.startswith("[") else [out]
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                items = json.loads(line_str)
                if isinstance(items, dict):
                    items = [items]
                for item in items:
                    unit_id = item.get("Id") or "unknown"
                    if unit_id != "unknown":
                        if item.get("LoadState") == "not-found":
                            states[unit_id] = ("not-found", "not-found")
                        else:
                            states[unit_id] = (
                                item.get("ActiveState") or "unknown",
                                item.get("UnitFileState") or "unknown",
                            )
            except json.JSONDecodeError:
                pass

        return states

    # Batch Query User Services
    user_units = [expand_path(c.source_path, ctx).name for c in USER_SERVICES]
    user_states = get_bulk_states(user_units, is_user=True)
    user_target_dir = get_user_config_dir(ctx) / "systemd" / "user"

    for cfg in USER_SERVICES:
        src = expand_path(cfg.source_path, ctx)
        target = user_target_dir / src.name
        active, enabled = user_states.get(src.name, ("unknown", "unknown"))
        table.add_row(src.name, "User Service", "Found" if src.exists() else "Missing", "Installed" if target.exists() else "Not Installed", active, enabled)

    # Batch Query DBus Symlinks
    for cfg in DBUS_SYMLINKS:
        src = expand_path(cfg.source_path, ctx)
        target = expand_path(cfg.target_path, ctx)
        tgt_state = "Linked" if target.is_symlink() and target.exists() else ("Wrong Target" if target.is_symlink() else ("File (Not Link)" if target.exists() else "Not Linked"))
        table.add_row(target.name, "DBus Link", "Found" if src.exists() else "Missing", tgt_state, "N/A", "N/A")

    # Batch Query System Services
    sys_units = [expand_path(c.source_path, ctx).name for c in SYSTEM_SERVICES]
    sys_states = get_bulk_states(sys_units, is_user=False)

    for cfg in SYSTEM_SERVICES:
        src = expand_path(cfg.source_path, ctx)
        target = SYSTEMD_SYSTEM_DIR / src.name
        active, enabled = sys_states.get(src.name, ("unknown", "unknown"))
        table.add_row(src.name, "System Service", "Found" if src.exists() else "Missing", "Installed" if target.exists() else "Not Installed", active, enabled)

    console.print(table)


def uninstall_all(ctx: UserContext, dry_run: bool, scope: Literal["user", "system", "all"] = "all") -> None:
    """Stops, disables, and removes installed units and symlinks with O(1) batch execution."""
    console.print(f"\n[bold red]--- Reverting Configured Services ({scope.upper()}) ---[/bold red]")

    if scope in ("user", "all"):
        user_target_dir = get_user_config_dir(ctx) / "systemd" / "user"
        user_units_to_disable: list[str] = []

        for cfg in USER_SERVICES:
            src_name = Path(cfg.source_path).name
            target = user_target_dir / src_name
            if target.is_symlink() or target.exists():
                user_units_to_disable.append(src_name)
                log_info(f"Removing {target}")
                if not dry_run:
                    try:
                        target.unlink(missing_ok=True)
                    except Exception as e:
                        log_error(f"Failed to remove {target}: {e}")

        if user_units_to_disable:
            log_info(f"Stopping & disabling user units: {', '.join(user_units_to_disable)}")
            run_systemctl(["disable", "--now"] + user_units_to_disable, is_user=True, dry_run=dry_run, ctx=ctx)

        run_systemctl(["daemon-reload"], is_user=True, dry_run=dry_run, ctx=ctx)

        for cfg in DBUS_SYMLINKS:
            target = expand_path(cfg.target_path, ctx)
            if target.is_symlink() or target.exists():
                log_info(f"Removing symlink {target}")
                if not dry_run:
                    try:
                        target.unlink(missing_ok=True)
                    except Exception as e:
                        log_error(f"Failed to remove symlink {target}: {e}")

    if scope in ("system", "all"):
        if not ctx.is_root:
            log_warn("Skipping system uninstall. Root privileges required.")
        else:
            sys_units_to_disable: list[str] = []
            for cfg in SYSTEM_SERVICES:
                src_name = Path(cfg.source_path).name
                target = SYSTEMD_SYSTEM_DIR / src_name
                if target.is_symlink() or target.exists():
                    sys_units_to_disable.append(src_name)
                    log_info(f"Removing {target}")
                    if not dry_run:
                        try:
                            target.unlink(missing_ok=True)
                        except Exception as e:
                            log_error(f"Failed to remove {target}: {e}")

            if sys_units_to_disable:
                log_info(f"Stopping & disabling system units: {', '.join(sys_units_to_disable)}")
                run_systemctl(["disable", "--now"] + sys_units_to_disable, is_user=False, dry_run=dry_run, ctx=ctx)

            run_systemctl(["daemon-reload"], is_user=False, dry_run=dry_run, ctx=ctx)

    log_success("Uninstall sequence complete.")


# ==============================================================================
# MAIN ENTRYPOINT & SPLIT EXECUTION ARCHITECTURE
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified Systemd & DBus Service Manager for Arch Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mode_group = parser.add_argument_group("Execution Modes")
    mode_group.add_argument("-a", "--all", action="store_true", help="Process User Services, DBus Symlinks, and System Services (Default)")
    mode_group.add_argument("-u", "--user", action="store_true", help="Process User Services only")
    mode_group.add_argument("-d", "--dbus", action="store_true", help="Process DBus / User Symlinks only")
    mode_group.add_argument("-s", "--system", action="store_true", help="Process System Services only")
    mode_group.add_argument("-st", "--status", action="store_true", help="Display live status table of all units")
    mode_group.add_argument("--uninstall", action="store_true", help="Stop, disable, and clean up installed units")

    opt_group = parser.add_argument_group("Options")
    opt_group.add_argument("-y", "--default", action="store_true", help="Non-interactive mode: Auto-apply default choices")
    opt_group.add_argument("-n", "--dry-run", action="store_true", help="Simulate actions without modifying system state")

    args = parser.parse_args()

    if not shutil.which("systemctl"):
        log_error("Systemd (systemctl) not found. This script requires systemd.")
        sys.exit(1)

    ctx = get_user_context()

    console.print(
        Panel.fit(
            f"[bold cyan]Unified Systemd & DBus Service Manager[/bold cyan]\n"
            f"[dim]User: {ctx.username} | Home: {ctx.home} | Root: {ctx.is_root}[/dim]",
            border_style="cyan",
        )
    )

    if args.status:
        display_status(ctx)
        return

    script_path = Path(sys.argv[0]).resolve().as_posix()

    # Determine execution scope
    run_user = args.user or args.all
    run_dbus = args.dbus or args.all
    run_system = args.system or args.all

    if not any([args.user, args.dbus, args.system, args.all]):
        run_user = run_dbus = run_system = True

    # Security Check: Prevent root from executing user domains directly
    if (run_user or run_dbus) and ctx.is_root and not args.dry_run:
        log_error("CRITICAL: Do not execute --user or --dbus as root. Run the script as your normal user. It will securely prompt for sudo when --system is required.")
        sys.exit(1)

    if args.uninstall:
        scope: Literal["user", "system", "all"] = "system" if ctx.is_root else ("all" if args.all else ("user" if args.user or args.dbus else "all"))
        uninstall_all(ctx, dry_run=args.dry_run, scope=scope)

        # Fork for system uninstall if requested by a normal user
        if scope in ("all", "user") and args.all and not ctx.is_root and not args.dry_run:
            if shutil.which("sudo"):
                log_info("Forking system uninstallation via sudo...")
                python_path = os.pathsep.join(sys.path)
                subprocess.run(["sudo", "env", f"PYTHONPATH={python_path}", sys.executable, script_path, "--uninstall", "--system"], check=False)
        return

    # Subprocess Split Execution for System Scope
    if run_system and not ctx.is_root and not args.dry_run:
        log_info("System services require root privileges.")
        if shutil.which("sudo"):
            log_info("Forking system service installation securely via sudo...")
            python_path = os.pathsep.join(sys.path)
            sudo_cmd = ["sudo", "env", f"PYTHONPATH={python_path}", sys.executable, script_path, "--system"]
            if args.default:
                sudo_cmd.append("-y")
            try:
                subprocess.run(sudo_cmd, check=True)
                run_system = False  # Handled successfully by subprocess
            except subprocess.CalledProcessError:
                log_error("Sudo execution for system services failed.")
                sys.exit(1)
        else:
            log_error("Root access required for system services. Please run with sudo.")
            sys.exit(1)

    # Core Execution Flow
    try:
        user_target_dir = get_user_config_dir(ctx) / "systemd" / "user"

        if run_user:
            process_service_batch(USER_SERVICES, target_dir=user_target_dir, is_user=True, use_defaults=args.default, dry_run=args.dry_run, ctx=ctx)

        if run_dbus:
            process_symlinks(DBUS_SYMLINKS, dry_run=args.dry_run, ctx=ctx)

        if run_system and ctx.is_root:
            process_service_batch(SYSTEM_SERVICES, target_dir=SYSTEMD_SYSTEM_DIR, is_user=False, use_defaults=args.default, dry_run=args.dry_run, ctx=ctx)

        if run_user or run_dbus or (run_system and ctx.is_root):
            console.print("-" * 50)
            log_success("All assigned operations completed successfully.")

    except KeyboardInterrupt:
        console.print("\n[bold red][ABORTED][/bold red] Caught SIGINT.")
        sys.exit(130)


if __name__ == "__main__":
    main()
