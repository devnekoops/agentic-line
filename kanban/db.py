from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def now() -> float:
    return time.time()


def uid() -> str:
    return uuid.uuid4().hex


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repositories (
 id TEXT PRIMARY KEY, github_id INTEGER UNIQUE NOT NULL, full_name TEXT UNIQUE NOT NULL,
 default_branch TEXT NOT NULL, clone_url TEXT NOT NULL, html_url TEXT NOT NULL,
 test_command TEXT NOT NULL DEFAULT '', setup_command TEXT NOT NULL DEFAULT '',
 image TEXT NOT NULL, defaults TEXT NOT NULL DEFAULT '{}',
 last_sync REAL, sync_error TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id),
 issue_id INTEGER UNIQUE NOT NULL, issue_number INTEGER NOT NULL, title TEXT NOT NULL,
 issue_body TEXT NOT NULL DEFAULT '', draft_spec TEXT NOT NULL DEFAULT '', draft_version INTEGER NOT NULL DEFAULT 0,
 issue_updated TEXT NOT NULL, issue_url TEXT NOT NULL, issue_state TEXT NOT NULL DEFAULT 'open',
 stage TEXT NOT NULL DEFAULT 'backlog', position INTEGER NOT NULL DEFAULT 0,
 labels TEXT NOT NULL DEFAULT '[]', defaults TEXT NOT NULL DEFAULT '{}',
 conflict INTEGER NOT NULL DEFAULT 0, spec_id TEXT, active_pr_id TEXT,
 branch TEXT, workspace TEXT, base_sha TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(repository_id, issue_number)
);
CREATE TABLE IF NOT EXISTS specs (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
 body TEXT NOT NULL, body_hash TEXT NOT NULL, issue_updated TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(task_id,revision)
);
CREATE TABLE IF NOT EXISTS pull_requests (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), github_id INTEGER UNIQUE NOT NULL,
 number INTEGER NOT NULL, node_id TEXT NOT NULL, url TEXT NOT NULL, branch TEXT NOT NULL,
 base_sha TEXT NOT NULL, head_sha TEXT NOT NULL, state TEXT NOT NULL, draft INTEGER NOT NULL,
 merged INTEGER NOT NULL DEFAULT 0, checks TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), phase TEXT NOT NULL,
 state TEXT NOT NULL, config TEXT NOT NULL, input TEXT NOT NULL, parent_run_id TEXT REFERENCES runs(id),
 spec_id TEXT, pr_id TEXT, base_sha TEXT, head_sha TEXT, session_file TEXT, session_id TEXT,
 start_entry TEXT, end_entry TEXT, result TEXT NOT NULL DEFAULT '', error TEXT,
 usage TEXT NOT NULL DEFAULT '{}', test_result TEXT, stop_requested INTEGER NOT NULL DEFAULT 0,
 checkpoint TEXT, pid INTEGER, started_at REAL, finished_at REAL, created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(task_id)
 WHERE state IN ('queued','running','waiting_input','stopping');
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
 kind TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT,
 role TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT NOT NULL REFERENCES runs(id),
 spec_id TEXT NOT NULL, pr_id TEXT NOT NULL, base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
 summary TEXT NOT NULL, raw TEXT NOT NULL, verdict TEXT NOT NULL, posted_id INTEGER,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
 id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), severity TEXT NOT NULL,
 title TEXT NOT NULL, file TEXT NOT NULL DEFAULT '', line INTEGER, body TEXT NOT NULL,
 suggestion TEXT NOT NULL DEFAULT '', disposition TEXT NOT NULL DEFAULT 'accept', reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', dedupe_key TEXT UNIQUE NOT NULL,
 owner TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, result TEXT, created_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, request TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', result TEXT, error TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_state (
 id INTEGER PRIMARY KEY CHECK (id=1), owner TEXT NOT NULL, pid INTEGER NOT NULL, heartbeat REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def execute(self, sql: str, args: tuple = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, args)

    def insert(self, table: str, **values: Any) -> str:
        # Table and column names are internal constants; only values come from requests.
        with self.connect() as conn:
            keys = ",".join(values)
            conn.execute(
                f"INSERT INTO {table} ({keys}) VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
        return values.get("id", "")

    def update(self, table: str, record_id: str, **values: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                f"UPDATE {table} SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), record_id),
            )

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dump(value)),
        )

    def enqueue(self, kind: str, payload: dict, dedupe_key: str | None = None) -> str:
        job_id = uid()
        key = dedupe_key or job_id
        with self.connect(immediate=True) as conn:
            row = conn.execute("SELECT id FROM jobs WHERE dedupe_key=?", (key,)).fetchone()
            if row:
                return row["id"]
            conn.execute(
                "INSERT INTO jobs(id,kind,payload,dedupe_key,created_at) VALUES(?,?,?,?,?)",
                (job_id, kind, dump(payload), key, now()),
            )
        return job_id

    def claim(self, owner: str) -> dict | None:
        with self.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                (owner, now() + 30, row["id"]),
            )
            return dict(row) | {"owner": owner, "state": "running"}

    def event(self, run_id: str, kind: str, data: Any) -> None:
        self.insert("events", run_id=run_id, kind=kind, data=dump(data), created_at=now())
