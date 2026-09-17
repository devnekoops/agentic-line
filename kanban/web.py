from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import bleach
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from .config import Config
from .db import Database, dump, now, uid
from .github import Conflict, IntegrationError
from .github_login import GitHubLogin
from .workflow import PHASES, RUN_STATES, STAGES, TERMINAL, Workflow, spec_text

ROOT = Path(__file__).parent
markdown = MarkdownIt("commonmark", {"html": False, "linkify": False})


def render_markdown(text: str) -> Markup:
    return Markup(
        bleach.clean(
            markdown.render(text or ""),
            tags=[
                "p",
                "br",
                "strong",
                "em",
                "code",
                "pre",
                "ul",
                "ol",
                "li",
                "blockquote",
                "h1",
                "h2",
                "h3",
                "h4",
                "hr",
                "a",
            ],
            attributes={"a": ["href", "title"]},
            protocols=["https", "http", "mailto"],
        )
    )


def create_app(config: Config | None = None, workflow: Workflow | None = None) -> FastAPI:
    config = config or Config.from_env()
    config.prepare()
    db = workflow.db if workflow else Database(config.database)
    workflow = workflow or Workflow(config, db)
    github_login = GitHubLogin(workflow.github.auth)
    token = config.local_token()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await github_login.close()
            await workflow.github.close()

    app = FastAPI(title="Agentic Line", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config, app.state.db, app.state.workflow = config, db, workflow
    app.state.github_login = github_login
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["markdown"] = render_markdown
    templates.env.filters["fromjson"] = lambda value: json.loads(value or "{}")
    templates.env.filters["date"] = lambda value: (
        datetime.fromtimestamp(value).strftime("%m/%d %H:%M") if value else "—"
    )
    templates.env.globals.update(stages=STAGES, phases=PHASES, run_states=RUN_STATES, uid=uid)

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.url.hostname
        allowed = {"127.0.0.1", "localhost", "::1", config.host}
        if config.testing:
            allowed.add("testserver")
        if host not in allowed:
            return HTMLResponse("Unknown host", status_code=403)
        if request.url.path == "/" and request.query_params.get("token"):
            if hmac.compare_digest(request.query_params["token"], token):
                response = RedirectResponse("/", status_code=303)
                response.set_cookie(
                    "kanban_session", token, httponly=True, samesite="strict", max_age=86400 * 30
                )
                response.headers["Referrer-Policy"] = "no-referrer"
                return response
        public = request.url.path.startswith("/static/") or request.url.path == "/login"
        if not public and not hmac.compare_digest(request.cookies.get("kanban_session", ""), token):
            return (
                RedirectResponse("/login", status_code=303)
                if not request.url.path.startswith("/api/")
                else JSONResponse({"detail": "Login required"}, 401)
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return HTMLResponse("Originが一致しません。", status_code=403)
            if not public:
                csrf = request.headers.get("x-csrf-token", "")
                if not csrf and "application/x-www-form-urlencoded" in request.headers.get(
                    "content-type", ""
                ):
                    await (
                        request.body()
                    )  # Keep the body replayable for the endpoint after middleware parsing.
                    csrf = (await request.form()).get("csrf", "")
                if not hmac.compare_digest(str(csrf), token):
                    return HTMLResponse("画面を再読み込みして操作してください。", status_code=403)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Cache-Control": "no-store",
            }
        )
        return response

    def context(request: Request, **values):
        worker = db.one("SELECT * FROM worker_state WHERE id=1")
        return {
            "request": request,
            "csrf": token,
            "repositories": db.all("SELECT * FROM repositories ORDER BY full_name"),
            "worker_online": bool(worker and now() - worker["heartbeat"] < 20),
            "models": db.setting("models", []),
            **values,
        }

    def page(request: Request, name: str, **values):
        return templates.TemplateResponse(request=request, name=name, context=context(request, **values))

    def notice(request: Request, text: str, error=False, status=200):
        return templates.TemplateResponse(
            request=request, name="notice.html", context={"message": text, "error": error}, status_code=status
        )

    @app.exception_handler(IntegrationError)
    async def integration_error(request: Request, exc: IntegrationError):
        status = 409 if isinstance(exc, Conflict) else 400
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=status)
        return notice(request, str(exc), error=True, status=status)

    async def data(request: Request) -> dict:
        if "application/json" in request.headers.get("content-type", ""):
            try:
                values = await request.json()
            except ValueError as exc:
                raise IntegrationError("JSONの形式を確認してください。") from exc
        else:
            values = dict(await request.form())
        if not isinstance(values, dict):
            raise IntegrationError("入力はオブジェクト形式で指定してください。")
        for key, value in values.items():
            if key == "finding_ids":
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise IntegrationError("finding_idsはIDの配列で指定してください。")
            elif not isinstance(value, (str, int, float, bool)) and value is not None:
                raise IntegrationError(f"{key}の形式を確認してください。")
        return values

    def number(value, label: str) -> int:
        try:
            return int(value)
        except (ValueError, TypeError) as exc:
            raise IntegrationError(f"{label}は整数で指定してください。") from exc

    def model_choice(value: str) -> tuple[str, str]:
        parts = str(value).split("|", 1)
        if len(parts) != 2 or not all(parts):
            raise IntegrationError("モデルを一覧から選択してください。")
        return parts[0], parts[1]

    def queued(request: Request, job_id: str):
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if request.url.path.startswith("/api/"):
            return JSONResponse({"job_id": job_id}, status_code=202)
        return page(request, "job.html", job=job)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return page(request, "login.html")

    @app.post("/login")
    async def login(request: Request):
        values = await data(request)
        if not hmac.compare_digest(str(values.get("token", "")), token):
            return notice(request, "ログイントークンが一致しません。", True, 403)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("kanban_session", token, httponly=True, samesite="strict", max_age=86400 * 30)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def board(request: Request, repository: str = "", q: str = "", filter: str = "all"):
        repos = db.all("SELECT * FROM repositories ORDER BY created_at")
        selected = workflow.repo(repository) if repository else (repos[0] if repos else None)
        tasks = (
            db.all(
                "SELECT t.*,r.state AS run_state,r.phase AS run_phase,r.config AS run_config,p.number AS pr_number,"
                "p.url AS pr_url,p.state AS pr_state,p.merged AS pr_merged FROM tasks t "
                "LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE task_id=t.id ORDER BY created_at DESC LIMIT 1) "
                "LEFT JOIN pull_requests p ON p.id=t.active_pr_id WHERE t.repository_id=? ORDER BY t.position,t.updated_at DESC",
                (selected["id"],),
            )
            if selected
            else []
        )
        if q:
            tasks = [
                t
                for t in tasks
                if q.lower() in (t["title"] + " " + str(t["issue_number"]) + " " + t["labels"]).lower()
            ]
        if filter == "active":
            tasks = [t for t in tasks if t["run_state"] in {"queued", "running", "stopping", "waiting_input"}]
        if filter == "attention":
            tasks = [
                t
                for t in tasks
                if t["conflict"] or t["run_state"] in {"failed", "interrupted", "waiting_input"}
            ]
        return page(
            request, "board.html", selected=selected, tasks=tasks, query=q, filter=filter, active="board"
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request, repository: str = ""):
        return page(
            request,
            "settings.html",
            active="settings",
            auth=config.pi_auth_status(),
            github_auth=await workflow.github.auth.status(),
            github_login=github_login.status(),
            defaults=db.setting("defaults", {}),
            selected=workflow.repo(repository) if repository else None,
            config=config,
            models_updated=db.setting("models_updated"),
        )

    @app.post("/ui/settings/github")
    async def github_settings(request: Request):
        values = await data(request)
        method = values.get("method", "pat")
        error = False
        try:
            if method == "cli":
                username = await github_login.use_cli()
                message = f"GitHub CLIの認証（{username}）を使う設定にしました。"
            elif method == "pat":
                await github_login.use_pat(str(values.get("token", "")))
                message = "GitHubのトークン認証を設定しました。"
            else:
                raise IntegrationError("GitHubの認証方式が不正です。")
        except IntegrationError as exc:
            error, message = True, str(exc)
        response = page(
            request,
            "github_auth.html",
            github_auth=await workflow.github.auth.status(),
            github_login=github_login.status(),
            auth_message=message,
            auth_error=error,
        )
        response.status_code = 400 if error else 200
        return response

    @app.get("/ui/settings/github", response_class=HTMLResponse)
    async def github_auth_status(request: Request):
        return page(
            request,
            "github_auth.html",
            github_auth=await workflow.github.auth.status(),
            github_login=github_login.status(),
        )

    @app.post("/ui/settings/github/login")
    async def github_login_start(request: Request):
        await github_login.start()
        return page(request, "github_login.html", github_login=github_login.status())

    @app.get("/ui/settings/github/login/{attempt_id}", response_class=HTMLResponse)
    async def github_login_status(request: Request, attempt_id: str):
        status = github_login.status()
        response = page(request, "github_login.html", github_login=status)
        if not status["active"] or status["id"] != attempt_id:
            response.headers["HX-Trigger"] = "github-auth-changed"
        return response

    @app.post("/ui/settings/github/login/{attempt_id}/cancel")
    async def github_login_cancel(request: Request, attempt_id: str):
        await github_login.cancel(attempt_id)
        return page(request, "github_login.html", github_login=github_login.status())

    @app.post("/ui/settings/models")
    async def refresh_models(request: Request):
        return queued(request, db.enqueue("models", {}, f"models:{uid()}"))

    @app.post("/ui/settings/defaults")
    async def save_defaults(request: Request):
        values = await data(request)
        defaults = {}
        for phase in ("spec", "implementation", "review"):
            value = str(values.get(phase, ""))
            if value:
                provider, model = model_choice(value)
                defaults[phase] = {
                    "provider": provider,
                    "model": model,
                    "instruction": str(values.get(f"{phase}_instruction", "")),
                }
        repo_id = values.get("repository_id")
        task_id = values.get("task_id")
        if task_id:
            workflow.task(task_id)
            db.update("tasks", task_id, defaults=dump(defaults))
        elif repo_id:
            workflow.repo(repo_id)
            db.update(
                "repositories",
                repo_id,
                defaults=dump(defaults),
                test_command=str(values.get("test_command", "")),
                setup_command=str(values.get("setup_command", "")),
                image=str(values.get("image") or config.sandbox_image),
            )
        else:
            db.set_setting("defaults", defaults)
        return notice(request, "設定を保存しました。次の実行から反映します。")

    @app.post("/api/repositories")
    @app.post("/ui/repositories")
    async def add_repository(request: Request):
        values = await data(request)
        return queued(
            request, db.enqueue("add_repository", {"name": values.get("name", "")}, values.get("key"))
        )

    @app.post("/api/repositories/{repo_id}/sync")
    @app.post("/ui/repositories/{repo_id}/sync")
    async def sync_repository(repo_id: str, request: Request):
        workflow.repo(repo_id)
        return queued(request, db.enqueue("sync", {"repository_id": repo_id}))

    @app.get("/api/repositories")
    async def get_repositories():
        return db.all("SELECT * FROM repositories ORDER BY full_name")

    @app.post("/api/tasks")
    @app.post("/ui/tasks")
    async def create_task(request: Request):
        values = await data(request)
        workflow.repo(values.get("repository_id", ""))
        title = str(values.get("title", "")).strip()
        if not title or len(title) > 256:
            raise IntegrationError("タイトルは1〜256文字で入力してください。")
        return queued(
            request,
            db.enqueue(
                "create_issue",
                {
                    "repository_id": values["repository_id"],
                    "title": title,
                    "body": str(values.get("body", "")),
                },
                values.get("key"),
            ),
        )

    def task_context(task_id: str) -> dict:
        task = workflow.task(task_id)
        repo = workflow.repo(task["repository_id"])
        runs = db.all("SELECT * FROM runs WHERE task_id=? ORDER BY created_at DESC", (task_id,))
        current = runs[0] if runs else None
        pr = (
            db.one("SELECT * FROM pull_requests WHERE id=?", (task["active_pr_id"],))
            if task["active_pr_id"]
            else None
        )
        reviews = db.all("SELECT * FROM reviews WHERE task_id=? ORDER BY created_at DESC", (task_id,))
        for review in reviews:
            review["findings"] = db.all("SELECT * FROM findings WHERE review_id=?", (review["id"],))
            review["stale"] = (
                task["spec_id"] != review["spec_id"]
                or not pr
                or pr["id"] != review["pr_id"]
                or pr["head_sha"] != review["head_sha"]
                or pr["base_sha"] != review["base_sha"]
            )
        events = (
            db.all("SELECT * FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 60", (current["id"],))
            if current
            else []
        )
        return {
            "task": task,
            "selected": repo,
            "runs": runs,
            "current": current,
            "pr": pr,
            "reviews": reviews,
            "specs": db.all("SELECT * FROM specs WHERE task_id=? ORDER BY revision DESC", (task_id,)),
            "messages": db.all("SELECT * FROM messages WHERE task_id=? ORDER BY created_at", (task_id,)),
            "events": list(reversed(events)),
            "active": "board",
        }

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def detail(task_id: str, request: Request, tab: str = "spec"):
        return page(request, "task.html", **task_context(task_id), tab=tab)

    @app.get("/api/tasks/{task_id}")
    async def task_api(task_id: str):
        return task_context(task_id)

    @app.get("/ui/tasks/{task_id}/diff", response_class=HTMLResponse)
    async def task_diff(task_id: str, request: Request):
        task = workflow.task(task_id)
        diff = "実装を開始すると差分が表示されます。"
        if task["workspace"] and Path(task["workspace"]).exists():
            diff = await workflow.workspaces.diff(Path(task["workspace"]), task["base_sha"])
        return page(request, "diff.html", diff=diff)

    @app.post("/ui/tasks/{task_id}/spec")
    async def save_spec(task_id: str, request: Request):
        values = await data(request)
        version = number(values.get("version"), "仕様版")
        workflow.save_spec(task_id, str(values.get("body", "")), version)
        response = notice(request, "仕様案を保存しました。")
        response.headers["HX-Trigger"] = json.dumps({"specSaved": {"version": version + 1}})
        return response

    @app.post("/api/tasks/{task_id}/spec-revisions")
    @app.post("/ui/tasks/{task_id}/confirm")
    async def confirm_spec(task_id: str, request: Request):
        values = await data(request)
        task = workflow.task(task_id)
        body = str(values.get("body", task["draft_spec"]))
        if "version" in values and number(values["version"], "仕様版") != task["draft_version"]:
            raise Conflict("仕様案が更新されています。再読み込みして確認してください。")
        if body != task["draft_spec"]:
            workflow.save_spec(task_id, body, task["draft_version"])
        return queued(
            request,
            db.enqueue(
                "confirm_spec",
                {"task_id": task_id, "body": body, "expected_body": task["issue_body"]},
                values.get("key"),
            ),
        )

    @app.post("/ui/tasks/{task_id}/resolve")
    async def resolve(task_id: str, request: Request):
        task = workflow.task(task_id)
        values = await data(request)
        db.update(
            "tasks",
            task_id,
            conflict=0,
            draft_spec=spec_text(task["issue_body"])
            if values.get("choice") == "remote"
            else task["draft_spec"],
            draft_version=task["draft_version"] + 1,
            stage="spec",
            spec_id=None,
        )
        return RedirectResponse(f"/tasks/{task_id}", 303)

    def model_values(values: dict) -> dict:
        result = {
            key: values[key] for key in ("provider", "model", "max_seconds", "max_tools") if values.get(key)
        }
        if values.get("selection"):
            result["provider"], result["model"] = model_choice(values["selection"])
        for key in ("max_seconds", "max_tools"):
            if key in result:
                result[key] = number(result[key], key)
        return result

    @app.post("/api/tasks/{task_id}/runs")
    @app.post("/ui/tasks/{task_id}/runs")
    async def start_run(task_id: str, request: Request):
        values = await data(request)
        phase = values.get("phase", "implementation")
        if phase == "spec" and "body" in values:
            task = workflow.task(task_id)
            if str(values["body"]) != task["draft_spec"]:
                workflow.save_spec(task_id, str(values["body"]), number(values.get("version"), "仕様版"))
        run_id = workflow.create_run(
            task_id,
            phase,
            str(values.get("instruction", "")),
            model_values(values),
            finding_ids=values.get("finding_ids"),
            dedupe_key=values.get("key"),
            spec_mode=str(values["spec_mode"]) if "spec_mode" in values else None,
            spec_document=str(values["spec_document"]) if "spec_document" in values else None,
        )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"run_id": run_id}, 202)
        return HTMLResponse(
            "",
            headers={
                "HX-Redirect": f"/tasks/{task_id}?tab={'spec' if phase == 'spec' else 'review' if phase == 'review' else 'implementation'}"
            },
        )

    @app.post("/api/runs/{run_id}/stop")
    @app.post("/ui/runs/{run_id}/stop")
    async def stop_run(run_id: str, request: Request):
        workflow.stop(run_id)
        return notice(request, "停止を要求しました。変更の保存が終わるまでお待ちください。")

    @app.post("/api/runs/{run_id}/resume")
    @app.post("/ui/runs/{run_id}/resume")
    async def resume_run(run_id: str, request: Request):
        values = await data(request)
        return queued(
            request,
            workflow.switch(
                run_id, str(values.get("instruction", "")), model_values(values), values.get("key") or uid()
            ),
        )

    @app.post("/ui/findings/{finding_id}")
    async def disposition(finding_id: str, request: Request):
        values = await data(request)
        workflow.set_disposition(finding_id, values.get("disposition", ""), str(values.get("reason", "")))
        return notice(request, "指摘への対応を保存しました。")

    @app.post("/api/reviews/{review_id}/publish")
    @app.post("/ui/reviews/{review_id}/publish")
    async def publish(review_id: str, request: Request):
        return queued(request, db.enqueue("publish_review", {"review_id": review_id}, f"publish:{review_id}"))

    @app.post("/api/tasks/{task_id}/ready-for-review")
    @app.post("/ui/tasks/{task_id}/ready")
    async def ready(task_id: str, request: Request):
        values = await data(request)
        if values.get("confirmed") != "yes":
            raise IntegrationError("PRの内容を確認したことにチェックしてください。")
        return queued(
            request,
            db.enqueue(
                "ready", {"task_id": task_id, "head_sha": values.get("head_sha", "")}, values.get("key")
            ),
        )

    @app.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
    async def job_status(job_id: str, request: Request):
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise HTTPException(404)
        response = page(request, "job.html", job=job)
        if job["state"] == "succeeded":
            result = json.loads(job["result"] or "{}")
            if result.get("task_id"):
                response.headers["HX-Redirect"] = f"/tasks/{result['task_id']}"
            elif result.get("repository_id"):
                response.headers["HX-Redirect"] = f"/?repository={result['repository_id']}"
            elif "count" in result:
                response.headers["HX-Redirect"] = "/settings"
        return response

    @app.get("/api/jobs/{job_id}")
    async def job_api(job_id: str):
        return db.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    @app.post("/ui/jobs/{job_id}/retry")
    async def retry_job(job_id: str, request: Request):
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job or job["state"] not in {"failed", "interrupted"}:
            raise Conflict("再確認できるジョブではありません。")
        if job["kind"] == "run":
            raise Conflict("エージェントの実行はタスク画面の「続ける」から再開してください。")
        db.update("jobs", job_id, state="queued", error=None)
        return queued(request, job_id)

    @app.get("/ui/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request):
        workflow.task(task_id)

        async def stream():
            last = None
            while not await request.is_disconnected():
                values = task_context(task_id)
                template = templates.env.get_template("runtime.html")
                html = template.render(context(request, **values))
                if html != last:
                    yield "event: runtime\ndata: " + html.replace("\n", "\ndata: ") + "\n\n"
                    last = html
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: int = 0):
        if not db.one("SELECT id FROM runs WHERE id=?", (run_id,)):
            raise HTTPException(404)
        cursor = max(after, number(request.headers.get("last-event-id", "0"), "イベントID"))

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                events = db.all(
                    "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200", (run_id, cursor)
                )
                for event in events:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {event['data']}\n\n"
                state = db.one("SELECT state FROM runs WHERE id=?", (run_id,))["state"]
                if state in TERMINAL and len(events) < 200:
                    yield f"event: done\ndata: {dump({'state': state})}\n\n"
                    break
                if not events:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
