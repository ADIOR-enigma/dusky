#!/usr/bin/env bash
# Stress tests for install_fix.py on the real game dir.
GAME=/mnt/zram1/Factorio_2.1.14
cd /mnt/zram1/online_ai/factorio_wayland_fix || exit 1
PASS=0; FAIL=0
ok()   { echo "[PASS] $1"; PASS=$((PASS+1)); }
bad()  { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }

echo "===== TEST 3: break (delete shim + unset ENV) then repair ====="
rm -f "$GAME/eglfix/libEGL.so.1"
sed -i 's|^ENV=.*|#ENV=REMOVED_BY_TEST|' "$GAME/local.config"
python3 install_fix.py --skip-packages > /tmp/t3.log 2>&1
grep -q 'SMOKE TEST PASSED' /tmp/t3.log && ok "repair: shim rebuilt + smoke passed" || { bad "repair failed"; tail -5 /tmp/t3.log; }
grep -q 'ENV="env LD_PRELOAD=.*eglfix/libEGL.so.1"' "$GAME/local.config" && ok "repair: ENV line restored" || bad "ENV line missing"
[ -f "$GAME/eglfix/libEGL.so.1" ] && ok "repair: libEGL.so.1 present" || bad "shim missing after repair"

echo "===== TEST 4: fresh-install simulation (whole eglfix dir gone) ====="
mv "$GAME/eglfix" /tmp/eglfix_backup_$$
sed -i 's|^ENV=.*|#ENV=REMOVED_BY_TEST|' "$GAME/local.config"
python3 install_fix.py --skip-packages > /tmp/t4.log 2>&1
[ -f "$GAME/eglfix/libEGL.so.1" ] && grep -q 'SMOKE TEST PASSED' /tmp/t4.log \
  && ok "fresh: full rebuild from embedded source + smoke passed" || { bad "fresh install failed"; tail -8 /tmp/t4.log; }
[ -f "$GAME/eglfix/eglfix.c" ] && [ -f "$GAME/eglfix/export.map" ] && ok "fresh: sources written alongside shim" || bad "sources missing"
grep -q '^ENV="env LD_PRELOAD=' "$GAME/local.config" && ok "fresh: ENV wired" || bad "ENV not wired"
# verify the script-rebuilt shim is byte-identical to our reference build
if cmp -s "$GAME/eglfix/eglfix.c" /tmp/eglfix_backup_$$/eglfix.c; then
  ok "fresh: written eglfix.c identical to reference"
else
  bad "fresh: eglfix.c differs from reference"
fi
rm -rf /tmp/eglfix_backup_$$

echo "===== TEST 5: --force rebuild ====="
BEFORE=$(md5sum "$GAME/eglfix/libEGL.so.1" | cut -d' ' -f1)
sleep 1
python3 install_fix.py --skip-packages --force > /tmp/t5.log 2>&1
AFTER=$(md5sum "$GAME/eglfix/libEGL.so.1" | cut -d' ' -f1)
[ "$BEFORE" = "$AFTER" ] && ok "force rebuild is deterministic (same hash)" || bad "force rebuild changed hash"
grep -q 'SMOKE TEST PASSED' /tmp/t5.log && ok "force: smoke passed" || bad "force: smoke failed"

echo "===== TEST 6: idempotency (run twice, expect no rebuild / no config churn) ====="
M1=$(md5sum "$GAME/eglfix/libEGL.so.1" | cut -d' ' -f1)
C1=$(md5sum "$GAME/local.config" | cut -d' ' -f1)
python3 install_fix.py --skip-packages > /tmp/t6a.log 2>&1
python3 install_fix.py --skip-packages > /tmp/t6b.log 2>&1
M2=$(md5sum "$GAME/eglfix/libEGL.so.1" | cut -d' ' -f1)
C2=$(md5sum "$GAME/local.config" | cut -d' ' -f1)
[ "$M1" = "$M2" ] && ok "idempotent: shim unchanged across runs" || bad "shim changed"
[ "$C1" = "$C2" ] && ok "idempotent: local.config unchanged across runs" || bad "config changed"
grep -q 'already wired correctly' /tmp/t6b.log && ok "idempotent: no ENV churn reported" || bad "config churn"
grep -q 'already built and up to date' /tmp/t6b.log && ok "idempotent: no rebuild reported" || bad "rebuild reported on 2nd run"

echo
echo "===== RESULT: $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
