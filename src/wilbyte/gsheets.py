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
import threading
import time
from collections import deque

import httpx

API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# What the refresh token has to be minted with. `spreadsheets` covers reading
# the lead masterlists and writing the summary. `drive` is the wide one and it
# is needed: the masterfile is built from a Drive *folder* RYTE did not create,
# and `drive.file` only ever sees files the app made itself.
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
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


# Google answers 503 now and again on a run that reads forty sheets in a row,
# and it means nothing except "ask again". Without this a masterlist that was
# fine ends up on the summary as a dash, which reads as a real problem.
RETRY_PAUSES = (1.0, 3.0, 10.0)

# A quota refusal is a different animal from a 503: the limit is per minute,
# so the only thing that clears it is waiting most of a minute. Three seconds
# of politeness just spends another request on the same refusal.
QUOTA_PAUSES = (20.0, 45.0, 60.0)
_WORTH_RETRYING = (429, 500, 502, 503, 504)

# Google allows 60 read requests a minute per user. Counting eighty-five
# masterlists is eighty-five reads, so the run has to pace itself or twenty of
# them come back as dashes - which is what happened the first time it ran
# against the real folder. Slightly under the limit, because the minute Google
# measures and the minute measured here are not the same minute.
READS_A_MINUTE = 50
_recent: deque[float] = deque()
_pace_lock = threading.Lock()


def _pace() -> None:
    """Block until another read would sit inside the per-minute allowance."""
    while True:
        with _pace_lock:
            now = time.monotonic()
            while _recent and now - _recent[0] > 60:
                _recent.popleft()
            if len(_recent) < READS_A_MINUTE:
                _recent.append(now)
                return
            wait = 60 - (now - _recent[0]) + 0.1
        time.sleep(max(wait, 0.1))


def _pauses_for(response: httpx.Response | None) -> tuple[float, ...]:
    if response is not None and response.status_code == 429:
        return QUOTA_PAUSES
    return RETRY_PAUSES


def _get(path: str, **params) -> dict:
    last: str = ""
    for attempt in range(len(RETRY_PAUSES) + 1):
        headers = {"Authorization": f"Bearer {access_token()}"}
        pauses = RETRY_PAUSES
        _pace()
        try:
            response = httpx.get(
                f"{API_ROOT}/{path}", params=params, headers=headers, timeout=60
            )
        except httpx.HTTPError as exc:
            last = f"Sheets request failed: {exc}"
        else:
            if response.status_code < 400:
                return response.json()
            if response.status_code not in _WORTH_RETRYING:
                raise SheetsError(explain(response, path))
            last = explain(response, path)
            pauses = _pauses_for(response)

        if attempt < len(pauses):
            time.sleep(pauses[attempt])
    raise SheetsError(last)


def _send(method: str, url: str, **kwargs) -> httpx.Response:
    """A write, paced and retried like a read.

    Writes have their own per-minute allowance and building thirty masterlists
    is sixty of them. Without this the run half-finishes and the second half
    comes back as quota refusals.
    """
    last: httpx.Response | None = None
    for attempt in range(len(RETRY_PAUSES) + 1):
        _pace()
        headers = {"Authorization": f"Bearer {access_token()}"}
        response = httpx.request(
            method, url, headers=headers, timeout=60, **kwargs
        )
        if response.status_code < 400 or response.status_code not in _WORTH_RETRYING:
            return response

        last = response
        pauses = _pauses_for(response)
        if attempt < len(pauses):
            time.sleep(pauses[attempt])
    return last if last is not None else response


def whoami() -> str:
    """Which Google account the refresh token belongs to, or "" if it won't say.

    The whole of a sharing problem is *which account to share with*, and the
    token itself is the only thing that knows. Asking costs one request and
    turns "share it with that account" into an address somebody can paste.

    Answered once per run: a walk over forty masterlists can refuse twenty
    times, and the account behind the token is the same on all twenty.
    """
    if _token_cache.get("email"):
        return str(_token_cache["email"])
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
    email = str(((response.json() or {}).get("user") or {}).get("emailAddress") or "")
    if email:
        _token_cache["email"] = email
    return email


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
    if response.status_code == 429:
        # The raw body is four lines of quota metric names and a project
        # number, and none of it is anything anybody can act on.
        return (
            "Google's per-minute read limit (60 a minute for one account). "
            "I pace myself under it and wait when I hit it, so this only shows "
            "up if something else was reading the same account at the time — "
            "run it again."
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


def facts(spreadsheet: str) -> tuple[str, list[str]]:
    """A sheet's title and its tab titles, in one request.

    Both are needed before a single row is counted. A deploy sheet kept
    outside the folder is only recognisable by these two things, and counting
    column A of whatever tab happens to be first is how a list of states
    became "56 Spanish FEX leads".
    """
    payload = _get(
        sheet_id(spreadsheet), fields="properties.title,sheets.properties.title"
    )
    title = str((payload.get("properties") or {}).get("title") or "")
    tabs = [
        str((sheet.get("properties") or {}).get("title") or "")
        for sheet in payload.get("sheets") or []
    ]
    return title, tabs


def count_and_header(
    spreadsheet: str, *, tab: str = "", column: str = COUNT_COLUMN, header: bool = True
) -> tuple[int, list[str]]:
    """How many rows a sheet holds, and what its first row says.

    Both in one request. The header is what tells a lead masterlist apart from
    an agent auto-deploy sheet, and asking separately would double the number
    of requests a run makes against a per-minute limit it already paces for.
    """
    wanted = sheet_id(spreadsheet)
    where = f"'{tab}'!" if tab else ""
    payload = _get(
        f"{wanted}/values:batchGet",
        ranges=[f"{where}{column}:{column}", f"{where}1:1"],
    )
    ranges = payload.get("valueRanges") or []

    down = (ranges[0].get("values") if len(ranges) > 0 else []) or []
    filled = sum(1 for row in down if row and str(row[0]).strip())
    count = max(filled - (1 if header and filled else 0), 0)

    across = (ranges[1].get("values") if len(ranges) > 1 else []) or []
    titles = [str(cell) for cell in (across[0] if across else [])]
    return count, titles


def tab_rows(spreadsheet: str, tab: str) -> list[list[str]]:
    """Everything on a tab, formulas as formulas.

    A linked cell reads "Open sheet" and holds =HYPERLINK("…"), and the link
    is the part that says which sheet a row is already about. Asking for the
    formatted value would hand back the label and lose it.
    """
    where = f"'{tab}'" if tab else "A:Z"
    payload = _get(
        f"{sheet_id(spreadsheet)}/values/{where}", valueRenderOption="FORMULA"
    )
    return [[str(cell) for cell in row] for row in payload.get("values") or []]


def _cell_name(row: int, column: int) -> str:
    """(0, 1) -> "B1". Zero-based in, A1 notation out."""
    letters = ""
    column += 1
    while column:
        column, rest = divmod(column - 1, 26)
        letters = chr(65 + rest) + letters
    return f"{letters}{row + 1}"


def update_cells(spreadsheet: str, tab: str, cells: list[tuple[int, int, str]]) -> int:
    """Write single cells, leaving every other cell alone. Returns how many.

    Cell by cell rather than row by row: the rows around them are somebody's
    work, and a whole-row write would blank a column nobody asked about.
    """
    if not cells:
        return 0

    wanted = sheet_id(spreadsheet)
    _must_be_writable(wanted)
    where = f"'{tab}'!" if tab else ""
    written = _send(
        "POST",
        f"{API_ROOT}/{wanted}/values:batchUpdate",
        json={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": f"{where}{_cell_name(row, column)}", "values": [[value]]}
                for row, column, value in cells
            ],
        },
    )
    if written.status_code >= 400:
        raise SheetsError(explain(written, wanted))
    return len(cells)


def append_rows(spreadsheet: str, rows: list[list], *, tab: str) -> str:
    """Add rows to the end of a tab, leaving everything above them alone.

    Appending rather than replacing, because these tabs are somebody's now:
    the rows already there were sorted, edited and linked by hand, and a run
    that rewrote them would undo an afternoon's work every time.
    """
    if not rows:
        return f"https://docs.google.com/spreadsheets/d/{sheet_id(spreadsheet)}/edit"

    wanted = sheet_id(spreadsheet)
    _must_be_writable(wanted)
    where = f"'{tab}'" if tab else "A1"
    written = _send(
        "POST",
        f"{API_ROOT}/{wanted}/values/{where}:append",
        params={
            "valueInputOption": "USER_ENTERED",
            "insertDataOption": "INSERT_ROWS",
        },
        json={"values": rows},
    )
    if written.status_code >= 400:
        raise SheetsError(explain(written, wanted))
    return f"https://docs.google.com/spreadsheets/d/{wanted}/edit"


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
    _must_be_writable(wanted)
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

    try:
        _send("POST", f"{API_ROOT}/{wanted}:batchUpdate", json={"requests": requests})
    except httpx.HTTPError:
        # Deliberately swallowed. The numbers are already written and correct;
        # losing them to a failed cosmetic call would be the worse outcome.
        return


DONE_BACKGROUND = _colour(0.85, 0.92, 0.83)


def highlight_rows(spreadsheet: str, tab: str, rows: list[int]) -> None:
    """Paint the given rows green: these ones are sorted out.

    Best effort, like prettify. The status column already says so in words;
    this is so somebody can see at a glance which half of the list is done.
    """
    if not rows:
        return
    wanted = sheet_id(spreadsheet)
    _must_be_writable(wanted)
    found = tab_ids(wanted).get(tab)
    if found is None:
        return

    requests = [
        {"repeatCell": {
            "range": {
                "sheetId": found, "startRowIndex": number, "endRowIndex": number + 1,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": DONE_BACKGROUND}},
            "fields": "userEnteredFormat.backgroundColor",
        }}
        for number in rows
    ]
    try:
        _send("POST", f"{API_ROOT}/{wanted}:batchUpdate", json={"requests": requests})
    except httpx.HTTPError:
        return


def ensure_tab(spreadsheet: str, title: str) -> None:
    """Make a tab if the sheet hasn't got one by that name.

    The inactive masterlists go on their own tab and that tab does not exist
    the first time. Making it is better than writing them in underneath the
    live ones, which is the thing the separate tab is for.
    """
    wanted = sheet_id(spreadsheet)
    _must_be_writable(wanted)
    if title in tab_names(wanted):
        return

    try:
        made = _send(
            "POST",
            f"{API_ROOT}/{wanted}:batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        )
    except httpx.HTTPError as exc:
        raise SheetsError(f"Couldn't add the '{title}' tab: {exc}") from exc
    if made.status_code >= 400:
        raise SheetsError(explain(made, wanted))


# --------------------------------------------------------------- the folder
#
# The masterlists are not only Discord channels any more: they are files in one
# Drive folder, and that folder is the list of what exists. A lead type with a
# channel and no file is the thing to make; a file with no channel is still a
# masterlist and still belongs on the masterfile.

DRIVE_ROOT = "https://www.googleapis.com/drive/v3/files"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

# drive.google.com/drive/folders/<id>, or the bare id.
_FOLDER_ID = re.compile(r"/folders/([A-Za-z0-9_-]{10,})")


def folder_id(url_or_id: str) -> str:
    """The folder id, from a pasted Drive link or from the id itself."""
    text = (url_or_id or "").strip()
    found = _FOLDER_ID.search(text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text
    raise SheetsError(f"That doesn't look like a Drive folder link: {text[:120]}")


def _drive(params: dict) -> dict:
    last = ""
    for attempt in range(len(RETRY_PAUSES) + 1):
        headers = {"Authorization": f"Bearer {access_token()}"}
        _pace()
        try:
            response = httpx.get(DRIVE_ROOT, params=params, headers=headers, timeout=60)
        except httpx.HTTPError as exc:
            last = f"Drive request failed: {exc}"
        else:
            if response.status_code < 400:
                return response.json()
            if response.status_code not in _WORTH_RETRYING:
                raise SheetsError(explain(response, "the folder"))
            last = explain(response, "the folder")
        if attempt < len(RETRY_PAUSES):
            time.sleep(RETRY_PAUSES[attempt])
    raise SheetsError(last)


def sheets_in_folder(folder: str) -> list[dict]:
    """Every spreadsheet in a Drive folder: [{id, name, url}, …].

    Sub-folders are not walked. The masterlists sit together at one level, and
    a walk that went deeper would sweep up whatever else is filed underneath.
    """
    wanted = folder_id(folder)
    found: list[dict] = []
    page = ""
    while True:
        params = {
            "q": f"'{wanted}' in parents and mimeType='{SHEET_MIME}' and trashed=false",
            "fields": "nextPageToken,files(id,name)",
            "pageSize": 200,
            "orderBy": "name",
            # Shared drives look like ordinary folders and answer empty without
            # these two, which reads as "the folder is empty" rather than as a
            # setting nobody turned on.
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        payload = _drive(params)
        for file in payload.get("files") or []:
            found.append({
                "id": str(file.get("id") or ""),
                "name": str(file.get("name") or ""),
                "url": f"https://docs.google.com/spreadsheets/d/{file.get('id')}/edit",
            })
        page = str(payload.get("nextPageToken") or "")
        if not page:
            break
    return found


# ------------------------------------------------- the one writable sheet
#
# Everything else Google-shaped that RYTE touches is a lead masterlist, and a
# masterlist is read and never written. The rule is enforced here rather than
# remembered: the only sheet that can be written is the masterfile named in
# the .env. A write anywhere else raises before it can change anything, and
# there is no code left that could create a file either.

SUMMARY_VAR = "LEADS_SUMMARY_SHEET_ID"


def the_masterfile() -> str:
    """The id of the one sheet RYTE may write to, from the .env."""
    named = (os.getenv(SUMMARY_VAR) or "").strip()
    if not named:
        return ""
    try:
        return sheet_id(named)
    except SheetsError:
        return ""


def writable(spreadsheet: str) -> bool:
    """Whether RYTE is allowed to change this sheet at all."""
    return sheet_id(spreadsheet) == the_masterfile()


def _must_be_writable(wanted: str) -> None:
    if writable(wanted):
        return
    raise SheetsError(
        "I'm not allowed to change that sheet. The only one I can add to, "
        f"link in or edit is the masterfile named in {SUMMARY_VAR}. Every "
        "lead masterlist is read-only to me."
    )


def ours(found: list[str], expected: list[str]) -> bool:
    """Whether a tab's first row is a summary tab RYTE may replace.

    Not an exact match. Somebody deleting a column by hand - which is what
    happened the first time this ran - leaves a tab that is still plainly the
    summary, and refusing to update it helps nobody. Two of the expected
    headings is the test: a lead sheet's Name/Email/Phone/State shares none of
    them, and neither does an agent deploy config.
    """
    if not [cell for cell in found if str(cell).strip()]:
        return True
    here = {str(cell).strip().lower() for cell in found}
    shared = sum(1 for cell in expected if str(cell).strip().lower() in here)
    return shared >= min(2, len(expected))


def first_row(spreadsheet: str, tab: str) -> list[str]:
    """Row 1 of a tab, or [] when the tab is empty."""
    where = f"'{tab}'!1:1" if tab else "1:1"
    payload = _get(f"{sheet_id(spreadsheet)}/values/{where}")
    values = payload.get("values") or []
    return [str(cell) for cell in (values[0] if values else [])]


def write_rows(
    spreadsheet: str,
    rows: list[list],
    *,
    tab: str = "",
    expect_header: list[str] | None = None,
) -> str:
    """Replace a tab's contents with `rows`. Returns the sheet's URL.

    Cleared first, then written. Without the clear, a summary that got shorter
    leaves the tail of the previous one underneath it, and a stale row on a
    file people read as current is worse than an empty one.

    `expect_header` is the safety catch on that clear. Given one, the tab is
    only replaced if it is empty or already carries that exact header - so a
    wrong id in the .env refuses to write instead of wiping somebody's leads.
    Every masterlist in the folder is read and never written, and this is what
    makes that true rather than merely intended.
    """
    wanted = sheet_id(spreadsheet)
    _must_be_writable(wanted)
    where = f"'{tab}'" if tab else (tab_names(wanted) or ["Sheet1"])[0]
    if not tab:
        where = f"'{where}'"

    if expect_header is not None:
        found = first_row(wanted, tab)
        if not ours(found, expect_header):
            raise SheetsError(
                "I won't write there. That tab holds something that isn't the "
                f"summary — its first row reads {found[:4]}. Check "
                "LEADS_SUMMARY_SHEET_ID points at the masterfile and not at a "
                "lead sheet."
            )

    try:
        cleared = _send("POST", f"{API_ROOT}/{wanted}/values/{where}:clear")
        if cleared.status_code >= 400:
            raise SheetsError(explain(cleared, wanted))

        written = _send(
            "PUT",
            f"{API_ROOT}/{wanted}/values/{where}!A1",
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": rows},
        )
    except httpx.HTTPError as exc:
        raise SheetsError(f"Couldn't write to the sheet: {exc}") from exc
    if written.status_code >= 400:
        raise SheetsError(explain(written, wanted))

    return f"https://docs.google.com/spreadsheets/d/{wanted}/edit"
