from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import environment, progress
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
    return ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent", "--", executable, *args]


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

    async def complete(
        self, model_id: str, prompt: str, cwd: Path, timeout: int = 900, attachments=(), repair: bool = False,
        network: bool = False, watch: Optional[progress.Watch] = None,
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
        if spec.id in {"cli:gemini", "cli:qwen"}:
            args[1] = prompt
            stdin = None
        else:
            stdin = prompt.encode()
        command = repair_command(executable, args) if repair else [executable, *args]
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
        text = stdout.decode(errors="replace").strip()
        if spec.id in {"cli:claude", "cli:gemini", "cli:qwen"}:
            try:
                payload = json.loads(text)
                text = payload.get("result") or payload.get("response") or payload.get("text") or text
            except json.JSONDecodeError:
                pass
        return text
