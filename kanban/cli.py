from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys

import uvicorn

from .config import Config


def main() -> None:
    parser = argparse.ArgumentParser(description="Agentic Line — local GitHub kanban")
    parser.add_argument("command", nargs="?", choices=["serve", "web", "worker", "url"], default="serve")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.port:
        os.environ["KANBAN_PORT"] = str(args.port)
    config = Config.from_env()
    config.prepare()
    if args.command == "worker":
        from .worker import main as worker_main

        worker_main()
        return
    print(f"ブラウザで開く: {config.origin}/?token={config.local_token()}", flush=True)
    if args.command == "url":
        return
    worker = None
    if args.command == "serve":
        worker = subprocess.Popen([sys.executable, "-m", "kanban.worker"], start_new_session=True)
    try:
        from .web import create_app

        uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)
    finally:
        if worker and worker.poll() is None:
            os.killpg(worker.pid, signal.SIGINT)
            try:
                worker.wait(timeout=25)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=5)


if __name__ == "__main__":
    main()
