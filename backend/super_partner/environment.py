"""Fixes to the machine that tasks run on, shared by every sandbox.

An automatic diagnosis may repair the environment a task needs (see diagnosis.py): it installs
software into the tools folder and records environment variables and host ports in its
``sandbox.json``. Every task command then sees them: API models' sandboxed commands get the
folder mounted read-only, and every command, CLI ones included, gets ``bin`` on PATH,
``python`` on PYTHONPATH and the recorded variables.

    <data_dir>/tools/
      bin/            executables (or symlinks to them)
      python/         packages from ``pip install --target``
      sandbox.json    {"env": {"NAME": "value"}, "host_ports": [26585]}

A FlexLM license server listed by its lmgrd port brings its vendor daemons along: they listen
on ports chosen at each start, so ``license_daemon_ports`` finds them in /proc, from lmgrd's own
process tree only, every time a command runs. An lmgrd run by another user (root) hides its
sockets; then ``lmutil lmstat`` is traced on the host, and the local ports it connects to after
asking lmgrd, still listening and owned by lmgrd's user, are its vendor daemons (cached for a
minute, or until one stops listening).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Set by the sandbox itself, or would let a recorded value change what every command loads.
RESERVED_ENV = {"PATH", "HOME", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "LANG"}
EDA_PROFILE = Path("/workspace/eda/.edarc")
PROC = Path("/proc")
LICENSE_MANAGER = "lmgrd"
TCP_LISTEN = "0A"
PROBE_TTL = 60  # seconds a traced lmstat's answer is reused while its ports still listen
PROBE_TIMEOUT = 20
CONNECT = re.compile(r'sin6?_port=htons\((\d+)\).*?(?:inet_addr\("([^"]+)"\)|inet_pton\(AF_INET6, "([^"]+)")')
PIN_ADVICE = (
    "Pin each vendor daemon's port in the license file (VENDOR snpslmd PORT=27021), restart lmgrd "
    "(lmutil lmdown -c <file>; lmgrd -c <file>) and add that port to host_ports."
)
_warned: set[str] = set()
_probes: dict[int, tuple[float, set[int]]] = {}
_probe_lock = threading.Lock()


def tools_dir() -> Path:
    return (settings.data_dir / "tools").resolve()


def prepare() -> Path:
    root = tools_dir()
    for folder in (root / "bin", root / "python"):
        folder.mkdir(parents=True, exist_ok=True)
    return root


def _overrides() -> dict:
    path = tools_dir() / "sandbox.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("Ignoring unreadable %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def recorded_env() -> dict[str, str]:
    env = _overrides().get("env")
    if not isinstance(env, dict):
        return {}
    return {
        name: value for name, value in env.items()
        if isinstance(value, str) and ENV_NAME.match(str(name)) and name not in RESERVED_ENV
    }


def host_ports() -> list[int]:
    """Listed ports, plus the vendor daemons of a license server listed by its lmgrd port."""
    recorded = _overrides().get("host_ports")
    ports = [port for port in recorded if isinstance(port, int) and 0 < port < 65536] if isinstance(recorded, list) else []
    listed = set(settings.sandbox_host_ports) | set(ports)
    return sorted(listed | license_daemon_ports(listed))


def _listeners(proc: Path) -> dict[str, tuple[int, str]]:
    """Socket inode -> (port, owner uid) of every listening TCP socket (IPv4 and IPv6)."""
    found = {}
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == TCP_LISTEN:
                found[fields[9]] = (int(fields[1].rsplit(":", 1)[1], 16), fields[7])
    return found


def _processes(proc: Path) -> dict[str, tuple[str, str]]:
    """pid -> (command name, parent pid)."""
    found = {}
    for entry in proc.iterdir() if proc.is_dir() else ():
        if not entry.name.isdigit():
            continue
        try:
            head, _, rest = (entry / "stat").read_text().rpartition(")")
            found[entry.name] = (head.partition("(")[2], rest.split()[1])
        except (OSError, IndexError):
            continue
    return found


def _sockets(proc: Path, pid: str) -> set[str]:
    """Socket inodes the process holds open; raises OSError if its fds cannot be read."""
    folder = proc / pid / "fd"
    try:
        fds = os.listdir(folder)
    except FileNotFoundError:  # the process has exited
        return set()
    found = set()
    for fd in fds:
        try:
            target = os.readlink(folder / fd)
        except FileNotFoundError:  # closed meanwhile
            continue
        if target.startswith("socket:["):
            found.add(target[8:-1])
    return found


def _uid(proc: Path, pid: str) -> str | None:
    try:
        for line in (proc / pid / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return line.split()[1]
    except (OSError, IndexError):
        pass
    return None


def _cmdline(proc: Path, pid: str) -> list[str]:
    try:
        return (proc / pid / "cmdline").read_text().split("\0")
    except OSError:
        return []


def license_daemon_ports(ports, proc=None) -> set[int]:
    """Ports the vendor daemons (snpslmd, ...) of an lmgrd listening on one of ``ports`` listen on.

    Only lmgrd's own children count, so no other local service is ever added. When lmgrd runs as
    another user its fds are unreadable, and a traced ``lmutil lmstat`` (``_probe``) finds them
    instead; failing that, the warning says how to pin a vendor daemon's port.
    """
    proc = Path(proc or PROC)
    wanted = set(ports)
    if not wanted:
        return set()
    listeners = _listeners(proc)
    if not listeners:
        return set()
    processes = _processes(proc)
    found: set[int] = set()
    claimed: set[int] = set()
    hidden: dict[str, str] = {}  # pid -> uid of an lmgrd whose sockets cannot be read
    for pid, (name, _) in processes.items():
        if name != LICENSE_MANAGER:
            continue
        try:
            own = {listeners[inode][0] for inode in _sockets(proc, pid) if inode in listeners}
        except OSError:
            uid = _uid(proc, pid)
            if uid is not None:
                hidden[pid] = uid
            continue
        if not own & wanted:
            continue
        claimed |= own
        for child, (_, parent) in processes.items():
            if parent != pid:
                continue
            try:
                found |= {listeners[inode][0] for inode in _sockets(proc, child) if inode in listeners}
            except OSError:
                _warn_once(f"child:{child}", "Cannot read the sockets of vendor daemon %s, so its port is "
                           "not bridged into the sandbox. " + PIN_ADVICE, child)
    if hidden:
        found |= _hidden_daemon_ports(proc, wanted - claimed, hidden, set(listeners.values()))
    return found


def _hidden_daemon_ports(proc: Path, wanted: set[int], hidden: dict[str, str], listening: set[tuple[int, str]]) -> set[int]:
    """Vendor daemon ports of lmgrds whose sockets cannot be read: each listed port listening as
    one of their users is asked with a traced ``lmutil lmstat``."""
    users = set(hidden.values())
    candidates = sorted(port for port, uid in listening if port in wanted and uid in users)
    if not candidates:
        return set()
    found: set[int] = set()
    with _probe_lock:
        for port in candidates:
            uid = next(uid for listened, uid in listening if listened == port and uid in users)
            cached = _probes.get(port)
            if cached and time.monotonic() - cached[0] < PROBE_TTL and all((p, uid) in listening for p in cached[1]):
                found |= cached[1]
                continue
            daemons = {p for p in _probe(port, _lmutil(proc, hidden)) if p != port and (p, uid) in listening}
            _probes[port] = (time.monotonic(), daemons)
            if not daemons:
                _warn_once(f"probe:{port}", (
                    "Cannot read the sockets of the license server on port %s (lmgrd runs as another user), "
                    "and tracing lmutil lmstat (needs strace and lmutil) found no vendor daemon, so their "
                    "ports are not bridged into the sandbox. " + PIN_ADVICE), port)
            found |= daemons
    return found


def _lmutil(proc: Path, hidden: dict[str, str]) -> str | None:
    """lmutil on the commands' PATH, else next to lmgrd or its license file."""
    folders = [command_env(dict(os.environ))["PATH"]]
    for pid in hidden:
        args = _cmdline(proc, pid)
        files = [args[0]] if args and args[0].startswith("/") else []
        files += [value for flag, value in zip(args, args[1:]) if flag == "-c" and value.startswith("/")]
        for path in files:
            folder = os.path.dirname(path)
            folders += [folder, os.path.dirname(folder)]
    return shutil.which("lmutil", path=os.pathsep.join(folders))


def _probe(port: int, lmutil: str | None) -> set[int]:
    """Local ports ``lmutil lmstat -c <port>@localhost`` connects to, traced with strace: lmgrd
    answers it with its vendor daemons' ports. Runs on the host, never in a sandbox."""
    strace = shutil.which("strace")
    if not strace or not lmutil:
        return set()
    command = [strace, "-f", "-e", "trace=connect", lmutil, "lmstat", "-c", f"{port}@localhost"]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError:
        return set()
    try:
        _, trace = process.communicate(timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        _, trace = process.communicate()
    found = set()
    for line in trace.splitlines():
        match = CONNECT.search(line)
        if match and _loopback(match.group(2) or match.group(3)):
            found.add(int(match.group(1)))
    return found


def _loopback(address: str) -> bool:
    return address.startswith(("127.", "::ffff:127.")) or address == "::1"


def _warn_once(key: str, message: str, *args) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


def command_env(base: dict[str, str]) -> dict[str, str]:
    """``base`` with the tools folder on PATH and PYTHONPATH and the recorded variables."""
    root = tools_dir()
    env = {**base, **recorded_env()}
    env["PATH"] = f"{root / 'bin'}:{base.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    env["PYTHONPATH"] = ":".join(filter(None, [str(root / "python"), base.get("PYTHONPATH")]))
    return env


def shell_setup() -> str:
    """Load the installed EDA tools in the command's sandbox, with its sanitized environment.

    A non-interactive bash does not read this profile itself. Keep initialization in the
    child shell: it must not run on the host or redirect the caller's stdout/stderr.
    """
    profile = shlex.quote(str(EDA_PROFILE))
    return f"if [ -r {profile} ]; then . {profile} || exit $?; fi\n"
