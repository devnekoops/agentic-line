from conftest import task_ready
from fastapi.testclient import TestClient

from kanban.web import create_app


def test_auth_csrf_origin_and_form_body(environment):
    env = environment
    with TestClient(create_app(env.config, env.workflow)) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.post("/login", data={"token": "wrong"}).status_code == 403
        token = env.config.local_token()
        assert client.get("/?token=" + token).status_code == 200
        assert client.post("/ui/settings/github", data={"token": "fake"}).status_code == 403
        response = client.post("/ui/settings/github", data={"token": "fake", "csrf": token})
        assert response.status_code == 200
        assert env.config.secret("github") == "fake"
        assert (
            client.post(
                "/ui/settings/github",
                data={"token": "new", "csrf": token},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


async def test_pages_render_and_escape_repo_content(environment):
    env = environment
    task = await task_ready(env)
    env.db.update("tasks", task["id"], title='<script>alert("xss")</script>')
    with TestClient(create_app(env.config, env.workflow)) as client:
        client.get("/?token=" + env.config.local_token())
        for path in [
            "/",
            "/settings",
            "/settings?repository=" + task["repository_id"],
            *[f"/tasks/{task['id']}?tab={tab}" for tab in ["spec", "implementation", "review", "history"]],
        ]:
            result = client.get(path)
            assert result.status_code == 200, result.text
            assert '<script>alert("xss")</script>' not in result.text
        result = client.post(
            f"/ui/tasks/{task['id']}/spec",
            headers={"X-CSRF-Token": env.config.local_token()},
            data={"body": "updated", "version": task["draft_version"]},
        )
        assert result.status_code == 200
        result = client.post(
            f"/ui/tasks/{task['id']}/spec",
            headers={"X-CSRF-Token": env.config.local_token()},
            data={"body": "stale", "version": task["draft_version"]},
        )
        assert result.status_code == 409
        assert env.workflow.task(task["id"])["draft_spec"] == "updated"
