from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUPER_PARTNER_", env_file=".env")

    data_dir: Path = Path(".super-partner")
    database_path: Optional[Path] = None
    workspace_root: Path = Path("workspaces")
    allow_host_shell: bool = False
    default_budget_usd: float = 2.0
    max_parallel_nodes: int = 3
    api_base_url: str = "http://127.0.0.1:8000"

    @property
    def db_path(self) -> Path:
        return self.database_path or self.data_dir / "super-partner.db"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


settings = Settings()
