from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    NodeAttempt,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)

ORIGIN = {"origin": "http://localhost:5173"}


def scored(model_id: str) -> ModelProfile:
    return ModelProfile(id=model_id, provider="x", display_name=model_id, context_window=200_000,
                        input_per_million=1, output_per_million=1,
                        scores=CapabilityScore(**{key: 90 for key in CapabilityScore.model_fields}))


def failed_run(status=RunStatus.FAILED) -> ExecutionRun:
    nodes = [
        TaskNode(id="a", title="A", instructions="a", capability="coding", model_id="x:one",
                 status=NodeStatus.SUCCEEDED, result="done"),
        TaskNode(id="b", title="B", instructions="b", capability="coding", model_id="x:gone",
                 dependencies=["a"], status=NodeStatus.FAILED, failed_attempts=3, estimated_cost_usd=.5,
                 attempts=[NodeAttempt(model_id="x:gone", outcome="error", reason="node --check needs approval")]),
        TaskNode(id="c", title="C", instructions="c", capability="coding", model_id="x:one",
                 dependencies=["b"], estimated_cost_usd=.25),
    ]
    return ExecutionRun(id="r1", prompt="p", status=status, budget_usd=1, actual_cost_usd=.2,
                        rework_rounds=2, error="1 task node(s) failed", use_cli=False,
                        plan=TaskPlan(summary="s", nodes=nodes))


async def test_failed_task_resumes_from_the_failed_node(task_api, monkeypatch):
    api, client = task_api
    started = []
    monkeypatch.setattr(api.executor, "start", started.append)
    monkeypatch.setattr(api, "models", {m.id: m for m in [scored("x:one")]})
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: set(ids))
    api.db.save_run(failed_run())

    response = await client.post("/api/tasks/r1/resume", json={"budget_usd": 2}, headers=ORIGIN)

    assert response.status_code == 200, response.text
    run = api.db.get_run("r1")
    a, b, c = run.plan.nodes
    assert run.status == RunStatus.QUEUED and started == ["r1"]
    assert (run.budget_usd, run.rework_rounds, run.error) == (2, 0, None)
    assert a.status == NodeStatus.SUCCEEDED and a.result == "done"
    assert b.status == NodeStatus.PENDING and b.failed_attempts == 0
    assert b.feedback == "node --check needs approval" and len(b.attempts) == 1
    # Its model is gone, so it was routed again.
    assert b.model_id == "x:one" and c.status == NodeStatus.PENDING
    assert [e.kind for e in api.db.events("r1")][-1] == "run.resumed"


async def test_resume_checks_status_and_budget(task_api, monkeypatch):
    api, client = task_api
    monkeypatch.setattr(api.executor, "start", lambda run_id: None)
    monkeypatch.setattr(api, "models", {m.id: m for m in [scored("x:one"), scored("x:gone")]})
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: set(ids))
    api.db.save_run(failed_run())
    low = await client.post("/api/tasks/r1/resume", json={"budget_usd": .5}, headers=ORIGIN)
    assert low.status_code == 409 and "needed to finish" in low.json()["detail"]
    assert api.db.get_run("r1").status == RunStatus.FAILED

    cancelled = failed_run(RunStatus.CANCELLED)
    cancelled.id = "r2"
    api.db.save_run(cancelled)
    assert (await client.post("/api/tasks/r2/resume", json={}, headers=ORIGIN)).status_code == 200

    done = failed_run(RunStatus.SUCCEEDED)
    done.id = "r3"
    api.db.save_run(done)
    assert (await client.post("/api/tasks/r3/resume", json={}, headers=ORIGIN)).status_code == 409


async def test_timeline_lists_task_events_in_order(task_api):
    from super_partner.models import Event
    api, client = task_api
    api.db.save_run(failed_run())
    api.db.add_event(Event(run_id="r1", kind="node.started", data={"node_id": "b", "attempt": 1}))
    api.db.add_event(Event(run_id="r1", kind="node.failed", data={"node_id": "b", "error": "boom"}))
    events = (await client.get("/api/tasks/r1/timeline")).json()
    assert [(e["kind"], e["data"]["node_id"]) for e in events] == [("node.started", "b"), ("node.failed", "b")]
    assert events[0]["created_at"] and events[0]["id"] < events[1]["id"]
    later = (await client.get("/api/tasks/r1/timeline", params={"after": events[0]["id"]})).json()
    assert [e["kind"] for e in later] == ["node.failed"]
    assert (await client.get("/api/tasks/missing/timeline")).status_code == 404
