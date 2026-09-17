"""Runs only inside the task container; no application credentials are mounted."""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path("/workspace")
MAX_OUTPUT = 60000


def target(value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT) or ".git" in path.relative_to(ROOT).parts:
        raise ValueError("作業領域の外や.gitへはアクセスできません。")
    return path


def execute(data):
    kind = data["tool"]
    if kind == "bash":
        process = subprocess.Popen(
            data["command"],
            shell=True,
            executable="/bin/sh",
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output = bytearray()

        def drain():
            while chunk := process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    del output[:-MAX_OUTPUT]

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=min(max(int(data.get("timeout", 120)), 1), 600))
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Also stop background children retaining the output pipe after the shell exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            reader.join(timeout=2)
        return {
            "exit_code": -1 if timed_out else process.returncode,
            "output": bytes(output).decode(errors="replace"),
            "timeout": timed_out,
        }
    path = target(data.get("path", "."))
    if kind == "list":
        return {
            "entries": [
                p.name + ("/" if p.is_dir() else "") for p in sorted(path.iterdir()) if p.name != ".git"
            ][:1000]
        }
    if kind == "read":
        lines = path.read_text().splitlines()
        offset, limit = max(int(data.get("offset", 1)) - 1, 0), min(int(data.get("limit", 300)), 1000)
        return {
            "text": "\n".join(
                f"{n + 1}: {line}" for n, line in enumerate(lines) if offset <= n < offset + limit
            )[:MAX_OUTPUT],
            "total_lines": len(lines),
        }
    if kind == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data["content"])
        return {"written": str(path)}
    if kind == "edit":
        text = path.read_text()
        before = data["old_text"]
        if not before or text.count(before) != 1:
            raise ValueError("old_text must match exactly once")
        path.write_text(text.replace(before, data["new_text"], 1))
        return {"edited": str(path)}
    raise ValueError("Unknown tool")


if __name__ == "__main__":
    try:
        print(json.dumps(execute(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
