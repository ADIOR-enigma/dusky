#!/bin/bash
# Refreshes font cache and verifies font aliasing for Arch/Hyprland environment.

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
TARGET_FONT="Atkinson Hyperlegible"
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
NC=$'\033[0m' # No Color

echo -e "${YELLOW}:: Refreshing System Font Cache...${NC}"

# 1. Regenerate the cache (Verbose and Forced as requested)
#    We let stdout flow so you can see the directories being scanned.
fc-cache -fv

echo -e "\n${YELLOW}:: Verifying Font Aliases...${NC}"

# 2. Check the alias
#    We capture the output to perform a logic check
MATCH_OUTPUT=$(fc-match "Arial")
FAMILY_NAME=$(echo "$MATCH_OUTPUT" | cut -d'"' -f 2)

printf '   Input Request:  %sArial%s\n' "${NC}" "${NC}"
printf '   System Return:  %s%s%s\n' "${NC}" "$MATCH_OUTPUT" "${NC}"

# 3. Validation Logic
if [[ "$MATCH_OUTPUT" == *"$TARGET_FONT"* ]]; then
  printf '\n%s[SUCCESS] System is correctly aliased to %s.%s\n' "${GREEN}" "$TARGET_FONT" "${NC}"
else
  printf '\n%s[FAIL] System is NOT using %s.%s\n' "${RED}" "$TARGET_FONT" "${NC}"
  printf '       Current default for Arial is: %s\n' "$FAMILY_NAME"
  printf '       Check ~/.config/fontconfig/fonts.conf or missing font files.\n'
fi
