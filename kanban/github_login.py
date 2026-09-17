from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from uuid import uuid4

from .errors import IntegrationError
from .github_auth import GitHubAuth

CODE_TIMEOUT = 30
LOGIN_TIMEOUT = 15 * 60
CODE_PATTERN = re.compile(r"First copy your one-time code: ([A-Z0-9]{4}-[A-Z0-9]{4})\s*$")
USER_PATTERN = re.compile(r"Logged in as ([A-Za-z0-9][A-Za-z0-9-]*)\s*$")


@dataclass
class LoginAttempt:
    id: str
    state: str = "starting"
    code: str = ""
    message: str = "GitHubへの接続を準備しています。"

    @property
    def active(self) -> bool:
        return self.state in {"starting", "waiting"}


class GitHubLogin:
    """Own one browser login in the Web process; never store CLI output or tokens."""

    def __init__(self, auth: GitHubAuth):
        self.auth = auth
        self._lock = asyncio.Lock()
        self._attempt: LoginAttempt | None = None
        self._task: asyncio.Task | None = None

    def status(self) -> dict:
        if not self._attempt:
            return {"id": "", "state": "idle", "active": False, "code": "", "message": ""}
        return {**asdict(self._attempt), "active": self._attempt.active}

    async def start(self) -> None:
        async with self._lock:
            if self._attempt and self._attempt.active:
                return
            self._attempt = LoginAttempt(id=uuid4().hex)
            self._task = asyncio.create_task(self._run(self._attempt))

    async def _stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._attempt and self._attempt.active:
            self._finish(self._attempt, "cancelled", "接続をキャンセルしました。もう一度開始できます。")

    async def cancel(self, attempt_id: str) -> None:
        async with self._lock:
            # A stale tab must not cancel a newer login.
            if self._attempt and self._attempt.id == attempt_id:
                await self._stop()

    async def close(self) -> None:
        async with self._lock:
            await self._stop()

    async def use_cli(self) -> str:
        async with self._lock:
            await self._stop()
            username = await self.auth.use_cli()
            self._attempt = None
            return username

    async def use_pat(self, token: str) -> None:
        async with self._lock:
            await self._stop()
            self.auth.use_pat(token)
            self._attempt = None

    @staticmethod
    def _finish(attempt: LoginAttempt, state: str, message: str) -> None:
        attempt.state, attempt.message, attempt.code = state, message, ""

    async def _run(self, attempt: LoginAttempt) -> None:
        process = None
        try:
            # Non-interactive gh emits the code and URL and polls GitHub itself.
            # Leave git_protocol unset to preserve the user's existing CLI setting.
            async with asyncio.timeout(CODE_TIMEOUT) as timeout:
                process = await self.auth.cli_process(
                    "auth",
                    "login",
                    "--hostname",
                    "github.com",
                    "--web",
                    "--skip-ssh-key",
                    "--clipboard=false",
                    capture_stderr=True,
                )
                username, failure, output_size = "", "", 0
                while line := await process.stdout.readline():
                    output_size += len(line)
                    if output_size > 64 * 1024:
                        raise IntegrationError("認証の応答を読み取れません。GitHub CLIを更新してください。")
                    text = line.decode(errors="replace")
                    if match := CODE_PATTERN.search(text):
                        if attempt.state == "starting":
                            attempt.code, attempt.state = match[1], "waiting"
                            attempt.message = "GitHubでの認証を待っています。"
                            timeout.reschedule(asyncio.get_running_loop().time() + LOGIN_TIMEOUT)
                    if match := USER_PATTERN.search(text):
                        username = match[1]
                    if "expired_token" in text or "token_expired" in text:
                        failure = "expired"
                    elif "access_denied" in text:
                        failure = "denied"
                code = await process.wait()
                if failure == "expired":
                    raise TimeoutError()
                if code or not username or attempt.state != "waiting":
                    raise IntegrationError(
                        "GitHubで認証が許可されませんでした。もう一度開始できます。"
                        if failure == "denied"
                        else "GitHubへの接続に失敗しました。通信状態とGitHub CLIを確認し、再試行してください。"
                    )
                async with self._lock:
                    # Pin the account reported by this login, even if another terminal switches gh.
                    await self.auth.use_cli(username)
                    self._finish(attempt, "success", f"{username}としてGitHubに接続しました。")
        except TimeoutError:
            waiting = attempt.state == "waiting"
            self._finish(
                attempt,
                "expired" if waiting else "error",
                "認証の有効期限が切れました。もう一度開始してください。"
                if waiting
                else "認証コードを取得できませんでした。通信状態を確認し、再試行してください。",
            )
        except IntegrationError as exc:
            self._finish(attempt, "error", str(exc))
        except (OSError, ValueError):
            self._finish(
                attempt, "error", "認証の応答を読み取れません。GitHub CLIを更新して再試行してください。"
            )
        finally:
            if process and process.returncode is None:
                process.kill()
                # Drain a full pipe as well, so shutdown cannot hang on unread output.
                await process.communicate()
