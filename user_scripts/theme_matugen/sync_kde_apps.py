#!/usr/bin/env python3
"""
===============================================================================
DUSKY THEME: KDE & KF6 APPLICATION COLOR SCHEME SYNCHRONIZER
===============================================================================
Ensures that all KDE Frameworks 6 applications (Dolphin, Kate, Gwenview,
Okular, Ark, etc.) have their [UiSettings] and [General] color schemes pinned
to 'Matugen' so they dynamically reflect the current Material You palette.
===============================================================================
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

KDE_APP_CONFIGS = (
    "dolphinrc",
    "katerc",
    "kwrite_config",
    "gwenviewrc",
    "okularrc",
    "arkrc",
    "kcalcrc",
    "konsolerc",
)


def patch_ini_group(content: str, group_name: str, entries: dict[str, str]) -> str:
    """Safely updates or injects key-value pairs into a specific INI group."""
    lines = content.splitlines()
    out = []
    in_group = False
    replaced = set()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_group:
                # Append missing keys for this group before moving to next group
                for k, v in entries.items():
                    if k not in replaced:
                        out.append(f"{k}={v}")
                        replaced.add(k)
                in_group = False
            if stripped == f"[{group_name}]":
                in_group = True
                out.append(line)
                continue

        if in_group and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in entries:
                out.append(f"{key}={entries[key]}")
                replaced.add(key)
                continue

        out.append(line)

    if in_group:
        for k, v in entries.items():
            if k not in replaced:
                out.append(f"{k}={v}")
                replaced.add(k)

    # If the group didn't exist, create it
    all_groups = {l.strip().strip("[]") for l in lines if l.strip().startswith("[") and l.strip().endswith("]")}
    if group_name not in all_groups:
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{group_name}]")
        for k, v in entries.items():
            out.append(f"{k}={v}")

    return "\n".join(out).strip() + "\n"


def atomic_write(path: Path, content: str) -> None:
    """Performs an atomic write to disk with fsync to guarantee crash safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = path.parent
    fd, temp_path = tempfile.mkstemp(dir=temp_dir, prefix=f".{path.name}.tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def sync_kde_apps(scheme_name: str = "Matugen", quiet: bool = False, dry_run: bool = False) -> bool:
    """Synchronizes all target KDE application config files."""
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).resolve()
    synced = []

    for conf_name in KDE_APP_CONFIGS:
        conf_path = config_dir / conf_name
        original_content = conf_path.read_text(encoding="utf-8") if conf_path.is_file() else ""

        # Update [UiSettings] and [General]
        content = patch_ini_group(original_content, "UiSettings", {"ColorScheme": scheme_name})
        content = patch_ini_group(content, "General", {"ColorScheme": scheme_name})

        if content != original_content:
            if not dry_run:
                atomic_write(conf_path, content)
            synced.append(conf_name)

    if not quiet:
        if synced:
            action = "Would sync" if dry_run else "Synced"
            print(f"[+] {action} '{scheme_name}' color scheme to KDE app configs: {', '.join(synced)}")
        else:
            print(f"[i] All KDE app configs already configured for '{scheme_name}'.")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize KDE Frameworks 6 application color schemes.")
    parser.add_argument("--scheme", default="Matugen", help="Color scheme name to pin (default: Matugen)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress standard output")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Simulate changes without writing to disk")
    args = parser.parse_args()

    success = sync_kde_apps(scheme_name=args.scheme, quiet=args.quiet, dry_run=args.dry_run)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
