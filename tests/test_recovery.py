import json
from pathlib import Path

import pytest
from conftest import FakePi, task_ready

from kanban.db import dump, now
from kanban.github import Conflict
from kanban.worker import Worker


async def test_restart_preserves_uncommitted_work_and_resume_configuration(environment):
    env = environment
    task = await task_ready(env)
    run_id = env.workflow.create_run(task["id"], "implementation", overrides={"model": "model-b"})
    worker = Worker(env.config, env.db, env.workflow)
    await worker.once()
    original = env.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    workspace = Path(env.workflow.task(task["id"])["workspace"])
    (workspace / "unsaved.txt").write_text("work before crash")
    env.db.update("runs", run_id, state="running", finished_at=None)
    env.db.execute("UPDATE jobs SET state='running' WHERE kind='run'")
    await worker.recover()
    recovered = env.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert recovered["state"] == "interrupted"
    assert Path(recovered["checkpoint"], "workspace.tar.gz").exists()
    env.db.set_setting("defaults", {"implementation": {"model": "model-a"}})
    resumed_id = env.workflow.create_run(task["id"], "implementation", parent_id=run_id)
    resumed = env.db.one("SELECT * FROM runs WHERE id=?", (resumed_id,))
    assert json.loads(resumed["config"])["model"] == "model-b"
    await worker.once()
    assert (workspace / "unsaved.txt").read_text() == "work before crash"
    assert (
        env.db.one("SELECT session_file FROM runs WHERE id=?", (resumed_id,))["session_file"]
        == original["session_file"]
    )


async def test_provider_error_keeps_work_without_fallback(environment):
    env = environment
    task = await task_ready(env)

    class LimitedPi(FakePi):
        async def run(self, *args, **kwargs):
            result = await super().run(*args, **kwargs)
            result["error"] = "usage_limit_reached: subscription quota exhausted"
            return result

    env.workflow.pi_factory = LimitedPi
    run_id = env.workflow.create_run(task["id"], "implementation")
    await Worker(env.config, env.db, env.workflow).once()
    run = env.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert run["state"] == "failed"
    assert "usage_limit_reached" in run["error"]
    assert Path(run["checkpoint"], "workspace.tar.gz").exists()
    assert json.loads(run["config"])["provider"] == "openai-codex"
    assert len(env.db.all("SELECT * FROM runs")) == 1


async def test_model_mismatch_fails_before_agent_work(environment):
    env = environment
    task = await task_ready(env)

    class WrongModel(FakePi):
        async def start(self, *args, **kwargs):
            await super().start(*args, **kwargs)
            self.model = "unexpected-model"

    env.workflow.pi_factory = WrongModel
    run_id = env.workflow.create_run(task["id"], "implementation")
    await Worker(env.config, env.db, env.workflow).once()
    run = env.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert run["state"] == "failed"
    assert "異なるモデル" in run["error"]
    assert not (Path(env.workflow.task(task["id"])["workspace"]) / "feature.py").exists()


async def test_cancel_queued_run_and_periodic_sync_deduplication(environment):
    env = environment
    task = await task_ready(env)
    run_id = env.workflow.create_run(task["id"], "implementation")
    env.workflow.stop(run_id)
    worker = Worker(env.config, env.db, env.workflow)
    await worker.once()
    assert env.db.one("SELECT state FROM runs WHERE id=?", (run_id,))["state"] == "cancelled"
    assert not env.server.prs
    worker.schedule_sync()
    worker.schedule_sync()
    assert len(env.db.all("SELECT * FROM jobs WHERE kind='sync'")) == 1


async def test_base_change_invalidates_fix_and_ready_requires_idle(environment):
    env = environment
    task = await task_ready(env)
    worker = Worker(env.config, env.db, env.workflow)
    env.workflow.create_run(task["id"], "implementation")
    await worker.once()
    env.workflow.create_run(task["id"], "review")
    await worker.once()
    pr = env.db.one("SELECT * FROM pull_requests")
    env.db.update("pull_requests", pr["id"], base_sha="new-base")
    with pytest.raises(Conflict):
        env.workflow.create_run(task["id"], "fix")


async def test_sse_replay_returns_only_events_after_cursor(environment):
    from fastapi.testclient import TestClient

    from kanban.web import create_app

    env = environment
    task = await task_ready(env)
    rid = env.workflow.create_run(task["id"], "spec")
    env.db.event(rid, "status", {"message": "first"})
    cursor = env.db.one("SELECT max(seq) AS seq FROM events")["seq"]
    env.db.event(rid, "status", {"message": "second"})
    env.db.update("runs", rid, state="succeeded", finished_at=now(), result="ok", usage=dump({}))
    with TestClient(create_app(env.config, env.workflow)) as client:
        client.get("/?token=" + env.config.local_token())
        response = client.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": str(cursor)})
        assert "first" not in response.text
        assert "second" in response.text
        assert "event: done" in response.text


async def test_missing_workspace_fails_without_using_parent_git_repository(environment):
    import shutil

    env = environment
    task = await task_ready(env)
    worker = Worker(env.config, env.db, env.workflow)
    first = env.workflow.create_run(task["id"], "implementation")
    await worker.once()
    task = env.workflow.task(task["id"])
    shutil.rmtree(task["workspace"])
    second = env.workflow.create_run(task["id"], "implementation", parent_id=first)
    await worker.once()
    run = env.db.one("SELECT * FROM runs WHERE id=?", (second,))
    assert run["state"] == "failed"
    assert "作業フォルダが見つかりません" in run["error"]
    original = env.db.one("SELECT * FROM runs WHERE id=?", (first,))
    assert Path(original["checkpoint"], "workspace.tar.gz").exists()
