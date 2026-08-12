#!/usr/bin/env python3

import sys
from pathlib import Path

_dusky_root = Path.home() / "user_scripts" / "dusky_tui"
if str(_dusky_root) not in sys.path:
    sys.path.insert(0, str(_dusky_root))

from python.frontend.core_types import ConfigItem

# =============================================================================
# 1. CORE APPLICATION ROUTING
# =============================================================================
ENGINE_TYPE = "autostart"
TARGET_FILE = "~/.config/hypr/edit_here/source/autostart.lua"
APP_TITLE = "Autostart & Services"

# =============================================================================
# 2. UI & ENVIRONMENT BEHAVIOR
# =============================================================================
DEFAULT_MODE = "auto"
THEME_FILE = "~/.config/matugen/generated/dusky_tui.json"
ENABLE_USER_PRESETS = True
USER_PRESETS_TAB = "Profiles"

# =============================================================================
# 3. TABS DEFINITION
# =============================================================================
TABS = [
    "System",
    "Autostart Services",
    "Quick Actions",
    "Profiles"
]

# =============================================================================
# 4. SCHEMA DEFINITION
# =============================================================================
SCHEMA = {
    # -------------------------------------------------------------------------
    # TAB 0: System Configuration (AST mapped natively to hl.config)
    # -------------------------------------------------------------------------
    0: [
        ConfigItem(
            label="Enable XWayland Subsystem",
            key="enabled",
            scope="xwayland",
            type_="bool",
            default=True,
            group="Compatibility",
            extended_help="**XWayland Support**\n\nToggles the XWayland translation layer globally.\n\n- **ON**: Better compatibility for older X11 applications.\n- **OFF**: Disables the layer to save 20-30 MB of RAM, but strictly prevents non-Wayland applications from functioning."
        ),
    ],

    # -------------------------------------------------------------------------
    # TAB 1: Autostart Services (Booleans controlling autostart.lua execution)
    # -------------------------------------------------------------------------
    1: [
        # --- Interface & Background Services ---
        ConfigItem(
            label="Wallpaper Engine (awww-daemon)",
            key="awww_daemon",
            scope="autostart",
            type_="bool",
            default=True,
            group="Interface & Background",
            extended_help="**Wallpaper Daemon**\n\nAutomatically launches `awww-daemon` on login to render desktop wallpapers."
        ),
        ConfigItem(
            label="Status Bar (Waybar)",
            key="waybar",
            scope="autostart",
            type_="bool",
            default=True,
            group="Interface & Background",
            extended_help="**Waybar Status Bar**\n\nAutomatically launches the Waybar panel on startup."
        ),
        ConfigItem(
            label="Waybar Productivity Timer",
            key="waybar_timer",
            scope="autostart",
            type_="bool",
            default=False,
            group="Interface & Background",
            extended_help="**Waybar Pomodoro Timer**\n\nAutomatically launches the pomodoro timer module on Waybar startup."
        ),
        ConfigItem(
            label="Network Manager Applet (nm-applet)",
            key="nm_applet",
            scope="autostart",
            type_="bool",
            default=False,
            group="Interface & Background",
            extended_help="**Network Tray Applet**\n\nLaunches the NetworkManager tray applet automatically."
        ),
        ConfigItem(
            label="Wallpaper Audio Visualizer",
            key="audio_visualizer",
            scope="autostart",
            type_="bool",
            default=False,
            group="Interface & Background",
            extended_help="**Audio Visualizer Layer**\n\nLaunches background audio visualizer on boot."
        ),

        # --- System Daemons & Security ---
        ConfigItem(
            label="Gnome Keyring Secrets Daemon",
            key="gnome_keyring",
            scope="autostart",
            type_="bool",
            default=False,
            group="Daemons & Services",
            extended_help="**Gnome Keyring**\n\nLaunches the Gnome Keyring secrets daemon for storing application credentials."
        ),
        ConfigItem(
            label="Hyprland Idle Manager (hypridle)",
            key="hypridle",
            scope="autostart",
            type_="bool",
            default=False,
            group="Daemons & Services",
            extended_help="**Hypridle Daemon**\n\nLaunches Hyprland's idle management service for screen locking and dimming."
        ),
        ConfigItem(
            label="Keyboard Layout Notifier",
            key="layout_notify",
            scope="autostart",
            type_="bool",
            default=False,
            group="Daemons & Services",
            extended_help="**Layout Notifier**\n\nRuns keyboard layout notification script on startup."
        ),
        ConfigItem(
            label="Grant Root XHost Display Access",
            key="xhost_root",
            scope="autostart",
            type_="bool",
            default=False,
            group="Daemons & Services",
            extended_help="**Root XHost Access**\n\nGrants root access to the display server (needed for GUI administrative tools)."
        ),
        ConfigItem(
            label="Hyprland Plugin Manager (hyprpm reload)",
            key="hyprpm_reload",
            scope="autostart",
            type_="bool",
            default=False,
            group="Daemons & Services",
            extended_help="**Hyprpm Reload**\n\nReloads Hyprland plugins automatically upon startup."
        ),

        # --- Clipboard Services ---
        ConfigItem(
            label="Cliphist Text History Listener",
            key="cliphist_text",
            scope="autostart",
            type_="bool",
            default=False,
            group="Clipboard Services",
            extended_help="**Cliphist Text**\n\nStarts text clipboard history listener on login."
        ),
        ConfigItem(
            label="Cliphist Image History Listener",
            key="cliphist_image",
            scope="autostart",
            type_="bool",
            default=False,
            group="Clipboard Services",
            extended_help="**Cliphist Image**\n\nStarts image clipboard history listener on login."
        ),
        ConfigItem(
            label="Cliphist Custom DB Text Listener",
            key="cliphist_db_text",
            scope="autostart",
            type_="bool",
            default=False,
            group="Clipboard Services",
            extended_help="**Cliphist Custom DB Text**\n\nStarts text clipboard listener using custom database environment."
        ),
        ConfigItem(
            label="Cliphist Custom DB Image Listener",
            key="cliphist_db_image",
            scope="autostart",
            type_="bool",
            default=False,
            group="Clipboard Services",
            extended_help="**Cliphist Custom DB Image**\n\nStarts image clipboard listener using custom database environment."
        ),
        ConfigItem(
            label="Clipboard Persistence (wl-clip-persist)",
            key="clip_persist",
            scope="autostart",
            type_="bool",
            default=False,
            group="Clipboard Services",
            extended_help="**Clipboard Persistence**\n\nEnsures copied selection remains active even if source app exits."
        ),

        # --- Environment Integration ---
        ConfigItem(
            label="Import Systemd Environment",
            key="systemd_env",
            scope="autostart",
            type_="bool",
            default=False,
            group="Environment",
            extended_help="**Systemd Environment**\n\nImports current environment variables into systemd user instance."
        ),
        ConfigItem(
            label="Update DBus Activation Environment",
            key="dbus_env",
            scope="autostart",
            type_="bool",
            default=False,
            group="Environment",
            extended_help="**DBus Environment**\n\nUpdates DBus activation environment with systemd variables."
        ),

        # --- Dusky Glance Dashboards Autostart ---
        ConfigItem(
            label="Autostart Glance: CPU Usage",
            key="glance_cpu",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**CPU Glance Autostart**\n\nLaunches Rofi CPU monitoring overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: CPU Power Draw",
            key="glance_cpu_power",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**CPU Power Glance Autostart**\n\nLaunches CPU power consumption overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Memory (RAM)",
            key="glance_ram",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**RAM Glance Autostart**\n\nLaunches RAM usage overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: RAM Temperature",
            key="glance_ram_temp",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**RAM Temp Glance Autostart**\n\nLaunches memory temperature overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: ZRAM Usage",
            key="glance_zram",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**ZRAM Glance Autostart**\n\nLaunches ZRAM compression overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Temperatures",
            key="glance_temp",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Temperature Glance Autostart**\n\nLaunches CPU/GPU thermal overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Battery Status",
            key="glance_battery",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Battery Glance Autostart**\n\nLaunches battery status overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Battery Percent",
            key="glance_battery_percent",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Battery Percent Glance Autostart**\n\nLaunches battery percentage overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Battery Power Draw",
            key="glance_battery_watts",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Battery Power Draw Autostart**\n\nLaunches battery power draw overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Battery Time Remaining",
            key="glance_battery_time",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Battery Time Remaining Autostart**\n\nLaunches battery time remaining overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: GPU Power Draw",
            key="glance_gpu_power",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**GPU Power Draw Autostart**\n\nLaunches GPU power draw overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: GPU Usage",
            key="glance_gpu_usage",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**GPU Usage Autostart**\n\nLaunches GPU utilization overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: GPU Memory",
            key="glance_gpu_mem",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**GPU Memory Autostart**\n\nLaunches GPU VRAM usage overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Network Bandwidth",
            key="glance_network",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Network Glance Autostart**\n\nLaunches network bandwidth overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: System Uptime",
            key="glance_uptime",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Uptime Glance Autostart**\n\nLaunches system uptime overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Workspace Overview",
            key="glance_workspace",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Workspace Glance Autostart**\n\nLaunches workspace overview overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Disk Usage",
            key="glance_disk",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Disk Glance Autostart**\n\nLaunches disk usage overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Disk Read Activity",
            key="glance_disk_read",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Disk Read Autostart**\n\nLaunches disk read activity overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Disk Write Activity",
            key="glance_disk_write",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Disk Write Autostart**\n\nLaunches disk write activity overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Disk Temperature",
            key="glance_disk_temp",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Disk Temp Autostart**\n\nLaunches disk temperature overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Clock & Calendar",
            key="glance_clock",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Clock Glance Autostart**\n\nLaunches clock and calendar overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Compact Clock",
            key="glance_clock_short",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Compact Clock Glance Autostart**\n\nLaunches minimal clock overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Live Stopwatch",
            key="glance_stopwatch",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Stopwatch Glance Autostart**\n\nLaunches live stopwatch counter at startup."
        ),
        ConfigItem(
            label="Autostart Glance: Countdown Timer (15m)",
            key="glance_timer",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**Timer Glance Autostart**\n\nLaunches 15-minute countdown timer overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: GPU HUD Overlay",
            key="glance_hud",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**GPU HUD Autostart**\n\nLaunches GPU HUD overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: World Clock (New York)",
            key="glance_world_ny",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**World Clock NY Autostart**\n\nLaunches New York world clock overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: World Clock (Tokyo)",
            key="glance_world_tokyo",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**World Clock Tokyo Autostart**\n\nLaunches Tokyo world clock overlay at startup."
        ),
        ConfigItem(
            label="Autostart Glance: World Clock (London)",
            key="glance_world_london",
            scope="autostart",
            type_="bool",
            default=False,
            group="Dusky Glance Dashboards (Autostart)",
            extended_help="**World Clock London Autostart**\n\nLaunches London world clock overlay at startup."
        ),
    ],

    # -------------------------------------------------------------------------
    # TAB 2: Quick Actions (Manual Execution Triggers)
    # -------------------------------------------------------------------------
    2: [
        # --- Dusky Glance Dashboards ---
        ConfigItem(
            label="Dusky Glance Dashboards",
            key="menu_dusky_glance",
            scope="DEFAULT",
            type_="menu",
            default=None,
            is_parent=True,
            expanded=False,
            group="Monitors",
            extended_help="**Dusky Glance Modules**\n\nLaunch various system monitoring overlays directly via Rofi."
        ),
        ConfigItem(
            label="Glance: CPU Usage",
            key="action_glance_cpu",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --cpu",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**CPU Glance**\n\nExecutes Rofi CPU utilization overlay."
        ),
        ConfigItem(
            label="Glance: CPU Power",
            key="action_glance_cpu_power",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --cpu-power",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**CPU Power Glance**\n\nExecutes Rofi CPU power consumption overlay."
        ),
        ConfigItem(
            label="Glance: Memory (RAM)",
            key="action_glance_ram",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --ram",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**RAM Glance**\n\nExecutes Rofi memory usage overlay."
        ),
        ConfigItem(
            label="Glance: RAM Temperature",
            key="action_glance_ram_temp",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --ram-temp",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**RAM Temp Glance**\n\nExecutes Rofi memory thermal overlay."
        ),
        ConfigItem(
            label="Glance: ZRAM Usage",
            key="action_glance_zram",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --zram",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**ZRAM Glance**\n\nExecutes Rofi ZRAM compression overlay."
        ),
        ConfigItem(
            label="Glance: Temperatures",
            key="action_glance_temp",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --temp",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Temperature Glance**\n\nExecutes Rofi thermal metrics overlay."
        ),
        ConfigItem(
            label="Glance: Battery Status",
            key="action_glance_battery",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --battery",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Battery Glance**\n\nExecutes Rofi battery state overlay."
        ),
        ConfigItem(
            label="Glance: Network",
            key="action_glance_network",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --network",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Network Glance**\n\nExecutes Rofi network connection overlay."
        ),
        ConfigItem(
            label="Glance: System Uptime",
            key="action_glance_uptime",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --uptime",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Uptime Glance**\n\nExecutes Rofi system uptime overlay."
        ),
        ConfigItem(
            label="Glance: Workspace",
            key="action_glance_workspace",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --workspace",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Workspace Glance**\n\nExecutes Rofi workspace overview."
        ),
        ConfigItem(
            label="Glance: Disk Usage",
            key="action_glance_disk",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --disk",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Disk Glance**\n\nExecutes Rofi root partition usage overlay."
        ),
        ConfigItem(
            label="Glance: Clock",
            key="action_glance_clock",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --clock",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Clock Glance**\n\nExecutes Rofi clock and calendar overlay."
        ),
        ConfigItem(
            label="Glance: Stopwatch",
            key="action_glance_stopwatch",
            scope="DEFAULT",
            type_="action",
            default="~/user_scripts/rofi/dusky_glance.sh --stopwatch",
            parent_ref="menu_dusky_glance",
            group="Monitors",
            extended_help="**Stopwatch Glance**\n\nExecutes Rofi live stopwatch counter."
        ),

        # --- Manual Execution Actions ---
        ConfigItem(
            label="Launch / Reload Waybar",
            key="action_launch_waybar",
            scope="DEFAULT",
            type_="action",
            default="hypr-app $HOME/user_scripts/waybar/waybar_toggle.sh",
            group="Interface",
            extended_help="**Waybar Controller**\n\nManually launches/reloads the Waybar status panel."
        ),
        ConfigItem(
            label="Toggle Waybar Timer",
            key="action_toggle_timer",
            scope="DEFAULT",
            type_="action",
            default="hypr-app $HOME/user_scripts/waybar/toggle_timer_waybar.sh",
            group="Interface",
            extended_help="**Waybar Timer**\n\nToggles the pomodoro productivity timer."
        ),
        ConfigItem(
            label="Start Wallpaper Engine",
            key="action_launch_awww",
            scope="DEFAULT",
            type_="action",
            default="hypr-app awww-daemon",
            group="Interface",
            extended_help="**Wallpaper Engine**\n\nManually starts `awww-daemon`."
        ),
        ConfigItem(
            label="Start Network Applet",
            key="action_nm_applet",
            scope="DEFAULT",
            type_="action",
            default="hypr-app nm-applet",
            group="Interface",
            extended_help="**Network Manager Applet**\n\nManually launches nm-applet tray icon."
        ),
        ConfigItem(
            label="Start Gnome Keyring Daemon",
            key="action_gnome_keyring",
            scope="DEFAULT",
            type_="action",
            default="hypr-app /usr/bin/gnome-keyring-daemon --start --components=secrets",
            group="Services",
            extended_help="**Gnome Keyring**\n\nManually launches Gnome Keyring daemon."
        ),
        ConfigItem(
            label="Grant Root XHost Access",
            key="action_xhost_root",
            scope="DEFAULT",
            type_="action",
            default="hypr-app xhost +si:localuser:root",
            group="Services",
            extended_help="**XHost Root Access**\n\nGrants root access to the display server."
        ),
        ConfigItem(
            label="Start Hypridle (Idle Manager)",
            key="action_hypridle",
            scope="DEFAULT",
            type_="action",
            default="hypr-app hypridle",
            group="Services",
            extended_help="**Hypridle**\n\nManually starts Hyprland idle daemon."
        ),
        ConfigItem(
            label="Start Layout Notifier",
            key="action_layout_notify",
            scope="DEFAULT",
            type_="action",
            default="hypr-app $HOME/user_scripts/hypr/layout_notify.sh",
            group="Services",
            extended_help="**Layout Notifier**\n\nManually launches layout notifier script."
        ),
        ConfigItem(
            label="Update Systemd Environment",
            key="action_systemd_env",
            scope="DEFAULT",
            type_="action",
            default="systemctl --user import-environment $(env | cut -d'=' -f 1)",
            group="Environment",
            extended_help="**Systemd Environment**\n\nImports current environment into systemd."
        ),
        ConfigItem(
            label="Update DBus Environment",
            key="action_dbus_env",
            scope="DEFAULT",
            type_="action",
            default="dbus-update-activation-environment --systemd --all",
            group="Environment",
            extended_help="**DBus Environment**\n\nUpdates DBus activation environment."
        ),
    ],

    # -------------------------------------------------------------------------
    # TAB 3: Profiles (System Presets)
    # -------------------------------------------------------------------------
    3: [
        ConfigItem(
            label="Deploy Lightweight Mode",
            key="preset_lightweight_mode",
            scope="DEFAULT",
            type_="preset",
            default=None,
            group="Optimization",
            preset_payload={
                "xwayland.enabled": False,
                "autostart.audio_visualizer": False,
                "autostart.waybar_timer": False
            },
            extended_help="**Lightweight Preset**\n\nOptimizes RAM usage by aggressively disabling XWayland and non-essential background layers."
        ),
        ConfigItem(
            label="Restore Standard Defaults",
            key="preset_restore_defaults",
            scope="DEFAULT",
            type_="preset",
            default=None,
            group="Optimization",
            preset_payload={
                "xwayland.enabled": True,
                "autostart.waybar": True,
                "autostart.awww_daemon": True
            },
            extended_help="**Standard Defaults**\n\nRe-enables standard desktop services and XWayland compatibility layer."
        ),
    ]
}

# =============================================================================
# DIRECT EXECUTION HANDLER
# =============================================================================
if __name__ == "__main__":
    import sys, subprocess
    from pathlib import Path

    script_path = Path(__file__).resolve()
    main_router = Path.home() / "user_scripts" / "dusky_tui" / "python" / "main" / "main.py"

    if main_router.exists():
        sys.exit(subprocess.run([sys.executable, str(main_router), str(script_path)] + sys.argv[1:]).returncode)
    else:
        print(f"[-] Error: Main Dusky TUI router not found at {main_router}", file=sys.stderr)
        sys.exit(1)
