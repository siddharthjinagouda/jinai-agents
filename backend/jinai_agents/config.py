"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("JINAI_DATABASE_URL", "sqlite:///./jinai.db"))
    data_dir: Path = field(default_factory=lambda: Path(_env("JINAI_DATA_DIR", "./jinai-data")).resolve())
    admin_token: str = field(default_factory=lambda: _env("JINAI_ADMIN_TOKEN"))
    secret_key: str = field(default_factory=lambda: _env("JINAI_SECRET_KEY"))  # Fernet key for agent secrets
    public_url: str = field(default_factory=lambda: _env("JINAI_PUBLIC_URL", "http://localhost:8300"))
    # URL the sandboxed agent uses to reach this server (AI calls). For Docker use host.docker.internal.
    internal_url: str = field(default_factory=lambda: _env("JINAI_INTERNAL_URL", "http://127.0.0.1:8300"))

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    builder_model: str = field(default_factory=lambda: _env("JINAI_BUILDER_MODEL", "claude-sonnet-5-5"))
    agent_model: str = field(default_factory=lambda: _env("JINAI_AGENT_MODEL", "claude-sonnet-5-5"))
    # USD per million tokens, used for cost tracking only. Check current prices and set these.
    price_in: float = field(default_factory=lambda: float(_env("JINAI_PRICE_IN_PER_MTOK", "3")))
    price_out: float = field(default_factory=lambda: float(_env("JINAI_PRICE_OUT_PER_MTOK", "15")))

    # Runner
    runner: str = field(default_factory=lambda: _env("JINAI_RUNNER", "process"))  # process | docker
    runner_image: str = field(default_factory=lambda: _env("JINAI_RUNNER_IMAGE", "jinai-runner:latest"))
    workers: int = field(default_factory=lambda: int(_env("JINAI_WORKERS", "4")))
    scheduler_enabled: bool = field(default_factory=lambda: _env("JINAI_SCHEDULER", "1") == "1")

    # Builder / repair limits
    builder_max_turns: int = field(default_factory=lambda: int(_env("JINAI_BUILDER_MAX_TURNS", "30")))
    repairs_per_day: int = field(default_factory=lambda: int(_env("JINAI_REPAIRS_PER_DAY", "3")))

    # Email (alerts and notify()). Without SMTP, messages are kept in the run record only.
    smtp_host: str = field(default_factory=lambda: _env("JINAI_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("JINAI_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("JINAI_SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("JINAI_SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("JINAI_SMTP_FROM", "agents@jinai.local"))
    owner_email: str = field(default_factory=lambda: _env("JINAI_OWNER_EMAIL"))

    web_dir: Path = field(default_factory=lambda: Path(_env("JINAI_WEB_DIR", str(Path(__file__).resolve().parents[2] / "web" / "out"))))


settings = Settings()


def reload() -> Settings:
    global settings
    settings = Settings()
    return settings


SANDBOX_LIB = Path(__file__).resolve().parent / "sandbox"
