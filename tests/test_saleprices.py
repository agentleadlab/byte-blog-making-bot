"""The sale: Lead Type | English's prices on Tuesday 1 AM, back Friday 11:59 PM."""

from datetime import datetime, timedelta

from wilbyte import ghl, saleprices
from wilbyte.bot import client

ORIGINAL = [
    "Text-Verified Veteran Leads [FRESH - $45/Lead]",
    "Text-Verified Mortgage Leads [FRESH - $43/Lead]",
    "Final Expense [FRESH - $28/Lead]",
    "Aged Veteran - 180+ days [AGED - $1/Lead]",
    "Aged IUL [AGED - $7/Lead]",
    "SPANISH Mortgage [FRESH - $43/Lead]",
    "SPANISH Aged [AGED - $9/Lead]",
]


def test_franklins_table():
    planned, left = saleprices.plan(ORIGINAL)
    assert planned == [
        "Text-Verified Veteran Leads [FRESH - $33.75/Lead]",
        "Text-Verified Mortgage Leads [FRESH - $32.25/Lead]",
        "Final Expense [FRESH - $19.60/Lead]",
        "Aged Veteran - 180+ days [AGED - $0.50/Lead]",
        "Aged IUL [AGED - $3.50/Lead]",
        "SPANISH Mortgage [FRESH - $38.70/Lead]",
        "SPANISH Aged [AGED - $8.10/Lead]",
    ]
    assert left == []
    assert saleprices.sale_text("Aged [AGED - $8/Lead]")[0] == "Aged [AGED - $4/Lead]"
    assert saleprices.sale_text("Odd [FRESH - $99/Lead]")[0] is None


def test_clock():
    held = {"original": ORIGINAL}
    before = saleprices.APPLY_AT - timedelta(minutes=1)
    assert saleprices.due(held, before) == ""
    assert saleprices.due(held, saleprices.APPLY_AT) == "apply"
    held["applied_at"] = "x"
    assert saleprices.due(held, saleprices.APPLY_AT + timedelta(hours=5)) == ""
    assert saleprices.due(held, saleprices.RESTORE_AT) == "restore"
    held["restored_at"] = "y"
    assert saleprices.due(held, saleprices.RESTORE_AT + timedelta(hours=1)) == ""
    # Never applied: nothing to put back. Three failed tries: left to a person.
    assert saleprices.due({"original": ORIGINAL}, saleprices.RESTORE_AT) == ""
    assert saleprices.due({"original": ORIGINAL, "tries_apply": 3}, saleprices.APPLY_AT) == ""


class FakeGHL:
    """A GHL that keeps one dropdown, and takes new options only as "options"."""

    location_id = "loc"

    def __init__(self, options, *, takes="options"):
        self.fields = {"f1": {"id": "f1", "name": "Lead Type | English", "dataType": "SINGLE_OPTIONS",
                              "picklistOptions": list(options), "position": 3, "model": "contact"},
                       "f2": {"id": "f2", "name": "Lead Type | Spanish", "picklistOptions": ["x $9"]}}
        self.takes = takes
        self.puts = []

    def _request(self, method, path, **kw):
        if method == "GET" and path.endswith("/customFields"):
            return {"customFields": list(self.fields.values())}
        field_id = path.rsplit("/", 1)[-1]
        if method == "GET":
            return {"customField": dict(self.fields[field_id], picklistOptions=list(self.fields[field_id]["picklistOptions"]))}
        if method == "PUT":
            self.puts.append((field_id, kw["json"]))
            if self.takes in kw["json"]:
                self.fields[field_id]["picklistOptions"] = list(kw["json"][self.takes])
            return {}
        if method == "POST":
            self.fields["t"] = {"id": "t", "name": kw["json"]["name"], "picklistOptions": kw["json"]["options"]}
            return {"customField": {"id": "t"}}
        if method == "DELETE":
            del self.fields[field_id]
            return {}
        raise AssertionError(path)

    def close(self):
        pass


def _wired(monkeypatch, fake):
    monkeypatch.setattr(ghl, "GHLClient", lambda *a, **k: fake)

    class Secrets:
        ghl_api_token = "t"
        ghl_location_id = "loc"

    class Config:
        secrets = Secrets()

    return Config()


def test_on_and_back_touches_only_that_field(monkeypatch):
    fake = FakeGHL(ORIGINAL, takes="picklistOptions")
    config = _wired(monkeypatch, fake)
    held, problem = client._sale_snapshot(config)
    assert not problem and held["original"] == ORIGINAL

    # Somebody adds an option during the sale: it's kept, not wiped.
    worked, said = client._sale_change(config, "on")
    assert worked, said
    assert fake.fields["f1"]["picklistOptions"][0].endswith("$33.75/Lead]")
    fake.fields["f1"]["picklistOptions"].append("New Product [FRESH - $50/Lead]")

    worked, said = client._sale_change(config, "off")
    assert worked, said
    assert fake.fields["f1"]["picklistOptions"] == ORIGINAL + ["New Product [FRESH - $50/Lead]"]
    assert {field_id for field_id, _ in fake.puts} == {"f1"}
    assert fake.fields["f2"]["picklistOptions"] == ["x $9"]
    assert saleprices.load()["restored_at"]


def test_wont_save_a_list_already_on_sale(monkeypatch):
    on_sale, _ = saleprices.plan(ORIGINAL)
    config = _wired(monkeypatch, FakeGHL(on_sale))
    held, problem = client._sale_snapshot(config)
    assert "already reads at sale prices" in problem and not held.get("original")


def test_the_test_field(monkeypatch):
    fake = FakeGHL(ORIGINAL)
    shape, said = saleprices.test_run(fake)
    assert shape == "options"
    assert "t" not in fake.fields and fake.fields["f1"]["picklistOptions"] == ORIGINAL
    refusing = FakeGHL(ORIGINAL, takes="nothing")
    shape, said = saleprices.test_run(refusing)
    assert shape == "" and any("wouldn't take" in one for one in said)
