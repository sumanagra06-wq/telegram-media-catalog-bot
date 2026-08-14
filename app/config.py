from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required deployment configuration is invalid."""


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Small dotenv loader used only for local development; production uses real env vars."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _parse_int(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    owner_ids: frozenset[int]
    file_database_channel_id: int
    user_database_channel_id: int
    webhook_base_url: str
    webhook_path: str
    webhook_secret_token: str
    host: str
    port: int
    log_level: str

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url.rstrip('/')}{self.webhook_path}"

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.owner_ids

    @classmethod
    def from_env(cls) -> Config:
        _load_dotenv()
        owner_raw = _required("OWNER_IDS")
        try:
            owners = frozenset(
                int(value.strip()) for value in owner_raw.split(",") if value.strip()
            )
        except ValueError as exc:
            raise ConfigError("OWNER_IDS must contain comma-separated integer user IDs") from exc
        if not owners:
            raise ConfigError("At least one OWNER_IDS value is required")
        if any(owner_id <= 0 for owner_id in owners):
            raise ConfigError("OWNER_IDS values must be positive Telegram user IDs")

        file_db = _parse_int("FILE_DATABASE_CHANNEL_ID")
        user_db = _parse_int("USER_DATABASE_CHANNEL_ID")
        if file_db >= 0 or user_db >= 0:
            raise ConfigError("Database channel IDs must be negative Telegram channel IDs")
        if file_db == user_db:
            raise ConfigError("File and user database channels must be different")

        base_url = os.getenv("WEBHOOK_BASE_URL", "").strip()
        if not base_url:
            railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
            if railway_domain:
                base_url = f"https://{railway_domain}"
        if not base_url.startswith("https://"):
            raise ConfigError("WEBHOOK_BASE_URL must be an https:// URL")

        path = os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip()
        if not path.startswith("/"):
            path = f"/{path}"
        if path == "/" or ".." in path or not re.fullmatch(r"/[A-Za-z0-9_/-]+", path):
            raise ConfigError("WEBHOOK_PATH must be a non-root URL path using safe characters")
        secret = _required("WEBHOOK_SECRET_TOKEN")
        if not 16 <= len(secret) <= 256:
            raise ConfigError("WEBHOOK_SECRET_TOKEN must contain 16 to 256 characters")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
            raise ConfigError(
                "WEBHOOK_SECRET_TOKEN may contain only letters, digits, underscore and hyphen"
            )

        try:
            port = int(os.getenv("PORT", "8080"))
        except ValueError as exc:
            raise ConfigError("PORT must be an integer") from exc

        return cls(
            bot_token=_required("BOT_TOKEN"),
            owner_ids=owners,
            file_database_channel_id=file_db,
            user_database_channel_id=user_db,
            webhook_base_url=base_url,
            webhook_path=path,
            webhook_secret_token=secret,
            # Railway containers must listen on every interface; ingress still controls exposure.
            host=os.getenv("HOST", "0.0.0.0"),  # nosec B104
            port=port,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
