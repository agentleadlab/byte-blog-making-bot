"""PandaDoc, read for signed contracts.

The signed contract is the thing a dispute is lost without, and PandaDoc does
not email it - "pandadoc doesnt send contract on gmail, we have to download it
on pandadoc". So it is read where it lives.

Read only. The client has one method and it is a GET; nothing here can send,
create, change or delete a document.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

BASE = "https://api.pandadoc.com/public/v1"

#: PandaDoc's number for a document everybody has signed.
COMPLETED = 2


class PandaDocError(RuntimeError):
    """Anything that stopped a read, said in English."""


class PandaDoc:
    def __init__(self, key: str, *, timeout: float = 30.0):
        import httpx

        if not (key or "").strip():
            raise PandaDocError("PANDADOC_API_KEY isn't in .env.")
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"API-Key {key.strip()}", "Accept": "application/json"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._http.close()

    def get(self, path: str, **params) -> dict:
        import httpx

        try:
            got = self._http.get(BASE + path, params=params)
        except httpx.HTTPError as exc:
            raise PandaDocError(f"Couldn't reach PandaDoc: {exc}") from exc
        if got.status_code == 401:
            raise PandaDocError(
                "PandaDoc says the key isn't valid (HTTP 401). Check PANDADOC_API_KEY "
                "in .env - the whole key, nothing around it."
            )
        if got.status_code == 403:
            raise PandaDocError(
                "PandaDoc refused (HTTP 403) - usually the plan doesn't include API "
                "access, or the key belongs to a different workspace."
            )
        if got.status_code == 429:
            raise PandaDocError("PandaDoc says too many requests - try again in a minute.")
        if got.status_code >= 400:
            raise PandaDocError(f"PandaDoc said HTTP {got.status_code}: {got.text[:200]}")
        return got.json()

    def pdf(self, doc_id: str) -> bytes:
        """The signed document as a PDF - PandaDoc's own download, a GET."""
        import httpx

        try:
            got = self._http.get(f"{BASE}/documents/{doc_id}/download")
        except httpx.HTTPError as exc:
            raise PandaDocError(f"Couldn't reach PandaDoc: {exc}") from exc
        if got.status_code >= 400:
            raise PandaDocError(f"PandaDoc wouldn't hand over the PDF (HTTP {got.status_code}).")
        return got.content

    def search(self, words: str, *, count: int = 50) -> list[dict]:
        """Signed documents whose name has these words in it, any age."""
        found = self.get("/documents", status=COMPLETED, q=words, count=count)
        return list(found.get("results") or [])

    def completed(self, *, count: int = 5) -> list[dict]:
        """The most recently changed signed documents."""
        found = self.get(
            "/documents", status=COMPLETED, count=count,
            order_by="-date_modified",
        )
        return list(found.get("results") or [])


    def completed_since(self, since: str, *, page: int = 1, count: int = 100) -> list[dict]:
        """Signed documents changed since then, oldest first, a page at a time."""
        found = self.get(
            "/documents", status=COMPLETED, modified_from=since,
            order_by="date_modified", count=count, page=page,
        )
        return list(found.get("results") or [])

    def details(self, doc_id: str) -> dict:
        return self.get(f"/documents/{doc_id}/details")


# ------------------------------------------------ kept in step, like Payra

CONTRACTS_PATH = _state_dir() / "pandadoc-contracts.json"

#: How far back the first read goes.
FIRST_DAYS = 90

#: Documents whose recipients are read in one pass. The first read is a few
#: hundred; the rest are read on the passes after it rather than all at once.
DETAILS_PER_PASS = 60


def load(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or CONTRACTS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("docs"), dict):
        data = {"docs": {}}
    return data


def save(data: dict, path: Path | None = None) -> None:
    where = path or CONTRACTS_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(data), encoding="utf-8")
    spare.replace(where)


def sync(key: str, *, now: datetime | None = None, path: Path | None = None,
         client=None) -> dict:
    """Read what was signed since last time, and who it was sent to."""
    now = now or datetime.now(timezone.utc)
    data = load(path)
    since = data.get("since") or (now - timedelta(days=FIRST_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reading = client or PandaDoc(key)
    try:
        latest = since
        for page in range(1, 50):
            got = reading.completed_since(since, page=page)
            for one in got:
                doc_id = str(one.get("id") or "")
                if not doc_id:
                    continue
                held = data["docs"].setdefault(doc_id, {})
                held["title"] = str(one.get("name") or "")
                held["modified"] = str(one.get("date_modified") or "")
                latest = max(latest, held["modified"] or latest)
            if len(got) < 100:
                break
        waiting = [doc_id for doc_id, one in data["docs"].items() if "emails" not in one]
        for doc_id in waiting[:DETAILS_PER_PASS]:
            told = reading.details(doc_id)
            held = data["docs"][doc_id]
            held["emails"] = sorted({
                str(one.get("email") or "").casefold()
                for one in told.get("recipients") or [] if one.get("email")
            })
            held["signed"] = str(told.get("date_completed") or held.get("modified") or "")
    finally:
        if client is None:
            reading.__exit__(None, None, None)
    # A day back over the last one seen, so nothing finishing at the same
    # moment as the read is missed.
    try:
        back = datetime.fromisoformat(latest.replace("Z", "+00:00")) - timedelta(days=1)
        data["since"] = back.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        data["since"] = since
    data["synced_at"] = now.isoformat()
    save(data, path)
    return data


def contracts(data: dict) -> list:
    """What is held, as the matching wants it. Only documents whose
    recipients have been read - one still waiting is not yet a contract
    anybody can be matched to by email."""
    from .contracts import Contract

    found = []
    for doc_id, one in (data.get("docs") or {}).items():
        if "emails" not in one:
            continue
        try:
            signed = datetime.fromisoformat(str(one.get("signed") or one.get("modified")).replace("Z", "+00:00"))
        except ValueError:
            continue
        found.append(Contract(doc_id, str(one.get("title") or ""), signed, tuple(one["emails"])))
    return found


def find_signed(key: str, *, name: str, email: str = "", paid=None, client=None):
    """The signed contract for one customer's order. (Contract or None, how, problem).

    For a dispute: searched by name across every signed document, whatever
    its age, then each one's recipients read. The email on the dispute is what
    makes it theirs; a name alone is said to be a name alone. Of theirs, the
    last one signed on or before the payment (a few days' grace, for a
    contract signed just after paying) - "every order" has its own, and the
    disputed charge is the one this is for.
    """
    from datetime import date as _date

    from .contracts import Contract, same_name

    if not (name or "").strip():
        return None, "", "No name to look the contract up by."
    reading = client or PandaDoc(key)
    try:
        hits = reading.search(name)
        mine = []
        for one in hits:
            contract = Contract(str(one.get("id") or ""), str(one.get("name") or ""),
                                datetime.now(timezone.utc))
            if not same_name(contract.person, name):
                continue
            told = reading.details(contract.doc_id)
            emails = tuple(sorted({
                str(r.get("email") or "").casefold()
                for r in told.get("recipients") or [] if r.get("email")
            }))
            signed = str(told.get("date_completed") or one.get("date_modified") or "")
            try:
                when = datetime.fromisoformat(signed.replace("Z", "+00:00"))
            except ValueError:
                continue
            mine.append(Contract(contract.doc_id, contract.title, when, emails))
    finally:
        if client is None:
            reading.__exit__(None, None, None)
    if not mine:
        return None, "", f"No signed contract for “{name}” in PandaDoc."
    wanted = (email or "").strip().casefold()
    by_email = [one for one in mine if wanted and wanted in one.emails]
    pool, how = (by_email, "email") if by_email else (mine, "name")
    if isinstance(paid, _date):
        latest = paid + timedelta(days=3)
        before = [one for one in pool if one.signed.date() <= latest]
        pool = before or pool
    return max(pool, key=lambda one: one.signed), how, ""


def test(key: str) -> str:
    """What the key can see, for `@RYTE pandadoc test`."""
    try:
        with PandaDoc(key) as reading:
            signed = reading.completed(count=5)
    except PandaDocError as exc:
        return f"❌ {exc}"
    if not signed:
        return ("✅ PandaDoc answered - the key works - but it shows no signed documents. "
                "If you have signed contracts, the key may be for a different workspace.")
    lines = [f"• {one.get('name') or '(no name)'} — signed "
             f"{str(one.get('date_modified') or '')[:10]}" for one in signed]
    return "✅ PandaDoc is connected (read-only). Latest signed documents:\n" + "\n".join(lines)
