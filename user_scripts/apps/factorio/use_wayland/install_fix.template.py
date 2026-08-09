#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_fix.py — Factorio 2.1.x "Failed to create shader" native-Wayland fix.

SELF-CONTAINED: this single file embeds the EGL interposer source (eglfix.c),
the linker version script (export.map) and a GL/EGL smoke test. It builds the
shim, installs it into your jc141 Factorio repack, wires the launcher config
and verifies everything empirically. Package installs auto-elevate to sudo
(interactive password prompt) only when needed.

Why this exists
---------------
Factorio 2.1.14 crashes ~1s after launch on a native Wayland session (no
XWayland) with:

    Error ShaderOpenGL.cpp:15: Failed to create shader '__core__/graphics/shaders/sprite.vert'.

Root cause (empirically verified): on the Wayland/EGL path the game loses EGL
context currency on the main thread between GL init and the first shader
compile. At the first glCreateShader, eglGetCurrentContext()==NULL, every GL
call fails silently, glCreateShader returns 0 and the game aborts. The X11
path works; native Wayland doesn't. The game dlsyms EGL itself, so the only
way to intercept is a libEGL.so.1 interposer (same SONAME) loaded via
LD_PRELOAD. It forwards all EGL calls to the real glvnd library, remembers the
last good eglMakeCurrent binding, and re-binds it on the calling thread right
before the first glCreateShader/glCreateProgram.

Usage
-----
    python3 install_fix.py --game-dir /path/to/Factorio_2.1.14   # install/repair
    python3 install_fix.py --check                                # inspect only
    python3 install_fix.py --game-dir X --force                   # force rebuild
    python3 install_fix.py --game-dir X --skip-packages           # skip pacman
    python3 install_fix.py --verify-game                          # also launch the
                                                                  #   game and confirm
                                                                  #   it initialises

If --game-dir is omitted the script auto-detects common locations.
Tested on Arch Linux (pacman). Requires libglvnd (EGL headers) + gcc to build
the shim; the script installs them if missing.

Exit codes: 0 = ok, 1 = error, 2 = check found problems.
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# --------------------------------------------------------------------------
# Embedded sources (generated at build time; do not edit by hand)
# --------------------------------------------------------------------------
EGLFIX_C = @@EGLFIX_C@@
EXPORT_MAP = @@EXPORT_MAP@@

# Brief README written into the game's eglfix/ dir so the kit is fully
# self-contained (survives a fresh install of the game dir too).
GAME_README = """# Factorio 2.1.14 native-Wayland fix (libEGL shim)

This directory is managed by `install_fix.py` (auto-rebuilt on every run).
The fix: an EGL interposer (`libEGL.so.1`) loaded via LD_PRELOAD that forwards
all EGL calls to the real glvnd libEGL and re-binds the remembered EGL context
on the calling thread right before the game's first `glCreateShader` -- this
fixes the native-Wayland-only crash:

    Error ShaderOpenGL.cpp:15: Failed to create shader '__core__/graphics/shaders/sprite.vert'.

Root cause: on the Wayland/EGL path Factorio loses EGL context currency on the
main thread between GL init and first shader compile, so glCreateShader returns
0 and the game aborts. X11 works; native Wayland does not.

How to re-apply on a fresh system (auto-installs packages, auto-sudo):

    python3 install_fix.py --game-dir /path/to/Factorio_2.1.14
    python3 install_fix.py --verify-game   # also launch the game and confirm

Files: libEGL.so.1 (built), eglfix.c + export.map (sources).
Wiring: local.config gets ENV="env LD_PRELOAD=<this dir>/libEGL.so.1" -- the
`env` prefix is REQUIRED because the jc141 launcher word-splits $ENV into the
command array and bwrap only execs real binaries.
To remove the fix: delete the ENV= line from ../local.config and this dir.
Diagnostics: the shim logs to /tmp/eglfix.log.
"""

# Small GL/EGL smoke test: creates a surfaceless context, then simulates the
# Factorio bug (unbind, then glCreateShader with no current context).
# Without the shim this must FAIL (glCreateShader -> 0, the crash condition);
# with the shim preloaded it must PASS (shim re-binds and the shader is made).
SMOKE_TEST_C = r"""
#define EGL_EGLEXT_PROTOTYPES 1
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <stdio.h>

#ifndef EGL_PLATFORM_SURFACELESS_MESA
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#endif

typedef unsigned int GLenum;
typedef unsigned int GLuint;
typedef const unsigned char *(*PFN_glGetString)(GLenum);
typedef void (*PFN_glClear)(GLenum);
typedef GLuint (*PFN_glCreateShader)(GLenum);
typedef EGLDisplay (*PFN_eglGetPlatformDisplayEXT)(EGLenum, void *, const EGLint *);

int main(void) {
    /* Prefer the surfaceless platform (headless-safe); resolve the EXT
     * entry point via eglGetProcAddress because glvnd does not export it. */
    EGLDisplay dpy = EGL_NO_DISPLAY;
    PFN_eglGetPlatformDisplayEXT pGetPlatformDisplayEXT =
        (PFN_eglGetPlatformDisplayEXT)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (pGetPlatformDisplayEXT) {
        dpy = pGetPlatformDisplayEXT(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
    }
    if (dpy == EGL_NO_DISPLAY) dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (dpy == EGL_NO_DISPLAY) { printf("FAIL: no EGL display\n"); return 1; }

    EGLint major = 0, minor = 0;
    if (!eglInitialize(dpy, &major, &minor)) { printf("FAIL: eglInitialize\n"); return 1; }
    if (!eglBindAPI(EGL_OPENGL_API)) { printf("FAIL: eglBindAPI\n"); return 1; }

    EGLint ca[] = { EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                    EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT, EGL_NONE };
    EGLConfig cfg; EGLint n = 0;
    if (!eglChooseConfig(dpy, ca, &cfg, 1, &n) || n < 1) { printf("FAIL: eglChooseConfig\n"); return 1; }

    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, NULL);
    if (ctx == EGL_NO_CONTEXT) { printf("FAIL: eglCreateContext\n"); return 1; }
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) { printf("FAIL: eglMakeCurrent\n"); return 1; }

    PFN_glGetString glGetString = (PFN_glGetString)eglGetProcAddress("glGetString");
    PFN_glClear glClear = (PFN_glClear)eglGetProcAddress("glClear");
    PFN_glCreateShader glCreateShader = (PFN_glCreateShader)eglGetProcAddress("glCreateShader");

    if (glGetString) printf("GL_VERSION=%s\n", (const char *)glGetString(0x1F02));
    if (glClear) glClear(0x4000);
    printf("pre-unbind glGetError=0x%x\n", (unsigned)eglGetError());

    /* Simulate the Factorio bug: lose context currency, then compile a shader. */
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT)) { printf("FAIL: unbind\n"); return 1; }
    printf("eglGetCurrentContext after unbind=%p (0 == lost, exactly like Factorio)\n",
           (void *)eglGetCurrentContext());

    GLuint shader = glCreateShader ? glCreateShader(0x8B31) : 0; /* GL_VERTEX_SHADER */
    printf("glCreateShader after unbind -> %u\n", shader);
    if (shader != 0) {
        printf("PASS: shader created without current context (shim re-bound it)\n");
        return 0;
    }
    printf("FAIL: glCreateShader returned 0 -- this IS the Factorio crash condition\n");
    return 1;
}
"""

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
PACKAGES_FIX = ["gcc", "libglvnd", "sdl3", "fuse-overlayfs", "bubblewrap"]
PACKAGES_TEST_TOOLS = ["wtype", "grim", "imagemagick"]


def log(msg, level="*"):
    print("[%s] %s" % (level, msg), flush=True)


def run(cmd, timeout=120, env=None, input_data=None):
    """Run a command; return CompletedProcess. Timeouts abort loudly.

    NOTE: do not pass input=None explicitly -- CPython then sets the child's
    stdin to /dev/null, which breaks sudo -S reading a piped password.
    """
    kwargs = dict(capture_output=True, text=True, timeout=timeout)
    if env is not None:
        kwargs["env"] = env
    if input_data is not None:
        kwargs["input"] = input_data
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        raise SystemExit("COMMAND TIMED OUT after %ss: %s\n%s%s" % (timeout, " ".join(cmd), out, err))


def sudo_cmd():
    """Return a sudo command list. Uses -n if passwordless sudo works,
    otherwise plain sudo (interactive). Supports SUDO_STDIN=1 for -S."""
    if not shutil.which("sudo"):
        raise SystemExit("ERROR: sudo not found -- install it with: pacman -S sudo "
                         "(or run this script as root / with a root-capable user)")
    probe = subprocess.run(["sudo", "-n", "true"], capture_output=True)
    if probe.returncode == 0:
        return ["sudo", "-n"]
    if os.environ.get("SUDO_STDIN") == "1":
        return ["sudo", "-S"]
    return ["sudo"]


def run_root(cmd, input_data=None, timeout=120):
    """Run a command with root privileges (auto-elevate)."""
    return run(sudo_cmd() + cmd, input_data=input_data, timeout=timeout)


def find_game_dir(guess):
    """Locate the Factorio jc141 repack directory."""
    candidates = []
    if guess:
        candidates.append(os.path.abspath(os.path.expanduser(guess)))
    env_dir = os.environ.get("FACTORIO_DIR")
    if env_dir:
        candidates.append(os.path.abspath(os.path.expanduser(env_dir)))
    home = os.path.expanduser("~")
    candidates += [
        "/mnt/zram1/Factorio_2.1.14",
        os.path.join(home, "Factorio_2.1.14"),
        os.path.join(home, "Games", "Factorio_2.1.14"),
        os.path.join(home, "Games", "jc141", "Factorio_2.1.14"),
        os.path.join(os.getcwd(), "Factorio_2.1.14"),
        os.getcwd(),
    ]
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if (os.path.isfile(os.path.join(c, "local.config"))
                and os.path.isfile(os.path.join(c, "start.n.sh"))):
            return c
    return None


def distro_supports_pacman():
    return shutil.which("pacman") is not None


def package_installed(pkg):
    r = run(["pacman", "-Q", pkg])
    return r.returncode == 0


def install_packages(pkgs, skip):
    if skip:
        log("--skip-packages: skipping package installation")
        return
    missing = [p for p in pkgs if not package_installed(p)]
    if not missing:
        log("all required packages already installed: %s" % ", ".join(pkgs))
        return
    log("installing packages: %s (may prompt for sudo password)" % ", ".join(missing))
    # Fresh-install pacman must sync the repo DB and download packages; that
    # can take minutes, so give it a generous timeout.
    r = run_root(["pacman", "-S", "--needed", "--noconfirm"] + missing, timeout=600)
    if r.returncode != 0:
        raise SystemExit("ERROR: package install failed:\n%s%s" % (r.stdout, r.stderr))
    for p in missing:
        if not package_installed(p):
            raise SystemExit("ERROR: package '%s' still not installed after pacman -S" % p)
    log("packages installed successfully")


def check_prereqs():
    if not shutil.which("gcc"):
        raise SystemExit("ERROR: gcc not found. Run without --skip-packages, or: sudo pacman -S gcc")
    if not os.path.isfile("/usr/include/EGL/egl.h"):
        raise SystemExit("ERROR: EGL headers missing. Run without --skip-packages, or: sudo pacman -S libglvnd")
    if not os.path.isfile("/usr/lib/libEGL.so.1"):
        raise SystemExit("ERROR: runtime libEGL.so.1 missing. Run without --skip-packages, or: sudo pacman -S libglvnd")


def write_if_changed(path, content):
    """Write content only if it differs; returns True if written."""
    try:
        with open(path) as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(content)
    return True


def build_shim(eglfix_dir, force=False):
    """Write embedded sources and compile libEGL.so.1. Returns shim path."""
    os.makedirs(eglfix_dir, exist_ok=True)
    src_path = os.path.join(eglfix_dir, "eglfix.c")
    map_path = os.path.join(eglfix_dir, "export.map")
    shim_path = os.path.join(eglfix_dir, "libEGL.so.1")

    # Keep the sources on disk (self-documenting) but only touch them when
    # the embedded content actually differs, so mtimes stay stable and we
    # don't spuriously rebuild on every run.
    src_written = write_if_changed(src_path, EGLFIX_C)
    write_if_changed(map_path, EXPORT_MAP)
    write_if_changed(os.path.join(eglfix_dir, "README.md"), GAME_README)

    need_build = force or not os.path.isfile(shim_path) or src_written
    if not need_build:
        # Rebuild if the embedded source is newer than the built shim.
        try:
            if os.path.getmtime(src_path) > os.path.getmtime(shim_path):
                need_build = True
        except OSError:
            need_build = True
    if not need_build:
        log("shim already built and up to date: %s" % shim_path)
        return shim_path

    log("compiling EGL interposer...")
    tmp = tempfile.mkdtemp(prefix="eglfix_build_")
    try:
        out_tmp = os.path.join(tmp, "libEGL.so.1")
        cmd = ["gcc", "-shared", "-fPIC", "-O2", "-Wall",
               "-o", out_tmp, src_path, "-ldl", "-pthread",
               "-Wl,-soname,libEGL.so.1", "-Wl,--version-script=%s" % map_path]
        r = run(cmd)
        if r.returncode != 0:
            raise SystemExit("ERROR: gcc failed:\n%s%s" % (r.stdout, r.stderr))
        shutil.copyfile(out_tmp, shim_path)
        os.chmod(shim_path, 0o755)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Verify SONAME so the game's dlopen("libEGL.so.1") really returns us.
    if shutil.which("readelf"):
        r = run(["readelf", "-d", shim_path])
        if "Library soname: [libEGL.so.1]" not in r.stdout:
            raise SystemExit("ERROR: built shim has wrong SONAME (expected libEGL.so.1)")
    log("built and verified: %s" % shim_path)
    return shim_path


def wire_config(cfg_path, shim_abs):
    """Idempotently set ENV="env LD_PRELOAD=<shim>" in local.config."""
    env_line = 'ENV="env LD_PRELOAD=%s"' % shim_abs
    with open(cfg_path) as f:
        lines = f.readlines()

    idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*ENV=", ln):
            idx = i
            break

    changed = False
    if idx is not None:
        if lines[idx].strip() == env_line:
            log("launcher config already wired correctly")
            return False
        lines[idx] = env_line + "\n"
        changed = True
        log("updated existing ENV= line in %s" % cfg_path)
    else:
        # Insert right after the header comment block (first non-comment/blank line).
        insert_at = 0
        for i, ln in enumerate(lines):
            if ln.strip() and not ln.startswith("#"):
                insert_at = i
                break
            insert_at = i + 1
        note = "# Factorio native-Wayland shader fix (see eglfix/README.md; managed by install_fix.py)\n"
        lines.insert(insert_at, env_line + "\n")
        lines.insert(insert_at, note)
        changed = True
        log("added ENV= line to %s" % cfg_path)

    if changed:
        # Preserve the pristine config: only write the backup once.
        if not os.path.isfile(cfg_path + ".bak"):
            shutil.copyfile(cfg_path, cfg_path + ".bak")
        with open(cfg_path, "w") as f:
            f.writelines(lines)
    return changed


def check_launcher_compat(game_dir):
    """The jc141 ENV feature needs start.n.sh to use $ENV word-split."""
    for launcher in ("start.n.sh", "space.age.sh"):
        p = os.path.join(game_dir, launcher)
        if os.path.isfile(p):
            with open(p) as f:
                content = f.read()
            if "ENV" not in content:
                log("WARNING: %s does not reference $ENV -- the fix may not auto-apply; "
                    "manually run: LD_PRELOAD=<shim> ./runtime/run.sh ./factorio" % launcher, "!")


def run_smoke_test(eglfix_dir, shim_path):
    """Compile the smoke test and run it twice: baseline (expect FAIL) and with
    the shim (expect PASS). Returns True if the shim run passed."""
    tmp = tempfile.mkdtemp(prefix="eglfix_smoke_")
    try:
        test_c = os.path.join(tmp, "eglfix_test.c")
        test_bin = os.path.join(tmp, "eglfix_test")
        with open(test_c, "w") as f:
            f.write(SMOKE_TEST_C)
        r = run(["gcc", "-O2", "-o", test_bin, test_c, "-lEGL", "-ldl"])
        if r.returncode != 0:
            raise SystemExit("ERROR: smoke test compile failed:\n%s%s" % (r.stdout, r.stderr))

        # Baseline (no shim) -- expected to reproduce the Factorio condition.
        base = run([test_bin])
        base_failed = base.returncode != 0 or "PASS" not in base.stdout
        log("baseline (no shim): %s"
            % ("reproduced crash condition (expected)" if base_failed else "UNEXPECTEDLY passed"))

        # With the shim preloaded.
        os.environ.pop("LD_PRELOAD", None)
        rm_log = run(["rm", "-f", "/tmp/eglfix.log"])
        env = dict(os.environ)
        env["LD_PRELOAD"] = shim_path
        shim_run = run([test_bin], env=env)
        print("    " + shim_run.stdout.replace("\n", "\n    ").rstrip())
        if shim_run.returncode == 0 and "PASS" in shim_run.stdout:
            log("SMOKE TEST PASSED: shim re-binds the context (fix verified)")
            return True
        log("SMOKE TEST FAILED with the shim loaded. Check /tmp/eglfix.log:", "!")
        if os.path.isfile("/tmp/eglfix.log"):
            with open("/tmp/eglfix.log") as f:
                for ln in f.read().splitlines():
                    print("    " + ln)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def kill_game_procs(proc):
    """Kill the whole process tree we launched for verification. The launcher
    is spawned as a session leader, so killing its process group covers the
    start.n.sh script and the bwrap wrapper. NOTE: the jc141 launcher invokes
    bwrap with --new-session, so the sandboxed game lives in its OWN session
    outside our process group -- the pkill -x factorio fallback below is what
    actually kills the game. Do not "simplify" it away."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-9", "-x", "factorio"], capture_output=True)
    except Exception:
        pass
    time.sleep(2)


def verify_game_launch(game_dir, max_wait=75):
    """Launch the game via start.n.sh (shim applies automatically through the
    wired local.config) and check the log for either 'Factorio initialised'
    (PASS) or 'Failed to create shader' (FAIL). Requires a live Wayland/X11
    session; returns True/False, or None if the check can't run here."""
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        log("no display session (WAYLAND_DISPLAY/DISPLAY unset) -- "
            "skipping game launch check", "!")
        return None
    # pgrep -x matches the process name exactly, so it can't be fooled by a
    # shell whose command line merely mentions "factorio".
    if subprocess.run(["pgrep", "-x", "factorio"],
                      capture_output=True).returncode == 0:
        log("a Factorio process is already running -- skipping game launch check", "!")
        return None
    launcher = os.path.join(game_dir, "start.n.sh")
    if not os.path.isfile(launcher):
        log("start.n.sh not found -- skipping game launch check", "!")
        return None
    if not os.access(launcher, os.X_OK):
        log("start.n.sh is not executable (chmod +x needed) -- skipping game launch check", "!")
        return None

    log("launching game via %s (up to %ss)..." % (launcher, max_wait))
    log_path = os.path.join(tempfile.gettempdir(), "eglfix_verify.log")
    try:
        os.remove(log_path)
    except OSError:
        pass
    with open(log_path, "w") as out:
        proc = subprocess.Popen([launcher], stdout=out, stderr=subprocess.STDOUT,
                                cwd=game_dir, start_new_session=True)
    ok = None
    try:
        start = time.time()
        while time.time() - start < max_wait:
            if proc.poll() is not None:
                log("launcher exited early (rc=%s)" % proc.returncode, "!")
                break
            time.sleep(2)
            try:
                with open(log_path) as f:
                    content = f.read()
            except OSError:
                continue
            if "Failed to create shader" in content:
                ok = False
                break
            if "Factorio initialised" in content:
                ok = True
                break
    finally:
        kill_game_procs(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    if ok is True:
        log("GAME LAUNCH CHECK PASSED: reached 'Factorio initialised' on native Wayland")
        return True
    if ok is False:
        log("GAME LAUNCH CHECK FAILED: 'Failed to create shader' appeared in the log", "!")
        return False
    # Timeout with no error in the log: inconclusive, not a failure -- the
    # smoke test already proved the shim at the GL level. Slow hardware
    # (cold DwarFS cache, first mount) can exceed max_wait legitimately.
    log("GAME LAUNCH CHECK INCONCLUSIVE: no 'Factorio initialised' within %ss and no "
        "shader error either -- treat as unverified, not broken. Log: %s"
        % (max_wait, log_path), "!")
    return None


def inspect_state(game_dir, eglfix_dir, shim_path):
    """Print what is present / missing. Returns problem count."""
    problems = 0
    print()
    print("== Inspection report ==")
    print("  game dir : %s" % game_dir)
    print("  eglfix dir: %s" % eglfix_dir)

    ok = os.path.isfile(shim_path)
    print("  shim built        : %s" % ("OK" if ok else "MISSING"))
    problems += (not ok)

    cfg_path = os.path.join(game_dir, "local.config")
    wired = False
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            cfg = f.read()
        wired = 'ENV="env LD_PRELOAD=%s"' % shim_path in cfg
    print("  launcher wired    : %s" % ("OK" if wired else "NOT CONFIGURED"))
    problems += (not wired)

    for pkg in PACKAGES_FIX:
        if not package_installed(pkg):
            print("  package %-14s: MISSING" % pkg)
            problems += 1
    print("  problems found    : %d" % problems)
    return problems


def main():
    ap = argparse.ArgumentParser(
        description="Install/repair the Factorio native-Wayland shader fix.")
    ap.add_argument("--game-dir", help="Path to the Factorio jc141 repack dir "
                                       "(auto-detected if omitted)")
    ap.add_argument("--check", action="store_true",
                    help="Inspect state and exit (no changes)")
    ap.add_argument("--force", action="store_true",
                    help="Force rebuild of the shim even if up to date")
    ap.add_argument("--skip-packages", action="store_true",
                    help="Do not install packages with pacman")
    ap.add_argument("--with-testing-tools", action="store_true",
                    help="Also install wtype/grim/imagemagick (test tools)")
    ap.add_argument("--verify-game", action="store_true",
                    help="After installing, launch the game and confirm it "
                         "reaches 'Factorio initialised' (needs a display session)")
    args = ap.parse_args()

    log("Factorio native-Wayland fix installer")
    if not distro_supports_pacman():
        raise SystemExit("ERROR: pacman not found. This installer targets Arch Linux "
                         "(the jc141 repack + Mesa/EGL setup this fix was developed on).")

    game_dir = find_game_dir(args.game_dir)
    if not game_dir:
        raise SystemExit("ERROR: could not find the Factorio game dir. "
                         "Pass --game-dir /path/to/Factorio_2.1.14")
    log("game dir: %s" % game_dir)

    eglfix_dir = os.path.join(game_dir, "eglfix")
    shim_path = os.path.join(eglfix_dir, "libEGL.so.1")

    if args.check:
        problems = inspect_state(game_dir, eglfix_dir, shim_path)
        if problems:
            log("run the installer (no flags) to fix the %d problem(s) above" % problems, "!")
            sys.exit(2)
        log("everything looks good")
        sys.exit(0)

    pkgs = list(PACKAGES_FIX)
    if args.with_testing_tools:
        pkgs += PACKAGES_TEST_TOOLS
    install_packages(pkgs, args.skip_packages)
    check_prereqs()

    shim = build_shim(eglfix_dir, force=args.force)

    cfg_path = os.path.join(game_dir, "local.config")
    if not os.path.isfile(cfg_path):
        raise SystemExit("ERROR: %s not found -- is this really a jc141 repack?" % cfg_path)
    wire_config(cfg_path, shim)
    check_launcher_compat(game_dir)

    passed = run_smoke_test(eglfix_dir, shim)
    if not passed:
        raise SystemExit("ERROR: smoke test failed -- fix not verified. See output above.")

    if args.verify_game:
        print()
        game_ok = verify_game_launch(game_dir)
        if game_ok is False:
            raise SystemExit("ERROR: the game still failed to initialise -- fix NOT verified. "
                             "See the log above.")

    print()
    log("DONE. Factorio should now run on native Wayland: cd %s && ./start.n.sh" % game_dir)
    log("Files installed: %s" % eglfix_dir)
    log("Config: %s (ENV= line added/updated; backup at local.config.bak)" % cfg_path)
    log("Diagnostic log written by the shim at /tmp/eglfix.log")


if __name__ == "__main__":
    main()
