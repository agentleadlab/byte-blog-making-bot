"""Reading Faith's text messages on RingCentral, and nothing else.

"i dont need RYte to send the messages, i need him to ping me on discord on
how to respond to their message the way how Faith respond."

So this reads. There is no send in here, no reply, no delete and no marking
as read - not switched off, absent - so nothing that calls this can put a
word in front of an agent. What RYTE does with what it reads is a Discord
message to Franklin, and the words go to the agent only if he sends them.

Narrow like the inbox reader is narrow: the one extension it reads is set in
.env and put into the request here rather than passed in. Reading another
user's messages needs an admin's JWT with Read Messages, which could read
anybody's - so the narrowness is this file's job, not the credential's.

Signed in with a JWT, which RingCentral trades for an access token that
lasts about two hours. The JWT itself does not expire; the token is simply
fetched again when it does.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass

import httpx

PLATFORM = "https://platform.ringcentral.com"
TOKEN_PATH = "/restapi/oauth/token"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: The most RingCentral hands back in one page of the message store.
PAGE = 1000

#: How many pages one read will follow before stopping. A first read of a few
#: months of one person's texts is a handful of pages; this stops a loop that
#: never ends if the paging ever does.
MOST_PAGES = 40


class RingError(RuntimeError):
    """Anything that stopped a read, said in English."""


@dataclass
class RingCreds:
    client_id: str
    client_secret: str
    jwt: str


class RingClient:
    """One signed-in session that reads one extension's texts."""

    def __init__(self, creds: RingCreds, *, extension: str,
                 server: str = PLATFORM, timeout: float = 30.0):
        if not (extension or "").strip():
            raise RingError(
                "No extension set. RINGCENTRAL_EXTENSION in .env is the line "
                "Faith texts agents from - its extension number or its name - "
                "and it is the only one this reads."
            )
        self._creds = creds
        self._extension = extension.strip()
        self._server = (server or PLATFORM).rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self._token = ""
        self._token_until = 0.0
        self._extension_id = ""
        self._owner = ""

    def __enter__(self) -> "RingClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ signing in

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_until:
            return self._token
        basic = base64.b64encode(
            f"{self._creds.client_id}:{self._creds.client_secret}".encode()
        ).decode()
        try:
            reply = self._client.post(
                self._server + TOKEN_PATH,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"grant_type": JWT_GRANT, "assertion": self._creds.jwt},
            )
        except httpx.HTTPError as exc:
            raise RingError(f"Couldn't reach RingCentral to sign in: {exc}") from exc
        if reply.status_code >= 400:
            raise RingError(_why_not_signed_in(reply.status_code, reply.text))
        got = reply.json()
        self._token = str(got.get("access_token") or "")
        self._token_until = time.time() + float(got.get("expires_in") or 3600) - 60
        if not self._token:
            raise RingError("RingCentral signed us in but sent no access token back.")
        return self._token

    def _get(self, path_or_url: str, params: dict | None = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else self._server + path_or_url
        try:
            reply = self._client.get(
                url, params=params,
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise RingError(f"Couldn't reach RingCentral: {exc}") from exc
        if reply.status_code == 401:
            # The token lapsed early. Once, then say so.
            self._token = ""
            raise RingError("RingCentral said the sign-in had lapsed; it will be fetched again.")
        if reply.status_code == 403:
            raise RingError(
                "RingCentral refused that. The app needs the Read Messages and "
                "Read Accounts permissions, and the JWT has to belong to an "
                "admin to read somebody else's texts."
            )
        if reply.status_code == 429:
            raise RingError("RingCentral asked us to slow down; trying again next minute.")
        if reply.status_code >= 400:
            raise RingError(f"RingCentral said {reply.status_code}: {reply.text[:200]}")
        return reply.json()

    # ------------------------------------------------------------ reading

    def extension_id(self) -> str:
        """Faith's extension, as RingCentral's own id, from its number or name.

        People know an extension by its number - "103" - and the API wants its
        internal id. Looked up rather than asked for, so .env holds the thing a
        person can read off the admin portal.
        """
        if self._extension_id:
            return self._extension_id
        wanted = self._extension.casefold()
        seen, page = [], 1
        while page <= MOST_PAGES:
            got = self._get("/restapi/v1.0/account/~/extension",
                             {"perPage": PAGE, "page": page})
            for one in got.get("records") or []:
                number = str(one.get("extensionNumber") or "")
                name = " ".join(str(one.get("name") or "").split())
                seen.append((number, name))
                if wanted in (number.casefold(), name.casefold()):
                    self._extension_id = str(one.get("id") or "")
                    self._owner = name
                    return self._extension_id
            if not (got.get("navigation") or {}).get("nextPage"):
                break
            page += 1
        # Say what was there, so fixing .env is one look rather than a hunt.
        listed = ", ".join(
            f"{number} {name}".strip() for number, name in seen[:30] if number or name
        )
        raise RingError(
            f"No extension “{self._extension}” on the account. "
            + (f"It has: {listed}." if listed else "It listed none at all.")
        )

    def owner(self) -> str:
        """Whose line this is, as RingCentral names it - "Arnold Tarpley"."""
        self.extension_id()
        return self._owner

    def own_numbers(self) -> list[str]:
        """Every phone number on this extension, as RingCentral lists them.

        A line can have more than one. Ext. 101 texts from (878) and has a
        (412) number too - both "Arnold Tarpley (me)" in the app - and a text
        from one of them into a thread is the line itself, not an agent.
        """
        got = self._get(
            f"/restapi/v1.0/account/~/extension/{self.extension_id()}/phone-number",
            {"perPage": PAGE},
        )
        return [
            str(one.get("phoneNumber") or "")
            for one in got.get("records") or [] if one.get("phoneNumber")
        ]

    def texts(self, *, since: str, until: str = "") -> list[dict]:
        """Every SMS on this one extension from `since` on, oldest first.

        `since` and `until` are ISO timestamps. Both directions: what the
        agents sent is the question, what Faith sent back is the answer, and
        the pair is the whole point.
        """
        path = f"/restapi/v1.0/account/~/extension/{self.extension_id()}/message-store"
        params = {"messageType": "SMS", "availability": "Alive",
                  "dateFrom": since, "perPage": PAGE}
        if until:
            params["dateTo"] = until
        found, url, pages = [], path, 0
        while url and pages < MOST_PAGES:
            got = self._get(url, params if pages == 0 else None)
            found.extend(got.get("records") or [])
            url = str(((got.get("navigation") or {}).get("nextPage") or {}).get("uri") or "")
            pages += 1
        found.sort(key=lambda one: str(one.get("creationTime") or ""))
        return found


def _why_not_signed_in(code: int, body: str) -> str:
    said = (body or "")[:300]
    if "invalid_client" in said or "unauthorized_client" in said:
        return (
            "RingCentral didn't accept the app. Check RINGCENTRAL_CLIENT_ID and "
            "RINGCENTRAL_CLIENT_SECRET, and that the app uses the JWT auth flow."
        )
    if "invalid_grant" in said:
        return (
            "RingCentral didn't accept the JWT. It may have been revoked, or be "
            "restricted to other apps - check RINGCENTRAL_JWT."
        )
    return f"RingCentral wouldn't sign us in ({code}): {said[:160]}"


def open_ring(secrets) -> RingClient:
    """A signed-in reader for Faith's extension, or a sentence about what's missing."""
    missing = [
        name for name, value in (
            ("RINGCENTRAL_CLIENT_ID", getattr(secrets, "ringcentral_client_id", "")),
            ("RINGCENTRAL_CLIENT_SECRET", getattr(secrets, "ringcentral_client_secret", "")),
            ("RINGCENTRAL_JWT", getattr(secrets, "ringcentral_jwt", "")),
            ("RINGCENTRAL_EXTENSION", getattr(secrets, "ringcentral_extension", "")),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise RingError("RingCentral isn't set up: " + ", ".join(missing) + " missing in .env.")
    return RingClient(
        RingCreds(
            secrets.ringcentral_client_id.strip(),
            secrets.ringcentral_client_secret.strip(),
            secrets.ringcentral_jwt.strip(),
        ),
        extension=secrets.ringcentral_extension,
    )


def configured(secrets) -> bool:
    """Whether there is enough in .env to try."""
    return all(
        (getattr(secrets, name, "") or "").strip()
        for name in ("ringcentral_client_id", "ringcentral_client_secret",
                     "ringcentral_jwt", "ringcentral_extension")
    )
