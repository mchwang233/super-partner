from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


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


CLI_SPECS = {
    "cli:codex": CliSpec(
        id="cli:codex", executable="codex", auth_command=("login", "status"),
        invoke_args=("exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write", "--color", "never", "-"),
        fallback_model_id="openai:gpt-5.6-terra",
    ),
    "cli:claude": CliSpec(
        id="cli:claude", executable="claude", auth_command=("auth", "status"),
        invoke_args=("-p", "--output-format", "json", "--permission-mode", "acceptEdits", "--no-session-persistence"),
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
        try:
            version_result = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=3, check=False,
            )
            version = (version_result.stdout or version_result.stderr).strip().splitlines()[-1][:120]
            auth = subprocess.run(
                [executable, *spec.auth_command], capture_output=True, text=True, timeout=5, check=False,
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

    async def complete(self, model_id: str, prompt: str, cwd: Path, timeout: int = 900) -> str:
        spec = CLI_SPECS[model_id]
        executable = shutil.which(spec.executable)
        if not executable:
            raise RuntimeError(f"{spec.executable} is no longer installed")
        args = list(spec.invoke_args)
        if spec.id in {"cli:gemini", "cli:qwen"}:
            args[1] = prompt
            stdin = None
        else:
            stdin = prompt.encode()
        spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(
            executable, *args, cwd=str(cwd), stdin=asyncio.subprocess.PIPE if stdin else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # The CLI may already be running; finish spawning so its whole group can be killed.
            await _kill_process_group(await spawn)
            raise
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_process_group(process)
            raise RuntimeError(f"{spec.executable} timed out after {timeout}s")
        except asyncio.CancelledError:
            await _kill_process_group(process)
            raise
        if process.returncode:
            raise RuntimeError(f"{spec.executable} failed: {stderr.decode(errors='replace')[-500:]}")
        text = stdout.decode(errors="replace").strip()
        if spec.id in {"cli:claude", "cli:gemini", "cli:qwen"}:
            try:
                payload = json.loads(text)
                text = payload.get("result") or payload.get("response") or payload.get("text") or text
            except json.JSONDecodeError:
                pass
        return text
