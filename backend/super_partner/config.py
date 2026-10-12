from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUPER_PARTNER_", env_file=".env")

    data_dir: Path = Path(".super-partner")
    database_path: Optional[Path] = None
    workspace_root: Path = Path("workspaces")
    # Existing project directories a task may target; CLI agents edit files there in place.
    project_roots: list[Path] = [Path.home()]
    allow_host_shell: bool = False
    default_budget_usd: float = 2.0
    max_parallel_nodes: int = 3
    api_base_url: str = "http://127.0.0.1:8000"
    # Host names the API answers to; any other Host header is refused to block DNS rebinding.
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]
    # Browser origins that may call the API; other origins cannot change state.
    allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    catalog_auto_sync: bool = True
    catalog_sync_interval_hours: float = Field(default=24, ge=1, le=720)
    artificial_analysis_api_key: Optional[SecretStr] = Field(default=None, repr=False)

    @property
    def db_path(self) -> Path:
        return self.database_path or self.data_dir / "super-partner.db"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


settings = Settings()
