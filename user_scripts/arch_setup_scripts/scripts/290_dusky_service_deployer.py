#!/usr/bin/env python3
"""
==============================================================================
Dusky Service Deployer (290_dusky_service_deployer.py)
==============================================================================
Context: Arch Linux (Bleeding-Edge) / Hyprland / UWSM / Systemd 261
Python: 3.14+ with Rich UI presentation

Replaces and consolidates:
  - 290_system_services.sh            (Core System Services)
  - 065_enabling_user_services.sh      (Core User Session Services)
  - 110_aur_packages_sudo_services.sh  (AUR System Services)
  - 115_aur_packages_user_services.sh  (AUR User Session Services)

Features & Architecture:
  - Declarative ServiceConfig data structures with default enable/disable toggles
  - Unprivileged user execution with sterile IPC environment (XDG_RUNTIME_DIR/DBus)
  - Isolated subprocess sudo forking for system service operations
  - High-speed bulk systemctl status querying via property parsing
  - Interactive prompting (-i/--interactive) & non-interactive modes (-y/--default)
  - Fuzzy unit suggestion engine for missing services using difflib
  - DBus activation reloading via busctl
  - Reversion/disable mode (--disable)
  - Structured JSON status output (--json)
==============================================================================
"""

import argparse
import difflib
import json
import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Final, Literal, NamedTuple

# Rich Presentation Imports
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

# Initialize Rich Consoles
console = Console()
error_console = Console(stderr=True)


# ==============================================================================
# 1. DOMAIN CONFIGURATION MODELS
# ==============================================================================

@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Declarative configuration model for a Systemd service/timer/socket unit."""
    name: str
    enabled_by_default: bool = True
    description: str = ""


# ==============================================================================
# 2. SERVICE CONFIGURATION SECTIONS (USER EDITABLE)
# ==============================================================================

# Core System Services (System Scope - Sudo Required)
SYSTEM_SERVICES: Final[list[ServiceConfig]] = [
    ServiceConfig("NetworkManager.service", True, "Network connection manager"),
    ServiceConfig("udisks2.service", True, "Disk management and auto-mounting daemon"),
    ServiceConfig("thermald.service", True, "Thermal daemon for CPU temperature management"),
    ServiceConfig("bluetooth.service", True, "Bluetooth protocol stack daemon"),
    ServiceConfig("ufw.service", True, "Uncomplicated Firewall daemon"),
    ServiceConfig("fstrim.timer", True, "Weekly SSD TRIM maintenance timer"),
    ServiceConfig("systemd-timesyncd.service", True, "Network time synchronization daemon"),
    ServiceConfig("acpid.service", True, "Advanced Configuration and Power Interface daemon"),
    ServiceConfig("systemd-resolved.service", True, "Network Name Resolution manager"),
    ServiceConfig("snapper-cleanup.timer", True, "Btrfs Snapper snapshot cleanup timer"),
    ServiceConfig("snapper-cleanup.service", True, "Btrfs Snapper snapshot cleanup service"),
    # Optional / Disabled by Default:
    ServiceConfig("tlp.service", False, "Power management daemon (disabled by default)"),
    ServiceConfig("vsftpd.service", False, "FTP server daemon (disabled by default)"),
    ServiceConfig("reflector.timer", False, "Pacman mirrorlist reflector timer (disabled by default)"),
]

# AUR System Services (System Scope - Sudo Required)
AUR_SYSTEM_SERVICES: Final[list[ServiceConfig]] = [
    ServiceConfig("fwupd.service", True, "Firmware update daemon"),
    ServiceConfig("warp-svc.service", True, "Cloudflare WARP VPN service daemon"),
    ServiceConfig("preload.service", True, "Adaptive readahead daemon"),
    ServiceConfig("asusd.service", True, "ASUS ROG/TUF Linux control daemon"),
]

# Core User Services (User Session Scope - Executed as User)
USER_SERVICES: Final[list[ServiceConfig]] = [
    ServiceConfig("pipewire.socket", True, "PipeWire multimedia socket"),
    ServiceConfig("pipewire-pulse.socket", True, "PipeWire PulseAudio emulation socket"),
    ServiceConfig("wireplumber.service", True, "PipeWire session manager daemon"),
    ServiceConfig("hypridle.service", True, "Hyprland idle management daemon"),
    ServiceConfig("hyprpolkitagent.service", True, "Hyprland PolicyKit authentication agent"),
    ServiceConfig("fumon.service", True, "File/Folder monitoring service"),
    ServiceConfig("gnome-keyring-daemon.service", True, "GNOME Keyring secret storage daemon"),
    ServiceConfig("gnome-keyring-daemon.socket", True, "GNOME Keyring control socket"),
    ServiceConfig("mako.service", True, "Mako notification daemon"),
    # Optional / Disabled by Default:
    ServiceConfig("hyprsunset.service", False, "Hyprland blue-light temperature daemon"),
]

# AUR User Services (User Session Scope - Executed as User)
AUR_USER_SERVICES: Final[list[ServiceConfig]] = [
    ServiceConfig("hypridle.service", True, "Hyprland idle manager (AUR session)"),
]


# ==============================================================================
# 3. ENUMS & DATA STRUCTURES
# ==============================================================================

class Scope(Enum):
    SYSTEM = auto()
    USER = auto()


class Category(Enum):
    SYSTEM_CORE = "Core System Services"
    SYSTEM_AUR = "AUR System Services"
    USER_CORE = "Core User Services"
    USER_AUR = "AUR User Services"


class UnitStatus(Enum):
    ENABLED_ACTIVE = "Enabled & Active"
    ENABLED_INACTIVE = "Enabled"
    DISABLED_ACTIVE = "Active (Disabled)"
    DISABLED = "Disabled"
    STATIC = "Static"
    MASKED = "Masked"
    MISSING = "Not Installed"
    ERROR = "Error"


@dataclass(slots=True)
class UnitState:
    unit_name: str
    scope: Scope
    category: Category
    description: str = ""
    exists: bool = False
    load_state: str = "not-found"
    active_state: str = "inactive"
    unit_file_state: str = "disabled"

    @property
    def status_enum(self) -> UnitStatus:
        if not self.exists or self.load_state == "not-found":
            return UnitStatus.MISSING
        if self.unit_file_state == "masked":
            return UnitStatus.MASKED
        if self.unit_file_state in ("static", "indirect"):
            return UnitStatus.STATIC

        is_enabled = self.unit_file_state in ("enabled", "enabled-runtime", "alias")
        is_active = self.active_state in ("active", "reloading")

        if is_enabled and is_active:
            return UnitStatus.ENABLED_ACTIVE
        if is_enabled:
            return UnitStatus.ENABLED_INACTIVE
        if is_active:
            return UnitStatus.DISABLED_ACTIVE
        return UnitStatus.DISABLED


class ProcessingResult(NamedTuple):
    unit_name: str
    category: Category
    status: UnitStatus
    message: str
    output: str = ""


@dataclass(frozen=True)
class UserContext:
    username: str
    home: Path
    uid: int
    gid: int
    is_root: bool


# ==============================================================================
# 4. CONTEXT & ENVIRONMENT RESOLUTION
# ==============================================================================

def resolve_user_context() -> UserContext:
    """Resolves real non-root user details safely across sudo/doas/pkexec contexts."""
    is_root = os.geteuid() == 0
    real_uid = os.getuid()

    if is_root:
        escalation_uid = os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID")
        if escalation_uid and escalation_uid.isdigit():
            real_uid = int(escalation_uid)
        elif "DOAS_USER" in os.environ:
            try:
                real_uid = pwd.getpwnam(os.environ["DOAS_USER"]).pw_uid
            except KeyError:
                pass
        else:
            try:
                real_uid = pwd.getpwnam(os.getlogin()).pw_uid
            except (OSError, KeyError):
                pass

    try:
        pw = pwd.getpwuid(real_uid)
    except KeyError:
        error_console.print(f"[bold red][ERROR][/bold red] Fatal: Resolved UID {real_uid} does not map to a valid user.")
        sys.exit(1)

    return UserContext(
        username=pw.pw_name,
        home=Path(pw.pw_dir),
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        is_root=is_root,
    )


def get_user_ipc_env(ctx: UserContext) -> dict[str, str]:
    """
    Constructs a sterile IPC environment to prevent DBus/systemctl --user failure
    when executed under root/sudo contexts.
    """
    runtime_dir = Path(f"/run/user/{ctx.uid}")
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


def build_systemctl_cmd(scope: Scope, args: list[str], ctx: UserContext, read_only: bool = False) -> tuple[list[str], dict[str, str] | None]:
    """Constructs systemctl command vectors and sterile execution environments."""
    if scope == Scope.SYSTEM:
        if read_only or ctx.is_root:
            return ["systemctl"] + args, None
        else:
            return ["sudo", "systemctl"] + args, None
    else:  # Scope.USER
        env = get_user_ipc_env(ctx)
        if ctx.is_root:
            if ctx.username == "root":
                return ["systemctl", "--user"] + args, env
            return ["sudo", "-u", ctx.username, "systemctl", "--user"] + args, env
        else:
            return ["systemctl", "--user"] + args, env


# ==============================================================================
# 5. DIAGNOSTICS & FUZZY MATCHING
# ==============================================================================

def normalize_unit_name(name: str) -> str:
    """Ensures service/socket/timer unit suffix is present."""
    if not any(name.endswith(ext) for ext in (".service", ".socket", ".timer", ".target", ".path", ".device", ".mount", ".automount", ".swap")):
        return f"{name}.service"
    return name


def suggest_missing_unit(unit_name: str, scope: Scope, ctx: UserContext) -> list[str]:
    """Finds fuzzy suggestions if a requested systemd unit file is missing."""
    search_dirs: list[Path] = []
    if scope == Scope.SYSTEM:
        search_dirs = [Path("/usr/lib/systemd/system"), Path("/etc/systemd/system")]
    else:
        search_dirs = [
            Path("/usr/lib/systemd/user"),
            ctx.home / ".config" / "systemd" / "user",
            ctx.home / ".local" / "share" / "systemd" / "user",
        ]

    available_units: list[str] = []
    for d in search_dirs:
        if d.exists() and d.is_dir():
            for p in d.iterdir():
                if p.is_file() and any(p.name.endswith(ext) for ext in (".service", ".timer", ".socket")):
                    available_units.append(p.name)

    matches = difflib.get_close_matches(unit_name, available_units, n=3, cutoff=0.5)
    return matches


# ==============================================================================
# 6. SYSTEMD ENGINE & QUERY PIPELINE
# ==============================================================================

def query_bulk_unit_states(units: list[tuple[ServiceConfig, Scope, Category]], ctx: UserContext) -> list[UnitState]:
    """Queries unit metadata in bulk via high-speed systemctl show property parsing."""
    states: list[UnitState] = []

    sys_targets = [t for t in units if t[1] == Scope.SYSTEM]
    usr_targets = [t for t in units if t[1] == Scope.USER]

    def fetch_scope_states(target_group: list[tuple[ServiceConfig, Scope, Category]], scope: Scope) -> dict[str, dict[str, str]]:
        if not target_group:
            return {}
        unit_names = [normalize_unit_name(t[0].name) for t in target_group]
        cmd, env = build_systemctl_cmd(
            scope,
            ["show", "--property=Id,ActiveState,UnitFileState,LoadState"] + unit_names,
            ctx=ctx,
            read_only=True,
        )
        try:
            res = subprocess.run(cmd, capture_output=True, env=env, text=True, timeout=15)
            out = res.stdout.strip()
            if not out:
                return {}

            results: dict[str, dict[str, str]] = {}
            current: dict[str, str] = {}
            for line in out.splitlines():
                line_str = line.strip()
                if not line_str:
                    if "Id" in current:
                        results[current["Id"]] = current
                    current = {}
                    continue
                if "=" in line_str:
                    k, v = line_str.split("=", 1)
                    current[k] = v
            if "Id" in current:
                results[current["Id"]] = current
            return results
        except (subprocess.TimeoutExpired, Exception):
            return {}

    sys_res = fetch_scope_states(sys_targets, Scope.SYSTEM)
    usr_res = fetch_scope_states(usr_targets, Scope.USER)

    for cfg, scope, cat in units:
        norm_name = normalize_unit_name(cfg.name)
        data = sys_res.get(norm_name) if scope == Scope.SYSTEM else usr_res.get(norm_name)
        if data and data.get("LoadState") != "not-found":
            st = UnitState(
                unit_name=norm_name,
                scope=scope,
                category=cat,
                description=cfg.description,
                exists=True,
                load_state=data.get("LoadState", "loaded"),
                active_state=data.get("ActiveState", "inactive"),
                unit_file_state=data.get("UnitFileState", "disabled"),
            )
        else:
            st = UnitState(unit_name=norm_name, scope=scope, category=cat, description=cfg.description, exists=False)
        states.append(st)

    return states


def process_unit_action(
    cfg: ServiceConfig,
    scope: Scope,
    category: Category,
    action: Literal["enable", "disable"],
    now: bool,
    interactive: bool,
    dry_run: bool,
    ctx: UserContext,
) -> ProcessingResult:
    """Enables or disables a single unit cleanly with interactive prompt support."""
    norm_name = normalize_unit_name(cfg.name)
    states = query_bulk_unit_states([(cfg, scope, category)], ctx)
    st = states[0]

    if not st.exists:
        suggestions = suggest_missing_unit(norm_name, scope, ctx)
        msg = "Unit not found (Package not installed)"
        if suggestions:
            msg += f" | Suggestions: {', '.join(suggestions)}"
        return ProcessingResult(unit_name=norm_name, category=category, status=UnitStatus.MISSING, message=msg)

    if st.status_enum == UnitStatus.MASKED:
        return ProcessingResult(unit_name=norm_name, category=category, status=UnitStatus.MASKED, message="Unit is masked (Skipped)")

    # Interactive prompt evaluation
    should_execute = True
    if interactive and sys.stdin.isatty():
        prompt_msg = f"Execute [bold cyan]{action}[/bold cyan] for [bold yellow]{norm_name}[/bold yellow]?"
        should_execute = Confirm.ask(prompt_msg, default=cfg.enabled_by_default if action == "enable" else False)

    if not should_execute:
        return ProcessingResult(unit_name=norm_name, category=category, status=st.status_enum, message="Skipped by user prompt")

    if action == "enable":
        if st.status_enum == UnitStatus.ENABLED_ACTIVE or (st.status_enum == UnitStatus.ENABLED_INACTIVE and not now):
            return ProcessingResult(
                unit_name=norm_name,
                category=category,
                status=st.status_enum,
                message="Already enabled & active" if st.active_state == "active" else "Already enabled",
            )
        if st.status_enum == UnitStatus.STATIC:
            if st.active_state == "active":
                return ProcessingResult(unit_name=norm_name, category=category, status=UnitStatus.STATIC, message="Static unit (Already active)")
            if not now:
                return ProcessingResult(unit_name=norm_name, category=category, status=UnitStatus.STATIC, message="Static unit (Cannot enable without --now start)")
            cmd_flags = ["start"]
        else:
            cmd_flags = ["enable"]
            if now:
                cmd_flags.append("--now")
    else:  # disable
        if st.status_enum == UnitStatus.DISABLED and st.active_state == "inactive":
            return ProcessingResult(unit_name=norm_name, category=category, status=UnitStatus.DISABLED, message="Already disabled & inactive")
        if st.status_enum == UnitStatus.STATIC:
            if st.active_state == "inactive":
                return ProcessingResult(unit_name=norm_name, category=category, status=UnitStatus.STATIC, message="Static unit (Already inactive)")
            cmd_flags = ["stop"]
        else:
            cmd_flags = ["disable"]
            if now:
                cmd_flags.append("--now")

    cmd_flags.append(norm_name)
    cmd, env = build_systemctl_cmd(scope, cmd_flags, ctx=ctx, read_only=False)

    if dry_run:
        return ProcessingResult(
            unit_name=norm_name,
            category=category,
            status=UnitStatus.ENABLED_ACTIVE if action == "enable" else UnitStatus.DISABLED,
            message=f"[DRY-RUN] Would execute: {' '.join(cmd)}",
        )

    try:
        proc = subprocess.run(cmd, capture_output=True, env=env, text=True, timeout=20)
        if proc.returncode == 0:
            msg = f"Successfully {action}d" + (" & started" if now and action == "enable" else (" & stopped" if now else ""))
            return ProcessingResult(
                unit_name=norm_name,
                category=category,
                status=UnitStatus.ENABLED_ACTIVE if action == "enable" and now else UnitStatus.DISABLED,
                message=msg,
                output=proc.stdout.strip(),
            )
        else:
            return ProcessingResult(
                unit_name=norm_name,
                category=category,
                status=UnitStatus.ERROR,
                message=f"Failed to {action}",
                output=proc.stderr.strip() or proc.stdout.strip(),
            )
    except subprocess.TimeoutExpired:
        return ProcessingResult(
            unit_name=norm_name,
            category=category,
            status=UnitStatus.ERROR,
            message="Execution timed out (20s limit reached)",
        )
    except Exception as e:
        return ProcessingResult(
            unit_name=norm_name,
            category=category,
            status=UnitStatus.ERROR,
            message=f"Execution error: {e}",
        )


def reload_dbus(dry_run: bool, ctx: UserContext) -> None:
    """Reloads DBus configuration via busctl for dbus-broker compatibility."""
    cmd = ["busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReloadConfig"]
    if dry_run:
        console.print(f"[bold yellow][DRY-RUN][/bold yellow] Would execute DBus reload: {' '.join(cmd)}")
        return
    env = get_user_ipc_env(ctx)
    try:
        res = subprocess.run(cmd, capture_output=True, env=env, text=True, timeout=10)
        if res.returncode == 0:
            console.print("[bold green][OK][/bold green] DBus configuration reloaded via busctl.")
        else:
            console.print(f"[bold yellow][WARN][/bold yellow] DBus reload skipped or failed: {res.stderr.strip()}")
    except Exception as e:
        console.print(f"[bold red][ERR][/bold red] DBus reload failed: {e}")


def daemon_reload(scope: Scope, dry_run: bool, ctx: UserContext) -> None:
    """Executes daemon-reload for system or user scope."""
    cmd, env = build_systemctl_cmd(scope, ["daemon-reload"], ctx=ctx, read_only=False)
    scope_str = "System" if scope == Scope.SYSTEM else "User"
    if dry_run:
        console.print(f"[bold yellow][DRY-RUN][/bold yellow] Would run {scope_str} daemon-reload: {' '.join(cmd)}")
        return
    res = subprocess.run(cmd, capture_output=True, env=env, text=True, timeout=20)
    if res.returncode == 0:
        console.print(f"[bold green][OK][/bold green] Executed {scope_str} daemon-reload.")
    else:
        console.print(f"[bold red][ERR][/bold red] Failed {scope_str} daemon-reload: {res.stdout.strip()}")


# ==============================================================================
# 7. RICH PRESENTATION & RENDERING
# ==============================================================================

def render_header(ctx: UserContext) -> None:
    """Renders the main Dusky Service Deployer header panel."""
    header_text = Text()
    header_text.append("⚡ DUSKY SERVICE DEPLOYER (290_dusky_service_deployer.py) ⚡\n", style="bold cyan")
    header_text.append("Context: Hyprland / UWSM | Kernel: ", style="dim white")
    header_text.append(f"{os.uname().release} | User: ", style="bold yellow")
    header_text.append(ctx.username, style="bold green")
    header_text.append(f" (Root: {ctx.is_root})", style="dim cyan")
    console.print(Panel(header_text, expand=False, border_style="cyan"))


def render_status_table(states: list[UnitState]) -> None:
    """Renders a summary table of all service statuses."""
    table = Table(title="Dusky Deployed Systemd Services Overview", show_header=True, header_style="bold magenta", expand=True)

    table.add_column("Scope", style="dim", width=8)
    table.add_column("Category", width=22)
    table.add_column("Unit Name", style="bold")
    table.add_column("Installed", justify="center", width=12)
    table.add_column("Enabled State", justify="center", width=14)
    table.add_column("Active State", justify="center", width=12)
    table.add_column("Overall Status", width=22)

    for st in states:
        scope_badge = "[blue]SYSTEM[/blue]" if st.scope == Scope.SYSTEM else "[purple]USER[/purple]"
        installed_badge = "[green]YES[/green]" if st.exists else "[red]NO[/red]"
        enabled_badge = f"[green]{st.unit_file_state.upper()}[/green]" if st.unit_file_state in ("enabled", "static") else f"[yellow]{st.unit_file_state.upper()}[/yellow]"
        active_badge = f"[green]{st.active_state.upper()}[/green]" if st.active_state == "active" else f"[dim]{st.active_state.upper()}[/dim]"

        match st.status_enum:
            case UnitStatus.ENABLED_ACTIVE:
                status_fmt = "[bold green]✔ Enabled & Active[/bold green]"
            case UnitStatus.ENABLED_INACTIVE:
                status_fmt = "[cyan]● Enabled[/cyan]"
            case UnitStatus.DISABLED_ACTIVE:
                status_fmt = "[yellow]▲ Active (Disabled)[/yellow]"
            case UnitStatus.DISABLED:
                status_fmt = "[yellow]○ Disabled[/yellow]"
            case UnitStatus.STATIC:
                status_fmt = "[blue]🔒 Static[/blue]"
            case UnitStatus.MASKED:
                status_fmt = "[magenta]🚫 Masked[/magenta]"
            case UnitStatus.MISSING:
                status_fmt = "[dim red]✖ Not Installed[/dim red]"
            case UnitStatus.ERROR:
                status_fmt = "[bold red]✖ Error[/bold red]"

        table.add_row(
            scope_badge,
            st.category.value,
            st.unit_name,
            installed_badge,
            enabled_badge,
            active_badge,
            status_fmt,
        )

    console.print(table)


def export_json_status(states: list[UnitState]) -> None:
    """Exports unit status data as formatted JSON."""
    data = [
        {
            "unit": st.unit_name,
            "scope": st.scope.name,
            "category": st.category.value,
            "description": st.description,
            "installed": st.exists,
            "load_state": st.load_state,
            "unit_file_state": st.unit_file_state,
            "active_state": st.active_state,
            "status": st.status_enum.value,
        }
        for st in states
    ]
    print(json.dumps(data, indent=2))


def render_results(results: list[ProcessingResult]) -> None:
    """Displays action execution results categorized neatly."""
    success_count = 0
    skip_count = 0
    missing_count = 0
    error_count = 0

    current_category: Category | None = None

    for res in results:
        if res.category != current_category:
            current_category = res.category
            console.print(f"\n[bold yellow]=== {current_category.value} ===[/bold yellow]")

        match res.status:
            case UnitStatus.ENABLED_ACTIVE | UnitStatus.ENABLED_INACTIVE:
                console.print(f" [bold green][OK][/bold green]    {res.unit_name:<30} -> {res.message}")
                success_count += 1
            case UnitStatus.DISABLED | UnitStatus.STATIC | UnitStatus.DISABLED_ACTIVE:
                console.print(f" [bold blue][SKIP][/bold blue]  {res.unit_name:<30} -> {res.message}")
                skip_count += 1
            case UnitStatus.MISSING:
                console.print(f" [bold yellow][MISSING][/bold yellow] {res.unit_name:<30} -> {res.message}")
                missing_count += 1
            case UnitStatus.ERROR:
                console.print(f" [bold red][FAIL][/bold red]   {res.unit_name:<30} -> {res.message}")
                if res.output:
                    console.print(f"         └─ [red]{res.output}[/red]")
                error_count += 1

    summary_text = (
        f"[bold green]Success: {success_count}[/bold green] | "
        f"[bold blue]Unchanged/Skipped: {skip_count}[/bold blue] | "
        f"[yellow]Missing: {missing_count}[/yellow] | "
        f"[bold red]Errors: {error_count}[/bold red]"
    )
    console.print(Panel(summary_text, title="Execution Summary", border_style="bright_blue"))


# ==============================================================================
# 8. CLI ORCHESTRATION & MAIN ENTRYPOINT
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dusky Service Deployer - Arch Linux Systemd & AUR Service Deployer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("-a", "--all", action="store_true", help="Process ALL service categories (default)")
    parser.add_argument("-s", "--system", action="store_true", help="Process Core System services")
    parser.add_argument("--aur-system", action="store_true", help="Process AUR System services")
    parser.add_argument("-u", "--user", action="store_true", help="Process Core User services")
    parser.add_argument("--aur-user", action="store_true", help="Process AUR User services")
    parser.add_argument("-c", "--status", action="store_true", help="Inspect and display status matrix without making changes")
    parser.add_argument("-i", "--interactive", action="store_true", help="Interactively prompt before processing each service")
    parser.add_argument("-y", "--default", action="store_true", help="Non-interactive mode: Auto-apply default configured actions")
    parser.add_argument("--disable", action="store_true", help="Disable (and stop) targeted services")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions without executing systemctl commands")
    parser.add_argument("--no-now", action="store_true", help="Enable/disable services without immediate start/stop")
    parser.add_argument("--daemon-reload", action="store_true", help="Issue daemon-reload after processing services")
    parser.add_argument("--dbus-reload", action="store_true", help="Issue DBus configuration reload via busctl")
    parser.add_argument("--json", action="store_true", help="Output status matrix as JSON (used with --status)")

    args = parser.parse_args()

    # Pre-execution sanity check
    if not shutil.which("systemctl"):
        error_console.print("[bold red][ERROR][/bold red] Systemd (systemctl) not found. This script requires systemd.")
        sys.exit(1)

    ctx = resolve_user_context()

    if not args.json:
        render_header(ctx)

    # Determine targeted categories
    targeted_categories: set[Category] = set()

    if args.system:
        targeted_categories.add(Category.SYSTEM_CORE)
    if args.aur_system:
        targeted_categories.add(Category.SYSTEM_AUR)
    if args.user:
        targeted_categories.add(Category.USER_CORE)
    if args.aur_user:
        targeted_categories.add(Category.USER_AUR)

    if args.all or not targeted_categories:
        targeted_categories = {
            Category.SYSTEM_CORE,
            Category.SYSTEM_AUR,
            Category.USER_CORE,
            Category.USER_AUR,
        }

    # Subprocess Sudo Escalation Fork for System Services
    requires_system_scope = any(cat in (Category.SYSTEM_CORE, Category.SYSTEM_AUR) for cat in targeted_categories)
    if requires_system_scope and not ctx.is_root and not args.status and not args.dry_run:
        if shutil.which("sudo"):
            if not args.json:
                console.print("[bold blue][INFO][/bold blue] System services require root privileges. Forking via sudo...")
            script_path = Path(sys.argv[0]).resolve().as_posix()
            python_path = os.pathsep.join(sys.path)
            sudo_cmd = ["sudo", "env", f"PYTHONPATH={python_path}", sys.executable, script_path, "--system", "--aur-system"]
            if args.disable:
                sudo_cmd.append("--disable")
            if args.no_now:
                sudo_cmd.append("--no-now")
            if args.interactive:
                sudo_cmd.append("--interactive")
            if args.default:
                sudo_cmd.append("--default")
            if args.daemon_reload:
                sudo_cmd.append("--daemon-reload")

            subprocess.run(sudo_cmd, check=False)
            targeted_categories.discard(Category.SYSTEM_CORE)
            targeted_categories.discard(Category.SYSTEM_AUR)
            if not targeted_categories:
                if args.dbus_reload:
                    reload_dbus(dry_run=args.dry_run, ctx=ctx)
                return

    # Build target service tuples: (ServiceConfig, scope, category)
    unit_targets: list[tuple[ServiceConfig, Scope, Category]] = []

    if Category.SYSTEM_CORE in targeted_categories:
        unit_targets.extend([(cfg, Scope.SYSTEM, Category.SYSTEM_CORE) for cfg in SYSTEM_SERVICES if args.status or args.all or cfg.enabled_by_default or args.interactive])
    if Category.SYSTEM_AUR in targeted_categories:
        unit_targets.extend([(cfg, Scope.SYSTEM, Category.SYSTEM_AUR) for cfg in AUR_SYSTEM_SERVICES if args.status or args.all or cfg.enabled_by_default or args.interactive])
    if Category.USER_CORE in targeted_categories:
        unit_targets.extend([(cfg, Scope.USER, Category.USER_CORE) for cfg in USER_SERVICES if args.status or args.all or cfg.enabled_by_default or args.interactive])
    if Category.USER_AUR in targeted_categories:
        unit_targets.extend([(cfg, Scope.USER, Category.USER_AUR) for cfg in AUR_USER_SERVICES if args.status or args.all or cfg.enabled_by_default or args.interactive])

    # Status Inspection Mode
    if args.status:
        if not args.json:
            console.print("\n[bold cyan]Querying systemd unit states in bulk via property engine...[/bold cyan]")
        states = query_bulk_unit_states(unit_targets, ctx=ctx)
        states.sort(key=lambda s: (s.category.value, s.unit_name))

        if args.json:
            export_json_status(states)
        else:
            render_status_table(states)
        return

    # Execution Mode (Enable or Disable)
    action_type: Literal["enable", "disable"] = "disable" if args.disable else "enable"
    now_flag = not args.no_now
    results: list[ProcessingResult] = []

    console.print(f"\n[bold cyan]Executing '{action_type}' for {len(unit_targets)} services...[/bold cyan]")
    if args.dry_run:
        console.print("[bold yellow]*** DRY-RUN MODE ACTIVE - No changes will be made ***[/bold yellow]\n")

    for cfg, scope, cat in unit_targets:
        res = process_unit_action(
            cfg=cfg,
            scope=scope,
            category=cat,
            action=action_type,
            now=now_flag,
            interactive=args.interactive and not args.default,
            dry_run=args.dry_run,
            ctx=ctx,
        )
        results.append(res)

    render_results(results)

    # Perform daemon-reload or dbus-reload
    if args.daemon_reload:
        console.print("\n[bold cyan]Triggering daemon-reload...[/bold cyan]")
        if any(scope == Scope.SYSTEM for _, scope, _ in unit_targets):
            daemon_reload(Scope.SYSTEM, dry_run=args.dry_run, ctx=ctx)
        if any(scope == Scope.USER for _, scope, _ in unit_targets):
            daemon_reload(Scope.USER, dry_run=args.dry_run, ctx=ctx)

    if args.dbus_reload:
        reload_dbus(dry_run=args.dry_run, ctx=ctx)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold red][ABORTED][/bold red] Interrupted by user (SIGINT).")
        sys.exit(130)
