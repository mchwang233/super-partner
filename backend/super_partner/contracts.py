"""Artifact handoffs and bounded work packages. No model assertion substitutes for evidence."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath

from .models import Artifact, ArtifactContract, NodeStatus, TaskNode, TaskPlan


class ContractError(ValueError):
    """A plan cannot be executed without weakening an artifact contract."""


class ArtifactBlocked(RuntimeError):
    """Required evidence is missing, unsafe, stale, or has not been accepted."""


RULES = """
Artifact handoffs: separate VDB collection (coverage_vdb), URG text-report generation
(urg_report), and delivery audit (delivery_audit) when the request needs these distinct jobs.
Do not force every coverage task into three nodes: an existing external VDB can feed a report
directly; a request for collection alone needs no report or audit. Never combine production
with its independent delivery audit. Do not invent extra stages or weaken acceptance criteria.
Each such executable step declares artifact_contract:
{"stage": "coverage_vdb|urg_report|delivery_audit", "inputs":
 [{"path": "relative/path", "kind": "file|directory", "source": "external|dependency"}],
 "outputs": [{"path": "relative/path", "kind": "file|directory"}],
 "acceptance_commands": ["commands that actually validate the deliverables"],
 "blocked_when": ["specific missing evidence or environment conditions"],
 "max_turns": 20, "max_cost_usd": 0.5}.
Choose explicit limits appropriate for the work and available budget, not these example values.
Inputs are evidence that MUST already exist, not files this step is responsible for generating.
Use inputs: [] when there are none. A dependency input must be an output of an ancestor step;
declare that dependency (after_parts across major parts). External evidence needs no producer.
Declare VDBs as directory artifacts, text reports and audit records as file artifacts. List outputs
in files and inputs in reads. Audit consumes accepted reports, never an unfinished report or a
worker's verbal completion claim. New output files are expected to be absent at startup.
Limits accumulate across automatic retries/model switches; at a limit stop with diagnosis and
logs, without scope expansion or automatic continuation. Missing upstream evidence returns
BLOCKED: <reason>. CLI channels without reliable turn accounting cannot execute these limits.
"""


def declarations(raw: dict) -> dict:
    if "artifact_contract" not in raw:
        return {}
    try:
        return {"artifact_contract": ArtifactContract.model_validate(raw["artifact_contract"])}
    except ValueError as exc:
        raise ContractError(f"artifact_contract: {exc}") from exc


def safe_name(name: str) -> bool:
    # Database files inside VDBs are data, so do not use the review policy's .db exclusion.
    from .quality import PRIVATE_DIRS, PRIVATE_NAMES

    path = PurePosixPath(name)
    return bool(name.strip() and not path.is_absolute() and path.parts and name == path.as_posix()
                and not any(part in {"..", ".", ".git", ".super-partner"}
                            or part.lower() in PRIVATE_DIRS | PRIVATE_NAMES
                            or part.lower().startswith(".env")
                            or Path(part).stem.lower() in PRIVATE_NAMES
                            or Path(part).suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
                            for part in path.parts)
                and not any(char in name for char in "*?[]\\\n\r\0"))


def stages(node: TaskNode) -> set[str]:
    """Recognize explicit jobs, not arbitrary mentions of coverage in a software request."""
    text = node.title.lower()
    found = set()
    if any(path.rstrip("/").endswith(".vdb") for path in node.files) or (
        "vdb" in text and re.search(r"采集|生成|收集|collect|generat|capture|simulat", text)
    ):
        found.add("coverage_vdb")
    if "urg" in text and re.search(r"报告|文本|report|text", text):
        found.add("urg_report")
    if re.search(r"交付审计|delivery audit|independent audit|独立审计", text):
        found.add("delivery_audit")
    if node.artifact_contract and node.artifact_contract.stage:
        found.add(node.artifact_contract.stage)
    return found


def node_problems(node: TaskNode, *, coverage: bool = False) -> list[str]:
    contract = node.artifact_contract
    jobs = stages(node)
    if not coverage and jobs == {"delivery_audit"} and not contract:
        return []
    if len(jobs) > 1:
        return [f"{node.title}: separate artifact stages: {', '.join(sorted(jobs))}"]
    if not contract:
        return [f"{node.title}: artifact_contract is required"] if jobs else []
    errors = []
    if jobs and contract.stage not in jobs:
        errors.append("declare the artifact stage")
    paths = [item.path for item in contract.inputs + contract.outputs]
    if any(not safe_name(path) for path in paths):
        errors.append("artifact paths must be safe, relative, concrete paths")
    if len(set(paths)) != len(paths):
        errors.append("inputs and outputs must be distinct; own outputs are not prerequisites")
    if any(item.path not in node.files for item in contract.outputs):
        errors.append("list every output in files")
    if any(item.path not in node.reads for item in contract.inputs):
        errors.append("list every input in reads")
    if not node.acceptance_criteria.strip():
        errors.append("retain concrete acceptance_criteria")
    if contract.stage == "coverage_vdb" and not any(
        item.kind == "directory" and item.path.endswith(".vdb") for item in contract.outputs
    ):
        errors.append("coverage_vdb must deliver a VDB directory")
    if contract.stage == "urg_report":
        if not any(item.kind == "directory" and item.path.endswith(".vdb") for item in contract.inputs):
            errors.append("urg_report must consume a VDB directory")
        if not any(item.kind == "file" for item in contract.outputs):
            errors.append("urg_report must deliver a text report file")
    if contract.stage == "delivery_audit" and not any(item.kind == "file" for item in contract.inputs):
        errors.append("delivery_audit must consume report evidence")
    return [f"{node.title}: {error}" for error in errors]


def ancestors(node: TaskNode, nodes: list[TaskNode]) -> list[TaskNode]:
    by_id, seen, todo = {item.id: item for item in nodes}, set(), list(node.dependencies)
    while todo:
        key = todo.pop()
        if key in seen or key not in by_id:
            continue
        seen.add(key)
        todo += by_id[key].dependencies
    return [item for item in nodes if item.id in seen]


def producers(item: Artifact, nodes: list[TaskNode]) -> list[TaskNode]:
    return [node for node in nodes if node.artifact_contract and any(
        out.path == item.path and out.kind == item.kind for out in node.artifact_contract.outputs)]


def plan_problems(plan: TaskPlan, *, partial: bool = False) -> list[str]:
    coverage = any(stages(node) & {"coverage_vdb", "urg_report"} for node in plan.nodes)
    problems = []
    delivered = {}
    for node in plan.nodes:
        problems += node_problems(node, coverage=coverage)
        contract = node.artifact_contract
        if not contract:
            if coverage and node.verifies and any(item.artifact_contract for item in ancestors(node, plan.nodes)):
                problems.append(f"{node.title}: audit of artifact work requires artifact_contract")
            continue
        for output in contract.outputs:
            if output.path in delivered:
                problems.append(f"{node.title}: duplicate artifact producer for {output.path}; retain the original limits")
            delivered[output.path] = node.id
        before = ancestors(node, plan.nodes)
        for item in contract.inputs:
            owners = producers(item, plan.nodes)
            if any(owner.id == node.id for owner in owners):
                continue  # the local distinct-input/output check explains this
            if item.source == "external" and owners:
                problems.append(f"{node.title}: {item.path} is produced in this plan, not external evidence")
            if (item.source == "dependency" and len(producers(item, before)) != 1
                    and not (partial and node.after_parts and not owners)):
                problems.append(f"{node.title}: {item.path} needs exactly one declared producer dependency")
        if contract.stage == "delivery_audit":
            reports = [item for item in before if item.artifact_contract
                       and item.artifact_contract.stage == "urg_report"]
            for report in reports:
                if not any(producers(item, [report]) for item in contract.inputs):
                    problems.append(f"{node.title}: audit must consume accepted report from {report.title}")
    return problems


def validate(plan: TaskPlan, *, partial: bool = False) -> None:
    if problems := plan_problems(plan, partial=partial):
        raise ContractError("; ".join(problems))


def workspace(run, node) -> Path:
    from .config import settings
    return Path(run.project_dir) if run.project_dir and node.needs_workspace else settings.workspace_root / run.folder_id


def _path(root: Path, name: str) -> Path:
    from .config import settings

    if not safe_name(name):
        raise ArtifactBlocked(f"不安全的产物路径：{name}")
    base = root.resolve()
    path = base / name
    if any(part.is_symlink() for part in [path, *path.parents] if part != base and base in part.parents):
        raise ArtifactBlocked(f"产物不能通过符号链接读取：{name}")
    if base not in path.resolve().parents:
        raise ArtifactBlocked(f"产物超出工作目录：{name}")
    # Match the sandbox's hidden platform data, including configurable names. A task's own
    # workspace may be inside data_dir/workspace_root; the sandbox exposes that workspace only.
    for folder in (Path(settings.data_dir).resolve(), Path(settings.workspace_root).resolve()):
        if folder != base and base in folder.parents and (path == folder or folder in path.parents):
            raise ArtifactBlocked(f"产物位于受保护的平台数据目录：{name}")
    database = Path(settings.db_path).resolve()
    if path in {Path(f"{database}{suffix}") for suffix in ("", "-wal", "-shm", "-journal")}:
        raise ArtifactBlocked(f"产物不能读取平台数据库：{name}")
    return path


def fingerprint(root: Path, item: Artifact) -> str:
    """Hash bytes and freshness metadata; an empty/missing artifact is never evidence."""
    try:
        path = _path(root, item.path)
        if item.kind == "directory":
            if not path.is_dir():
                raise ArtifactBlocked(f"缺少目录产物：{item.path}")
            files = []
            for folder, dirs, names in os.walk(path, followlinks=False):
                for name in dirs + names:
                    _path(root, (Path(folder) / name).relative_to(root.resolve()).as_posix())
                files += [Path(folder) / name for name in names]
                if len(files) > 20_000:
                    raise ArtifactBlocked(f"目录产物过大，无法完整核验：{item.path}")
        else:
            files = [path]
        digest, size = hashlib.sha256(), 0
        for file in sorted(files):
            if not file.is_file():
                raise ArtifactBlocked(f"不是普通文件：{item.path}")
            stat = file.stat()
            digest.update(f"{file.relative_to(path) if file != path else file.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
        if not size:
            raise ArtifactBlocked(f"产物为空：{item.path}")
        return digest.hexdigest()
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ArtifactBlocked):
            raise
        raise ArtifactBlocked(f"无法核验产物 {item.path}：{exc}") from exc


def output_snapshot(root: Path, node: TaskNode) -> dict[str, str]:
    return {item.path: fingerprint(root, item) for item in node.artifact_contract.outputs}


def accepted(run, node: TaskNode, seen=None) -> None:
    """Revalidate a saved acceptance before a consumer or lead can rely on it."""
    evidence, contract = node.artifact_evidence, node.artifact_contract
    if not contract:
        return
    if (not evidence or not evidence.accepted or evidence.generation != node.generation
            or [check.get("command") for check in evidence.checks] != contract.acceptance_commands
            or not all(check.get("ok") for check in evidence.checks)):
        raise ArtifactBlocked(f"{node.title} 尚无有效产物验收证据")
    if output_snapshot(workspace(run, node), node) != evidence.outputs:
        raise ArtifactBlocked(f"{node.title} 的已验收产物已变化或过期")
    seen = set() if seen is None else seen
    if node.id in seen:
        raise ArtifactBlocked("产物依赖形成循环")
    seen.add(node.id)
    if input_snapshot(run, node, seen) != evidence.inputs:
        raise ArtifactBlocked(f"{node.title} 的输入证据已变化，报告过期")
    seen.remove(node.id)


def input_snapshot(run, node: TaskNode, seen=None) -> dict[str, str]:
    before, found = ancestors(node, run.plan.nodes), {}
    for item in node.artifact_contract.inputs:
        value = fingerprint(workspace(run, node), item)
        if item.source == "dependency":
            owners = producers(item, before)
            if len(owners) != 1 or owners[0].status != NodeStatus.SUCCEEDED:
                raise ArtifactBlocked(f"{item.path} 缺少已完成且已验收的上游步骤")
            producer = owners[0]
            accepted(run, producer, seen)
            if producer.artifact_evidence.outputs.get(item.path) != value:
                raise ArtifactBlocked(f"{item.path} 与上游验收的产物不一致")
        found[item.path] = value
    return found


def prepare(run, node: TaskNode) -> None:
    """Only inputs are prerequisites; outputs form a baseline for this generation's work."""
    if not node.artifact_contract:
        return
    if problems := node_problems(node):
        raise ArtifactBlocked("; ".join(problems))
    inputs = input_snapshot(run, node)
    if node.contract_generation == node.generation:
        if inputs != node.artifact_inputs:
            raise ArtifactBlocked("输入证据在本轮工作后变化，必须重新规划或返工")
        return
    baseline = {}
    for item in node.artifact_contract.outputs:
        try:
            baseline[item.path] = fingerprint(workspace(run, node), item)
        except ArtifactBlocked:
            baseline[item.path] = ""
    node.contract_generation = node.generation
    node.artifact_inputs, node.artifact_baseline = inputs, baseline
    node.artifact_evidence = None


def delivery_snapshot(run, node: TaskNode) -> dict[str, str]:
    if node.contract_generation != node.generation:
        raise ArtifactBlocked("尚未记录本次产物工作的输入和基线")
    if input_snapshot(run, node) != node.artifact_inputs:
        raise ArtifactBlocked("输入证据变化，当前报告不能交付")
    outputs = output_snapshot(workspace(run, node), node)
    for path, value in outputs.items():
        if node.artifact_baseline.get(path) == value:
            raise ArtifactBlocked(f"产物未更新，不能交付旧报告：{path}")
    return outputs


def prompt(node: TaskNode) -> str:
    if not node.artifact_contract:
        return ""
    return ("Artifact contract (additional to the original acceptance criteria):\n"
            + node.artifact_contract.model_dump_json(indent=2)
            + f"\nAlready used: {node.contract_turns} turns, ${node.contract_cost_usd:.6f}."
            "\nCreate your own outputs; they need not exist before you start. Do not expand scope. "
            "Missing prerequisite evidence: preserve logs and end with BLOCKED: <why>. "
            "At a declared limit stop with diagnostic evidence. Audit only accepted artifacts.")
