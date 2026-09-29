#!/usr/bin/env python3
"""subs.py — recurring charges, found in the ledger and ranked by rand.

IN   analysis rows (date, description, amount, category)
OUT  [{name, category, amount, monthly, annual_if_cut, charges, months, …}]

F3, 2026-09-27. SUBS mode was unwired: run.py gave the advisor an empty list.

RECURRING = the same merchant, charged in two or more calendar months, at
amounts within ±15% of each other. One merchant can bill several things (an
app store bills every subscription bought through it), so each merchant's
charges are clustered by amount and every cluster is its own line.

Only commitment-like categories are searched (SUBSCRIPTION_CATEGORIES): a loaf
of bread bought twice is not a subscription. `monthly` is a line's total over
the months it appeared in, so four top-ups a month read as one monthly habit.
Merchant names drop the card mask, dates, amounts and reference numbers FNB
embeds in the narrative, so a phone number never becomes part of a name.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

SUBSCRIPTION_CATEGORIES = frozenset({
    "Digital subscriptions",
    "Business / side project",
    "Connectivity",
    "Fitness (gym)",
    "Entertainment",
    "Gaming",
    "Medical aid & health",
    "Insurance (policy)",
    "Home maintenance",
})
TOLERANCE = Decimal("0.15")
_CENTS = Decimal("0.01")
_MASK_TAIL = re.compile(r"\s*\d{6}\*\d{4}.*$")
_NUMBERS = re.compile(r"\b\d[\d.,:-]*\b")


def _day(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def merchant_name(description: str) -> str:
    """The merchant, without the card mask, dates, amounts and references.
    A run of 3+ digits goes even when glued to letters: a debit order can
    carry a new reference every month ("FIBRECO123456789 NETCASH")."""
    text = _MASK_TAIL.sub("", (description or "").upper()).replace("/", " ")
    text = _NUMBERS.sub(" ", re.sub(r"\d{3,}", " ", text))
    text = re.sub(r"(?<!\w)[-*#.]+(?!\w)", " ", text)  # punctuation left behind
    return re.sub(r"\s+", " ", text).strip()[:32]


def _canonical(names) -> dict:
    """name -> the shortest name it shares a prefix with (8+ characters): FNB
    cuts a foreign-currency merchant shorter, so one store can arrive as
    "APPLE.COM BIL" one month and "APPLE.COM BI" the next."""
    out = {}
    for name in sorted(names, key=len):
        out[name] = next(
            (short for short in out.values() if len(short) >= 8 and name.startswith(short)),
            name,
        )
    return out


def _clusters(charges: list) -> list:
    """Charges grouped by amount: each within TOLERANCE of its cluster's median."""
    out = []
    for row in sorted(charges, key=lambda r: -r.amount):  # smallest amount first
        amount = -row.amount
        for cluster in out:
            amounts = sorted(-r.amount for r in cluster)
            median = amounts[len(amounts) // 2]
            if abs(amount - median) <= median * TOLERANCE:
                cluster.append(row)
                break
        else:
            out.append([row])
    return out


def find(rows: list) -> list:
    """Every recurring charge in `rows`, largest monthly cost first."""
    by_merchant: dict = {}
    for row in rows:
        if row.amount >= 0 or (getattr(row, "category", "") or "") not in SUBSCRIPTION_CATEGORIES:
            continue
        by_merchant.setdefault(merchant_name(row.description), []).append(row)

    canon = _canonical(by_merchant)
    merged: dict = {}
    for name, charges in by_merchant.items():
        merged.setdefault(canon[name], []).extend(charges)

    found = []
    for name, charges in merged.items():
        for cluster in _clusters(charges):
            months = {(_day(r.date).year, _day(r.date).month) for r in cluster}
            if len(months) < 2:
                continue
            latest = max(cluster, key=lambda r: _day(r.date))
            total = sum((-r.amount for r in cluster), Decimal("0"))
            monthly = (total / len(months)).quantize(_CENTS, rounding=ROUND_HALF_UP)
            found.append({
                "name": name,
                "category": latest.category,
                "amount": -latest.amount,
                "monthly": monthly,
                "annual_if_cut": monthly * 12,
                "charges": len(cluster),
                "months": len(months),
                "first_seen": min(_day(r.date) for r in cluster),
                "last_seen": _day(latest.date),
                "decision": "",
            })
    found.sort(key=lambda s: (-s["monthly"], s["name"]))
    return found
