"""Import transactions from a bank's CSV export.

SimpleFIN only reaches back about 90 days, but most banks let you download
years of history as CSV. Every bank lays its file out differently, so this
module guesses which columns hold the date, description and amount; the UI
shows the guess and lets the user correct it before importing.
"""
import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime

DATE_FORMATS = [
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d",
    "%d/%m/%Y", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y",
]
MATCH_WINDOW_DAYS = 3  # a bank's CSV date and SimpleFIN's posted date can differ by a day or two

_HEADER_DATE = re.compile(r"date", re.I)
_HEADER_MONEY = re.compile(r"amount|debit|credit|withdrawal|deposit", re.I)


def parse_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Split CSV text into a header row and data rows.

    Some banks put account details above the real header, so the header is the
    first row (within the top 15) that has a date column and a money column.
    """
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    except csv.Error as e:
        raise ValueError(f"That file couldn't be read as CSV ({e}).")
    if not rows:
        raise ValueError("That file is empty.")

    header_at = next((i for i, r in enumerate(rows[:15])
                      if any(_HEADER_DATE.search(c) for c in r) and any(_HEADER_MONEY.search(c) for c in r)),
                     None)
    if header_at is None:
        width = max(len(r) for r in rows)
        headers, data = [f"Column {i + 1}" for i in range(width)], rows
    else:
        headers, data = [h.strip() for h in rows[header_at]], rows[header_at + 1:]
    width = len(headers)
    return headers, [(r + [""] * width)[:width] for r in data]


def parse_date(value: str, fmt: str) -> date:
    value = value.strip()
    try:
        return datetime.strptime(value, fmt).date()
    except ValueError:
        # Drop a trailing time, e.g. "2025-01-03 00:00:00" or "2025-01-03T08:15".
        return datetime.strptime(re.split(r"[ T]", value)[0], fmt).date()


def detect_date_format(values: list[str]) -> str | None:
    values = [v for v in values if v.strip()][:50]
    for fmt in DATE_FORMATS:
        try:
            for v in values:
                parse_date(v, fmt)
            return fmt
        except ValueError:
            continue
    return None


def parse_amount(value: str) -> float | None:
    s = value.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):  # accounting style: (12.50)
        negative, s = True, s[1:-1]
    if s.endswith("-"):                         # trailing minus: 12.50-
        negative, s = True, s[:-1]
    if s.upper().endswith("DR"):
        negative, s = True, s[:-2]
    elif s.upper().endswith("CR"):
        s = s[:-2]
    v = float(s)
    return -abs(v) if negative else v


def _is_amount(value: str) -> bool:
    try:
        return parse_amount(value) is not None
    except ValueError:
        return False


def guess_mapping(headers: list[str], rows: list[list[str]]) -> dict:
    lower = [h.lower() for h in headers]
    used: set[int] = set()

    def find(*patterns):
        for p in patterns:
            for i, h in enumerate(lower):
                if i not in used and re.search(p, h):
                    used.add(i)
                    return i
        return None

    sample = rows[:50]
    column = lambda i: [r[i] for r in sample]

    m = {
        "date": find(r"^date$", r"post", r"trans.*date", r"date"),
        "description": find(r"description", r"payee|merchant|^name$", r"memo|details|narrative|name"),
        "amount": find(r"^amount", r"amount"),
        "debit": None,
        "credit": None,
    }
    if m["amount"] is None:
        m["debit"] = find(r"debit|withdrawal|money out|outflow")
        m["credit"] = find(r"credit|deposit|money in|inflow")

    # Files without usable headers: judge columns by their contents instead.
    if m["date"] is None:
        m["date"] = next((i for i in range(len(headers))
                          if i not in used and detect_date_format(column(i))), None)
        if m["date"] is not None:
            used.add(m["date"])
    if m["amount"] is None and m["debit"] is None and m["credit"] is None:
        m["amount"] = next((i for i in range(len(headers)) if i not in used
                            and sum(map(_is_amount, column(i))) >= 0.8 * len(sample)), None)
        if m["amount"] is not None:
            used.add(m["amount"])
    if m["description"] is None:
        free = [i for i in range(len(headers)) if i not in used]
        if free:
            m["description"] = max(free, key=lambda i: sum(len(v) for v in column(i)))

    # Card exports often list purchases as positive numbers. Suggest flipping
    # when card payments are negative while most other rows are positive, or
    # when nearly everything is positive.
    m["flip"] = False
    if m["amount"] is not None:
        pairs = [(r[m["description"]] if m["description"] is not None else "", _safe_amount(r[m["amount"]]))
                 for r in sample]
        amounts = [a for _, a in pairs if a]
        payments = [a for d, a in pairs if a and _PAYMENT.search(d)]
        mostly_positive = bool(amounts) and sum(a > 0 for a in amounts) / len(amounts)
        m["flip"] = bool(
            (payments and all(a < 0 for a in payments) and mostly_positive > 0.5)
            or (not payments and mostly_positive > 0.8))
    return m


_BALANCE_LINE = re.compile(r"(beginning|ending|opening|closing) balance\b", re.I)
_PAYMENT = re.compile(r"payment|thank you|autopay", re.I)


def _safe_amount(value):
    try:
        return parse_amount(value)
    except ValueError:
        return None


def interpret(headers, rows, mapping) -> tuple[list[dict], list[dict], str | None]:
    """Apply a column mapping. Returns (transactions, errors, date_format)."""
    if mapping.get("date") is None or mapping.get("description") is None:
        raise ValueError("Choose the date and description columns.")
    if mapping.get("amount") is None and mapping.get("debit") is None and mapping.get("credit") is None:
        raise ValueError("Choose the amount column, or the debit and credit columns.")

    fmt = detect_date_format([r[mapping["date"]] for r in rows])
    if not fmt:
        raise ValueError("The date column doesn't contain dates Penny recognizes.")

    txs, errors = [], []
    for n, r in enumerate(rows, start=1):
        # Some banks (e.g. Bank of America) include balance lines with no amount.
        if _BALANCE_LINE.match(r[mapping["description"]].strip()):
            continue
        try:
            d = parse_date(r[mapping["date"]], fmt)
            if mapping.get("amount") is not None:
                amount = parse_amount(r[mapping["amount"]])
            else:
                debit = parse_amount(r[mapping["debit"]]) if mapping.get("debit") is not None else None
                credit = parse_amount(r[mapping["credit"]]) if mapping.get("credit") is not None else None
                amount = None if debit is None and credit is None else abs(credit or 0) - abs(debit or 0)
            if amount is None:
                raise ValueError("no amount")
        except ValueError:
            errors.append({"row": n, "text": ", ".join(c for c in r if c)[:120]})
            continue
        if mapping.get("flip"):
            amount = -amount
        description = " ".join(r[mapping["description"]].split()) or "Unknown"
        txs.append({"date": d, "amount": round(amount, 2), "description": description})
    return txs, errors, fmt


def to_simplefin_shape(txs: list[dict], affects_balance: bool = True) -> list[dict]:
    """Give each row a stable ID so importing the same file twice changes nothing."""
    seen: Counter = Counter()
    out = []
    for t in txs:
        key = (t["date"].isoformat(), f"{t['amount']:.2f}", t["description"])
        seen[key] += 1  # two identical coffees on the same day are both kept
        digest = hashlib.sha1("|".join((*key, str(seen[key]))).encode()).hexdigest()[:16]
        out.append({
            "id": f"csv:{digest}",
            "posted": int(datetime.combine(t["date"], dtime(12, 0)).timestamp()),
            "amount": f"{t['amount']:.2f}",
            "description": t["description"],
            "affects_balance": affects_balance,
        })
    return out


def drop_overlap(conn, account_id: str) -> int:
    """Delete imported rows that duplicate a transaction SimpleFIN already has.

    Matches one-to-one on account, exact amount, and dates within a few days.
    Runs after every import and every sync, so the order you do them in
    doesn't matter.
    """
    csv_prefix = f"{account_id}:csv:%"
    pool = defaultdict(list)  # cents -> [(posted, id)] of imported rows
    for r in conn.execute("SELECT id, posted, amount FROM transactions WHERE account_id = ? AND id LIKE ?",
                          (account_id, csv_prefix)):
        pool[round(r["amount"] * 100)].append((r["posted"], r["id"]))
    if not pool:
        return 0

    doomed = []
    window = MATCH_WINDOW_DAYS * 86400
    for r in conn.execute("SELECT posted, amount FROM transactions WHERE account_id = ? AND id NOT LIKE ? "
                          "ORDER BY posted", (account_id, csv_prefix)):
        candidates = pool.get(round(r["amount"] * 100))
        if not candidates:
            continue
        best = min(candidates, key=lambda c: abs(c[0] - r["posted"]))
        if abs(best[0] - r["posted"]) <= window:
            candidates.remove(best)
            doomed.append(best[1])
    conn.executemany("DELETE FROM transactions WHERE id = ?", [(i,) for i in doomed])
    return len(doomed)
