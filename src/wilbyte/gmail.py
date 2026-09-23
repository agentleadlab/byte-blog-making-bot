"""The payment confirmation, fetched from the inbox it was emailed to.

Summit Pay - Payra - has no API worth the name: "we cant get direct api here,,
but the invoices get emailed to me so we can use that way". So the receipt for
a disputed transaction is found where it actually is, rather than somebody
saving it out of Gmail and dragging it into Discord in the middle of writing a
rebuttal.

The receipt is the email. Payra's confirmation has no attachment on it - the
reference, the day it was paid, the customer, the card and the total are a
table in the body, and the invoice number is a link to a page rather than a
file. So the body is the exhibit and an attachment is a bonus.

Read-only, and narrow by construction. Every search is pinned to one sender -
the address the invoices come from, set in .env - so there is no call here
that can read anything else in the inbox, whatever it is asked for. The one
thing RYTE takes out is an attachment on a message from that address.

Signing in is the same OAuth client as everything else - the client is the
app rather than the person - with its own refresh token when the invoices
arrive at a different Google account than the one that owns the sheets, which
they do. A refresh token belongs to one account.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from .gsheets import (
    Credentials, SheetsError, credentials, explain_token, why_refused,
)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: How far back to look for an invoice. A disputed transaction can be months
#: old - Jose Zambrano's was June, disputed in September - so this is wide.
LOOK_BACK_DAYS = 400

#: Attachments worth taking, on the emails that have any. Payra's confirmation
#: does not: the receipt is the email itself, and the invoice number in it is a
#: link to a page rather than a file. So an attachment is a bonus here and the
#: body is the evidence.
KEEPS = re.compile(r"\.(pdf|png|jpe?g)$", re.IGNORECASE)

_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_SPACES = re.compile(r"[ \t]*\n[ \t]*")


class GmailError(RuntimeError):
    """Anything that stopped a search or a download, said in English."""


@dataclass
class Found:
    """One email, and the files on it."""

    message_id: str
    subject: str = ""
    when: str = ""
    #: The receipt itself. Payra's confirmation is a table in the body - the
    #: reference, who paid, the card, the total - and that is the exhibit.
    body: str = ""
    files: list = field(default_factory=list)


@dataclass
class Attached:
    """One file off an email, already downloaded."""

    name: str
    data: bytes


class GmailClient:
    """One signed-in session. Searches one sender, reads, downloads. No more."""

    def __init__(
        self,
        creds: Credentials,
        *,
        sender: str,
        timeout: float = 30.0,
        setting: str = "GMAIL_INVOICE_SENDER",
        what: str = "Summit Pay's invoices arrive from",
    ):
        if not (sender or "").strip():
            raise GmailError(
                f"No sender set. {setting} in .env is the address {what}, and "
                "every search is pinned to it - without one there is nothing "
                "to search."
            )
        self._creds = creds
        self._sender = sender.strip()
        self._client = httpx.Client(timeout=timeout)
        self._token = ""
        self._token_until = 0.0

    def __enter__(self) -> "GmailClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _access_token(self) -> str:
        import time

        if self._token and time.time() < self._token_until:
            return self._token
        try:
            reply = self._client.post(
                TOKEN_URL,
                data={
                    "client_id": self._creds.client_id,
                    "client_secret": self._creds.client_secret,
                    "refresh_token": self._creds.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise GmailError(f"Couldn't reach Google to sign in: {exc}") from exc
        if reply.status_code >= 400:
            raise GmailError(explain_token(reply.status_code, reply.text))
        got = reply.json()
        self._token = str(got.get("access_token") or "")
        self._token_until = time.time() + float(got.get("expires_in") or 3600) - 60
        if not self._token:
            raise GmailError("Google signed us in but sent no access token back.")
        return self._token

    def _get(self, path: str, **params) -> dict:
        try:
            reply = self._client.get(
                f"{API}{path}",
                params=params or None,
                headers={"Authorization": f"Bearer {self._access_token()}"},
            )
        except httpx.HTTPError as exc:
            raise GmailError(f"Couldn't reach Gmail: {exc}") from exc
        if reply.status_code == 403:
            # What Google said, not what we assume it meant. A scope that was
            # never ticked and an API that was never enabled on the project
            # are the same 403 and different afternoons to fix.
            raise GmailError(
                "Gmail refused that: "
                + (why_refused(reply.text) or "no reason given")
                + "\n-# If that mentions a scope, the one it wants is "
                "https://www.googleapis.com/auth/gmail.readonly."
            )
        if reply.status_code >= 400:
            raise GmailError(f"Gmail said {reply.status_code}: {reply.text[:200]}")
        return reply.json()

    # ------------------------------------------------------------- searching

    def whoami(self) -> tuple[str, int]:
        """(the address this token belongs to, how many messages it can see).

        A refresh token belongs to one account, and a token minted against the
        wrong one looks exactly like an empty inbox: every search comes back
        with nothing and none of it is an error. This is the one call that
        says which mailbox is actually being read.
        """
        got = self._get("/profile")
        return str(got.get("emailAddress") or ""), int(got.get("messagesTotal") or 0)

    def invoices_for(self, *terms: str, since: date | None = None) -> list[Found]:
        """Emails from the invoice sender that mention all of `terms`.

        The sender is not a term - it is the whole search's boundary, and it
        is put there here rather than passed in, so no caller can widen it.
        """
        # No "has:attachment". Payra's confirmation carries none, and asking
        # for one found nothing at all - which is the whole of what is being
        # looked for.
        wanted = [f"from:{self._sender}"]
        for term in terms:
            said = " ".join(str(term or "").split())
            if said:
                wanted.append(f'"{said}"')
        start = since or (date.today() - timedelta(days=LOOK_BACK_DAYS))
        wanted.append(f"after:{start:%Y/%m/%d}")

        got = self._get("/messages", q=" ".join(wanted), maxResults=10)
        found = []
        for one in got.get("messages") or []:
            message_id = str(one.get("id") or "")
            if message_id:
                found.append(self.read(message_id))
        return found

    def read(self, message_id: str) -> Found:
        """One email's subject, date and what is attached to it."""
        got = self._get(f"/messages/{message_id}", format="full")
        headers = {
            str(one.get("name") or "").casefold(): str(one.get("value") or "")
            for one in ((got.get("payload") or {}).get("headers") or [])
        }
        found = Found(
            message_id=message_id,
            subject=headers.get("subject", ""),
            when=headers.get("date", ""),
        )
        payload = got.get("payload") or {}
        found.body = _body_of(payload)
        found.files = _attachments_in(payload)
        return found

    def download(self, message_id: str, attachment_id: str) -> bytes:
        """The bytes of one attachment on one of that sender's emails."""
        got = self._get(
            f"/messages/{message_id}/attachments/{attachment_id}"
        )
        raw = str(got.get("data") or "")
        if not raw:
            return b""
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _body_of(part: dict) -> str:
    """The email as readable text. Plain if it has any, else the HTML undone.

    Payra sends both, and the plain part is the one written for a person -
    but the receipt table only exists in the HTML on some of them, so the
    markup is stripped rather than the email given up on.
    """
    plain = _first_part(part, "text/plain")
    if plain.strip():
        return _tidy(plain)
    return _tidy(_TAGS.sub(" ", _first_part(part, "text/html")))


def _first_part(part: dict, kind: str) -> str:
    """The first body of this type anywhere in the email."""
    if str(part.get("mimeType") or "") == kind:
        raw = str((part.get("body") or {}).get("data") or "")
        if raw:
            return base64.urlsafe_b64decode(
                raw + "=" * (-len(raw) % 4)
            ).decode("utf-8", "replace")
    for inside in part.get("parts") or []:
        found = _first_part(inside, kind)
        if found:
            return found
    return ""


def _tidy(text: str) -> str:
    """Readable: no runs of blank lines, no trailing spaces, no &amp;."""
    import html

    said = html.unescape(text or "")
    said = _SPACES.sub("\n", said)
    while "\n\n\n" in said:
        said = said.replace("\n\n\n", "\n\n")
    return said.strip()


def _attachments_in(part: dict) -> list:
    """[(filename, attachment id)] for everything worth keeping on an email.

    Walks the parts rather than trusting the first one: a Summit Pay invoice
    arrives as a multipart with the PDF two levels down.
    """
    found = []
    name = str(part.get("filename") or "")
    body = part.get("body") or {}
    if name and KEEPS.search(name) and body.get("attachmentId"):
        found.append((name, str(body["attachmentId"])))
    for inside in part.get("parts") or []:
        found.extend(_attachments_in(inside))
    return found


def open_contracts(secrets) -> GmailClient:
    """A reader pinned to whoever sends the signed contracts.

    The same narrowness as the invoice reader and for the same reason: the
    sender is the boundary of what can be read, and it is set here rather
    than passed in so nothing calling this can widen it.

    Written for PandaDoc on the belief that it emails the completed PDF. On
    this account it does not - the PDF is downloaded from PandaDoc - so this
    finds nothing unless contracts reach the inbox some other way.
    """
    return GmailClient(
        _signed_in(secrets),
        sender=getattr(secrets, "gmail_contract_sender", ""),
        setting="GMAIL_CONTRACT_SENDER",
        what="the signed contracts arrive from, \"pandadoc.com\" for a whole domain",
    )


def open_gmail(secrets) -> GmailClient:
    """A signed-in client, or a plain sentence about what is missing.

    The invoices arrive at a different Google account than the one that owns
    the sheets, and a refresh token belongs to one account - so GMAIL_REFRESH_
    TOKEN is that account's, minted against the same OAuth client. The client
    is the app rather than the person, so only the token changes.

    Left blank, Gmail signs in as everything else does, which is right when
    the invoices come to the same address.
    """
    return GmailClient(
        _signed_in(secrets), sender=getattr(secrets, "gmail_invoice_sender", "")
    )


def _signed_in(secrets) -> Credentials:
    """The credentials the inbox is read with, whoever is being read."""
    try:
        creds = credentials(secrets)
    except SheetsError as exc:
        raise GmailError(str(exc).replace("Google Sheets", "Gmail")) from exc
    instead = (getattr(secrets, "gmail_refresh_token", "") or "").strip()
    if instead:
        # And its own client when it has one. A refresh token belongs to the
        # OAuth client it was minted under as much as to the person, and this
        # project has several - so a token minted against "Ryte" is refused by
        # "Ryte Leads" with "unauthorized_client" and nothing else to go on.
        creds = Credentials(
            (getattr(secrets, "gmail_client_id", "") or "").strip()
            or creds.client_id,
            (getattr(secrets, "gmail_client_secret", "") or "").strip()
            or creds.client_secret,
            instead,
        )
    return creds
