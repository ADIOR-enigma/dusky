#!/usr/bin/env python3
"""
Elite ZRAM & Swap Subsystem Manager
Provides a robust, unified management backend for:
- ZRAM0 (Compressed RAM Swap @ Priority 32767)
- ZRAM1 (/mnt/zram1 Hybrid RAM Disk - Ext4 ZRAM / Tmpfs / Disabled)
- Disk Swap (/swap/swapfile @ Lowest Priority -1)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# --- ANSI Formatting ---
class C:
    RED = "\033[1;31m"
    GRN = "\033[1;32m"
    YLW = "\033[1;33m"
    BLU = "\033[1;34m"
    CYN = "\033[1;36m"
    BOLD = "\033[1m"
    RST = "\033[0m"

def info(msg: str): print(f"{C.BLU}[INFO]{C.RST} {msg}")
def ok(msg: str): print(f"{C.GRN}[ OK ]{C.RST} {msg}")
def warn(msg: str): print(f"{C.YLW}[WARN]{C.RST} {msg}")
def err(msg: str): print(f"{C.RED}[FAIL]{C.RST} {msg}", file=sys.stderr)
def die(msg: str, code: int = 1) -> NoReturn:
    err(msg)
    sys.exit(code)

ZRAM_CONF_DIR = Path("/etc/systemd/zram-generator.conf.d")
ZRAM0_CONF = ZRAM_CONF_DIR / "99-elite-zram.conf"
ZRAM1_CONF = ZRAM_CONF_DIR / "99-elite-zram1.conf"
SWAPFILE_PATH = Path("/swap/swapfile")
FSTAB_PATH = Path("/etc/fstab")

def get_real_user_info() -> tuple[str, int, Path]:
    sudo_user = os.environ.get("SUDO_USER")
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_user and sudo_uid and int(sudo_uid) != 0:
        try:
            import pwd
            return sudo_user, int(sudo_uid), Path(pwd.getpwnam(sudo_user).pw_dir)
        except Exception:
            pass
    try:
        import pwd
        loginuid_path = Path("/proc/self/loginuid")
        if loginuid_path.exists():
            l_uid = int(loginuid_path.read_text().strip())
            if 0 < l_uid < 65534:
                pw = pwd.getpwuid(l_uid)
                return pw.pw_name, pw.pw_uid, Path(pw.pw_dir)
        for pw in pwd.getpwall():
            if 1000 <= pw.pw_uid < 60000 and pw.pw_name != "nobody":
                return pw.pw_name, pw.pw_uid, Path(pw.pw_dir)
    except Exception:
        pass
    return "root", 0, Path.home()

def get_script_206() -> Path:
    _, _, home_dir = get_real_user_info()
    candidates = [
        home_dir / "user_scripts/arch_setup_scripts/scripts/206_zram_tmpfs_mounts.py",
        Path.home() / "user_scripts/arch_setup_scripts/scripts/206_zram_tmpfs_mounts.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]

def notify(title: str, message: str, urgency: str = "normal"):
    """Dispatches a desktop notification dynamically to the active user."""
    if shutil.which("notify-send"):
        try:
            user, uid, _ = get_real_user_info()
            if user != "root" and os.geteuid() == 0:
                runtime_dir = f"/run/user/{uid}"
                env = os.environ.copy()
                env["XDG_RUNTIME_DIR"] = runtime_dir
                subprocess.run(
                    ["sudo", "-u", user, "notify-send", "-a", "Dusky Swap Manager", "-u", urgency, title, message],
                    env=env,
                    check=False,
                    timeout=2
                )
            else:
                subprocess.run(["notify-send", "-a", "Dusky Swap Manager", "-u", urgency, title, message], check=False, timeout=2)
        except Exception:
            pass

def escalate_root_if_needed():
    if os.geteuid() != 0:
        if shutil.which("sudo"):
            os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
        elif shutil.which("pkexec"):
            os.execvp("pkexec", ["pkexec", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
        else:
            die("Root privileges required (sudo or pkexec not available).")

def run_cmd(cmd: list[str], check: bool = True) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            die(f"Command failed: {' '.join(cmd)}\n{e.stderr.strip()}")
        return e.stdout.strip() + e.stderr.strip()

# =============================================================================
# STATUS QUERIES & INTUITIVE FORMATTING (Non-Root)
# =============================================================================

def get_total_ram_gb() -> float:
    try:
        meminfo = Path("/proc/meminfo").read_text()
        m = re.search(r"MemTotal:\s+(\d+)\s+kB", meminfo)
        if m:
            return int(m.group(1)) / (1024 * 1024)
    except Exception:
        pass
    return 8.0

def format_clean_size(expr_or_size: str) -> str:
    s = expr_or_size.strip()
    total_ram = get_total_ram_gb()
    
    m = re.search(r"ram\s*\*\s*([0-9.]+)", s)
    if m:
        factor = float(m.group(1))
        gb = total_ram * factor
        return f"{gb:.1f} GB" if not gb.is_integer() else f"{gb:.0f} GB"
    
    m_div = re.search(r"ram\s*/\s*([0-9.]+)", s)
    if m_div:
        denom = float(m_div.group(1))
        factor = 1.0 / denom
        gb = total_ram * factor
        return f"{gb:.1f} GB" if not gb.is_integer() else f"{gb:.0f} GB"

    if s.lower() == "ram":
        return f"{total_ram:.1f} GB" if not total_ram.is_integer() else f"{total_ram:.0f} GB"
    
    m_num = re.match(r"^([0-9.]+)\s*([KMGT]?B?)$", s, re.IGNORECASE)
    if m_num:
        val = float(m_num.group(1))
        unit = m_num.group(2).upper()
        if unit.startswith("G"):
            return f"{val:.1f} GB" if not val.is_integer() else f"{val:.0f} GB"
        elif unit.startswith("M"):
            return f"{val:.0f} MB"
        elif not unit:
            gb = val / 1024 if val >= 512 else val
            return f"{gb:.1f} GB" if not gb.is_integer() else f"{gb:.0f} GB"
            
    return s

def get_zram0_status() -> str:
    try:
        swaps = Path("/proc/swaps").read_text() if Path("/proc/swaps").exists() else ""
        if "/dev/zram0" in swaps:
            zram_out = run_cmd(["zramctl", "--output", "DISKSIZE", "--noheadings", "/dev/zram0"], check=False)
            size = zram_out.strip() if zram_out else ""
            if size:
                return format_clean_size(size)
            return "Active"
        return "Disabled"
    except Exception:
        return "Disabled"

def get_zram0_size() -> str:
    if ZRAM0_CONF.exists():
        match = re.search(r"zram-size\s*=\s*(.+)", ZRAM0_CONF.read_text())
        if match:
            raw = match.group(1).strip()
            m = re.search(r"ram\s*\*\s*([0-9.]+)", raw)
            if m:
                factor = float(m.group(1))
                return f"{int(round(factor * 100))}%"
            if raw == "ram":
                return "100%"
            return raw
    total_ram = get_total_ram_gb()
    return "80%" if total_ram <= 8.5 else ("50%" if total_ram < 31.5 else "20%")

def get_zram1_status() -> str:
    try:
        source = run_cmd(["findmnt", "-rn", "-o", "SOURCE", "--mountpoint", "/mnt/zram1"], check=False)
        if source in ("/dev/zram1", "zram1"):
            zram_out = run_cmd(["zramctl", "--output", "DISKSIZE", "--noheadings", "/dev/zram1"], check=False)
            size = zram_out.strip() if zram_out else ""
            if size:
                return format_clean_size(size)
            return "Active"
        elif source == "tmpfs":
            size_out = run_cmd(["findmnt", "-rn", "-o", "SIZE", "--mountpoint", "/mnt/zram1"], check=False)
            if size_out:
                return format_clean_size(size_out)
            return "Active"
        return "Disabled"
    except Exception:
        return "Disabled"

def get_zram1_size() -> str:
    if ZRAM1_CONF.exists():
        match = re.search(r"zram-size\s*=\s*(.+)", ZRAM1_CONF.read_text())
        if match:
            raw = match.group(1).strip()
            m = re.search(r"ram\s*\*\s*([0-9.]+)", raw)
            if m:
                factor = float(m.group(1))
                return f"{int(round(factor * 100))}%"
            if raw == "ram":
                return "100%"
            return raw
    return "100%"

def get_disk_swap_status() -> str:
    try:
        swaps = Path("/proc/swaps").read_text() if Path("/proc/swaps").exists() else ""
        if "/swap/swapfile" in swaps or "swapfile" in swaps:
            if SWAPFILE_PATH.exists():
                size_gb = SWAPFILE_PATH.stat().st_size / (1024**3)
                return f"{size_gb:.1f} GB" if not size_gb.is_integer() else f"{size_gb:.0f} GB"
            return "Active"
        return "Disabled"
    except Exception:
        return "Disabled"

def get_disk_swap_size() -> str:
    if SWAPFILE_PATH.exists():
        try:
            size_bytes = SWAPFILE_PATH.stat().st_size
            gb = size_bytes / (1024**3)
            if gb >= 1.0:
                return f"{gb:.0f} GB" if gb.is_integer() else f"{gb:.1f} GB"
            mb = size_bytes / (1024**2)
            return f"{mb:.0f} MB"
        except Exception:
            pass
    return "4 GB"

def print_full_status():
    print(f"\n{C.BOLD}=== DUSKY MEMORY & SWAP TOPOLOGY STATUS ==={C.RST}\n")
    print(f"  {C.CYN}• ZRAM0 (Compressed Swap):{C.RST}     {get_zram0_status()}")
    print(f"  {C.CYN}• ZRAM1 (/mnt/zram1 Disk):{C.RST}     {get_zram1_status()}")
    print(f"  {C.CYN}• Disk Swap (/swap/swapfile):{C.RST}  {get_disk_swap_status()}\n")
    
    if Path("/proc/swaps").exists():
        print(f"{C.BOLD}[ Active Kernel Swaps (/proc/swaps) ]{C.RST}")
        print(Path("/proc/swaps").read_text().strip())
        print()

# =============================================================================
# ZRAM0 CONFIGURATION
# =============================================================================

def ensure_zram_device(dev_name: str = "zram0") -> None:
    dev_path = Path(f"/dev/{dev_name}")
    if not dev_path.exists():
        hot_add = Path("/sys/class/zram-control/hot_add")
        if hot_add.exists():
            try:
                hot_add.read_text()
            except Exception:
                pass
        if not dev_path.exists():
            run_cmd(["modprobe", "zram"], check=False)

def parse_size_input(raw: str) -> str:
    val = raw.strip()
    if not val:
        total_ram = get_total_ram_gb()
        return "ram * 0.8" if total_ram <= 8.5 else ("ram * 0.5" if total_ram < 31.5 else "ram * 0.2")
    
    val_clean = val.lower().replace(" ", "").replace("gib", "g").replace("mib", "m").replace("kib", "k")
    
    if val_clean in ("auto", "default"):
        total_ram = get_total_ram_gb()
        return "ram * 0.8" if total_ram <= 8.5 else ("ram * 0.5" if total_ram < 31.5 else "ram * 0.2")

    # Handle percentage: "80%" -> "ram * 0.8"
    if val_clean.endswith("%"):
        try:
            pct = float(val_clean[:-1])
            if pct == 100.0:
                return "ram"
            return f"ram * {pct / 100.0:.2f}".rstrip("0").rstrip(".")
        except ValueError:
            pass
            
    # Handle pure integers from 1 to 100: assume percentage (e.g. 80 -> ram * 0.8)
    try:
        n = float(val_clean)
        if 0.0 < n <= 2.0:
            return f"ram * {n:.2f}".rstrip("0").rstrip(".")
        elif 3.0 <= n <= 100.0 and n.is_integer():
            pct = n / 100.0
            return "ram" if pct == 1.0 else f"ram * {pct:.2f}".rstrip("0").rstrip(".")
    except ValueError:
        pass

    # Handle explicit GB/MB fixed sizes (e.g. "4g", "4gb", "2048m", "512mb") -> convert to MiB integer for zram-generator
    m_size = re.match(r"^([0-9.]+)\s*([gmk]b?)$", val_clean)
    if m_size:
        num = float(m_size.group(1))
        unit = m_size.group(2)
        if unit.startswith("g"):
            return str(int(num * 1024))
        elif unit.startswith("m"):
            return str(int(num))
        elif unit.startswith("k"):
            return str(int(num / 1024))

    # Format standard expressions cleanly (e.g. ram * 0.5)
    val_formatted = re.sub(r"\s*([*/+-])\s*", r" \1 ", val_clean)
    return val_formatted

def set_zram0_size(size_raw: str):
    escalate_root_if_needed()
    size_expr = parse_size_input(size_raw)
    info(f"Configuring ZRAM0 size expression: {C.BOLD}{size_expr}{C.RST}")

    # Determine intelligent resident limit
    res_limit = "ram * 0.5"
    m = re.search(r"ram\s*\*\s*([0-9.]+)", size_expr)
    if m:
        factor = float(m.group(1))
        if factor <= 0.5:
            res_limit = f"ram * {factor:.2f}".rstrip("0").rstrip(".")

    ZRAM_CONF_DIR.mkdir(parents=True, exist_ok=True)
    content = f"""# Managed by Dusky Memory & Swap Manager
[zram0]
zram-size = {size_expr}
zram-resident-limit = {res_limit}
compression-algorithm = zstd(level=2)
swap-priority = 32767
options = discard
"""
    ZRAM0_CONF.write_text(content)
    
    # 1. Stop existing swap and setup units
    run_cmd(["swapoff", "/dev/zram0"], check=False)
    run_cmd(["systemctl", "stop", "dev-zram0.swap"], check=False)
    run_cmd(["systemctl", "stop", "systemd-zram-setup@zram0.service"], check=False)
    run_cmd(["zramctl", "--reset", "/dev/zram0"], check=False)
    
    # 2. Ensure device exists and reload systemd daemon
    ensure_zram_device("zram0")
    run_cmd(["systemctl", "daemon-reload"])
    
    # 3. Bring up new zram device and activate swap
    run_cmd(["/usr/lib/systemd/system-generators/zram-generator", "--setup-device", "zram0"], check=False)
    run_cmd(["swapon", "-p", "32767", "/dev/zram0"], check=False)
    
    ok(f"ZRAM0 configured successfully to: {size_expr} (Priority: 32767)")
    notify("ZRAM0 Updated", f"ZRAM0 swap configured to {size_expr} @ Max Priority 32767")

def disable_zram0():
    escalate_root_if_needed()
    info("Disabling ZRAM0 compressed swap...")
    run_cmd(["swapoff", "/dev/zram0"], check=False)
    run_cmd(["systemctl", "stop", "dev-zram0.swap"], check=False)
    run_cmd(["systemctl", "stop", "systemd-zram-setup@zram0.service"], check=False)
    run_cmd(["zramctl", "--reset", "/dev/zram0"], check=False)
    if ZRAM0_CONF.exists():
        ZRAM0_CONF.unlink()
    run_cmd(["systemctl", "daemon-reload"])
    ok("ZRAM0 compressed swap disabled completely.")
    notify("ZRAM0 Disabled", "ZRAM0 swap has been disabled (Zero RAM table overhead).")

def enable_zram0():
    escalate_root_if_needed()
    set_zram0_size("auto")

# =============================================================================
# ZRAM1 CONFIGURATION
# =============================================================================

def set_zram1_size(size_raw: str):
    escalate_root_if_needed()
    size_expr = parse_size_input(size_raw)
    info(f"Configuring ZRAM1 disk size expression: {C.BOLD}{size_expr}{C.RST}")

    if not ZRAM1_CONF.exists():
        set_zram1_backend("zram")
    
    content = f"""# Managed by Dusky Memory & Swap Manager
[zram1]
zram-size = {size_expr}
zram-resident-limit = ram * 4 / 5
fs-type = ext4
mount-point = /mnt/zram1
compression-algorithm = zstd(level=2)
options = rw,nosuid,nodev,discard,noatime,lazytime,X-mount.mode=1777
"""
    ZRAM1_CONF.write_text(content)
    run_cmd(["umount", "-q", "/mnt/zram1"], check=False)
    run_cmd(["systemctl", "stop", "systemd-zram-setup@zram1.service"], check=False)
    run_cmd(["zramctl", "--reset", "/dev/zram1"], check=False)
    ensure_zram_device("zram1")
    run_cmd(["systemctl", "daemon-reload"])
    run_cmd(["systemctl", "restart", "systemd-zram-setup@zram1.service"], check=False)
    ok(f"ZRAM1 disk size updated to: {size_expr}")
    notify("ZRAM1 Updated", f"/mnt/zram1 size set to {size_expr}")

def set_zram1_backend(backend: str):
    escalate_root_if_needed()
    mode = backend.lower().strip()
    script_206 = get_script_206()
    if mode in ("zram", "ext4"):
        info("Switching /mnt/zram1 to Ext4 ZRAM Block Device...")
        if script_206.exists():
            run_cmd(["python3", str(script_206), "--zram"])
        else:
            set_zram1_size("ram")
        ok("/mnt/zram1 configured as Ext4 ZRAM Block device.")
        notify("ZRAM1 Attached", "/mnt/zram1 configured as Ext4 ZRAM Block device.")
    elif mode in ("tmpfs", "ram"):
        info("Switching /mnt/zram1 to Pure Tmpfs RAM Mount...")
        if script_206.exists():
            run_cmd(["python3", str(script_206), "--tmpfs"])
        ok("/mnt/zram1 configured as Pure Tmpfs RAM disk.")
        notify("Tmpfs Attached", "/mnt/zram1 configured as Pure Tmpfs RAM disk.")
    elif mode in ("disable", "none", "off"):
        disable_zram1()
    else:
        die(f"Invalid backend: '{backend}'. Choose 'zram', 'tmpfs', or 'disable'.")

def disable_zram1():
    escalate_root_if_needed()
    info("Dismantling /mnt/zram1 secondary RAM disk...")
    script_206 = get_script_206()
    if script_206.exists():
        run_cmd(["python3", str(script_206), "--disable"])
    else:
        run_cmd(["systemctl", "stop", "systemd-zram-setup@zram1.service"], check=False)
        run_cmd(["umount", "-q", "/mnt/zram1"], check=False)
        run_cmd(["zramctl", "--reset", "/dev/zram1"], check=False)
        if ZRAM1_CONF.exists():
            ZRAM1_CONF.unlink()
        run_cmd(["systemctl", "daemon-reload"])
    ok("/mnt/zram1 disabled cleanly.")
    notify("ZRAM1 Disabled", "Secondary RAM disk /mnt/zram1 has been dismantled.")

# =============================================================================
# DISK SWAP CONFIGURATION (/swap/swapfile)
# =============================================================================

def parse_disk_swap_bytes(raw: str) -> int:
    s = raw.strip().upper()
    match = re.match(r"^([0-9.]+)\s*([KMGT]?B?)$", s)
    if not match:
        die(f"Invalid disk swap size format: '{raw}'. Examples: '4G', '8G', '512M'.")
    
    val = float(match.group(1))
    if val <= 0:
        die(f"Invalid disk swap size: '{raw}'. Size must be greater than 0.")
        
    unit = match.group(2)
    
    if unit in ("G", "GB", "GIB"):
        return int(val * 1024 * 1024 * 1024)
    elif unit in ("M", "MB", "MIB"):
        return int(val * 1024 * 1024)
    elif unit in ("K", "KB", "KIB"):
        return int(val * 1024)
    elif unit in ("T", "TB", "TIB"):
        return int(val * 1024 * 1024 * 1024 * 1024)
    else:
        return int(val * 1024 * 1024 * 1024) if val < 128 else int(val * 1024 * 1024)

def set_disk_swap_size(size_raw: str):
    escalate_root_if_needed()
    target_bytes = parse_disk_swap_bytes(size_raw)
    size_mb = int(target_bytes / (1024 * 1024))
    info(f"Allocating disk swapfile of {size_mb} MB at {SWAPFILE_PATH} (Priority: -1)...")

    # 1. Turn off active swapfile
    run_cmd(["swapoff", str(SWAPFILE_PATH)], check=False)

    # 2. Ensure parent directory exists
    SWAPFILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 3. Detect filesystem type for BTRFS vs Ext4/XFS
    fs_type = run_cmd(["findmnt", "-rn", "-o", "FSTYPE", "-T", str(SWAPFILE_PATH.parent)], check=False)
    
    if SWAPFILE_PATH.exists():
        SWAPFILE_PATH.unlink()

    if fs_type == "btrfs":
        info("BTRFS filesystem detected: Using native btrfs mkswapfile...")
        btrfs_res = run_cmd(["btrfs", "filesystem", "mkswapfile", "-s", f"{size_mb}M", str(SWAPFILE_PATH)], check=False)
        if not SWAPFILE_PATH.exists() or SWAPFILE_PATH.stat().st_size == 0:
            # Fallback for older btrfs-progs
            run_cmd(["touch", str(SWAPFILE_PATH)])
            run_cmd(["chattr", "+C", str(SWAPFILE_PATH)], check=False)
            run_cmd(["dd", "if=/dev/zero", f"of={SWAPFILE_PATH}", "bs=1M", f"count={size_mb}", "status=none"])
            run_cmd(["chmod", "0600", str(SWAPFILE_PATH)])
            run_cmd(["mkswap", "-U", "clear", str(SWAPFILE_PATH)])
    else:
        info(f"{fs_type or 'Standard'} filesystem detected: Allocating swapfile...")
        try:
            run_cmd(["fallocate", "-l", f"{size_mb}M", str(SWAPFILE_PATH)])
        except Exception:
            run_cmd(["dd", "if=/dev/zero", f"of={SWAPFILE_PATH}", "bs=1M", f"count={size_mb}", "status=none"])
        run_cmd(["chmod", "0600", str(SWAPFILE_PATH)])
        run_cmd(["mkswap", "-U", "clear", str(SWAPFILE_PATH)])

    # 4. Activate with lowest priority (-1)
    run_cmd(["swapon", "-p", "-1", str(SWAPFILE_PATH)])

    # 5. Update /etc/fstab atomically
    if FSTAB_PATH.exists():
        lines = FSTAB_PATH.read_text().splitlines()
        new_lines = []
        found = False
        for line in lines:
            if re.search(r"^\s*#?\s*/swap/swapfile\b", line):
                new_lines.append(f"{SWAPFILE_PATH} none swap defaults,pri=-1 0 0")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{SWAPFILE_PATH} none swap defaults,pri=-1 0 0")
        FSTAB_PATH.write_text("\n".join(new_lines) + "\n")

    ok(f"Disk swap active at {SWAPFILE_PATH} ({size_mb} MB, Priority: -1).")
    notify("Disk Swap Allocated", f"Disk swapfile created: {size_mb} MB @ Lowest Priority (-1)")

def disable_disk_swap():
    escalate_root_if_needed()
    info("Disabling disk swapfile...")
    run_cmd(["swapoff", str(SWAPFILE_PATH)], check=False)
    
    if FSTAB_PATH.exists():
        lines = FSTAB_PATH.read_text().splitlines()
        new_lines = []
        for line in lines:
            if re.search(r"^\s*/swap/swapfile\b", line):
                new_lines.append(f"# {line.strip()}")
            else:
                new_lines.append(line)
        FSTAB_PATH.write_text("\n".join(new_lines) + "\n")
    
    ok("Disk swap disabled and commented out in /etc/fstab.")
    notify("Disk Swap Disabled", "Disk swap has been turned off.")

def enable_disk_swap():
    escalate_root_if_needed()
    if not SWAPFILE_PATH.exists():
        set_disk_swap_size("4G")
        return
        
    run_cmd(["swapon", "-p", "-1", str(SWAPFILE_PATH)], check=False)
    if FSTAB_PATH.exists():
        lines = FSTAB_PATH.read_text().splitlines()
        new_lines = []
        for line in lines:
            if re.search(r"^\s*#\s*/swap/swapfile\b", line):
                new_lines.append(re.sub(r"^\s*#\s*", "", line))
            else:
                new_lines.append(line)
        FSTAB_PATH.write_text("\n".join(new_lines) + "\n")
    ok("Disk swap enabled @ Priority -1.")
    notify("Disk Swap Enabled", "Disk swap activated @ Lowest Priority (-1).")

# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Elite ZRAM & Disk Swap Subsystem Manager")
    
    # Status Queries
    parser.add_argument("--status", action="store_true", help="Print complete status overview")
    parser.add_argument("--zram0-status", action="store_true", help="Print ZRAM0 status string")
    parser.add_argument("--zram0-size", action="store_true", help="Print ZRAM0 size expression")
    parser.add_argument("--zram1-status", action="store_true", help="Print ZRAM1 status string")
    parser.add_argument("--zram1-size", action="store_true", help="Print ZRAM1 size expression")
    parser.add_argument("--disk-swap-status", action="store_true", help="Print Disk Swap status string")
    parser.add_argument("--disk-swap-size", action="store_true", help="Print Disk Swap size string")
    
    # Mutators
    parser.add_argument("--set-zram0-size", metavar="EXPR", help="Set ZRAM0 size expression (e.g. 'ram * 0.8', '4G')")
    parser.add_argument("--disable-zram0", action="store_true", help="Disable ZRAM0 compressed swap completely")
    parser.add_argument("--enable-zram0", action="store_true", help="Enable ZRAM0 compressed swap")
    
    parser.add_argument("--set-zram1-size", metavar="EXPR", help="Set ZRAM1 disk size expression (e.g. 'ram', '8G')")
    parser.add_argument("--set-zram1-backend", choices=["zram", "tmpfs", "disable"], help="Switch /mnt/zram1 backend")
    parser.add_argument("--disable-zram1", action="store_true", help="Disable /mnt/zram1 secondary RAM disk")
    
    parser.add_argument("--set-disk-swap-size", metavar="SIZE", help="Resize/create disk swapfile (e.g. '4G', '8G')")
    parser.add_argument("--disable-disk-swap", action="store_true", help="Disable disk swapfile")
    parser.add_argument("--enable-disk-swap", action="store_true", help="Enable disk swapfile")
    
    args = parser.parse_args()

    # Query Handlers
    if args.zram0_status:
        print(get_zram0_status())
        return
    if args.zram0_size:
        print(get_zram0_size())
        return
    if args.zram1_status:
        print(get_zram1_status())
        return
    if args.zram1_size:
        print(get_zram1_size())
        return
    if args.disk_swap_status:
        print(get_disk_swap_status())
        return
    if args.disk_swap_size:
        print(get_disk_swap_size())
        return
    if args.status:
        print_full_status()
        return

    # Mutation Handlers
    if args.set_zram0_size:
        set_zram0_size(args.set_zram0_size)
    elif args.disable_zram0:
        disable_zram0()
    elif args.enable_zram0:
        enable_zram0()
    elif args.set_zram1_size:
        set_zram1_size(args.set_zram1_size)
    elif args.set_zram1_backend:
        set_zram1_backend(args.set_zram1_backend)
    elif args.disable_zram1:
        disable_zram1()
    elif args.set_disk_swap_size:
        set_disk_swap_size(args.set_disk_swap_size)
    elif args.disable_disk_swap:
        disable_disk_swap()
    elif args.enable_disk_swap:
        enable_disk_swap()
    else:
        print_full_status()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C.YLW}Operation cancelled by user.{C.RST}")
        sys.exit(130)
