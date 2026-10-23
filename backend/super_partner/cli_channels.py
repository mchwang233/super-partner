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

from . import activity, calls, environment, progress
from .attachments import path
from .config import settings
from .contracts import ArtifactBlocked

# Bound the full UTF-8 input conservatively, leaving room for CLI/protocol wrapping.
CODEX_INPUT_LIMIT = 1048576
CODEX_INPUT_RESERVE = 65536


class InputTooLarge(ArtifactBlocked):
    """Required context cannot fit: block for replanning, not model/availability fallback."""


def input_too_large(error):
    return "input_too_large" in str(error).lower()


def codex_input(prompt, *, retry=False):
    from .team import compact_context

    limit = CODEX_INPUT_LIMIT - CODEX_INPUT_RESERVE
    before = prompt
    if retry or len(prompt.encode()) > limit:
        prompt = compact_context(prompt, budget=1024 if retry else 32768)
    calls.warning(f"Codex input: chars {len(before)} -> {len(prompt)}, "
                  f"UTF-8 bytes {len(before.encode())} -> {len(prompt.encode())}; "
                  f"limit {CODEX_INPUT_LIMIT}, reserve {CODEX_INPUT_RESERVE}, retry={retry}")
    calls.save()
    if len(prompt.encode()) > limit or (retry and prompt == before):
        raise InputTooLarge("input_too_large: 必须保留的要求、验收标准或未决事项无法安全缩减；"
                            "请负责人拆分上下文。禁止原样重试或切换模型绕过。")
    return prompt


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
    # Its sandbox: the lead's permission profile (codex_step_permissions with lead), added per call.
    "cli:codex": ("exec", "--json", "--skip-git-repo-check", "--color", "never"),
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
    """Open the CLI's sandbox to the internet. Gemini and Qwen run unsandboxed already; Codex
    steps get it from their permission profile (codex_step_permissions)."""
    if model_id == "cli:claude":
        return [CLAUDE_NETWORK_SETTINGS if arg == CLAUDE_SANDBOX_SETTINGS else arg for arg in args]
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


# The permission profile a Codex step runs under (Codex's own sandbox, bubblewrap on Linux): its
# workspace writable; this account's home (keys, Codex's login, other projects), the platform's
# data and the other tasks' folders unreadable; the machine's shared /tmp replaced by TMPDIR.
CODEX_STEP_PROFILE = "super-partner-step"
# Folders denied entry by entry: Codex lifts a deny only on a whole entry, not inside one.
MAX_DENIED = 400


def _deny_except(base: Path, keep: list[Path], rules: dict[str, str]) -> None:
    """Deny every entry of ``base`` that is not, or does not lead to, one of ``keep``."""
    keep = [path for path in keep if path == base or base in path.parents]
    if not keep:
        rules[str(base)] = "deny"
        return
    if base in keep or not base.is_dir() or len(rules) > MAX_DENIED:
        return
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue  # what it points to is denied or kept where it is
        _deny_except(entry, keep, rules)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_package() -> Optional[Path]:
    """The folder Codex is installed in (its sandbox helper runs from there)."""
    found = shutil.which(CLI_SPECS["cli:codex"].executable)
    if not found:
        return None
    real = Path(found).resolve()
    return real.parent.parent if real.parent.name == "bin" else real.parent


CODEX_LEAD_PROFILE = "super-partner-lead"


# /tmp entries a step's profile denies one by one; past this many it denies all of /tmp.
MAX_TMP_ENTRIES = 2000


def _shared_tmp(cwd: Path, rules: dict[str, str], shared: Path = Path("/tmp")) -> None:
    """/tmp stays writable, since some tools write there whatever TMPDIR says (URG creates its
    temporary files in /tmp and crashes when it cannot), but what is already in it stays
    unreadable: it is denied entry by entry. A step's own temporary folder is elsewhere
    (private_tmp)."""
    shared = shared.resolve()
    if shared in cwd.parents:
        _deny_except(shared, [cwd], rules)
        return
    try:
        entries = [entry for entry in shared.iterdir() if not entry.is_symlink()]
    except OSError:
        entries = None
    if entries is None or len(entries) > MAX_TMP_ENTRIES:
        rules[":slash_tmp"] = "deny"
        return
    rules[str(shared)] = "write"
    for entry in sorted(entries):
        rules[str(entry)] = "deny"


def codex_step_permissions(cwd: Path, network: bool, lead: bool = False, tmpdir: Optional[str] = None) -> list[str]:
    """``-c`` options defining and selecting the profile for a step working in ``cwd`` with its
    own ``tmpdir`` (CODEX_STEP_PROFILE), or with ``lead`` for a lead's read-only conversation
    there (CODEX_LEAD_PROFILE: the tasks' folders stay readable, so it can look at their results)."""
    cwd = cwd.resolve()
    keep = [cwd, codex_home().resolve() / "tmp", environment.tools_dir().resolve(),
            *([Path(tmpdir).resolve()] if tmpdir and not lead else [])]
    if package := codex_package():
        keep.append(package)
    rules: dict[str, str] = {}
    bases = (Path.home(), settings.data_dir) if lead else (Path.home(), settings.data_dir, settings.workspace_root)
    for base in dict.fromkeys(Path(p).resolve() for p in bases):
        if base != Path("/") and base.is_dir():
            _deny_except(base, keep, rules)
    if lead:
        rules[str(cwd)] = "read"
    else:
        _shared_tmp(cwd, rules)
        rules.update({":tmpdir": "write", str(cwd): "write"})
    table = "{ " + ", ".join(f"{json.dumps(path)} = {json.dumps(mode)}" for path, mode in rules.items()) + " }"
    name = CODEX_LEAD_PROFILE if lead else CODEX_STEP_PROFILE
    return ["-c", f'default_permissions="{name}"',
            "-c", f'permissions.{name}.extends="{":read-only" if lead else ":workspace"}"',
            "-c", f"permissions.{name}.filesystem={table}",
            *(["-c", f"permissions.{name}.network={{ enabled = true }}"] if network else [])]


@contextlib.contextmanager
def private_tmp(wanted: bool = True):
    """A temporary folder of a Codex step's own, its TMPDIR, removed afterwards (None unless ``wanted``)."""
    if not wanted:
        yield None
        return
    import tempfile
    # Under the data folder, which every step's profile hides: steps do not see each other's.
    parent = Path(settings.data_dir).resolve() / "tmp"
    parent.mkdir(parents=True, exist_ok=True)
    folder = tempfile.mkdtemp(prefix="sp-codex-tmp-", dir=str(parent))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


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
    async def _watched(spec, process, talk: asyncio.Future, cwd: Path, watch: progress.Watch, started: float,
                       printed: list[int]):
        """Wait for the CLI, asking ``watch`` every progress_check_minutes whether it is stuck;
        ``started`` is when it was launched (epoch seconds), ``printed`` how much output it has
        produced so far."""
        last, seen = started, 0
        tracker = progress.CliProgress(Path(cwd), started, process.pid)
        while True:
            done, _ = await asyncio.wait({talk}, timeout=settings.progress_check_minutes * 60)
            if done:
                return talk.result()
            evidence = await tracker.evidence(last, log=activity.current.get(), printed=printed[0] - seen)
            seen, last = printed[0], time.time()
            if reason := await watch(evidence):
                raise progress.Stopped(f"{spec.executable} {progress.STOPPED}{reason}")

    @staticmethod
    async def _streamed(process, stdin: Optional[bytes], last: list, printed: Optional[list] = None) -> tuple[bytes, bytes]:
        """``process.communicate``, noting in ``last[0]`` when it last printed anything, in
        ``printed[0]`` how much it has printed, and passing each line to the running step's
        activity (activity.note_line)."""
        async def drain(stream, sink, lines=False):
            pending = b""
            while chunk := await stream.read(65536):
                sink.append(chunk)
                last[0] = time.monotonic()
                if printed is not None:
                    printed[0] += len(chunk)
                if lines:
                    *done, pending = (pending + chunk).split(b"\n")
                    for line in done:
                        activity.note_line(line)
            if lines and pending:
                activity.note_line(pending)

        if stdin is not None and process.stdin:
            process.stdin.write(stdin)
            with contextlib.suppress(ConnectionError):
                await process.stdin.drain()
            process.stdin.close()
        out, err = [], []
        await asyncio.gather(drain(process.stdout, out, activity.current.get() is not None), drain(process.stderr, err))
        await process.wait()
        return b"".join(out), b"".join(err)

    @staticmethod
    async def _until_quiet(spec, talk: asyncio.Future, last: list, idle: float, timeout: float):
        """Wait for the CLI while it keeps printing: stopped after ``idle`` seconds without any
        output, or ``timeout`` seconds in all."""
        started = time.monotonic()
        while True:
            done, _ = await asyncio.wait({talk}, timeout=min(30, idle))
            if done:
                return talk.result()
            now = time.monotonic()
            if now - last[0] > idle:
                raise asyncio.TimeoutError(f"no output for {idle:g}s")
            if now - started > timeout:
                raise asyncio.TimeoutError(f"{timeout:g}s")

    async def _run_input(self, spec, command, cwd, prompt, timeout, watch=None, **kwargs):
        """Check the fully assembled input and retry only an explicitly rejected, smaller input."""
        if watch is not None:
            kwargs["watch"] = watch
        if spec.id != "cli:codex":
            return await self._run(spec, command, cwd, prompt.encode(), timeout, **kwargs)
        prompt = codex_input(prompt)
        for attempt in range(2):
            try:
                output = await self._run(spec, command, cwd, prompt.encode(), timeout, **kwargs)
                # Some CLI versions emit a failed turn with a zero exit status.
                for line in output.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(event, dict) and event.get("type") in {"turn.failed", "error"}
                            and input_too_large(event)):
                        raise InputTooLarge("input_too_large: Codex rejected the input")
                return output
            except RuntimeError as exc:
                if not input_too_large(exc):
                    raise
                if attempt:
                    raise InputTooLarge("input_too_large: 缩减后仍被拒绝，停止重试，交回负责人拆分上下文。") from exc
                prompt = codex_input(prompt, retry=True)

    async def _run(self, spec, command: list[str], cwd: Path, stdin: Optional[bytes], timeout: float,
                   watch: Optional[progress.Watch] = None, idle: Optional[float] = None,
                   extra_env: Optional[dict] = None) -> str:
        """Run a CLI command in its own process group and return its output; a failure raises
        RuntimeError, and the whole group is killed when the call ends early. With ``idle`` it
        runs while it keeps printing (up to ``timeout``), and is stopped after ``idle`` seconds
        without output."""
        # Tools that environment repairs installed are on PATH for every CLI.
        env = environment.command_env(dict(os.environ))
        env.update(extra_env or {})
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
        last, printed = [time.monotonic()], [0]
        # A watched CLI streams too: the checker is told how much output it printed since the
        # last check, even when the call is not the step's own activity.
        streamed = idle or watch is not None or activity.current.get() is not None
        talk = asyncio.ensure_future(self._streamed(process, stdin, last, printed)
                                     if streamed else process.communicate(stdin))
        try:
            if watch is not None:
                stdout, stderr = await self._watched(spec, process, talk, cwd, watch, started, printed)
            elif idle:
                stdout, stderr = await self._until_quiet(spec, talk, last, idle, timeout)
            else:
                stdout, stderr = await asyncio.wait_for(asyncio.shield(talk), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await _kill_process_group(process)
            talk.cancel()
            raise RuntimeError(f"{spec.executable} timed out after {str(exc) or f'{timeout}s'}") from None
        except (asyncio.CancelledError, progress.Stopped):
            await _kill_process_group(process)
            talk.cancel()
            raise
        if process.returncode:
            raise RuntimeError(f"{spec.executable} failed: {_failure_detail(stdout, stderr)}")
        return stdout.decode(errors="replace")

    async def sandbox_run(self, model_id: str, command: str, cwd: Path, timeout: float,
                          network: bool = False) -> tuple[str, bool]:
        """Run ``command`` in ``model_id``'s own sandbox exactly as its steps get it (the step's
        permission profile, codex_step_permissions; a TMPDIR of its own; the environment the
        platform gives every CLI), without the model: its output, and whether it exited 0. Only
        Codex can (``codex sandbox``); RuntimeError for any other CLI."""
        if model_id != "cli:codex" or not (executable := shutil.which(CLI_SPECS[model_id].executable)):
            raise RuntimeError(f"{model_id} cannot run a command in its sandbox without the model")
        with private_tmp() as tmp:
            process = await asyncio.create_subprocess_exec(
                executable, "sandbox", "-P", CODEX_STEP_PROFILE, "-C", str(cwd),
                *codex_step_permissions(cwd, network, tmpdir=tmp),
                "--", "/bin/bash", "-c", command, cwd=str(cwd),
                env={**environment.command_env(dict(os.environ)), "TMPDIR": tmp}, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout)
            except asyncio.TimeoutError:
                await _kill_process_group(process)
                return f"Command timed out after {timeout:g}s", False
            except asyncio.CancelledError:
                await _kill_process_group(process)
                raise
        text = output.decode(errors="replace").strip() or "(the command printed no output)"
        return (text if not process.returncode else f"Exit code {process.returncode}\n{text}"), not process.returncode

    async def converse(self, model_id: str, prompt: str, cwd: Path, session_id: Optional[str] = None,
                       timeout: Optional[float] = None, effort: Optional[str] = None) -> tuple[str, str]:
        """Say ``prompt`` in the conversation ``session_id`` (a new one without it), in ``cwd``;
        returns the reply and the conversation's id. Only SESSION_CLIS keep conversations.

        Without ``timeout``, Codex (which prints its work as it goes) runs while it keeps printing,
        stopped after lead_idle_minutes without output, at most lead_call_max_minutes; Claude Code
        (which answers at the end) gets lead_call_max_minutes. With it, ``timeout`` is a hard limit."""
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
            args = (["exec", "resume", session_id, "--json", "--skip-git-repo-check", "-"]
                    if session_id else [*SESSION_ARGS[model_id], "-"])
            # Refinement can use the lead's per-part choice; ordinary lead calls keep their default.
            options = [*codex_settings(effort or settings.lead_reasoning_effort, settings.codex_lead_service_tier),
                       *codex_step_permissions(cwd, network=False, lead=True)]
            # `exec resume` parses its own options. Overrides on the outer `exec` are
            # discarded on resume, restoring the user's effort/tier/plugins instead.
            position = 3 if session_id else 1
            args = [*args[:position], *options, *args[position:]]
            if record := calls.row():
                chosen = effort or settings.lead_reasoning_effort
                record["effective_effort"] = CODEX_EFFORTS.get(chosen, chosen)
        calls.save()
        try:
            idle = settings.lead_idle_minutes * 60 if timeout is None and model_id == "cli:codex" else None
            output = await self._run_input(spec, [executable, *args], cwd, prompt,
                                     timeout if timeout is not None else settings.lead_call_max_minutes * 60,
                                     idle=idle)
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
        step = spec.id == "cli:codex" and not repair
        with private_tmp(step) as tmp:
            return await self._complete(spec, executable, args, step, tmp, prompt, cwd, timeout, attachments,
                                        repair, network, watch, effort)

    async def _complete(self, spec, executable, args, step, tmp, prompt, cwd, timeout, attachments, repair,
                        network, watch, effort):
        if step:
            # Codex's own sandbox with the step's permission profile instead of workspace-write.
            at = args.index("--sandbox")
            args = [*args[:at], *args[at + 2:]]
            args = [args[0], *codex_step_permissions(cwd, network, tmpdir=tmp), *args[1:]]
        elif network and not repair:
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
        with codex_rules(cwd) if step else contextlib.nullcontext():
            if spec.id == "cli:codex":
                stdout = await self._run_input(spec, command, cwd, prompt, timeout, watch,
                                              extra_env={"TMPDIR": tmp} if tmp else None)
            else:
                stdout = await self._run(spec, command, cwd, stdin, timeout, watch,
                                         extra_env={"TMPDIR": tmp} if tmp else None)
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
