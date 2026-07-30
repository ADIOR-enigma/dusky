#!/usr/bin/env python3
# ==============================================================================
#  DUSKY UPDATER (v9.5.0) — BLEEDING EDGE ARCH / PYTHON 3.14 TUI
# ==============================================================================
import argparse
import asyncio
import datetime
import fcntl
import importlib
import importlib.util
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import site
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import deque
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

# --- Enforcement: Bleeding-Edge Python 3.14+ ---
if sys.version_info < (3, 14):
    sys.stdout.write("\033[1;31m[FATAL]\033[0m Dusky requires Python 3.14+ bleeding-edge architecture.\n")
    sys.exit(1)

VERSION = "9.5.0"
SCRIPT_DIR: Path = Path(__file__).resolve().parent
PROFILES_DIR: Path = Path(
    os.environ.get("DUSKY_UPDATER_PROFILES_DIR", SCRIPT_DIR / "profiles")
).resolve()


def load_global_config() -> dict:
    custom_path = os.environ.get("DUSKY_UPDATER_SETTINGS")
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.is_file():
            try:
                with open(p, "rb") as f:
                    return tomllib.load(f)
            except Exception as e:
                sys.stderr.write(f"[WARN] Failed to parse custom config ({p}): {e}\n")

    config_path = PROFILES_DIR / "settings" / "update_dusky.toml"
    if config_path.is_file():
        try:
            with open(config_path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            sys.stderr.write(f"[WARN] Failed to parse global config ({config_path}): {e}\n")
    return {}


GLOBAL_CONFIG = load_global_config()

ASCII_MODE = GLOBAL_CONFIG.get("ui", {}).get("ascii_mode", False)
DISK_MIN_FREE_MB = GLOBAL_CONFIG.get("execution", {}).get("disk_min_free_mb", 100)
DISK_COPY_RESERVE_MB = GLOBAL_CONFIG.get("execution", {}).get("disk_copy_reserve_mb", 64)
NAMESPACE = GLOBAL_CONFIG.get("paths", {}).get("namespace", "dusky-updater")


# ==============================================================================
#  PATH RESOLUTION UTILITIES
# ==============================================================================
def user_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        with suppress(Exception):
            return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def documents_root() -> Path:
    raw = GLOBAL_CONFIG.get("paths", {}).get("documents_dir", "Documents")
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return user_home() / p


def _documents_subdir(key: str, default: str) -> Path:
    raw = GLOBAL_CONFIG.get("paths", {}).get(key, default)
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return documents_root() / p


def logs_dir() -> Path:
    return _documents_subdir("logs_subdir", "logs")


def backups_dir() -> Path:
    return _documents_subdir("backups_subdir", "dusky_backups")


def runtime_dir() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidate = Path(xdg_runtime) / NAMESPACE
        if ensure_secure_dir(candidate):
            return candidate
    candidate = Path(f"/tmp/{NAMESPACE}-{os.getuid()}")
    ensure_secure_dir(candidate)
    return candidate


def lock_path() -> Path:
    lock_file = GLOBAL_CONFIG.get("paths", {}).get("lock_file", "lock")
    return runtime_dir() / lock_file


# ==============================================================================
#  PRE-FLIGHT BOOTSTRAP & DEPENDENCY RESOLUTION
# ==============================================================================
def verify_sudo() -> bool:
    if not shutil.which("sudo"):
        sys.stdout.write("\033[1;31m[FATAL]\033[0m sudo is required by UPDATE_SEQUENCE but is not installed or not in PATH.\n")
        return False

    sys.stdout.write("\033[1;36m[DUSKY PRE-FLIGHT]\033[0m Securing administrative kernel privileges...\n")

    try:
        rc = subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode
        if rc == 0:
            return True
    except Exception:
        pass

    if sys.stdin.isatty():
        try:
            subprocess.run(['sudo', '-v'], check=True)
            return True
        except subprocess.CalledProcessError:
            pass

    sys.stdout.write("\033[1;31m[FATAL]\033[0m Sudo authentication failed. Aborting.\n")
    return False


def bootstrap_dependencies() -> bool:
    missing = [
        pkg for mod, pkg in [("textual", "python-textual"), ("rich", "python-rich")]
        if importlib.util.find_spec(mod) is None
    ]
    if missing:
        sys.stdout.write(f"\033[1;33m[DUSKY BOOTSTRAP]\033[0m Resolving dependencies: {', '.join(missing)}\n")
        if not verify_sudo():
            sys.exit(1)
        try:
            subprocess.run(['sudo', 'pacman', '-S', '--noconfirm'] + missing, check=True)
            importlib.invalidate_caches()
            importlib.reload(site)
        except subprocess.CalledProcessError:
            sys.stdout.write("\033[1;31m[FATAL]\033[0m Dependency resolution failed.\n")
            sys.exit(1)
        return True
    return False


SUDO_ALREADY_ACQUIRED = bootstrap_dependencies()

try:
    from rich.markup import escape
    from rich.syntax import Syntax
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.reactive import reactive
    from textual.widgets import ContentSwitcher, Label, ListItem, ListView, ProgressBar, RichLog, Static
except ImportError:
    sys.stdout.write("\033[1;31m[FATAL]\033[0m UI library import failed post-resolution. Ensure Arch mirrors are synced.\n")
    sys.exit(1)


# ==============================================================================
#  CLI ARGUMENT PARSING & CONFIGURATION
# ==============================================================================
OPT_DRY_RUN = False
OPT_SKIP_SYNC = False
OPT_SYNC_ONLY = False
OPT_FORCE = False
OPT_STOP_ON_FAIL = False
OPT_ALLOW_DIVERGED_RESET = False
OPT_POST_SELF_UPDATE = False
OPT_PROFILE_NAME = "01_update_default"


def show_help():
    help_text = f"""Dusky Updater v{VERSION} — Dotfile sync and setup tool for Arch Linux / Hyprland

Usage: update_dusky.py [OPTIONS]

Options:
  --help, -h               Show this help message and exit
  --version                Show version and exit
  --profile NAME           Specify custom profile TOML (default: 01_update_default)
  --dry-run                Preview actions without making changes
  --skip-sync              Skip git sync, only run the script sequence
  --sync-only              Pull updates but do not run scripts
  --force                  Skip confirmation prompts
  --stop-on-fail           Abort script execution on first hard failure
  --allow-diverged-reset   In non-interactive mode, allow reset on diverged or unrelated history
  --list                   List all active scripts in the update sequence
  --doctor                 Run system diagnostics check and exit

Update sequence entry formats:
  U | script.sh --auto
  S | ignore-fail | script.sh --auto
  U | | script.sh --auto

Field 1:
  U = run as user
  S = run with sudo

Field 2:
  Optional flags. Supported values: ignore-fail, interactive

Logs are saved to:
  {logs_dir()}

Backups are saved to:
  {backups_dir()}
"""
    sys.stdout.write(help_text)
    sys.exit(0)


def show_version():
    sys.stdout.write(f"Dusky Updater v{VERSION}\n")
    sys.exit(0)


def run_doctor():
    sys.stdout.write("Dusky Updater Doctor\n=====================\n")
    sys.stdout.write(f"Version:        {VERSION}\n")
    sys.stdout.write(f"Python:         {sys.version.split()[0]}\n")
    sys.stdout.write(f"Executable:     {sys.executable}\n")
    sys.stdout.write(f"UID/EUID:       {os.getuid()}/{os.geteuid()}\n")
    sys.stdout.write(f"User:           {user_home().name}\n")
    sys.stdout.write(f"Home:           {user_home()}\n")
    sys.stdout.write(f"Logs dir:       {logs_dir()}\n")
    sys.stdout.write(f"Backups dir:    {backups_dir()}\n")
    sys.stdout.write(f"Runtime dir:    {runtime_dir()}\n")
    sys.stdout.write(f"Profiles dir:   {PROFILES_DIR}\n")

    profiles = list_profiles()
    sys.stdout.write(f"Profiles found: {len(profiles)}\n")
    for p in profiles:
        sys.stdout.write(f"  - {p.name}\n")
    sys.exit(0)


def list_active_scripts(profile: 'ProfileConfig'):
    user_tasks = [t for t in profile.tasks if t.mode != 'GIT']
    sys.stdout.write(f"Active scripts in profile '{profile.name}':\n\n")
    for i, task in enumerate(user_tasks):
        display_mode = task.mode
        if task.ignore_fail:
            display_mode += ",ignore"
        cmd_str = f"{task.name} {' '.join(task.args)}".strip()
        sys.stdout.write(f"  {i+1:3d}) [{display_mode}] {cmd_str}\n")
    sys.stdout.write(f"\nTotal: {len(user_tasks)} active script(s)\n")
    sys.exit(0)


def parse_args():
    global OPT_DRY_RUN, OPT_SKIP_SYNC, OPT_SYNC_ONLY, OPT_FORCE
    global OPT_STOP_ON_FAIL, OPT_ALLOW_DIVERGED_RESET, OPT_POST_SELF_UPDATE, OPT_PROFILE_NAME

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--help', '-h', action='store_true')
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--doctor', action='store_true')
    parser.add_argument('--profile', type=str, default="01_update_default")
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--skip-sync', action='store_true')
    parser.add_argument('--sync-only', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--stop-on-fail', action='store_true')
    parser.add_argument('--allow-diverged-reset', action='store_true')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--post-self-update', action='store_true')

    args, unknown = parser.parse_known_args()

    if unknown:
        sys.stderr.write(f"Error: Unknown option {unknown[0]}\n")
        show_help()

    if args.help:
        show_help()
    if args.version:
        show_version()
    if args.doctor:
        run_doctor()

    OPT_PROFILE_NAME = args.profile
    OPT_DRY_RUN = args.dry_run
    OPT_SKIP_SYNC = args.skip_sync
    OPT_SYNC_ONLY = args.sync_only
    OPT_FORCE = args.force
    OPT_STOP_ON_FAIL = args.stop_on_fail
    OPT_ALLOW_DIVERGED_RESET = args.allow_diverged_reset
    OPT_POST_SELF_UPDATE = args.post_self_update

    return args


# ==============================================================================
#  PROFILE LOADING ENGINE
# ==============================================================================
@dataclass
class ProfileConfig:
    name: str
    description: str
    filepath: Path
    repo_url: str
    branch: str
    search_dirs: list[str]
    conflict_resolutions: dict[str, str]
    sequence: list[str]
    tasks: list['DuskyTask'] = field(default_factory=list)


def list_profiles() -> list[Path]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted([p for p in PROFILES_DIR.glob("*.toml") if p.is_file()])


def load_profile(name_or_path: str) -> ProfileConfig:
    p = Path(name_or_path).expanduser()
    if not p.is_file():
        if not name_or_path.endswith(".toml"):
            p = PROFILES_DIR / f"{name_or_path}.toml"
        else:
            p = PROFILES_DIR / name_or_path

    if not p.is_file():
        available = list_profiles()
        if available:
            p = available[0]
        else:
            sys.stderr.write(f"[FATAL] Profile not found: {name_or_path}\n")
            sys.exit(1)

    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)

        prof_meta = data.get("profile", {})
        git_cfg = data.get("git", {})
        seq_cfg = data.get("sequence", {})

        return ProfileConfig(
            name=prof_meta.get("name", p.stem),
            description=prof_meta.get("description", ""),
            filepath=p,
            repo_url=git_cfg.get("repo_url", GLOBAL_CONFIG.get("git", {}).get("repo_url", "https://github.com/dusklinux/dusky")),
            branch=git_cfg.get("branch", GLOBAL_CONFIG.get("git", {}).get("branch", "main")),
            search_dirs=git_cfg.get("search_dirs", [
                "user_scripts/arch_setup_scripts/scripts",
                "user_scripts/arch_setup_scripts",
                "user_scripts/networking",
                "user_scripts/misc_extra",
                "user_scripts/update_dusky",
                "user_scripts/services",
            ]),
            conflict_resolutions=data.get("conflict_resolutions", {}),
            sequence=seq_cfg.get("tasks", []),
        )
    except Exception as e:
        sys.stderr.write(f"[FATAL] Failed to load profile '{p}': {e}\n")
        sys.exit(1)


# ==============================================================================
#  STORAGE, LOGGING & LOCKING UTILITIES
# ==============================================================================
ACTIVE_LOG_BASE_DIR = None
ACTIVE_BACKUP_BASE_DIR = None
RUNTIME_DIR = None
LOCK_FILE = None
LOCK_FD = None
LOG_FILE = None
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

CLR_RED = "\033[1;31m"
CLR_GRN = "\033[1;32m"
CLR_YLW = "\033[1;33m"
CLR_BLU = "\033[1;34m"
CLR_CYN = "\033[1;36m"
CLR_RST = "\033[0m"


def strip_ansi(text: str) -> str:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def log(level: str, msg: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{level}]"

    if level == "INFO":
        prefix = f"{CLR_BLU}[INFO ]{CLR_RST}"
    elif level == "OK":
        prefix = f"{CLR_GRN}[OK   ]{CLR_RST}"
    elif level == "WARN":
        prefix = f"{CLR_YLW}[WARN ]{CLR_RST}"
    elif level == "ERROR":
        prefix = f"{CLR_RED}[ERROR]{CLR_RST}"
    elif level == "SECTION":
        prefix = f"\n{CLR_CYN}═══════{CLR_RST}"

    if 'app' in globals() and globals()['app'] is not None and getattr(globals()['app'], '_running', False):
        with suppress(Exception):
            globals()['app'].log_main(msg)
    else:
        if level == "SECTION":
            sys.stdout.write(f"{prefix} {msg}\n")
        elif level == "RAW":
            sys.stdout.write(f"{msg}\n")
        else:
            sys.stdout.write(f"{prefix} {msg}\n")
        sys.stdout.flush()

    if LOG_FILE and GLOBAL_CONFIG.get("logging", {}).get("enabled", True):
        with suppress(OSError):
            stripped = strip_ansi(msg)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] [{level:<7s}] {stripped}\n")


def desktop_notify(summary: str, body: str, urgency: str = "normal") -> None:
    if OPT_DRY_RUN or not GLOBAL_CONFIG.get("notifications", {}).get("desktop_enabled", True):
        return
    if shutil.which("notify-send"):
        app_name = GLOBAL_CONFIG.get("notifications", {}).get("app_name", "Dusky Updater")
        with suppress(Exception):
            subprocess.run(
                ["notify-send", "--urgency", urgency, "--app-name", app_name, summary, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )


def auto_prune() -> None:
    log_days = GLOBAL_CONFIG.get("paths", {}).get("log_retention_days", 14)
    backup_days = GLOBAL_CONFIG.get("paths", {}).get("backup_retention_days", 14)
    now_sec = time.time()

    if log_days > 0:
        cutoff = now_sec - (log_days * 86400)
        l_dir = logs_dir()
        if l_dir.is_dir():
            with suppress(Exception):
                for f in l_dir.glob("dusky_update_*.log"):
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        with suppress(OSError):
                            f.unlink()

    if backup_days > 0:
        cutoff = now_sec - (backup_days * 86400)
        b_dir = backups_dir()
        if b_dir.is_dir():
            backup_prefixes = (
                "pre_reset_", "user_mods_", "untracked_collisions_",
                "needs_merge_", "initial_conflicts_", "repo_history_"
            )
            with suppress(Exception):
                for d in b_dir.iterdir():
                    if d.is_dir() and any(d.name.startswith(p) for p in backup_prefixes):
                        if d.stat().st_mtime < cutoff:
                            with suppress(OSError):
                                shutil.rmtree(d, ignore_errors=True)


def ensure_secure_dir(path: Path) -> bool:
    if path.is_symlink():
        return False
    if path.exists() and not path.is_dir():
        return False
    try:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError:
        return False

    try:
        st = path.stat()
        is_owner = st.st_uid == os.getuid()
        is_writable = os.access(path, os.W_OK)
        return is_owner and is_writable and not path.is_symlink()
    except OSError:
        return False


def make_private_dir_under(base: Path, prefix: str) -> Optional[Path]:
    if not ensure_secure_dir(base):
        return None
    try:
        d = tempfile.mkdtemp(prefix=prefix, dir=base)
        dp = Path(d)
        dp.chmod(0o700)
        return dp
    except Exception:
        return None


def make_private_file_under(base: Path, prefix: str, suffix: str = ".log") -> Optional[Path]:
    if not ensure_secure_dir(base):
        return None
    try:
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=base)
        os.close(fd)
        p = Path(path)
        p.chmod(0o600)
        return p
    except Exception:
        return None


def setup_storage_roots():
    global ACTIVE_LOG_BASE_DIR, ACTIVE_BACKUP_BASE_DIR
    l_dir = logs_dir()
    b_dir = backups_dir()

    if ensure_secure_dir(l_dir):
        ACTIVE_LOG_BASE_DIR = l_dir
    else:
        sys.stderr.write(f"Error: Cannot create log directory: {l_dir}\n")
        sys.exit(1)

    if ensure_secure_dir(b_dir):
        ACTIVE_BACKUP_BASE_DIR = b_dir
    else:
        sys.stderr.write(f"Error: Cannot create backup directory: {b_dir}\n")
        sys.exit(1)


def setup_runtime_dir():
    global RUNTIME_DIR, LOCK_FILE
    RUNTIME_DIR = runtime_dir()
    LOCK_FILE = lock_path()


def acquire_lock():
    global LOCK_FD
    try:
        LOCK_FD = open(LOCK_FILE, "a")
    except OSError as e:
        sys.stderr.write(f"Error: Cannot open lock file: {LOCK_FILE} ({e})\n")
        return False

    try:
        fcntl.flock(LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        sys.stderr.write("Error: Another instance is already running.\n")
        return False


def release_lock():
    global LOCK_FD
    if LOCK_FD is not None:
        with suppress(OSError):
            LOCK_FD.close()
        LOCK_FD = None


def check_disk_space(path: Path) -> bool:
    try:
        usage = shutil.disk_usage(path)
        available_mb = usage.free // (1024 * 1024)
        if available_mb < DISK_MIN_FREE_MB:
            log("ERROR", f"Low disk space: {available_mb}MB available at {path} (need {DISK_MIN_FREE_MB}MB)")
            return False
        return True
    except Exception:
        return False


def get_available_bytes(path: Path) -> int:
    try:
        usage = shutil.disk_usage(path)
        return usage.free
    except Exception:
        return 0


def path_copy_size_bytes(path: Path) -> int:
    if not (path.exists() or path.is_symlink()):
        return 0
    if path.is_symlink():
        with suppress(OSError):
            return path.lstat().st_size
        return 0
    if path.is_dir():
        size = 0
        with suppress(Exception):
            for root, dirs, files in os.walk(path):
                size += Path(root).stat().st_size
                for f in files:
                    fp = Path(root) / f
                    if not fp.is_symlink():
                        size += fp.stat().st_size
                    else:
                        size += fp.lstat().st_size
        return size
    else:
        with suppress(OSError):
            return path.stat().st_size
        return 0


def ensure_free_space_for_bytes(target_path: Path, required_bytes: int, context: str = "operation") -> bool:
    if required_bytes <= 0:
        return True
    available_bytes = get_available_bytes(target_path)
    reserve_bytes = DISK_COPY_RESERVE_MB * 1024 * 1024
    if available_bytes < required_bytes + reserve_bytes:
        required_mb = (required_bytes + reserve_bytes + 1048575) // 1048576
        available_mb = (available_bytes + 1048575) // 1048576
        log("ERROR", f"Insufficient free space for {context}: {available_mb}MB available, need at least {required_mb}MB")
        return False
    return True


def setup_logging():
    global LOG_FILE
    if not GLOBAL_CONFIG.get("logging", {}).get("enabled", True):
        return
    LOG_FILE = make_private_file_under(ACTIVE_LOG_BASE_DIR, f"dusky_update_{RUN_TIMESTAMP}_", ".log")
    if not LOG_FILE:
        sys.stderr.write("Error: Cannot create log file\n")
        sys.exit(1)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("================================================================================\n")
            f.write(f" DUSKY UPDATE LOG — {RUN_TIMESTAMP}\n")
            f.write(f" Kernel: {os.uname().release} | User: {user_home().name} | Python: {sys.version.split()[0]}\n")
            f.write("================================================================================\n")
    except Exception as e:
        sys.stderr.write(f"Error: Cannot write to log file: {e}\n")
        sys.exit(1)


# ==============================================================================
#  THEME COMPILER (MATUGEN JSON)
# ==============================================================================
def compile_theme() -> dict[str, str]:
    default_palette = GLOBAL_CONFIG.get("ui", {}).get("default_palette", {
        "bg": "#1a110e", "fg": "#f1dfd9", "accent": "#ffb59b",
        "error": "#ffb4ab", "warning": "#e7bdaf", "success": "#d5c68e", "muted": "#53433e"
    })
    theme: dict[str, str] = dict(default_palette)

    search_paths = GLOBAL_CONFIG.get("ui", {}).get("theme_paths", [
        ".config/matugen/generated/dusky_tui.json"
    ])

    for raw in search_paths:
        theme_path = Path(raw).expanduser()
        if not theme_path.is_absolute():
            theme_path = user_home() / theme_path

        if theme_path.is_file():
            try:
                data = json.loads(theme_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    theme.update({str(k): str(v) for k, v in data.items()})
                    break
            except (json.JSONDecodeError, OSError):
                pass
    return theme


THEME = compile_theme()


def get_rgb_color(hex_str: str, default: tuple[int, int, int] = (255, 181, 155)) -> tuple[int, int, int]:
    try:
        clean_hex = hex_str.lstrip('#')
        if len(clean_hex) >= 6:
            return int(clean_hex[0:2], 16), int(clean_hex[2:4], 16), int(clean_hex[4:6], 16)
        elif len(clean_hex) == 3:
            return int(clean_hex[0]*2, 16), int(clean_hex[1]*2, 16), int(clean_hex[2]*2, 16)
    except (ValueError, IndexError, Exception):
        pass
    return default


sidebar_w = GLOBAL_CONFIG.get("ui", {}).get("sidebar_width", 35)
log_w = 100 - sidebar_w

DUSKY_CSS = f"""
Screen {{ background: {THEME['bg']}; color: {THEME['fg']}; }}
#sidebar {{
    width: {sidebar_w}%; 
    border-right: solid {THEME['muted']}4d; 
    background: {THEME['bg']};
    height: 100%;
    scrollbar-size-vertical: 1;
}}
#log_container {{ 
    width: {log_w}%; padding: 0; 
    background: {THEME['bg']}; 
    height: 100%;
}}
ContentSwitcher {{ height: 1fr; width: 100%; }}
RichLog {{
    height: 1fr; background: transparent; color: {THEME['fg']};
    border: none; padding: 1 2;
    scrollbar-size-vertical: 1;
}}
ScrollBar {{
    background: transparent;
}}
ScrollBar > .scrollbar--track {{
    background: transparent;
}}
ScrollBar > .scrollbar--bar {{
    color: {THEME['accent']}66;
}}
ScrollBar > .scrollbar--bar:hover {{
    color: {THEME['accent']}cc;
}}
ListView {{ background: transparent; overflow-x: hidden; height: 100%; scrollbar-size-vertical: 1; }}
ListItem {{ 
    padding: 0 1; 
    border-left: tall transparent;
    background: transparent;
}}
ListItem:focus {{ 
    background: {THEME['accent']}1a; 
    border-left: tall {THEME['accent']};
}}
.header-panel {{
    dock: top; height: 1; 
    background: {THEME['bg']}; 
    color: {THEME['accent']};
    content-align: center middle; 
    text-style: bold;
    border-bottom: solid {THEME['muted']}4d;
}}
ProgressBar {{ dock: bottom; margin: 0; height: 1; }}
ProgressBar > .progress--bar {{ color: {THEME['accent']}; }}
ProgressBar > .progress--remaining {{ background: {THEME['muted']}33; }}
"""

# ==============================================================================
#  MANIFEST & PATH CONSTANTS
# ==============================================================================
WORK_TREE = user_home()
GIT_DIR = WORK_TREE / "dusky"


# ==============================================================================
#  STRUCTURAL PATTERN MATCHING & PARSING
# ==============================================================================
@dataclass
class DuskyTask:
    name: str
    mode: Literal['U', 'S', 'GIT']
    ignore_fail: bool
    interactive: bool
    args: list[str]
    status: Literal['pending', 'running', 'success', 'failed', 'skipped'] = 'pending'
    resolved_path: Optional[Path] = None
    interpreter: Optional[list[str]] = None
    path_state: str = "ok"  # "ok", "missing", "conflict"


def parse_manifest(profile: ProfileConfig) -> list[DuskyTask]:
    tasks = [
        DuskyTask("Git Bare Repo Validation", 'GIT', False, False, []),
        DuskyTask("Fetch Upstream & Diff", 'GIT', False, False, []),
        DuskyTask("Forensic Collision Backup", 'GIT', False, False, []),
        DuskyTask("Atomic Snapshot (CoW)", 'GIT', False, False, []),
        DuskyTask("Apply Bare Updates (Reset)", 'GIT', False, False, [])
    ]

    interactive_heuristics = {'reboot_post_lua_update.sh', 'tui_matugen.py', 'dusky_firefox_tui.sh'}

    for entry in profile.sequence:
        entry = entry.strip()
        if not entry or entry.startswith('#'):
            continue

        parts = [p.strip() for p in entry.split("|", 2)]

        if len(parts) == 1:
            mode, flags_raw, cmd_part = "U", "", parts[0]
        elif len(parts) == 2:
            mode, cmd_part = parts[0], parts[1]
            flags_raw = ""
        elif len(parts) == 3:
            mode, flags_raw, cmd_part = parts[0], parts[1], parts[2]
        else:
            continue

        flags = {f.lower() for f in flags_raw.split()}
        with suppress(Exception):
            cmd_tokens = shlex.split(cmd_part)
        if 'cmd_tokens' not in locals() or not cmd_tokens:
            cmd_tokens = cmd_part.split()

        if not cmd_tokens:
            continue

        script_name, *args = cmd_tokens
        ignore_fail = bool(flags.intersection({"ignore", "ignore-fail", "true"}))
        interactive = bool(flags.intersection({"interactive", "tui", "prompt"}))

        if not interactive and any(script_name.startswith(s) for s in interactive_heuristics):
            interactive = True

        tasks.append(DuskyTask(
            name=script_name, mode=mode,  # type: ignore
            ignore_fail=ignore_fail, interactive=interactive,
            args=args
        ))
    return tasks


def is_script_interactive(script_path: Path) -> bool:
    if not script_path.exists() or not script_path.is_file():
        return False
    try:
        with open(script_path, 'r', errors='ignore') as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                line_clean = line.strip().replace(" ", "").lower()
                if "#dusky_interactive=true" in line_clean or "#dusky_interactive=1" in line_clean:
                    return True
    except Exception:
        pass
    return False


def resolve_and_validate_manifest(profile: ProfileConfig, tasks: list[DuskyTask]) -> bool:
    log("INFO", "Performing pre-flight validation and conflict resolution...")

    preflight_failures = 0
    needs_python = False

    for task in tasks:
        if task.mode == 'GIT':
            continue

        script = task.name
        matches = []

        if "/" in script:
            explicit_path = Path(script)
            if not explicit_path.is_absolute():
                explicit_path = WORK_TREE / explicit_path
            if explicit_path.is_file() and os.access(explicit_path, os.R_OK):
                matches.append(explicit_path)
        else:
            for d in profile.search_dirs:
                dir_path = WORK_TREE / d
                candidate = dir_path / script
                if candidate.is_file() and os.access(candidate, os.R_OK):
                    matches.append(candidate)

        if len(matches) == 0:
            task.resolved_path = Path(script)
            task.path_state = "missing"
            log("ERROR", f"Required script not found or unreadable: {script}")
            preflight_failures += 1
            continue
        elif len(matches) == 1:
            script_path = matches[0]
        else:
            predefined = profile.conflict_resolutions.get(script)
            if predefined:
                explicit_pre = Path(predefined)
                if not explicit_pre.is_absolute():
                    explicit_pre = WORK_TREE / explicit_pre
                if explicit_pre.is_file() and os.access(explicit_pre, os.R_OK):
                    script_path = explicit_pre
                    log("INFO", f"Resolved duplicate '{script}' using conflict resolution -> {script_path}")
                else:
                    log("ERROR", f"Predefined resolution for '{script}' is missing or unreadable: {explicit_pre}")
                    task.resolved_path = Path(script)
                    task.path_state = "missing"
                    preflight_failures += 1
                    continue
            else:
                if OPT_DRY_RUN or OPT_FORCE or not sys.stdin.isatty():
                    log("ERROR", f"Conflict: Multiple versions of '{script}' found.")
                    for m in matches:
                        log("ERROR", f"  Found at: {m}")
                    log("ERROR", "Cannot prompt in non-interactive/dry-run mode.")
                    task.resolved_path = Path(script)
                    task.path_state = "conflict"
                    preflight_failures += 1
                    continue

                sys.stdout.write(f"\n{CLR_YLW}[CONFLICT DETECTED]{CLR_RST} Multiple versions of {script} found:\n")
                for j, m in enumerate(matches):
                    sys.stdout.write(f"  {j+1}) {m}\n")

                choice = ""
                while True:
                    try:
                        choice = input(f"Which one should be executed? (1-{len(matches)}): ").strip()
                    except (KeyboardInterrupt, EOFError):
                        log("ERROR", "Input interrupted. Aborting.")
                        sys.exit(1)
                    if choice.isdigit() and 1 <= int(choice) <= len(matches):
                        script_path = matches[int(choice) - 1]
                        log("OK", f"Selected: {script_path}")
                        break
                    print(f"Invalid choice. Please enter a number between 1 and {len(matches)}.")

        task.resolved_path = script_path
        task.path_state = "ok"

        if is_script_interactive(script_path):
            task.interactive = True

        first_line = ""
        with suppress(OSError):
            with open(script_path, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline().rstrip('\r\n')

        has_py_ext = script_path.suffix == ".py"
        has_sh_ext = script_path.suffix == ".sh"
        has_py_shebang = False
        has_bash_shebang = False
        extracted_interpreter = []

        shebang_match = re.match(r'^#!\s*(.+)', first_line)
        if shebang_match:
            shebang_cmd = shebang_match.group(1).strip()
            extracted_interpreter = shebang_cmd.split()
            if any("python" in token for token in extracted_interpreter):
                has_py_shebang = True
            elif extracted_interpreter:
                base_interp = os.path.basename(extracted_interpreter[0])
                if base_interp in ("bash", "sh", "zsh", "dash", "ksh"):
                    has_bash_shebang = True

        resolved_interpreter = []

        if (has_py_ext and has_bash_shebang) or (has_sh_ext and has_py_shebang):
            if OPT_DRY_RUN or OPT_FORCE or not sys.stdin.isatty():
                log("ERROR", f"Interpreter conflict for '{script}': File extension and Shebang disagree.")
                preflight_failures += 1
                continue

            sys.stdout.write(f"\n{CLR_YLW}[INTERPRETER CONFLICT]{CLR_RST} Script {script} has conflicting indicators.\n")
            sys.stdout.write("  1) Run with Bash\n")
            sys.stdout.write("  2) Run with Python\n")

            int_choice = ""
            while True:
                try:
                    int_choice = input("Select interpreter (1-2): ").strip()
                except (KeyboardInterrupt, EOFError):
                    log("ERROR", "Input interrupted. Aborting.")
                    sys.exit(1)
                if int_choice == "1":
                    resolved_interpreter = ["bash"]
                    break
                elif int_choice == "2":
                    resolved_interpreter = ["python3"]
                    needs_python = True
                    break
                else:
                    print("Invalid choice.")
        else:
            if has_py_ext or has_py_shebang:
                needs_python = True
                resolved_interpreter = extracted_interpreter or ["python3"]
            elif extracted_interpreter:
                resolved_interpreter = extracted_interpreter
            else:
                resolved_interpreter = ["bash"]

        task.interpreter = resolved_interpreter

    if preflight_failures > 0:
        log("ERROR", f"Aborting preflight due to {preflight_failures} resolution error(s)")
        return False

    if needs_python and shutil.which("python3") is None and shutil.which("python") is None:
        if OPT_DRY_RUN:
            log("WARN", "[DRY-RUN] Python dependency detected but not installed.")
        else:
            log("WARN", "Python dependency detected, but 'python' binary is not installed.")
            log("INFO", "Installing Python via pacman...")
            try:
                subprocess.run(["sudo", "pacman", "-S", "python", "--noconfirm", "--needed"], check=True)
                log("OK", "Python installed successfully.")
            except subprocess.CalledProcessError:
                log("ERROR", "Failed to install Python. Aborting update sequence.")
                return False

    log("OK", "Preflight validation complete.")
    return True


# ==============================================================================
#  GIT ASYNCHRONOUS ENGINE
# ==============================================================================
class GitEngine:
    def __init__(self, app: App, profile: ProfileConfig):
        self.app = app
        self.profile = profile
        self.log = app.log_main  # type: ignore
        self.git_cmd_base = ['git', f'--git-dir={GIT_DIR}', f'--work-tree={WORK_TREE}']
        backups_dir().mkdir(parents=True, exist_ok=True)

    async def _run(self, *args: str, check: bool = True, task_idx: int = -1) -> tuple[int, str, str]:
        cmd = self.git_cmd_base + list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out, err = stdout.decode('utf-8', errors='replace').strip(), stderr.decode('utf-8', errors='replace').strip()

        if task_idx != -1 and err:
            self.app.log_task(escape(err), task_idx)  # type: ignore

        if proc.returncode != 0 and check:
            msg = f"[bold {THEME['error']}]Git Architecture Error ({proc.returncode}):[/] {escape(err)}"
            self.log(msg)
            if task_idx != -1:
                self.app.log_task(msg, task_idx)  # type: ignore
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=out, stderr=err)
        return proc.returncode, out, err

    async def _run_raw(self, *args: str, timeout_sec: int = 0) -> tuple[int, str, str]:
        cmd = self.git_cmd_base + list(args)
        try:
            coro = asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            if timeout_sec > 0:
                proc = await asyncio.wait_for(coro, timeout=timeout_sec)
            else:
                proc = await coro
            stdout, stderr = await proc.communicate()
            return (proc.returncode,
                    stdout.decode('utf-8', errors='replace').strip(),
                    stderr.decode('utf-8', errors='replace').strip())
        except asyncio.TimeoutError:
            return 124, "", "timeout"
        except Exception as e:
            return 1, "", str(e)

    def _tlog(self, msg: str, idx: int, also_main: bool = False):
        self.app.log_task(msg, idx)  # type: ignore
        if also_main:
            self.log(msg)

    def _detect_git_lock_state(self) -> str:
        for lock_name in ('index.lock', 'config.lock', 'packed-refs.lock',
                          'shallow.lock', 'HEAD.lock', 'ORIG_HEAD.lock', 'FETCH_HEAD.lock'):
            if (GIT_DIR / lock_name).exists():
                return lock_name
        refs_dir = GIT_DIR / "refs"
        if refs_dir.is_dir():
            with suppress(Exception):
                for root, dirs, files in os.walk(refs_dir):
                    for f in files:
                        if f.endswith('.lock'):
                            return str((Path(root) / f).relative_to(GIT_DIR))
        return 'none'

    async def _get_repo_state(self, task_idx: int) -> str:
        if GIT_DIR.is_symlink():
            self._tlog(f"[bold {THEME['error']}]GIT_DIR must not be a symlink: {GIT_DIR}[/]", task_idx, True)
            return 'invalid'
        if not GIT_DIR.exists():
            return 'absent'
        if not GIT_DIR.is_dir():
            self._tlog(f"[bold {THEME['error']}]GIT_DIR path exists but is not a directory: {GIT_DIR}[/]", task_idx, True)
            return 'invalid'
        if GIT_DIR.stat().st_uid != os.getuid():
            self._tlog(f"[bold {THEME['error']}]GIT_DIR is not owned by current user: {GIT_DIR}[/]", task_idx, True)
            return 'invalid'
        if not WORK_TREE.is_dir() or not os.access(WORK_TREE, os.W_OK):
            self._tlog(f"[bold {THEME['error']}]Work tree is not writable: {WORK_TREE}[/]", task_idx, True)
            return 'invalid'

        lock_name = self._detect_git_lock_state()
        while lock_name != 'none':
            lock_path = GIT_DIR / lock_name
            self._tlog(f"[bold {THEME['warning']}]Git lock detected: {lock_path}[/]", task_idx, True)
            try:
                lock_age = int(time.time() - lock_path.stat().st_mtime)
            except OSError:
                lock_age = 0

            lock_open = False
            with suppress(OSError):
                lock_real = str(lock_path.resolve())
                for pid_dir in os.listdir("/proc"):
                    if not pid_dir.isdigit():
                        continue
                    fd_dir = f"/proc/{pid_dir}/fd"
                    with suppress(OSError):
                        for fd_name in os.listdir(fd_dir):
                            with suppress(OSError):
                                if os.readlink(os.path.join(fd_dir, fd_name)) == lock_real:
                                    lock_open = True
                                    break
                    if lock_open:
                        break

            if lock_open:
                self._tlog(f"[bold {THEME['error']}]Lock file is held by a live process. Refusing to remove.[/]", task_idx, True)
                return 'invalid'

            if lock_age <= 60:
                self._tlog(f"[bold {THEME['error']}]Lock file is too recent ({lock_age}s). Refusing to auto-remove.[/]", task_idx, True)
                return 'invalid'

            try:
                lock_path.unlink()
                self._tlog(f"[bold {THEME['success']}]Stale lock cleared ({lock_age}s old): {lock_name}[/]", task_idx, True)
            except Exception:
                self._tlog(f"[bold {THEME['error']}]Failed to remove stale lock: {lock_path}[/]", task_idx, True)
                return 'invalid'

            new_lock = self._detect_git_lock_state()
            if new_lock == lock_name:
                self._tlog(f"[bold {THEME['error']}]Lock persists after removal attempt: {lock_path}[/]", task_idx, True)
                return 'invalid'
            lock_name = new_lock

        rc, _, _ = await self._run_raw('rev-parse', '--git-dir')
        if rc != 0:
            self._tlog(f"[bold {THEME['error']}]Repository metadata invalid or corrupted: {GIT_DIR}[/]", task_idx, True)
            return 'invalid'
        return 'valid'

    def _detect_git_operation_state(self) -> str:
        if (GIT_DIR / 'rebase-merge').is_dir() or (GIT_DIR / 'rebase-apply').is_dir():
            return 'rebase'
        if (GIT_DIR / 'MERGE_HEAD').is_file():
            return 'merge'
        if (GIT_DIR / 'CHERRY_PICK_HEAD').is_file():
            return 'cherry-pick'
        if (GIT_DIR / 'REVERT_HEAD').is_file():
            return 'revert'
        if (GIT_DIR / 'BISECT_LOG').is_file():
            return 'bisect'
        return 'none'

    async def _ensure_repo_defaults(self):
        rc, val, _ = await self._run_raw('config', '--get', 'status.showUntrackedFiles')
        if val.strip() != 'no':
            await self._run_raw('config', 'status.showUntrackedFiles', 'no')

    def _canon_url(self, url: str) -> str:
        url = url.rstrip('/').removesuffix('.git')
        for prefix, replacement in [
            ('git@github.com:', 'github.com/'),
            ('ssh://git@github.com/', 'github.com/'),
            ('https://github.com/', 'github.com/'),
            ('http://github.com/', 'github.com/'),
        ]:
            if url.startswith(prefix):
                return replacement + url[len(prefix):]
        return url

    async def _get_fetch_source(self) -> str:
        want = self._canon_url(self.profile.repo_url)
        for remote in ('origin', 'dusky-upstream'):
            rc, url, _ = await self._run_raw('remote', 'get-url', remote)
            if rc == 0 and self._canon_url(url.strip()) == want:
                return remote
        return self.profile.repo_url

    async def _fetch_with_retry(self, source: str, tracking_ref: str, task_idx: int) -> bool:
        FETCH_TIMEOUT = GLOBAL_CONFIG.get("git", {}).get("fetch_timeout", 60)
        MAX_ATTEMPTS = GLOBAL_CONFIG.get("git", {}).get("fetch_max_attempts", 5)
        wait = 2
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._tlog(f"[dim]Fetch attempt {attempt}/{MAX_ATTEMPTS}...[/dim]", task_idx)
            cmd = ['git', f'--git-dir={GIT_DIR}', f'--work-tree={WORK_TREE}',
                   'fetch', '--no-write-fetch-head', source, f'+refs/heads/{self.profile.branch}:{tracking_ref}']
            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT),
                    timeout=FETCH_TIMEOUT
                )
                stdout, _ = await proc.communicate()
                output = stdout.decode('utf-8', errors='replace').strip()
                if output:
                    self._tlog(f"[dim]{escape(output)}[/dim]", task_idx)
                rc = proc.returncode
            except asyncio.TimeoutError:
                rc = 124
            if rc == 0:
                return True
            if attempt < MAX_ATTEMPTS:
                reason = "timed out" if rc == 124 else f"rc={rc}"
                self._tlog(f"[bold {THEME['warning']}]Fetch {attempt}/{MAX_ATTEMPTS} {reason}. Retrying in {wait}s...[/]", task_idx, True)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 60)
        return False

    async def _clone_with_retry(self, task_idx: int) -> bool:
        CLONE_TIMEOUT = GLOBAL_CONFIG.get("git", {}).get("clone_timeout", 120)
        MAX_ATTEMPTS = GLOBAL_CONFIG.get("git", {}).get("clone_max_attempts", 5)
        wait = 2
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._tlog(f"[dim]Clone attempt {attempt}/{MAX_ATTEMPTS}...[/dim]", task_idx)
            cmd = ['git', 'clone', '--bare', '--branch', self.profile.branch, self.profile.repo_url, str(GIT_DIR)]
            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT),
                    timeout=CLONE_TIMEOUT
                )
                stdout, _ = await proc.communicate()
                output = stdout.decode('utf-8', errors='replace').strip()
                if output:
                    self._tlog(f"[dim]{escape(output)}[/dim]", task_idx)
                rc = proc.returncode
            except asyncio.TimeoutError:
                rc = 124
            if rc == 0:
                await self._run_raw('config', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
                return True
            if GIT_DIR.exists():
                shutil.rmtree(str(GIT_DIR), ignore_errors=True)
            if attempt < MAX_ATTEMPTS:
                reason = "timed out" if rc == 124 else f"rc={rc}"
                self._tlog(f"[bold {THEME['warning']}]Clone {attempt}/{MAX_ATTEMPTS} {reason}. Retrying in {wait}s...[/]", task_idx, True)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 60)
        return False

    async def _collect_dir_collision_roots(self, root_rel: str, tracked_exact: dict,
                                            tracked_descendants: dict, out_dict: dict):
        stack = [root_rel]
        while stack:
            rel = stack.pop()
            abs_path = WORK_TREE / rel
            if not (abs_path.exists() or abs_path.is_symlink()):
                continue
            if abs_path.is_symlink() or not abs_path.is_dir():
                if rel not in tracked_exact:
                    out_dict[rel] = 1
                continue
            if rel in tracked_exact:
                out_dict[rel] = 1
                continue
            try:
                children = [p.name for p in abs_path.iterdir()]
            except OSError:
                children = []
            if rel in tracked_descendants:
                if not children:
                    out_dict[rel] = 1
                else:
                    for child in children:
                        stack.append(f"{rel}/{child}")
            else:
                out_dict[rel] = 1

    async def _backup_worktree_collisions(self, ref: str, honor_tracked: bool, task_idx: int) -> bool:
        rc, ls_tree, _ = await self._run_raw('ls-tree', '-r', '-z', '--name-only', ref)
        incoming = [f for f in ls_tree.split('\0') if f]

        tracked_exact: dict = {}
        tracked_descendants: dict = {}
        if honor_tracked:
            _, ls_files, _ = await self._run_raw('ls-files', '-z')
            for f in ls_files.split('\0'):
                if not f:
                    continue
                tracked_exact[f] = 1
                parts = f.split('/')
                for i in range(1, len(parts)):
                    tracked_descendants['/'.join(parts[:i])] = 1

        collision_candidates: dict = {}
        for tgt in incoming:
            abs_path = WORK_TREE / tgt
            if abs_path.exists() or abs_path.is_symlink():
                if abs_path.is_dir() and not abs_path.is_symlink():
                    if honor_tracked and tgt in tracked_descendants:
                        await self._collect_dir_collision_roots(tgt, tracked_exact, tracked_descendants, collision_candidates)
                    else:
                        collision_candidates[tgt] = 1
                elif not honor_tracked or tgt not in tracked_exact:
                    collision_candidates[tgt] = 1
            ancestor = ""
            remaining = tgt
            while '/' in remaining:
                part, remaining = remaining.split('/', 1)
                ancestor = f"{ancestor}/{part}" if ancestor else part
                abs_anc = WORK_TREE / ancestor
                if abs_anc.exists() or abs_anc.is_symlink():
                    if abs_anc.is_symlink() or not abs_anc.is_dir():
                        if not honor_tracked or ancestor not in tracked_exact:
                            collision_candidates[ancestor] = 1
                        break

        collision_roots: dict = {}
        for coll in collision_candidates:
            skip = any(
                '/'.join(coll.split('/')[:i]) in collision_candidates
                for i in range(1, len(coll.split('/')))
            )
            if not skip:
                collision_roots[coll] = 1

        if not collision_roots:
            self._tlog(f"[bold {THEME['success']}]No structural filesystem conflicts detected.[/]", task_idx)
            return True

        required_bytes = sum(
            path_copy_size_bytes(WORK_TREE / r)
            for r in collision_roots
            if (WORK_TREE / r).exists() or (WORK_TREE / r).is_symlink()
        )
        backup_base = backups_dir()
        if not check_disk_space(backup_base):
            return False
        if not ensure_free_space_for_bytes(backup_base, required_bytes, "collision backup"):
            return False

        backup_dir = make_private_dir_under(backup_base, f"untracked_collisions_{RUN_TIMESTAMP}_")
        if not backup_dir:
            self._tlog(f"[bold {THEME['error']}]Failed to create collision backup directory[/]", task_idx, True)
            return False

        with suppress(Exception):
            (backup_dir / "INFO.txt").write_text(
                f"Dusky work-tree collision backup\nCreated: {RUN_TIMESTAMP}\nRef: {ref}\nWork tree: {WORK_TREE}\n"
            )
            (backup_dir / "INFO.txt").chmod(0o600)

        moved_log = backup_dir / "MOVED_PATHS.txt"
        with suppress(Exception):
            moved_log.write_text("")
            moved_log.chmod(0o600)

        self._tlog(f"[bold {THEME['warning']}]{len(collision_roots)} work-tree collision(s) found. Backing up...[/]", task_idx, True)
        for coll_rel in collision_roots:
            coll_src = WORK_TREE / coll_rel
            if not (coll_src.exists() or coll_src.is_symlink()):
                continue
            coll_dest = backup_dir / coll_rel
            coll_dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(coll_src), str(coll_dest))
                self._tlog(f"[dim]  → Backed up collision: {escape(coll_rel)}[/dim]", task_idx)
                with suppress(Exception):
                    with open(moved_log, "a") as mf:
                        mf.write(f"{coll_rel}\n")
            except Exception as e:
                self._tlog(f"[bold {THEME['error']}]Failed to move collision {escape(coll_rel)}: {escape(str(e))}[/]", task_idx, True)
                return False

        self._tlog(f"[bold {THEME['success']}]Collisions backed up → {backup_dir}[/]", task_idx, True)
        return True

    async def _capture_tracked_changes(self) -> tuple[list, dict, dict, dict]:
        await self._run_raw('update-index', '-q', '--refresh')
        rc, raw, _ = await self._run_raw('diff-index', '--raw', '--no-renames', '-z', 'HEAD', '--')

        paths: list = []
        status_map: dict = {}
        old_mode_map: dict = {}
        old_oid_map: dict = {}

        if rc != 0 or not raw.strip():
            return paths, status_map, old_mode_map, old_oid_map

        records = raw.split('\0')
        i = 0
        while i + 1 < len(records):
            meta = records[i].lstrip(':')
            path = records[i + 1]
            i += 2
            if not meta or not path:
                continue
            parts = meta.split()
            if len(parts) < 5:
                continue
            oldmode, _, oldoid, _, status = parts[0], parts[1], parts[2], parts[3], parts[4]
            status = status.rstrip('0123456789')
            paths.append(path)
            status_map[path] = status
            old_mode_map[path] = oldmode
            old_oid_map[path] = oldoid

        return paths, status_map, old_mode_map, old_oid_map

    async def _backup_user_modifications(self, change_paths: list, change_status: dict, task_idx: int) -> Optional[Path]:
        if not change_paths:
            return None

        backup_base = backups_dir()
        required_bytes = sum(
            path_copy_size_bytes(WORK_TREE / p)
            for p in change_paths
            if change_status.get(p) != 'D' and ((WORK_TREE / p).exists() or (WORK_TREE / p).is_symlink())
        )
        if not check_disk_space(backup_base):
            return None
        if not ensure_free_space_for_bytes(backup_base, required_bytes, "modified-files backup"):
            return None

        backup_dir = make_private_dir_under(backup_base, f"user_mods_{RUN_TIMESTAMP}_")
        if not backup_dir:
            self._tlog(f"[bold {THEME['error']}]Failed to create user-mods backup dir[/]", task_idx, True)
            return None

        manifest = backup_dir / "MANIFEST.txt"
        try:
            manifest.write_text("")
            manifest.chmod(0o600)
        except Exception:
            return None

        for path in change_paths:
            st = change_status.get(path, "?")
            src = WORK_TREE / path
            if st == 'D' or not (src.exists() or src.is_symlink()):
                with suppress(Exception):
                    with open(manifest, "a") as mf:
                        mf.write(f"status={st} has_copy=0 path={path}\n")
                continue
            dest = backup_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                proc = await asyncio.create_subprocess_exec('cp', '-a', '--reflink=auto', str(src), str(dest))
                await proc.wait()
                if proc.returncode != 0:
                    self._tlog(f"[bold {THEME['error']}]Backup failed for: {escape(path)}[/]", task_idx, True)
                    return None
                with open(manifest, "a") as mf:
                    mf.write(f"status={st} has_copy=1 path={path}\n")
            except Exception as e:
                self._tlog(f"[bold {THEME['error']}]Exception backing up {escape(path)}: {escape(str(e))}[/]", task_idx, True)
                return None

        self._tlog(f"[bold {THEME['success']}]Backed up {len(change_paths)} tracked change(s) → {backup_dir}[/]", task_idx, True)
        return backup_dir

    async def _backup_full_tracked_tree(self, task_idx: int) -> Optional[Path]:
        backup_base = backups_dir()
        _, ls_files, _ = await self._run_raw('ls-files', '-z')
        tracked = [f for f in ls_files.split('\0') if f]
        required_bytes = sum(
            path_copy_size_bytes(WORK_TREE / p)
            for p in tracked
            if (WORK_TREE / p).exists() or (WORK_TREE / p).is_symlink()
        )
        if not check_disk_space(backup_base):
            return None
        if not ensure_free_space_for_bytes(backup_base, required_bytes, "full tracked-tree backup"):
            return None

        backup_dir = make_private_dir_under(backup_base, f"pre_reset_{RUN_TIMESTAMP}_")
        if not backup_dir:
            self._tlog(f"[bold {THEME['error']}]Failed to create full tracked-tree backup dir[/]", task_idx, True)
            return None

        with suppress(Exception):
            _, head, _ = await self._run_raw('rev-parse', 'HEAD')
            info = backup_dir / "INFO.txt"
            info.write_text(f"Dusky full tracked-tree backup\nCreated: {RUN_TIMESTAMP}\nHEAD: {head.strip()}\n")
            info.chmod(0o600)

        copied = 0
        for path in tracked:
            src = WORK_TREE / path
            if not (src.exists() or src.is_symlink()):
                continue
            dest = backup_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                proc = await asyncio.create_subprocess_exec('cp', '-a', '--reflink=auto', str(src), str(dest))
                await proc.wait()
                if proc.returncode == 0:
                    copied += 1
            except Exception:
                pass

        self._tlog(f"[bold {THEME['success']}]Full tracked-tree backup: {backup_dir} ({copied} file(s))[/]", task_idx, True)
        return backup_dir

    async def _backup_git_history(self, task_idx: int) -> Optional[Path]:
        backup_base = backups_dir()
        required_bytes = path_copy_size_bytes(GIT_DIR)
        if not check_disk_space(backup_base):
            return None
        if not ensure_free_space_for_bytes(backup_base, required_bytes, "Git history backup"):
            return None

        backup_root = make_private_dir_under(backup_base, f"repo_history_{RUN_TIMESTAMP}_")
        if not backup_root:
            self._tlog(f"[bold {THEME['error']}]Failed to create Git history backup dir[/]", task_idx, True)
            return None

        backup_repo = backup_root / "repo.git"
        try:
            proc = await asyncio.create_subprocess_exec('cp', '-a', '--reflink=auto', str(GIT_DIR), str(backup_repo))
            await proc.wait()
            if proc.returncode != 0:
                self._tlog(f"[bold {THEME['error']}]Failed to copy Git history[/]", task_idx, True)
                return None
        except Exception as e:
            self._tlog(f"[bold {THEME['error']}]Exception copying git dir: {escape(str(e))}[/]", task_idx, True)
            return None

        with suppress(Exception):
            info = backup_root / "INFO.txt"
            info.write_text(f"Dusky Git history backup\nCreated: {RUN_TIMESTAMP}\nSource: {GIT_DIR}\n")
            info.chmod(0o600)

        self._tlog(f"[bold {THEME['success']}]Git history preserved → {backup_root}[/]", task_idx, True)
        return backup_root

    async def _get_head_path_meta(self, path: str) -> tuple[str, str]:
        rc, record, _ = await self._run_raw('ls-tree', '-z', 'HEAD', '--', path)
        if rc != 0 or not record.strip():
            return ('', '')
        try:
            meta_part = record.split('\t')[0]
            parts = meta_part.strip().split()
            if len(parts) >= 3:
                return (parts[0], parts[2])
        except Exception:
            pass
        return ('', '')

    async def _restore_user_modifications(self, backup_dir: Path, change_paths: list,
                                           change_status: dict, change_old_mode: dict,
                                           change_old_oid: dict, task_idx: int) -> bool:
        if not (backup_dir and backup_dir.is_dir() and change_paths):
            return True

        merge_dir: Optional[Path] = None
        restore_count = merge_count = deletion_count = 0
        all_ok = True

        for path in change_paths:
            status = change_status.get(path, "?")
            old_oid = change_old_oid.get(path, "")
            old_mode = change_old_mode.get(path, "")
            backup_src = backup_dir / path
            target = WORK_TREE / path

            new_mode, new_oid = await self._get_head_path_meta(path)
            old_oid_valid = bool(old_oid and old_oid.strip("0"))

            if status == 'D':
                if not new_oid:
                    action = "delete-preserved"
                elif old_oid_valid and new_oid == old_oid and new_mode == old_mode:
                    action = "delete-safe"
                else:
                    action = "delete-merge"
            else:
                has_copy = backup_src.exists() or backup_src.is_symlink()
                if not has_copy:
                    continue
                if old_oid_valid:
                    safe = (new_oid == old_oid and new_mode == old_mode) or not new_oid
                else:
                    safe = not new_oid
                action = "restore" if safe else "merge"

            if action == "delete-preserved":
                deletion_count += 1

            elif action == "delete-safe":
                try:
                    if target.exists() or target.is_symlink():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(str(target))
                        else:
                            target.unlink()
                    deletion_count += 1
                    self._tlog(f"[dim]  → Re-applied tracked deletion: {escape(path)}[/dim]", task_idx)
                except Exception as e:
                    self._tlog(f"[bold {THEME['error']}]Failed to re-apply deletion {escape(path)}: {escape(str(e))}[/]", task_idx, True)
                    all_ok = False

            elif action in ("delete-merge", "merge"):
                if not merge_dir:
                    backup_base = backups_dir()
                    merge_dir = make_private_dir_under(backup_base, f"needs_merge_{RUN_TIMESTAMP}_")
                    if not merge_dir:
                        self._tlog(f"[bold {THEME['error']}]Failed to create merge dir[/]", task_idx, True)
                        all_ok = False
                        continue

                if action == "delete-merge":
                    marker = merge_dir / (path + ".dusky_deleted")
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        marker.write_text(
                            f"Tracked deletion needs review.\nPath: {path}\n"
                            f"Old mode: {old_mode} | Old oid: {old_oid}\n"
                            f"New mode: {new_mode or '<absent>'} | New oid: {new_oid or '<absent>'}\n"
                        )
                        marker.chmod(0o600)
                        merge_count += 1
                        self._tlog(f"[dim]  → Deletion needs manual review: {escape(path)}[/dim]", task_idx)
                    except Exception:
                        all_ok = False
                else:
                    mdest = merge_dir / path
                    mdest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        proc = await asyncio.create_subprocess_exec('cp', '-a', '--reflink=auto', str(backup_src), str(mdest))
                        await proc.wait()
                        if proc.returncode == 0:
                            merge_count += 1
                            self._tlog(f"[dim]  → Upstream changed: {escape(path)} (your version saved for merge)[/dim]", task_idx)
                        else:
                            self._tlog(f"[bold {THEME['error']}]Failed to save merge copy: {escape(path)}[/]", task_idx, True)
                            all_ok = False
                    except Exception as e:
                        self._tlog(f"[bold {THEME['error']}]Exception saving merge copy {escape(path)}: {escape(str(e))}[/]", task_idx, True)
                        all_ok = False

            elif action == "restore":
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with tempfile.TemporaryDirectory(prefix=f".{target.name}.dtmp.", dir=target.parent) as tmpdir:
                        tmp_file = Path(tmpdir) / target.name
                        proc = await asyncio.create_subprocess_exec('cp', '-a', '--reflink=auto', str(backup_src), str(tmp_file))
                        await proc.wait()
                        if proc.returncode == 0:
                            if target.exists() or target.is_symlink():
                                displaced = Path(tmpdir) / (".old_" + target.name)
                                shutil.move(str(target), str(displaced))
                            shutil.move(str(tmp_file), str(target))
                            restore_count += 1
                            self._tlog(f"[dim]  → Restored: {escape(path)}[/dim]", task_idx)
                        else:
                            self._tlog(f"[bold {THEME['error']}]Restore failed for: {escape(path)}[/]", task_idx, True)
                            all_ok = False
                except Exception as e:
                    self._tlog(f"[bold {THEME['error']}]Exception restoring {escape(path)}: {escape(str(e))}[/]", task_idx, True)
                    all_ok = False

        if restore_count:
            self._tlog(f"[bold {THEME['success']}]Auto-restored {restore_count} file(s) (upstream had not changed them)[/]", task_idx, True)
        if merge_count:
            self._tlog(f"[bold {THEME['warning']}]{merge_count} file(s) need manual merge (upstream also changed them)[/]", task_idx, True)
            if merge_dir:
                self._tlog(f"[dim]  Review in: {merge_dir}[/dim]", task_idx)
        if deletion_count:
            self._tlog(f"[bold {THEME['warning']}]{deletion_count} tracked deletion(s) handled[/]", task_idx, True)

        if all_ok:
            shutil.rmtree(str(backup_dir), ignore_errors=True)

        return all_ok

    async def execute_phase(self) -> bool:
        UPSTREAM_TRACKING_REF = f'refs/dusky-updater/upstream/{self.profile.branch}'

        user_mods_backup: Optional[Path] = None
        local_head = ""
        change_paths: list = []
        change_status: dict = {}
        change_old_mode: dict = {}
        change_old_oid: dict = {}

        try:
            # Task 0: Bare Repo Validation
            idx = 0
            self.app.update_task_state(idx, "running")  # type: ignore
            self._tlog(f"[bold {THEME['accent']}]>>> PROCESS INITIATED:[/] Bare Repository Validation\n", idx)

            repo_state = await self._get_repo_state(idx)

            if repo_state == 'absent':
                self._tlog(f"[bold {THEME['warning']}]Bare repository missing. Cloning from upstream...[/]", idx, True)
                if not await self._clone_with_retry(idx):
                    raise RuntimeError("Clone sequence failed.")
                await self._ensure_repo_defaults()

                self._tlog("[dim]Checking out files into work-tree...[/dim]", idx)
                if not await self._backup_worktree_collisions('HEAD', honor_tracked=False, task_idx=idx):
                    raise RuntimeError("Collision backup failed during initial checkout.")

                rc, _, err = await self._run_raw('checkout')
                if rc != 0:
                    self._tlog(f"[bold {THEME['error']}]Checkout failed: {escape(err)}[/]", idx, True)
                    raise RuntimeError("Work-tree checkout failed.")

                self._tlog(f"[bold {THEME['success']}]Repository cloned and checked out successfully.[/]", idx, True)
                self.app.update_task_state(idx, "success")  # type: ignore
                for i in range(1, 5):
                    self.app.update_task_state(i, "skipped")  # type: ignore
                return True

            elif repo_state == 'invalid':
                raise RuntimeError("Repository is in an invalid or unsafe state.")

            self._tlog(f"[bold {THEME['success']}]Bare repository integrity verified.[/]", idx)
            self.app.update_task_state(idx, "success")  # type: ignore

            # Task 1: Fetch Upstream & Diff
            idx = 1
            self.app.update_task_state(idx, "running")  # type: ignore
            self._tlog(f"[bold {THEME['accent']}]>>> PROCESS INITIATED:[/] Fetch Upstream & Diff\n", idx)

            op = self._detect_git_operation_state()
            if op != 'none':
                self._tlog(f"[bold {THEME['error']}]Git {op} is in progress. Resolve it manually first.[/]", idx, True)
                raise RuntimeError(f"Git {op} in progress.")

            fetch_source = await self._get_fetch_source()
            self._tlog(f"[dim]Fetching from {escape(fetch_source)}...[/dim]", idx)

            if not await self._fetch_with_retry(fetch_source, UPSTREAM_TRACKING_REF, idx):
                raise RuntimeError("Fetch failed after all retry attempts.")

            rc, raw_local, _ = await self._run_raw('rev-parse', '--verify', '-q', 'HEAD')
            local_head = raw_local.strip() if rc == 0 else ""

            rc, raw_remote, _ = await self._run_raw('rev-parse', '--verify', '-q', UPSTREAM_TRACKING_REF)
            remote_head = raw_remote.strip() if rc == 0 else ""

            if not remote_head:
                self._tlog(f"[bold {THEME['error']}]Cannot determine upstream HEAD for branch {self.profile.branch}.[/]", idx, True)
                raise RuntimeError("No upstream HEAD available.")

            if not local_head:
                self._tlog(f"[bold {THEME['warning']}]Local repository has no commits yet. Initializing from upstream...[/]", idx, True)
                rc1, _, _ = await self._run_raw('symbolic-ref', 'HEAD', f'refs/heads/{self.profile.branch}')
                if rc1 != 0:
                    raise RuntimeError("Failed to point HEAD at branch.")
                if not await self._backup_worktree_collisions(UPSTREAM_TRACKING_REF, honor_tracked=False, task_idx=idx):
                    raise RuntimeError("Collision backup failed during unborn init.")
                rc2, _, err2 = await self._run_raw('reset', '--hard', UPSTREAM_TRACKING_REF)
                if rc2 != 0:
                    self._tlog(f"[bold {THEME['error']}]Failed to init unborn repo: {escape(err2)}[/]", idx, True)
                    raise RuntimeError("Reset of unborn repo failed.")
                await self._ensure_repo_defaults()
                self._tlog(f"[bold {THEME['success']}]Repository synchronized (initial bootstrap).[/]", idx, True)
                self.app.update_task_state(idx, "success")  # type: ignore
                for i in range(2, 5):
                    self.app.update_task_state(i, "skipped")  # type: ignore
                return True

            if local_head == remote_head:
                self._tlog(f"[bold {THEME['success']}]Repository synchronization perfect. Origin matched.[/]", idx, True)
                await self._ensure_repo_defaults()
                self.app.update_task_state(idx, "success")  # type: ignore
                for i in range(2, 5):
                    self.app.update_task_state(i, "skipped")  # type: ignore
                return True

            rc, commit_count_raw, _ = await self._run_raw('rev-list', '--count', f'{local_head}..{remote_head}')
            commit_count = commit_count_raw.strip() or "?"
            rc, changed_raw, _ = await self._run_raw('diff', '--name-only', f'{local_head}..{remote_head}')
            changed_files = [f for f in changed_raw.split('\n') if f.strip()]
            self._tlog(
                f"\n[bold {THEME['accent']}]Upstream changes:[/]\n"
                f"    Commits behind:  {commit_count}\n"
                f"    Files changed:   {len(changed_files)}",
                idx
            )
            rc_log, log_out, _ = await self._run_raw('log', '--oneline', '--no-decorate', '-10', f'{local_head}..{remote_head}')
            if rc_log == 0 and log_out:
                self._tlog("    Recent commits:", idx)
                for line in log_out.split('\n')[:10]:
                    self._tlog(f"      {escape(line)}", idx)

            rc, diff_out, _ = await self._run_raw('diff', f'{local_head}..{remote_head}')
            if diff_out.strip():
                self._tlog(f"\n[bold {THEME['warning']}]Differential Divergence Detected:[/]\n", idx)
                self.app.log_task(Syntax(diff_out, "diff", theme="monokai", background_color="default", word_wrap=True), idx)  # type: ignore
                self.app.git_diff_text = diff_out  # type: ignore

            mb_rc, base_commit, _ = await self._run_raw('merge-base', local_head, remote_head)
            base_commit = base_commit.strip()

            if mb_rc == 1 or (mb_rc == 0 and not base_commit):
                self._tlog(f"[bold {THEME['warning']}]Local repository does not share history with upstream (unrelated histories).[/]", idx, True)
                if not OPT_ALLOW_DIVERGED_RESET:
                    self._tlog(f"[bold {THEME['error']}]Aborting: non-interactive mode and unrelated history. Use --allow-diverged-reset to override.[/]", idx, True)
                    raise RuntimeError("Unrelated upstream history. Aborting.")
                if not await self._backup_git_history(idx):
                    raise RuntimeError("Git history backup failed.")
                if not await self._backup_worktree_collisions(UPSTREAM_TRACKING_REF, honor_tracked=True, task_idx=idx):
                    raise RuntimeError("Collision backup failed.")
                if not await self._backup_full_tracked_tree(idx):
                    raise RuntimeError("Full tracked-tree backup failed.")
                rc_r, _, err_r = await self._run_raw('reset', '--hard', UPSTREAM_TRACKING_REF)
                if rc_r != 0:
                    raise RuntimeError(f"Reset failed (unrelated histories): {err_r}")
                await self._ensure_repo_defaults()
                self._tlog(f"[bold {THEME['success']}]Reset complete. Previous state fully preserved in backup.[/]", idx, True)
                self.app.update_task_state(idx, "success")  # type: ignore
                for i in range(2, 5):
                    self.app.update_task_state(i, "skipped")  # type: ignore
                return True

            elif mb_rc != 0:
                raise RuntimeError(f"merge-base failed (rc={mb_rc}).")

            if base_commit == local_head:
                self._tlog(f"[bold {THEME['accent']}]Fast-forward sync detected.[/]", idx)
            else:
                self._tlog(f"[bold {THEME['warning']}]Local history diverged from upstream.[/]", idx, True)
                if not OPT_ALLOW_DIVERGED_RESET:
                    self._tlog(f"[bold {THEME['error']}]Aborting: non-interactive mode and diverged history. Use --allow-diverged-reset to override.[/]", idx, True)
                    raise RuntimeError("Diverged history detected. Aborting.")
                if not await self._backup_git_history(idx):
                    raise RuntimeError("Git history backup failed.")

            self.app.update_task_state(idx, "success")  # type: ignore

            # Task 2: Forensic Collision Backup
            idx = 2
            self.app.update_task_state(idx, "running")  # type: ignore
            self._tlog(f"[bold {THEME['accent']}]>>> PROCESS INITIATED:[/] Forensic Collision Backup\n", idx)

            if not await self._backup_worktree_collisions(UPSTREAM_TRACKING_REF, honor_tracked=True, task_idx=idx):
                raise RuntimeError("Collision backup failed.")
            self.app.update_task_state(idx, "success")  # type: ignore

            # Task 3: Atomic Snapshot (CoW)
            idx = 3
            self.app.update_task_state(idx, "running")  # type: ignore
            self._tlog(f"[bold {THEME['accent']}]>>> PROCESS INITIATED:[/] Atomic Snapshot (CoW)\n", idx)

            change_paths, change_status, change_old_mode, change_old_oid = await self._capture_tracked_changes()
            if change_paths:
                user_mods_backup = await self._backup_user_modifications(change_paths, change_status, idx)
                if user_mods_backup is None:
                    raise RuntimeError("User modifications backup failed.")
            else:
                self._tlog(f"[bold {THEME['success']}]No local tracked modifications found. Snapshot skipped.[/]", idx)

            self.app.update_task_state(idx, "success")  # type: ignore

            # Task 4: Apply Reset
            idx = 4
            self.app.update_task_state(idx, "running")  # type: ignore
            self._tlog(f"[bold {THEME['accent']}]>>> PROCESS INITIATED:[/] Apply Bare Updates (Reset)\n", idx)

            rc_reset, _, err_reset = await self._run_raw('reset', '--hard', UPSTREAM_TRACKING_REF)
            if rc_reset != 0:
                self._tlog(f"[bold {THEME['error']}]Reset failed: {escape(err_reset)}[/]", idx, True)
                raise RuntimeError(f"Reset failed (rc={rc_reset}).")

            self._tlog(f"[bold {THEME['success']}]Bare Repository reset applied and synchronized.[/]", idx, True)

            if user_mods_backup and change_paths:
                self._tlog(f"[bold {THEME['accent']}]Restoring your tracked modifications...[/]", idx)
                ok = await self._restore_user_modifications(
                    user_mods_backup, change_paths, change_status, change_old_mode, change_old_oid, idx
                )
                if not ok:
                    self._tlog(f"[bold {THEME['warning']}]Some files could not be restored. Backup preserved at: {user_mods_backup}[/]", idx, True)

            await self._ensure_repo_defaults()
            self.app.update_task_state(idx, "success")  # type: ignore
            return True

        except Exception as e:
            err_msg = f"[bold {THEME['error']}][FATAL][/] Git Sync Failure: {escape(str(e))}"
            self.log(err_msg)
            for i in range(5):
                st = self.app.tasks[i].status  # type: ignore
                if st == "running":
                    self.app.update_task_state(i, "failed")  # type: ignore
                elif st == "pending":
                    self.app.update_task_state(i, "skipped")  # type: ignore
            return False


# ==============================================================================
#  TEXTUAL UI COMPONENTS
# ==============================================================================
class MainLogItem(ListItem):
    def compose(self) -> ComposeResult:
        yield Label(f" [bold {THEME['accent']}]CORE[/] Dusky Execution Engine", classes="list-item-label")


class TaskItem(ListItem):
    status = reactive("pending")

    def __init__(self, task: DuskyTask, index: int):
        super().__init__()
        self.dusky_task = task
        self.task_index = index

    def compose(self) -> ComposeResult:
        yield Label(id=f"lbl-{self.task_index}")

    def on_mount(self) -> None:
        self._update_label()

    def watch_status(self, old_status: str, new_status: str) -> None:
        self._update_label()

    def _update_label(self) -> None:
        if not self.is_mounted:
            return

        if self.dusky_task.mode == 'GIT':
            badge = f"[bold {THEME['accent']}]GIT[/]"
        elif self.dusky_task.mode == 'S':
            badge = f"[bold {THEME['error']}]SUDO[/]"
        else:
            badge = f"[bold {THEME['success']}]USER[/]"

        cmd_str = f"{self.dusky_task.name} {' '.join(self.dusky_task.args)}".strip()
        if len(cmd_str) > 31:
            cmd_str = cmd_str[:28] + "..."
        cmd_str = escape(cmd_str)

        suffix = ""
        symbols = GLOBAL_CONFIG.get("ui", {}).get(
            "ascii_symbols" if ASCII_MODE else "unicode_symbols",
            {"pending": "○", "running": "◉", "success": "✓", "failed": "✗", "skipped": "-"}
        )

        icon_map = {
            'pending': f"[dim {THEME['muted']}]{symbols.get('pending', '○')}[/]",
            'running': f"[bold {THEME['accent']} blink]{symbols.get('running', '◉')}[/]",
            'success': f"[bold {THEME['success']}]{symbols.get('completed', '✓')}[/]",
            'failed':  f"[bold {THEME['error']}]{symbols.get('failed', '✗')}[/]",
            'skipped': f"[dim {THEME['warning']}]{symbols.get('skipped', '-')}[/]"
        }
        icon = icon_map.get(self.status, "?")

        color_map = {
            'running': f"bold {THEME['fg']}", 'pending': f"dim {THEME['muted']}",
            'success': f"bold {THEME['success']}", 'failed': f"bold {THEME['error']}",
            'skipped': f"dim {THEME['warning']}"
        }
        color = color_map.get(self.status, "white")

        with suppress(Exception):
            self.query_one(Label).update(f" {icon}  {badge}  [{color}]{cmd_str}[/]{suffix}")


# ==============================================================================
#  MAIN APPLICATION ENGINE
# ==============================================================================
class DuskyApp(App):
    CSS = DUSKY_CSS

    def __init__(self, profile: ProfileConfig, tasks: list[DuskyTask], has_sudo: bool):
        super().__init__()
        self.profile = profile
        self.tasks = tasks
        self.has_sudo = has_sudo
        self.abort_flag = False
        self.git_diff_text = ""

    def compose(self) -> ComposeResult:
        yield Static(f" 🦅 DUSKY PIPELINE ENGINE (v{VERSION} — {self.profile.name})", classes="header-panel")

        with Horizontal():
            with Vertical(id="sidebar"):
                yield ListView(id="task_list")

            with Vertical(id="log_container"):
                with ContentSwitcher(initial="log-main", id="log_switcher"):
                    yield RichLog(id="log-main", markup=True, wrap=True, auto_scroll=True)
                    for i in range(len(self.tasks)):
                        yield RichLog(id=f"log-task-{i}", markup=True, wrap=True, auto_scroll=True)

        yield ProgressBar(total=len(self.tasks), id="main_progress", show_eta=False)

    async def on_mount(self) -> None:
        self.progress = self.query_one("#main_progress", ProgressBar)

        list_view = self.query_one("#task_list", ListView)
        list_view.append(MainLogItem())
        for i, task in enumerate(self.tasks):
            list_view.append(TaskItem(task, i))

        self.log_main(f"[bold {THEME['accent']}]======================================================[/]")
        self.log_main(f"[bold {THEME['fg']}] ARCHITECTURE INITIALIZATION — {datetime.now().strftime('%H:%M:%S')}[/]")
        self.log_main(f"[bold {THEME['accent']}] Profile: {self.profile.name}[/]")
        self.log_main(f"[bold {THEME['accent']}]======================================================[/]")

        if self.has_sudo:
            interval = GLOBAL_CONFIG.get("sudo", {}).get("heartbeat_interval", 60.0)
            self.set_interval(float(interval), self.ping_sudo)

        self.run_worker(self.execute_pipeline(), exclusive=True, thread=False)

    def log_main(self, message: str) -> None:
        self.query_one("#log-main", RichLog).write(message)

    def log_task(self, message: Any, index: int) -> None:
        with suppress(Exception):
            self.query_one(f"#log-task-{index}", RichLog).write(message)

    def update_task_state(self, index: int, new_status: str) -> None:
        self.tasks[index].status = new_status  # type: ignore
        list_view = self.query_one("#task_list", ListView)

        with suppress(Exception):
            task_nodes = list_view.query(TaskItem).nodes
            if index < len(task_nodes):
                task_nodes[index].status = new_status

        if new_status == "running" and list_view.index in [None, 0, index]:
            list_view.index = index + 1

        if new_status in ("success", "failed", "skipped"):
            self.progress.advance(1)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if item is None:
            return
        switcher = self.query_one("#log_switcher", ContentSwitcher)
        if isinstance(item, MainLogItem):
            switcher.current = "log-main"
        elif isinstance(item, TaskItem):
            switcher.current = f"log-task-{item.task_index}"

    async def ping_sudo(self) -> None:
        with suppress(Exception):
            proc = await asyncio.create_subprocess_exec(
                'sudo', '-n', '-v', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()

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

    async def execute_pipeline(self) -> None:
        if not OPT_SKIP_SYNC:
            self.log_main(f"\n[bold {THEME['accent']}]═══ Phase 1: Git Architecture Reconciliation ═══[/]\n")
            git_engine = GitEngine(self, self.profile)
            if not await git_engine.execute_phase():
                self.abort_flag = True
                self.log_main(f"\n[bold {THEME['error']} blink]SYSTEM HALTED. GIT INTEGRITY VIOLATION.[/]")
                for index in range(5, len(self.tasks)):
                    self.update_task_state(index, "skipped")
                return
        else:
            self.log_main(f"\n[bold {THEME['accent']}]═══ Phase 1: Git Architecture Reconciliation (SKIPPED) ═══[/]\n")
            for index in range(5):
                self.update_task_state(index, "skipped")

        if OPT_SYNC_ONLY:
            self.log_main(f"\n[bold {THEME['success']}]SYNC COMPLETE. (--sync-only specified)[/]")
            return

        self.log_main(f"\n[bold {THEME['accent']}]═══ Phase 2: Configuration Pipeline Execution ═══[/]\n")

        success_count, fail_count = 0, 0

        for index in range(5, len(self.tasks)):
            if self.abort_flag:
                self.update_task_state(index, "skipped")
                continue

            task = self.tasks[index]
            self.update_task_state(index, "running")

            cmd_str = f"{task.name} {' '.join(task.args)}".strip()
            self.log_main(f"\n[bold {THEME['warning']}]>[/] Executing Process: [bold {THEME['fg']}]{escape(cmd_str)}[/]")
            self.log_task(f"[bold {THEME['accent']}]>>> PROCESS INITIATED:[/] {escape(cmd_str)}\n", index)

            if task.resolved_path and task.path_state == "ok" and task.resolved_path.is_file():
                resolved_path: Optional[Path] = task.resolved_path
            else:
                resolved_path = None
                for d in self.profile.search_dirs:
                    candidate = WORK_TREE / d / task.name
                    if candidate.is_file():
                        resolved_path = candidate
                        break

            if not resolved_path:
                err = f"[bold {THEME['error']}][ERROR][/] Architecture File Missing: {escape(task.name)}"
                self.log_main(err)
                self.log_task(err, index)
                self.update_task_state(index, "failed")
                fail_count += 1
                if not task.ignore_fail or OPT_STOP_ON_FAIL:
                    self.abort_flag = True
                continue

            interpreter = task.interpreter or []
            exec_cmd = interpreter + [str(resolved_path)] + task.args
            if not interpreter:
                exec_cmd = [str(resolved_path)] + task.args
            if task.mode == 'S':
                exec_cmd = ['sudo', '-n'] + exec_cmd

            try:
                if task.interactive:
                    self.log_main(f"[dim]Suspending UI abstraction... Passing raw PTY control...[/]")
                    self.log_task(f"[dim]Interactive flag detected. Console control delegated to user.[/]", index)

                    with self._suspend_ui():
                        r, g, b = get_rgb_color(THEME['accent'])
                        sys.stdout.write(f"\n\033[1;38;2;{r};{g};{b}m=== DUSKY INTERACTIVE ABSTRACTION: {task.name} ===\033[0m\n\n")
                        sys.stdout.flush()

                        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
                        try:
                            proc = await asyncio.create_subprocess_exec(*exec_cmd, cwd=str(WORK_TREE))
                            await proc.wait()
                            rc = proc.returncode
                        finally:
                            signal.signal(signal.SIGINT, old_int)

                        sys.stdout.write(f"\n\033[1;38;2;{r};{g};{b}m=== ABSTRACTION TERMINATED (Code: {rc}) ===\033[0m\n")
                        sys.stdout.flush()

                    self.log_task(f"\n[bold {THEME['success']}]PTY control returned. Exit Code: {rc}[/]", index)

                else:
                    proc = await asyncio.create_subprocess_exec(
                        *exec_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(WORK_TREE)
                    )
                    if proc.stdout:
                        buffer = ""
                        while True:
                            chunk = await proc.stdout.read(4096)
                            if not chunk:
                                break
                            buffer += chunk.decode('utf-8', errors='replace')
                            normalized = buffer.replace("\r\n", "\n").replace("\r", "\n")
                            if "\n" in normalized:
                                lines = normalized.split("\n")
                                for line in lines[:-1]:
                                    self.log_task(Text.from_ansi(line.rstrip()), index)
                                buffer = lines[-1]
                        if buffer:
                            self.log_task(Text.from_ansi(buffer.rstrip()), index)

                    await proc.wait()
                    rc = proc.returncode

                if rc == 0:
                    self.update_task_state(index, "success")
                    success_count += 1
                    self.log_main(f"[bold {THEME['success']}][OK][/] Process Complete.")
                    self.log_task(f"\n[bold {THEME['success']}]>>> EXECUTION SUCCESSFUL[/]", index)
                else:
                    if task.ignore_fail and not OPT_STOP_ON_FAIL:
                        self.update_task_state(index, "skipped")
                        self.log_main(f"[bold {THEME['warning']}][WARN][/] Process failure (Code {rc}) suppressed by manifest.")
                        self.log_task(f"\n[bold {THEME['warning']}]>>> EXECUTION FAILED / SUPPRESSED (Code {rc})[/]", index)
                    else:
                        self.update_task_state(index, "failed")
                        fail_count += 1
                        self.log_main(f"[bold {THEME['error']}][FATAL][/] Process aborted execution sequence (Code {rc}).")
                        self.log_task(f"\n[bold {THEME['error']}]>>> FATAL EXECUTION FAILURE (Code {rc})[/]", index)
                        self.abort_flag = True

            except Exception as e:
                err_msg = f"[bold {THEME['error']}][ERROR][/] Internal Exception: {escape(str(e))}"
                self.log_main(err_msg)
                self.log_task(err_msg, index)
                self.update_task_state(index, "failed")
                if not task.ignore_fail or OPT_STOP_ON_FAIL:
                    self.abort_flag = True

            await asyncio.sleep(0.01)

        self.log_main(f"\n[bold {THEME['accent']}]═══════ Pipeline Summary ═══════[/]")
        self.log_main(f"  Successful Deployments : [bold {THEME['success']}]{success_count}[/]")
        self.log_main(f"  Failed Operations      : [bold {THEME['error']}]{fail_count}[/]")

        if self.abort_flag:
            self.log_main(f"\n[bold {THEME['error']} blink]SYSTEM PIPELINE ABORTED.[/]")
            desktop_notify("Dusky Update", f"{fail_count} required script(s) failed", urgency="critical")
        else:
            self.log_main(f"\n[bold {THEME['success']}]ARCHITECTURE DEPLOYMENT COMPLETED.[/]")
            desktop_notify("Dusky Update", "Update completed successfully", urgency="normal")

        self.log_main("\n[dim]Press 'Ctrl+C' or 'Q' to terminate abstraction shell.[/dim]")

    def action_quit(self) -> None:
        self.abort_flag = True
        self.exit()


if __name__ == "__main__":
    try:
        args = parse_args()
        profile = load_profile(OPT_PROFILE_NAME)
        tasks = parse_manifest(profile)
        profile.tasks = tasks
        has_sudo = SUDO_ALREADY_ACQUIRED

        if args.list:
            list_active_scripts(profile)

        if not OPT_SYNC_ONLY and not OPT_DRY_RUN:
            if not has_sudo and any(t.mode == 'S' for t in tasks):
                if not verify_sudo():
                    sys.exit(1)
                has_sudo = True

        setup_storage_roots()
        setup_runtime_dir()
        if not acquire_lock():
            sys.exit(1)

        auto_prune()

        if not OPT_SYNC_ONLY:
            if not resolve_and_validate_manifest(profile, tasks):
                sys.stderr.write(
                    "\033[1;31m[FATAL]\033[0m Pre-flight validation failed. "
                    "Resolve the above errors and re-run.\n"
                )
                sys.exit(1)

        setup_logging()

        app = DuskyApp(profile, tasks, has_sudo)
        app.run()

    except KeyboardInterrupt:
        sys.stdout.write("\n\033[1;33m[WARN]\033[0m User interrupt detected. Terminating.\n")
        sys.exit(130)
