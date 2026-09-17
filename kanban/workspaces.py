from __future__ import annotations

import asyncio
import os
import tarfile
from pathlib import Path

from .config import Config
from .github import IntegrationError


class Workspaces:
    def __init__(self, config: Config):
        self.config = config
        self.askpass = config.data_dir / "runtime" / "git-askpass.py"
        self.askpass.write_text(
            "#!/usr/bin/env python3\nimport os,sys\nprint('x-access-token' if 'username' in sys.argv[1].lower() else os.environ.get('KANBAN_GIT_TOKEN',''))\n"
        )
        self.askpass.chmod(0o700)

    async def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(self.askpass),
            "KANBAN_GIT_TOKEN": self.config.secret("github"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Kanban Local",
            "-c",
            "user.email=kanban@localhost",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 180)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if check and proc.returncode:
            message = err.decode(errors="replace")[-1500:]
            token = env["KANBAN_GIT_TOKEN"]
            if token:
                message = message.replace(token, "[redacted]")
            raise IntegrationError(f"Git操作に失敗しました: {message}")
        return out.decode(errors="replace").strip()

    async def clone(self, repo: dict) -> Path:
        path = self.config.data_dir / "repos" / repo["id"]
        if not (path / ".git").exists():
            await self.git("clone", "--no-checkout", "--", repo["clone_url"], str(path))
        await self.git("fetch", "--prune", "origin", cwd=path)
        return path

    async def prepare(self, repo: dict, task: dict) -> tuple[Path, str, str]:
        clone = await self.clone(repo)
        path = self.config.data_dir / "workspaces" / task["id"]
        branch = task.get("branch") or f"kanban/issue-{task['issue_number']}-{task['id'][:8]}"
        if task.get("workspace") and not path.exists():
            raise IntegrationError(
                "作業フォルダが見つかりません。artifactsのチェックポイントを確認し、復元してから再開してください。"
            )
        if not path.exists():
            existing = await self.git("branch", "--list", branch, cwd=clone)
            await self.git("worktree", "prune", cwd=clone)
            if existing:
                await self.git("worktree", "add", str(path), branch, cwd=clone)
            else:
                await self.git(
                    "worktree", "add", "-b", branch, str(path), f"origin/{repo['default_branch']}", cwd=clone
                )
        await self.check_workspace(path)
        base = task.get("base_sha") or await self.git(
            "rev-parse", f"origin/{repo['default_branch']}", cwd=clone
        )
        return path, branch, base

    async def commit(self, path: Path, message: str) -> str:
        await self.check_workspace(path)
        await self.git("add", "--all", cwd=path)
        changes = await self.git("diff", "--cached", "--name-only", cwd=path)
        if changes:
            await self.git("commit", "-m", message, cwd=path)
        return await self.git("rev-parse", "HEAD", cwd=path)

    async def push(self, path: Path, branch: str) -> str:
        await self.check_workspace(path)
        await self.git("push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}", cwd=path)
        return await self.git("rev-parse", "HEAD", cwd=path)

    async def diff(self, path: Path, base: str, head: str | None = None) -> str:
        return await self.git("diff", "--no-ext-diff", base, *([head] if head else []), "--", cwd=path)

    async def review_snapshot(self, repo: dict, task: dict, head: str, run_id: str) -> Path:
        clone = await self.clone(repo)
        target = self.config.data_dir / "reviews" / run_id
        target.mkdir(parents=True, exist_ok=True)
        archive = self.config.data_dir / "runtime" / f"{run_id}.tar"
        await self.git("archive", "--format=tar", f"--output={archive}", head, cwd=clone)
        with tarfile.open(archive) as stream:
            stream.extractall(target, filter="data")
        archive.unlink()
        return target

    async def checkpoint(self, task: dict, run_id: str) -> str | None:
        if not task.get("workspace"):
            return None
        path = Path(task["workspace"])
        if not path.exists():
            return None
        await self.check_workspace(path)
        target = self.config.data_dir / "artifacts" / run_id
        target.mkdir(parents=True, exist_ok=True)
        patch = await self.git("diff", "--binary", "HEAD", cwd=path)
        (target / "changes.patch").write_text(patch)
        # Include ignored and untracked files for exact recovery, but never the worktree's .git pointer.
        with tarfile.open(target / "workspace.tar.gz", "w:gz") as stream:
            for entry in path.iterdir():
                if entry.name != ".git":
                    stream.add(entry, arcname=entry.name, recursive=True)
        (target / "head.txt").write_text(await self.git("rev-parse", "HEAD", cwd=path))
        return str(target)

    async def check_workspace(self, path: Path) -> None:
        if not (path / ".git").is_file():
            raise IntegrationError("作業フォルダのGit管理情報が見つかりません。操作を停止しました。")
        common = await self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
        root = await self.git("rev-parse", "--show-toplevel", cwd=path)
        if (
            not Path(common).resolve().is_relative_to((self.config.data_dir / "repos").resolve())
            or Path(root).resolve() != path.resolve()
        ):
            raise IntegrationError("作業フォルダがアプリの管理リポジトリを参照していません。")
