from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

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
    # API models work on files and run sandboxed commands through tools (agent.py); off, they
    # only return text and cannot be routed to nodes that change a project.
    api_agent: bool = True
    # Turns of tool calls per attempt of an API model; 0 means no limit. Long attempts are not cut
    # off but checked (progress_check_minutes).
    api_agent_max_turns: int = Field(default=0, ge=0, le=1000)
    # Minutes of an attempt after which, and then every so often, another model checks whether it
    # is still making progress; only a stuck attempt is stopped. CLIs are checked the same way
    # instead of being killed at a fixed timeout.
    progress_check_minutes: float = Field(default=10, gt=0)
    # A failed attempt goes to the task's lead to decide what next (supervisor.py), instead of a
    # new diagnosis each time. Past these many decisions for one step, or for the whole task, the
    # task pauses for the user.
    lead_decisions: bool = True
    max_node_decisions: int = Field(default=5, ge=1, le=50)
    max_run_decisions: int = Field(default=25, ge=1, le=500)
    # Output tokens per request for a model whose own ceiling the catalog does not give; a known
    # ceiling is used as is. Reasoning models spend their thinking from this too, so a small cap
    # can end a turn before it writes anything. A model that rejects the cap is retried with a
    # smaller one; the task budget can lower it further.
    api_agent_max_output_tokens: int = Field(default=32768, ge=1024, le=262144)
    # How hard DeepSeek's reasoning models think (its `reasoning_effort`); "none" turns thinking
    # off. Higher is slower and spends more output tokens, and gets harder problems right.
    # A project brief (briefing.py) written by a cheap model for the lead to plan from.
    project_brief: bool = True
    # Models used only for helper work (the project brief, reviews, progress checks), never to
    # run a task's steps or lead it: glm-5.3-flash leaves tool-calling requests unanswered.
    helper_only_models: list[str] = Field(default_factory=lambda: ["glm:glm-5.3-flash"])
    deepseek_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max"] = "max"
    # Seconds to wait for one API response; long outputs take minutes to generate.
    api_request_timeout: float = Field(default=600, ge=30)
    # A response not complete after this many seconds, even if data keeps arriving, fails at
    # once without a retry: its model counts as unavailable for a while, and another takes over.
    api_reply_deadline: float = Field(default=300, ge=30)
    # Local TCP ports that API models' sandboxed commands may still reach, such as an EDA
    # license server: lmgrd's port plus its vendor daemon's, which must be pinned in the license
    # file (VENDOR snpslmd port=...). All other network access stays blocked. Needs socat.
    sandbox_host_ports: list[int] = []
    # Host environment variables copied into that sandbox, e.g. VCS_HOME or
    # SNPSLMD_LICENSE_FILE. Listing PATH replaces the sandbox's minimal PATH with the host's.
    sandbox_env: list[str] = []
    # Subtasks (and their reviews) go to API models; a membership CLI only takes a subtask no
    # API model can do. Planning may still use a CLI.
    subtasks_prefer_api: bool = True
    # Handle a failed attempt instead of only retrying it: the task's lead decides (lead_decisions),
    # or, without a lead able to, a diagnosis looks for what blocks it.
    auto_diagnose: bool = True
    # Diagnose and repair this application's source, separately from task workspaces.
    platform_self_repair: bool = True
    platform_root: Optional[Path] = None
    platform_repair_attempts: int = Field(default=2, ge=1, le=3)
    # Spending cap of a platform engineer job the user starts (engineer.py); membership CLIs cost
    # nothing extra. Repairs started from a task are charged to that task instead.
    engineer_budget_usd: float = Field(default=2.0, ge=0)
    # Commit an applied platform change to the source checkout's current branch (never pushed).
    engineer_commit: bool = True
    # Have another model check each node's result against its acceptance criteria.
    review_nodes: bool = True
    # Without a lead deciding (the fallback): runs per node (first try plus retries) before it
    # fails, retries going to the strongest model, and times verification nodes may send work back
    # within one task. With a lead, max_node_decisions and max_run_decisions bound it instead.
    max_node_attempts: int = Field(default=3, ge=1, le=10)
    max_rework_rounds: int = Field(default=2, ge=0, le=10)
    # Upstream results longer than this reach downstream nodes as a summary.
    handoff_max_chars: int = Field(default=4000, ge=500)
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
