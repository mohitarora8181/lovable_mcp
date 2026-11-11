from functools import lru_cache
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .config import Settings, get_settings
from .history import (
    JsonProjectStore,
    ProjectExistsError,
    ProjectNotFoundError,
    get_project_store,
    slugify,
)
from .lovable import LovableMcpClient, LovableMcpError
from .models import (
    CreateProjectRequest,
    CreateProjectResponse,
    DeleteProjectResponse,
    EnhanceProjectRequest,
    EnhanceProjectResponse,
    LandingPage,
)
from .oauth import LovableOAuth, LovableOAuthError
from .security import require_api_key
from .automation import AutomationService
from .models import AutomationLog, ScheduleRequest, ScheduleResponse, WorkflowRequest, WorkflowResponse
from .dashboard_spa import DASHBOARD_SPA_HTML
from .workflow_dashboard import WORKFLOW_DASHBOARD_HTML
from .workflows import WorkflowService

@asynccontextmanager
async def lifespan(_: FastAPI):
    await get_automation().start()
    await get_workflows().start()
    yield
    get_automation().stop()
    get_workflows().stop()


app = FastAPI(title="Lovable Landing Pages API", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in get_settings().cors_allowed_origins.split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
logger = logging.getLogger(__name__)
DUPLICATE_ROUTE_SENTINEL = "CANCEL_DUPLICATE_PROJECT_ID"


def _is_duplicate_route_cancellation(response: str) -> bool:
    normalized = response.strip().strip("`").strip()
    return normalized == DUPLICATE_ROUTE_SENTINEL


def get_projects(settings: Settings = Depends(get_settings)) -> JsonProjectStore:
    return get_project_store(settings.projects_file)


@lru_cache
def get_lovable_oauth() -> LovableOAuth:
    return LovableOAuth(get_settings())


def get_lovable_client(settings: Settings = Depends(get_settings)) -> LovableMcpClient:
    return LovableMcpClient(settings.lovable_mcp_url, get_lovable_oauth())


@lru_cache
def get_automation() -> AutomationService:
    settings = get_settings()
    return AutomationService(settings, get_project_store(settings.projects_file), LovableMcpClient(settings.lovable_mcp_url, get_lovable_oauth()))


@lru_cache
def get_workflows() -> WorkflowService:
    settings = get_settings()
    automation = get_automation()
    return WorkflowService(settings, get_project_store(settings.projects_file), LovableMcpClient(settings.lovable_mcp_url, get_lovable_oauth()), automation.logs)


@app.get("/", include_in_schema=False)
async def dashboard() -> HTMLResponse:
    # The dashboard never polls logs automatically: logging is refreshed only by user action.
    return HTMLResponse(
        WORKFLOW_DASHBOARD_HTML.replace(
            "document.querySelector('.side button:nth-child(3)').click()",
            "document.querySelectorAll('.side button')[1].click()",
        )
    )


@app.get("/auth/lovable/login", include_in_schema=False)
async def lovable_login(oauth: LovableOAuth = Depends(get_lovable_oauth)) -> RedirectResponse:
    try:
        return RedirectResponse(await oauth.authorization_url(), status_code=status.HTTP_302_FOUND)
    except LovableOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/auth/lovable/callback", include_in_schema=False)
async def lovable_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    oauth: LovableOAuth = Depends(get_lovable_oauth),
) -> HTMLResponse:
    if error:
        detail = error_description or error
        raise HTTPException(status_code=400, detail=f"Lovable authorization was denied: {detail}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Lovable callback is missing code or state.")
    try:
        await oauth.exchange_callback(code, state)
    except LovableOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return HTMLResponse(
        "<h1>Lovable connected</h1><p>You can close this window and return to the API.</p>"
    )


@app.get("/automation/jobs", response_model=list[ScheduleResponse])
async def list_jobs(_: None = Depends(require_api_key)) -> list[ScheduleResponse]:
    return await get_automation().store.list()


@app.get("/automation/logs", response_model=list[AutomationLog])
async def list_logs(_: None = Depends(require_api_key)) -> list[AutomationLog]:
    return await get_automation().logs.list()


@app.post("/automation/jobs", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_job(payload: ScheduleRequest, _: None = Depends(require_api_key)) -> ScheduleResponse:
    try:
        job = await get_automation().store.create(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    get_automation().sync(job)
    return job


@app.put("/automation/jobs/{schedule_id}", response_model=ScheduleResponse)
async def update_job(schedule_id: str, payload: ScheduleRequest, _: None = Depends(require_api_key)) -> ScheduleResponse:
    try:
        job = await get_automation().store.update(schedule_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    get_automation().sync(job)
    return job


@app.post("/automation/jobs/{schedule_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_job(schedule_id: str, background_tasks: BackgroundTasks, _: None = Depends(require_api_key)) -> dict[str, str]:
    if not await get_automation().store.get(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    background_tasks.add_task(get_automation().run, schedule_id)
    return {"status": "queued"}


@app.delete("/automation/jobs/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(schedule_id: str, _: None = Depends(require_api_key)) -> None:
    automation = get_automation()
    if not await automation.store.remove(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    if automation.scheduler.get_job(schedule_id):
        automation.scheduler.remove_job(schedule_id)


@app.get("/workflows", response_model=list[WorkflowResponse])
async def list_workflows(_: None = Depends(require_api_key)) -> list[WorkflowResponse]:
    return await get_workflows().store.list()


@app.post("/workflows", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(payload: WorkflowRequest, _: None = Depends(require_api_key)) -> WorkflowResponse:
    try:
        workflow = await get_workflows().store.create(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    get_workflows().sync(workflow)
    return workflow


@app.put("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(workflow_id: str, payload: WorkflowRequest, _: None = Depends(require_api_key)) -> WorkflowResponse:
    try:
        workflow = await get_workflows().store.update(workflow_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    get_workflows().sync(workflow)
    return workflow


@app.post("/workflows/{workflow_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_workflow(workflow_id: str, background_tasks: BackgroundTasks, _: None = Depends(require_api_key)) -> dict[str, str]:
    service = get_workflows()
    if not await service.store.get(workflow_id):
        raise HTTPException(status_code=404, detail="Workflow not found.")
    run_id = service.create_run()
    background_tasks.add_task(service.run, workflow_id, run_id, True)
    return {"status": "queued", "run_id": run_id}


@app.get("/workflows/runs/{run_id}/events")
async def workflow_run_events(run_id: str, _: None = Depends(require_api_key)) -> StreamingResponse:
    channel = get_workflows().get_run_events(run_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Workflow run not found or expired.")
    return StreamingResponse(
        channel.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(workflow_id: str, _: None = Depends(require_api_key)) -> None:
    service = get_workflows()
    if not await service.store.remove(workflow_id):
        raise HTTPException(status_code=404, detail="Workflow not found.")
    if service.scheduler.get_job(workflow_id):
        service.scheduler.remove_job(workflow_id)


@app.post("/projects", response_model=CreateProjectResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_project(
    payload: CreateProjectRequest,
    background_tasks: BackgroundTasks,
    projects: JsonProjectStore = Depends(get_projects),
    lovable: LovableMcpClient = Depends(get_lovable_client),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_api_key),
) -> CreateProjectResponse:
    lovable_project_id = _host_project_id(settings)
    project_id = payload.project_id if payload.project_id is not None else slugify(payload.idea)
    try:
        page = await projects.create(project_id, lovable_project_id, payload.idea)
    except ProjectExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail="This project_id or its generated route already exists. Choose another project_id.",
        ) from exc

    background_tasks.add_task(
        _build_and_publish,
        lovable,
        lovable_project_id,
        _create_page_prompt(page, payload),
        projects,
        project_id,
        True,
    )

    return CreateProjectResponse(
        **page.model_dump(),
        url=_page_url(settings, page.route),
    )


@app.post(
    "/projects/{project_id:path}/enhancements",
    response_model=EnhanceProjectResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enhance_project(
    project_id: str,
    payload: EnhanceProjectRequest,
    background_tasks: BackgroundTasks,
    projects: JsonProjectStore = Depends(get_projects),
    lovable: LovableMcpClient = Depends(get_lovable_client),
    _: None = Depends(require_api_key),
) -> EnhanceProjectResponse:
    page = await _get_project_or_404(project_id, projects)
    background_tasks.add_task(
        _build_and_publish,
        lovable,
        page.lovable_project_id,
        _enhancement_prompt(page, payload.enhancement),
        projects,
        page.project_id,
        False,
    )
    return EnhanceProjectResponse(
        project_id=page.project_id,
        route=page.route,
    )


@app.delete(
    "/projects/{project_id:path}",
    response_model=DeleteProjectResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    projects: JsonProjectStore = Depends(get_projects),
    lovable: LovableMcpClient = Depends(get_lovable_client),
    _: None = Depends(require_api_key),
) -> DeleteProjectResponse:
    page = await _get_project_or_404(project_id, projects)
    background_tasks.add_task(_delete_and_publish, lovable, page, projects)
    return DeleteProjectResponse(project_id=page.project_id, route=page.route)


async def _build_and_publish(
    lovable: LovableMcpClient,
    lovable_project_id: str,
    prompt: str,
    projects: JsonProjectStore,
    project_id: str,
    is_new_page: bool,
) -> None:
    """Run after the HTTP response: build first, then publish the host project."""
    try:
        response = await lovable.send_message(lovable_project_id, prompt)
        if _is_duplicate_route_cancellation(response):
            if is_new_page:
                await projects.remove(project_id)
            logger.info("Cancelled page build for existing route '%s'; nothing was published.", project_id)
            return
        await lovable.deploy_project(lovable_project_id)
        logger.info("Lovable completed and published page '%s'.", project_id)
    except LovableMcpError:
        logger.exception("Lovable background build/publish failed for page '%s'.", project_id)
        if is_new_page:
            await projects.remove(project_id)


async def _delete_and_publish(
    lovable: LovableMcpClient,
    page: LandingPage,
    projects: JsonProjectStore,
) -> None:
    """Remove one page route and publish only after Lovable confirms the change."""
    try:
        await lovable.send_message(page.lovable_project_id, _delete_page_prompt(page))
        await lovable.deploy_project(page.lovable_project_id)
        await projects.remove(page.project_id)
        logger.info("Lovable deleted and unpublished page '%s'.", page.project_id)
    except LovableMcpError:
        logger.exception("Lovable background delete/publish failed for page '%s'.", page.project_id)


async def _get_project_or_404(project_id: str, projects: JsonProjectStore) -> LandingPage:
    try:
        return await projects.get(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project page not found.") from exc


def _host_project_id(settings: Settings) -> str:
    if not settings.lovable_project_id:
        raise HTTPException(
            status_code=500,
            detail="LOVABLE_PROJECT_ID must be set to the one allowed Lovable host project.",
        )
    return settings.lovable_project_id


def _page_url(settings: Settings, route: str) -> str:
    route = "/" + route.lstrip("/")
    if settings.lovable_base_url:
        return f"{settings.lovable_base_url.rstrip('/')}{route}"
    return route


def _create_page_prompt(page: LandingPage, payload: CreateProjectRequest) -> str:
    return f"""You are modifying a shared multi-page landing-site project.

Create one new landing page for this idea: {payload.idea}

Treat the supplied project ID `{page.project_id}` as an opaque identifier: keep it unchanged as the project ID, do not reject or normalize it, and do not use it directly as a filesystem path. Derive a short, readable, lowercase URL route from that project ID. The server's reserved route for this request is `{page.route}`; use that exact route consistently in the router, links, and metadata.

Before making any edits, inspect the current route definitions and route-specific folders. Cancel only if the exact route `{page.route}` is already registered or its exact route folder already exists in the current project files. A similar route name, matching page idea, prior conversation, project ID mentioned in this prompt, or uncertainty is not evidence of a duplicate. If no exact route or folder exists, proceed with creation. If an exact duplicate exists, do not write, edit, generate, or delete code; do not change files; and do not publish or deploy anything. Return exactly this cancellation marker as your entire response: `{DUPLICATE_ROUTE_SENTINEL}`.

Only if `{page.project_id}` and `{page.route}` do not already exist, proceed with this page creation. Its public route MUST be `{page.route}`. Follow the existing framework's routing conventions and keep the implementation isolated under a route-specific folder named after the short route segment (or the closest framework-equivalent route folder). Register `{page.route}` and add an interactive link to it from the official homepage at `/`.

On the official homepage, add a clear, accessible, responsive link or page card for `{page.route}`. Use a concise label based on this page's content and point it directly to `{page.route}`. Append it to an existing pages, projects, or featured-content section when available; otherwise add a small section for this page without redesigning the homepage. Preserve all existing homepage content, links, visual styling, and behavior.

Do not change, move, delete, rename, restyle, or break any existing routes or their route-specific folders. The only allowed existing-page edit is the minimal change to the official homepage needed to add this link. Do not alter global configuration unless strictly required to register the new route.

This is an autonomous, pre-approved build request from a server-side automation. Do not ask for clarification, confirmation, approval, or a design choice. Do not pause for user input. Make sensible product and design decisions from the provided brief, complete the implementation, and report the result only after the work is done.

Design references: {payload.reference_sites}
Image URLs/assets to use when appropriate: {payload.images}
URLs that must appear somewhere on this page: {payload.urls_to_include}

Before designing or coding, infer the product category, likely audience, brand personality, and visual expectations from the idea and design references. Evaluate several appropriate color-theme directions internally, then deliberately choose the strongest one for this specific landing page. Use the selected palette consistently for background, surface, typography, primary/secondary calls to action, and interactive states. Prioritize clear visual hierarchy and accessible text/background contrast; do not copy a reference site's branding unless the request explicitly asks for it.

Build a polished, responsive, accessible single landing page using that chosen visual direction. Include route-specific SEO metadata: a unique descriptive title, meta description, canonical URL for `{page.route}` when the site's base URL is available, and suitable Open Graph/social sharing title, description, and image metadata. Make the page recognizable to search engines by using one clear H1, meaningful section headings, semantic HTML, descriptive link text, and image alt text. Do not duplicate another route's title or description. Include a footer on this page with the visible credit `Developed by Mohit Arora`, where `Mohit Arora` is a hyperlink to `https://instagram.com/mohit_8181`. Report the selected color direction, SEO metadata, files, and route you changed when finished."""


def _enhancement_prompt(page: LandingPage, enhancement: str) -> str:
    return f"""You are modifying a shared multi-page landing-site project.

Apply this enhancement only to the landing page at `{page.route}`: {enhancement}

This is an autonomous, pre-approved build request from a server-side automation. Do not ask for clarification, confirmation, approval, or a design choice. Do not pause for user input. Make reasonable decisions, implement the enhancement completely, and report the result only after the work is done.

You may edit only the route-specific folder for `{page.route.lstrip('/')}` and the minimum routing code needed for that existing route. Do not modify, delete, move, rename, or restyle any other page, route, or route-specific folder. Preserve the existing route `{page.route}` and its public behavior. Keep its route-specific SEO metadata unique and accurate; update its title, meta description, Open Graph metadata, semantic headings, or alt text only when this enhancement changes them. Report exactly which files you changed."""


def _delete_page_prompt(page: LandingPage) -> str:
    return f"""You are modifying a shared multi-page landing-site project.

Delete only the landing page at `{page.route}`. Remove its route-specific folder named after the route segment `{page.route.lstrip('/')}` and unregister that one route according to the existing routing conventions.

This is an autonomous, pre-approved deletion request from a server-side automation. Do not ask for clarification, confirmation, or approval. Do not pause for user input. Complete only this deletion, then report the result.

Do not modify, delete, move, rename, or restyle any other page, route, route-specific folder, global styles, or configuration. Do not replace this page with a redirect or fallback page. Report exactly which files you removed or changed when finished."""


_DASHBOARD_HTML = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Landing Page Automations</title><style>body{font:16px system-ui;margin:0;background:#101624;color:#edf2ff}main{max-width:900px;margin:auto;padding:32px}section{background:#192237;padding:22px;border-radius:14px;margin:18px 0}input,textarea,button{font:inherit;padding:10px;border-radius:7px;border:1px solid #40506e;background:#0f1728;color:#fff}input,textarea{width:100%;box-sizing:border-box;margin:6px 0 14px}textarea{min-height:85px}button{background:#5472ff;cursor:pointer;margin-right:7px}button.secondary{background:#334155}.job{border-top:1px solid #40506e;padding:15px 0}.muted{color:#afbdd8;font-size:.9rem}label{font-weight:650}#notice{min-height:24px;color:#a9f5be}</style></head><body><main><h1>Landing Page Automations</h1><p class='muted'>Groq creates a page brief; Lovable builds and deploys it. This panel should remain local/private.</p><section><label>Control-panel API key</label><input id='key' type='password' placeholder='APP_API_KEY'><button onclick='saveKey()'>Unlock</button><span id='notice'></span></section><section><h2>New daily job</h2><form onsubmit='createJob(event)'><label>Name</label><input id='name' required placeholder='Daily startup landing page'><label>Cron (minute hour day month weekday)</label><input id='cron' required value='0 9 * * *'><label>Daily topic / prompt for Groq</label><textarea id='prompt' required placeholder='Create a landing page about today’s useful productivity idea...'></textarea><label>Reference sites (one URL per line)</label><textarea id='refs'></textarea><label>Image URLs (one per line)</label><textarea id='images'></textarea><label>URLs to include (one per line)</label><textarea id='links'></textarea><button>Create schedule</button></form></section><section><h2>Schedules</h2><div id='jobs'>Unlock the panel to load jobs.</div></section></main><script>const el=id=>document.getElementById(id),lines=id=>el(id).value.split('\\n').map(x=>x.trim()).filter(Boolean);let jobsById={};function headers(){return {'Authorization':'Bearer '+localStorage.apiKey,'Content-Type':'application/json'}}function saveKey(){localStorage.apiKey=el('key').value;load()}async function load(){try{let r=await fetch('/automation/jobs',{headers:headers()});if(!r.ok)throw Error((await r.json()).detail);let jobs=await r.json();jobsById=Object.fromEntries(jobs.map(j=>[j.id,j]));el('notice').textContent='Connected';el('jobs').innerHTML=jobs.length?jobs.map(j=>`<div class='job'><b>${j.name}</b> <span class='muted'>${j.cron} · ${j.enabled?'enabled':'disabled'}</span><p>${j.prompt}</p><p class='muted'>Last: ${j.last_status||'never'} ${j.last_detail||''}</p><button onclick='toggle("${j.id}")'>${j.enabled?'Disable':'Enable'}</button><button class='secondary' onclick='run("${j.id}")'>Run now</button><button class='secondary' onclick='removeJob("${j.id}")'>Delete</button></div>`).join(''):'No schedules yet.'}catch(e){el('jobs').textContent='Unable to load: '+e.message}}async function createJob(e){e.preventDefault();let body={name:el('name').value,cron:el('cron').value,prompt:el('prompt').value,reference_sites:lines('refs'),images:lines('images'),urls_to_include:lines('links'),enabled:true};let r=await fetch('/automation/jobs',{method:'POST',headers:headers(),body:JSON.stringify(body)});if(!r.ok)return alert((await r.json()).detail);e.target.reset();el('cron').value='0 9 * * *';load()}async function toggle(id){let j={...jobsById[id]};j.enabled=!j.enabled;delete j.id;delete j.last_run_at;delete j.last_status;delete j.last_detail;await fetch('/automation/jobs/'+id,{method:'PUT',headers:headers(),body:JSON.stringify(j)});load()}async function run(id){await fetch('/automation/jobs/'+id+'/run',{method:'POST',headers:headers()});load()}async function removeJob(id){if(confirm('Delete this schedule?')){await fetch('/automation/jobs/'+id,{method:'DELETE',headers:headers()});load()}}el('key').value=localStorage.apiKey||'';if(localStorage.apiKey)load()</script></body></html>"""
