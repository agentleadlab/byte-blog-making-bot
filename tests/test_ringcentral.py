"""Reading Faith's texts on RingCentral, and nothing else."""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace as NS

import pytest

from wilbyte import ringcentral


class FakeRing:
    """RingCentral as far as signing in and reading goes."""

    def __init__(self, *, extensions=None, pages=None, status=200):
        self.extensions = extensions if extensions is not None else [
            {"id": 111, "extensionNumber": "101", "name": "Franklin May Maldonado"},
            {"id": 222, "extensionNumber": "103", "name": "Faith Hannah Calla"},
        ]
        self.pages = pages if pages is not None else [[]]
        self.status = status
        self.signed_in = []
        self.read = []

    def post(self, url, headers=None, data=None):
        self.signed_in.append({"url": url, "headers": headers, "data": data})
        return NS(status_code=200, text="",
                  json=lambda: {"access_token": "tok", "expires_in": 7199})

    def get(self, url, params=None, headers=None):
        self.read.append({"url": url, "params": params, "headers": headers})
        if self.status != 200:
            return NS(status_code=self.status, text="no", json=lambda: {})
        if url.endswith("/phone-number"):
            return NS(status_code=200, text="", json=lambda: {"records": [
                {"phoneNumber": "+18785550100"}, {"phoneNumber": "+14125550177"},
                {"usageType": "Fax"},
            ]})
        if url.endswith("/extension"):
            return NS(status_code=200, text="",
                      json=lambda: {"records": self.extensions, "navigation": {}})
        # The page is in the address: the first read asks for the path, and
        # every next page RingCentral hands back says which one it is.
        found = re.search(r"[?&]page=(\d+)", url)
        at = int(found.group(1)) - 1 if found else 0
        page = self.pages[at] if at < len(self.pages) else []
        more = at + 1 < len(self.pages)
        nxt = f"https://platform.ringcentral.com/x/message-store?page={at + 2}"
        return NS(status_code=200, text="", json=lambda: {
            "records": page,
            "navigation": {"nextPage": {"uri": nxt}} if more else {},
        })

    def close(self):
        pass


def _client(monkeypatch, *, extension="103", **kw):
    fake = FakeRing(**kw)
    monkeypatch.setattr(ringcentral.httpx, "Client", lambda **k: fake)
    return ringcentral.RingClient(
        ringcentral.RingCreds("id", "secret", "the-jwt"), extension=extension,
    ), fake


# --------------------------------------------------- it cannot send anything


def test_there_is_no_way_to_send_a_text():
    """"i dont need RYte to send the messages." Not switched off - absent."""
    names = [name for name, _ in inspect.getmembers(ringcentral.RingClient)]

    assert not [
        name for name in names
        if re.search(r"send|reply|post|delete|mark|write|put|patch", name, re.I)
    ]


def test_the_only_thing_it_ever_posts_is_the_sign_in():
    """A POST to anything but the token endpoint would be a write."""
    source = inspect.getsource(ringcentral)

    assert source.count(".post(") == 1
    assert "TOKEN_PATH" in source.split(".post(")[1][:80]
    assert ".put(" not in source and ".patch(" not in source and ".delete(" not in source


# ------------------------------------------------------------ signing in


def test_it_signs_in_with_the_jwt(monkeypatch):
    client, fake = _client(monkeypatch)
    client.extension_id()

    sign_in = fake.signed_in[0]
    assert sign_in["url"].endswith("/restapi/oauth/token")
    assert sign_in["data"] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": "the-jwt",
    }
    assert sign_in["headers"]["Authorization"].startswith("Basic ")


def test_one_sign_in_serves_many_reads(monkeypatch):
    client, fake = _client(monkeypatch)
    client.texts(since="2026-09-01T00:00:00.000Z")
    client.texts(since="2026-09-02T00:00:00.000Z")

    assert len(fake.signed_in) == 1


# ------------------------------------------------------ the one extension


def test_faith_is_found_by_her_extension_number(monkeypatch):
    client, _ = _client(monkeypatch, extension="103")

    assert client.extension_id() == "222"


def test_or_by_her_name(monkeypatch):
    client, _ = _client(monkeypatch, extension="faith hannah calla")

    assert client.extension_id() == "222"


def test_an_extension_that_isnt_there_says_what_is(monkeypatch):
    """So fixing .env is one look rather than a hunt."""
    client, _ = _client(monkeypatch, extension="999")

    with pytest.raises(ringcentral.RingError) as said:
        client.extension_id()

    assert "103 Faith Hannah Calla" in str(said.value)


def test_only_her_extension_is_ever_read(monkeypatch):
    """An admin's JWT could read anybody's texts. The narrowness is here."""
    client, fake = _client(monkeypatch, extension="103")
    client.texts(since="2026-09-01T00:00:00.000Z")

    reads = [one["url"] for one in fake.read if "message-store" in one["url"]]
    assert reads and all("/extension/222/message-store" in one for one in reads)


def test_no_extension_set_is_refused():
    with pytest.raises(ringcentral.RingError) as said:
        ringcentral.RingClient(ringcentral.RingCreds("a", "b", "c"), extension="")

    assert "RINGCENTRAL_EXTENSION" in str(said.value)


# --------------------------------------------------------------- reading


def test_texts_are_asked_for_as_sms_from_a_time(monkeypatch):
    client, fake = _client(monkeypatch)
    client.texts(since="2026-09-01T00:00:00.000Z")

    asked = next(one["params"] for one in fake.read if "message-store" in one["url"])
    assert asked["messageType"] == "SMS"
    assert asked["dateFrom"] == "2026-09-01T00:00:00.000Z"


def test_every_page_is_read_and_the_texts_come_back_oldest_first(monkeypatch):
    client, _ = _client(monkeypatch, pages=[
        [{"id": 2, "creationTime": "2026-09-02T00:00:00.000Z"}],
        [{"id": 1, "creationTime": "2026-09-01T00:00:00.000Z"}],
    ])

    got = client.texts(since="2026-09-01T00:00:00.000Z")

    assert [one["id"] for one in got] == [1, 2]


def test_a_refusal_says_what_the_app_needs(monkeypatch):
    client, _ = _client(monkeypatch, status=403)

    with pytest.raises(ringcentral.RingError) as said:
        client.extension_id()

    assert "Read Messages" in str(said.value) and "admin" in str(said.value)


# ----------------------------------------------------------------- set up


def test_what_is_missing_is_named():
    with pytest.raises(ringcentral.RingError) as said:
        ringcentral.open_ring(NS(
            ringcentral_client_id="x", ringcentral_client_secret="",
            ringcentral_jwt="", ringcentral_extension="103",
        ))

    assert "RINGCENTRAL_CLIENT_SECRET" in str(said.value)
    assert "RINGCENTRAL_JWT" in str(said.value)


def test_configured_only_with_all_four():
    full = dict(ringcentral_client_id="a", ringcentral_client_secret="b",
                ringcentral_jwt="c", ringcentral_extension="103")

    assert ringcentral.configured(NS(**full))
    assert not ringcentral.configured(NS(**{**full, "ringcentral_jwt": " "}))



def test_the_lines_own_numbers_come_from_that_extension_only(monkeypatch):
    client, fake = _client(monkeypatch)

    got = client.own_numbers()

    assert got == ["+18785550100", "+14125550177"]
    asked = [one["url"] for one in fake.read if one["url"].endswith("/phone-number")]
    assert asked and all("/extension/222/phone-number" in one for one in asked)


def test_whose_line_it_is_is_known(monkeypatch):
    client, _ = _client(monkeypatch, extension="103")

    assert client.owner() == "Faith Hannah Calla"
