"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    pass


def _get(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise ConfigError(f"environment variable {name} is required")
    return value


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer") from e


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError as e:
        raise ConfigError(f"{name} must be a number") from e


def _hash_or_none(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return None
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ConfigError(f"{name} must be a SHA-256 hex digest (64 characters)")
    return value


@dataclass(frozen=True)
class CalDAVSettings:
    url: str
    user: str
    password: str
    owner: str
    timezone: ZoneInfo

    @property
    def principal_path(self) -> str:
        return f"/{self.owner}/"

    @classmethod
    def from_env(cls) -> CalDAVSettings:
        tz_name = _get("USER_TIMEZONE", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception as e:
            raise ConfigError(f"USER_TIMEZONE {tz_name!r} is not a valid IANA time zone") from e
        return cls(
            url=_get("CALDAV_URL", "http://radicale:5232"),
            user=_get("CALDAV_USER"),
            password=_get("CALDAV_PASSWORD"),
            owner=_get("CAL_USER"),
            timezone=tz,
        )


@dataclass(frozen=True)
class IngestSettings:
    caldav: CalDAVSettings
    calendar: str
    llm_url: str
    api_key_hash: str
    default_event_minutes: int
    rate_limit_per_hour: int
    data_dir: str
    job_ttl_hours: int
    max_body_bytes: int
    max_text_chars: int
    llm_timeout: int
    all_day_conflicts: bool = False
    # Confidence check and review queue (docs/DESIGN.md §8).
    checker_url: str | None = None
    check_threshold: float = 0.5
    helper_key_hash: str | None = None
    review_ttl_days: int = 7
    review_lease_minutes: int = 10

    @property
    def calendar_path(self) -> str:
        return f"/{self.caldav.owner}/{self.calendar}/"

    @classmethod
    def from_env(cls) -> IngestSettings:
        key_hash = _get("INGEST_API_KEY_HASH").strip().lower()
        if len(key_hash) != 64 or any(c not in "0123456789abcdef" for c in key_hash):
            raise ConfigError("INGEST_API_KEY_HASH must be a SHA-256 hex digest (64 characters)")
        return cls(
            caldav=CalDAVSettings.from_env(),
            calendar=_get("INGEST_CALENDAR", "personal"),
            llm_url=_get("LLM_URL", "http://llm:8080"),
            api_key_hash=key_hash,
            default_event_minutes=_int("DEFAULT_EVENT_MINUTES", 60),
            rate_limit_per_hour=_int("INGEST_RATE_LIMIT_PER_HOUR", 30),
            data_dir=_get("DATA_DIR", "/data"),
            job_ttl_hours=_int("JOB_TTL_HOURS", 24),
            max_body_bytes=_int("MAX_BODY_BYTES", 64 * 1024),
            max_text_chars=_int("MAX_TEXT_CHARS", 20_000),
            llm_timeout=_int("LLM_TIMEOUT_SECONDS", 300),
            all_day_conflicts=os.environ.get("INGEST_ALL_DAY_CONFLICTS", "false").lower() in ("1", "true", "yes"),
            checker_url=os.environ.get("CHECKER_URL") or None,
            check_threshold=_float("CHECK_THRESHOLD", 0.5),
            helper_key_hash=_hash_or_none("HELPER_API_KEY_HASH"),
            review_ttl_days=_int("REVIEW_TTL_DAYS", 7),
            review_lease_minutes=_int("REVIEW_LEASE_MINUTES", 10),
        )


@dataclass(frozen=True)
class WebSettings:
    caldav: CalDAVSettings
    agenda_days: int
    cache_seconds: int
    password_hash: str | None  # optional extra login in the app itself

    @classmethod
    def from_env(cls) -> WebSettings:
        password_hash = os.environ.get("AGENDA_PASSWORD_HASH") or None
        if password_hash and not password_hash.startswith("pbkdf2_sha256$"):
            raise ConfigError("AGENDA_PASSWORD_HASH must come from `python -m calapp.web.hashpw`")
        return cls(
            caldav=CalDAVSettings.from_env(),
            agenda_days=_int("AGENDA_DAYS", 14),
            cache_seconds=_int("AGENDA_CACHE_SECONDS", 60),
            password_hash=password_hash,
        )
