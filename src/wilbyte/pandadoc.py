"""PandaDoc, read for signed contracts.

The signed contract is the thing a dispute is lost without, and PandaDoc does
not email it - "pandadoc doesnt send contract on gmail, we have to download it
on pandadoc". So it is read where it lives.

Read only. The client has one method and it is a GET; nothing here can send,
create, change or delete a document.
"""

from __future__ import annotations

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

    def completed(self, *, count: int = 5) -> list[dict]:
        """The most recently changed signed documents."""
        found = self.get(
            "/documents", status=COMPLETED, count=count,
            order_by="-date_modified",
        )
        return list(found.get("results") or [])


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
