import asyncio
import sqlite3

import pytest
from super_partner.config import settings
from super_partner.db import Database
from super_partner.models import Event, ExecutionRun, RunStatus, TaskPlan


def saved_run(database, run_id="finished", status=RunStatus.SUCCEEDED, project_dir=None):
    run = ExecutionRun(
        id=run_id,
        prompt="Implement a feature",
        status=status,
        plan=TaskPlan(summary="Finished project", nodes=[]),
        budget_usd=1,
        project_dir=str(project_dir) if project_dir else None,
        final_result="Completed output",
    )
    database.save_run(run)
    database.add_event(Event(run_id=run.id, kind="plan.created"))
    database.add_event(Event(run_id=run.id, kind=f"run.{status.value}"))
    return run


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_delete_finished_history_preserves_files_and_other_records(
    task_api, tmp_path, status
):
    api, client = task_api
    project = tmp_path / "project"
    project.mkdir()
    project_file = project / "main.py"
    project_file.write_text("print('keep me')")
    workspace = settings.workspace_root / "finished"
    workspace.mkdir(parents=True)
    artifact = workspace / "result.txt"
    artifact.write_text("keep artifact")
    run = saved_run(api.db, status=status, project_dir=project)
    other = saved_run(api.db, "other")

    response = await client.delete(f"/api/tasks/{run.id}")

    assert response.status_code == 204 and response.content == b""
    assert (await client.get(f"/api/tasks/{run.id}")).status_code == 404
    assert (await client.get(f"/api/tasks/{run.id}/events")).status_code == 404
    assert [item["id"] for item in (await client.get("/api/tasks")).json()] == [other.id]
    assert api.db.events(run.id) == []
    assert len(api.db.events(other.id)) == 2
    assert project_file.read_text() == "print('keep me')"
    assert artifact.read_text() == "keep artifact"
    reopened = Database(settings.db_path)
    try:
        assert reopened.get_run(run.id) is None
        assert reopened.events(run.id) == []
        assert reopened.get_run(other.id) == other
    finally:
        reopened.connection.close()


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.DRAFT,
        RunStatus.AWAITING_CONFIRMATION,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
    ],
)
async def test_delete_rejects_unfinished_runs_without_changing_history(task_api, status):
    api, client = task_api
    run = saved_run(api.db, status=status)
    events = api.db.events(run.id)

    assert (await client.delete(f"/api/tasks/{run.id}")).status_code == 409
    assert api.db.get_run(run.id) == run
    assert api.db.events(run.id) == events


async def test_delete_missing_or_already_deleted_record_returns_404(task_api):
    api, client = task_api
    other = saved_run(api.db, "other")
    assert (await client.delete("/api/tasks/missing")).status_code == 404
    assert (await client.delete(f"/api/tasks/{other.id}")).status_code == 204
    assert (await client.delete(f"/api/tasks/{other.id}")).status_code == 404


async def test_delete_waits_for_cancel_cleanup_and_releases_finished_executor_state(task_api):
    api, client = task_api
    run = saved_run(api.db, status=RunStatus.CANCELLED)
    cleanup = asyncio.Event()
    task = asyncio.create_task(cleanup.wait())
    api.executor.tasks[run.id] = task
    api.executor.locks[run.id] = asyncio.Lock()
    try:
        assert (await client.delete(f"/api/tasks/{run.id}")).status_code == 409
        assert api.db.get_run(run.id) == run
        assert len(api.db.events(run.id)) == 2
    finally:
        cleanup.set()
        await task

    assert (await client.delete(f"/api/tasks/{run.id}")).status_code == 204
    assert run.id not in api.executor.tasks
    assert run.id not in api.executor.locks


async def test_existing_event_stream_closes_after_deletion(task_api):
    api, client = task_api
    run = saved_run(api.db)
    response = await api.task_events(run.id)
    stream = response.body_iterator
    assert "plan.created" in await stream.__anext__()
    assert (await client.delete(f"/api/tasks/{run.id}")).status_code == 204
    # The stream may have already buffered the final event, then must finish without keepalives.
    remaining = [message async for message in stream]
    assert all("keepalive" not in message for message in remaining)


def test_run_and_events_deletion_rolls_back_together(tmp_path):
    database = Database(tmp_path / "atomic.db")
    try:
        run = saved_run(database)
        with database.connection:
            database.connection.execute(
                "CREATE TRIGGER prevent_event_delete BEFORE DELETE ON events "
                "BEGIN SELECT RAISE(ABORT, 'event deletion failed'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="event deletion failed"):
            database.delete_run(run.id)
        assert database.get_run(run.id) == run
        assert len(database.events(run.id)) == 2
    finally:
        database.connection.close()
