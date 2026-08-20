"""Live TUI dashboard for Dusky Keylogger (Textual 8.x).

Today / week / month / all-time cards, an hourly sparkline, a top-keys
bar chart, and a live feed of recent events. SQLite reads run on a
Textual worker thread so the UI thread never blocks on WAL I/O.
"""

from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import containers, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Digits, Footer, Header, Label, Sparkline, Static

from .stats import card_totals, hourly_series, summarize
from .storage import EventRow, KeyStore

REFRESH_SECONDS = 2.0
BAR_WIDTH = 28
MAX_TOP_KEYS = 12
MAX_EVENT_ROWS = 14

CARDS = [
    ("today", "Today"),
    ("week", "This Week"),
    ("month", "This Month"),
    ("all", "All Time"),
]


def _bar_chart(entries: list[tuple[str, int]], width: int = BAR_WIDTH) -> Text:
    max_count = max((c for _, c in entries), default=0) or 1
    lines: list[Text] = []
    for name, count in entries:
        fraction = count / max_count
        filled = round(fraction * width)
        display_name = f"{name[:14]:<14}"
        line = Text(f"{display_name}{count:>7}  ", style="bold #88c0d0")
        line.append("█" * filled, style="#5e81ac")
        line.append("░" * (width - filled), style="grey23")
        lines.append(line)
    return Text("\n").join(lines)


class DuskyDashboard(App):
    TITLE = "Dusky Keylogger"
    SUB_TITLE = "live keystroke analytics"

    BINDINGS = [
        Binding("r", "refresh_now", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #cards {
        height: 7;
    }
    #cards Horizontal {
        height: 7;
    }
    .card {
        width: 1fr;
        height: 7;
        border: round $border-blurred;
        background: $surface;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    .card Label {
        color: $text-muted;
        text-style: bold;
    }
    .card Digits {
        color: $accent;
        height: auto;
    }
    #charts {
        height: 13;
        margin-top: 1;
    }
    .panel {
        width: 1fr;
        height: 13;
        border: round $border-blurred;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    .panel-title {
        color: $text-muted;
        text-style: bold;
        height: 1;
    }
    #activity {
        height: 3;
    }
    #topkeys {
        height: 9;
    }
    #recent-panel {
        height: 1fr;
        margin-top: 1;
        border: round $border-blurred;
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
    }
    """

    def __init__(self, db_path: str | Path, refresh: float = REFRESH_SECONDS) -> None:
        super().__init__()
        # Ensure DB exists so first run doesn't show empty error; init is idempotent.
        store = KeyStore(db_path)
        if not store.path.exists():
            try:
                store.init_db()
            except Exception:
                pass
        self._store = store
        self._refresh = max(0.5, float(refresh))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with containers.Horizontal(id="cards"):
            for key, label in CARDS:
                with containers.Vertical(classes="card"):
                    yield Label(label)
                    yield Digits("0", id=f"digits-{key}")
        with containers.Horizontal(id="charts"):
            with containers.Vertical(classes="panel"):
                yield Label("Activity today (by hour)", classes="panel-title")
                yield Sparkline([], summary_function=max, id="activity")
                yield Static("", id="activity-legend")
            with containers.Vertical(classes="panel"):
                yield Label("Most used keys", classes="panel-title")
                with containers.VerticalScroll(id="topkeys", can_focus=True):
                    yield Static("", id="topkeys-content")
        with containers.Vertical(id="recent-panel"):
            yield Label("Recent events", classes="panel-title")
            yield DataTable(id="recent")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#recent", DataTable)
        table.add_columns("Time", "Key", "Char", "Kind", "Device")
        self.set_interval(self._refresh, self.refresh_data)
        self.refresh_data()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        try:
            cards = card_totals(self._store)
            series = [float(c) for _, c in hourly_series(self._store)]
            today = summarize(self._store, "today", limit_keys=MAX_TOP_KEYS)
            recent = self._store.recent(MAX_EVENT_ROWS)
        except Exception as exc:
            # SQLite busy or missing table on fresh install -- don't spam, just notify once.
            self.call_from_thread(self.notify, f"DB read failed: {exc}", severity="warning")
            return
        try:
            self.call_from_thread(self._apply, cards, series, today.top_keys, recent)
        except Exception:
            # App may be closing while worker still runs.
            pass

    def _apply(
        self,
        cards: dict[str, int],
        series: list[float],
        top_keys: list[tuple[str, int]],
        recent: list[EventRow],
    ) -> None:
        for key, value in cards.items():
            self.query_one(f"#digits-{key}", Digits).update(f"{value:,}")

        self.query_one("#activity", Sparkline).data = series
        if series:
            peak = max(series)
            hour = series.index(peak)
            self.query_one("#activity-legend", Static).update(
                f"peak: {hour:02d}:00 ({peak:,.0f} keys)"
            )
        else:
            self.query_one("#activity-legend", Static).update("no activity yet")

        self.query_one("#topkeys-content", Static).update(
            _bar_chart(top_keys) if top_keys else Text("no keys logged yet")
        )

        table = self.query_one("#recent", DataTable)
        table.clear()
        for row in recent:
            dt = datetime.fromtimestamp(row.ts_ms / 1000).strftime("%H:%M:%S")
            table.add_row(
                dt,
                row.key_name,
                row.char if row.char is not None else "",
                row.kind,
                row.device,
            )

    def action_refresh_now(self) -> None:
        self.refresh_data()
        self.notify("Refreshed")


def main(db_path: str | Path) -> None:
    DuskyDashboard(db_path).run()
