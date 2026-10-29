from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import importer
from app.categorize import description_key, normalize_merchant
from app.sync import purchase_date


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    return TestClient(app, base_url="http://127.0.0.1:8000")


# Made-up rows laid out like a credit union's export: no header, an empty
# third column, and a first row whose description says "Date" and "DEBIT".
CREDIT_UNION_CSV = """\
10/28/2026,WITHDRAWAL DEBIT CARD PURCHASE EVERGREEN SKI AREA ILDate 10/27/26 PURCH 0000000000000000000001%% Card 15 #1111,,-97.00
10/27/2026,WITHDRAWAL POS #000000000386 PINECREST GROCERY SPRINGFIELD IL%% Card 15 #1111,,-17.77
10/26/2026,From Share 10 ,,500.00
10/02/2026,Deposit ACH ZELLE TYPE: CASHOUT CO: ZELLEACH Trace 000000000000268,,100.00
09/20/2026,Withdrawal ACH ZELLE TYPE: PAYMENT CO: ZELLE NAME: PAT EXAMPLEACH Trace 000000000000613,,-30.00
09/10/2026,Withdrawal ACH ZELLE TYPE: PAYMENT CO: ZELLE NAME: PAT EXAMPLEACH Trace 000000000000242,,-60.00
"""


def at_noon(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 12).timestamp())


def import_csv(client, text):
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    return client.post("/api/import", json={"csv": text, "mapping": mapping,
                                            "new_account": {"name": "Checking", "balance": 1000}}).json()


def by_description(client, contains):
    return next(t for t in client.get("/api/transactions").json() if contains in t["description"])


def test_first_row_mentioning_date_and_debit_is_not_taken_as_the_header():
    headers, rows = importer.parse_table(CREDIT_UNION_CSV)
    assert headers[0] == "Column 1" and len(rows) == 6
    mapping = importer.guess_mapping(headers, rows)
    assert (mapping["date"], mapping["description"], mapping["amount"]) == (0, 1, 3)


def test_purchase_date_comes_from_description_or_transacted_at():
    posted = at_noon(date(2026, 10, 28))
    assert purchase_date(posted, "SKI AREA ILDate 10/27/26 PURCH 123") == at_noon(date(2026, 10, 27))
    assert purchase_date(posted, "no date here", transacted_at=posted - 2 * 86400) == posted - 2 * 86400
    # Later dates (money becoming available) and far-off dates leave it alone.
    assert purchase_date(posted, "Effective Date:10/30/2026") == posted
    assert purchase_date(posted, "Date 01/02/26") == posted


def test_imported_rows_use_purchase_date_and_reimporting_adds_nothing(client):
    first = import_csv(client, CREDIT_UNION_CSV)
    assert first["imported"] == 6 and first["unreadable"] == 0
    assert by_description(client, "EVERGREEN SKI")["date"] == "2026-10-27"
    assert by_description(client, "PINECREST GROCERY")["date"] == "2026-10-27"

    again = client.post("/api/import", json={
        "csv": CREDIT_UNION_CSV, "account_id": first["account_id"],
        "mapping": client.post("/api/import/preview", json={"csv": CREDIT_UNION_CSV}).json()["mapping"]}).json()
    assert again["imported"] == 0


def test_credit_union_wording_gives_the_same_merchants_as_simplefin():
    assert normalize_merchant("Withdrawal Home Banking To Share 70") == normalize_merchant("", "Transfer to Share 70")
    assert normalize_merchant("Deposit Home Banking From Share 10") == normalize_merchant("", "Transfer from Account 10")
    assert normalize_merchant("Deposit ACH ZELLE TYPE: CASHOUT CO: ZELLEACH Trace 0910") == "zelle"
    assert normalize_merchant("Withdrawal ACH ZELLE TYPE: PAYMENT CO: ZELLE NAME: PAT EXAMPLEACH Trace 1") == "zelle"


def test_description_rules_split_one_merchant_into_categories(client):
    import_csv(client, CREDIT_UNION_CSV)
    client.put("/api/merchant-rules", json={"merchant": "zelle", "category": "Shopping"})
    d = client.get("/api/merchant-descriptions", params={"merchant": "zelle"}).json()
    # The two payments differ only by trace number, so they're one description.
    assert sorted(x["count"] for x in d["descriptions"]) == [1, 2]

    assert client.put("/api/description-rules", json={
        "merchant": "zelle", "pattern": "CASHOUT", "category": "Income"}).json()["pattern"] == "cashout"
    assert by_description(client, "CASHOUT")["category"] == "Income"
    assert by_description(client, "000000000000613")["category"] == "Shopping"

    # A more specific rule beats a broader one.
    payment = next(x for x in d["descriptions"] if x["count"] == 2)
    client.put("/api/description-rules", json={"merchant": "zelle", "pattern": "payment", "category": "Dining"})
    client.put("/api/description-rules", json={"merchant": "zelle", "pattern": payment["key"], "category": "Entertainment"})
    tx = by_description(client, "000000000000242")
    assert tx["category"] == "Entertainment" and tx["description_rule"] == payment["key"]

    merchant = next(m for m in client.get("/api/merchants").json() if m["merchant"] == "zelle")
    assert merchant["variants"] == 2 and merchant["description_rules"] == 3

    client.delete("/api/description-rules", params={"merchant": "zelle", "pattern": "cashout"})
    assert by_description(client, "CASHOUT")["category"] == "Shopping"


def test_deleting_a_category_moves_its_description_rules(client):
    import_csv(client, CREDIT_UNION_CSV)
    client.post("/api/categories", json={"name": "Friends"})
    client.put("/api/description-rules", json={"merchant": "zelle", "pattern": "payment", "category": "Friends"})
    client.delete("/api/categories", params={"name": "Friends", "move_to": "Other"})
    assert by_description(client, "000000000000613")["category"] == "Other"


def test_rule_needs_words_not_just_numbers(client):
    r = client.put("/api/description-rules", json={"merchant": "zelle", "pattern": "0910 22", "category": "Other"})
    assert r.status_code == 400
    assert description_key("Trace 0910  ABC 12") == "trace abc"


def test_year_end_summary_rows_already_on_a_statement_are_dropped(client):
    statement = "Date,Description,Amount\n12/22/2025,KROGER #4410 SPRINGFIELD IL,-8.87\n01/05/2026,TACO SHOP,-12.00\n"
    summary = "Date,Description,Amount\n12/22/2025,\"KROGER 4410 SPRINGFIELD, IL\",-8.87\n11/02/2025,BOOK STORE,-20.00\n"
    first = import_csv(client, statement)
    mapping = client.post("/api/import/preview", json={"csv": summary}).json()["mapping"] | {"flip": False, "no_balance": True}
    client.post("/api/import", json={"csv": summary, "mapping": mapping, "account_id": first["account_id"]})
    descriptions = sorted(t["description"] for t in client.get("/api/transactions").json())
    assert descriptions == ["BOOK STORE", "KROGER #4410 SPRINGFIELD IL", "TACO SHOP"]


def import_summary(client, account_id, text):
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"] | {"flip": False, "no_balance": True}
    client.post("/api/import", json={"csv": text, "mapping": mapping, "account_id": account_id})


def test_same_price_drinks_on_nearby_days_are_not_mistaken_for_duplicates(client):
    # The statement starts 12/21. The summary also has a $7.25 beer from 12/20,
    # which only it lists, and the 12/22 beer both list.
    statement = "Date,Description,Amount\n12/22/2025,FSP*THISTLE QUILL BREWIFAIRVIEW IL,-7.25\n"
    summary = ("Date,Description,Amount\n12/20/2025,\"FSP*THISTLE QUILL BR FAIRVIEW, IL\",-7.25\n"
               "12/22/2025,\"FSP*THISTLE QUILL BR FAIRVIEW, IL\",-7.25\n")
    account = import_csv(client, statement)["account_id"]
    import_summary(client, account, summary)
    assert sorted(t["date"] for t in client.get("/api/transactions").json()) == ["2025-12-20", "2025-12-22"]


def test_two_identical_drinks_the_same_day_are_both_kept(client):
    two = "Date,Description,Amount\n12/22/2025,TST* GRANITE CAFE SPRINGFIELD IL,-6.50\n12/22/2025,TST* GRANITE CAFE SPRINGFIELD IL,-6.50\n"
    account = import_csv(client, two)["account_id"]
    import_summary(client, account, two.replace("SPRINGFIELD IL", "SPRINGFIELD, IL"))
    assert len(client.get("/api/transactions").json()) == 2


def test_imported_purchase_before_simplefin_coverage_is_kept(client):
    from app import db
    from app.sync import upsert_transaction
    # The export ends 10/13; SimpleFIN starts with a different $7.25 drink on 10/15.
    account = import_csv(client, "Date,Description,Amount\n10/13/2026,CORNER BAR,-7.25\n")["account_id"]
    conn = db.connect()
    with conn:
        upsert_transaction(conn, account, {"id": "TRN-1", "posted": at_noon(date(2026, 10, 15)), "amount": "-7.25",
                                           "description": "CORNER BAR"})
        importer.drop_overlap(conn, account)
    dates = sorted(r[0] for r in conn.execute("SELECT date(posted, 'unixepoch', 'localtime') FROM transactions"))
    assert dates == ["2026-10-13", "2026-10-15"]
