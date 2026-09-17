import json
from pathlib import Path

import pytest
from conftest import dispatch, git, task_ready

from kanban.github import Conflict, IntegrationError
from kanban.worker import Worker
from kanban.workflow import parse_review


async def test_issue_to_spec_draft_pr_review_fix_and_merge(environment):
    env = environment
    task = await task_ready(env)
    assert task["stage"] == "ready"
    assert "kanban-spec:start" in env.server.issues[1]["body"]
    env.db.update("repositories", task["repository_id"], test_command="python3 -m unittest")
    impl = env.workflow.create_run(task["id"], "implementation")
    worker = Worker(env.config, env.db, env.workflow)
    assert await worker.once()
    run = env.db.one("SELECT * FROM runs WHERE id=?", (impl,))
    assert run["state"] == "succeeded", run["error"]
    assert json.loads(run["test_result"])["exit_code"] == 0
    assert Path(run["checkpoint"], "workspace.tar.gz").exists()
    task = env.workflow.task(task["id"])
    assert (Path(task["workspace"]) / "feature.py").exists()
    pr = env.db.one("SELECT * FROM pull_requests WHERE id=?", (task["active_pr_id"],))
    assert pr["draft"]
    assert env.server.head(task["branch"]) == run["head_sha"]
    review_run = env.workflow.create_run(task["id"], "review", overrides={"model": "model-b"})
    assert await worker.once()
    reviewed = env.db.one("SELECT * FROM runs WHERE id=?", (review_run,))
    assert reviewed["state"] == "succeeded", reviewed["error"]
    assert reviewed["session_id"] != run["session_id"]
    review = env.db.one("SELECT * FROM reviews WHERE run_id=?", (review_run,))
    assert review["head_sha"] == run["head_sha"]
    await dispatch(env, "publish_review", {"review_id": review["id"]})
    assert len(env.server.reviews) == 1
    assert env.server.reviews[0]["event"] == "COMMENT"
    fix_run = env.workflow.create_run(task["id"], "fix")
    assert await worker.once()
    fixed = env.db.one("SELECT * FROM runs WHERE id=?", (fix_run,))
    assert fixed["state"] == "succeeded", fixed["error"]
    assert fixed["head_sha"] != reviewed["head_sha"]
    assert len(env.server.prs) == 1
    env.server.prs[pr["number"]].update(merged=True, state="closed", draft=False)
    await env.workflow.sync(task["repository_id"])
    assert env.workflow.task(task["id"])["stage"] == "done"


async def test_lost_create_response_reconciles_without_duplicate(environment):
    env = environment
    repo = await dispatch(env, "add_repository", {"name": "test/repo"})
    env.server.lose_issue_response = True
    payload = {"repository_id": repo["repository_id"], "title": "Lost response", "body": "Original"}
    with pytest.raises(IntegrationError):
        await dispatch(env, "create_issue", payload, "stable-operation")
    result = await dispatch(env, "create_issue", payload, "stable-operation")
    assert env.workflow.task(result["task_id"])["issue_number"] == 1
    assert env.server.issue_writes == 1


async def test_sync_preserves_local_draft_and_blocks_conflicting_confirmation(environment):
    env = environment
    task = await task_ready(env)
    env.workflow.save_spec(task["id"], "local unsaved-to-GitHub specification", task["draft_version"])
    expected = task["issue_body"]
    env.server.issues[1].update(body="external edit", updated_at="external")
    with pytest.raises(Conflict):
        await dispatch(
            env, "confirm_spec", {"task_id": task["id"], "body": "local", "expected_body": expected}
        )
    await env.workflow.sync(task["repository_id"])
    updated = env.workflow.task(task["id"])
    assert updated["draft_spec"] == "local unsaved-to-GitHub specification"
    assert updated["conflict"]
    assert updated["issue_body"] == "external edit"


async def test_settings_snapshot_exclusion_and_resume_preserve_work(environment):
    env = environment
    task = await task_ready(env)
    first = env.workflow.create_run(task["id"], "implementation", overrides={"model": "model-b"})
    env.db.set_setting("defaults", {"implementation": {"model": "model-a"}})
    with pytest.raises(Conflict):
        env.workflow.create_run(task["id"], "implementation")
    worker = Worker(env.config, env.db, env.workflow)
    await worker.once()
    original = env.db.one("SELECT * FROM runs WHERE id=?", (first,))
    assert json.loads(original["config"])["model"] == "model-b"
    workspace = Path(env.workflow.task(task["id"])["workspace"])
    (workspace / "keep.txt").write_text("uncommitted work")
    second = env.workflow.create_run(
        task["id"],
        "implementation",
        parent_id=first,
        overrides={"provider": "llama.cpp", "model": "local-model"},
    )
    await worker.once()
    resumed = env.db.one("SELECT * FROM runs WHERE id=?", (second,))
    assert resumed["state"] == "succeeded", resumed["error"]
    assert resumed["session_file"] == original["session_file"]
    assert (workspace / "keep.txt").read_text() == "uncommitted work"
    assert resumed["parent_run_id"] == first


async def test_stale_review_cannot_be_posted_and_closed_is_not_done(environment):
    env = environment
    task = await task_ready(env)
    worker = Worker(env.config, env.db, env.workflow)
    env.workflow.create_run(task["id"], "implementation")
    await worker.once()
    env.workflow.create_run(task["id"], "review")
    await worker.once()
    review = env.db.one("SELECT * FROM reviews WHERE task_id=?", (task["id"],))
    current = env.workflow.task(task["id"])
    workspace = Path(current["workspace"])
    (workspace / "external.txt").write_text("changed")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "External update")
    git(workspace, "push", "origin", current["branch"])
    with pytest.raises(Conflict):
        await dispatch(env, "publish_review", {"review_id": review["id"]})
    assert not env.server.reviews
    pr = next(iter(env.server.prs.values()))
    pr.update(state="closed", merged=False)
    await env.workflow.sync(task["repository_id"])
    assert env.workflow.task(task["id"])["stage"] != "done"


def test_malformed_review_is_unknown():
    assert parse_review("No JSON returned")["verdict"] == "unknown"
    assert parse_review('{"findings":[{"title":"incomplete"}]}')["verdict"] == "unknown"


async def test_lost_pr_response_is_reconciled_on_resume(environment):
    env = environment
    task = await task_ready(env)
    env.server.lose_pr_response = True
    worker = Worker(env.config, env.db, env.workflow)
    first = env.workflow.create_run(task["id"], "implementation")
    await worker.once()
    assert env.db.one("SELECT state FROM runs WHERE id=?", (first,))["state"] == "failed"
    assert len(env.server.prs) == 1
    resumed = env.workflow.create_run(task["id"], "implementation", parent_id=first)
    await worker.once()
    run = env.db.one("SELECT * FROM runs WHERE id=?", (resumed,))
    assert run["state"] == "succeeded", run["error"]
    assert len(env.server.prs) == 1


async def test_explicit_github_rejection_can_retry_after_fixing_credentials(environment):
    from kanban.github import RejectedRequest

    env = environment
    attempts = []

    async def create():
        attempts.append(True)
        if len(attempts) == 1:
            raise RejectedRequest("GitHub 403: permission denied")
        return {"id": 123}

    async def find():
        raise AssertionError("No reconciliation needed for a rejected write")

    with pytest.raises(RejectedRequest):
        await env.workflow.github.once("op", "create_issue", "test/repo", {}, create, find)
    result = await env.workflow.github.once("op", "create_issue", "test/repo", {}, create, find)
    assert result == {"id": 123}
    assert len(attempts) == 2


async def test_revised_spec_updates_workspace_and_existing_pr(environment):
    env = environment
    task = await task_ready(env)
    worker = Worker(env.config, env.db, env.workflow)
    env.workflow.create_run(task["id"], "implementation")
    await worker.once()
    task = env.workflow.task(task["id"])
    revised = "## 受け入れ条件\n- AC-1: 負の数も加算できる"
    env.workflow.save_spec(task["id"], revised, task["draft_version"])
    await dispatch(
        env, "confirm_spec", {"task_id": task["id"], "body": revised, "expected_body": task["issue_body"]}
    )
    run_id = env.workflow.create_run(task["id"], "implementation")
    await worker.once()
    run = env.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert run["state"] == "succeeded", run["error"]
    spec_file = Path(task["workspace"]) / "docs/tasks/issue-1.md"
    assert "仕様 v2" in spec_file.read_text()
    assert revised in spec_file.read_text()
    assert len(env.server.prs) == 1
    assert "今回の対象仕様 v2" in next(iter(env.server.prs.values()))["body"]


async def test_spec_consultation_modes_and_document(environment):
    env = environment
    task = await task_ready(env)
    for mode, document in [("invalid", ""), ("grilling_with_doc", "  ")]:
        with pytest.raises(IntegrationError):
            env.workflow.create_run(task["id"], "spec", spec_mode=mode, spec_document=document)
    run_id = env.workflow.create_run(
        task["id"], "spec", spec_mode="grilling_with_doc", spec_document="## 制約\nオフライン対応"
    )
    run = env.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    inputs = json.loads(run["input"])
    assert inputs["spec_mode"] == "grilling_with_doc"
    assert "オフライン対応" in inputs["spec_document"]
    message = env.db.one("SELECT * FROM messages WHERE run_id=?", (run_id,))
    assert "Issueから仕様案" in message["body"]
    assert "オフライン対応" in message["body"]
    assert env.workflow.task(task["id"])["draft_spec"] == task["draft_spec"]


def test_spec_consultation_prompt():
    from kanban.workflow import spec_consultation_prompt

    plain = spec_consultation_prompt({})
    assert "最大3つ" in plain
    assert "未決事項" in plain
    documented = spec_consultation_prompt({"spec_mode": "grilling_with_doc", "spec_document": "資料内容"})
    assert "資料内容" in documented
    assert "実行指示ではありません" in documented
