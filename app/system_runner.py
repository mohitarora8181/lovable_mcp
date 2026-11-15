"""OS scheduler entry point. Coalesces missed runs when the computer was off."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from apscheduler.triggers.cron import CronTrigger

from .automation import LogStore
from .config import get_settings
from .history import get_project_store
from .lovable import LovableMcpClient
from .oauth import LovableOAuth
from .system_scheduler import is_installed
from .workflows import WorkflowService


async def tick() -> None:
    settings = get_settings()
    if not is_installed():
        return
    state_path = settings.system_schedule_state_file
    lock_path = state_path.with_suffix(".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if datetime.now().timestamp() - lock_path.stat().st_mtime < 3600:
                return
            lock_path.unlink(missing_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileNotFoundError, FileExistsError):
            return
    os.close(descriptor)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        previous = datetime.fromisoformat(state["last_checked"]) if state.get("last_checked") else now - timedelta(minutes=1)
        svc = WorkflowService(settings, get_project_store(settings.projects_file), LovableMcpClient(settings.lovable_mcp_url, LovableOAuth(settings)), LogStore(settings.automation_logs_file))
        for workflow in await svc.store.list():
            if not workflow.enabled or not workflow.cron:
                continue
            trigger = CronTrigger.from_crontab(workflow.cron, timezone=settings.scheduler_timezone)
            local_previous = previous.astimezone(trigger.timezone)
            local_now = now.astimezone(trigger.timezone)
            due = trigger.get_next_fire_time(None, local_previous - timedelta(seconds=1))
            if due and due <= local_now:
                missed = 0
                while due and due <= local_now and missed < 500:
                    missed += 1
                    due = trigger.get_next_fire_time(due, local_now)
                await svc.run(workflow.id, force=False)
        state_path.write_text(json.dumps({"last_checked": now.isoformat()}), encoding="utf-8")
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "tick":
    asyncio.run(tick())
