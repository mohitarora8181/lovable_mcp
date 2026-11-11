import asyncio
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import re

from .models import LandingPage


class ProjectExistsError(Exception):
    pass


class ProjectNotFoundError(Exception):
    pass


class JsonProjectStore:
    """Single-process local metadata store for pages in the shared project."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def create(self, project_id: str, lovable_project_id: str, idea: str) -> LandingPage:
        async with self._lock:
            data = self._read()
            route = f"/{slugify(project_id)[:50].rstrip('-') or 'landing-page'}"
            if project_id in data or any(item.get("route") == route for item in data.values()):
                raise ProjectExistsError(project_id)
            page = LandingPage(
                project_id=project_id,
                lovable_project_id=lovable_project_id,
                route=route,
                created_at=datetime.now(timezone.utc),
                idea=idea,
            )
            data[project_id] = page.model_dump(mode="json")
            self._write(data)
            return page

    async def get(self, project_id: str) -> LandingPage:
        async with self._lock:
            data = self._read()
            item = data.get(project_id)
            if item is None:
                raise ProjectNotFoundError(project_id)
            return LandingPage.model_validate(item)

    async def remove(self, project_id: str) -> None:
        """Undo a newly reserved page if Lovable rejects its creation."""
        async with self._lock:
            data = self._read()
            data.pop(project_id, None)
            self._write(data)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        temporary_path.replace(self.path)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "landing-page"


@lru_cache
def get_project_store(path: Path) -> JsonProjectStore:
    return JsonProjectStore(path)
