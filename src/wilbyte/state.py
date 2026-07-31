"""Ledger of videos already turned into posts.

The playlist is worked through over multiple sessions ("as of now we are on
August 12, I'm gonna finish up until August 14... then we'll proceed to the week
17 to 21"), so re-running the same playlist must skip what is already done.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import REPO_ROOT


def _state_dir() -> Path:
    """Where the ledger lives.

    Overridable so a container can point it at a mounted volume - on an ephemeral
    filesystem the ledger would reset on every redeploy and the bot would happily
    repost videos it had already done.
    """
    override = os.getenv("WILBYTE_STATE_DIR")
    return Path(override) if override else REPO_ROOT / "state"


DEFAULT_LEDGER_PATH = _state_dir() / "ledger.json"


@dataclass
class LedgerEntry:
    video_id: str
    title: str
    url_slug: str
    scheduled_at: str | None
    ghl_post_id: str | None
    processed_at: str

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "url_slug": self.url_slug,
            "scheduled_at": self.scheduled_at,
            "ghl_post_id": self.ghl_post_id,
            "processed_at": self.processed_at,
        }


@dataclass
class Ledger:
    path: Path = DEFAULT_LEDGER_PATH
    entries: dict[str, LedgerEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Ledger":
        ledger_path = path or DEFAULT_LEDGER_PATH
        ledger = cls(path=ledger_path)
        if not ledger_path.exists():
            return ledger
        try:
            raw = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt ledger must not block a run; the worst case is a duplicate
            # post, which is visible and fixable, versus a hard stop that is not.
            return ledger
        for item in raw.get("entries", []):
            entry = LedgerEntry(
                video_id=item["video_id"],
                title=item.get("title", ""),
                url_slug=item.get("url_slug", ""),
                scheduled_at=item.get("scheduled_at"),
                ghl_post_id=item.get("ghl_post_id"),
                processed_at=item.get("processed_at", ""),
            )
            ledger.entries[entry.video_id] = entry
        return ledger

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "entries": [e.to_dict() for e in self.entries.values()],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def has(self, video_id: str) -> bool:
        return video_id in self.entries

    def record(
        self,
        *,
        video_id: str,
        title: str,
        url_slug: str,
        scheduled_at: datetime | None,
        ghl_post_id: str | None,
    ) -> LedgerEntry:
        entry = LedgerEntry(
            video_id=video_id,
            title=title,
            url_slug=url_slug,
            scheduled_at=scheduled_at.isoformat() if scheduled_at else None,
            ghl_post_id=ghl_post_id,
            processed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.entries[video_id] = entry
        return entry
