"""Validated application repairs and a durable handoff to an independent restart worker.

This file deliberately uses only the standard library: a frozen copy activates a repair
outside the API's service, and can roll it back even when the new API cannot start.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

SOURCE_DIRS = {"backend", "frontend", "tests", "scripts"}
ROOT_FILES = {"pyproject.toml", "README.md", "Dockerfile", "compose.yaml", ".gitignore",
              ".dockerignore", "start.sh", "stop.sh"}
SKIP = {".git", ".agents", ".aws", ".claude", ".codex", ".super-partner", "workspaces",
        ".venv", "node_modules", "dist", "build", "__pycache__", ".pytest_cache", ".ruff_cache"}
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".html", ".json", ".sh",
            ".md", ".svg", ".png", ".webp"}
PRIVATE = {"credentials.json", "secrets.json", ".env"}
CHECK_TIMEOUT = 600


class RepairError(RuntimeError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def backup_database(source: Path, destination: Path) -> None:
    with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live, \
            contextlib.closing(sqlite3.connect(destination)) as copied:
        live.backup(copied)


def source_files(root: Path) -> dict[str, dict]:
    """Snapshot source only; never include live tasks, secrets, dependencies or Git state."""
    result = {}
    for base, directories, files in os.walk(root, followlinks=False):
        relative = Path(base).relative_to(root)
        directories[:] = [d for d in directories if d not in SKIP
                          and (relative.parts or d in SOURCE_DIRS)]
        for directory in directories:
            if (Path(base) / directory).is_symlink():
                raise RepairError(f"源码目录不能是符号链接：{relative / directory}")
        for name in files:
            path = Path(base) / name
            rel = path.relative_to(root)
            if (name in PRIVATE or name.startswith(".env.")
                    or (not relative.parts and name not in ROOT_FILES)
                    or (relative.parts and path.suffix not in SUFFIXES)):
                continue
            if path.is_symlink() or not path.is_file():
                raise RepairError(f"源码文件不能是符号链接：{rel}")
            if path.stat().st_size > 5_000_000 or len(result) >= 5000:
                raise RepairError("平台修复源码范围过大。")
            result[rel.as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": path.stat().st_mode & 0o777,
            }
    return result


def changed(before: dict, after: dict) -> list[str]:
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def source_root(configured=None):
    root = Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "backend/super_partner/api.py").is_file():
        return root
    return None


def unit_name(root: Path) -> str:
    checksum = subprocess.run(["cksum"], input=str(root).encode(), capture_output=True, check=True)
    return f"super-partner-{checksum.stdout.split()[0].decode()}-api.service"


def service_properties(unit: str) -> dict:
    result = subprocess.run(["systemctl", "--user", "show", unit, "--property=MainPID,WorkingDirectory,ActiveState"],
                            capture_output=True, text=True, timeout=10, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


class PlatformRepairs:
    def __init__(self, root, data_dir: Path, api_url: str, enabled: bool = True, database_path=None):
        self.root = source_root(root)
        self.directory = data_dir.resolve() / "platform-repairs"
        self.api_url = api_url.rstrip("/")
        self.enabled = enabled
        self.database_path = Path(database_path).resolve() if database_path else None

    def context(self) -> str:
        if not self.enabled:
            return "Platform self-repair is disabled in the application configuration."
        if not self.root:
            return "No editable Super Partner source checkout was found in this installation."
        return (f"Super Partner source checkout (read-only during investigation): {self.root}\n"
                "Its code is under backend/super_partner and frontend; regression tests are "
                "under tests and frontend/tests. The repair controller will provide an isolated "
                "copy, run independent checks, preserve local data, and activate validated fixes "
                "for all projects. Never modify live source, credentials, task records or Git history.")

    async def prepare(self, run_id: str, diagnosis_id: str, *, allow_network: bool = False) -> dict:
        if not self.enabled or not self.root:
            raise RepairError("平台自修复未启用，或当前安装没有可编辑的 Super Partner 源码。")
        unit = unit_name(self.root)
        try:
            service = await asyncio.to_thread(service_properties, unit)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RepairError("平台自动生效需要由 start.sh 启动的用户服务；无法连接该服务管理器。") from exc
        if service.get("MainPID") != str(os.getpid()) or service.get("WorkingDirectory") != str(self.root):
            raise RepairError("当前 API 不是此源码目录的受管服务，不能安全自动重启其他进程。")
        self.directory.mkdir(parents=True, exist_ok=True)
        folder = self.directory / uuid4().hex
        folder.mkdir(mode=0o700)
        before = source_files(self.root)
        for name in before:
            for directory in ["before", "source"]:
                destination = folder / directory / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / name, destination)
        dependencies = self.root / "frontend/node_modules"
        if dependencies.is_dir():
            (folder / "source/frontend/node_modules").symlink_to(dependencies, target_is_directory=True)
        shutil.copyfile(__file__, folder / "activate.py")
        job = {"id": folder.name, "scope": "super_partner", "status": "editing",
               "run_id": run_id, "diagnosis_id": diagnosis_id, "root": str(self.root),
               "folder": str(folder), "unit": unit, "old_pid": os.getpid(),
               "database_path": str(self.database_path) if self.database_path else None,
               "api_url": self.api_url, "before": before, "files": [], "checks": [],
               "allow_network": allow_network,
               "created_at": time.time()}
        self.save(job)
        return job

    def save(self, job: dict) -> None:
        atomic_json(Path(job["folder"]) / "job.json", job)

    def pending(self):
        path = self.directory / "pending.json"
        if not path.is_file():
            return None
        pointer = json.loads(path.read_text())
        identifier = pointer.get("id", "")
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise RepairError("平台修复记录标识无效。")
        return json.loads((self.directory / identifier / "job.json").read_text())

    def clear_pending(self, job: dict) -> None:
        current = self.pending()
        if current and current["id"] == job["id"]:
            (self.directory / "pending.json").unlink()

    def schedule(self, job: dict) -> None:
        self.save(job)
        atomic_json(self.directory / "pending.json", {"id": job["id"]})

    async def check(self, job: dict) -> list[dict]:
        source = Path(job["folder"]) / "source"
        after = source_files(source)
        files = changed(job["before"], after)
        if not files:
            raise RepairError("诊断没有修改平台源码，不能报告修复完成。")
        required = {"pyproject.toml", "backend/super_partner/api.py", "frontend/package.json"}
        required.update(name for name in job["before"] if name.startswith(("tests/", "frontend/tests/")))
        if required - after.keys():
            raise RepairError("平台修复不能删除应用入口、构建配置或原有回归测试。")
        if not any(name.startswith(("tests/", "frontend/tests/")) for name in files):
            raise RepairError("平台修复需要新增或更新能复现当前缺陷的回归测试。")
        if not any(name.startswith(("backend/", "frontend/", "scripts/"))
                   and not name.startswith("frontend/tests/") for name in files):
            raise RepairError("只有测试变化，没有实际的平台修复。")
        job.update(status="validating", files=files, after=after, checks=[])
        self.save(job)
        commands = [
            ("Python syntax", [sys.executable, "-m", "compileall", "-q", "backend"], source),
            ("Backend tests", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], source),
            ("Frontend tests", ["npm", "test"], source / "frontend"),
            ("Frontend build", ["npm", "run", "build"], source / "frontend"),
        ]
        if self.database_path:
            snapshot = source / ".validation/database-compatibility.db"
            # Migrations are checked against a private copy of real records. The copy is
            # removed before the model can receive feedback and edit the candidate again.
            probe = """
import sqlite3, sys
from collections import Counter
from pathlib import Path
from super_partner.db import Database

def quoted(name):
    return '\"' + name.replace('\"', '\"\"') + '\"'

path = Path(sys.argv[1])
with sqlite3.connect(path) as connection:
    tables = [r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    before = {t: Counter(connection.execute('SELECT * FROM ' + quoted(t)).fetchall()) for t in tables}
    columns = {t: [r[1] for r in connection.execute('PRAGMA table_info(' + quoted(t) + ')')] for t in tables}
database = Database(path)
try:
    database.list_runs()
    database.list_models()
    for table in tables:
        sql = 'SELECT ' + ','.join(quoted(c) for c in columns[table]) + ' FROM ' + quoted(table)
        assert Counter(tuple(r) for r in database.connection.execute(sql)) == before[table], 'Existing records changed'
    assert database.connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
finally:
    database.connection.close()
print('Existing records remain compatible')
"""
            commands.insert(1, ("Existing database compatibility", [sys.executable, "-c", probe, str(snapshot)], source))
        for title, command, cwd in commands:
            if title == "Existing database compatibility":
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                backup_database(self.database_path, snapshot)
            try:
                result = await self._check_command(command, cwd, source,
                                                   allow_network=job.get("allow_network", False))
            finally:
                if title == "Existing database compatibility":
                    for suffix in ["", "-wal", "-shm", "-journal"]:
                        Path(str(snapshot) + suffix).unlink(missing_ok=True)
            job["checks"].append({"name": title, **result})
            self.save(job)
            if result["returncode"]:
                raise RepairError(f"{title} 未通过：\n{result['output']}")
        if source_files(source) != after:
            raise RepairError("验证过程中源码发生变化，必须重新检查。")
        job["status"] = "validated"
        self.save(job)
        return job["checks"]

    async def _check_command(self, command: list[str], cwd: Path, source: Path,
                             *, allow_network: bool = False) -> dict:
        import pwd

        # Only the validation controller imports application helpers. The frozen
        # activation/rollback worker must remain runnable with the standard library.
        from . import agent

        if not shutil.which("bwrap"):
            raise RepairError("平台修复验证需要 bubblewrap，以隔离真实任务数据。")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "HOME": str(source / ".validation/home"), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(source / "backend"), "SUPER_PARTNER_CATALOG_AUTO_SYNC": "false",
               "SUPER_PARTNER_PLATFORM_SELF_REPAIR": "false",
               "SUPER_PARTNER_DATA_DIR": str(source / ".validation/data"),
               "SUPER_PARTNER_DATABASE_PATH": str(source / ".validation/data/test.db"),
               "SUPER_PARTNER_WORKSPACE_ROOT": str(source / ".validation/workspaces"),
               "SUPER_PARTNER_VALIDATION_ALLOW_NETWORK": str(allow_network).lower()}
        if allow_network:
            env.update({name: os.environ[name] for name in agent.PROXY_ENV if name in os.environ})
        (source / ".validation/home").mkdir(parents=True, exist_ok=True)
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        hidden = ["--tmpfs", str(home)] if home != Path("/") and home.is_dir() else []
        # Expose installed runtimes/dependencies read-only, not the checkout containing
        # the real database, credentials, unrelated projects, or private home files.
        runtimes = {Path(sys.prefix), self.root / "frontend/node_modules"}
        for executable in ["node", "npm"]:
            located = shutil.which(executable)
            if located:
                runtimes.add(Path(located).resolve().parent.parent)
        mounts = [arg for path in sorted(runtimes) if path.is_dir() and home in path.parents
                  for arg in ("--ro-bind", str(path), str(path))]
        network = (agent.network_prefix(agent.proxy_ports()) if agent.network_available() else []) if allow_network else []
        isolated = [] if allow_network else ["--unshare-net"]
        # Tests may start a nested network sandbox. bubblewrap's private /dev would
        # otherwise hide the TUN device that pasta requires even with networking enabled.
        tun = ["--dev-bind", "/dev/net/tun", "/dev/net/tun"] if allow_network and Path("/dev/net/tun").is_char_device() else []
        args = [*network, "bwrap", "--ro-bind", "/", "/", "--tmpfs", "/tmp", *hidden, *mounts,
                "--bind", str(source), str(source),
                "--dev", "/dev", *tun, "--proc", "/proc", *isolated, "--die-with-parent", "--", *command]
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env, start_new_session=True,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), CHECK_TIMEOUT)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise
        return {"returncode": process.returncode, "output": output.decode(errors="replace")[-8000:]}

    async def launch(self, job: dict) -> None:
        if source_files(Path(job["root"])) != job["before"]:
            raise RepairError("验证期间平台源码被其他操作修改，已保留这些改动；本次修复未覆盖它们。")
        job["status"] = "scheduled"
        self.schedule(job)
        folder = Path(job["folder"])
        command = ["systemd-run", "--user", "--quiet", "--collect",
                   f"--unit={job['unit'][:-8]}-repair-{job['id'][:12]}",
                   f"--property=StandardOutput=append:{folder / 'activation.log'}",
                   f"--property=StandardError=append:{folder / 'activation.log'}",
                   "--", sys.executable, str(folder / "activate.py"), str(folder / "job.json")]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT)
        output, _ = await process.communicate()
        if process.returncode:
            job.update(status="failed", error=output.decode(errors="replace")[-2000:])
            self.save(job)
            raise RepairError(f"平台修复生效任务启动失败：{job['error']}")


def install_files(job: dict, *, rollback: bool = False) -> None:
    root = Path(job["root"])
    source = Path(job["folder"]) / ("before" if rollback else "source")
    wanted = job["before"] if rollback else job["after"]
    for name in job["files"]:
        destination = root / name
        if root not in destination.resolve().parents or destination.is_symlink():
            raise RepairError(f"修复路径越界或变成符号链接：{name}")
        if name not in wanted:
            destination.unlink(missing_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".sp-repair-tmp")
        shutil.copyfile(source / name, temporary)
        temporary.chmod(wanted[name]["mode"])
        temporary.replace(destination)


def wait_healthy(job: dict, timeout: float = 60) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(job["api_url"] + "/health", timeout=2) as response:
                body = json.load(response)
            pid = service_properties(job["unit"]).get("MainPID")
            if body.get("status") == "ok" and body.get("pid") == int(pid or 0) and pid != str(job["old_pid"]):
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(0.5)
    raise RepairError("新版本在 60 秒内未通过服务健康检查。")


def activate(job_path: Path) -> None:
    job = json.loads(job_path.read_text())
    applied = False
    try:
        if job["status"] != "scheduled" or source_files(Path(job["root"])) != job["before"]:
            raise RepairError("平台源码或修复状态发生变化，停止应用补丁。")
        if source_files(Path(job["folder"]) / "source") != job["after"]:
            raise RepairError("验证后的修复副本发生变化，停止应用补丁。")
        service = service_properties(job["unit"])
        if service.get("MainPID") != str(job["old_pid"]) or service.get("WorkingDirectory") != job["root"]:
            raise RepairError("目标服务已发生变化，停止重启。")
        job.update(status="activating", activation_started_at=time.time())
        atomic_json(job_path, job)
        if job.get("database_path"):
            backup_database(Path(job["database_path"]), Path(job["folder"]) / "database-before.db")
        applied = True
        install_files(job)
        subprocess.run(["systemctl", "--user", "restart", job["unit"]], check=True, timeout=90)
        wait_healthy(job)
        job["status"] = "activated"
    except Exception as exc:
        job.update(status="failed", error=str(exc))
        if applied:
            try:
                # Refuse to overwrite a third party's edits made during activation.
                current = source_files(Path(job["root"]))
                for name in job["files"]:
                    if current.get(name) not in (job["before"].get(name), job["after"].get(name)):
                        raise RepairError(f"回滚发现并发修改，已保留：{name}")
                install_files(job, rollback=True)
                subprocess.run(["systemctl", "--user", "restart", job["unit"]], check=True, timeout=90)
                wait_healthy(job)
                job["status"] = "rolled_back"
            except Exception as rollback_error:
                job["error"] += f"；回滚检查：{rollback_error}"
    job["finished_at"] = time.time()
    atomic_json(job_path, job)


if __name__ == "__main__":
    activate(Path(sys.argv[1]))
