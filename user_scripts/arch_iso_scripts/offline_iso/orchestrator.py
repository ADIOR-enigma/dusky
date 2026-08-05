#!/usr/bin/env python3
# DUSKY_BOOTSTRAP_PACKAGES: python python-textual python-rich git
# dusky_interactive=true
# ==============================================================================
#  ARCH LINUX ISO TEXTUAL ORCHESTRATOR (v19.0 - Async PTY Engine + Auto-Prompt)
# ==============================================================================
# Architecture: Asynchronous Non-Blocking PTY Stream Engine | Textual Split TUI
# Features: Progress Bar/Speed Extraction | Auto-Prompt Responder | State Persistence
# Compatibility: Python 3.14+ | Textual 8.2+ | Arch Linux ISO (2026+)
# ==============================================================================

VERSION = "19.0.0"

import os
import sys
import subprocess
import time
import fcntl
import hashlib
import tarfile
import shlex
import argparse
import shutil
import asyncio
import pty
import termios
import struct
import functools
import re
import tomllib
import atexit
import datetime
import signal
import json
import sqlite3
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Dict, Optional, Tuple, Any
from contextlib import suppress, contextmanager, nullcontext

try:
    from rich.console import Console
    from rich.text import Text

    from textual.app import App, ComposeResult
    from textual.containers import Container, Horizontal, Vertical
    from textual.widgets import Header, Footer, Static, RichLog, ProgressBar, Button, Label, Input, OptionList
    from textual.widgets.option_list import Option
    from textual.screen import ModalScreen
    from textual import work, on
except ImportError as exc:
    sys.stderr.write(f"[FATAL] Missing Python dependencies: {exc}\n")
    sys.stderr.write("Install: python-textual python-rich\n")
    sys.exit(8)

# ==============================================================================
# CONSTANTS & CONFIGURATION LOAD
# ==============================================================================
VERSION = "19.0.0"
SCRIPT_DIR: Path = Path(__file__).resolve().parent
PROFILES_DIR: Path = Path(
    os.environ.get("DUSKY_PROFILES_DIR", SCRIPT_DIR / "profiles")
).resolve()


def load_global_config() -> dict:
    config_path = PROFILES_DIR / "settings" / "orchestrator.toml"
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            sys.stderr.write(f"[WARN] Failed to parse global config: {e}\n")
    return {}


GLOBAL_CONFIG = load_global_config()

ASCII_MODE = GLOBAL_CONFIG.get("ui", {}).get("ascii_mode", False)
MAX_DEFER_PASSES = GLOBAL_CONFIG.get("execution", {}).get("max_defer_passes", 3)

UNICODE_SYMBOLS = GLOBAL_CONFIG.get(
    "ui",
    {},
).get(
    "unicode_symbols",
    {
        "logo": "◈",
        "completed": "✓",
        "running": "◉",
        "failed": "x",
        "skipped": "⊘",
        "pending": "·",
        "sep": "│",
    },
)

ASCII_SYMBOLS = GLOBAL_CONFIG.get(
    "ui",
    {},
).get(
    "ascii_symbols",
    {
        "logo": "DUSKY",
        "completed": "OK",
        "running": "RUN",
        "failed": "ERR",
        "skipped": "SKIP",
        "pending": "...",
        "sep": "|",
    },
)


def S(key: str) -> str:
    syms = ASCII_SYMBOLS if ASCII_MODE else UNICODE_SYMBOLS
    return syms.get(key, "")


# High-Performance Regexes
ANSI_STRIP_REGEX = re.compile(
    r'\x1B(?:[@-Z\\-_]|\[(?>(?:[0-?]*+)[ -/]*+[@-~])|\](?>\d*;.*?)(?:\x07|\x1B\\)|\]8;;.*?(?:\x07|\x1B\\)|\x1B\(B)'
)
PCT_REGEX = re.compile(r'(?<![0-9])(?>\d{1,2}|100)%')
SPEED_ETA_REGEX = re.compile(r'(\d+(?:\.\d+)?\s+[KMG]?i?B/s)\s+([\d:]+)', re.IGNORECASE)
PROGRESS_BAR_REGEX = re.compile(r'\[[#=\- oO@%:.0123456789━─░▒▓█▏▎▍▌▋▊▉●○◉◌]{3,}\]|\b\d{1,3}%\b')
INTERACTIVE_RE = re.compile(r'^\s*#\s*dusky_interactive\s*=\s*(?:true|1)\b', re.IGNORECASE)


def _build_prompt_rules() -> list[tuple[str, re.Pattern[str], str]]:
    default_rules = [
        ("pgp_import", r"(?i)(::\s*Import PGP key.*\?\s*\[Y/n\]|::\s*Append key\?.*\[Y/n\]|Import PGP key.*\?\s*\[Y/n\])", "y\n"),
        ("pacman_proceed", r"(?i)::\s*(Proceed with (?:installation|download|upgrade)|Continue (?:installation|download|upgrade)).*\?\s*\[Y/n\]", "y\n"),
        ("pacman_replace", r"(?i)::\s*Replace\s+.*\?\s*\[Y/n\]", "y\n"),
        ("pacman_remove_conflict", r"(?i)::\s*Remove conflicting file.*\?\s*\[Y/n\]", "y\n"),
        ("generic_yes", r"(?i)\[Y/n\]|\(Y/n\)", "y\n"),
    ]
    config_rules = GLOBAL_CONFIG.get("prompts", {}).get("rules", None)
    rules = []
    items_to_parse = config_rules if config_rules is not None else default_rules
    for item in items_to_parse:
        if isinstance(item, dict):
            name, pattern, kind = item["name"], item["pattern"], item["kind"]
            resp = "y\n" if kind in ("yes", "y") else f"{kind}\n"
        else:
            name, pattern, resp = item
        rules.append((name, re.compile(pattern, re.MULTILINE), resp))
    return rules


PROMPT_RULES = _build_prompt_rules()
_LOCK_FD: Optional[int] = None

# ==============================================================================
# PATH RESOLUTION HELPERS
# ==============================================================================
def user_home() -> Path:
    return Path.home()


@functools.cache
def documents_root() -> Path:
    docs_dir = GLOBAL_CONFIG.get("paths", {}).get("documents_dir", "Documents")
    p = Path(docs_dir).expanduser()
    root = p if p.is_absolute() else user_home() / p
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"[FATAL] Cannot create Documents root {root}: {e}\n")
        sys.exit(1)
    return root


def _documents_subdir(name: str) -> Path:
    p = Path(name).expanduser()
    path = p if p.is_absolute() else documents_root() / p
    try:
        path.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            path.chmod(0o700)
    except OSError as e:
        sys.stderr.write(f"[FATAL] Cannot create required directory {path}: {e}\n")
        sys.exit(1)
    return path


@functools.cache
def logs_dir() -> Path:
    return _documents_subdir(GLOBAL_CONFIG.get("paths", {}).get("logs_subdir", "logs"))


def now_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso() -> str:
    return datetime.datetime.now().isoformat()


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def resolve_home(path_str: str) -> Path:
    p = Path(os.path.expandvars(path_str.strip())).expanduser()
    if not p.is_absolute():
        p = SCRIPT_DIR / p
    return p


@functools.cache
def state_dir() -> Path:
    return _documents_subdir(GLOBAL_CONFIG.get("paths", {}).get("state_subdir", "state"))


def file_checksum(path: Path) -> str:
    try:
        h = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def make_state_key(task: "OrchestratorTask", occurrence: int) -> str:
    args_key = shlex.join(task.args)
    timeout_repr = "" if task.timeout is None else str(task.timeout)
    material = "|".join(
        [
            task.mode,
            task.script_name,
            args_key,
            str(occurrence),
            task.checksum,
            task.condition or "",
            str(int(task.interactive)),
            str(int(task.ignore_fail)),
            str(int(task.force_flag)),
            timeout_repr,
            str(int(task.always)),
            str(int(task.once)),
            task.once_mode,
            task.once_scope,
        ]
    ).encode("utf-8")
    return hashlib.blake2b(material, digest_size=16).hexdigest()


# ==============================================================================
# NOTIFICATION MANAGER
# ==============================================================================
class NotificationManager:
    audio_enabled: bool = True
    desktop_enabled: bool = True

    @staticmethod
    def play_sound(event_type: str) -> None:
        if not NotificationManager.audio_enabled:
            return
        cfg = GLOBAL_CONFIG.get("notifications", {})
        if not cfg.get("audio_enabled", True):
            return

        players = cfg.get("audio_players", ["pw-play", "paplay"])
        sound_map = cfg.get("sound_map", {})
        fallback = cfg.get("fallback_sound", "/usr/share/sounds/freedesktop/stereo/bell.oga")

        sound_file = sound_map.get(event_type, fallback)
        if not Path(sound_file).exists():
            sound_file = fallback
            if not Path(sound_file).exists():
                return

        player_bin = None
        for p in players:
            if shutil.which(p):
                player_bin = p
                break

        if not player_bin:
            return

        with suppress(Exception):
            subprocess.Popen(
                [player_bin, sound_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    @staticmethod
    def send_desktop(title: str, body: str, urgency: str = "normal") -> None:
        if not NotificationManager.desktop_enabled:
            return
        cfg = GLOBAL_CONFIG.get("notifications", {})
        if not cfg.get("desktop_enabled", True):
            return

        if not shutil.which("notify-send"):
            return

        app_name = cfg.get("app_name", "Dusky Arch ISO Installer")
        with suppress(Exception):
            subprocess.Popen(
                ["notify-send", "-a", app_name, "-u", urgency, title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


# ==============================================================================
# RUN LOGGER
# ==============================================================================
class RunLogger:
    def __init__(self, profile_name: str, run_id: str):
        log_config = GLOBAL_CONFIG.get("logging", {})
        self.enabled = log_config.get("enabled", True)
        self.write_task_logs = log_config.get("write_task_logs", True)
        self.write_reports = log_config.get("write_reports", True)

        self.root: Path | None = None
        self.main_path: Path | None = None
        self._main = None
        self._task_files: dict[str, object] = {}
        self.run_id = run_id

        if not self.enabled:
            return

        try:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.root = logs_dir() / f"{stamp}_{safe_filename(profile_name)}_{run_id}"
            self.root.mkdir(parents=True, exist_ok=True)
            self.main_path = self.root / "orchestrator.log"
            self._main = open(self.main_path, "a", encoding="utf-8", errors="replace")
            self.system(f"Logging started for profile: {profile_name}")
            self.system(f"Run ID: {run_id}")
        except OSError as e:
            sys.stderr.write(f"[WARN] Cannot create log directory under {logs_dir()}: {e}\n")
            self.enabled = False

    def system(self, msg: str) -> None:
        if not self.enabled or self._main is None:
            return
        with suppress(OSError):
            self._main.write(f"[{now_ts()}] {msg}\n")
            self._main.flush()

    def task_log_path(self, task: Any) -> Path:
        if self.root is None:
            return Path("/dev/null")
        return self.root / f"{task.index:03d}_{safe_filename(task.script_name)}.log"

    def open_task(self, task: Any, cmd: list[str]) -> None:
        if not self.enabled or not self.write_task_logs:
            return
        with suppress(OSError):
            f = open(self.task_log_path(task), "a", encoding="utf-8", errors="replace")
            f.write(f"[{now_ts()}] TASK START: {task.script_name}\n")
            f.write(f"[{now_ts()}] MODE: {task.mode}\n")
            f.write(f"[{now_ts()}] PATH: {task.resolved_path}\n")
            f.write(f"[{now_ts()}] INTERPRETER: {task.interpreter or 'direct'}\n")
            f.write(f"[{now_ts()}] ARGS: {shlex.join(task.args)}\n")
            f.write(f"[{now_ts()}] COMMAND: {shlex.join(cmd)}\n")
            f.write(f"[{now_ts()}] CONDITION: {task.condition or 'always'}\n")
            f.flush()
            self._task_files[task.state_key] = f

    def write_task(self, task: Any, line: str) -> None:
        if not self.enabled or not self.write_task_logs:
            return
        f = self._task_files.get(task.state_key)
        if f is None:
            return
        with suppress(OSError):
            f.write(line + "\n")
            f.flush()

    def close_task(self, task: Any, status: str = "", exit_code: int | None = None, duration: float = 0.0) -> None:
        if not self.enabled or not self.write_task_logs:
            return
        f = self._task_files.pop(task.state_key, None)
        if f is None:
            return
        with suppress(OSError):
            f.write(f"\n[{now_ts()}] TASK END: {task.script_name}\n")
            f.write(f"[{now_ts()}] STATUS: {status}\n")
            f.write(f"[{now_ts()}] EXIT CODE: {exit_code}\n")
            f.write(f"[{now_ts()}] DURATION: {duration:.2f}s\n")
            f.flush()
            f.close()

    def write_report(
        self,
        profile_name: str,
        tasks: list[Any],
        statuses: dict[str, str],
        counters: dict[str, int],
    ) -> None:
        if not self.enabled or not self.write_reports or self.root is None:
            return

        report = {
            "run_id": self.run_id,
            "generated": now_iso(),
            "profile": profile_name,
            "version": VERSION,
            "python": sys.version,
            "user": "root" if os.geteuid() == 0 else os.environ.get("USER", "user"),
            "counters": counters,
            "tasks": [],
        }

        lines = [
            "# Dusky ISO Installer Report",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Generated: `{now_iso()}`",
            f"- Profile: `{profile_name}`",
            f"- Version: `{VERSION}`",
            "",
            "## Summary",
            "",
        ]

        for k, v in sorted(counters.items()):
            lines.append(f"- **{k.capitalize()}**: {v}")

        lines.extend(["", "## Task Details", "", "| # | Script | Status | Mode | Condition |", "|---|---|---|---|---|"])

        for t in tasks:
            st = statuses.get(t.state_key, "PENDING")
            report["tasks"].append({
                "index": t.index,
                "script": t.script_name,
                "status": st,
                "mode": t.mode,
                "condition": t.condition or "always",
            })
            lines.append(f"| {t.index} | `{t.script_name}` | {st} | {t.mode} | `{t.condition or 'always'}` |")

        with suppress(OSError):
            (self.root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            (self.root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ==============================================================================
# CONDITION EVALUATOR
# ==============================================================================
class ConditionEvaluator:
    def __init__(self):
        self.cache: dict[str, bool] = {}

    def check(self, cond: str | None) -> bool:
        if not cond or cond.strip().lower() == "always":
            return True
        cond_clean = cond.strip()
        if cond_clean in self.cache:
            return self.cache[cond_clean]

        res = self._eval(cond_clean)
        self.cache[cond_clean] = res
        return res

    def _eval(self, cond: str) -> bool:
        kind, _, value = cond.partition(":")
        kind = kind.strip().lower()
        value = value.strip()

        if kind == "not":
            return not self.check(value)
        if kind == "wayland":
            return bool(os.environ.get("WAYLAND_DISPLAY"))
        if kind == "x11":
            return bool(os.environ.get("DISPLAY"))
        if kind == "command":
            return shutil.which(value) is not None
        if kind == "dir":
            return Path(value).expanduser().is_dir()
        if kind == "file":
            return Path(value).expanduser().is_file()
        if kind == "package":
            pkg_cmd = GLOBAL_CONFIG.get("conditions", {}).get("package_check_cmd", ["pacman", "-Qq"])
            if not pkg_cmd or not shutil.which(pkg_cmd[0]):
                return False
            try:
                return subprocess.run(pkg_cmd + [value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            except Exception:
                return False
        if kind == "gpu":
            vendor_map = GLOBAL_CONFIG.get("conditions", {}).get("gpu_vendor_map", {
                "nvidia": "0x10de", "intel": "0x8086", "amd": "0x1002", "vmware": "0x15ad", "virtio": "0x1af4"
            })
            target = vendor_map.get(value.lower())
            if target:
                drm_path = Path("/sys/class/drm")
                if drm_path.exists():
                    for card in drm_path.glob("card[0-9]*"):
                        vf = card / "device" / "vendor"
                        if vf.exists() and vf.read_text().strip().lower() == target:
                            return True
            return False

        return True


# ==============================================================================
# DATA CLASSES
# ==============================================================================
class TaskStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    SKIPPED = auto()


@dataclass
class OrchestratorTask:
    index: int
    script_name: str
    args: List[str]
    mode: str = "U"
    ignore_fail: bool = False
    interactive: bool = False
    interactive_override: Optional[bool] = None
    force_flag: bool = False
    condition: Optional[str] = None
    timeout: Optional[float] = None
    interpreter: str = "bash"
    checksum: str = ""
    state_key: str = ""
    resolved_path: Optional[Path] = None
    status: TaskStatus = TaskStatus.PENDING
    error_msg: Optional[str] = None
    duration: float = 0.0
    always: bool = False
    retry: int = 0
    retry_delay: float = 1.0
    on_failure: str = "ask"
    once: bool = False
    once_mode: str = "content"
    once_scope: str = "profile"


@dataclass
class ProfileConfig:
    filepath: Optional[Path]
    name: str
    description: str
    phase1_tasks: List[OrchestratorTask]
    phase2_tasks: List[OrchestratorTask]
    search_dirs: List[Path] = field(default_factory=list)
    conflict_resolutions: Dict[str, str] = field(default_factory=dict)
    policy: Dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# PROFILE PARSER & ENGINE
# ==============================================================================
def parse_task_entry(raw_entry: str | dict, index: int = 1) -> OrchestratorTask:
    if isinstance(raw_entry, dict):
        return parse_task_table(raw_entry, index)

    raw = raw_entry.strip()
    parts = [p.strip() for p in raw.split("|", 2)]

    if len(parts) == 1:
        mode, flags, cmd = "U", "", parts[0]
    elif len(parts) == 2:
        mode, cmd = parts
        flags = ""
    elif len(parts) == 3:
        mode, flags, cmd = parts
    else:
        raise ValueError(f"Malformed entry: {raw_entry}")

    ignore_fail = False
    interactive = False
    interactive_override: bool | None = None
    force_flag = False
    always = False
    condition: str | None = None
    timeout: float | None = None
    retry = 0
    retry_delay = 1.0
    on_failure = "ask"
    once = False
    once_mode = "content"
    once_scope = "profile"

    for flag in flags.split(","):
        f = flag.strip().lower()
        if not f:
            continue

        if f in ("true", "ignore", "ignore-fail"):
            ignore_fail = True
        elif f in ("interactive", "tui", "prompt", "fullscreen", "tty", "suspend"):
            interactive = True
            interactive_override = True
        elif f in ("no-interactive", "noninteractive", "inline", "embedded"):
            interactive = False
            interactive_override = False
        elif f in ("force", "--force"):
            force_flag = True
        elif f in ("always", "always_run"):
            always = True
        elif f in ("once", "run_once", "sticky"):
            once = True
        elif f in ("once:content", "once:hash"):
            once = True
            once_mode = "content"
        elif f in ("once:forever", "once:exact", "once:permanent"):
            once = True
            once_mode = "forever"
        elif f in ("once:profile", "once:local"):
            once = True
            once_scope = "profile"
        elif f in ("once:global", "once:machine"):
            once = True
            once_scope = "global"
        elif f.startswith("condition:"):
            cond_val = flag.strip()[10:]
            if condition is None:
                condition = cond_val
            else:
                condition = f"{condition},{cond_val}"
        elif f.startswith("if:"):
            cond_val = flag.strip()[3:]
            if condition is None:
                condition = cond_val
            else:
                condition = f"{condition},{cond_val}"
        elif f.startswith("timeout:"):
            with suppress(ValueError):
                timeout = float(flag.strip()[8:])
        elif f.startswith("retry:"):
            with suppress(ValueError):
                retry = max(0, int(flag.strip()[6:]))
        elif f.startswith("retry_delay:"):
            with suppress(ValueError):
                retry_delay = max(0.0, float(flag.strip()[12:]))
        elif f.startswith("on_failure:"):
            val = flag.strip()[11:].lower()
            if val in ("ask", "abort", "continue", "skip", "manual"):
                on_failure = val

    cmd_tokens = shlex.split(cmd.strip())
    if not cmd_tokens:
        raise ValueError(f"Empty command in entry: {raw_entry}")

    if cmd_tokens[0] == "true" and len(cmd_tokens) > 1:
        ignore_fail = True
        cmd_tokens = cmd_tokens[1:]

    if "--force" in cmd_tokens:
        force_flag = True

    return OrchestratorTask(
        index=index,
        script_name=cmd_tokens[0],
        args=cmd_tokens[1:],
        mode=mode.strip().upper(),
        ignore_fail=ignore_fail,
        interactive=interactive,
        interactive_override=interactive_override,
        force_flag=force_flag,
        condition=condition,
        timeout=timeout,
        always=always,
        retry=retry,
        retry_delay=retry_delay,
        on_failure=on_failure,
        once=once,
        once_mode=once_mode,
        once_scope=once_scope,
    )


def parse_task_table(table: dict, index: int) -> OrchestratorTask:
    cmd = str(table.get("cmd") or table.get("script") or table.get("path") or "").strip()
    if not cmd:
        raise ValueError(f"Task table at index {index} missing cmd/script/path")

    args_raw = table.get("args", [])
    if isinstance(args_raw, str):
        args = shlex.split(args_raw)
    elif isinstance(args_raw, list):
        args = [str(x) for x in args_raw]
    else:
        args = []

    if not args and " " in cmd:
        cmd_tokens = shlex.split(cmd)
        if cmd_tokens:
            cmd = cmd_tokens[0]
            args = cmd_tokens[1:]

    flags = str(table.get("flags", ""))
    ignore_fail = bool(table.get("ignore_fail", False))

    interactive_override: bool | None = None
    if "interactive" in table:
        interactive = bool(table.get("interactive"))
        interactive_override = interactive
    else:
        interactive = False

    force_flag = bool(table.get("force", False))
    always = bool(table.get("always", False))
    condition = table.get("condition")
    timeout = table.get("timeout")

    try:
        retry = max(0, int(table.get("retry", 0)))
    except Exception:
        retry = 0

    try:
        retry_delay = max(0.0, float(table.get("retry_delay", 1.0)))
    except Exception:
        retry_delay = 1.0

    on_failure = str(table.get("on_failure", "ask")).lower()
    if on_failure not in ("ask", "abort", "continue", "skip", "manual"):
        on_failure = "ask"

    once = bool(table.get("once", False))
    once_mode = str(table.get("once_mode", "content")).lower()
    if once_mode not in ("content", "forever"):
        once_mode = "content"

    once_scope = str(table.get("once_scope", "profile")).lower()
    if once_scope not in ("profile", "global"):
        once_scope = "profile"

    for flag in flags.split(","):
        f = flag.strip().lower()
        if not f:
            continue

        if f in ("true", "ignore", "ignore-fail"):
            ignore_fail = True
        elif f in ("interactive", "tui", "prompt", "fullscreen", "tty", "suspend"):
            interactive = True
            interactive_override = True
        elif f in ("no-interactive", "noninteractive", "inline", "embedded"):
            interactive = False
            interactive_override = False
        elif f in ("force", "--force"):
            force_flag = True
        elif f in ("always", "always_run"):
            always = True
        elif f in ("once", "run_once", "sticky"):
            once = True
        elif f in ("once:content", "once:hash"):
            once = True
            once_mode = "content"
        elif f in ("once:forever", "once:exact", "once:permanent"):
            once = True
            once_mode = "forever"
        elif f in ("once:profile", "once:local"):
            once = True
            once_scope = "profile"
        elif f in ("once:global", "once:machine"):
            once = True
            once_scope = "global"
        elif f.startswith("condition:"):
            cond_val = flag.strip()[10:]
            if condition is None:
                condition = cond_val
            else:
                condition = f"{condition},{cond_val}"
        elif f.startswith("if:"):
            cond_val = flag.strip()[3:]
            if condition is None:
                condition = cond_val
            else:
                condition = f"{condition},{cond_val}"
        elif f.startswith("timeout:"):
            with suppress(ValueError):
                timeout = float(flag.strip()[8:])
        elif f.startswith("retry:"):
            with suppress(ValueError):
                retry = max(0, int(flag.strip()[6:]))
        elif f.startswith("retry_delay:"):
            with suppress(ValueError):
                retry_delay = max(0.0, float(flag.strip()[12:]))
        elif f.startswith("on_failure:"):
            val = flag.strip()[11:].lower()
            if val in ("ask", "abort", "continue", "skip", "manual"):
                on_failure = val

    if "--force" in args:
        force_flag = True

    try:
        timeout_value = float(timeout) if timeout is not None else None
    except Exception:
        timeout_value = None

    return OrchestratorTask(
        index=index,
        script_name=cmd,
        args=args,
        mode=str(table.get("mode", "U")).strip().upper(),
        ignore_fail=ignore_fail,
        interactive=interactive,
        interactive_override=interactive_override,
        force_flag=force_flag,
        condition=str(condition).strip() if condition else None,
        timeout=timeout_value,
        always=always,
        retry=retry,
        retry_delay=retry_delay,
        on_failure=on_failure,
        once=once,
        once_mode=once_mode,
        once_scope=once_scope,
    )


def load_profile(filepath: Path) -> ProfileConfig:
    with open(filepath, "rb") as f:
        data = tomllib.load(f)

    p_data = data.get("profile", {})
    ph1_data = data.get("phase1", {})
    ph2_data = data.get("phase2", {})
    s_data = data.get("search_dirs", {})
    cr_data = data.get("conflict_resolutions", {})
    pol_data = data.get("policy", {})

    conflict_resolutions: Dict[str, str] = {}
    for key, val in cr_data.items():
        if isinstance(val, str):
            conflict_resolutions[key] = str(resolve_home(val))
        elif isinstance(val, dict):
            for sub_key, sub_val in val.items():
                if isinstance(sub_val, str):
                    conflict_resolutions[f"{key}.{sub_key}"] = str(resolve_home(sub_val))

    policy: Dict[str, Any] = {}
    for key, val in pol_data.items():
        policy[key] = val

    search_dirs: List[Path] = []
    for d in s_data.get("dirs", []):
        p = Path(str(d)).expanduser()
        if not p.is_absolute():
            p = SCRIPT_DIR / p
        p = p.resolve()
        if not p.exists():
            sys.stderr.write(f"[WARN] Search directory does not exist: {p}\n")
        if p not in search_dirs:
            search_dirs.append(p)

    p1_tasks = []
    for idx, line in enumerate(ph1_data.get("scripts", []), start=1):
        try:
            p1_tasks.append(parse_task_entry(line, index=idx))
        except ValueError as e:
            sys.stderr.write(f"Error parsing profile {filepath.name} [phase1]: {e}\n")
            sys.exit(1)

    p2_tasks = []
    for idx, line in enumerate(ph2_data.get("scripts", []), start=1):
        try:
            p2_tasks.append(parse_task_entry(line, index=idx))
        except ValueError as e:
            sys.stderr.write(f"Error parsing profile {filepath.name} [phase2]: {e}\n")
            sys.exit(1)

    return ProfileConfig(
        filepath=filepath,
        name=p_data.get("name", filepath.stem),
        description=p_data.get("description", ""),
        phase1_tasks=p1_tasks,
        phase2_tasks=p2_tasks,
        search_dirs=search_dirs,
        conflict_resolutions=conflict_resolutions,
        policy=policy,
    )


def discover_profiles() -> List[ProfileConfig]:
    if not PROFILES_DIR.exists():
        return []
    profiles = []
    for f in sorted(PROFILES_DIR.glob("*.toml")):
        if f.parent.name == "settings":
            continue
        try:
            profiles.append(load_profile(f))
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to load profile {f.name}: {e}\n")
    return profiles


def verify_offline_repo_fast(repo_dir: str = "/offline_repo") -> Tuple[bool, str]:
    """
    Fast verification of offline package repository integrity.
    Checks archrepo.db tar metadata, file existence, non-zero file sizes,
    and SHA256 checksums of repository package files.
    Returns (is_valid, reason).
    """
    r_path = Path(repo_dir)
    if not r_path.is_dir():
        return False, f"Offline repository directory '{repo_dir}' does not exist."
    
    db_path = r_path / "archrepo.db"
    if not db_path.is_file():
        return False, f"Repository database '{db_path}' not found."

    expected_pkgs: Dict[str, str] = {}
    try:
        with tarfile.open(db_path, "r:*") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/desc"):
                    f = tar.extractfile(member)
                    if not f:
                        continue
                    lines = f.read().decode('utf-8', errors='ignore').splitlines()
                    filename = None
                    sha256sum = None
                    for i, line in enumerate(lines):
                        if line.strip() == "%FILENAME%" and i + 1 < len(lines):
                            filename = lines[i + 1].strip()
                        elif line.strip() == "%SHA256SUM%" and i + 1 < len(lines):
                            sha256sum = lines[i + 1].strip()
                    if filename and sha256sum:
                        expected_pkgs[filename] = sha256sum
    except Exception as e:
        return False, f"Failed to parse database '{db_path}': {e}"

    if not expected_pkgs:
        return False, "Repository database contains no valid package metadata."

    for filename, expected_sha in expected_pkgs.items():
        pkg_file = r_path / filename
        if not pkg_file.is_file():
            return False, f"Missing offline package: {filename}"
        
        try:
            st = pkg_file.stat()
            if st.st_size == 0:
                return False, f"Corrupted 0-byte package file: {filename}"
        except Exception:
            return False, f"Cannot stat package file: {filename}"

        h = hashlib.sha256()
        try:
            with open(pkg_file, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest().lower() != expected_sha.lower():
                return False, f"SHA256 checksum mismatch on {filename} (corrupted copy/unplug)"
        except Exception as e:
            return False, f"Read error on {filename}: {e}"

    return True, "Offline repository clean and verified."


# ==============================================================================
# ONCE-STORE (SQLITE)
# ==============================================================================
class OnceStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (state_dir() / "once.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        busy_timeout = int(GLOBAL_CONFIG.get("execution", {}).get("db_busy_timeout", 5000))
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute(f"PRAGMA busy_timeout = {max(busy_timeout, 0)}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS once_markers (
                marker_key    TEXT PRIMARY KEY,
                profile       TEXT NOT NULL,
                scope         TEXT NOT NULL DEFAULT 'profile',
                mode          TEXT NOT NULL,
                script_name   TEXT NOT NULL,
                args_key      TEXT NOT NULL,
                resolved_path TEXT NOT NULL,
                checksum      TEXT NOT NULL,
                once_mode     TEXT NOT NULL,
                exit_code     INTEGER,
                run_id        TEXT NOT NULL,
                version       TEXT NOT NULL,
                created       REAL NOT NULL,
                updated       REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_once_script ON once_markers (script_name)"
        )
        self.conn.commit()

    def _make_key(self, scope: str, profile_part: str, mode: str, script_name: str, args_key: str) -> str:
        material = f"once|{scope}|{profile_part}|{mode}|{script_name}|{args_key}"
        return hashlib.blake2b(material.encode("utf-8"), digest_size=16).hexdigest()

    def _scope_value(self, scope: str) -> str:
        return scope if scope in ("profile", "global") else "profile"

    def _profile_part(self, profile_name: str, scope: str) -> str:
        if self._scope_value(scope) == "global":
            return "__global__"
        return profile_name

    def marker_valid(self, task: OrchestratorTask, profile_name: str) -> bool:
        if not task.once:
            return False
        scope = self._scope_value(task.once_scope)
        profile_part = self._profile_part(profile_name, scope)
        args_key = shlex.join(task.args)
        key = self._make_key(scope, profile_part, task.mode, task.script_name, args_key)
        row = self.conn.execute(
            "SELECT mode, once_mode, checksum, resolved_path FROM once_markers WHERE marker_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return False
        db_mode, db_once_mode, db_checksum, db_resolved = row
        if db_mode != task.mode:
            return False
        if task.once_mode == "content":
            current_checksum = task.checksum or file_checksum(task.resolved_path) if task.resolved_path else ""
            if db_checksum and current_checksum and db_checksum != current_checksum:
                return False
        return True

    def mark_success(self, task: OrchestratorTask, profile_name: str, exit_code: int, run_id: str) -> None:
        if not task.once:
            return
        scope = self._scope_value(task.once_scope)
        profile_part = self._profile_part(profile_name, scope)
        args_key = shlex.join(task.args)
        key = self._make_key(scope, profile_part, task.mode, task.script_name, args_key)
        now = time.time()
        checksum = task.checksum or file_checksum(task.resolved_path) if task.resolved_path else ""
        self.conn.execute(
            """
            INSERT INTO once_markers
                (marker_key, profile, scope, mode, script_name, args_key,
                 resolved_path, checksum, once_mode, exit_code, run_id,
                 version, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(marker_key) DO UPDATE SET
                exit_code = excluded.exit_code,
                run_id    = excluded.run_id,
                version   = excluded.version,
                updated   = excluded.updated
            """,
            (
                key, profile_name, scope, task.mode, task.script_name, args_key,
                str(task.resolved_path or ""), checksum, task.once_mode, exit_code, run_id,
                VERSION, now, now,
            ),
        )
        self.conn.commit()

    def forget(self, script_name: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM once_markers WHERE script_name = ?", (script_name,)
        )
        self.conn.commit()
        return cur.rowcount

    def print_list(self, profile_name: Optional[str] = None) -> None:
        rows = self.conn.execute(
            "SELECT profile, mode, script_name, args_key, once_mode, exit_code, version, updated "
            "FROM once_markers ORDER BY updated"
        ).fetchall()
        if not rows:
            print("No once markers recorded.")
            return
        for profile, mode, script_name, args_key, once_mode, exit_code, version, updated in rows:
            when = datetime.datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M")
            args = f" {args_key}" if args_key else ""
            print(f"{when}  [{mode}] {script_name}{args}  ({once_mode}, exit={exit_code}, v{version})")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ==============================================================================
# LOCKING & INTERPRETER RESOLUTION
# ==============================================================================
def parse_args():
    parser = argparse.ArgumentParser(description="Dusky Arch ISO Textual Orchestrator")
    parser.add_argument("--phase1", action="store_true", help="Run Phase 1 (ISO Environment)")
    parser.add_argument("--phase2", action="store_true", help="Run Phase 2 (Chroot Environment)")
    parser.add_argument("--reset", action="store_true", help="Reset execution state for the current phase")
    parser.add_argument("--dry-run", action="store_true", help="Dry run: validate scripts presence and exit")
    parser.add_argument("--force", action="store_true", help="Pass --force flag to subscripts")
    parser.add_argument("--manual", "-m", action="store_true", help="Manual mode: prompt before each script")
    parser.add_argument("--stop-on-fail", action="store_true", help="Halt execution if any script fails")
    parser.add_argument("--auto", action="store_true", help="Non-interactive automatic mode")
    parser.add_argument("--profile", type=str, help="Specify profile TOML to execute")
    parser.add_argument("--list-profiles", action="store_true", help="List all available installer profiles and exit")
    parser.add_argument("--list-scripts", action="store_true", help="List all tasks in the selected profile and exit")
    parser.add_argument("--list-once", action="store_true", help="List recorded once-markers and exit")
    parser.add_argument("--forget-once", type=str, help="Remove recorded once-marker(s) for a script and exit")
    parser.add_argument("--doctor", action="store_true", help="Check orchestrator and profile health and exit")
    parser.add_argument("--explain", action="store_true", help="Explain what would happen for each task and exit")
    parser.add_argument("--task-timeout", type=float, default=None, help="Default per-task timeout in seconds (0 = no timeout)")
    parser.add_argument("--no-audio", action="store_true", help="Disable audio notifications")
    parser.add_argument("--no-notify", action="store_true", help="Disable desktop notifications")
    return parser.parse_args()


def _cleanup_lock(lock_file: Path):
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


def acquire_lock(lock_file: Path) -> bool:
    global _LOCK_FD
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    except Exception as e:
        sys.stderr.write(f"\033[1;31m[ERROR]\033[0m Could not open lock file {lock_file}: {e}\n")
        return False

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FD = fd
        atexit.register(lambda: _cleanup_lock(lock_file))
        return True
    except BlockingIOError:
        sys.stderr.write(f"\033[1;31m[ERROR]\033[0m Another instance is already running on {lock_file}.\n")
        try:
            os.close(fd)
        except OSError:
            pass
        return False


def resolve_interpreter(script_path: Path) -> Tuple[str, bool]:
    is_interactive = False
    first_line = ""
    try:
        with open(script_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line_num in range(20):
                line = f.readline()
                if not line:
                    break
                if line_num == 0:
                    first_line = line.strip()
                if INTERACTIVE_RE.search(line):
                    is_interactive = True
    except Exception:
        pass

    suffix = script_path.suffix.lower()
    ext_map = GLOBAL_CONFIG.get("execution", {}).get("extension_interpreters", {
        ".py": "python3", ".sh": "bash", ".fish": "fish"
    })
    if suffix in ext_map:
        interp = ext_map[suffix]
    elif "python" in first_line:
        interp = "python3"
    else:
        interp = GLOBAL_CONFIG.get("execution", {}).get("default_interpreter", "bash")

    return interp, is_interactive


def resolve_script(
    script_name: str,
    search_dirs: List[Path],
    conflict_resolutions: Optional[Dict[str, str]] = None,
) -> Optional[Path]:
    """Locate a script by name. Order: explicit path (SCRIPT_DIR-relative),
    then conflict_resolutions, then SCRIPT_DIR, then profile search_dirs
    (resolved relative to SCRIPT_DIR or absolute); each is searched
    recursively into subdirectories."""
    if "/" in script_name or "\\" in script_name:
        p = SCRIPT_DIR / script_name
        return p if p.is_file() else None

    if conflict_resolutions:
        resolved = conflict_resolutions.get(script_name)
        if resolved:
            rp = Path(resolved)
            if rp.is_file():
                return rp

    roots: List[Path] = [SCRIPT_DIR]
    for d in search_dirs:
        if d not in roots:
            roots.append(d)

    for root in roots:
        direct = root / script_name
        if direct.is_file():
            return direct
        for sub in root.rglob(script_name):
            if sub.is_file():
                return sub
    return None


# ==============================================================================
# MODAL SCREENS
# ==============================================================================
class FailureModalScreen(ModalScreen):
    def __init__(self, task_name: str, error_msg: str):
        super().__init__()
        self.task_name = task_name
        self.error_msg = error_msg

    def compose(self) -> ComposeResult:
        with Container(id="modal_dialog"):
            yield Label(f"⚠ TASK FAILED: {self.task_name}", id="modal_title")
            yield Static(self.error_msg, id="error_details")
            with Horizontal(id="button_bar"):
                yield Button("Retry [R]", variant="primary", id="btn_retry")
                yield Button("Skip [S]", variant="warning", id="btn_skip")
                yield Button("Quit [Q]", variant="error", id="btn_quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_retry":
            self.dismiss("retry")
        elif event.button.id == "btn_skip":
            self.dismiss("skip")
        elif event.button.id == "btn_quit":
            self.dismiss("quit")

    def on_key(self, event) -> None:
        key = event.key.lower()
        if key == "r":
            self.dismiss("retry")
        elif key == "s":
            self.dismiss("skip")
        elif key == "q":
            self.dismiss("quit")


class ManualModalScreen(ModalScreen):
    def __init__(self, task_name: str):
        super().__init__()
        self.task_name = task_name

    def compose(self) -> ComposeResult:
        with Container(id="manual_dialog"):
            yield Label(f"{S('logo')} MANUAL STEP REQUIRED", id="manual_title")
            yield Static(f"About to execute: [bold white]{self.task_name}[/bold white]\nProceed with execution?", id="manual_details")
            with Horizontal(id="button_bar"):
                yield Button("Proceed [Y]", variant="success", id="btn_yes")
                yield Button("Skip [S]", variant="warning", id="btn_skip")
                yield Button("Quit [Q]", variant="error", id="btn_quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_yes":
            self.dismiss("yes")
        elif event.button.id == "btn_skip":
            self.dismiss("skip")
        elif event.button.id == "btn_quit":
            self.dismiss("quit")

    def on_key(self, event) -> None:
        key = event.key.lower()
        if key in ("y", "enter", "space"):
            self.dismiss("yes")
        elif key == "s":
            self.dismiss("skip")
        elif key == "q":
            self.dismiss("quit")


# ==============================================================================
# MAIN TEXTUAL APP
# ==============================================================================
class DuskyOrchestratorApp(App):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen, RichLog, Vertical, Horizontal, ScrollBar {
        background: #0d1117;
        color: #c9d1d9;
        scrollbar-color: #58a6ff80;
        scrollbar-color-hover: #58a6ff;
        scrollbar-color-active: #58a6ff;
        scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
    }
    Screen {
        layout: vertical;
    }
    #top_header {
        height: 3;
        dock: top;
        background: #161b22;
        color: #58a6ff;
        padding: 0 1;
        layout: vertical;
        border-bottom: solid #30363d;
    }
    #header_title {
        text-style: bold;
        color: #58a6ff;
        width: 100%;
        text-align: center;
    }
    #header_telemetry {
        color: #e3b341;
        text-style: italic;
    }
    #progress_bar {
        margin: 0 1;
        width: 100%;
    }
    ProgressBar > .progress--bar {
        color: #58a6ff;
    }
    #main_content {
        layout: horizontal;
        height: 1fr;
    }
    #left_pane {
        width: 38%;
        border-right: solid #30363d;
        padding: 0 1;
        height: 100%;
        overflow-y: auto;
        background: #0d1117;
    }
    #left_pane:focus {
        background-tint: transparent 0%;
    }
    #right_pane {
        width: 62%;
        height: 100%;
        padding: 0 1;
        background: #0d1117;
    }
    .task_row {
        layout: horizontal;
        height: 1;
    }
    .task_icon { width: 3; text-align: center; }
    .task_mode { width: 5; text-align: center; color: #d29922; }
    .task_mode_sudo { width: 5; text-align: center; color: #f85149; text-style: bold; }
    .task_mode_user { width: 5; text-align: center; color: #3fb950; text-style: bold; }
    .task_name { width: 1fr; color: #c9d1d9; }
    
    RichLog {
        height: 100%;
        border: none;
        background: #0d1117;
        color: #c9d1d9;
        scrollbar-size-vertical: 1;
    }
    #footer {
        dock: bottom;
        height: 1;
        background: #090d16;
        color: #8b949e;
    }

    FailureModalScreen, ManualModalScreen {
        align: center middle;
        background: rgba(0,0,0,0.85);
    }
    #modal_dialog {
        width: 75;
        height: auto;
        border: heavy #f85149;
        background: #161b22;
        padding: 1 2;
    }
    #manual_dialog {
        width: 75;
        height: auto;
        border: heavy #58a6ff;
        background: #161b22;
        padding: 1 2;
    }
    #modal_title {
        text-align: center;
        text-style: bold;
        color: #f85149;
        margin-bottom: 1;
    }
    #manual_title {
        text-align: center;
        text-style: bold;
        color: #58a6ff;
        margin-bottom: 1;
    }
    #error_details {
        color: #d29922;
        margin-bottom: 1;
        max-height: 10;
        overflow-y: auto;
    }
    #button_bar {
        layout: horizontal;
        align: center middle;
        height: 3;
    }
    Button {
        height: 1;
        min-width: 14;
        border: none;
        margin: 0 1;
    }
    """

    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("m", "toggle_manual", "Manual Mode"),
        ("r", "reset_state", "Reset State"),
    ]

    def __init__(
        self,
        tasks: List[OrchestratorTask],
        phase_title: str,
        profile_name: str,
        state_file: Path,
        manual: bool,
        stop_on_fail: bool,
        force: bool,
        task_timeout: float = 0.0,
        once_store: Optional[OnceStore] = None,
    ):
        super().__init__()
        self.tasks = tasks
        self.phase_title = phase_title
        self.profile_name = profile_name
        self.state_file = state_file
        self.manual = manual
        self.stop_on_fail = stop_on_fail
        self.force_flag = force
        self.task_timeout = max(task_timeout or 0.0, 0.0)
        self.once_store = once_store or OnceStore()

        self.current_idx = 0
        self.completed_keys = set()
        self.task_statuses: dict[str, str] = {}
        self.counters = {"completed": 0, "failed": 0, "skipped": 0, "pending": len(tasks)}
        self.conditions = ConditionEvaluator()
        self.run_id = hashlib.md5(f"{time.time()}:{phase_title}".encode()).hexdigest()[:8]
        self.logger = RunLogger(profile_name, self.run_id)

        if self.state_file.exists():
            try:
                self.completed_keys = set(self.state_file.read_text().splitlines())
            except Exception:
                pass

        max_lines = GLOBAL_CONFIG.get("ui", {}).get("max_log_lines", 6000)
        self.log_widget = RichLog(id="syslog", highlight=True, markup=False, max_lines=max_lines)
        self.left_pane = Vertical(id="left_pane")
        self.progress_bar = ProgressBar(total=len(self.tasks), show_eta=False, id="progress_bar")
        self.header_title = Static(
            f"{S('logo')} DUSKY ARCH INSTALLER  [{self.phase_title}]  (Profile: {self.profile_name})",
            id="header_title",
        )
        self.header_telemetry = Static("Status: Ready | Telemetry: Idle", id="header_telemetry")

    def compose(self) -> ComposeResult:
        with Vertical(id="top_header"):
            yield self.header_title
            with Horizontal():
                yield self.header_telemetry
                yield self.progress_bar

        with Horizontal(id="main_content"):
            yield self.left_pane
            with Vertical(id="right_pane"):
                yield self.log_widget

        yield Footer()

    def on_mount(self) -> None:
        self.render_task_list()

        for t in self.tasks:
            if t.state_key in self.completed_keys:
                t.status = TaskStatus.COMPLETED
                self.task_statuses[t.state_key] = "COMPLETED"
                self.counters["completed"] += 1
                self.counters["pending"] -= 1
                self.progress_bar.advance(1)

        self.log_system(f"Started Phase: {self.phase_title}")
        self.log_system(f"Active Profile: {self.profile_name}")
        self.log_system(f"Loaded Cached State: {len(self.completed_keys)} tasks completed")

        self.run_worker(self.run_execution_loop())

    def render_task_list(self):
        self.left_pane.remove_children()
        for i, t in enumerate(self.tasks):
            if t.status == TaskStatus.COMPLETED or t.state_key in self.completed_keys:
                icon = f"[bold #3fb950]{S('completed')}[/]"
            elif not t.resolved_path:
                icon = f"[bold #f85149]![/]"
            elif t.status == TaskStatus.RUNNING:
                icon = f"[bold #58a6ff]{S('running')}[/]"
            elif t.status == TaskStatus.FAILED:
                icon = f"[bold #f85149]{S('failed')}[/]"
            elif t.status == TaskStatus.SKIPPED:
                icon = f"[bold #d29922]{S('skipped')}[/]"
            else:
                icon = f"[#8b949e]{S('pending')}[/]"

            if t.mode == "S":
                mode_cls = "task_mode_sudo"
                mode_str = "SUDO"
            else:
                mode_cls = "task_mode_user"
                mode_str = "USER"

            if t.status == TaskStatus.COMPLETED or t.state_key in self.completed_keys:
                name_style = "bold #3fb950"
            elif t.status == TaskStatus.RUNNING:
                name_style = "bold #58a6ff"
            elif t.status == TaskStatus.FAILED:
                name_style = "bold #f85149"
            elif t.status == TaskStatus.SKIPPED:
                name_style = "dim #d29922"
            else:
                name_style = "dim #8b949e"

            name_text = f"[{name_style}]{t.script_name}[/]"
            row = Horizontal(
                Static(icon, classes="task_icon"),
                Static(mode_str, classes=mode_cls),
                Static(name_text, classes="task_name"),
                classes="task_row",
                id=f"row_{i}",
            )
            self.left_pane.mount(row)

    def update_task_status(self, idx: int, status: TaskStatus):
        self.tasks[idx].status = status
        try:
            row = self.query_one(f"#row_{idx}")
            icon_w = row.children[0]
            name_w = row.children[2]
            t = self.tasks[idx]
            if status == TaskStatus.RUNNING:
                icon_w.update(f"[bold #58a6ff]{S('running')}[/]")
                name_w.update(f"[bold #58a6ff]{t.script_name}[/]")
            elif status == TaskStatus.COMPLETED:
                icon_w.update(f"[bold #3fb950]{S('completed')}[/]")
                name_w.update(f"[bold #3fb950]{t.script_name}[/]")
            elif status == TaskStatus.FAILED:
                icon_w.update(f"[bold #f85149]{S('failed')}[/]")
                name_w.update(f"[bold #f85149]{t.script_name}[/]")
            elif status == TaskStatus.SKIPPED:
                icon_w.update(f"[bold #d29922]{S('skipped')}[/]")
                name_w.update(f"[dim #d29922]{t.script_name}[/]")
        except Exception:
            pass

    def log_system(self, msg: str):
        text_ansi = f"\033[1;36m[SYSTEM]\033[0m {msg}"
        self.log_widget.write(Text.from_ansi(text_ansi))
        self.logger.system(msg)

    def log_task(self, msg: str):
        self.log_widget.write(Text.from_ansi(msg))

    def update_telemetry(self, status_str: str, speed_str: str = ""):
        if speed_str:
            self.header_telemetry.update(f"Status: {status_str} | Speed/ETA: {speed_str}")
        else:
            self.header_telemetry.update(f"Status: {status_str}")

    @contextmanager
    def _suspend_ui(self):
        suspend = getattr(self, "suspend", None)
        if callable(suspend):
            with suppress(Exception):
                with suspend():
                    yield
                return

        driver = getattr(self, "driver", None)
        if driver is not None and hasattr(driver, "stop_application_mode"):
            with suppress(Exception):
                driver.stop_application_mode()

        try:
            yield
        finally:
            if driver is not None and hasattr(driver, "start_application_mode"):
                with suppress(Exception):
                    driver.start_application_mode()

    async def push_screen_wait(self, screen: Any) -> Any:
        future = asyncio.get_running_loop().create_future()

        def _callback(res: Any) -> None:
            if not future.done():
                future.set_result(res)

        self.push_screen(screen, callback=_callback)
        return await future

    async def run_execution_loop(self):
        while self.current_idx < len(self.tasks):
            task = self.tasks[self.current_idx]

            if task.state_key in self.completed_keys:
                self.current_idx += 1
                continue

            if not task.resolved_path:
                await self.handle_missing_task(task)
                return

            if task.once and self.once_store.marker_valid(task, self.profile_name):
                self.log_system(f"Once-marker valid for '{task.script_name}' ({task.once_mode}). Skipping.")
                self.task_skipped(task)
                continue

            if task.condition and not self.conditions.check(task.condition):
                self.log_system(f"Condition '{task.condition}' unfulfilled. Skipping {task.script_name}.")
                self.task_skipped(task)
                continue

            if self.manual:
                res = await self.push_screen_wait(ManualModalScreen(task.script_name))
                if res == "yes":
                    pass
                elif res == "skip":
                    self.task_skipped(task)
                    continue
                else:
                    self.exit(1)
                    return

            await self.execute_task(task)
            return

        self.log_system("All tasks in this phase completed successfully!")
        self.update_telemetry("Finished Phase")
        self.logger.write_report(self.profile_name, self.tasks, self.task_statuses, self.counters)
        NotificationManager.play_sound("complete")
        NotificationManager.send_desktop("Phase Completed", f"Successfully completed {self.phase_title}")
        await asyncio.sleep(1.5)
        self.exit(0)

    async def handle_missing_task(self, task: OrchestratorTask):
        self.update_task_status(self.current_idx, TaskStatus.FAILED)
        self.log_task(f"\033[1;31m[ERROR] Missing script: {task.script_name}\033[0m")
        NotificationManager.play_sound("alert")

        if self.stop_on_fail or task.on_failure == "abort":
            self.log_system("stop-on-fail/abort active. Terminating installer phase.")
            await asyncio.sleep(1.5)
            self.exit(1)
            return
        if task.on_failure == "skip":
            self.task_skipped(task)
            return
        if task.on_failure == "continue":
            self.log_system(f"on_failure=continue active. Proceeding past {task.script_name}.")
            self.current_idx += 1
            self.run_worker(self.run_execution_loop())
            return

        res = await self.push_screen_wait(FailureModalScreen(task.script_name, "Script file not found on disk."))
        if res == "retry":
            self.run_worker(self.run_execution_loop())
        elif res == "skip":
            self.task_skipped(task)
        else:
            self.exit(1)

    async def execute_task(self, task: OrchestratorTask):
        self.update_task_status(self.current_idx, TaskStatus.RUNNING)
        self.log_widget.write(Text.from_ansi(f"\n\033[1;36m>>> PROCESS INITIATED: {task.script_name}\033[0m"))
        self.update_telemetry(f"Running {task.script_name}")

        args = list(task.args)
        if self.force_flag and "--force" not in args:
            args.append("--force")

        cmd = [task.interpreter, str(task.resolved_path)] + args
        timeout = task.timeout if task.timeout is not None else self.task_timeout
        max_attempts = max(1, task.retry + 1)

        for attempt in range(1, max_attempts + 1):
            self.logger.open_task(task, cmd)
            start_t = time.time()
            rc = 1
            error_msg = ""

            try:
                if task.interactive:
                    # INTERACTIVE SUSPENSION: Delegate terminal directly to command with SIGINT protection
                    self.log_system(f"Delegating terminal to interactive process: {task.script_name}")
                    await asyncio.sleep(0.3)

                    try:
                        with self._suspend_ui():
                            rc = (await asyncio.to_thread(subprocess.run, cmd)).returncode
                    except KeyboardInterrupt:
                        rc = 130

                    dur = time.time() - start_t
                    self.log_system(f"TUI Resumed. Script exited with code: {rc}")

                    if rc != 0:
                        error_msg = f"Exit code {rc}"
                else:
                    # NON-INTERACTIVE PTY EXECUTION
                    master_fd, slave_fd = pty.openpty()
                    try:
                        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdin=slave_fd,
                            stdout=slave_fd,
                            stderr=slave_fd,
                            close_fds=True,
                            start_new_session=True,
                        )
                        os.close(slave_fd)

                        loop = asyncio.get_running_loop()
                        buffer = ""

                        try:
                            async with asyncio.timeout(timeout) if timeout and timeout > 0 else nullcontext():
                                while True:
                                    try:
                                        data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                                        if not data:
                                            break
                                        text = data.decode("utf-8", errors="replace")
                                        buffer += text

                                        for p_name, rule_re, p_resp in PROMPT_RULES:
                                            if rule_re.search(text):
                                                try:
                                                    os.write(master_fd, p_resp.encode("utf-8"))
                                                    self.log_system(f"Auto-responded to prompt ({p_name})")
                                                except OSError:
                                                    pass
                                                break

                                        speed_match = SPEED_ETA_REGEX.search(buffer)
                                        pct_match = PCT_REGEX.search(buffer)
                                        if speed_match:
                                            self.update_telemetry(
                                                f"Running {task.script_name}",
                                                f"{speed_match.group(1)} (ETA {speed_match.group(2)})",
                                            )
                                        elif pct_match:
                                            self.update_telemetry(f"Running {task.script_name} ({pct_match.group(0)})")

                                        while "\r" in buffer or "\n" in buffer:
                                            r_idx = buffer.find("\r")
                                            n_idx = buffer.find("\n")
                                            if r_idx != -1 and (n_idx == -1 or r_idx < n_idx):
                                                line, buffer = buffer[:r_idx], buffer[r_idx + 1 :]
                                            else:
                                                line, buffer = buffer[:n_idx], buffer[n_idx + 1 :]

                                            stripped = ANSI_STRIP_REGEX.sub("", line).strip()
                                            if not stripped:
                                                continue
                                            if PROGRESS_BAR_REGEX.search(line) and len(line) < 80 and not ("Error" in line or "ERR" in line):
                                                continue

                                            self.log_task(line + "\n")
                                            self.logger.write_task(task, stripped)

                                    except (OSError, BlockingIOError):
                                        if proc.returncode is not None:
                                            break
                                        await asyncio.sleep(0.05)

                            if buffer:
                                stripped = ANSI_STRIP_REGEX.sub("", buffer).strip()
                                if stripped and not PROGRESS_BAR_REGEX.search(buffer):
                                    self.log_task(stripped + "\n")
                                    self.logger.write_task(task, stripped)
                        except TimeoutError:
                            error_msg = f"Timeout after {timeout:.0f}s"
                            try:
                                proc.kill()
                            except ProcessLookupError:
                                pass
                            await proc.wait()
                        else:
                            rc = await proc.wait()
                    finally:
                        try:
                            os.close(master_fd)
                        except OSError:
                            pass

                    dur = time.time() - start_t
                    if rc != 0 and not error_msg:
                        error_msg = f"Process exited with status code {rc}"
            except Exception as e:
                dur = time.time() - start_t
                error_msg = str(e)

            if rc == 0:
                self.logger.close_task(task, status="COMPLETED", exit_code=0, duration=dur)
                if task.once:
                    self.once_store.mark_success(task, self.profile_name, 0, self.run_id)
                await self.task_success(task, dur)
                return

            self.logger.close_task(task, status="FAILED", exit_code=rc or 1, duration=dur)

            if task.ignore_fail:
                self.log_system(f"Task exited with status {rc} but ignore_fail is active. Proceeding.")
                if task.once:
                    self.once_store.mark_success(task, self.profile_name, 0, self.run_id)
                await self.task_success(task, dur)
                return

            if attempt < max_attempts:
                self.log_system(f"Attempt {attempt}/{max_attempts} failed. Retrying in {task.retry_delay}s...")
                NotificationManager.play_sound("alert")
                await asyncio.sleep(task.retry_delay)
                continue

            await self.task_failure(task, error_msg or f"Process exited with status code {rc}", dur)
            return

    async def task_success(self, task: OrchestratorTask, duration: float = 0.0):
        self.update_task_status(self.current_idx, TaskStatus.COMPLETED)
        self.log_task("\n\033[1;32m>>> EXECUTION SUCCESSFUL\033[0m")
        self.completed_keys.add(task.state_key)
        self.task_statuses[task.state_key] = "COMPLETED"
        self.counters["completed"] += 1
        if self.counters["pending"] > 0:
            self.counters["pending"] -= 1
        self.logger.close_task(task, status="COMPLETED", exit_code=0, duration=duration)

        try:
            with open(self.state_file, "a") as f:
                f.write(task.state_key + "\n")
        except Exception as e:
            self.log_system(f"Failed to record state: {e}")

        self.progress_bar.advance(1)
        self.current_idx += 1
        self.run_worker(self.run_execution_loop())

    def task_skipped(self, task: OrchestratorTask):
        self.update_task_status(self.current_idx, TaskStatus.SKIPPED)
        self.log_system(f"Skipped task: {task.script_name}")
        self.task_statuses[task.state_key] = "SKIPPED"
        self.counters["skipped"] += 1
        if self.counters["pending"] > 0:
            self.counters["pending"] -= 1
        self.logger.close_task(task, status="SKIPPED")
        self.progress_bar.advance(1)
        self.current_idx += 1
        self.run_worker(self.run_execution_loop())

    async def task_failure(self, task: OrchestratorTask, reason: str, duration: float = 0.0):
        self.update_task_status(self.current_idx, TaskStatus.FAILED)
        self.log_task(f"\n\033[1;31m>>> EXECUTION FAILED: {reason}\033[0m")
        self.task_statuses[task.state_key] = "FAILED"
        self.counters["failed"] += 1
        if self.counters["pending"] > 0:
            self.counters["pending"] -= 1
        self.logger.close_task(task, status="FAILED", exit_code=1, duration=duration)
        NotificationManager.play_sound("alert")
        NotificationManager.send_desktop("Task Failed", f"Script '{task.script_name}' failed: {reason}", urgency="critical")

        if self.stop_on_fail or task.on_failure == "abort":
            self.log_system("stop-on-fail/abort active. Terminating installer phase.")
            await asyncio.sleep(1.5)
            self.exit(1)
        elif task.on_failure == "skip":
            self.task_skipped(task)
        elif task.on_failure == "continue":
            self.log_system(f"on_failure=continue active. Proceeding past {task.script_name}.")
            self.current_idx += 1
            self.run_worker(self.run_execution_loop())
        else:
            res = await self.push_screen_wait(FailureModalScreen(task.script_name, reason))
            if res == "retry":
                self.run_worker(self.run_execution_loop())
            elif res == "skip":
                self.task_skipped(task)
            else:
                self.exit(1)

    def action_quit_app(self):
        self.exit(1)

    def action_toggle_manual(self):
        self.manual = not self.manual
        mode = "ENABLED" if self.manual else "DISABLED"
        self.log_system(f"Manual step confirmation mode {mode}")

    def action_reset_state(self):
        if self.state_file.exists():
            try:
                self.state_file.unlink()
                self.completed_keys.clear()
                self.log_system("Phase completion state reset.")
            except Exception as e:
                self.log_system(f"Failed to reset state: {e}")


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
def main():
    args = parse_args()

    if args.list_profiles:
        print("Available Installer Profiles:")
        profiles = discover_profiles()
        if not profiles:
            print("  (No profiles found)")
        for p in profiles:
            pname = p.filepath.name if p.filepath else "Unknown"
            print(f"  - {pname}: {p.name} ({p.description})")
            print(f"    Phase 1 tasks: {len(p.phase1_tasks)}, Phase 2 tasks: {len(p.phase2_tasks)}")
        sys.exit(0)

    phase1 = args.phase1
    phase2 = args.phase2

    if not phase1 and not phase2:
        phase1 = True

    profiles = discover_profiles()
    selected_profile: Optional[ProfileConfig] = None

    if args.profile:
        for p in profiles:
            if p.filepath and (p.filepath.name == args.profile or p.name.lower() == args.profile.lower()):
                selected_profile = p
                break
        if not selected_profile:
            p_path = Path(args.profile)
            if p_path.exists():
                try:
                    selected_profile = load_profile(p_path)
                except Exception as e:
                    sys.stderr.write(f"Error loading profile '{args.profile}': {e}\n")
                    sys.exit(1)

    repo_valid, repo_reason = verify_offline_repo_fast("/offline_repo")

    if not selected_profile:
        if args.auto or not sys.stdin.isatty():
            if repo_valid:
                for p in profiles:
                    if p.filepath and "offline" in p.name.lower():
                        selected_profile = p
                        break
            else:
                sys.stderr.write(f"\n[WARN] Offline repository verification failed: {repo_reason}\n")
                sys.stderr.write("[WARN] Auto-selecting ONLINE profile to prevent pacstrap failures.\n\n")
                for p in profiles:
                    if p.filepath and "online" in p.name.lower():
                        selected_profile = p
                        break
            if not selected_profile and profiles:
                selected_profile = profiles[0]
        else:
            from rich.panel import Panel
            from rich.console import Console
            from rich.prompt import Prompt
            console = Console()

            console.print("\n")
            console.print(Panel("[bold cyan]Dusky Arch Installer Profile Selection[/bold cyan]", box=box.ROUNDED))

            profile_choices = []
            for i, p in enumerate(profiles, start=1):
                p_name = p.name
                p_desc = p.description
                is_offline = "offline" in p_name.lower() or (p.filepath and "offline" in p.filepath.name.lower())

                if is_offline and not repo_valid:
                    status_str = f"[bold red][CORRUPTED - UNAVAILABLE][/bold red]"
                    available = False
                elif is_offline:
                    status_str = "[bold green][VERIFIED CLEAN][/bold green]"
                    available = True
                else:
                    status_str = "[bold green][AVAILABLE][/bold green]"
                    available = True

                console.print(f"  [bold yellow]{i}.[/bold yellow] [bold white]{p_name}[/bold white] — [dim]{p_desc}[/dim] {status_str}")
                profile_choices.append((p, available))

            if not repo_valid:
                console.print(Panel(f"[bold red]OFFLINE REPO CORRUPTION DETECTED:[/bold red]\n{repo_reason}\n"
                                    "[yellow]Offline installation disabled. Please select Online Profile or re-copy ISO cleanly.[/yellow]", box=box.ROUNDED))

            default_idx = "2" if (not repo_valid and len(profiles) >= 2) else "1"
            while True:
                choice = Prompt.ask("\nSelect Profile Number", choices=[str(i) for i in range(1, len(profiles) + 1)], default=default_idx)
                idx = int(choice) - 1
                p, avail = profile_choices[idx]
                if not avail:
                    console.print(f"[red]Profile '{p.name}' is unavailable because the offline repository is corrupted. Please choose another option.[/red]")
                    continue
                selected_profile = p
                break

    if not selected_profile:
        sys.stderr.write(f"Error: No valid installer profile found in '{PROFILES_DIR}'. Installation aborted.\n")
        sys.exit(1)

    profile_name = selected_profile.name
    policy = selected_profile.policy

    if args.no_audio:
        NotificationManager.audio_enabled = False
    elif policy.get("audio", True) is False:
        NotificationManager.audio_enabled = False

    if args.no_notify:
        NotificationManager.desktop_enabled = False
    elif policy.get("notify", True) is False:
        NotificationManager.desktop_enabled = False

    manual = args.manual or bool(policy.get("manual", False))
    stop_on_fail = args.stop_on_fail or bool(policy.get("stop_on_fail", False))
    force = args.force or bool(policy.get("force", False))
    task_timeout = args.task_timeout if args.task_timeout is not None else float(policy.get("task_timeout", 0.0))

    raw_sequence = selected_profile.phase1_tasks if phase1 else selected_profile.phase2_tasks

    tasks: List[OrchestratorTask] = []
    occurrence: Dict[str, int] = {}
    for i, t in enumerate(raw_sequence, start=1):
        resolved_path = resolve_script(t.script_name, selected_profile.search_dirs, selected_profile.conflict_resolutions)

        interpreter = t.interpreter
        is_interactive = t.interactive
        if resolved_path:
            interpreter, file_interactive = resolve_interpreter(resolved_path)
            if file_interactive:
                is_interactive = True

        args_key = shlex.join(t.args)
        occ_key = f"{t.mode}|{t.script_name}|{args_key}"
        occurrence[occ_key] = occurrence.get(occ_key, 0) + 1
        checksum = file_checksum(resolved_path) if resolved_path else ""

        task = OrchestratorTask(
            index=i,
            script_name=t.script_name,
            args=t.args,
            mode=t.mode,
            ignore_fail=t.ignore_fail,
            interactive=is_interactive,
            interactive_override=t.interactive_override,
            force_flag=force or t.force_flag,
            condition=t.condition,
            timeout=t.timeout,
            interpreter=interpreter,
            checksum=checksum,
            resolved_path=resolved_path,
            always=t.always,
            retry=t.retry,
            retry_delay=t.retry_delay,
            on_failure=t.on_failure,
            once=t.once,
            once_mode=t.once_mode,
            once_scope=t.once_scope,
        )
        task.state_key = make_state_key(task, occurrence[occ_key])
        tasks.append(task)

    once_store = OnceStore()

    if phase2:
        phase_title = "PHASE 2: CHROOT"
        state_file = Path("/root/.arch_install_phase2.state")
        lock_file = Path("/tmp/orchestrator_phase2.lock")
    else:
        phase_title = "PHASE 1: ISO"
        state_file = Path("/tmp/.arch_install_phase1.state")
        lock_file = Path("/tmp/orchestrator_phase1.lock")

    if args.list_scripts:
        print(f"Profile: {profile_name} ({phase_title if phase1 or phase2 else 'all'})")
        for t in tasks:
            flags = []
            if t.ignore_fail:
                flags.append("IGNORE_FAIL")
            if t.interactive:
                flags.append("INTERACTIVE")
            if t.condition:
                flags.append(f"COND: {t.condition}")
            if t.timeout is not None:
                flags.append(f"TIMEOUT: {t.timeout}s")
            if t.retry:
                flags.append(f"RETRY: {t.retry}")
            if t.once:
                flags.append("ONCE")
            path = str(t.resolved_path) if t.resolved_path else "MISSING"
            print(f"  {t.index:2d}. [{t.mode}] {t.script_name} {' '.join(t.args)} ({', '.join(flags)}) -> {path}")
        once_store.close()
        sys.exit(0)

    if args.list_once:
        once_store.print_list()
        once_store.close()
        sys.exit(0)

    if args.forget_once:
        count = once_store.forget(args.forget_once)
        print(f"Forgot {count} once-marker(s) for '{args.forget_once}'.")
        once_store.close()
        sys.exit(0)

    if args.doctor:
        print(f"VERSION: {VERSION}")
        print(f"SCRIPT_DIR: {SCRIPT_DIR}")
        print(f"PROFILES_DIR: {PROFILES_DIR}")
        print(f"State DB: {once_store.db_path}")
        print(f"Root check: {'OK' if os.geteuid() == 0 else 'NOT ROOT (expected for real run)'}")
        profiles = discover_profiles()
        print(f"Profiles: {len(profiles)}")
        for p in profiles:
            ph1 = len(p.phase1_tasks)
            ph2 = len(p.phase2_tasks)
            print(f"  - {p.filepath.name}: {p.name} (phase1={ph1}, phase2={ph2})")
        missing_all = [t.script_name for t in tasks if not t.resolved_path]
        print(f"Selected profile '{profile_name}': {len(tasks)} tasks, {len(missing_all)} missing")
        print("Doctor check complete.")
        once_store.close()
        sys.exit(0)

    if args.explain:
        print(f"=== EXPLAIN FOR {profile_name} ===")
        for t in tasks:
            reasons = []
            if not t.resolved_path:
                reasons.append("MISSING SCRIPT -> WILL DEFER")
            else:
                if t.once and once_store.marker_valid(t, profile_name):
                    reasons.append("SKIP (once-marker valid)")
                if t.condition:
                    reasons.append(f"condition: {t.condition}")
                if t.always:
                    reasons.append("always")
                if t.once:
                    reasons.append("once")
                if t.ignore_fail:
                    reasons.append("ignore-fail")
                if not reasons:
                    reasons.append("RUN")
            print(f"  {t.index:2d}. [{t.mode}] {t.script_name} {' '.join(t.args)}")
            print(f"       {', '.join(reasons)}")
            if t.timeout is not None:
                print(f"       timeout: {t.timeout}s")
            if t.retry:
                print(f"       retry: {t.retry} (delay {t.retry_delay}s)")
            if t.on_failure != "ask":
                print(f"       on_failure: {t.on_failure}")
        once_store.close()
        sys.exit(0)

    if args.dry_run:
        print(f"=== DRY RUN FOR {phase_title} ===")
        print(f"Active Profile: {profile_name}")
        print(f"State file: {state_file}")
        for i, t in enumerate(tasks):
            status = "PENDING"
            if not t.resolved_path:
                status = "MISSING"
            elif t.once and once_store.marker_valid(t, profile_name):
                status = "SKIP (once-marker valid)"
            print(
                f"  {i+1:2d}. {t.script_name} {' '.join(t.args)} [{'IGNORE_FAIL' if t.ignore_fail else 'STRICT'}] [{'INTERACTIVE' if t.interactive else 'NON-INT'}] -> {status} (using {t.interpreter})"
            )
        once_store.close()
        sys.exit(0)

    if args.reset:
        if state_file.exists():
            try:
                state_file.unlink()
                print(f"Reset completion state for {phase_title}")
            except Exception as e:
                sys.stderr.write(f"Failed to reset state: {e}\n")
        else:
            print(f"No state file found for {phase_title}")

    if not acquire_lock(lock_file):
        once_store.close()
        sys.exit(1)

    if os.geteuid() != 0:
        sys.stderr.write("Error: This installer orchestrator must be run as root.\n")
        once_store.close()
        sys.exit(1)

    missing = [t.script_name for t in tasks if not t.resolved_path]
    if missing:
        sys.stderr.write(f"Error: Missing critical script files in {SCRIPT_DIR}:\n")
        for m in missing:
            sys.stderr.write(f"  - {m}\n")
        once_store.close()
        sys.exit(1)

    app = DuskyOrchestratorApp(
        tasks=tasks,
        phase_title=phase_title,
        profile_name=profile_name,
        state_file=state_file,
        manual=manual,
        stop_on_fail=stop_on_fail,
        force=force,
        task_timeout=task_timeout,
        once_store=once_store,
    )
    app.run()


if __name__ == "__main__":
    main()
