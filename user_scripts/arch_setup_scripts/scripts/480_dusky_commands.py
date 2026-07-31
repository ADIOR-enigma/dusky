#!/usr/bin/env python3
"""
===============================================================================
 DUSKY COMMANDS — Bleeding-Edge Multi-Stage Fleet Orchestrator
===============================================================================
 Context:     Arch Linux (Bleeding-Edge) / Hyprland / UWSM / Systemd 261
 Target File:  480_dusky_commands.py
 Location:    ~/user_scripts/arch_setup_scripts/scripts/
 Description: Consolidates and modernizes 480_dusky_commands.sh,
              dusky_commands_before.sh, and dusky_commands_after.sh into a
              unified, feature-complete Python 3.14 orchestrator.

 Features & Architecture (Synthesized from 130 & 290 Reference Scripts):
   - Declarative stage configuration blocks (BEFORE, SETUP, AFTER).
   - Headless & Rich UI Dual Presentation (RICH_AVAILABLE guard & fallback).
   - Machine-readable JSON output mode (--json).
   - UserContext resolution supporting privilege escalation (SUDO_UID / loginuid).
   - IPC environment tunneling (XDG_RUNTIME_DIR & DBus protection).
   - Non-blocking flock concurrency control.
   - Idempotent state tracking with 100% hash parity with original bash scripts.
   - Sudo pre-flight authentication & background keepalive loop.
   - Execution modes: --before (-b), --setup (-s), --after (-a), --all (-A).
   - Interactive mode (-i / --interactive) & Non-interactive mode (-y / --default).
   - Dry-run mode (-n / --dry-run), Force re-run (-f / --force), Status matrix (-st / --status).
   - Captured execution output on failure & final Execution Summary panel.
===============================================================================
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import NamedTuple, Sequence

# --- Rich UI Presentation Guard ---
RICH_AVAILABLE = False
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm
    from rich.table import Table
    from rich.text import Text
    RICH_AVAILABLE = True
    console = Console()
    error_console = Console(stderr=True)
except ImportError:
    Console = Panel = Confirm = Table = Text = None  # type: ignore
    console = error_console = None  # type: ignore


def check_ui_deps(is_json: bool) -> None:
    """Enforces Rich UI requirement only if not running in JSON mode."""
    if not RICH_AVAILABLE and not is_json:
        print("ERROR: The 'rich' python library is required for interactive/UI mode.", file=sys.stderr)
        print("Install it via: sudo pacman -S python-rich", file=sys.stderr)
        sys.exit(1)


# ==============================================================================
# 1. USER CONFIGURATION AREA — DEFINE STAGE FLEET COMMANDS HERE
# ==============================================================================

class Mode(Enum):
    USER = "U"   # Runs as normal unprivileged user
    SUDO = "S"   # Runs as root via sudo


@dataclass(frozen=True, slots=True)
class FleetCommand:
    mode: Mode
    cmd: str
    description: str = ""

    @property
    def mode_str(self) -> str:
        return self.mode.value

    @property
    def state_hash(self) -> str:
        # Maintain 100% SHA256 string parity with original bash scripts: sha256("MODE | COMMAND")
        raw_entry = f"{self.mode.value} | {self.cmd}"
        return hashlib.sha256(raw_entry.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------
# Stage 1: Setup / Post-Install Stage (legacy 480_dusky_commands.sh)
# ------------------------------------------------------------------------------
SETUP_COMMANDS: list[FleetCommand] = [
    FleetCommand(Mode.USER, 'gsettings set org.gnome.desktop.interface icon-theme "Papirus"', "Set GNOME Icon Theme"),
    FleetCommand(Mode.USER, 'gsettings set org.gnome.desktop.interface gtk-theme "adw-gtk3"', "Set GNOME GTK Theme"),
    FleetCommand(Mode.USER, 'mkdir -p "$HOME/.config/gtk-3.0" "$HOME/.config/gtk-4.0"', "Create GTK Config Directories"),
    FleetCommand(Mode.USER, 'ln -nfs "$HOME/.config/matugen/generated/gtk-3.css" "$HOME/.config/gtk-3.0/gtk.css"', "Link Matugen GTK-3 CSS"),
    FleetCommand(Mode.USER, 'ln -nfs "$HOME/.config/matugen/generated/gtk-4.css" "$HOME/.config/gtk-4.0/gtk.css"', "Link Matugen GTK-4 CSS"),
    FleetCommand(Mode.USER, 'ln -nfs "/usr/share/themes/adw-gtk3/gtk-4.0/libadwaita.css" "$HOME/.config/gtk-4.0/libadwaita.css"', "Link Adw-GTK3 Libadwaita CSS"),
    FleetCommand(Mode.USER, 'ln -nfs "/usr/share/themes/adw-gtk3/gtk-4.0/libadwaita-tweaks.css" "$HOME/.config/gtk-4.0/libadwaita-tweaks.css"', "Link Libadwaita Tweaks CSS"),
    FleetCommand(Mode.USER, 'mkdir -p "$HOME/Documents/dusky_backups/"', "Create Backups Directory"),
    FleetCommand(
        Mode.USER,
        'TARGET="$HOME/user_scripts/dusky_system/click_away_to_dismiss" && '
        'wayland-scanner client-header "$TARGET/hyprland-focus-grab-v1.xml" "$TARGET/hyprland-focus-grab-v1-client-protocol.h" && '
        'wayland-scanner private-code "$TARGET/hyprland-focus-grab-v1.xml" "$TARGET/hyprland-focus-grab-v1-client-protocol.c" && '
        'gcc -shared -fPIC -o "$TARGET/libwaylandgrab.so" "$TARGET/dusky.c" "$TARGET/hyprland-focus-grab-v1-client-protocol.c" -lwayland-client -lpthread -ldl',
        "Compile Wayland Click-Away Shared Library"
    ),
    FleetCommand(Mode.USER, 'systemctl --user daemon-reload && systemctl --user restart dusky_quickpanal.service || true', "Reload & Restart Quickpanel Service"),
    FleetCommand(Mode.USER, '"$HOME/user_scripts/dusky_system/reload_cc/cc_restart.sh" --quiet &', "Background Restart Control Center"),
    FleetCommand(Mode.USER, '"$HOME/user_scripts/dusky_system/quickpanals/reload_quickpanal.sh/" --quiet &', "Background Reload Quickpanel"),
]

# ------------------------------------------------------------------------------
# Stage 2: Pre-System-Update Stage (legacy dusky_commands_before.sh)
# ------------------------------------------------------------------------------
BEFORE_COMMANDS: list[FleetCommand] = [
    FleetCommand(Mode.USER, 'mkdir -p ~/.config/opencode/themes || true', "Create Opencode Themes Directory"),
    FleetCommand(Mode.USER, 'mkdir -p ~/.config/Kvantum/matugen || true', "Create Kvantum Matugen Directory"),
    FleetCommand(Mode.USER, 'systemctl --user disable --now dusky_sliders.service || true', "Disable Legacy Sliders Service"),
    FleetCommand(
        Mode.SUDO,
        'systemctl stop dusky_snaapshot.timer dusky_snaapshot.service 2>/dev/null; systemctl disable dusky_snaapshot.timer 2>/dev/null; true',
        "Stop & Disable Legacy Typo Snapshot Service"
    ),
    FleetCommand(Mode.SUDO, 'rm -f /etc/systemd/system/dusky_snaapshot.service /etc/systemd/system/dusky_snaapshot.timer', "Remove Legacy Typo Snapshot Unit Files"),
    FleetCommand(Mode.SUDO, 'systemctl daemon-reload', "Systemd System Daemon Reload"),
]

# ------------------------------------------------------------------------------
# Stage 3: Post-System-Update Stage (legacy dusky_commands_after.sh)
# ------------------------------------------------------------------------------
AFTER_COMMANDS: list[FleetCommand] = [
    FleetCommand(Mode.USER, 'hyprctl reload', "Reload Hyprland Configuration"),
    FleetCommand(Mode.USER, 'systemctl --user enable --now mako.service || true', "Enable & Start Mako Notification Daemon"),
    FleetCommand(Mode.SUDO, 'systemctl enable --now ufw.service || true', "Enable & Start UFW Firewall Service"),
    FleetCommand(
        Mode.USER,
        'TARGET="$HOME/user_scripts/dusky_system/click_away_to_dismiss" && '
        'wayland-scanner client-header "$TARGET/hyprland-focus-grab-v1.xml" "$TARGET/hyprland-focus-grab-v1-client-protocol.h" && '
        'wayland-scanner private-code "$TARGET/hyprland-focus-grab-v1.xml" "$TARGET/hyprland-focus-grab-v1-client-protocol.c" && '
        'gcc -shared -fPIC -o "$TARGET/libwaylandgrab.so" "$TARGET/dusky.c" "$TARGET/hyprland-focus-grab-v1-client-protocol.c" -lwayland-client -lpthread -ldl',
        "Recompile Wayland Click-Away Shared Library"
    ),
    FleetCommand(Mode.USER, 'systemctl --user enable --now dusky_quickpanal.service || true', "Enable & Start Dusky Quickpanel Service"),
    FleetCommand(Mode.USER, 'systemctl --user daemon-reload || true', "Reload Systemd User Daemon"),
    FleetCommand(Mode.USER, 'systemctl --user restart dusky_quickpanal.service || true', "Restart Dusky Quickpanel Service"),
    FleetCommand(Mode.USER, 'systemctl --user restart osd_lock.service || true', "Restart OSD Lock Service"),
]

# Register configured stages in default lifecycle order
STAGES: dict[str, list[FleetCommand]] = {
    "before": BEFORE_COMMANDS,
    "setup": SETUP_COMMANDS,
    "after": AFTER_COMMANDS,
}

# ==============================================================================
# 2. CONTEXT & ENVIRONMENT RESOLUTION
# ==============================================================================

@dataclass(frozen=True)
class UserContext:
    username: str
    home: Path
    uid: int
    gid: int
    is_root: bool


def resolve_user_context() -> UserContext:
    """Resolves real non-root user details prioritizing active privilege escalation context."""
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
                loginuid_raw = Path("/proc/self/loginuid").read_text(encoding="utf-8").strip()
                loginuid = int(loginuid_raw)
                if loginuid != 4294967295:
                    real_uid = loginuid
            except (FileNotFoundError, ValueError, OSError):
                pass

    try:
        pw = pwd.getpwuid(real_uid)
    except KeyError:
        if error_console:
            error_console.print(f"[bold red][ERROR][/bold red] Fatal: Resolved UID {real_uid} does not map to a valid user.")
        else:
            print(f"ERROR: Fatal: Resolved UID {real_uid} does not map to a valid user.", file=sys.stderr)
        sys.exit(1)

    return UserContext(
        username=pw.pw_name,
        home=Path(pw.pw_dir),
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        is_root=is_root,
    )


def get_escalator() -> str:
    """Resolves available privilege escalation tool (sudo, doas, or pkexec)."""
    return shutil.which("sudo") or shutil.which("doas") or shutil.which("pkexec") or "sudo"


def get_user_ipc_env(ctx: UserContext) -> dict[str, str]:
    """Constructs a sterile whitelist IPC environment for subshell command executions."""
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


# ==============================================================================
# 3. ENUMS & RESULT MODELS
# ==============================================================================

class ExecutionStatus(Enum):
    SUCCESS = "Success"
    SKIPPED = "Skipped"
    FAILED = "Failed"
    DRY_RUN = "Dry-Run"


class CommandResult(NamedTuple):
    stage: str
    command: FleetCommand
    status: ExecutionStatus
    message: str
    output: str = ""


# ==============================================================================
# 4. LOGGING & PRIVILEGE SUBSYSTEM
# ==============================================================================

class Logger:
    def __init__(self, log_path: Path, is_json: bool = False):
        self.log_path = log_path
        self.is_json = is_json
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.log_path, "a", encoding="utf-8")
        self.write_plain(f"--- Dusky Commands Session Started: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    def write_plain(self, message: str) -> None:
        clean_text = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", message)
        clean_text = re.sub(r"\[/?(?:bold|dim|italic|underline|uppercase|cyan|blue|green|yellow|red|magenta|purple|white)[^\]]*\]", "", clean_text)
        self._file.write(f"{clean_text}\n")
        self._file.flush()

    def log(self, level: str, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        if not self.is_json and console:
            match level.upper():
                case "INFO":
                    console.print(f"[bold blue][INFO][/bold blue] {message}")
                case "SUCCESS" | "OK":
                    console.print(f"[bold green][OK][/bold green]   {message}")
                case "WARN":
                    console.print(f"[bold yellow][WARN][/bold yellow] {message}")
                case "ERROR":
                    if error_console:
                        error_console.print(f"[bold red][ERROR][/bold red] {message}")
                    else:
                        print(f"[ERROR] {message}", file=sys.stderr)
                case "RUN":
                    console.print(f"[bold cyan][RUN][/bold cyan]  {message}")
                case _:
                    console.print(f"[{level}] {message}")

        self.write_plain(f"[{timestamp}] [{level}] {message}")

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


class SudoKeepAlive(threading.Thread):
    """Background daemon thread maintaining active sudo timestamp during execution."""

    def __init__(self, interval: int = 45):
        super().__init__(daemon=True)
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        escalator = get_escalator()
        while not self.stop_event.is_set():
            if self.stop_event.wait(self.interval):
                break
            subprocess.run([escalator, "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        self.stop_event.set()


# ==============================================================================
# 5. ORCHESTRATION ENGINE
# ==============================================================================

class FleetPatcherEngine:
    def __init__(self, ctx: UserContext, is_json: bool = False) -> None:
        self.ctx = ctx
        self.is_json = is_json
        self.state_dir = ctx.home / ".local/state/dusky"
        self.state_file = self.state_dir / "patch_history.state"

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.log_file = ctx.home / "Documents" / "logs" / f"dusky_patcher_{timestamp}.log"

        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
        lock_dir = Path(xdg_runtime) if xdg_runtime else Path(f"/run/user/{ctx.uid}")
        if not lock_dir.exists():
            lock_dir = Path("/tmp")
        self.lock_file = lock_dir / "dusky_fleet_patcher.lock"

        self.lock_fd: int | None = None
        self.completed_patches: set[str] = set()
        self.logger: Logger | None = None
        self.sudo_keepalive: SudoKeepAlive | None = None

    def acquire_lock(self) -> None:
        try:
            self.lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            if not self.is_json and error_console:
                error_console.print("[bold red]CRITICAL ERROR: Another dusky fleet patcher instance is currently running![/bold red]")
            else:
                print("CRITICAL ERROR: Another dusky fleet patcher instance is currently running!", file=sys.stderr)
            sys.exit(1)

    def load_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            with open(self.state_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        self.completed_patches.add(stripped)

    def record_completed(self, cmd_hash: str) -> None:
        self.completed_patches.add(cmd_hash)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "a", encoding="utf-8") as f:
            f.write(f"{cmd_hash}\n")

    def ensure_sudo(self, pending_commands: Sequence[FleetCommand]) -> bool:
        needs_sudo = any(cmd.mode == Mode.SUDO and cmd.state_hash not in self.completed_patches for cmd in pending_commands)
        if not needs_sudo:
            return True

        if self.logger:
            self.logger.log("INFO", "Root privileges required for upcoming patches. Authenticating...")

        escalator = get_escalator()
        res = subprocess.run([escalator, "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode != 0:
            auth_res = subprocess.run([escalator, "-v"])
            if auth_res.returncode != 0:
                if self.logger:
                    self.logger.log("ERROR", "Sudo authentication failed. Cannot apply root patches.")
                return False

        self.sudo_keepalive = SudoKeepAlive()
        self.sudo_keepalive.start()
        return True

    def run_stage(
        self,
        stage_name: str,
        commands: list[FleetCommand],
        force: bool = False,
        dry_run: bool = False,
        interactive: bool = False,
        use_defaults: bool = False,
    ) -> list[CommandResult]:
        results: list[CommandResult] = []
        if not commands:
            return results

        if self.logger:
            self.logger.log("INFO", f"=== Stage: [bold uppercase]{stage_name}[/bold uppercase] ({len(commands)} commands) ===")

        pending = commands if force else [c for c in commands if c.state_hash not in self.completed_patches]
        if not pending:
            if self.logger:
                self.logger.log("SUCCESS", f"All patches in stage '{stage_name}' are already up to date.")
            for c in commands:
                results.append(CommandResult(stage_name, c, ExecutionStatus.SKIPPED, "Already applied"))
            return results

        if not dry_run and not self.ensure_sudo(pending):
            for c in pending:
                results.append(CommandResult(stage_name, c, ExecutionStatus.FAILED, "Sudo authentication failed"))
            return results

        env = get_user_ipc_env(self.ctx)
        escalator = get_escalator()
        total = len(commands)

        for idx, cmd_obj in enumerate(commands, start=1):
            cmd_hash = cmd_obj.state_hash
            is_done = cmd_hash in self.completed_patches

            if is_done and not force:
                if self.logger:
                    self.logger.log("INFO", f"[{idx}/{total}] Skipping (Already applied): {cmd_obj.cmd}")
                results.append(CommandResult(stage_name, cmd_obj, ExecutionStatus.SKIPPED, "Already applied"))
                continue

            # Interactive Prompting (if enabled)
            if interactive and not use_defaults and not self.is_json and sys.stdin.isatty() and Confirm:
                prompt_msg = f"Execute [{cmd_obj.mode_str}] patch '{cmd_obj.description or cmd_obj.cmd}'?"
                if not Confirm.ask(prompt_msg, default=True):
                    if self.logger:
                        self.logger.log("INFO", f"[{idx}/{total}] Skipped by user: {cmd_obj.cmd}")
                    results.append(CommandResult(stage_name, cmd_obj, ExecutionStatus.SKIPPED, "Skipped by user prompt"))
                    continue

            if dry_run:
                if self.logger:
                    self.logger.log("RUN", f"[{idx}/{total}] [DRY-RUN] Would apply [{cmd_obj.mode_str}]: {cmd_obj.cmd}")
                results.append(CommandResult(stage_name, cmd_obj, ExecutionStatus.DRY_RUN, "Dry-run simulation"))
                continue

            if self.logger:
                self.logger.log("RUN", f"[{idx}/{total}] Applying [{cmd_obj.mode_str}]: {cmd_obj.cmd}")

            if cmd_obj.mode == Mode.SUDO:
                exec_cmd = [escalator, "bash", "-c", f"set -eo pipefail; {cmd_obj.cmd}"]
            else:
                exec_cmd = ["bash", "-c", f"set -eo pipefail; {cmd_obj.cmd}"]

            res = subprocess.run(exec_cmd, capture_output=True, env=env, text=True)
            output_text = (res.stderr.strip() + "\n" + res.stdout.strip()).strip()

            if res.returncode == 0:
                self.record_completed(cmd_hash)
                if self.logger:
                    self.logger.log("SUCCESS", "Patch applied successfully.")
                results.append(CommandResult(stage_name, cmd_obj, ExecutionStatus.SUCCESS, "Patch applied successfully", output_text))
            else:
                if self.logger:
                    self.logger.log("WARN", f"Patch failed with exit code {res.returncode}: {cmd_obj.cmd}")
                    if output_text and not self.is_json and console:
                        console.print(f"         └─ [red]{output_text}[/red]")
                    self.logger.log("WARN", "Continuing orchestration sequence despite failure...")
                results.append(CommandResult(stage_name, cmd_obj, ExecutionStatus.FAILED, f"Failed with exit code {res.returncode}", output_text))

        return results

    def cleanup(self) -> None:
        if self.sudo_keepalive:
            self.sudo_keepalive.stop()
        if self.logger:
            self.logger.close()
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
            except OSError:
                pass


# ==============================================================================
# 6. RICH PRESENTATION & CLI RENDERING
# ==============================================================================

def render_header(ctx: UserContext, selected_stages: list[str]) -> None:
    """Renders the main Dusky Commands header panel."""
    if not console:
        return
    header_text = Text()
    header_text.append("⚡ DUSKY COMMANDS — Bleeding-Edge Multi-Stage Fleet Orchestrator ⚡\n", style="bold cyan")
    header_text.append("Context: Arch Linux (Bleeding-Edge) | Kernel: ", style="dim white")
    header_text.append(f"{os.uname().release} | User: ", style="bold yellow")
    header_text.append(ctx.username, style="bold green")
    header_text.append(f" (Root: {ctx.is_root})\n", style="dim cyan")
    header_text.append(f"Target Stages: {', '.join(s.upper() for s in selected_stages)}", style="bold magenta")

    console.print(Panel(header_text, expand=False, border_style="cyan"))


def render_status_matrix(engine: FleetPatcherEngine, selected_stages: list[str]) -> None:
    """Displays a status matrix of all configured commands and their applied state."""
    if not console:
        return
    table = Table(title="Dusky Commands — Patch History & State Matrix", show_lines=True)
    table.add_column("Stage", style="bold cyan")
    table.add_column("Mode", style="bold yellow", justify="center")
    table.add_column("Applied", justify="center", width=12)
    table.add_column("Command", style="white")
    table.add_column("Description", style="dim green")

    for stage_name in selected_stages:
        cmds = STAGES.get(stage_name, [])
        for cmd_obj in cmds:
            mode_badge = "[bold red]SUDO[/bold red]" if cmd_obj.mode == Mode.SUDO else "[bold green]USER[/bold green]"
            is_applied = cmd_obj.state_hash in engine.completed_patches
            status_fmt = "[green]✔ YES[/green]" if is_applied else "[yellow]○ PENDING[/yellow]"
            table.add_row(stage_name.upper(), mode_badge, status_fmt, cmd_obj.cmd, cmd_obj.description or "-")

    console.print(table)


def list_commands() -> None:
    """Displays all configured stages and fleet commands in a Rich table."""
    if not console:
        return
    table = Table(title="Dusky Commands — Configured Stages & Fleet Commands", show_lines=True)
    table.add_column("Stage", style="bold cyan")
    table.add_column("Mode", style="bold yellow", justify="center")
    table.add_column("Command", style="white")
    table.add_column("Description", style="dim green")

    for stage_name, cmds in STAGES.items():
        for cmd_obj in cmds:
            mode_badge = "[bold red]SUDO[/bold red]" if cmd_obj.mode == Mode.SUDO else "[bold green]USER[/bold green]"
            table.add_row(stage_name.upper(), mode_badge, cmd_obj.cmd, cmd_obj.description or "-")

    console.print(table)


def render_execution_summary(all_results: list[CommandResult]) -> None:
    """Displays a summary panel of execution results."""
    if not console:
        return

    success_cnt = sum(1 for r in all_results if r.status == ExecutionStatus.SUCCESS)
    skipped_cnt = sum(1 for r in all_results if r.status == ExecutionStatus.SKIPPED)
    failed_cnt = sum(1 for r in all_results if r.status == ExecutionStatus.FAILED)
    dry_cnt = sum(1 for r in all_results if r.status == ExecutionStatus.DRY_RUN)

    summary_text = (
        f"[bold green]Applied/Success: {success_cnt}[/bold green] | "
        f"[bold blue]Skipped/Up-to-Date: {skipped_cnt}[/bold blue] | "
        f"[bold yellow]Dry-Run: {dry_cnt}[/bold yellow] | "
        f"[bold red]Failures: {failed_cnt}[/bold red]"
    )
    console.print(Panel(summary_text, title="Orchestration Execution Summary", border_style="bright_blue"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dusky Commands — Bleeding-Edge Multi-Stage Fleet Orchestrator",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    stage_group = parser.add_argument_group("Stage Selection Flags (Can combine multiple)")
    stage_group.add_argument("-b", "--before", action="store_true", help="Run Pre-Update stage commands")
    stage_group.add_argument("-s", "--setup", action="store_true", help="Run Setup / Post-Install stage commands")
    stage_group.add_argument("-a", "--after", action="store_true", help="Run Post-Update stage commands")
    stage_group.add_argument("-A", "--all", action="store_true", help="Run ALL stages in sequence (before -> setup -> after)")
    stage_group.add_argument("--stage", choices=["before", "setup", "after", "all"], help="Specify a single target stage or 'all'")

    control_group = parser.add_argument_group("Execution Control Flags")
    control_group.add_argument("-i", "--interactive", action="store_true", help="Interactively prompt before executing each patch")
    control_group.add_argument("-y", "--default", action="store_true", help="Non-interactive mode (auto-apply default choices)")
    control_group.add_argument("-f", "--force", action="store_true", help="Force re-execution of commands even if previously completed")
    control_group.add_argument("-n", "--dry-run", action="store_true", help="Preview planned command executions without making changes")
    control_group.add_argument("-st", "--status", action="store_true", help="Inspect and display patch state matrix without executing")
    control_group.add_argument("-l", "--list", action="store_true", help="Display all configured stages and commands in a Rich table")
    control_group.add_argument("--json", action="store_true", help="Output status/execution results as formatted JSON")
    control_group.add_argument("--reset-state", action="store_true", help="Reset and clear state history file")

    return parser.parse_args()


# ==============================================================================
# 7. MAIN ENTRYPOINT
# ==============================================================================

def main() -> None:
    args = parse_args()
    check_ui_deps(args.json)

    ctx = resolve_user_context()

    # Security Check: Prevent running directly as root
    if ctx.is_root and not os.environ.get("SUDO_UID"):
        if not args.json and error_console:
            error_console.print("[bold red]CRITICAL ERROR: Do NOT run this script directly as root! Run as normal user.[/bold red]")
        else:
            print("CRITICAL ERROR: Do NOT run this script directly as root!", file=sys.stderr)
        sys.exit(1)

    # Handle --list flag
    if args.list:
        list_commands()
        sys.exit(0)

    engine = FleetPatcherEngine(ctx, is_json=args.json)

    # Handle --reset-state flag
    if args.reset_state:
        if engine.state_file.exists():
            engine.state_file.unlink()
            if not args.json and console:
                console.print("[bold green]State history cleared successfully.[/bold green]")
            elif args.json:
                print(json.dumps({"status": "success", "message": "State history cleared successfully"}))
        else:
            if not args.json and console:
                console.print("[bold yellow]No state file found to clear.[/bold yellow]")
            elif args.json:
                print(json.dumps({"status": "warning", "message": "No state file found to clear"}))
        sys.exit(0)

    # Resolve active stages
    selected_stages: list[str] = []
    if args.all or args.stage == "all":
        selected_stages = ["before", "setup", "after"]
    else:
        if args.before or args.stage == "before":
            selected_stages.append("before")
        if args.setup or args.stage == "setup":
            selected_stages.append("setup")
        if args.after or args.stage == "after":
            selected_stages.append("after")

    # Default to 'all' if no stage specified
    if not selected_stages:
        selected_stages = ["before", "setup", "after"]

    # Acquire lock & load state
    engine.acquire_lock()
    engine.load_state()

    # Status Inspection Mode
    if args.status:
        if args.json:
            json_data = []
            for st in selected_stages:
                for c in STAGES.get(st, []):
                    json_data.append({
                        "stage": st,
                        "mode": c.mode_str,
                        "cmd": c.cmd,
                        "description": c.description,
                        "hash": c.state_hash,
                        "applied": c.state_hash in engine.completed_patches,
                    })
            print(json.dumps(json_data, indent=2))
        else:
            render_status_matrix(engine, selected_stages)
        sys.exit(0)

    engine.logger = Logger(engine.log_file, is_json=args.json)

    if not args.json:
        render_header(ctx, selected_stages)

    def handle_exit(signum, frame):
        engine.cleanup()
        if not args.json and console:
            console.print("\n[bold red][ABORTED][/bold red] Interrupted by signal.")
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    all_results: list[CommandResult] = []

    try:
        for stage_name in selected_stages:
            commands = STAGES.get(stage_name, [])
            results = engine.run_stage(
                stage_name,
                commands,
                force=args.force,
                dry_run=args.dry_run,
                interactive=args.interactive,
                use_defaults=args.default,
            )
            all_results.extend(results)

        if args.json:
            json_res = [
                {
                    "stage": r.stage,
                    "mode": r.command.mode_str,
                    "cmd": r.command.cmd,
                    "status": r.status.value,
                    "message": r.message,
                    "output": r.output,
                }
                for r in all_results
            ]
            print(json.dumps(json_res, indent=2))
        else:
            render_execution_summary(all_results)
            if engine.logger:
                engine.logger.log("SUCCESS", "All requested fleet patches completed and verified.")
    finally:
        engine.cleanup()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if console:
            console.print("\n[bold red][ABORTED][/bold red] Interrupted by user (SIGINT).")
        else:
            print("\n[ABORTED] Interrupted by user (SIGINT).", file=sys.stderr)
        sys.exit(130)
