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
    # Steps of one task running at once; 0: no limit, every step whose inputs are ready starts
    # (steps changing the same files still take turns, scopes.py).
    max_parallel_nodes: int = Field(default=0, ge=0)
    # A hard step takes the strongest model, unless a cheaper one scores within this many points:
    # a point or two is not worth a model many times slower and dearer (GLM 5.3 against DeepSeek
    # Flash, 2026-10-21: 93.9 against 92.3 for coding, about 8 times slower and 20 times dearer).
    hard_score_margin: float = Field(default=2.0, ge=0)
    # A step others wait for is put to the lead once it has run this many times its expected
    # minutes (its expected turns at minutes_per_turn): go on, or stop for a faster model.
    minutes_per_turn: float = Field(default=0.5, gt=0)
    critical_overrun_factor: float = Field(default=2.0, gt=0)
    # Before a task's first step, check the step sandbox, the tools and licenses the plan needs,
    # the internet (if allowed) and the models (preflight.py).
    preflight: bool = True
    preflight_command_timeout: float = Field(default=120, gt=0)
    # Before the first step, write this machine's skill for each tool the task needs that has none
    # yet, by trying it in a step's sandbox (preflight.py, skills.py); at most this long each.
    preflight_skills: bool = True
    # Optional post-task learning; the UI can persist overrides. Its budget is separate and shown.
    learning_enabled: bool = False
    learning_budget_usd: float = Field(default=0.5, ge=0, le=100)
    learning_auto_platform: bool = True
    skill_preparation_timeout: float = Field(default=900, gt=0)
    # A public address that shows the internet can be reached from a step's sandbox.
    preflight_network_url: str = "https://pypi.org/simple/"
    # Steps of one task running on one API provider at once; a step past it goes to another
    # provider's model if one can do it for at most spread_cost_factor times the cost. Off (0) by
    # default: steps one after another on one provider are fine (a failing one switches model
    # anyway), and spreading would hand everyday steps to weaker models.
    max_steps_per_provider: int = Field(default=0, ge=0)
    spread_cost_factor: float = Field(default=3.0, ge=1.0)
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
    # A step's reasoning effort by its difficulty: thinking hard on every turn of an easy step only
    # makes it slower. Calls outside steps (reviews, summaries) use deepseek_reasoning_effort.
    reasoning_by_difficulty: dict[str, str] = Field(default_factory=lambda: {"easy": "low", "normal": "medium", "hard": "max"})
    # The task's lead plans and decides for the whole task: it thinks hardest. Diagnoses a step below.
    lead_reasoning_effort: str = "max"
    diagnosis_reasoning_effort: str = "high"
    # Codex's service tier for the lead ("fast", "flex", ...); empty keeps the user's Codex config.
    codex_lead_service_tier: str = ""
    # Codex as the platform runs it leaves out the user's plugins, apps and MCP servers, which its
    # leads and steps do not use and which slow every start.
    codex_without_extras: bool = True
    # Where the leads' conversations keep their folders (a CLI conversation is tied to one); outside
    # any project, so a CLI does not take a project's instructions (AGENTS.md, CLAUDE.md) for its own.
    # Empty: <XDG cache>/super-partner/leads.
    lead_root: Optional[Path] = None
    # Seconds to wait for one API response; long outputs take minutes to generate.
    api_request_timeout: float = Field(default=600, ge=30)
    # A response not complete after this many seconds, even if data keeps arriving, fails at
    # once without a retry: its model counts as unavailable for a while, and another takes over.
    api_reply_deadline: float = Field(default=300, ge=30)
    # Local TCP ports that API models' sandboxed commands may still reach, such as an EDA
    # license server's lmgrd port; its vendor daemons' ports (snpslmd, ...) are found and added at
    # each command (environment.license_daemon_ports; for a root lmgrd by tracing lmutil lmstat,
    # needs strace). All other network access stays blocked.
    # Needs socat.
    sandbox_host_ports: list[int] = []
    # Shell scripts every step command sources first, e.g. an EDA tool setup that puts vcs on PATH
    # and sets license variables. A machine's own scripts can also be recorded in
    # .super-partner/tools/sandbox.json as "profiles" (environment.profiles).
    sandbox_profiles: list[str] = []
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
