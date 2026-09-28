"""Trello having a bad second, and what RYTE does about it.

From a real morning: `Setup check: GET /cards/6a9849ee... -> HTTP 503`. Trello
answered 503 once and RYTE gave up on that read. The board is walked every
twenty seconds all day, so a one-second outage in the middle of a rollover used
to abandon it half done.
"""

from __future__ import annotations

import httpx
import pytest

from wilbyte.trello import TrelloClient, TrelloError


class Answering:
    """A client that gives the prepared answers in order, and counts the asks."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def request(self, method, path, **kwargs):
        self.asked.append((method, path))
        answer = self.answers.pop(0) if self.answers else self.answers
        if isinstance(answer, Exception):
            raise answer
        return answer


def replied(status, body=b'{"id":"abc"}'):
    return httpx.Response(
        status, content=body, request=httpx.Request("GET", "https://api.trello.com/1/x")
    )


@pytest.fixture
def client(monkeypatch):
    """A client that retries without anybody waiting three seconds for it."""
    monkeypatch.setattr(TrelloClient, "_wait", staticmethod(lambda seconds: None))
    made = TrelloClient("key", "token")
    return made


def test_a_503_is_asked_again_rather_than_given_up_on(client):
    client._client = Answering(replied(503, b"temporarily unavailable"), replied(200))

    assert client._request("GET", "/cards/abc") == {"id": "abc"}
    assert len(client._client.asked) == 2


def test_a_503_that_never_clears_still_says_503(client):
    client._client = Answering(*[replied(503, b"down") for _ in range(4)])

    with pytest.raises(TrelloError) as raised:
        client._request("GET", "/cards/abc")

    assert "503" in str(raised.value)


def test_it_gives_up_rather_than_asking_forever(client):
    client._client = Answering(*[replied(503) for _ in range(9)])

    with pytest.raises(TrelloError):
        client._request("GET", "/cards/abc")

    # Three pauses, so four asks. A board job that hangs for ten minutes
    # retrying is worse than one that says Trello is down.
    assert len(client._client.asked) == 4


def test_a_first_try_that_works_is_one_request(client):
    client._client = Answering(replied(200))

    client._request("GET", "/cards/abc")

    assert len(client._client.asked) == 1


def test_a_refusal_that_is_our_fault_is_not_asked_again(client):
    """401 is a bad token and 404 is a deleted card. Neither improves by
    waiting, and both want to reach somebody quickly."""
    client._client = Answering(replied(401, b"invalid token"), replied(200))

    with pytest.raises(TrelloError):
        client._request("GET", "/cards/abc")

    assert len(client._client.asked) == 1


def test_a_connection_that_never_left_the_laptop_is_asked_again(client):
    client._client = Answering(httpx.ConnectError("no route to host"), replied(200))

    assert client._request("GET", "/cards/abc") == {"id": "abc"}


def test_a_connection_that_never_comes_back_says_so(client):
    client._client = Answering(*[httpx.ConnectError("no route") for _ in range(4)])

    with pytest.raises(TrelloError) as raised:
        client._request("GET", "/cards/abc")

    assert "failed to send" in str(raised.value)


# --------------------------------- asking twice must not do it twice


def test_a_post_is_not_repeated_after_a_503(client):
    """The 503 may have arrived after Trello made the card. Asking again would
    make a second one, and a duplicate card on the board is worse than a
    warning in the window."""
    client._client = Answering(replied(503), replied(200))

    with pytest.raises(TrelloError):
        client._request("POST", "/cards")

    assert len(client._client.asked) == 1


def test_a_post_is_repeated_when_the_rate_limiter_says_it_never_ran(client):
    """429 means Trello refused to do it, not that it might have."""
    client._client = Answering(replied(429), replied(200))

    assert client._request("POST", "/cards") == {"id": "abc"}
    assert len(client._client.asked) == 2


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_saying_what_the_answer_should_be_is_safe_to_repeat(method, client):
    """Ticking an item complete twice leaves it complete."""
    client._client = Answering(replied(503), replied(200))

    client._request(method, "/checklists/abc/checkItems/def")

    assert len(client._client.asked) == 2


# ------------------------------------------------ asking less often


def test_refused_for_asking_too_often_it_waits_out_the_window(monkeypatch):
    waited = []
    monkeypatch.setattr(TrelloClient, "_wait", staticmethod(waited.append))
    made = TrelloClient("key", "token")
    slow_down = httpx.Response(429, headers={"Retry-After": "15"}, content=b"",
                               request=httpx.Request("GET", "https://api.trello.com/1/x"))
    made._client = Answering(replied(429), slow_down, replied(429, b""), replied(200))

    assert made._request("GET", "/cards/abc") == {"id": "abc"}
    assert waited == [10.0, 15.0, 10.0], "the whole ten-second window, or longer when it says"


def test_a_long_retry_after_is_not_waited_forever(monkeypatch):
    waited = []
    monkeypatch.setattr(TrelloClient, "_wait", staticmethod(waited.append))
    made = TrelloClient("key", "token")
    made._client = Answering(
        httpx.Response(429, headers={"Retry-After": "600"}, content=b"",
                       request=httpx.Request("GET", "https://api.trello.com/1/x")),
        replied(200),
    )
    made._request("GET", "/cards/abc")
    assert waited == [30.0]


def test_a_503_still_waits_only_a_moment(monkeypatch):
    waited = []
    monkeypatch.setattr(TrelloClient, "_wait", staticmethod(waited.append))
    made = TrelloClient("key", "token")
    made._client = Answering(replied(503), replied(200))
    made._request("GET", "/cards/abc")
    assert waited == [1.0]


def test_the_whole_board_in_one_request_in_the_order_the_lists_give(client):
    import json

    cards = [
        {"id": "b2", "idList": "L2", "pos": 20}, {"id": "a2", "idList": "L1", "pos": 200},
        {"id": "b1", "idList": "L2", "pos": 10}, {"id": "a1", "idList": "L1", "pos": 100},
        {"id": "gone", "idList": "archived-list", "pos": 1},
    ]
    client._client = Answering(replied(200, json.dumps(cards).encode()))

    got = client.cards_in("board", [{"id": "L1"}, {"id": "L2"}])

    assert [card["id"] for card in got] == ["a1", "a2", "b1", "b2"]
    assert client._client.asked == [("GET", "/boards/board/cards/open")]
