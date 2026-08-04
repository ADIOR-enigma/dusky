#!/usr/bin/env python3
"""
Robust toggle for Cloudflare WARP with desktop notifications.
Atomically maintains state file at ~/.config/dusky/settings/warp_state.
Targets Python 3.14+ on Arch Linux. Runs as the unprivileged user.
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
import time

# ─── Constants ───────────────────────────────────────────────────────────

APP_NAME = "dusky-warp"             # Mako app-name override target
POLL_TIMEOUT_SEC = 10
CMD_TIMEOUT_SEC = 6
NOTIFY_TIMEOUT_SEC = 4
TOS_DEADLINE_SEC = 8.0
STATE_MODE = 0o600
MAX_PTY_BUF = 64 * 1024

ICON_CONN = "network-vpn"
ICON_DISC = "network-offline"
ICON_WAIT = "network-transmit-receive"
ICON_ERR = "dialog-error"

STATE_FILE = pathlib.Path("~/.config/dusky/settings/warp_state").expanduser()

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


def ensure_tos_accepted(current_status: str) -> str:
    """If registration/TOS appears pending, attempt to accept via PTY.
    Returns the refreshed status string."""
    if not status_needs_registration(current_status):
        return current_status
    if not shutil.which("warp-cli"):
        return current_status

    _accept_tos_via_pty(["warp-cli", "registration", "new"])
    time.sleep(0.5)
    return get_warp_status()


# ─── Core Logic ──────────────────────────────────────────────────────────


def connect_warp() -> bool:
    log_info("Initiating connection sequence...")
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

    status = ensure_tos_accepted(get_warp_status())

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
