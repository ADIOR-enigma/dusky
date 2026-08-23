import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Self

from python.frontend.core_types import BaseEngine
from python.engines.cmdline import BridgedStateDict


class SystemdBootEngine(BaseEngine):
    """
    Intelligent engine for systemd-boot (systemd 261 / Linux 7.2+).
    
    Manages:
    - DEFAULT scope: Kernel command-line parameters in options line of entry .conf
    - ENTRY scope: Entry metadata (title, sort-key, version, etc.) in entry .conf
    - LOADER scope: Global bootloader settings in /boot/loader/loader.conf (default, timeout, console-mode, editor)
    """

    def __init__(self, config_path: str = "") -> None:
        self.config_path: Path = self._resolve_config_path(config_path)
        self.loader_conf_path: Path = Path("/boot/loader/loader.conf")
        self.cache: BridgedStateDict = BridgedStateDict()
        self.file_mtime_ns: int = 0
        self.loader_mtime_ns: int = 0

    @staticmethod
    def _resolve_config_path(config_path: str) -> Path:
        if config_path:
            p = Path(config_path).expanduser().resolve()
            if p.exists():
                return p
        # Check /boot/loader/entries/ for any custom or default conf
        entries_dir = Path("/boot/loader/entries")
        if entries_dir.is_dir():
            # Look for active/default conf first
            loader_conf = Path("/boot/loader/loader.conf")
            if loader_conf.exists():
                try:
                    for line in loader_conf.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.startswith("default ") or line.startswith("default\t"):
                            def_val = line.split(None, 1)[1].strip()
                            cand = entries_dir / def_val
                            if cand.exists():
                                return cand
                            for match in entries_dir.glob(f"*{def_val}*"):
                                if match.is_file():
                                    return match
                except Exception:
                    pass
            # Look for dusky or arch entries
            for cand in sorted(entries_dir.glob("*.conf"), key=lambda f: (0 if "dusky" in f.name else 1, f.name)):
                if cand.is_file():
                    return cand
        return Path("/boot/loader/entries/arch-linux.conf")

    @classmethod
    def from_path(cls, config_path: str) -> Self:
        return cls(config_path)

    @property
    def target_path(self) -> str:
        return str(self.config_path)

    def load_state(self) -> dict[str, Any]:
        self.cache = BridgedStateDict()

        # 1. Load entry config (DEFAULT cmdline parameters and ENTRY metadata)
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8", errors="replace") as f:
                    self.file_mtime_ns = os.fstat(f.fileno()).st_mtime_ns
                    content = f.read()

                for line in content.splitlines():
                    line_clean = line.strip()
                    if not line_clean or line_clean.startswith("#"):
                        continue

                    if match := re.match(r"^([ \t]*)options([ \t]+)(.*)$", line):
                        tokens = re.split(r'((?:[^\s"\']|"[^"]*"|\'[^\']*\')+)', match.group(3))
                        args = [t for t in tokens if t.strip()]
                        counts: dict[str, int] = {}

                        for arg in args:
                            k, v = arg.split("=", 1) if "=" in arg else (arg, "true")
                            counts[k] = counts.get(k, 0) + 1
                            self.cache[f"DEFAULT/{k}:{counts[k]}"] = v
                            self.cache[f"DEFAULT/{k}"] = v
                    elif " " in line_clean or "\t" in line_clean:
                        k, v = line_clean.split(None, 1)
                        self.cache[f"ENTRY/{k}"] = v.strip()
            except OSError:
                pass

        # 2. Load global loader.conf (LOADER scope)
        if self.loader_conf_path.exists():
            try:
                with open(self.loader_conf_path, "r", encoding="utf-8", errors="replace") as f:
                    self.loader_mtime_ns = os.fstat(f.fileno()).st_mtime_ns
                    for line in f.read().splitlines():
                        line_clean = line.strip()
                        if line_clean and not line_clean.startswith("#") and (" " in line_clean or "\t" in line_clean):
                            k, v = line_clean.split(None, 1)
                            self.cache[f"LOADER/{k}"] = v.strip()
            except OSError:
                pass

        return self.cache

    def write_value(self, target_key: str, target_scope: str, new_value: str, item_type: str = "string") -> tuple[bool, str, str]:
        return self.write_batch([(target_key, target_scope, new_value, item_type)])

    def write_batch(self, changes: list[tuple[str, str, str, str]]) -> tuple[bool, str, str]:
        if not changes:
            return True, "No pending changes.", ""

        loader_changes = [c for c in changes if c[1] == "LOADER"]
        entry_and_cmdline_changes = [c for c in changes if c[1] in ("DEFAULT", "ENTRY")]

        success = True
        msgs = []

        if loader_changes:
            ok_l, msg_l = self._write_loader_conf(loader_changes)
            if not ok_l:
                return False, msg_l, ""
            msgs.append(msg_l)

        if entry_and_cmdline_changes:
            ok_e, msg_e = self._write_entry_conf(entry_and_cmdline_changes)
            if not ok_e:
                return False, msg_e, ""
            msgs.append(msg_e)

        return True, " ".join(msgs) or "Successfully saved changes.", ""

    def _write_loader_conf(self, changes: list[tuple[str, str, str, str]]) -> tuple[bool, str]:
        lines: list[str] = []
        if self.loader_conf_path.exists():
            try:
                with open(self.loader_conf_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                lines = []

        changes_dict = {key: val for key, _, val, _ in changes}
        out_lines: list[str] = []
        handled_keys: set[str] = set()

        for line in lines:
            line_s = line.strip()
            if line_s and not line_s.startswith("#") and (" " in line_s or "\t" in line_s):
                k, _ = line_s.split(None, 1)
                if k in changes_dict:
                    val = str(changes_dict[k]).strip()
                    handled_keys.add(k)
                    if val not in ("unset", "__delete__", ""):
                        out_lines.append(f"{k:<16}{val}")
                    continue
            out_lines.append(line)

        for k, val in changes_dict.items():
            if k not in handled_keys:
                val_s = str(val).strip()
                if val_s not in ("unset", "__delete__", ""):
                    out_lines.append(f"{k:<16}{val_s}")

        content = "\n".join(out_lines) + "\n"
        return self._atomic_write(self.loader_conf_path, content)

    def _write_entry_conf(self, changes: list[tuple[str, str, str, str]]) -> tuple[bool, str]:
        lines: list[str] = []
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError as e:
                return False, f"Failed to open config for verification: {e}"

        cmdline_changes = [c for c in changes if c[1] == "DEFAULT"]
        entry_changes = {c[0]: c[2] for c in changes if c[1] == "ENTRY"}

        out_lines: list[str] = []
        options_found = False
        handled_entry_keys: set[str] = set()

        changes_dict = {(scope, key): (val, itype) for key, scope, val, itype in cmdline_changes}
        applied_commits: set[tuple[str, str]] = set()

        for line in lines:
            line_s = line.strip()
            # Handle ENTRY metadata lines (title, sort-key, version, etc.)
            if line_s and not line_s.startswith("#") and not line_s.startswith("options"):
                if " " in line_s or "\t" in line_s:
                    k, _ = line_s.split(None, 1)
                    if k in entry_changes:
                        val = str(entry_changes[k]).strip()
                        handled_entry_keys.add(k)
                        if val not in ("unset", "__delete__", ""):
                            out_lines.append(f"{k:<10} {val}")
                        continue

            # Handle options line (DEFAULT scope)
            if match := re.match(r"^([ \t]*)options([ \t]+)(.*)$", line):
                options_found = True
                leading_space, spacing, options_val = match.groups()
                tokens = re.split(r'((?:[^\s"\']|"[^"]*"|\'[^\']*\')+)', options_val)

                max_counts: dict[str, int] = {}
                for t in tokens:
                    if t.strip():
                        k = t.split("=", 1)[0]
                        max_counts[k] = max_counts.get(k, 0) + 1

                out_tokens: list[str] = []
                counts: dict[str, int] = {}

                for t in tokens:
                    if not t.strip():
                        out_tokens.append(t)
                        continue

                    k = t.split("=", 1)[0]
                    counts[k] = counts.get(k, 0) + 1

                    lookup_exact = ("DEFAULT", f"{k}:{counts[k]}")
                    lookup_base = ("DEFAULT", k)

                    target_val = None
                    target_itype = None
                    matched_lookup = None

                    if lookup_exact in changes_dict:
                        target_val, target_itype = changes_dict[lookup_exact]
                        matched_lookup = lookup_exact
                    elif counts.get(k, 0) == max_counts.get(k, 0) and lookup_base in changes_dict:
                        target_val, target_itype = changes_dict[lookup_base]
                        matched_lookup = lookup_base

                    if target_val is not None:
                        applied_commits.add(matched_lookup)
                        val_str = str(target_val)
                        val_lower = val_str.lower()

                        match (val_lower, target_itype):
                            case ("__delete__" | "unset" | "", _) | ("false", "bool"):
                                if out_tokens and out_tokens[-1].isspace():
                                    out_tokens.pop()
                            case _:
                                if target_itype == "bool" and val_lower == "true":
                                    out_tokens.append(k)
                                else:
                                    out_tokens.append(f"{k}={val_str}")
                    else:
                        out_tokens.append(t)

                for key_raw, scope, val, target_itype in cmdline_changes:
                    lookup = (scope, key_raw)
                    if lookup in applied_commits:
                        continue

                    val_str = str(val)
                    val_lower = val_str.lower()

                    match (val_lower, target_itype):
                        case ("__delete__" | "unset" | "", _) | ("false", "bool"):
                            continue
                        case _:
                            clean_key = key_raw.split(":")[0] if ":" in key_raw else key_raw
                            needs_space = False
                            for tk in reversed(out_tokens):
                                if tk:
                                    needs_space = bool(tk.strip())
                                    break
                            if needs_space:
                                out_tokens.append(" ")

                            if target_itype == "bool" and val_lower == "true":
                                out_tokens.append(clean_key)
                            else:
                                out_tokens.append(f"{clean_key}={val_str}")
                            applied_commits.add(lookup)

                out_lines.append(f"{leading_space}options{spacing}{''.join(out_tokens).strip()}")
            else:
                out_lines.append(line)

        # Append any new ENTRY metadata keys that weren't in file
        for k, val in entry_changes.items():
            if k not in handled_entry_keys:
                val_s = str(val).strip()
                if val_s not in ("unset", "__delete__", ""):
                    out_lines.insert(0, f"{k:<10} {val_s}")

        if not options_found and cmdline_changes:
            new_tokens: list[str] = []
            for key_raw, scope, val, target_itype in cmdline_changes:
                val_str = str(val)
                val_lower = val_str.lower()
                if val_lower in ("__delete__", "unset", "") or (target_itype == "bool" and val_lower == "false"):
                    continue
                clean_key = key_raw.split(":")[0] if ":" in key_raw else key_raw
                if new_tokens:
                    new_tokens.append(" ")
                if target_itype == "bool" and val_lower == "true":
                    new_tokens.append(clean_key)
                else:
                    new_tokens.append(f"{clean_key}={val_str}")
            if new_tokens:
                out_lines.append(f"options\t{''.join(new_tokens)}")

        content = "\n".join(out_lines) + "\n"
        return self._atomic_write(self.config_path, content)

    def _atomic_write(self, target_path: Path, final_content: str) -> tuple[bool, str]:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file_path = None
        success = False
        try:
            with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8", dir=target_path.parent) as tf:
                temp_file_path = Path(tf.name)
                tf.write(final_content)
                tf.flush()
                os.fsync(tf.fileno())

            if target_path.exists():
                try:
                    stat_info = target_path.stat()
                    os.chmod(temp_file_path, stat.S_IMODE(stat_info.st_mode))
                    os.chown(temp_file_path, stat_info.st_uid, stat_info.st_gid)
                except OSError:
                    pass

            os.replace(temp_file_path, target_path)
            success = True
            return True, f"Updated {target_path.name}"
        except PermissionError:
            if temp_file_path and temp_file_path.exists():
                try:
                    temp_file_path.unlink()
                except OSError:
                    pass
            try:
                res = subprocess.run(
                    ["sudo", "-n", "tee", str(target_path)],
                    input=final_content.encode(),
                    capture_output=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    return True, f"Updated {target_path.name} (via sudo)"
                return False, "AUTH_REQUIRED"
            except Exception:
                return False, "AUTH_REQUIRED"
        except OSError as e:
            return False, f"Write error on {target_path.name}: {e}"
        finally:
            if temp_file_path and temp_file_path.exists() and not success:
                try:
                    temp_file_path.unlink()
                except OSError:
                    pass
