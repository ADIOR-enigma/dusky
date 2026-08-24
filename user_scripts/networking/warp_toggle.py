#!/usr/bin/env python3
"""
Robust toggle for Cloudflare WARP with desktop notifications.
Atomically maintains state file at ~/.config/dusky/settings/warp_state.
Targets Python 3.14+ on Arch Linux. Runs as the unprivileged user.

Auto-enables and starts warp-svc via polkit when the daemon is not running:
- Uses `run0` (systemd 256+, native polkit via systemd-run, bleeding-edge) as
  primary — triggers hyprpolkitagent on Hyprland/Wayland, no sudo/pkexec needed.
- Falls back to `pkexec` (polkit) — explicit graphical prompt via hyprpolkitagent.
- Falls back to `systemctl` D-Bus polkit (systemd will prompt via the same agent).
- Falls back to `sudo` with TTY prompt or GUI askpass (yad/rofi) if polkit fails.

Keyring is intentionally NOT used for system service auth: polkit is the
correct mechanism for privileged system operations, handling caching
(auth_admin_keep), session tracking, and secure GUI prompts without storing
passwords. Keyring is for app secrets, not sudo/polkit passwords.
"""

import argparse
import os
import pathlib
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

# Requires bleeding-edge Python as requested — no backwards compat
if sys.version_info < (3, 14):
    sys.stderr.write("warp_toggle.py requires Python 3.14+ (Arch bleeding-edge)\n")
    sys.exit(1)

# ─── Constants ───────────────────────────────────────────────────────────

APP_NAME = "dusky-warp"             # Mako app-name override target
POLL_TIMEOUT_SEC = 10
CMD_TIMEOUT_SEC = 6
NOTIFY_TIMEOUT_SEC = 4
TOS_DEADLINE_SEC = 8.0
STATE_MODE = 0o600

ICON_CONN = "network-vpn"
ICON_DISC = "network-offline"
ICON_WAIT = "network-transmit-receive"
ICON_ERR = "dialog-error"

STATE_FILE = pathlib.Path("~/.config/dusky/settings/warp_state").expanduser()

# Service management (systemd 261, kernel 7.2, Arch rolling)
WARP_SERVICE = "warp-svc.service"
DAEMON_SOCKET = pathlib.Path("/run/cloudflare-warp/warp_service")
SERVICE_START_TIMEOUT_SEC = 60  # allow time for password entry
DAEMON_READY_TIMEOUT_SEC = 12

TOS_PROMPT_RE = re.compile(
    r"accept|terms|\[y/n\]|y/N|do you|agree|tos", re.IGNORECASE
)
REGISTRATION_RE = re.compile(
    r"registration\s*(missing|needs|required)|tos|terms of service",
    re.IGNORECASE,
)

# ─── Styling ─────────────────────────────────────────────────────────────

if sys.stdout.isatty():
    C_RESET = "\033[0m"
    C_BOLD = "\033[1m"
    C_GREEN = "\033[1;32m"
    C_BLUE = "\033[1;34m"
    C_RED = "\033[1;31m"
    C_YELLOW = "\033[1;33m"
else:
    C_RESET = C_BOLD = C_GREEN = C_BLUE = C_RED = C_YELLOW = ""

# ─── Logging ─────────────────────────────────────────────────────────────


def log_info(msg: str) -> None:
    print(f"{C_BLUE}[INFO]{C_RESET} {msg}", flush=True)


def log_success(msg: str) -> None:
    print(f"{C_GREEN}[OK]{C_RESET}   {msg}", flush=True)


def log_warn(msg: str) -> None:
    print(f"{C_YELLOW}[WARN]{C_RESET} {msg}", file=sys.stderr, flush=True)


def log_error(msg: str) -> None:
    print(f"{C_RED}[ERR]{C_RESET}  {msg}", file=sys.stderr, flush=True)


# ─── State Management ────────────────────────────────────────────────────


def update_state_file(state: bool) -> None:
    """Atomically write the boolean tunnel state for desktop widgets."""
    tmp_file = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(f"{state}\n", encoding="utf-8")
        os.chmod(tmp_file, STATE_MODE)
        os.replace(tmp_file, STATE_FILE)
    except OSError as e:
        log_error(f"Failed to update state file: {e}")
        try:
            tmp_file.unlink()
        except OSError:
            pass


# ─── Notification Helper ─────────────────────────────────────────────────


def notify_user(
    title: str,
    message: str,
    urgency: str = "low",
    icon: str = ICON_WAIT,
) -> None:
    if not shutil.which("notify-send"):
        return
    cmd = [
        "notify-send", "-u", urgency, "-a", APP_NAME, "-i", icon,
        "-h", "string:x-canonical-private-synchronous:dusky-warp",
        "--", title, message,
    ]
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=NOTIFY_TIMEOUT_SEC,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        pass


# ─── WARP Service Management (polkit-aware) ──────────────────────────────

def _is_service_active(service: str = WARP_SERVICE) -> bool:
    """Return True if systemd service is active (running)."""
    try:
        res = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            timeout=3,
            check=False,
        )
        return res.returncode == 0
    except (subprocess.SubprocessError, OSError):
        # Fallback: parse human output
        try:
            res = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return res.stdout.strip() == "active"
        except Exception:
            return False


def _is_service_enabled(service: str = WARP_SERVICE) -> bool:
    """Return True if service is enabled to start on boot."""
    try:
        res = subprocess.run(
            ["systemctl", "is-enabled", service],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        out = res.stdout.strip()
        # "enabled", "enabled-runtime", "static" count as enabled for our purposes;
        # "static" means it cannot be disabled but is available.
        # "disabled", "masked", "indirect" etc are not enabled.
        return out in ("enabled", "enabled-runtime", "static", "indirect")
    except (subprocess.SubprocessError, OSError):
        return False


def _is_daemon_responsive() -> bool:
    """Return True if warp-cli can talk to the daemon."""
    try:
        res = subprocess.run(
            ["warp-cli", "status"],
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_SEC,
            check=False,
        )
        # warp-cli returns 0 and prints "Status update:" when daemon is reachable,
        # non-zero with "Unable to connect to the CloudflareWARP daemon" when not.
        return res.returncode == 0 and "Status update:" in res.stdout
    except (subprocess.SubprocessError, OSError):
        return False


def _wait_for_daemon(timeout: int = DAEMON_READY_TIMEOUT_SEC) -> bool:
    """Poll until daemon socket and warp-cli are responsive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Fast path: if socket exists and cli responsive, we're done
        if DAEMON_SOCKET.exists() and _is_daemon_responsive():
            return True
        # Also accept cli responsive without socket check (some versions use different path)
        if _is_daemon_responsive():
            return True
        # If service became active but not yet responsive, keep waiting
        if _is_service_active():
            # service active but not ready yet — wait a bit
            pass
        time.sleep(0.5)
    return _is_daemon_responsive()


def _is_graphical_session() -> bool:
    """Heuristic for whether a graphical polkit agent is available."""
    return bool(
        os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("DISPLAY")
        or os.environ.get("XDG_SESSION_TYPE") in ("wayland", "x11")
    )


def _find_gui_askpass_helper() -> str | None:
    """Return the first available GUI password prompt helper."""
    # Prefer yad (available on this system), then zenity, kdialog, rofi
    for prog in ("yad", "zenity", "kdialog", "rofi"):
        if shutil.which(prog):
            return prog
    return None


def _create_askpass_script(helper: str) -> pathlib.Path:
    """Create a temporary SUDO_ASKPASS script that uses *helper*."""
    # Use mktemp to avoid race
    fd, path = tempfile.mkstemp(prefix="dusky-warp-askpass-", suffix=".sh")
    os.close(fd)
    p = pathlib.Path(path)
    if helper == "yad":
        content = """#!/bin/sh
# yad askpass for sudo -A
exec yad --entry --hide-text --title="WARP Service Authentication" --text="Authentication required to manage WARP service\\nEnter password for $USER:" --width=400 --center --on-top 2>/dev/null
"""
    elif helper == "zenity":
        content = """#!/bin/sh
exec zenity --password --title="WARP Service Authentication" 2>/dev/null
"""
    elif helper == "kdialog":
        content = """#!/bin/sh
exec kdialog --password "Authentication required to manage WARP service" 2>/dev/null
"""
    elif helper == "rofi":
        content = """#!/bin/sh
exec rofi -dmenu -password -p "Password for $USER (WARP):" -theme-str 'window {width: 400px;}' 2>/dev/null
"""
    else:
        content = """#!/bin/sh
printf "No askpass helper\\n" >&2; exit 1
"""
    p.write_text(content, encoding="utf-8")
    os.chmod(p, 0o700)
    return p


def _try_sudo_askpass(systemctl_args: list[str], timeout: int) -> bool:
    """Try sudo -A with a GUI askpass helper (yad/rofi/etc)."""
    if not shutil.which("sudo"):
        return False
    helper = _find_gui_askpass_helper()
    if not helper:
        log_warn("No GUI askpass helper found (yad/zenity/kdialog/rofi) for sudo fallback.")
        return False
    if not _is_graphical_session():
        log_warn("No graphical session detected for GUI askpass fallback.")
        return False

    askpass = _create_askpass_script(helper)
    try:
        env = os.environ.copy()
        env["SUDO_ASKPASS"] = str(askpass)
        # Ensure Wayland/X11 vars are preserved for the helper
        for k in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY"):
            if k in os.environ:
                env[k] = os.environ[k]
        cmd = ["sudo", "-A", "systemctl"] + systemctl_args
        log_info(f"Attempting sudo askpass ({helper}): systemctl {' '.join(systemctl_args)}")
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        if res.returncode == 0:
            log_success(f"sudo -A succeeded via {helper}: systemctl {' '.join(systemctl_args)}")
            return True
        else:
            err = (res.stderr or "") + (res.stdout or "")
            log_warn(f"sudo -A failed (exit {res.returncode}): {err.strip()[:500]}")
            return False
    except subprocess.TimeoutExpired:
        log_warn(f"sudo -A timed out after {timeout}s")
        return False
    except OSError as e:
        log_warn(f"sudo -A execution failed: {e}")
        return False
    finally:
        try:
            askpass.unlink()
        except OSError:
            pass


def _run_privileged_systemctl(
    args: list[str],
    timeout: int = SERVICE_START_TIMEOUT_SEC,
    description: str = "",
) -> bool:
    """
    Run `systemctl <args>` with privilege escalation.

    Tries, in order (bleeding-edge native first):
      1. run0 systemctl (systemd 256+, native polkit via systemd-run)
      2. pkexec systemctl  (polkit, graphical prompt via hyprpolkitagent)
      3. systemctl directly (D-Bus polkit, also prompts via agent)
      4. sudo (TTY prompt if terminal, or GUI askpass via yad/rofi)

    Returns True on success, False otherwise.
    """
    # ── 1. run0 (most modern, systemd-native, replaces sudo/pkexec) ─
    if shutil.which("run0"):
        cmd = ["run0", "systemctl"] + args
        log_info(f"Requesting privilege via run0 (systemd-native): systemctl {' '.join(args)}")
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if res.returncode == 0:
                log_success(f"run0 succeeded: {description or ' '.join(args)}")
                return True
            else:
                err = (res.stderr or "") + (res.stdout or "")
                err_lower = err.lower()
                log_warn(f"run0 failed (exit {res.returncode}): {err.strip()[:600]}")
                if any(kw in err_lower for kw in ["dismissed", "cancelled", "canceled"]):
                    notify_user(
                        "Authentication Cancelled",
                        "Polkit authentication was cancelled.",
                        "critical",
                        ICON_ERR,
                    )
                    return False
                if "access denied" in err_lower and "interactive authentication" in err_lower:
                    log_info("run0 requires interactive authentication, trying next method...")
                # fall through to pkexec
        except subprocess.TimeoutExpired:
            log_warn(f"run0 timed out after {timeout}s (user did not respond)")
            notify_user(
                "Authentication Timeout",
                "Polkit authentication timed out.",
                "critical",
                ICON_ERR,
            )
            return False
        except OSError as e:
            log_warn(f"run0 execution failed: {e}")

    # ── 2. pkexec (explicit polkit) ─────────────────────────────
    if shutil.which("pkexec"):
        cmd = ["pkexec", "systemctl"] + args
        log_info(f"Requesting privilege via polkit (pkexec): systemctl {' '.join(args)}")
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if res.returncode == 0:
                log_success(f"pkexec succeeded: {description or ' '.join(args)}")
                return True
            else:
                err = (res.stderr or "") + (res.stdout or "")
                err_lower = err.lower()
                log_warn(f"pkexec failed (exit {res.returncode}): {err.strip()[:600]}")
                # User cancelled/dismissed — don't try fallbacks, respect cancellation
                if any(kw in err_lower for kw in ["dismissed", "cancelled", "canceled"]):
                    notify_user(
                        "Authentication Cancelled",
                        "Polkit authentication was cancelled.",
                        "critical",
                        ICON_ERR,
                    )
                    return False
                # If it's an auth failure but not dismissal, try next method
                # e.g., "Not authorized", "Authorization failed"
        except subprocess.TimeoutExpired:
            log_warn(f"pkexec timed out after {timeout}s (user did not respond)")
            notify_user(
                "Authentication Timeout",
                "Polkit authentication timed out.",
                "critical",
                ICON_ERR,
            )
            return False
        except OSError as e:
            log_warn(f"pkexec execution failed: {e}")

    # ── 3. systemctl direct (D-Bus polkit) ──────────────────────────
    if shutil.which("systemctl"):
        cmd = ["systemctl"] + args
        log_info(f"Attempting direct systemctl (D-Bus polkit): systemctl {' '.join(args)}")
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if res.returncode == 0:
                log_success(f"systemctl succeeded: {description or ' '.join(args)}")
                return True
            else:
                err = (res.stderr or "") + (res.stdout or "")
                err_lower = err.lower()
                log_warn(f"systemctl failed (exit {res.returncode}): {err.strip()[:600]}")
                # Check for auth-related failures that warrant fallback
                if "interactive authentication required" in err_lower or "not authorized" in err_lower or "access denied" in err_lower:
                    log_info("systemctl requires authentication, trying sudo fallback...")
                    # fall through to sudo
                elif any(kw in err_lower for kw in ["dismissed", "cancelled", "canceled"]):
                    return False
                elif "failed" in err_lower and "not authorized" not in err_lower and "interactive authentication" not in err_lower:
                    # Genuine systemctl failure (e.g., unit not found, masked)
                    # Don't fallback to sudo if it's not an auth issue
                    # But we still allow sudo fallback as it might succeed with proper perms
                    pass
        except subprocess.TimeoutExpired:
            log_warn(f"systemctl timed out after {timeout}s")
            return False
        except OSError as e:
            log_warn(f"systemctl execution failed: {e}")

    # ── 4a. sudo with TTY (if we have a terminal) ──────────────────
    if (sys.stdin.isatty() or sys.stdout.isatty()) and shutil.which("sudo"):
        cmd = ["sudo", "systemctl"] + args
        log_info(f"Attempting sudo (TTY): systemctl {' '.join(args)}")
        try:
            res = subprocess.run(cmd, timeout=timeout, check=False)
            if res.returncode == 0:
                log_success(f"sudo succeeded: {description or ' '.join(args)}")
                return True
            else:
                log_warn(f"sudo failed (exit {res.returncode})")
        except subprocess.TimeoutExpired:
            log_warn(f"sudo timed out after {timeout}s")
            return False
        except OSError as e:
            log_warn(f"sudo execution failed: {e}")

    # ── 4b. sudo with GUI askpass (yad/rofi) ───────────────────────
    if _is_graphical_session() and shutil.which("sudo"):
        if _try_sudo_askpass(args, timeout):
            return True

    log_error(f"All privilege escalation methods failed for: systemctl {' '.join(args)}")
    return False


def ensure_warp_svc_running(need_connect: bool = True) -> bool:
    """
    Ensure warp-svc is active and daemon is responsive.

    - If service is already active and daemon responsive, returns True immediately.
    - If service is inactive and need_connect is True, attempts to start
      (and enable) the service via polkit, prompting for password graphically
      via hyprpolkitagent.
    - If need_connect is False (e.g., user wants to disconnect), does NOT
      auto-start; just returns False to indicate no need to start.

    Returns True if daemon is now ready, False otherwise.
    """
    # Fast path: service active and daemon responsive
    if _is_service_active():
        if _is_daemon_responsive():
            log_info("WARP service is active and daemon responsive.")
            return True
        # Service active but daemon not yet responsive — wait briefly
        log_info("Service active but daemon not yet responsive, waiting...")
        if _wait_for_daemon(timeout=5):
            log_success("Daemon became responsive after short wait.")
            return True
        log_warn("Service active but daemon still not responsive after wait.")
        # Service is active but daemon stuck; fall through to try restart?
        # For now, treat as not ready and attempt a restart via privileged action
        # But only if we need to connect.

    else:
        log_info("WARP service is not active.")

    if not need_connect:
        log_info("Service start not required (disconnect requested).")
        # Treat as disconnected state
        update_state_file(False)
        return False

    # Need to start service
    notify_user(
        "WARP Service Not Running",
        "Starting Cloudflare WARP service (authentication may be required)...",
        "normal",
        ICON_WAIT,
    )
    log_info("Attempting to start WARP service with privilege escalation (polkit)...")

    enabled = _is_service_enabled()
    success = False

    # To satisfy "auto enable" requirement, use enable --now when disabled.
    # This both enables for boot and starts immediately.
    if not enabled:
        log_info("Service is disabled, enabling and starting (enable --now)...")
        success = _run_privileged_systemctl(
            ["enable", "--now", WARP_SERVICE],
            timeout=SERVICE_START_TIMEOUT_SEC,
            description="enable --now warp-svc",
        )
        if not success:
            log_warn("enable --now failed, trying plain start as fallback...")
            success = _run_privileged_systemctl(
                ["start", WARP_SERVICE],
                timeout=SERVICE_START_TIMEOUT_SEC,
                description="start warp-svc",
            )
            if success:
                # Try to enable for persistence separately (best effort)
                log_info("Service started, ensuring it is enabled for boot...")
                _run_privileged_systemctl(
                    ["enable", WARP_SERVICE],
                    timeout=15,
                    description="enable warp-svc",
                )
    else:
        log_info("Service is enabled but not active, starting...")
        success = _run_privileged_systemctl(
            ["start", WARP_SERVICE],
            timeout=SERVICE_START_TIMEOUT_SEC,
            description="start warp-svc",
        )

    if not success:
        log_error("Failed to start WARP service via all privilege escalation methods.")
        notify_user(
            "Service Start Failed",
            "Could not start WARP service. Check authentication or run manually: systemctl start warp-svc",
            "critical",
            ICON_ERR,
        )
        update_state_file(False)
        return False

    # Wait for daemon to become ready
    log_info("Waiting for daemon to become ready...")
    notify_user(
        "Starting WARP Service...",
        "Waiting for daemon to become ready...",
        "normal",
        ICON_WAIT,
    )
    if _wait_for_daemon(timeout=DAEMON_READY_TIMEOUT_SEC):
        log_success("WARP daemon is now ready.")
        # Double-check that service is now enabled for persistence
        if not enabled and not _is_service_enabled():
            log_info("Ensuring service is enabled for auto-start on boot...")
            _run_privileged_systemctl(
                ["enable", WARP_SERVICE],
                timeout=15,
                description="enable warp-svc (post-start)",
            )
        return True
    else:
        log_error("WARP daemon did not become ready after service start.")
        notify_user(
            "Daemon Not Ready",
            "Service started but daemon did not become responsive. Check: journalctl -u warp-svc",
            "critical",
            ICON_ERR,
        )
        if not _is_service_active():
            log_error("Service is not active after start attempt.")
        update_state_file(False)
        return False


# ─── WARP Status ─────────────────────────────────────────────────────────


def get_warp_status() -> str:
    """Return the status string from `warp-cli status`, or 'Unknown'."""
    try:
        res = subprocess.run(
            ["warp-cli", "status"],
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_SEC,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return "Unknown"

    if res.returncode != 0:
        # Detect daemon not running for more helpful logging
        combined = (res.stdout or "") + (res.stderr or "")
        if "Unable to connect" in combined or "No such file or directory" in combined:
            log_info("warp-cli cannot connect to daemon (service likely not running).")
        return "Unknown"

    for line in res.stdout.splitlines():
        if line.startswith("Status update:"):
            return line.split(":", 1)[1].strip()
    return "Unknown"


def status_needs_registration(status: str) -> bool:
    return status == "Unknown" or bool(REGISTRATION_RE.search(status))


# ─── TOS Acceptance ──────────────────────────────────────────────────────


def _accept_tos_via_pty(cmd: list[str]) -> bool:
    """Run cmd under a PTY and answer any y/N TOS prompt. Returns True if
    a prompt was detected and answered."""
    log_info(f"Attempting auto-TOS via PTY: {' '.join(cmd)}")

    try:
        pid, fd = pty.fork()
    except OSError as exc:
        log_warn(f"pty.fork() failed: {exc}")
        return False

    if pid == 0:
        # Child: replace image; never return into parent's cleanup path.
        try:
            os.execvp(cmd[0], cmd)
        except OSError:
            os._exit(127)

    answered = False
    saw_output = False
    deadline = time.monotonic() + TOS_DEADLINE_SEC

    try:
        while time.monotonic() < deadline:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break

            if r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                saw_output = True

                if not answered:
                    text = chunk.decode(errors="replace")
                    if TOS_PROMPT_RE.search(text):
                        try:
                            os.write(fd, b"y\n")
                            answered = True
                        except OSError:
                            pass

                # Check for child exit after consuming this chunk.
                try:
                    wpid, _ = os.waitpid(pid, os.WNOHANG)
                    if wpid != 0:
                        # Drain any remaining buffered output.
                        try:
                            while True:
                                remaining = os.read(fd, 4096)
                                if not remaining:
                                    break
                        except OSError:
                            pass
                        break
                except ChildProcessError:
                    break
            else:
                # Idle: check if the child has exited.
                try:
                    wpid, _ = os.waitpid(pid, os.WNOHANG)
                    if wpid != 0:
                        break
                except ChildProcessError:
                    break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    if answered:
        log_success("TOS prompt answered.")
    elif saw_output:
        log_info("No TOS prompt detected in PTY output.")
    return answered


def ensure_tos_accepted(current_status: str, auto_start: bool = True) -> str:
    """If registration/TOS appears pending, attempt to accept via PTY.
    Returns the refreshed status string.

    If auto_start is True (default, for connect flows), the daemon service
    will be auto-started via polkit if not responsive. For disconnect flows,
    set auto_start=False to avoid unnecessary privilege prompts.
    """
    if not status_needs_registration(current_status):
        return current_status
    if not shutil.which("warp-cli"):
        return current_status

    # Ensure daemon is running before attempting registration/TOS.
    # registration new will fail if daemon is down.
    if not _is_daemon_responsive():
        if not auto_start:
            log_info("Daemon not responsive but auto_start disabled (disconnect flow) — skipping TOS check.")
            return current_status
        log_info("Daemon not responsive before TOS check — ensuring service is running...")
        if not ensure_warp_svc_running(need_connect=True):
            log_warn("Service ensure failed before TOS acceptance; proceeding anyway.")
        else:
            # Re-check status after service start
            current_status = get_warp_status()
            if not status_needs_registration(current_status):
                return current_status

    _accept_tos_via_pty(["warp-cli", "registration", "new"])
    time.sleep(0.5)
    return get_warp_status()


# ─── Core Logic ──────────────────────────────────────────────────────────


def connect_warp() -> bool:
    log_info("Initiating connection sequence...")

    # Ensure service is running before attempting connect
    if not _is_daemon_responsive():
        log_info("Daemon not responsive, ensuring service before connect...")
        if not ensure_warp_svc_running(need_connect=True):
            log_error("Cannot connect: WARP service failed to start.")
            notify_user("Error", "Cannot connect: WARP service failed to start.", "critical", ICON_ERR)
            update_state_file(False)
            return False

    notify_user("Connecting...", "Establishing secure tunnel.", "normal", ICON_WAIT)

    try:
        res = subprocess.run(
            ["warp-cli", "connect"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CMD_TIMEOUT_SEC,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log_error(f"Failed to run warp-cli connect: {e}")
        update_state_file(False)
        return False

    if res.returncode != 0:
        # Re-check if failure was due to daemon not running (race)
        if not _is_daemon_responsive() and not _is_service_active():
            log_warn("Connect failed and daemon is not running — attempting service start and retry...")
            if ensure_warp_svc_running(need_connect=True):
                # Retry connect once after service recovery
                try:
                    res2 = subprocess.run(
                        ["warp-cli", "connect"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=CMD_TIMEOUT_SEC,
                        check=False,
                    )
                    if res2.returncode == 0:
                        log_info("Retry connect succeeded after service start.")
                        res = res2
                    else:
                        log_error("Retry connect also failed.")
                        notify_user("Error", "Failed to send connect command.", "critical", ICON_ERR)
                        update_state_file(False)
                        return False
                except Exception as e:
                    log_error(f"Retry connect exception: {e}")
                    update_state_file(False)
                    return False
            else:
                log_error("Failed to send connect command (service start failed).")
                notify_user("Error", "Failed to send connect command.", "critical", ICON_ERR)
                update_state_file(False)
                return False
        else:
            log_error("Failed to send connect command.")
            notify_user("Error", "Failed to send connect command.", "critical", ICON_ERR)
            update_state_file(False)
            return False

    for _ in range(POLL_TIMEOUT_SEC):
        if get_warp_status() == "Connected":
            log_success("WARP is now Connected.")
            notify_user("Connected", "Secure tunnel active.", "normal", ICON_CONN)
            update_state_file(True)
            return True
        time.sleep(1)

    log_error("Connection timed out.")
    notify_user(
        "Timeout",
        f"Failed to connect within {POLL_TIMEOUT_SEC} seconds.",
        "critical",
        ICON_ERR,
    )
    update_state_file(False)
    return False


def disconnect_warp() -> bool:
    log_info("Disconnecting...")
    # If daemon not running, we're already effectively disconnected
    if not _is_daemon_responsive() and not _is_service_active():
        log_info("Daemon not running — already disconnected.")
        notify_user("Disconnected", "Secure tunnel closed (service was not running).", "low", ICON_DISC)
        update_state_file(False)
        return True

    try:
        res = subprocess.run(
            ["warp-cli", "disconnect"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CMD_TIMEOUT_SEC,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log_error(f"Failed to run warp-cli disconnect: {e}")
        notify_user("Error", "Failed to disconnect WARP.", "critical", ICON_ERR)
        return False

    if res.returncode == 0:
        log_success("Disconnected successfully.")
        notify_user("Disconnected", "Secure tunnel closed.", "low", ICON_DISC)
        update_state_file(False)
        return True

    # If disconnect failed due to daemon not running, treat as success
    if not _is_daemon_responsive():
        log_warn("Disconnect command failed but daemon is not responsive — treating as disconnected.")
        update_state_file(False)
        return True

    log_error("Failed to disconnect.")
    notify_user("Error", "Failed to disconnect WARP.", "critical", ICON_ERR)
    return False


# ─── CLI & Entry ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Robust Cloudflare WARP connection toggler."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--connect", action="store_true", help="Force connection")
    group.add_argument("--disconnect", action="store_true", help="Force disconnection")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if not shutil.which("warp-cli"):
        log_warn("warp-cli not found. Skipping WARP toggle.")
        sys.exit(0)

    # Pre-flight: ensure service is running if we will need to connect
    # We do this before status checks so that `Unknown` due to daemon down
    # is resolved via polkit auto-start instead of failing.
    # Determine intent: if --disconnect, we don't need to start.
    will_need_connect = False
    if args.connect:
        will_need_connect = True
    elif args.disconnect:
        will_need_connect = False
    else:
        # Toggle mode: inspect current status *without* service ensure first
        # If daemon down, status will be Unknown and we will want to connect
        # So we peek at service state to decide
        if not _is_service_active() or not _is_daemon_responsive():
            # Service down implies next action is connect (unless already connected)
            # We will need to connect, so ensure service
            will_need_connect = True
        else:
            # Service is up, check WARP status to decide toggle direction
            # This avoids prompting for service start when we intend to disconnect
            peek_status = get_warp_status()
            if peek_status in ("Connected", "Connecting", "Paused"):
                will_need_connect = False
            else:
                will_need_connect = True
            # We'll re-use this peek_status below to avoid double call
            # Store it for later
            # Instead of re-calling ensure_tos, handle separately
            if will_need_connect:
                # Ensure service (should already be up, but keep for robustness)
                if not _is_daemon_responsive():
                    ensure_warp_svc_running(need_connect=True)
            status = ensure_tos_accepted(peek_status, auto_start=will_need_connect)
            match (args.connect, args.disconnect):
                case (True, False):
                    if status == "Connected":
                        log_success("Already Connected. No action taken.")
                        update_state_file(True)
                    else:
                        connect_warp()
                case (False, True):
                    if status == "Disconnected":
                        log_success("Already Disconnected. No action taken.")
                        update_state_file(False)
                    else:
                        disconnect_warp()
                case _:
                    log_info(f"Current Status: {C_BOLD}{status}{C_RESET}")
                    if status in ("Connected", "Connecting", "Paused"):
                        disconnect_warp()
                    else:
                        connect_warp()
            return

    # For --connect or initial toggle when service down
    if will_need_connect:
        # Ensure service before any warp-cli calls
        if not _is_service_active() or not _is_daemon_responsive():
            if not ensure_warp_svc_running(need_connect=True):
                log_error("Aborting: WARP service could not be started.")
                sys.exit(1)
        status = ensure_tos_accepted(get_warp_status(), auto_start=True)
    else:
        # Disconnect flow: if daemon/service not running, treat as already disconnected
        # and avoid prompting for polkit authentication
        if not _is_daemon_responsive() and not _is_service_active():
            log_info("Service not running and disconnect requested — treating as already disconnected.")
            status = "Disconnected"
        else:
            status = ensure_tos_accepted(get_warp_status(), auto_start=False)

    match (args.connect, args.disconnect):
        case (True, False):
            if status == "Connected":
                log_success("Already Connected. No action taken.")
                update_state_file(True)
            else:
                connect_warp()
        case (False, True):
            if status == "Disconnected":
                log_success("Already Disconnected. No action taken.")
                update_state_file(False)
            else:
                disconnect_warp()
        case _:
            log_info(f"Current Status: {C_BOLD}{status}{C_RESET}")
            if status in ("Connected", "Connecting", "Paused"):
                disconnect_warp()
            else:
                connect_warp()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr, flush=True)
        sys.exit(130)
