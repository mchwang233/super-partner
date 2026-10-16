"""A finished task's output: its files, a ZIP download, and publishing to GitHub.

A task without a project writes into its scratch folder, and all of it is the output. A task
on a project changed files in the user's own checkout, so only the files its nodes changed
are output, and that checkout is never pushed from here.
"""
from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import httpx

from .config import settings
from .models import ExecutionRun, NodeStatus

# Tool and review folders the agents leave behind are not part of the output.
SKIP = {".git", ".review", ".claude", ".codex", "node_modules", "__pycache__", ".venv", "venv", ".super-partner"}
MAX_FILES = 5_000
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_VIEW_BYTES = 1024 * 1024
GITHUB_API = "https://api.github.com"
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
GITHUB_REMOTE = re.compile(r"^(git@github\.com:|https://github\.com/)[A-Za-z0-9._-]+/[A-Za-z0-9._-]+?(\.git)?/?$")


class PublishError(RuntimeError):
    pass


def output_root(run: ExecutionRun) -> Path:
    return Path(run.project_dir) if run.project_dir else (settings.workspace_root / run.folder_id)


def output_files(run: ExecutionRun) -> list[dict]:
    """Relative path and size of every output file."""
    root = output_root(run)
    if not root.is_dir():
        return []
    if run.project_dir:
        names = sorted({name for node in run.plan.nodes for name in node.changed_files})
        candidates = [root / name for name in names]
    else:
        candidates = []
        for folder, dirs, names in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name not in SKIP)
            candidates.extend(Path(folder, name) for name in sorted(names))
            if len(candidates) >= MAX_FILES:
                break
    found = []
    for path in candidates[:MAX_FILES]:
        if path.is_file() and not path.is_symlink():
            found.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
    return found


def resolve_output(run: ExecutionRun, relative: str) -> Path:
    """An output file by its relative path; anything else (escapes, unlisted files) is refused."""
    if relative not in {item["path"] for item in output_files(run)}:
        raise PublishError("文件不在这个任务的输出里")
    return output_root(run) / relative


def read_output(run: ExecutionRun, relative: str) -> str:
    path = resolve_output(run, relative)
    data = path.read_bytes()[:MAX_VIEW_BYTES + 1]
    if b"\x00" in data[:8192]:
        raise PublishError("这是二进制文件，请下载 ZIP 后查看")
    text = data[:MAX_VIEW_BYTES].decode("utf-8", errors="replace")
    return text + ("\n[... 文件过大，只显示前 1 MB]" if len(data) > MAX_VIEW_BYTES else "")


def result_markdown(run: ExecutionRun) -> str:
    """A readable record of the task: request, final result, and each step's output."""
    lines = [f"# {run.plan.summary}", "", f"> {run.prompt}", "", "## 最终结果", "", run.final_result or "（无）", ""]
    for index, node in enumerate(run.plan.nodes, 1):
        status = {NodeStatus.SUCCEEDED: "已完成", NodeStatus.FAILED: "失败", NodeStatus.UNABLE: "无法完成",
                  NodeStatus.BLOCKED: "未执行"}.get(node.status, node.status.value)
        lines += [f"## {index:02d} {node.title}（{status} · {node.model_id or '未分配'}）", ""]
        if node.unable_reason:
            lines += [f"**无法完成的原因**：{node.unable_reason}", ""]
        lines += [node.result or "（无输出）", ""]
    return "\n".join(lines)


def slug(run: ExecutionRun) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", run.plan.summary).strip("-").lower()[:40]
    return f"{base or 'task'}-{run.created_at:%Y%m%d}-{run.id[:6]}"


def archive(run: ExecutionRun) -> bytes:
    buffer, total = io.BytesIO(), 0
    root = output_root(run)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        names = set()
        for item in output_files(run):
            total += item["size"]
            if total > MAX_ARCHIVE_BYTES:
                raise PublishError("输出文件超过 200 MB，无法打包")
            bundle.write(root / item["path"], item["path"])
            names.add(item["path"])
        bundle.writestr("SUPER_PARTNER_RESULT.md" if "RESULT.md" in names else "RESULT.md", result_markdown(run))
    return buffer.getvalue()


def _clean(text: str, token: Optional[str]) -> str:
    return text.replace(token, "***") if token else text


def _git(args: list[str], cwd: Path, env: dict, token: Optional[str] = None) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise PublishError(_clean(f"git {args[0]} 失败：{(result.stderr or result.stdout).strip()[-400:]}", token))


def _identity() -> tuple[str, str]:
    def get(key: str) -> str:
        result = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True, check=False)
        return result.stdout.strip()
    return get("user.name") or "Super Partner", get("user.email") or "super-partner@localhost"


def push_snapshot(run: ExecutionRun, remote: str, token: Optional[str] = None) -> None:
    """Commit the task's output in a fresh repository and push it as ``main``."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="publish-", dir=settings.data_dir))
    try:
        (staging / "RESULT.md").write_text(result_markdown(run))
        for item in output_files(run):
            target = staging / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_root(run) / item["path"], target)
        name, email = _identity()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
               "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        if token:
            # The token goes in through the environment, never on a command line or in the URL.
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
        _git(["init", "-q", "-b", "main"], staging, env)
        _git(["add", "-A"], staging, env)
        _git(["commit", "-q", "-m", f"{run.plan.summary}\n\nGenerated by Super Partner from task {run.id}."], staging, env)
        _git(["push", "-q", remote, "main"], staging, env, token)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def create_repository(token: str, name: str, private: bool, description: str, client: httpx.AsyncClient) -> dict:
    response = await client.post(
        f"{GITHUB_API}/user/repos",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"name": name, "private": private, "description": description[:300], "auto_init": False},
    )
    if response.status_code == 401:
        raise PublishError("GitHub 令牌无效或已过期")
    if response.status_code == 422:
        raise PublishError(f"GitHub 上已经有名为 {name} 的仓库，换个名字再试")
    if response.is_error:
        raise PublishError(_clean(f"GitHub 创建仓库失败（{response.status_code}）：{response.text[:200]}", token))
    return response.json()


def check_remote(remote: str) -> str:
    remote = remote.strip()
    if not GITHUB_REMOTE.match(remote):
        raise PublishError("请填写 GitHub 仓库地址，例如 git@github.com:you/repo.git")
    return remote
