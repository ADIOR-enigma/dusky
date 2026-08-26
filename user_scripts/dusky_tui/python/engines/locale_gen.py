#!/usr/bin/env python3
"""
LocaleGenEngine - Intelligent glibc /etc/locale.gen and systemd region engine.
Supports:
- /etc/locale.gen glibc locale enable/disable toggling with in-place preservation.
- systemd-timedated: Timezone, NTP synchronization, Local RTC.
- systemd-localed: Primary LANG and granular LC_* variable configuration (/etc/locale.conf).
- systemd-vconsole: TTY console keymap and X11/Wayland keyboard mappings (/etc/vconsole.conf).
- Live execution and atomic compilation of locale-gen.
"""
import os
import re
import tempfile
import threading
import subprocess
from pathlib import Path
from typing import Any

from python.frontend.core_types import BaseEngine

class LocaleGenEngine(BaseEngine):
    """
    Bleeding-edge engine for glibc /etc/locale.gen, /etc/locale.conf, /etc/vconsole.conf,
    and systemd localed/timedated services.
    """

    def __init__(self, config_path: str = "/etc/locale.gen") -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.locale_conf_path = Path("/etc/locale.conf")
        self.vconsole_conf_path = Path("/etc/vconsole.conf")
        self.cache: dict[str, Any] = {}
        self.file_mtime_ns: int = 0
        self._lock = threading.Lock()

    @property
    def target_path(self) -> str:
        return str(self.config_path)

    def load_state(self) -> dict[str, Any]:
        with self._lock:
            self.cache = {}

            # 1. Parse /etc/locale.gen for glibc locale states
            if self.config_path.exists():
                try:
                    with open(self.config_path, "r", encoding="utf-8", errors="replace") as f:
                        self.file_mtime_ns = os.fstat(f.fileno()).st_mtime_ns
                        lines = f.readlines()

                    for line in lines:
                        stripped = line.strip()
                        if not stripped:
                            continue

                        is_commented = stripped.startswith("#")
                        content = stripped.lstrip("#").strip()

                        parts = content.split()
                        if len(parts) >= 2 and "_" in parts[0]:
                            locale_full = f"{parts[0]} {parts[1]}"
                            locale_short = parts[0]
                            is_enabled = not is_commented

                            self.cache[locale_full] = is_enabled
                            self.cache[locale_short] = is_enabled
                except Exception as e:
                    print(f"[LocaleGenEngine] Failed to read {self.config_path.name}: {e}")

            # 2. Parse /etc/locale.conf for system LANG and LC_* variables
            if self.locale_conf_path.exists():
                try:
                    for line in self.locale_conf_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        line_s = line.strip()
                        if line_s and not line_s.startswith("#") and "=" in line_s:
                            k, v = line_s.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            self.cache[k] = v
                            if k == "LANG":
                                self.cache["lang"] = v
                                self.cache["action_set_lang"] = v
                except Exception:
                    pass

            # 3. Parse /etc/vconsole.conf for KEYMAP and XKB settings
            if self.vconsole_conf_path.exists():
                try:
                    for line in self.vconsole_conf_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        line_s = line.strip()
                        if line_s and not line_s.startswith("#") and "=" in line_s:
                            k, v = line_s.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            self.cache[k] = v
                            if k == "KEYMAP":
                                self.cache["keymap"] = v
                                self.cache["action_set_keymap"] = v
                            elif k == "XKBLAYOUT":
                                self.cache["x11_layout"] = v
                            elif k == "XKBMODEL":
                                self.cache["x11_model"] = v
                            elif k == "XKBOPTIONS":
                                self.cache["x11_options"] = v
                except Exception:
                    pass

            # 4. Query timedatectl for timezone, NTP, and RTC state
            try:
                res = subprocess.run(["timedatectl", "show"], capture_output=True, text=True, timeout=2)
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.splitlines():
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip()
                            if k == "Timezone":
                                self.cache["timezone"] = v
                                self.cache["action_set_timezone"] = v
                            elif k == "NTP":
                                self.cache["ntp_sync"] = v.lower() in ("yes", "true", "1")
                            elif k == "LocalRTC":
                                self.cache["rtc_local"] = v.lower() in ("yes", "true", "1")
            except Exception:
                pass

            return self.cache

    def write_value(self, target_key: str, target_scope: str, new_value: str, item_type: str = "string") -> tuple[bool, str, str]:
        return self.write_batch([(target_key, target_scope, new_value, item_type)])

    def write_batch(self, changes: list[tuple[str, str, str, str]]) -> tuple[bool, str, str]:
        if not changes:
            return True, "No pending changes.", ""

        with self._lock:
            locale_gen_changes = []
            action_messages = []
            debug_logs = []

            for key, scope, val, itype in changes:
                k_lower = key.lower()
                val_str = str(val).strip()

                # Action Trigger: locale-gen compilation
                if key == "action_locale_gen" or (itype == "action" and "locale-gen" in val_str):
                    try:
                        res = subprocess.run(["locale-gen"], capture_output=True, text=True, timeout=120)
                        if res.returncode == 0:
                            action_messages.append("Compiled locales via `locale-gen`.")
                        else:
                            return False, f"locale-gen failed: {res.stderr.strip()}", res.stderr
                        debug_logs.append(res.stdout)
                    except Exception as e:
                        return False, f"Failed to execute locale-gen: {e}", str(e)

                # Timezone modification
                elif key in ("timezone", "action_set_timezone", "set_timezone"):
                    if val_str and val_str not in ("nil", "unset", ""):
                        try:
                            res = subprocess.run(["timedatectl", "set-timezone", val_str], capture_output=True, text=True, timeout=10)
                            if res.returncode == 0:
                                action_messages.append(f"Timezone set to {val_str}.")
                                self.cache["timezone"] = val_str
                                self.cache["action_set_timezone"] = val_str
                            else:
                                action_messages.append(f"timedatectl set-timezone failed: {res.stderr.strip()}")
                        except Exception as e:
                            action_messages.append(f"Failed to set timezone: {e}")

                # NTP sync toggle
                elif key == "ntp_sync":
                    is_en = val_str.lower() in ("true", "1", "yes", "on", "t")
                    try:
                        res = subprocess.run(["timedatectl", "set-ntp", "true" if is_en else "false"], capture_output=True, text=True, timeout=10)
                        if res.returncode == 0:
                            action_messages.append(f"NTP Time Sync set to {'enabled' if is_en else 'disabled'}.")
                            self.cache["ntp_sync"] = is_en
                        else:
                            action_messages.append(f"timedatectl set-ntp failed: {res.stderr.strip()}")
                    except Exception as e:
                        action_messages.append(f"Failed to toggle NTP: {e}")

                # Local RTC toggle
                elif key == "rtc_local":
                    is_local = val_str.lower() in ("true", "1", "yes", "on", "t")
                    try:
                        res = subprocess.run(["timedatectl", "set-local-rtc", "true" if is_local else "false"], capture_output=True, text=True, timeout=10)
                        if res.returncode == 0:
                            action_messages.append(f"Hardware RTC set to {'Local Time' if is_local else 'UTC'}.")
                            self.cache["rtc_local"] = is_local
                        else:
                            action_messages.append(f"timedatectl set-local-rtc failed: {res.stderr.strip()}")
                    except Exception as e:
                        action_messages.append(f"Failed to set RTC: {e}")

                # System LANG and LC_* variables
                elif key in ("LANG", "lang", "action_set_lang", "set_lang") or key.startswith("LC_"):
                    param_key = "LANG" if key in ("LANG", "lang", "action_set_lang", "set_lang") else key
                    if val_str and val_str not in ("nil", "unset", ""):
                        try:
                            res = subprocess.run(["localectl", "set-locale", f"{param_key}={val_str}"], capture_output=True, text=True, timeout=10)
                            if res.returncode == 0:
                                action_messages.append(f"Locale {param_key} set to {val_str}.")
                                self.cache[param_key] = val_str
                            else:
                                action_messages.append(f"localectl set-locale failed: {res.stderr.strip()}")
                        except Exception as e:
                            action_messages.append(f"Failed to set {param_key}: {e}")

                # TTY Console Keymap
                elif key in ("KEYMAP", "keymap", "action_set_keymap", "set_keymap"):
                    if val_str and val_str not in ("nil", "unset", ""):
                        try:
                            res = subprocess.run(["localectl", "set-keymap", val_str], capture_output=True, text=True, timeout=10)
                            if res.returncode == 0:
                                action_messages.append(f"TTY keymap set to {val_str}.")
                                self.cache["KEYMAP"] = val_str
                            else:
                                action_messages.append(f"localectl set-keymap failed: {res.stderr.strip()}")
                        except Exception as e:
                            action_messages.append(f"Failed to set keymap: {e}")

                # X11 Keymap
                elif key in ("XKBLAYOUT", "x11_layout"):
                    if val_str and val_str not in ("nil", "unset", ""):
                        try:
                            res = subprocess.run(["localectl", "set-x11-keymap", val_str], capture_output=True, text=True, timeout=10)
                            if res.returncode == 0:
                                action_messages.append(f"X11 layout set to {val_str}.")
                                self.cache["XKBLAYOUT"] = val_str
                            else:
                                action_messages.append(f"localectl set-x11-keymap failed: {res.stderr.strip()}")
                        except Exception as e:
                            action_messages.append(f"Failed to set X11 layout: {e}")

                # Interactive Shell Actions (if launched via action trigger)
                elif itype == "action":
                    continue

                # /etc/locale.gen glibc locale toggle
                else:
                    locale_gen_changes.append((key, scope, val, itype))

            # Apply /etc/locale.gen file modifications if any
            if locale_gen_changes:
                lines = []
                if self.config_path.exists():
                    try:
                        with open(self.config_path, "r", encoding="utf-8", errors="replace") as f:
                            self.file_mtime_ns = os.fstat(f.fileno()).st_mtime_ns
                            lines = f.readlines()
                    except Exception as e:
                        return False, f"Failed to read {self.config_path.name}: {e}", ""

                for key, scope, val, itype in locale_gen_changes:
                    if isinstance(val, str):
                        is_enabled = val.strip().lower() in ("true", "1", "yes", "on", "t", "y")
                    else:
                        is_enabled = bool(val)

                    target_key = key.strip()
                    matched = False

                    new_lines = []
                    for line in lines:
                        stripped = line.strip()
                        content = stripped.lstrip("#").strip()
                        parts = content.split()

                        if len(parts) >= 2 and "_" in parts[0]:
                            locale_full = f"{parts[0]} {parts[1]}"
                            locale_short = parts[0]

                            if target_key in (locale_full, locale_short, content):
                                matched = True
                                if is_enabled:
                                    new_lines.append(f"{locale_full}\n")
                                else:
                                    new_lines.append(f"#{locale_full}\n")
                                continue

                        new_lines.append(line)

                    lines = new_lines

                    if not matched:
                        entry = f"{target_key}\n" if is_enabled else f"#{target_key}\n"
                        lines.append(entry)

                # Atomic Disk Commit with sudo fallback
                final_content = "".join(lines)
                ok, msg = self._atomic_write(self.config_path, final_content)
                if not ok:
                    return False, msg, ""
                action_messages.append(f"Updated {len(locale_gen_changes)} locale definitions in locale.gen.")

            return True, " ".join(action_messages) or "Successfully saved changes.", "\n".join(debug_logs)

    def _atomic_write(self, target_path: Path, final_content: str) -> tuple[bool, str]:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file_path = None
        success = False
        try:
            with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8", dir=target_path.parent) as tf:
                temp_file_path = Path(tf.name)
                tf.write(final_content)
                tf.flush()
                os.fsync(tf.fileno())

            if target_path.exists():
                try:
                    mode = target_path.stat().st_mode
                    os.chmod(temp_file_path, mode)
                except OSError:
                    pass

            os.replace(temp_file_path, target_path)
            self.file_mtime_ns = os.stat(target_path).st_mtime_ns
            success = True
            return True, f"Updated {target_path.name}"
        except PermissionError:
            if temp_file_path and temp_file_path.exists():
                try:
                    temp_file_path.unlink()
                except OSError:
                    pass
            try:
                res = subprocess.run(
                    ["sudo", "-n", "tee", str(target_path)],
                    input=final_content.encode(),
                    capture_output=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    return True, f"Updated {target_path.name} (via sudo)"
                return False, "AUTH_REQUIRED"
            except Exception:
                return False, "AUTH_REQUIRED"
        except OSError as e:
            return False, f"Write error on {target_path.name}: {e}"
        finally:
            if temp_file_path and temp_file_path.exists() and not success:
                try:
                    temp_file_path.unlink()
                except OSError:
                    pass
