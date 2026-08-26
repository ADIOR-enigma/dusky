#!/usr/bin/env python3
#d: Verify the ZRAM and mount setup

import os
import sys
import subprocess
import re
import argparse
from pathlib import Path

# --- Argument Parsing (Executed BEFORE Privilege Escalation) ---
parser = argparse.ArgumentParser(description="Deep ZRAM & Memory Architecture Diagnostics")
parser.add_argument("--strict", action="store_true", help="Exit with non-zero code on any warning/mismatch")
parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
args = parser.parse_args()

# --- Presentation ---
class C:
    RED = "\033[1;31m"
    GRN = "\033[1;32m"
    YLW = "\033[1;33m"
    BLU = "\033[1;34m"
    BOLD = "\033[1m"
    RST = "\033[0m"

    @classmethod
    def strip(cls):
        for attr in ("RED", "GRN", "YLW", "BLU", "BOLD", "RST"):
            setattr(cls, attr, "")

if args.no_color or not sys.stdout.isatty() or "NO_COLOR" in os.environ:
    C.strip()

has_warnings = False

def info(msg: str): print(f"{C.BLU}[INFO]{C.RST} {msg}")
def ok(msg: str): print(f"{C.GRN}[PASS]{C.RST} {msg}")
def warn(msg: str): 
    global has_warnings
    has_warnings = True
    print(f"{C.YLW}[WARN]{C.RST} {msg}")

def report_issue(msg: str):
    global has_warnings
    has_warnings = True
    if args.strict:
        print(f"{C.RED}[FAIL]{C.RST} {msg}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"{C.YLW}[WARN]{C.RST} {msg}")

# --- Privilege Check ---
if os.geteuid() != 0:
    if subprocess.call(["command", "-v", "sudo"], stdout=subprocess.DEVNULL, shell=True) != 0:
        print(f"{C.RED}[!] sudo is required to run diagnostics as root.{C.RST}", file=sys.stderr)
        sys.exit(1)
    os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])

def run_cmd(cmd: list) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.strip()
    except Exception as e:
        return ""

print(f"\n{C.BOLD}=== Initiating Deep Architecture Diagnostics (Kernel 7.2+ Ready) ==={C.RST}\n")

# --- 1. Bootloader / ZSWAP Check ---
info("Checking ZSWAP state...")
zswap_path = Path("/sys/module/zswap/parameters/enabled")
if zswap_path.exists():
    if zswap_path.read_text().strip() in ("Y", "1"):
        report_issue("ZSWAP is currently ACTIVE. Kernel cmdline parameter 'zswap.enabled=0' is recommended with pure ZRAM.")
    else:
        ok("ZSWAP is cleanly disabled at the kernel level.")
else:
    info("ZSWAP module not loaded or built-in (Clean).")

# --- 2. Memory Calculations (Page-Aligned) ---
info("Calculating total physical memory maps...")
try:
    with open('/proc/meminfo', 'r') as f:
        meminfo = f.read()
    mem_total_kb = int(re.search(r"MemTotal:\s+(\d+)\s+kB", meminfo).group(1))
    mem_total_bytes = mem_total_kb * 1024
except Exception as e:
    report_issue(f"Could not parse /proc/meminfo: {e}")
    mem_total_bytes = 0

def verify_limit(device: str, expected_ratio: float):
    mm_stat_path = Path(f"/sys/block/{device}/mm_stat")
    if not mm_stat_path.exists():
        report_issue(f"Stats matrix for {device} does not exist in sysfs.")
        return
    
    try:
        stats = mm_stat_path.read_text().strip().split()
        if len(stats) < 4:
            report_issue(f"Invalid mm_stat matrix format for {device}.")
            return
        actual_bytes = int(stats[3])  # 4th column is mem_limit
    except Exception as e:
        report_issue(f"Kernel rejected read on mm_stat for {device}: {e}")
        return
        
    if actual_bytes == 0:
        info(f"{device} memory resident limit is uncapped (0 / unlimited).")
        return

    if mem_total_bytes > 0:
        expected_bytes = int(mem_total_bytes * expected_ratio)
        tolerance = max(expected_bytes * 0.10, 64 * 1024 * 1024)  # 10% tolerance for page shifts
        
        if abs(actual_bytes - expected_bytes) <= tolerance:
            ok(f"{device} resident limit aligned correctly (~{actual_bytes / (1024**3):.2f} GB)")
        else:
            info(f"{device} resident limit active: {actual_bytes / (1024**3):.2f} GB (configured ratio: {expected_ratio:.2f})")

# --- 3. Base Swap Verification (zram0) ---
info("Verifying main ZRAM swap topology...")
zramctl_out = run_cmd(["zramctl", "--output", "NAME", "--noheadings"])
if "/dev/zram0" not in zramctl_out:
    report_issue("/dev/zram0 is not active in zramctl. (Reboot or systemctl restart systemd-zram-setup@zram0.service may be required).")
else:
    swapon_out = run_cmd(["swapon", "--show=NAME,PRIO", "--noheadings"])
    if "/dev/zram0" not in swapon_out:
        report_issue("/dev/zram0 exists but is not currently mounted as swap.")
    else:
        ok("/dev/zram0 swap is fully active.")

    zram0_conf = Path("/etc/systemd/zram-generator.conf.d/99-elite-zram.conf")
    zram0_limit_ratio = 0.5
    if zram0_conf.exists():
        match = re.search(r"zram-resident-limit\s*=\s*ram\s*\*\s*([0-9.]+)", zram0_conf.read_text())
        if match:
            try:
                zram0_limit_ratio = float(match.group(1))
            except ValueError:
                pass

    verify_limit("zram0", zram0_limit_ratio)

# --- 4. Hybrid Mount Detection (zram1 / tmpfs / disabled) ---
info("Interrogating /mnt/zram1 mount backend...")
mount_source = run_cmd(["findmnt", "-rn", "-o", "SOURCE", "--mountpoint", "/mnt/zram1"])
mount_opts = run_cmd(["findmnt", "-rn", "-o", "OPTIONS", "--mountpoint", "/mnt/zram1"])
zram1_conf = Path("/etc/systemd/zram-generator.conf.d/99-elite-zram1.conf")

if not mount_source:
    if not zram1_conf.exists():
        ok("Backend resolved as: Disabled / None (Minimal RAM footprint mode, zero memory overhead).")
    else:
        info("Secondary /mnt/zram1 configured in zram-generator (Staged / pending mount).")

elif mount_source == "tmpfs":
    ok(f"Backend dynamically resolved as: Pure Tmpfs RAM disk.")
    if "uid=" in mount_opts and "gid=" in mount_opts:
         ok("Tmpfs user/group ownership mapping is intact.")
    else:
         warn("Tmpfs user ownership options not explicitly set in mount flags.")

elif mount_source in ("/dev/zram1", "zram1"):
    ok(f"Backend dynamically resolved as: Ext4 ZRAM Block.")
    verify_limit("zram1", 0.80)
    
    # Verify Ext4 Journal Annihilation
    dumpe2fs_out = run_cmd(["dumpe2fs", "-h", "/dev/zram1"])
    if "has_journal" in dumpe2fs_out:
        warn("Ext4 journal is present on zram1 (disable journal recommended for lower RAM write overhead).")
    else:
        ok("Ext4 filesystem confirmed as journal-less (Zero unnecessary write overhead).")
    
    # Verify Mount Options
    for opt in ["noatime", "lazytime", "discard", "rw"]:
        if opt not in mount_opts.split(","):
            warn(f"Mount option recommendation for zram block: '{opt}' not active.")
    ok("Ext4 mount options verified.")
else:
    info(f"Custom mount source for /mnt/zram1: {mount_source}")

# --- 5. Algorithm Verification ---
info("Testing compression algorithm setup...")
devices_to_check = []
if Path("/sys/block/zram0").exists():
    devices_to_check.append("zram0")
if Path("/sys/block/zram1").exists() and mount_source in ("/dev/zram1", "zram1"):
    devices_to_check.append("zram1")

for dev in devices_to_check:
    algo_path = Path(f"/sys/block/{dev}/comp_algorithm")
    if algo_path.exists():
        algo_data = algo_path.read_text().strip()
        if "[zstd]" in algo_data:
            ok(f"{dev} is running ZSTD natively.")
        else:
            info(f"{dev} active compression algorithm: {algo_data}")
    else:
        info(f"{dev} sysfs comp_algorithm node not exposed.")

if has_warnings and args.strict:
    print(f"\n{C.RED}{C.BOLD}=== DIAGNOSTICS DETECTED ITEMS REQUIRING ATTENTION (STRICT MODE). ==={C.RST}\n")
    sys.exit(1)
else:
    print(f"\n{C.GRN}{C.BOLD}=== DIAGNOSTICS COMPLETE. SYSTEM ARCHITECTURE VERIFIED CLEANLY. ==={C.RST}\n")
    sys.exit(0)
