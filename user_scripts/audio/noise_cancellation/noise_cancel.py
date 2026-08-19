#!/usr/bin/env python3
"""
Dusky Noise Cancellation - Modern GTK3 & Headless PipeWire AI Audio Filter
Target Specification: Arch Linux (Kernel 7.1+ / August 2026 Spec), Python 3.14.6+
Pure bleeding-edge implementation with zero legacy shims or backwards compatibility shims.

Architecture:
- Real-time PipeWire RNNoise deep-learning GRU neural network filter node
- Zero-latency IPC control over non-blocking FIFO
- Dynamic Material You / Matugen GTK3 theme styling
- Dual Mode: Full GTK3 GUI + instant Headless CLI for Hyprland keybindings
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final
import json
import os
import signal
import subprocess
import sys
import time

# August 2026 Bleeding-Edge Constant declarations
APP_ID: Final[str] = "org.dusky.noise-cancellation"
HOME_DIR: Final[Path] = Path.home()
STATE_DIR: Final[Path] = HOME_DIR / ".config" / "noise-cancel-gtk"
CONFIG_FILE: Final[Path] = STATE_DIR / "config.json"
FIFO_PATH: Final[Path] = STATE_DIR / "control.fifo"
PID_FILE: Final[Path] = STATE_DIR / "daemon.pid"

# Sandboxed environment for commands
COMMAND_ENV: Final[dict[str, str]] = os.environ.copy()
COMMAND_ENV["LC_ALL"] = "C.UTF-8"
COMMAND_ENV["LANG"] = "C.UTF-8"

# Sleek Dusky GTK3 CSS Styling using dynamic Matugen GTK Theme Tokens
DUSKY_CSS: Final[str] = """
window.panel-window {
    background-color: alpha(@theme_bg_color, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
}

* { outline: none; }
*:focus { outline: none; box-shadow: none; }

.header-title {
    font-size: 17px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: @theme_fg_color;
}

.header-subtitle-active {
    font-size: 12px;
    font-weight: 600;
    color: @theme_selected_bg_color;
}

.header-subtitle-inactive {
    font-size: 12px;
    font-weight: 500;
    color: alpha(@theme_fg_color, 0.5);
}

.section-label {
    font-size: 13px;
    font-weight: 700;
    color: alpha(@theme_fg_color, 0.9);
}

.value-label {
    font-size: 12px;
    font-weight: 600;
    color: @theme_selected_bg_color;
}

.device-combo {
    background-color: alpha(@theme_base_color, 0.6);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    padding: 2px 6px;
    color: @theme_fg_color;
}

scale trough {
    min-height: 6px;
    border-radius: 3px;
    background-color: alpha(@theme_fg_color, 0.12);
}

scale highlight {
    border-radius: 3px;
    background-color: @theme_selected_bg_color;
}

scale slider {
    min-width: 16px;
    min-height: 16px;
    border-radius: 8px;
    background-color: @theme_selected_bg_color;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.35);
}

switch.compact-switch {
    min-width: 44px;
    min-height: 22px;
}

switch.compact-switch slider {
    min-width: 20px;
    min-height: 20px;
}

.footer-info {
    font-size: 11px;
    font-weight: 500;
    color: alpha(@theme_fg_color, 0.4);
}
"""


@dataclass(slots=True)
class AudioConfig:
    enabled: bool = False
    aggressiveness: int = 70  # 0 to 100%
    volume: int = 100  # 0 to 200%
    monitor: bool = False
    source: str = "default"


def find_helper_binary() -> Path | None:
    candidates: list[Path] = [
        HOME_DIR / ".cache" / "ghelper" / "libs" / "ghelper-audio",
        HOME_DIR / "Documents" / "ghelper" / "audio-helper" / "ghelper-audio",
        Path("/opt/ghelper/ghelper-audio"),
        Path("/usr/local/bin/ghelper-audio"),
        Path("/usr/bin/ghelper-audio"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None


def load_config() -> AudioConfig:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return AudioConfig(
                    enabled=bool(data.get("enabled", False)),
                    aggressiveness=int(data.get("aggressiveness", 70)),
                    volume=int(data.get("volume", 100)),
                    monitor=bool(data.get("monitor", False)),
                    source=str(data.get("source", "default")),
                )
        except Exception:
            pass
    return AudioConfig()


def save_config(cfg: AudioConfig) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(cfg), f, indent=2)
    except Exception:
        pass


def get_daemon_pid() -> int | None:
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            if PID_FILE.exists():
                PID_FILE.unlink(missing_ok=True)
    return None


def send_daemon_cmd(cmd_str: str) -> bool:
    if not FIFO_PATH.exists():
        return False
    try:
        fd = os.open(str(FIFO_PATH), os.O_WRONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "w") as f:
            f.write(cmd_str.strip() + "\n")
            f.flush()
        return True
    except (OSError, IOError):
        return False


def enumerate_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = [("default", "Default System Microphone")]
    try:
        out = subprocess.check_output(
            ["pw-cli", "ls", "Node"],
            text=True,
            stderr=subprocess.DEVNULL,
            env=COMMAND_ENV,
        )
        blocks = out.split("\tid ")
        for b in blocks:
            if 'media.class = "Audio/Source"' in b:
                lines = b.split("\n")
                name: str | None = None
                desc: str | None = None
                for l in lines:
                    if 'node.name = "' in l:
                        name = l.split('"')[1]
                    if 'node.description = "' in l:
                        desc = l.split('"')[1]
                if (
                    name
                    and not name.startswith("ghelper-audio")
                    and not name.endswith(".monitor")
                ):
                    sources.append((name, desc or name))
    except Exception:
        pass
    return sources


def start_daemon(cfg: AudioConfig | None = None) -> bool:
    if cfg is None:
        cfg = load_config()

    pid = get_daemon_pid()
    if pid:
        apply_config_to_daemon(cfg)
        return True

    bin_path = find_helper_binary()
    if not bin_path:
        print("Error: ghelper-audio binary not found on system.", file=sys.stderr)
        return False

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FIFO_PATH.unlink(missing_ok=True)
    os.mkfifo(str(FIFO_PATH), 0o600)

    # Spawn daemon runner in background with clean process isolation
    py_code = f"""
import subprocess, os, sys

fifo_path = {repr(str(FIFO_PATH))}
bin_path = {repr(str(bin_path))}

proc = subprocess.Popen(
    [bin_path],
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    text=True
)

while proc.poll() is None:
    try:
        with open(fifo_path, "r") as f:
            for line in f:
                cmd = line.strip()
                if cmd == "QUIT":
                    proc.stdin.write("QUIT\\n")
                    proc.stdin.flush()
                    proc.terminate()
                    sys.exit(0)
                if cmd:
                    proc.stdin.write(cmd + "\\n")
                    proc.stdin.flush()
    except Exception:
        pass
"""

    daemon = subprocess.Popen(
        [sys.executable, "-c", py_code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=COMMAND_ENV,
    )

    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(daemon.pid))

    time.sleep(0.15)
    apply_config_to_daemon(cfg)
    return True


def stop_daemon() -> bool:
    pid = get_daemon_pid()
    if not pid:
        return False

    send_daemon_cmd("QUIT")
    time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    PID_FILE.unlink(missing_ok=True)
    FIFO_PATH.unlink(missing_ok=True)
    return True


def apply_config_to_daemon(cfg: AudioConfig) -> None:
    send_daemon_cmd(f"RNN {1 if cfg.enabled else 0}")
    send_daemon_cmd(f"AGG {cfg.aggressiveness * 10}")
    send_daemon_cmd(f"VOL {cfg.volume * 10}")
    send_daemon_cmd(f"MON {1 if cfg.monitor else 0}")
    send_daemon_cmd(f"SRC {cfg.source}")


# -----------------------------------------------------------------------------
#   GTK3 Interface (Dusky Style)
# -----------------------------------------------------------------------------
def run_gtk_app() -> None:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk

    cfg = load_config()

    # Load dynamic Matugen/GTK CSS Provider
    provider = Gtk.CssProvider()
    provider.load_from_data(DUSKY_CSS.encode("utf-8"))
    screen = Gdk.Screen.get_default()
    if screen:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    class NoiseCancelWindow(Gtk.Window):
        def __init__(self) -> None:
            super().__init__(title="Dusky Noise Cancellation")
            self.set_default_size(360, 400)
            self.set_border_width(18)
            self.set_position(Gtk.WindowPosition.CENTER)
            self.get_style_context().add_class("panel-window")

            self.cfg = cfg
            self.sources = enumerate_sources()

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            self.add(vbox)

            # Header / Toggle Box
            header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)

            title_lbl = Gtk.Label(label="AI Noise Cancellation", xalign=0)
            title_lbl.get_style_context().add_class("header-title")

            self.status_lbl = Gtk.Label(xalign=0)
            self.update_status_label()

            title_box.pack_start(title_lbl, False, False, 0)
            title_box.pack_start(self.status_lbl, False, False, 0)
            header_box.pack_start(title_box, True, True, 0)

            self.master_switch = Gtk.Switch()
            self.master_switch.set_valign(Gtk.Align.CENTER)
            self.master_switch.get_style_context().add_class("compact-switch")
            self.master_switch.set_active(self.cfg.enabled)
            self.master_switch.connect("notify::active", self.on_master_toggled)
            header_box.pack_end(self.master_switch, False, False, 0)

            vbox.pack_start(header_box, False, False, 0)
            vbox.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
            )

            # Input Device Selector
            src_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            src_lbl = Gtk.Label(label="Input Microphone", xalign=0)
            src_lbl.get_style_context().add_class("section-label")
            src_box.pack_start(src_lbl, False, False, 0)

            self.src_combo = Gtk.ComboBoxText()
            self.src_combo.get_style_context().add_class("device-combo")
            active_idx = 0
            for idx, (node, desc) in enumerate(self.sources):
                self.src_combo.append(node, desc)
                if node == self.cfg.source:
                    active_idx = idx
            self.src_combo.set_active(active_idx)
            self.src_combo.connect("changed", self.on_source_changed)
            src_box.pack_start(self.src_combo, False, False, 0)
            vbox.pack_start(src_box, False, False, 0)

            # Aggressiveness Slider
            agg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            agg_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            agg_lbl = Gtk.Label(label="Suppression Intensity", xalign=0)
            agg_lbl.get_style_context().add_class("section-label")
            self.agg_val_lbl = Gtk.Label(
                label=f"{self.cfg.aggressiveness}%", xalign=1
            )
            self.agg_val_lbl.get_style_context().add_class("value-label")
            agg_hdr.pack_start(agg_lbl, True, True, 0)
            agg_hdr.pack_end(self.agg_val_lbl, False, False, 0)
            agg_box.pack_start(agg_hdr, False, False, 0)

            self.agg_scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, 100, 5
            )
            self.agg_scale.set_value(self.cfg.aggressiveness)
            self.agg_scale.connect("value-changed", self.on_agg_changed)
            agg_box.pack_start(self.agg_scale, False, False, 0)
            vbox.pack_start(agg_box, False, False, 0)

            # Microphone Gain Slider
            vol_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vol_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            vol_lbl = Gtk.Label(label="Output Gain", xalign=0)
            vol_lbl.get_style_context().add_class("section-label")
            self.vol_val_lbl = Gtk.Label(label=f"{self.cfg.volume}%", xalign=1)
            self.vol_val_lbl.get_style_context().add_class("value-label")
            vol_hdr.pack_start(vol_lbl, True, True, 0)
            vol_hdr.pack_end(self.vol_val_lbl, False, False, 0)
            vol_box.pack_start(vol_hdr, False, False, 0)

            self.vol_scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, 200, 5
            )
            self.vol_scale.set_value(self.cfg.volume)
            self.vol_scale.connect("value-changed", self.on_vol_changed)
            vol_box.pack_start(self.vol_scale, False, False, 0)
            vbox.pack_start(vol_box, False, False, 0)

            # Loopback Monitor Checkbox
            mon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mon_lbl = Gtk.Label(label="Headphone Voice Monitor (Loopback)", xalign=0)
            mon_lbl.get_style_context().add_class("section-label")
            self.mon_check = Gtk.CheckButton()
            self.mon_check.set_active(self.cfg.monitor)
            self.mon_check.connect("toggled", self.on_monitor_toggled)
            mon_box.pack_start(mon_lbl, True, True, 0)
            mon_box.pack_end(self.mon_check, False, False, 0)
            vbox.pack_start(mon_box, False, False, 0)

            # Footer
            footer_lbl = Gtk.Label(
                label="Virtual Source: G-Helper Microphone (PipeWire RNNoise)",
                xalign=0.5,
            )
            footer_lbl.get_style_context().add_class("footer-info")
            vbox.pack_end(footer_lbl, False, False, 0)

            if self.cfg.enabled:
                start_daemon(self.cfg)

        def update_status_label(self) -> None:
            if self.cfg.enabled:
                self.status_lbl.set_text("Active (Neural Filtering ON)")
                self.status_lbl.get_style_context().remove_class(
                    "header-subtitle-inactive"
                )
                self.status_lbl.get_style_context().add_class("header-subtitle-active")
            else:
                self.status_lbl.set_text("Disabled (Direct Bypass)")
                self.status_lbl.get_style_context().remove_class(
                    "header-subtitle-active"
                )
                self.status_lbl.get_style_context().add_class(
                    "header-subtitle-inactive"
                )

        def on_master_toggled(self, switch: Gtk.Switch, _gparam: Any) -> None:
            active = switch.get_active()
            self.cfg.enabled = active
            save_config(self.cfg)
            self.update_status_label()
            if active:
                start_daemon(self.cfg)
            else:
                stop_daemon()

        def on_source_changed(self, combo: Gtk.ComboBoxText) -> None:
            node = combo.get_active_id()
            if node:
                self.cfg.source = node
                save_config(self.cfg)
                send_daemon_cmd(f"SRC {node}")

        def on_agg_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.aggressiveness = val
            self.agg_val_lbl.set_text(f"{val}%")
            save_config(self.cfg)
            send_daemon_cmd(f"AGG {val * 10}")

        def on_vol_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.volume = val
            self.vol_val_lbl.set_text(f"{val}%")
            save_config(self.cfg)
            send_daemon_cmd(f"VOL {val * 10}")

        def on_monitor_toggled(self, check: Gtk.CheckButton) -> None:
            active = check.get_active()
            self.cfg.monitor = active
            save_config(self.cfg)
            send_daemon_cmd(f"MON {1 if active else 0}")

    win = NoiseCancelWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


# -----------------------------------------------------------------------------
#   CLI Parser
# -----------------------------------------------------------------------------
def main() -> None:
    args = sys.argv[1:]
    cfg = load_config()

    if not args or args[0] in ("--gui", "-g"):
        run_gtk_app()
        return

    cmd = args[0].lower()

    if cmd in ("--on", "-1", "on"):
        cfg.enabled = True
        save_config(cfg)
        start_daemon(cfg)
        print("🎙️ AI Noise Cancellation turned ON.")
    elif cmd in ("--off", "-0", "off"):
        cfg.enabled = False
        save_config(cfg)
        stop_daemon()
        print("🔇 AI Noise Cancellation turned OFF.")
    elif cmd in ("--toggle", "-t", "toggle"):
        is_on = bool(get_daemon_pid())
        if is_on:
            cfg.enabled = False
            save_config(cfg)
            stop_daemon()
            print("🔇 AI Noise Cancellation turned OFF.")
        else:
            cfg.enabled = True
            save_config(cfg)
            start_daemon(cfg)
            print("🎙️ AI Noise Cancellation turned ON.")
    elif cmd in ("--status", "-s", "status"):
        pid = get_daemon_pid()
        if pid:
            print(
                f"ON (PID {pid}, Aggressiveness: {cfg.aggressiveness}%, Volume: {cfg.volume}%)"
            )
        else:
            print("OFF")
    elif cmd in ("--set-agg", "--agg") and len(args) > 1:
        try:
            val = int(args[1])
            val = max(0, min(100, val))
            cfg.aggressiveness = val
            save_config(cfg)
            send_daemon_cmd(f"AGG {val * 10}")
            print(f"Set Aggressiveness to {val}%")
        except ValueError:
            print("Invalid value for aggressiveness (0-100).", file=sys.stderr)
    elif cmd in ("--set-vol", "--vol") and len(args) > 1:
        try:
            val = int(args[1])
            val = max(0, min(200, val))
            cfg.volume = val
            save_config(cfg)
            send_daemon_cmd(f"VOL {val * 10}")
            print(f"Set Volume to {val}%")
        except ValueError:
            print("Invalid value for volume (0-200).", file=sys.stderr)
    elif cmd in ("--help", "-h"):
        print("""Usage: noise_cancel.py [COMMAND]

Commands:
  --gui, -g           Launch minimal GTK3 graphical control window (default)
  --toggle, -t        Toggle noise cancellation ON / OFF
  --on                Turn noise cancellation ON
  --off               Turn noise cancellation OFF
  --status, -s        Print current noise cancellation status
  --set-agg <0-100>   Set RNNoise suppression aggressiveness (0 to 100%)
  --set-vol <0-200>   Set microphone volume/gain (0 to 200%)
  --help, -h          Show this help message
""")
    else:
        print(f"Unknown command: {cmd}. Run with --help for usage.")


if __name__ == "__main__":
    main()
