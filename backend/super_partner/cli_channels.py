from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from . import calls, environment, progress
from .attachments import path
from .config import settings


@dataclass(frozen=True)
class CliSpec:
    id: str
    executable: str
    auth_command: tuple[str, ...]
    invoke_args: tuple[str, ...]
    fallback_model_id: str


@dataclass
class CliStatus:
    id: str
    installed: bool
    authenticated: bool
    auth_type: Optional[str] = None
    version: Optional[str] = None
    error: Optional[str] = None

    def public(self) -> dict:
        return asdict(self)


# Claude Code runs shell commands inside its own bubblewrap sandbox: writes only in the working
# directory (and /tmp), no network, and no way to ask for an unsandboxed command. Sandboxed
# commands need no approval, which a background run could never give; file edits are accepted.
CLAUDE_SANDBOX_SETTINGS = json.dumps({"sandbox": {
    "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
}})

# A task that allows the network: sandboxed commands may reach any domain except this machine,
# whose services (this application's API among them) stay closed.
CLAUDE_NETWORK_SETTINGS = json.dumps({"sandbox": {
    "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
    "network": {"allowedDomains": ["*"], "deniedDomains": ["localhost", "127.0.0.1", "::1"]},
}})
CODEX_NETWORK = ("-c", "sandbox_workspace_write.network_access=true")

# Repairing the environment: Claude Code without its sandbox and without asking for approval.
# The whole CLI runs under bubblewrap instead, which only blocks gaining root (see repair_command).
CLAUDE_REPAIR_SETTINGS = json.dumps({"sandbox": {"enabled": False}})
REPAIR_ARGS = {
    "cli:claude": (
        "-p", "--output-format", "json", "--permission-mode", "bypassPermissions",
        "--settings", CLAUDE_REPAIR_SETTINGS, "--no-session-persistence",
    ),
    "cli:codex": (
        "exec", "--ephemeral", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
        "--color", "never", "-",
    ),
}


def _failure_detail(stdout: bytes, stderr: bytes) -> str:
    """Why a CLI exited with an error. Claude Code in JSON mode reports errors such as a usage
    limit in the ``result`` of its stdout and leaves stderr empty."""
    detail = stderr.decode(errors="replace").strip()
    if not detail:
        text = stdout.decode(errors="replace").strip()
        try:
            payload = json.loads(text)
            text = str(payload.get("result") or payload.get("error") or text) if isinstance(payload, dict) else text
        except json.JSONDecodeError:
            pass
        detail = text or "no output"
    return detail[-500:]


def repair_command(executable: str, args: list[str]) -> list[str]:
    """Run a CLI on the host as the user, network included, but under no_new_privs (which
    bubblewrap always sets), so sudo and other setuid programs cannot raise privileges."""
    if not shutil.which("bwrap"):
        raise RuntimeError("repairing the environment needs bubblewrap (bwrap), which blocks sudo")
    # The real /dev: a repair checks the steps' sandbox, which needs /dev/net/tun for the internet.
    return ["bwrap", "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--die-with-parent", "--",
            executable, *args]


# The task's lead (supervisor.py) keeps one conversation per task. It reads files and runs
# sandboxed commands to look things up, but does not edit: the steps' models do the work.
SESSION_ARGS = {
    "cli:claude": ("-p", "--output-format", "json", "--permission-mode", "default",
                   "--settings", CLAUDE_SANDBOX_SETTINGS, "--disallowedTools", "Edit,Write,NotebookEdit"),
    "cli:codex": ("exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never"),
}
SESSION_CLIS = set(SESSION_ARGS)
# How a CLI says the conversation to continue is gone.
SESSION_MISSING = re.compile(r"no (conversation|session|rollout)|not found|does not exist", re.IGNORECASE)


class SessionLost(RuntimeError):
    """A conversation to continue no longer exists (the CLI cleaned it up, say)."""


class UsageText(str):
    """Keep the existing text API while carrying usage reported by the CLI."""
    def __new__(cls, text, usage=None):
        value = super().__new__(cls, text)
        value.usage = usage or {}
        return value


def cli_usage(payload, codex=False):
    data = payload.get("usage") or {}
    if not isinstance(data, dict) or "input_tokens" not in data or "output_tokens" not in data:
        return {}
    cached = data.get("cached_input_tokens", 0) if codex else data.get("cache_read_input_tokens", 0)
    written = data.get("cache_creation_input_tokens", 0)
    return {"input_tokens": data["input_tokens"] + (0 if codex else cached + written),
            "output_tokens": data["output_tokens"], "cached_input_tokens": cached, "cache_write_tokens": written}


def codex_output(output):
    text, usage = "", {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            text = event["item"].get("text") or text
        if event.get("type") == "turn.completed":
            for key, value in cli_usage(event, codex=True).items():
                usage[key] = usage.get(key, 0) + value
    return UsageText(text or output.strip(), usage)


CLI_SPECS = {
    "cli:codex": CliSpec(
        id="cli:codex", executable="codex", auth_command=("login", "status"),
        invoke_args=("exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write", "--color", "never", "-"),
        fallback_model_id="openai:gpt-5.6-terra",
    ),
    "cli:claude": CliSpec(
        id="cli:claude", executable="claude", auth_command=("auth", "status"),
        invoke_args=(
            "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
            "--settings", CLAUDE_SANDBOX_SETTINGS, "--no-session-persistence",
        ),
        fallback_model_id="anthropic:claude-sonnet-5",
    ),
    "cli:gemini": CliSpec(
        id="cli:gemini", executable="gemini", auth_command=("--version",),
        invoke_args=("-p", "", "--output-format", "json"),
        fallback_model_id="google:gemini-pro",
    ),
    "cli:qwen": CliSpec(
        id="cli:qwen", executable="qwen", auth_command=("--version",),
        invoke_args=("-p", "", "--output-format", "json"),
        fallback_model_id="qwen:qwen-plus",
    ),
}


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill the CLI and any tools it spawned; they share the session started for it."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


def _with_network(model_id: str, args: list[str]) -> list[str]:
    """Open the CLI's sandbox to the internet. Gemini and Qwen run unsandboxed already."""
    if model_id == "cli:claude":
        return [CLAUDE_NETWORK_SETTINGS if arg == CLAUDE_SANDBOX_SETTINGS else arg for arg in args]
    if model_id == "cli:codex":
        return [args[0], *CODEX_NETWORK, *args[1:]]
    return args


# Codex's own features the platform's leads and steps do not use (Codex feature flags).
CODEX_EXTRAS = ("plugins", "apps", "browser_use", "in_app_browser", "computer_use")
# Reasoning efforts as Codex names them; the platform's scale (providers.EFFORTS) maps onto it.
CODEX_EFFORTS = {"none": "minimal", "minimal": "minimal", "low": "low", "medium": "medium", "high": "high",
                 "xhigh": "xhigh", "max": "max", "ultra": "max"}
# A server's own table in Codex's config: [mcp_servers.name] or [mcp_servers."name"].
CODEX_SERVER = re.compile(r'^\s*\[\s*mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\s*\]\s*$', re.MULTILINE)


def codex_servers() -> list[str]:
    """The MCP servers the user's Codex config starts, by name, read from $CODEX_HOME/config.toml:
    the platform turns each off, since what they are differs from machine to machine. (Plugins'
    servers go with ``--disable plugins``.)"""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        text = (home / "config.toml").read_text(errors="replace")
    except OSError:
        return []
    return list(dict.fromkeys(quoted or bare for quoted, bare in CODEX_SERVER.findall(text)))


def codex_settings(effort: Optional[str] = None, service_tier: str = "") -> list[str]:
    """Options that make one Codex run the platform's rather than the user's: how hard it thinks,
    its service tier, and none of the user's plugins, apps and MCP servers."""
    options = []
    if effort:
        options += ["-c", f'model_reasoning_effort="{CODEX_EFFORTS.get(effort, effort)}"']
    if service_tier:
        options += ["-c", f'service_tier="{service_tier}"']
    if settings.codex_without_extras:
        options += [arg for name in CODEX_EXTRAS for arg in ("--disable", name)]
        # Codex splits a -c key on dots and takes quotes literally: only plain names can be addressed.
        options += [arg for name in codex_servers() if re.fullmatch(r"[A-Za-z0-9_-]+", name)
                    for arg in ("-c", f"mcp_servers.{name}.enabled=false")]
    return options


# Codex refuses rm -f and the like as dangerous even inside its sandbox (its exec policy), which
# stops builds and clean-ups; an allow rule in the folder it works in lifts that, and its sandbox
# still keeps everything outside the folder read-only. rm with wildcards or $(...) stays refused,
# since Codex cannot tell what it removes (2026-10-21); steps are told to use find -delete.
CODEX_RULES = ".codex/rules/super-partner.rules"
CODEX_ALLOW = 'prefix_rule(pattern=["rm"], decision="allow")\n'
_codex_rule_users: dict[Path, int] = {}


@contextlib.contextmanager
def codex_rules(cwd: Path):
    """The allow rule in ``cwd`` while Codex works there, then gone again (with any folder it made),
    so neither a task's results nor a project keep it. Steps at once in one folder share it."""
    rule = Path(cwd) / CODEX_RULES
    made = [folder for folder in (rule.parent.parent, rule.parent) if not folder.exists()]
    if not _codex_rule_users.get(rule):
        try:
            rule.parent.mkdir(parents=True, exist_ok=True)
            rule.write_text(CODEX_ALLOW)
        except OSError:
            made = []
    _codex_rule_users[rule] = _codex_rule_users.get(rule, 0) + 1
    try:
        yield
    finally:
        _codex_rule_users[rule] -= 1
        if not _codex_rule_users[rule]:
            del _codex_rule_users[rule]
            with contextlib.suppress(OSError):
                rule.unlink()
                for folder in reversed(made):
                    folder.rmdir()


def _with_attachments(model_id: str, args: list[str], attachments: list) -> list[str]:
    """Let the CLI open attached files, which live outside its working directory."""
    if not attachments:
        return args
    if model_id == "cli:codex":
        # Codex reads the whole disk in its sandbox; images also go in as real image inputs.
        # "--image=" keeps the value from swallowing the trailing "-" (prompt on stdin).
        images = [f"--image={path(item)}" for item in attachments if item.kind == "image"]
        return [args[0], *images, *args[1:]]
    if model_id == "cli:claude":
        return [*args, "--add-dir", *sorted({str(path(item).parent) for item in attachments})]
    return args


class CliChannelRegistry:
    def __init__(self) -> None:
        self._statuses: dict[str, CliStatus] = {}

    @staticmethod
    def tool_limit_capabilities(model_id: str) -> dict:
        # CLI-reported turns are not individual tool calls; we cannot intercept their tools.
        return {"counting_unit": None, "hard_limit": False}

    def refresh(self) -> dict[str, CliStatus]:
        self._statuses = {model_id: self._probe(spec) for model_id, spec in CLI_SPECS.items()}
        return self._statuses

    @property
    def statuses(self) -> dict[str, CliStatus]:
        return self._statuses or self.refresh()

    def available_model_ids(self) -> set[str]:
        return {model_id for model_id, status in self.statuses.items() if status.authenticated}

    def _probe(self, spec: CliSpec) -> CliStatus:
        executable = shutil.which(spec.executable)
        if not executable:
            return CliStatus(id=spec.id, installed=False, authenticated=False)
        # Generous limits: on a busy machine a Node CLI can take several seconds just to start.
        try:
            version_result = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=15, check=False,
            )
            version = (version_result.stdout or version_result.stderr).strip().splitlines()[-1][:120]
            auth = subprocess.run(
                [executable, *spec.auth_command], capture_output=True, text=True, timeout=15, check=False,
            )
            output = (auth.stdout + auth.stderr).lower()
            authenticated = auth.returncode == 0 and not any(
                marker in output for marker in ("not logged in", "unauthenticated", "login required")
            )
            auth_type = None
            if authenticated:
                if "chatgpt" in output or "claude.ai" in output or "subscription" in output:
                    auth_type = "membership"
                else:
                    auth_type = "authenticated"
            return CliStatus(
                id=spec.id, installed=True, authenticated=authenticated,
                auth_type=auth_type, version=version,
                error=None if authenticated else "CLI is installed but not authenticated",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CliStatus(id=spec.id, installed=True, authenticated=False, error=str(exc)[:200])

    @staticmethod
    async def _watched(spec, process, talk: asyncio.Future, cwd: Path, watch: progress.Watch, started: float):
        """Wait for the CLI, asking ``watch`` every progress_check_minutes whether it is stuck;
        ``started`` is when it was launched (epoch seconds)."""
        last = started
        while True:
            done, _ = await asyncio.wait({talk}, timeout=settings.progress_check_minutes * 60)
            if done:
                return talk.result()
            evidence = await progress.cli_evidence(Path(cwd), started, last, process.pid)
            last = time.time()
            if reason := await watch(evidence):
                raise progress.Stopped(f"{spec.executable} {progress.STOPPED}{reason}")

    async def _run(self, spec, command: list[str], cwd: Path, stdin: Optional[bytes], timeout: float,
                   watch: Optional[progress.Watch] = None) -> str:
        """Run a CLI command in its own process group and return its output; a failure raises
        RuntimeError, and the whole group is killed when the call ends early."""
        # Tools that environment repairs installed are on PATH for every CLI.
        env = environment.command_env(dict(os.environ))
        started = time.time()
        spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(
            *command, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE if stdin else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # The CLI may already be running; finish spawning so its whole group can be killed.
            await _kill_process_group(await spawn)
            raise
        talk = asyncio.ensure_future(process.communicate(stdin))
        try:
            if watch is None:
                stdout, stderr = await asyncio.wait_for(asyncio.shield(talk), timeout=timeout)
            else:
                stdout, stderr = await self._watched(spec, process, talk, cwd, watch, started)
        except asyncio.TimeoutError:
            await _kill_process_group(process)
            talk.cancel()
            raise RuntimeError(f"{spec.executable} timed out after {timeout}s")
        except (asyncio.CancelledError, progress.Stopped):
            await _kill_process_group(process)
            talk.cancel()
            raise
        if process.returncode:
            raise RuntimeError(f"{spec.executable} failed: {_failure_detail(stdout, stderr)}")
        return stdout.decode(errors="replace")

    async def converse(self, model_id: str, prompt: str, cwd: Path, session_id: Optional[str] = None,
                       timeout: float = 600, effort: Optional[str] = None) -> tuple[str, str]:
        """Say ``prompt`` in the conversation ``session_id`` (a new one without it), in ``cwd``;
        returns the reply and the conversation's id. Only SESSION_CLIS keep conversations."""
        spec = CLI_SPECS[model_id]
        executable = shutil.which(spec.executable)
        if not executable or model_id not in SESSION_ARGS:
            raise RuntimeError(f"{model_id} cannot keep a conversation here")
        if model_id == "cli:claude":
            sid = session_id or str(uuid4())
            args = [*SESSION_ARGS[model_id], "--resume" if session_id else "--session-id", sid]
            if effort:
                args += ["--effort", effort]
                if record := calls.row():
                    record["effective_effort"] = effort
        else:
            sid = session_id
            args = (["exec", "resume", session_id, "--json", "--skip-git-repo-check",
                     "-c", 'sandbox_mode="read-only"', "-"] if session_id else [*SESSION_ARGS[model_id], "-"])
            # Refinement can use the lead's per-part choice; ordinary lead calls keep their default.
            options = codex_settings(effort or settings.lead_reasoning_effort, settings.codex_lead_service_tier)
            # `exec resume` parses its own options. Overrides on the outer `exec` are
            # discarded on resume, restoring the user's effort/tier/plugins instead.
            position = 3 if session_id else 1
            args = [*args[:position], *options, *args[position:]]
            if record := calls.row():
                chosen = effort or settings.lead_reasoning_effort
                record["effective_effort"] = CODEX_EFFORTS.get(chosen, chosen)
        calls.save()
        try:
            output = await self._run(spec, [executable, *args], cwd, prompt.encode(), timeout)
        except RuntimeError as exc:
            if session_id and SESSION_MISSING.search(str(exc)):
                raise SessionLost(str(exc)) from exc
            raise
        if model_id == "cli:claude":
            payload = json.loads(output)
            return UsageText(str(payload.get("result") or ""), cli_usage(payload)), str(payload.get("session_id") or sid)
        text = ""
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                sid = event.get("thread_id") or sid
            elif event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                text = event["item"].get("text") or text
        if not sid:
            raise RuntimeError("codex did not report its conversation id")
        return codex_output(output), sid

    async def complete(
        self, model_id: str, prompt: str, cwd: Path, timeout: int = 900, attachments=(), repair: bool = False,
        network: bool = False, watch: Optional[progress.Watch] = None, effort: Optional[str] = None,
    ) -> str:
        """Run a CLI in ``cwd``. With ``repair`` it leaves its sandbox to fix the environment;
        with ``network`` its sandbox lets commands reach the internet. With ``watch`` it is not
        killed at ``timeout`` but checked every progress_check_minutes, and stopped only if stuck."""
        spec = CLI_SPECS[model_id]
        executable = shutil.which(spec.executable)
        if not executable:
            raise RuntimeError(f"{spec.executable} is no longer installed")
        args = list(REPAIR_ARGS.get(spec.id, spec.invoke_args) if repair else spec.invoke_args)
        if network and not repair:
            args = _with_network(spec.id, args)
        args = _with_attachments(spec.id, args, list(attachments))
        if spec.id == "cli:codex":
            args = [args[0], *codex_settings(effort), *args[1:]]
            if "--json" not in args:
                args.insert(1, "--json")
        if spec.id in {"cli:gemini", "cli:qwen"}:
            args[1] = prompt
            stdin = None
        else:
            stdin = prompt.encode()
        command = repair_command(executable, args) if repair else [executable, *args]
        with codex_rules(cwd) if spec.id == "cli:codex" and not repair else contextlib.nullcontext():
            stdout = await self._run(spec, command, cwd, stdin, timeout, watch)
        text = stdout.strip()
        if spec.id == "cli:codex":
            return codex_output(stdout)
        if spec.id in {"cli:claude", "cli:gemini", "cli:qwen"}:
            try:
                payload = json.loads(text)
                text = payload.get("result") or payload.get("response") or payload.get("text") or text
                return UsageText(text, cli_usage(payload))
            except json.JSONDecodeError:
                pass
        return text
