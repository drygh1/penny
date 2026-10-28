"""Parsing Bank of America PDF statements, using made-up statement text in the real layout."""
import base64
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import pdf_statements

MONTHLY = """Visa Signature®
Account# 4000 1234 5678 9012
December 21 - January 20, 2026
New Balance Total $1,019.55
Account Summary/Payment Information
Previous Balance $1,500.00
Payments and Other Credits -$1,520.25
Purchases and Adjustments $1,039.80
Statement Closing Date 01/20/2026
Transactions
Payments and Other Credits
12/22 12/23 BA ELECTRONIC PAYMENT 0952 9012 -1,500.00
12/26 12/27 Etsy.com Brookvale NY 6793 9012 -20.25
TOTAL PAYMENTS AND OTHER CREDITS FOR THIS PERIOD -$1,520.25
Purchases and Adjustments
12/30 12/31 NORTHSIDE MARKET SPRINGFIELD IL 8472 9012 74.59
01/02 01/04 EXAMPLE AIR 0000000000000SPRINGFIELD IL
DOE/JANE 03/07 ABC/XYZ RNDTRP XYZ/ABC
5235 9012 400.00
01/08 01/11 MAPLE GAS LTD. FAIRVIEW ON
55.00 CAD
continued on next page...
6908 9012 40.00
01/15 01/16 Hulu 877-8244858 CA 1729 9012 525.21
TOTAL PURCHASES AND ADJUSTMENTS FOR THIS PERIOD $1,039.80
Interest Charged
01/20 01/20 INTEREST CHARGED ON PURCHASES 0.00
TOTAL INTEREST CHARGED FOR THIS PERIOD $0.00""".splitlines()

YEAR_END = """Here's how you used your credit card in 2024
2024 year-end summary of credit card transactions
This statement takes a look at how, when and where you used your credit card ending in 9012 between
Account snapshot
Total spent
$498.00
Date Description Location Amount
05/16/24 CORNER TIRE SHOP BOZEMAN, MT 10.00
06/25/24 DROPBOX*PLUS QUILL, CA 2.00CR
11/20/24 EXAMPLE AIR 0000000000 800-5550100, CA 490.00
$498.00""".splitlines()


def test_monthly_statement():
    r = pdf_statements.parse_lines(MONTHLY)
    assert r["kind"] == "monthly" and r["account"] == "9012" and r["affects_balance"]
    assert r["check"]["ok"], r["check"]
    by_desc = {t["description"]: t for t in r["transactions"]}
    # Payments and refunds are money in; purchases are money out.
    assert by_desc["BA ELECTRONIC PAYMENT"]["amount"] == 1500.00
    assert by_desc["NORTHSIDE MARKET SPRINGFIELD IL"]["amount"] == -74.59
    # December charges on a January statement belong to the previous year.
    assert by_desc["NORTHSIDE MARKET SPRINGFIELD IL"]["date"] == date(2025, 12, 31)
    # Charges that wrap across lines (airfare, foreign currency) are read whole.
    assert by_desc["EXAMPLE AIR 0000000000000SPRINGFIELD IL"]["amount"] == -400.00
    assert by_desc["MAPLE GAS LTD. FAIRVIEW ON"]["amount"] == -40.00
    # Zero-dollar interest lines are skipped.
    assert len(r["transactions"]) == 6


def test_monthly_statement_flags_a_mismatch():
    lines = [l for l in MONTHLY if "NORTHSIDE" not in l]
    assert not pdf_statements.parse_lines(lines)["check"]["ok"]


def test_year_end_summary():
    r = pdf_statements.parse_lines(YEAR_END)
    assert r["kind"] == "year_end" and not r["affects_balance"] and r["check"]["ok"]
    assert [t["amount"] for t in r["transactions"]] == [-10.00, 2.00, -490.00]
    assert r["transactions"][0]["date"] == date(2024, 5, 16)


def test_unknown_pdf_is_rejected():
    with pytest.raises(pdf_statements.StatementError):
        pdf_statements.parse_lines(["Some other bank statement", "01/01 coffee 4.00"])


# ---------- through the API ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    # Uploads carry the made-up statement text in place of real PDF bytes.
    monkeypatch.setattr(pdf_statements, "_lines", lambda data: data.decode().splitlines())
    from app.main import app
    return TestClient(app, base_url="http://127.0.0.1:8000")


def upload(*docs):
    return {"files": [{"name": f"s{i}.pdf", "data": base64.b64encode("\n".join(d).encode()).decode()}
                      for i, d in enumerate(docs)]}


def test_year_end_import_does_not_rewrite_past_balances(client):
    r = client.post("/api/import/pdf", json=upload(YEAR_END)).json()
    assert r["mapping"]["no_balance"] and r["statements"][0]["check"]["ok"]
    client.post("/api/import", json={"csv": r["csv"], "mapping": r["mapping"],
                                     "new_account": {"name": "Visa", "balance": -800}})
    history = client.get("/api/accounts").json()["history"]
    assert {h["net_worth"] for h in history} == {-800}  # purchases alone don't move past balances
    spending = client.get("/api/spending/monthly", params={"months": 36}).json()
    assert any(m["month"] == "2024-11" and m["spending"] == 490.00 for m in spending)


def test_monthly_import_rebuilds_past_balances(client):
    r = client.post("/api/import/pdf", json=upload(MONTHLY)).json()
    assert not r["mapping"]["no_balance"]
    client.post("/api/import", json={"csv": r["csv"], "mapping": r["mapping"],
                                     "new_account": {"name": "Visa", "balance": -1019.55}})
    history = client.get("/api/accounts").json()["history"]
    assert history[0]["net_worth"] == -1500.00  # the statement's previous balance


def test_mixing_summary_and_monthly_is_refused(client):
    assert client.post("/api/import/pdf", json=upload(YEAR_END, MONTHLY)).status_code == 400


def test_bad_file_is_reported_per_statement(client):
    r = client.post("/api/import/pdf", json=upload(["not a statement"], MONTHLY)).json()
    assert "error" in r["statements"][0] and r["statements"][1]["count"] == 6
