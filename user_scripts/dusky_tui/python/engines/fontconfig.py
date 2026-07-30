import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from typing import Any
import subprocess
import shutil
import os
import re

from python.frontend.core_types import BaseEngine

class FontconfigEngine(BaseEngine):
    """
    O(1) XML serialization engine for Arch Linux fontconfig rules.
    Operates strictly within ~/.config/fontconfig/conf.d/ to adhere to XDG standards.
    """
    def __init__(self, config_path: str = "~/.config/fontconfig/conf.d/99-dusky-fonts.conf"):
        self.config_path = Path(config_path).expanduser().resolve()
        self.cache: dict[str, Any] = {}

    @property
    def target_path(self) -> str:
        return str(self.config_path)

    def load_state(self) -> dict[str, Any]:
        if not self.config_path.exists() or self.config_path.stat().st_size == 0:
            return {}

        state = {}
        try:
            tree = ET.parse(self.config_path)
            root = tree.getroot()

            # Parse Font Aliases (sans-serif, serif, monospace, emoji, etc.)
            for alias in root.findall('alias'):
                family_elem = alias.find('family')
                if family_elem is not None and family_elem.text:
                    family = family_elem.text.strip()
                    prefer_families = alias.findall('prefer/family')
                    if prefer_families:
                        font_list = [pf.text.strip() for pf in prefer_families if pf.text]
                        if font_list:
                            state[family] = font_list[0] if len(font_list) == 1 else font_list

            # Parse Subpixel Rendering & Hinting Configuration across match blocks
            for match in root.findall("match"):
                for edit in match.findall('edit'):
                    name = edit.get('name')
                    if name:
                        bool_val = edit.find('bool')
                        if bool_val is not None:
                            state[name] = bool_val.text.strip().lower() in ("true", "1", "yes", "on", "t", "y")
                            continue
                        
                        const_val = edit.find('const')
                        if const_val is not None:
                            state[name] = const_val.text.strip()
                            continue

                        double_val = edit.find('double')
                        if double_val is not None:
                            try:
                                state[name] = float(double_val.text.strip())
                            except ValueError:
                                state[name] = double_val.text.strip()
                            continue

                        int_val = edit.find('int')
                        if int_val is not None:
                            try:
                                state[name] = int(int_val.text.strip())
                            except ValueError:
                                state[name] = int_val.text.strip()
                            continue

                        string_val = edit.find('string')
                        if string_val is not None:
                            state[name] = string_val.text.strip()
                            continue
            self.cache = state
        except Exception as e:
            print(f"[FontconfigEngine] Load Exception: {e}")
            
        return state

    def write_batch(self, changes: list[tuple[str, str, str, str]]) -> tuple[bool, str, str]:
        if not changes:
            return True, "No pending changes.", ""

        state = self.load_state()
        known_bool_keys = {"antialias", "hinting", "autohint", "embeddedbitmap", "subpixel"}
        known_consts = {
            "none", "rgb", "bgr", "vrgb", "vbgr", 
            "hintnone", "hintslight", "hintmedium", "hintfull", 
            "lcdnone", "lcddefault", "lcdlight", "lcdlegacy"
        }

        for key, scope, val, itype in changes:
            if val is None or val == "":
                state.pop(key, None)
            elif itype == "bool" or key in known_bool_keys or isinstance(val, bool):
                if isinstance(val, bool):
                    state[key] = val
                else:
                    state[key] = str(val).lower() in ("true", "1", "yes", "on", "t", "y")
            elif itype == "int":
                try:
                    state[key] = int(val)
                except (ValueError, TypeError):
                    state[key] = val
            elif itype == "float":
                try:
                    state[key] = float(val)
                except (ValueError, TypeError):
                    state[key] = val
            else:
                state[key] = val

        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            
            root = ET.Element('fontconfig')

            # 1. Generate Alias Blocks
            font_classes = ['sans-serif', 'serif', 'monospace', 'emoji']
            for fc in font_classes:
                if fc in state and state[fc]:
                    alias = ET.SubElement(root, 'alias', {'binding': 'strong'})
                    fam = ET.SubElement(alias, 'family')
                    fam.text = fc
                    pref = ET.SubElement(alias, 'prefer')
                    
                    v = state[fc]
                    if isinstance(v, list):
                        for f_item in v:
                            pref_fam = ET.SubElement(pref, 'family')
                            pref_fam.text = str(f_item)
                    else:
                        pref_fam = ET.SubElement(pref, 'family')
                        pref_fam.text = str(v)

            # 2. Generate Rendering Match Block (DTD compliant)
            render_keys = [k for k in state if k not in font_classes and state[k] is not None]
            if render_keys:
                match = ET.SubElement(root, 'match', {'target': 'font'})
                for k in render_keys:
                    edit = ET.SubElement(match, 'edit', {'mode': 'assign', 'name': k})
                    v = state[k]
                    if isinstance(v, bool):
                        b = ET.SubElement(edit, 'bool')
                        b.text = "true" if v else "false"
                    elif isinstance(v, (int, float)) or (isinstance(v, str) and (v.isdigit() or (v.replace('.', '', 1).isdigit() and v.count('.') == 1))):
                        d = ET.SubElement(edit, 'double' if "." in str(v) else 'int')
                        d.text = str(v)
                    elif str(v).lower() in known_consts:
                        c = ET.SubElement(edit, 'const')
                        c.text = str(v)
                    else:
                        s = ET.SubElement(edit, 'string')
                        s.text = str(v)

            # 3. Format and clean XML string
            xmlstr = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
            
            # Inject correct DOCTYPE
            xmlstr = re.sub(
                r'^\s*<\?xml[^>]*\?>',
                '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">',
                xmlstr,
                count=1
            )
            
            # Strip empty lines generated by minidom's text node artifacts
            clean_xml = "\n".join(line for line in xmlstr.split('\n') if line.strip()) + "\n"

            # 4. Atomic Write with fsync
            temp_path = self.config_path.with_name(f".{self.config_path.name}.tmp-{os.getpid()}")
            with open(temp_path, 'w', encoding='utf-8') as f:
                f.write(clean_xml)
                f.flush()
                os.fsync(f.fileno())
            temp_path.replace(self.config_path)

            self.cache = state
            
            # 5. Non-blocking System Cache Refresh (fail-safe)
            fc_cache_bin = shutil.which("fc-cache")
            if fc_cache_bin:
                try:
                    subprocess.Popen(
                        [fc_cache_bin, "-f"], 
                        stdout=subprocess.DEVNULL, 
                        stderr=subprocess.DEVNULL
                    )
                except Exception:
                    pass
            
            return True, f"Successfully applied {len(changes)} font settings.", ""
            
        except Exception as e:
            return False, f"XML Generation Failed: {e}", ""

    def write_value(self, target_key: str, target_scope: str, new_value: str, item_type: str = "string") -> tuple[bool, str, str]:
        return self.write_batch([(target_key, target_scope, new_value, item_type)])
