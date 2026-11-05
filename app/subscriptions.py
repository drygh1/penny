"""Detect recurring charges (subscriptions and bills) from transaction history.

A merchant counts as recurring when its charges arrive on a regular cadence
(weekly ... yearly) for a roughly steady amount. Grocery runs at the same
store fail the amount check; one-off purchases fail the cadence check.

One merchant can bill for several things (Apple, Google, Amazon), all under
the same description. When a merchant's charges don't read as one series,
they're grouped by price and each group is checked on its own.

Each one is a bill (rent, utilities: not something you'd cancel) or a
subscription, going by its category unless the user moved it.
"""
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from statistics import median

from .categorize import NON_SPENDING

DAY = 86400

# name, nominal period in days, accepted interval range
CADENCES = [
    ("weekly", 7, (5, 9)),
    ("every 2 weeks", 14, (12, 17)),
    ("monthly", 30.44, (25, 36)),
    ("quarterly", 91.3, (80, 100)),
    ("yearly", 365.25, (340, 390)),
]

# Recurring charges in these categories are bills unless the user says otherwise.
BILL_CATEGORIES = {"Housing", "Utilities & Bills"}


@dataclass
class Recurring:
    key: str                # merchant, or "merchant|price" for one of several at a merchant
    merchant: str
    name: str
    category: str
    cadence: str
    amount: float           # most recent charge, positive
    monthly_cost: float
    yearly_cost: float
    last_date: str
    next_date: str
    charge_count: int
    active: bool
    previous_amount: float | None  # old price, when the price changed recently
    price_changed_on: str | None
    ignored: bool = False
    description_rule: str | None = None  # rule that set the category, if any
    kind: str = "subscription"           # or "bill"

    def to_dict(self):
        return asdict(self)


def _cadence_for(intervals: list[float]):
    med = median(intervals)
    for name, period, (lo, hi) in CADENCES:
        if lo <= med <= hi:
            in_range = sum(lo <= i <= hi for i in intervals)
            # Allow the odd skipped/doubled month.
            if in_range / len(intervals) >= 0.7:
                return name, period
    return None


def _near(a: float, ref: float) -> bool:
    return abs(a - ref) <= max(0.15 * ref, 1.0)


def _steady_amount(amounts: list[float]) -> bool:
    # One price change is fine, so split at the latest change: charges since
    # then match the current price, and the earlier ones must be mostly steady.
    last = amounts[-1]
    i = len(amounts) - 1
    while i > 0 and _near(amounts[i - 1], last):
        i -= 1
    before = amounts[:i][-6:]
    if not before:
        return True
    mid = median(before)
    return sum(_near(a, mid) for a in before) / len(before) >= 0.7


def _same_price(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.02 * b, 0.5)


def _series(key: str, merchant: str, txs: list, today: date, merge_same_day=True) -> Recurring | None:
    """One recurring charge from txs (sorted by date), or None if they aren't one."""
    # Merge same-day charges (e.g. split authorizations) into one.
    charges: list[tuple[int, float, str, str | None]] = []
    for t in txs:
        if merge_same_day and charges and t["posted"] - charges[-1][0] < DAY:
            ts, amt, cat, rule = charges[-1]
            charges[-1] = (ts, amt - t["amount"], cat, rule)
        else:
            charges.append((t["posted"], -t["amount"], t["category"], t.get("description_rule")))

    if len(charges) < 2:
        return None
    intervals = [(b[0] - a[0]) / DAY for a, b in zip(charges, charges[1:])]
    cadence = _cadence_for(intervals)
    if not cadence:
        return None
    cadence_name, period = cadence
    # Two charges is only convincing for long cadences; short ones need three.
    if len(charges) < 3 and period < 80:
        return None
    amounts = [c[1] for c in charges]
    if not _steady_amount(amounts):
        return None

    last_ts, last_amount, category, description_rule = charges[-1]
    last = date.fromtimestamp(last_ts)
    next_due = last + timedelta(days=round(period))
    active = (today - last).days <= period * 1.5 + 5
    # Walk back to the most recent price change; report it if it was recent
    # and the old price was fixed (bills like electricity vary every month).
    previous_amount = price_changed_on = None
    for i in range(len(amounts) - 2, -1, -1):
        if not _same_price(amounts[i], last_amount):
            changed = date.fromtimestamp(charges[i + 1][0])
            if (today - changed).days <= 120 and i > 0 and _same_price(amounts[i - 1], amounts[i]):
                previous_amount, price_changed_on = round(amounts[i], 2), changed.isoformat()
            break

    return Recurring(
        key=key,
        merchant=merchant,
        name=merchant.title(),
        category=category,
        cadence=cadence_name,
        amount=round(last_amount, 2),
        monthly_cost=round(last_amount * 30.44 / period, 2),
        yearly_cost=round(last_amount * 365.25 / period, 2),
        last_date=last.isoformat(),
        next_date=next_due.isoformat(),
        charge_count=len(charges),
        active=active,
        previous_amount=previous_amount,
        price_changed_on=price_changed_on,
        description_rule=description_rule,
    )


def _by_price(txs: list) -> list[tuple[str, list]]:
    """Split one merchant's charges (sorted by date) into groups at the same price.
    A group is named by its first price, so its key stays put as charges come in.

    Groups need three charges, even yearly ones: two purchases a year apart at
    the same price are too easily a coincidence at a store you shop at a lot.
    """
    groups: dict[str, list] = {}
    for t in txs:
        price = -t["amount"]
        label = next((k for k, g in groups.items() if _same_price(price, -g[-1]["amount"])), None)
        groups.setdefault(label or f"{price:.2f}", []).append(t)
    return [(label, g) for label, g in groups.items() if len(g) >= 3]


def detect(transactions, today: date, names=None, overrides=None, kinds=None, bill_categories=None) -> list[Recurring]:
    """transactions: iterable of dicts with merchant, posted (unix), amount, category
    and optionally description_rule.

    Only outflows (amount < 0) are considered; transfers are skipped.
    overrides and kinds are keyed by Recurring.key: "ignored", and "bill"/"subscription".
    bill_categories replaces BILL_CATEGORIES once one of them has been renamed.
    """
    names = names or {}
    overrides = overrides or {}
    kinds = kinds or {}
    bill_categories = BILL_CATEGORIES if bill_categories is None else bill_categories
    by_merchant: dict[str, list] = {}
    for t in transactions:
        if t["amount"] >= 0 or t["category"] in NON_SPENDING:
            continue
        by_merchant.setdefault(t["merchant"], []).append(t)

    results = []
    for merchant, txs in by_merchant.items():
        txs.sort(key=lambda t: t["posted"])
        whole = _series(merchant, merchant, txs, today)
        # Same-price charges on one day are separate purchases, not a split authorization.
        found = [whole] if whole else [
            r for label, group in _by_price(txs)
            if (r := _series(f"{merchant}|{label}", merchant, group, today, merge_same_day=False))]
        for r in found:
            r.name = names.get(merchant, r.name)
            r.ignored = overrides.get(r.key) == "ignored"
            r.kind = kinds.get(r.key) or ("bill" if r.category in bill_categories else "subscription")
        results += found

    results.sort(key=lambda r: (not r.active, -r.monthly_cost))
    return results
