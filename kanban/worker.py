from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path

from .config import Config
from .db import Database, dump, now, uid
from .workflow import Workflow

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: Config, db: Database | None = None, workflow: Workflow | None = None):
        self.config = config
        self.db = db or Database(config.database)
        self.workflow = workflow or Workflow(config, self.db)
        self.owner = uid()
        self.stopping = asyncio.Event()
        self.current_job: str | None = None
        self.lock = None

    async def heartbeat(self) -> None:
        while not self.stopping.is_set():
            self.db.execute(
                "INSERT INTO worker_state(id,owner,pid,heartbeat) VALUES(1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,pid=excluded.pid,heartbeat=excluded.heartbeat",
                (self.owner, os.getpid(), now()),
            )
            if self.current_job:
                self.db.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=? AND owner=?",
                    (now() + 30, self.current_job, self.owner),
                )
            await asyncio.sleep(5)

    async def recover(self) -> None:
        # The exclusive OS lock proves no previous Worker remains active.
        for run in self.db.all("SELECT * FROM runs WHERE state IN ('running','waiting_input','stopping')"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "--force",
                    f"kanban-{run['id']}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except FileNotFoundError:
                pass
            if run["pid"]:
                # Verify command line before terminating a possibly recycled PID.
                try:
                    command = Path(f"/proc/{run['pid']}/cmdline").read_bytes()
                    cwd = Path(f"/proc/{run['pid']}/cwd").resolve()
                    expected = self.config.data_dir / "runtime" / run["id"]
                    if b"--mode\x00rpc" in command and cwd == expected.resolve():
                        os.killpg(run["pid"], 15)
                except (OSError, ProcessLookupError):
                    pass
            checkpoint = run["checkpoint"]
            try:
                checkpoint = await self.workflow.workspaces.checkpoint(
                    self.workflow.task(run["task_id"]), run["id"]
                )
            except Exception as exc:
                log.warning("Checkpoint recovery failed for %s: %s", run["id"], exc)
            self.db.update(
                "runs",
                run["id"],
                state="interrupted",
                checkpoint=checkpoint,
                error="前回のWorkerが終了しました。状態を確認して再開してください。",
                finished_at=now(),
                pid=None,
            )
        self.db.execute(
            "UPDATE jobs SET state='interrupted',error='前回のWorkerが終了しました。再確認できます。' WHERE state='running'"
        )

    async def once(self) -> bool:
        job = self.db.claim(self.owner)
        if not job:
            return False
        self.current_job = job["id"]
        try:
            result = await self.workflow.handle(job)
            self.db.update(
                "jobs", job["id"], state="succeeded", result=dump(result), error=None, finished_at=now()
            )
        except asyncio.CancelledError:
            self.db.update(
                "jobs", job["id"], state="interrupted", error="Workerを停止しました。", finished_at=now()
            )
            raise
        except Exception as exc:
            log.warning("Job %s (%s): %s", job["id"], job["kind"], exc)
            self.db.update("jobs", job["id"], state="failed", error=str(exc), finished_at=now())
        finally:
            self.current_job = None
        return True

    async def serve(self) -> None:
        self.lock = (self.config.data_dir / "worker.lock").open("w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Workerは既に起動しています。") from exc
        await self.recover()
        heartbeat = asyncio.create_task(self.heartbeat())
        next_sync = 0
        try:
            while not self.stopping.is_set():
                if now() >= next_sync:
                    self.schedule_sync()
                    next_sync = now() + 60
                if not await self.once():
                    await asyncio.sleep(0.4)
        finally:
            self.stopping.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.workflow.github.close()
            self.lock.close()

    def schedule_sync(self) -> None:
        for repo in self.db.all("SELECT id FROM repositories"):
            pending = self.db.one(
                "SELECT id FROM jobs WHERE kind='sync' AND state IN ('queued','running') "
                "AND json_extract(payload,'$.repository_id')=?",
                (repo["id"],),
            )
            if not pending:
                self.db.enqueue("sync", {"repository_id": repo["id"]})


def main() -> None:
    config = Config.from_env()
    config.prepare()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(Worker(config).serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
