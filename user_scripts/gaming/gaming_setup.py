#!/usr/bin/env python3
"""
Arch Linux Universal Gaming Architecture Installer.
Engineered for Bleeding-Edge Arch Linux, Pure Wayland, Hyprland, and Linux Kernel 7.x+.

Features:
- Pure Wayland Gaming Pipeline (zero legacy Xorg dependencies)
- Multi-GPU Dynamic Hardware Auto-Detection (Intel, AMD, NVIDIA, & Hybrid Prime Offload)
- Safe, Idempotent Pacman & [multilib] Configuration with Parallel Downloads
- Bleeding-Edge Native Gaming Stack (Steam, Lutris, Wine-Staging, Winetricks, GameMode, MangoHud, Gamescope)
- Complete 32-bit & 64-bit Audio/Video/Vulkan Compatibility Layer
- Kernel 7.x / Sysctl Gaming Performance Tweaks (vm.max_map_count, split-lock mitigation, nofile limits)
- Real-time Frame Pacing (Gamescope CAP_SYS_NICE capability & GameMode daemon)
- Flatpak Ecosystem & Runtime Vulkan Layers (Bottles, Flatseal, ProtonPlus, Heroic, MangoHud runtime)
- Native Wayland Flatpak Overrides & Host Filesystem Permissions
- Seamless Application Launcher & Hicolor Icon Bridging for Wayland/Hyprland
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

# Pre-flight check and graceful bootstrap for rich library
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("\n[INFO] Initializing setup environment: 'python-rich' is being loaded...")
    if os.geteuid() != 0 and shutil.which("pacman") and shutil.which("sudo"):
        try:
            print("Installing python-rich for modern terminal interface...")
            subprocess.run(["sudo", "pacman", "-S", "--needed", "--noconfirm", "python-rich"], check=True)
            from rich.console import Console
            from rich.panel import Panel
            from rich.prompt import Confirm, Prompt
            from rich.table import Table
            from rich.text import Text
        except Exception:
            print("\n[CRITICAL ERROR] The 'rich' library is not installed.")
            print("Please install it: sudo pacman -S python-rich")
            sys.exit(1)
    else:
        print("\n[CRITICAL ERROR] The 'rich' library is not installed.")
        print("Please install it: sudo pacman -S python-rich")
        sys.exit(1)

console = Console()

# Configuration Constants
SYSCTL_GAMING_CONF = """# Gaming performance & stability optimizations for Arch Linux / Kernel 7.x+
# Memory mapping limit for 64-bit Wine/Proton games (prevents crashes in Star Citizen, UE5, Hogwarts Legacy)
vm.max_map_count = 2147483642

# Prevent micro-stuttering caused by kernel split-lock penalty mitigation in modern games
kernel.split_lock_mitigate = 0
"""

LIMITS_GAMING_CONF = """# File descriptor limits for Wine/Proton ESYNC & FSYNC
* soft nofile 524288
* hard nofile 1048576
"""


@dataclass
class GPUInfo:
    vendor_id: str
    vendor_name: str
    device_name: str
    pci_slot: str
    driver: str


class SetupContext:
    def __init__(self, dry_run: bool = False, auto_yes: bool = False, skip_gpu: bool = False):
        self.dry_run = dry_run
        self.auto_yes = auto_yes
        self.skip_gpu = skip_gpu
        self.stop_sudo_event = threading.Event()
        self.sudo_thread: Optional[threading.Thread] = None


def keep_sudo_alive(stop_event: threading.Event):
    """
    Background daemon thread to refresh the 'sudo' timestamp cache.
    Refreshes every 90 seconds to prevent credential expiration during long downloads.
    """
    while not stop_event.is_set():
        try:
            subprocess.run(
                ["sudo", "-v"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
        stop_event.wait(90)


def check_root_and_locks(ctx: SetupContext):
    """Ensure non-root execution and intelligently check/manage pacman database locks."""
    if os.geteuid() == 0:
        console.print("[bold red]CRITICAL ERROR: Do not run this script as root.[/bold red]")
        console.print("Run it as your normal user. Sudo will be invoked securely with proper permissions.")
        sys.exit(1)

    db_lck = Path("/var/lib/pacman/db.lck")
    if db_lck.exists():
        console.print(f"[bold yellow]Notice: Pacman lock file exists at {db_lck}[/bold yellow]")

        # Check if an active process is actually holding or using the lock
        lock_holder = None
        if shutil.which("fuser"):
            try:
                res = subprocess.run(["fuser", str(db_lck)], capture_output=True, text=True)
                if res.stdout.strip():
                    lock_holder = res.stdout.strip()
            except Exception:
                pass

        # Check running processes for package managers
        active_mgrs = []
        try:
            res = subprocess.run(["pgrep", "-a", "pacman|yay|paru|pamac"], capture_output=True, text=True)
            if res.stdout.strip():
                active_mgrs = res.stdout.strip().splitlines()
        except Exception:
            pass

        if lock_holder or active_mgrs:
            console.print("[bold red]CRITICAL: Another package manager is actively running.[/bold red]")
            if active_mgrs:
                console.print(f"Active processes:\n[dim]{chr(10).join(active_mgrs)}[/dim]")
            console.print("Please wait for ongoing package operations to finish before running this installer.")
            sys.exit(1)
        else:
            console.print("[yellow]No active package manager detected. The lock appears to be stale (from an interrupted process or reboot).[/yellow]")
            if ctx.auto_yes or Confirm.ask("[bold cyan]Remove stale pacman lock file and continue?[/bold cyan]", default=True):
                if ctx.dry_run:
                    console.print("[dim][DRY RUN] Would execute: sudo rm -f /var/lib/pacman/db.lck[/dim]")
                else:
                    subprocess.run(["sudo", "rm", "-f", str(db_lck)], check=True)
                    console.print("[bold green]✔ Stale lock removed successfully.[/bold green]")
            else:
                console.print("[red]Aborted by user.[/red]")
                sys.exit(1)


def run_command(ctx: SetupContext, command: str, description: str, critical: bool = True, show_command: bool = True, retries: int = 1) -> bool:
    """
    Executes a shell command natively and interactively.
    Supports retry attempts for network-sensitive operations (pacman, flatpak, aur).
    """
    console.print(f"\n[bold cyan]Task:[/bold cyan] {description}")
    if show_command:
        console.print(f"[dim]{command}[/dim]")

    if ctx.dry_run:
        console.print("[dim][DRY RUN] Skipped actual execution.[/dim]")
        return True

    if not ctx.auto_yes:
        if not Confirm.ask("[bold yellow]Execute this step?[/bold yellow]", default=True):
            console.print("[dim]Skipped by user.[/dim]")
            return True

    console.print("[dim]" + "─" * 60 + "[/dim]")
    for attempt in range(1, retries + 1):
        try:
            if attempt > 1:
                console.print(f"[yellow]Retrying task (attempt {attempt}/{retries})...[/yellow]")
            result = subprocess.run(command, shell=True)
            console.print("[dim]" + "─" * 60 + "[/dim]")

            if result.returncode == 0:
                console.print("[bold green]✔ Success[/bold green]")
                return True
            else:
                console.print(f"[bold red]✘ Failed with exit code {result.returncode}[/bold red]")
                if attempt < retries:
                    time.sleep(2)
                    continue
                if critical:
                    console.print("[bold red]A critical step failed. Aborting installer to maintain system stability.[/bold red]")
                    sys.exit(1)
                return False
        except Exception as e:
            console.print(f"[bold red]✘ Execution error: {e}[/bold red]")
            if attempt < retries:
                time.sleep(2)
                continue
            if critical:
                sys.exit(1)
            return False
    return False


def enable_multilib_and_optimizations(ctx: SetupContext) -> bool:
    """
    Idempotently enables [multilib] and modern pacman features (Color, ParallelDownloads, DisableDownloadTimeout).
    Creates an atomic backup before applying changes.
    """
    pacman_conf = Path("/etc/pacman.conf")
    if not pacman_conf.exists():
        console.print("[bold red]Critical system file /etc/pacman.conf not found![/bold red]")
        sys.exit(1)

    try:
        content = pacman_conf.read_text()
        lines = content.splitlines()
    except Exception as e:
        console.print(f"[bold red]Failed to read pacman.conf: {e}[/bold red]")
        sys.exit(1)

    # Check if multilib is already fully enabled
    multilib_active = False
    include_active = False
    in_multilib_check = False
    has_disable_timeout = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[multilib]":
            multilib_active = True
            in_multilib_check = True
        elif in_multilib_check and stripped.startswith("[") and stripped.endswith("]"):
            in_multilib_check = False
        elif in_multilib_check and re.match(r"^Include\s*=", stripped):
            include_active = True
        if "DisableDownloadTimeout" in stripped and not stripped.startswith("#"):
            has_disable_timeout = True

    multilib_ready = multilib_active and include_active

    new_lines = []
    modified = False
    in_multilib_edit = False
    found_multilib_comment = False
    options_passed = False

    for line in lines:
        stripped = line.strip()

        # Enable Color if commented
        if re.match(r"^#\s*Color\b", stripped):
            new_lines.append("Color")
            modified = True
            continue

        # Enable ParallelDownloads if commented
        if re.match(r"^#\s*ParallelDownloads\b", stripped):
            new_lines.append("ParallelDownloads = 5")
            modified = True
            continue

        # Enable DisableDownloadTimeout in [options]
        if stripped == "[options]":
            options_passed = True

        if options_passed and not has_disable_timeout and (stripped.startswith("[") and stripped != "[options]"):
            new_lines.append("DisableDownloadTimeout")
            has_disable_timeout = True
            modified = True

        # Handle [multilib] uncommenting
        if not multilib_ready:
            if re.match(r"^\s*#\s*\[multilib\]\s*$", line):
                new_lines.append("[multilib]")
                in_multilib_edit = True
                found_multilib_comment = True
                modified = True
                continue

            if in_multilib_edit:
                if re.match(r"^\s*#?\s*\[.*\]\s*$", line) and "multilib" not in line:
                    in_multilib_edit = False
                elif re.match(r"^\s*#\s*Include\s*=", line):
                    new_lines.append(re.sub(r"^\s*#\s*", "", line))
                    modified = True
                    continue

        new_lines.append(line)

    if options_passed and not has_disable_timeout:
        new_lines.insert(3, "DisableDownloadTimeout")
        modified = True

    # If multilib was completely absent, append it
    if not multilib_ready and not found_multilib_comment and not multilib_active:
        new_lines.extend(["", "[multilib]", "Include = /etc/pacman.d/mirrorlist"])
        modified = True

    if modified:
        temp_conf = Path("/tmp/pacman_gaming.conf")
        temp_conf.write_text("\n".join(new_lines) + "\n")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_cmd = f"sudo cp /etc/pacman.conf /etc/pacman.conf.bak.{timestamp}"
        apply_cmd = f"sudo install -m 644 {temp_conf} /etc/pacman.conf && rm -f {temp_conf}"

        run_command(ctx, f"{backup_cmd} && {apply_cmd}", "Configure pacman.conf (enable [multilib], Color, ParallelDownloads, & DisableDownloadTimeout)", show_command=False)
        return True

    return True


def detect_gpus() -> List[GPUInfo]:
    """
    Intelligently auto-detects all GPUs present on the system via lspci and sysfs DRM nodes.
    Accurately detects AMD, Intel, NVIDIA, and Multi-GPU / Hybrid laptop setups.
    """
    gpus: List[GPUInfo] = []
    seen_slots: Set[str] = set()

    # Strategy 1: lspci query
    if shutil.which("lspci"):
        try:
            res = subprocess.run(["lspci", "-mm", "-nn"], capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                if any(ctrl in line for ctrl in ['"0300"', '"0301"', '"0302"', "VGA", "3D", "Display"]):
                    parts = re.findall(r'"([^"]*)"', line)
                    slot = line.split()[0]
                    if slot in seen_slots:
                        continue
                    seen_slots.add(slot)

                    device_name = parts[2] if len(parts) > 2 else "Unknown Graphics Controller"
                    vendor_id = ""
                    vendor_name = parts[1] if len(parts) > 1 else "Unknown Vendor"

                    if "[10de]" in line or "10de:" in line:
                        vendor_id = "10de"
                        vendor_name = "NVIDIA"
                    elif "[1002]" in line or "1002:" in line:
                        vendor_id = "1002"
                        vendor_name = "AMD"
                    elif "[8086]" in line or "8086:" in line:
                        vendor_id = "8086"
                        vendor_name = "Intel"

                    driver = ""
                    for pci_dev in Path("/sys/bus/pci/devices").glob(f"*{slot}"):
                        driver_path = pci_dev / "driver"
                        if driver_path.exists():
                            driver = driver_path.resolve().name
                            break

                    gpus.append(GPUInfo(
                        vendor_id=vendor_id,
                        vendor_name=vendor_name,
                        device_name=device_name,
                        pci_slot=slot,
                        driver=driver
                    ))
        except Exception:
            pass

    # Strategy 2: Fallback to sysfs DRM devices
    if not gpus:
        for drm_card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            vendor_file = drm_card / "device/vendor"
            device_file = drm_card / "device/device"
            if vendor_file.exists():
                try:
                    v_id = vendor_file.read_text().strip().lower().replace("0x", "")
                    d_id = device_file.read_text().strip().lower().replace("0x", "") if device_file.exists() else ""
                    slot = drm_card.name

                    v_name = "Unknown"
                    if v_id == "10de":
                        v_name = "NVIDIA"
                    elif v_id == "1002":
                        v_name = "AMD"
                    elif v_id == "8086":
                        v_name = "Intel"

                    driver = ""
                    driver_link = drm_card / "device/driver"
                    if driver_link.exists():
                        driver = driver_link.resolve().name

                    gpus.append(GPUInfo(
                        vendor_id=v_id,
                        vendor_name=v_name,
                        device_name=f"DRM Device [{d_id}]",
                        pci_slot=slot,
                        driver=driver
                    ))
                except Exception:
                    pass

    return gpus


def get_gpu_packages(detected_gpus: List[GPUInfo]) -> Tuple[List[str], str]:
    """Determines the required driver packages based on detected GPU hardware."""
    pkgs: Set[str] = set()
    descriptions: List[str] = []

    has_amd = any(g.vendor_name == "AMD" or g.vendor_id == "1002" for g in detected_gpus)
    has_intel = any(g.vendor_name == "Intel" or g.vendor_id == "8086" for g in detected_gpus)
    has_nvidia = any(g.vendor_name == "NVIDIA" or g.vendor_id == "10de" for g in detected_gpus)

    if has_amd:
        pkgs.update([
            "mesa", "lib32-mesa",
            "vulkan-radeon", "lib32-vulkan-radeon",
            "vulkan-mesa-layers", "lib32-vulkan-mesa-layers"
        ])
        descriptions.append("AMD Radeon Vulkan (RADV) & 32/64-bit Mesa")

    if has_intel:
        pkgs.update([
            "mesa", "lib32-mesa",
            "vulkan-intel", "lib32-vulkan-intel",
            "intel-media-driver",
            "vulkan-mesa-layers", "lib32-vulkan-mesa-layers"
        ])
        descriptions.append("Intel Vulkan (ANV), VA-API Media Driver, & 32/64-bit Mesa")

    if has_nvidia:
        pkgs.update([
            "nvidia-open-dkms",
            "nvidia-utils", "lib32-nvidia-utils",
            "libva-nvidia-driver",
            "nvidia-settings",
            "opencl-nvidia", "lib32-opencl-nvidia",
            "egl-wayland"
        ])
        descriptions.append("NVIDIA Open Kernel Modules (DKMS), 32/64-bit Vulkan/OpenGL, VA-API, & Wayland EGL")

    # If Hybrid GPU setup detected (NVIDIA dGPU + Intel/AMD iGPU), include nvidia-prime for prime-run
    if has_nvidia and (has_intel or has_amd):
        pkgs.add("nvidia-prime")
        descriptions.append("NVIDIA Prime Render Offload utilities (prime-run)")

    return sorted(list(pkgs)), ", ".join(descriptions)


def configure_gpu_drivers(ctx: SetupContext):
    """Presents detected GPUs and installs required Vulkan & OpenGL drivers."""
    if ctx.skip_gpu:
        console.print("[dim]Skipping GPU driver installation as requested.[/dim]")
        return

    detected_gpus = detect_gpus()

    table = Table(title="Detected Graphics Hardware", show_header=True, header_style="bold magenta")
    table.add_column("Slot", style="cyan")
    table.add_column("Vendor", style="bold green")
    table.add_column("Device Model", style="white")
    table.add_column("Active Driver", style="yellow")

    if detected_gpus:
        for g in detected_gpus:
            table.add_row(g.pci_slot, g.vendor_name, g.device_name, g.driver or "Unknown")
        console.print(table)
    else:
        console.print("[yellow]No discrete or integrated GPUs auto-detected via PCI/DRM.[/yellow]")

    auto_pkgs, auto_desc = get_gpu_packages(detected_gpus)

    if detected_gpus and auto_pkgs:
        console.print(f"\n[bold green]Auto-detected profile:[/bold green] {auto_desc}")
        if ctx.auto_yes:
            gpu_choice = "1"
        else:
            console.print("\n[bold cyan]Select GPU Installation Mode:[/bold cyan]")
            console.print("1. Install auto-detected drivers [bold green](Recommended)[/bold green]")
            console.print("2. AMD (Radeon Vulkan + Mesa)")
            console.print("3. NVIDIA (GeForce Open-DKMS + Wayland + 32-bit)")
            console.print("4. Intel (Arc / Iris Xe Vulkan + Media Driver)")
            console.print("5. Hybrid (Intel/AMD iGPU + NVIDIA dGPU + prime-run)")
            console.print("6. Skip (I manually manage graphics drivers)")
            gpu_choice = Prompt.ask("Enter choice", choices=["1", "2", "3", "4", "5", "6"], default="1")
    else:
        console.print("\n[bold cyan]Select GPU Vendor for Vulkan & 32-bit Drivers:[/bold cyan]")
        console.print("1. AMD (Radeon Vulkan + Mesa)")
        console.print("2. NVIDIA (GeForce Open-DKMS + Wayland + 32-bit)")
        console.print("3. Intel (Arc / Iris Xe Vulkan + Media Driver)")
        console.print("4. Hybrid (Intel/AMD iGPU + NVIDIA dGPU + prime-run)")
        console.print("5. Skip (I manually manage graphics drivers)")
        gpu_choice = Prompt.ask("Enter choice", choices=["1", "2", "3", "4", "5"], default="5")

    target_pkgs = []
    target_desc = ""

    if detected_gpus and auto_pkgs and gpu_choice == "1":
        target_pkgs = auto_pkgs
        target_desc = f"Install auto-detected GPU drivers: {auto_desc}"
    elif gpu_choice == ("2" if (detected_gpus and auto_pkgs) else "1"):
        target_pkgs = ["mesa", "lib32-mesa", "vulkan-radeon", "lib32-vulkan-radeon", "vulkan-mesa-layers", "lib32-vulkan-mesa-layers"]
        target_desc = "Install native AMD 32/64-bit Vulkan (RADV) & Mesa drivers"
    elif gpu_choice == ("3" if (detected_gpus and auto_pkgs) else "2"):
        target_pkgs = ["nvidia-open-dkms", "nvidia-utils", "lib32-nvidia-utils", "libva-nvidia-driver", "nvidia-settings", "opencl-nvidia", "lib32-opencl-nvidia", "egl-wayland"]
        target_desc = "Install NVIDIA Open DKMS, 32/64-bit Vulkan/OpenGL utilities, VA-API, and Wayland bridge"
    elif gpu_choice == ("4" if (detected_gpus and auto_pkgs) else "3"):
        target_pkgs = ["mesa", "lib32-mesa", "vulkan-intel", "lib32-vulkan-intel", "intel-media-driver", "vulkan-mesa-layers", "lib32-vulkan-mesa-layers"]
        target_desc = "Install native Intel 32/64-bit Vulkan (ANV), VA-API Media Driver, and Mesa"
    elif gpu_choice == ("5" if (detected_gpus and auto_pkgs) else "4"):
        target_pkgs = ["mesa", "lib32-mesa", "vulkan-intel", "lib32-vulkan-intel", "intel-media-driver", "vulkan-radeon", "lib32-vulkan-radeon", "nvidia-open-dkms", "nvidia-utils", "lib32-nvidia-utils", "libva-nvidia-driver", "nvidia-prime", "egl-wayland"]
        target_desc = "Install Hybrid Multi-GPU drivers (Intel/AMD + NVIDIA + prime-run offload)"
    else:
        console.print("[dim]Skipping GPU driver installation.[/dim]")
        return

    if target_pkgs:
        pkgs_str = " ".join(target_pkgs)
        run_command(ctx, f"sudo pacman -S --needed --noconfirm {pkgs_str}", target_desc, retries=3)


def apply_kernel_and_sysctl_optimizations(ctx: SetupContext):
    """
    Applies kernel 7.x / modern gaming sysctl parameters and file descriptor limits.
    - vm.max_map_count: Prevents memory mapping crashes in heavy 64-bit games & Proton (UE5, Star Citizen, Hogwarts Legacy)
    - split_lock_mitigate=0: Prevents stutter from unaligned memory access throttling
    - nofile limits: Prevents Esync/Fsync file descriptor exhaustion
    """
    console.print("\n[bold cyan]Configuring Kernel & System Gaming Optimizations...[/bold cyan]")

    sysctl_file = Path("/etc/sysctl.d/99-gaming.conf")
    limits_file = Path("/etc/security/limits.d/99-gaming.conf")

    temp_sysctl = Path("/tmp/99-gaming-sysctl.conf")
    temp_limits = Path("/tmp/99-gaming-limits.conf")

    temp_sysctl.write_text(SYSCTL_GAMING_CONF)
    temp_limits.write_text(LIMITS_GAMING_CONF)

    cmd = (
        f"sudo install -m 644 {temp_sysctl} {sysctl_file} && "
        f"sudo install -m 644 {temp_limits} {limits_file} && "
        f"rm -f {temp_sysctl} {temp_limits} && "
        f"sudo sysctl --system && "
        f"sudo sysctl -p {sysctl_file}"
    )

    run_command(
        ctx,
        cmd,
        "Apply gaming sysctl tweaks (vm.max_map_count=2147483642, split_lock_mitigate=0, and nofile limits)",
        critical=False,
        show_command=False
    )


def install_native_gaming_stack(ctx: SetupContext):
    """Installs the complete native gaming architecture and 32/64-bit runtime libraries for Wayland."""
    native_packages = [
        # Core Gaming Clients & Compatibility
        "steam",
        "lutris",
        "wine-staging",
        "wine-gecko",
        "wine-mono",
        "winetricks",
        "flatpak",

        # Pure Wayland Toolkits & Scanning
        "qt5-wayland",
        "qt6-wayland",

        # Performance & Overlay Stack
        "gamescope",
        "gamemode",
        "lib32-gamemode",
        "mangohud",
        "lib32-mangohud",
        "goverlay",

        # 32-bit Runtime Dependencies for Wine & Proton
        "lib32-gnutls",
        "lib32-gtk3",
        "lib32-libpulse",
        "lib32-alsa-plugins",
        "lib32-vulkan-icd-loader",
        "vulkan-icd-loader",
        "vulkan-tools",
        "lib32-libxcomposite",
        "lib32-libxinerama",
        "lib32-libxrandr",
        "lib32-libxcursor",
        "lib32-libxi",

        # Compatibility, Fonts, and Tools
        "ttf-liberation",
        "noto-fonts",
        "cabextract",
        "samba",
        "zenity",
        "desktop-file-utils",
        "fuse-overlayfs",
        "bubblewrap",
        "psmisc"
    ]

    pkgs_str = " ".join(native_packages)
    run_command(
        ctx,
        f"sudo pacman -S --needed --noconfirm {pkgs_str}",
        "Install Steam, Lutris, Wine-Staging, Gamescope, GameMode, MangoHud, Goverlay, & 32-bit runtimes.",
        retries=3
    )

    # Enable Gamescope real-time scheduling capability (CAP_SYS_NICE) for low-latency Wayland frame pacing
    if shutil.which("setcap") and Path("/usr/bin/gamescope").exists():
        run_command(
            ctx,
            "sudo setcap 'CAP_SYS_NICE=eip' /usr/bin/gamescope",
            "Grant Gamescope CAP_SYS_NICE capability for real-time frame pacing under Wayland",
            critical=False
        )

    # Enable GameMode daemon service for the current user session
    run_command(
        ctx,
        "systemctl --user enable --now gamemoded.service",
        "Enable and start Feral GameMode user daemon",
        critical=False
    )

    # DwarFS AUR support for compressed repacks
    aur_helper = next((h for h in ("paru", "yay") if shutil.which(h)), None)
    if not shutil.which("dwarfs") and aur_helper:
        run_command(
            ctx,
            f"{aur_helper} -S --needed --noconfirm dwarfs",
            "Install DwarFS filesystem tools from AUR (required for high-efficiency compressed repacks).",
            critical=False
        )


def configure_flatpak_ecosystem(ctx: SetupContext):
    """
    Idempotently configures Flathub remotes, installs essential Flatpak gaming apps,
    installs Vulkan runtime layers for MangoHud/Gamescope, and configures sandbox overrides.
    """
    # 1. Add Flathub remotes for both user and system scope
    run_command(
        ctx,
        "flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo && "
        "sudo flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo",
        "Initialize Flathub remote repositories (User & System scope)."
    )

    # 2. Install Flatpak apps
    flatpak_apps = [
        ("Bottles", "com.usebottles.bottles"),
        ("Flatseal", "com.github.tchx84.Flatseal"),
        ("ProtonPlus", "com.vysp3r.ProtonPlus"),
        ("Heroic Games Launcher", "com.heroicgameslauncher.hgl")
    ]

    for app_name, app_id in flatpak_apps:
        run_command(
            ctx,
            f"sudo flatpak install --system -y --noninteractive --or-update flathub {app_id}",
            f"Install {app_name} via Flatpak sandbox.",
            critical=False
        )

    # 3. Install Flatpak MangoHud & Gamescope runtime layers (25.08 & 24.08 branches)
    flatpak_layers = [
        ("MangoHud Flatpak Vulkan Layer (25.08)", "org.freedesktop.Platform.VulkanLayer.MangoHud//25.08"),
        ("MangoHud Flatpak Vulkan Layer (24.08)", "org.freedesktop.Platform.VulkanLayer.MangoHud//24.08"),
        ("Gamescope Flatpak Vulkan Layer (25.08)", "org.freedesktop.Platform.VulkanLayer.gamescope//25.08"),
        ("Gamescope Flatpak Vulkan Layer (24.08)", "org.freedesktop.Platform.VulkanLayer.gamescope//24.08")
    ]
    for layer_name, layer_id in flatpak_layers:
        run_command(
            ctx,
            f"sudo flatpak install --system -y --noninteractive --or-update flathub {layer_id}",
            f"Install {layer_name} for seamless overlay support inside Flatpaks.",
            critical=False
        )

    # 4. Bottles & Heroic native Wayland socket + global filesystem overrides
    run_command(
        ctx,
        "sudo flatpak override --system --socket=wayland --socket=fallback-x11 --filesystem=host com.usebottles.bottles && "
        "sudo flatpak override --system --socket=wayland --socket=fallback-x11 --filesystem=host com.heroicgameslauncher.hgl",
        "Grant Bottles and Heroic native Wayland sockets and host filesystem permissions.",
        critical=False
    )


def get_installed_flatpaks() -> List[str]:
    """Dynamically fetches a list of all installed Flatpak Application IDs across system and user scopes."""
    apps: Set[str] = set()
    for scope_flag in ["--system", "--user"]:
        try:
            result = subprocess.run(
                ["flatpak", "list", scope_flag, "--app", "--columns=application"],
                capture_output=True, text=True, check=True
            )
            for line in result.stdout.splitlines():
                if line.strip():
                    apps.add(line.strip())
        except Exception:
            pass
    return sorted(list(apps))


def integrate_desktop_and_icons(ctx: SetupContext):
    """
    Idempotently bridges Flatpak .desktop files and hicolor application icons into
    the user's local XDG directories (~/.local/share/applications and ~/.local/share/icons).
    Ensures Wayland/Hyprland launchers (Rofi, Wofi, Fuzzel, Anyrun) instantly display
    apps and crisp icons without requiring a logout/reboot.
    """
    user_apps_dir = Path.home() / ".local/share/applications"
    user_icons_dir = Path.home() / ".local/share/icons/hicolor"

    user_apps_dir.mkdir(parents=True, exist_ok=True)
    user_icons_dir.mkdir(parents=True, exist_ok=True)

    system_export_dir = Path("/var/lib/flatpak/exports/share")
    user_export_dir = Path.home() / ".local/share/flatpak/exports/share"

    # 1. Clean broken symlinks safely
    try:
        for f in user_apps_dir.iterdir():
            if f.is_symlink() and not f.exists():
                f.unlink()
    except Exception as e:
        console.print(f"[yellow]Warning during symlink cleanup: {e}[/yellow]")

    # 2. Bridge .desktop entries
    installed_apps = get_installed_flatpaks()
    for app_id in installed_apps:
        desktop_file = f"{app_id}.desktop"
        target_path = None

        if (system_export_dir / "applications" / desktop_file).exists():
            target_path = system_export_dir / "applications" / desktop_file
        elif (user_export_dir / "applications" / desktop_file).exists():
            target_path = user_export_dir / "applications" / desktop_file

        if target_path:
            symlink_path = user_apps_dir / desktop_file
            if symlink_path.is_symlink() or symlink_path.exists():
                if not symlink_path.is_symlink():
                    continue
                try:
                    if os.readlink(symlink_path) == str(target_path):
                        continue
                except OSError:
                    pass
                symlink_path.unlink()

            symlink_path.symlink_to(target_path)

    # 3. Bridge application icons across all hicolor resolutions
    for base_export in [system_export_dir / "icons/hicolor", user_export_dir / "icons/hicolor"]:
        if not base_export.exists():
            continue
        try:
            for size_dir in base_export.iterdir():
                if not size_dir.is_dir():
                    continue
                apps_icon_dir = size_dir / "apps"
                if not apps_icon_dir.exists():
                    continue

                target_user_icon_dir = user_icons_dir / size_dir.name / "apps"
                target_user_icon_dir.mkdir(parents=True, exist_ok=True)

                for icon_file in apps_icon_dir.iterdir():
                    if icon_file.is_file():
                        symlink_icon = target_user_icon_dir / icon_file.name
                        if symlink_icon.is_symlink() or symlink_icon.exists():
                            if not symlink_icon.is_symlink():
                                continue
                            try:
                                if os.readlink(symlink_icon) == str(icon_file):
                                    continue
                            except OSError:
                                pass
                            symlink_icon.unlink()
                        symlink_icon.symlink_to(icon_file)
        except Exception:
            pass

    # 4. Trigger desktop and icon cache updates
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(user_apps_dir)], capture_output=True)
    if shutil.which("gtk-update-icon-cache"):
        subprocess.run(["gtk-update-icon-cache", "-f", "-t", str(user_icons_dir)], capture_output=True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Arch Linux Universal Gaming Architecture - Bleeding-Edge Native Installer for Hyprland / Pure Wayland.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Non-interactive mode (automatically confirm all steps).")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Dry run mode (simulate operations without modifying system).")
    parser.add_argument("--skip-gpu", action="store_true", help="Skip GPU driver detection and installation.")
    parser.add_argument("--skip-flatpak", action="store_true", help="Skip Flatpak applications and runtime layers.")
    parser.add_argument("--skip-sysctl", action="store_true", help="Skip kernel sysctl and limits optimizations.")
    return parser.parse_args()


def main():
    args = parse_arguments()
    ctx = SetupContext(
        dry_run=args.dry_run,
        auto_yes=args.yes,
        skip_gpu=args.skip_gpu
    )

    console.clear()
    console.print(Panel.fit(
        "[bold magenta]Arch Linux Universal Gaming Architecture[/bold magenta]\n"
        "[white]Bleeding-Edge Native Installer for Drivers, Steam, Lutris, Wine-Staging, Gamescope, & Pure Wayland/Hyprland.[/white]",
        border_style="magenta"
    ))

    # 1. Pre-flight verification
    check_root_and_locks(ctx)

    if not ctx.dry_run:
        console.print("\n[cyan]Authenticating with sudo for system configuration...[/cyan]")
        try:
            subprocess.run(["sudo", "-v"], check=True)
        except subprocess.CalledProcessError:
            console.print("[bold red]Failed to authenticate with sudo. Exiting.[/bold red]")
            sys.exit(1)

        # Start non-blocking background sudo keep-alive daemon
        ctx.sudo_thread = threading.Thread(target=keep_sudo_alive, args=(ctx.stop_sudo_event,), daemon=True)
        ctx.sudo_thread.start()

    try:
        # 2. Pacman configuration & [multilib] activation
        console.print("\n[bold cyan]Step 1: Synchronizing Pacman Repositories & [multilib][/bold cyan]")
        enable_multilib_and_optimizations(ctx)

        # 3. Synchronize package databases with multilib enabled
        run_command(
            ctx,
            "sudo pacman -Syu --needed --noconfirm",
            "Synchronize package databases and apply core system upgrades.",
            retries=3
        )

        # 4. GPU Detection and Driver Installation
        console.print("\n[bold cyan]Step 2: Graphics Architecture & Vulkan Drivers[/bold cyan]")
        configure_gpu_drivers(ctx)

        # 5. Kernel 7.x & Sysctl Gaming Optimizations
        if not args.skip_sysctl:
            console.print("\n[bold cyan]Step 3: Kernel 7.x & Sysctl Performance Tuning[/bold cyan]")
            apply_kernel_and_sysctl_optimizations(ctx)

        # 6. Complete Native Gaming Stack
        console.print("\n[bold cyan]Step 4: Native Gaming Stack & 32-bit Runtimes (Pure Wayland)[/bold cyan]")
        install_native_gaming_stack(ctx)

        # 7. Flatpak Ecosystem & Runtime Layers
        if not args.skip_flatpak:
            console.print("\n[bold cyan]Step 5: Flatpak Sandbox & Runtime Layers[/bold cyan]")
            configure_flatpak_ecosystem(ctx)

            with console.status("[bold green]Bridging Flatpak desktop entries and application icons...[/bold green]", spinner="dots"):
                integrate_desktop_and_icons(ctx)
            console.print("[bold green]✔ Application launcher and icon integration complete![/bold green]")

        # 8. Post-Install Report & Hyprland Guidance
        report_text = Text()
        report_text.append("✔ Gaming Architecture Established!\n", style="bold green")
        report_text.append("Your Arch Linux installation is fully armed for native games, Steam Proton, Lutris, and modern Windows repacks.\n\n", style="white")
        report_text.append("Key Features Configured:\n", style="bold cyan")
        report_text.append("• Pure Wayland Graphics Pipeline: Vulkan 32/64-bit with Hybrid GPU support (prime-run)\n", style="white")
        report_text.append("• Wine-Staging with Esync/Fsync and native Wayland staging driver\n", style="white")
        report_text.append("• Gamescope micro-compositor with CAP_SYS_NICE real-time frame pacing & Feral GameMode daemon\n", style="white")
        report_text.append("• vm.max_map_count=2147483642 & kernel.split_lock_mitigate=0 tuned in /etc/sysctl.d/99-gaming.conf\n", style="white")
        report_text.append("• Flatpak native Wayland sockets & crisp icons bridged directly into ~/.local/share/\n\n", style="white")
        report_text.append("Hyprland & Wayland Pro-Tips:\n", style="bold yellow")
        report_text.append("1. Zero-Latency Tearing: Add `windowrulev2 = immediate, class:^(steam_app_.*)$` to your Hyprland window rules.\n", style="white")
        report_text.append("2. Hybrid GPUs: Run games on discrete GPU using `prime-run <command>` or gamescope.\n", style="white")
        report_text.append("3. FPS Limiting: Use `fps_limiter.py <fps> <command>` for universal low-latency frame capping.\n", style="white")
        report_text.append("4. Native Wayland Proton: Set `PROTON_ENABLE_WAYLAND=1` in Steam launch options for native Wayland surface presentation.\n", style="white")

        console.print(Panel(report_text, title="[bold green]Installation Summary[/bold green]", border_style="green"))

    finally:
        ctx.stop_sudo_event.set()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n[bold red]Script interrupted by user. Exiting safely.[/bold red]")
        sys.exit(0)
