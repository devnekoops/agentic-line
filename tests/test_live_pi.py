"""Explicit opt-in: uses the user's Pi login and real Docker; no GitHub writes."""

import json
import os
from pathlib import Path

import pytest
from conftest import task_ready

from kanban.pi import PiClient, available_models
from kanban.sandbox import Sandbox
from kanban.worker import Worker

pytestmark = pytest.mark.skipif(
    os.getenv("KANBAN_LIVE_PI_TEST") != "1", reason="requires explicit live Pi test"
)


async def test_real_codex_implementation_review_and_resume(environment):
    env = environment
    assert env.config.pi_auth_status().get("openai-codex") == "oauth"
    env.workflow.pi_factory, env.workflow.sandbox_factory = PiClient, Sandbox
    env.db.set_setting("models", await available_models(env.config))
    model = os.getenv("KANBAN_LIVE_MODEL", "gpt-5.6-luna")
    env.db.set_setting(
        "defaults",
        {
            phase: {"provider": "openai-codex", "model": model, "max_seconds": 180, "max_tools": 30}
            for phase in ["spec", "implementation", "review"]
        },
    )
    task = await task_ready(env)
    env.db.update(
        "repositories", task["repository_id"], test_command="python3 -m unittest discover -s tests -v"
    )
    worker = Worker(env.config, env.db, env.workflow)
    run_id = env.workflow.create_run(
        task["id"],
        "implementation",
        "calculator.py に整数を2つ受け取って加算する add(a,b) を実装してください。tests/test_calculator.py に標準unittestの正常値と負の数のテストを作り、テストを実行してください。外部依存は不要です。",
    )
    await worker.once()
    run = env.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert run["state"] == "succeeded", run["error"]
    assert json.loads(run["test_result"])["exit_code"] == 0
    workspace = Path(env.workflow.task(task["id"])["workspace"])
    assert (workspace / "calculator.py").exists()
    assert env.db.one("SELECT seq FROM events WHERE run_id=? AND kind='tool_execution_start'", (run_id,))
    review_id = env.workflow.create_run(task["id"], "review")
    await worker.once()
    review_run = env.db.one("SELECT * FROM runs WHERE id=?", (review_id,))
    assert review_run["state"] == "succeeded", review_run["error"]
    assert review_run["session_id"] != run["session_id"]
    review = env.db.one("SELECT * FROM reviews WHERE run_id=?", (review_id,))
    assert review["head_sha"] == run["head_sha"]
    assert review["verdict"] in {"pass", "changes"}, review["raw"]
    resume_id = env.workflow.create_run(
        task["id"],
        "implementation",
        "既存の実装を維持してください。workspace_bashでテストを再実行し、結果だけ報告してください。",
        parent_id=run_id,
    )
    await worker.once()
    resumed = env.db.one("SELECT * FROM runs WHERE id=?", (resume_id,))
    assert resumed["state"] == "succeeded", resumed["error"]
    assert resumed["session_file"] == run["session_file"]
    assert json.loads(resumed["test_result"])["exit_code"] == 0
