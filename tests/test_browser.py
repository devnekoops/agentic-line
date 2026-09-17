"""Opt-in real browser acceptance test; GitHub and inference stay local/fake."""

import asyncio
import json
import os
import socket
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from kanban.web import create_app
from kanban.worker import Worker

pytestmark = pytest.mark.skipif(os.getenv("KANBAN_BROWSER_TEST") != "1", reason="opt-in browser test")


async def test_browser_issue_spec_implementation_review(environment, fake_gh):
    env = environment
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(env.config, env.workflow),
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    serving = asyncio.create_task(server.serve())
    worker = Worker(env.config, env.db, env.workflow)
    working = asyncio.create_task(worker.serve())
    browser = None
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.05)
        async with async_playwright() as p:
            browser_env = os.environ.copy()
            extra = os.getenv("KANBAN_BROWSER_ENV")
            if extra:
                browser_env.update(json.loads(Path(extra).read_text()))
            browser = await p.chromium.launch(headless=True, env=browser_env)
            page = await browser.new_page(viewport={"width": 1500, "height": 1080})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"http://127.0.0.1:{port}/?token={env.config.local_token()}")
            await page.get_by_role("link", name="接続を設定する →").click()
            await page.get_by_role("button", name="GitHubに接続", exact=True).click()
            await expect(page.locator("#github-login-code")).to_have_value("ABCD-EFGH")
            await page.reload()
            await expect(page.locator("#github-login-code")).to_have_value("ABCD-EFGH")
            folder = Path("test-results")
            folder.mkdir(exist_ok=True)
            await page.screenshot(path=str(folder / "github-browser-login.png"), full_page=True)
            # The popup is a local stand-in: no real GitHub login or credentials are used.
            await page.context.route(
                "https://github.com/login/device",
                lambda route: route.fulfill(
                    body="<h1>GitHub authorization (test)</h1>", content_type="text/html"
                ),
            )
            await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            async with page.expect_popup() as opening:
                await page.get_by_role("link", name="コードをコピーしてGitHubを開く").click()
            popup = await opening.value
            await expect(popup.get_by_role("heading")).to_have_text("GitHub authorization (test)")
            assert popup.url == "https://github.com/login/device"
            await popup.close()
            assert await page.evaluate("navigator.clipboard.readText()") == "ABCD-EFGH"
            fake_gh.state["login_approved"] = True
            fake_gh.save()
            await expect(page.get_by_text("CLI 接続済み", exact=True)).to_be_visible(timeout=10000)
            await expect(page.locator("#github-login-code")).to_have_count(0)
            assert env.db.setting("github_auth")["username"] == "alice"
            # A denied login keeps the previous connection and offers a working retry.
            fake_gh.state.update(login_approved=False, login_mode="access_denied")
            fake_gh.save()
            await page.get_by_role("button", name="GitHubに接続", exact=True).click()
            await expect(
                page.get_by_text("GitHubで認証が許可されませんでした。もう一度開始できます。")
            ).to_be_visible()
            fake_gh.state["login_mode"] = "success"
            fake_gh.save()
            await page.get_by_role("button", name="もう一度GitHubに接続", exact=True).click()
            await expect(page.locator("#github-login-code")).to_have_value("ABCD-EFGH")
            await page.get_by_role("button", name="接続をキャンセル", exact=True).click()
            await expect(page.get_by_text("接続をキャンセルしました。もう一度開始できます。")).to_be_visible()
            await expect(page.locator("#github-login-code")).to_have_count(0)
            await page.get_by_role("button", name="GitHub CLIの認証を使う").click()
            await expect(page.get_by_text("CLI 接続済み", exact=True)).to_be_visible()
            assert env.db.setting("github_auth")["username"] == "alice"
            fake_gh.state["fail"] = True
            fake_gh.save()
            await page.get_by_role("button", name="認証状態を再確認").click()
            await expect(page.locator("#github-auth .badge")).to_have_text("接続が必要")
            await page.get_by_role("button", name="GitHub CLIの認証を使う").click()
            await expect(page.locator("#github-auth .notice.error")).to_be_visible()
            fake_gh.state["fail"] = False
            fake_gh.save()
            await page.get_by_text("アクセストークンを使う", exact=True).click()
            await page.locator("#github-token").fill("browser-test-pat")
            await page.get_by_role("button", name="トークン認証を使う").click()
            await expect(page.locator("#github-auth .badge")).to_have_text("トークン設定済み")
            await page.get_by_role("button", name="GitHub CLIの認証を使う").click()
            await expect(page.get_by_text("CLI 接続済み", exact=True)).to_be_visible()
            assert "fake-cli-token-one" not in await page.content()
            folder = Path("test-results")
            folder.mkdir(exist_ok=True)
            await page.screenshot(path=str(folder / "github-cli-auth.png"), full_page=True)
            await page.locator("#repository-name").fill("test/repo")
            await page.get_by_role("button", name="追加・同期").click()
            await expect(page.get_by_role("button", name="＋ Issueを起票")).to_be_visible(timeout=15000)
            await page.get_by_role("button", name="＋ Issueを起票").click()
            await page.locator("#issue-title").fill("加算機能を実装する")
            await page.locator("#issue-body").fill("2つの値を加算したい。")
            await page.get_by_role("button", name="GitHubにIssueを作成").click()
            await expect(page.locator("#spec-body")).to_be_visible(timeout=15000)
            await page.locator("#spec-body").fill("## 受け入れ条件\n- AC-1: 2つの整数を加算できる")
            await page.get_by_role("button", name="仕様を確定してIssueへ反映 →").click()
            await expect(page.locator(".panel-heading").get_by_text("確定 v1")).to_be_visible(timeout=15000)
            assert env.workflow.task(env.db.one("SELECT id FROM tasks")["id"])["draft_spec"].endswith(
                "加算できる"
            )
            run_form = page.locator(".execution form").filter(has=page.locator("select[name=phase]"))
            await run_form.get_by_role("button", name="この設定で開始 →").click()
            await expect(page.locator("[sse-swap=runtime] .badge").first).to_have_text("完了", timeout=15000)
            await page.get_by_role("link", name="結果を表示・画面を更新 ↻").click()
            await expect(page.get_by_text("変更とテストを完了しました。").first).to_be_visible()
            await page.get_by_role("link", name="レビュー", exact=True).click()
            run_form = page.locator(".execution form").filter(has=page.locator("select[name=phase]"))
            await run_form.locator("select[name=selection]").select_option("openai-codex|model-b")
            await run_form.get_by_role("button", name="この設定で開始 →").click()
            await expect(page.locator("[sse-swap=runtime] .badge").first).to_have_text("完了", timeout=15000)
            await page.get_by_role("link", name="結果を表示・画面を更新 ↻").click()
            await expect(page.get_by_role("heading", name="空配列の扱い")).to_be_visible()
            folder = Path("test-results")
            folder.mkdir(exist_ok=True)
            await page.screenshot(path=str(folder / "review.png"), full_page=True)
            await page.get_by_role("link", name="ボード", exact=False).first.click()
            await expect(page.locator(".task-card")).to_have_count(1)
            await page.screenshot(path=str(folder / "board.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.locator(".task-card").click()
            await page.screenshot(path=str(folder / "mobile.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert errors == []
            await browser.close()
            browser = None
    finally:
        if browser:
            await browser.close()
        server.should_exit = True
        working.cancel()
        await asyncio.gather(working, return_exceptions=True)
        await asyncio.wait_for(serving, 10)
