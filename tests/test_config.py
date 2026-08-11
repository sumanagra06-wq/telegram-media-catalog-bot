import pytest

from app.config import Config, ConfigError

BASE_ENV = {
    "BOT_TOKEN": "123456:valid-looking-token",
    "OWNER_IDS": "123456789",
    "FILE_DATABASE_CHANNEL_ID": "-1001111111111",
    "USER_DATABASE_CHANNEL_ID": "-1002222222222",
    "WEBHOOK_BASE_URL": "https://example.up.railway.app",
    "WEBHOOK_PATH": "/telegram/webhook",
    "WEBHOOK_SECRET_TOKEN": "safe_secret-token_123456789",
}


def _set_env(monkeypatch, **overrides):
    for key in (
        "BOT_TOKEN",
        "OWNER_IDS",
        "FILE_DATABASE_CHANNEL_ID",
        "USER_DATABASE_CHANNEL_ID",
        "WEBHOOK_BASE_URL",
        "RAILWAY_PUBLIC_DOMAIN",
        "WEBHOOK_PATH",
        "WEBHOOK_SECRET_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    values = BASE_ENV | overrides
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_valid_config(monkeypatch):
    _set_env(monkeypatch)
    config = Config.from_env()
    assert config.webhook_url == "https://example.up.railway.app/telegram/webhook"
    assert config.owner_ids == frozenset({123456789})


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("OWNER_IDS", "-1", "positive"),
        ("FILE_DATABASE_CHANNEL_ID", "123", "negative"),
        ("WEBHOOK_PATH", "/", "non-root"),
        ("WEBHOOK_SECRET_TOKEN", "has invalid spaces here", "only letters"),
    ],
)
def test_invalid_security_sensitive_values(monkeypatch, key, value, error):
    _set_env(monkeypatch, **{key: value})
    with pytest.raises(ConfigError, match=error):
        Config.from_env()
