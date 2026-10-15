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
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Set by the sandbox itself, or would let a recorded value change what every command loads.
RESERVED_ENV = {"PATH", "HOME", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "LANG"}


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
    recorded = _overrides().get("host_ports")
    ports = [port for port in recorded if isinstance(port, int) and 0 < port < 65536] if isinstance(recorded, list) else []
    return sorted(set(settings.sandbox_host_ports) | set(ports))


def command_env(base: dict[str, str]) -> dict[str, str]:
    """``base`` with the tools folder on PATH and PYTHONPATH and the recorded variables."""
    root = tools_dir()
    env = {**base, **recorded_env()}
    env["PATH"] = f"{root / 'bin'}:{base.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    env["PYTHONPATH"] = ":".join(filter(None, [str(root / "python"), base.get("PYTHONPATH")]))
    return env
