#!/usr/bin/env bash
# UWE5622: consult mgmt state; hciconfig PSCAN alone can be misleading.
# Share this lock with bt-pair. Do not reset/power-cycle an active adapter.
set -u
exec 9>/run/bt-connectable.lock
flock -n 9 || exit 0
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RESULT=$(timeout 12 python3 "$ROOT/bt-mgmt-mode.py" ensure-connectable 2>/dev/null)
if [[ $? -eq 0 ]]; then
    [[ "$RESULT" == changed ]] && logger -t bt-connectable "connectable/fast-connectable restored"
    exit 0
fi
timeout 5 hciconfig hci0 pscan >/dev/null 2>&1 || true
RESULT=$(timeout 12 python3 "$ROOT/bt-mgmt-mode.py" ensure-connectable 2>/dev/null)
if [[ $? -eq 0 ]]; then
    logger -t bt-connectable "connectable/fast-connectable restored after hciconfig fallback"
    exit 0
fi
logger -t bt-connectable "connectable not confirmed; retry on next timer tick"
exit 1
