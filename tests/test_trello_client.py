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
