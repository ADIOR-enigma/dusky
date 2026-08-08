#!/usr/bin/env python3
#d: Refresh the font cache and align default font aliases

"""
Font cache refresh + default-font alignment for clean installs.

Aligns the setup-time font state with the Dusky Font Manager TUI:
  * builds conf.d/99-dusky-fonts.conf using the actual engine (same writer,
    same DTD header, binding="strong" aliases) from the schema defaults,
  * adds metric-compat Arial/Helvetica/Verdana -> default sans-family
    rewrites (qual="first" form, verified NOT to hijack generic requests),
  * runs `fc-cache -f`, then verifies sans-serif/serif/monospace/emoji and
    Arial/Helvetica/Verdana/Times New Roman resolution.

Default family is configurable without editing code:
    DUSKY_DEFAULT_SANS="JetBrainsMono Nerd Font" python3 140_dusky_font_configurator.py
  or --font-family "..." (schema Tab-0 default is Atkinson Hyperlegible).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
NC = "\033[0m"

USER_SCRIPTS = Path(os.environ.get("USER_SCRIPTS", "~/user_scripts")).expanduser().resolve()
DUSKY_TUI_ROOT = USER_SCRIPTS / "dusky_tui"
SCHEMA_PATH = USER_SCRIPTS / "fonts" / "tui_fonts.py"
ENGINE_OUTPUT = "~/.config/fontconfig/conf.d/99-dusky-fonts.conf"

_METRIC_COMPAT_SANS = ("Arial", "Helvetica", "Verdana")


def _load_schema():
    """Import tui_fonts schema from the fonts repo via importlib."""
    if not SCHEMA_PATH.is_file():
        return None
    sys.path.insert(0, str(SCHEMA_PATH.parent))
    if str(DUSKY_TUI_ROOT) not in sys.path:
        sys.path.insert(0, str(DUSKY_TUI_ROOT))
    spec = importlib.util.spec_from_file_location("_tui_fonts_140", SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_tui_fonts_140"] = mod
    spec.loader.exec_module(mod)
    return mod


def _default_sans_family() -> str:
    """DUSKY_DEFAULT_SANS env var, then --font arg (in os.environ), then the
    schema's Tab 0 sans-serif default, then Atkinson Hyperlegible."""
    env = os.environ.get("DUSKY_DEFAULT_SANS")
    if env:
        return env
    mod = _load_schema()
    if mod:
        for items in mod.SCHEMA.values():
            for item in items:
                if item.key == "sans-serif" and item.default:
                    return str(item.default)
    return "Atkinson Hyperlegible"


def _metric_rewrite_block(name: str, target: str) -> str:
    return (
        f'  <match target="pattern">\n'
        f'    <test qual="first" name="family">\n'
        f'      <string>{name}</string>\n'
        f'    </test>\n'
        f'    <edit name="family" mode="assign" binding="strong">\n'
        f'      <string>{target}</string>\n'
        f'    </edit>\n'
        f'  </match>\n'
    )


def build_config(target: str) -> tuple[bool, str]:
    """Write canonical config via the real engine (same path the TUI uses),
    overriding Tab 0 sans-serif to the requested default."""
    if not DUSKY_TUI_ROOT.is_dir():
        return False, f"missing engine root: {DUSKY_TUI_ROOT}"
    sys.path.insert(0, str(DUSKY_TUI_ROOT))
    mod = _load_schema()
    if mod is None:
        return False, f"missing schema: {SCHEMA_PATH}"
    try:
        from python.engines.fontconfig import FontconfigEngine

        changes = []
        for items in mod.SCHEMA.values():
            for item in items:
                if item.type_ in ("action", "preset"):
                    continue
                val = target if item.key == "sans-serif" else item.default
                changes.append((item.key, item.scope, val, item.type_))

        engine = FontconfigEngine(ENGINE_OUTPUT)
        ok, msg, err = engine.write_batch(changes)

        conf = Path(ENGINE_OUTPUT).expanduser()
        if ok and conf.is_file():
            text = conf.read_text()
            missing = [name for name in _METRIC_COMPAT_SANS
                       if f">{name}</string>" not in text]
            if missing:
                blocks = "".join(_metric_rewrite_block(n, target) for n in missing)
                conf.write_text(text.replace("</fontconfig>", blocks + "</fontconfig>"))
            _drop_legacy_conf()
            engine._refresh_cache_async()
        return (ok, msg) if ok else (False, err or msg)
    except Exception as exc:
        return False, str(exc)


def _drop_legacy_conf() -> None:
    """The engine absorbs the legacy ~/.config/fontconfig/fonts.conf into its
    own emit state, so the legacy file must not keep applying raw
    qual="any" + binding="strong" rewrites alongside the canonical config
    (that is how 'Times New Roman' rewrites kept hijacking generic serif
    requests). Delete it outright; no backup is kept."""
    legacy = Path.home() / ".config" / "fontconfig" / "fonts.conf"
    if not legacy.is_file():
        return
    legacy.unlink()
    print("  [i] Removed legacy fonts.conf (superseded by generated config)")


def resolve_match(family: str) -> str:
    try:
        out = subprocess.run(["fc-match", family], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return ""
    return out.strip().lower()


def verify(target_sans: str) -> tuple[int, list[tuple[str, bool]]]:
    generic_checks = [
        ("sans-serif", target_sans),
        ("serif", "Liberation Serif"),
        ("monospace", "JetBrainsMono Nerd Font Mono"),
        ("emoji", "Noto Color Emoji"),
    ]
    results = []
    for generic, expect in generic_checks:
        out = resolve_match(generic)
        results.append((f"{generic} -> {expect}", _matched(out, expect)))

    for name in (*_METRIC_COMPAT_SANS, "Times New Roman"):
        expect = target_sans if name in _METRIC_COMPAT_SANS else "Liberation Serif"
        out = resolve_match(name)
        results.append((f"{name} -> {expect}", _matched(out, expect)))

    failures = sum(1 for _label, ok in results if not ok)
    return failures, results


def _matched(matcher_out: str, expect: str) -> bool:
    if not matcher_out or not expect:
        return False
    return (expect.lower() in matcher_out.lower()
            or expect.split()[0].lower() in matcher_out.lower())


def main() -> int:
    parser = argparse.ArgumentParser(description="Font cache refresh + alias verify")
    parser.add_argument("--font-family", default=None,
                        help="Override the default sans-serif family "
                             "(also: DUSKY_DEFAULT_SANS)")
    args = parser.parse_args()
    if args.font_family:
        os.environ["DUSKY_DEFAULT_SANS"] = args.font_family

    target = _default_sans_family()
    print(f"{YELLOW}:: Refreshing System Font Cache (target sans: "
          f"{GREEN}{target}{NC}{YELLOW})...{NC}")

    ok, msg = build_config(target)
    if not ok:
        print(f"{RED}[FAIL] writing fontconfig config: {msg}{NC}")
        sys.exit(1)
    print(f"  [i] {msg}")

    subprocess.run(["fc-cache", "-f"], check=False)

    print(f"\n{YELLOW}:: Verifying Font Aliases...{NC}")
    failures, results = verify(target)
    for label, ok in results:
        print(f"{GREEN}[+] {label}{NC}" if ok else f"{RED}[-] {label}{NC}")

    if failures:
        print(f"\n{RED}[FAIL] {failures} aliases did not resolve as expected.{NC}")
        sys.exit(1)
    print(f"\n{GREEN}[SUCCESS] System fonts aligned to '{target}'.{NC}")


if __name__ == "__main__":
    main()