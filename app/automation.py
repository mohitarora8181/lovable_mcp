import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .history import JsonProjectStore, ProjectExistsError, slugify
from .lovable import LovableMcpClient, LovableMcpError
from .models import AutomationLog, CreateProjectRequest, ScheduleRequest, ScheduleResponse


class ScheduleStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    async def list(self) -> list[ScheduleResponse]:
        async with self.lock:
            return [ScheduleResponse.model_validate(item) for item in self._read().values()]

    async def get(self, schedule_id: str) -> ScheduleResponse | None:
        async with self.lock:
            item = self._read().get(schedule_id)
            return ScheduleResponse.model_validate(item) if item else None

    async def create(self, payload: ScheduleRequest) -> ScheduleResponse:
        _validate_cron(payload.cron)
        async with self.lock:
            data = self._read()
            item = ScheduleResponse(id=str(uuid4()), **payload.model_dump())
            data[item.id] = item.model_dump(mode="json")
            self._write(data)
            return item

    async def update(self, schedule_id: str, payload: ScheduleRequest) -> ScheduleResponse | None:
        _validate_cron(payload.cron)
        async with self.lock:
            data = self._read()
            old = data.get(schedule_id)
            if not old:
                return None
            item = ScheduleResponse(
                id=schedule_id,
                last_run_at=old.get("last_run_at"),
                last_status=old.get("last_status"),
                last_detail=old.get("last_detail"),
                **payload.model_dump(),
            )
            data[schedule_id] = item.model_dump(mode="json")
            self._write(data)
            return item

    async def remove(self, schedule_id: str) -> bool:
        async with self.lock:
            data = self._read()
            found = data.pop(schedule_id, None) is not None
            self._write(data)
            return found

    async def result(self, schedule_id: str, status: str, detail: str) -> None:
        async with self.lock:
            data = self._read()
            if schedule_id in data:
                data[schedule_id].update(
                    {"last_run_at": datetime.now(timezone.utc).isoformat(), "last_status": status, "last_detail": detail}
                )
                self._write(data)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.path)


class LogStore:
    """A short persistent activity feed for the local control panel."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    async def add(self, level: str, message: str, schedule_id: str | None = None) -> None:
        async with self.lock:
            items = self._read()
            items.append(AutomationLog(timestamp=datetime.now(timezone.utc), level=level, message=message, schedule_id=schedule_id).model_dump(mode="json"))
            self._write(items[-200:])

    async def list(self, limit: int = 100) -> list[AutomationLog]:
        async with self.lock:
            return [AutomationLog.model_validate(item) for item in self._read()[-limit:]][::-1]

    def _read(self) -> list:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, items: list) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        temp.replace(self.path)


class AutomationService:
    def __init__(self, settings: Settings, projects: JsonProjectStore, lovable: LovableMcpClient) -> None:
        self.settings, self.projects, self.lovable = settings, projects, lovable
        self.store = ScheduleStore(settings.schedules_file)
        self.logs = LogStore(settings.automation_logs_file)
        self.scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)

    async def start(self) -> None:
        self.scheduler.start()
        for job in await self.store.list():
            self.sync(job)

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def sync(self, job: ScheduleResponse) -> None:
        self.scheduler.remove_job(job.id) if self.scheduler.get_job(job.id) else None
        if job.enabled:
            self.scheduler.add_job(self.run, CronTrigger.from_crontab(job.cron, timezone=self.settings.scheduler_timezone), args=[job.id], id=job.id, replace_existing=True, max_instances=1, coalesce=True)

    async def run(self, schedule_id: str) -> None:
        job = await self.store.get(schedule_id)
        if not job or not job.enabled:
            return
        try:
            await self.logs.add("info", f"Started scheduled build: {job.name}", schedule_id)
            payload = CreateProjectRequest(
                idea=job.prompt,
                reference_sites=job.reference_sites,
                images=job.images,
                urls_to_include=job.urls_to_include,
            )
            lovable_project_id = self.settings.lovable_project_id
            if not lovable_project_id:
                raise RuntimeError("LOVABLE_PROJECT_ID is not configured.")
            project_id = payload.project_id or f"{slugify(payload.idea)}-{datetime.now().strftime('%Y%m%d')}"
            try:
                page = await self.projects.create(project_id, lovable_project_id, payload.idea)
            except ProjectExistsError:
                project_id = f"{project_id}-{uuid4().hex[:6]}"
                page = await self.projects.create(project_id, lovable_project_id, payload.idea)
            from .main import _create_page_prompt
            await self.logs.add("info", f"Sending page brief for {page.route} to Lovable", schedule_id)
            await self.lovable.send_message(lovable_project_id, _create_page_prompt(page, payload))
            await self.logs.add("info", f"Lovable completed build for {page.route}; publishing", schedule_id)
            await self.lovable.deploy_project(lovable_project_id)
            await self.store.result(schedule_id, "success", f"Published {page.route}")
            await self.logs.add("success", f"Published {page.route}", schedule_id)
        except Exception as exc:
            await self.store.result(schedule_id, "failed", str(exc)[:1000])
            await self.logs.add("error", f"Failed: {str(exc)[:500]}", schedule_id)


def _validate_cron(value: str) -> None:
    try:
        CronTrigger.from_crontab(value)
    except ValueError as exc:
        raise ValueError("cron must have five fields, e.g. '0 9 * * *'.") from exc
