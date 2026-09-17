from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kanban.config import Config
from kanban.db import Database, uid
from kanban.github import GitHub
from kanban.workflow import Workflow


def git(cwd: Path, *args: str) -> str:
    return (
        subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


class GitHubServer:
    def __init__(self, remote: Path):
        self.remote = remote
        self.issues, self.prs, self.reviews = {}, {}, []
        self.issue_writes = 0
        self.lose_issue_response = False
        self.lose_pr_response = False
        self.tick = 0

    def head(self, branch="main"):
        return git(self.remote, "rev-parse", branch)

    def request(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        data = json.loads(request.content) if request.content else {}
        self.tick += 1
        if path == "/repos/test/repo":
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "full_name": "test/repo",
                    "default_branch": "main",
                    "clone_url": str(self.remote),
                    "html_url": "https://github.com/test/repo",
                },
            )
        if path.endswith("/issues"):
            if method == "GET":
                return httpx.Response(200, json=list(self.issues.values()))
            self.issue_writes += 1
            number = len(self.issues) + 1
            issue = {
                "id": number + 100,
                "number": number,
                "title": data["title"],
                "body": data["body"],
                "state": "open",
                "updated_at": str(self.tick),
                "labels": [],
                "html_url": f"https://github.com/test/repo/issues/{number}",
            }
            self.issues[number] = issue
            if self.lose_issue_response:
                self.lose_issue_response = False
                raise httpx.ReadError("Response lost after server commit")
            return httpx.Response(201, json=issue)
        if "/issues/" in path:
            issue = self.issues[int(path.split("/")[-1])]
            if method == "PATCH":
                issue.update(data, updated_at=str(self.tick))
            return httpx.Response(200, json=issue)
        if path.endswith("/pulls"):
            if method == "GET":
                return httpx.Response(200, json=list(self.prs.values()))
            number = len(self.prs) + 10
            pr = {
                "id": number + 200,
                "node_id": f"PR_{number}",
                "number": number,
                "title": data["title"],
                "body": data["body"],
                "draft": data["draft"],
                "state": "open",
                "merged": False,
                "html_url": f"https://github.com/test/repo/pull/{number}",
                "head": {"ref": data["head"], "sha": self.head(data["head"])},
                "base": {"sha": self.head(), "ref": "main"},
            }
            self.prs[number] = pr
            if self.lose_pr_response:
                self.lose_pr_response = False
                raise httpx.ReadError("Response lost after PR commit")
            return httpx.Response(201, json=pr)
        if path.endswith("/reviews"):
            if method == "GET":
                return httpx.Response(200, json=self.reviews)
            review = {"id": len(self.reviews) + 400, **data}
            self.reviews.append(review)
            return httpx.Response(201, json=review)
        if "/pulls/" in path:
            pr = self.prs[int(path.split("/")[-1])]
            if method == "PATCH":
                pr.update(data)
            pr["head"]["sha"] = self.head(pr["head"]["ref"])
            pr["base"]["sha"] = self.head()
            return httpx.Response(200, json=pr)
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path == "/graphql":
            pr = next(p for p in self.prs.values() if p["node_id"] == data["variables"]["id"])
            pr["draft"] = False
            return httpx.Response(
                200,
                json={
                    "data": {
                        "markPullRequestReadyForReview": {
                            "pullRequest": {"id": pr["node_id"], "isDraft": False}
                        }
                    }
                },
            )
        return httpx.Response(404, json={"message": f"Unhandled {method} {path}"})


class FakeSandbox:
    workspaces = {}

    def __init__(self, run_id, workspace, image, readonly=False):
        self.name = "kanban-" + run_id
        self.workspaces[self.name] = workspace

    async def start(self):
        pass

    async def run(self, command, timeout=300):
        return {"exit_code": 0, "output": "2 tests passed"}

    async def close(self):
        pass


class FakePi:
    def __init__(self, config, run_id):
        self.config, self.run_id = config, run_id
        self.process = SimpleNamespace(pid=os.getpid())

    async def start(self, provider, model, session_file=None, sandbox=None, **kwargs):
        self.provider, self.model, self.sandbox = provider, model, sandbox
        self.file = session_file or str(self.config.data_dir / "sessions" / (self.run_id + ".jsonl"))
        Path(self.file).touch()
        self.session_id = Path(self.file).stem

    async def command(self, kind, **kwargs):
        if kind == "get_state":
            return {
                "sessionFile": self.file,
                "sessionId": self.session_id,
                "model": {"provider": self.provider, "id": self.model},
            }
        if kind == "get_entries":
            return {"leafId": None}
        return {}

    async def run(self, prompt, on_event, should_stop, timeout, max_tools):
        await on_event({"type": "tool_execution_start", "toolName": "workspace_read"})
        if "最終回答はJSONのみ" in prompt:
            text = json.dumps(
                {
                    "summary": "境界値の確認が必要",
                    "verdict": "changes",
                    "findings": [
                        {
                            "severity": "medium",
                            "title": "空配列の扱い",
                            "file": "feature.py",
                            "line": 1,
                            "body": "入力が空のケースを確認してください。",
                            "suggestion": "テストを追加する",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        elif prompt.startswith("GitHub Issue"):
            workspace = FakeSandbox.workspaces[self.sandbox]
            text = "変更とテストを完了しました。"
            (workspace / "feature.py").write_text(
                "def add(a, b):\n    return a + b\n" + ("# fixed\n" if "修正対象の指摘" in prompt else "")
            )
            (workspace / "untracked.txt").write_text("preserve me")
        else:
            text = "## 受け入れ条件\n- AC-1: 加算できる\n- AC-2: 空入力を扱う"
        await on_event(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}}
        )
        return {
            "text": text,
            "state": await self.command("get_state"),
            "stats": {"tokens": {"input": 10, "output": 20}},
            "leaf_id": "end",
            "stopped": should_stop(),
            "error": None,
        }

    async def close(self):
        pass


@pytest.fixture
def environment(tmp_path):
    origin = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("# Test repository\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "Initial commit")
    git(seed, "push", "origin", "main")
    config = Config(tmp_path / "app", testing=True)
    config.prepare()
    db = Database(config.database)
    server = GitHubServer(origin)
    client = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(server.request)
    )
    gh = GitHub(config, db, client)
    workflow = Workflow(config, db, gh, pi_factory=FakePi, sandbox_factory=FakeSandbox)
    db.set_setting(
        "models",
        [
            {"provider": "openai-codex", "id": "model-a"},
            {"provider": "openai-codex", "id": "model-b"},
            {"provider": "llama.cpp", "id": "local-model"},
        ],
    )
    return SimpleNamespace(config=config, db=db, server=server, workflow=workflow, seed=seed, remote=origin)


async def dispatch(env, kind, payload, job_id=None):
    return await env.workflow.handle({"id": job_id or uid(), "kind": kind, "payload": json.dumps(payload)})


async def task_ready(env):
    repo = await dispatch(env, "add_repository", {"name": "test/repo"})
    created = await dispatch(
        env, "create_issue", {"repository_id": repo["repository_id"], "title": "加算機能", "body": "背景"}
    )
    task = env.workflow.task(created["task_id"])
    env.workflow.save_spec(task["id"], "## 受け入れ条件\n- AC-1: 加算する", 0)
    task = env.workflow.task(task["id"])
    await dispatch(
        env,
        "confirm_spec",
        {"task_id": task["id"], "body": task["draft_spec"], "expected_body": task["issue_body"]},
    )
    return env.workflow.task(task["id"])
