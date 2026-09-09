#!/usr/bin/env bash
set -u

env_file=/etc/napcat/napcat.env
state_file=/run/napcat-watchdog.last
offline_event_state_file=/run/napcat-watchdog.offline-event
manual_auth_state_file=/run/napcat-watchdog.manual-auth

# Do not create a QR-code restart loop before password fallback is configured.
if [[ ! -s "$env_file" ]] || ! grep -qE '^NAPCAT_QUICK_PASSWORD(_MD5)?=' "$env_file"; then
    exit 0
fi

if ! systemctl is-active --quiet qqbot.service; then
    exit 0
fi

now=$(date +%s)

onebot_connected() {
    ss -Htn state established '( sport = :8080 )' \
        | awk '$3 == "127.0.0.1:8080" && $4 ~ /^127\.0\.0\.1:[0-9]+$/ { found = 1 } END { exit !found }'
}

manual_auth_required() {
    journalctl -u napcat.service --since "15 minutes ago" --no-pager -o cat \
        | grep -qE '密码回退需要验证码|密码回退需要新设备验证|需要验证码|新设备需要扫码验证|异常设备需要验证|请扫描下面的二维码|登录态已失效|请重新登录'
}

if manual_auth_required; then
    if [[ ! -f "$manual_auth_state_file" ]]; then
        : >"$manual_auth_state_file"
        logger -t napcat-watchdog 'QQ login verification required; waiting for manual completion'
    fi
    rm -f "$state_file"
    exit 0
fi

# QQ can emit bot_offline while the OneBot TCP connection remains established.
# Inspect the recent service logs so a KickedOffLine is handled immediately.
if journalctl -u napcat.service -u qqbot.service --since "90 seconds ago" --no-pager -o cat \
    | grep -qE 'KickedOffLine|notice\.bot_offline|账号状态变更为离线'; then
    last_event=0
    if [[ -f "$offline_event_state_file" ]]; then
        last_event=$(<"$offline_event_state_file")
    fi
    if (( now - last_event >= 180 )); then
        printf '%s\n' "$now" >"$offline_event_state_file"
        logger -t napcat-watchdog 'QQ offline event detected; restarting NapCat'
        systemctl restart napcat.service
        exit 0
    fi
fi

if onebot_connected; then
    if [[ -f "$manual_auth_state_file" ]]; then
        rm -f "$manual_auth_state_file"
        logger -t napcat-watchdog 'QQ login verification completed; watchdog resumed'
    fi
    rm -f "$state_file"
    exit 0
fi

last=0
if [[ -f "$state_file" ]]; then
    last=$(<"$state_file")
fi

# Record the first failed check and wait for a second one before restarting.
if (( last == 0 )); then
    printf '%s\n' "$now" >"$state_file"
    exit 0
fi
if (( now - last < 120 )); then
    exit 0
fi

printf '%s\n' "$now" >"$state_file"
logger -t napcat-watchdog 'OneBot connection has been down for about two minutes; restarting NapCat'
systemctl restart napcat.service
