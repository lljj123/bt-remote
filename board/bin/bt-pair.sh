#!/usr/bin/env bash
# Pairing is closed by default. Open an explicit, bounded window with bt-pair on 300.
set -euo pipefail
if [[ ${1:-status} == help || ${1:-} == --help ]]; then
    echo "Usage: sudo bt-pair {on [seconds]|off|status|list|forget MAC|alias NAME}"
    exit 0
fi
exec 9>/run/bt-connectable.lock
flock -w 45 9 || { echo "Bluetooth management busy; retry shortly" >&2; exit 1; }
prop() { timeout 10 busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 "$@"; }
bt() { timeout 10 bluetoothctl "$@"; }
case "${1:-status}" in
    on)
        seconds=${2:-300}
        [[ $seconds =~ ^[0-9]{1,4}$ ]] && (( 10#$seconds >= 1 && 10#$seconds <= 3600 )) || {
            echo "Pairing window must be 1..3600 seconds" >&2; exit 2;
        }
        seconds=$((10#$seconds))
        prop Powered b true
        prop PairableTimeout u "$seconds"
        prop DiscoverableTimeout u "$seconds"
        prop Pairable b true
        prop Discoverable b true
        echo "Pairing enabled for $seconds seconds. Close early with: sudo bt-pair off"
        ;;
    off)
        prop Discoverable b false
        prop Pairable b false
        timeout 8 btmgmt --index 0 connectable on
        echo "Pairing/discovery disabled; existing peers remain connectable."
        ;;
    status)
        bt show
        timeout 8 btmgmt --index 0 info
        ;;
    list) bt devices Paired ;;
    forget)
        mac=${2:-}
        [[ $mac =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]] || { echo "Expected Bluetooth MAC" >&2; exit 2; }
        mac=${mac^^}
        timeout 10 busctl call org.bluez /org/bluez/hci0 org.bluez.Adapter1 RemoveDevice o "/org/bluez/hci0/dev_${mac//:/_}"
        ;;
    alias)
        [[ -n ${2:-} ]] || { echo "Expected a device name" >&2; exit 2; }
        prop Alias s "$2"
        ;;
    *) echo "Unknown command; use bt-pair help" >&2; exit 2 ;;
esac
