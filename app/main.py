from functools import lru_cache
import logging

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse

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
    EnhanceProjectRequest,
    EnhanceProjectResponse,
    LandingPage,
)
from .oauth import LovableOAuth, LovableOAuthError

app = FastAPI(title="Lovable Landing Pages API", version="0.2.0")
logger = logging.getLogger(__name__)


def get_projects(settings: Settings = Depends(get_settings)) -> JsonProjectStore:
    return get_project_store(settings.projects_file)


@lru_cache
def get_lovable_oauth() -> LovableOAuth:
    return LovableOAuth(get_settings())


def get_lovable_client(settings: Settings = Depends(get_settings)) -> LovableMcpClient:
    return LovableMcpClient(settings.lovable_mcp_url, get_lovable_oauth())


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


@app.post("/projects", response_model=CreateProjectResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_project(
    payload: CreateProjectRequest,
    background_tasks: BackgroundTasks,
    projects: JsonProjectStore = Depends(get_projects),
    lovable: LovableMcpClient = Depends(get_lovable_client),
    settings: Settings = Depends(get_settings),
) -> CreateProjectResponse:
    lovable_project_id = _host_project_id(settings)
    project_id = payload.project_id or slugify(payload.idea)
    try:
        page = await projects.create(project_id, lovable_project_id, payload.idea)
    except ProjectExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail="This project_id already exists. Choose another project_id.",
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
        url=_page_url(settings, page.project_id),
    )


@app.post(
    "/projects/{project_id}/enhancements",
    response_model=EnhanceProjectResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enhance_project(
    project_id: str,
    payload: EnhanceProjectRequest,
    background_tasks: BackgroundTasks,
    projects: JsonProjectStore = Depends(get_projects),
    lovable: LovableMcpClient = Depends(get_lovable_client),
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
        await lovable.send_message(lovable_project_id, prompt)
        await lovable.deploy_project(lovable_project_id)
        logger.info("Lovable completed and published page '%s'.", project_id)
    except LovableMcpError:
        logger.exception("Lovable background build/publish failed for page '%s'.", project_id)
        if is_new_page:
            await projects.remove(project_id)


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


def _page_url(settings: Settings, project_id: str) -> str:
    if settings.lovable_base_url:
        return f"{settings.lovable_base_url.rstrip('/')}/{project_id}"
    return f"/{project_id}"


def _create_page_prompt(page: LandingPage, payload: CreateProjectRequest) -> str:
    return f"""You are modifying a shared multi-page landing-site project.

Create one new landing page for this idea: {payload.idea}

This page's public route MUST be `{page.route}`. First inspect the existing routing and follow its framework conventions. Keep this page's implementation isolated under a route-specific folder named `{page.project_id}` (or the closest framework-equivalent route folder). Register only the `{page.route}` route.

Do not change, move, delete, rename, restyle, or break any existing routes or their route-specific folders. Do not alter global configuration unless it is strictly required to register this one new route.

Design references: {payload.reference_sites}
Image URLs/assets to use when appropriate: {payload.images}
URLs that must appear somewhere on this page: {payload.urls_to_include}

Build a polished, responsive, accessible single landing page. Report the files and route you changed when finished."""


def _enhancement_prompt(page: LandingPage, enhancement: str) -> str:
    return f"""You are modifying a shared multi-page landing-site project.

Apply this enhancement only to the landing page at `{page.route}`: {enhancement}

You may edit only the route-specific folder for `{page.project_id}` and the minimum routing code needed for that existing route. Do not modify, delete, move, rename, or restyle any other page, route, or route-specific folder. Preserve the existing route `{page.route}` and its public behavior. Report exactly which files you changed."""
