#!/usr/bin/env python3
"""
===============================================================================
DUSKY SCREENTIME: MATUGEN THEMED RICH & FZF DASHBOARD (Python 3.14)
===============================================================================
Ultra-fast, lightweight screentime visualization engine featuring:
1. Full-bleed Live Rich Terminal Dashboard with locked bottom footer
2. Single-instance Live lifecycle (ZERO TTY termios deadlock on tab switch)
3. Instant 1..5 period tab switching in < 1ms
4. Strict TTY ECHO suppression (ZERO mouse/key/number leakage to stdout)
5. Robust ANSI sequence buffer parser for Arrow keys & Vim controls
6. Smart Yesterday fallback to most recent recorded date
7. Dynamic Matugen color integration (~/.config/matugen/generated/dusky_tui.json)

FIXES (Kitty freeze / threading.excepthook on 1..5):
- Live auto_refresh is OFF. Rich's _RefreshThread has no try/except; any render
  error killed the thread, FileProxy-wrapped stderr made threading.excepthook
  itself fail, and live.update() without refresh=True then never redrew.
- redirect_stdout/stderr disabled so exception hooks and the TTY stay intact.
- Main-thread-only live.update(..., refresh=True) with a hard try/except.
- No O_NONBLOCK on stdin (VMIN=0/VTIME=0 + select). Mixing O_NONBLOCK with
  VMIN=1 races Rich/Kitty and can raise in the refresh thread.
- Period keys never tear down Live or touch termios.
- Streaming ANSI parser + input buffer so split CSI / leftover bytes can't
  poison the next key.
- Cached JSON + Hyprland window so a tab switch does zero I/O.
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import sys
import termios
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure local imports work
SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from desktop_resolver import DesktopResolver
except ImportError:
    try:
        from python.desktop_resolver import AppInfo, DesktopResolver
    except ImportError:
        DesktopResolver = None  # type: ignore[misc, assignment]

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

DATA_FILE = Path("~/.local/share/dusky/screentime/screentime_data.json").expanduser()
THEME_FILE = Path("~/.config/matugen/generated/dusky_tui.json").expanduser()
LOG_FILE = Path("~/.local/share/dusky/screentime/screentime_error.log").expanduser()

DEFAULT_COLORS: dict[str, str] = {
    "bg": "#0e1416",
    "fg": "#dee3e5",
    "accent": "#82d3e2",
    "error": "#ffb4ab",
    "warning": "#b1cbd0",
    "success": "#bbc5ea",
    "muted": "#3f484a",
    "cursor_bg": "#1c2528",
}

PERIOD_KEYS: dict[str, str] = {
    "period_today": "today",
    "period_yesterday": "yesterday",
    "period_week": "week",
    "period_month": "month",
    "period_all": "all",
}

# Mouse / cursor control (written only when Live does not own the TTY)
_MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
_MOUSE_OFF = "\x1b[?1000l\x1b[?1006l"
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"
_CLEAR_HOME = "\x1b[2J\x1b[3J\x1b[H"


# =============================================================================
# LOGGING / THEME / DATA
# =============================================================================
def log_error(err_msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {err_msg}\n")
    except Exception:
        pass


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    """Never print thread crashes onto the Live TTY (that is the freeze)."""
    try:
        tb = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        name = getattr(args.thread, "name", "?")
        log_error(f"threading.excepthook in {name}: {args.exc_type} {args.exc_value}\n{tb}")
    except Exception:
        pass


threading.excepthook = _thread_excepthook


def _safe_color(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    v = value.strip()
    if not v or "[" in v or "]" in v or "\n" in v or "\x1b" in v:
        return fallback
    if v.startswith("#"):
        hexpart = v[1:]
        if len(hexpart) in (3, 6, 8) and all(c in "0123456789abcdefABCDEF" for c in hexpart):
            return v
        return fallback
    return v


def load_theme_colors() -> dict[str, str]:
    colors = DEFAULT_COLORS.copy()
    if THEME_FILE.exists():
        try:
            with open(THEME_FILE, "r", encoding="utf-8") as f:
                user_colors = json.load(f)
                if isinstance(user_colors, dict):
                    for key, fallback in DEFAULT_COLORS.items():
                        if key in user_colors:
                            colors[key] = _safe_color(user_colors[key], fallback)
                    for key, val in user_colors.items():
                        if key not in colors:
                            colors[key] = _safe_color(val, DEFAULT_COLORS["fg"])
        except Exception as e:
            log_error(f"load_theme_colors error: {e}")
    return colors


def load_screentime_data() -> dict[str, dict[str, Any]]:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            log_error(f"load_screentime_data error: {e}")
    return {}


def get_active_hypr_window() -> tuple[str, str]:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")

    sock_path = None
    if xdg_runtime and sig:
        p = Path(xdg_runtime) / "hypr" / sig / ".socket.sock"
        if p.exists():
            sock_path = p

    if not sock_path and xdg_runtime:
        base_dir = Path(xdg_runtime) / "hypr"
        if base_dir.exists():
            try:
                for sdir in base_dir.iterdir():
                    sp = sdir / ".socket.sock"
                    if sp.exists():
                        sock_path = sp
                        break
            except Exception:
                sock_path = None

    if sock_path:
        try:
            import socket

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.05)
                s.connect(str(sock_path))
                s.sendall(b"j/activewindow")
                resp = s.recv(4096).decode("utf-8", errors="ignore")
                data = json.loads(resp)
                if isinstance(data, dict):
                    return str(data.get("class", "")).strip(), str(data.get("title", "")).strip()
        except Exception as e:
            log_error(f"get_active_hypr_window error: {e}")

    return "", ""


def simplify_category(cat: str) -> str:
    if not isinstance(cat, str):
        return "System"
    match cat:
        case "Terminal & Shell" | "TerminalEmulator":
            return "Terminal"
        case "Web Browser":
            return "Browser"
        case "Audio & Video" | "AudioVideo" | "Multimedia player":
            return "Media"
        case "Agentic Platform":
            return "AI"
        case "Development":
            return "Dev"
        case "Utilities" | "System" | "System Controls" | "System Settings":
            return "System"
        case "Virtual machine viewer/manager" | "Virtual Machine":
            return "VM"
        case _:
            if len(cat) > 15:
                return f"{cat[:12]}..."
            return cat


def format_duration(seconds: int) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "0s"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    match (h > 0, m > 0):
        case (True, _):
            return f"{h}h {m:02d}m {s:02d}s"
        case (False, True):
            return f"{m}m {s:02d}s"
        case _:
            return f"{s}s"


def make_bar_text(percent: float, colors: dict[str, str], is_active: bool, is_cursor: bool, width: int = 16) -> Text:
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round((percent / 100.0) * width))
    filled = max(0, min(width, filled))
    empty = width - filled

    accent = colors.get("accent", "#82d3e2")
    success = colors.get("success", "#bbc5ea")
    muted = colors.get("muted", "#3f484a")

    txt = Text()
    bar_color = success if is_active else accent
    txt.append("━" * filled, style=f"bold {bar_color}")
    txt.append("─" * empty, style=f"dim {muted}")
    return txt


def aggregate_by_range(
    raw_data: dict[str, dict[str, Any]], range_key: str
) -> tuple[dict[str, dict[str, Any]], int, str]:
    today_date = datetime.now()
    today_str = today_date.strftime("%Y-%m-%d")
    yesterday_str = (today_date - timedelta(days=1)).strftime("%Y-%m-%d")

    target_days: list[str] = []
    display_label = ""

    match range_key:
        case "today":
            target_days = [today_str]
            display_label = "Today"
        case "yesterday":
            if yesterday_str in raw_data:
                target_days = [yesterday_str]
                display_label = "Yesterday"
            else:
                past_dates = sorted([d for d in raw_data.keys() if d < today_str], reverse=True)
                if past_dates:
                    target_days = [past_dates[0]]
                    display_label = f"Yesterday ({past_dates[0]})"
                else:
                    target_days = [yesterday_str]
                    display_label = "Yesterday"
        case "week":
            target_days = [
                (today_date - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in range(7)
            ]
            display_label = "Past 7 Days"
        case "month":
            target_days = [
                (today_date - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in range(30)
            ]
            display_label = "Past 30 Days"
        case _:
            target_days = list(raw_data.keys())
            display_label = "All Time"

    agg: dict[str, dict[str, Any]] = {}
    total_time = 0

    for day in target_days:
        if day not in raw_data or not isinstance(raw_data[day], dict):
            continue
        for cls, info in raw_data[day].items():
            if not isinstance(info, dict):
                continue
            dur = info.get("duration", 0)
            if not isinstance(dur, (int, float)) or dur <= 0:
                continue
            dur = int(dur)
            if cls not in agg:
                agg[cls] = {
                    "name": str(info.get("name", cls)),
                    "category": str(info.get("category", "Application")),
                    "icon": str(info.get("icon", "")),
                    "duration": 0,
                    "sessions": 0,
                    "titles": {},
                }
            agg[cls]["duration"] += dur
            agg[cls]["sessions"] += int(info.get("sessions", 1))
            total_time += dur

            titles_dict = info.get("titles")
            if isinstance(titles_dict, dict):
                for t_title, t_dur in titles_dict.items():
                    if isinstance(t_dur, (int, float)):
                        agg[cls]["titles"][str(t_title)] = (
                            agg[cls]["titles"].get(str(t_title), 0) + int(t_dur)
                        )

    return agg, total_time, display_label


# =============================================================================
# COLOR-CODED FZF PREVIEW RENDERER
# =============================================================================
def render_fzf_preview(app_class: str, range_key: str = "today") -> None:
    console = Console()
    colors = load_theme_colors()
    raw_data = load_screentime_data()
    agg, total_time, _ = aggregate_by_range(raw_data, range_key)

    if app_class not in agg:
        console.print(f"[bold red]No screentime data found for class:[/] {app_class}")
        return

    info = agg[app_class]
    name = info.get("name", app_class)
    cat = simplify_category(info.get("category", "Application"))
    icon = info.get("icon", "")
    dur = info.get("duration", 0)
    sessions = info.get("sessions", 1)
    share = (dur / total_time * 100.0) if total_time > 0 else 0.0

    accent = colors.get("accent", "#82d3e2")
    success = colors.get("success", "#bbc5ea")
    fg = colors.get("fg", "#dee3e5")

    console.print(f"\n\033[1;38;5;81m:: \033[1;37m{name}\033[0m  \033[1;38;5;203m({cat})\033[0m")
    console.print(f"\033[38;5;242mClass:\033[0m \033[1;37m{app_class}\033[0m \033[38;5;238m│\033[0m \033[38;5;242mIcon:\033[0m \033[38;5;114m{icon}\033[0m")
    console.print("\033[38;5;238m────────────────────────────────────────────────────────\033[0m")
    console.print(
        f"Time: \033[1;38;5;114m{format_duration(dur)}\033[0m  \033[38;5;238m│\033[0m  Share: \033[1;38;5;220m{share:.1f}%\033[0m  \033[38;5;238m│\033[0m  Sessions: \033[1;38;5;81m{sessions}\033[0m"
    )
    console.print("\033[38;5;238m────────────────────────────────────────────────────────\033[0m")
    console.print("\n\033[1;38;5;220m󰏖 Window Title & Document Breakdown:\033[0m\n")

    table = Table(box=None, show_header=True, header_style=f"bold {accent}", expand=True)
    table.add_column("Duration", style=f"bold {success}", width=12, no_wrap=True)
    table.add_column("Window Title / Document", style=f"{fg}", no_wrap=True)

    titles_sorted = sorted(
        info.get("titles", {}).items(), key=lambda x: x[1], reverse=True
    )

    if titles_sorted:
        for t_title, t_dur in titles_sorted:
            table.add_row(format_duration(t_dur), str(t_title))
    else:
        table.add_row("-", "No detailed window titles recorded")

    console.print(table)


# =============================================================================
# COLOR-CODED INTERACTIVE FZF EXPLORER MODE
# =============================================================================
def run_fzf_explorer(range_key: str = "today") -> None:
    raw_data = load_screentime_data()
    agg, total_time, _ = aggregate_by_range(raw_data, range_key)
    colors = load_theme_colors()

    if not agg:
        print("[!] No screentime data available for the selected period.")
        return

    sorted_apps = sorted(agg.items(), key=lambda x: x[1]["duration"], reverse=True)

    lines = []
    for cls, info in sorted_apps:
        dur = info["duration"]
        share = (dur / total_time * 100.0) if total_time > 0 else 0.0
        name = info.get("name", cls)
        cat = simplify_category(info.get("category", "Application"))

        disp = f"\033[1;38;5;81m{name:<26}\033[0m \033[38;5;238m│\033[0m \033[1;38;5;114m{format_duration(dur):<10}\033[0m \033[38;5;238m│\033[0m \033[1;38;5;220m{share:5.1f}%\033[0m \033[38;5;238m│\033[0m \033[38;5;246m{cat:<12}\033[0m \033[38;5;238m│\033[0m {cls}"
        lines.append(disp)

    script_path = sys.argv[0]
    preview_cmd = f'python3 "{script_path}" --preview {{5}} {range_key}'

    visual_header = f" \033[1;37m{'APPLICATION':<26}\033[0m \033[38;5;238m│\033[0m \033[1;37m{'TIME':<10}\033[0m \033[38;5;238m│\033[0m \033[1;37m{'SHARE':<6}\033[0m \033[38;5;238m│\033[0m \033[1;37m{'CATEGORY':<12}\033[0m"

    fzf_cmd = [
        "fzf",
        "--ansi",
        "--delimiter=│",
        "--with-nth=1,2,3,4",
        "--no-hscroll",
        "--highlight-line",
        "--prompt= 󱎫 Screentime ❯ ",
        "--pointer=❯ ",
        "--marker=✔ ",
        "--layout=reverse",
        "--border=rounded",
        "--border-label= 󱎫 Dusky Screentime Explorer [Alt+C: Copy Summary] ",
        "--border-label-pos=3",
        "--info=hidden",
        f"--header={visual_header}",
        "--header-first",
        f"--color=bg+:{colors.get('muted', '#3f484a')},bg:{colors.get('bg', '#0e1416')},spinner:{colors.get('accent', '#82d3e2')}",
        f"--color=fg:{colors.get('fg', '#dee3e5')},fg+:{colors.get('fg', '#dee3e5')},header:{colors.get('accent', '#82d3e2')},info:{colors.get('accent', '#82d3e2')}",
        f"--color=pointer:{colors.get('success', '#bbc5ea')},marker:{colors.get('success', '#bbc5ea')},prompt:{colors.get('accent', '#82d3e2')}",
        f"--color=hl:{colors.get('accent', '#82d3e2')},hl+:{colors.get('accent', '#82d3e2')},border:{colors.get('muted', '#3f484a')},label:{colors.get('accent', '#82d3e2')}",
        f"--preview={preview_cmd}",
        "--preview-window=right,50%,border-left,wrap",
        "--bind=alt-c:execute-silent(echo {1} {2} | wl-copy)+change-prompt( 󱎫 Copied Summary! ❯ )",
    ]

    input_data = "\n".join(lines).encode("utf-8")
    try:
        proc = subprocess.run(fzf_cmd, input=input_data, capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            _ = proc.stdout.decode("utf-8").strip()
    except Exception as e:
        log_error(f"fzf error: {e}")


# =============================================================================
# LIVE RICH TERMINAL DASHBOARD
# =============================================================================
def render_dashboard_layout(
    range_key: str,
    colors: dict[str, str],
    scroll_offset: int,
    cursor_idx: int,
    console_height: int,
    raw_data: dict[str, dict[str, Any]] | None = None,
    active_window: tuple[str, str] | None = None,
) -> tuple[Panel, int, int, int]:
    try:
        return _render_dashboard_layout_impl(
            range_key, colors, scroll_offset, cursor_idx, console_height, raw_data, active_window
        )
    except Exception as e:
        log_error(f"render_dashboard_layout error: {e}\n{traceback.format_exc()}")
        err = Panel(
            Text(f"Render error (see log). Period={range_key}: {e}", style="bold red"),
            border_style="red",
            expand=True,
            height=max(8, console_height or 24),
        )
        return err, scroll_offset, cursor_idx, 0


def _render_dashboard_layout_impl(
    range_key: str,
    colors: dict[str, str],
    scroll_offset: int,
    cursor_idx: int,
    console_height: int,
    raw_data: dict[str, dict[str, Any]] | None,
    active_window: tuple[str, str] | None,
) -> tuple[Panel, int, int, int]:
    if raw_data is None:
        raw_data = load_screentime_data()
    agg, total_time, r_name = aggregate_by_range(raw_data, range_key)
    if active_window is None:
        active_cls, active_title = get_active_hypr_window()
    else:
        active_cls, active_title = active_window

    console_height = max(8, int(console_height or 24))

    accent = colors.get("accent", "#82d3e2")
    success = colors.get("success", "#bbc5ea")
    warning = colors.get("warning", "#b1cbd0")
    fg = colors.get("fg", "#dee3e5")
    muted = colors.get("muted", "#3f484a")
    cursor_bg = colors.get("cursor_bg", "#1c2528")

    # Period tab indicator bar
    period_tabs = Text(overflow="ellipsis", no_wrap=True)
    period_tabs.append("  ", style=f"dim {muted}")
    tab_defs = [
        ("1", "today", "Today"),
        ("2", "yesterday", "Yesterday"),
        ("3", "week", "7 Days"),
        ("4", "month", "30 Days"),
        ("5", "all", "All Time"),
    ]
    for key_num, key_id, label in tab_defs:
        if key_id == range_key:
            period_tabs.append(f" {key_num}:{label} ", style=f"bold {accent} on {cursor_bg}")
        else:
            period_tabs.append(f" {key_num}:{label} ", style=f"dim {fg}")
        period_tabs.append(" ", style=f"dim {muted}")

    header_text = Text(overflow="ellipsis", no_wrap=True)
    header_text.append(" 󱎫 Dusky Screentime ", style=f"bold {accent}")
    header_text.append(f"({r_name})", style=f"bold {warning}")
    header_text.append("  Total: ", style=f"{fg}")
    header_text.append(f"{format_duration(total_time)}", style=f"bold {success}")
    header_text.append("  Apps: ", style=f"{fg}")
    header_text.append(f"{len(agg)}", style=f"bold {fg}")

    if active_cls:
        header_text.append("   ▶ ACTIVE: ", style=f"bold {success}")
        header_text.append(f"{active_cls}", style=f"bold {success}")
    else:
        header_text.append("   ▶ ACTIVE: ", style=f"dim {muted}")
        header_text.append("idle", style=f"dim {muted}")

    table = Table(box=None, expand=True, show_header=True, header_style=f"bold {accent}")
    table.add_column("", width=2, justify="center", no_wrap=True)
    table.add_column("Application & Category", ratio=3, no_wrap=True)
    table.add_column("Time", width=12, justify="right", no_wrap=True)
    table.add_column("Share", width=8, justify="right", no_wrap=True)
    table.add_column("Usage Bar", ratio=2, no_wrap=True)
    table.add_column("", width=1, justify="center", no_wrap=True)

    sorted_apps = sorted(agg.items(), key=lambda x: x[1]["duration"], reverse=True)
    total_apps = len(sorted_apps)

    if sorted_apps and sorted_apps[0][1]["duration"] > 0:
        max_dur = max(1, sorted_apps[0][1]["duration"])
    else:
        max_dur = 1

    visible_rows = max(3, console_height - 7)
    max_scroll = max(0, total_apps - visible_rows)

    if total_apps > 0:
        cursor_idx = max(0, min(cursor_idx, total_apps - 1))
        if cursor_idx < scroll_offset:
            scroll_offset = cursor_idx
        elif cursor_idx >= scroll_offset + visible_rows:
            scroll_offset = cursor_idx - visible_rows + 1
    else:
        cursor_idx = 0
        scroll_offset = 0

    scroll_offset = max(0, min(scroll_offset, max_scroll))
    page_apps = sorted_apps[scroll_offset : scroll_offset + visible_rows]

    if total_apps > visible_rows:
        thumb_h = max(1, int(round((visible_rows / total_apps) * visible_rows)))
        max_thumb_top = max(0, visible_rows - thumb_h)
        if max_scroll > 0:
            thumb_top = int(round((scroll_offset / max_scroll) * max_thumb_top))
        else:
            thumb_top = 0
    else:
        thumb_h = visible_rows
        thumb_top = 0

    for idx_in_page, (cls, info) in enumerate(page_apps):
        global_idx = scroll_offset + idx_in_page
        dur = info["duration"]
        share = (dur / total_time * 100.0) if total_time > 0 else 0.0
        is_active = (cls.lower() == active_cls.lower() and active_cls != "")
        is_cursor = (global_idx == cursor_idx)

        if is_active:
            status_cell = Text("●", style=f"bold {success}")
        elif is_cursor:
            status_cell = Text("▸", style=f"bold {accent}")
        else:
            status_cell = Text("·", style=f"dim {muted}")

        app_name = info.get("name", cls)
        cat = simplify_category(info.get("category", "Application"))

        bg_style = f"on {cursor_bg}" if is_cursor else ""

        app_cell = Text()
        if is_active:
            app_cell.append(f"{app_name}", style=f"bold {success}")
            app_cell.append(f"  ({cat})", style=f"dim {success}")
        elif is_cursor:
            app_cell.append(f"{app_name}", style=f"bold {fg}")
            app_cell.append(f"  ({cat})", style=f"dim {warning}")
        else:
            app_cell.append(f"{app_name}", style=f"bold {fg}" if global_idx == 0 else f"{fg}")
            app_cell.append(f"  ({cat})", style=f"dim {warning}")

        dur_cell = Text(format_duration(dur), style=f"bold {success}" if is_active or is_cursor or global_idx == 0 else f"{success}")
        share_cell = Text(f"{share:.1f}%", style=f"bold {success}" if is_active else f"{accent}")
        bar_cell = make_bar_text((dur / max_dur) * 100.0, colors, is_active, is_cursor, width=16)

        if total_apps > visible_rows:
            if thumb_top <= idx_in_page < thumb_top + thumb_h:
                scroll_cell = Text("┃", style=f"bold {accent}")
            else:
                scroll_cell = Text("│", style=f"dim {muted}")
        else:
            scroll_cell = Text("")

        if bg_style:
            status_cell.stylize(bg_style)
            app_cell.stylize(bg_style)
            dur_cell.stylize(bg_style)
            share_cell.stylize(bg_style)
            bar_cell.stylize(bg_style)

        table.add_row(status_cell, app_cell, dur_cell, share_cell, bar_cell, scroll_cell)

    rows_rendered = len(page_apps)
    if rows_rendered < visible_rows:
        for _ in range(visible_rows - rows_rendered):
            table.add_row("", "", "", "", "", "")

    footer_text = Text(overflow="ellipsis", no_wrap=True)
    footer_text.append(" Controls: ", style=f"bold {muted}")
    footer_text.append("[1-5] Period   [j/k/↑/↓] Move   [Ctrl-D/Ctrl-U] Page   ", style=f"{fg}")
    footer_text.append("[Enter] Details   [F] FZF   [Q] Quit", style=f"bold {accent}")

    layout_group = Table.grid(expand=True)
    layout_group.add_row(period_tabs)
    layout_group.add_row(header_text)
    layout_group.add_row(table)
    layout_group.add_row(footer_text)

    if total_apps > visible_rows:
        subtitle_str = (
            f"[bold {accent}]Item {cursor_idx + 1} of {total_apps}[/] "
            f"[dim]({scroll_offset + 1}–{min(scroll_offset + visible_rows, total_apps)} visible | j/k/Wheel to scroll)[/dim]"
        )
    elif total_apps > 0:
        subtitle_str = f"[bold {accent}]Item {cursor_idx + 1} of {total_apps}[/] [dim](Live Dashboard)[/dim]"
    else:
        safe_name = str(r_name).replace("[", "").replace("]", "")
        subtitle_str = f"[bold {warning}]No screentime data recorded for {safe_name}[/]"

    panel = Panel(
        layout_group,
        border_style=f"{accent}",
        subtitle=subtitle_str,
        expand=True,
        height=console_height,
    )
    return panel, scroll_offset, cursor_idx, max_scroll


# =============================================================================
# TERMINAL / INPUT
# =============================================================================
def set_terminal_cbreak(fd: int) -> list[Any]:
    """Enter non-canonical, no-echo mode WITHOUT O_NONBLOCK.

    VMIN=0 / VTIME=0 makes os.read() return immediately with whatever is
    queued. Combined with select() this is the race-free pattern. The old
    VMIN=1 + O_NONBLOCK mix is undefined on many ttys (including Kitty) and
    is what blew up Rich's refresh thread via unexpected EAGAIN / short reads.
    """
    old_settings = termios.tcgetattr(fd)
    new_settings = termios.tcgetattr(fd)

    # iflag: no software flow control, no CR→NL (so Ctrl-S / Enter stay raw)
    new_settings[0] &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR)
    # lflag: no echo, non-canonical. Keep ISIG off so we handle Ctrl-C ourselves.
    new_settings[3] &= ~(
        termios.ECHO
        | termios.ECHOE
        | termios.ECHOK
        | termios.ECHONL
        | termios.ICANON
        | termios.IEXTEN
        | termios.ISIG
    )
    new_settings[6][termios.VMIN] = 0
    new_settings[6][termios.VTIME] = 0

    termios.tcsetattr(fd, termios.TCSAFLUSH, new_settings)
    return old_settings


def restore_terminal(fd: int, old_settings: list[Any], old_flags: int | None = None) -> None:
    try:
        sys.stdout.write(_MOUSE_OFF + _CURSOR_SHOW + _CLEAR_HOME)
        sys.stdout.flush()
    except Exception:
        pass
    if old_flags is not None:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        except Exception:
            pass
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception:
        pass


def _write_tty(data: str) -> None:
    try:
        sys.stdout.write(data)
        sys.stdout.flush()
    except Exception:
        pass


def parse_input_sequence(buf: bytes) -> tuple[str | None, int]:
    """Parse one command from the front of *buf*.

    Returns (command_or_None, bytes_consumed).
    consumed == 0 means the sequence is incomplete — wait for more bytes.
    Never consumes past the current command, so a burst like b'15j' is
    processed as three separate keys.
    """
    if not buf:
        return None, 0

    # ----- Escape / CSI / mouse / SS3 -----
    if buf[0] == 0x1B:
        if len(buf) == 1:
            return None, 0

        # SGR mouse: ESC [ < btn ; x ; y M/m
        if buf.startswith(b"\x1b[<"):
            for i in range(3, len(buf)):
                if buf[i] in (ord("M"), ord("m")):
                    try:
                        body = buf[3:i].decode("ascii", errors="ignore")
                        parts = body.split(";")
                        if parts and parts[0].isdigit():
                            b_code = int(parts[0])
                            if b_code == 64:
                                return "scroll_up", i + 1
                            if b_code == 65:
                                return "scroll_down", i + 1
                    except Exception:
                        pass
                    return None, i + 1
            return (None, 0) if len(buf) < 64 else (None, 1)

        # CSI: ESC [
        if buf[1] == ord("["):
            if len(buf) < 3:
                return None, 0
            # Walk to the final byte (0x40–0x7E) per ECMA-48
            for i in range(2, len(buf)):
                if 0x40 <= buf[i] <= 0x7E:
                    final = chr(buf[i])
                    inner = buf[2:i].decode("ascii", errors="ignore")
                    num = inner.split(";")[0] if inner else ""
                    if final == "A":
                        return "up", i + 1
                    if final == "B":
                        return "down", i + 1
                    if final == "H":
                        return "home", i + 1
                    if final == "F":
                        return "end", i + 1
                    if final == "~":
                        if num == "5":
                            return "page_up", i + 1
                        if num == "6":
                            return "page_down", i + 1
                        if num in {"1", "7"}:
                            return "home", i + 1
                        if num in {"4", "8"}:
                            return "end", i + 1
                    return None, i + 1
            return (None, 0) if len(buf) < 32 else (None, 1)

        # SS3: ESC O A  (application cursor keys)
        if buf[1] == ord("O"):
            if len(buf) < 3:
                return None, 0
            if buf[2] == ord("A"):
                return "up", 3
            if buf[2] == ord("B"):
                return "down", 3
            if buf[2] == ord("H"):
                return "home", 3
            if buf[2] == ord("F"):
                return "end", 3
            return None, 3

        # ESC + regular key (Alt+key) — consume both, ignore
        return None, 2

    ch = buf[:1]
    match ch:
        case b"q" | b"Q" | b"\x03":
            return "quit", 1
        case b"\r" | b"\n":
            return "details", 1
        case b"j" | b"s":
            return "down", 1
        case b"k" | b"w":
            return "up", 1
        case b"\x04":
            return "page_down", 1
        case b"\x15":
            return "page_up", 1
        case b"\x06":
            return "half_page_down", 1
        case b"\x02":
            return "half_page_up", 1
        case b"g":
            return "home", 1
        case b"G":
            return "end", 1
        case b"1":
            return "period_today", 1
        case b"2":
            return "period_yesterday", 1
        case b"3":
            return "period_week", 1
        case b"4":
            return "period_month", 1
        case b"5":
            return "period_all", 1
        case b"f" | b"/":
            return "fzf", 1
        case b"r":
            return "refresh", 1
        case _:
            return None, 1


class _DashCache:
    """In-memory snapshot so 1..5 never blocks on disk or Hyprland."""

    __slots__ = ("raw_data", "raw_ts", "active", "active_ts", "colors", "colors_ts")

    def __init__(self) -> None:
        self.raw_data: dict[str, dict[str, Any]] = {}
        self.raw_ts: float = 0.0
        self.active: tuple[str, str] = ("", "")
        self.active_ts: float = 0.0
        self.colors: dict[str, str] = DEFAULT_COLORS.copy()
        self.colors_ts: float = 0.0

    def reload(self, force: bool = False, now: float | None = None) -> None:
        import time as _time

        t = now if now is not None else _time.monotonic()
        if force or (t - self.raw_ts) >= 2.0:
            try:
                self.raw_data = load_screentime_data()
            except Exception as e:
                log_error(f"cache raw_data: {e}")
            self.raw_ts = t
        if force or (t - self.active_ts) >= 1.0:
            try:
                self.active = get_active_hypr_window()
            except Exception as e:
                log_error(f"cache active: {e}")
            self.active_ts = t
        if force or (t - self.colors_ts) >= 5.0:
            try:
                self.colors = load_theme_colors()
            except Exception as e:
                log_error(f"cache colors: {e}")
            self.colors_ts = t


def _pause_live(live: Live, fd: int, old_settings: list[Any]) -> None:
    """Leave alt-screen + cbreak so a child (fzf / input) can own the TTY."""
    try:
        live.stop()
    except Exception as e:
        log_error(f"live.stop: {e}")
    _write_tty(_MOUSE_OFF + _CURSOR_SHOW + _CLEAR_HOME)
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception as e:
        log_error(f"pause termios restore: {e}")


def _resume_live(live: Live, fd: int) -> None:
    try:
        set_terminal_cbreak(fd)
    except Exception as e:
        log_error(f"resume cbreak: {e}")
    _write_tty(_MOUSE_ON + _CURSOR_HIDE)
    try:
        live.start(refresh=True)
    except Exception as e:
        log_error(f"live.start: {e}")


def run_live_dashboard() -> None:
    console = Console(
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        soft_wrap=False,
    )

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print("[bold red]screentime_tui requires a real TTY.[/bold red]")
        return

    fd = sys.stdin.fileno()
    old_settings = set_terminal_cbreak(fd)

    # Enable SGR mouse wheel reporting once, before Live takes the alt screen.
    _write_tty(_CLEAR_HOME + _MOUSE_ON + _CURSOR_HIDE)

    cache = _DashCache()
    cache.reload(force=True)

    range_key = "today"
    scroll_offset = 0
    cursor_idx = 0

    def build_panel() -> Panel:
        nonlocal scroll_offset, cursor_idx
        panel, scroll_offset, cursor_idx, _max_scroll = render_dashboard_layout(
            range_key,
            cache.colors,
            scroll_offset,
            cursor_idx,
            console.height,
            raw_data=cache.raw_data,
            active_window=cache.active,
        )
        return panel

    def push_frame(live: Live) -> None:
        try:
            live.update(build_panel(), refresh=True)
        except Exception as e:
            log_error(f"live.update/refresh: {e}\n{traceback.format_exc()}")

    try:
        panel = build_panel()

        # Single Live instance. NO refresh thread. NO FileProxy.
        # The main loop is the only writer — this is what makes 1..5 crash-free.
        with Live(
            panel,
            console=console,
            screen=True,
            auto_refresh=False,
            redirect_stdout=False,
            redirect_stderr=False,
            transient=True,
            vertical_overflow="crop",
        ) as live:
            input_buf = bytearray()
            running = True

            while running:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                except InterruptedError:
                    ready = []

                got_keys = False
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        chunk = b""
                    except OSError as e:
                        log_error(f"os.read: {e}")
                        chunk = b""

                    if chunk:
                        input_buf.extend(chunk)
                        got_keys = True

                # Drain every complete command before painting once.
                while input_buf:
                    cmd, consumed = parse_input_sequence(bytes(input_buf))
                    if consumed <= 0:
                        # Incomplete ESC — drop if it sits too long, else wait.
                        if input_buf[0] == 0x1B and len(input_buf) > 48:
                            del input_buf[0]
                            continue
                        break
                    del input_buf[:consumed]

                    match cmd:
                        case "quit":
                            running = False
                            break
                        case "up":
                            cursor_idx = max(0, cursor_idx - 1)
                        case "down":
                            cursor_idx += 1
                        case "scroll_up":
                            cursor_idx = max(0, cursor_idx - 3)
                        case "scroll_down":
                            cursor_idx += 3
                        case "page_up":
                            cursor_idx = max(0, cursor_idx - 10)
                        case "page_down":
                            cursor_idx += 10
                        case "half_page_up":
                            cursor_idx = max(0, cursor_idx - 15)
                        case "half_page_down":
                            cursor_idx += 15
                        case "home":
                            cursor_idx = 0
                        case "end":
                            cursor_idx = 10**9
                        case "period_today" | "period_yesterday" | "period_week" | "period_month" | "period_all":
                            # Pure in-memory state flip. No Live restart. No termios.
                            range_key = PERIOD_KEYS[cmd]
                            cursor_idx = 0
                            scroll_offset = 0
                        case "details":
                            agg, _, _ = aggregate_by_range(cache.raw_data, range_key)
                            sorted_apps = sorted(agg.items(), key=lambda x: x[1]["duration"], reverse=True)
                            if sorted_apps and 0 <= cursor_idx < len(sorted_apps):
                                target_app = sorted_apps[cursor_idx][0]
                                _pause_live(live, fd, old_settings)
                                try:
                                    render_fzf_preview(target_app, range_key)
                                    print("\n[Press Enter to return to Dashboard...]")
                                    try:
                                        input()
                                    except EOFError:
                                        pass
                                finally:
                                    _resume_live(live, fd)
                                    cache.reload(force=True)
                        case "fzf":
                            _pause_live(live, fd, old_settings)
                            try:
                                run_fzf_explorer(range_key)
                            finally:
                                _resume_live(live, fd)
                                cache.reload(force=True)
                        case "refresh":
                            cache.reload(force=True)
                        case _:
                            pass

                if not running:
                    break

                # Idle tick: refresh cached live data. Key path: skip I/O.
                if not got_keys:
                    cache.reload(force=False)

                push_frame(live)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        log_error(f"run_live_dashboard unhandled exception: {e}\n{traceback.format_exc()}")
    finally:
        restore_terminal(fd, old_settings)
        try:
            console.print("[bold green]✔ Screentime Dashboard closed cleanly.[/bold green]")
        except Exception:
            pass


# =============================================================================
# CLI ENTRY POINT
# =============================================================================
def main() -> None:
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        match cmd:
            case "--preview":
                app_cls = sys.argv[2] if len(sys.argv) > 2 else ""
                r_key = sys.argv[3] if len(sys.argv) > 3 else "today"
                render_fzf_preview(app_cls, r_key)
                return
            case "--fzf" | "-i" | "fzf" | "explore":
                r_key = sys.argv[2] if len(sys.argv) > 2 else "today"
                run_fzf_explorer(r_key)
                return
            case "--help" | "-h":
                print("Usage: screentime_tui.py [OPTIONS]")
                print("  (no args)           Launch Python Rich Live Dashboard")
                print("  --fzf, -i           Launch Interactive FZF Explorer")
                print("  --preview CLS KEY   Render ANSI preview window for FZF")
                return
            case _:
                pass

    run_live_dashboard()


if __name__ == "__main__":
    main()
