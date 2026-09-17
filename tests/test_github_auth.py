import asyncio
import json
import os
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from kanban.errors import IntegrationError
from kanban.github import GitHub
from kanban.github_auth import GitHubAuth
from kanban.web import create_app


async def test_cli_selection_is_pinned_refreshes_token_and_keeps_it_out_of_storage(
    environment, fake_gh, monkeypatch
):
    env = environment
    auth = env.workflow.github.auth
    env.config.save_secret("github", "previous-pat")
    monkeypatch.setenv("GH_TOKEN", "ambient-token-must-not-win")
    assert await auth.use_cli() == "alice"
    assert env.db.setting("github_auth") == {"method": "cli", "username": "alice"}
    assert await auth.token() == "fake-cli-token-one"
    fake_gh.state["accounts"] = [
        {"login": "alice", "active": False, "state": "success"},
        {"login": "bob", "active": True, "state": "success"},
    ]
    fake_gh.state["tokens"]["alice"] = "rotated-token"
    fake_gh.save()
    assert await auth.token() == "rotated-token"
    status = await auth.status()
    assert status["connected"] and status["username"] == "alice" and status["cli_username"] == "bob"
    assert env.config.secret("github") == "previous-pat"
    assert "rotated-token" not in json.dumps(env.db.all("SELECT * FROM settings"))
    assert "fake-cli-token" not in json.dumps(status)
    calls = [json.loads(line) for line in fake_gh.calls_path.read_text().splitlines()]
    assert all(call["ambient_token"] is None for call in calls)
    assert all("github.com" in call["args"] for call in calls)


async def test_api_and_git_use_same_cli_identity_and_pat_switch_reaches_existing_instances(
    environment, fake_gh, monkeypatch
):
    env = environment
    seen = []

    async def request(req):
        seen.append(req.headers["Authorization"])
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(request))
    github = GitHub(env.config, env.db, client)
    original = asyncio.create_subprocess_exec
    git_tokens = []

    async def spawn(*args, **kwargs):
        if args[0] == "git":
            git_tokens.append(kwargs["env"].get("KANBAN_GIT_TOKEN"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        await env.workflow.github.auth.use_cli()
        await github.request("GET", "/user")
        await env.workflow.workspaces.git("fetch", "origin", cwd=env.seed)
        assert seen[-1] == "Bearer fake-cli-token-one"
        assert git_tokens[-1] == "fake-cli-token-one"
        env.workflow.github.auth.use_pat("new-pat")
        await github.request("GET", "/user")
        await env.workflow.workspaces.git("fetch", "origin", cwd=env.seed)
        assert seen[-1] == "Bearer new-pat"
        assert git_tokens[-1] == "new-pat"
    finally:
        await client.aclose()


async def test_logout_never_falls_back_and_local_git_remains_available(environment, fake_gh):
    env = environment
    auth = env.workflow.github.auth
    auth.use_pat("saved-pat")
    await auth.use_cli()
    fake_gh.state["tokens"] = {}
    fake_gh.state["accounts"] = []
    fake_gh.save()
    with pytest.raises(IntegrationError, match="再ログイン"):
        await auth.token()
    with pytest.raises(IntegrationError):
        await env.workflow.github.repository("test/repo")
    with pytest.raises(IntegrationError):
        await env.workflow.workspaces.git("fetch", "origin", cwd=env.seed)
    assert await env.workflow.workspaces.git("rev-parse", "HEAD", cwd=env.seed)
    assert not (await auth.status())["connected"]
    auth.use_pat("")
    assert await auth.token() == "saved-pat"


async def test_invalid_cli_status_with_exit_zero_does_not_replace_existing_auth(environment, fake_gh):
    auth = environment.workflow.github.auth
    auth.use_pat("saved-pat")
    fake_gh.state["accounts"][0].update(state="error", error="fake-cli-token-one", token="fake-cli-token-one")
    fake_gh.save()
    with pytest.raises(IntegrationError, match="認証が無効"):
        await auth.use_cli()
    assert await auth.token() == "saved-pat"
    assert "fake-cli-token-one" not in json.dumps(await auth.status())


async def test_missing_binary_and_timeout_are_actionable_and_reap_child(environment, fake_gh, monkeypatch):
    env = environment
    missing = GitHubAuth(replace(env.config, gh_bin=str(env.config.data_dir / "missing-gh")), env.db)
    with pytest.raises(IntegrationError, match="インストール"):
        await missing.use_cli()
    assert "インストール" in (await missing.status())["cli_message"]
    fake_gh.state["sleep"] = True
    fake_gh.save()
    monkeypatch.setattr("kanban.github_auth.CLI_TIMEOUT", 0.2)
    with pytest.raises(IntegrationError, match="時間切れ"):
        await env.workflow.github.auth.use_cli()
    child = json.loads(fake_gh.calls_path.read_text().splitlines()[-1])["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_settings_connect_refresh_error_recovery_and_csrf(environment, fake_gh):
    env = environment
    with TestClient(create_app(env.config, env.workflow)) as client:
        client.get("/?token=" + env.config.local_token())
        headers = {"X-CSRF-Token": env.config.local_token()}
        assert client.post("/ui/settings/github", data={"method": "cli"}).status_code == 403
        response = client.post("/ui/settings/github", data={"method": "cli"}, headers=headers)
        assert response.status_code == 200
        assert "CLI 接続済み" in response.text and "alice" in response.text
        assert "fake-cli-token-one" not in response.text
        assert not env.config.secret("github")
        fake_gh.state["fail"] = True
        fake_gh.save()
        response = client.post("/ui/settings/github", data={"method": "cli"}, headers=headers)
        assert response.status_code == 400
        assert "fake-cli-token-one" not in response.text
        assert 'id="github-auth"' in response.text
        assert "GitHub CLIの認証を使う" in response.text
        assert env.db.setting("github_auth")["method"] == "cli"
        response = client.get("/ui/settings/github")
        assert response.status_code == 200 and "接続が必要" in response.text
        response = client.post(
            "/ui/settings/github", data={"method": "pat", "token": "new-pat"}, headers=headers
        )
        assert response.status_code == 200 and "トークン設定済み" in response.text
        assert "new-pat" not in response.text
