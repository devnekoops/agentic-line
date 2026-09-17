from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    github_url: str = "https://api.github.com"
    pi_bin: str = "pi"
    pi_agent_dir: Path = Path.home() / ".pi" / "agent"
    sandbox_image: str = "python:3.13-slim"
    testing: bool = False

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            data_dir=Path(
                os.getenv("KANBAN_DATA_DIR", str(Path.home() / ".local/share/kanban"))
            ).expanduser(),
            host=os.getenv("KANBAN_HOST", "127.0.0.1"),
            port=int(os.getenv("KANBAN_PORT", "8765")),
            pi_bin=os.getenv("KANBAN_PI_BIN", "pi"),
            pi_agent_dir=Path(os.getenv("KANBAN_PI_AGENT_DIR", str(Path.home() / ".pi/agent"))).expanduser(),
            sandbox_image=os.getenv("KANBAN_SANDBOX_IMAGE", "python:3.13-slim"),
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("secrets", "repos", "workspaces", "reviews", "sessions", "artifacts", "runtime"):
            (self.data_dir / name).mkdir(exist_ok=True, mode=0o700)

    @property
    def database(self) -> Path:
        return self.data_dir / "kanban.sqlite3"

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def local_token(self) -> str:
        path = self.data_dir / "secrets" / "local-token"
        try:
            with path.open("x") as stream:
                os.chmod(path, 0o600)
                stream.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        return path.read_text().strip()

    def secret(self, name: str) -> str:
        if name not in {"github", "openrouter"}:
            raise ValueError("Unknown secret")
        path = self.data_dir / "secrets" / name
        return path.read_text().strip() if path.exists() else ""

    def save_secret(self, name: str, value: str) -> None:
        if name not in {"github", "openrouter"}:
            raise ValueError("Unknown secret")
        path = self.data_dir / "secrets" / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value.strip())

    def pi_auth_status(self) -> dict[str, str]:
        path = self.pi_agent_dir / "auth.json"
        if not path.exists():
            return {}
        try:
            return {
                key: value.get("type", "unknown")
                for key, value in json.loads(path.read_text()).items()
                if isinstance(value, dict)
            }
        except (ValueError, OSError):
            return {}
