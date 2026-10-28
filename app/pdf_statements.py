"""Read transactions out of Bank of America credit card PDF statements.

Bank of America only offers recent activity as CSV; older history comes as
PDFs. Two layouts are supported:

- Monthly statements ("eStmt_*.pdf"): every charge, credit and card payment.
  Each statement prints its previous and new balance, so the parsed
  transactions are checked against that change.
- Year-end summaries ("YearEndSummary_*.pdf"): a year of purchases and
  refunds, but no card payments. Good for spending history, but they can't
  be used to rebuild past balances.

On these statements a purchase is a positive number. Penny stores spending as
negative, so amounts are flipped.
"""
import io
import re
from datetime import date

import pypdf

MONEY = r"-?\$?-?[\d,]+\.\d{2}"

# Monthly: "02/21 02/23 HARBOR COFFEE CO JUNEAU AK 4999 9012 21.25"
# (transaction date, posting date, description, reference #, account last 4, amount).
# Interest lines have no reference or account number.
_MONTHLY_TX = re.compile(r"^(\d{2}/\d{2}) (\d{2}/\d{2}) (.+?) (?:\d{4} \d{4} )?(-?[\d,]+\.\d{2})$")
# Airline and foreign-currency charges wrap: the first line has the dates and
# description, extra lines follow, and the last one holds "ref acct amount".
_MONTHLY_START = re.compile(r"^(\d{2}/\d{2}) (\d{2}/\d{2}) (.+)$")
_MONTHLY_END = re.compile(r"^\d{4} \d{4} (-?[\d,]+\.\d{2})$")

# Year-end: "05/16/24 CORNER TIRE SHOP BOZEMAN, MT 10.00", refunds end in "CR".
_YEAREND_TX = re.compile(r"^(\d{2}/\d{2}/\d{2}) (.+?) ([\d,]+\.\d{2})(CR)?$")


class StatementError(ValueError):
    pass


def _money(s: str) -> float:
    s = s.replace("$", "").replace(",", "")
    negative = s.count("-") % 2 == 1
    return -float(s.replace("-", "")) if negative else float(s.replace("-", ""))


def _lines(data: bytes) -> list[str]:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [p.extract_text() or "" for p in reader.pages]
    except Exception:
        raise StatementError("couldn't be opened as a PDF.")
    return [" ".join(line.split()) for page in pages for line in page.splitlines() if line.strip()]


def parse(data: bytes) -> dict:
    return parse_lines(_lines(data))


def parse_lines(lines: list[str]) -> dict:
    """Returns {kind, period, account, transactions, check, affects_balance}.

    transactions: [{date, description, amount}] with spending negative.
    check: {"expected", "found", "ok"}, comparing the parsed total with the
    total printed on the statement.
    """
    text = "\n".join(lines)
    if "year-end summary of credit card transactions" in text.lower():
        return _parse_year_end(lines, text)
    if "Statement Closing Date" in text and "Purchases and Adjustments" in text:
        return _parse_monthly(lines, text)
    raise StatementError(
        "isn't a layout Penny can read yet. It reads Bank of America credit card "
        "monthly statements and year-end summaries.")


def _parse_monthly(lines, text):
    closing_match = re.search(r"Statement Closing Date (\d{2})/(\d{2})/(\d{4})", text)
    if not closing_match:
        raise StatementError("is missing its statement closing date.")
    closing = date(int(closing_match[3]), int(closing_match[1]), int(closing_match[2]))

    def full_date(mm_dd: str) -> date:
        month, day = map(int, mm_dd.split("/"))
        # A statement spans about a month, so a later month than the closing
        # date means the previous year (December charges on a January statement).
        return date(closing.year if month <= closing.month else closing.year - 1, month, day)

    txs = []
    printed_total = 0.0
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _MONTHLY_TX.match(line)
        if m:
            posted, description, printed = m[2], m[3], _money(m[4])
        elif (start := _MONTHLY_START.match(line)):
            # Look a few lines ahead for the "ref acct amount" line.
            end = next((j for j in range(i + 1, min(i + 5, len(lines))) if _MONTHLY_END.match(lines[j])), None)
            if end is None:
                i += 1
                continue
            posted, description, printed = start[2], start[3], _money(_MONTHLY_END.match(lines[end])[1])
            i = end
        else:
            i += 1
            continue
        i += 1
        if printed == 0:
            continue
        printed_total += printed
        txs.append({"date": full_date(posted), "description": description, "amount": round(-printed, 2)})

    previous = re.search(rf"Previous Balance ({MONEY})", text)
    new = re.search(rf"New Balance Total ({MONEY})", text)
    check = None
    if previous and new:
        expected = round(_money(new[1]) - _money(previous[1]), 2)
        check = {"expected": expected, "found": round(printed_total, 2),
                 "ok": abs(expected - printed_total) < 0.005}
    period = re.search(r"([A-Z][a-z]+ \d{1,2} - [A-Z][a-z]+ \d{1,2}, \d{4})", text)
    account = re.search(r"Account# [\d ]*?(\d{4})\n", text + "\n")
    return {
        "kind": "monthly",
        "period": period[1] if period else closing.isoformat(),
        "account": account[1] if account else None,
        "transactions": txs,
        "check": check,
        "affects_balance": True,
    }


def _parse_year_end(lines, text):
    txs = []
    for line in lines:
        m = _YEAREND_TX.match(line)
        if not m:
            continue
        month, day, year = map(int, m[1].split("/"))
        value = _money(m[3])
        txs.append({
            "date": date(2000 + year, month, day),
            "description": m[2],
            "amount": value if m[4] else -value,  # CR = refund
        })
    spent = re.search(r"Total spent\n?\$?([\d,]+\.\d{2})", text)
    found = round(-sum(t["amount"] for t in txs), 2)
    check = {"expected": _money(spent[1]), "found": found, "ok": abs(_money(spent[1]) - found) < 0.005} if spent else None
    year = re.search(r"(\d{4}) year-end summary", text)
    account = re.search(r"ending in (\d{4})", text)
    return {
        "kind": "year_end",
        "period": f"All of {year[1]}" if year else "Year-end summary",
        "account": account[1] if account else None,
        "transactions": txs,
        "check": check,
        "affects_balance": False,
    }
