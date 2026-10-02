"""Every order going live has a signed contract of its own.

"every order, contracts are this and the basic, tag me only".
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from wilbyte import alreadysaid
from wilbyte.contracts import CONTRACT_BEFORE_DAYS, Contract, Order, card_email, pair, same_name

DAY = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)


def _order(card, made_days, *, name="Oliver Lisowski", email="oliver@example.com"):
    return Order(card_id=card, name=name, email=email, made=DAY + timedelta(days=made_days),
                 launch=date(2026, 10, 2))


def _contract(doc, signed_days, *, person="Oliver Lisowski", emails=("oliver@example.com",),
              kind="Basic Contract (1x lead order)"):
    return Contract(doc, f"{kind} x {person}", DAY + timedelta(days=signed_days), emails)


def test_the_person_is_read_off_every_kind_of_title():
    assert _contract("d", 0, kind="Contrato Básico OTP (1x Compra de Leads)",
                     person="Mario Guajardo López").person == "Mario Guajardo López"
    assert _contract("d", 0).person == "Oliver Lisowski"


def test_an_order_with_its_contract_is_found_by_email():
    got = pair([_order("c1", 0)], [_contract("d1", -1)])
    assert got["c1"].contract.doc_id == "d1" and got["c1"].how == "email"


def test_an_order_with_none_is_missing():
    got = pair([_order("c1", 0)], [_contract("d1", 0, person="Somebody Else", emails=("x@example.com",))])
    assert got["c1"].contract is None and got["c1"].how == ""


def test_a_reorder_needs_its_own_contract():
    """"every order". Last month's contract covers last month's order."""
    first, again = _order("c1", -30), _order("c2", 0)
    got = pair([again, first], [_contract("d1", -31)])

    assert got["c1"].contract.doc_id == "d1"
    assert got["c2"].contract is None


def test_two_orders_two_contracts_each_get_one():
    got = pair([_order("c1", -5), _order("c2", 0)], [_contract("d1", -5), _contract("d2", 0)])
    assert {got["c1"].contract.doc_id, got["c2"].contract.doc_id} == {"d1", "d2"}


def test_a_contract_signed_long_before_the_order_doesnt_count():
    got = pair([_order("c1", 0)], [_contract("d1", -(CONTRACT_BEFORE_DAYS + 1))])
    assert got["c1"].contract is None


def test_a_name_with_a_different_email_needs_a_look():
    got = pair([_order("c1", 0)], [_contract("d1", 0, emails=("other@example.com",))])

    assert got["c1"].how == "name"
    assert "other@example.com, not oliver@example.com" in got["c1"].notes[0]


def test_the_email_wins_over_the_name():
    got = pair([_order("c1", 0)], [
        _contract("byname", -1, emails=("other@example.com",)),
        _contract("byemail", 0, person="O. Lisowski"),
    ])
    assert got["c1"].contract.doc_id == "byemail"


def test_names_match_whole_and_past_accents():
    assert same_name("Mario Guajardo López", "mario  guajardo lopez")
    assert not same_name("Mario Guajardo", "Mario Guajardo López")
    assert not same_name("", "")


def test_the_cards_email_is_read_off_the_form():
    assert card_email("First Name: Jo\nEmail: Jo.Example@Example.com\nPhone: x") == "jo.example@example.com"
    assert card_email("no email here") == ""


# ------------------------------------------------ the watcher


def _watch(monkeypatch, paired, *, hour=9):
    from wilbyte.bot import client

    sent = []

    class Responder:
        async def send(self, text, **kw):
            sent.append(text)

    class Bot:
        ticks = 0
        config = SimpleNamespace(
            schedule=SimpleNamespace(timezone="America/New_York"),
            secrets=SimpleNamespace(discord_notify_user_id="42"),
        )

        def is_closed(self):
            Bot.ticks += 1
            return Bot.ticks > 1

    class Clock:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 10, 2, hour, tzinfo=tz)

    monkeypatch.setattr(client, "datetime", Clock)
    monkeypatch.setattr(client, "_board_responder", lambda bot: Responder())
    monkeypatch.setattr(client, "CONTRACT_CHECK_SECONDS", 0)
    monkeypatch.setattr(client.jobs, "board_day", lambda config: date(2026, 10, 2))
    monkeypatch.setattr(client.jobs, "contract_check", lambda config, today=None: (paired, []))
    asyncio.run(client.contract_check_loop(Bot()))
    return sent


def _paired(how="", contract=None):
    from wilbyte.contracts import Paired

    order = _order("c9", 0, name="Elijah Lucas")
    return Paired(order, contract, how, ["a note"] if how == "name" else [])


def test_a_missing_contract_tags_franklin_once(monkeypatch):
    """"tag me only"."""
    first = _watch(monkeypatch, [_paired()])
    again = _watch(monkeypatch, [_paired()])

    assert first[0].startswith("<@42>") and "Elijah Lucas" in first[0]
    assert "No signed contract" in first[0]
    assert again == []


def test_signed_after_being_flagged_is_said_once(monkeypatch):
    _watch(monkeypatch, [_paired()])
    signed = _watch(monkeypatch, [_paired("email", _contract("d1", 0))])
    quiet = _watch(monkeypatch, [_paired("email", _contract("d1", 0))])

    assert "Signed since" in signed[0] and quiet == []


def test_a_contract_found_from_the_start_says_nothing(monkeypatch):
    assert _watch(monkeypatch, [_paired("email", _contract("d1", 0))]) == []


def test_nothing_before_seven_in_the_morning(monkeypatch):
    assert _watch(monkeypatch, [_paired()], hour=5) == []


def test_asking_for_it():
    from wilbyte.bot import mentions

    assert mentions.parse("contracts", max_batch=5).action == "contracts"
    assert mentions.parse("check contracts", max_batch=5).action == "contracts"


def test_only_orders_going_live_today_or_tomorrow_are_checked(monkeypatch):
    from wilbyte import pandadoc
    from wilbyte.bot import jobs

    def card_id(days_ago, name):
        made = int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())
        return f"{made:08x}" + f"{abs(hash(name)):016x}"[:16]

    def card(name, live, *, days_ago=3, closed=False):
        return {"id": card_id(days_ago, name), "name": f"NEW AGENT- {name}", "closed": closed,
                "desc": f"Email: {name.split()[0].lower()}@example.com\nLIVE {live}",
                "shortUrl": f"https://trello.com/c/{name.split()[0]}"}

    board = [
        card("Elijah Lucas", "FRI, OCT 2"),
        card("Later Person", "MON, OCT 12"),
        card("Gone Person", "FRI, OCT 2", closed=True),
        card("Old Person", "FRI, OCT 2", days_ago=90),
    ]

    class Board:
        def board_cards(self, board_id, archived=False):
            return board

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda config: Board())
    monkeypatch.setattr(pandadoc, "sync", lambda key: {"docs": {}})
    config = SimpleNamespace(secrets=SimpleNamespace(pandadoc_api_key="k", trello_board_id="b"))
    paired, problems = jobs.contract_check(config, today=date(2026, 10, 2))

    assert problems == []
    assert [one.order.name for one in paired] == ["Elijah Lucas"]
    assert paired[0].order.email == "elijah@example.com" and paired[0].contract is None


def test_no_key_says_so():
    from wilbyte.bot import jobs

    paired, problems = jobs.contract_check(SimpleNamespace(secrets=SimpleNamespace()), today=date(2026, 10, 2))
    assert paired == [] and "PANDADOC_API_KEY" in problems[0]
