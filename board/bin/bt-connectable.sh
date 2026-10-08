#!/usr/bin/env bash
# UWE5622: consult mgmt state; hciconfig PSCAN alone can be misleading.
# Share this lock with bt-pair. Do not reset/power-cycle an active adapter.
set -u
exec 9>/run/bt-connectable.lock
flock -n 9 || exit 0
state() { timeout 8 btmgmt --index 0 info 2>/dev/null | grep -m1 'current settings'; }
has_conn() { grep -qE '(^|[[:space:]])connectable([[:space:]]|$)' <<< "$1"; }
S=$(state)
if has_conn "$S"; then exit 0; fi
timeout 8 btmgmt --index 0 connectable on >/dev/null 2>&1 || true
if has_conn "$(state)"; then
    logger -t bt-connectable "connectable restored"
    exit 0
fi
timeout 5 hciconfig hci0 pscan >/dev/null 2>&1 || true
logger -t bt-connectable "connectable not confirmed; retry on next timer tick"
exit 1
