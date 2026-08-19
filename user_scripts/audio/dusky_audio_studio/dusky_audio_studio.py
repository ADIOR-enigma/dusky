#!/usr/bin/env python3
"""
Dusky Audio Studio & Voice DSP - Comprehensive GTK3 & Headless Audio Processing Tool
Target Specification: Arch Linux (Kernel 7.1+ / August 2026 Spec), Python 3.14.6+
Pure bleeding-edge implementation with zero legacy shims or backwards compatibility shims.

Features:
- RNNoise Recurrent Neural Network Noise Suppression
- Granular Pitch Shifter (-24 to +24 semitones)
- 16-Band Robot Vocoder with Voice Pitch Tracking & Matrix Timbre
- Chromatic Autotune & Monotone Pitch Snap (DECtalk / T-Pain)
- Lo-Fi Vintage Bitcrusher (Bit depth & downsampling)
- Vocal Bandpass Filters (Telephone, Walkie-Talkie, Helmet Resonance)
- Rhythmic Stutter Gate Chopper (Cylon / Battlestar Galactica)
- Tape Delay & Echo (0 to 1000 ms)
- Freeverb Algorithmic Reverb Tank
- 9-Band Studio Parametric Equalizer
- Headphone Voice Loopback Monitoring
- Dual Mode: Full Multi-Tab GTK3 Interface + Instant CLI / Hyprland Keybindings
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final
import json
import os
import shutil
import signal
import subprocess
import sys
import time

# Constant declarations
APP_ID: Final[str] = "org.dusky.audio-studio"
HOME_DIR: Final[Path] = Path.home()
STATE_DIR: Final[Path] = HOME_DIR / ".config" / "dusky_audio_studio"
CONFIG_FILE: Final[Path] = STATE_DIR / "config.json"
FIFO_PATH: Final[Path] = STATE_DIR / "control.fifo"
PID_FILE: Final[Path] = STATE_DIR / "daemon.pid"
GUI_PID_FILE: Final[Path] = STATE_DIR / "gui.pid"

# Sandboxed environment
COMMAND_ENV: Final[dict[str, str]] = os.environ.copy()
COMMAND_ENV["LC_ALL"] = "C.UTF-8"
COMMAND_ENV["LANG"] = "C.UTF-8"

# Modern Dusky GTK3 CSS using dynamic Matugen GTK Theme Tokens
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
    font-size: 16px;
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
    font-size: 12px;
    font-weight: 700;
    color: alpha(@theme_fg_color, 0.9);
}

.value-label {
    font-size: 11px;
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

.preset-btn {
    border-radius: 8px;
    padding: 4px 10px;
    font-weight: 600;
    font-size: 11px;
    background-color: alpha(@theme_fg_color, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.05);
    color: @theme_fg_color;
    transition: background-color 150ms ease;
}

.preset-btn:hover {
    background-color: alpha(@theme_selected_bg_color, 0.25);
    color: @theme_selected_bg_color;
}

.preset-btn.active-preset {
    background-color: @theme_selected_bg_color;
    color: @theme_selected_fg_color;
}

notebook tab {
    padding: 6px 14px;
    font-weight: 700;
    font-size: 12px;
    border-bottom: 2px solid transparent;
}

notebook tab:checked {
    border-bottom: 2px solid @theme_selected_bg_color;
    color: @theme_selected_bg_color;
}

scale trough {
    min-height: 5px;
    border-radius: 3px;
    background-color: alpha(@theme_fg_color, 0.12);
}

scale highlight {
    border-radius: 3px;
    background-color: @theme_selected_bg_color;
}

scale slider {
    min-width: 14px;
    min-height: 14px;
    border-radius: 7px;
    background-color: @theme_selected_bg_color;
}

switch.compact-switch {
    min-width: 40px;
    min-height: 20px;
}

switch.compact-switch slider {
    min-width: 18px;
    min-height: 18px;
}

.footer-info {
    font-size: 11px;
    font-weight: 500;
    color: alpha(@theme_fg_color, 0.4);
}

.warning-banner {
    background-color: alpha(@theme_selected_bg_color, 0.12);
    border: 1px solid alpha(@theme_selected_bg_color, 0.35);
    border-radius: 8px;
    padding: 8px 12px;
}

.warning-text {
    font-size: 12px;
    font-weight: 600;
    color: @theme_selected_bg_color;
}
"""


@dataclass(slots=True)
class AudioConfig:
    # Master
    enabled: bool = False
    source: str = "default"
    volume: int = 100  # 0..200%
    monitor: bool = False

    # Noise Suppression
    rnnoise_on: bool = True
    aggressiveness: int = 70  # 0..100%

    # Vocoder & Voice Character
    vocoder_on: bool = False
    vocoder_mix: int = 70  # 0..100%
    vocoder_carrier_hz: int = 110  # 50..440 Hz
    vocoder_attack_ms: int = 5  # 1..100 ms
    vocoder_release_ms: int = 30  # 5..500 ms
    vocoder_detune: int = 20  # 0..200 per-mille
    vocoder_follow: bool = True
    vocoder_pitch_shift: int = 0  # -24..+24 semitones
    vocoder_matrix: int = 50  # 0..100%

    # Pitch & Modulation
    pitch_shift: int = 0  # -2400..+2400 centisemitones (-24..+24 st)
    autotune_on: bool = False
    autotune_target_hz: int = 0  # 0=chromatic, >0=monotone target
    bitcrush_bits: int = 0  # 0=bypass, 1..15
    bitcrush_downsample: int = 1  # 1..64
    bandpass_hpf_hz: int = 0  # 0..2000 Hz
    bandpass_lpf_hz: int = 0  # 0..20000 Hz
    stutter_hz: int = 0  # 0..40 Hz

    # Delay / Echo
    delay_on: bool = False
    delay_ms: int = 250  # 10..1000 ms
    delay_feedback: int = 35  # 0..95%
    delay_mix: int = 30  # 0..100%

    # Reverb
    reverb_on: bool = False
    reverb_room: int = 70  # 0..100%
    reverb_damp: int = 50  # 0..100%
    reverb_width: int = 80  # 0..100%
    reverb_mix: int = 35  # 0..100%

    # 9-Band EQ gains (-1200..+1200 centi-dB -> -12dB..+12dB)
    eq_on: bool = False
    eq_gains: list[int] = field(
        default_factory=lambda: [0, 300, 0, -200, 0, 300, 0, 200, 0]
    )


# Audio Presets
PRESETS: Final[dict[str, dict[str, Any]]] = {
    "Natural Clean": {
        "vocoder_on": False,
        "pitch_shift": 0,
        "autotune_on": False,
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 0,
        "bandpass_lpf_hz": 0,
        "stutter_hz": 0,
        "vocoder_matrix": 0,
    },
    "Daft Punk": {
        "vocoder_on": True,
        "vocoder_mix": 90,
        "vocoder_carrier_hz": 110,
        "vocoder_detune": 50,
        "vocoder_attack_ms": 2,
        "vocoder_release_ms": 12,
        "vocoder_follow": True,
        "vocoder_pitch_shift": 0,
        "pitch_shift": 0,
        "autotune_on": False,
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 0,
        "bandpass_lpf_hz": 0,
        "stutter_hz": 0,
        "vocoder_matrix": 15,
    },
    "Darth Vader": {
        "vocoder_on": False,
        "pitch_shift": -500,  # -5 semitones
        "autotune_on": False,
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 80,
        "bandpass_lpf_hz": 2500,
        "stutter_hz": 0,
        "vocoder_matrix": 0,
    },
    "Chipmunk": {
        "vocoder_on": False,
        "pitch_shift": 1200,  # +12 semitones (1 octave up)
        "autotune_on": False,
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 150,
        "bandpass_lpf_hz": 0,
        "stutter_hz": 0,
        "vocoder_matrix": 0,
    },
    "Cylon Robot": {
        "vocoder_on": True,
        "vocoder_mix": 90,
        "vocoder_carrier_hz": 90,
        "vocoder_detune": 160,
        "vocoder_attack_ms": 10,
        "vocoder_release_ms": 70,
        "vocoder_follow": False,
        "vocoder_pitch_shift": 0,
        "pitch_shift": 0,
        "autotune_on": False,
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 0,
        "bandpass_lpf_hz": 0,
        "stutter_hz": 6,
        "vocoder_matrix": 45,
    },
    "Kraftwerk": {
        "vocoder_on": True,
        "vocoder_mix": 95,
        "vocoder_carrier_hz": 140,
        "vocoder_detune": 10,
        "vocoder_attack_ms": 3,
        "vocoder_release_ms": 15,
        "vocoder_follow": False,
        "vocoder_pitch_shift": 0,
        "pitch_shift": 0,
        "autotune_on": False,
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 0,
        "bandpass_lpf_hz": 0,
        "stutter_hz": 0,
        "vocoder_matrix": 0,
    },
    "Matrix Agent": {
        "vocoder_on": True,
        "vocoder_mix": 90,
        "vocoder_carrier_hz": 70,
        "vocoder_detune": 90,
        "vocoder_attack_ms": 10,
        "vocoder_release_ms": 55,
        "vocoder_follow": False,
        "vocoder_pitch_shift": 0,
        "pitch_shift": -200,
        "autotune_on": False,
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 300,
        "bandpass_lpf_hz": 3400,
        "stutter_hz": 0,
        "vocoder_matrix": 100,
    },
    "Robot Phone": {
        "vocoder_on": False,
        "pitch_shift": 0,
        "autotune_on": False,
        "bitcrush_bits": 8,
        "bitcrush_downsample": 2,
        "bandpass_hpf_hz": 300,
        "bandpass_lpf_hz": 3400,
        "stutter_hz": 0,
        "vocoder_matrix": 0,
    },
    "T-Pain Autotune": {
        "vocoder_on": False,
        "pitch_shift": 0,
        "autotune_on": True,
        "autotune_target_hz": 0,  # chromatic snap
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 0,
        "bandpass_lpf_hz": 0,
        "stutter_hz": 0,
        "vocoder_matrix": 0,
    },
    "Stephen Hawking": {
        "vocoder_on": False,
        "pitch_shift": 0,
        "autotune_on": True,
        "autotune_target_hz": 120,  # monotone snap
        "bitcrush_bits": 0,
        "bitcrush_downsample": 1,
        "bandpass_hpf_hz": 0,
        "bandpass_lpf_hz": 0,
        "stutter_hz": 0,
        "vocoder_matrix": 0,
    },
}


def find_helper_binary() -> Path | None:
    candidates: list[Path] = [
        HOME_DIR / ".cache" / "dusky_audio_studio" / "dusky_audio_dsp",
        STATE_DIR / "dusky_audio_dsp",
        HOME_DIR / ".local" / "bin" / "dusky_audio_dsp",
        Path("/usr/local/bin/dusky_audio_dsp"),
        Path("/usr/bin/dusky_audio_dsp"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None


def send_desktop_notification(
    title: str, message: str, urgency: str = "critical"
) -> None:
    try:
        subprocess.run(
            [
                "notify-send",
                "-a",
                "Dusky Audio Studio",
                "-u",
                urgency,
                "-i",
                "audio-volume-high",
                title,
                message,
            ],
            stderr=subprocess.DEVNULL,
            env=COMMAND_ENV,
        )
    except Exception:
        pass


def check_system_dependencies() -> list[str]:
    missing: list[str] = []
    # 1. Check rnnoise system package / shared library
    if not (
        Path("/usr/lib/librnnoise.so").exists()
        or Path("/usr/lib/librnnoise.so.0").exists()
    ):
        missing.append(
            "Package 'rnnoise' is missing (fix with: sudo pacman -S rnnoise)"
        )

    # 2. Check PipeWire daemon / pw-cli
    if not shutil.which("pw-cli"):
        missing.append(
            "Package 'pipewire' is missing (fix with: sudo pacman -S pipewire wireplumber)"
        )

    # 3. Check DSP engine helper binary
    if not find_helper_binary():
        missing.append(
            "Dusky Audio DSP engine is missing (~/.cache/dusky_audio_studio/dusky_audio_dsp)"
        )

    return missing


def load_config() -> AudioConfig:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                cfg = AudioConfig()
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
                return cfg
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
    seen_names: set[str] = {"default"}
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
                is_virtual = False
                for l in lines:
                    if 'node.name = "' in l:
                        name = l.split('"')[1]
                    if 'node.description = "' in l:
                        desc = l.split('"')[1]
                    if (
                        'media.category = "Playback"' in l
                        or 'media.role = "Communication"' in l
                    ):
                        is_virtual = True

                if not name or is_virtual:
                    continue

                lower_name = name.lower()
                lower_desc = (desc or "").lower()
                if (
                    lower_name.startswith("ghelper")
                    or lower_name.startswith("dusky")
                    or lower_name.startswith("rnnoise")
                    or "noise suppressed" in lower_desc
                    or "audio monitor" in lower_desc
                    or "audio capture" in lower_desc
                    or lower_name.endswith(".monitor")
                ):
                    continue

                if name not in seen_names:
                    seen_names.add(name)
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

    # Pre-flight dependency check
    missing = check_system_dependencies()
    if missing:
        err_msg = (
            "Dusky Audio Studio cannot start due to missing dependencies:\n\n"
            + "\n".join(f"• {m}" for m in missing)
        )
        print(f"\n❌ [Dusky Audio Error]\n{err_msg}\n", file=sys.stderr)
        send_desktop_notification(
            "Dusky Audio Studio — Missing Dependency",
            "\n".join(f"• {m}" for m in missing),
        )
        return False

    bin_path = find_helper_binary()
    if not bin_path:
        return False

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FIFO_PATH.unlink(missing_ok=True)
    os.mkfifo(str(FIFO_PATH), 0o600)

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
    # 1. Master enable/disable
    send_daemon_cmd(f"SRC {cfg.source}")
    send_daemon_cmd(f"VOL {cfg.volume * 10}")
    send_daemon_cmd(f"MON {1 if cfg.monitor else 0}")

    # 2. RNNoise
    send_daemon_cmd(f"RNN {1 if (cfg.enabled and cfg.rnnoise_on) else 0}")
    send_daemon_cmd(f"AGG {cfg.aggressiveness * 10}")

    # 3. Vocoder & Pitch
    send_daemon_cmd(f"VOC {1 if (cfg.enabled and cfg.vocoder_on) else 0}")
    send_daemon_cmd(
        f"VOP {cfg.vocoder_mix * 10} {cfg.vocoder_carrier_hz} {cfg.vocoder_attack_ms} {cfg.vocoder_release_ms} {cfg.vocoder_detune} {1 if cfg.vocoder_follow else 0} {cfg.vocoder_pitch_shift}"
    )
    send_daemon_cmd(f"MTX {cfg.vocoder_matrix * 10}")
    send_daemon_cmd(f"PSH {cfg.pitch_shift if cfg.enabled else 0}")
    send_daemon_cmd(f"ATN {1 if (cfg.enabled and cfg.autotune_on) else 0}")
    send_daemon_cmd(f"ATT {cfg.autotune_target_hz}")
    send_daemon_cmd(
        f"BCR {cfg.bitcrush_bits if cfg.enabled else 0} {cfg.bitcrush_downsample}"
    )
    send_daemon_cmd(
        f"BPF {cfg.bandpass_hpf_hz if cfg.enabled else 0} {cfg.bandpass_lpf_hz if cfg.enabled else 0}"
    )
    send_daemon_cmd(
        f"STT {cfg.stutter_hz if cfg.enabled else 0} 500"
    )  # 50% duty cycle

    # 4. Delay
    send_daemon_cmd(f"DLY {1 if (cfg.enabled and cfg.delay_on) else 0}")
    send_daemon_cmd(
        f"DLP {cfg.delay_ms} {cfg.delay_feedback * 10} {cfg.delay_mix * 10}"
    )

    # 5. Reverb
    send_daemon_cmd(f"RVB {1 if (cfg.enabled and cfg.reverb_on) else 0}")
    send_daemon_cmd(
        f"RVP {cfg.reverb_room * 10} {cfg.reverb_damp * 10} {cfg.reverb_width * 10} {cfg.reverb_mix * 10}"
    )

    # 6. EQ
    send_daemon_cmd(f"EQ {1 if (cfg.enabled and cfg.eq_on) else 0}")
    eq_types = [3, 1, 0, 0, 0, 0, 0, 2, 0]
    eq_freqs = [80, 120, 250, 400, 1500, 3500, 6000, 9000, 12000]
    eq_q = [707, 707, 1000, 1000, 1000, 700, 1000, 700, 1000]
    for idx, gain in enumerate(cfg.eq_gains):
        send_daemon_cmd(
            f"EQB {idx} {eq_types[idx]} {eq_freqs[idx]} {eq_q[idx]} {gain}"
        )


# -----------------------------------------------------------------------------
#   GTK3 Interface
# -----------------------------------------------------------------------------
def run_gtk_app() -> None:
    # Single instance toggle: if GUI window is already open, close it (toggle behavior)
    if GUI_PID_FILE.exists():
        try:
            with open(GUI_PID_FILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            # Process is running, toggle it closed
            os.kill(old_pid, signal.SIGTERM)
            GUI_PID_FILE.unlink(missing_ok=True)
            return
        except (OSError, ValueError):
            GUI_PID_FILE.unlink(missing_ok=True)

    with open(GUI_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    try:
        GLib.set_prgname("dusky_audio_studio.py")
        GLib.set_application_name("Dusky Audio Studio")
    except Exception:
        pass

    cfg = load_config()

    # Load dynamic Matugen/GTK CSS Provider
    provider = Gtk.CssProvider()
    provider.load_from_data(DUSKY_CSS.encode("utf-8"))
    screen = Gdk.Screen.get_default()
    if screen:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    class AudioStudioWindow(Gtk.Window):
        def __init__(self) -> None:
            super().__init__(title="Dusky Audio Studio & Voice DSP")
            self.set_wmclass("dusky_audio_studio.py", "dusky_audio_studio.py")
            self.set_default_size(630, 700)
            self.set_border_width(18)
            self.set_position(Gtk.WindowPosition.CENTER)
            self.get_style_context().add_class("panel-window")

            self.cfg = cfg
            self.sources = enumerate_sources()
            self._updating_ui = False

            main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            self.add(main_vbox)

            # --- Header / Master Switch (Center Aligned Title) ---
            header_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=12
            )

            # Left spacer to match right switch width for true center alignment
            left_spacer = Gtk.Box()
            left_spacer.set_size_request(44, -1)
            header_box.pack_start(left_spacer, False, False, 0)

            title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            title_box.set_halign(Gtk.Align.CENTER)
            title_box.set_hexpand(True)

            title_lbl = Gtk.Label(label="Dusky Audio Studio", xalign=0.5)
            title_lbl.get_style_context().add_class("header-title")

            self.status_lbl = Gtk.Label(xalign=0.5)
            self.update_status_label()

            title_box.pack_start(title_lbl, False, False, 0)
            title_box.pack_start(self.status_lbl, False, False, 0)
            header_box.pack_start(title_box, True, True, 0)

            self.master_switch = Gtk.Switch()
            self.master_switch.set_valign(Gtk.Align.CENTER)
            self.master_switch.set_halign(Gtk.Align.END)
            self.master_switch.get_style_context().add_class("compact-switch")
            self.master_switch.set_active(self.cfg.enabled)
            self.master_switch.connect("notify::active", self.on_master_toggled)
            header_box.pack_end(self.master_switch, False, False, 0)

            main_vbox.pack_start(header_box, False, False, 0)

            # --- Missing Dependencies Warning Banner ---
            missing = check_system_dependencies()
            if missing:
                warn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                warn_box.get_style_context().add_class("warning-banner")
                warn_title = Gtk.Label(
                    label="⚠️ Missing System Audio Dependencies:", xalign=0
                )
                warn_title.get_style_context().add_class("warning-text")
                warn_box.pack_start(warn_title, False, False, 0)
                for m in missing:
                    item_lbl = Gtk.Label(label=f"  • {m}", xalign=0)
                    item_lbl.get_style_context().add_class("footer-info")
                    warn_box.pack_start(item_lbl, False, False, 0)
                main_vbox.pack_start(warn_box, False, False, 0)

            # --- Top Bar: Device & Monitor Loopback ---
            top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            self.src_combo = Gtk.ComboBoxText()
            self.src_combo.get_style_context().add_class("device-combo")
            active_idx = 0
            for idx, (node, desc) in enumerate(self.sources):
                self.src_combo.append(node, desc)
                if node == self.cfg.source:
                    active_idx = idx
            self.src_combo.set_active(active_idx)
            self.src_combo.connect("changed", self.on_source_changed)
            top_bar.pack_start(self.src_combo, True, True, 0)

            mon_btn = Gtk.CheckButton(label="Hear Voice")
            mon_btn.set_active(self.cfg.monitor)
            mon_btn.connect("toggled", self.on_monitor_toggled)
            top_bar.pack_end(mon_btn, False, False, 0)
            main_vbox.pack_start(top_bar, False, False, 0)

            main_vbox.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                False,
                False,
                0,
            )

            # --- Multi-Tab Notebook ---
            self.notebook = Gtk.Notebook()
            self.notebook.set_scrollable(True)
            main_vbox.pack_start(self.notebook, True, True, 0)

            # Build Tabs
            self.build_tab_noise()
            self.build_tab_voice_fx()
            self.build_tab_spatial_dsp()
            self.build_tab_equalizer()

            # --- Footer ---
            footer_lbl = Gtk.Label(
                label="Virtual Device: Dusky Studio Microphone (PipeWire Low-Latency DSP)",
                xalign=0.5,
            )
            footer_lbl.get_style_context().add_class("footer-info")
            main_vbox.pack_end(footer_lbl, False, False, 0)

            if self.cfg.enabled:
                start_daemon(self.cfg)

        # ---------------------------------------------------------------------
        # Tab 1: Noise & Levels
        # ---------------------------------------------------------------------
        def build_tab_noise(self) -> None:
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            vbox.set_border_width(12)

            # Master Gain
            vbox.pack_start(
                self.create_slider_row(
                    "Microphone Output Gain",
                    self.cfg.volume,
                    0,
                    200,
                    "%",
                    self.on_volume_changed,
                ),
                False,
                False,
                0,
            )

            # RNNoise Toggle + Aggressiveness
            rnn_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            rnn_lbl = Gtk.Label(
                label="RNNoise Neural Suppression", xalign=0
            )
            rnn_lbl.get_style_context().add_class("section-label")
            self.rnn_switch = Gtk.Switch()
            self.rnn_switch.get_style_context().add_class("compact-switch")
            self.rnn_switch.set_active(self.cfg.rnnoise_on)
            self.rnn_switch.connect("notify::active", self.on_rnnoise_toggled)
            rnn_hdr.pack_start(rnn_lbl, True, True, 0)
            rnn_hdr.pack_end(self.rnn_switch, False, False, 0)
            vbox.pack_start(rnn_hdr, False, False, 0)

            vbox.pack_start(
                self.create_slider_row(
                    "Noise Reduction Aggressiveness",
                    self.cfg.aggressiveness,
                    0,
                    100,
                    "%",
                    self.on_agg_changed,
                ),
                False,
                False,
                0,
            )

            self.notebook.append_page(vbox, Gtk.Label(label="🎙️ Noise & Level"))

        # ---------------------------------------------------------------------
        # Tab 2: Voice Transformers & Character Presets
        # ---------------------------------------------------------------------
        def build_tab_voice_fx(self) -> None:
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)

            # Presets FlowBox
            preset_lbl = Gtk.Label(label="Voice FX Character Presets", xalign=0)
            preset_lbl.get_style_context().add_class("section-label")
            vbox.pack_start(preset_lbl, False, False, 0)

            flowbox = Gtk.FlowBox()
            flowbox.set_valign(Gtk.Align.START)
            flowbox.set_max_children_per_line(4)
            flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
            flowbox.set_row_spacing(6)
            flowbox.set_column_spacing(6)

            for name in PRESETS:
                btn = Gtk.Button(label=name)
                btn.get_style_context().add_class("preset-btn")
                btn.connect(
                    "clicked", lambda _, n=name: self.apply_preset_by_name(n)
                )
                flowbox.add(btn)
            vbox.pack_start(flowbox, False, False, 0)

            vbox.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                False,
                False,
                4,
            )

            # Pitch Shifter (-24 to +24 st)
            self.pitch_row = self.create_slider_row(
                "Pitch Shifter",
                int(self.cfg.pitch_shift / 100),
                -24,
                24,
                " st",
                self.on_pitch_changed,
            )
            vbox.pack_start(self.pitch_row, False, False, 0)

            # Vocoder / Robot Toggle + Settings
            voc_hdr = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8
            )
            voc_lbl = Gtk.Label(
                label="16-Band Vocoder (Robot Voice)", xalign=0
            )
            voc_lbl.get_style_context().add_class("section-label")
            self.voc_switch = Gtk.Switch()
            self.voc_switch.get_style_context().add_class("compact-switch")
            self.voc_switch.set_active(self.cfg.vocoder_on)
            self.voc_switch.connect("notify::active", self.on_vocoder_toggled)
            voc_hdr.pack_start(voc_lbl, True, True, 0)
            voc_hdr.pack_end(self.voc_switch, False, False, 0)
            vbox.pack_start(voc_hdr, False, False, 0)

            # Matrix Intensity
            self.matrix_row = self.create_slider_row(
                "Matrix / Sentinel Timbre",
                self.cfg.vocoder_matrix,
                0,
                100,
                "%",
                self.on_matrix_changed,
            )
            vbox.pack_start(self.matrix_row, False, False, 0)

            # Autotune Switch
            atn_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8
            )
            atn_lbl = Gtk.Label(
                label="Autotune (Pitch Snap / Chromatic)", xalign=0
            )
            atn_lbl.get_style_context().add_class("section-label")
            self.atn_switch = Gtk.Switch()
            self.atn_switch.get_style_context().add_class("compact-switch")
            self.atn_switch.set_active(self.cfg.autotune_on)
            self.atn_switch.connect("notify::active", self.on_autotune_toggled)
            atn_box.pack_start(atn_lbl, True, True, 0)
            atn_box.pack_end(self.atn_switch, False, False, 0)
            vbox.pack_start(atn_box, False, False, 0)

            # Bitcrusher (0..15 bits)
            self.bitcrush_row = self.create_slider_row(
                "Lo-Fi Bitcrusher",
                self.cfg.bitcrush_bits,
                0,
                15,
                " bits",
                self.on_bitcrush_changed,
            )
            vbox.pack_start(self.bitcrush_row, False, False, 0)

            # Stutter Chopper
            self.stutter_row = self.create_slider_row(
                "Stutter Chopper Gate",
                self.cfg.stutter_hz,
                0,
                20,
                " Hz",
                self.on_stutter_changed,
            )
            vbox.pack_start(self.stutter_row, False, False, 0)

            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(
                Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
            )
            scrolled.add(vbox)
            self.notebook.append_page(scrolled, Gtk.Label(label="🤖 Voice FX"))

        # ---------------------------------------------------------------------
        # Tab 3: Spatial DSP (Delay & Reverb)
        # ---------------------------------------------------------------------
        def build_tab_spatial_dsp(self) -> None:
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            vbox.set_border_width(12)

            # Tape Delay
            dly_hdr = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8
            )
            dly_lbl = Gtk.Label(label="Stereo Tape Echo / Delay", xalign=0)
            dly_lbl.get_style_context().add_class("section-label")
            self.dly_switch = Gtk.Switch()
            self.dly_switch.get_style_context().add_class("compact-switch")
            self.dly_switch.set_active(self.cfg.delay_on)
            self.dly_switch.connect("notify::active", self.on_delay_toggled)
            dly_hdr.pack_start(dly_lbl, True, True, 0)
            dly_hdr.pack_end(self.dly_switch, False, False, 0)
            vbox.pack_start(dly_hdr, False, False, 0)

            vbox.pack_start(
                self.create_slider_row(
                    "Delay Time",
                    self.cfg.delay_ms,
                    10,
                    1000,
                    " ms",
                    self.on_delay_time_changed,
                ),
                False,
                False,
                0,
            )
            vbox.pack_start(
                self.create_slider_row(
                    "Delay Feedback",
                    self.cfg.delay_feedback,
                    0,
                    95,
                    "%",
                    self.on_delay_fb_changed,
                ),
                False,
                False,
                0,
            )
            vbox.pack_start(
                self.create_slider_row(
                    "Delay Wet/Dry Mix",
                    self.cfg.delay_mix,
                    0,
                    100,
                    "%",
                    self.on_delay_mix_changed,
                ),
                False,
                False,
                0,
            )

            vbox.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                False,
                False,
                4,
            )

            # Reverb
            rvb_hdr = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8
            )
            rvb_lbl = Gtk.Label(label="Algorithmic Reverb Tank", xalign=0)
            rvb_lbl.get_style_context().add_class("section-label")
            self.rvb_switch = Gtk.Switch()
            self.rvb_switch.get_style_context().add_class("compact-switch")
            self.rvb_switch.set_active(self.cfg.reverb_on)
            self.rvb_switch.connect("notify::active", self.on_reverb_toggled)
            rvb_hdr.pack_start(rvb_lbl, True, True, 0)
            rvb_hdr.pack_end(self.rvb_switch, False, False, 0)
            vbox.pack_start(rvb_hdr, False, False, 0)

            vbox.pack_start(
                self.create_slider_row(
                    "Reverb Room Size",
                    self.cfg.reverb_room,
                    0,
                    100,
                    "%",
                    self.on_reverb_room_changed,
                ),
                False,
                False,
                0,
            )
            vbox.pack_start(
                self.create_slider_row(
                    "Reverb Dampening",
                    self.cfg.reverb_damp,
                    0,
                    100,
                    "%",
                    self.on_reverb_damp_changed,
                ),
                False,
                False,
                0,
            )
            vbox.pack_start(
                self.create_slider_row(
                    "Reverb Wet/Dry Mix",
                    self.cfg.reverb_mix,
                    0,
                    100,
                    "%",
                    self.on_reverb_mix_changed,
                ),
                False,
                False,
                0,
            )

            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(
                Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
            )
            scrolled.add(vbox)
            self.notebook.append_page(
                scrolled, Gtk.Label(label="🎛️ Delay & Reverb")
            )

        # ---------------------------------------------------------------------
        # Tab 4: 9-Band Studio EQ
        # ---------------------------------------------------------------------
        def build_tab_equalizer(self) -> None:
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            vbox.set_border_width(12)

            eq_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            eq_lbl = Gtk.Label(
                label="9-Band Studio Parametric EQ", xalign=0
            )
            eq_lbl.get_style_context().add_class("section-label")
            self.eq_switch = Gtk.Switch()
            self.eq_switch.get_style_context().add_class("compact-switch")
            self.eq_switch.set_active(self.cfg.eq_on)
            self.eq_switch.connect("notify::active", self.on_eq_toggled)
            eq_hdr.pack_start(eq_lbl, True, True, 0)
            eq_hdr.pack_end(self.eq_switch, False, False, 0)
            vbox.pack_start(eq_hdr, False, False, 0)

            bands = [
                ("80 Hz (Sub Bass)", 0),
                ("120 Hz (Warmth)", 1),
                ("250 Hz (Low Mid)", 2),
                ("400 Hz (Boxiness)", 3),
                ("1.5 kHz (Presence)", 4),
                ("3.5 kHz (Clarity)", 5),
                ("6.0 kHz (Vocal Detail)", 6),
                ("9.0 kHz (Air)", 7),
                ("12.0 kHz (Brilliance)", 8),
            ]

            self.eq_scales: list[Gtk.Scale] = []
            for name, idx in bands:
                val = (
                    self.cfg.eq_gains[idx] / 100
                    if idx < len(self.cfg.eq_gains)
                    else 0
                )
                row = self.create_slider_row(
                    name,
                    int(val),
                    -12,
                    12,
                    " dB",
                    lambda s, i=idx: self.on_eq_band_changed(i, s),
                )
                vbox.pack_start(row, False, False, 0)

            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(
                Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
            )
            scrolled.add(vbox)
            self.notebook.append_page(
                scrolled, Gtk.Label(label="📊 9-Band EQ")
            )

        # ---------------------------------------------------------------------
        # Helper: Create Slider Row
        # ---------------------------------------------------------------------
        def create_slider_row(
            self,
            title: str,
            val: int,
            min_v: int,
            max_v: int,
            unit: str,
            callback: Any,
        ) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=title, xalign=0)
            lbl.get_style_context().add_class("section-label")
            val_lbl = Gtk.Label(label=f"{val}{unit}", xalign=1)
            val_lbl.get_style_context().add_class("value-label")
            hdr.pack_start(lbl, True, True, 0)
            hdr.pack_end(val_lbl, False, False, 0)
            box.pack_start(hdr, False, False, 0)

            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, min_v, max_v, 1
            )
            scale.set_value(val)

            def on_val(s: Gtk.Scale) -> None:
                v = int(s.get_value())
                val_lbl.set_text(f"{v}{unit}")
                if not self._updating_ui:
                    callback(s)

            scale.connect("value-changed", on_val)
            box.pack_start(scale, False, False, 0)
            box._scale = scale  # type: ignore
            box._val_lbl = val_lbl  # type: ignore
            return box

        # ---------------------------------------------------------------------
        # Callbacks & Events
        # ---------------------------------------------------------------------
        def update_status_label(self) -> None:
            if self.cfg.enabled:
                self.status_lbl.set_text("Active (Audio Processing ON)")
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
            if self._updating_ui:
                return
            active = switch.get_active()
            if active:
                ok = start_daemon(self.cfg)
                if not ok:
                    self._updating_ui = True
                    switch.set_active(False)
                    self._updating_ui = False
                    self.cfg.enabled = False
                    save_config(self.cfg)
                    self.update_status_label()
                    return
            else:
                stop_daemon()
            self.cfg.enabled = active
            save_config(self.cfg)
            self.update_status_label()

        def on_source_changed(self, combo: Gtk.ComboBoxText) -> None:
            node = combo.get_active_id()
            if node:
                self.cfg.source = node
                save_config(self.cfg)
                send_daemon_cmd(f"SRC {node}")

        def on_volume_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.volume = val
            save_config(self.cfg)
            send_daemon_cmd(f"VOL {val * 10}")

        def on_rnnoise_toggled(self, switch: Gtk.Switch, _g: Any) -> None:
            active = switch.get_active()
            self.cfg.rnnoise_on = active
            save_config(self.cfg)
            send_daemon_cmd(
                f"RNN {1 if (self.cfg.enabled and active) else 0}"
            )

        def on_agg_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.aggressiveness = val
            save_config(self.cfg)
            send_daemon_cmd(f"AGG {val * 10}")

        def on_pitch_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.pitch_shift = val * 100
            save_config(self.cfg)
            send_daemon_cmd(
                f"PSH {self.cfg.pitch_shift if self.cfg.enabled else 0}"
            )

        def on_vocoder_toggled(self, switch: Gtk.Switch, _g: Any) -> None:
            active = switch.get_active()
            self.cfg.vocoder_on = active
            save_config(self.cfg)
            send_daemon_cmd(
                f"VOC {1 if (self.cfg.enabled and active) else 0}"
            )

        def on_matrix_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.vocoder_matrix = val
            save_config(self.cfg)
            send_daemon_cmd(f"MTX {val * 10}")

        def on_autotune_toggled(self, switch: Gtk.Switch, _g: Any) -> None:
            active = switch.get_active()
            self.cfg.autotune_on = active
            save_config(self.cfg)
            send_daemon_cmd(
                f"ATN {1 if (self.cfg.enabled and active) else 0}"
            )

        def on_bitcrush_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.bitcrush_bits = val
            save_config(self.cfg)
            send_daemon_cmd(
                f"BCR {val if self.cfg.enabled else 0} {self.cfg.bitcrush_downsample}"
            )

        def on_stutter_changed(self, scale: Gtk.Scale) -> None:
            val = int(scale.get_value())
            self.cfg.stutter_hz = val
            save_config(self.cfg)
            send_daemon_cmd(f"STT {val if self.cfg.enabled else 0} 500")

        def on_delay_toggled(self, switch: Gtk.Switch, _g: Any) -> None:
            active = switch.get_active()
            self.cfg.delay_on = active
            save_config(self.cfg)
            send_daemon_cmd(
                f"DLY {1 if (self.cfg.enabled and active) else 0}"
            )

        def on_delay_time_changed(self, scale: Gtk.Scale) -> None:
            self.cfg.delay_ms = int(scale.get_value())
            save_config(self.cfg)
            send_daemon_cmd(
                f"DLP {self.cfg.delay_ms} {self.cfg.delay_feedback * 10} {self.cfg.delay_mix * 10}"
            )

        def on_delay_fb_changed(self, scale: Gtk.Scale) -> None:
            self.cfg.delay_feedback = int(scale.get_value())
            save_config(self.cfg)
            send_daemon_cmd(
                f"DLP {self.cfg.delay_ms} {self.cfg.delay_feedback * 10} {self.cfg.delay_mix * 10}"
            )

        def on_delay_mix_changed(self, scale: Gtk.Scale) -> None:
            self.cfg.delay_mix = int(scale.get_value())
            save_config(self.cfg)
            send_daemon_cmd(
                f"DLP {self.cfg.delay_ms} {self.cfg.delay_feedback * 10} {self.cfg.delay_mix * 10}"
            )

        def on_reverb_toggled(self, switch: Gtk.Switch, _g: Any) -> None:
            active = switch.get_active()
            self.cfg.reverb_on = active
            save_config(self.cfg)
            send_daemon_cmd(
                f"RVB {1 if (self.cfg.enabled and active) else 0}"
            )

        def on_reverb_room_changed(self, scale: Gtk.Scale) -> None:
            self.cfg.reverb_room = int(scale.get_value())
            save_config(self.cfg)
            send_daemon_cmd(
                f"RVP {self.cfg.reverb_room * 10} {self.cfg.reverb_damp * 10} {self.cfg.reverb_width * 10} {self.cfg.reverb_mix * 10}"
            )

        def on_reverb_damp_changed(self, scale: Gtk.Scale) -> None:
            self.cfg.reverb_damp = int(scale.get_value())
            save_config(self.cfg)
            send_daemon_cmd(
                f"RVP {self.cfg.reverb_room * 10} {self.cfg.reverb_damp * 10} {self.cfg.reverb_width * 10} {self.cfg.reverb_mix * 10}"
            )

        def on_reverb_mix_changed(self, scale: Gtk.Scale) -> None:
            self.cfg.reverb_mix = int(scale.get_value())
            save_config(self.cfg)
            send_daemon_cmd(
                f"RVP {self.cfg.reverb_room * 10} {self.cfg.reverb_damp * 10} {self.cfg.reverb_width * 10} {self.cfg.reverb_mix * 10}"
            )

        def on_eq_toggled(self, switch: Gtk.Switch, _g: Any) -> None:
            active = switch.get_active()
            self.cfg.eq_on = active
            save_config(self.cfg)
            send_daemon_cmd(f"EQ {1 if (self.cfg.enabled and active) else 0}")

        def on_eq_band_changed(self, idx: int, scale: Gtk.Scale) -> None:
            val = int(scale.get_value()) * 100
            if idx < len(self.cfg.eq_gains):
                self.cfg.eq_gains[idx] = val
                save_config(self.cfg)
                eq_types = [3, 1, 0, 0, 0, 0, 0, 2, 0]
                eq_freqs = [80, 120, 250, 400, 1500, 3500, 6000, 9000, 12000]
                eq_q = [707, 707, 1000, 1000, 1000, 700, 1000, 700, 1000]
                send_daemon_cmd(
                    f"EQB {idx} {eq_types[idx]} {eq_freqs[idx]} {eq_q[idx]} {val}"
                )

        def on_monitor_toggled(self, check: Gtk.CheckButton) -> None:
            active = check.get_active()
            self.cfg.monitor = active
            save_config(self.cfg)
            send_daemon_cmd(f"MON {1 if active else 0}")

        def apply_preset_by_name(self, name: str) -> None:
            p = PRESETS.get(name)
            if not p:
                return
            self._updating_ui = True
            for k, v in p.items():
                if hasattr(self.cfg, k):
                    setattr(self.cfg, k, v)
            save_config(self.cfg)
            apply_config_to_daemon(self.cfg)

            # Sync UI switches
            self.voc_switch.set_active(self.cfg.vocoder_on)
            self.atn_switch.set_active(self.cfg.autotune_on)
            self.pitch_row._scale.set_value(
                int(self.cfg.pitch_shift / 100)
            )  # type: ignore
            self.matrix_row._scale.set_value(self.cfg.vocoder_matrix)  # type: ignore
            self.bitcrush_row._scale.set_value(self.cfg.bitcrush_bits)  # type: ignore
            self.stutter_row._scale.set_value(self.cfg.stutter_hz)  # type: ignore
            self._updating_ui = False

    win = AudioStudioWindow()

    def on_destroy(*_):
        GUI_PID_FILE.unlink(missing_ok=True)
        Gtk.main_quit()

    win.connect("destroy", on_destroy)
    win.show_all()
    Gtk.main()
    GUI_PID_FILE.unlink(missing_ok=True)


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
        print("🎙️ Dusky Audio DSP turned ON.")
    elif cmd in ("--off", "-0", "off"):
        cfg.enabled = False
        save_config(cfg)
        stop_daemon()
        print("🔇 Dusky Audio DSP turned OFF.")
    elif cmd in ("--toggle", "-t", "toggle"):
        is_on = bool(get_daemon_pid())
        if is_on:
            cfg.enabled = False
            save_config(cfg)
            stop_daemon()
            print("🔇 Dusky Audio DSP turned OFF.")
        else:
            cfg.enabled = True
            save_config(cfg)
            start_daemon(cfg)
            print("🎙️ Dusky Audio DSP turned ON.")
    elif cmd in ("--status", "-s", "status"):
        pid = get_daemon_pid()
        if pid:
            print(
                f"ON (PID {pid}, Noise Suppression: {cfg.aggressiveness}%, Volume: {cfg.volume}%, Vocoder: {'ON' if cfg.vocoder_on else 'OFF'})"
            )
        else:
            print("OFF")
    elif cmd in ("--preset", "-p") and len(args) > 1:
        p_name = " ".join(args[1:])
        match = None
        for k in PRESETS:
            if k.lower() == p_name.lower():
                match = k
                break
        if match:
            for k, v in PRESETS[match].items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            save_config(cfg)
            apply_config_to_daemon(cfg)
            print(f"✨ Applied Preset: {match}")
        else:
            print(
                f"Preset '{p_name}' not found. Available: {', '.join(PRESETS.keys())}"
            )
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
        print("""Usage: dusky_audio_studio.py [COMMAND]

Commands:
  --gui, -g                 Launch complete GTK3 Audio Studio window (default)
  --toggle, -t              Toggle Audio DSP / Noise Cancellation ON / OFF
  --on                      Turn Audio DSP ON
  --off                     Turn Audio DSP OFF
  --status, -s              Print current status
  --preset, -p <name>       Apply voice preset (e.g. "Daft Punk", "Darth Vader", "Cylon Robot")
  --set-agg <0-100>         Set RNNoise suppression aggressiveness (0 to 100%)
  --set-vol <0-200>         Set microphone volume/gain (0 to 200%)
  --help, -h                Show this help message

Presets:
  """ + ", ".join(f'"{k}"' for k in PRESETS.keys()))
    else:
        print(f"Unknown command: {cmd}. Run with --help for usage.")


if __name__ == "__main__":
    main()
