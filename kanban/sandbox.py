from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .github import IntegrationError


class Sandbox:
    def __init__(self, name: str, workspace: Path, image: str, readonly: bool = False):
        self.name = "kanban-" + name
        self.workspace, self.image, self.readonly = workspace, image, readonly
        self.started = False

    async def command(self, *args: str, timeout: int = 60) -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise IntegrationError("実行コンテナ: " + err.decode(errors="replace")[-1200:])
        return out.decode(errors="replace")

    async def start(self) -> None:
        runner = Path(__file__).parent / "agent" / "tool_runner.py"
        git_mount = []
        if (self.workspace / ".git").is_file():
            git_mount = [
                "--mount",
                f"type=bind,src={self.workspace.resolve() / '.git'},dst=/workspace/.git,readonly",
            ]
        await self.command(
            "run",
            "--detach",
            "--rm",
            "--name",
            self.name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,size=512m",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--mount",
            f"type=bind,src={self.workspace.resolve()},dst=/workspace"
            + (",readonly" if self.readonly else ""),
            "--mount",
            f"type=bind,src={runner.resolve()},dst=/kanban-tool.py,readonly",
            "--workdir",
            "/workspace",
            *git_mount,
            self.image,
            "python3",
            "-c",
            "import time; time.sleep(86400)",
            timeout=180,
        )
        self.started = True

    async def run(self, command: str, timeout: int = 300) -> dict:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            self.name,
            "python3",
            "/kanban-tool.py",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps({"tool": "bash", "command": command, "timeout": timeout}).encode()
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload), timeout + 10)
        except BaseException:
            proc.kill()
            await proc.wait()
            await self.close()
            raise
        if proc.returncode:
            raise IntegrationError(err.decode(errors="replace")[-1000:])
        return json.loads(out)

    async def close(self) -> None:
        if self.started:
            try:
                await self.command("rm", "--force", self.name)
            except IntegrationError:
                pass
            self.started = False
