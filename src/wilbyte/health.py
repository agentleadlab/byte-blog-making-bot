"""Every connection RYTE uses, tried once - "if we create this, how am i
going to know that it'll work?"

Read only, every one of them: a list, a profile, a token. Nothing sent,
written, posted or moved, and nothing that costs more than a list call. Each
answers (name, True / False / None, what was found) - None for one that isn't
set up here, which is not a failure.

`@RYTE health` says it in Discord; standby on a new host writes it to the
logs before RYTE is let near Discord at all.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _short(exc: BaseException, most: int = 140) -> str:
    said = " ".join(str(exc).split()) or type(exc).__name__
    return said if len(said) <= most else said[: most - 1] + "…"


def _clock(config) -> tuple:
    from zoneinfo import ZoneInfo

    zone = getattr(getattr(config, "schedule", None), "timezone", "") or "America/New_York"
    now = datetime.now(ZoneInfo(zone))
    machine = datetime.now().astimezone().tzname()
    return True, f"{now:%a %b %d %I:%M %p} {zone} · this machine's clock is {machine}"


def _storage() -> tuple:
    from .state import _state_dir

    where = Path(_state_dir())
    where.mkdir(parents=True, exist_ok=True)
    probe = where / ".health-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    said = f"memory at {where} is writable"
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        if not os.path.ismount("/data"):
            return False, said + " - but /data isn't a volume: everything is lost on the next deploy"
        said += " · /data is a volume"
    return True, said


def _trello(config) -> tuple:
    from .bot.jobs import open_trello

    with open_trello(config) as client:
        lists = client.board_lists(config.secrets.trello_board_id)
    return True, f"board read - {len(lists)} lists"


def _payra(secrets) -> tuple:
    from . import payraapi

    if not (secrets.payra_api_token and secrets.payra_site_id):
        return None, "not set up"
    with payraapi.PayraClient(secrets.payra_api_token, secrets.payra_site_id, timeout=30.0) as client:
        client.changed("invoices", payraapi.when(datetime.now(timezone.utc) - timedelta(hours=1)))
    return True, "invoices read"


def _google(secrets) -> tuple:
    from . import gsheets

    if not getattr(secrets, "google_refresh_token", None):
        return None, "not set up"
    scopes = gsheets.granted(gsheets.credentials(secrets))
    return True, f"signed in - {len(scopes)} permission(s)"


def _drive(secrets) -> tuple:
    from . import drive, gsheets

    if not getattr(secrets, "clients_drive_folder", None):
        return None, "no clients folder set"
    with drive.open_drive(secrets) as client:
        found = client.about(gsheets.folder_id_in(secrets.clients_drive_folder))
        if found:
            return True, "clients folder found"
        # Ryte's Drive permission only sees what Ryte made: a folder somebody
        # else made and shared reads as missing even when uploads into it work.
        kept = client.kept_here()
    if kept:
        return True, f"clients folder in use - {kept}{'+' if kept >= 10 else ''} thing(s) Ryte kept there"
    return None, ("signed in; the clients folder can't be seen until Ryte has kept a picture in it "
                  "(only used when a quiet channel is cleared out)")


def _gmail(secrets) -> tuple:
    from . import gmail

    if not getattr(secrets, "gmail_invoice_sender", None):
        return None, "not set up"
    with gmail.open_gmail(secrets) as client:
        address, _count = client.whoami()
    return True, f"reading {address}"


def _notion(secrets) -> tuple:
    from . import notion

    page = (getattr(secrets, "notion_sop_page_id", None) or getattr(secrets, "notion_recordings_page_id", None)
            or getattr(secrets, "notion_library_page_id", None))
    if not (getattr(secrets, "notion_token", None) and page):
        return None, "not set up"
    with notion.NotionClient(secrets.notion_token, timeout=30.0) as client:
        client.page(page)
    return True, "page read"


def _pandadoc(secrets) -> tuple:
    from . import pandadoc

    if not getattr(secrets, "pandadoc_api_key", None):
        return None, "not set up"
    said = " ".join(pandadoc.test(secrets.pandadoc_api_key).split())
    return ("✅" in said and "❌" not in said), said[:140]


def _hub(secrets) -> tuple:
    from . import hub

    if not getattr(secrets, "hub_api_token", None):
        return None, "not set up"
    found = hub.agents(secrets, timeout=30.0)
    return True, f"read only - {len(found)} fulfilled agent(s)"


def _zoom(secrets) -> tuple:
    from . import zoom

    if not (getattr(secrets, "zoom_account_id", None) and getattr(secrets, "zoom_client_id", None)):
        return None, "not set up"
    with zoom.ZoomClient(secrets.zoom_account_id, secrets.zoom_client_id, secrets.zoom_client_secret,
                         timeout=30.0) as client:
        client.token()
    return True, "signed in"


def _fathom(secrets) -> tuple:
    from . import fathom

    if not getattr(secrets, "fathom_api_key", None):
        return None, "not set up"
    with fathom.FathomClient(secrets.fathom_api_key, timeout=30.0) as client:
        client.meetings(limit=1)
    return True, "recordings read"


def _ringcentral(secrets) -> tuple:
    from . import ringcentral

    if not getattr(secrets, "ringcentral_jwt", None):
        return None, "not set up"
    with ringcentral.open_ring(secrets) as client:
        who = client.owner()
    return True, f"signed in{f' as {who}' if who else ''} - reads only, never sends"


def _youtube() -> tuple:
    from . import youtube, youtube_api

    proxy = "through the proxy" if youtube.proxy_url() else "no proxy set"
    if not youtube_api.api_key():
        return None, f"no YOUTUBE_API_KEY · {proxy}"
    youtube_api.get_video("jNQXAC9IVRw")  # "Me at the zoo": always there
    return True, f"Data API answers · transcripts {proxy}"


def _claude(secrets) -> tuple:
    from anthropic import Anthropic

    if not getattr(secrets, "anthropic_api_key", None):
        return False, "no ANTHROPIC_API_KEY - nothing gets read"
    Anthropic(api_key=secrets.anthropic_api_key).models.list(limit=1)
    return True, "key works"


def _ghl(config) -> tuple:
    from . import ghl

    secrets = config.secrets
    if not (getattr(secrets, "ghl_api_token", None) and getattr(secrets, "ghl_location_id", None)):
        return None, "not set up"
    with ghl.GHLClient(secrets.ghl_api_token, secrets.ghl_location_id) as client:
        sites = client.list_blog_sites()
    return True, f"{len(sites)} blog site(s)"


def _wordpress(config) -> tuple:
    if not getattr(config.secrets, "wordpress_app_password", None):
        return None, "not set up"
    from .bot.jobs import _wordpress

    with _wordpress(config) as site:
        site.me()
    return True, "signed in"


def _stripe() -> tuple:
    from . import stripepay

    if not stripepay.configured():
        return None, "not set up"
    stripepay._call("GET", "products", params={"limit": 1})
    return True, "products read" + ("" if stripepay.live() else " (test key)")


def checks(config) -> list[tuple[str, bool | None, str]]:
    """Every connection, tried once, in the order they matter to the team."""
    secrets = config.secrets
    tries = (
        ("Clock", lambda: _clock(config)),
        ("Memory", _storage),
        ("Trello", lambda: _trello(config)),
        ("Payra", lambda: _payra(secrets)),
        ("Claude", lambda: _claude(secrets)),
        ("Distro Hub", lambda: _hub(secrets)),
        ("Google", lambda: _google(secrets)),
        ("Google Drive", lambda: _drive(secrets)),
        ("Gmail", lambda: _gmail(secrets)),
        ("PandaDoc", lambda: _pandadoc(secrets)),
        ("Notion", lambda: _notion(secrets)),
        ("RingCentral", lambda: _ringcentral(secrets)),
        ("Zoom", lambda: _zoom(secrets)),
        ("Fathom", lambda: _fathom(secrets)),
        ("YouTube", _youtube),
        ("GHL", lambda: _ghl(config)),
        ("Website", lambda: _wordpress(config)),
        ("Stripe", _stripe),
    )
    found = []
    for name, attempt in tries:
        try:
            ok, said = attempt()
        except Exception as exc:  # one down must not hide the rest
            ok, said = False, _short(exc)
        found.append((name, ok, said))
    return found


def describe(found: list[tuple[str, bool | None, str]]) -> str:
    marks = {True: "✅", False: "❌", None: "➖"}
    lines = [f"{marks[ok]} **{name}** — {said}" for name, ok, said in found]
    broken = sum(1 for _name, ok, _said in found if ok is False)
    head = "🩺 **Health** — everything I use answers." if not broken else f"🩺 **Health** — {broken} not working."
    return "\n".join([head, *lines])


def standing_by() -> bool:
    """Standby: on a new host before the switch - "dont do anything that will
    affect his current setup yet". RYTE checks its connections and waits,
    and never connects to Discord, so two copies can't answer at once."""
    return (os.getenv("RYTE_STANDBY") or "").strip().casefold() in {"1", "true", "yes", "on"}
