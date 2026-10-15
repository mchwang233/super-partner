"""Tools for API models, so they can act on a workspace like the membership CLIs do.

A node run by an API model becomes a tool-calling loop: the model reads and edits files in
its working directory and runs commands, the executor carries out each call and returns the
result, until the model answers without calling a tool. File tools never leave the working
directory; commands run under bubblewrap with the home folder hidden, no network, an empty
environment (no API keys), and write access only to the working directory. Ports listed in
``sandbox_host_ports`` are the one way out: each is bridged in through a Unix socket.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import shutil
import signal
import tempfile
from pathlib import Path

from . import environment
from .config import settings
from .secrets import SECRET_ENV

MAX_TOOL_OUTPUT = 20_000
MAX_READ_LINES = 400
MAX_LISTED = 500
COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 300
# Installing software while repairing the environment can take a while.
MAX_HOST_COMMAND_TIMEOUT = 900
# Where the bridged ports' sockets appear inside the sandbox.
SANDBOX_PORTS_DIR = "/tmp/.sp-ports"


class ToolError(Exception):
    pass


TOOLS = [
    {
        "name": "list_dir",
        "description": "List files and folders in a directory of the working directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory, relative to the working directory. Default '.'."},
        }},
    },
    {
        "name": "read_file",
        "description": "Read a text file, optionally a range of lines.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "First line to read, starting at 1."},
            "limit": {"type": "integer", "description": f"Number of lines, at most {MAX_READ_LINES}."},
        }, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content. Creates folders as needed.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
        }, "required": ["path", "content"]},
    },
    {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_text with new_text in a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
        }, "required": ["path", "old_text", "new_text"]},
    },
    {
        "name": "run_command",
        "description": (
            "Run a bash command in the working directory, in a sandbox without network access "
            "that can only write to the working directory. Returns exit code and output."
        ),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout_seconds": {"type": "integer", "description": f"Default {COMMAND_TIMEOUT}, at most {MAX_COMMAND_TIMEOUT}."},
        }, "required": ["command"]},
    },
]

AGENT_INSTRUCTIONS = (
    "You can act on the working directory with tools: list_dir, read_file, write_file, "
    "edit_file and run_command. Do the work with them rather than only describing it: create "
    "and change the files, and run checks such as syntax checks or tests when useful. Paths are "
    "relative to the working directory. Commands have no network access{ports}. When the work "
    "is done, reply without calling a tool and summarize what you did and what you verified."
)


def sandbox_available() -> bool:
    return shutil.which("bwrap") is not None


HOST_INSTRUCTIONS = (
    "You can act with tools: list_dir, read_file, write_file and edit_file work in the working "
    "directory; run_command runs bash on the host machine as the user, with network access but "
    "without root (sudo is blocked), in the working directory. When the work is done, reply "
    "without calling a tool."
)


def sandbox_ports() -> list[int]:
    """Host ports bridged into the sandbox; none without socat, which listens on them inside."""
    return environment.host_ports() if shutil.which("socat") else []


def agent_instructions() -> str:
    ports = sandbox_ports()
    note = f", except to local ports {', '.join(map(str, ports))} (host services such as a license server)" if ports else ""
    return AGENT_INSTRUCTIONS.format(ports=note)


def tools_for(sandboxed: bool) -> list[dict]:
    return [tool for tool in TOOLS if sandboxed or tool["name"] != "run_command"]


def _clip(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + f"\n[... {len(text) - MAX_TOOL_OUTPUT} more characters clipped]"


class Workspace:
    """Carries out tool calls inside one directory."""

    def __init__(self, root: Path, host: bool = False):
        self.root = root.resolve()
        # Host mode, for repairing the environment: commands leave the sandbox (network, any
        # folder the user can write) but still cannot gain root.
        self.host = host

    def instructions(self) -> str:
        return HOST_INSTRUCTIONS if self.host else agent_instructions()

    def _path(self, raw) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("path is required")
        path = (self.root / raw).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError(f"{raw} is outside the working directory")
        return path

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    async def call(self, name: str, args: dict) -> str:
        """Run one tool; failures come back as text so the model can correct itself."""
        try:
            if not isinstance(args, dict) or "__invalid__" in args:
                raise ToolError("arguments must be a JSON object")
            if name == "list_dir":
                return self.list_dir(args.get("path") or ".")
            if name == "read_file":
                return self.read_file(args.get("path"), args.get("offset"), args.get("limit"))
            if name == "write_file":
                return self.write_file(args.get("path"), args.get("content"))
            if name == "edit_file":
                return self.edit_file(args.get("path"), args.get("old_text"), args.get("new_text"))
            if name == "run_command":
                return await self.run_command(args.get("command"), args.get("timeout_seconds"))
            raise ToolError(f"unknown tool {name}")
        except ToolError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error: {exc.strerror or exc}"

    def list_dir(self, raw: str) -> str:
        path = self._path(raw)
        if not path.is_dir():
            raise ToolError(f"{raw} is not a directory")
        entries = sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name))
        lines = [f"{entry.name}/" if entry.is_dir() else f"{entry.name} ({entry.stat().st_size} bytes)" for entry in entries[:MAX_LISTED]]
        if len(entries) > MAX_LISTED:
            lines.append(f"... {len(entries) - MAX_LISTED} more")
        return "\n".join(lines) or "(empty)"

    def read_file(self, raw, offset, limit) -> str:
        path = self._path(raw)
        if not path.is_file():
            raise ToolError(f"{raw} is not a file")
        lines = path.read_text(errors="replace").splitlines()
        start = max(1, int(offset or 1))
        count = max(1, min(int(limit or MAX_READ_LINES), MAX_READ_LINES))
        chunk = lines[start - 1:start - 1 + count]
        note = f"\n[lines {start}-{start + len(chunk) - 1} of {len(lines)}]" if len(lines) > len(chunk) else ""
        return _clip("\n".join(chunk)) + note

    def write_file(self, raw, content) -> str:
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        path = self._path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"Wrote {self._relative(path)} ({len(content.encode())} bytes)"

    def edit_file(self, raw, old_text, new_text) -> str:
        if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
            raise ToolError("old_text and new_text must be strings and old_text not empty")
        path = self._path(raw)
        text = path.read_text()
        count = text.count(old_text)
        if count != 1:
            raise ToolError(f"old_text occurs {count} times in {raw}; it must occur exactly once")
        path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {self._relative(path)}"

    def sandbox_args(self, command: str, ports_dir: str | None = None, ports: tuple[int, ...] | list[int] = ()) -> list[str]:
        home = str(Path.home())
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
        env.update({name: os.environ[name] for name in settings.sandbox_env if name in os.environ})
        env = environment.command_env(env)
        env.update(HOME=str(self.root), LANG="C.UTF-8")
        bridge = ["--bind", ports_dir, SANDBOX_PORTS_DIR] if ports_dir else []
        tools = str(environment.tools_dir())
        shared = ["--ro-bind", tools, tools] if os.path.isdir(tools) else []
        return [
            "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", *bridge,
            # Hide the user's home (keys, tokens, other projects), then expose only this folder
            # and the tools the environment repairs installed.
            "--tmpfs", home, *shared, "--bind", str(self.root), str(self.root),
            "--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session",
            "--clearenv", *[arg for name, value in env.items() for arg in ("--setenv", name, value)],
            "--chdir", str(self.root), "--", "/bin/bash", "-c", _listen_script(ports) + 'exec /bin/bash -c "$1"',
            "bash", command,
        ]

    def host_args(self, command: str) -> list[str]:
        """The host as the user, network included, minus API keys; bubblewrap still sets
        no_new_privs, so sudo and other setuid programs cannot raise privileges."""
        env = {name: value for name, value in os.environ.items() if name not in SECRET_ENV.values()}
        return [
            "bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent", "--new-session",
            "--clearenv", *[arg for name, value in environment.command_env(env).items() for arg in ("--setenv", name, value)],
            "--chdir", str(self.root), "--", "/bin/bash", "-c", command,
        ]

    async def run_command(self, command, timeout) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command is required")
        if not sandbox_available():
            raise ToolError("commands are unavailable: bubblewrap is not installed")
        limit = MAX_HOST_COMMAND_TIMEOUT if self.host else MAX_COMMAND_TIMEOUT
        seconds = max(1, min(int(timeout or COMMAND_TIMEOUT), limit))
        ports = [] if self.host else sandbox_ports()
        async with port_bridge(ports) as ports_dir:
            process = await asyncio.create_subprocess_exec(
                *(self.host_args(command) if self.host else self.sandbox_args(command, ports_dir, ports)),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ToolError(f"command timed out after {seconds}s")
        return f"exit code {process.returncode}\n" + _clip(output.decode(errors="replace"))


def _listen_script(ports: list[int]) -> str:
    """Shell lines that listen on each bridged port inside the sandbox, then wait until ready."""
    if not ports:
        return ""
    lines = [
        f"socat TCP-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:{SANDBOX_PORTS_DIR}/{port}.sock"
        " </dev/null >/dev/null 2>&1 &" for port in ports
    ]
    # A listening socket shows in /proc/net/tcp as "<addr>:<hex port> 00000000:0000 0A".
    ready = " && ".join(f'grep -q ":{port:04X} 00000000:0000 0A" /proc/net/tcp' for port in ports)
    lines.append(f"for _ in $(seq 250); do {{ {ready}; }} && break; sleep 0.02; done")
    return "\n".join(lines) + "\n"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def _forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        writer.close()
        return
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


@contextlib.asynccontextmanager
async def port_bridge(ports: list[int]):
    """Serve each host port on a Unix socket in a fresh folder for the sandbox to mount.

    The forwarding runs in this process, so nothing outlives the command. Yields the folder,
    or None when there is nothing to bridge.
    """
    if not ports:
        yield None
        return
    # Under /tmp, not a possibly long TMPDIR: Unix socket paths are limited to about 107 bytes.
    folder = tempfile.mkdtemp(prefix="sp-ports-", dir="/tmp" if os.path.isdir("/tmp") else None)
    servers = []
    try:
        for port in ports:
            path = f"{folder}/{port}.sock"
            servers.append(await asyncio.start_unix_server(functools.partial(_forward, port=port), path=path))
            os.chmod(path, 0o600)
        yield folder
    finally:
        for server in servers:
            server.close()
        shutil.rmtree(folder, ignore_errors=True)
