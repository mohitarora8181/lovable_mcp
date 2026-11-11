from datetime import datetime

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    """The content needed to create one route in the shared Lovable project."""

    idea: str = Field(min_length=1, max_length=20_000)
    reference_sites: list[str] = Field(default_factory=list, max_length=20)
    images: list[str] = Field(default_factory=list, max_length=30)
    urls_to_include: list[str] = Field(default_factory=list, max_length=30)
    project_id: str | None = Field(
        default=None,
        description="Optional page identifier stored as provided; the public route is derived from it.",
    )


class EnhanceProjectRequest(BaseModel):
    enhancement: str = Field(min_length=1, max_length=20_000)


class LandingPage(BaseModel):
    project_id: str
    lovable_project_id: str
    route: str
    created_at: datetime
    idea: str


class CreateProjectResponse(LandingPage):
    url: str
    status: str = "queued"
    message: str = "Build queued. Lovable will build and publish this page in the background."


class EnhanceProjectResponse(BaseModel):
    project_id: str
    route: str
    status: str = "queued"
    message: str = "Enhancement queued. Lovable will apply it and publish the site in the background."


class DeleteProjectResponse(BaseModel):
    project_id: str
    route: str
    status: str = "queued"
    message: str = "Deletion queued. Lovable will remove this page and publish the site in the background."


class ScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    cron: str = Field(description="Five-part cron: minute hour day-of-month month day-of-week")
    prompt: str = Field(min_length=1, max_length=20_000)
    reference_sites: list[str] = Field(default_factory=list, max_length=20)
    images: list[str] = Field(default_factory=list, max_length=30)
    urls_to_include: list[str] = Field(default_factory=list, max_length=30)
    enabled: bool = True


class ScheduleResponse(ScheduleRequest):
    id: str
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_detail: str | None = None


class AutomationLog(BaseModel):
    timestamp: datetime
    level: str
    message: str
    schedule_id: str | None = None


class WorkflowNode(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    type: str = Field(pattern="^(start|external_api|transform|create_page|stop)$")
    label: str = Field(min_length=1, max_length=100)
    x: int = 0
    y: int = 0
    input_schema: dict = Field(default_factory=dict, description="JSON Schema-like input contract")
    output_schema: dict = Field(default_factory=dict, description="JSON Schema-like output contract")
    input_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Target field to a context selector, e.g. {'idea': '$.previous.title'}",
    )
    output_selector: str | None = Field(
        default=None,
        description="JMESPath selector applied to an external API JSON response",
    )
    transform_script: str | None = Field(
        default=None,
        description="JMESPath transformation expression; its result becomes the node output",
    )
    script: str | None = Field(
        default=None,
        description="Transform-node script source. Execution mode is configured per node.",
    )
    config: dict = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    source: str
    target: str


class WorkflowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    cron: str | None = Field(default=None, description="Optional five-part cron expression")
    enabled: bool = True
    nodes: list[WorkflowNode] = Field(min_length=2, max_length=30)
    edges: list[WorkflowEdge] = Field(max_length=40)


class WorkflowResponse(WorkflowRequest):
    id: str
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_detail: str | None = None
