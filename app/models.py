from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CreateProjectRequest(BaseModel):
    """The content needed to create one route in the shared Lovable project."""

    idea: str = Field(min_length=1, max_length=20_000)
    reference_sites: list[str] = Field(default_factory=list, max_length=20)
    images: list[str] = Field(default_factory=list, max_length=30)
    urls_to_include: list[str] = Field(default_factory=list, max_length=30)
    project_id: str | None = Field(
        default=None,
        description="Optional URL-safe page identifier. A slug is derived from the idea when omitted.",
    )

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        valid = value.replace("-", "").isalnum()
        if not value or len(value) > 80 or not valid or value != value.lower():
            raise ValueError("project_id must be a lowercase kebab-case identifier.")
        return value


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
