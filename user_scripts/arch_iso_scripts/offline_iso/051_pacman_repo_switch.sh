#!/usr/bin/env bash
# ==============================================================================
# pacman_repo_switch.sh
#
# Manages pacman's repository configuration, toggling between:
#   Network: OFFLINE (Local media file://) OR ONLINE (HTTPS Mirrors)
#   Target:  Standard Arch Linux (x86_64)
#
# Works correctly in BOTH:
#   - Arch Linux ISO live environment and install chroot (already root)
#   - Post-installed Arch Linux system (self-elevates via sudo if needed)
#
# Usage:
#   ./pacman_repo_switch.sh                       # Interactive menu
#   ./pacman_repo_switch.sh --online --arch       # Online standard Arch
#   ./pacman_repo_switch.sh --offline --arch      # Offline standard Arch
#   ./pacman_repo_switch.sh --help                # Show usage information
#
# ==============================================================================

set -euo pipefail

# ==============================================================================
# SECTION 1 — USER CONFIGURATION
# ==============================================================================

# Dynamically detect if we are inside the chroot (Phase 2) or on the ISO (Phase 1)
# MUST begin with 'file:///' (three slashes) to ensure an absolute path.
if [[ -d "/offline_repo" ]]; then
    OFFLINE_REPO_PATH="file:///offline_repo"
else
    OFFLINE_REPO_PATH="file:///run/archiso/bootmnt/arch/repo"
fi

OFFLINE_REPO_NAME="archrepo"
PACMAN_CONF="/etc/pacman.conf"
MIRRORLIST_FILE="/etc/pacman.d/mirrorlist"
BACKUP_SUFFIX=".pacman-switch.bak"

# ==============================================================================
# SECTION 2 — SELF-ELEVATION
# ==============================================================================

if [[ "${EUID}" -ne 0 ]]; then
    if [[ -z "${BASH_SOURCE[0]:-}" || ! -f "${BASH_SOURCE[0]}" ]]; then
        echo "[ERROR] Cannot self-elevate. Script must be executed from a file, not stdin/pipe." >&2
        echo "[ERROR] Please run this script as root directly." >&2
        exit 1
    fi

    _SELF="$(readlink -f "${BASH_SOURCE[0]}")"

    if command -v sudo &>/dev/null; then
        echo "[INFO]  Root privileges are required."
        echo "[INFO]  Re-launching under sudo — you may be prompted for your password."
        exec sudo "${_SELF}" "$@"
        echo "[ERROR] 'exec sudo' failed unexpectedly." >&2
        exit 1
    else
        echo "[ERROR] Root privileges are required and 'sudo' was not found." >&2
        echo "[ERROR] Please run this script as root directly." >&2
        exit 1
    fi
fi

# ==============================================================================
# SECTION 3 — TERMINAL COLOR SETUP
# ==============================================================================

if [[ -t 1 ]]; then
    CLR_RED=$(tput setaf 1 2>/dev/null)    || CLR_RED=""
    CLR_GREEN=$(tput setaf 2 2>/dev/null)  || CLR_GREEN=""
    CLR_YELLOW=$(tput setaf 3 2>/dev/null) || CLR_YELLOW=""
    CLR_CYAN=$(tput setaf 6 2>/dev/null)   || CLR_CYAN=""
    CLR_BOLD=$(tput bold 2>/dev/null)      || CLR_BOLD=""
    CLR_RESET=$(tput sgr0 2>/dev/null)     || CLR_RESET=""
else
    CLR_RED=""
    CLR_GREEN=""
    CLR_YELLOW=""
    CLR_CYAN=""
    CLR_BOLD=""
    CLR_RESET=""
fi

NETWORK_MODE=""

# ==============================================================================
# SECTION 5 — LOGGING HELPERS
# ==============================================================================

log_info()  { printf "%s[INFO]%s  %s\n"  "${CLR_GREEN}"  "${CLR_RESET}" "$*";      }
log_warn()  { printf "%s[WARN]%s  %s\n"  "${CLR_YELLOW}" "${CLR_RESET}" "$*";      }
log_error() { printf "%s[ERROR]%s %s\n"  "${CLR_RED}"    "${CLR_RESET}" "$*" >&2;  }
log_step()  { printf "\n%s%s==>%s %s%s%s\n" \
                  "${CLR_BOLD}" "${CLR_CYAN}" "${CLR_RESET}" \
                  "${CLR_BOLD}" "$*"          "${CLR_RESET}";                       }

# ==============================================================================
# SECTION 6 — STARTUP VALIDATION
# ==============================================================================

validate_config() {
    local errors=0

    if [[ "${OFFLINE_REPO_PATH}" != file:///* ]]; then
        log_error "OFFLINE_REPO_PATH must begin with 'file:///' (three slashes)."
        log_error "  Current value : '${OFFLINE_REPO_PATH}'"
        errors=$(( errors + 1 ))
    fi

    if [[ -z "${OFFLINE_REPO_NAME}" || "${OFFLINE_REPO_NAME}" =~ [[:space:]/\\] ]]; then
        log_error "OFFLINE_REPO_NAME must be valid (no spaces or slashes)."
        errors=$(( errors + 1 ))
    fi

    local pacman_conf_dir mirrorlist_dir
    pacman_conf_dir="$(dirname "${PACMAN_CONF}")"
    mirrorlist_dir="$(dirname "${MIRRORLIST_FILE}")"

    if [[ ! -d "${pacman_conf_dir}" ]]; then
        log_error "Parent directory for PACMAN_CONF does not exist: '${pacman_conf_dir}'"
        errors=$(( errors + 1 ))
    fi

    if [[ ! -d "${mirrorlist_dir}" ]]; then
        log_error "Parent directory for MIRRORLIST_FILE does not exist: '${mirrorlist_dir}'"
        errors=$(( errors + 1 ))
    fi

    if (( errors > 0 )); then
        log_error "Configuration validation failed with ${errors} error(s). Aborting."
        exit 1
    fi
}

# ==============================================================================
# SECTION 7 — ATOMIC FILE WRITE & BACKUP
# ==============================================================================

write_file_atomically() {
    local dest="${1:?write_file_atomically: a destination path argument is required}"
    local dest_dir
    dest_dir="$(dirname "${dest}")"
    local tmpfile

    if [[ ! -d "${dest_dir}" ]]; then
        log_error "Destination directory does not exist: '${dest_dir}'"
        return 1
    fi

    tmpfile="$(mktemp -p "${dest_dir}" .pacman-switch.XXXXXXXXXX)"
    
    if [[ -z "${tmpfile}" || ! -f "${tmpfile}" ]]; then
        log_error "Failed to create temporary file in '${dest_dir}'."
        return 1
    fi

    chmod 0644 "${tmpfile}"
    chown root:root "${tmpfile}"

    if [[ -f "${dest}" ]]; then
        chmod --reference="${dest}" "${tmpfile}" 2>/dev/null || true
        chown --reference="${dest}" "${tmpfile}" 2>/dev/null || true
    fi

    if ! cat > "${tmpfile}"; then
        rm -f "${tmpfile}"
        log_error "Failed to write content to temporary file: '${tmpfile}'"
        return 1
    fi

    if ! mv "${tmpfile}" "${dest}"; then
        rm -f "${tmpfile}"
        log_error "Failed to rename temp file to destination: '${dest}'"
        return 1
    fi

    return 0
}

backup_file() {
    local src="${1:?backup_file: a source file path argument is required}"
    local backup="${src}${BACKUP_SUFFIX}"

    if [[ ! -f "${src}" ]]; then
        log_warn "Source file '${src}' not found — skipping backup."
        return 0
    fi

    if [[ -f "${backup}" ]]; then
        log_warn "Overwriting existing backup: '${backup}'"
    fi

    cp --preserve=all "${src}" "${backup}"
    log_info "Backup saved: '${backup}'"
}

# ==============================================================================
# SECTION 8 — SWITCH TO ONLINE
# ==============================================================================

switch_to_online() {
    log_step "Switching to ONLINE Repositories (ARCH LINUX)"

    local write_timestamp
    write_timestamp="$(date --utc '+%Y-%m-%d %H:%M:%S UTC')"

    log_info "Backing up existing configuration files..."
    backup_file "${PACMAN_CONF}"
    backup_file "${MIRRORLIST_FILE}"

    log_info "Writing online pacman.conf -> '${PACMAN_CONF}'..."

    {
        cat << ONLINE_PACMAN_CONF_EOF
# ==============================================================================
# /etc/pacman.conf — ONLINE MODE
# ==============================================================================
# Managed by: pacman_repo_switch.sh
# State:      ONLINE (ARCH LINUX)
# Written:    ${write_timestamp}
# ==============================================================================

[options]
Color
ILoveCandy
VerbosePkgLists
HoldPkg     = pacman glibc
CheckSpace
ParallelDownloads = 10
DisableDownloadTimeout
DownloadUser = alpm

SigLevel    = Required DatabaseOptional
LocalFileSigLevel = Optional

ONLINE_PACMAN_CONF_EOF

        echo "Architecture = auto"
        echo ""

        cat << 'ARCH_BLOCK_EOF'
[core]
Include = /etc/pacman.d/mirrorlist

[extra]
Include = /etc/pacman.d/mirrorlist

[multilib]
Include = /etc/pacman.d/mirrorlist
ARCH_BLOCK_EOF
    } | write_file_atomically "${PACMAN_CONF}"

    log_info "Online pacman.conf written successfully."

    # Write Standard Arch Mirrorlist - HARDCODED to x86_64 to prevent $arch expansion bugs
    write_file_atomically "${MIRRORLIST_FILE}" << 'ONLINE_MIRRORLIST_EOF'
################################################################################
# /etc/pacman.d/mirrorlist — ONLINE MODE
################################################################################
Server = https://frankfurt.mirror.pkgbuild.com/$repo/os/x86_64
Server = https://johannesburg.mirror.pkgbuild.com/$repo/os/x86_64
Server = https://london.mirror.pkgbuild.com/$repo/os/x86_64
Server = https://losangeles.mirror.pkgbuild.com/$repo/os/x86_64
Server = https://mirror.moson.org/arch/$repo/os/x86_64
Server = https://mirror.sunred.org/archlinux/$repo/os/x86_64
Server = https://arch.mirror.constant.com/$repo/os/x86_64
Server = https://arch.phinau.de/$repo/os/x86_64
Server = https://mirror.theo546.fr/archlinux/$repo/os/x86_64
Server = https://berlin.mirror.pkgbuild.com/$repo/os/x86_64
ONLINE_MIRRORLIST_EOF

    printf "\n%s%s[OK]%s  Online repository configuration applied.%s\n" \
        "${CLR_BOLD}" "${CLR_GREEN}" "${CLR_RESET}" "${CLR_RESET}"
}

# ==============================================================================
# SECTION 9 — SWITCH TO OFFLINE
# ==============================================================================

switch_to_offline() {
    log_step "Switching to OFFLINE Repositories (ARCH LINUX)"

    local write_timestamp
    write_timestamp="$(date --utc '+%Y-%m-%d %H:%M:%S UTC')"

    log_info "Backing up existing configuration files..."
    backup_file "${PACMAN_CONF}"
    backup_file "${MIRRORLIST_FILE}"

    write_file_atomically "${MIRRORLIST_FILE}" << OFFLINE_MIRRORLIST_EOF
################################################################################
# /etc/pacman.d/mirrorlist — OFFLINE MODE
################################################################################
# Managed by: pacman_repo_switch.sh
# State:      OFFLINE — local installation media repository
# Written:    ${write_timestamp}
#
# Current offline repository URL:
#   ${OFFLINE_REPO_PATH}
#

Server = ${OFFLINE_REPO_PATH}
OFFLINE_MIRRORLIST_EOF

    log_info "Offline mirrorlist written successfully."

    {
        cat << OFFLINE_PACMAN_CONF_EOF
# ==============================================================================
# /etc/pacman.conf — OFFLINE MODE
# ==============================================================================
# Managed by: pacman_repo_switch.sh
# State:      OFFLINE (ARCH LINUX)
# Written:    ${write_timestamp}

[options]
Color
ILoveCandy
VerbosePkgLists
HoldPkg     = pacman glibc
CheckSpace
ParallelDownloads = 5

# Pacman 7.1.0+ limits VFS read access via Landlock/seccomp sandboxes.
# We explicitly disable sandboxing to guarantee file:/// block device reads.
DisableSandbox

# DownloadUser Disabled: Prevents 'alpm' user permission drops which 
# block read access to root-owned offline file:/// media mounts.
# DownloadUser = alpm

SigLevel    = Required DatabaseOptional
LocalFileSigLevel = Optional

OFFLINE_PACMAN_CONF_EOF

        echo "Architecture = auto"

        cat << OFFLINE_REPO_BLOCK_EOF

[${OFFLINE_REPO_NAME}]
SigLevel = Never
Include = ${MIRRORLIST_FILE}
OFFLINE_REPO_BLOCK_EOF
    } | write_file_atomically "${PACMAN_CONF}"

    log_info "Offline pacman.conf written successfully."
    log_step "Verifying Offline Repository"

    local repo_fs_path="${OFFLINE_REPO_PATH#file://}"
    local db_file="${repo_fs_path}/${OFFLINE_REPO_NAME}.db"

    if [[ ! -d "${repo_fs_path}" ]]; then
        log_warn "Offline repository directory not found: '${repo_fs_path}'"
        log_warn "This is expected if the installation media is not currently mounted."
        printf "\n%s%s[OK]%s  Offline repository configuration applied.%s\n" \
            "${CLR_BOLD}" "${CLR_GREEN}" "${CLR_RESET}" "${CLR_RESET}"
        return 0
    fi

    log_info "Offline repository directory found: '${repo_fs_path}'"

    if [[ ! -f "${db_file}" ]]; then
        log_warn "Database file not found: '${db_file}'"
        printf "\n%s%s[OK]%s  Offline repository configuration applied.%s\n" \
            "${CLR_BOLD}" "${CLR_GREEN}" "${CLR_RESET}" "${CLR_RESET}"
        return 0
    fi

    log_info "Database file confirmed: '${db_file}'"
    log_info "Syncing offline package database..."

    local pacman_exit=0
    # Note: Sy is kept here ONLY because it's accessing the local file:// repo 
    # and takes milliseconds, which pacstrap needs to function correctly.
    pacman -Sy || pacman_exit=$?

    if (( pacman_exit == 0 )); then
        log_info "Offline package database synced successfully."
    else
        log_warn "'pacman -Sy' exited with code ${pacman_exit}."
    fi

    printf "\n%s%s[OK]%s  Offline repository configuration applied.%s\n" \
        "${CLR_BOLD}" "${CLR_GREEN}" "${CLR_RESET}" "${CLR_RESET}"
}

# ==============================================================================
# SECTION 10 — INTERACTIVE MENU
# ==============================================================================

show_menu() {
    printf "\n"
    printf "%s%s╔══════════════════════════════════════════╗%s\n" \
        "${CLR_BOLD}" "${CLR_CYAN}" "${CLR_RESET}"
    printf "%s%s║     Pacman Repository State Manager     ║%s\n" \
        "${CLR_BOLD}" "${CLR_CYAN}" "${CLR_RESET}"
    printf "%s%s╚══════════════════════════════════════════╝%s\n" \
        "${CLR_BOLD}" "${CLR_CYAN}" "${CLR_RESET}"
    printf "\n"
    printf "  Current script settings:\n"
    printf "    pacman.conf   : %s\n" "${PACMAN_CONF}"
    printf "    mirrorlist    : %s\n" "${MIRRORLIST_FILE}"
    printf "    Offline URL   : %s\n" "${OFFLINE_REPO_PATH}"
    printf "    Offline repo  : [%s]\n" "${OFFLINE_REPO_NAME}"
    printf "\n"
    printf "  %s[1]%s  Online   — Standard Arch Linux\n" "${CLR_BOLD}" "${CLR_RESET}"
    printf "  %s[2]%s  Offline  — Standard Arch Linux\n" "${CLR_BOLD}" "${CLR_RESET}"
    printf "  %s[q]%s  Quit     — no changes will be made\n\n" "${CLR_BOLD}" "${CLR_RESET}"

    local user_choice
    while true; do
        printf "  Your choice [1-2/q]: "

        if ! read -r -n1 -t 60 user_choice; then
            printf "\n"
            log_warn "No input received within 60 seconds. Quitting with no changes."
            exit 0
        fi
        printf "\n"

        case "${user_choice}" in
            1) TARGET_OS="arch";    switch_to_online;  return 0 ;;
            2) TARGET_OS="arch";    switch_to_offline; return 0 ;;
            q|Q) log_info "Quit selected. No changes were made."; exit 0 ;;
            *) log_warn "Invalid choice: '${user_choice}'. Please enter 1, 2, or q." ;;
        esac
    done
}

# ==============================================================================
# SECTION 11 — USAGE / HELP
# ==============================================================================

show_usage() {
    printf "\nUsage: %s [OPTIONS]\n\n" "${BASH_SOURCE[0]}"
    printf "  --online    Write online HTTPS configuration and sync.\n"
    printf "  --offline   Write offline local file:// configuration.\n"
    printf "  --help      Display this help text.\n\n"
    printf "  Requires root. If not root, the script will attempt to re-launch\n"
    printf "  itself automatically using 'sudo'.\n\n"
}

# ==============================================================================
# SECTION 12 — ENTRY POINT
# ==============================================================================

main() {
    validate_config

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --online)  NETWORK_MODE="online"; shift ;;
            --offline) NETWORK_MODE="offline"; shift ;;
            --arch)    shift ;; # Ignored for backward compatibility
            --help|-h) show_usage; exit 0 ;;
            *)         log_error "Unknown argument: '$1'"; show_usage; exit 1 ;;
        esac
    done

    if [[ "${NETWORK_MODE}" == "online" ]]; then
        switch_to_online
    elif [[ "${NETWORK_MODE}" == "offline" ]]; then
        switch_to_offline
    else
        show_menu
    fi
}

main "$@"
