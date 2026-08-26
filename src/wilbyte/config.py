"""Configuration loading: TOML for brand/workflow defaults, env for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


def _env(name: str) -> str | None:
    """Read an env var, trimming stray whitespace and quotes.

    Credentials pasted into a hosting dashboard routinely pick up a trailing
    newline or wrapping quotes, which turns a valid token into a 401. Defined
    first because module-level path resolution below already uses it.
    """
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip().strip("\"'").strip() or None


def _repo_root() -> Path:
    """Locate the directory holding `config/`, `prompts/` and `assets/`.

    Editable installs sit two levels below the repo root, but an installed
    package lands in site-packages where that guess is meaningless - so fall
    back to the working directory when it carries the config, and let
    WILBYTE_HOME override both (that's what the container sets).
    """
    override = _env("WILBYTE_HOME")
    if override:
        return Path(override)

    source_guess = Path(__file__).resolve().parents[2]
    if (source_guess / "config" / "wilbyte.toml").exists():
        return source_guess

    cwd = Path.cwd()
    if (cwd / "config" / "wilbyte.toml").exists():
        return cwd
    return source_guess


REPO_ROOT = _repo_root()
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
    # Never schedule before this day, whatever the blog calendar says. The
    # escape hatch for posts GHL reports without a schedule: they look like
    # free days and get booked twice. An ISO date, or empty for no floor.
    earliest_day: str = ""

    @property
    def hour_minute(self) -> tuple[int, int]:
        hour, minute = self.time.split(":")
        return int(hour), int(minute)

    @property
    def floor(self) -> date | None:
        text = (self.earliest_day or "").strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ConfigError(
                f"[schedule] earliest_day must be a date like 2026-08-18, not {text!r}."
            ) from exc


@dataclass
class CoverConfig:
    width: int
    height: int
    kicker_min_words: int
    kicker_max_words: int
    alt_text_source: str
    headline_max_chars: int = 46


@dataclass
class DiscordConfig:
    require_approval: bool
    approval_timeout_minutes: int
    max_batch: int

    @property
    def approval_timeout_seconds(self) -> float:
        return self.approval_timeout_minutes * 60


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
    discord_bot_token: str | None = None
    discord_guild_id: str | None = None
    discord_channel_ids: tuple[str, ...] = ()
    discord_role_ids: tuple[str, ...] = ()
    # Channels to watch for YouTube links posted by anyone - including other
    # bots - so a new video becomes a blog post without anyone asking.
    discord_watch_channel_ids: tuple[str, ...] = ()
    # Where the watcher sends its review card. It cannot be the watched channel:
    # that one is an announcements feed, not a place to work.
    discord_post_channel_id: str | None = None
    # Where new sales-call cards are announced. Falls back to the first
    # allowed channel, so this only matters if they want their own room.
    discord_recordings_channel_id: str | None = None
    # Off unless asked for. The team reviews the day's calls and decides
    # which deserve a card, so filing everything found would fill the
    # gallery with calls they had already decided against.
    recordings_autofile: bool = False
    # The daily board. Key and token from trello.com/power-ups/admin; the board
    # id is the short code in its URL.
    # Off unless asked for. This one writes to a board four people work off
    # every day, on a timer, whether or not anybody is looking - so turning it
    # on is a deliberate act and turning it off needs no code change.
    trello_auto: bool = False
    # File new agents as they land in In Que. Separate from trello_auto: the
    # board moves happen three times a day and this happens all day, so
    # wanting one is not wanting the other.
    trello_agents_auto: bool = False
    # Where the board's own messages go. Falls back to the posting channel.
    discord_board_channel_id: str | None = None
    # Who gets pinged when the board needs a person rather than a report.
    # Without it the message still goes out, just without the tap on the
    # shoulder - which for something checked twice an afternoon is the point.
    discord_notify_user_id: str | None = None
    # The SOP library: a page to hold the database, and the channel it fills
    # from. RYTE files what lands in that channel and answers questions on it.
    notion_sop_page_id: str | None = None
    # The library that existed before RYTE did. Read once into an index, never
    # written to - it is somebody's hand-built page, not ours to reorganise.
    # Falls back to the SOP page when they are the same thing.
    notion_library_page_id: str | None = None
    discord_sop_channel_ids: tuple[str, ...] = ()
    trello_key: str | None = None
    trello_token: str | None = None
    trello_board_id: str | None = None
    # Notion, for filing sales-call recordings. The token comes from
    # notion.so/my-integrations and the page must be shared with it.
    notion_token: str | None = None
    notion_recordings_page_id: str | None = None
    notion_cover_url: str | None = None
    notion_icon_url: str | None = None
    # Falls back to an emoji, which never expires and needs nothing hosted.
    notion_icon_emoji: str | None = None
    # Zoom Server-to-Server OAuth, for reading a call the share link hides
    # behind a passcode.
    zoom_account_id: str | None = None
    zoom_client_id: str | None = None
    zoom_client_secret: str | None = None
    # Fathom, for the closer it records instead of Zoom.
    fathom_api_key: str | None = None

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
    discord: DiscordConfig
    secrets: Secrets = field(default_factory=Secrets)
    path: Path | None = None


def _id_list(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated list of Discord snowflake ids from the env."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


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
            discord=DiscordConfig(**raw["discord"]),
            secrets=Secrets(
                anthropic_api_key=_env("ANTHROPIC_API_KEY"),
                ghl_api_token=_env("GHL_API_TOKEN"),
                ghl_location_id=_env("GHL_LOCATION_ID"),
                ghl_blog_id=_env("GHL_BLOG_ID"),
                ghl_author_id=_env("GHL_AUTHOR_ID"),
                ghl_category_id=_env("GHL_CATEGORY_ID"),
                discord_bot_token=_env("DISCORD_BOT_TOKEN"),
                discord_guild_id=_env("DISCORD_GUILD_ID"),
                discord_channel_ids=_id_list(_env("DISCORD_CHANNEL_IDS")),
                discord_role_ids=_id_list(_env("DISCORD_ROLE_IDS")),
                discord_watch_channel_ids=_id_list(_env("DISCORD_WATCH_CHANNEL_IDS")),
                discord_post_channel_id=_env("DISCORD_POST_CHANNEL_ID"),
                discord_recordings_channel_id=_env("DISCORD_RECORDINGS_CHANNEL_ID"),
                recordings_autofile=(_env("RECORDINGS_AUTOFILE") or "").strip().lower()
                in ("1", "true", "yes"),
                notion_sop_page_id=_env("NOTION_SOP_PAGE_ID"),
                notion_library_page_id=_env("NOTION_LIBRARY_PAGE_ID"),
                discord_sop_channel_ids=_id_list(_env("DISCORD_SOP_CHANNEL_IDS")),
                trello_key=_env("TRELLO_KEY"),
                trello_token=_env("TRELLO_TOKEN"),
                trello_board_id=_env("TRELLO_BOARD_ID"),
                trello_auto=(_env("TRELLO_AUTO") or "").strip().lower()
                in ("1", "true", "yes"),
                trello_agents_auto=(_env("TRELLO_AGENTS_AUTO") or "").strip().lower()
                in ("1", "true", "yes"),
                discord_board_channel_id=_env("DISCORD_BOARD_CHANNEL_ID"),
                discord_notify_user_id=_env("DISCORD_NOTIFY_USER_ID"),
                notion_token=_env("NOTION_TOKEN"),
                notion_recordings_page_id=_env("NOTION_RECORDINGS_PAGE_ID"),
                notion_cover_url=_env("NOTION_COVER_URL"),
                notion_icon_url=_env("NOTION_ICON_URL"),
                notion_icon_emoji=_env("NOTION_ICON_EMOJI"),
                zoom_account_id=_env("ZOOM_ACCOUNT_ID"),
                zoom_client_id=_env("ZOOM_CLIENT_ID"),
                zoom_client_secret=_env("ZOOM_CLIENT_SECRET"),
                fathom_api_key=_env("FATHOM_API_KEY"),
            ),
            path=config_path,
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Malformed config in {config_path}: {exc}") from exc
