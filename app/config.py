from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or `.env`."""

    lovable_mcp_url: str = "https://mcp.lovable.dev"
    # Leave blank for MCP OAuth dynamic client registration (recommended for local Free accounts).
    lovable_oauth_client_id: str | None = None
    lovable_oauth_client_secret: str | None = None
    lovable_oauth_redirect_uri: str = "http://127.0.0.1:8000/auth/lovable/callback"
    lovable_oauth_scopes: str = "offline projects:read projects:write"
    oauth_store_file: Path = Path("data/lovable_oauth.json")
    # This is the only real Lovable project the API is allowed to modify.
    lovable_project_id: str | None = None
    lovable_base_url: str | None = None
    projects_file: Path = Path("data/projects.json")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
