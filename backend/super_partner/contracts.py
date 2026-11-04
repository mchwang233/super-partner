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
Declare VDBs as directory artifacts with a path ending in .vdb (for example artifacts/run.vdb);
use that exact path and kind in the consumer's inputs. Text reports and audit records are file artifacts. List outputs
in files and inputs in reads. Audit consumes accepted reports, never an unfinished report or a
worker's verbal completion claim. New output files are expected to be absent at startup.
Writing a generator/parser script, its unit tests or documentation is preparation, not report
production: do not label it urg_report or make real VDBs prerequisites for fixture-based tests.
Only the step that actually produces reports uses urg_report. Helper code/docs or intermediate
verification can use stage: null when they need bounded artifact contracts. A verification step
of contracted artifact work must also declare its inputs, output evidence and limits.
max_turns counts tool calls; give real work room (at least {floors}
by difficulty; lower values are raised to these). Limits accumulate across automatic
retries/model switches; at a limit the step stops with its logs and the task's lead decides
whether to grant more calls, split the work or give up. Missing upstream evidence returns
BLOCKED: <reason>. Membership CLIs cannot count tool calls; a contract step they run is bounded
by time instead (about 1.5 minutes per declared tool call) and by progress checks.
"""


# The fewest tool calls a contract step gets, by its difficulty: a lead's tighter guess stopped
# hard work before it had written its outputs.
MIN_TOOL_CALLS = {"easy": 20, "normal": 40, "hard": 80}
RULES = RULES.replace("{floors}", ", ".join(f"{n} for {d}" for d, n in MIN_TOOL_CALLS.items()))


def declarations(raw: dict) -> dict:
    if "artifact_contract" not in raw:
        return {}
    try:
        contract = ArtifactContract.model_validate(raw["artifact_contract"])
    except ValueError as exc:
        raise ContractError(f"artifact_contract: {exc}") from exc
    floor = MIN_TOOL_CALLS.get(str(raw.get("difficulty") or "normal"), MIN_TOOL_CALLS["normal"])
    from .config import settings
    if settings.contract_call_floors and contract.max_turns < floor:
        # The cost limit grows with the calls, so the raised limit is not cut short by it.
        contract.max_cost_usd = round(contract.max_cost_usd * floor / contract.max_turns, 6)
        contract.max_turns = floor
    return {"artifact_contract": contract}


def extension(node: TaskNode) -> dict:
    """What the lead grants a contract step at its limit by running it again: one more
    difficulty's worth of tool calls past what it has used, and cost in proportion."""
    contract = node.artifact_contract
    more = MIN_TOOL_CALLS.get(node.difficulty, MIN_TOOL_CALLS["normal"])
    used = max(node.contract_turns, node.contract_tool_calls)
    per_call = contract.max_cost_usd / contract.max_turns
    return {"max_turns": used + more,
            "max_cost_usd": round(max(contract.max_cost_usd, node.contract_cost_usd) + per_call * more, 6)}


def extend(node: TaskNode) -> dict:
    granted = extension(node)
    node.artifact_contract.max_turns = granted["max_turns"]
    node.artifact_contract.max_cost_usd = granted["max_cost_usd"]
    return granted


def safe_name(name: str) -> bool:
    # Database files inside VDBs are data, so do not use the review policy's .db exclusion.
    from .quality import PRIVATE_DIRS, PRIVATE_NAMES

    path = PurePosixPath(name)
    return bool(name.strip() and not path.is_absolute() and path.parts and name == path.as_posix()
                and not any(part in {"..", ".", ".git", ".super-partner"}
                            or part.lower() in PRIVATE_DIRS | PRIVATE_NAMES
                            or part.lower().startswith(".env")
                            or Path(part).stem.lower() in PRIVATE_NAMES
                            or Path(part).suffix.lower() in {".pem", ".p12", ".pfx"}
                            for part in path.parts)
                and not any(char in name for char in "*?[]\\\n\r\0"))


def stages(node: TaskNode) -> set[str]:
    """Recognize explicit jobs, not arbitrary mentions of coverage in a software request."""
    text = node.title.lower()
    found = set()
    production = r"采集|生成|收集|collect|generat|capture|simulat"
    # "Generate a report from VDB" names an input, not another production stage.
    # Keep phrases that actually produce it ("from RTL generate VDB") and always
    # respect declared VDB writes below; input wording cannot conceal a real output.
    vdb_work = re.sub(
        r"(?:\bfrom\b|\busing\b|从|基于|使用|读取|消费)([^,;。；\n]{0,40}?)vdb",
        lambda match: match.group(0) if re.search(production, match.group(1)) else "", text,
    )
    if any(path.rstrip("/").endswith(".vdb") for path in node.files) or (
        "vdb" in vdb_work and re.search(production, vdb_work)
    ):
        found.add("coverage_vdb")
    # Tool names in helper/document titles do not make those tasks artifact production.
    # Explicit contracts still apply, and any real report output keeps the normal check.
    suffixes = {PurePosixPath(path).suffix.lower() for path in node.files}
    preparation = bool(suffixes) and (
        (suffixes <= {".py", ".sh", ".js", ".ts", ".tcl", ".pl"}
         and re.search(r"脚本|解析|验收器|验证器|生成器|parser|script|validator|generator", text))
        or (suffixes <= {".md", ".rst"} and re.search(r"说明|文档|documentation|guide|instructions", text))
    )
    if "urg" in text and re.search(r"报告|文本|report|text", text) and not preparation:
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
        errors.append("coverage_vdb must deliver a VDB directory (kind=directory, path ending in .vdb)")
    if contract.stage == "urg_report":
        if not any(item.kind == "directory" and item.path.endswith(".vdb") for item in contract.inputs):
            errors.append("urg_report must consume a VDB directory (kind=directory, path ending in .vdb)")
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


# Tools write .key files that are not keys (VCS's ucli.key logs simulator commands): a .key
# file is refused for what it holds, not for its name.
KEY_MARKS = (b"PRIVATE KEY", b"BEGIN OPENSSH", b"BEGIN PGP PRIVATE")


def _not_a_key(file: Path, name: str) -> None:
    if file.suffix.lower() == ".key":
        with file.open("rb") as stream:
            head = stream.read(65536)
        if any(mark in head for mark in KEY_MARKS):
            raise ArtifactBlocked(f"产物像是私钥，不能读取：{name}")


def fingerprint(root: Path, item: Artifact) -> str:
    """Hash bytes and freshness metadata; an empty/missing artifact is never evidence."""
    try:
        path = _path(root, item.path)
        if item.kind == "directory":
            if not path.is_dir():
                raise ArtifactBlocked(f"缺少目录产物：{item.path}")
            files, links = [], []
            for folder, dirs, names in os.walk(path, followlinks=False):
                for name in dirs + names:
                    entry = Path(folder) / name
                    if entry.is_symlink() and path.resolve() in entry.resolve().parents:
                        # Tools link within their own outputs (a VCS database shares files across
                        # tests): such a link is recorded as a link, never followed.
                        links.append(entry)
                        continue
                    _path(root, entry.relative_to(root.resolve()).as_posix())
                files += [Path(folder) / name for name in names if Path(folder) / name not in links]
                if len(files) > 20_000:
                    raise ArtifactBlocked(f"目录产物过大，无法完整核验：{item.path}")
        else:
            files, links = [path], []
        digest, size = hashlib.sha256(), 0
        for link in sorted(links):
            digest.update(f"{link.relative_to(path)}->{os.readlink(link)}".encode())
        for file in sorted(files):
            if not file.is_file():
                raise ArtifactBlocked(f"不是普通文件：{item.path}")
            _not_a_key(file, item.path)
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
    """Revalidate a saved acceptance before a consumer or lead can rely on it; nothing to
    revalidate when the platform does not accept steps (settings.acceptance off)."""
    from .config import settings
    evidence, contract = node.artifact_evidence, node.artifact_contract
    if not contract or not settings.acceptance:
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
    """Each input's fingerprint; a dependency's must match what its producer had accepted. With
    acceptance off (settings.acceptance) nothing was accepted: a finished producer is enough."""
    from .config import settings
    before, found = ancestors(node, run.plan.nodes), {}
    for item in node.artifact_contract.inputs:
        value = fingerprint(workspace(run, node), item)
        if item.source == "dependency":
            owners = producers(item, before)
            if len(owners) != 1 or owners[0].status != NodeStatus.SUCCEEDED:
                raise ArtifactBlocked(f"{item.path} 缺少已完成且已验收的上游步骤")
            producer = owners[0]
            accepted(run, producer, seen)
            if settings.acceptance and producer.artifact_evidence.outputs.get(item.path) != value:
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
    contract = node.artifact_contract
    outputs = ", ".join(item.path for item in contract.outputs)
    used = max(node.contract_turns, node.contract_tool_calls)
    return ("Artifact contract (additional to the original acceptance criteria):\n"
            + contract.model_dump_json(indent=2)
            + f"\nAlready used: {used} of {contract.max_turns} tool calls, ${node.contract_cost_usd:.6f}."
            f"\nWrite every declared output first ({outputs}), then check them with the acceptance "
            "commands. Check only your own outputs: work that other steps own (their testbenches, "
            "simulations, synthesis, reports) is not yours, however useful it looks. "
            "Create your own outputs; they need not exist before you start. Do not expand scope. "
            "Missing prerequisite evidence: preserve logs and end with BLOCKED: <why>. "
            "At a declared limit stop with diagnostic evidence. Audit only accepted artifacts.")
