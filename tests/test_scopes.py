"""Steps in one project run at the same time when the files they change do not overlap."""
import asyncio

from super_partner import scopes
from super_partner.config import settings
from super_partner.executor import Executor
from super_partner.models import TaskNode
from super_partner.providers import ProviderResult
from test_leading import MODELS


def test_what_clashes():
    css, js = scopes.Scope(["style.css"]), scopes.Scope(["app.js"], ["style.css"])
    assert not css.clashes(scopes.Scope(["app.py"]))
    assert css.clashes(js)  # one changes what the other reads
    assert not scopes.Scope([], ["a.py"]).clashes(scopes.Scope([], ["a.py"]))  # two readers
    assert scopes.Scope().clashes(scopes.Scope(["x"]))  # no scope: the folder to itself


class Team:
    """Each step writes its file and waits a moment; records how many ran at once."""

    def __init__(self, root):
        self.root, self.now, self.most = root, 0, 0

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.now += 1
        self.most = max(self.most, self.now)
        name = prompt.split("写 ", 1)[1].split()[0]
        (self.root / name).write_text("done\n")
        await asyncio.sleep(0.05)
        self.now -= 1
        return ProviderResult("做完了", 0, 0, model_id)


def project(database, tmp_path, *steps):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("# p\n")
    run = database.get_run("r")
    run.project_dir = str(root)
    run.plan.nodes = [TaskNode(id=key, title=key, instructions=f"写 {name}", capability="coding", model_id="x:api",
                               needs_workspace=True, files=files) for key, name, files in steps]
    database.save_run(run)
    return root


async def test_packages_with_their_own_files_run_together_and_keep_their_own_changes(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = project(database, tmp_path, ("a", "a.css", ["a.css"]), ("b", "b.js", ["b.js"]))
    team = Team(root)
    await Executor(database, team, MODELS).execute("r")
    result = database.get_run("r")
    assert team.most == 2
    assert [node.changed_files for node in result.plan.nodes] == [["a.css"], ["b.js"]]


async def test_steps_without_a_scope_take_turns(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = project(database, tmp_path, ("a", "a.css", []), ("b", "b.js", ["b.js"]))
    team = Team(root)
    await Executor(database, team, MODELS).execute("r")
    assert team.most == 1
