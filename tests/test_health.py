"""`@RYTE health` and standby - "if we create this, how am i going to know
that it'll work?"."""

from __future__ import annotations

from types import SimpleNamespace as NS

from wilbyte import health


def _config(**secrets):
    base = dict(trello_board_id="b", payra_api_token=None, payra_site_id=None, google_refresh_token=None,
                clients_drive_folder=None, gmail_invoice_sender=None, notion_token=None, notion_sop_page_id=None,
                notion_recordings_page_id=None, notion_library_page_id=None, pandadoc_api_key=None,
                hub_api_token=None, zoom_account_id=None, zoom_client_id=None, fathom_api_key=None,
                ringcentral_jwt=None, anthropic_api_key="k", ghl_api_token=None, wordpress_app_password=None)
    base.update(secrets)
    return NS(secrets=NS(**base), schedule=NS(timezone="America/New_York"))


def test_every_connection_is_tried_and_one_down_doesnt_hide_the_rest(monkeypatch):
    monkeypatch.setattr(health, "_trello", lambda config: (_ for _ in ()).throw(RuntimeError("401 bad token")))
    monkeypatch.setattr(health, "_claude", lambda secrets: (True, "key works"))
    monkeypatch.setattr(health, "_youtube", lambda: (None, "no YOUTUBE_API_KEY"))
    monkeypatch.setattr(health, "_stripe", lambda: (None, "not set up"))
    found = {name: (ok, said) for name, ok, said in health.checks(_config())}
    assert found["Trello"] == (False, "401 bad token")
    assert found["Claude"] == (True, "key works")
    assert found["Payra"] == (None, "not set up") and found["Memory"][0] is True
    said = health.describe(health.checks(_config()))
    assert said.startswith("🩺 **Health** — 1 not working.") and "❌ **Trello** — 401 bad token" in said


def test_memory_on_railway_without_a_volume_is_said(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setattr(health.os.path, "ismount", lambda path: False)
    ok, said = health._storage()
    assert ok is False and "isn't a volume" in said


def test_standby_waits_without_ever_starting_the_bot(monkeypatch, capsys):
    from wilbyte import cli

    monkeypatch.setenv("RYTE_STANDBY", "1")
    monkeypatch.setattr(cli, "load_config", lambda: _config())
    monkeypatch.setattr(health, "checks", lambda config: [("Trello", True, "board read - 5 lists")])
    started = []
    import wilbyte.bot as bot_pkg

    monkeypatch.setattr(bot_pkg, "run_bot", lambda config: started.append(1))

    class Stop(Exception):
        pass

    def sleep(seconds):
        raise Stop

    import time

    monkeypatch.setattr(time, "sleep", sleep)
    try:
        cli.cmd_bot(NS())
    except Stop:
        pass
    assert started == [], "never on Discord in standby"
    assert "✅ Trello — board read - 5 lists" in capsys.readouterr().out
    monkeypatch.setenv("RYTE_STANDBY", "0")
    assert not health.standing_by()


def test_asked_for_by_name():
    from wilbyte.bot import mentions

    for said in ("health", "health check", "check connections"):
        assert mentions.parse(said, max_batch=5).action == "health"


def test_each_probe_runs_against_its_service_once_keys_are_there(monkeypatch):
    """With keys present each probe makes its one read - none of them trips
    over how its client is opened."""
    from wilbyte import fathom, ghl, hub, notion, payraapi, ringcentral, trello, website, zoom
    from wilbyte.bot import jobs

    called = []

    class Fake:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

        def __getattr__(self, name):
            def call(*a, **k):
                called.append(name)
                return [] if name in ("board_lists", "list_blog_sites", "meetings") else {}
            return call

        def owner(self):
            called.append("owner")
            return "Arnold Tarpley"

    for module, name in ((payraapi, "PayraClient"), (ghl, "GHLClient"), (notion, "NotionClient"),
                         (zoom, "ZoomClient"), (fathom, "FathomClient")):
        monkeypatch.setattr(module, name, Fake)
    monkeypatch.setattr(jobs, "open_trello", lambda config: Fake())
    monkeypatch.setattr(jobs, "_wordpress", lambda config: Fake())
    monkeypatch.setattr(ringcentral, "open_ring", lambda secrets: Fake())
    monkeypatch.setattr(hub, "agents", lambda secrets, **k: [1, 2])
    config = _config(payra_api_token="t", payra_site_id="s", ghl_api_token="pit-x", ghl_location_id="l",
                     notion_token="n", notion_sop_page_id="p", zoom_account_id="a", zoom_client_id="c",
                     zoom_client_secret="z", fathom_api_key="f", ringcentral_jwt="j", hub_api_token="h",
                     wordpress_app_password="w")
    assert health._trello(config)[0] and health._payra(config.secrets)[0] and health._ghl(config)[0]
    assert health._notion(config.secrets)[0] and health._zoom(config.secrets)[0]
    assert health._fathom(config.secrets)[0] and health._ringcentral(config.secrets)[0]
    assert health._hub(config.secrets) == (True, "read only - 2 fulfilled agent(s)")
    assert health._wordpress(config)[0]
    assert {"board_lists", "changed", "list_blog_sites", "page", "token", "meetings", "owner", "me"} <= set(called)


def test_a_drive_folder_ryte_cant_see_isnt_called_missing(monkeypatch):
    """"❌ Google Drive — clients folder not found or not shared" - Ryte's
    Drive permission only sees what Ryte made, so a shared folder reads as
    missing even when pictures go into it."""
    from wilbyte import drive

    kept = [0]

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def about(self, file_id):
            return {}

        def kept_here(self):
            return kept[0]

    monkeypatch.setattr(drive, "open_drive", lambda secrets: Client())
    secrets = NS(clients_drive_folder="https://drive.google.com/drive/folders/abcdefghijklmnop")
    ok, said = health._drive(secrets)
    assert ok is None and "can't be seen" in said
    kept[0] = 3
    ok, said = health._drive(secrets)
    assert ok is True and "3 thing(s)" in said
