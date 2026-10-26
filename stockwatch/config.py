"""Application configuration. Secrets are supplied by the environment."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    finnhub_api_key: str = field(repr=False)
    database_path: Path
    allowed_user_ids: frozenset[int]
    cache_seconds: int = 60
    log_level: str = "INFO"
    requests_per_minute: int = 45

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigError("Set TELEGRAM_BOT_TOKEN to the token from BotFather.")
        api_key = values.get("FINNHUB_API_KEY", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", api_key):
            raise ConfigError("Set FINNHUB_API_KEY to your Finnhub API key.")
        path = values.get("DATABASE_PATH", "data/stockwatch-us.sqlite3").strip()
        if not path or path == ":memory:":
            raise ConfigError("DATABASE_PATH must point to a persistent SQLite file.")
        try:
            allowed = frozenset(
                int(part.strip())
                for part in values.get("ALLOWED_USER_IDS", "").split(",")
                if part.strip()
            )
            cache = int(values.get("MARKET_CACHE_SECONDS", "60"))
            requests = int(values.get("MARKET_REQUESTS_PER_MINUTE", "45"))
        except ValueError:
            raise ConfigError("User IDs, MARKET_CACHE_SECONDS and request limit must be integers.") from None
        if any(user_id <= 0 for user_id in allowed):
            raise ConfigError("ALLOWED_USER_IDS must contain positive Telegram user IDs.")
        if not 15 <= cache <= 3600:
            raise ConfigError("MARKET_CACHE_SECONDS must be between 15 and 3600.")
        if not 1 <= requests <= 50:
            raise ConfigError("MARKET_REQUESTS_PER_MINUTE must be between 1 and 50.")
        level = values.get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR.")
        return cls(token, api_key, Path(path).expanduser().resolve(), allowed, cache, level, requests)
