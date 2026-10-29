"""Detect recurring charges (subscriptions and bills) from transaction history.

A merchant counts as recurring when its charges arrive on a regular cadence
(weekly ... yearly) for a roughly steady amount. Grocery runs at the same
store fail the amount check; one-off purchases fail the cadence check.
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


@dataclass
class Recurring:
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


def detect(transactions, today: date, names=None, overrides=None) -> list[Recurring]:
    """transactions: iterable of dicts with merchant, posted (unix), amount, category.

    Only outflows (amount < 0) are considered; transfers are skipped.
    """
    names = names or {}
    overrides = overrides or {}
    by_merchant: dict[str, list] = {}
    for t in transactions:
        if t["amount"] >= 0 or t["category"] in NON_SPENDING:
            continue
        by_merchant.setdefault(t["merchant"], []).append(t)

    results = []
    for merchant, txs in by_merchant.items():
        txs.sort(key=lambda t: t["posted"])
        # Merge same-day charges (e.g. split authorizations) into one.
        charges: list[tuple[int, float, str]] = []
        for t in txs:
            if charges and t["posted"] - charges[-1][0] < DAY:
                ts, amt, cat = charges[-1]
                charges[-1] = (ts, amt - t["amount"], cat)
            else:
                charges.append((t["posted"], -t["amount"], t["category"]))

        if len(charges) < 2:
            continue
        intervals = [(b[0] - a[0]) / DAY for a, b in zip(charges, charges[1:])]
        cadence = _cadence_for(intervals)
        if not cadence:
            continue
        cadence_name, period = cadence
        # Two charges is only convincing for long cadences; short ones need three.
        if len(charges) < 3 and period < 80:
            continue
        amounts = [c[1] for c in charges]
        if not _steady_amount(amounts):
            continue

        last_ts, last_amount, category = charges[-1]
        last = date.fromtimestamp(last_ts)
        next_due = last + timedelta(days=round(period))
        active = (today - last).days <= period * 1.5 + 5
        # Walk back to the most recent price change; report it if it was recent
        # and the old price was fixed (bills like electricity vary every month).
        same = lambda a, b: abs(a - b) <= max(0.02 * b, 0.5)
        previous_amount = price_changed_on = None
        for i in range(len(amounts) - 2, -1, -1):
            if not same(amounts[i], last_amount):
                changed = date.fromtimestamp(charges[i + 1][0])
                if (today - changed).days <= 120 and i > 0 and same(amounts[i - 1], amounts[i]):
                    previous_amount, price_changed_on = round(amounts[i], 2), changed.isoformat()
                break

        results.append(Recurring(
            merchant=merchant,
            name=names.get(merchant, merchant.title()),
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
            ignored=overrides.get(merchant) == "ignored",
        ))

    results.sort(key=lambda r: (not r.active, -r.monthly_cost))
    return results
