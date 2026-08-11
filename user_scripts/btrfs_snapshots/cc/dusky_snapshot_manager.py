#!/usr/bin/env python3
"""
Dusky Btrfs & Snapper Master Controller  --  v3.0.0 "atomic exchange"

Target platform (hard requirement, no compatibility shims):
    * Arch Linux, kernel >= 7.1
    * btrfs-progs >= 6.19
    * snapper >= 0.13
    * util-linux >= 2.42 (mount --mkdir, findmnt --json)
    * systemd >= 259
    * Python >= 3.16 (PEP 649 lazy annotations, PEP 750 t-strings era)
    * fzf >= 0.6x

Architectural invariants
------------------------
1.  ACTIVATION IS ONE SYSCALL.  The live subvolume and the staged clone are
    swapped with renameat2(RENAME_EXCHANGE).  btrfs implements
    btrfs_rename_exchange() and explicitly permits exchanging two subvolume
    links (fs/btrfs/inode.c: both inodes are BTRFS_FIRST_FREE_OBJECTID).
    There is therefore NO instant at which the live path is absent, not even
    if the machine loses power mid-operation.  The old two-step
    "rename away, then rename in" window is a genuine unbootable-system
    hazard and has been removed.

2.  ROLLBACK IS ALSO ONE SYSCALL.  Undo of a committed exchange is another
    RENAME_EXCHANGE, so multi-subvolume (root+home) transactions unwind
    exactly, with no partially-restored state.

3.  DEFERRED DELETION NEEDS NO INTERPRETER.  The generated boot-time unit is
    pure systemd + /usr/bin/btrfs.  It never re-enters this script, so a
    restored root that predates Dusky (or ships a different Python) still
    cleans itself up.  Paths are embedded with correct systemd Exec quoting
    ('%' -> '%%', '$' -> '$$', double-quoted) instead of the broken
    systemd-escape --path round trip, in which a '-' in a subvolume name
    silently becomes '/' on unescape.

4.  NOTHING DESTRUCTIVE HAPPENS WITHOUT A UUID PROOF.  Every private
    subvolid=5 mount is verified: the mounted tree must report the expected
    filesystem UUID and subvolume id 5, and must be non-empty.  A wrong-disk
    restore is not survivable, so it is checked, not assumed.
"""

import argparse
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid as uuidlib
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NoReturn

type JSONDict = dict[str, Any]

DUSKY_VERSION: Final = "3.0.0"

# argv[0] must be frozen *before* anything chdir()s, otherwise the fzf preview
# command line is rebuilt against the wrong cwd (a real defect in v2).
SCRIPT_PATH: Final = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else Path(__file__).resolve()

RUN_DIR: Final = Path("/run/dusky")
MNT_ROOT: Final = RUN_DIR / "mnt"
LOCK_PATH: Final = RUN_DIR / "dusky.lock"
STATE_DIR: Final = Path("/var/lib/dusky")
LOG_PATH: Final = Path("/var/log/dusky.log")

TAG_RETIRED: Final = "_to_delete_"
TAG_STAGED: Final = "_dusky_new_"
TAG_SEND: Final = ".tmp_send_"
TAG_RECV: Final = ".btrfs_recv_"
TRANSIENT_TOKENS: Final = (TAG_RETIRED, TAG_STAGED, "_restore_", TAG_SEND, TAG_RECV)

# Only used as a *hint*; real snapshot roots are discovered from snapper and
# from the live mount table. Never used to authorise a deletion.
HINT_SNAPSHOT_ROOTS: Final = frozenset({"@snapshots", "@home_snapshots"})

SAFE_NAME_RE: Final = re.compile(r"\A[A-Za-z0-9@._+:=-]{1,200}\Z")
SUBVOL_LIST_ID_RE: Final = re.compile(r"\bID\s+(\d+)\b")
SUBVOL_LIST_PATH_RE: Final = re.compile(r"\bpath\s+(.+)\Z")
SUBVOL_LIST_UUID_RE: Final = re.compile(r"\buuid\s+([0-9a-fA-F-]{36})\b")
SUBVOL_SHOW_ID_RE: Final = re.compile(r"^\s*Subvolume ID:\s*(\d+)\s*$", re.MULTILINE)
SUBVOL_SHOW_UUID_RE: Final = re.compile(r"^\s*UUID:\s*([0-9a-fA-F-]{36})\s*$", re.MULTILINE)
SUBVOL_SHOW_RECV_RE: Final = re.compile(r"^\s*Received UUID:\s*([0-9a-fA-F-]{36})\s*$", re.MULTILINE)
SUBVOL_SHOW_FLAGS_RE: Final = re.compile(r"^\s*Flags:\s*(.+)$", re.MULTILINE)
GET_DEFAULT_RE: Final = re.compile(r"\bID\s+(\d+)\b")

SUBPROCESS_ENV: Final = {
    **{k: v for k, v in os.environ.items() if not k.startswith(("LC_", "LANG"))},
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    "COLUMNS": "200",
}

# ANSI palette (Catppuccin-ish 256 colour approximations)
C_RESET = "\033[0m"
C_ERR = "\033[1;38;5;196m"
C_WARN = "\033[1;38;5;220m"
C_OK = "\033[1;38;5;114m"
C_INFO = "\033[1;38;5;81m"
C_ACCENT = "\033[1;38;5;213m"
C_DIM = "\033[38;5;246m"
C_RULE = "\033[38;5;238m"


# =============================================================================
# ERRORS
# =============================================================================
class DuskyError(RuntimeError):
    """Recoverable-at-top-level fatal condition. Never raised mid-unwind."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class DuskyAbort(DuskyError):
    """User aborted / non-interactive context."""


def die(message: str, exit_code: int = 1) -> NoReturn:
    raise DuskyError(message, exit_code)


# =============================================================================
# LOGGING
# =============================================================================
def _build_logger() -> logging.Logger:
    log = logging.getLogger("dusky")
    log.setLevel(logging.INFO)
    log.propagate = False

    with suppress(Exception):
        journal = logging.handlers.SysLogHandler(address="/dev/log")
        journal.setFormatter(logging.Formatter("dusky[%(process)d]: %(levelname)s %(message)s"))
        log.addHandler(journal)

    with suppress(OSError):
        existed = LOG_PATH.exists()
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        if not existed:
            with suppress(OSError):
                LOG_PATH.chmod(0o600)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(handler)

    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


LOG: Final = _build_logger()


# =============================================================================
# TERMINAL I/O  --  everything interactive goes through /dev/tty
# =============================================================================
_TTY_IN: Any = None
_TTY_OUT: Any = None


def _tty() -> tuple[Any, Any] | None:
    """
    Open the controlling terminal directly.

    fzf owns /dev/tty for its own UI but releases it on exit; stdin/stdout of
    this process may be pipes (fzf preview children, --json consumers, systemd).
    Reading prompts from sys.stdin in those contexts silently returns EOF and
    was a source of 'the TUI ignored my answer' behaviour.
    """
    global _TTY_IN, _TTY_OUT
    if _TTY_IN is None or _TTY_IN.closed:
        try:
            _TTY_IN = open("/dev/tty", "r", encoding="utf-8", errors="replace")
            _TTY_OUT = open("/dev/tty", "w", encoding="utf-8", errors="replace")
        except OSError:
            return None
    return _TTY_IN, _TTY_OUT


def interactive() -> bool:
    return _tty() is not None


def ask(prompt: str) -> str:
    pair = _tty()
    if pair is None:
        raise DuskyAbort("[!] Interactive input required but no controlling terminal is available.")
    tin, tout = pair
    tout.write(prompt)
    tout.flush()
    line = tin.readline()
    if line == "":
        raise DuskyAbort("[!] Aborted (EOF on terminal).")
    return line.strip()


def confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    while True:
        try:
            answer = ask(f"\n{C_WARN}{prompt} [y/N]: {C_RESET}").lower()
        except DuskyAbort:
            return False
        except KeyboardInterrupt:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        say(f"{C_DIM}Please answer y or n.{C_RESET}")


def pause(message: str = "Press Enter to return...") -> None:
    with suppress(DuskyAbort, KeyboardInterrupt):
        ask(f"\n{C_OK}{message}{C_RESET}")


def say(text: str = "") -> None:
    print(text, flush=True)


def warn(text: str) -> None:
    print(f"{C_WARN}{text}{C_RESET}", file=sys.stderr, flush=True)
    LOG.warning(strip_ansi(text))


def note(text: str) -> None:
    print(f"{C_INFO}{text}{C_RESET}", flush=True)


def good(text: str) -> None:
    print(f"{C_OK}{text}{C_RESET}", flush=True)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def display_width(text: str) -> int:
    width = 0
    for ch in strip_ansi(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


# =============================================================================
# PROCESS EXECUTION
# =============================================================================
@dataclass(frozen=True, slots=True)
class Proc:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip()

    @property
    def message(self) -> str:
        return self.stderr.strip() or self.stdout.strip() or "<no output>"


def run(*argv: str, check: bool = False, timeout: float | None = 900.0, stdin_text: str | None = None) -> Proc:
    cmd = [str(a) for a in argv]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=SUBPROCESS_ENV,
            timeout=timeout,
            input=stdin_text,
            check=False,
        )
    except FileNotFoundError as exc:
        die(f"[!] Missing executable: {cmd[0]} ({exc})")
    except subprocess.TimeoutExpired:
        die(f"[!] Timed out after {timeout}s: {shlex.join(cmd)}")
    except OSError as exc:
        die(f"[!] Failed to execute: {shlex.join(cmd)}\n    {exc}")

    proc = Proc(cmd, completed.returncode, completed.stdout or "", completed.stderr or "")
    if not proc.ok:
        LOG.debug("cmd failed rc=%s %s :: %s", proc.returncode, shlex.join(cmd), proc.message)
    if check and not proc.ok:
        die(f"[!] Command failed ({proc.returncode}): {shlex.join(cmd)}\n    {proc.message}", proc.returncode)
    return proc


def run_tty(*argv: str) -> int:
    cmd = [str(a) for a in argv]
    try:
        return subprocess.run(cmd, env=SUBPROCESS_ENV, check=False).returncode
    except OSError as exc:
        die(f"[!] Failed to execute: {shlex.join(cmd)}\n    {exc}")


def require_tools(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        die(f"[!] Missing required tool(s): {', '.join(missing)}")


# =============================================================================
# PRIVILEGES
# =============================================================================
def ensure_root() -> None:
    if os.geteuid() == 0:
        return
    if shutil.which("sudo") is None:
        die("[!] Root privileges are required and sudo is not installed.")
    print(f"{C_WARN}[*] Elevating via sudo...{C_RESET}", file=sys.stderr, flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    argv = ["sudo", "--", sys.executable, str(SCRIPT_PATH), *sys.argv[1:]]
    try:
        os.execvp("sudo", argv)
    except OSError as exc:
        die(f"[!] Failed to elevate privileges: {exc}")


# =============================================================================
# LOCKING  (re-entrant: a nested 'with dusky_lock()' in one process is a no-op,
# because flock on a second file description in the same process deadlocks.)
# =============================================================================
_LOCK_FD: int | None = None
_LOCK_DEPTH = 0


@contextmanager
def dusky_lock(*, wait: bool = True) -> Iterator[None]:
    global _LOCK_FD, _LOCK_DEPTH
    if _LOCK_DEPTH > 0:
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return

    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        die(f"[!] Cannot create lock {LOCK_PATH}: {exc}")

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not wait:
                die("[!] Another Dusky operation holds the lock. Refusing to queue.")
            warn("[*] Another Dusky operation is in progress; waiting for the lock...")
            fcntl.flock(fd, fcntl.LOCK_EX)
        with suppress(OSError):
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        _LOCK_FD, _LOCK_DEPTH = fd, 1
        yield
    finally:
        _LOCK_DEPTH -= 1
        if _LOCK_DEPTH <= 0:
            _LOCK_FD, _LOCK_DEPTH = None, 0
            with suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(fd)


@contextmanager
def critical_section() -> Iterator[None]:
    """
    Block (do not discard) terminating signals across the activation window.

    pthread_sigmask is strictly better than signal.SIG_IGN: a Ctrl-C pressed
    inside the window is *queued* and delivered the instant the window closes,
    so the user's intent is honoured rather than silently swallowed.
    """
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


# =============================================================================
# renameat2(RENAME_EXCHANGE)
# =============================================================================
AT_FDCWD: Final = -100
RENAME_NOREPLACE: Final = 1 << 0
RENAME_EXCHANGE: Final = 1 << 1


def _load_libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c") or "libc.so.6"
    lib = ctypes.CDLL(name, use_errno=True)
    if not hasattr(lib, "renameat2"):
        die("[!] glibc does not export renameat2(); atomic activation is unavailable.")
    lib.renameat2.restype = ctypes.c_int
    lib.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    return lib


_LIBC: Final = _load_libc()


def rename_exchange(left: Path, right: Path) -> None:
    """Atomically swap two directory entries (both must exist)."""
    rc = _LIBC.renameat2(AT_FDCWD, os.fsencode(left), AT_FDCWD, os.fsencode(right), RENAME_EXCHANGE)
    if rc != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(left), None, str(right))


def rename_noreplace(src: Path, dst: Path) -> None:
    rc = _LIBC.renameat2(AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE)
    if rc != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(src), None, str(dst))


def probe_exchange(top: Path) -> bool:
    """
    Empirically prove RENAME_EXCHANGE works on subvolume links of *this*
    filesystem before we bet the root filesystem on it.
    """
    tag = f".dusky_probe_{os.getpid()}_{int(time.time())}"
    a, b = top / f"{tag}_a", top / f"{tag}_b"
    try:
        if not run("btrfs", "subvolume", "create", str(a)).ok:
            return False
        if not run("btrfs", "subvolume", "create", str(b)).ok:
            return False
        try:
            rename_exchange(a, b)
        except OSError as exc:
            LOG.error("RENAME_EXCHANGE probe failed: %s", exc)
            return False
        return True
    finally:
        for path in (a, b):
            if path.exists():
                run("btrfs", "subvolume", "delete", "--", str(path))


# =============================================================================
# MOUNT / DEVICE RESOLUTION
# =============================================================================
@dataclass(frozen=True, slots=True)
class Filesystem:
    uuid: str
    source: str

    @property
    def mount_source(self) -> str:
        """Always mount by UUID: /dev/sdX enumeration is not stable between
        the findmnt call and the mount call, and multi-device btrfs is only
        addressable coherently by fsid."""
        return f"UUID={self.uuid}"


def findmnt_entries(target: str | None = None, *, fstab: bool = False) -> list[JSONDict]:
    argv = ["findmnt", "--json", "--evaluate", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS,UUID,PROPAGATION"]
    if fstab:
        argv.append("--fstab")
    if target is not None:
        argv += ["--mountpoint", target]
    proc = run(*argv)
    if not proc.ok or not proc.text:
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    entries: list[JSONDict] = []
    def _walk(items: list[JSONDict]) -> None:
        for item in items:
            entries.append(item)
            if "children" in item and isinstance(item["children"], list):
                _walk(item["children"])
    _walk(payload.get("filesystems", []))
    return entries


def effective_mount(target: str) -> JSONDict | None:
    """
    Return the *topmost* mount at a mountpoint.

    findmnt lists stacked mounts in mount order; the last one is what the
    kernel actually resolves. v2 took the first entry, so an over-mounted
    path (a half-finished remount, a container bind) resolved to the wrong
    subvolume - and that subvolume would then be renamed away by a restore.
    """
    entries = findmnt_entries(target)
    exact = [e for e in entries if str(e.get("target", "")) == target] or entries
    return exact[-1] if exact else None


def subvol_from_options(options: str) -> str | None:
    match = re.search(r"(?:\A|,)subvol=([^,]+)(?:,|\Z)", options.strip())
    if not match:
        return None
    return match.group(1).strip().strip('"').lstrip("/") or None


def strip_source_subvol(source: str) -> str:
    return re.sub(r"\[.*?\]\Z", "", source).strip()


def filesystem_of(mountpoint: str) -> Filesystem:
    entry = effective_mount(mountpoint)
    if entry is None:
        die(f"[!] {mountpoint} is not a mount point.")
    if str(entry.get("fstype", "")) != "btrfs":
        die(f"[!] {mountpoint} is not btrfs (fstype={entry.get('fstype')!r}).")

    fs_uuid = str(entry.get("uuid") or "").strip()
    source = strip_source_subvol(str(entry.get("source") or ""))

    if not fs_uuid and source.startswith("/dev/"):
        blk = run("blkid", "-s", "UUID", "-o", "value", os.path.realpath(source))
        fs_uuid = blk.text.splitlines()[0].strip() if blk.ok and blk.text else ""
    if not fs_uuid and source.startswith("UUID="):
        fs_uuid = source.split("=", 1)[1].strip()
    if not fs_uuid:
        die(f"[!] Could not resolve the btrfs filesystem UUID for {mountpoint} (source={source or '<none>'}).")
    return Filesystem(uuid=fs_uuid, source=source)


def subvol_show(path: str) -> JSONDict | None:
    proc = run("btrfs", "subvolume", "show", "--", str(path))
    if not proc.ok:
        return None
    lines = proc.stdout.splitlines()
    header = lines[0].strip() if lines else ""
    rel = "" if header.endswith("is btrfs root") or header == "/" else header.lstrip("/")
    id_match = SUBVOL_SHOW_ID_RE.search(proc.stdout)
    uuid_match = SUBVOL_SHOW_UUID_RE.search(proc.stdout)
    recv_match = SUBVOL_SHOW_RECV_RE.search(proc.stdout)
    flags_match = SUBVOL_SHOW_FLAGS_RE.search(proc.stdout)
    return {
        "path": rel,
        "id": int(id_match.group(1)) if id_match else None,
        "uuid": uuid_match.group(1) if uuid_match else "",
        "received_uuid": recv_match.group(1) if recv_match else "",
        "readonly": bool(flags_match and "readonly" in flags_match.group(1)),
    }


def active_subvol(mountpoint: str, *, required: bool = True) -> tuple[str, int] | None:
    """
    Resolve (relative subvolume path, subvolume id) for a live mount point.

    Kernel truth (BTRFS_IOC_GET_SUBVOL_INFO via 'btrfs subvolume show') is the
    primary source; the mount option string is used purely as a cross-check.
    A disagreement means something is over-mounted and we refuse to proceed.
    """
    info = subvol_show(mountpoint)
    entry = effective_mount(mountpoint)
    opt_subvol = subvol_from_options(str(entry.get("options", ""))) if entry else None

    if info is not None and info["id"] is not None:
        rel = str(info["path"]).strip("/")
        if opt_subvol and rel and opt_subvol.strip("/") != rel:
            die(
                f"[!] Inconsistent view of {mountpoint}: mount says subvol={opt_subvol!r} "
                f"but the kernel reports {rel!r}. Refusing to operate on an ambiguous mount."
            )
        if rel:
            return rel, int(info["id"])
        if required:
            die(
                f"[!] {mountpoint} is the top-level tree (subvolid=5). Dusky refuses to "
                "restore over an FS_TREE mount; boot with subvol=@ style layouts."
            )
        return None

    if opt_subvol:
        die(f"[!] btrfs could not describe {mountpoint} even though it is mounted with subvol={opt_subvol}.")
    if required:
        die(f"[!] Could not determine the active btrfs subvolume for {mountpoint}.")
    return None


def is_mountpoint(path: str | Path) -> bool:
    return run("mountpoint", "-q", "--", str(path)).ok


# =============================================================================
# PRIVATE TOP-LEVEL MOUNTS
# =============================================================================
def _ensure_private_mnt_root() -> None:
    MNT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not is_mountpoint(MNT_ROOT):
        run("mount", "--bind", str(MNT_ROOT), str(MNT_ROOT))
    # Stop our transient subvolid=5 mounts propagating into every other mount
    # namespace on the box (systemd makes / shared by default).
    run("mount", "--make-private", str(MNT_ROOT))


def sweep_stale_mounts() -> int:
    """Reap /run/dusky/mnt leftovers from a killed run. /run is tmpfs, so a
    reboot clears these, but a crashed session must not wedge the next one."""
    if not MNT_ROOT.is_dir():
        return 0
    reaped = 0
    for child in sorted(MNT_ROOT.iterdir()):
        if not child.is_dir() or not child.name.startswith("top_"):
            continue
        if is_mountpoint(child):
            if not run("umount", "--", str(child)).ok:
                run("umount", "--lazy", "--", str(child))
        with suppress(OSError):
            child.rmdir()
            reaped += 1
    return reaped


@contextmanager
def top_level(fs: Filesystem, *, writable: bool = True, quiet: bool = False) -> Iterator[Path]:
    """
    Mount subvolid=5 privately and verify we mounted what we intended.

    Never silently degrades to read-only: a read-only fallback under a caller
    that is about to rename subvolumes just produces EROFS half way through a
    transaction.
    """
    _ensure_private_mnt_root()
    sweep_stale_mounts()

    mnt = Path(tempfile.mkdtemp(prefix="top_", dir=str(MNT_ROOT)))
    opts = "subvolid=5,nodev,nosuid,noexec,noatime"
    if not writable:
        opts += ",ro"

    if not quiet:
        note(f"[*] Mounting top-level tree (subvolid=5) of UUID={fs.uuid}...")

    mounted = run("mount", "-t", "btrfs", "-o", opts, fs.mount_source, str(mnt))
    if not mounted.ok and not writable:
        # A read-only VFS flag can collide with an existing read-write
        # superblock for the same fsid; the tree is still safe to read.
        mounted = run("mount", "-t", "btrfs", "-o", "subvolid=5,nodev,nosuid,noexec,noatime", fs.mount_source, str(mnt))
    if not mounted.ok:
        with suppress(OSError):
            mnt.rmdir()
        die(f"[!] Failed to mount subvolid=5 for UUID={fs.uuid}:\n    {mounted.message}")

    try:
        seen = effective_mount(str(mnt))
        seen_uuid = str((seen or {}).get("uuid") or "").strip()
        info = subvol_show(str(mnt))
        if seen_uuid and seen_uuid != fs.uuid:
            die(f"[!] REFUSING TO CONTINUE: mounted UUID={seen_uuid} but expected UUID={fs.uuid}.")
        if info is None or info["id"] not in (5, None):
            die(f"[!] {mnt} is not the top-level (subvolid=5) tree of UUID={fs.uuid}.")
        if not any(mnt.iterdir()):
            die(
                f"[!] The top-level tree of UUID={fs.uuid} is empty. This is the signature of a "
                "wrong/zeroed device or a failed multi-device assembly. Aborting."
            )
        yield mnt
    finally:
        if not quiet:
            note("[*] Unmounting top-level tree...")
        detached = False
        last = ""
        for attempt in range(4):
            result = run("umount", "--", str(mnt))
            if result.ok:
                detached = True
                break
            last = result.message
            time.sleep(0.4 * (attempt + 1))
        if not detached:
            run("umount", "--lazy", "--", str(mnt))
            LOG.warning("Lazy-unmounted %s after failures: %s", mnt, last)
        with suppress(OSError):
            mnt.rmdir()


# =============================================================================
# SUBVOLUME ENUMERATION  (no extra mount needed: 'btrfs subvolume list -a'
# walks the whole root tree of the filesystem from any mount point on it.)
# =============================================================================
@dataclass(slots=True)
class Subvolume:
    id: int
    path: str
    uuid: str
    fs_uuid: str
    readonly: bool
    mount_target: str
    mounted_at: str = ""

    def as_meta(self) -> JSONDict:
        return {
            "id": str(self.id),
            "path": self.path,
            "uuid": self.uuid,
            "fs_uuid": self.fs_uuid,
            "is_ro": self.readonly,
            "mount_target": self.mount_target,
            "mounted_at": self.mounted_at,
        }


def parse_subvol_list(output: str) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        id_match = SUBVOL_LIST_ID_RE.search(line)
        path_match = SUBVOL_LIST_PATH_RE.search(line)
        if not id_match or not path_match:
            continue
        path = path_match.group(1).strip()
        if path.startswith("<FS_TREE>/"):
            path = path[len("<FS_TREE>/") :]
        uuid_match = SUBVOL_LIST_UUID_RE.search(line)
        rows.append((int(id_match.group(1)), path.strip("/"), uuid_match.group(1) if uuid_match else ""))
    return rows


def btrfs_filesystems() -> dict[str, tuple[Filesystem, str]]:
    """fs_uuid -> (Filesystem, a mount point on it)."""
    result: dict[str, tuple[Filesystem, str]] = {}
    for entry in findmnt_entries():
        if str(entry.get("fstype")) != "btrfs":
            continue
        target = str(entry.get("target") or "")
        fs_uuid = str(entry.get("uuid") or "").strip()
        if not fs_uuid or not target:
            continue
        if fs_uuid in result:
            # Prefer the shortest mount point: closest to the fs root.
            if len(target) < len(result[fs_uuid][1]):
                result[fs_uuid] = (result[fs_uuid][0], target)
            continue
        result[fs_uuid] = (Filesystem(fs_uuid, strip_source_subvol(str(entry.get("source") or ""))), target)
    return result


def mounted_subvol_paths() -> dict[str, str]:
    """relative subvolume path -> mount point, for every live btrfs mount."""
    mapping: dict[str, str] = {}
    for entry in findmnt_entries():
        if str(entry.get("fstype")) != "btrfs":
            continue
        target = str(entry.get("target") or "")
        sv = subvol_from_options(str(entry.get("options") or ""))
        if sv:
            mapping.setdefault(sv.strip("/"), target)
        elif target:
            info = subvol_show(target)
            if info and info["path"]:
                mapping.setdefault(str(info["path"]).strip("/"), target)
    return mapping


def snapshot_roots() -> set[str]:
    roots: set[str] = set()
    for cfg in snapper_configs():
        base = cfg["subvolume"]
        snaps = "/.snapshots" if base == "/" else f"{base.rstrip('/')}/.snapshots"
        resolved = active_subvol(snaps, required=False)
        if resolved:
            roots.add(resolved[0].strip("/"))
    for path, _target in mounted_subvol_paths().items():
        if path.endswith("_snapshots") or path == ".snapshots" or path.endswith("/.snapshots"):
            roots.add(path)
    for hint in HINT_SNAPSHOT_ROOTS:
        roots.add(hint)
    return {r for r in roots if r}


def classify_subvol(path: str, roots: set[str]) -> str:
    """'transient' | 'snapshot' | 'normal'"""
    p = path.strip("/")
    if any(token in p for token in TRANSIENT_TOKENS):
        return "transient"
    if p == ".snapshots" or p.startswith(".snapshots/") or "/.snapshots/" in f"/{p}":
        return "snapshot"
    for root in roots:
        if p == root or p.startswith(root + "/"):
            return "snapshot"
    if re.fullmatch(r"(?:.*/)?\.snapshots/\d+/snapshot", p):
        return "snapshot"
    if re.fullmatch(r"(?:.*/)?[^/]+_snapshots/\d+/snapshot", p):
        return "snapshot"
    return "normal"


def enumerate_subvolumes(*, include_snapshots: bool = False, include_transient: bool = False) -> list[Subvolume]:
    roots = snapshot_roots()
    live = mounted_subvol_paths()
    found: list[Subvolume] = []

    for fs_uuid, (fs, mount_target) in btrfs_filesystems().items():
        listing = run("btrfs", "subvolume", "list", "-a", "--sort=path", "--", mount_target)
        if not listing.ok:
            LOG.error("subvolume list failed on %s: %s", mount_target, listing.message)
            continue
        ro_listing = run("btrfs", "subvolume", "list", "-a", "-r", "--", mount_target)
        ro_ids = {sid for sid, _p, _u in parse_subvol_list(ro_listing.stdout)} if ro_listing.ok else set()

        for sid, path, sv_uuid in parse_subvol_list(listing.stdout):
            kind = classify_subvol(path, roots)
            if kind == "snapshot" and not include_snapshots:
                continue
            if kind == "transient" and not include_transient:
                continue
            found.append(
                Subvolume(
                    id=sid,
                    path=path,
                    uuid=sv_uuid,
                    fs_uuid=fs_uuid,
                    readonly=sid in ro_ids,
                    mount_target=mount_target,
                    mounted_at=live.get(path, ""),
                )
            )
    return found


def protected_subvolumes() -> set[str]:
    """
    Subvolumes that must never be deleted by the TUI.

    Union of: every live mount's subvolume, every snapper snapshot root, every
    subvol referenced by /etc/fstab, and the filesystem default subvolume
    (which, if deleted, bricks the boot even though nothing has it mounted).
    """
    protected: set[str] = set(mounted_subvol_paths().keys())
    protected |= snapshot_roots()

    with suppress(OSError):
        for line in Path("/etc/fstab").read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) >= 4 and fields[2] == "btrfs":
                sv = subvol_from_options(fields[3])
                if sv:
                    protected.add(sv.strip("/"))

    for fs_uuid, (_fs, mount_target) in btrfs_filesystems().items():
        default = run("btrfs", "subvolume", "get-default", "--", mount_target)
        if not default.ok:
            continue
        match = GET_DEFAULT_RE.search(default.stdout)
        if not match or match.group(1) == "5":
            continue
        default_id = int(match.group(1))
        listing = run("btrfs", "subvolume", "list", "-a", "--", mount_target)
        if listing.ok:
            for sid, path, _u in parse_subvol_list(listing.stdout):
                if sid == default_id:
                    protected.add(path)
    return {p for p in protected if p}


# =============================================================================
# SNAPPER LAYER
# =============================================================================
def snapper_configs() -> list[dict[str, str]]:
    configs: list[dict[str, str]] = []
    config_dir = Path("/etc/snapper/configs")
    if config_dir.is_dir():
        for cfg_file in sorted(config_dir.iterdir()):
            if not cfg_file.is_file() or cfg_file.name.startswith("."):
                continue
            if cfg_file.name.endswith((".pacnew", ".pacsave", ".bak", ".old", "~")):
                continue
            subvolume = "/"
            with suppress(OSError):
                match = re.search(
                    r'^SUBVOLUME="?([^"\n]+)"?', cfg_file.read_text(errors="replace"), re.MULTILINE
                )
                if match:
                    subvolume = os.path.normpath(match.group(1).strip())
            configs.append({"config": cfg_file.name, "subvolume": subvolume})
    return configs


def snapper_config_subvolume(config: str) -> str:
    for cfg in snapper_configs():
        if cfg["config"] == config:
            return cfg["subvolume"]
    proc = run("snapper", "-c", config, "get-config")
    if proc.ok:
        for line in proc.stdout.splitlines():
            key, sep, value = line.replace("\u2502", "|").partition("|")
            if sep and key.strip() == "SUBVOLUME" and value.strip():
                return os.path.normpath(value.strip())
    die(f"[!] Unknown snapper config {config!r}.")


def snapshots_mountpoint(target_mnt: str) -> str:
    return "/.snapshots" if target_mnt == "/" else f"{target_mnt.rstrip('/')}/.snapshots"


def validate_snap_id(raw: str) -> str:
    value = str(raw).strip()
    if not value.isdigit() or int(value) <= 0:
        die(f"[!] Invalid snapshot id: {raw!r}")
    return str(int(value))


def parse_dt(raw: object, *, assume_utc: bool = True) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value > 1_000_000_000_000:
            value /= 1000.0
        with suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value, tz=UTC)
        return None

    text = str(raw).strip()
    if not text:
        return None
    candidates = [text]
    if " " in text:
        candidates.append(text.replace(" ", "T", 1))
    for candidate in candidates:
        with suppress(ValueError):
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None and assume_utc else parsed.astimezone(UTC)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%a %d %b %Y %H:%M:%S", "%a %b %d %H:%M:%S %Y"):
        with suppress(ValueError):
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC) if assume_utc else parsed.astimezone(UTC)
    return None


def human_date(raw: object) -> str:
    parsed = parse_dt(raw)
    return parsed.astimezone().strftime("%m/%d/%y %H:%M") if parsed else (str(raw).strip() if raw else "")


def time_ago(moment: datetime | None) -> str:
    if moment is None:
        return "unknown"
    seconds = int((datetime.now(UTC) - moment.astimezone(UTC)).total_seconds())
    if seconds < 0:
        return "future"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 2592000:
        return f"{seconds // 86400}d ago"
    return f"{seconds // 2592000}mo ago"


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def parse_userdata(raw: object) -> dict[str, str]:
    data: dict[str, str] = {}
    if raw is None:
        return data
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if k is not None}
    for part in re.split(r"[;,]", str(raw)):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip():
            data[key.strip()] = value.strip()
    return data


def _looks_like_snapshot(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    return _first(obj, "number", "id", "num", "#") is not None and (
        _first(obj, "date", "timestamp", "time", "description", "type") is not None
    )


def _find_records(obj: object, depth: int = 0) -> list[JSONDict] | None:
    if depth > 8:
        return None
    if isinstance(obj, list):
        if obj and all(isinstance(i, dict) for i in obj) and any(_looks_like_snapshot(i) for i in obj):
            return [dict(i) for i in obj]
        for item in obj:
            found = _find_records(item, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(obj, dict):
        for key in ("snapshots", "entries", "data", "list", "rows"):
            if key in obj:
                found = _find_records(obj[key], depth + 1)
                if found is not None:
                    return found
        columns, rows = obj.get("columns"), obj.get("rows")
        if isinstance(columns, list) and isinstance(rows, list) and rows and all(isinstance(r, (list, tuple)) for r in rows):
            names: list[str] = []
            for column in columns:
                label = column if isinstance(column, str) else str(_first(column, "name", "key", "id", "title") or "")
                normalised = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
                names.append({"num": "number", "#": "number", "desc": "description"}.get(normalised, normalised))
            records = [
                {(names[i] if i < len(names) and names[i] else f"col_{i}"): value for i, value in enumerate(row)}
                for row in rows
            ]
            if any(_looks_like_snapshot(r) for r in records):
                return records
        for value in obj.values():
            found = _find_records(value, depth + 1)
            if found is not None:
                return found
    return None


def parse_snapper_table(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = [p.strip() for p in re.split(r"[|\u2502]", line)]
        if len(parts) < 7:
            continue
        snap_id = re.sub(r"[*+-]+\Z", "", parts[0])
        if not snap_id.isdigit() or snap_id == "0":
            continue
        records.append(
            {
                "number": snap_id,
                "type": parts[1],
                "pre_number": parts[2],
                "date": parts[3],
                "user": parts[4],
                "cleanup": parts[5],
                "description": parts[6],
                "userdata": "|".join(parts[7:]).strip() if len(parts) > 7 else "",
            }
        )
    return records


def records_to_rows(records: Sequence[JSONDict], config: str) -> list[dict[str, Any]]:
    target = snapper_config_subvolume(config)
    snaps_mnt = snapshots_mountpoint(target)
    snaps_live = Path(snaps_mnt).is_mount()
    rows: list[dict[str, Any]] = []

    for record in records:
        raw_id = _first(record, "number", "id", "num", "#")
        if raw_id is None:
            continue
        snap_id = re.sub(r"[*+-]+\Z", "", str(raw_id).strip())
        if not snap_id.isdigit() or snap_id == "0":
            continue

        raw_date = _first(record, "date", "timestamp", "time")
        moment = parse_dt(raw_date)
        userdata_raw = _first(record, "userdata", "user_data")
        userdata_dict = parse_userdata(userdata_raw)
        pre_number = str(_first(record, "pre_number", "pre_num") or "").strip()
        location = f"{snaps_mnt}/{snap_id}/snapshot"
        dead = bool(snaps_live and not Path(location).exists())
        description = str(_first(record, "description", "desc") or "")
        if dead and not description.startswith("[DEAD]"):
            description = f"[DEAD] {description}"

        rows.append(
            {
                "config": config,
                "id": snap_id,
                "type": str(_first(record, "type", "snapshot_type") or ""),
                "date": human_date(raw_date),
                "raw_date": "" if raw_date is None else str(raw_date),
                "epoch": moment.timestamp() if moment else None,
                "description": description,
                "cleanup": str(_first(record, "cleanup", "cleanup_algorithm") or ""),
                "userdata": ",".join(f"{k}={v}" for k, v in userdata_dict.items()),
                "userdata_dict": userdata_dict,
                "user": str(_first(record, "user", "creator") or "root"),
                "pre_number": "" if pre_number in ("0", "-") else pre_number,
                "age": time_ago(moment),
                "location": location,
                "dead": dead,
            }
        )
    return rows


def snapshot_rows(config: str) -> list[dict[str, Any]]:
    """
    Always query snapper with --utc --iso.

    Local-time output is ambiguous for one hour every autumn; the coordinated
    pair matcher compares timestamps across two configs with a hard second
    threshold, so a DST fold could otherwise shift a candidate by 3600s and
    either reject a correct pair or accept a wrong one.
    """
    proc = run("snapper", "--jsonout", "--utc", "--iso", "-c", config, "list", "--disable-used-space")
    if proc.ok and proc.text:
        with suppress(json.JSONDecodeError):
            records = _find_records(json.loads(proc.stdout))
            if records is not None:
                return records_to_rows(records, config)
    fallback = run("snapper", "--utc", "--iso", "-c", config, "list", "--disable-used-space")
    if not fallback.ok:
        LOG.error("snapper list failed for %s: %s", config, fallback.message)
        return []
    return records_to_rows(parse_snapper_table(fallback.stdout), config)


def all_snapshot_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cfg in snapper_configs():
        rows.extend(snapshot_rows(cfg["config"]))
    return rows


# =============================================================================
# COORDINATED PAIR MATCHING
# =============================================================================
PAIR_STRICT_SECONDS: Final = 120


@dataclass(frozen=True, slots=True)
class PairMatch:
    left_id: str
    right_id: str
    method: str
    delta: float

    @property
    def exact(self) -> bool:
        return self.method in ("dusky_pair", "identical timestamp")


def find_pair(
    left_cfg: str,
    right_cfg: str,
    *,
    target_date: str | None = None,
    target_desc: str | None = None,
    target_userdata: dict[str, str] | None = None,
    left_id_hint: str | None = None,
    threshold: int = PAIR_STRICT_SECONDS,
) -> PairMatch:
    """
    Resolve a synchronized snapshot pair.

    Ranked strategies, all of which refuse ambiguity rather than guessing:
      1. dusky_pair userdata (uuid4, unique by construction, dusky_role verified)
      2. identical timestamp
      3. identical description + same snapshot type, nearest timestamp within
         'threshold' seconds (default 120s, not 900s)

    v2 defaulted to a 900 second window over *all* home snapshots with no type
    check, so an unrelated hourly timeline snapshot taken 14 minutes later - or
    the 'post' half of a pacman pre/post pair - could be silently accepted and
    restored alongside a 'pre' root snapshot.
    """
    left_rows = snapshot_rows(left_cfg)
    right_rows = snapshot_rows(right_cfg)
    if not left_rows:
        die(f"[!] No snapshots found for config {left_cfg!r}.")
    if not right_rows:
        die(f"[!] No snapshots found for config {right_cfg!r}.")

    left: dict[str, Any] | None = None
    if left_id_hint is not None:
        matches = [r for r in left_rows if r["id"] == str(left_id_hint)]
        if len(matches) == 1:
            left = matches[0]
    if left is None:
        if target_date is None:
            die("[!] Neither a snapshot id nor a target date was supplied for pair matching.")
        wanted = parse_dt(target_date)
        matches = [r for r in left_rows if r["raw_date"] == target_date]
        if not matches and wanted is not None:
            matches = [r for r in left_rows if r["epoch"] is not None and abs(r["epoch"] - wanted.timestamp()) < 1.0]
        if len(matches) > 1:
            ids = ", ".join(r["id"] for r in matches)
            die(f"[!] Ambiguous: {len(matches)} {left_cfg} snapshots match {target_date!r} (ids: {ids}).")
        if not matches:
            die(f"[!] No {left_cfg} snapshot matches {target_date!r}.")
        left = matches[0]

    if left["dead"]:
        die(f"[!] {left_cfg} snapshot {left['id']} is dead (its subvolume is missing).")

    userdata = dict(target_userdata or left.get("userdata_dict") or {})
    pair_id = userdata.get("dusky_pair")
    if pair_id:
        tagged = [
            r
            for r in right_rows
            if r.get("userdata_dict", {}).get("dusky_pair") == pair_id
            and r.get("userdata_dict", {}).get("dusky_role", right_cfg) == right_cfg
        ]
        if len(tagged) > 1:
            die(f"[!] Corrupt pairing: {len(tagged)} {right_cfg} snapshots share dusky_pair={pair_id}.")
        if len(tagged) == 1:
            if tagged[0]["dead"]:
                die(f"[!] Paired {right_cfg} snapshot {tagged[0]['id']} is dead.")
            return PairMatch(left["id"], tagged[0]["id"], "dusky_pair", 0.0)

    same_time = [r for r in right_rows if r["raw_date"] == left["raw_date"] and not r["dead"]]
    if len(same_time) == 1:
        return PairMatch(left["id"], same_time[0]["id"], "identical timestamp", 0.0)
    if len(same_time) > 1 and left["description"]:
        narrowed = [r for r in same_time if r["description"] == left["description"]]
        if len(narrowed) == 1:
            return PairMatch(left["id"], narrowed[0]["id"], "identical timestamp", 0.0)

    if left["epoch"] is None:
        die("[!] The source snapshot has an unparseable timestamp; heuristic matching is unsafe.")

    candidates = [
        r
        for r in right_rows
        if not r["dead"]
        and r["epoch"] is not None
        and (not left["type"] or not r["type"] or r["type"].lower() == left["type"].lower())
        and (not left["description"] or r["description"] == left["description"])
    ]
    if not candidates:
        die(
            f"[!] No {right_cfg} snapshot shares the description "
            f"{left['description']!r} and type {left['type']!r}. Refusing to guess."
        )

    scored = sorted(candidates, key=lambda r: abs(r["epoch"] - left["epoch"]))
    best = scored[0]
    delta = abs(best["epoch"] - left["epoch"])
    if delta > threshold:
        die(
            f"[!] Closest {right_cfg} candidate (id {best['id']}) is {delta:.0f}s away, "
            f"beyond the {threshold}s safety threshold. Create pairs with --create-pair "
            "so they carry dusky_pair userdata."
        )
    if len(scored) > 1 and abs(abs(scored[1]["epoch"] - left["epoch"]) - delta) < 1.0:
        die(f"[!] Ambiguous: {right_cfg} snapshots {best['id']} and {scored[1]['id']} are equidistant.")
    return PairMatch(left["id"], best["id"], "nearest timestamp", delta)


# =============================================================================
# BOOT-TIME CLEANUP UNITS  (pure systemd, no interpreter dependency)
# =============================================================================
def systemd_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


CLEANUP_UNIT_TEMPLATE: Final = """[Unit]
Description=Dusky deferred Btrfs cleanup of {display}
Documentation=man:btrfs-subvolume(8)
DefaultDependencies=yes
After=local-fs.target
ConditionVirtualization=!container

[Service]
Type=oneshot
RemainAfterExit=no
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=infinity
ExecStartPre=/usr/bin/mkdir -p {mnt}
ExecStartPre=/usr/bin/mount -t btrfs -o subvolid=5,nodev,nosuid,noexec -U {fs_uuid} {mnt}
ExecStart=/usr/bin/btrfs subvolume delete -- {victim}
ExecStartPost=/usr/bin/rm -f /etc/systemd/system/%n /etc/systemd/system/multi-user.target.wants/%n
ExecStopPost=-/usr/bin/umount --lazy {mnt}
ExecStopPost=-/usr/bin/rmdir {mnt}

[Install]
WantedBy=multi-user.target
"""


def schedule_boot_cleanup(*, fs_uuid: str, subvol_rel: str, offline_root: Path | None) -> str:
    """
    Emit a one-shot unit that deletes 'subvol_rel' on the next boot.

    Design notes vs the v2 implementation:
      * No Python and no staged script copy. The restored root may be older
        than Dusky, or carry a different interpreter path; requiring it to run
        our script to free disk space was a needless dependency, and the copy
        was written into the doomed subvolume anyway.
      * No systemd-escape round trip. '-' in a subvolume name unescapes to '/',
        and systemd itself unescapes \\xNN sequences in Exec lines, so the v2
        path could be mangled twice. We instead embed the literal path with
        correct systemd Exec quoting.
      * The unit removes itself only in ExecStartPost, i.e. only on success, so
        a failed deletion is retried on the following boot instead of leaking.
    """
    if not SAFE_NAME_RE.fullmatch(subvol_rel.strip("/").replace("/", "")):
        LOG.warning("Cleanup target %r contains unusual characters; relying on systemd quoting.", subvol_rel)

    digest = hashlib.blake2s(f"{fs_uuid}:{subvol_rel}".encode(), digest_size=8).hexdigest()
    unit_name = f"dusky-cleanup-{digest}.service"
    mnt = f"/run/dusky/cleanup-{digest}"
    victim = f"{mnt}/{subvol_rel.strip('/')}"

    content = CLEANUP_UNIT_TEMPLATE.format(
        display=subvol_rel.strip("/").replace("%", "%%"),
        mnt=systemd_quote(mnt),
        fs_uuid=systemd_quote(fs_uuid),
        victim=systemd_quote(victim),
    )

    base = (offline_root / "etc/systemd/system") if offline_root else Path("/etc/systemd/system")
    base.mkdir(parents=True, exist_ok=True)
    unit_path = base / unit_name
    tmp_path = unit_path.with_suffix(".service.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.chmod(0o644)
    os.replace(tmp_path, unit_path)

    # Write the enablement symlink by hand. This is exactly what
    # 'systemctl --root=... enable' does, and it is the only correct method for
    # an offline root that is not yet mounted at /.
    wants = base / "multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    link = wants / unit_name
    with suppress(OSError):
        if link.is_symlink() or link.exists():
            link.unlink()
    link.symlink_to(f"/etc/systemd/system/{unit_name}")

    if offline_root is None:
        run("systemctl", "daemon-reload")

    LOG.info("Scheduled %s to delete %s on UUID=%s", unit_name, subvol_rel, fs_uuid)
    return unit_name


def list_pending_cleanups() -> list[str]:
    base = Path("/etc/systemd/system")
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.glob("dusky-cleanup-*.service"))


# =============================================================================
# RESTORE ENGINE
# =============================================================================
@dataclass(slots=True)
class RestoreTarget:
    config: str
    snap_id: str
    mountpoint: str
    fs: Filesystem
    active_path: str
    active_id: int
    snapshots_path: str


@dataclass(slots=True)
class RestorePlan:
    target: RestoreTarget
    top: Path
    source: Path
    live: Path
    staged: Path
    retired: Path
    staged_created: bool = False
    exchanged: bool = False
    retired_named: bool = False
    new_subvol_id: int | None = None
    notes: list[str] = field(default_factory=list)


def resolve_target(config: str, snap_id: str) -> RestoreTarget:
    snap_id = validate_snap_id(snap_id)
    mountpoint = snapper_config_subvolume(config)
    if not is_mountpoint(mountpoint) and mountpoint != "/":
        die(f"[!] The subvolume for config {config!r} ({mountpoint}) is not mounted; cannot resolve it safely.")

    fs = filesystem_of(mountpoint)
    resolved = active_subvol(mountpoint)
    if resolved is None:
        die(f"[!] Could not resolve the active subvolume for {mountpoint}.")
    active_path, active_id = resolved

    snaps_mnt = snapshots_mountpoint(mountpoint)
    snaps = active_subvol(snaps_mnt, required=False)
    if snaps is None:
        die(
            f"[!] {snaps_mnt} is not a mounted btrfs subvolume. Snapper's snapshot store must be a "
            "real subvolume (for example @snapshots mounted at /.snapshots) for atomic rollback."
        )
    snaps_fs = filesystem_of(snaps_mnt)
    if snaps_fs.uuid != fs.uuid:
        die(f"[!] {snaps_mnt} lives on UUID={snaps_fs.uuid} but {mountpoint} is on UUID={fs.uuid}.")

    return RestoreTarget(
        config=config,
        snap_id=snap_id,
        mountpoint=mountpoint,
        fs=fs,
        active_path=active_path,
        active_id=active_id,
        snapshots_path=snaps[0],
    )


def build_plan(target: RestoreTarget, top: Path, stamp: str) -> RestorePlan:
    live = top / target.active_path
    source = top / target.snapshots_path / target.snap_id / "snapshot"
    return RestorePlan(
        target=target,
        top=top,
        source=source,
        live=live,
        staged=live.with_name(f"{live.name}{TAG_STAGED}{target.snap_id}_{stamp}"),
        retired=live.with_name(f"{live.name}{TAG_RETIRED}{stamp}"),
    )


def assert_flat_topology(plan: RestorePlan) -> None:
    """
    Refuse to restore a subvolume that physically contains nested subvolumes.

    btrfs snapshots are not recursive: nested subvolumes appear as empty
    directories in the clone, and after activation the originals are trapped
    inside the retired subvolume, which then cannot even be deleted
    (BTRFS_IOC_SNAP_DESTROY returns ENOTEMPTY). The classic failure is a
    snapper 'root' config whose /.snapshots is nested inside @ rather than
    being a separate top-level @snapshots subvolume - restoring it would
    orphan every snapshot you own.
    """
    listing = run("btrfs", "subvolume", "list", "-a", "--", str(plan.top))
    if not listing.ok:
        die(f"[!] Could not enumerate subvolumes of the top-level tree:\n    {listing.message}")
    prefix = plan.target.active_path.strip("/") + "/"
    nested = [path for _sid, path, _u in parse_subvol_list(listing.stdout) if path.startswith(prefix)]
    if nested:
        listed = "\n".join(f"      - {n}" for n in nested)
        die(
            f"\n[!] CRITICAL HALT: {plan.target.active_path!r} physically contains nested subvolumes:\n"
            f"{listed}\n"
            "[!] An atomic rollback would strand them inside the retired subvolume and make it\n"
            "    undeletable. Move them to the top level (for example @snapshots, @var_log,\n"
            "    @var_cache) and mount them via fstab before restoring."
        )


def preflight(plans: Sequence[RestorePlan], *, allow_default_fixup: bool) -> None:
    seen: set[str] = set()
    for plan in plans:
        key = f"{plan.target.fs.uuid}:{plan.target.active_path}"
        if key in seen:
            die(f"[!] Two restore targets resolve to the same subvolume: {key}")
        seen.add(key)

        if not plan.source.is_dir():
            die(f"[!] Snapshot {plan.target.snap_id} of {plan.target.config!r} is missing at {plan.source}")
        info = subvol_show(str(plan.source))
        if info is None or info["id"] is None:
            die(f"[!] {plan.source} is not a btrfs subvolume.")
        if not info["readonly"]:
            warn(f"[!] Snapshot {plan.target.snap_id} of {plan.target.config!r} is writable; its content may have drifted.")
        if not plan.live.is_dir():
            die(f"[!] Active subvolume missing at {plan.live}")
        live_info = subvol_show(str(plan.live))
        if live_info is None or live_info["id"] != plan.target.active_id:
            die(
                f"[!] {plan.live} does not match the live mount of {plan.target.mountpoint} "
                f"(expected subvolume id {plan.target.active_id})."
            )
        if plan.staged.exists() or plan.retired.exists():
            die(f"[!] Transient path collision for {plan.target.config!r}; retry in a second.")
        if "%" in plan.retired.name or "$" in plan.retired.name:
            die(f"[!] Refusing to generate a cleanup unit for the unsafe name {plan.retired.name!r}.")

        assert_flat_topology(plan)

        default = run("btrfs", "subvolume", "get-default", "--", str(plan.top))
        match = GET_DEFAULT_RE.search(default.stdout) if default.ok else None
        if match and int(match.group(1)) == plan.target.active_id and not allow_default_fixup:
            die(
                f"[!] The filesystem default subvolume is id {plan.target.active_id}, which is exactly the "
                "subvolume being replaced. After the swap the bootloader would still select the OLD "
                "subvolume, which is scheduled for deletion - i.e. an unbootable system. Re-run with "
                "--fix-default to have Dusky repoint the default subvolume atomically after activation."
            )


def activate(plans: Sequence[RestorePlan]) -> None:
    """
    One RENAME_EXCHANGE per plan. Failure at plan N unwinds plans 0..N-1 with
    the inverse exchange, so the filesystem is never left half-restored and
    never left without the live subvolume path.
    """
    committed: list[RestorePlan] = []
    try:
        with critical_section():
            for plan in plans:
                note(f"[*] Activating {plan.target.config!r}: atomic exchange of {plan.live.name}")
                rename_exchange(plan.live, plan.staged)
                plan.exchanged = True
                committed.append(plan)
            for plan in plans:
                # plan.staged now holds the previous live subvolume.
                try:
                    rename_noreplace(plan.staged, plan.retired)
                    plan.retired_named = True
                except OSError as exc:
                    plan.notes.append(f"could not rename retired subvolume: {exc}")
                    plan.retired = plan.staged
                    plan.retired_named = True
    except OSError as exc:
        for plan in reversed(committed):
            with suppress(OSError):
                if plan.retired_named:
                    rename_exchange(plan.live, plan.retired)
                    plan.retired.rename(plan.staged)
                else:
                    rename_exchange(plan.live, plan.staged)
                plan.exchanged = False
        die(f"[!] Activation failed and was rolled back atomically: {exc}")


def finalise(plans: Sequence[RestorePlan], *, fix_default: bool) -> None:
    root_plan = next((p for p in plans if p.target.mountpoint == "/"), None)

    for plan in plans:
        info = subvol_show(str(plan.live))
        plan.new_subvol_id = info["id"] if info else None
        run("btrfs", "filesystem", "sync", str(plan.top))

        if fix_default and plan.new_subvol_id is not None:
            default = run("btrfs", "subvolume", "get-default", "--", str(plan.top))
            match = GET_DEFAULT_RE.search(default.stdout) if default.ok else None
            if match and int(match.group(1)) == plan.target.active_id:
                note(f"[*] Repointing the filesystem default subvolume to id {plan.new_subvol_id}...")
                run("btrfs", "subvolume", "set-default", str(plan.new_subvol_id), str(plan.top), check=True)

    for plan in plans:
        rel = plan.retired.relative_to(plan.top).as_posix()
        busy = is_mountpoint(plan.target.mountpoint)

        if not busy:
            note(f"[*] Deleting the previous state of {plan.target.config!r}...")
            deleted = run("btrfs", "subvolume", "delete", "--", str(plan.retired))
            if deleted.ok:
                run("btrfs", "filesystem", "sync", str(plan.top))
                continue
            warn(f"[!] Immediate deletion failed ({deleted.message}); deferring to boot.")
        else:
            note(f"[*] {plan.target.mountpoint} is live; deferring deletion of {rel} to the next boot.")

        # If a root restore is part of this transaction, the running / is the
        # RETIRED subvolume: anything written there dies at reboot. Units must
        # be written into the subvolume that will actually boot.
        offline_root = root_plan.live if root_plan is not None else None
        try:
            unit = schedule_boot_cleanup(fs_uuid=plan.target.fs.uuid, subvol_rel=rel, offline_root=offline_root)
            good(f"[+] Scheduled {unit} to reclaim {rel}.")
        except OSError as exc:
            warn(f"[!] Could not schedule boot cleanup for {rel}: {exc}. Delete it manually with:\n"
                 f"    dusky --cleanup-subvol {plan.target.fs.uuid} {rel}")


def audit_boot_consistency(root_dir: Path) -> list[str]:
    """
    A restored @ carries /usr/lib/modules from the snapshot, but the ESP still
    holds whatever kernel was installed last. If they disagree the machine
    boots into a kernel with no modules: no disk, no network, no keyboard.
    Cheap to check, catastrophic to miss.
    """
    problems: list[str] = []
    modules_dir = root_dir / "usr/lib/modules"
    if not modules_dir.is_dir():
        return ["restored root has no /usr/lib/modules directory"]
    available = {p.name for p in modules_dir.iterdir() if p.is_dir()}

    # Only *separately mounted* boot partitions can disagree with the restored
    # tree; a /boot that lives inside the subvolume is consistent by definition.
    esp_candidates = [p for p in (Path("/boot"), Path("/efi"), Path("/boot/efi")) if p.is_dir() and is_mountpoint(p)]

    for esp in esp_candidates:
        for vmlinuz in sorted(esp.glob("vmlinuz-*")):
            release = vmlinuz.name.removeprefix("vmlinuz-")
            if release and release not in available and not any(a.startswith(release) for a in available):
                problems.append(f"{vmlinuz} has no matching modules in the restored root ({release})")
        for entry in sorted((esp / "loader/entries").glob("*.conf")) if (esp / "loader/entries").is_dir() else []:
            with suppress(OSError):
                text = entry.read_text(errors="replace")
                for match in re.finditer(r"^\s*linux\s+(\S+)", text, re.MULTILINE):
                    kernel = match.group(1).rsplit("/", 1)[-1]
                    release = kernel.removeprefix("vmlinuz-")
                    if release and release not in available:
                        problems.append(f"boot entry {entry.name} references {kernel}, absent from the restored root")
    return sorted(set(problems))


def perform_restore(targets: Sequence[RestoreTarget], *, fix_default: bool, assume_yes: bool) -> list[RestorePlan]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    by_uuid: dict[str, Filesystem] = {t.fs.uuid: t.fs for t in targets}

    with dusky_lock(), ExitStack() as stack:
        tops = {fs_uuid: stack.enter_context(top_level(fs)) for fs_uuid, fs in by_uuid.items()}

        for fs_uuid, top in tops.items():
            if not probe_exchange(top):
                die(
                    f"[!] RENAME_EXCHANGE is not usable on UUID={fs_uuid}. Dusky will not perform a "
                    "non-atomic rollback. Check kernel and btrfs-progs versions."
                )

        plans = [build_plan(t, tops[t.fs.uuid], stamp) for t in targets]
        preflight(plans, allow_default_fixup=fix_default)

        say()
        for plan in plans:
            say(
                f"  {C_ACCENT}{plan.target.config:<8}{C_RESET} "
                f"{C_DIM}{plan.target.mountpoint}{C_RESET}  "
                f"{plan.target.active_path} <- snapshot {C_INFO}{plan.target.snap_id}{C_RESET}"
            )
        if not confirm("Commit this atomic rollback?", assume_yes=assume_yes):
            raise DuskyAbort("[*] Aborted; nothing was modified.")

        for plan in plans:
            note(f"[*] Staging writable clone for {plan.target.config!r}: {plan.staged.name}")
            created = run("btrfs", "subvolume", "snapshot", str(plan.source), str(plan.staged))
            if not created.ok:
                for done in plans:
                    if done.staged_created:
                        run("btrfs", "subvolume", "delete", "--", str(done.staged))
                die(f"[!] Failed to stage clone for {plan.target.config!r}:\n    {created.message}")
            plan.staged_created = True

        for top in tops.values():
            run("btrfs", "filesystem", "sync", str(top))

        activate(plans)
        finalise(plans, fix_default=fix_default)

        root_plan = next((p for p in plans if p.target.mountpoint == "/"), None)
        if root_plan is not None:
            issues = audit_boot_consistency(root_plan.live)
            if issues:
                warn("[!] BOOT CONSISTENCY WARNINGS for the restored root:")
                for issue in issues:
                    warn(f"      - {issue}")
                warn("    Regenerate the initramfs / reinstall the matching kernel before rebooting,")
                warn("    for example:  arch-chroot into the restored root and run 'pacman -S linux'.")
        return plans


def reactivate_mount(mountpoint: str) -> bool:
    """
    Swap a non-root mount to the restored subvolume without rebooting.

    Deliberately never uses 'umount -l': a lazy unmount would leave processes
    writing into the retired subvolume, and those writes vanish at the next
    boot. If the mount is busy we say so loudly and stop.
    """
    if not is_mountpoint(mountpoint):
        note(f"[*] {mountpoint} is not mounted; the restored subvolume applies at the next mount.")
        return True

    children = [e for e in findmnt_entries() if str(e.get("target", "")).startswith(mountpoint.rstrip("/") + "/")]
    if children:
        warn(f"[!] {mountpoint} has submounts ({', '.join(str(c.get('target')) for c in children)}); skipping live remount.")
        return False

    note(f"[*] Remounting {mountpoint} onto the restored subvolume...")
    if not run("umount", "--", mountpoint).ok:
        warn(
            f"[!] {mountpoint} is busy, so the live filesystem still points at the RETIRED subvolume.\n"
            f"[!] The restore is committed on disk. Anything written to {mountpoint} from now until\n"
            "[!] you reboot will be discarded. Reboot as soon as possible."
        )
        return False
    if not run("mount", "--", mountpoint).ok:
        die(f"[!] CRITICAL: {mountpoint} was unmounted but could not be remounted. Fix this before rebooting.")
    good(f"[+] {mountpoint} is now serving the restored snapshot.")
    return True


# =============================================================================
# BACKUP (btrfs send | btrfs receive)
# =============================================================================
def backup_subvolume(fs: Filesystem, src_rel: str, destination: str, *, parent_rel: str | None = None) -> Path:
    dest = Path(destination).resolve()
    if not dest.is_dir():
        die(f"[!] Destination is not a directory: {dest}")
    fstype = run("stat", "-f", "-c", "%T", str(dest))
    if not fstype.ok or "btrfs" not in fstype.text.lower():
        die(f"[!] Destination {dest} is not btrfs; btrfs receive requires a btrfs target.")
    dest_fs = filesystem_of(str(dest)) if is_mountpoint(str(dest)) else None
    dest_uuid = dest_fs.uuid if dest_fs else run("findmnt", "-n", "-o", "UUID", "-T", str(dest)).text

    if dest_uuid == fs.uuid:
        warn("[!] Destination is the SAME btrfs filesystem as the source. This is a copy, not a backup.")

    with dusky_lock(), top_level(fs) as top:
        for orphan in sorted(top.glob(f"{TAG_SEND}*")):
            LOG.info("Sweeping orphaned send snapshot %s", orphan)
            run("btrfs", "subvolume", "delete", "--", str(orphan))

        source = top / src_rel.strip("/")
        if not source.exists():
            die(f"[!] Source subvolume not found at the physical layer: {src_rel}")
        info = subvol_show(str(source))
        if info is None:
            die(f"[!] {src_rel} is not a btrfs subvolume.")

        with ExitStack() as stack:
            send_source = source
            if not info["readonly"]:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                ephemeral = top / f"{TAG_SEND}{source.name or 'root'}_{stamp}"
                note("[*] Source is writable; taking an ephemeral read-only snapshot for a consistent stream...")
                run("btrfs", "subvolume", "snapshot", "-r", str(source), str(ephemeral), check=True)
                run("btrfs", "filesystem", "sync", str(top))
                stack.callback(lambda: run("btrfs", "subvolume", "delete", "--", str(ephemeral)))
                send_source = ephemeral
            send_info = subvol_show(str(send_source)) or {}

            staging = Path(tempfile.mkdtemp(dir=str(dest), prefix=TAG_RECV))
            stack.callback(lambda: _purge_staging(staging))

            argv = ["btrfs", "send"]
            if parent_rel:
                parent_abs = top / parent_rel.strip("/")
                if not parent_abs.exists():
                    die(f"[!] Parent subvolume for the incremental stream not found: {parent_rel}")
                argv += ["-p", str(parent_abs)]
            argv.append(str(send_source))

            note(f"[*] {shlex.join(argv)} | btrfs receive {staging}")
            send_err = stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace"))

            send_proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=send_err, env=SUBPROCESS_ENV)
            assert send_proc.stdout is not None
            recv_proc = subprocess.Popen(
                ["btrfs", "receive", str(staging)],
                stdin=send_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=SUBPROCESS_ENV,
            )
            send_proc.stdout.close()
            _, recv_err = recv_proc.communicate()
            send_rc = send_proc.wait()
            send_err.seek(0)
            send_stderr = send_err.read().strip()

            if recv_proc.returncode != 0:
                die(f"[!] btrfs receive failed (rc={recv_proc.returncode}):\n{recv_err.strip()}\n{send_stderr}")
            if send_rc != 0:
                # -13 is SIGPIPE. v2 treated it as success, which would mask a
                # truncated stream whenever receive died mid-transfer.
                reason = "SIGPIPE (receive exited early)" if send_rc == -13 else f"rc={send_rc}"
                die(f"[!] btrfs send failed: {reason}\n{send_stderr}")

            received = [p for p in staging.iterdir()]
            if len(received) != 1:
                die(f"[!] Expected exactly one received subvolume in staging, found {len(received)}.")
            item = received[0]

            got = subvol_show(str(item)) or {}
            if send_info.get("uuid") and got.get("received_uuid") and send_info["uuid"] != got["received_uuid"]:
                die(
                    "[!] Integrity check failed: received_uuid "
                    f"{got['received_uuid']} != source uuid {send_info['uuid']}."
                )

            label = Path(src_rel).name or "root"
            if label == "snapshot":
                label = f"{Path(src_rel).parent.parent.name}_{Path(src_rel).parent.name}"
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            final = dest / f"dusky_backup_{label}_{stamp}"
            if final.exists():
                die(f"[!] Backup target already exists: {final}")
            os.rename(item, final)
            run("btrfs", "filesystem", "sync", str(dest))
            good(f"[+] Backup complete: {final}")
            if got.get("received_uuid"):
                say(f"{C_DIM}    received_uuid {got['received_uuid']} (usable as a parent for incremental sends){C_RESET}")
            return final


def _purge_staging(staging: Path) -> None:
    if not staging.exists():
        return
    for item in sorted(staging.iterdir()):
        run("btrfs", "subvolume", "delete", "--", str(item))
        if item.exists():
            shutil.rmtree(item, ignore_errors=True)
    with suppress(OSError):
        staging.rmdir()


def sweep_orphans(*, apply: bool) -> int:
    """Reclaim .tmp_send_*, .btrfs_recv_* and abandoned *_dusky_new_* clones."""
    count = 0
    for fs_uuid, (fs, _mount) in btrfs_filesystems().items():
        with top_level(fs, writable=apply, quiet=True) as top:
            for child in sorted(top.iterdir()):
                name = child.name
                if not (name.startswith(TAG_SEND) or TAG_STAGED in name):
                    continue
                count += 1
                say(f"  {C_WARN}orphan{C_RESET} UUID={fs_uuid} {name}")
                if apply:
                    run("btrfs", "subvolume", "delete", "--", str(child), check=True)
        for entry in findmnt_entries():
            if str(entry.get("fstype")) != "btrfs":
                continue
            target = Path(str(entry.get("target") or ""))
            if not target.is_dir():
                continue
            for staging in sorted(target.glob(f"{TAG_RECV}*")):
                count += 1
                say(f"  {C_WARN}orphan{C_RESET} staging {staging}")
                if apply:
                    _purge_staging(staging)
    return count


# =============================================================================
# COMMAND HANDLERS
# =============================================================================
def cmd_list(config: str, as_json: bool) -> None:
    if as_json:
        say(json.dumps(snapshot_rows(config), ensure_ascii=False))
        return
    sys.exit(run_tty("snapper", "-c", config, "list"))


def cmd_create(config: str, description: str) -> None:
    with dusky_lock():
        run("snapper", "-c", config, "create", "-d", description, check=True)
    good(f"[+] Snapshot created for {config!r}.")


def cmd_create_pair(left: str, right: str, description: str) -> None:
    if left == right:
        die("[!] A coordinated pair requires two distinct configs.")
    pair_id = uuidlib.uuid4().hex
    with dusky_lock():
        first = run(
            "snapper", "-c", left, "create", "-d", description,
            "--userdata", f"dusky_pair={pair_id},dusky_role={left}", check=True,
        )
        del first
        second = run(
            "snapper", "-c", right, "create", "-d", description,
            "--userdata", f"dusky_pair={pair_id},dusky_role={right}",
        )
        if not second.ok:
            warn(f"[!] {right} snapshot failed; rolling back the {left} half so no half-pair is left behind.")
            for row in snapshot_rows(left):
                if row.get("userdata_dict", {}).get("dusky_pair") == pair_id:
                    run("snapper", "-c", left, "delete", row["id"])
            die(f"[!] Coordinated create failed:\n    {second.message}")
    good(f"[+] Coordinated snapshots created (dusky_pair={pair_id}).")


def cmd_delete(config: str, snap_id: str) -> None:
    snap_id = validate_snap_id(snap_id)
    with dusky_lock():
        result = run("snapper", "-c", config, "delete", snap_id)
        if result.ok:
            good(f"[+] Deleted snapshot {snap_id} of {config!r}.")
            return

        target = snapper_config_subvolume(config)
        snaps_mnt = snapshots_mountpoint(target)
        if not Path(snaps_mnt).is_mount():
            die(f"[!] Failed to delete {snap_id}: {result.message}")
        meta_dir = Path(snaps_mnt) / snap_id
        subvol = meta_dir / "snapshot"
        if meta_dir.is_dir() and not subvol.exists():
            allowed = {"info.xml", "filelist-0.txt"}
            leftovers = {p.name for p in meta_dir.iterdir()}
            if leftovers - allowed:
                die(f"[!] Refusing to purge {meta_dir}: unexpected content {sorted(leftovers - allowed)}")
            shutil.rmtree(meta_dir, ignore_errors=True)
            if not meta_dir.exists():
                good(f"[+] Purged dead snapshot metadata {snap_id} of {config!r}.")
                return
        die(f"[!] Failed to delete snapshot {snap_id} of {config!r}:\n    {result.message}")


def cmd_delete_pair(left: str, left_id: str, right: str, right_id: str) -> None:
    if left == right:
        die("[!] A coordinated delete requires two distinct configs.")
    cmd_delete(left, left_id)
    cmd_delete(right, right_id)
    good("[+] Coordinated deletion complete.")


def cmd_restore(config: str, snap_id: str, *, remount: bool, fix_default: bool, assume_yes: bool) -> None:
    target = resolve_target(config, snap_id)
    perform_restore([target], fix_default=fix_default, assume_yes=assume_yes)
    good(f"\n[+] Restore of {config!r} committed.")
    if target.mountpoint == "/":
        say(f"{C_ERR}[!] ROOT RESTORED. Reboot now; the running system is the retired subvolume.{C_RESET}")
        return
    if remount:
        reactivate_mount(target.mountpoint)
    else:
        warn(f"[!] {target.mountpoint} still serves the retired subvolume until you remount or reboot.")


def cmd_restore_pair(left: str, left_id: str, right: str, right_id: str, *, remount: bool, fix_default: bool, assume_yes: bool) -> None:
    if left == right:
        die("[!] A coordinated restore requires two distinct configs.")
    targets = [resolve_target(left, left_id), resolve_target(right, right_id)]
    if targets[0].active_path == targets[1].active_path and targets[0].fs.uuid == targets[1].fs.uuid:
        die("[!] Both configs resolve to the same subvolume.")
    perform_restore(targets, fix_default=fix_default, assume_yes=assume_yes)
    good("\n[+] Coordinated restore committed.")
    if any(t.mountpoint == "/" for t in targets):
        say(f"{C_ERR}[!] ROOT RESTORED. Reboot now.{C_RESET}")
    for target in targets:
        if target.mountpoint != "/" and remount:
            reactivate_mount(target.mountpoint)


def cmd_cleanup_subvol(fs_uuid: str, subvol_rel: str) -> None:
    """
    Manual counterpart of the generated boot unit.

    Guardrail: only paths carrying a Dusky transient marker are eligible, so a
    mangled or hostile invocation cannot be turned into 'delete @home'.
    """
    rel = subvol_rel.strip("/")
    if not any(token in rel for token in (TAG_RETIRED, TAG_STAGED, TAG_SEND)):
        die(f"[!] Refusing to delete {rel!r}: it carries no Dusky transient marker.")

    # A retired subvolume is normally still serving the running / until reboot.
    # It is not a mountpoint at its top-level path, so a naive mountpoint check
    # would happily let us delete the filesystem out from under the live system.
    # Compare subvolume ids against every live btrfs mount instead.
    live_ids: dict[int, str] = {}
    for entry in findmnt_entries():
        if str(entry.get("fstype")) != "btrfs":
            continue
        target = str(entry.get("target") or "")
        if str(entry.get("uuid") or "") != fs_uuid.strip() or not target:
            continue
        info = subvol_show(target)
        if info and info["id"] is not None:
            live_ids[int(info["id"])] = target

    fs = Filesystem(uuid=fs_uuid.strip(), source="")
    with dusky_lock(), top_level(fs, quiet=True) as top:
        victim = top / rel
        if not victim.exists():
            LOG.info("Cleanup target already gone: %s", rel)
            return
        if is_mountpoint(victim):
            die(f"[!] Refusing to delete a mounted subvolume: {rel}")
        info = subvol_show(str(victim))
        if info is None or info["id"] is None:
            die(f"[!] {rel} is not a subvolume.")
        if int(info["id"]) in live_ids:
            die(
                f"[!] Refusing to delete {rel}: subvolume id {info['id']} is currently serving "
                f"{live_ids[int(info['id'])]}. Reboot first; the boot-time unit will reclaim it."
            )
        run("btrfs", "subvolume", "delete", "--", str(victim), check=True)
        LOG.info("Deleted %s on UUID=%s", rel, fs_uuid)


def cmd_doctor() -> None:
    say(f"{C_ACCENT}Dusky doctor v{DUSKY_VERSION}{C_RESET}")
    say(f"{C_RULE}{'-' * 62}{C_RESET}")

    for tool in ("btrfs", "snapper", "findmnt", "mount", "systemctl", "fzf"):
        path = shutil.which(tool)
        state = f"{C_OK}ok{C_RESET} {C_DIM}{path}{C_RESET}" if path else f"{C_ERR}MISSING{C_RESET}"
        say(f"  tool {tool:<10} {state}")

    kernel = run("uname", "-r").text
    say(f"  kernel        {C_DIM}{kernel}{C_RESET}")
    say(f"  btrfs-progs   {C_DIM}{run('btrfs', '--version').text}{C_RESET}")
    say(f"  python        {C_DIM}{sys.version.split()[0]}{C_RESET}")

    say()
    for fs_uuid, (fs, mount_target) in btrfs_filesystems().items():
        say(f"{C_INFO}filesystem UUID={fs_uuid}{C_RESET} {C_DIM}({fs.source} at {mount_target}){C_RESET}")
        default = run("btrfs", "subvolume", "get-default", "--", mount_target)
        say(f"  default subvolume : {default.text or 'unknown'}")
        with suppress(DuskyError):
            with top_level(fs, quiet=True) as top:
                say(f"  RENAME_EXCHANGE   : {'supported' if probe_exchange(top) else 'NOT SUPPORTED'}")

    say()
    for cfg in snapper_configs():
        mountpoint = cfg["subvolume"]
        snaps_mnt = snapshots_mountpoint(mountpoint)
        live = is_mountpoint(snaps_mnt)
        resolved = active_subvol(mountpoint, required=False)
        say(f"{C_INFO}snapper config {cfg['config']!r}{C_RESET} -> {mountpoint}")
        say(f"  active subvolume  : {resolved[0] if resolved else C_ERR + 'unresolved' + C_RESET}")
        say(f"  snapshot store    : {snaps_mnt} {'(mounted subvolume)' if live else C_WARN + '(NOT a mounted subvolume)' + C_RESET}")
        if resolved:
            listing = run("btrfs", "subvolume", "list", "-a", "--", mountpoint)
            prefix = resolved[0].strip("/") + "/"
            nested = [p for _i, p, _u in parse_subvol_list(listing.stdout) if p.startswith(prefix)] if listing.ok else []
            if nested:
                say(f"  {C_ERR}nested subvolumes : {', '.join(nested)}{C_RESET}")
                say(f"  {C_ERR}                    rollback of this config is BLOCKED{C_RESET}")
            else:
                say(f"  nested subvolumes : {C_OK}none (flat, rollback-safe){C_RESET}")

    pending = list_pending_cleanups()
    say()
    say(f"pending boot cleanups : {', '.join(pending) if pending else 'none'}")
    stale = sweep_stale_mounts()
    say(f"stale private mounts  : {stale} reaped")

    esp = next((p for p in (Path('/efi'), Path('/boot')) if p.is_dir() and is_mountpoint(p)), None)
    if esp is not None:
        problems = audit_boot_consistency(Path("/"))
        say(f"kernel/module match   : {C_OK + 'consistent' + C_RESET if not problems else C_ERR + '; '.join(problems) + C_RESET}")


# =============================================================================
# fzf TUI
# =============================================================================
US = "\x1f"
VIEWS: Final = ("home", "root", "coordinated", "global", "subvolumes", "maintenance")

TAB_DEFS: Final = (
    ("home", "\U000f011c HOME", "114"),
    ("root", "\U000f0288 ROOT", "39"),
    ("coordinated", "\U000f0450 ROOT+HOME", "213"),
    ("global", "\U000f0191 GLOBAL", "81"),
    ("subvolumes", "\U000f02ca SUBVOLUMES", "203"),
    ("maintenance", "\U000f0493 MAINTENANCE", "215"),
)


def panel(title: str, rows: Sequence[str], width: int = 52) -> None:
    say(f"{C_WARN}\u256d\u2500 {title} {C_WARN}{'\u2500' * max(0, width - display_width(title) - 5)}\u256e{C_RESET}")
    for row in rows:
        pad = max(0, width - display_width(row) - 4)
        say(f"{C_WARN}\u2502{C_RESET} {row}{' ' * pad} {C_WARN}\u2502{C_RESET}")
    say(f"{C_WARN}\u2570{'\u2500' * (width - 2)}\u256f{C_RESET}\n")


def tui_preview(view: str, line: str, *, show_diff: bool) -> None:
    parts = line.split(US)
    try:
        meta = json.loads(parts[1]) if len(parts) > 1 else {}
    except ValueError:
        meta = {}

    if view == "subvolumes":
        panel(
            f"{C_ACCENT}\U000f03d6 SUBVOLUME ACTIONS{C_RESET}",
            [
                f"{C_OK}[CTRL-N]{C_RESET}  create top-level subvolume",
                f"{C_INFO}[CTRL-S]{C_RESET}  native btrfs snapshot",
                f"{C_ACCENT}[CTRL-G]{C_RESET}  init snapper config",
                f"{C_WARN}[CTRL-B]{C_RESET}  send/receive backup",
                f"{C_ERR}[DEL]{C_RESET}     delete subvolume",
                f"{C_DIM}[TAB]{C_RESET}     next view",
            ],
        )
    elif view == "maintenance":
        panel(
            f"{C_WARN}\U000f0493 MAINTENANCE{C_RESET}",
            [
                f"{C_ERR}[DEL]{C_RESET}     reclaim selected orphan",
                f"{C_DIM}[TAB]{C_RESET}     next view",
                f"{C_DIM}Transient artefacts from interrupted{C_RESET}",
                f"{C_DIM}restores and backups live here.{C_RESET}",
            ],
        )
    else:
        panel(
            f"{C_WARN}\U000f03d6 SHORTCUTS{C_RESET}",
            [
                f"{C_OK}[ENTER]{C_RESET}   atomic restore",
                f"{C_ERR}[DEL]{C_RESET}     delete snapshot(s)",
                f"{C_INFO}[CTRL-S]{C_RESET}  create snapshot",
                f"{C_WARN}[CTRL-B]{C_RESET}  backup to external btrfs",
                f"{C_ACCENT}[TAB]{C_RESET}     next view",
                f"{C_DIM}[CTRL-A/X]{C_RESET} select / deselect all",
                f"{C_DIM}[CTRL-V/P]{C_RESET} diff mode on / off",
                f"{C_DIM}[ALT-P]{C_RESET}    toggle this pane",
            ],
        )

    if not meta or meta.get("empty"):
        say(f"{C_DIM}[i] Nothing selected.{C_RESET}")
        return

    def field_row(label: str, value: object, colour: str = "\033[38;5;253m") -> None:
        say(f" {C_DIM}{label:<9}{C_RESET}\u2502 {colour}{value}{C_RESET}")

    if view == "subvolumes":
        say(f"{C_ACCENT}\U000f02ca SUBVOLUME{C_RESET}")
        say(f"{C_RULE}{'-' * 52}{C_RESET}")
        field_row("id", meta.get("id", "?"))
        field_row("path", meta.get("path", "?"))
        field_row("flags", "read-only" if meta.get("is_ro") else "read-write")
        field_row("fs uuid", meta.get("fs_uuid", "?"), C_DIM)
        field_row("mounted", meta.get("mounted_at") or "not mounted", C_OK if meta.get("mounted_at") else C_DIM)
        return

    if view == "maintenance":
        say(f"{C_WARN}\U000f0493 ORPHANED ARTEFACT{C_RESET}")
        say(f"{C_RULE}{'-' * 52}{C_RESET}")
        field_row("path", meta.get("path", "?"))
        field_row("fs uuid", meta.get("fs_uuid", "?"), C_DIM)
        field_row("kind", meta.get("kind", "?"), C_WARN)
        return

    config = str(meta.get("config") or ("root" if view in ("root", "coordinated") else "home"))
    say(f"{C_INFO}\U000f0191 SNAPSHOT {meta.get('id')}{C_RESET}")
    say(f"{C_RULE}{'-' * 52}{C_RESET}")
    field_row("config", config.upper())
    field_row("type", meta.get("type") or "-", C_ACCENT)
    if meta.get("pre_number"):
        field_row("pre", meta["pre_number"])
    field_row("date", meta.get("date") or "-", "\033[38;5;220m")
    field_row("age", meta.get("age") or "-", C_OK)
    field_row("user", meta.get("user") or "root")
    if meta.get("cleanup"):
        field_row("cleanup", meta["cleanup"])
    if meta.get("location"):
        field_row("path", meta["location"], C_DIM)
    if meta.get("userdata"):
        field_row("userdata", meta["userdata"], C_DIM)
    if meta.get("dead"):
        field_row("state", "DEAD / SUBVOLUME MISSING", C_ERR)
    field_row("desc", meta.get("description") or "-")
    say()

    if view == "coordinated":
        try:
            match = find_pair(
                "root",
                "home",
                target_date=str(meta.get("raw_date") or ""),
                target_desc=str(meta.get("description") or ""),
                target_userdata=dict(meta.get("userdata_dict") or {}),
                left_id_hint=str(meta.get("id")),
            )
            colour = C_OK if match.exact else C_WARN
            say(f"{colour}\U000f0450 PAIR  root={match.left_id}  home={match.right_id}{C_RESET}")
            say(f"{C_DIM}    matched by {match.method} (delta {match.delta:.0f}s){C_RESET}\n")
        except DuskyError as exc:
            say(f"{C_ERR}\U000f0450 PAIR UNRESOLVED{C_RESET}\n{C_DIM}{exc}{C_RESET}\n")

    if not show_diff:
        say(f"{C_DIM}[i] File changes hidden for scroll performance.{C_RESET}")
        say(f"{C_DIM}    CTRL-V compute diff  |  CTRL-P hide again{C_RESET}")
        return

    def render_diff(cfg: str, snap: str) -> None:
        say(f"{C_ACCENT}\u25b6 {cfg}: files that would change{C_RESET}")
        proc = run("snapper", "-c", cfg, "status", f"{snap}..0", timeout=120)
        if not proc.ok:
            say(f"  {C_ERR}{proc.message}{C_RESET}")
            return
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if not lines:
            say(f"  {C_DIM}no differences{C_RESET}")
            return
        for index, entry in enumerate(lines[:150]):
            status, _, rest = entry.partition(" ")
            path = rest.strip()
            if status.startswith("+"):
                say(f"  {C_ERR}[-]{C_RESET} {C_DIM}{path}{C_RESET}")
            elif status.startswith("-"):
                say(f"  {C_OK}[+]{C_RESET} {path}")
            else:
                say(f"  {C_WARN}[~]{C_RESET} {path}")
            del index
        if len(lines) > 150:
            say(f"  {C_DIM}... {len(lines) - 150} more{C_RESET}")

    if view in ("home", "root", "global"):
        render_diff(config, str(meta.get("id")))
    elif view == "coordinated":
        render_diff("root", str(meta.get("id")))
        with suppress(DuskyError):
            match = find_pair(
                "root", "home",
                target_date=str(meta.get("raw_date") or ""),
                target_desc=str(meta.get("description") or ""),
                target_userdata=dict(meta.get("userdata_dict") or {}),
                left_id_hint=str(meta.get("id")),
            )
            say()
            render_diff("home", match.right_id)


def _tab_bar(current: str) -> str:
    cells = []
    for view_id, label, colour in TAB_DEFS:
        if view_id == current:
            cells.append(f"\033[1;38;5;232;48;5;{colour}m {label} {C_RESET}")
        else:
            cells.append(f"{C_DIM} {label} {C_RESET}")
    return "  " + "  ".join(cells)


def _storage_header() -> str:
    gib = 1024 ** 3
    total, used, free = shutil.disk_usage("/")
    return (
        f" {C_INFO}\U000f02ca BTRFS{C_RESET} "
        f"\033[38;5;253m{total / gib:.1f}G total{C_RESET} {C_RULE}|{C_RESET} "
        f"\033[38;5;203m{used / gib:.1f}G used{C_RESET} {C_RULE}|{C_RESET} "
        f"{C_OK}{free / gib:.1f}G free{C_RESET}  {C_RULE}|{C_RESET} {C_DIM}dusky {DUSKY_VERSION}{C_RESET} "
    )


def _rows_for_view(view: str) -> list[str]:
    sep = f"{C_RULE}\u2502{C_RESET}"
    rule = f"{C_RULE}{'\u2500' * 400}{C_RESET}"
    out = [_tab_bar(view), rule]

    if view == "subvolumes":
        out.append(f"{C_DIM}{'ID':>5}{C_RESET} {sep} {C_DIM}{'MOUNTED AT':<16}{C_RESET} {sep} {C_DIM}{'RO':<3}{C_RESET} {sep} {C_DIM}PATH{C_RESET}")
        items = sorted(enumerate_subvolumes(), key=lambda s: s.path)
        for item in items:
            visible = (
                f"\033[1;38;5;39m{item.id:>5}{C_RESET} {sep} "
                f"\033[38;5;220m{(item.mounted_at or '-'):<16}{C_RESET} {sep} "
                f"{(C_ERR + 'ro ' + C_RESET) if item.readonly else (C_OK + 'rw ' + C_RESET)} {sep} "
                f"\033[38;5;253m{item.path}{C_RESET}"
            )
            out.append(f"{visible}{US}{json.dumps(item.as_meta())}")
        if not items:
            out.append(f"{C_DIM}{'-':>5}{C_RESET} {sep} no subvolumes{US}" + json.dumps({"empty": True}))
        return out

    if view == "maintenance":
        out.append(f"{C_DIM}{'KIND':<12}{C_RESET} {sep} {C_DIM}{'FS UUID':<36}{C_RESET} {sep} {C_DIM}PATH{C_RESET}")
        found = 0
        for item in enumerate_subvolumes(include_transient=True, include_snapshots=False):
            if classify_subvol(item.path, snapshot_roots()) != "transient":
                continue
            found += 1
            kind = "retired" if TAG_RETIRED in item.path else "staged" if TAG_STAGED in item.path else "ephemeral"
            meta = item.as_meta() | {"kind": kind}
            visible = (
                f"{C_WARN}{kind:<12}{C_RESET} {sep} {C_DIM}{item.fs_uuid:<36}{C_RESET} {sep} "
                f"\033[38;5;253m{item.path}{C_RESET}"
            )
            out.append(f"{visible}{US}{json.dumps(meta)}")
        if not found:
            out.append(f"{C_OK}{'clean':<12}{C_RESET} {sep} no orphaned artefacts{US}" + json.dumps({"empty": True}))
        return out

    if view == "global":
        out.append(
            f"{C_DIM}{'CFG':<9}{C_RESET} {sep} {C_DIM}{'ID':>5}{C_RESET} {sep} {C_DIM}{'AGE':<9}{C_RESET} "
            f"{sep} {C_DIM}{'DATE':<15}{C_RESET} {sep} {C_DIM}DESCRIPTION{C_RESET}"
        )
        rows = sorted(all_snapshot_rows(), key=lambda r: (r["config"], -int(r["id"])))
    else:
        out.append(
            f"{C_DIM}{'ID':>5}{C_RESET} {sep} {C_DIM}{'TYPE':<7}{C_RESET} {sep} {C_DIM}{'AGE':<9}{C_RESET} "
            f"{sep} {C_DIM}{'DATE':<15}{C_RESET} {sep} {C_DIM}DESCRIPTION{C_RESET}"
        )
        config = "root" if view in ("root", "coordinated") else "home"
        rows = sorted(snapshot_rows(config), key=lambda r: -int(r["id"]))

    if not rows:
        out.append(f"{C_ERR} no snapshots{C_RESET}{US}" + json.dumps({"empty": True}))
        return out

    for row in rows:
        colour = C_ERR if row["dead"] else "\033[38;5;253m"
        if view == "global":
            visible = (
                f"{C_ACCENT}{row['config']:<9}{C_RESET} {sep} \033[1;38;5;39m{row['id']:>5}{C_RESET} {sep} "
                f"{C_OK}{row['age']:<9}{C_RESET} {sep} \033[38;5;220m{row['date']:<15}{C_RESET} {sep} "
                f"{colour}{row['description']}{C_RESET}"
            )
        else:
            visible = (
                f"\033[1;38;5;39m{row['id']:>5}{C_RESET} {sep} {C_ACCENT}{row['type']:<7}{C_RESET} {sep} "
                f"{C_OK}{row['age']:<9}{C_RESET} {sep} \033[38;5;220m{row['date']:<15}{C_RESET} {sep} "
                f"{colour}{row['description']}{C_RESET}"
            )
        out.append(f"{visible}{US}{json.dumps(row)}")
    return out


def launch_tui() -> None:
    require_tools("fzf", "btrfs", "snapper", "findmnt")
    if not interactive():
        die("[!] The TUI requires a controlling terminal.")

    colors = (
        "bg+:#1e1e2e,bg:-1,spinner:#f5e0dc,fg:#cdd6f4,fg+:#cdd6f4,header:#89b4fa,"
        "info:#cba6f7,pointer:#f5e0dc,marker:#a6e3a1,prompt:#cba6f7,hl:#f38ba8,"
        "hl+:#f38ba8,border:#585b70,label:#a6e3a1"
    )
    self_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT_PATH))}"
    view_index = 0
    empty_cycles = 0

    while True:
        view = VIEWS[view_index]
        lines = _rows_for_view(view)
        preview = f"{self_cmd} --tui-preview {view} {{}}"
        preview_diff = f"{self_cmd} --tui-preview {view} --show-diff {{}}"

        argv = [
            "fzf",
            "--multi",
            "--ansi",
            "--reverse",
            "--delimiter=\\x1f",
            "--with-nth=1",
            "--header", _storage_header(),
            "--header-first",
            "--header-lines=3",
            "--border=rounded",
            "--border-label", f" Dusky {DUSKY_VERSION} ",
            "--prompt", " :: action \u276f ",
            f"--color={colors}",
            "--pointer=\u258c",
            "--marker=\u25b6",
            "--no-hscroll",
            "--ellipsis=",
            "--highlight-line",
            "--scrollbar=\u2503",
            "--info=inline-right",
            "--expect=enter,ctrl-d,delete,tab,btab,ctrl-s,ctrl-n,ctrl-g,ctrl-b,ctrl-r",
            "--bind=ctrl-a:select-all,ctrl-x:deselect-all,ctrl-space:toggle,"
            "shift-down:toggle+down,shift-up:toggle+up,"
            f"ctrl-p:change-preview({preview})+change-prompt( :: action \u276f ),"
            f"ctrl-v:change-preview({preview_diff})+change-prompt( :: diff \u276f ),"
            "alt-p:toggle-preview",
            "--preview", preview,
            "--preview-window", "right,46%,border-left,wrap",
        ]

        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, encoding="utf-8", env=SUBPROCESS_ENV,
            )
            stdout, _ = process.communicate(input="\n".join(lines))
        except OSError as exc:
            die(f"[!] Failed to launch fzf: {exc}")

        if process.returncode in (2, 130):
            say(f"\n{C_DIM}[*] Bye.{C_RESET}")
            return
        if not stdout.strip():
            empty_cycles += 1
            if empty_cycles > 3:
                die("[!] fzf produced no output three times in a row; aborting to avoid a spin loop.")
            continue
        empty_cycles = 0

        output = stdout.strip().split("\n")
        key = output[0]

        if key in ("tab", "btab"):
            view_index = (view_index + (1 if key == "tab" else -1)) % len(VIEWS)
            continue
        if key == "ctrl-r":
            continue

        selected: list[JSONDict] = []
        for line in output[1:]:
            chunks = line.split(US)
            if len(chunks) < 2:
                continue
            with suppress(ValueError):
                meta = json.loads(chunks[1])
                if isinstance(meta, dict) and not meta.get("empty"):
                    selected.append(meta)

        try:
            if _dispatch(view, key, selected):
                return
        except DuskyAbort as exc:
            say(f"{C_DIM}{exc}{C_RESET}")
            pause()
        except DuskyError as exc:
            say(f"{C_ERR}{exc}{C_RESET}")
            pause()


def _dispatch(view: str, key: str, selected: list[JSONDict]) -> bool:
    """Returns True when the TUI should exit."""
    if key == "ctrl-s" and view in ("home", "root", "coordinated", "global"):
        config = "root" if view == "coordinated" else view
        if view == "global":
            config = ask(f"{C_WARN}[*] target config: {C_RESET}")
        if not config:
            return False
        description = ask(f"{C_WARN}[*] description: {C_RESET}")
        if not description:
            return False
        if view == "coordinated":
            cmd_create_pair("root", "home", description)
        else:
            cmd_create(config, description)
        pause()
        return False

    if not selected:
        return False
    head = selected[0]

    if view == "coordinated":
        pairs: list[PairMatch] = []
        for meta in selected:
            match = find_pair(
                "root", "home",
                target_date=str(meta.get("raw_date") or ""),
                target_desc=str(meta.get("description") or ""),
                target_userdata=dict(meta.get("userdata_dict") or {}),
                left_id_hint=str(meta.get("id")),
            )
            pairs.append(match)
            say(f"{C_DIM}[*] pair root={match.left_id} home={match.right_id} via {match.method}{C_RESET}")

        if key == "enter":
            if len(pairs) != 1:
                die("[!] Select exactly one pair to restore.")
            match = pairs[0]
            if not match.exact and not confirm(
                f"This pair was matched heuristically ({match.method}, {match.delta:.0f}s apart). Continue?"
            ):
                return False
            cmd_restore_pair("root", match.left_id, "home", match.right_id, remount=True, fix_default=True, assume_yes=False)
            pause("Press Enter to exit...")
            return True
        if key in ("ctrl-d", "delete"):
            if confirm(f"Permanently delete {len(pairs)} snapshot pair(s)?"):
                for match in pairs:
                    cmd_delete_pair("root", match.left_id, "home", match.right_id)
            pause()
        return False

    if view in ("home", "root", "global"):
        if key == "enter":
            if len(selected) != 1:
                die("[!] Select exactly one snapshot to restore.")
            if head.get("dead"):
                die("[!] That snapshot is dead: its subvolume is missing.")
            cmd_restore(str(head["config"]), str(head["id"]), remount=True, fix_default=True, assume_yes=False)
            pause("Press Enter to exit...")
            return True
        if key in ("ctrl-d", "delete"):
            if confirm(f"Permanently delete {len(selected)} snapshot(s)?"):
                for meta in selected:
                    cmd_delete(str(meta["config"]), str(meta["id"]))
            pause()
            return False
        if key == "ctrl-b":
            if len(selected) != 1 or head.get("dead"):
                die("[!] Select exactly one live snapshot to back up.")
            config = str(head["config"])
            mountpoint = snapper_config_subvolume(config)
            fs = filesystem_of(mountpoint)
            snaps = active_subvol(snapshots_mountpoint(mountpoint))
            if snaps is None:
                die("[!] Snapshot store is not a subvolume.")
            rel = f"{snaps[0]}/{head['id']}/snapshot"
            destination = ask(f"{C_WARN}[*] destination (btrfs mount, e.g. /mnt/backup): {C_RESET}")
            if destination:
                backup_subvolume(fs, rel, destination)
            pause()
        return False

    if view == "maintenance":
        if key in ("ctrl-d", "delete"):
            if confirm(f"Reclaim {len(selected)} orphaned artefact(s)?"):
                for meta in selected:
                    cmd_cleanup_subvol(str(meta["fs_uuid"]), str(meta["path"]))
                good("[+] Reclaimed.")
            pause()
        return False

    # subvolumes view
    if key in ("ctrl-n", "ctrl-s", "ctrl-g", "ctrl-b") and len(selected) > 1:
        die("[!] Select exactly one subvolume for that action.")

    fs = Filesystem(uuid=str(head.get("fs_uuid", "")), source="")
    path = str(head.get("path", ""))

    match key:
        case "ctrl-n":
            name = ask(f"{C_WARN}[*] new top-level subvolume name (e.g. @data): {C_RESET}")
            if name:
                nocow = confirm("Disable copy-on-write (chattr +C)?")
                create_top_level_subvolume(fs, name, nocow)
            pause()
        case "ctrl-s":
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            default = f"@snapshots/{path.strip('@/').replace('/', '_')}_{stamp}"
            dest = ask(f"{C_WARN}[*] destination relative to fs root [{default}]: {C_RESET}") or default
            readonly = confirm("Read-only snapshot?")
            with dusky_lock(), top_level(fs) as top:
                src = top / path
                target = top / dest.lstrip("/")
                if not src.exists():
                    die(f"[!] Source subvolume vanished: {path}")
                if target.exists():
                    die(f"[!] Destination already exists: {dest}")
                target.parent.mkdir(parents=True, exist_ok=True)
                argv = ["btrfs", "subvolume", "snapshot"] + (["-r"] if readonly else [])
                run(*argv, str(src), str(target), check=True)
                run("btrfs", "filesystem", "sync", str(top))
            good("[+] Snapshot created.")
            pause()
        case "ctrl-g":
            say(f"{C_DIM}[i] The subvolume must already be mounted for snapper create-config.{C_RESET}")
            mountpoint = ask(f"{C_WARN}[*] live mount point: {C_RESET}")
            name = ask(f"{C_WARN}[*] snapper config name: {C_RESET}")
            if mountpoint and name:
                with dusky_lock():
                    run("snapper", "-c", name, "create-config", mountpoint, check=True)
                good(f"[+] Config {name!r} created.")
            pause()
        case "ctrl-b":
            destination = ask(f"{C_WARN}[*] destination (btrfs mount): {C_RESET}")
            if destination:
                backup_subvolume(fs, path, destination)
            pause()
        case "delete" | "ctrl-d":
            protected = protected_subvolumes()
            victims = []
            for meta in selected:
                candidate = str(meta["path"]).strip("/")
                if candidate in protected:
                    say(f"{C_ERR}[!] GUARDRAIL: {candidate} is mounted, in fstab, a snapshot root, or the "
                        f"filesystem default subvolume. Refusing.{C_RESET}")
                else:
                    say(f"  target {candidate}")
                    victims.append(meta)
            if victims and confirm(f"Permanently delete {len(victims)} subvolume(s)?"):
                grouped: dict[str, list[str]] = {}
                for meta in victims:
                    grouped.setdefault(str(meta["fs_uuid"]), []).append(str(meta["path"]))
                with dusky_lock():
                    for fs_uuid, paths in grouped.items():
                        with top_level(Filesystem(fs_uuid, "")) as top:
                            for rel in paths:
                                victim = top / rel
                                if not victim.exists():
                                    say(f"{C_WARN}[-] already gone: {rel}{C_RESET}")
                                    continue
                                run("btrfs", "subvolume", "delete", "--", str(victim), check=True)
                            run("btrfs", "filesystem", "sync", str(top))
                good("[+] Deleted.")
            pause()
    return False


# =============================================================================
# TOP-LEVEL SUBVOLUME CREATION
# =============================================================================
def validate_subvol_name(name: str) -> str:
    candidate = name.strip().lstrip("/")
    if not candidate or candidate in (".", ".."):
        die("[!] Invalid subvolume name.")
    if "/" in candidate:
        die("[!] Top-level mode takes a single name such as @data, not a path.")
    if candidate.startswith("."):
        die("[!] Names starting with '.' are reserved.")
    if not SAFE_NAME_RE.fullmatch(candidate):
        die("[!] Use only [A-Za-z0-9@._+:=-]; other characters break fstab, systemd and the bootloader.")
    if any(token in candidate for token in TRANSIENT_TOKENS):
        die("[!] That name collides with a Dusky transient marker.")
    return candidate


def create_top_level_subvolume(fs: Filesystem, name: str, nocow: bool) -> None:
    name = validate_subvol_name(name)
    with dusky_lock(), top_level(fs) as top:
        target = top / name
        if os.path.lexists(target):
            die(f"[!] {name} already exists at the top level.")
        run("btrfs", "subvolume", "create", str(target), check=True)
        if nocow:
            run("chattr", "+C", str(target), check=True)
            attrs = run("lsattr", "-d", str(target))
            flags = attrs.text.split()[0] if attrs.ok and attrs.text else ""
            if "C" not in flags:
                warn("[!] NOCOW could not be verified; check with lsattr -d.")
            else:
                good("[+] Created with copy-on-write disabled (NOCOW).")
        else:
            good("[+] Created.")
        run("btrfs", "filesystem", "sync", str(top))
    say(f"{C_DIM}    mount -o subvol=/{name},noatime UUID={fs.uuid} /your/mountpoint{C_RESET}")


# =============================================================================
# CLI
# =============================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dusky",
        description=f"Dusky Btrfs + Snapper master controller {DUSKY_VERSION}",
        color=True,
        suggest_on_error=True,
    )
    parser.add_argument("--version", action="version", version=f"dusky {DUSKY_VERSION}")
    parser.add_argument("-c", "--config", help="snapper configuration name")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--yes", action="store_true", help="assume yes for confirmations")
    parser.add_argument("--no-remount", action="store_true", help="never live-remount after a non-root restore")
    parser.add_argument("--remount", action="store_true", help="force the live remount attempt")
    parser.add_argument("--fix-default", action="store_true",
                        help="repoint the filesystem default subvolume after activation")
    parser.add_argument("--pair-threshold", type=int, default=PAIR_STRICT_SECONDS,
                        help=f"seconds allowed between paired snapshots (default {PAIR_STRICT_SECONDS})")
    parser.add_argument("--parent", help="parent subvolume (relative to fs root) for an incremental send")
    parser.add_argument("--cleanup-subvol", nargs=2, metavar=("UUID", "SUBVOL"), help=argparse.SUPPRESS)
    parser.add_argument("--tui-preview", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    group = parser.add_mutually_exclusive_group()
    group.add_argument("-l", "--list", action="store_true", help="list snapshots")
    group.add_argument("-C", "--create", metavar="DESC", help="create a snapshot")
    group.add_argument("-R", "--restore", metavar="ID", help="atomically restore a snapshot")
    group.add_argument("-D", "--delete", metavar="ID", help="delete a snapshot")
    group.add_argument("--create-pair", nargs=3, metavar=("CFG1", "CFG2", "DESC"), help="coordinated create")
    group.add_argument("--restore-pair", nargs=4, metavar=("CFG1", "ID1", "CFG2", "ID2"), help="coordinated restore")
    group.add_argument("--delete-pair", nargs=4, metavar=("CFG1", "ID1", "CFG2", "ID2"), help="coordinated delete")
    group.add_argument("--sync-restore", nargs="+", metavar="ARG", help="pair-match then restore (DATE|ID [DESC])")
    group.add_argument("--sync-delete", nargs="+", metavar="ARG", help="pair-match then delete (DATE|ID [DESC])")
    group.add_argument("--backup", nargs=2, metavar=("ID", "DEST"), help="send a snapshot of --config to DEST")
    group.add_argument("--list-subvols", action="store_true", help="enumerate subvolumes")
    group.add_argument("--sweep", action="store_true", help="report orphaned transient artefacts")
    group.add_argument("--sweep-apply", action="store_true", help="reclaim orphaned transient artefacts")
    group.add_argument("--doctor", action="store_true", help="audit the system for rollback readiness")
    return parser


def _sync_args(values: Sequence[str]) -> tuple[str | None, str | None, str | None]:
    if not 1 <= len(values) <= 2:
        die("[!] --sync-* takes DATE_OR_ID [DESCRIPTION].")
    first = values[0]
    description = values[1] if len(values) == 2 else None
    if first.isdigit():
        return None, description, first
    return first, description, None


def main(argv: Sequence[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.tui_preview is not None:
        rest = list(args.tui_preview)
        show_diff = "--show-diff" in rest
        rest = [a for a in rest if a != "--show-diff"]
        if not rest:
            return 0
        tui_preview(rest[0], " ".join(rest[1:]), show_diff=show_diff)
        return 0

    ensure_root()
    with suppress(OSError):
        os.chdir("/")

    remount = not args.no_remount
    if args.remount:
        remount = True

    if args.cleanup_subvol:
        cmd_cleanup_subvol(args.cleanup_subvol[0], args.cleanup_subvol[1])
        return 0
    if args.doctor:
        cmd_doctor()
        return 0
    if args.sweep or args.sweep_apply:
        found = sweep_orphans(apply=args.sweep_apply)
        good(f"[+] {found} orphaned artefact(s) {'reclaimed' if args.sweep_apply else 'found'}.")
        return 0
    if args.list_subvols:
        subvols = enumerate_subvolumes(include_snapshots=args.json, include_transient=args.json)
        if args.json:
            say(json.dumps([s.as_meta() for s in subvols], ensure_ascii=False))
        else:
            for item in sorted(subvols, key=lambda s: s.path):
                say(f"{item.id:>6}  {'ro' if item.readonly else 'rw'}  {item.mounted_at or '-':<16}  {item.path}")
        return 0

    needs_config = any((args.list, args.create, args.restore, args.delete, args.backup))
    if needs_config and not args.config:
        parser.error("-c/--config is required for --list/--create/--restore/--delete/--backup")

    if args.list:
        cmd_list(args.config, args.json)
    elif args.create:
        cmd_create(args.config, args.create)
    elif args.delete:
        cmd_delete(args.config, args.delete)
    elif args.restore:
        cmd_restore(args.config, args.restore, remount=remount, fix_default=args.fix_default, assume_yes=args.yes)
    elif args.create_pair:
        cmd_create_pair(*args.create_pair)
    elif args.delete_pair:
        cmd_delete_pair(*args.delete_pair)
    elif args.restore_pair:
        cmd_restore_pair(*args.restore_pair, remount=remount, fix_default=args.fix_default, assume_yes=args.yes)
    elif args.backup:
        snap_id, destination = args.backup
        mountpoint = snapper_config_subvolume(args.config)
        fs = filesystem_of(mountpoint)
        snaps = active_subvol(snapshots_mountpoint(mountpoint))
        if snaps is None:
            die("[!] The snapshot store is not a mounted subvolume.")
        backup_subvolume(fs, f"{snaps[0]}/{validate_snap_id(snap_id)}/snapshot", destination, parent_rel=args.parent)
    elif args.sync_restore or args.sync_delete:
        values = args.sync_restore or args.sync_delete
        date, description, hint = _sync_args(values)
        match = find_pair(
            "root", "home",
            target_date=date, target_desc=description,
            left_id_hint=hint, threshold=args.pair_threshold,
        )
        say(f"[*] pair: root={match.left_id} home={match.right_id} ({match.method}, {match.delta:.0f}s)")
        if args.sync_restore:
            cmd_restore_pair("root", match.left_id, "home", match.right_id,
                             remount=remount, fix_default=args.fix_default, assume_yes=args.yes)
        else:
            cmd_delete_pair("root", match.left_id, "home", match.right_id)
    else:
        parser.error("no action requested (try --doctor, --list, or run with no arguments for the TUI)")
    return 0


def entrypoint() -> int:
    try:
        if len(sys.argv) == 1:
            ensure_root()
            with suppress(OSError):
                os.chdir("/")
            launch_tui()
            return 0
        return main(sys.argv[1:])
    except DuskyAbort as exc:
        print(f"{C_DIM}{exc}{C_RESET}", file=sys.stderr)
        return 130
    except DuskyError as exc:
        print(f"{C_ERR}{exc}{C_RESET}", file=sys.stderr)
        LOG.critical(strip_ansi(str(exc)))
        return exc.exit_code
    except KeyboardInterrupt:
        print(f"\n{C_ERR}[!] Interrupted.{C_RESET}", file=sys.stderr)
        return 130
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(entrypoint())
