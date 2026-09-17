import asyncio
import json
import os
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from kanban.github_auth import GitHubAuth
from kanban.github_login import GitHubLogin
from kanban.web import create_app


async def wait_state(login, *states):
    async with asyncio.timeout(5):
        while login.status()["state"] not in states:
            await asyncio.sleep(0.01)
    return login.status()


def login_calls(fake_gh):
    if not fake_gh.calls_path.exists():
        return []
    return [
        call
        for line in fake_gh.calls_path.read_text().splitlines()
        if (call := json.loads(line))["args"][:2] == ["auth", "login"]
    ]


def assert_reaped(fake_gh):
    for call in login_calls(fake_gh):
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)


async def test_browser_login_reuses_attempt_and_pins_authenticated_user(environment, fake_gh, monkeypatch):
    auth = environment.workflow.github.auth
    auth.use_pat("previous-pat")
    monkeypatch.setenv("GH_TOKEN", "ambient-token-must-not-win")
    login = GitHubLogin(auth)
    try:
        await asyncio.gather(*(login.start() for _ in range(5)))
        pending = await wait_state(login, "waiting")
        assert pending["code"] == "ABCD-EFGH"
        assert await auth.token() == "previous-pat"
        await login.start()
        assert login.status()["id"] == pending["id"]
        assert len(login_calls(fake_gh)) == 1
        fake_gh.state["accounts"] = [
            {"login": "alice", "active": False, "state": "success"},
            {"login": "bob", "active": True, "state": "success"},
        ]
        fake_gh.state["login_approved"] = True
        fake_gh.save()
        result = await wait_state(login, "success")
        assert result["code"] == ""
        assert auth.connection() == {"method": "cli", "username": "alice"}
        assert await auth.token() == "fake-cli-token-one"
        assert "fake-cli-token" not in json.dumps(result)
        stored = json.dumps(environment.db.all("SELECT * FROM settings"))
        assert "fake-cli-token" not in stored and "ABCD-EFGH" not in stored
        assert login_calls(fake_gh)[0]["ambient_token"] is None
    finally:
        await login.close()
    assert_reaped(fake_gh)


@pytest.mark.parametrize(
    "mode,state",
    [("access_denied", "error"), ("expired_token", "expired"), ("failure", "error"), ("oversized", "error")],
)
async def test_failed_login_preserves_auth_and_hides_output(environment, fake_gh, mode, state):
    auth = environment.workflow.github.auth
    auth.use_pat("previous-pat")
    fake_gh.state["login_mode"] = mode
    fake_gh.save()
    login = GitHubLogin(auth)
    try:
        await login.start()
        result = await wait_state(login, state)
        assert not result["code"] and not result["active"]
        assert "fake-cli-token" not in json.dumps(result)
        assert await auth.token() == "previous-pat"
    finally:
        await asyncio.wait_for(login.close(), 3)
    assert_reaped(fake_gh)


@pytest.mark.parametrize("mode,state", [("no-code", "error"), ("success", "expired")])
async def test_login_timeouts_kill_process(environment, fake_gh, monkeypatch, mode, state):
    monkeypatch.setattr("kanban.github_login.CODE_TIMEOUT", 0.2)
    monkeypatch.setattr("kanban.github_login.LOGIN_TIMEOUT", 0.2)
    fake_gh.state["login_mode"] = mode
    fake_gh.save()
    login = GitHubLogin(environment.workflow.github.auth)
    try:
        await login.start()
        await wait_state(login, state)
    finally:
        await login.close()
    assert_reaped(fake_gh)


async def test_cancel_retry_stale_cancel_and_manual_auth_switch(environment, fake_gh):
    login = GitHubLogin(environment.workflow.github.auth)
    try:
        await login.start()
        first = await wait_state(login, "waiting")
        await login.cancel(first["id"])
        assert login.status()["state"] == "cancelled" and not login.status()["code"]
        assert_reaped(fake_gh)
        await login.start()
        second = await wait_state(login, "waiting")
        await login.cancel(first["id"])
        assert second["id"] != first["id"] and login.status()["active"]
        await login.use_pat("manual-pat")
        assert await login.auth.token() == "manual-pat"
        assert not login.status()["active"]
        assert_reaped(fake_gh)
        await login.start()
        await wait_state(login, "waiting")
        assert await login.use_cli() == "alice"
        assert_reaped(fake_gh)
        await login.start()
        await wait_state(login, "waiting")
    finally:
        await login.close()
    assert_reaped(fake_gh)


async def test_missing_cli_is_actionable_and_can_retry(environment, fake_gh):
    auth = GitHubAuth(replace(environment.config, gh_bin="/nonexistent/gh"), environment.db)
    login = GitHubLogin(auth)
    try:
        await login.start()
        result = await wait_state(login, "error")
        assert "インストール" in result["message"]
        auth.config = environment.config
        await login.start()
        await wait_state(login, "waiting")
    finally:
        await login.close()
    assert_reaped(fake_gh)


def test_login_routes_require_auth_csrf_and_origin_and_shutdown_cancels(environment, fake_gh):
    env = environment
    app = create_app(env.config, env.workflow)
    with TestClient(app) as client:
        endpoint = "/ui/settings/github/login"
        assert client.post(endpoint, follow_redirects=False).status_code == 303
        client.get(f"/?token={env.config.local_token()}")
        assert client.post(endpoint).status_code == 403
        headers = {"X-CSRF-Token": env.config.local_token()}
        assert client.post(endpoint, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        assert not login_calls(fake_gh)
        assert client.post(endpoint, headers=headers).status_code == 200
        deadline = time.monotonic() + 5
        while app.state.github_login.status()["state"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        attempt = app.state.github_login.status()
        assert attempt["state"] == "waiting"
        assert "ABCD-EFGH" in client.get("/settings").text
        response = client.get(endpoint + "/" + attempt["id"])
        assert response.headers["Cache-Control"] == "no-store"
        assert "ABCD-EFGH" in response.text and "fake-cli-token" not in response.text
        cancel = endpoint + "/" + attempt["id"] + "/cancel"
        assert client.post(cancel).status_code == 403
        assert len(login_calls(fake_gh)) == 1
    assert_reaped(fake_gh)
