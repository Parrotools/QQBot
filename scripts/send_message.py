"""通过 OneBot HTTP API（NapCat httpServers）发送消息，无需 Bot 进程在场。

用法：
    python scripts/send_message.py user:123456 "你好"
    python scripts/send_message.py group:789012 "通知"

环境变量：
    ONEBOT_HTTP_URL     NapCat HTTP 服务端地址，如 http://127.0.0.1:3000
    ONEBOT_ACCESS_TOKEN 与 NapCat 一致的 token
"""

import os
import sys

import httpx


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    target, message = sys.argv[1], sys.argv[2]
    type_, sep, target_id = target.partition(":")
    type_ = type_.strip().lower()
    if not sep or type_ not in ("user", "group") or not target_id.isdigit():
        print("目标格式应为 user:QQ号 或 group:群号")
        return 2

    base_url = os.environ.get("ONEBOT_HTTP_URL", "http://127.0.0.1:3000").rstrip("/")
    token = os.environ.get("ONEBOT_ACCESS_TOKEN", "")

    if type_ == "user":
        action, params = "send_private_msg", {"user_id": int(target_id), "message": message}
    else:
        action, params = "send_group_msg", {"group_id": int(target_id), "message": message}

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.post(f"{base_url}/{action}", json=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        print(f"发送失败：{e}")
        return 1

    if data.get("status") in ("ok", "async") or data.get("retcode") == 0:
        print(f"发送成功 message_id={data.get('data', {}).get('message_id')}")
        return 0
    print(f"发送失败：{data}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
