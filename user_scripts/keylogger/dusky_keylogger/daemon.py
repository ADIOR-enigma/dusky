"""Always-on Dusky Keylogger daemon.

Wires the evdev KeyListener to SQLite via a dedicated writer thread:
the asyncio loop never issues a blocking sqlite3 call. Designed to run
under systemd Type=simple, stopping cleanly on SIGINT/SIGTERM.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from . import __version__
from .listener import KeyListener, KeyPress
from .storage import EventRow, EventWriter, KeyStore, row_from_press

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_INTERVAL = 0.5
MAX_BUFFER = 256


def default_data_dir() -> Path:
    override = os.environ.get("DUSKY_KEYLOGGER_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "dusky-keylogger"


def default_config_path() -> Path:
    return Path.home() / ".config" / "dusky-keylogger" / "config.json"


DEFAULT_CONFIG: dict = {
    "flush_interval": DEFAULT_FLUSH_INTERVAL,
    "log_level": "info",
}


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or default_config_path()
    config = dict(DEFAULT_CONFIG)
    try:
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as fh:
                config.update(json.load(fh))
        else:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            with cfg_path.open("w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=2)
                fh.write("\n")
    except (OSError, ValueError) as exc:
        logger.warning("Could not read config %s: %s", cfg_path, exc)
    return config


def sd_notify(message: str) -> None:
    """Send a systemd notification (READY / WATCHDOG / STOPPING / STATUS).

    No-op when not running under systemd (NOTIFY_SOCKET unset).
    """
    raw = os.environ.get("NOTIFY_SOCKET")
    if not raw:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
        try:
            addr = "\0" + raw[1:] if raw.startswith("@") else raw
            sock.connect(addr)
            sock.send(message.encode())
        finally:
            sock.close()
    except OSError:
        logger.debug("sd_notify(%r) failed", message, exc_info=True)


def _setup_logging(level: str, data_dir: Path) -> None:
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    logging.basicConfig(
        level=level_map.get(str(level).lower(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    try:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_dir / "daemon.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-7s] %(message)s", "%Y-%m-%d %H:%M:%S"
            )
        )
        logging.getLogger().addHandler(file_handler)
    except OSError as exc:
        logger.warning("File logging disabled: %s", exc)


class Daemon:
    """Keystroke logging daemon. Event loop never blocks on SQLite."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        config: dict | None = None,
    ) -> None:
        self._data_dir = Path(data_dir) if data_dir else default_data_dir()
        self._config = config or load_config()
        self._store = KeyStore(self._data_dir / "keys.db")
        self._writer = EventWriter(self._store)
        self._listener: KeyListener | None = None
        self._buffer: list[EventRow] = []
        self._flush_interval = float(
            self._config.get("flush_interval", DEFAULT_FLUSH_INTERVAL)
        )
        self._stop = asyncio.Event()
        self._started_at = time.monotonic()

    def _handle_press(self, press: KeyPress) -> None:
        self._buffer.append(row_from_press(press))
        if len(self._buffer) >= MAX_BUFFER:
            self._kick_flush()

    def _kick_flush(self) -> None:
        if not self._buffer:
            return
        rows, self._buffer = self._buffer, []
        if not self._writer.submit(rows):
            logger.error(
                "Writer queue saturated -- holding %d events in memory",
                len(rows),
            )
            self._buffer = rows + self._buffer

    async def _flush_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._flush_interval
                    )
                except TimeoutError:
                    self._kick_flush()
                    err = self._writer.last_error
                    if err is not None:
                        logger.error("Writer error: %s", err)
        except asyncio.CancelledError:
            raise

    async def _watchdog_loop(self, interval: float) -> None:
        """Ping systemd's watchdog at half WatchdogSec when WATCHDOG_USEC is set."""
        while not self._stop.is_set():
            sd_notify("WATCHDOG=1")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def run(self) -> None:
        _setup_logging(self._config.get("log_level", "info"), self._data_dir)
        self._store.init_db()
        self._writer.start()

        listener = KeyListener()
        self._listener = listener
        listener.on_key = self._handle_press
        await listener.start()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        flush_task = asyncio.create_task(self._flush_loop(), name="dusky-flush")
        wd_usec = int(os.environ.get("WATCHDOG_USEC", "0"))
        wd_task: asyncio.Task | None = None
        if wd_usec > 0:
            wd_task = asyncio.create_task(
                self._watchdog_loop(wd_usec / 2_000_000), name="dusky-watchdog"
            )
        sd_notify(f"READY=1\nSTATUS=dusky v{__version__} listening\n")
        logger.info(
            "Dusky Keylogger v%s started (data: %s)",
            __version__,
            self._store.path,
        )
        try:
            await self._stop.wait()
        finally:
            sd_notify("STOPPING=1\nSTATUS=flushing\n")
            logger.info("Shutting down...")
            flush_task.cancel()
            if wd_task is not None:
                wd_task.cancel()
            await asyncio.gather(
                flush_task, *((wd_task,) if wd_task else ()), return_exceptions=True
            )
            await listener.stop()
            self._kick_flush()
            if self._buffer:
                # The writer queue was saturated, so the kick-flush was
                # rejected. We are shutting down -- a synchronous write is
                # acceptable now; never drop the final keystrokes.
                rows, self._buffer = self._buffer, []
                try:
                    self._store.insert_many(rows)
                except Exception:
                    logger.exception("Final flush failed (%d rows)", len(rows))
            self._writer.close(timeout=8.0)
            if self._writer.last_error is not None:
                logger.error("Writer finished with error: %s", self._writer.last_error)
            logger.info(
                "Shutdown complete: %d rows persisted, uptime %.1fs",
                self._writer.written,
                time.monotonic() - self._started_at,
            )

    async def stop(self) -> None:
        self._stop.set()
