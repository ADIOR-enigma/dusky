#!/usr/bin/env python3

import sys
import subprocess
import shutil
import importlib.util
import os
import re
import argparse
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import platform
import time
from pathlib import Path

# ==========================================
# 1. AUTONOMOUS FAIL-SAFE DEPENDENCY RESOLVER
# ==========================================
def resolve_dependencies() -> None:
    """Iterative dependency resolver with TTY awareness and PIP/AUR fallbacks."""
    requirements = {
        "rich": {"pac": "python-rich", "pip": "rich"},
        "keyring": {"pac": "python-keyring", "pip": "keyring"},
        "questionary": {"pac": "python-questionary", "pip": "questionary"},
        "psutil": {"pac": "python-psutil", "pip": "psutil"}
    }

    missing = [mod for mod in requirements if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    if not sys.stdout.isatty():
        print(f"\n[✖] FATAL: Missing dependencies ({', '.join(missing)}) in non-interactive shell.")
        print("[✖] Cannot invoke pacman/sudo. Please run interactively to bootstrap.")
        sys.exit(1)

    print(f"\n[*] Missing dependencies detected: {', '.join(missing)}")
    print("[*] Engaging autonomous fail-safe resolver...\n")

    subprocess.run(["sudo", "-v"], check=False)
    aur_helper = next((h for h in ["paru", "yay"] if shutil.which(h)), None)

    for mod in missing:
        pkg_pac = requirements[mod]["pac"]
        pkg_pip = requirements[mod]["pip"]
        print(f" -> Resolving '{mod}'...")
        
        success = False

        if aur_helper:
            res = subprocess.run([aur_helper, "-S", "--needed", "--noconfirm", pkg_pac], 
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            success = (res.returncode == 0)

        if not success:
            res = subprocess.run(["sudo", "pacman", "-S", "--needed", "--noconfirm", pkg_pac], 
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            success = (res.returncode == 0)

        if not success:
            print(f"    [!] '{pkg_pac}' absent from repos. Injecting via pip bypass...")
            res = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", pkg_pip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            success = (res.returncode == 0)

        if not success:
            print(f"\n[✖] FATAL: Absolute failure resolving '{mod}'.")
            sys.exit(1)

    print("\n[✔] Matrix dependencies successfully satisfied. Rebooting manager...\n")
    os.execv(sys.executable, [sys.executable] + sys.argv)

resolve_dependencies()

from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.table import Table
from rich.align import Align
from rich.text import Text
from rich.rule import Rule
import keyring
import questionary
import psutil

# ==========================================
# 2. UI THEMING & GLOBAL INIT
# ==========================================
custom_theme = Theme({
    "info": "dim cyan",
    "warning": "bold yellow",
    "error": "bold red",
    "success": "bold green",
    "highlight": "bold magenta",
    "muted": "dim white"
})

console = Console(theme=custom_theme)

if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    console.print("[warning]⚠ DBUS_SESSION_BUS_ADDRESS not found. Keyring auth operations may fail.[/warning]")

# ==========================================
# 3. MODERN TYPE ALIASES (Python 3.12+)
# ==========================================
type ProcList = list[psutil.Process]
type ProfileList = list[str]

# ==========================================
# 3.5 GOOGLE INTEGRATION (borrowed from AntigravityManager)
# ==========================================
# The Manager refreshes the Google access token *before* account-switch injection
# (see docs/cloud_features.md 2.4 and cli/core.py:refresh_access_token), verifies
# tokens against Google's quota API, and restarts the IDE after switching
# (see switchFlow.ts). This keeps profile restores fresh instead of injecting stale
# tokens.
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Public OAuth client used by Antigravity itself (OAuthClientRegistryService.ts);
# refresh tokens are bound to this client, so refreshed tokens are accepted by the
# IDE. Extra clients can be supplied via ANTIGRAVITY_OAUTH_CLIENTS
# ("key|client_id|client_secret|label;...") and selected with
# ANTIGRAVITY_OAUTH_CLIENT_KEY, exactly like the project reads them.
OAUTH_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
OAUTH_CLIENT_SECRET = "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"
DEFAULT_OAUTH_CLIENT_KEY = "antigravity_enterprise"
OAUTH_CLIENTS_ENV = "ANTIGRAVITY_OAUTH_CLIENTS"
ACTIVE_OAUTH_CLIENT_ENV = "ANTIGRAVITY_OAUTH_CLIENT_KEY"
TOKEN_REFRESH_TIMEOUT_S = 20

# Internal Cloud Code APIs (mirrors GoogleAPIService.ts endpoint lists).
LOAD_PROJECT_ENDPOINTS = [
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist",
]
QUOTA_API_ENDPOINTS = [
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels",
    "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
]
API_TIMEOUT_S = 20


def _platform_arch() -> str:
    """Platform/arch tags for the User-Agent (mirrors buildUserAgent in the project)."""
    plat = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        arch = "amd64"
    elif arch in ("aarch64", "arm64"):
        arch = "arm64"
    return f"{plat}/{arch}"


def build_user_agent(version: str = "2.5.0") -> str:
    """User-Agent in the same format the project sends to Google's APIs."""
    return f"antigravity/{version} {_platform_arch()}"


def is_token_expired(expiry: object) -> bool:
    """True when the ISO-8601 access-token expiry is missing, unparseable, or within 60s."""
    if not isinstance(expiry, str) or not expiry.strip():
        return True
    try:
        exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= exp_dt - timedelta(seconds=60)


def format_token_expiry(expires_in: int) -> str:
    """RFC-3339 expiry (UTC, milliseconds) matching the IDE's credential-store payload."""
    ts = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    return ts.isoformat(timespec="milliseconds")


def _oauth_clients() -> list[dict]:
    """Resolve OAuth clients, honoring the same env overrides the project reads."""
    clients = [{
        "key": DEFAULT_OAUTH_CLIENT_KEY,
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
    }]
    raw = os.environ.get(OAUTH_CLIENTS_ENV, "").strip()
    if raw:
        for entry in raw.split(";"):
            parts = [part.strip() for part in entry.split("|")]
            if len(parts) < 3:
                continue
            key = parts[0].lower()
            client_id, client_secret = parts[1], parts[2]
            if not key or not client_id or not client_secret:
                continue
            existing = next((c for c in clients if c["key"] == key), None)
            if existing:
                existing.update({"client_id": client_id, "client_secret": client_secret})
            else:
                clients.append({"key": key, "client_id": client_id, "client_secret": client_secret})
    active = os.environ.get(ACTIVE_OAUTH_CLIENT_ENV, DEFAULT_OAUTH_CLIENT_KEY).strip().lower()
    active_client = next((c for c in clients if c["key"] == active), None)
    if active_client:
        return [active_client] + [c for c in clients if c["key"] != active]
    return clients


def _refresh_with_client(refresh_token: str, client_id: str, client_secret: str) -> tuple[dict | None, str, str]:
    """One refresh attempt with a specific OAuth client.

    Returns (result, status, reason); status is 'ok', 'client-mismatch' (try the
    next client), 'rejected' (stop), or 'network' (stop).
    """
    body = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    try:
        with urlopen(Request(OAUTH_TOKEN_URL, data=body, method="POST"), timeout=TOKEN_REFRESH_TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        # Surface the OAuth error (e.g. invalid_grant = dead refresh token).
        # These error codes never contain secrets.
        reason = "unknown error"
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            reason = error_body.get("error") or error_body.get("error_description") or reason
        except Exception:
            pass
        if e.code in (400, 401, 403) or "unauthorized_client" in reason or "invalid_client" in reason:
            return None, "client-mismatch", reason
        return None, "rejected", f"HTTP {e.code}: {reason}"
    except Exception as e:
        return None, "network", str(e)
    if "access_token" not in result or "expires_in" not in result:
        return None, "rejected", "malformed response"
    return result, "ok", ""


def refresh_access_token(refresh_token: str) -> dict | None:
    """Exchange an expired access token via Google's OAuth token endpoint (stdlib only).

    Tries configured OAuth clients in order, mirroring GoogleAPIService.refreshAccessToken.
    """
    last_reason = "no OAuth clients configured"
    last_status = "rejected"
    for client in _oauth_clients():
        result, status, reason = _refresh_with_client(
            refresh_token, client["client_id"], client["client_secret"]
        )
        if result is not None:
            return result
        last_reason = reason
        last_status = status
        if status != "client-mismatch":
            break
    label = "failed (network)" if last_status == "network" else "rejected"
    console.print(f"[warning]⚠ Token refresh {label}: {last_reason}[/warning]")
    return None


def fetch_available_models(access_token: str, timeout_s: int = API_TIMEOUT_S) -> dict:
    """Fetch live model quota from Google's internal API (mirrors fetchQuota).

    Returns {'models': {name: percent}} on success, or {'error': reason, 'auth': bool}
    on failure ('auth' distinguishes token rejection from transient API trouble).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": build_user_agent(),
        "Content-Type": "application/json",
    }
    project_id: str | None = None
    for endpoint in LOAD_PROJECT_ENDPOINTS:
        try:
            with urlopen(
                Request(
                    endpoint,
                    data=json.dumps({"metadata": {"ideType": "ANTIGRAVITY"}}).encode("utf-8"),
                    headers=headers,
                    method="POST",
                ),
                timeout=timeout_s,
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            project_id = data.get("cloudaicompanionProject")
            if project_id:
                break
        except Exception:
            continue

    payload_dict: dict = {"project": project_id} if project_id else {}
    last_error: str | None = None
    last_auth = False
    for endpoint in QUOTA_API_ENDPOINTS:
        # Mirror GoogleAPIService.fetchQuota: a 403 with a project ID attached is
        # retried on the same endpoint without the project before moving on.
        attempts = [payload_dict] + ([{}] if project_id else [])
        for attempt_payload in attempts:
            try:
                with urlopen(
                    Request(
                        endpoint,
                        data=json.dumps(attempt_payload).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    ),
                    timeout=timeout_s,
                ) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                models: dict[str, int] = {}
                for name, info in (raw.get("models") or {}).items():
                    quota_info = info.get("quotaInfo") or {}
                    fraction = quota_info.get("remainingFraction")
                    if isinstance(fraction, (int, float)):
                        models[name] = max(0, min(100, int(fraction * 100)))
                if not models:
                    last_error = "no quota info in response"
                    last_auth = False
                    continue
                return {"models": models}
            except HTTPError as e:
                if e.code == 401:
                    return {"error": "HTTP 401 (token rejected or forbidden)", "auth": True}
                if e.code == 403:
                    last_error = "HTTP 403 (token rejected or forbidden)"
                    last_auth = True
                    continue
                last_error = f"HTTP {e.code}"
                last_auth = False
                continue
            except Exception as e:
                last_error = str(e)
                last_auth = False
                continue
    return {"error": last_error or "failed", "auth": last_auth}


def find_antigravity_executable() -> str | None:
    """Locate the Antigravity binary (mirrors paths.ts getAntigravityExecutablePath).

    Priority: AGM_ANTIGRAVITY_BIN > gui_config.json > PATH (preserves user wrapper
    flags such as --disable-gpu-sandbox) > known Linux install paths.
    """
    env_path = os.environ.get("AGM_ANTIGRAVITY_BIN", "").strip()
    # A configured-but-missing path falls through, mirroring the project's
    # getConfiguredAntigravityExecutablePath(requireExists=true) semantics.
    if env_path and os.path.exists(env_path):
        return env_path
    try:
        config_path = Path.home() / ".antigravity-agent" / "gui_config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        exe = str(data.get("antigravity_executable") or "").strip()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    from_path = shutil.which("antigravity")
    if from_path:
        return from_path
    for candidate in (
        "/usr/bin/antigravity",
        "/usr/local/bin/antigravity",
        "/usr/share/antigravity/antigravity",
        "/opt/Antigravity/antigravity",
        "/opt/antigravity/antigravity",
        str(Path.home() / ".local" / "share" / "antigravity" / "antigravity"),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def start_antigravity() -> bool:
    """Relaunch Antigravity detached (mirrors switchFlow.startAntigravity / cli start_process)."""
    exe = find_antigravity_executable()
    if not exe:
        console.print("[warning]⚠ Could not locate the Antigravity executable to relaunch.[/warning]")
        return False
    args: list[str] = []
    try:
        config_data = json.loads(
            (Path.home() / ".antigravity-agent" / "gui_config.json").read_text(encoding="utf-8")
        )
        raw_args = config_data.get("antigravity_args")
        if isinstance(raw_args, list):
            args = [str(arg) for arg in raw_args]
    except Exception:
        pass
    try:
        subprocess.Popen(
            [exe, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        console.print(f"[success]✔ Relaunched Antigravity ({exe}).[/success]")
        return True
    except Exception as e:
        console.print(f"[error]✖ Failed to relaunch Antigravity: {e}[/error]")
        return False

# ==========================================
# 4. CORE MANAGER CLASS
# ==========================================
class ProfileManager:
    def __init__(self, force_mode: bool = False, restart_mode: bool = False) -> None:
        self.force_mode = force_mode
        self.restart_mode = restart_mode
        # Environment overrides enable relocating storage and sandboxed testing.
        self.storage_dir = Path(
            os.environ.get("AGM_STORAGE_DIR")
            or (Path.home() / ".config" / "dusky" / "settings" / "apps" / "antigravity")
        )
        self.profiles_dir = self.storage_dir / "profiles"
        self.active_profile_file = self.profiles_dir / "active_profile.txt"
        self.order_file = self.profiles_dir / "profile_order.txt"
        self.service = os.environ.get("AGM_KEYRING_SERVICE", "gemini")
        self.account = os.environ.get("AGM_KEYRING_ACCOUNT", "antigravity")
        
        try:
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            console.print(f"[error]✖ Fatal: Filesystem constraint preventing directory creation in {self.storage_dir}: {e}[/error]")
            sys.exit(1)

    @staticmethod
    def is_valid_name(name: str) -> bool:
        """Strict alphanumeric, dash, and underscore validation."""
        return bool(re.match(r"^[a-zA-Z0-9_-]+$", name))

    def get_active(self) -> str | None:
        if self.active_profile_file.is_file():
            try:
                name = self.active_profile_file.read_text(encoding="utf-8").strip()
                # Security: Prevent path traversal by validating the string
                if name and self.is_valid_name(name) and (self.profiles_dir / name).is_dir():
                    return name
            except IOError as e:
                console.print(f"[warning]⚠ State read error: {e}[/warning]")
        return None

    def _read_order(self) -> list[str]:
        """Read the persisted display order (one profile name per line), if any."""
        try:
            if not self.order_file.is_file():
                return []
            return [
                line.strip()
                for line in self.order_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and self.is_valid_name(line.strip())
            ]
        except (IOError, OSError):
            return []

    def _persist_order(self) -> None:
        """Persist the current effective display order to the order file."""
        try:
            self.order_file.write_text("\n".join(self.get_all()) + "\n", encoding="utf-8")
        except (IOError, OSError) as e:
            console.print(f"[warning]⚠ Could not persist profile order: {e}[/warning]")

    def get_all(self) -> ProfileList:
        try:
            # Security: Filter out invalid directories (e.g. backup folders)
            dirs = [p.name for p in self.profiles_dir.iterdir() if p.is_dir() and self.is_valid_name(p.name)]
            # Ordered list from the order file (filtered to existing profiles, deduped)
            ordered = list(dict.fromkeys(name for name in self._read_order() if name in dirs))
            # Self-heal: append any profiles not listed yet (e.g. imported/created), sorted
            missing = sorted(set(dirs) - set(ordered))
            return ordered + missing
        except (IOError, OSError):
            return []

    def _get_token_path(self, profile_name: str) -> Path:
        """Resolve token path and silently migrate legacy .json extensions to .txt"""
        legacy = self.profiles_dir / profile_name / "keyring_token.json"
        txt = self.profiles_dir / profile_name / "keyring_token.txt"
        if legacy.exists() and not txt.exists():
            try:
                legacy.rename(txt)
            except OSError:
                pass
        return txt

    def check_running_processes(self) -> ProcList:
        """Kernel-level mapping with exact basename precision and lineage exclusions."""
        procs: ProcList = []
        current_pid = os.getpid()
        parent_pid = os.getppid()
        
        try:
            grandparent_pid = psutil.Process(parent_pid).ppid()
        except psutil.Error:
            grandparent_pid = -1
            
        exclude_pids = {current_pid, parent_pid, grandparent_pid}
        target_bins = {"antigravity", "agy", "antigravity-cli", "antigravity-ide"}
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['pid'] in exclude_pids:
                    continue
                    
                name = (proc.info['name'] or "").lower()
                cmdline = proc.info['cmdline'] or []
                
                is_match = False
                
                if name in target_bins:
                    is_match = True
                else:
                    for arg in cmdline:
                        base = Path(arg).name.lower()
                        if base in target_bins and "switch_accounts" not in base:
                            is_match = True
                            break
                            
                if is_match:
                    procs.append(proc)
            except psutil.Error:
                pass
        return procs

    def kill_processes(self, processes: ProcList) -> None:
        """Safely terminate blocking processes with broad exception handling."""
        for proc in processes:
            try:
                proc.terminate()
            except psutil.Error:
                continue
        
        gone, alive = psutil.wait_procs(processes, timeout=3.0)
        for proc in alive:
            try:
                proc.kill() 
            except psutil.Error:
                pass
                
        console.print("[success]✔ Conflicting processes resolved.[/success]")

    def stash_keyring(self, profile_name: str) -> None:
        try:
            token = keyring.get_password(self.service, self.account)
            token_file = self._get_token_path(profile_name)
            if token:
                # Pre-create with 0600 so the payload is never world-readable, even briefly
                token_file.touch(mode=0o600, exist_ok=True)
                token_file.write_text(token, encoding="utf-8")
                # Security Override: Force UNIX permissions regardless of existing file state
                token_file.chmod(0o600)
                console.print(f"[info]ℹ Secured auth token to '{profile_name}'.[/info]")
            else:
                console.print(f"[warning]⚠ OS keyring returned no credentials for {self.service}/{self.account}; nothing stashed.[/warning]")
        except Exception as e:
            console.print(f"[warning]⚠ Credential stash failure: {e}[/warning]")

    def restore_keyring(self, profile_name: str) -> None:
        token_file = self._get_token_path(profile_name)
        if token_file.is_file():
            try:
                token = token_file.read_text(encoding="utf-8").strip()
                if not token:
                    console.print(f"[warning]⚠ Empty credential payload in '{profile_name}'. Skipping restore.[/warning]")
                    return
                restored = self._maybe_refresh_token(profile_name, token)
                keyring.set_password(self.service, self.account, restored)
                if restored != token:
                    token_file.touch(mode=0o600, exist_ok=True)
                    token_file.write_text(restored, encoding="utf-8")
                    token_file.chmod(0o600)
                    console.print(f"[success]✔ Stash for '{profile_name}' updated with refreshed token.[/success]")
                read_back = keyring.get_password(self.service, self.account)
                if read_back != restored:
                    # Secret Service can briefly return None right after a write;
                    # retry once before reporting a problem.
                    time.sleep(0.5)
                    read_back = keyring.get_password(self.service, self.account)
                if read_back == restored:
                    console.print(f"[success]✔ Keyring verified: '{profile_name}' restored, read-back round-trip intact ({len(restored)} bytes).[/success]")
                else:
                    console.print(f"[warning]⚠ Keyring read-back mismatch for '{profile_name}': write completed but verification could not be confirmed.[/warning]")
            except Exception as e:
                console.print(f"[error]✖ Credential restore failure: {e}[/error]")
        else:
            try:
                keyring.delete_password(self.service, self.account)
                console.print("[info]ℹ Purged global auth state (profile initialized fresh).[/info]")
            except Exception:
                pass 

    def _maybe_refresh_token(self, profile_name: str, raw: str, verify: bool = True) -> str:
        """Best-effort proactive refresh of an expired access token before injection.

        Borrowed from AntigravityManager's switch workflow (refresh before injection).
        Format-preserving: only access_token/expiry are touched; id_token, auth_method,
        token_type and refresh_token are kept intact. Never blocks a switch: on any
        failure the original payload is returned unchanged, matching the legacy script.
        """
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if not isinstance(payload, dict):
            return raw
        token = payload.get("token")
        if not isinstance(token, dict):
            return raw
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return raw
        if not is_token_expired(token.get("expiry")):
            return raw
        console.print(f"[info]⟳ Refreshing expired token for '{profile_name}'...[/info]")
        result = refresh_access_token(refresh_token)
        if result is None:
            console.print(f"[warning]⚠ Could not refresh expired token for '{profile_name}'; using stored token (IDE may re-auth).[/warning]")
            return raw
        token["access_token"] = result["access_token"]
        token["expiry"] = format_token_expiry(result["expires_in"])
        fresh_id = result.get("id_token")
        if isinstance(fresh_id, str) and fresh_id:
            payload["id_token"] = fresh_id
        if verify:
            # Bounded to 5s on the switch path so a quota-API hiccup never stalls
            # the actual switch (the refresh itself already used its own timeout).
            verified = fetch_available_models(result["access_token"], timeout_s=5)
            if "error" in verified:
                console.print(f"[warning]⚠ Refreshed token could not be verified against Google: {verified['error']}.[/warning]")
            else:
                model_count = len(verified["models"])
                min_pct = min(verified["models"].values()) if verified["models"] else 0
                console.print(f"[success]✔ Refreshed token verified against Google ({model_count} models, lowest quota {min_pct}%).[/success]")
        console.print(f"[success]✔ Refreshed expired access token for '{profile_name}'.[/success]")
        return json.dumps(payload, separators=(",", ":"))

    def check_profile(self, profile_name: str) -> bool:
        """Validate a profile's token (refresh if expired) and verify it live against Google."""
        token_file = self._get_token_path(profile_name)
        if not token_file.is_file():
            console.print(f"[error]✖ Profile '{profile_name}' has no stored credentials.[/error]")
            return False
        try:
            raw = token_file.read_text(encoding="utf-8").strip()
            # verify=False: the table fetch below is the single authoritative check.
            refreshed = self._maybe_refresh_token(profile_name, raw, verify=False)
            if refreshed != raw:
                token_file.touch(mode=0o600, exist_ok=True)
                token_file.write_text(refreshed, encoding="utf-8")
                token_file.chmod(0o600)
                # Keep the OS keyring in sync when the checked profile is the active one;
                # otherwise the IDE keeps serving the stale token the user just refreshed.
                if profile_name == self.get_active():
                    keyring.set_password(self.service, self.account, refreshed)
                console.print(f"[success]✔ Stash for '{profile_name}' updated with refreshed token.[/success]")
            payload = json.loads(refreshed)
            token = payload.get("token") if isinstance(payload, dict) else None
            access_token = token.get("access_token") if isinstance(token, dict) else None
            if not isinstance(access_token, str) or not access_token:
                console.print(f"[error]✖ No access token available for '{profile_name}'.[/error]")
                return False
            result = fetch_available_models(access_token)
            if "error" in result:
                if result.get("auth"):
                    console.print(f"[error]✖ Token rejected by Google for '{profile_name}': {result['error']}[/error]")
                    return False
                console.print(f"[warning]⚠ Could not reach Google's quota API for '{profile_name}' ({result['error']}); token status unknown.[/warning]")
                return True
            models = result["models"]
            if not models:
                console.print(f"[warning]⚠ '{profile_name}' verified but returned no model quota info.[/warning]")
                return True
            table = Table(title=f"Live Quota — {profile_name}", border_style="cyan", expand=True)
            table.add_column("Model", style="cyan")
            table.add_column("Remaining", justify="right")
            for name in sorted(models):
                pct = models[name]
                style = "bold green" if pct >= 50 else ("bold yellow" if pct >= 20 else "bold red")
                table.add_row(name, Text(f"{pct}%", style=style))
            console.print(table)
            console.print(f"[success]✔ '{profile_name}' verified against Google.[/success]")
            return True
        except Exception as e:
            console.print(f"[error]✖ Verification error for '{profile_name}': {e}[/error]")
            return False

    def switch(self, target_profile: str) -> bool:
        if not self.is_valid_name(target_profile):
            console.print(f"[error]✖ Error: Invalid profile syntax '{target_profile}'.[/error]")
            return False

        current_profile = self.get_active()
        if current_profile == target_profile:
            console.print(f"[info]ℹ State unchanged. Already on '{target_profile}'.[/info]")
            if self.restart_mode:
                start_antigravity()
            return True

        running_procs = self.check_running_processes()
        if running_procs:
            if self.restart_mode:
                console.print(f"[warning]⚠ {len(running_procs)} Antigravity process(es) running — closing gracefully for restart...[/warning]")
                self.kill_processes(running_procs)
            elif self.force_mode:
                console.print("[warning]⚠ Force override active: Bypassing process collision checks.[/warning]")
            elif not sys.stdin.isatty():
                console.print(f"\n[error]✖ Active Antigravity processes detected in non-interactive mode. Aborting switch to prevent background hang. Use -f/--force to override.[/error]")
                return False
            else:
                console.print(f"\n[warning]⚠ {len(running_procs)} Active Antigravity process(es) detected![/warning]")
                action = questionary.select(
                    "Resolve collision:",
                    choices=[
                        questionary.Choice("Abort (Safe)", value="cancel"),
                        questionary.Choice("SIGKILL & Proceed", value="kill"),
                        questionary.Choice("Ignore & Proceed (Risky)", value="ignore")
                    ],
                    style=questionary.Style([('pointer', 'fg:ansiyellow bold')])
                ).ask()
                
                match action:
                    case "cancel" | None:
                        console.print("[error]Operation aborted.[/error]")
                        return False
                    case "kill":
                        self.kill_processes(running_procs)
                    case "ignore":
                        console.print("[warning]Proceeding with collision risk...[/warning]")

        if current_profile:
            self.stash_keyring(current_profile)

        target_path = self.profiles_dir / target_profile
        try:
            target_path.mkdir(parents=True, exist_ok=True)
            self.restore_keyring(target_profile)
            self.active_profile_file.write_text(target_profile, encoding="utf-8")
        except IOError as e:
            console.print(f"[error]✖ IO fault during state switch: {e}[/error]")
            return False
            
        console.print(f"\n[success]✔ Switched to isolated profile: '{target_profile}'.[/success]")
        if self.restart_mode:
            start_antigravity()
        return True

    def cycle_next(self) -> bool:
        profiles = self.get_all()
        if not profiles:
            console.print("[error]✖ Error: Array is empty. No profiles to cycle.[/error]")
            return False
            
        active = self.get_active()
        next_profile = profiles[0] if active not in profiles else profiles[(profiles.index(active) + 1) % len(profiles)]
            
        console.print(f"\n[info]⟳ Iterating to next sequential profile...[/info]")
        return self.switch(next_profile)

    def create(self, name: str) -> None:
        if not self.is_valid_name(name):
            console.print("[error]✖ Syntax Error: Alphanumeric, dash, and underscores exclusively.[/error]")
            return
            
        profile_path = self.profiles_dir / name
        if profile_path.is_dir():
            console.print(f"[error]✖ Collision: Profile '{name}' already exists.[/error]")
            return
            
        try:
            profile_path.mkdir(parents=True)
            console.print(f"[success]✔ Initialized isolated context: '{name}'.[/success]")
            self._persist_order()
            if questionary.confirm("Execute context switch to new profile now?").ask():
                self.switch(name)
        except OSError as e:
            console.print(f"[error]✖ IO Error during initialization: {e}[/error]")

    def delete(self, name: str) -> None:
        if name == self.get_active():
            console.print("[error]✖ State lock: Cannot wipe the active profile. Cycle first.[/error]")
            return
            
        profile_path = self.profiles_dir / name
        if not profile_path.is_dir():
            console.print(f"[error]✖ Missing Reference: '{name}' does not exist.[/error]")
            return
            
        if questionary.confirm(f"Permanently wipe '{name}' and all isolated data?").ask():
            try:
                shutil.rmtree(profile_path)
                console.print(f"[success]✔ Profile '{name}' successfully eradicated.[/success]")
                self._persist_order()
            except OSError as e:
                console.print(f"[error]✖ IO Fault during deletion: {e}[/error]")

    def rename(self, old_name: str, new_name: str) -> bool:
        """Rename a saved profile (directory plus active marker if applicable)."""
        if old_name == new_name:
            console.print(f"[info]ℹ New name identical to current name.[/info]")
            return False
        if not self.is_valid_name(old_name) or not self.is_valid_name(new_name):
            console.print("[error]✖ Syntax Error: Alphanumeric, dash, and underscores exclusively.[/error]")
            return False
        old_path = self.profiles_dir / old_name
        if not old_path.is_dir():
            console.print(f"[error]✖ Missing Reference: '{old_name}' does not exist.[/error]")
            return False
        new_path = self.profiles_dir / new_name
        if new_path.is_dir():
            console.print(f"[error]✖ Collision: Profile '{new_name}' already exists.[/error]")
            return False
        # Capture active state BEFORE renaming: get_active() validates that the
        # profile directory exists, so it returns None once the dir is renamed.
        was_active = self.get_active() == old_name
        ordered = self._read_order()
        try:
            old_path.rename(new_path)
            if was_active:
                self.active_profile_file.write_text(new_name, encoding="utf-8")
            # Preserve the profile's position in the display/cycle order
            if ordered:
                if old_name in ordered:
                    ordered = [new_name if name == old_name else name for name in ordered]
                else:
                    ordered = ordered + [new_name]
                self.order_file.write_text("\n".join(ordered) + "\n", encoding="utf-8")
            console.print(f"[success]✔ Profile '{old_name}' renamed to '{new_name}'.[/success]")
            return True
        except OSError as e:
            console.print(f"[error]✖ IO Fault during rename: {e}[/error]")
            return False

    def reorder(self, name: str, direction: str) -> bool:
        """Move a profile up or down in the display/cycle order."""
        profiles = self.get_all()
        if name not in profiles:
            console.print(f"[error]✖ Missing Reference: '{name}' does not exist.[/error]")
            return False
        if len(profiles) < 2:
            console.print("[info]ℹ Need at least two profiles to reorder.[/info]")
            return False
        idx = profiles.index(name)
        if direction == "up":
            if idx == 0:
                console.print(f"[info]ℹ '{name}' is already at the top.[/info]")
                return False
            profiles[idx], profiles[idx - 1] = profiles[idx - 1], profiles[idx]
        elif direction == "down":
            if idx == len(profiles) - 1:
                console.print(f"[info]ℹ '{name}' is already at the bottom.[/info]")
                return False
            profiles[idx], profiles[idx + 1] = profiles[idx + 1], profiles[idx]
        else:
            console.print(f"[error]✖ Invalid direction: '{direction}'. Use 'up' or 'down'.[/error]")
            return False
        try:
            self.order_file.write_text("\n".join(profiles) + "\n", encoding="utf-8")
            console.print(f"[success]✔ Moved '{name}' {direction} (now position {profiles.index(name) + 1}).[/success]")
            return True
        except (IOError, OSError) as e:
            console.print(f"[error]✖ IO Fault during reorder: {e}[/error]")
            return False

    def render_dashboard(self) -> None:
        active = self.get_active()
        profiles = self.get_all()
        
        table = Table(title="Local Isolation Matrix", title_style="highlight", border_style="magenta", expand=True)
        table.add_column("Index", justify="right", style="cyan", no_wrap=True)
        table.add_column("State", justify="center", no_wrap=True)
        table.add_column("Profile Name", style="success")
        table.add_column("Login Status", justify="center", no_wrap=True)
        
        for idx, p in enumerate(profiles, start=1):
            is_active = p == active
            status_text = Text("● ACTIVE", style="bold green") if is_active else Text("○ STANDBY", style="dim white")
            
            token_file = self._get_token_path(p)
            auth_state = Text("Void", style="dim yellow")
            if token_file.is_file() and token_file.stat().st_size > 0:
                try:
                    payload = json.loads(token_file.read_text(encoding="utf-8"))
                    token_info = payload.get("token") if isinstance(payload, dict) else None
                    expiry = token_info.get("expiry") if isinstance(token_info, dict) else None
                    if is_token_expired(expiry):
                        auth_state = Text("Secured (Expired)", style="bold yellow")
                    else:
                        auth_state = Text("Secured (Valid)", style="bold cyan")
                except Exception:
                    auth_state = Text("Secured", style="bold cyan")
            
            table.add_row(str(idx), status_text, p, auth_state)
            
        console.print(Rule(style="dim magenta"))
        if not profiles:
            console.print(Align.center("[muted]No profiles found. Create a profile to begin.[/muted]"))
        else:
            console.print(table)
        console.print(Rule(style="dim magenta"))

# ==========================================
# 5. ROUTER & EVENT LOOP
# ==========================================
def build_profile_choices(profiles: ProfileList, active_profile: str | None = None, lock_active: bool = False) -> list[questionary.Choice]:
    choices = []
    for p in profiles:
        if lock_active and p == active_profile:
            choices.append(questionary.Choice(f"{p} (Active - Locked)", value=p, disabled="Cannot delete active profile"))
        else:
            choices.append(questionary.Choice(p, value=p))
    choices.append(questionary.Choice("↩ Cancel / Go Back", value=None))
    return choices

def interactive_tui(manager: ProfileManager) -> None:
    while True:
        console.clear()
        
        title = Text("🚀 Antigravity Profile Manager", style="bold magenta")
        subtitle = Text("Account Isolation & Credentials Switcher", style="italic cyan")
        console.print(Panel(Align.center(Text.assemble(title, "\n", subtitle)), border_style="magenta"))
        
        manager.render_dashboard()
        
        profiles = manager.get_all()
        main_choices = []
        
        if profiles:
            main_choices.append(questionary.Choice("Switch Profile", value="switch"))
            main_choices.append(questionary.Choice("Cycle to Next Profile", value="cycle"))
            main_choices.append(questionary.Choice("Check Profile Quota", value="check"))
        
        main_choices.extend([
            questionary.Choice("Create New Profile", value="create"),
            questionary.Choice("Delete Profile", value="delete", disabled="No profiles created" if not profiles else ("Cannot delete the only active profile" if len(profiles) == 1 and manager.get_active() in profiles else None)),
            questionary.Choice("Rename Profile", value="rename", disabled="No profiles created" if not profiles else None),
            questionary.Choice("Reorder Profiles", value="reorder", disabled="Need at least two profiles" if len(profiles) < 2 else None),
            questionary.Choice("Backup/Save Credentials", value="stash", disabled="No active profile" if not manager.get_active() else None),
            questionary.Choice("Quit", value="quit")
        ])

        try:
            action = questionary.select(
                "Select Action:",
                choices=main_choices,
                use_indicator=True,
                pointer="❯",
                style=questionary.Style([('pointer', 'fg:ansimagenta bold')])
            ).ask()
        except KeyboardInterrupt:
            console.print("\n[info]Session terminated via interrupt.[/info]")
            break

        if action is None or action == "quit":
            console.print("[info]Session terminated.[/info]")
            break

        console.print("")
        
        try:
            match action:
                case "switch":
                    target = questionary.select(
                        "Select profile to switch to:", 
                        choices=build_profile_choices(profiles),
                        style=questionary.Style([('pointer', 'fg:ansimagenta bold')])
                    ).ask()
                    
                    if target:
                        if manager.switch(target):
                            # A successful switch is the natural end of the task: return to the shell.
                            break
                        questionary.press_any_key_to_continue("\nPress any key to return...").ask()
                case "cycle":
                    if manager.cycle_next():
                        break
                    questionary.press_any_key_to_continue("\nPress any key to return...").ask()
                case "check":
                    target = questionary.select(
                        "Select profile to check quota:", 
                        choices=build_profile_choices(profiles),
                        style=questionary.Style([('pointer', 'fg:ansimagenta bold')])
                    ).ask()
                    
                    if target:
                        manager.check_profile(target)
                        questionary.press_any_key_to_continue("\nPress any key to return...").ask()
                case "create":
                    name = questionary.text("Enter name for new profile (leave blank to cancel):").ask()
                    
                    if name and name.strip(): 
                        manager.create(name.strip())
                        questionary.press_any_key_to_continue("\nPress any key to return...").ask()
                case "delete":
                    target = questionary.select(
                        "Select profile to delete:", 
                        choices=build_profile_choices(profiles, manager.get_active(), lock_active=True),
                        style=questionary.Style([('pointer', 'fg:ansimagenta bold')])
                    ).ask()
                    
                    if target: 
                        manager.delete(target)
                        questionary.press_any_key_to_continue("\nPress any key to return...").ask()
                case "rename":
                    target = questionary.select(
                        "Select profile to rename:", 
                        choices=build_profile_choices(profiles),
                        style=questionary.Style([('pointer', 'fg:ansimagenta bold')])
                    ).ask()
                    
                    if target:
                        new_name = questionary.text(
                            "Enter new name for profile (leave blank to cancel):",
                            validate=lambda v: v.strip() == "" or manager.is_valid_name(v.strip())
                            or "Invalid name: alphanumeric, dashes, and underscores only"
                        ).ask()
                        if new_name and new_name.strip():
                            manager.rename(target, new_name.strip())
                        questionary.press_any_key_to_continue("\nPress any key to return...").ask()
                case "reorder":
                    target = questionary.select(
                        "Select profile to move:", 
                        choices=build_profile_choices(profiles),
                        style=questionary.Style([('pointer', 'fg:ansimagenta bold')])
                    ).ask()
                    
                    if target:
                        while True:
                            move_action = questionary.select(
                                f"Move '{target}' where?",
                                choices=[
                                    questionary.Choice("▲ Move Up", value="up"),
                                    questionary.Choice("▼ Move Down", value="down"),
                                    questionary.Choice("↩ Done", value=None)
                                ],
                                style=questionary.Style([('pointer', 'fg:ansimagenta bold')])
                            ).ask()
                            if move_action is None:
                                break
                            if manager.reorder(target, move_action):
                                # Live feedback: repaint so the new position is visible
                                # before the next move decision.
                                console.clear()
                                title = Text("🚀 Antigravity Profile Manager", style="bold magenta")
                                subtitle = Text("Account Isolation & Credentials Switcher", style="italic cyan")
                                console.print(Panel(Align.center(Text.assemble(title, "\n", subtitle)), border_style="magenta"))
                                manager.render_dashboard()
                        questionary.press_any_key_to_continue("\nPress any key to return...").ask()
                case "stash":
                    active_profile = manager.get_active()
                    if active_profile:
                        manager.stash_keyring(active_profile)
                        questionary.press_any_key_to_continue("\nPress any key to return...").ask()
        except KeyboardInterrupt:
            continue

def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Profile Manager & Credentials Switcher")
    parser.add_argument("profile", nargs="?", help="Direct profile override")
    parser.add_argument("-l", "--list", action="store_true", help="List all available profiles and exit")
    parser.add_argument("-n", "--next", action="store_true", help="Cycle to the next profile and exit")
    parser.add_argument("-f", "--force", action="store_true", help="Bypass running process check and force switch non-interactively")
    parser.add_argument("-r", "--restart", action="store_true", help="Gracefully close Antigravity if running, switch, then relaunch it — starts it even if it was not running (mirrors the AntigravityManager switch flow)")
    parser.add_argument("-c", "--check", nargs="?", const="__active__", metavar="PROFILE", help="Validate a profile's token (refresh if expired) and verify it live against Google (defaults to active profile)")
    
    args = parser.parse_args()

    manager = ProfileManager(force_mode=args.force, restart_mode=args.restart)

    if args.list:
        manager.render_dashboard()
    elif args.check is not None:
        target = args.check if args.check != "__active__" else (args.profile or manager.get_active())
        if not target:
            console.print("[error]✖ No active profile to check.[/error]")
            sys.exit(1)
        if not manager.check_profile(target):
            sys.exit(1)
    elif args.next:
        if not manager.cycle_next():
            sys.exit(1)
    elif args.profile:
        if not manager.switch(args.profile):
            sys.exit(1)
    else:
        if not sys.stdin.isatty():
            console.print("[error]✖ Interactive mode requires a terminal. Use -l, -n, -f, -r, -c, or a profile name instead.[/error]")
            sys.exit(1)
        interactive_tui(manager)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[error]Process killed via SIGINT.[/error]")
        sys.exit(130)
