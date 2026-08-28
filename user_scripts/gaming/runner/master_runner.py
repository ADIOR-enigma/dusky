#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
Master Game Runner Engine (AGY Gaming Architecture)
================================================================================

Target Architecture:
  * Arch Linux (Bleeding Edge, Linux Kernel 7.2+)
  * Pure Wayland & Hyprland Session (Wayland native / XWayland bridge)
  * Dual-GPU Matrix: Intel Iris Xe (iGPU) + NVIDIA GeForce RTX 3050 Ti (dGPU)
  * Declarative TOML Profiles with Preset Inheritance (Zero Hardcoded Game IDs)
  * Dynamic DwarFS FUSE & fuse-overlayfs Mount Lifecycle with Rock-Solid Teardown
  * Automated Wine/Proton Prefix Provisioning (Redistributables & Winetricks)
  * Gamescope Micro-Compositor, Mangoapp/MangoHud, Feral GameMode, PipeWire Tuning
  * Interactive Rich TUI Dashboard, System Doctor, & Deep Auto-Scaffolder

Author: Dusk / AGY Team
Date: 2026-08-28
================================================================================
"""

import argparse
import atexit
import copy
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# Rich Console Integration
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE: Final[bool] = True
except ImportError:
    RICH_AVAILABLE: Final[bool] = False


# ==============================================================================
# 1. CONSTANTS & SYSTEM PATHS
# ==============================================================================

ENGINE_NAME: Final[str] = "Master Game Runner Engine"
ENGINE_VERSION: Final[str] = "1.4.1"
SELF_DIR: Final[Path] = Path(__file__).resolve().parent
GLOBAL_CONFIG_PATH: Final[Path] = SELF_DIR / "config.toml"
PRESETS_DIR: Final[Path] = SELF_DIR / "presets"
PROFILES_DIR: Final[Path] = SELF_DIR / "profiles"

console: Console | None = Console() if RICH_AVAILABLE else None


def log_info(msg: str) -> None:
    if console:
        console.print(f"[bold cyan]ℹ[/bold cyan] {msg}")
    else:
        print(f"[INFO] {msg}")


def log_success(msg: str) -> None:
    if console:
        console.print(f"[bold green]✔[/bold green] {msg}")
    else:
        print(f"[OK] {msg}")


def log_warning(msg: str) -> None:
    if console:
        console.print(f"[bold yellow]⚠[/bold yellow] {msg}")
    else:
        print(f"[WARN] {msg}", file=sys.stderr)


def log_error(msg: str) -> None:
    if console:
        console.print(f"[bold red]✘[/bold red] {msg}")
    else:
        print(f"[ERROR] {msg}", file=sys.stderr)


def send_notification(title: str, message: str, urgency: str = "normal", icon: str = "applications-games") -> None:
    """Dispatches a native Wayland / DBus desktop notification."""
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.run(
            ["notify-send", "-a", ENGINE_NAME, "-u", urgency, "-i", icon, title, message],
            capture_output=True,
            timeout=2
        )
    except Exception:
        pass


# ==============================================================================
# 2. HARDWARE & GPU DETECTION ENGINE
# ==============================================================================

@dataclass(frozen=True, slots=True)
class GPUInfo:
    dev_node: str
    pci_slot: str
    vendor_id: str
    vendor_name: str
    device_name: str
    boot_vga: int
    driver: str

    @property
    def is_primary(self) -> bool:
        return self.boot_vga == 1

    @property
    def is_nvidia(self) -> bool:
        return "10de" in self.vendor_id.lower() or "nvidia" in self.vendor_name.lower()

    @property
    def is_intel(self) -> bool:
        return "8086" in self.vendor_id.lower() or "intel" in self.vendor_name.lower()

    @property
    def is_amd(self) -> bool:
        return "1002" in self.vendor_id.lower() or "amd" in self.vendor_name.lower()


GPU_VENDOR_MAP: Final[dict[str, str]] = {
    "0x8086": "Intel",
    "0x1002": "AMD",
    "0x10de": "NVIDIA",
    "0x1af4": "RedHat VirtIO",
}


def detect_gpus() -> list[GPUInfo]:
    """Auto-detects all GPUs present via DRM card nodes and lspci."""
    gpus: list[GPUInfo] = []
    seen_slots: set[str] = set()

    for s in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        p = Path(s)
        if not re.fullmatch(r"card\d+", p.name):
            continue
        dev_node = f"/dev/dri/{p.name}"
        if not Path(dev_node).exists():
            continue

        try:
            sys_dev = Path(os.path.realpath(p / "device"))
        except Exception:
            continue

        vdir = None
        cur = sys_dev
        for _ in range(10):
            if (cur / "vendor").exists():
                vdir = cur
                break
            if cur == cur.parent:
                break
            cur = cur.parent

        if not vdir:
            continue

        try:
            vid = vdir.joinpath("vendor").read_text(encoding="utf-8").strip().lower()
        except Exception:
            continue

        pci_slot = vdir.name
        if pci_slot in seen_slots:
            continue
        seen_slots.add(pci_slot)

        boot_vga = 0
        for bp in [vdir / "boot_vga", sys_dev / "boot_vga"]:
            if bp.exists():
                try:
                    boot_vga = int(bp.read_text(encoding="utf-8").strip())
                    break
                except Exception:
                    pass

        driver = "unknown"
        for d in [vdir / "driver", sys_dev / "driver"]:
            if d.exists():
                try:
                    driver = Path(os.path.realpath(d)).name
                    break
                except Exception:
                    pass

        vendor_name = GPU_VENDOR_MAP.get(vid, f"Unknown ({vid})")
        device_name = f"Graphics Device [{pci_slot}]"

        if shutil.which("lspci"):
            try:
                res = subprocess.run(["lspci", "-s", pci_slot], capture_output=True, text=True, timeout=2)
                if res.stdout.strip():
                    m = re.match(r"^[0-9a-fA-F:.]+ [^:]+: (.+)$", res.stdout.strip())
                    if m:
                        device_name = m.group(1)
            except Exception:
                pass

        gpus.append(GPUInfo(
            dev_node=dev_node,
            pci_slot=pci_slot,
            vendor_id=vid,
            vendor_name=vendor_name,
            device_name=device_name,
            boot_vga=boot_vga,
            driver=driver
        ))

    # Fallback to lspci if DRM nodes not populated
    if not gpus and shutil.which("lspci"):
        try:
            res = subprocess.run(["lspci", "-mm", "-nn"], capture_output=True, text=True, timeout=2)
            for line in res.stdout.splitlines():
                if any(ctrl in line for ctrl in ['"0300"', '"0301"', '"0302"', "VGA", "3D"]):
                    parts = re.findall(r'"([^"]*)"', line)
                    slot = line.split()[0]
                    if slot in seen_slots:
                        continue
                    seen_slots.add(slot)
                    dev_name = parts[2] if len(parts) > 2 else "VGA Device"
                    vid = "0x10de" if "10de" in line else ("0x8086" if "8086" in line else "0x1002")
                    vname = GPU_VENDOR_MAP.get(vid, "Unknown")
                    gpus.append(GPUInfo(
                        dev_node=f"/dev/dri/card{len(gpus)}",
                        pci_slot=slot,
                        vendor_id=vid,
                        vendor_name=vname,
                        device_name=dev_name,
                        boot_vga=1 if len(gpus) == 0 else 0,
                        driver="unknown"
                    ))
        except Exception:
            pass

    return gpus


# ==============================================================================
# 3. CONFIGURATION PARSER & CASCADING INHERITANCE
# ==============================================================================

def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively deep merges override dict into base dict."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = copy.deepcopy(value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def expand_variables(val: Any, context: dict[str, str]) -> Any:
    """Recursively expands shell and context variables ($HOME, ~, $GAME_DIR, etc.)."""
    if isinstance(val, str):
        expanded = os.path.expanduser(os.path.expandvars(val))
        for k, v in context.items():
            expanded = expanded.replace(f"${k}", v).replace(f"${{{k}}}", v)
        return expanded
    elif isinstance(val, dict):
        return {k: expand_variables(v, context) for k, v in val.items()}
    elif isinstance(val, list):
        return [expand_variables(item, context) for item in val]
    return val


class ProfileManager:
    """Discovers, loads, and merges game profiles and presets."""

    def __init__(self, profiles_dir: Path = PROFILES_DIR, presets_dir: Path = PRESETS_DIR, global_config_path: Path = GLOBAL_CONFIG_PATH):
        self.profiles_dir = profiles_dir
        self.presets_dir = presets_dir
        self.global_config_path = global_config_path
        self.global_config = self._load_toml(global_config_path)

    def _load_toml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            log_error(f"Failed to parse TOML at {path}: {e}")
            return {}

    def discover_profiles(self) -> dict[str, Path]:
        """Discovers all .toml game profiles in profiles/ excluding templates."""
        profiles = {}
        if not self.profiles_dir.exists():
            return profiles
        for p in sorted(self.profiles_dir.glob("*.toml")):
            if p.name.startswith("_"):
                continue
            profile_id = p.stem
            profiles[profile_id] = p
        return profiles

    def discover_presets(self) -> dict[str, Path]:
        """Discovers all .toml base presets in presets/."""
        presets = {}
        if not self.presets_dir.exists():
            return presets
        for p in sorted(self.presets_dir.glob("*.toml")):
            preset_id = p.stem
            presets[preset_id] = p
        return presets

    def load_preset_chain(self, preset_name: str, visited: set[str] | None = None) -> dict[str, Any]:
        """Recursively loads preset inheritance hierarchy."""
        if visited is None:
            visited = set()
        if preset_name in visited:
            log_warning(f"Circular preset inheritance detected: {preset_name}")
            return {}
        visited.add(preset_name)

        preset_file = self.presets_dir / f"{preset_name}.toml"
        if not preset_file.exists():
            log_warning(f"Referenced preset '{preset_name}' not found at {preset_file}")
            return {}

        data = self._load_toml(preset_file)
        parent_name = data.get("extends")
        if parent_name:
            parent_data = self.load_preset_chain(parent_name, visited)
            return deep_merge(parent_data, data)
        return data

    def load_profile(self, profile_id_or_path: str, cli_overrides: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        """
        Loads a game profile by ID or path, resolves inheritance, and applies CLI overrides.
        Precedence: Global Defaults -> Presets -> Profile -> CLI Overrides
        """
        candidate_path = Path(profile_id_or_path).resolve()
        if candidate_path.is_file():
            profile_path = candidate_path
            profile_id = candidate_path.stem
        else:
            profile_id = profile_id_or_path
            profile_path = self.profiles_dir / f"{profile_id}.toml"
            if not profile_path.exists():
                raise FileNotFoundError(f"Profile '{profile_id}' not found in {self.profiles_dir}")

        raw_profile = self._load_toml(profile_path)

        # 1. Base Global Defaults
        resolved_config = copy.deepcopy(self.global_config)

        # 2. Inherited Presets
        extends_preset = raw_profile.get("extends")
        if extends_preset:
            preset_data = self.load_preset_chain(extends_preset)
            resolved_config = deep_merge(resolved_config, preset_data)

        # 3. Game Profile
        resolved_config = deep_merge(resolved_config, raw_profile)

        # Ensure metadata id
        if "meta" not in resolved_config:
            resolved_config["meta"] = {}
        if "id" not in resolved_config["meta"] or not resolved_config["meta"]["id"]:
            resolved_config["meta"]["id"] = profile_id

        # 4. Apply CLI Overrides
        if cli_overrides:
            resolved_config = deep_merge(resolved_config, cli_overrides)

        # 5. Variable Expansion
        game_dir = resolved_config.get("paths", {}).get("game_dir", "")
        game_dir_expanded = os.path.expanduser(os.path.expandvars(game_dir))
        
        context = {
            "GAME_DIR": game_dir_expanded,
            "ZRAM": "/mnt/zram1",
            "HOME": str(Path.home()),
        }
        resolved_config = expand_variables(resolved_config, context)

        return profile_id, resolved_config


# ==============================================================================
# 4. FUSE / DWARFS & OVERLAYFS MOUNT MANAGER
# ==============================================================================

class MountManager:
    """Manages the lifecycle of DwarFS FUSE and fuse-overlayfs mounts."""

    @staticmethod
    def is_mounted(path: Path) -> bool:
        """Checks if a directory is an active and healthy mountpoint."""
        if not path.exists():
            return False
        try:
            res = subprocess.run(["mountpoint", "-q", str(path)], capture_output=True, timeout=2)
            if res.returncode != 0:
                return False
            # Health check: probe directory access (catches disconnected FUSE transport endpoints)
            os.listdir(str(path))
            return True
        except OSError:
            # Dead/stale FUSE endpoint -> unmount stale mount
            subprocess.run(["fusermount3", "-u", "-z", str(path)], capture_output=True)
            return False
        except Exception:
            return False

    @staticmethod
    def get_profile_paths(config: dict[str, Any]) -> dict[str, Path]:
        """Resolves absolute paths for DwarFS and OverlayFS layers."""
        paths_cfg = config.get("paths", {})
        game_dir = Path(paths_cfg.get("game_dir", "")).resolve()

        def resolve_sub(rel_or_abs: str) -> Path:
            p = Path(rel_or_abs)
            return p if p.is_absolute() else (game_dir / p).resolve()

        dwarfs_img_str = paths_cfg.get("dwarfs_image", "")
        dwarfs_img = resolve_sub(dwarfs_img_str) if dwarfs_img_str else None

        dwarfs_mnt_str = paths_cfg.get("dwarfs_mount", "files/.game-root-mnt")
        dwarfs_mnt = resolve_sub(dwarfs_mnt_str)

        overlay_dir_str = paths_cfg.get("overlay_dir", "files/game-root")
        overlay_dir = resolve_sub(overlay_dir_str)

        overlay_storage_str = paths_cfg.get("overlay_storage", "files/overlay-storage")
        overlay_storage = resolve_sub(overlay_storage_str)

        overlay_work_str = paths_cfg.get("overlay_work", "files/.game-root-work")
        overlay_work = resolve_sub(overlay_work_str)

        return {
            "game_dir": game_dir,
            "dwarfs_image": dwarfs_img,
            "dwarfs_mount": dwarfs_mnt,
            "overlay_dir": overlay_dir,
            "overlay_storage": overlay_storage,
            "overlay_work": overlay_work,
        }

    @classmethod
    def get_mount_status(cls, config: dict[str, Any]) -> tuple[bool, bool]:
        """Returns (dwarfs_mounted, overlay_mounted)."""
        paths = cls.get_profile_paths(config)
        dwarfs_mounted = cls.is_mounted(paths["dwarfs_mount"])
        overlay_mounted = cls.is_mounted(paths["overlay_dir"])
        return dwarfs_mounted, overlay_mounted

    @classmethod
    def mount(cls, config: dict[str, Any], dry_run: bool = False) -> bool:
        """Mounts DwarFS FUSE image and fuse-overlayfs union layer."""
        paths = cls.get_profile_paths(config)
        dwarfs_img = paths["dwarfs_image"]
        dwarfs_mnt = paths["dwarfs_mount"]
        overlay_dir = paths["overlay_dir"]
        overlay_storage = paths["overlay_storage"]
        overlay_work = paths["overlay_work"]
        game_dir = paths["game_dir"]

        if not dwarfs_img or not dwarfs_img.exists():
            return True

        dwarfs_mounted = cls.is_mounted(dwarfs_mnt)
        overlay_mounted = cls.is_mounted(overlay_dir)

        if dwarfs_mounted and overlay_mounted:
            log_info(f"Game already mounted at {overlay_dir}")
            return True

        # Clean stale mounts if partially mounted or broken
        cls.unmount(config, dry_run=dry_run, silent=True)

        # Calculate DwarFS cache size (default 25% of RAM)
        storage_cfg = config.get("storage", {})
        cache_percent = storage_cfg.get("dwarfs_cache_percent", 25)
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
            m = re.search(r"MemTotal:\s+(\d+)\s+kB", meminfo)
            total_kb = int(m.group(1)) if m else 16777216
        except Exception:
            total_kb = 16777216
        cache_kb = (total_kb * cache_percent) // 100

        tidy_interval = storage_cfg.get("dwarfs_tidy_interval", "15m")
        tidy_max_age = storage_cfg.get("dwarfs_tidy_max_age", "30m")

        if dry_run:
            log_info(f"[DRY RUN] Would create directories: {dwarfs_mnt}, {overlay_storage}, {overlay_work}, {overlay_dir}")
            log_info(f"[DRY RUN] Would mount DwarFS: dwarfs {dwarfs_img} -> {dwarfs_mnt} (cache: {cache_kb}k)")
            log_info(f"[DRY RUN] Would mount fuse-overlayfs: {overlay_dir}")
            return True

        # Ensure workdir is clean (fuse-overlayfs requirement)
        if overlay_work.exists():
            shutil.rmtree(overlay_work, ignore_errors=True)

        for d in [dwarfs_mnt, overlay_storage, overlay_work, overlay_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Locate DwarFS binary: bundled repack binary vs system dwarfs
        dwarfs_bin = game_dir / "files" / "dwarfs-binary"
        if dwarfs_bin.exists():
            try:
                dwarfs_bin.chmod(0o755)
            except Exception:
                pass

        if dwarfs_bin.exists() and os.access(dwarfs_bin, os.X_OK):
            dwarfs_cmd = [
                str(dwarfs_bin), "--tool=dwarfs", str(dwarfs_img), str(dwarfs_mnt),
                "-o", "tidy_strategy=time",
                "-o", f"tidy_interval={tidy_interval}",
                "-o", f"tidy_max_age={tidy_max_age}",
                "-o", f"cachesize={cache_kb}k",
                "-o", "clone_fd",
                "-o", "cache_image"
            ]
        elif shutil.which("dwarfs"):
            dwarfs_cmd = [
                "dwarfs", str(dwarfs_img), str(dwarfs_mnt),
                "-o", "tidy_strategy=time",
                "-o", f"tidy_interval={tidy_interval}",
                "-o", f"tidy_max_age={tidy_max_age}",
                "-o", f"cachesize={cache_kb}k",
                "-o", "clone_fd",
                "-o", "cache_image"
            ]
        else:
            log_error("DwarFS executable not found (neither bundled repack nor system 'dwarfs').")
            return False

        log_info(f"Mounting DwarFS image: {dwarfs_img.name} -> {dwarfs_mnt} (Cache: {cache_kb // 1024}MB)")
        res = subprocess.run(dwarfs_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            log_error(f"DwarFS mount failed: {res.stderr.strip() or res.stdout.strip()}")
            return False

        # Mount fuse-overlayfs
        uid = os.getuid()
        gid = os.getgid()
        overlay_cmd = [
            "fuse-overlayfs",
            "-o", f"squash_to_uid={uid}",
            "-o", f"squash_to_gid={gid}",
            "-o", f"lowerdir={dwarfs_mnt},upperdir={overlay_storage},workdir={overlay_work}",
            str(overlay_dir)
        ]

        log_info(f"Mounting fuse-overlayfs: {overlay_dir}")
        res = subprocess.run(overlay_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            log_error(f"fuse-overlayfs mount failed: {res.stderr.strip() or res.stdout.strip()}")
            subprocess.run(["fusermount3", "-u", "-z", str(dwarfs_mnt)], capture_output=True)
            return False

        log_success(f"Game mounted successfully at {overlay_dir}")
        return True

    @classmethod
    def unmount(cls, config: dict[str, Any], dry_run: bool = False, silent: bool = False) -> bool:
        """Safely unmounts fuse-overlayfs and DwarFS FUSE."""
        paths = cls.get_profile_paths(config)
        dwarfs_mnt = paths["dwarfs_mount"]
        overlay_dir = paths["overlay_dir"]
        overlay_work = paths["overlay_work"]

        if dry_run:
            if not silent:
                log_info(f"[DRY RUN] Would unmount: {overlay_dir} and {dwarfs_mnt}")
            return True

        success = True
        try:
            subprocess.run(["fuser", "-k", str(dwarfs_mnt)], capture_output=True, timeout=2)
        except Exception:
            pass

        for mount_pt in [overlay_dir, dwarfs_mnt]:
            res = subprocess.run(["fusermount3", "-u", "-z", str(mount_pt)], capture_output=True)
            if res.returncode != 0 and cls.is_mounted(mount_pt):
                res2 = subprocess.run(["sudo", "-n", "umount", "-l", str(mount_pt)], capture_output=True)
                if res2.returncode != 0:
                    if not silent:
                        log_warning(f"Could not cleanly unmount {mount_pt}")
                    success = False

        if config.get("storage", {}).get("auto_clean_workdir", True) and overlay_work.exists():
            shutil.rmtree(overlay_work, ignore_errors=True)

        if not silent:
            log_success(f"Unmounted {config.get('meta', {}).get('name', 'game')}")
        return success

    @classmethod
    def unmount_all(cls, manager: ProfileManager, dry_run: bool = False) -> int:
        """Unmounts all active game profiles found."""
        profiles = manager.discover_profiles()
        unmounted_count = 0
        for pid, path in profiles.items():
            try:
                _, cfg = manager.load_profile(pid)
                dw_mounted, ov_mounted = cls.get_mount_status(cfg)
                if dw_mounted or ov_mounted:
                    log_info(f"Found active mount for profile: {pid}")
                    cls.unmount(cfg, dry_run=dry_run)
                    unmounted_count += 1
            except Exception as e:
                log_error(f"Error checking profile {pid}: {e}")
        return unmounted_count


# ==============================================================================
# 5. GLOBAL LIFECYCLE & SIGNAL SAFETY
# ==============================================================================

class ActiveRunContext:
    """Tracks running processes and mounts for guaranteed clean teardown."""
    active_profile_config: dict[str, Any] | None = None
    game_process: subprocess.Popen | None = None
    wineserver_managed: bool = False
    wine_prefix: Path | None = None
    cleaned: bool = False


def cleanup_active_context() -> None:
    """Emergency and normal teardown cleanup handler."""
    if ActiveRunContext.cleaned:
        return
    ActiveRunContext.cleaned = True

    if ActiveRunContext.game_process and ActiveRunContext.game_process.poll() is None:
        log_warning("Terminating game process...")
        try:
            ActiveRunContext.game_process.terminate()
            ActiveRunContext.game_process.wait(timeout=3)
        except Exception:
            try:
                ActiveRunContext.game_process.kill()
            except Exception:
                pass

    if ActiveRunContext.wineserver_managed:
        log_info("Stopping wineserver...")
        env = os.environ.copy()
        if ActiveRunContext.wine_prefix:
            env["WINEPREFIX"] = str(ActiveRunContext.wine_prefix)
        try:
            subprocess.run(["wineserver", "-k"], env=env, capture_output=True, timeout=3)
        except Exception:
            pass

    if ActiveRunContext.active_profile_config:
        cfg = ActiveRunContext.active_profile_config
        if cfg.get("runner", {}).get("auto_unmount_on_exit", True):
            MountManager.unmount(cfg, silent=True)


def signal_handler(signum: int, frame: Any) -> None:
    sig_name = signal.Signals(signum).name
    log_warning(f"Received signal {sig_name}. Initiating safe teardown...")
    cleanup_active_context()
    sys.exit(128 + signum)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGHUP, signal_handler)
atexit.register(cleanup_active_context)


# ==============================================================================
# 6. RUNNER EXECUTION PIPELINE
# ==============================================================================

class GameRunner:
    """Orchestrates environment, GPU offload, Gamescope, Wine, and process execution."""

    def __init__(
        self,
        profile_id: str,
        config: dict[str, Any],
        extra_args: list[str] | None = None,
        dry_run: bool = False,
        verbose: bool = False,
        reprovision: bool = False
    ):
        self.profile_id = profile_id
        self.config = config
        self.extra_args = extra_args or []
        self.dry_run = dry_run
        self.verbose = verbose
        self.reprovision = reprovision
        self.paths = MountManager.get_profile_paths(config)
        self.gpus = detect_gpus()

    def run_hooks(self, hook_name: str) -> None:
        """Executes pre/post shell hook commands."""
        hooks = self.config.get("hooks", {}).get(hook_name, [])
        if not hooks:
            return
        log_info(f"Executing {hook_name} hooks ({len(hooks)} commands)...")
        for cmd in hooks:
            if self.dry_run:
                log_info(f"[DRY RUN] Hook command: {cmd}")
                continue
            try:
                res = subprocess.run(cmd, shell=True, cwd=str(self.paths["game_dir"]), capture_output=not self.verbose)
                if res.returncode != 0:
                    log_warning(f"Hook '{cmd}' exited with code {res.returncode}")
            except Exception as e:
                log_error(f"Error executing hook '{cmd}': {e}")

    def prepare_wine_prefix(self) -> Path:
        """Initializes and provisions the Wine prefix with runtime dependencies."""
        wine_cfg = self.config.get("runtime", {}).get("wine", {})
        prefix_str = wine_cfg.get("prefix_dir", "files/wine-prefix")
        p = Path(prefix_str)
        prefix_path = p if p.is_absolute() else (self.paths["game_dir"] / p).resolve()

        stamp_file = prefix_path / ".runner_provisioned"
        prefix_initialized = prefix_path.exists() and (prefix_path / "system.reg").exists()

        if self.reprovision and stamp_file.exists():
            stamp_file.unlink(missing_ok=True)

        if prefix_initialized and stamp_file.exists():
            return prefix_path

        log_info(f"Preparing and provisioning Wine prefix at {prefix_path}...")
        if self.dry_run:
            log_info(f"[DRY RUN] Would initialize and provision Wine prefix at {prefix_path}")
            return prefix_path

        prefix_path.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["WINEPREFIX"] = str(prefix_path)
        env["WINEARCH"] = wine_cfg.get("arch", "win64")
        env["WINEDEBUG"] = "-all"

        wine_bin = wine_cfg.get("wine_binary", "wine")
        wineboot_bin = "wineboot"
        winecfg_bin = "winecfg"
        wineserver_bin = "wineserver"

        # 1. Base Prefix Creation
        if not prefix_initialized:
            subprocess.run([wineboot_bin, "-i"], env=env, capture_output=True)
            subprocess.run([winecfg_bin, "-v", "win11"], env=env, capture_output=True)
            subprocess.run([wineserver_bin, "-w"], env=env, capture_output=True)

        # 2. Automated Prefix Provisioning Pipeline (Redistributables & Winetricks)
        if not stamp_file.exists():
            root_dir = self.paths["overlay_dir"] if self.paths["dwarfs_image"] and self.paths["dwarfs_image"].exists() else self.paths["game_dir"]

            redist_candidates: list[Path] = []
            declared_redists = wine_cfg.get("redistributables", [])

            for dr in declared_redists:
                p_cand = (root_dir / dr).resolve()
                if p_cand.exists() and p_cand not in redist_candidates:
                    redist_candidates.append(p_cand)

            # Deep recursive scan for common installers if none declared or to supplement
            if root_dir.exists():
                common_patterns = [
                    "**/VC_redist*.exe", "**/vcredist*.exe",
                    "**/DXSETUP.exe", "**/dxsetup.exe",
                    "**/oalinst.exe", "**/OpenAL*.exe",
                    "**/windowsdesktop-runtime*.exe"
                ]
                for pat in common_patterns:
                    for found in root_dir.glob(pat):
                        if found.is_file() and found not in redist_candidates:
                            redist_candidates.append(found)

            # Execute redistributable installers silently
            for installer in redist_candidates:
                log_info(f"Provisioning runtime dependency: {installer.name}...")
                quiet_flag = "/q" if any(k in installer.name for k in ["VC_redist", "vcredist", "windowsdesktop"]) else "/silent"
                subprocess.run([wine_bin, str(installer), quiet_flag], env=env, capture_output=True)
                subprocess.run([wineserver_bin, "-w"], env=env, capture_output=True)

            # Apply Winetricks Verbs
            winetricks_verbs = wine_cfg.get("winetricks", [])
            if winetricks_verbs:
                bundled_winetricks = root_dir / "winetricks.sh"
                for verb in winetricks_verbs:
                    log_info(f"Applying winetricks verb: {verb}...")
                    if bundled_winetricks.exists() and os.access(bundled_winetricks, os.R_OK):
                        subprocess.run(["bash", str(bundled_winetricks), "-q", verb], env=env, cwd=str(root_dir), capture_output=True)
                    elif shutil.which("winetricks"):
                        subprocess.run(["winetricks", "-q", verb], env=env, capture_output=True)
                    subprocess.run([wineserver_bin, "-w"], env=env, capture_output=True)

            stamp_file.write_text(f"provisioned_at={time.time()}\nengine_version={ENGINE_VERSION}\n", encoding="utf-8")
            subprocess.run([wineserver_bin, "-w"], env=env, capture_output=True)
            log_success("Wine prefix provisioning completed successfully.")

        return prefix_path

    def build_environment(self) -> dict[str, str]:
        """Constructs pure Wayland environment variables for the execution pipeline."""
        env = os.environ.copy()

        # 1. Profile custom environment variables
        custom_env = self.config.get("env", {})
        for k, v in custom_env.items():
            if k == "LD_PRELOAD":
                expanded_preload = Path(os.path.expanduser(os.path.expandvars(str(v)))).resolve()
                if expanded_preload.exists():
                    log_info(f"Injecting Wayland runtime shim: {expanded_preload.name}")
                    if "LD_PRELOAD" in env and env["LD_PRELOAD"]:
                        env["LD_PRELOAD"] = f"{expanded_preload}:{env['LD_PRELOAD']}"
                    else:
                        env["LD_PRELOAD"] = str(expanded_preload)
                else:
                    log_warning(f"LD_PRELOAD shim not found at {expanded_preload}; skipping injection.")
            else:
                env[k] = str(v)

        # 2. Audio & PipeWire Tuning
        audio_cfg = self.config.get("audio", {})
        if audio_cfg.get("driver") == "pipewire":
            latency = audio_cfg.get("pipewire_latency", "128/48000")
            env["PIPEWIRE_LATENCY"] = latency

        # 3. Pure Wayland & Graphics Presentation
        gfx_cfg = self.config.get("graphics", {})
        if gfx_cfg.get("prefer_xwayland", False):
            env["SDL_VIDEODRIVER"] = "x11"
            env["GDK_BACKEND"] = "x11"
            env["QT_QPA_PLATFORM"] = "xcb"
            if not env.get("DISPLAY"):
                x_sockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
                if x_sockets:
                    sock_num = Path(x_sockets[0]).name.replace("X", ":")
                    env["DISPLAY"] = sock_num
                else:
                    env["DISPLAY"] = ":0"
        else:
            env["SDL_VIDEODRIVER"] = "wayland"
            env["CLUTTER_BACKEND"] = "wayland"
            env["GDK_BACKEND"] = "wayland"
            env["QT_QPA_PLATFORM"] = "wayland"
            env["PROTON_ENABLE_WAYLAND"] = "1"
            env["WAYLAND_DISPLAY"] = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
            env["XDG_SESSION_TYPE"] = "wayland"

        # 4. GPU Offload Configuration
        gpu_choice = gfx_cfg.get("gpu", "auto")
        has_nvidia = any(g.is_nvidia for g in self.gpus)
        has_intel = any(g.is_intel for g in self.gpus)
        runtime_type = self.config.get("runtime", {}).get("type", "native")

        is_heavy_profile = (
            runtime_type == "wine" or
            "unreal" in str(self.config.get("extends", "")).lower()
        )

        use_discrete = (gpu_choice == "discrete") or (gpu_choice == "auto" and is_heavy_profile and has_nvidia)

        if use_discrete and has_nvidia:
            log_info("GPU Pipeline: NVIDIA Discrete GPU (Render Offload via PRIME)")
            env["__NV_PRIME_RENDER_OFFLOAD"] = "1"
            env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
            env["__VK_LAYER_NV_optimus"] = "NVIDIA_only"
            if gfx_cfg.get("vulkan_icd") == "nvidia" or gfx_cfg.get("vulkan_icd") == "auto":
                if Path("/usr/share/vulkan/icd.d/nvidia_icd.json").exists():
                    env["VK_ICD_FILENAMES"] = "/usr/share/vulkan/icd.d/nvidia_icd.json"
        elif gpu_choice == "integrated" or (not use_discrete and has_intel):
            log_info("GPU Pipeline: Intel Iris Xe Integrated GPU")
            env["__NV_PRIME_RENDER_OFFLOAD"] = "0"
            env["DRI_PRIME"] = "0"
            env["MESA_LOADER_DRIVER_OVERRIDE"] = "iris"
            if Path("/usr/share/vulkan/icd.d/intel_icd.x86_64.json").exists():
                env["VK_ICD_FILENAMES"] = "/usr/share/vulkan/icd.d/intel_icd.x86_64.json"

        # 5. Wine / Proton Specific Environment
        if runtime_type == "wine":
            wine_cfg = self.config.get("runtime", {}).get("wine", {})
            prefix_path = self.prepare_wine_prefix()
            ActiveRunContext.wine_prefix = prefix_path
            ActiveRunContext.wineserver_managed = True

            env["WINEPREFIX"] = str(prefix_path)
            env["WINEARCH"] = wine_cfg.get("arch", "win64")
            env["WINEDEBUG"] = wine_cfg.get("debug", "fixme-all")

            if wine_cfg.get("large_address_aware", True):
                env["WINE_LARGE_ADDRESS_AWARE"] = "1"

            match wine_cfg.get("sync_mode", "fsync"):
                case "fsync":
                    env["WINEFSYNC"] = "1"
                    env["WINEESYNC"] = "0"
                case "esync":
                    env["WINEESYNC"] = "1"
                    env["WINEFSYNC"] = "0"
                case "ntsync":
                    env["WINENTSYNC"] = "1"
                    env["WINEFSYNC"] = "0"
                    env["WINEESYNC"] = "0"
                case "server":
                    env["WINEFSYNC"] = "0"
                    env["WINEESYNC"] = "0"

            dll_map = wine_cfg.get("dll_overrides", {})
            if isinstance(dll_map, dict) and dll_map:
                override_str = ";".join(f"{k}={v}" for k, v in dll_map.items())
                env["WINEDLLOVERRIDES"] = override_str

            if wine_cfg.get("dxvk_nvapi", False):
                env["DXVK_ENABLE_NVAPI"] = "1"

        # 6. MangoHud Environment
        perf_cfg = self.config.get("performance", {})
        fps_limit = perf_cfg.get("fps_limit", 0)

        if perf_cfg.get("mangohud", False):
            env["MANGOHUD"] = "1"
            preset = perf_cfg.get("mangohud_preset", "")
            cfg_parts = []
            if preset:
                cfg_parts.append(f"preset={preset}")
            if fps_limit > 0:
                cfg_parts.append(f"fps_limit={fps_limit}")
            if cfg_parts:
                env["MANGOHUD_CONFIG"] = ",".join(cfg_parts)
        elif fps_limit > 0 and not self.config.get("graphics", {}).get("gamescope", {}).get("enabled", False):
            env["DXVK_FRAME_RATE"] = str(fps_limit)

        return env

    def build_command_pipeline(self) -> tuple[list[str], Path]:
        """Assembles the complete nested command pipeline and working directory."""
        paths_cfg = self.config.get("paths", {})
        runtime_cfg = self.config.get("runtime", {})
        runtime_type = runtime_cfg.get("type", "native")

        if self.paths["dwarfs_image"] and self.paths["dwarfs_image"].exists():
            root_dir = self.paths["overlay_dir"]
        else:
            root_dir = self.paths["game_dir"]

        exec_rel = paths_cfg.get("executable", "")
        if not exec_rel:
            raise ValueError(f"No executable defined for profile '{self.profile_id}'")

        exec_path = (root_dir / exec_rel).resolve()

        if not exec_path.exists():
            # Search for matching executable name in subdirectories
            exec_name = Path(exec_rel).name
            for cand in root_dir.glob(f"**/{exec_name}"):
                if cand.is_file():
                    log_warning(f"Executable {exec_rel} not found at root; found at {cand.relative_to(root_dir)}")
                    exec_path = cand
                    break

        # Auto-ensure execution permission for Linux ELF binaries and scripts
        if runtime_type == "native" and exec_path.exists():
            try:
                exec_path.chmod(exec_path.stat().st_mode | 0o755)
            except Exception:
                pass

        working_dir_cfg = paths_cfg.get("working_dir", "")
        if working_dir_cfg:
            working_dir = (root_dir / working_dir_cfg).resolve()
        else:
            working_dir = exec_path.parent

        cmd_args = list(paths_cfg.get("arguments", []))
        cmd_args.extend(self.extra_args)

        if runtime_type == "wine":
            wine_bin = runtime_cfg.get("wine", {}).get("wine_binary", "wine")
            base_cmd = [wine_bin, str(exec_path)] + cmd_args
        else:
            base_cmd = [str(exec_path)] + cmd_args

        pipeline = base_cmd

        # Wrap: Gamescope (Wayland native backend)
        gamescope_cfg = self.config.get("graphics", {}).get("gamescope", {})
        perf_cfg = self.config.get("performance", {})
        using_gamescope = gamescope_cfg.get("enabled", False) and shutil.which("gamescope")

        if using_gamescope:
            log_info("Pipeline Layer: Gamescope Wayland Micro-Compositor")
            gs_cmd = ["gamescope", "--backend", "wayland", "--expose-wayland"]
            w = gamescope_cfg.get("width", 1920)
            h = gamescope_cfg.get("height", 1080)
            W = gamescope_cfg.get("output_width", 1920)
            H = gamescope_cfg.get("output_height", 1080)
            r = gamescope_cfg.get("refresh_rate", 144)

            gs_cmd.extend(["-w", str(w), "-h", str(h), "-W", str(W), "-H", str(H), "-r", str(r)])

            match gamescope_cfg.get("mode", "embedded"):
                case "embedded" | "borderless":
                    gs_cmd.append("-b")
                case "fullscreen":
                    gs_cmd.append("-f")
                case "nested":
                    pass

            if gamescope_cfg.get("fsr_upscaling", False):
                gs_cmd.extend(["-F", "fsr", "--fsr-sharpness", str(gamescope_cfg.get("fsr_sharpness", 2))])

            if gamescope_cfg.get("allow_tearing", True):
                gs_cmd.append("--immediate-flips")

            if gamescope_cfg.get("force_grab_cursor", False):
                gs_cmd.append("--force-grab-cursor")

            if gamescope_cfg.get("hdr", False):
                gs_cmd.append("--hdr-enabled")

            # Gamescope Mangoapp integration
            if perf_cfg.get("mangohud", False) and shutil.which("mangoapp"):
                gs_cmd.append("--mangoapp")

            extra_gs = gamescope_cfg.get("extra_args", [])
            gs_cmd.extend(extra_gs)
            gs_cmd.append("--")
            pipeline = gs_cmd + pipeline
        elif perf_cfg.get("mangohud", False) and shutil.which("mangohud"):
            # Standalone MangoHud wrapper (when not running inside Gamescope)
            pipeline = ["mangohud"] + pipeline

        # Wrap: GameMode
        if perf_cfg.get("gamemode", True) and shutil.which("gamemoderun"):
            log_info("Pipeline Layer: Feral GameMode (gamemoderun)")
            pipeline = ["gamemoderun"] + pipeline

        # Wrap: Prime-Run (if discrete requested and prime-run binary available)
        gfx_cfg = self.config.get("graphics", {})
        if gfx_cfg.get("gpu") == "discrete" and shutil.which("prime-run"):
            pipeline = ["prime-run"] + pipeline

        # Wrap: Bubblewrap Sandbox
        sandbox_cfg = self.config.get("sandbox", {})
        if sandbox_cfg.get("enabled", False) and shutil.which("bwrap"):
            log_info("Pipeline Layer: Bubblewrap Sandbox")
            bwrap_cmd = [
                "bwrap",
                "--ro-bind", "/usr", "/usr",
                "--ro-bind-try", "/lib", "/lib",
                "--ro-bind-try", "/lib64", "/lib64",
                "--ro-bind", "/bin", "/bin",
                "--ro-bind", "/etc", "/etc",
                "--proc", "/proc",
                "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--bind", str(self.paths["game_dir"]), str(self.paths["game_dir"])
            ]
            if sandbox_cfg.get("bind_gpu", True):
                bwrap_cmd.extend(["--dev-bind-try", "/dev/dri", "/dev/dri"])
                for nv in glob.glob("/dev/nvidia*"):
                    bwrap_cmd.extend(["--dev-bind-try", nv, nv])
            if sandbox_cfg.get("bind_sound", True):
                uid = os.getuid()
                for sock in [f"/run/user/{uid}/pipewire-0", f"/run/user/{uid}/pulse"]:
                    if Path(sock).exists():
                        bwrap_cmd.extend(["--ro-bind-try", sock, sock])
            if sandbox_cfg.get("bind_wayland", True):
                w_disp = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
                w_sock = Path(f"/run/user/{os.getuid()}/{w_disp}")
                if w_sock.exists():
                    bwrap_cmd.extend(["--ro-bind-try", str(w_sock), str(w_sock)])
            if not sandbox_cfg.get("bind_network", False):
                bwrap_cmd.append("--unshare-net")

            if sandbox_cfg.get("isolate_home", True):
                sb_home = Path(sandbox_cfg.get("sandbox_home", f"~/.local/share/game_sandboxes/{self.profile_id}")).expanduser()
                sb_home.mkdir(parents=True, exist_ok=True)
                bwrap_cmd.extend(["--bind", str(sb_home), str(Path.home())])

            pipeline = bwrap_cmd + ["--chdir", str(working_dir), "--"] + pipeline

        return pipeline, working_dir

    def execute(self) -> int:
        """Runs the game through its entire lifecycle."""
        meta = self.config.get("meta", {})
        game_name = meta.get("name", self.profile_id)

        log_info(f"Preparing to launch: [bold green]{game_name}[/bold green] ({self.profile_id})")

        # 1. Pre-Mount Hook
        self.run_hooks("pre_mount")

        # 2. Mount DwarFS + OverlayFS
        if not MountManager.mount(self.config, dry_run=self.dry_run):
            log_error(f"Mount failed. Aborting execution for {self.profile_id}.")
            return 1

        ActiveRunContext.active_profile_config = self.config

        # 3. Post-Mount Hook
        self.run_hooks("post_mount")

        # 4. Pre-Launch Hook (Executed before build_environment so any just-in-time assets are built)
        self.run_hooks("pre_launch")

        # 5. Build Environment & Command Pipeline
        env = self.build_environment()
        pipeline, working_dir = self.build_command_pipeline()

        # Display Pipeline Details
        if console:
            table = Table(title=f"Game Launch Specification: {game_name}", box=box.ROUNDED)
            table.add_column("Parameter", style="cyan", no_wrap=True)
            table.add_column("Resolved Value", style="magenta")
            table.add_row("Profile ID", self.profile_id)
            table.add_row("Runtime Type", self.config.get("runtime", {}).get("type", "native"))
            table.add_row("Working Dir", str(working_dir))
            table.add_row("GPU Target", self.config.get("graphics", {}).get("gpu", "auto"))
            table.add_row("Gamescope", str(self.config.get("graphics", {}).get("gamescope", {}).get("enabled", False)))
            table.add_row("FPS Limit", str(self.config.get("performance", {}).get("fps_limit", 0)))
            table.add_row("Command Pipeline", " ".join(pipeline))
            console.print(table)
        else:
            print(f"Executing: {' '.join(pipeline)}")

        if self.dry_run:
            log_info("[DRY RUN] Simulation complete. Exiting without launching binary.")
            MountManager.unmount(self.config, dry_run=True)
            return 0

        # 6. Spawn Game Process
        start_time = time.time()
        send_notification("Game Starting", f"Launching {game_name}...", icon=meta.get("icon", "applications-games"))

        try:
            proc = subprocess.Popen(pipeline, cwd=str(working_dir), env=env)
            ActiveRunContext.game_process = proc
            returncode = proc.wait()
        except KeyboardInterrupt:
            log_warning("Execution interrupted by user.")
            returncode = 130
        except Exception as e:
            log_error(f"Failed to execute game binary: {e}")
            returncode = 1

        elapsed = int(time.time() - start_time)
        mins, secs = divmod(elapsed, 60)
        duration_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

        log_info(f"Game process exited with code {returncode} (Session Duration: {duration_str})")
        send_notification("Game Exited", f"{game_name} session ended ({duration_str}).", icon=meta.get("icon", "applications-games"))

        # 7. Post-Launch Hook
        self.run_hooks("post_launch")

        # 8. Teardown & Unmount
        cleanup_active_context()

        # 9. Post-Unmount Hook
        self.run_hooks("post_unmount")

        return returncode


# ==============================================================================
# 7. AUTO-SCAFFOLDER / PROFILE GENERATOR (`master_runner.py init`)
# ==============================================================================

class ProfileScaffolder:
    """Intelligently inspects a directory and generates a ready-to-run TOML profile."""

    @staticmethod
    def scaffold(
        target_dir: Path,
        name: str | None = None,
        profile_id: str | None = None,
        preset: str | None = None,
        output_path: Path | None = None,
        overwrite: bool = False,
        install_desktop: bool = False
    ) -> Path:
        target_dir = target_dir.resolve()
        if not target_dir.is_dir():
            raise NotADirectoryError(f"Target directory {target_dir} does not exist.")

        folder_name = target_dir.name
        if not profile_id:
            clean_id = re.sub(r"[-. ]+", "_", folder_name.lower())
            clean_id = re.sub(r"_jc141$", "", clean_id)
            profile_id = clean_id

        if not name:
            clean_name = folder_name.replace("-jc141", "").replace(".", " ").replace("_", " ").title()
            name = clean_name

        # Detect DwarFS image
        dwarfs_img_rel = ""
        for dw in target_dir.glob("**/*.dwarfs"):
            dwarfs_img_rel = str(dw.relative_to(target_dir))
            break

        # Detect Engine & Archetype
        is_unity = False
        is_ue = False
        is_wine = False
        executable_rel = ""
        args: list[str] = []
        has_vc_redist = False

        start_scripts = list(target_dir.glob("start*.sh")) + list(target_dir.glob("*/start*.sh"))
        for s in start_scripts:
            try:
                content = s.read_text(encoding="utf-8", errors="ignore")
                if "SYSWINE" in content or "wine" in content:
                    is_wine = True
                if "VC_redist" in content or "vcredist" in content:
                    has_vc_redist = True
                m_cmd = re.search(r'CMD=\((.*?)\)', content)
                if m_cmd:
                    tokens = m_cmd.group(1).split()
                    for t in tokens:
                        t = t.strip('"\'./')
                        if t.endswith((".exe", ".x86_64", ".bin", ".elf", ".sh", ".x86")):
                            executable_rel = t
                            break
                        if t == "-dx12":
                            args.append("-dx12")
            except Exception:
                pass

        # Inspect dwarfs-tree or directory
        tree_files = list(target_dir.glob("**/dwarfs-tree"))
        tree_text = ""
        for tf in tree_files:
            try:
                tree_text = tf.read_text(encoding="utf-8", errors="ignore")
                break
            except Exception:
                pass

        if "globalgamemanagers" in tree_text or "UnityPlayer.so" in tree_text or "GameAssembly.so" in tree_text:
            is_unity = True
        if "Engine/Binaries" in tree_text or "UE4" in tree_text or "UE5" in tree_text or "FactoryGame" in tree_text:
            is_ue = True
            is_wine = True
        if "VC_redist" in tree_text or "vcredist" in tree_text:
            has_vc_redist = True

        # Choose base preset
        if preset:
            chosen_preset = preset
        elif is_ue:
            chosen_preset = "base_unreal_engine_5"
        elif is_unity:
            chosen_preset = "base_unity_native"
        elif is_wine:
            chosen_preset = "base_wine_dxvk"
        else:
            chosen_preset = "base_native"

        runtime_type = "wine" if is_wine else "native"

        if not executable_rel:
            if is_wine:
                executable_rel = "game.exe"
            elif is_unity:
                executable_rel = f"{profile_id}.x86_64"
            else:
                executable_rel = "bin/x64/game"

        if output_path is None:
            output_path = PROFILES_DIR / f"{profile_id}.toml"

        if output_path.exists() and not overwrite:
            raise FileExistsError(f"Profile file already exists at {output_path}. Use --overwrite to replace it.")

        toml_content = f"""# ==============================================================================
# {name} Game Profile
# Auto-generated by Master Game Runner Engine Scaffolder
# ==============================================================================

extends = "{chosen_preset}"

[meta]
id = "{profile_id}"
name = "{name}"
version = "1.0.0"
icon = "applications-games"
developer = "Unknown"
genre = "General"
description = "Auto-scaffolded profile for {name}."

[paths]
game_dir = "{target_dir}"
"""
        if dwarfs_img_rel:
            toml_content += f"""dwarfs_image = "{dwarfs_img_rel}"
dwarfs_mount = "files/.game-root-mnt"
overlay_dir = "files/game-root"
overlay_storage = "files/overlay-storage"
overlay_work = "files/.game-root-work"
"""
        toml_content += f"""executable = "{executable_rel}"
arguments = {args}

[runtime]
type = "{runtime_type}"
"""
        if runtime_type == "wine":
            prefix_loc = "files/prefix" if (target_dir / "files" / "prefix").exists() else "files/wine-prefix-ew"
            redist_list = '["VC_redist.x64.exe"]' if has_vc_redist else '[]'
            toml_content += f"""
[runtime.wine]
prefix_dir = "{prefix_loc}"
arch = "win64"
sync_mode = "fsync"
dxvk = true
vkd3d = {str(is_ue).lower()}
redistributables = {redist_list}
winetricks = ["dxvk"]
"""

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(toml_content, encoding="utf-8")
        log_success(f"Generated profile at: {output_path}")

        if install_desktop:
            mgr = ProfileManager()
            install_desktop_shortcut(mgr, profile_id)

        return output_path


# ==============================================================================
# 8. SYSTEM DOCTOR DIAGNOSTIC ENGINE (`master_runner.py doctor`)
# ==============================================================================

def run_doctor() -> bool:
    """Runs a comprehensive system diagnostic check."""
    if not console:
        print("Doctor diagnostics require rich console.")
        return False

    table = Table(title="System Doctor Diagnostics (AGY Gaming Environment)", box=box.ROUNDED)
    table.add_column("Category", style="cyan", no_wrap=True)
    table.add_column("Component", style="white")
    table.add_column("Status", justify="center")
    table.add_column("Details / Actionable Advice", style="dim")

    overall_ok = True

    # 1. Kernel & Sysctl
    try:
        uname = os.uname()
        table.add_row("Kernel", "Linux Version", "[bold green]OK[/bold green]", f"{uname.sysname} {uname.release}")
    except Exception:
        pass

    try:
        max_map = int(Path("/proc/sys/vm/max_map_count").read_text(encoding="utf-8").strip())
        if max_map >= 2147483642:
            table.add_row("Sysctl", "vm.max_map_count", "[bold green]OK[/bold green]", f"{max_map} (UE5 & DX12 ready)")
        else:
            table.add_row("Sysctl", "vm.max_map_count", "[bold yellow]WARN[/bold yellow]", f"{max_map} (Recommend: sysctl -w vm.max_map_count=2147483642)")
    except Exception as e:
        table.add_row("Sysctl", "vm.max_map_count", "[bold red]FAIL[/bold red]", str(e))

    try:
        split_lock = Path("/proc/sys/kernel/split_lock_mitigate").read_text(encoding="utf-8").strip()
        table.add_row("Sysctl", "split_lock_mitigate", "[bold green]OK[/bold green]", f"{split_lock} (0 = low-latency gaming)")
    except Exception:
        pass

    # 2. Display & Wayland Session
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    if wayland_display:
        table.add_row("Display", "Wayland Session", "[bold green]OK[/bold green]", f"Active socket: {wayland_display}")
    else:
        table.add_row("Display", "Wayland Session", "[bold yellow]WARN[/bold yellow]", "WAYLAND_DISPLAY not set")

    # 3. GPU Matrix & Drivers
    gpus = detect_gpus()
    if gpus:
        for g in gpus:
            role = "Primary Display" if g.is_primary else "3D Render Offload"
            status = "[bold green]OK[/bold green]"
            table.add_row("GPU", f"{g.vendor_name} ({g.driver})", status, f"{g.device_name} [{role}]")
    else:
        table.add_row("GPU", "GPU Detection", "[bold red]FAIL[/bold red]", "No GPU DRM nodes found")
        overall_ok = False

    # 4. Binaries & Packaging
    binaries = [
        ("dwarfs", "DwarFS FUSE driver", True),
        ("fuse-overlayfs", "Rootless overlay filesystem", True),
        ("fusermount3", "FUSE unmount utility", True),
        ("wine", "Wine-Staging runtime", True),
        ("wineserver", "Wine prefix coordinator", True),
        ("winetricks", "Wine prefix configuration helper", True),
        ("gamescope", "Wayland micro-compositor", False),
        ("mangohud", "Universal OpenGL/Vulkan HUD & limiter", False),
        ("mangoapp", "Gamescope performance overlay daemon", False),
        ("gamemoderun", "Feral GameMode performance daemon", False),
        ("bwrap", "Bubblewrap container sandbox", False),
        ("prime-run", "NVIDIA PRIME GPU offload wrapper", False),
    ]

    for binary, desc, required in binaries:
        path = shutil.which(binary)
        if path:
            table.add_row("Tools", binary, "[bold green]OK[/bold green]", f"{desc} ({path})")
        else:
            if required:
                table.add_row("Tools", binary, "[bold red]MISSING[/bold red]", f"Required: sudo pacman -S {binary}")
                overall_ok = False
            else:
                table.add_row("Tools", binary, "[bold yellow]OPTIONAL[/bold yellow]", f"{desc} not found")

    # 5. Audio Server
    pipewire_sock = Path(f"/run/user/{os.getuid()}/pipewire-0")
    if pipewire_sock.exists():
        table.add_row("Audio", "PipeWire Socket", "[bold green]OK[/bold green]", f"Connected ({pipewire_sock})")
    else:
        table.add_row("Audio", "PipeWire Socket", "[bold yellow]WARN[/bold yellow]", "PipeWire socket not active")

    console.print(table)
    return overall_ok


# ==============================================================================
# 9. VALIDATOR ENGINE (`master_runner.py validate`)
# ==============================================================================

def validate_profiles(manager: ProfileManager, target_id: str | None = None) -> bool:
    """Validates profile syntax, files, and executable paths."""
    profiles = manager.discover_profiles()
    if target_id:
        if target_id not in profiles:
            log_error(f"Profile '{target_id}' not found.")
            return False
        profiles = {target_id: profiles[target_id]}

    table = Table(title="Game Profile Validation Matrix", box=box.ROUNDED)
    table.add_column("Profile ID", style="cyan")
    table.add_column("Game Title", style="white")
    table.add_column("Preset", style="blue")
    table.add_column("DwarFS Image", justify="center")
    table.add_column("Executable Check", justify="center")
    table.add_column("Status", justify="center")

    all_valid = True

    for pid, p_path in profiles.items():
        try:
            _, cfg = manager.load_profile(pid)
            meta = cfg.get("meta", {})
            name = meta.get("name", pid)
            preset = cfg.get("extends", "none")

            paths = MountManager.get_profile_paths(cfg)
            dwarfs_img = paths["dwarfs_image"]

            if dwarfs_img:
                dw_status = "[bold green]Present[/bold green]" if dwarfs_img.exists() else "[bold red]Missing[/bold red]"
                if not dwarfs_img.exists():
                    all_valid = False
            else:
                dw_status = "[dim]None[/dim]"

            exec_rel = cfg.get("paths", {}).get("executable", "")
            ov_dir = paths["overlay_dir"]
            gm_dir = paths["game_dir"]
            
            exec_cand1 = ov_dir / exec_rel
            exec_cand2 = gm_dir / exec_rel
            is_mounted = MountManager.is_mounted(ov_dir)

            if is_mounted and exec_cand1.exists():
                exec_status = "[bold green]Ready (Mounted)[/bold green]"
            elif exec_cand2.exists():
                exec_status = "[bold green]Found[/bold green]"
            elif dwarfs_img and dwarfs_img.exists():
                exec_status = "[bold cyan]Packed in DwarFS[/bold cyan]"
            else:
                exec_status = "[bold yellow]Unverified[/bold yellow]"

            status = "[bold green]VALID[/bold green]" if dw_status != "[bold red]Missing[/bold red]" else "[bold red]INVALID[/bold red]"
            table.add_row(pid, name, preset, dw_status, exec_status, status)
        except Exception as e:
            table.add_row(pid, "ERROR", "none", "[red]ERR[/red]", "[red]ERR[/red]", f"[bold red]{e}[/bold red]")
            all_valid = False

    if console:
        console.print(table)
    return all_valid


# ==============================================================================
# 10. DESKTOP SHORTCUT INTEGRATION
# ==============================================================================

def install_desktop_shortcut(manager: ProfileManager, profile_id: str) -> bool:
    """Generates a ~/.local/share/applications/<id>.desktop shortcut."""
    _, cfg = manager.load_profile(profile_id)
    meta = cfg.get("meta", {})
    name = meta.get("name", profile_id)
    comment = meta.get("description", f"Launch {name} via Master Game Runner Engine")
    icon = meta.get("icon", "applications-games")

    desktop_dir = Path.home() / ".local" / "share" / "applications"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    target_file = desktop_dir / f"{profile_id}.desktop"

    script_path = (SELF_DIR / "master_runner.py").resolve()

    content = f"""[Desktop Entry]
Type=Application
Name={name}
Comment={comment}
Exec=python3 {script_path} run {profile_id}
Icon={icon}
Terminal=false
Categories=Game;
StartupNotify=true
"""
    target_file.write_text(content, encoding="utf-8")
    log_success(f"Installed desktop entry: {target_file}")

    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(desktop_dir)], capture_output=True)
    return True


# ==============================================================================
# 11. INTERACTIVE RICH TUI DASHBOARD (`master_runner.py menu`)
# ==============================================================================

def render_interactive_menu(manager: ProfileManager) -> None:
    """Renders the Rich TUI Dashboard for instant keyboard launching and management."""
    if not console:
        print("Interactive menu requires rich console.")
        return

    while True:
        console.clear()
        console.print(Panel.fit(
            f"[bold cyan]{ENGINE_NAME} v{ENGINE_VERSION}[/bold cyan]\n"
            "[dim]Bleeding-Edge Arch Linux • Pure Wayland • Modular TOML Engine[/dim]",
            border_style="cyan"
        ))

        profiles = manager.discover_profiles()
        if not profiles:
            console.print("[bold yellow]No game profiles found in profiles/.[/bold yellow]")
            break

        table = Table(title="Available Game Profiles", box=box.ROUNDED)
        table.add_column("#", style="bold cyan", justify="right")
        table.add_column("ID", style="cyan")
        table.add_column("Game Title", style="white")
        table.add_column("Type", style="magenta")
        table.add_column("GPU", style="green")
        table.add_column("Mount Status", justify="center")
        table.add_column("Genre / Description", style="dim")

        profile_list = list(profiles.items())

        for idx, (pid, p_path) in enumerate(profile_list, 1):
            try:
                _, cfg = manager.load_profile(pid)
                name = cfg.get("meta", {}).get("name", pid)
                genre = cfg.get("meta", {}).get("genre", "Game")
                rtype = cfg.get("runtime", {}).get("type", "native")
                gpu = cfg.get("graphics", {}).get("gpu", "auto")

                dw_mounted, ov_mounted = MountManager.get_mount_status(cfg)
                if dw_mounted and ov_mounted:
                    mount_badge = "[bold green]MOUNTED[/bold green]"
                elif dw_mounted or ov_mounted:
                    mount_badge = "[bold yellow]PARTIAL[/bold yellow]"
                else:
                    mount_badge = "[dim]UNMOUNTED[/dim]"

                table.add_row(str(idx), pid, name, rtype, gpu, mount_badge, genre)
            except Exception as e:
                table.add_row(str(idx), pid, "[red]Error loading[/red]", "-", "-", "[red]ERR[/red]", str(e))

        console.print(table)
        console.print("\n[bold]Commands:[/bold] [cyan]1-{}[/cyan] Launch Game | [yellow]m <#>[/yellow] Mount | [yellow]u <#>[/yellow] Unmount | [magenta]d[/magenta] Doctor | [magenta]v[/magenta] Validate | [red]q[/red] Quit".format(len(profile_list)))

        choice = Prompt.ask("\n[bold green]Selection[/bold green]", default="q").strip()

        if choice.lower() in ("q", "quit", "exit"):
            break
        elif choice.lower() == "d":
            run_doctor()
            Prompt.ask("\nPress Enter to return to menu...")
        elif choice.lower() == "v":
            validate_profiles(manager)
            Prompt.ask("\nPress Enter to return to menu...")
        elif choice.lower().startswith("m "):
            target = choice[2:].strip()
            try:
                idx = int(target) - 1
                pid = profile_list[idx][0]
                _, cfg = manager.load_profile(pid)
                MountManager.mount(cfg)
            except Exception as e:
                log_error(f"Failed to mount: {e}")
            Prompt.ask("\nPress Enter to continue...")
        elif choice.lower().startswith("u "):
            target = choice[2:].strip()
            try:
                idx = int(target) - 1
                pid = profile_list[idx][0]
                _, cfg = manager.load_profile(pid)
                MountManager.unmount(cfg)
            except Exception as e:
                log_error(f"Failed to unmount: {e}")
            Prompt.ask("\nPress Enter to continue...")
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(profile_list):
                pid = profile_list[idx][0]
                _, cfg = manager.load_profile(pid)
                runner = GameRunner(pid, cfg)
                runner.execute()
                Prompt.ask("\nPress Enter to return to menu...")


# ==============================================================================
# 12. CLI ARGUMENT PARSER & MAIN ENTRYPOINT
# ==============================================================================

def main():
    # Quality of Life: Auto-dispatch direct game invocation (e.g. `python3 master_runner.py <game_id>`)
    known_commands = {
        "run", "menu", "list", "status", "mount", "unmount", "unmount-all",
        "validate", "init", "doctor", "install-desktop", "install-all-desktops",
        "-h", "--help"
    }

    if len(sys.argv) > 1 and sys.argv[1] not in known_commands:
        target_cand = sys.argv[1]
        profiles_cand = [p.stem for p in PROFILES_DIR.glob("*.toml") if not p.name.startswith("_")]
        if target_cand in profiles_cand or Path(target_cand).is_file():
            sys.argv.insert(1, "run")

    parser = argparse.ArgumentParser(
        prog="master_runner.py",
        description=f"{ENGINE_NAME} v{ENGINE_VERSION} — Declarative Arch Linux Gaming Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="Operational Subcommands")

    # Command: run
    run_parser = subparsers.add_parser("run", help="Launch a game by profile ID or path")
    run_parser.add_argument("profile", help="Profile ID or path to .toml profile")
    run_parser.add_argument("--gpu", choices=["auto", "discrete", "integrated"], help="Override GPU offload target")
    run_parser.add_argument("--gamescope", action="store_true", default=None, help="Force enable Gamescope micro-compositor")
    run_parser.add_argument("--no-gamescope", action="store_false", dest="gamescope", help="Force disable Gamescope")
    run_parser.add_argument("--gamescope-res", help="Gamescope render resolution (e.g. 1920x1080)")
    run_parser.add_argument("--gamescope-out-res", help="Gamescope output display resolution (e.g. 1920x1080)")
    run_parser.add_argument("--gamescope-mode", choices=["embedded", "nested", "fullscreen", "borderless"], help="Gamescope mode")
    run_parser.add_argument("--gamescope-fsr", action="store_true", help="Enable Gamescope FSR upscaling")
    run_parser.add_argument("--gamescope-sharpness", type=int, help="Gamescope FSR sharpness (0-20)")
    run_parser.add_argument("--gamescope-tearing", action="store_true", help="Enable Gamescope immediate flips (low-latency tearing)")
    run_parser.add_argument("--fps", "--fps-limit", type=int, dest="fps_limit", help="Universal FPS limiter cap")
    run_parser.add_argument("--mangohud", action="store_true", default=None, help="Force enable MangoHud overlay")
    run_parser.add_argument("--no-mangohud", action="store_false", dest="mangohud", help="Force disable MangoHud overlay")
    run_parser.add_argument("--mangohud-preset", help="MangoHud configuration preset (e.g. minimal, full)")
    run_parser.add_argument("--gamemode", action="store_true", default=None, help="Enable Feral GameMode")
    run_parser.add_argument("--no-gamemode", action="store_false", dest="gamemode", help="Disable Feral GameMode")
    run_parser.add_argument("--sync", choices=["fsync", "esync", "ntsync", "server"], help="Wine synchronization primitive")
    run_parser.add_argument("--wine-bin", help="Custom Wine / Proton binary path")
    run_parser.add_argument("--wine-debug", help="WINEDEBUG logging channel override")
    run_parser.add_argument("--dxvk", action="store_true", default=None, help="Enable DXVK translation")
    run_parser.add_argument("--vkd3d", action="store_true", default=None, help="Enable VKD3D-Proton translation")
    run_parser.add_argument("--dxvk-nvapi", action="store_true", help="Enable DXVK-NVAPI (DLSS/Reflex)")
    run_parser.add_argument("--reprovision", action="store_true", help="Force re-provisioning of Wine prefix redistributables and winetricks")
    run_parser.add_argument("--sandbox", action="store_true", default=None, help="Enable Bubblewrap isolation")
    run_parser.add_argument("--no-sandbox", action="store_false", dest="sandbox", help="Disable Bubblewrap isolation")
    run_parser.add_argument("--dry-run", action="store_true", help="Simulate pipeline setup without running game binary")
    run_parser.add_argument("-v", "--verbose", action="store_true", help="Enable detailed debug logs")
    run_parser.add_argument("extra_args", nargs="*", help="Arbitrary trailing arguments passed directly to the game binary")

    # Command: menu
    subparsers.add_parser("menu", help="Launch interactive Rich TUI Dashboard")

    # Command: list
    subparsers.add_parser("list", help="List all discovered game profiles and base presets")

    # Command: status
    subparsers.add_parser("status", help="Show active DwarFS and fuse-overlayfs mount statuses")

    # Command: mount
    mount_parser = subparsers.add_parser("mount", help="Mount DwarFS & OverlayFS for a game profile without launching")
    mount_parser.add_argument("profile", help="Profile ID to mount")
    mount_parser.add_argument("--dry-run", action="store_true", help="Simulate mount operation")

    # Command: unmount
    unmount_parser = subparsers.add_parser("unmount", help="Unmount DwarFS & OverlayFS for a game profile")
    unmount_parser.add_argument("profile", help="Profile ID to unmount")
    unmount_parser.add_argument("--dry-run", action="store_true", help="Simulate unmount operation")

    # Command: unmount-all
    unmount_all_parser = subparsers.add_parser("unmount-all", help="Unmount all active game profiles across the system")
    unmount_all_parser.add_argument("--dry-run", action="store_true", help="Simulate unmount operations")

    # Command: validate
    validate_parser = subparsers.add_parser("validate", help="Validate profile configuration syntax and assets")
    validate_parser.add_argument("profile", nargs="?", default=None, help="Profile ID to validate (or omit for all)")
    validate_parser.add_argument("--all", action="store_true", help="Validate all discovered profiles")

    # Command: init
    init_parser = subparsers.add_parser("init", help="Auto-scaffold a new game profile from target directory")
    init_parser.add_argument("path", help="Path to target game directory")
    init_parser.add_argument("--name", help="Display name of the game")
    init_parser.add_argument("--id", help="Profile ID slug")
    init_parser.add_argument("--preset", help="Force specific base preset")
    init_parser.add_argument("--output", help="Custom output path for generated .toml")
    init_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing profile if present")
    init_parser.add_argument("--install-desktop", action="store_true", help="Automatically create FreeDesktop shortcut")

    # Command: doctor
    subparsers.add_parser("doctor", help="Run system diagnostics on drivers, kernel, FUSE, and Wayland stack")

    # Command: install-desktop
    desktop_parser = subparsers.add_parser("install-desktop", help="Install .desktop application shortcut")
    desktop_parser.add_argument("profile", help="Profile ID to install shortcut for")

    # Command: install-all-desktops
    subparsers.add_parser("install-all-desktops", help="Install .desktop shortcuts for all discovered profiles")

    args = parser.parse_args()

    if not args.command:
        manager = ProfileManager()
        render_interactive_menu(manager)
        return

    manager = ProfileManager()

    match args.command:
        case "menu":
            render_interactive_menu(manager)

        case "list":
            profiles = manager.discover_profiles()
            presets = manager.discover_presets()

            if console:
                p_table = Table(title="Discovered Game Profiles", box=box.ROUNDED)
                p_table.add_column("Profile ID", style="cyan")
                p_table.add_column("Title", style="white")
                p_table.add_column("Preset Hierarchy", style="magenta")
                p_table.add_column("Game Directory", style="dim")

                for pid, path in profiles.items():
                    try:
                        _, cfg = manager.load_profile(pid)
                        title = cfg.get("meta", {}).get("name", pid)
                        preset = cfg.get("extends", "none")
                        gdir = cfg.get("paths", {}).get("game_dir", "-")
                        p_table.add_row(pid, title, preset, gdir)
                    except Exception as e:
                        p_table.add_row(pid, "[red]Error[/red]", "-", str(e))

                console.print(p_table)

                pr_table = Table(title="Base Archetype Presets", box=box.ROUNDED)
                pr_table.add_column("Preset ID", style="blue")
                pr_table.add_column("File Path", style="dim")
                for prid, path in presets.items():
                    pr_table.add_row(prid, str(path))
                console.print(pr_table)
            else:
                print("Profiles:", list(profiles.keys()))
                print("Presets:", list(presets.keys()))

        case "status":
            profiles = manager.discover_profiles()
            if console:
                table = Table(title="Active Game Mount & Runtime Status", box=box.ROUNDED)
                table.add_column("Profile ID", style="cyan")
                table.add_column("Game Title", style="white")
                table.add_column("DwarFS Layer", justify="center")
                table.add_column("OverlayFS Layer", justify="center")
                table.add_column("Playable Game Root", style="dim")

                for pid, p_path in profiles.items():
                    try:
                        _, cfg = manager.load_profile(pid)
                        paths = MountManager.get_profile_paths(cfg)
                        dw_m, ov_m = MountManager.get_mount_status(cfg)
                        dw_badge = "[bold green]MOUNTED[/bold green]" if dw_m else "[dim]UNMOUNTED[/dim]"
                        ov_badge = "[bold green]MOUNTED[/bold green]" if ov_m else "[dim]UNMOUNTED[/dim]"
                        name = cfg.get("meta", {}).get("name", pid)
                        table.add_row(pid, name, dw_badge, ov_badge, str(paths["overlay_dir"]))
                    except Exception as e:
                        table.add_row(pid, "ERROR", "-", "-", str(e))

                console.print(table)

        case "mount":
            try:
                pid, cfg = manager.load_profile(args.profile)
                MountManager.mount(cfg, dry_run=args.dry_run)
            except Exception as e:
                log_error(f"Mount failed: {e}")
                sys.exit(1)

        case "unmount":
            try:
                pid, cfg = manager.load_profile(args.profile)
                MountManager.unmount(cfg, dry_run=args.dry_run)
            except Exception as e:
                log_error(f"Unmount failed: {e}")
                sys.exit(1)

        case "unmount-all":
            count = MountManager.unmount_all(manager, dry_run=args.dry_run)
            log_success(f"Completed unmount sweep. Unmounted {count} active profiles.")

        case "doctor":
            ok = run_doctor()
            sys.exit(0 if ok else 1)

        case "validate":
            target = None if args.all or not args.profile else args.profile
            ok = validate_profiles(manager, target_id=target)
            sys.exit(0 if ok else 1)

        case "init":
            try:
                out_p = Path(args.output).resolve() if args.output else None
                ProfileScaffolder.scaffold(
                    target_dir=Path(args.path),
                    name=args.name,
                    profile_id=args.id,
                    preset=args.preset,
                    output_path=out_p,
                    overwrite=args.overwrite,
                    install_desktop=args.install_desktop
                )
            except Exception as e:
                log_error(f"Failed to auto-scaffold profile: {e}")
                sys.exit(1)

        case "install-desktop":
            try:
                install_desktop_shortcut(manager, args.profile)
            except Exception as e:
                log_error(f"Failed to install desktop shortcut: {e}")
                sys.exit(1)

        case "install-all-desktops":
            profiles = manager.discover_profiles()
            for pid in profiles:
                try:
                    install_desktop_shortcut(manager, pid)
                except Exception as e:
                    log_error(f"Failed for {pid}: {e}")

        case "run":
            overrides: dict[str, Any] = {}

            if args.gpu:
                overrides.setdefault("graphics", {})["gpu"] = args.gpu

            if args.gamescope is not None:
                overrides.setdefault("graphics", {}).setdefault("gamescope", {})["enabled"] = args.gamescope

            if args.gamescope_res:
                try:
                    w, h = map(int, args.gamescope_res.lower().split("x"))
                    overrides.setdefault("graphics", {}).setdefault("gamescope", {})["width"] = w
                    overrides.setdefault("graphics", {}).setdefault("gamescope", {})["height"] = h
                except ValueError:
                    log_error("Invalid --gamescope-res format. Use WIDTHxHEIGHT (e.g. 1920x1080)")

            if args.gamescope_out_res:
                try:
                    w, h = map(int, args.gamescope_out_res.lower().split("x"))
                    overrides.setdefault("graphics", {}).setdefault("gamescope", {})["output_width"] = w
                    overrides.setdefault("graphics", {}).setdefault("gamescope", {})["output_height"] = h
                except ValueError:
                    log_error("Invalid --gamescope-out-res format. Use WIDTHxHEIGHT (e.g. 1920x1080)")

            if args.gamescope_mode:
                overrides.setdefault("graphics", {}).setdefault("gamescope", {})["mode"] = args.gamescope_mode

            if args.gamescope_fsr:
                overrides.setdefault("graphics", {}).setdefault("gamescope", {})["fsr_upscaling"] = True

            if args.gamescope_sharpness is not None:
                overrides.setdefault("graphics", {}).setdefault("gamescope", {})["fsr_sharpness"] = args.gamescope_sharpness

            if args.gamescope_tearing:
                overrides.setdefault("graphics", {}).setdefault("gamescope", {})["allow_tearing"] = True

            if args.fps_limit is not None:
                overrides.setdefault("performance", {})["fps_limit"] = args.fps_limit

            if args.mangohud is not None:
                overrides.setdefault("performance", {})["mangohud"] = args.mangohud

            if args.mangohud_preset:
                overrides.setdefault("performance", {})["mangohud_preset"] = args.mangohud_preset

            if args.gamemode is not None:
                overrides.setdefault("performance", {})["gamemode"] = args.gamemode

            if args.sync:
                overrides.setdefault("runtime", {}).setdefault("wine", {})["sync_mode"] = args.sync

            if args.wine_bin:
                overrides.setdefault("runtime", {}).setdefault("wine", {})["wine_binary"] = args.wine_bin

            if args.wine_debug:
                overrides.setdefault("runtime", {}).setdefault("wine", {})["debug"] = args.wine_debug

            if args.dxvk is not None:
                overrides.setdefault("runtime", {}).setdefault("wine", {})["dxvk"] = args.dxvk

            if args.vkd3d is not None:
                overrides.setdefault("runtime", {}).setdefault("wine", {})["vkd3d"] = args.vkd3d

            if args.dxvk_nvapi:
                overrides.setdefault("runtime", {}).setdefault("wine", {})["dxvk_nvapi"] = True

            if args.sandbox is not None:
                overrides.setdefault("sandbox", {})["enabled"] = args.sandbox

            try:
                pid, cfg = manager.load_profile(args.profile, cli_overrides=overrides)
                runner = GameRunner(
                    profile_id=pid,
                    config=cfg,
                    extra_args=args.extra_args,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                    reprovision=args.reprovision
                )
                exit_code = runner.execute()
                sys.exit(exit_code)
            except Exception as e:
                log_error(f"Fatal execution error: {e}")
                if args.verbose:
                    import traceback
                    traceback.print_exc()
                sys.exit(1)


if __name__ == "__main__":
    main()
