"""Nova's aged pulls checked against the payments - "Ryte doesnt need to
create aged leads card anymore ... but i want ryte qc-ing it instead"."""

from wilbyte import agedqc

POST = """New Payment | Payra ✅
NAME: Kory Parent
EMAIL: kory@example.com
PRODUCT: Text-Verified Aged IUL Leads
INVOICE: INV-21087 · card •••• 9204 · 11/02/2026 10:01 AM
Notes:
> Name: Kory Parent
> Email: kory@example.com
> Aged IUL 30-90: 25 @ $8.00 — states AZ, LA, TX, VA, WA"""

DONE = """@Rook Kory Parent's aged order is done (INV-21087) — let them know their sheet is shared with their email:
> ✅ Aged IUL 30-90: **25** pulled (Arizona 5, Louisiana 5, Texas 5, Virginia 5, Washington 5) · sheet shared with kory@example.com
> https://docs.google.com/spreadsheets/d/1P5AgOAWTSHxfGd/edit"""


def test_a_pull_that_matches_the_payment():
    result = agedqc.check(POST, DONE, sheet_rows=25, sheet_states={"AZ": 5, "LA": 5, "TX": 5, "VA": 5, "WA": 5})
    assert result.problems == [] and result.client == "Kory Parent" and result.invoice == "INV-21087"
    assert "matches the payment" in agedqc.said(result)


def test_a_short_pull_a_stray_state_and_the_wrong_email_are_said():
    done = (DONE.replace("**25**", "**20**").replace("Texas 5", "Florida 5")
            .replace("shared with kory@example.com", "shared with someone@example.com"))
    result = agedqc.check(POST, done, sheet_rows=19)
    said = " ".join(result.problems)
    assert "paid for 25, Nova pulled 20" in said and "pulled in FL" in said
    assert "someone@example.com" in said and "The sheet has 19 leads" in said


def test_two_lines_paired_by_what_they_are():
    post = POST.replace("> Aged IUL 30-90: 25 @ $8.00 — states AZ, LA, TX, VA, WA",
                        "> Aged Vets 180+: 100 @ $1.00 — states AL, TX\n> Aged IUL 30-90: 25 @ $8.00 — states AZ")
    done = ("Kory Parent's aged order is done (INV-21087)\n✅ Aged IUL 30-90 days: 25 pulled (Arizona 25)\n"
            "✅ Aged Vets 180+: 100 pulled (Alabama 50, Texas 50) · sheet shared with kory@example.com\n"
            "https://docs.google.com/spreadsheets/d/abc/edit")
    assert agedqc.check(post, done).problems == []
    assert any("pulled none of it" in one for one in agedqc.check(post, done.replace("Aged Vets 180+: 100", "Aged FEX 180+: 100")).problems)


def test_a_sheet_that_cant_be_opened_is_said_not_held():
    result = agedqc.check(POST, DONE, sheet_problem="couldn't open it (403)")
    assert result.problems == [] and "wasn't checked" in agedqc.said(result)


def test_the_sheets_own_states_and_count():
    rows = [["Name", "Phone", "State"], ["A", "1", "AZ"], ["B", "2", "Texas"], [], ["C", "3", "FL"]]
    assert agedqc.sheet_states(rows) == (3, {"AZ": 1, "TX": 1, "FL": 1})
    result = agedqc.check(POST, DONE, sheet_rows=25, sheet_states={"AZ": 20, "FL": 5})
    assert any("FL, which they didn't order" in one for one in result.problems)


def test_the_qc_is_kept_for_the_audit():
    result = agedqc.check(POST, DONE)
    agedqc.remember("900", result)
    assert agedqc.recalled()["900"]["problems"] == []


def test_asking_for_it():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> aged qc").action == "agedqc"
    assert mentions.parse("<@1> aged qc 3 days").action == "agedqc"


def test_the_state_column_is_found_by_what_it_holds():
    """Kory Parent's sheet: owned by agentleadlab, and "The sheet itself
    wasn't checked" - a link banner above the leads, and no heading called
    State to find the column by."""
    rows = [["https://agentleadlab.com/replace"], ["First", "Last", "Phone", "Location", "Priority"],
            ["Ann", "Lee", "555", "Arizona", "high"], ["Bo", "Ray", "556", "LA", "high"],
            ["https://agentleadlab.com/replace"], ["Cy", "Fox", "557", "Texas", "low"]]
    assert agedqc.sheet_states(rows) == (3, {"AZ": 1, "LA": 1, "TX": 1})
    assert agedqc.sheet_states([["Name"], ["Ann"]]) == (0, {})


def test_the_share_said_other_ways():
    for said in ("sheet sent to kory@example.com", "sheet shared to **kory@example.com**",
                 "access for kory@example.com"):
        result = agedqc.check(POST, DONE.replace("sheet shared with kory@example.com", said))
        assert not any("who the sheet was shared with" in one for one in result.problems), said
