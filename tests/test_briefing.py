"""Every step starts from one project brief, kept current, instead of each model reading the project."""
import json
import re

from super_partner import briefing
from super_partner.config import settings
from super_partner.executor import Executor
from super_partner.models import NodeStatus, RunStatus, TaskNode
from super_partner.providers import ProviderResult
from super_partner.routing import brief_writer
from test_leading import MODELS
from test_turns import model


class Writer:
    """Summarizes every file it is sent, and records which."""

    def __init__(self):
        self.summarized, self.overviews = [], 0

    async def __call__(self, prompt):
        if prompt.startswith(briefing.OVERVIEW_PROMPT):
            self.overviews += 1
            return "A small tool. Run tests with: python -m pytest"
        paths = re.findall(r"^=== (.+) ===$", prompt, re.MULTILINE)
        self.summarized += paths
        return json.dumps({path: f"about {path}" for path in paths})


def project(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "README.md").write_text("# Tool\n")
    (tmp_path / "out.log").write_text("noise\n")
    (tmp_path / ".env").write_text("KEY=secret\n")
    return tmp_path


async def test_the_brief_is_written_once_then_only_changed_files_again(tmp_path):
    root, data = project(tmp_path / "p"), tmp_path / "data"
    writer = Writer()
    brief = await briefing.refresh(root, data, writer)
    assert sorted(writer.summarized) == ["README.md", "app.py"]  # no logs, no secrets
    assert brief["overview"].startswith("A small tool")

    (root / "app.py").write_text("def main():\n    return 1\n")
    (root / "README.md").unlink()
    writer.summarized.clear()
    brief = await briefing.refresh(root, data, writer)
    assert writer.summarized == ["app.py"] and set(brief["files"]) == {"app.py"} and writer.overviews == 1

    text = briefing.render(root, brief)
    assert "app.py (2 lines): about app.py" in text and "Run tests with" in text and "KEY=secret" not in text


def test_a_failed_summary_leaves_the_file_listed_without_one(tmp_path):
    root = project(tmp_path)
    text = briefing.render(root, {"files": {}})
    assert "app.py (2 lines)" in text and "Project brief" not in text


def test_a_step_gets_the_files_it_names_and_what_came_before_changed(tmp_path, database):
    root = project(tmp_path)
    (root / "lib").mkdir()
    (root / "lib" / "util.py").write_text("x = 1\n")
    (root / "big.js").write_text("y\n" * 40_000)
    run = database.get_run("r")
    run.plan.nodes[0].changed_files = ["lib/util.py"]
    step = TaskNode(id="b", title="B", instructions="改 app.py 和 big.js，别动 .env", capability="coding",
                    dependencies=[run.plan.nodes[0].id])
    text = briefing.attached_files(root, step, run)
    assert "=== app.py (full text, current) ===\ndef main()" in text and "=== lib/util.py" in text
    assert "big.js" not in text and "secret" not in text  # too long to attach; never secrets


def test_the_brief_is_written_by_the_cheapest_api_model():
    cheap, dear = model("cheap:api", 80), model("dear:api", 95)
    cheap.input_per_million, dear.input_per_million = 0.1, 3.0
    cli = model("cli:claude", 96, "cli")
    assert brief_writer([dear, cheap, cli], {"cheap:api", "dear:api", "cli:claude"}).id == "cheap:api"
    assert brief_writer([cli], {"cli:claude"}) is None


class Team:
    def __init__(self):
        self.prompts, self.summaries = [], []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if prompt.startswith(briefing.SUMMARY_PROMPT):
            self.summaries.append(kwargs.get("effort"))
            paths = re.findall(r"^=== (.+) ===$", prompt, re.MULTILINE)
            return ProviderResult(json.dumps({path: f"about {path}" for path in paths}), 0, 0, model_id)
        if prompt.startswith(briefing.OVERVIEW_PROMPT):
            return ProviderResult("overview", 0, 0, model_id)
        self.prompts.append(prompt)
        return ProviderResult("做完了", 0, 0, model_id)


async def test_each_step_of_a_project_task_starts_from_the_brief(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = project(tmp_path / "proj")
    run = database.get_run("r")
    run.project_dir, run.status = str(root), RunStatus.QUEUED
    run.plan.nodes[0].needs_workspace = True
    run.plan.nodes[0].status = NodeStatus.PENDING
    database.save_run(run)
    team = Team()
    await Executor(database, team, MODELS).execute("r")
    [prompt] = team.prompts
    assert "app.py (2 lines): about app.py" in prompt and team.summaries == ["low"]
