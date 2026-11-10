""""can you pull up all FND payments?" - answered "FND isn't a word I know"."""

from wilbyte import payraapi
from wilbyte.bot.mentions import parse


def test_the_question_is_read():
    assert parse("<@1> can you pull up all FND payments?").action == "paymentsof"
    assert parse("<@1> can you pull up all FND payments?").brief == "FND"
    assert parse("<@1> show me Bryce Evans payments").brief == "Bryce Evans"
    assert parse("<@1> payments from FND Life").brief == "FND Life"
    assert parse("<@1> how much has FND paid").brief == "FND"
    assert parse("<@1> clearout payments").action == "ledgerclear"
    assert parse("<@1> show me today's payments").action != "paymentsof"


DATA = {
    "invoices": {
        "a": {"id": "a", "number": "INV-21300", "name": "FND Life", "email": "join@fndlife.com", "total": 1400,
              "description": "Vet widow $200/day", "invoice_date": "2026-10-22", "payment_ids": ["p1"]},
        "b": {"id": "b", "number": "INV-21400", "name": "Francesca Hasapes", "email": "join@fndlife.com",
              "total": 1400, "invoice_date": "2026-10-29", "payment_ids": ["p2"]},
        "c": {"id": "c", "number": "INV-21500", "name": "Fender Daniels", "email": "fd@x.com", "total": 99,
              "invoice_date": "2026-11-01", "payment_ids": []},
    },
    "payments": {
        "p1": {"id": "p1", "amount": 1400, "paid_on": "2026-10-22", "status": "succeeded", "how": "Visa",
               "invoice_ids": ["a"]},
        "p2": {"id": "p2", "amount": 1400, "paid_on": "2026-10-29", "status": "succeeded", "invoice_ids": ["b"]},
    },
}


def test_fnd_finds_the_business_and_its_email_domain_not_a_word_inside_a_name():
    found = payraapi.payments_for(DATA, "FND")
    assert found["count"] == 2 and found["total_cents"] == 280000
    said = payraapi.payments_said(found, "FND")
    assert "$2,800.00" in said and "INV-21300" in said and "INV-21400" in said and "Fender" not in said


def test_nothing_found_says_so():
    assert "Nothing in Payra" in payraapi.payments_said(payraapi.payments_for(DATA, "Zzyzx"), "Zzyzx")
