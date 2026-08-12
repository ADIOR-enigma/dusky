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
"""

import os
import sys
import json
import time
import select
import termios
import tty
import fcntl
import traceback
import subprocess
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
    from python.desktop_resolver import AppInfo, DesktopResolver

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.style import Style

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


def log_error(err_msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {err_msg}\n")
    except Exception:
        pass


def load_theme_colors() -> dict[str, str]:
    colors = DEFAULT_COLORS.copy()
    if THEME_FILE.exists():
        try:
            with open(THEME_FILE, "r", encoding="utf-8") as f:
                user_colors = json.load(f)
                if isinstance(user_colors, dict):
                    colors.update(user_colors)
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
            for sdir in base_dir.iterdir():
                sp = sdir / ".socket.sock"
                if sp.exists():
                    sock_path = sp
                    break

    if sock_path:
        try:
            import socket

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
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
    warning = colors.get("warning", "#b1cbd0")
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
            selected_line = proc.stdout.decode("utf-8").strip()
    except Exception as e:
        log_error(f"fzf error: {e}")


# =============================================================================
# LIVE RICH TERMINAL DASHBOARD MODE WITH SINGLE-INSTANCE LIFECYCLE
# =============================================================================
def render_dashboard_layout(
    range_key: str, colors: dict[str, str], scroll_offset: int, cursor_idx: int, console_height: int
) -> tuple[Panel, int, int, int]:
    raw_data = load_screentime_data()
    agg, total_time, r_name = aggregate_by_range(raw_data, range_key)
    active_cls, active_title = get_active_hypr_window()

    accent = colors.get("accent", "#82d3e2")
    success = colors.get("success", "#bbc5ea")
    warning = colors.get("warning", "#b1cbd0")
    fg = colors.get("fg", "#dee3e5")
    muted = colors.get("muted", "#3f484a")
    cursor_bg = colors.get("cursor_bg", "#1c2528")

    # Clean Header Status Bar
    header_text = Text()
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

    # Table Setup
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

    visible_rows = max(3, console_height - 6)
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
            app_cell.append(f"  ({cat})", style=f"bold {accent}")
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

    footer_text = Text()
    footer_text.append(" Controls: ", style=f"bold {muted}")
    footer_text.append("[1-5] Period   [j/k/↑/↓] Move   [Ctrl-D/Ctrl-U] Page   ", style=f"{fg}")
    footer_text.append("[Enter] Details   [F] FZF   [Q] Quit", style=f"bold {accent}")

    layout_group = Table.grid(expand=True)
    layout_group.add_row(header_text)
    layout_group.add_row(table)
    layout_group.add_row(footer_text)

    if total_apps > visible_rows:
        subtitle_str = f"[bold {accent}]Item {cursor_idx + 1} of {total_apps}[/] [dim]({scroll_offset + 1}–{min(scroll_offset + visible_rows, total_apps)} visible | j/k/Wheel to scroll)[/dim]"
    elif total_apps > 0:
        subtitle_str = f"[bold {accent}]Item {cursor_idx + 1} of {total_apps}[/] [dim](Live Dashboard)[/dim]"
    else:
        subtitle_str = f"[bold {warning}]No screentime data recorded for {r_name}[/bold {warning}]"

    panel = Panel(
        layout_group,
        border_style=f"{accent}",
        subtitle=subtitle_str,
        expand=True,
        height=console_height,
    )
    return panel, scroll_offset, cursor_idx, max_scroll


def set_terminal_no_echo(fd: int) -> tuple[list[Any], int]:
    old_settings = termios.tcgetattr(fd)
    new_settings = termios.tcgetattr(fd)

    new_settings[3] = new_settings[3] & ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL | termios.ICANON)
    new_settings[6][termios.VMIN] = 1
    new_settings[6][termios.VTIME] = 0

    termios.tcsetattr(fd, termios.TCSANOW, new_settings)

    old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)

    return old_settings, old_flags


def restore_terminal(fd: int, old_settings: list[Any], old_flags: int) -> None:
    try:
        sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[?25h\x1b[2J\x1b[H")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
    except Exception:
        pass
    try:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
    except Exception:
        pass


def parse_input_sequence(chunk: bytes) -> tuple[str | None, int]:
    if not chunk:
        return None, 0

    if chunk.startswith(b"\x1b"):
        if b"[<" in chunk:
            try:
                m_str = chunk.decode("utf-8", errors="ignore")
                if "[<" in m_str:
                    m_packet = m_str[m_str.find("[<") + 2 :]
                    parts = m_packet.rstrip("mM").split(";")
                    if parts and parts[0].isdigit():
                        b_code = int(parts[0])
                        if b_code == 64:
                            return "scroll_up", len(chunk)
                        elif b_code == 65:
                            return "scroll_down", len(chunk)
            except Exception:
                pass
            return None, len(chunk)

        if chunk.startswith((b"\x1b[A", b"\x1bOA", b"\x1b[1;2A", b"\x1b[1;5A")):
            return "up", 3 if chunk.startswith(b"\x1b[A") else len(chunk)
        elif chunk.startswith((b"\x1b[B", b"\x1bOB", b"\x1b[1;2B", b"\x1b[1;5B")):
            return "down", 3 if chunk.startswith(b"\x1b[B") else len(chunk)
        elif chunk.startswith(b"\x1b[5~"):
            return "page_up", 4
        elif chunk.startswith(b"\x1b[6~"):
            return "page_down", 4
        elif chunk.startswith((b"\x1b[H", b"\x1b[1~")):
            return "home", len(chunk)
        elif chunk.startswith((b"\x1b[F", b"\x1b[4~")):
            return "end", len(chunk)

        return None, len(chunk)

    ch_byte = chunk[:1]
    match ch_byte:
        case b"q" | b"\x03":
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


def run_live_dashboard() -> None:
    console = Console()
    colors = load_theme_colors()
    range_key = "today"
    scroll_offset = 0
    cursor_idx = 0

    fd = sys.stdin.fileno()

    # Configure TTY mode ONCE at launch
    old_settings, old_flags = set_terminal_no_echo(fd)

    # Clear screen & enable SGR Mouse Wheel Reporting ONCE
    sys.stdout.write("\x1b[2J\x1b[3J\x1b[H\x1b[?1000h\x1b[?1006h")
    sys.stdout.flush()

    try:
        panel, scroll_offset, cursor_idx, max_scroll = render_dashboard_layout(
            range_key, colors, scroll_offset, cursor_idx, console.height
        )

        # Single Live context instance - NO RECREATION IN A LOOP
        with Live(panel, console=console, refresh_per_second=4, screen=True) as live:
            while True:
                r, _, _ = select.select([fd], [], [], 0.25)
                if r:
                    try:
                        chunk = os.read(fd, 1024)
                    except OSError:
                        chunk = b""

                    if chunk:
                        cmd, _ = parse_input_sequence(chunk)
                        match cmd:
                            case "quit":
                                break
                            case "up":
                                cursor_idx = max(0, cursor_idx - 1)
                            case "down":
                                cursor_idx = cursor_idx + 1
                            case "scroll_up":
                                cursor_idx = max(0, cursor_idx - 3)
                            case "scroll_down":
                                cursor_idx = cursor_idx + 3
                            case "page_up":
                                cursor_idx = max(0, cursor_idx - 10)
                            case "page_down":
                                cursor_idx = cursor_idx + 10
                            case "half_page_up":
                                cursor_idx = max(0, cursor_idx - 15)
                            case "half_page_down":
                                cursor_idx = cursor_idx + 15
                            case "home":
                                cursor_idx = 0
                            case "end":
                                cursor_idx = 999999
                            case "period_today":
                                range_key = "today"
                                cursor_idx = 0
                                scroll_offset = 0
                            case "period_yesterday":
                                range_key = "yesterday"
                                cursor_idx = 0
                                scroll_offset = 0
                            case "period_week":
                                range_key = "week"
                                cursor_idx = 0
                                scroll_offset = 0
                            case "period_month":
                                range_key = "month"
                                cursor_idx = 0
                                scroll_offset = 0
                            case "period_all":
                                range_key = "all"
                                cursor_idx = 0
                                scroll_offset = 0
                            case "details":
                                raw_data = load_screentime_data()
                                agg, _, _ = aggregate_by_range(raw_data, range_key)
                                sorted_apps = sorted(agg.items(), key=lambda x: x[1]["duration"], reverse=True)
                                if sorted_apps and cursor_idx < len(sorted_apps):
                                    target_app = sorted_apps[cursor_idx][0]
                                    
                                    live.stop()
                                    termios.tcsetattr(fd, termios.TCSANOW, old_settings)
                                    sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[2J\x1b[H")
                                    sys.stdout.flush()

                                    render_fzf_preview(target_app, range_key)
                                    print("\n[Press Enter to return to Dashboard...]")
                                    input()

                                    set_terminal_no_echo(fd)
                                    sys.stdout.write("\x1b[2J\x1b[3J\x1b[H\x1b[?1000h\x1b[?1006h")
                                    sys.stdout.flush()
                                    live.start()

                            case "fzf":
                                live.stop()
                                termios.tcsetattr(fd, termios.TCSANOW, old_settings)
                                sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[2J\x1b[H")
                                sys.stdout.flush()

                                run_fzf_explorer(range_key)

                                set_terminal_no_echo(fd)
                                sys.stdout.write("\x1b[2J\x1b[3J\x1b[H\x1b[?1000h\x1b[?1006h")
                                sys.stdout.flush()
                                live.start()

                            case "refresh":
                                colors = load_theme_colors()
                            case _:
                                pass

                # Re-render dashboard layout & update Live display instantly without tearing down Live
                panel, scroll_offset, cursor_idx, max_scroll = render_dashboard_layout(
                    range_key, colors, scroll_offset, cursor_idx, console.height
                )
                live.update(panel)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        log_error(f"run_live_dashboard unhandled exception: {e}\n{traceback.format_exc()}")
    finally:
        restore_terminal(fd, old_settings, old_flags)
        console.print("[bold green]✔ Screentime Dashboard closed cleanly.[/bold green]")


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
