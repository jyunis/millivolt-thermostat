#!/bin/sh
# Managed by ansible (inkframe wifi-resilience role).
# Self-heal wlan0: if the gateway is unreachable, bounce the wifi
# connection; if that fails, restart NetworkManager. Backstop for any
# NetworkManager give-up state the conf.d retry settings do not cover.
GW=$(ip route | awk "/^default/ {print \$3; exit}")
[ -n "$GW" ] || GW=192.168.1.1
for i in 1 2 3; do
    ping -c 1 -W 3 "$GW" > /dev/null 2>&1 && exit 0
    sleep 2
done
logger -t wifi-watchdog "gateway $GW unreachable; reconnecting wlan0"
nmcli device disconnect wlan0 > /dev/null 2>&1
sleep 2
if ! nmcli device connect wlan0 > /dev/null 2>&1; then
    logger -t wifi-watchdog "nmcli connect failed; restarting NetworkManager"
    systemctl restart NetworkManager
fi
