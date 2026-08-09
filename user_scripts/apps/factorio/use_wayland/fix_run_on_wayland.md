# Factorio 2.1.x Native-Wayland Crash — Diagnosis & Fix Guide

**Status: SOLVED and verified.** This document is the complete record of what was
wrong, how it was found, and how to re-apply the fix on a fresh system. It was
written on 2026-08-09 after the fix was proven end-to-end (game boots on native
Wayland with no XWayland, reaches the main menu, loads a save, renders frames,
zero GL errors — both with and without the jc141 bubblewrap sandbox).

Everything you need is in this folder:

| File | What it is |
|---|---|
| `install_fix.py` | **Self-contained installer** (embeds the shim source + a verification smoke test). Run it, done. |
| `INSTALL_FIX.md` | This guide. |
| `generate.py` | Dev-only: regenerates `install_fix.py` after editing the shim source in `../eglfix/`. |
| `stress_test.sh` | Dev-only: the stress-test suite used to verify the installer (break/repair, fresh-sim, idempotency). |

---

## 1. TL;DR

Factorio 2.1.14 crashes ~1 second after launch on a **native Wayland** session
(no XWayland) with:

```
Error ShaderOpenGL.cpp:15: Failed to create shader '__core__/graphics/shaders/sprite.vert'.
Error CrashHandler.cpp:616: Received 6
```

The cause is NOT the GPU driver, Mesa, or the shader files. It is that Factorio's
**EGL context stops being current on the main thread** between OpenGL init and
the first shader compile, so every GL call silently fails and `glCreateShader`
returns 0. This only happens on the native Wayland/EGL path — under X11/XWayland
the same game works fine.

The fix is a small `libEGL.so.1` **interposer** loaded via `LD_PRELOAD` that
forwards all EGL calls to the real library but re-binds the remembered EGL
context on the calling thread right before the first `glCreateShader`. After
that, the game renders normally on native Wayland.

**To fix a fresh install:**
```bash
sudo pacman -S --needed gcc libglvnd sdl3 fuse-overlayfs bubblewrap
python3 install_fix.py --game-dir /path/to/Factorio_2.1.14
cd /path/to/Factorio_2.1.14 && ./start.n.sh
```
The script installs the packages itself (auto-sudo) if you omit the pacman line.

---

## 2. The problem — symptoms

Verified environment where it crashed:
- Arch Linux, Hyprland, **Wayland-only session** (`DISPLAY=` empty, XWayland disabled).
- Intel Iris Xe iGPU + NVIDIA RTX 3050 Ti dGPU, Mesa 26.1.6, SDL3 3.4.14.
- Factorio 2.1.14 (build 87180, linux64, steam) from a jc141 repack, launched
  via `./start.n.sh` (DwarFS FUSE mount + optional bubblewrap sandbox).

The exact log (identical with or without the sandbox):

```
   0.521 Video driver: wayland
   0.609 Initialised OpenGL:[3] Mesa Intel(R) Iris(R) Xe Graphics (ADL GT2); driver: 4.6 (Core Profile) Mesa 26.1.6-arch3.1
   0.911 Graphics settings preset: integrated-gpuhigh
Factorio crashed...
Error ShaderOpenGL.cpp:15: Failed to create shader '__core__/graphics/shaders/sprite.vert'.
Error CrashHandler.cpp:616: Received 6
```

Key facts established early:
- The shader file exists and is readable (`sprite.vert`, 401 bytes).
- Standalone EGL test programs compile the *exact same shaders* fine on iris,
  llvmpipe and NVIDIA — so it is not a driver/compiler problem.
- The crash is identical on Intel, llvmpipe (software) and NVIDIA paths.

## 3. Root cause (empirically proven)

A gdb probe at `ShaderOpenGL.cpp:15` (the abort site) showed:

```
eglGetCurrentContext() == 0        <- NO current context on this thread
glCreateShader(GL_VERTEX_SHADER) == 0   <- every GL call fails silently
glGetError() == 0                  <- nothing to report, the call was a no-op
```

At the same time, tracing `eglCreateContext`/`eglMakeCurrent` showed the context
**was** bound successfully during init (~0.4 s), and the shader compile happens
~1 s later on the **same** thread — with no unbind or destroy in between. So the
binding was lost through a mechanism the game never directly triggers
(`eglReleaseThread`, surface recreation, or an internal SDL3/EGL Wayland path).

In short: **the game assumes the EGL context it created is still current when
it starts compiling shaders; on native Wayland it is not.** X11 doesn't hit
this bug, which is why every report says "forcing X11 works".

Why a naive LD_PRELOAD doesn't fix it:
- The game **dlsyms its EGL entry points itself** (proven: a preload interposing
  `gl*` symbols and `eglGetProcAddress` never got called — empty trace log;
  `strings` on the binary confirms it dlsyms `eglCreateContext`,
  `eglMakeCurrent`, `eglGetPlatformDisplayEXT`, etc.).
- Therefore the interception point must be **the library handle itself**: a
  library that declares SONAME `libEGL.so.1` so the game's own
  `dlopen("libEGL.so.1")` returns it. That is exactly what the shim does.

## 4. How the fix works

`libEGL.so.1` (in the game's `eglfix/` directory, built from the embedded
`eglfix.c` in `install_fix.py`):

1. Declares `SONAME libEGL.so.1` → any `dlopen("libEGL.so.1")` in the process
   (game or SDL) resolves to it.
2. Forwards **every** EGL call to the real glvnd `libEGL.so.1` (resolved by
   absolute path to avoid recursion).
3. Remembers the last successful `eglMakeCurrent` binding (display/surface/context),
   guarded by a mutex; tracks whether the surface is still alive.
4. Wraps `glCreateShader`/`glCreateProgram` (handed out through the wrapped
   `eglGetProcAddress`, exactly like the game resolves GL): right before the
   first call, if the calling thread has no current context, it re-binds the
   remembered context. This is **one-shot per binding** (re-armed only by a new
   `eglMakeCurrent`/`eglCreateContext`) so it can't hijack other contexts.
5. Rebind failures are logged and non-fatal, with a surfaceless retry.
6. Exports only `egl*`/`gl*` symbols (linker version script `export.map`), so
   the interposer can never accidentally shadow libc/libdl symbols.

## 5. Required packages (fresh Arch install)

Installed automatically by `install_fix.py` (auto-elevates to sudo only for
pacman; everything else runs as your user). All are also listed here for manual
install:

| Package | Why it's needed |
|---|---|
| `gcc` | Compiles the shim (`eglfix.c`). |
| `libglvnd` | Provides the **real** `/usr/lib/libEGL.so.1` the shim forwards to, **and** the EGL headers (`/usr/include/EGL/egl.h`) used to build it. |
| `sdl3` | Factorio 2.1.x links host SDL3 (the Wayland/EGL path runs through it). |
| `fuse-overlayfs` | Required by the jc141 launcher to mount `files/game-root` from the DwarFS archive. |
| `bubblewrap` | Required by the jc141 sandbox (`ISOLATE=1` in `~/.jc141rc`). |
| `wtype`, `grim`, `imagemagick` (optional, `--with-testing-tools`) | Only for automated testing (keyboard injection, screenshots, image stats). Not needed to play. |

Already part of any desktop Arch install (not installed by the script): `mesa`
(GPU drivers), a Wayland compositor (`hyprland`), `python`, `binutils`
(`readelf`, used for SONAME verification). The DwarFS FUSE driver is **shipped
inside the repack** (`files/dwarfs-binary`), so no package needed for that.

## 6. What to save (so a fresh install can be fixed)

Keep **these two files** somewhere safe (USB/cloud/another machine):

1. **`install_fix.py`** — the whole kit in one self-contained file: the shim
   source, the version script, and the verification smoke test. Nothing else is
   needed to rebuild and reinstall the fix.
2. **`INSTALL_FIX.md`** — this guide.

Optional to keep (dev/reference): `generate.py`, `stress_test.sh`, and the
already-built `Factorio_2.1.14/eglfix/` directory. The script regenerates the
game-side files anyway, so only the two files above are strictly required.

What the script (re)creates on the game machine:
- `<game>/eglfix/libEGL.so.1` — the fix.
- `<game>/eglfix/eglfix.c`, `<game>/eglfix/export.map` — sources for reference.
- `<game>/local.config` — one line added/updated:
  `ENV="env LD_PRELOAD=<game>/eglfix/libEGL.so.1"` (backup: `local.config.bak`).

The `env` prefix in the ENV line is **required**: the jc141 launcher inserts
`$ENV` word-split into the command array (`RUN+=( $ENV ... )`), and inside the
bubblewrap sandbox only a real binary such as `env` can apply the variable — a
bare `LD_PRELOAD=...` prefix is mis-exec'd by bwrap and the launcher dies
silently. (Verified: sandboxed launch failed until the `env` prefix was added.)

## 7. Fresh-install procedure

### Automated (recommended)

```bash
# 1. Put the game repack somewhere, e.g.:
#    /mnt/zram1/Factorio_2.1.14   (this machine's location, zram tmpfs)

# 2. Run the installer (it finds the game dir automatically, or pass --game-dir):
python3 install_fix.py --game-dir /mnt/zram1/Factorio_2.1.14
#    - installs missing packages (sudo prompt, or SUDO_STDIN=1 for piped password)
#    - builds the shim, wires local.config, runs a smoke test that MUST pass
#    - exit 0 = fixed; exit 2 in --check mode = problems found

# 3. Launch normally:
cd /mnt/zram1/Factorio_2.1.14 && ./start.n.sh
```

Useful flags: `--check` (inspect only), `--force` (rebuild), `--skip-packages`,
`--with-testing-tools`, `--game-dir`, and `--verify-game` — after installing,
launch the real game through `start.n.sh` and automatically confirm it reaches
`Factorio initialised` on native Wayland (needs a live Wayland/X11 session;
skips gracefully if there's no display or a game is already running).

### Manual (if you prefer, or for non-Arch)

```bash
sudo pacman -S --needed gcc libglvnd sdl3 fuse-overlayfs bubblewrap
cd /mnt/zram1/Factorio_2.1.14
mkdir -p eglfix
# copy eglfix.c + export.map (embedded in install_fix.py) into eglfix/
gcc -shared -fPIC -O2 -Wall -o eglfix/libEGL.so.1 eglfix/eglfix.c -ldl -pthread \
    -Wl,-soname,libEGL.so.1 -Wl,--version-script=eglfix/export.map
# add to local.config:
echo 'ENV="env LD_PRELOAD=/mnt/zram1/Factorio_2.1.14/eglfix/libEGL.so.1"' >> local.config
```

## 8. Verification checklist (all performed 2026-08-09)

The game itself, both sandboxed and unsandboxed, with the shim:
- [x] Log shows `Video driver: wayland` + `Initialised OpenGL: Mesa Intel Iris Xe` and **no** `Failed to create shader` crash.
- [x] Reaches the main menu; **keyboard input works** (wtype keypresses navigate the menu).
- [x] Loads a save: `Loading level.dat` + `Map version 2.1.14-1` (in the sandbox too).
- [x] Renders: `Custom mipmaps uploaded (2370)`; screenshots show real in-game
      content (100k+ colors, not a black screen).
- [x] **Zero** per-frame `INVALID_OPERATION` GL errors (were present pre-fix).
- [x] Shim event log shows exactly one `ensure_current: rebind ... -> 1`.

The installer's built-in smoke test (runs on every install): creates a
surfaceless EGL context, unbinds it, then calls `glCreateShader` — the shim
must re-bind and return a valid shader. Baseline (no shim) reproduces the
Factorio condition (`glCreateShader -> 0`) as a negative control. Verified
PASS on this machine.

`--verify-game` goes one level deeper and is the strongest automated proof:
it launches the actual game via `start.n.sh`, watches its log for either
`Factorio initialised` (PASS) or `Failed to create shader` (FAIL), then kills
the whole process tree it started. Output on this machine:

```
[*] launching game via .../start.n.sh (up to 75s)...
[*] GAME LAUNCH CHECK PASSED: reached 'Factorio initialised' on native Wayland
```

## 9. Troubleshooting

- **Smoke test fails:** check `/tmp/eglfix.log` (shim diagnostics) and the
  script output. Usually: missing EGL headers (`sudo pacman -S libglvnd`) or
  the real `libEGL.so.1` missing.
- **Game still crashes with the old error:** the `ENV=` line was probably lost
  (re-run the installer) or the game dir moved (the hardcoded path in
  `local.config` silently no-ops — ld.so prints "cannot preload ... ignored";
  re-run `install_fix.py --force` to refresh paths).
- **Launcher dies silently with no banner:** the `env` prefix is missing from
  the `ENV=` line (see §6).
- **You only see the crash on Wayland, X11 is fine:** that is expected — the
  bug is native-Wayland-specific; the shim makes the Wayland path work.
- **Space Age (`space.age.sh`):** uses the same `local.config`, so the fix
  applies automatically; no extra step.

## 10. Limitations & notes

- The shim is a **targeted fix for this game's single-context Wayland bug**,
  not a general-purpose EGL interposer (it assumes one primary context; the
  auto-rebind is one-shot per binding for that reason).
- The game binary was never modified — only the launcher config and a new
  `eglfix/` directory were added. To remove the fix: delete the `ENV=` line
  from `local.config` and the `eglfix/` directory.
- The shim writes a tiny diagnostic log to `/tmp/eglfix.log` (bind/ensure
  events only; no hot-path logging).
- This fix is for **Factorio 2.1.x on native Wayland**. If a future Factorio
  build fixes the underlying bug, the `ENV=` line can simply be removed.
