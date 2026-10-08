#!/bin/sh
# bt-gateway WiFi 切换看门狗
# 主程序在 netplan apply 之前 armed（写 flag 文件 + systemd-run 一个 --on-active 定时器）；
# 若主程序正常结束会 disarm（删 flag + stop 定时器）。若主程序崩溃/被杀，本脚本到点执行，
# 用 flag 里记录的备份无条件还原 netplan 配置，保证板子不会因为一次失败的 WiFi 切换失联。
set -eu
F=/run/bt-gateway/wifi-switch-flag
[ -f "$F" ] || exit 0
BK=$(head -n 1 "$F")
TARGET=$(sed -n '2p' "$F")
# Accept the one-line flag produced by older gateway releases during upgrades.
TARGET=${TARGET:-/etc/netplan/30-wifis-dhcp.yaml}
case "$TARGET" in /*) ;; *) logger -t bt-gateway "wifi-guard: invalid target"; exit 1;; esac
[ -f "$BK" ] || { logger -t bt-gateway "wifi-guard: backup $BK missing"; exit 1; }
cp -a "$BK" "$TARGET"
chmod 600 "$TARGET"
/usr/sbin/netplan apply
rm -f "$F"
logger -t bt-gateway "wifi-guard: rolled back netplan from $BK"
