from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .config import Config
from .db import Database, dump, now
from .errors import IntegrationError
from .github_auth import GitHubAuth


class Conflict(IntegrationError):
    pass


class UnknownOutcome(IntegrationError):
    pass


class RejectedRequest(IntegrationError):
    """GitHub explicitly rejected the write; retrying after correction is safe."""


def digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def marker(operation_id: str) -> str:
    return f"<!-- kanban-operation:{operation_id} -->"


class GitHub:
    def __init__(self, config: Config, db: Database, client: httpx.AsyncClient | None = None):
        self.config, self.db = config, db
        self.auth = GitHubAuth(config, db)
        self.client = client or httpx.AsyncClient(
            base_url=config.github_url, timeout=30, follow_redirects=False
        )
        self.owns_client = client is None

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = await self.auth.token()
        if not token and not self.config.testing:
            raise IntegrationError("接続設定でGitHub CLIまたはアクセストークンを設定してください。")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = await self.client.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if method == "GET" and attempt < 2:
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                raise IntegrationError("GitHubに接続できませんでした。通信状態を確認してください。") from exc
            if method == "GET" and response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(0.25 * 2**attempt)
                continue
            if response.is_error:
                try:
                    message = response.json().get("message", "GitHub request failed")
                except ValueError:
                    message = "GitHub request failed"
                if token:
                    message = message.replace(token, "[redacted]")
                error_type = (
                    RejectedRequest
                    if 400 <= response.status_code < 500 and response.status_code != 408
                    else IntegrationError
                )
                raise error_type(f"GitHub {response.status_code}: {message[:400]}")
            return response.json() if response.content else None
        raise IntegrationError("GitHubの取得に失敗しました。")

    async def pages(self, path: str, **params: Any) -> list[dict]:
        result = []
        page = 1
        while True:
            rows = await self.request("GET", path, params=params | {"per_page": 100, "page": page})
            if isinstance(rows, dict):
                rows = rows.get("check_runs", [])
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    async def repository(self, name: str) -> dict:
        return await self.request("GET", f"/repos/{name}")

    async def issue(self, name: str, number: int) -> dict:
        return await self.request("GET", f"/repos/{name}/issues/{number}")

    async def issues(self, name: str) -> list[dict]:
        return [
            row for row in await self.pages(f"/repos/{name}/issues", state="all") if "pull_request" not in row
        ]

    async def pr(self, name: str, number: int) -> dict:
        return await self.request("GET", f"/repos/{name}/pulls/{number}")

    async def once(
        self,
        operation_id: str,
        kind: str,
        scope: str,
        payload: dict,
        create: Callable[[], Awaitable[dict]],
        find: Callable[[], Awaitable[dict | None]],
    ) -> dict:
        saved = self.db.one("SELECT * FROM operations WHERE id=?", (operation_id,))
        if saved and saved["request"] != dump(payload):
            raise Conflict("同じ操作IDに異なる内容が指定されました。")
        if saved and saved["state"] == "done":
            return json.loads(saved["result"])
        if saved and saved["state"] in {"sending", "unknown"}:
            existing = await find()
            if existing:
                self.db.update("operations", operation_id, state="done", result=dump(existing), error=None)
                return existing
            raise UnknownOutcome(
                "前回のGitHub操作の結果を確認できません。再投稿せず、GitHub側を確認してください。"
            )
        if not saved:
            self.db.insert(
                "operations", id=operation_id, kind=kind, scope=scope, request=dump(payload), created_at=now()
            )
        self.db.update("operations", operation_id, state="sending")
        try:
            result = await create()
        except (Conflict, RejectedRequest) as exc:
            self.db.update("operations", operation_id, state="pending", error=str(exc))
            raise
        except Exception as exc:
            self.db.update("operations", operation_id, state="unknown", error=str(exc))
            raise
        self.db.update("operations", operation_id, state="done", result=dump(result), error=None)
        return result

    async def create_issue(self, name: str, title: str, body: str, operation_id: str) -> dict:
        payload = {"title": title, "body": f"{body}\n\n{marker(operation_id)}"}

        async def find() -> dict | None:
            return next(
                (row for row in await self.issues(name) if marker(operation_id) in (row.get("body") or "")),
                None,
            )

        return await self.once(
            operation_id,
            "create_issue",
            name,
            payload,
            lambda: self.request("POST", f"/repos/{name}/issues", json=payload),
            find,
        )

    async def update_spec(
        self, name: str, number: int, spec: str, expected_body: str, operation_id: str
    ) -> dict:
        start, end = "<!-- kanban-spec:start -->", "<!-- kanban-spec:end -->"
        managed = f"{start}\n{spec.strip()}\n{marker(operation_id)}\n{end}"
        body = expected_body
        if start in body and end in body and body.index(start) < body.index(end):
            body = body[: body.index(start)] + managed + body[body.index(end) + len(end) :]
        else:
            body = body.rstrip() + "\n\n" + managed
        payload = {"body": body}

        async def find() -> dict | None:
            current = await self.issue(name, number)
            return current if marker(operation_id) in (current.get("body") or "") else None

        async def update() -> dict:
            current = await self.issue(name, number)
            if (current.get("body") or "") != expected_body:
                # No write happened: reset to pending, so reconciliation won't hide a conflict.
                raise Conflict("IssueがGitHubで更新されています。同期して差分を確認してください。")
            return await self.request("PATCH", f"/repos/{name}/issues/{number}", json=payload)

        return await self.once(operation_id, "update_spec", f"{name}#{number}", payload, update, find)

    async def create_pr(
        self, name: str, base: str, branch: str, title: str, body: str, operation_id: str
    ) -> dict:
        payload = {
            "title": title,
            "body": body + "\n\n" + marker(operation_id),
            "base": base,
            "head": branch,
            "draft": True,
        }

        async def find() -> dict | None:
            rows = await self.pages(
                f"/repos/{name}/pulls", state="all", head=f"{name.split('/')[0]}:{branch}"
            )
            return next((row for row in rows if marker(operation_id) in (row.get("body") or "")), None)

        return await self.once(
            operation_id,
            "create_pr",
            name,
            payload,
            lambda: self.request("POST", f"/repos/{name}/pulls", json=payload),
            find,
        )

    async def update_pr(self, name: str, number: int, report: str) -> dict:
        current = await self.pr(name, number)
        start, end = "<!-- kanban-report:start -->", "<!-- kanban-report:end -->"
        block = f"{start}\n{report}\n{end}"
        body = current.get("body") or ""
        if start in body and end in body and body.index(start) < body.index(end):
            body = body[: body.index(start)] + block + body[body.index(end) + len(end) :]
        else:
            body += "\n\n" + block
        return await self.request("PATCH", f"/repos/{name}/pulls/{number}", json={"body": body})

    async def publish_review(self, name: str, number: int, head: str, body: str, operation_id: str) -> dict:
        current = await self.pr(name, number)
        if current["head"]["sha"] != head:
            raise Conflict("PRに新しいcommitがあります。再レビューしてください。")
        payload = {"commit_id": head, "body": body + "\n\n" + marker(operation_id), "event": "COMMENT"}

        async def find() -> dict | None:
            return next(
                (
                    row
                    for row in await self.pages(f"/repos/{name}/pulls/{number}/reviews")
                    if marker(operation_id) in (row.get("body") or "")
                ),
                None,
            )

        return await self.once(
            operation_id,
            "publish_review",
            f"{name}#{number}",
            payload,
            lambda: self.request("POST", f"/repos/{name}/pulls/{number}/reviews", json=payload),
            find,
        )

    async def ready(self, node_id: str) -> dict:
        result = await self.request(
            "POST",
            "/graphql",
            json={
                "query": "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id isDraft}}}",
                "variables": {"id": node_id},
            },
        )
        if result.get("errors"):
            raise IntegrationError(str(result["errors"][0].get("message", "Draft解除に失敗しました。")))
        return result["data"]["markPullRequestReadyForReview"]["pullRequest"]

    async def checks(self, name: str, head: str) -> list[dict]:
        return await self.pages(f"/repos/{name}/commits/{head}/check-runs")
