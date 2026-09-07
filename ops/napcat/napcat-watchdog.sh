#!/usr/bin/env bash
set -u

env_file=/etc/napcat/napcat.env
state_file=/run/napcat-watchdog.last

# Do not create a QR-code restart loop before password fallback is configured.
if [[ ! -s "$env_file" ]] || ! grep -qE '^NAPCAT_QUICK_PASSWORD(_MD5)?=' "$env_file"; then
    exit 0
fi

if ! systemctl is-active --quiet qqbot.service; then
    exit 0
fi

if systemctl is-active --quiet napcat.service \
    && ss -Htn state established '( sport = :8080 )' | grep -q '127.0.0.1:8080'; then
    rm -f "$state_file"
    exit 0
fi

now=$(date +%s)
last=0
if [[ -f "$state_file" ]]; then
    last=$(<"$state_file")
fi

# Require two consecutive failed checks before restarting NapCat.
if (( now - last < 120 )); then
    exit 0
fi

printf '%s\n' "$now" >"$state_file"
logger -t napcat-watchdog 'OneBot connection has been down for about two minutes; restarting NapCat'
systemctl restart napcat.service
