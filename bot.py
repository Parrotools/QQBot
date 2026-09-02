"""qq-llm-bot 入口。

启动方式（项目根目录）：
    python bot.py

NapCatQQ 通过 OneBot 11 反向 WebSocket 连接到本进程：
    Bot 侧监听  ws://<HOST>:<PORT>/onebot/v11/ws
"""

from pathlib import Path

import nonebot
from fastapi.responses import JSONResponse
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_plugins(str(Path(__file__).parent / "app" / "plugins"))


async def _startup() -> None:
    from app.services.runtime import init_runtime

    await init_runtime()


async def _shutdown() -> None:
    from app.services.runtime import close_runtime

    await close_runtime()


driver.on_startup(_startup)
driver.on_shutdown(_shutdown)


@driver.server_app.get("/healthz")
async def healthz():
    from app.services.runtime import get_runtime

    try:
        result = await get_runtime().health.check()
    except RuntimeError:
        return JSONResponse({"ok": False, "detail": "starting"}, status_code=503)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)

if __name__ == "__main__":
    nonebot.run()
