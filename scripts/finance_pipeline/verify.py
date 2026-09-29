#!/usr/bin/env python3
"""verify.py — integrity checks that run BEFORE anything is imported.

IN   parsed rows in file order and their Detected; a list of dates
OUT  the balance-chain result, the anchor verdict, the gap register

═══ THE BALANCE CHAIN ═══
Measured on the author's real cheque exports: not one break across every
consecutive pair of rows. That makes it the strongest correctness signal available,
and a break means the file is wrong — the import must stop rather than write a
plausible-looking but incorrect ledger.

DIRECTION MATTERS. Format A rows are NEWEST FIRST and `Balance` is the balance
*after* that transaction, so:

    balance[i] == balance[i+1] + amount[i]

This holds only on the RAW file. Once rows are sorted by date — as they are
inside the Excel model — the chain is destroyed. That is why this runs at parse
time and never against the workbook.

The credit card is exempt: its Balance column is the literal string `0` on every
row, so there is no anchor to check.

═══ GAPS ═══
A coverage gap is a run of days with no transactions at all — usually a
statement period nobody exported. Any per-month figure computed across one is
wrong (it spreads a partial spend over the full calendar), so analyse.py
refuses to state a deficit for a window a gap intersects.

Gap.days is INCLUSIVE of both endpoints. (An early note on the real ledger used
an exclusive count and was off by one per gap; corrected 2026-08-20.)

min_days=3 clears ordinary weekends. Every real gap on the author's account has
been far longer than that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from detect import BALANCED_KINDS


@dataclass
class ChainBreak:
    index: int
    expected: Decimal
    actual: Decimal
    description: str

    def __str__(self) -> str:
        return (
            f"row {self.index}: expected {self.expected}, got {self.actual} "
            f"({self.description[:40]})"
        )


@dataclass
class ChainResult:
    ok: bool
    checked: int
    breaks: list


@dataclass
class Gap:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def intersects(self, window_start: date, window_end: date) -> bool:
        return not (self.end < window_start or self.start > window_end)

    def __str__(self) -> str:
        return f"{self.start.isoformat()} → {self.end.isoformat()} ({self.days} days)"


def check_chain(txns: list, det) -> ChainResult:
    """Assert balance[i] == balance[i+1] + amount[i] on the raw, unsorted rows."""
    if getattr(det, "account_kind", None) not in BALANCED_KINDS:
        return ChainResult(ok=True, checked=0, breaks=[])

    breaks: list = []
    checked = 0
    for i in range(len(txns) - 1):
        cur, nxt = txns[i], txns[i + 1]
        if cur.balance is None or nxt.balance is None:
            continue
        checked += 1
        expected = nxt.balance + cur.amount
        if expected != cur.balance:
            breaks.append(ChainBreak(i, expected, cur.balance, cur.description))
    return ChainResult(ok=not breaks, checked=checked, breaks=breaks)


def check_anchor(txns: list, det) -> bool:
    """The preamble's `Balance:` equals the first (newest) row's balance.

    A second, independent confirmation that the file is internally consistent.
    """
    if getattr(det, "account_kind", None) not in BALANCED_KINDS:
        return True
    if not txns or det.closing_balance is None:
        return True
    # Skip leading pending rows — they have no settled balance (parse.py nulls
    # them). The anchor is the newest row that actually carries one.
    first = next((t for t in txns if t.balance is not None), None)
    if first is None:
        return True
    return first.balance == det.closing_balance


def find_gaps(dates: list, min_days: int = 3) -> list:
    """Runs of >= min_days consecutive days with no transactions at all."""
    if not dates:
        return []
    uniq = sorted(set(dates))
    gaps: list = []
    for prev, nxt in zip(uniq, uniq[1:]):
        missing = (nxt - prev).days - 1
        if missing >= min_days:
            gaps.append(Gap(prev + timedelta(days=1), nxt - timedelta(days=1)))
    return gaps
