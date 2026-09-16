"""Tagging a client blacklisted in GHL, after a chargeback.

"after that, ryte we'll go to GHL and find their contact information and put a
tag as blacklisted" — the last step of the chargeback run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from wilbyte import ghl
from wilbyte.bot import client as bot_client, jobs


JULIANA = {
    "id": "c1", "firstName": "Juliana", "lastName": "Hernandez",
    "email": "hjuliana650@gmail.com", "phone": "+1 (430) 230-2708", "tags": [],
}
OTHER_JAY = {
    "id": "c2", "firstName": "Jay", "lastName": "Sanderson",
    "email": "jay.s@example.com", "phone": "", "tags": [],
}
JAY = {
    "id": "c3", "firstName": "Jay", "lastName": "Rodriguez",
    "email": "jay@example.com", "phone": "", "tags": ["client"],
}


# ------------------------------------------------- who is this person


def test_an_email_is_the_identifier_that_means_it():
    assert ghl._is_them(JULIANA, "hjuliana650@gmail.com", "", "") is True


def test_an_email_matches_however_it_was_capitalised():
    assert ghl._is_them(
        {"email": "HJuliana650@Gmail.COM"}, "hjuliana650@gmail.com", "", ""
    ) is True


def test_a_phone_matches_past_its_punctuation():
    """+1 (430) 230-2708 and 14302302708 are one number written twice."""
    assert ghl._is_them(JULIANA, "", "14302302708", "") is True


def test_a_first_name_alone_does_not_match_anybody():
    """"Jay" would otherwise blacklist every Jay in the account."""
    assert ghl._is_them(JAY, "", "", "jay") is False


def test_the_whole_name_matches():
    assert ghl._is_them(JAY, "", "", "jay rodriguez") is True


def test_somebody_else_entirely_is_not_them():
    assert ghl._is_them(OTHER_JAY, "", "", "jay rodriguez") is False


def test_nothing_asked_matches_nobody():
    assert ghl._is_them(JULIANA, "", "", "") is False


# ------------------------------------------- what the command does with it


class Crm:
    """A stubbed GHL client that remembers what it was told to tag."""

    def __init__(self, contacts=(), refuses=()):
        self.contacts, self.refuses = list(contacts), set(refuses)
        self.tagged = []

    def find_contacts(self, *, email="", phone="", name="", cap=20000):
        return [
            one for one in self.contacts
            if ghl._is_them(one, email.casefold(), phone, name.casefold())
        ]

    def add_tags(self, contact_id, tags):
        if contact_id in self.refuses:
            raise ghl.GHLError("HTTP 403")
        self.tagged.append((contact_id, list(tags)))
        return list(tags)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Button:
    press = True

    def __init__(self, **kw):
        self.confirmed = type(self).press
        self.answered = True
        self.label = kw.get("label", "")
        self.danger = kw.get("danger", False)

    async def wait(self):
        return None


def _run(monkeypatch, asked, *, contacts, press=True, refuses=(), tag="blacklisted"):
    crm = Crm(contacts, refuses=refuses)
    monkeypatch.setattr(ghl, "GHLClient", lambda token, location, **kw: crm)

    class Pressed(Button):
        pass

    Pressed.press = press
    buttons = []

    class Watched(Pressed):
        def __init__(self, **kw):
            super().__init__(**kw)
            buttons.append(self)

    monkeypatch.setattr(bot_client.views, "ConfirmView", Watched)

    said = []

    async def send(content=None, *, embed=None, file=None, view=None):
        said.append(content or getattr(embed, "description", "") or "")

    config = SimpleNamespace(
        secrets=SimpleNamespace(
            ghl_api_token="t", ghl_location_id="l", ghl_blacklist_tag=tag,
        ),
        discord=SimpleNamespace(approval_timeout_seconds=1),
    )
    asyncio.run(bot_client._blacklist_them(
        SimpleNamespace(send=send, requester_id=1), config, asked,
    ))
    return crm, said, buttons


def test_pressing_it_tags_them(monkeypatch):
    crm, said, _ = _run(monkeypatch, "Juliana Hernandez", contacts=[JULIANA, JAY])

    assert crm.tagged == [("c1", ["blacklisted"])]
    assert "Tagged **Juliana Hernandez" in said[-1]


def test_nothing_is_tagged_without_the_press(monkeypatch):
    crm, said, _ = _run(
        monkeypatch, "Juliana Hernandez", contacts=[JULIANA], press=False,
    )

    assert crm.tagged == []


def test_the_button_is_red(monkeypatch):
    """A paying client who stops getting leads and is never told why."""
    _, _, buttons = _run(monkeypatch, "Juliana Hernandez", contacts=[JULIANA])

    assert buttons[-1].danger is True


def test_everyone_matching_is_shown_before_anything_is_written(monkeypatch):
    _, said, _ = _run(monkeypatch, "jay@example.com", contacts=[JULIANA, JAY])

    assert "Jay Rodriguez" in said[0]
    assert "hjuliana650" not in said[0]


def test_two_matches_are_both_named_and_flagged(monkeypatch):
    """More than one is something for a person to look at, not a coin to
    flip."""
    twin = {**JAY, "id": "c4", "email": "jay2@example.com"}
    _, said, _ = _run(monkeypatch, "Jay Rodriguez", contacts=[JAY, twin])

    assert "More than one contact matches" in said[0]
    assert "jay@example.com" in said[0] and "jay2@example.com" in said[0]


def test_nobody_matching_is_said_rather_than_passed_over(monkeypatch):
    crm, said, buttons = _run(monkeypatch, "Nobody At All", contacts=[JULIANA])

    assert crm.tagged == []
    assert buttons == []
    assert "No contact in GHL matches" in said[0]


def test_somebody_already_tagged_is_not_tagged_again(monkeypatch):
    done = {**JULIANA, "tags": ["client", "Blacklisted"]}
    crm, said, buttons = _run(monkeypatch, "Juliana Hernandez", contacts=[done])

    assert crm.tagged == []
    assert buttons == [], "it offered to do something already done"
    assert "already carry" in said[0]


def test_a_contact_ghl_refuses_is_named_rather_than_lost(monkeypatch):
    twin = {**JAY, "id": "c4", "email": "jay2@example.com", "tags": []}
    crm, said, _ = _run(
        monkeypatch, "Jay Rodriguez", contacts=[{**JAY, "tags": []}, twin],
        refuses={"c3"},
    )

    assert crm.tagged == [("c4", ["blacklisted"])]
    assert any("Couldn't tag" in one for one in said[-1].split("\n"))


def test_the_tag_spelling_is_configurable(monkeypatch):
    """A tag that differs by a capital is a second tag nobody's filters watch."""
    crm, _, buttons = _run(
        monkeypatch, "Juliana Hernandez", contacts=[JULIANA], tag="Blacklisted",
    )

    assert crm.tagged == [("c1", ["Blacklisted"])]
    assert "Blacklisted" in buttons[-1].label


def test_ghl_not_configured_says_so(monkeypatch):
    found, tag, problems = jobs.who_to_blacklist(
        SimpleNamespace(secrets=SimpleNamespace(
            ghl_api_token=None, ghl_location_id=None, ghl_blacklist_tag="blacklisted",
        )),
        "Juliana Hernandez",
    )

    assert found == []
    assert "GHL_API_TOKEN" in problems[0]


def test_asking_about_nobody_asks_who(monkeypatch):
    found, _, problems = jobs.who_to_blacklist(
        SimpleNamespace(secrets=SimpleNamespace(
            ghl_api_token="t", ghl_location_id="l", ghl_blacklist_tag="blacklisted",
        )),
        "   ",
    )

    assert found == []
    assert "Who?" in problems[0]


def test_blacklist_is_a_word_ryte_knows():
    from wilbyte.bot import mentions

    for typed in ("blacklist Juliana Hernandez", "blacklisted jay@example.com"):
        assert mentions.parse(f"<@1> {typed}").action == "blacklist"


# ------------------------------- finding one person among eleven thousand

# 11,431 contacts, fifty to a page, is 229 requests to tag one person. The
# account's own search does it in one — but what it hands back is checked here
# rather than trusted.


class Paged:
    """A GHL client with a real search and a real walk, both countable."""

    def __init__(self, everyone, *, search=None):
        self.everyone = list(everyone)
        self.search = everyone if search is None else list(search)
        self.location_id = "l"
        self.requests = []

    def _request(self, method, path, **kwargs):
        params = kwargs.get("params") or {}
        self.requests.append(params.get("query") or f"page:{params.get('startAfterId')}")
        if params.get("query") is not None:
            return {"contacts": list(self.search)}
        after = params.get("startAfterId")
        start = 0
        if after:
            start = next(
                (i + 1 for i, one in enumerate(self.everyone) if one["id"] == after), 0
            )
        return {"contacts": self.everyone[start:start + 50]}

    find_contacts = ghl.GHLClient.find_contacts
    _ask_for = ghl.GHLClient._ask_for


def test_the_search_answers_in_one_request_when_it_works():
    crm = Paged([JULIANA, JAY], search=[JULIANA])
    found = crm.find_contacts(email="hjuliana650@gmail.com")

    assert [one["id"] for one in found] == ["c1"]
    assert len(crm.requests) == 1, "it walked the whole account anyway"


def test_a_fuzzy_search_result_that_is_not_them_is_not_tagged():
    """Twenty near-misses come back and none of them is the person asked for."""
    crm = Paged([JULIANA], search=[OTHER_JAY, JAY])
    found = crm.find_contacts(email="hjuliana650@gmail.com")

    assert [one["id"] for one in found] == ["c1"]


def test_a_search_that_finds_nothing_falls_back_to_walking():
    """A rejected filter comes back empty rather than complaining, and "not in
    GHL" is the one answer that must not be guessed at."""
    crm = Paged([OTHER_JAY, JULIANA], search=[])
    found = crm.find_contacts(email="hjuliana650@gmail.com")

    assert [one["id"] for one in found] == ["c1"]
    assert len(crm.requests) > 1, "it gave up on the search's word"


def test_somebody_genuinely_absent_comes_back_empty():
    crm = Paged([OTHER_JAY, JAY], search=[])

    assert crm.find_contacts(email="nobody@example.com") == []


def test_the_wavv_blacklisted_tag_is_a_different_tag():
    """"blacklisted" and "wavv-blacklisted" both live in that account."""
    assert bot_client._carries({"tags": ["wavv-blacklisted"]}, "blacklisted") is False
    assert bot_client._carries({"tags": ["blacklisted"]}, "blacklisted") is True


# ------------------- the word that chose the command is not part of the name

# "@Ryte blacklist Juliana Hernandez" came back "No contact in GHL matches
# blacklist Juliana Hernandez" — the command word was searched for as though
# it were part of her name. `clearout` had it too, unnoticed because nobody
# had run the command that bans somebody and deletes a channel.


@pytest.mark.parametrize(
    "typed, action, brief",
    [
        ("blacklist Juliana Hernandez", "blacklist", "Juliana Hernandez"),
        ("blacklisted hjuliana650@gmail.com", "blacklist", "hjuliana650@gmail.com"),
        ("clearout Jay Rodriguez", "clearout", "Jay Rodriguez"),
        ("clear Jay Rodriguez", "clearout", "Jay Rodriguez"),
        ("closedown Jay Rodriguez", "clearout", "Jay Rodriguez"),
    ],
)
def test_the_command_word_does_not_reach_the_name(typed, action, brief):
    from wilbyte.bot import mentions

    said = mentions.parse(f"<@1> {typed}")

    assert (said.action, said.brief) == (action, brief)


def test_every_alias_is_stripped_not_just_the_obvious_one():
    """Built from ACTION_WORDS, so an alias added there cannot be forgotten."""
    from wilbyte.bot import mentions

    for action, words in mentions.NAMES_SOMEBODY.items():
        for word in words:
            assert mentions.ACTION_WORDS[word] == action
            assert mentions.parse(f"<@1> {word} Somebody Named").brief == "Somebody Named"


def test_the_command_on_its_own_still_asks_who():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> clearout").brief == ""
    assert mentions.parse("<@1> blacklist").brief == ""


def test_the_whole_message_still_travels_where_it_is_meant_to():
    """"rollover general" names which card, and that branch is untouched."""
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> rollover general").brief == "rollover general"


def test_what_the_stray_word_cost_the_clear_out():
    """Not the channel — "jayrodriguez" is inside "clearoutjayrodriguez", so
    that still matched. It was their Trello card, which is matched word by
    word and needs every word to be in the name. No card means no sheet link,
    and the clear-out would have written a blank one into ALL CLIENTS and then
    offered to delete the channel the link was the only copy of."""
    from wilbyte import agents, clearout as rules

    channels = [rules.Channel(channel_id="1", name="jay-rodriguez")]
    cards = [{"name": "New Agent - Jay Rodriguez", "id": "c1"}]

    assert rules.channels_for("clearout Jay Rodriguez", channels), "the channel was fine"
    assert agents.named_that("clearout Jay Rodriguez", cards) == []
    assert agents.named_that("Jay Rodriguez", cards) == cards
