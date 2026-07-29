#!/usr/bin/env python3
"""
===============================================================================
DUSKY TUI: FONTCONFIG SCHEMA
===============================================================================
Target: ~/.config/fontconfig/conf.d/99-dusky-fonts.conf
Engine: Fontconfig XML Serializer
===============================================================================
"""

import sys
from pathlib import Path

# Inject Dusky TUI root into Python path for standalone execution
_DUSKY_TUI_ROOT = Path.home() / "user_scripts" / "dusky_tui"
if str(_DUSKY_TUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_DUSKY_TUI_ROOT))

from python.frontend.core_types import ConfigItem

# =============================================================================
# 1. CORE APPLICATION ROUTING
# =============================================================================
ENGINE_TYPE = "fontconfig"
TARGET_FILE = "~/.config/fontconfig/conf.d/99-dusky-fonts.conf"
APP_TITLE = "Font Manager"

# =============================================================================
# 2. UI & ENVIRONMENT BEHAVIOR
# =============================================================================
DEFAULT_MODE = "auto"
ENABLE_USER_PRESETS = True
USER_PRESETS_TAB = "Profiles"
REQUIRE_ROOT = False

# =============================================================================
# 3. TABS DEFINITION
# =============================================================================
TABS = [
    "Typefaces",
    "Rendering Options",
    "System & Cache",
    "Profiles"
]

# =============================================================================
# 4. SCHEMA DEFINITION
# =============================================================================
SCHEMA = {
    # -------------------------------------------------------------------------
    # TAB 0: TYPEFACES
    # -------------------------------------------------------------------------
    0: [
        ConfigItem(
            label="System Sans-Serif Font",
            key="sans-serif",
            scope="DEFAULT",
            type_="picker",
            default="Atkinson Hyperlegible",
            options=[
                "Atkinson Hyperlegible", "Inter", "Roboto", "Noto Sans", 
                "Fira Sans", "Cantarell", "Ubuntu", "Open Sans", "DejaVu Sans"
            ],
            hints=[
                "High legibility (Braille Inst.)", "Modern, crisp UI", 
                "Classic Android UI", "Excellent global coverage", 
                "Mozilla's UI font", "GNOME default", 
                "Canonical's UI font", "Friendly & readable", "Classic open-source default"
            ],
            extended_help="Sets the primary Sans-Serif font used across the desktop environment (e.g., Waybar, Hyprland, GTK apps)."
        ),
        ConfigItem(
            label="System Serif Font",
            key="serif",
            scope="DEFAULT",
            type_="picker",
            default="Noto Serif",
            options=[
                "Noto Serif", "Merriweather", "PT Serif", 
                "Liberation Serif", "DejaVu Serif", "Georgia", "Ubuntu"
            ],
            hints=[
                "Excellent global coverage", "Highly readable for screens", 
                "Elegant and modern", "Times New Roman compatible", 
                "Wider proportions", "Classic web font", "Canonical's alternative"
            ],
            extended_help="Sets the primary Serif font (characterized by decorative feet). Mostly utilized in browsers and document viewers."
        ),
        ConfigItem(
            label="System Monospace Font",
            key="monospace",
            scope="DEFAULT",
            type_="picker",
            default="FiraCode Nerd Font",
            options=[
                "FiraCode Nerd Font", "JetBrainsMono Nerd Font", "Hack Nerd Font", 
                "MesloLGS NF", "Cascadia Code", "Iosevka Nerd Font", 
                "Source Code Pro", "Ubuntu Mono"
            ],
            hints=[
                "Popular with ligatures", "JetBrains IDE default", 
                "Workhorse terminal font", "Recommended for Powerlevel10k", 
                "Microsoft's coding font", "Highly customizable, narrow", 
                "Adobe's open source mono", "Canonical's mono font"
            ],
            extended_help="Sets the primary fixed-width font. Requires standard patched Nerd Fonts installed for icon rendering in terminals."
        ),
        ConfigItem(
            label="System Emoji Font",
            key="emoji",
            scope="DEFAULT",
            type_="picker",
            default="Noto Color Emoji",
            options=[
                "Noto Color Emoji", "Twemoji", "JoyPixels", "Apple Color Emoji"
            ],
            hints=[
                "Google's standard (Recommended)", "Twitter's flat design",
                "Highly detailed/glossy", "macOS port (Requires AUR)"
            ],
            extended_help="Forces the system-wide fallback for emoji rendering. Prevents rendering conflicts where fonts fail to display colored emojis."
        )
    ],
    
    # -------------------------------------------------------------------------
    # TAB 1: RENDERING OPTIONS
    # -------------------------------------------------------------------------
    1: [
        ConfigItem(
            label="Enable Antialiasing",
            key="antialias",
            scope="DEFAULT",
            type_="bool",
            default=True,
            extended_help="Smooths the jagged edges of fonts. Highly recommended for modern high-DPI and standard displays."
        ),
        ConfigItem(
            label="Enable Font Hinting",
            key="hinting",
            scope="DEFAULT",
            type_="bool",
            default=True,
            extended_help="Master switch for font hinting. When enabled, FreeType aligns font glyphs to pixel boundaries for sharp text rendering."
        ),
        ConfigItem(
            label="Hinting Style",
            key="hintstyle",
            scope="DEFAULT",
            type_="picker",
            default="hintslight",
            options=["hintnone", "hintslight", "hintmedium", "hintfull"],
            hints=[
                "No pixel alignment", "Light alignment (Recommended)", 
                "Medium alignment", "Strict pixel alignment"
            ],
            extended_help="Controls how font outlines are aligned to the screen's pixel grid. 'hintslight' is heavily recommended for FreeType on modern Arch Linux."
        ),
        ConfigItem(
            label="Subpixel Geometry (RGBA)",
            key="rgba",
            scope="DEFAULT",
            type_="picker",
            default="rgb",
            options=["none", "rgb", "bgr", "vrgb", "vbgr"],
            hints=[
                "Grayscale smoothing", "Standard horizontal (Most common)", 
                "Reversed horizontal", "Standard vertical", "Reversed vertical"
            ],
            extended_help="Configures subpixel rendering for LCD displays. 'rgb' is correct for 99% of modern desktop monitors."
        ),
        ConfigItem(
            label="LCD Filter",
            key="lcdfilter",
            scope="DEFAULT",
            type_="picker",
            default="lcddefault",
            options=["lcdnone", "lcddefault", "lcdlight", "lcdlegacy"],
            hints=[
                "No color fringe filter", "Standard filter (Recommended)", 
                "Light fringe filter", "Legacy FreeType filter"
            ],
            extended_help="Reduces color fringing when using subpixel rendering. 'lcddefault' ensures optimal text clarity."
        ),
        ConfigItem(
            label="Enable Embedded Bitmaps",
            key="embeddedbitmap",
            scope="DEFAULT",
            type_="bool",
            default=False,
            extended_help="Controls whether fonts with embedded bitmap glyphs display bitmaps at small sizes. Disabling this forces scalable vector glyphs, preventing pixelated text rendering."
        )
    ],

    # -------------------------------------------------------------------------
    # TAB 2: SYSTEM & CACHE
    # -------------------------------------------------------------------------
    2: [
        ConfigItem(
            label="Force Verbose Cache Rebuild",
            key="trigger_refresh",
            scope="DEFAULT",
            type_="action",
            default="fc-cache -fv",
            options=["trigger"],
            force_interactive=True,
            confirm_message="Are you sure you want to manually rebuild the font cache? This may take several seconds.",
            extended_help="Executes `fc-cache -fv` to force an immediate, verbose rebuild of the system font cache, bypassing the background refresh."
        ),
        ConfigItem(
            label="Verify Sans-Serif Font Resolution",
            key="trigger_verify_sans",
            scope="DEFAULT",
            type_="action",
            default="fc-match 'sans-serif'",
            options=["trigger"],
            popup_message="Check status bar for the test output.",
            extended_help="Executes `fc-match sans-serif` to verify which exact font file the system resolves for sans-serif requests."
        ),
        ConfigItem(
            label="Verify Monospace Font Resolution",
            key="trigger_verify_mono",
            scope="DEFAULT",
            type_="action",
            default="fc-match 'monospace'",
            options=["trigger"],
            popup_message="Check status bar for the test output.",
            extended_help="Executes `fc-match monospace` to verify which exact font file the system resolves for terminal/code requests."
        ),
        ConfigItem(
            label="Verify Arial Aliasing Fallback",
            key="trigger_verify",
            scope="DEFAULT",
            type_="action",
            default="fc-match 'Arial'",
            options=["trigger"],
            popup_message="Check status bar for the test output.",
            extended_help="Executes a test match to verify which exact font file the system currently falls back to when 'Arial' is requested."
        )
    ],
    
    # -------------------------------------------------------------------------
    # TAB 3: PROFILES (Presets)
    # -------------------------------------------------------------------------
    3: [
        ConfigItem(
            label="Apply Modern Sharp UI Profile",
            key="preset_modern_sharp",
            scope="DEFAULT",
            type_="preset",
            default=None,
            group="System Defaults",
            preset_payload={
                "sans-serif": "Inter",
                "serif": "Noto Serif",
                "monospace": "JetBrainsMono Nerd Font",
                "emoji": "Noto Color Emoji",
                "antialias": True,
                "hinting": True,
                "hintstyle": "hintslight",
                "rgba": "rgb",
                "lcdfilter": "lcddefault",
                "embeddedbitmap": False
            },
            extended_help="**Modern Sharp UI**\n\nApplies highly modern, crisp fonts (Inter, JetBrainsMono) with standard RGB subpixel rendering and slight hinting. Ideal for high-resolution standard monitors."
        ),
        ConfigItem(
            label="Apply Accessibility & Legibility Profile",
            key="preset_accessibility",
            scope="DEFAULT",
            type_="preset",
            default=None,
            group="System Defaults",
            preset_payload={
                "sans-serif": "Atkinson Hyperlegible",
                "serif": "Merriweather",
                "monospace": "FiraCode Nerd Font",
                "emoji": "Noto Color Emoji",
                "antialias": True,
                "hinting": True,
                "hintstyle": "hintslight",
                "embeddedbitmap": False
            },
            extended_help="**Accessibility Focus**\n\nPrioritizes character distinction using Atkinson Hyperlegible (developed by the Braille Institute) to prevent visual confusion between similar characters like '1', 'l', and 'I'."
        ),
        ConfigItem(
            label="Apply 4K / High-DPI Clean Profile",
            key="preset_hidpi_clean",
            scope="DEFAULT",
            type_="preset",
            default=None,
            group="System Defaults",
            preset_payload={
                "sans-serif": "Roboto",
                "serif": "Noto Serif",
                "monospace": "Iosevka Nerd Font",
                "emoji": "Noto Color Emoji",
                "antialias": True,
                "hinting": True,
                "hintstyle": "hintnone",
                "rgba": "none",
                "lcdfilter": "lcdnone",
                "embeddedbitmap": False
            },
            extended_help="**High-DPI / 4K Clean Profile**\n\nOptimized for 4K and Retina-class displays. Disables subpixel LCD geometry (`rgba=none`) and pixel grid alignment (`hintstyle=hintnone`) for ultra-clean pure vector outline rendering."
        ),
        ConfigItem(
            label="Apply Legacy Linux Defaults",
            key="preset_legacy_linux",
            scope="DEFAULT",
            type_="preset",
            default=None,
            group="System Defaults",
            preset_payload={
                "sans-serif": "DejaVu Sans",
                "serif": "DejaVu Serif",
                "monospace": "Hack Nerd Font",
                "emoji": "Noto Color Emoji",
                "antialias": True,
                "hinting": True,
                "hintstyle": "hintfull",
                "embeddedbitmap": False
            },
            extended_help="**Legacy Linux Config**\n\nRestores the classic open-source desktop appearance utilizing the DejaVu font family alongside strict/full hinting pixel alignment."
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
    # Route execution to the main Dusky TUI router
    main_router = Path.home() / "user_scripts" / "dusky_tui" / "python" / "main" / "main.py"

    if main_router.exists():
        sys.exit(subprocess.run([sys.executable, str(main_router), str(script_path)] + sys.argv[1:]).returncode)
    else:
        print(f"[-] Error: Main Dusky TUI router not found at {main_router}", file=sys.stderr)
        sys.exit(1)
