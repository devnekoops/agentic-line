from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import Config
from .db import uid
from .github import IntegrationError


class PiClient:
    def __init__(self, config: Config, run_id: str | None = None):
        self.config, self.run_id = config, run_id or uid()
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self.reader: asyncio.Task | None = None
        self.stderr_reader: asyncio.Task | None = None
        self.stderr = ""

    async def start(
        self,
        provider: str | None = None,
        model: str | None = None,
        session_file: str | None = None,
        sandbox: str | None = None,
        readonly: bool = False,
        system_prompt: str = "",
    ) -> None:
        cwd = self.config.data_dir / "runtime" / self.run_id
        cwd.mkdir(parents=True, exist_ok=True)
        args = [
            self.config.pi_bin,
            "--mode",
            "rpc",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if sandbox:
            names = ["read", "list", "bash"] if readonly else ["read", "list", "write", "edit", "bash"]
            args += ["--no-builtin-tools", "--tools", ",".join(f"workspace_{name}" for name in names)]
        else:
            args += ["--no-tools"]
        if provider:
            args += ["--provider", provider]
        if model:
            args += ["--model", model]
        if session_file:
            args += ["--session", session_file]
        elif sandbox:
            args += ["--session-dir", str(self.config.data_dir / "sessions" / self.run_id)]
        else:
            args += ["--no-session"]
        if system_prompt:
            args += ["--system-prompt", system_prompt]
        if sandbox:
            args += ["-e", str(Path(__file__).parent / "agent" / "sandbox.ts")]
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "USER",
                "LANG",
                "LC_ALL",
                "SSL_CERT_FILE",
                "NIX_SSL_CERT_FILE",
                "TERM",
                "OPENROUTER_API_KEY",
                "LLAMA_BASE_URL",
                "LLAMA_API_KEY",
            }
        }
        env.update(
            {
                "PI_CODING_AGENT_DIR": str(self.config.pi_agent_dir),
                "PI_OFFLINE": "1",
                "KANBAN_SANDBOX": sandbox or "",
                "KANBAN_READONLY": "1" if readonly else "0",
            }
        )
        self.process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        self.stderr_reader = asyncio.create_task(self._stderr())
        await self.command("get_state")

    async def _stderr(self) -> None:
        while line := await self.process.stderr.readline():
            self.stderr = (self.stderr + line.decode(errors="replace"))[-4000:]

    async def _read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                request_id = value.get("id")
                if value.get("type") == "response" and request_id in self.pending:
                    future = self.pending.pop(request_id)
                    if not future.done():
                        if value.get("success"):
                            future.set_result(value.get("data", {}))
                        else:
                            future.set_exception(IntegrationError(value.get("error", "Pi command failed")))
                else:
                    await self.events.put(value)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(IntegrationError("Piとの接続が終了しました。"))
            self.pending.clear()
            await self.events.put({"type": "process_exit"})

    async def command(self, kind: str, timeout: int = 30, **values) -> dict:
        if not self.process or self.process.returncode is not None:
            raise IntegrationError("Piが起動していません。")
        request_id = uid()
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.process.stdin.write((json.dumps({"id": request_id, "type": kind, **values}) + "\n").encode())
        await self.process.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def run(
        self,
        prompt: str,
        on_event: Callable[[dict], Awaitable[None]],
        should_stop: Callable[[], bool],
        timeout: int = 1800,
        max_tools: int = 150,
    ) -> dict:
        await self.command("set_auto_retry", enabled=True)
        await self.command("prompt", message=prompt)
        started = asyncio.get_running_loop().time()
        tool_count, stopped = 0, False
        while True:
            if (
                should_stop()
                or asyncio.get_running_loop().time() - started > timeout
                or tool_count >= max_tools
            ):
                stopped = True
                await self.command("clear_queue")
                await self.command("abort", timeout=20)
                break
            try:
                event = await asyncio.wait_for(self.events.get(), 0.3)
            except TimeoutError:
                continue
            if event.get("type") == "process_exit":
                raise IntegrationError("Piが予期せず終了しました。セッションと変更は保存されています。")
            if event.get("type") == "tool_execution_end":
                tool_count += 1
            await on_event(event)
            if event.get("type") == "agent_settled":
                break
        state = await self.command("get_state")
        messages = await self.command("get_messages")
        last = await self.command("get_last_assistant_text")
        stats = await self.command("get_session_stats")
        entries = await self.command("get_entries")
        errors = [
            m.get("errorMessage", "")
            for m in messages.get("messages", [])
            if m.get("role") == "assistant" and m.get("stopReason") == "error"
        ]
        last_message = next(
            (m for m in reversed(messages.get("messages", [])) if m.get("role") == "assistant"), {}
        )
        return {
            "text": last.get("text") or "",
            "state": state,
            "stats": stats,
            "leaf_id": entries.get("leafId"),
            "stopped": stopped,
            "error": errors[-1] if errors and last_message.get("stopReason") == "error" else None,
        }

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                await asyncio.wait_for(self.process.wait(), 5)
            except (ProcessLookupError, TimeoutError):
                if self.process.returncode is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def available_models(config: Config) -> list[dict]:
    pi = PiClient(config)
    try:
        await pi.start()
        result = await pi.command("get_available_models")
        return result.get("models", [])
    finally:
        await pi.close()
