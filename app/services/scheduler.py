"""基于 APScheduler 的持久化任务调度。"""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.database.db import Database
from app.services.notifications import NotificationSettingsService
from app.services.qq.dispatcher import MessageDispatcher

logger = logging.getLogger(__name__)

TaskHandler = Callable[[dict], Awaitable[None]]
NOTIFICATION_BY_TASK = {
    "reminder": "reminder_notify",
}


class SchedulerValidationError(ValueError):
    """任务或提醒格式不合法。"""


def parse_reminder_command(text: str, timezone_info: tzinfo = UTC) -> tuple[datetime | None, str | None, str]:
    body = text.strip()
    if body.lower().startswith("/remind"):
        body = body[len("/remind"):].strip()
    schedule, separator, message = body.partition("--")
    message = message.strip()
    schedule = schedule.strip()
    if not separator or not schedule or not message:
        raise SchedulerValidationError("格式：/remind YYYY-MM-DD HH:MM -- 内容，或 /remind cron:0 8 * * * -- 内容")

    if schedule.lower().startswith("cron:"):
        cron_expression = schedule[5:].strip()
        try:
            CronTrigger.from_crontab(cron_expression, timezone=timezone_info)
        except ValueError as e:
            raise SchedulerValidationError("cron 表达式无效，应为 5 段格式") from e
        return None, cron_expression, message

    try:
        run_at = datetime.strptime(schedule, "%Y-%m-%d %H:%M").replace(tzinfo=timezone_info)
    except ValueError as e:
        raise SchedulerValidationError("时间格式应为 YYYY-MM-DD HH:MM") from e
    if run_at <= datetime.now(timezone_info):
        raise SchedulerValidationError("提醒时间必须晚于当前时间")
    return run_at, None, message


class SchedulerService:
    def __init__(self, db: Database, dispatcher: MessageDispatcher, timezone_name: str = "UTC"):
        try:
            self._timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as e:
            raise SchedulerValidationError(f"时区不存在：{timezone_name}") from e
        self._db = db
        self._dispatcher = dispatcher
        self._notifications = NotificationSettingsService(db)
        self._scheduler = AsyncIOScheduler(timezone=self._timezone)
        self._started = False
        self._handlers: dict[str, TaskHandler] = {"reminder": self._handle_reminder}

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def job_count(self) -> int:
        return len(self._scheduler.get_jobs()) if self._started else 0

    async def sync_system_task(self, task_type: str, cron_expression: str) -> None:
        task = await self._db.fetch_scheduled_task_by_owner_type("__system__", task_type)
        cron_expression = cron_expression.strip()
        if not cron_expression:
            if task is not None and task["enabled"]:
                await self._db.update_scheduled_task_schedule(task["id"], None, None, False)
            return
        if task is None:
            await self.create_task("__system__", task_type, {}, cron_expression=cron_expression)
            return
        if task["cron_expression"] != cron_expression or not task["enabled"]:
            next_run = self._next_cron_run(cron_expression)
            await self._db.update_scheduled_task_schedule(task["id"], cron_expression, next_run.isoformat(), True)

    async def start(self) -> None:
        if self._started:
            return
        for task in await self._db.fetch_enabled_scheduled_tasks():
            if not self._schedule(task):
                await self._db.mark_scheduled_task_run(task["id"], datetime.now(UTC).isoformat(), None, False)
        self._scheduler.start()
        self._started = True

    async def stop(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False

    async def create_reminder(
        self,
        owner_id: str,
        message: str,
        run_at: datetime | None = None,
        cron_expression: str | None = None,
    ) -> int:
        owner_id = str(owner_id).strip()
        message = str(message).strip()
        if not owner_id or not message:
            raise SchedulerValidationError("提醒人和提醒内容不能为空")
        return await self.create_task(
            owner_id,
            "reminder",
            {"target_type": "user", "target_id": owner_id, "message": message},
            run_at=run_at,
            cron_expression=cron_expression,
        )

    async def create_task(
        self,
        owner_id: str,
        task_type: str,
        payload: dict,
        *,
        run_at: datetime | None = None,
        cron_expression: str | None = None,
    ) -> int:
        if task_type not in self._handlers:
            raise SchedulerValidationError(f"未注册的任务类型：{task_type}")
        if (run_at is None) == (cron_expression is None):
            raise SchedulerValidationError("必须且只能指定一次性时间或 cron 表达式")
        if not isinstance(payload, dict):
            raise SchedulerValidationError("任务 payload 必须是对象")

        if cron_expression is not None:
            cron_expression = cron_expression.strip()
            try:
                CronTrigger.from_crontab(cron_expression, timezone=self._timezone)
            except ValueError as e:
                raise SchedulerValidationError("cron 表达式无效，应为 5 段格式") from e
            next_run = self._next_cron_run(cron_expression)
        else:
            run_at = self._normalize_datetime(run_at)
            if run_at <= datetime.now(self._timezone):
                raise SchedulerValidationError("任务时间必须晚于当前时间")
            next_run = run_at

        task_id = await self._db.create_scheduled_task(
            str(owner_id), task_type, payload, cron_expression, next_run.isoformat()
        )
        if self._started:
            task = await self._db.fetch_scheduled_task(task_id)
            if task is not None:
                self._schedule(task)
        return task_id

    async def run_task(self, task_id: int) -> None:
        task = await self._db.fetch_scheduled_task(task_id)
        if task is None or not task["enabled"]:
            return

        try:
            notification_field = NOTIFICATION_BY_TASK.get(task["task_type"])
            if notification_field:
                settings = await self._notifications.get(task["owner_id"])
                if not settings[notification_field]:
                    logger.info("通知已关闭，跳过任务 task_id=%s type=%s", task_id, task["task_type"])
                else:
                    await self._handlers[task["task_type"]](
                        {**task, "payload": json.loads(task["payload"])}
                    )
            else:
                await self._handlers[task["task_type"]]({**task, "payload": json.loads(task["payload"])})
        except Exception:
            logger.exception("定时任务执行失败 task_id=%s type=%s", task_id, task["task_type"])
        finally:
            next_run = None
            enabled = bool(task["cron_expression"])
            if enabled:
                next_run = self._next_cron_run(task["cron_expression"])
            await self._db.mark_scheduled_task_run(
                task_id, datetime.now(UTC).isoformat(), next_run.isoformat() if next_run else None, enabled
            )

    async def _handle_reminder(self, task: dict) -> None:
        payload = task["payload"]
        if payload.get("target_type") != "user" or str(payload.get("target_id")) != str(task["owner_id"]):
            raise SchedulerValidationError("提醒只能发送给任务创建者本人")
        await self._dispatcher.enqueue_user(task["owner_id"], str(payload["message"]))

    def _schedule(self, task: dict) -> bool:
        if task["cron_expression"]:
            trigger = CronTrigger.from_crontab(task["cron_expression"], timezone=self._timezone)
        else:
            run_at = self._parse_datetime(task["next_run"])
            if run_at <= datetime.now(self._timezone):
                return False
            trigger = DateTrigger(run_date=run_at, timezone=self._timezone)
        self._scheduler.add_job(
            self.run_task,
            trigger=trigger,
            args=[task["id"]],
            id=f"scheduled-task-{task['id']}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        return True

    def _next_cron_run(self, cron_expression: str) -> datetime:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=self._timezone)
        next_run = trigger.get_next_fire_time(None, datetime.now(self._timezone))
        if next_run is None:
            raise SchedulerValidationError("cron 表达式没有下一次执行时间")
        return next_run

    def _normalize_datetime(self, value: datetime | None) -> datetime:
        if value is None:
            raise SchedulerValidationError("一次性任务缺少执行时间")
        if value.tzinfo is None:
            return value.replace(tzinfo=self._timezone)
        return value.astimezone(self._timezone)

    def _parse_datetime(self, value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as e:
            raise SchedulerValidationError("数据库中的任务时间无效") from e
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._timezone)
        return parsed.astimezone(self._timezone)
