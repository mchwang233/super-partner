"""Temporary artifacts and fake models for contract regressions; never invokes URG."""
from pathlib import Path

from super_partner.cli_channels import CliChannelRegistry
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import TOOL_LIMIT_REQUIREMENTS, ProviderResult


def contract_node(key="vdb", stage="coverage_vdb", inputs=(), output="coverage.vdb", **overrides):
    contract = {"stage": stage, "inputs": list(inputs), "outputs": [{"path": output, "kind": "directory" if output.endswith(".vdb") else "file"}],
                    "acceptance_commands": [f"validate {output}"], "blocked_when": ["required evidence or simulator unavailable"],
                    "max_turns": 3, "max_cost_usd": 1}
    contract.update(overrides.pop("contract", {}))
    return TaskNode(id=key, title=key, instructions=f"Produce {key}", capability="coding",
                    files=[output], reads=[item["path"] for item in inputs], model_id="test:worker",
                    acceptance_criteria="Real evidence satisfies the original requirements.",
                    artifact_contract=contract, **overrides)


def coverage_nodes():
    vdb = contract_node()
    report = contract_node("report", "urg_report", [{"path": "coverage.vdb", "kind": "directory", "source": "dependency"}],
                           "report.txt", dependencies=["vdb"])
    audit = contract_node("audit", "delivery_audit", [{"path": "report.txt", "kind": "file", "source": "dependency"}],
                          "audit.txt", dependencies=["report"], verifies=["report"])
    return [vdb, report, audit]


def raw(node):
    return {**node.model_dump(exclude={"id"}), "key": node.id}


class ArtifactProviders:
    def __init__(self, nodes, work=None):
        self.nodes = {node.id: node for node in nodes}
        self.work, self.calls = work, []
        self.authorized = {node.model_id for node in nodes}
        self.capabilities = {}
        self.actual_calls = []

    def configured_models(self, ids):
        return set(ids) & self.authorized

    def tool_limit_capabilities(self, model_id):
        return self.capabilities.get(model_id, CliChannelRegistry.tool_limit_capabilities(model_id)
                                     if model_id.startswith("cli:") else dict(TOOL_LIMIT_REQUIREMENTS))

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        trace = kwargs.get("tool_trace")
        if not trace:
            return ProviderResult('{"passed": true, "reason": "criteria checked"}', 1, 1, model_id)
        node = self.nodes[trace.stem.rsplit("-", 1)[0]]
        self.calls.append(node.id)
        budget = kwargs["spend_ok"]
        await budget.begin_turn()
        await budget.record(model_id, 1000, 1000)
        if budget.begin_tool:  # API channels count tool calls; a CLI's are not counted
            await budget.begin_tool()
        self.actual_calls.append((node.id, model_id))
        if self.work:
            result = await self.work(node, Path(workspace), kwargs)
            if result is not None:
                return result
        else:
            for item in node.artifact_contract.outputs:
                file = Path(workspace) / item.path
                if item.kind == "directory":
                    file = file / "coverage.db"
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(f"evidence {node.id}\n")
        kwargs["tool_log"].append({"turn": 1, "tool": "Write", "detail": node.files[0], "ok": True})
        return ProviderResult("Delivered\nVERDICT: PASS", 1000, 1000, model_id, turns=1)


def setup_executor(tmp_path, monkeypatch, nodes=None, work=None):
    nodes = coverage_nodes() if nodes is None else nodes
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    database = Database(tmp_path / "artifacts.db")
    database.save_run(ExecutionRun(id="artifacts", prompt="Produce coverage evidence", status=RunStatus.QUEUED,
                                   budget_usd=5, plan=TaskPlan(summary="coverage", nodes=nodes)))
    model = ModelProfile(id="test:worker", provider="test", display_name="test", supports_tools=True,
                         input_per_million=1, output_per_million=1, context_window=64000,
                         scores=CapabilityScore(coding=90))
    providers = ArtifactProviders(nodes, work)
    return Executor(database, providers, {model.id: model}), database, providers


def mock_acceptance(monkeypatch, seen=None, ok=True):
    from super_partner.agent import Workspace

    async def check(self, name, args):
        assert name == "Bash" and not self.host
        if seen is not None:
            seen.append((args["command"], self.network, self.host))
        return "artifact check passed" if ok else "artifact check failed", ok

    monkeypatch.setattr(Workspace, "run", check)
