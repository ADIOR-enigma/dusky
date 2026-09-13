#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <stdlib.h>

/*
 * runner_shim: Universal Linux Game Runner I/O & Concurrency Shim
 * 
 * 1. Mono / .NET File Sharing Violation Fix:
 *    Many Linux native games built on Mono (FNA, XNA, MonoGame, Unity) call
 *    new FileStream(path, FileMode.Open), which defaults to FileAccess.ReadWrite
 *    and FileShare.Read. When multiple threads stream assets simultaneously,
 *    Mono's internal file_share_table keyed by (dev, ino) raises a false
 *    sharing violation (ERROR_SHARING_VIOLATION 32), crashing the game.
 *    By providing a unique pseudo-inode to Mono's stat checks, each file
 *    stream handle is isolated and concurrent reading never conflicts.
 *
 * 2. DwarFS / fuse-overlayfs Copy-Up Protection:
 *    When games run on compressed DwarFS with fuse-overlayfs, opening static
 *    read-only archives with write intent (e.g. .NET's default O_RDWR) tricks
 *    the overlay into copying up entire multi-gigabyte files to disk.
 *    This shim transparently demotes O_RDWR to O_RDONLY for static asset
 *    archives, saving gigabytes of disk writes, avoiding I/O stalls, and
 *    preventing file duplication.
 */

static int is_game_process(void) {
    extern char *program_invocation_short_name;
    if (!program_invocation_short_name) return 1;
    const char *name = program_invocation_short_name;
    if (strcmp(name, "bash") == 0 || strcmp(name, "sh") == 0 ||
        strcmp(name, "cp") == 0 || strcmp(name, "mv") == 0 ||
        strcmp(name, "rm") == 0 || strcmp(name, "mkdir") == 0 ||
        strcmp(name, "systemd-run") == 0 || strcmp(name, "python3") == 0 ||
        strcmp(name, "bwrap") == 0 || strcmp(name, "tar") == 0) {
        return 0;
    }
    return 1;
}

static int is_mono_runtime(void) {
    static int cached = -1;
    if (cached != -1) return cached;
    if (!is_game_process()) {
        cached = 0;
        return 0;
    }
    if (dlsym(RTLD_DEFAULT, "mono_init") != NULL ||
        dlsym(RTLD_DEFAULT, "mono_w32file_create") != NULL ||
        dlsym(RTLD_DEFAULT, "mono_runtime_init") != NULL ||
        getenv("MONO_PATH") != NULL) {
        cached = 1;
        return 1;
    }
    cached = 0;
    return 0;
}

static uint64_t fake_ino = 1000000000ULL;

/* Hook __fxstat: Mono specifically calls __fxstat(1, fd, buf) in mono_w32file_create */
typedef int (*fxstat_fn)(int ver, int fd, struct stat *buf);
static fxstat_fn real_fxstat = NULL;

int __fxstat(int ver, int fd, struct stat *buf) {
    if (!real_fxstat) {
        real_fxstat = (fxstat_fn)dlsym(RTLD_NEXT, "__fxstat");
    }
    int res = real_fxstat(ver, fd, buf);
    if (res == 0 && buf && is_mono_runtime()) {
        buf->st_ino = __atomic_fetch_add(&fake_ino, 1, __ATOMIC_RELAXED);
    }
    return res;
}

/* Also hook fstat */
typedef int (*fstat_fn)(int fd, struct stat *buf);
static fstat_fn real_fstat = NULL;

int fstat(int fd, struct stat *buf) {
    if (!real_fstat) {
        real_fstat = (fstat_fn)dlsym(RTLD_NEXT, "fstat");
    }
    int res = real_fstat(fd, buf);
    if (res == 0 && buf && is_mono_runtime()) {
        buf->st_ino = __atomic_fetch_add(&fake_ino, 1, __ATOMIC_RELAXED);
    }
    return res;
}

/* Check if a file is an immutable game asset that should never trigger copy-up */
static int is_static_asset(const char *path) {
    if (!path) return 0;
    if (strstr(path, "/data/") || strstr(path, "data/textures") || strstr(path, "data/videos")) {
        return 1;
    }
    const char *ext = strrchr(path, '.');
    if (ext) {
        if (strcasecmp(ext, ".wem") == 0 || strcasecmp(ext, ".bnk") == 0 ||
            strcasecmp(ext, ".ogv") == 0 || strcasecmp(ext, ".xnb") == 0 ||
            strcasecmp(ext, ".pak") == 0 || strcasecmp(ext, ".vpk") == 0 ||
            strcasecmp(ext, ".pck") == 0 || strcasecmp(ext, ".bundle") == 0 ||
            strcasecmp(ext, ".assets") == 0 || strcasecmp(ext, ".bank") == 0 ||
            strcasecmp(ext, ".mp4") == 0 || strcasecmp(ext, ".mkv") == 0 ||
            strcasecmp(ext, ".bk2") == 0) {
            return 1;
        }
    }
    return 0;
}

/* Hook open to prevent DwarFS / fuse-overlayfs copy-up of multi-gigabyte read-only assets */
typedef int (*open_fn)(const char *pathname, int flags, ...);
static open_fn real_open = NULL;

int open(const char *pathname, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = va_arg(args, mode_t);
        va_end(args);
    }
    if (!real_open) {
        real_open = (open_fn)dlsym(RTLD_NEXT, "open");
    }
    if (is_game_process() && (flags & O_ACCMODE) == O_RDWR && is_static_asset(pathname)) {
        flags = (flags & ~O_ACCMODE) | O_RDONLY;
    }
    return real_open(pathname, flags, mode);
}

typedef int (*open64_fn)(const char *pathname, int flags, ...);
static open64_fn real_open64 = NULL;

int open64(const char *pathname, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = va_arg(args, mode_t);
        va_end(args);
    }
    if (!real_open64) {
        real_open64 = (open64_fn)dlsym(RTLD_NEXT, "open64");
    }
    if (is_game_process() && (flags & O_ACCMODE) == O_RDWR && is_static_asset(pathname)) {
        flags = (flags & ~O_ACCMODE) | O_RDONLY;
    }
    return real_open64(pathname, flags, mode);
}
