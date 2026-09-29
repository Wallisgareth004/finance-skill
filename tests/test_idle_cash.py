#!/usr/bin/env python3
"""test_idle_cash.py — F4: cash idle at ~0% is THE ONE MOVE, with its cost stated.

F4 (2026-09-27). When accessible cash above a buffer the owner keeps sits in an
account earning about nothing, the advisor's single recommendation must say so,
with the interest forgone at a rate the owner sets — instead of "Nothing
outstanding on the ledger". "About nothing" is MEASURED, not assumed: the
account's own interest credits over the window, annualised, against the
balance. The buffer, the rate and the "about zero" line live in a private file.
It becomes THE ONE MOVE only when it is worth more per month than the next
candidate. Every figure here is synthetic.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402

WINDOW = analyse.Window(date(2026, 3, 1), date(2026, 5, 29))  # 90 days
SETTINGS = {"threshold": Decimal("100000"), "rate_pct": Decimal("7.2"),
            "at_zero_below_pct": Decimal("1.0")}
AVAILABLE = Decimal("600000.00")
EXCESS = AVAILABLE - SETTINGS["threshold"]
PER_MONTH = (EXCESS * SETTINGS["rate_pct"] / 100 / 12).quantize(Decimal("0.01"))
PER_DAY = (EXCESS * SETTINGS["rate_pct"] / 100 / 365).quantize(Decimal("0.01"))


class _T:
    def __init__(self, d, desc, amt, category="Savings transfers"):
        self.date, self.description, self.amount = d, desc, Decimal(amt)
        self.category, self.is_transaction = category, True


class TestIdleCash(unittest.TestCase):
    def test_idle_cash_above_the_buffer_is_costed(self):
        idle = analyse.idle_cash(AVAILABLE, [_T(date(2026, 4, 30), "CREDIT INTEREST", "2.00")],
                                 WINDOW, SETTINGS)
        self.assertEqual(idle["excess"], EXCESS)
        self.assertEqual(idle["forgone_per_month"], PER_MONTH)
        self.assertEqual(idle["forgone_per_day"], PER_DAY)
        self.assertLess(idle["earning_pct"], Decimal("0.01"))

    def test_cash_under_the_buffer_is_not_idle(self):
        self.assertIsNone(analyse.idle_cash(Decimal("90000.00"), [], WINDOW, SETTINGS))

    def test_cash_that_earns_is_not_idle(self):
        interest = [_T(date(2026, m, 28), "CREDIT INTEREST", "900.00") for m in (3, 4, 5)]
        self.assertIsNone(analyse.idle_cash(AVAILABLE, interest, WINDOW, SETTINGS))

    def test_no_settings_or_no_balance_means_no_verdict(self):
        self.assertIsNone(analyse.idle_cash(AVAILABLE, [], WINDOW, None))
        self.assertIsNone(analyse.idle_cash(None, [], WINDOW, SETTINGS))


class TestTheOneMove(unittest.TestCase):
    IDLE = {"excess": EXCESS, "forgone_per_month": PER_MONTH, "forgone_per_day": PER_DAY,
            "earning_pct": Decimal("0.00"), "rate_pct": SETTINGS["rate_pct"],
            "threshold": SETTINGS["threshold"], "available": AVAILABLE}

    def test_idle_cash_is_the_move_instead_of_nothing_outstanding(self):
        out = analyse.advisor(subs=[], savings_ledger=[], today=date(2026, 5, 30), idle=self.IDLE)
        self.assertIn("idle", out["the_one_move"].lower())
        self.assertIn(f"R{PER_MONTH:,.2f}", out["the_one_move"])
        self.assertNotIn("Nothing outstanding", out["the_one_move"])

    def test_it_outranks_a_small_subscription(self):
        subs = [{"name": "Streaming", "monthly": Decimal("199.00"), "decision": ""}]
        out = analyse.advisor(subs=subs, savings_ledger=[], today=date(2026, 5, 30), idle=self.IDLE)
        self.assertIn("idle", out["the_one_move"].lower())

    def test_a_bigger_open_decision_stays_the_move(self):
        ledger = [{"name": "Big cut", "monthly": Decimal("5000"), "committed_on": date(2026, 5, 1),
                   "executed": False, "executed_on": None}]
        out = analyse.advisor(subs=[], savings_ledger=ledger, today=date(2026, 5, 30), idle=self.IDLE)
        self.assertIn("Big cut", out["the_one_move"])

    def test_without_idle_cash_nothing_changes(self):
        out = analyse.advisor(subs=[], savings_ledger=[], today=date(2026, 5, 30))
        self.assertEqual(out["the_one_move"], "Nothing outstanding on the ledger.")


if __name__ == "__main__":
    unittest.main()
