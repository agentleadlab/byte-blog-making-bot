"""Reading and writing Google Sheets, as the Agent Lead Lab Drive account.

Separate credentials from the YouTube ones on purpose: captions are fetched as
the channel's account and this is a different Google account entirely, so one
refresh token could never do both. Re-minting the YouTube one for this would
have broken caption fetching, which is exactly the sort of thing nobody
notices for a week.

Two jobs, both small. Count the rows in a lead masterlist, and write the
summary of those counts into one sheet.
"""

from __future__ import annotations

import os
import re
import time

import httpx

API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# What the refresh token was minted with. `spreadsheets` covers reading the
# lead masterlists and writing the summary; `drive.file` is there for making a
# new sheet later, and is not needed to write into one that already exists.
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)

OAUTH_VARS = (
    "LEADS_GOOGLE_CLIENT_ID",
    "LEADS_GOOGLE_CLIENT_SECRET",
    "LEADS_GOOGLE_REFRESH_TOKEN",
)

# Access tokens last an hour; refresh a little early rather than racing expiry.
_TTL_MARGIN = 120
_token_cache: dict[str, float | str] = {}


class SheetsError(RuntimeError):
    """Raised when Google refuses, or when the credentials aren't set."""


# docs.google.com/spreadsheets/d/<id>/edit#gid=0, or the bare id.
_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")


def sheet_id(url_or_id: str) -> str:
    """The spreadsheet id, from a pasted link or from the id itself."""
    text = (url_or_id or "").strip()
    found = _SHEET_ID.search(text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    raise SheetsError(f"That doesn't look like a Google Sheet link: {text[:120]}")


def credentials() -> tuple[str, str, str] | None:
    """(client_id, secret, refresh_token), or None when not configured."""
    found = tuple((os.getenv(name) or "").strip() for name in OAUTH_VARS)
    return found if all(found) else None  # type: ignore[return-value]


def missing_vars() -> list[str]:
    """Which of the three are still blank. Two of three behaves like none."""
    return [name for name in OAUTH_VARS if not (os.getenv(name) or "").strip()]


def configured() -> bool:
    return credentials() is not None


def access_token(*, force: bool = False) -> str:
    """Trade the refresh token for an access token, cached until it expires."""
    creds = credentials()
    if not creds:
        raise SheetsError(
            "The leads Google account isn't set up — "
            + ", ".join(missing_vars())
            + " still blank in .env."
        )

    now = time.time()
    if not force and _token_cache.get("token") and float(_token_cache.get("expires", 0)) > now:
        return str(_token_cache["token"])

    client_id, secret, refresh = creds
    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise SheetsError(f"Couldn't reach Google to refresh the token: {exc}") from exc
    if response.status_code >= 400:
        raise SheetsError(_explain_refresh(response))

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise SheetsError(f"No access token in Google's reply: {payload}")

    _token_cache["token"] = token
    _token_cache["expires"] = now + float(payload.get("expires_in", 3600)) - _TTL_MARGIN
    return str(token)


def _explain_refresh(response: httpx.Response) -> str:
    body = response.text[:300]
    if "invalid_grant" in body:
        return (
            "LEADS_GOOGLE_REFRESH_TOKEN is no longer valid. Refresh tokens die "
            "when the password changes, when access is revoked, or when the "
            "OAuth app is left in Testing rather than Internal. Mint it again "
            "at developers.google.com/oauthplayground."
        )
    if "invalid_client" in body:
        return (
            "LEADS_GOOGLE_CLIENT_ID or LEADS_GOOGLE_CLIENT_SECRET doesn't match "
            "the app the refresh token was minted for."
        )
    return f"Google refused the refresh: HTTP {response.status_code} {body}"


def _get(path: str, **params) -> dict:
    headers = {"Authorization": f"Bearer {access_token()}"}
    try:
        response = httpx.get(f"{API_ROOT}/{path}", params=params, headers=headers, timeout=60)
    except httpx.HTTPError as exc:
        raise SheetsError(f"Sheets request failed: {exc}") from exc
    if response.status_code >= 400:
        raise SheetsError(explain(response, path))
    return response.json()


def whoami() -> str:
    """Which Google account the refresh token belongs to, or "" if it won't say.

    The whole of a sharing problem is *which account to share with*, and the
    token itself is the only thing that knows. Asking costs one request and
    turns "share it with that account" into an address somebody can paste.
    """
    try:
        response = httpx.get(
            "https://www.googleapis.com/drive/v3/about",
            params={"fields": "user(emailAddress)"},
            headers={"Authorization": f"Bearer {access_token()}"},
            timeout=30,
        )
    except (httpx.HTTPError, SheetsError):
        return ""
    if response.status_code >= 400:
        return ""
    return str(((response.json() or {}).get("user") or {}).get("emailAddress") or "")


def explain(response: httpx.Response, where: str) -> str:
    """Google's refusal in terms of what to do about it."""
    body = response.text[:300]
    if response.status_code == 403 and "insufficient" in body.lower():
        return (
            "The Google login RYTE has doesn't cover Sheets. Mint the refresh "
            f"token again with {SCOPES[0]} among the scopes."
        )
    lowered = body.lower()
    # Google answers 403 for a project that never switched the API on, and it
    # reads exactly like a permission problem. It is not one, and telling
    # somebody to share a sheet they already own wastes their afternoon.
    if response.status_code == 403 and (
        "has not been used in project" in lowered
        or "service_disabled" in lowered
        or "accessnotconfigured" in lowered
    ):
        return (
            "The **Google Sheets API is not enabled** in that Cloud project — "
            "this isn't a sharing problem. Cloud Console → APIs & Services → "
            "Library → search 'Google Sheets API' → Enable, then try again."
        )
    if response.status_code == 403:
        who = whoami()
        named = f" — that's **{who}**" if who else ""
        return (
            "Google refused: the account behind LEADS_GOOGLE_REFRESH_TOKEN "
            f"can't open that sheet{named}. Either share the sheet with that "
            "account as an Editor, or mint the refresh token again signed in "
            f"as whoever owns it.\n-# Google said: {body[:160]}"
        )
    if response.status_code == 404:
        return f"No sheet with that id ({where.split('/')[0][:20]}…). Check the link."
    return f"Sheets -> HTTP {response.status_code}: {body}"


# The column a lead sheet is counted on. Column A is the first one somebody
# fills in, so a row with anything in it at all has something here.
COUNT_COLUMN = "A"


def row_count(spreadsheet: str, *, column: str = COUNT_COLUMN, header: bool = True) -> int:
    """How many leads a masterlist holds.

    One column rather than the whole sheet: these run to tens of thousands of
    rows and the only question is how many there are. The header row is not a
    lead, so it comes off.
    """
    wanted = sheet_id(spreadsheet)
    payload = _get(f"{wanted}/values/{column}:{column}", majorDimension="COLUMNS")
    values = (payload.get("values") or [[]])[0]
    filled = sum(1 for cell in values if str(cell).strip())
    return max(filled - (1 if header and filled else 0), 0)


def tab_names(spreadsheet: str) -> list[str]:
    """The tabs on a sheet, in order. The first is the default to write to."""
    payload = _get(sheet_id(spreadsheet), fields="sheets.properties.title")
    return [
        str((sheet.get("properties") or {}).get("title") or "")
        for sheet in payload.get("sheets") or []
    ]


def tab_ids(spreadsheet: str) -> dict[str, int]:
    """Tab title -> its numeric id. Formatting is addressed by the number."""
    payload = _get(sheet_id(spreadsheet), fields="sheets.properties(title,sheetId)")
    found = {}
    for sheet in payload.get("sheets") or []:
        properties = sheet.get("properties") or {}
        found[str(properties.get("title") or "")] = int(properties.get("sheetId") or 0)
    return found


def _colour(red: float, green: float, blue: float) -> dict:
    return {"red": red, "green": green, "blue": blue}


# Agent Lead Lab's green, near enough, and a grey light enough to read on.
HEADER_BACKGROUND = _colour(0.16, 0.44, 0.31)
BANDING = _colour(0.94, 0.96, 0.95)


def prettify(spreadsheet: str, tab: str, *, rows: int, columns: int = 4) -> None:
    """Make a written tab readable: a header that stays put, and room to read.

    Cosmetic, and it earns its place: this file is opened to answer "how many
    have we got", and a wall of unformatted text with the header scrolled off
    the top answers it slower than the Discord message did.

    Best effort. A summary that is correct but plain is worth having, so a
    formatting failure is not allowed to lose the write that already happened.
    """
    wanted = sheet_id(spreadsheet)
    found = tab_ids(wanted).get(tab)
    if found is None:
        return

    requests = [
        # The header stays visible when somebody scrolls a long list.
        {"updateSheetProperties": {
            "properties": {"sheetId": found, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        {"repeatCell": {
            "range": {"sheetId": found, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_BACKGROUND,
                "textFormat": {"bold": True, "foregroundColor": _colour(1, 1, 1)},
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }},
        # Counts read as numbers, with the thousands separator: 15,401 is the
        # number on the aged file and 15401 is harder to take in at a glance.
        {"repeatCell": {
            "range": {
                "sheetId": found, "startRowIndex": 1,
                "startColumnIndex": columns - 1, "endColumnIndex": columns,
            },
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                "horizontalAlignment": "RIGHT",
            }},
            "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
        }},
        {"autoResizeDimensions": {"dimensions": {
            "sheetId": found, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": columns,
        }}},
    ]
    if rows > 1:
        requests.append({"addBanding": {"bandedRange": {
            "range": {
                "sheetId": found, "startRowIndex": 1, "endRowIndex": rows,
                "startColumnIndex": 0, "endColumnIndex": columns,
            },
            "rowProperties": {
                "firstBandColor": _colour(1, 1, 1), "secondBandColor": BANDING,
            },
        }}})

    headers = {"Authorization": f"Bearer {access_token()}"}
    try:
        httpx.post(
            f"{API_ROOT}/{wanted}:batchUpdate",
            headers=headers,
            json={"requests": requests},
            timeout=60,
        )
    except httpx.HTTPError:
        # Deliberately swallowed. The numbers are already written and correct;
        # losing them to a failed cosmetic call would be the worse outcome.
        return


def ensure_tab(spreadsheet: str, title: str) -> None:
    """Make a tab if the sheet hasn't got one by that name.

    The inactive masterlists go on their own tab and that tab does not exist
    the first time. Making it is better than writing them in underneath the
    live ones, which is the thing the separate tab is for.
    """
    wanted = sheet_id(spreadsheet)
    if title in tab_names(wanted):
        return

    headers = {"Authorization": f"Bearer {access_token()}"}
    try:
        made = httpx.post(
            f"{API_ROOT}/{wanted}:batchUpdate",
            headers=headers,
            json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise SheetsError(f"Couldn't add the '{title}' tab: {exc}") from exc
    if made.status_code >= 400:
        raise SheetsError(explain(made, wanted))


def write_rows(spreadsheet: str, rows: list[list], *, tab: str = "") -> str:
    """Replace a tab's contents with `rows`. Returns the sheet's URL.

    Cleared first, then written. Without the clear, a summary that got shorter
    leaves the tail of the previous one underneath it, and a stale row on a
    file people read as current is worse than an empty one.
    """
    wanted = sheet_id(spreadsheet)
    where = f"'{tab}'" if tab else (tab_names(wanted) or ["Sheet1"])[0]
    if not tab:
        where = f"'{where}'"

    headers = {"Authorization": f"Bearer {access_token()}"}
    try:
        cleared = httpx.post(
            f"{API_ROOT}/{wanted}/values/{where}:clear", headers=headers, timeout=60
        )
        if cleared.status_code >= 400:
            raise SheetsError(explain(cleared, wanted))

        written = httpx.put(
            f"{API_ROOT}/{wanted}/values/{where}!A1",
            headers=headers,
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": rows},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise SheetsError(f"Couldn't write to the sheet: {exc}") from exc
    if written.status_code >= 400:
        raise SheetsError(explain(written, wanted))

    return f"https://docs.google.com/spreadsheets/d/{wanted}/edit"
