#!/usr/bin/env python3
"""
===============================================================================
DUSKY SCREENTIME: MATUGEN THEMED TEXTUAL TUI (Python 3.14 Bleeding-Edge)
===============================================================================
Clean, simple screentime dashboard.
Features:
- Application names resolved via `.desktop` files (matching Rofi behavior)
- Dynamic Matugen color integration (~/.config/matugen/generated/dusky_tui.json)
- Simple period switching (Today, Yesterday, Week, Month, All Time)
- Interactive scrolling & cursor navigation
- Modal inspection of window title breakdown (`Enter` on any row)
- Sleek fuzzy search app jump (`Ctrl+F` or `/`)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from desktop_resolver import AppInfo, DesktopResolver
except ImportError:
    from python.desktop_resolver import AppInfo, DesktopResolver

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, OptionList

DATA_FILE = Path("~/.local/share/dusky/screentime/screentime_data.json").expanduser()
THEME_FILE = Path("~/.config/matugen/generated/dusky_tui.json").expanduser()


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


def make_bar(percent: float, width: int = 18) -> str:
    filled = int(round((percent / 100.0) * width))
    filled = max(0, min(width, filled))
    empty = width - filled
    return "█" * filled + "░" * empty


class AppDetailModal(ModalScreen[None]):
    """
    Detailed modal popup inspecting an app's window title breakdown, total duration, sessions, and share.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("enter", "dismiss", "Close"),
    ]

    def __init__(
        self,
        app_class: str,
        app_info: dict[str, Any],
        total_screentime: int,
        colors: dict[str, str],
    ) -> None:
        super().__init__()
        self.app_class = app_class
        self.app_info = app_info
        self.total_screentime = total_screentime
        self.theme_colors = colors

    def compose(self) -> ComposeResult:
        name = self.app_info.get("name", self.app_class)
        cat = self.app_info.get("category", "Application")
        dur = self.app_info.get("duration", 0)
        sessions = self.app_info.get("sessions", 1)
        share = (
            (dur / self.total_screentime * 100.0)
            if self.total_screentime > 0
            else 0.0
        )

        accent_clr = self.theme_colors.get("accent", "#b2d189")
        success_clr = self.theme_colors.get("success", "#a0d0cb")
        fg_clr = self.theme_colors.get("fg", "#e2e3d8")

        with Container(id="detail-backdrop"):
            with Vertical(id="detail-box"):
                yield Label(
                    f"[bold {accent_clr}]{name}[/] [dim]({cat})[/]", id="detail-title"
                )
                yield Label(
                    f"Total Time: [bold {success_clr}]{format_duration(dur)}[/]  |  Share: [bold {accent_clr}]{share:.1f}%[/]  |  Focus Sessions: [bold {fg_clr}]{sessions}[/]",
                    id="detail-subtitle",
                )
                yield Label(
                    "[bold]Window Title Breakdown:[/bold]", id="detail-section-title"
                )
                table = DataTable(id="detail-table")
                table.cursor_type = "row"
                table.add_column("Duration", key="dur", width=14)
                table.add_column("Window Title / Document", key="title", width=None)

                titles_sorted = sorted(
                    self.app_info.get("titles", {}).items(),
                    key=lambda x: x[1],
                    reverse=True,
                )
                if titles_sorted:
                    for t_title, t_dur in titles_sorted:
                        table.add_row(format_duration(t_dur), t_title)
                else:
                    table.add_row("-", "No detailed window titles recorded")

                yield table
                with Horizontal(id="detail-footer"):
                    yield Button("Close (Esc/Enter)", id="btn-detail-close")

    def on_mount(self) -> None:
        self.query_one("#detail-table", DataTable).focus()

    @on(Button.Pressed, "#btn-detail-close")
    def on_close(self) -> None:
        self.dismiss(None)


class SearchAppModal(ModalScreen[int | None]):
    """
    Sleek fuzzy search popup to quickly find and jump to an application in the table.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("down", "cursor_down", "Down"),
        Binding("up", "cursor_up", "Up"),
    ]

    def __init__(
        self, apps_list: list[tuple[str, dict[str, Any]]], colors: dict[str, str]
    ) -> None:
        super().__init__()
        self.apps_list = apps_list
        self.theme_colors = colors
        self.filtered_indices: list[int] = []

    def compose(self) -> ComposeResult:
        with Container(id="search-backdrop"):
            with Vertical(id="search-box"):
                yield Label(
                    f"[bold {self.theme_colors.get('accent', '#b2d189')}]Search & Jump to Application (Esc to close)[/]",
                    id="search-title",
                )
                yield Input(
                    placeholder="Type app name or category...", id="search-input"
                )
                yield OptionList(id="search-options")
                with Horizontal(id="search-footer"):
                    yield Button("Cancel (Esc)", id="btn-search-close")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()
        self._filter_options("")

    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    @on(Input.Changed, "#search-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        self._filter_options(event.value)

    def _filter_options(self, query: str) -> None:
        ol = self.query_one("#search-options", OptionList)
        ol.clear_options()
        self.filtered_indices = []

        q = query.lower().strip()
        from textual.widgets.option_list import Option

        for orig_idx, (cls, info) in enumerate(self.apps_list):
            name = info.get("name", cls)
            cat = info.get("category", "Application")
            dur = format_duration(info.get("duration", 0))

            if not q or q in name.lower() or q in cls.lower() or q in cat.lower():
                txt = Text()
                txt.append(
                    f"{name} ", style=f"bold {self.theme_colors.get('fg', '#e2e3d8')}"
                )
                txt.append(
                    f"— {dur}",
                    style=f"bold {self.theme_colors.get('success', '#a0d0cb')}",
                )
                ol.add_option(Option(txt, id=f"opt_{orig_idx}"))
                self.filtered_indices.append(orig_idx)

    @on(OptionList.OptionSelected, "#search-options")
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index is not None and event.option_index < len(
            self.filtered_indices
        ):
            self.dismiss(self.filtered_indices[event.option_index])

    @on(Input.Submitted, "#search-input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        ol = self.query_one("#search-options", OptionList)
        if ol.highlighted is not None and ol.highlighted < len(self.filtered_indices):
            self.dismiss(self.filtered_indices[ol.highlighted])
        elif self.filtered_indices:
            self.dismiss(self.filtered_indices[0])

    @on(Button.Pressed, "#btn-search-close")
    def on_cancel(self) -> None:
        self.dismiss(None)


class ScreentimeTUI(App[None]):
    """
    Simple, humanized Screentime TUI.
    """

    BASE_CSS = """
    Screen {
        background: $surface;
        color: $foreground;
    }

    #top-header {
        height: 3;
        padding: 0 2;
        margin: 1 2 1 2;
        background: $surface;
        border: solid $secondary;
        align: left middle;
    }

    #lbl-total-time {
        width: 1fr;
        content-align: left middle;
    }

    #lbl-stats {
        width: auto;
        content-align: right middle;
    }

    #table-wrapper {
        height: auto;
        max-height: 1fr;
        padding: 0 2 1 2;
    }

    DataTable {
        height: auto;
        max-height: 1fr;
        background: $background;
        color: $foreground;
        border: solid $secondary;
    }

    DataTable > .datatable--header {
        background: $surface;
        color: $primary;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $success 25%;
        color: $foreground;
        text-style: bold;
    }

    DataTable > .datatable--highlight {
        background: $success 20%;
        color: $foreground;
        text-style: bold;
    }

    #search-backdrop, #detail-backdrop {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }

    #search-box {
        width: 84;
        height: 24;
        background: $background;
        border: heavy $primary;
        padding: 1 2;
    }

    #detail-box {
        width: 96;
        height: 28;
        background: $background;
        border: heavy $primary;
        padding: 1 2;
    }

    #search-title, #detail-title {
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }

    #detail-subtitle {
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }

    #detail-section-title {
        margin-top: 1;
        margin-bottom: 1;
    }

    #detail-table {
        height: 1fr;
        background: $surface;
        border: solid $secondary;
    }

    #search-input {
        margin-bottom: 1;
        background: $surface;
        color: $foreground;
        border: solid $secondary;
    }

    #search-options {
        height: 1fr;
        background: $background;
        border: solid $secondary;
    }

    #search-footer, #detail-footer {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    #btn-search-close, #btn-detail-close {
        background: $primary;
        color: $background;
        border: none;
    }
    """

    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+f", "search_apps", "Search", show=True),
        Binding("/", "search_apps", "Search", show=False),
        Binding("enter", "inspect_app", "Details", show=True),
        Binding("1", "select_range('today')", "Today", show=True),
        Binding("2", "select_range('yesterday')", "Yesterday", show=True),
        Binding("3", "select_range('week')", "Week", show=True),
        Binding("4", "select_range('month')", "Month", show=True),
        Binding("5", "select_range('all')", "All Time", show=True),
    ]

    current_range = reactive("today")

    def __init__(self) -> None:
        super().__init__()
        self.resolver: DesktopResolver = DesktopResolver()
        self.raw_data: dict[str, dict[str, Any]] = {}
        self.active_class: str = ""
        self.active_title: str = ""
        self.aggregated_apps: dict[str, dict[str, Any]] = {}

        self.theme_colors: dict[str, str] = {
            "bg": "#12140e",
            "fg": "#e2e3d8",
            "accent": "#b2d189",
            "error": "#ffb4ab",
            "warning": "#c0cbac",
            "success": "#a0d0cb",
            "muted": "#44483d",
        }
        self.last_theme_mtime: float = 0.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="top-header"):
            yield Label("", id="lbl-total-time")
            yield Label("", id="lbl-stats")

        with Container(id="table-wrapper"):
            yield DataTable(id="app-table")

        yield Footer()

    def apply_matugen_theme(self) -> None:
        self._theme_toggle = not getattr(self, "_theme_toggle", False)
        theme_name = "matugen_live_A" if self._theme_toggle else "matugen_live_B"

        custom_theme = Theme(
            name=theme_name,
            primary=self.theme_colors.get("accent", "#b2d189"),
            secondary=self.theme_colors.get("muted", "#44483d"),
            background=self.theme_colors.get("bg", "#12140e"),
            surface=self.theme_colors.get("bg", "#12140e"),
            warning=self.theme_colors.get("warning", "#c0cbac"),
            error=self.theme_colors.get("error", "#ffb4ab"),
            success=self.theme_colors.get("success", "#a0d0cb"),
            variables={
                "foreground": self.theme_colors.get("fg", "#e2e3d8"),
            },
        )
        self.register_theme(custom_theme)
        self.theme = theme_name

    async def watch_theme_file(self) -> None:
        if not THEME_FILE.exists():
            return
        try:
            stat_info = await asyncio.to_thread(THEME_FILE.stat)
            current_mtime = stat_info.st_mtime
            if current_mtime > self.last_theme_mtime:
                self.last_theme_mtime = current_mtime

                def _load_json() -> dict[str, Any]:
                    with open(THEME_FILE, "r", encoding="utf-8") as f:
                        return json.load(f)

                try:
                    new_theme = await asyncio.to_thread(_load_json)
                    self.theme_colors.update(new_theme)
                    self.apply_matugen_theme()
                    self._update_display(reset_cursor=False)
                except Exception:
                    pass
        except OSError:
            pass

    def on_mount(self) -> None:
        self.title = "Dusky Screentime"

        if THEME_FILE.exists():
            try:
                with open(THEME_FILE, "r", encoding="utf-8") as f:
                    self.theme_colors.update(json.load(f))
                self.last_theme_mtime = THEME_FILE.stat().st_mtime
            except Exception:
                pass

        self.apply_matugen_theme()

        table = self.query_one("#app-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Status", key="status", width=12)
        table.add_column("Application", key="app", width=None)
        table.add_column("Time", key="duration", width=16)
        table.add_column("Share", key="share", width=12)
        table.add_column("Bar", key="bar", width=22)

        self._load_data()
        self._update_display(reset_cursor=True)
        table.focus()

        self.set_interval(1.0, self._tick_refresh)
        self.set_interval(0.5, self.watch_theme_file)

    def _tick_refresh(self) -> None:
        self._load_data()
        self._update_display(reset_cursor=False)

    def _load_data(self) -> None:
        if DATA_FILE.exists():
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    self.raw_data = json.load(f)
            except Exception:
                pass

        self._get_live_active_class()

    def _get_live_active_class(self) -> None:
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
                import socket as _socket

                with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                    s.settimeout(0.3)
                    s.connect(str(sock_path))
                    s.sendall(b"j/activewindow")
                    resp = s.recv(4096).decode("utf-8", errors="ignore")
                    data = json.loads(resp)
                    self.active_class = data.get("class", "").strip()
                    self.active_title = data.get("title", "").strip()
                    return
            except Exception:
                pass

        self.active_class = ""
        self.active_title = ""

    def _aggregate_by_range(self) -> tuple[dict[str, dict[str, Any]], int]:
        today_date = datetime.now()
        today_str = today_date.strftime("%Y-%m-%d")
        yesterday_str = (today_date - timedelta(days=1)).strftime("%Y-%m-%d")

        target_days: list[str] = []
        match self.current_range:
            case "today":
                target_days = [today_str]
            case "yesterday":
                target_days = [yesterday_str]
            case "week":
                target_days = [
                    (today_date - timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in range(7)
                ]
            case "month":
                target_days = [
                    (today_date - timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in range(30)
                ]
            case _:
                target_days = list(self.raw_data.keys())

        agg: dict[str, dict[str, Any]] = {}
        total_time = 0

        for day in target_days:
            if day not in self.raw_data:
                continue
            for cls, info in self.raw_data[day].items():
                dur = info.get("duration", 0)
                if dur <= 0:
                    continue
                if cls not in agg:
                    agg[cls] = {
                        "name": info.get("name", cls),
                        "category": info.get("category", "Application"),
                        "icon": info.get("icon", ""),
                        "duration": 0,
                        "sessions": 0,
                        "titles": {},
                    }
                agg[cls]["duration"] += dur
                agg[cls]["sessions"] += info.get("sessions", 1)
                total_time += dur

                for t_title, t_dur in info.get("titles", {}).items():
                    agg[cls]["titles"][t_title] = (
                        agg[cls]["titles"].get(t_title, 0) + t_dur
                    )

        return agg, total_time

    def _update_display(self, reset_cursor: bool = False) -> None:
        table = self.query_one("#app-table", DataTable)
        saved_row = table.cursor_coordinate.row if table.row_count > 0 and not reset_cursor else 0

        self.aggregated_apps, total_time = self._aggregate_by_range()

        range_names = {
            "today": "Today",
            "yesterday": "Yesterday",
            "week": "Past 7 Days",
            "month": "Past 30 Days",
            "all": "All Time",
        }
        r_name = range_names.get(self.current_range, "Today")

        accent_clr = self.theme_colors.get("accent", "#b2d189")
        success_clr = self.theme_colors.get("success", "#a0d0cb")
        warning_clr = self.theme_colors.get("warning", "#c0cbac")
        fg_clr = self.theme_colors.get("fg", "#e2e3d8")
        muted_clr = self.theme_colors.get("muted", "#44483d")

        lbl_total_time = self.query_one("#lbl-total-time", Label)
        lbl_total_time.update(
            f"Total Time: [bold {success_clr}]{format_duration(total_time)}[/]  [dim {muted_clr}]({r_name})[/]"
        )

        lbl_stats = self.query_one("#lbl-stats", Label)
        lbl_stats.update(f"Tracked: [bold {fg_clr}]{len(self.aggregated_apps)}[/] apps")

        sorted_apps = sorted(
            self.aggregated_apps.items(), key=lambda x: x[1]["duration"], reverse=True
        )

        table.clear(columns=False)
        max_dur = sorted_apps[0][1]["duration"] if sorted_apps else 1

        for idx, (cls, info) in enumerate(sorted_apps):
            dur = info["duration"]
            share = (dur / total_time * 100.0) if total_time > 0 else 0.0

            is_active = (
                cls.lower() == self.active_class.lower() and self.active_class != ""
            )
            if is_active:
                status_txt = Text("▶ ACTIVE", style=f"bold reverse {success_clr}")
            else:
                status_txt = Text("  idle", style=f"dim {muted_clr}")

            app_name = info.get("name", cls)
            app_cat = info.get("category", "Application")

            app_txt = Text()
            if is_active:
                app_txt.append(app_name, style=f"bold underline {success_clr}")
                app_txt.append(f"  ({app_cat})", style=f"bold {success_clr}")
            else:
                app_txt.append(
                    app_name, style=f"bold {fg_clr}" if idx == 0 else f"{fg_clr}"
                )
                app_txt.append(f"  ({app_cat})", style=f"dim {warning_clr}")

            dur_txt = Text(
                format_duration(dur),
                style=f"bold {success_clr}"
                if is_active or idx == 0
                else f"{success_clr}",
            )
            share_txt = Text(
                f"{share:.1f}%",
                style=f"bold {success_clr}" if is_active else f"{accent_clr}",
            )

            bar_str = make_bar((dur / max_dur) * 100.0, width=20)
            bar_clr = (
                success_clr if is_active else (accent_clr if idx == 0 else warning_clr)
            )
            bar_txt = Text(bar_str, style=f"bold {bar_clr}")

            table.add_row(status_txt, app_txt, dur_txt, share_txt, bar_txt, key=cls)

        total_count = len(sorted_apps)
        if total_count > 0:
            target_row = max(0, min(saved_row, total_count - 1))
            try:
                table.move_cursor(row=target_row, column=0)
            except Exception:
                pass
            curr_idx = target_row + 1
        else:
            curr_idx = 0

        table.border_subtitle = (
            f" {curr_idx}/{total_count} " if total_count > 0 else " 0/0 "
        )

    def action_select_range(self, range_key: str) -> None:
        self.current_range = range_key
        self._update_display(reset_cursor=True)

    def action_search_apps(self) -> None:
        sorted_apps = sorted(
            self.aggregated_apps.items(), key=lambda x: x[1]["duration"], reverse=True
        )
        if not sorted_apps:
            return

        def _on_search_result(row_idx: int | None) -> None:
            if row_idx is not None:
                table = self.query_one("#app-table", DataTable)
                try:
                    table.move_cursor(row=row_idx, column=0)
                except Exception:
                    pass

        self.push_screen(
            SearchAppModal(sorted_apps, self.theme_colors), _on_search_result
        )

    def action_inspect_app(self) -> None:
        table = self.query_one("#app-table", DataTable)
        if table.row_count == 0 or table.cursor_coordinate.row < 0:
            return

        sorted_apps = sorted(
            self.aggregated_apps.items(), key=lambda x: x[1]["duration"], reverse=True
        )
        row_idx = table.cursor_coordinate.row
        if row_idx < len(sorted_apps):
            cls, info = sorted_apps[row_idx]
            _, total_time = self._aggregate_by_range()
            self.push_screen(
                AppDetailModal(cls, info, total_time, self.theme_colors)
            )

    @on(DataTable.RowSelected, "#app-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_inspect_app()


if __name__ == "__main__":
    app = ScreentimeTUI()
    app.run()
