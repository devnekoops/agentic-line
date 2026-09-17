from __future__ import annotations

import asyncio
import json
import os
import re

from .config import Config
from .db import Database
from .errors import IntegrationError

CLI_TIMEOUT = 10
LOGIN_COMMAND = "gh auth login --hostname github.com --web"


class GitHubAuth:
    """Resolve one explicitly selected identity for both the GitHub API and Git."""

    def __init__(self, config: Config, db: Database):
        self.config, self.db = config, db

    def connection(self) -> dict:
        # Existing installations continue to use their saved token.
        return self.db.setting("github_auth", {"method": "pat"})

    async def _gh(self, *args: str) -> tuple[int, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
                "GH_HOST",
                "GH_DEBUG",
                "DEBUG",
                "GH_FORCE_TTY",
            }
        }
        env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
        try:
            process = await asyncio.create_subprocess_exec(
                self.config.gh_bin,
                *args,
                env=env,
                cwd=self.config.data_dir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise IntegrationError(
                "GitHub CLI (gh)を起動できません。インストールと実行パスを確認してください。"
            ) from exc
        try:
            output, _ = await asyncio.wait_for(process.communicate(), CLI_TIMEOUT)
        except BaseException as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, TimeoutError):
                raise IntegrationError(
                    "GitHub CLIの応答が時間切れになりました。通信・認証情報の保存先を確認して再試行してください。"
                ) from None
            raise
        return process.returncode, output.decode(errors="replace").strip()

    async def _accounts(self) -> list[dict]:
        code, output = await self._gh("auth", "status", "--hostname", "github.com", "--json", "hosts")
        if code:
            raise IntegrationError(
                f"GitHub CLIの認証を確認できません。{LOGIN_COMMAND} でログインしてください。"
            )
        try:
            accounts = json.loads(output)["hosts"].get("github.com", [])
            if not isinstance(accounts, list) or not all(isinstance(a, dict) for a in accounts):
                raise ValueError()
            # Do not expose CLI output, token fields, or error strings to the UI.
            return [
                {
                    "login": a.get("login", ""),
                    "active": a.get("active", False),
                    "state": a.get("state", "error"),
                }
                for a in accounts
            ]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise IntegrationError(
                "GitHub CLIの認証情報を読み取れません。ghを更新して再試行してください。"
            ) from None

    async def _cli_token(self, username: str) -> str:
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", username):
            raise IntegrationError("GitHub CLIの認証を接続設定で選び直してください。")
        code, token = await self._gh("auth", "token", "--hostname", "github.com", "--user", username)
        if code or not token or any(character.isspace() for character in token):
            raise IntegrationError(
                f"GitHub CLIの認証を取得できません。{LOGIN_COMMAND} で再ログインしてください。"
            )
        return token

    async def token(self) -> str:
        connection = self.connection()
        if connection["method"] == "cli":
            return await self._cli_token(connection.get("username", ""))
        if connection["method"] == "pat":
            return self.config.secret("github")
        raise IntegrationError("GitHubの認証方式を接続設定で選び直してください。")

    async def use_cli(self) -> str:
        accounts = await self._accounts()
        active = next((a for a in accounts if a["active"]), None)
        if not active or active["state"] != "success":
            raise IntegrationError(
                f"GitHub CLIは未ログイン、または認証が無効です。{LOGIN_COMMAND} を実行してください。"
            )
        username = active["login"]
        await self._cli_token(username)
        self.db.set_setting("github_auth", {"method": "cli", "username": username})
        return username

    def use_pat(self, token: str) -> None:
        token = token.strip()
        if not token:
            if not self.config.secret("github"):
                raise IntegrationError("GitHubトークンを入力してください。")
        elif any(character.isspace() for character in token):
            raise IntegrationError("GitHubトークンに空白や改行が含まれています。")
        else:
            self.config.save_secret("github", token)
        self.db.set_setting("github_auth", {"method": "pat"})

    async def status(self) -> dict:
        connection = self.connection()
        pat_saved = bool(self.config.secret("github"))
        result = {
            "method": connection["method"],
            "pat_saved": pat_saved,
            "connected": connection["method"] == "pat" and pat_saved,
            "username": connection.get("username", ""),
            "cli_username": "",
            "cli_message": "",
        }
        try:
            accounts = await self._accounts()
            active = next((a for a in accounts if a["active"] and a["state"] == "success"), None)
            if active:
                result["cli_username"] = active["login"]
            if connection["method"] == "cli":
                selected = next((a for a in accounts if a["login"] == connection.get("username")), None)
                result["connected"] = bool(selected and selected["state"] == "success")
                if not result["connected"]:
                    result["cli_message"] = (
                        "選択したアカウントの認証を確認できません。再ログインしてください。"
                    )
            elif not active:
                result["cli_message"] = "GitHub CLIは未ログイン、または認証が無効です。"
        except IntegrationError as exc:
            result["cli_message"] = str(exc)
        return result
