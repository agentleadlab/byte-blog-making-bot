"""Configuration loading: TOML for brand/workflow defaults, env for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "wilbyte.toml"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass
class BrandConfig:
    name: str
    site_domain: str
    canonical_path_prefix: str

    def canonical_link(self, url_slug: str) -> str:
        return f"https://{self.site_domain}{self.canonical_path_prefix}{url_slug}"


@dataclass
class PostConfig:
    category: str
    author: str
    keywords: list[str]


@dataclass
class ScheduleConfig:
    time: str
    timezone: str
    weekdays_only: bool
    min_lead_minutes: int

    @property
    def hour_minute(self) -> tuple[int, int]:
        hour, minute = self.time.split(":")
        return int(hour), int(minute)


@dataclass
class CoverConfig:
    width: int
    height: int
    kicker_min_words: int
    kicker_max_words: int
    alt_text_source: str


@dataclass
class CopyConfig:
    model: str
    max_tokens: int
    meta_title_max: int
    meta_description_max: int


@dataclass
class Secrets:
    """Credentials pulled from the environment.

    Nothing here is required at import time - each pipeline stage validates only
    the secrets it actually needs, so `wilbyte plan` works with no GHL token.
    """

    anthropic_api_key: str | None = None
    ghl_api_token: str | None = None
    ghl_location_id: str | None = None
    ghl_blog_id: str | None = None
    ghl_author_id: str | None = None
    ghl_category_id: str | None = None

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = ", ".join(n.upper() for n in missing)
            raise ConfigError(
                f"Missing required environment variable(s): {env_names}. "
                "Copy .env.example to .env and fill them in."
            )


@dataclass
class Config:
    brand: BrandConfig
    post: PostConfig
    schedule: ScheduleConfig
    cover: CoverConfig
    copy: CopyConfig
    secrets: Secrets = field(default_factory=Secrets)
    path: Path | None = None


def load_config(path: Path | None = None, *, load_env: bool = True) -> Config:
    """Load `config/wilbyte.toml` plus `.env` secrets."""
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    if load_env:
        load_dotenv(REPO_ROOT / ".env")

    try:
        return Config(
            brand=BrandConfig(**raw["brand"]),
            post=PostConfig(**raw["post"]),
            schedule=ScheduleConfig(**raw["schedule"]),
            cover=CoverConfig(**raw["cover"]),
            copy=CopyConfig(**raw["copy"]),
            secrets=Secrets(
                anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
                ghl_api_token=os.getenv("GHL_API_TOKEN"),
                ghl_location_id=os.getenv("GHL_LOCATION_ID"),
                ghl_blog_id=os.getenv("GHL_BLOG_ID"),
                ghl_author_id=os.getenv("GHL_AUTHOR_ID"),
                ghl_category_id=os.getenv("GHL_CATEGORY_ID"),
            ),
            path=config_path,
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Malformed config in {config_path}: {exc}") from exc
