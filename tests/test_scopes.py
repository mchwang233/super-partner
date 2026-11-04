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


def project(database, tmp_path, *steps, folders=False):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("# p\n")
    run = database.get_run("r")
    run.project_dir = str(root)
    run.plan.nodes = [TaskNode(id=key, title=key, instructions=f"写 {name}", capability="coding", model_id="x:api",
                               needs_workspace=True, **{"folders" if folders else "files": paths})
                      for key, name, paths in steps]
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


async def test_a_step_waiting_for_the_folder_shows_as_queued_and_its_time_starts_when_it_gets_in(
        database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = project(database, tmp_path, ("a", "a.css", []), ("b", "b.js", []))
    seen = {}

    class Watching(Team):
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            name = prompt.split("写 ", 1)[1].split()[0]
            if name == "a.css":
                await asyncio.sleep(0.05)  # let b reach the folder lock
                other = next(node for node in database.get_run("r").plan.nodes if node.id == "b")
                seen["b"] = (other.status, other.waiting_for_folder, other.started_at)
            return await super().complete(model_id, prompt, workspace, **kwargs)

    await Executor(database, Watching(root), MODELS).execute("r")
    status, waiting, queued_at = seen["b"]
    assert (status, waiting) == ("running", True)
    after = next(node for node in database.get_run("r").plan.nodes if node.id == "b")
    assert not after.waiting_for_folder and after.started_at > queued_at
    kinds = [event.kind for event in database.events("r") if event.data.get("node_id") == "b"]
    assert kinds.index("node.queued") < kinds.index("node.dequeued")


async def test_steps_owning_different_folders_run_together_and_keep_their_own_changes(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = project(database, tmp_path, ("a", "core/a.sv", ["core/"]), ("b", "periph/b.sv", ["periph/"]), folders=True)
    (root / "core").mkdir()
    (root / "periph").mkdir()
    team = Team(root)
    await Executor(database, team, MODELS).execute("r")
    result = database.get_run("r")
    assert team.most == 2
    assert [node.changed_files for node in result.plan.nodes] == [["core/a.sv"], ["periph/b.sv"]]


async def test_steps_owning_nested_folders_take_turns(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = project(database, tmp_path, ("a", "rtl/a.sv", ["rtl/"]), ("b", "rtl/core/b.sv", ["rtl/core/"]), folders=True)
    (root / "rtl" / "core").mkdir(parents=True)
    team = Team(root)
    await Executor(database, team, MODELS).execute("r")
    assert team.most == 1


def test_a_step_owning_folders_is_told_to_write_only_there(database):
    from super_partner import quality
    node = TaskNode(id="a", title="a", instructions="写核", capability="coding", folders=["rtl/core/"])
    prompt, _ = quality.node_prompt(node, database.get_run("r"), 1000)
    assert "Your folders: rtl/core/" in prompt and "only inside yours" in prompt


class Early(Team):
    """a takes a while; b, which waits for a, sets up its testbench in the meantime."""

    def __init__(self, root, database):
        super().__init__(root)
        self.database, self.prompts, self.seen = database, [], {}

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.prompts.append(prompt)
        if "HEAD START" in prompt:
            a = next(node for node in self.database.get_run("r").plan.nodes if node.id == "a")
            self.seen["a"] = a.status
            (self.root / "tb" / "tb.sv").write_text("testbench\n")
            return ProviderResult("搭好了 tb/tb.sv，只差接上 a 的模块", 0, 0, model_id)
        if "写 a.sv" in prompt:
            await asyncio.sleep(0.1)
        return await super().complete(model_id, prompt, workspace, **kwargs)


def waiting_project(database, tmp_path, b_folders):
    root = project(database, tmp_path, ("a", "rtl/a.sv", ["rtl/"]), ("b", "tb/b.sv", b_folders), folders=True)
    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    run = database.get_run("r")
    run.plan.nodes[1].dependencies = ["a"]
    database.save_run(run)
    return root


async def test_a_step_waiting_for_another_starts_early_and_finishes_with_what_it_did(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    monkeypatch.setattr(settings, "head_start", True)
    root = waiting_project(database, tmp_path, ["tb/"])
    team = Early(root, database)
    await Executor(database, team, MODELS).execute("r")
    result = database.get_run("r")
    b = result.plan.nodes[1]
    assert team.seen["a"] == "running"  # b started while a was at work
    assert result.status == "succeeded" and len(b.attempts) == 1  # the early start is not an attempt
    assert not b.preparing and b.head_start.startswith("搭好了")
    final = team.prompts[-1]
    assert "写 tb/b.sv" in final and "HEAD START" not in final and "只差接上 a 的模块" in final
    kinds = [event.kind for event in database.events("r") if event.data.get("node_id") == "b"]
    assert kinds.index("node.head_start") < kinds.index("node.head_started") < kinds.index("node.started")


async def test_a_step_without_its_own_folders_or_files_waits_without_starting_early(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    monkeypatch.setattr(settings, "head_start", True)
    root = waiting_project(database, tmp_path, [])  # it could need the whole folder
    team = Early(root, database)
    await Executor(database, team, MODELS).execute("r")
    assert database.get_run("r").status == "succeeded"
    assert not any("HEAD START" in prompt for prompt in team.prompts) and team.most == 1


async def test_steps_wait_for_their_inputs_unless_starting_early_is_turned_on(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = waiting_project(database, tmp_path, ["tb/"])
    team = Early(root, database)
    await Executor(database, team, MODELS).execute("r")
    assert not any("HEAD START" in prompt for prompt in team.prompts) and team.most == 1


async def test_a_step_whose_inputs_are_in_use_does_not_start_early(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    monkeypatch.setattr(settings, "head_start", True)
    root = waiting_project(database, tmp_path, ["tb/"])
    run = database.get_run("r")
    b = run.plan.nodes[1]
    b.files, b.reads, b.folders = ["tb/b.sv"], ["rtl/a.sv"], []  # it reads what a is writing
    database.save_run(run)
    team = Early(root, database)
    await Executor(database, team, MODELS).execute("r")
    result = database.get_run("r")
    assert result.status == "succeeded" and len(result.plan.nodes[1].attempts) == 1
    # It neither waited for the folder in a head start nor held a place while a worked.
    assert not any("HEAD START" in prompt for prompt in team.prompts) and team.most == 1


async def test_what_steps_alongside_changed_and_what_they_read_is_known_to_each():
    lock = scopes.FolderLock()
    writer = scopes.Scope(["rtl/"], [], "写 RTL")
    reader = scopes.Scope(["tb/"], ["spec/in.txt"], "读规格")
    async with lock.hold(reader):
        async with lock.hold(writer):
            # Only the writer was there with the reader, and the reader does not write its input.
            assert writer.intrusions(["rtl/a.sv", "spec/in.txt", "notes.md"]) == [("spec/in.txt", "读规格")]
            writer.changed = ["rtl/a.sv", "notes.md"]
        # What the writer changed beyond its declared files is not the reader's.
        assert reader.own(["notes.md", "tb/tb.sv"]) == ["tb/tb.sv"]
    third = scopes.Scope(["fw/"], [], "固件")
    third.alongside, reader.alongside = [reader, writer], []
    # With another step there that could have changed it, nobody is blamed.
    assert third.intrusions(["spec/in.txt"]) == []


class Intruding(Team):
    """a also rewrites spec.txt, which b reads, while b is at work; only on its first try."""

    def __init__(self, root):
        super().__init__(root)
        self.tries = 0

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if "写 a.sv" in prompt:
            self.tries += 1
            if self.tries == 1:
                (self.root / "spec.txt").write_text("changed\n")
        return await super().complete(model_id, prompt, workspace, **kwargs)


async def test_a_step_that_changes_what_a_step_alongside_reads_does_not_pass(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    root = project(database, tmp_path, ("a", "a.sv", ["a.sv"]), ("b", "b.sv", ["b.sv"]))
    (root / "spec.txt").write_text("spec\n")
    run = database.get_run("r")
    run.plan.nodes[1].reads = ["spec.txt"]
    database.save_run(run)
    team = Intruding(root)
    await Executor(database, team, MODELS).execute("r")
    result = database.get_run("r")
    a = result.plan.nodes[0]
    assert team.most == 2
    assert "改了自己范围外的文件 spec.txt" in a.attempts[0].reason and "「b」" in a.attempts[0].reason
    assert result.status == "succeeded" and len(a.attempts) == 2  # its retry stayed in its own files
