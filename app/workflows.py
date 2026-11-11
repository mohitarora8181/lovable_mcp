import asyncio
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import socket
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import httpx
import jmespath
from jmespath import functions
from jsonschema import validate as validate_schema

from .automation import LogStore
from .config import Settings, get_settings
from .history import JsonProjectStore, ProjectExistsError, slugify
from .lovable import LovableMcpClient
from .models import CreateProjectRequest, WorkflowRequest, WorkflowResponse


class WorkflowJMESPathFunctions(functions.Functions):
    @functions.signature()
    def _func_today(self) -> str:
        timezone = ZoneInfo(get_settings().scheduler_timezone)
        return datetime.now(timezone).date().isoformat()

    @functions.signature()
    def _func_current_date(self) -> str:
        timezone = ZoneInfo(get_settings().scheduler_timezone)
        return datetime.now(timezone).date().isoformat()

    @functions.signature()
    def _func_now(self) -> str:
        timezone = ZoneInfo(get_settings().scheduler_timezone)
        return datetime.now(timezone).isoformat(timespec="seconds")

    @functions.signature({"types": ["string"]})
    def _func_lower(self, value: str) -> str:
        return value.lower()

    @functions.signature({"types": ["string"]})
    def _func_to_lower(self, value: str) -> str:
        return value.lower()

    @functions.signature({"types": ["string"]})
    def _func_upper(self, value: str) -> str:
        return value.upper()

    @functions.signature({"types": ["string"]})
    def _func_to_upper(self, value: str) -> str:
        return value.upper()

    @functions.signature(
        {"types": ["string"]},
        {"types": ["string"]},
        {"types": ["string"]},
    )
    def _func_replace(self, value: str, old: str, new: str) -> str:
        return value.replace(old, new)


_JMESPATH_OPTIONS = jmespath.Options(custom_functions=WorkflowJMESPathFunctions())


def _jmespath_search(expression: str, value):
    return jmespath.search(expression, value, options=_JMESPATH_OPTIONS)


class WorkflowRunEvents:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.condition = asyncio.Condition()
        self.finished = False

    async def publish(self, event: dict) -> None:
        async with self.condition:
            self.events.append(event)
            if event.get("type") in {"run_completed", "run_failed"}:
                self.finished = True
            self.condition.notify_all()

    async def stream(self):
        cursor = 0
        while True:
            timed_out = False
            async with self.condition:
                if cursor >= len(self.events) and not self.finished:
                    try:
                        await asyncio.wait_for(
                            self.condition.wait_for(lambda: cursor < len(self.events) or self.finished),
                            timeout=15,
                        )
                    except TimeoutError:
                        timed_out = True
                batch = self.events[cursor:]
                cursor = len(self.events)
                done = self.finished and not batch
            if timed_out:
                yield ": keep-alive\n\n"
                continue
            for event in batch:
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            if done:
                break


class WorkflowStore:
    def __init__(self, path: Path) -> None:
        self.path, self.lock = path, asyncio.Lock()

    async def list(self) -> list[WorkflowResponse]:
        async with self.lock:
            return [WorkflowResponse.model_validate(item) for item in self._read().values()]

    async def get(self, workflow_id: str) -> WorkflowResponse | None:
        async with self.lock:
            item = self._read().get(workflow_id)
            return WorkflowResponse.model_validate(item) if item else None

    async def create(self, payload: WorkflowRequest) -> WorkflowResponse:
        _validate_workflow(payload)
        async with self.lock:
            data = self._read()
            item = WorkflowResponse(id=str(uuid4()), **payload.model_dump())
            data[item.id] = item.model_dump(mode="json")
            self._write(data)
            return item

    async def update(self, workflow_id: str, payload: WorkflowRequest) -> WorkflowResponse | None:
        _validate_workflow(payload)
        async with self.lock:
            data = self._read()
            old = data.get(workflow_id)
            if not old:
                return None
            item = WorkflowResponse(id=workflow_id, last_run_at=old.get("last_run_at"), last_status=old.get("last_status"), last_detail=old.get("last_detail"), **payload.model_dump())
            data[workflow_id] = item.model_dump(mode="json")
            self._write(data)
            return item

    async def remove(self, workflow_id: str) -> bool:
        async with self.lock:
            data = self._read()
            found = data.pop(workflow_id, None) is not None
            self._write(data)
            return found

    async def result(self, workflow_id: str, state: str, detail: str) -> None:
        async with self.lock:
            data = self._read()
            if workflow_id in data:
                data[workflow_id].update({"last_run_at": datetime.now(timezone.utc).isoformat(), "last_status": state, "last_detail": detail})
                self._write(data)

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.path)


class WorkflowService:
    def __init__(self, settings: Settings, projects: JsonProjectStore, lovable: LovableMcpClient, logs: LogStore) -> None:
        self.settings, self.projects, self.lovable, self.logs = settings, projects, lovable, logs
        self.store = WorkflowStore(settings.workflows_file)
        self.scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
        self.run_channels: dict[str, WorkflowRunEvents] = {}

    def create_run(self) -> str:
        run_id = str(uuid4())
        self.run_channels[run_id] = WorkflowRunEvents()
        if len(self.run_channels) > 100:
            for old_id, channel in list(self.run_channels.items()):
                if channel.finished:
                    self.run_channels.pop(old_id, None)
                    if len(self.run_channels) <= 80:
                        break
        return run_id

    def get_run_events(self, run_id: str) -> WorkflowRunEvents | None:
        return self.run_channels.get(run_id)

    async def _publish(self, run_id: str | None, event: dict) -> None:
        if run_id and run_id in self.run_channels:
            await self.run_channels[run_id].publish(event)

    @staticmethod
    def _event_data(value):
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) > 50_000:
            return {"truncated": True, "preview": encoded[:50_000]}
        return value

    async def start(self) -> None:
        self.scheduler.start()
        for workflow in await self.store.list():
            self.sync(workflow)

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def sync(self, workflow: WorkflowResponse) -> None:
        if self.scheduler.get_job(workflow.id):
            self.scheduler.remove_job(workflow.id)
        if workflow.enabled and workflow.cron:
            self.scheduler.add_job(self.run, CronTrigger.from_crontab(workflow.cron, timezone=self.settings.scheduler_timezone), args=[workflow.id], id=workflow.id, max_instances=1, coalesce=True)

    async def run(self, workflow_id: str, run_id: str | None = None, force: bool = False) -> None:
        workflow = await self.store.get(workflow_id)
        current = None
        node_running = False
        if not workflow:
            await self._publish(run_id, {"type": "run_failed", "message": "Workflow not found."})
            return
        if not workflow.enabled and not force:
            await self._publish(run_id, {"type": "run_failed", "message": "Workflow is disabled."})
            return
        try:
            await self._publish(run_id, {"type": "run_started", "workflow_id": workflow_id, "name": workflow.name})
            await self.logs.add("info", f"Workflow started: {workflow.name}", workflow_id)
            nodes = {node.id: node for node in workflow.nodes}
            outgoing = {edge.source: edge.target for edge in workflow.edges}
            current, context, steps = next(node for node in workflow.nodes if node.type == "start"), {"workflow": {}}, 0
            while True:
                steps += 1
                if steps > len(nodes) + 1:
                    raise RuntimeError("Workflow loop detected.")
                node_input = _map_input(current.input_mapping, context, context.get("previous", {}))
                await self._publish(run_id, {
                    "type": "node_started", "node_id": current.id, "node_type": current.type,
                    "label": current.label, "input": self._event_data(node_input),
                })
                node_running = True
                started_at = perf_counter()
                if current.input_schema:
                    validate_schema(instance=node_input, schema=current.input_schema)
                previous = context.get("previous", {})
                prepared = current.model_copy(update={
                    "config": _render_templates(current.config, previous),
                    "output_selector": _render_templates(current.output_selector, previous),
                    "transform_script": _render_templates(current.transform_script, previous),
                    "script": _render_templates(current.script, previous),
                })
                output = await self._execute(prepared, node_input, workflow_id)
                if current.output_schema:
                    validate_schema(instance=output, schema=current.output_schema)
                await self._publish(run_id, {
                    "type": "node_completed", "node_id": current.id, "node_type": current.type,
                    "label": current.label, "input": self._event_data(node_input),
                    "output": self._event_data(output), "duration_ms": round((perf_counter() - started_at) * 1000),
                })
                node_running = False
                context["previous"] = output
                context["workflow"][current.id] = output
                if current.type == "stop":
                    break
                target_id = outgoing.get(current.id)
                if not target_id or target_id not in nodes:
                    raise RuntimeError(f"Node '{current.label}' has no valid next connection.")
                current = nodes[target_id]
            await self.store.result(workflow_id, "success", "Workflow completed")
            await self.logs.add("success", f"Workflow completed: {workflow.name}", workflow_id)
            await self._publish(run_id, {"type": "run_completed", "message": "Workflow completed."})
        except Exception as exc:
            await self.store.result(workflow_id, "failed", str(exc)[:1000])
            await self.logs.add("error", f"Workflow failed: {str(exc)[:500]}", workflow_id)
            if node_running and current is not None:
                await self._publish(run_id, {
                    "type": "node_failed", "node_id": current.id, "node_type": current.type,
                    "label": current.label, "message": str(exc)[:3000],
                })
            await self._publish(run_id, {"type": "run_failed", "message": str(exc)[:3000]})

    async def _execute(self, node, node_input: dict, workflow_id: str):
        await self.logs.add("info", f"Running {node.type} node: {node.label}", workflow_id)
        if node.type == "start":
            return node.config or node_input
        if node.type == "stop":
            return node_input
        if node.type == "transform":
            if node.config.get("mode", "selector") == "script":
                raise RuntimeError("Python script mode is unavailable until its execution policy is approved. Use Selector mode with a JMESPath expression.")
            expression = node.transform_script or node.config.get("expression")
            if not expression:
                raise RuntimeError("Selector transform needs a JMESPath expression.")
            return _jmespath_search(expression, node_input)
        if node.type == "external_api":
            url = str(node.config.get("url", ""))
            await _ensure_public_http_url(url)
            method = str(node.config.get("method", "GET")).upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                raise RuntimeError(f"Unsupported HTTP method: {method}")
            headers = node.config.get("headers") or {}
            if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                raise RuntimeError("HTTP node headers must be a JSON object containing string values.")
            headers = {"Accept": "application/json", **headers}
            query = _normalize_query_params(node.config.get("query") or {})
            body = node.config.get("body")
            if isinstance(body, (dict, list)):
                body = json.dumps(body)
                headers.setdefault("Content-Type", "application/json")
            request_url = _merge_query_params(url, query)
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.request(
                    method,
                    request_url,
                    headers=headers,
                    content=body.encode("utf-8") if isinstance(body, str) and body else body,
                )
                response.raise_for_status()
                if not response.content:
                    data = {}
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        data = response.text
            return _jmespath_search(node.output_selector, data) if node.output_selector else data
        if node.type == "create_page":
            payload_data = {**node.config, **node_input}
            for field in ("reference_sites", "images", "urls_to_include"):
                payload_data[field] = payload_data.get(field, []) or []
            payload = CreateProjectRequest.model_validate(payload_data)
            host_id = self.settings.lovable_project_id
            if not host_id:
                raise RuntimeError("LOVABLE_PROJECT_ID is not configured.")
            page_id = payload.project_id if payload.project_id is not None else f"{slugify(payload.idea)}-{datetime.now().strftime('%Y%m%d')}"
            try:
                page = await self.projects.create(page_id, host_id, payload.idea)
            except ProjectExistsError as exc:
                raise RuntimeError(f"Project ID '{page_id}' or its generated route already exists. Workflow cancelled without publishing.") from exc
            from .main import _create_page_prompt, _is_duplicate_route_cancellation
            response = await self.lovable.send_message(host_id, _create_page_prompt(page, payload))
            if _is_duplicate_route_cancellation(response):
                await self.projects.remove(page.project_id)
                raise RuntimeError(f"Project ID '{page.project_id}' or its route already exists in Lovable. Workflow cancelled without publishing.")
            await self.lovable.deploy_project(host_id)
            return {"project_id": page.project_id, "route": page.route}
        raise RuntimeError(f"Unsupported node type: {node.type}")


def _map_input(mapping: dict[str, str], context: dict, fallback) -> dict:
    if not mapping:
        return fallback if isinstance(fallback, dict) else {"value": fallback}
    return {
        key: _render_templates(selector, fallback)
        if "{{" in selector and "}}" in selector
        else _jmespath_search(selector.removeprefix("$.") if selector.startswith("$.") else selector, context)
        for key, selector in mapping.items()
    }


def _normalize_query_params(query) -> dict[str, object]:
    if not isinstance(query, dict):
        raise RuntimeError("HTTP node query parameters must be a JSON object.")

    def normalize(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (str, int, float)):
            return value
        if isinstance(value, list):
            return [item for item in (normalize(part) for part in value) if item is not None]
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        raise RuntimeError("HTTP query values must be strings, numbers, booleans, arrays, or objects.")

    if not all(isinstance(key, str) for key in query):
        raise RuntimeError("HTTP query parameter names must be strings.")
    return {key: normalized for key, value in query.items() if (normalized := normalize(value)) is not None}


def _merge_query_params(url: str, query: dict[str, object]) -> httpx.URL:
    """Preserve URL query pairs and append configured HTTP-node parameters."""
    request_url = httpx.URL(url)
    if not query:
        return request_url
    merged = list(request_url.params.multi_items())
    merged.extend(httpx.QueryParams(query).multi_items())
    return request_url.copy_with(query=str(httpx.QueryParams(merged)).encode("utf-8"))


_TEMPLATE_PATTERN = re.compile(r"{{\s*(.*?)\s*}}")


def _render_templates(value, previous):
    """Resolve {{path}} tokens recursively against the preceding node output."""
    if isinstance(value, dict):
        return {
            _render_templates(key, previous) if isinstance(key, str) else key:
            _render_templates(item, previous)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_render_templates(item, previous) for item in value]
    if not isinstance(value, str):
        return value

    matches = list(_TEMPLATE_PATTERN.finditer(value))
    if not matches:
        return value

    def resolve(expression: str):
        expression = expression.strip()
        if expression in {"", ".", "input", "previous", "output", "this"}:
            return previous
        for prefix in ("input.", "previous.", "output.", "this."):
            if expression.startswith(prefix):
                expression = expression[len(prefix):]
                break
        source = previous
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Cannot read '{{{{{expression}}}}}' because the previous output is text, not JSON. Use '{{{{input}}}}' to insert the full text."
                ) from exc
        if isinstance(source, dict) and "input" not in source:
            source = {**source, "input": source}
        return _jmespath_search(expression, source)

    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return resolve(matches[0].group(1))

    def as_text(item) -> str:
        if item is None:
            return ""
        if isinstance(item, (dict, list)):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, bool):
            return "true" if item else "false"
        return str(item)

    return _TEMPLATE_PATTERN.sub(lambda match: as_text(resolve(match.group(1))), value)


async def _ensure_public_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("External API nodes accept HTTPS public URLs only.")
    addresses = await asyncio.get_running_loop().run_in_executor(None, lambda: socket.getaddrinfo(parsed.hostname, None))
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise RuntimeError("External API URL resolves to a non-public address.")


def _validate_workflow(workflow: WorkflowRequest) -> None:
    if workflow.cron:
        CronTrigger.from_crontab(workflow.cron)
    ids = {node.id for node in workflow.nodes}
    if len(ids) != len(workflow.nodes) or sum(node.type == "start" for node in workflow.nodes) != 1 or sum(node.type == "stop" for node in workflow.nodes) != 1:
        raise ValueError("A workflow needs unique node IDs and exactly one Start and one Stop node.")
    if any(edge.source not in ids or edge.target not in ids for edge in workflow.edges):
        raise ValueError("Every connection must refer to an existing node.")
    outgoing: dict[str, str] = {}
    for edge in workflow.edges:
        if edge.source in outgoing:
            raise ValueError("Each node can have at most one outgoing connection.")
        outgoing[edge.source] = edge.target
    nodes = {node.id: node for node in workflow.nodes}
    start = next(node for node in workflow.nodes if node.type == "start")
    if any(edge.target == start.id for edge in workflow.edges):
        raise ValueError("Start cannot have an incoming connection.")
    if any(node.type == "stop" and node.id in outgoing for node in workflow.nodes):
        raise ValueError("Stop cannot have an outgoing connection.")
    visited, current = set(), start.id
    while current in outgoing:
        if current in visited:
            raise ValueError("Workflow connections cannot contain cycles.")
        visited.add(current)
        current = outgoing[current]
    if nodes[current].type != "stop":
        raise ValueError("The Start path must end at Stop.")
