"""The links in the texts, and what RYTE looks up before he drafts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from wilbyte import smslinks, stripepay
from wilbyte.bot import jobs
from wilbyte.smsreplies import Text

SHEET = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=77"
FORM = "https://link.agentleadlab.com/widget/form/abc123"


def _text(at, said, *, inbound=False, agent="3125550188", name="", team=False):
    one = Text(id=at, at=at, inbound=inbound, agent=agent, name=name, said=said,
               conversation=f"C-{agent}")
    one.team = team
    return one


# ------------------------------------------------------------- finding links


def test_links_are_found_however_they_were_typed():
    said = (f"Here you go ({SHEET}). Also www.agentleadlab.com/pricing, and "
            f"docs.google.com/forms/d/xyz! {SHEET}")

    assert smslinks.links_in(said) == [
        SHEET, "https://www.agentleadlab.com/pricing", "https://docs.google.com/forms/d/xyz",
    ]
    assert smslinks.links_in("no links here, see you at 5.30") == []


@pytest.mark.parametrize("link, kind", [
    (SHEET, "sheet"),
    ("https://docs.google.com/document/d/abc/edit", "doc"),
    ("https://drive.google.com/file/d/abc/view", "drive"),
    ("https://trello.com/c/AbC123/45-new-agent", "trello"),
    ("https://www.loom.com/share/0123456789abcdef0123456789abcdef", "loom"),
    ("https://buy.stripe.com/5kA3cd", "stripe_pay"),
    ("https://invoice.stripe.com/i/acct_1/live_abc", "stripe_invoice"),
    ("https://app.pandadoc.com/s/abc", "pandadoc"),
    ("https://us02web.zoom.us/j/123", "zoom"),
    ("https://calendly.com/faith/15min", "calendar"),
    (FORM, "form"),
    ("https://agentleadlab.com/faq", "web"),
])
def test_each_link_is_named_for_what_it_is(link, kind):
    assert smslinks.kind_of(link)[0] == kind


def test_a_link_with_a_tracking_tail_is_the_same_link():
    assert smslinks.same_link(FORM + "?utm_source=sms") == smslinks.same_link(FORM + "/")
    assert smslinks.same_link(FORM) != smslinks.same_link("https://link.agentleadlab.com/widget/form/other")


def test_her_standing_links_are_the_ones_she_sends_everybody():
    texts = [
        _text("1", f"Fill this in please {FORM}", agent="1"),
        _text("2", f"Here's the form {FORM}?utm_source=sms", agent="2"),
        _text("3", f"Your sheet: {SHEET}", agent="1"),
        _text("4", f"{FORM}", agent="3", inbound=True),        # an agent sending it
        _text("5", "https://trello.com/c/xyz", agent="4", team=True),  # Tre
    ]

    everything = smslinks.catalog(texts)
    standing = smslinks.standing(texts)

    assert [(one["kind"], one["times"], one["agents"]) for one in everything] == [
        ("form", 2, 2), ("sheet", 1, 1),
    ]
    assert everything[0]["said"] == f"Here's the form {FORM}?utm_source=sms"
    assert [one["kind"] for one in standing] == ["form"]


def test_past_texts_are_searched_newest_first():
    texts = [
        _text("2026-09-01", "leads stopped coming", inbound=True, agent="1"),
        _text("2026-09-20", "my leads stopped today", inbound=True, agent="2"),
        _text("2026-09-21", "leads are great", inbound=True, agent="2"),
    ]

    assert [one.at for one in smslinks.search(texts, "Leads STOPPED")] == ["2026-09-20", "2026-09-01"]
    assert [one.at for one in smslinks.search(texts, "leads stopped", agent="1")] == ["2026-09-01"]
    assert smslinks.search(texts, "a an") == []


# ------------------------------------------------------------ opening links


class Sheets:
    def __init__(self):
        self.closed = False

    def tabs(self, sheet):
        return [{"title": "September", "sheetId": 77}, {"title": "Old", "sheetId": 1}]

    def tab_named(self, sheet, gid):
        return {"77": "September"}.get(str(gid), "")

    def rows(self, sheet, span):
        assert span.startswith("'September'!")
        return [["Date", "Name", "State"]] + [[f"9/{at}", f"Lead {at}", "TX"] for at in range(1, 13)] + [["", ""]]

    def close(self):
        self.closed = True


def test_a_lead_sheet_is_read_for_its_newest_rows(monkeypatch):
    sheets = Sheets()
    monkeypatch.setattr(jobs, "open_sheets", lambda cfg: sheets)

    said = jobs.read_link(None, SHEET)

    assert said.startswith("a Google Sheet - tab \"September\" of 2")
    assert "12 rows under the header" in said
    assert "Header: Date | Name | State" in said
    assert said.rstrip().endswith("9/12 | Lead 12 | TX")
    assert "9/4 |" not in said, "more than the newest rows"
    assert sheets.closed


class Trello:
    def card_detail(self, card):
        assert card == "AbC123"
        return {"name": "New Agent - Adrian Pacheco", "idList": "L1", "desc": "Live Friday"}

    def board_lists(self, board):
        return [{"id": "L1", "name": "In Que"}]

    def card_comments(self, card):
        return ["setup is complete"]

    def close(self):
        pass


def test_a_trello_link_is_read_as_the_card(monkeypatch):
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Trello())

    said = jobs.read_link(NS(secrets=NS(trello_board_id="b")), "https://trello.com/c/AbC123/9-x")

    assert "\"New Agent - Adrian Pacheco\" in the \"In Que\" list" in said
    assert "Live Friday" in said and "- setup is complete" in said


def _stripe(monkeypatch, answers):
    asked = []

    def call(method, path, **kw):
        asked.append((method, path, kw.get("params")))
        assert method == "GET", "RYTE only reads Stripe here"
        got = answers(path, kw.get("params") or {})
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(stripepay, "_call", call)
    return asked


def test_a_payment_link_says_what_it_sells_and_who_paid(monkeypatch):
    def answers(path, params):
        if path == "payment_links":
            if not params.get("starting_after"):
                return {"data": [{"id": "plink_1", "url": "https://buy.stripe.com/other"}], "has_more": True}
            return {"data": [{"id": "plink_2", "url": "https://buy.stripe.com/5kA3cd", "active": True}]}
        if path == "payment_links/plink_2/line_items":
            return {"data": [{"quantity": 25, "description": "OTP Trucker IUL", "amount_total": 70000}]}
        if path == "checkout/sessions":
            assert params["payment_link"] == "plink_2"
            return {"data": [{"created": 1790000000, "status": "complete", "payment_status": "paid",
                              "customer_details": {"name": "Adrian Pacheco", "email": "a@x.com"}}]}
        raise AssertionError(path)

    _stripe(monkeypatch, answers)

    said = jobs.read_link(None, "https://buy.stripe.com/5kA3cd?prefilled_email=a")

    assert "sells 25 × OTP Trucker IUL ($700" in said and "active" in said
    assert "Adrian Pacheco <a@x.com> - complete, paid" in said


def test_links_ryte_cannot_open_say_so_rather_than_guess(monkeypatch):
    assert "doesn't open these" in jobs.read_link(None, "https://docs.google.com/document/d/x")
    assert "look the agent up in Stripe" in jobs.read_link(None, "https://invoice.stripe.com/i/a/b")
    monkeypatch.setattr(jobs, "open_sheets", lambda cfg: (_ for _ in ()).throw(RuntimeError("no Google")))
    assert jobs.read_link(None, SHEET) == "a Google Sheet - couldn't open it: no Google"


# ---------------------------------------------------------- Stripe and GHL


def test_an_agent_is_found_in_stripe_with_their_invoices(monkeypatch):
    def answers(path, params):
        if path == "customers/search":
            return {"data": [{"id": "cus_1", "name": "Adrian", "email": "a@x.com"}]}
        if path == "invoices":
            return {"data": [{"created": 1790000000, "number": "A-7", "status": "open",
                              "amount_due": 70000, "amount_paid": 0}]}
        if path == "charges":
            return {"data": []}
        raise AssertionError(path)

    asked = _stripe(monkeypatch, answers)

    said = jobs.stripe_customer("(312) 555-0188")

    assert asked[0][2]["query"] == "phone:'+13125550188'"
    assert "#A-7 open: due $700" in said and "Payments, newest first:\n- none" in said
    jobs.stripe_customer("a@x.com")
    jobs.stripe_customer("Adrian O'Neil")
    assert [one[2]["query"] for one in asked if one[1] == "customers/search"][1:] == [
        "email:'a@x.com'", "name~'Adrian ONeil'",
    ]


def test_nobody_in_stripe_is_said(monkeypatch):
    _stripe(monkeypatch, lambda path, params: {"data": []})

    assert jobs.stripe_customer("Nobody") == "No Stripe customer for Nobody."


def test_a_ghl_contact_is_found_by_one_search(monkeypatch):
    from wilbyte import ghl

    class GHL:
        def __init__(self, token, location):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def _ask_for(self, asked):
            return [
                {"firstName": "Adrian", "lastName": "Pacheco", "phone": "+13125550188",
                 "email": "a@x.com", "tags": ["otp trucker"], "dateAdded": "2026-09-01T00:00"},
                {"firstName": "Someone", "phone": "+19995550000"},
            ]

        def find_contacts(self, **kw):
            raise AssertionError("walked every contact")

    monkeypatch.setattr(ghl, "GHLClient", GHL)
    config = NS(secrets=NS(ghl_api_token="t", ghl_location_id="l", require=lambda *a: None))

    said = jobs.ghl_contact(config, "312-555-0188")

    assert said == "Adrian Pacheco <a@x.com> +13125550188 - added 2026-09-01; tags: otp trucker"


# ------------------------------------------------------ pages off the web


class Page:
    def __init__(self, status=200, body=b"", location=""):
        self.status_code, self.body = status, body
        self.headers = {"location": location} if location else {}
        self.is_redirect = bool(location)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def iter_bytes(self):
        yield self.body


def _web(monkeypatch, pages, addresses):
    import socket

    import httpx

    fetched = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port: [(0, 0, 0, "", (addresses.get(host, "93.184.216.34"), 0))])

    def stream(method, url, **kw):
        fetched.append(url)
        assert kw.get("follow_redirects") is False
        return pages[url]

    monkeypatch.setattr(httpx, "stream", stream)
    return fetched


def test_a_public_page_is_read_for_its_title_and_words(monkeypatch):
    _web(monkeypatch, {"https://agentleadlab.com/faq": Page(body=(
        b"<html><head><title>FAQ &amp; Help</title><meta name=\"description\" "
        b"content=\"Answers\"><script>var x=1</script></head><body><p>Leads go out daily.</p></body></html>"
    ))}, {})

    said = jobs.read_link(None, "https://agentleadlab.com/faq")

    assert said == "a page on agentleadlab.com - \"FAQ & Help\". Answers. It reads: FAQ & Help Leads go out daily."


def test_nothing_on_the_office_network_is_fetched(monkeypatch):
    fetched = _web(monkeypatch, {
        "https://short.ly/x": Page(status=302, location="http://router.local/admin"),
    }, {"router.local": "192.168.1.1", "localhost": "127.0.0.1"})

    assert "not a public address" in jobs.read_link(None, "http://localhost:8080/x")
    assert "not a public address" in jobs.read_link(None, "https://short.ly/x")
    assert fetched == ["https://short.ly/x"], "followed a redirect onto the office network"


# ------------------------------------------------------------ the look-ups


def test_each_look_up_answers_in_words_and_leaves_a_trail(monkeypatch):
    monkeypatch.setattr(jobs, "read_link", lambda cfg, link: f"opened {link}")
    monkeypatch.setattr(jobs, "agent_on_the_board",
                        lambda cfg, number, name="": {"agent": "Adrian", "url": "https://trello.com/c/a"} if number == "3125550188" else None)
    monkeypatch.setattr(jobs, "stripe_customer", lambda who: f"stripe {who}")
    monkeypatch.setattr(jobs, "ghl_contact", lambda cfg, who: (_ for _ in ()).throw(RuntimeError("token expired")))
    texts = [_text("2026-09-20", "leads stopped", inbound=True, agent="1", name="Jay")]
    look = jobs.RingLookups(None, texts, agent="3125550188")

    assert look.run("open_link", {"link": SHEET}) == f"opened {SHEET}"
    assert "Card: https://trello.com/c/a" in look.run("trello_card", {"who": "312 555 0188"})
    assert look.run("trello_card", {"who": "Nobody"}) == "No New Agent card on the board for Nobody."
    assert look.run("stripe_customer", {"who": "a@x.com"}) == "stripe a@x.com"
    assert look.run("ghl_contact", {"who": "x"}) == "Couldn't look that up: token expired"
    assert look.run("search_texts", {"words": "leads stopped"}) == "2026-09-20 Jay (Agent): leads stopped"
    assert look.run("search_texts", {"words": "leads", "this_agent_only": True}) == 'No texts with "leads" in them.'
    assert look.run("send_text", {}) == "There is no look-up called send_text."
    assert look.checked == ["the lead sheet", "their Trello card", "Trello (no card)", "Stripe",
                            "GHL (couldn't)", "past texts for “leads stopped”",
                            "past texts for “leads”"]


def test_a_long_answer_is_cut_to_size(monkeypatch):
    monkeypatch.setattr(jobs, "read_link", lambda cfg, link: "x" * 10000)

    got = jobs.RingLookups(None, []).run("open_link", {"link": "https://a.com/b"})

    assert len(got) == jobs.LOOKUP_CHARS + 1


def test_nothing_it_can_look_up_writes_anything():
    names = {one["name"] for one in jobs.RingLookups(None, []).tools()}

    assert names == {"open_link", "trello_card", "stripe_customer", "ghl_contact", "search_texts"}


# ------------------------------------------------ thinking, then replying


class Thinking:
    """Claude looking things up first: each answer in turn."""

    def __init__(self, turns):
        self.turns, self.asked = list(turns), []
        self.messages = self

    def create(self, **kw):
        import copy

        self.asked.append(copy.deepcopy(kw))
        return self.turns.pop(0)


def _call(name, input, id="t1"):
    return NS(type="tool_use", name=name, input=input, id=id)


REPLY = {"name": "reply", "input_schema": {}}


class Look:
    def __init__(self):
        self.ran, self.checked = [], []

    def tools(self):
        return [{"name": "open_link"}]

    def run(self, name, asked):
        self.ran.append((name, asked))
        self.checked.append("the lead sheet")
        return "last lead 9/23"


def test_claude_can_look_before_it_replies():
    client = Thinking([
        NS(content=[NS(type="text", text="Let me check the sheet."),
                    _call("open_link", {"link": SHEET})]),
        NS(content=[_call("reply", {"reply": "Your last lead came in 9/23!"}, id="t2")]),
    ])
    look = Look()

    got = jobs._think_then_reply(client, model="m", system="s", prompt="p",
                                 reply_tool=REPLY, look=look)

    assert got == {"reply": "Your last lead came in 9/23!"}
    assert look.ran == [("open_link", {"link": SHEET})]
    first, second = client.asked
    assert first["tool_choice"] == {"type": "any"}
    assert [one["name"] for one in first["tools"]] == ["reply", "open_link"]
    assert second["messages"][1]["content"][0] == {"type": "text", "text": "Let me check the sheet."}
    assert second["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "last lead 9/23"},
    ]


def test_without_look_ups_it_just_replies():
    client = Thinking([NS(content=[_call("reply", {"reply": "Hi!"})])])

    jobs._think_then_reply(client, model="m", system="s", prompt="p", reply_tool=REPLY)

    assert client.asked[0]["tool_choice"] == {"type": "tool", "name": "reply"}
    assert client.asked[0]["tools"] == [REPLY]


def test_after_a_few_look_ups_it_has_to_reply(monkeypatch):
    monkeypatch.setattr(jobs, "LOOKUPS", 2)
    client = Thinking([
        NS(content=[_call("open_link", {"link": "a"})]),
        NS(content=[_call("open_link", {"link": "b"})]),
        NS(content=[_call("reply", {"reply": "ok"})]),
    ])

    jobs._think_then_reply(client, model="m", system="s", prompt="p", reply_tool=REPLY, look=Look())

    assert client.asked[-1]["tool_choice"] == {"type": "tool", "name": "reply"}
    assert client.asked[-1]["tools"] == [REPLY]


def test_no_reply_at_all_is_a_failure():
    client = Thinking([NS(content=[NS(type="text", text="hmm")])])

    with pytest.raises(ValueError):
        jobs._think_then_reply(client, model="m", system="s", prompt="p", reply_tool=REPLY, look=Look())


def test_the_draft_opens_the_links_and_says_what_it_checked(monkeypatch):
    import anthropic

    from wilbyte import smsreplies

    client = Thinking([NS(content=[_call("reply", {"reply": "Hi!", "why": "x", "blanks": []})])])
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: client)
    config = NS(secrets=NS(anthropic_api_key="k", require=lambda *a: None), copy=NS(model="m"))
    done = [smsreplies.Exchange(agent="1", name="", asked="hi", answered="hey", at="1")]
    look = Look()

    got = jobs.draft_like_faith(
        config, asked="is this my sheet?", done=done,
        thread=[_text("1", f"is this my sheet? {SHEET}", inbound=True)],
        links=[{"what": "a form", "link": FORM, "times": 40, "agents": 31, "said": "Fill this in"}],
        look=look,
    )

    prompt = client.asked[0]["messages"][0]["content"]
    assert f"WHAT THE LINKS IN THESE TEXTS ARE" in prompt and f"{SHEET}\nlast lead 9/23" in prompt
    assert f"- a form: {FORM} - sent 40 times to 31 agents" in prompt
    assert "check it rather than guess" in client.asked[0]["system"]
    assert got["checked"] == ["the lead sheet"]


def test_the_ping_says_what_was_checked():
    from wilbyte.bot import client

    note = client._ring_notes({"why": "like her", "checked": ["Stripe", "the lead sheet", "Stripe"]})

    assert note == "-# like her · checked Stripe, the lead sheet"


def test_a_lesson_is_explained_knowing_what_its_links_are(monkeypatch):
    import anthropic

    client = Thinking([NS(content=[_call("lesson", {"why": "She sent the form.", "rule": "Send the form.", "kind": "link"})])])
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: client)
    config = NS(secrets=NS(anthropic_api_key="k", require=lambda *a: None), copy=NS(model="m"))

    jobs.explain_the_change(config, {"asked": "how do I start", "suggested": "Hi!",
                                     "sent": f"Fill this in {FORM}"},
                            opened=[(FORM, "a form - \"Onboarding\"")])

    assert f"WHAT THE LINKS IN IT ARE, opened just now:\n\n{FORM}\na form - \"Onboarding\"" in client.asked[0]["messages"][0]["content"]


def test_the_links_in_a_changed_suggestion_are_opened_to_learn_from(monkeypatch):
    from wilbyte import ringtexts

    ringtexts.save({"texts": [], "lessons": [{
        "asked": "how do I start", "suggested": "Hi!", "sent": f"Fill this in {FORM}",
        "at": "2026-09-24T10:05", "asked_at": "2026-09-24T10:00", "key": "C-1",
        "agent": "1", "same": False, "kept": 0.0,
    }]})
    monkeypatch.setattr(jobs, "agent_on_the_board", lambda cfg, n, name="": None)
    monkeypatch.setattr(jobs, "read_link", lambda cfg, link: f"opened {link}")
    seen = {}

    def change(cfg, one, **kw):
        seen.update(kw)
        return {"why": "w", "rule": "r", "kind": "link"}

    monkeypatch.setattr(jobs, "explain_the_change", change)

    jobs.ring_study(NS(secrets=None))

    assert seen["opened"] == [(FORM, f"opened {FORM}")]


def test_the_playbook_is_told_which_links_she_sends(monkeypatch):
    import anthropic

    from wilbyte import smsreplies

    client = Thinking([NS(content=[NS(type="text", text="## Playbook")])])
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: client)
    config = NS(secrets=NS(anthropic_api_key="k", require=lambda *a: None), copy=NS(model="m"))
    done = [smsreplies.Exchange(agent="1", name="", asked=f"q{at}", answered="a", at=str(at)) for at in range(25)]

    jobs.faith_playbook(config, done, [], [{"what": "a form", "link": FORM, "times": 40,
                                            "agents": 31, "said": "Fill this in"}])

    prompt = client.asked[0]["messages"][0]["content"]
    assert f"- a form: {FORM} - 40 times, 31 agents" in prompt
    assert "**Links she sends**" in prompt
