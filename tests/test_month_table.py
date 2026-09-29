#!/usr/bin/env python3
"""test_month_table.py — F1: the CFO report breaks the window into calendar months.

One 90-day figure hides the month that went wrong. F1 (2026-09-27) adds a table:
one row per calendar month inside the window, with days covered, income, spend
on each account, the deficit, and pending card spend.

  · a deficit is stated for COMPLETE months only; a month the window cuts short
    shows its spend to date and no monthly rate (a part-month scaled up is a
    guess, and this report does not guess)
  · two deficits per month: the cheque account alone (the definition the
    owner's earlier figures used, kept so they stay comparable) and all
    accounts (card purchases included, D12)
  · pending card spend sits in the month it was authorised, apart from spend
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402

Q = Decimal("0.01")


class _T:
    def __init__(self, d, desc, amt, category, account="cheque"):
        self.date, self.description, self.amount = d, desc, Decimal(amt)
        self.category, self.account, self.is_transaction = category, account, True


ROWS = [
    _T(date(2026, 2, 15), "SHOP", "-500.00", "Groceries"),
    _T(date(2026, 2, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity"),
    _T(date(2026, 3, 5), "SHOP", "-1000.00", "Groceries"),
    _T(date(2026, 3, 8), "iK *HARBOUR GRILL", "-500.00", "Restaurants", "card"),
    _T(date(2026, 3, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity"),
    _T(date(2026, 3, 25), "CREDIT CARD PAYMENT", "-500.00", "Transfer — card settlement"),
    _T(date(2026, 3, 25), "PAYMENT - THANK YOU", "500.00", "Transfer — card settlement", "card"),
    _T(date(2026, 4, 10), "SHOP", "-700.00", "Groceries"),
    _T(date(2026, 4, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity"),
]
PENDING = [_T(date(2026, 4, 19), "SYNTH CAFE", "-99.90", "Restaurants")]
WINDOW = analyse.Window(date(2026, 2, 10), date(2026, 4, 20))


class TestMonthTable(unittest.TestCase):
    def setUp(self):
        self.table = analyse.month_table(ROWS, WINDOW, gaps=[], today=date(2026, 4, 21),
                                         pending=PENDING)
        self.by = {m["month"]: m for m in self.table}

    def test_one_row_per_calendar_month_in_the_window(self):
        self.assertEqual([m["month"] for m in self.table], ["2026-02", "2026-03", "2026-04"])
        self.assertEqual((self.by["2026-02"]["from"], self.by["2026-04"]["to"]),
                         (date(2026, 2, 10), date(2026, 4, 20)))

    def test_a_complete_month_has_both_deficits(self):
        m = self.by["2026-03"]
        self.assertTrue(m["complete"])
        self.assertEqual((m["spend_cheque"], m["spend_card"]), (Decimal("1000.00"), Decimal("500.00")))
        self.assertEqual(m["deficit_cheque"],
                         (Decimal("30000") - (Decimal("1000") * Decimal("30.44") / 31).quantize(Q)))
        self.assertEqual(m["deficit"],
                         (Decimal("30000") - (Decimal("1500") * Decimal("30.44") / 31).quantize(Q)))

    def test_a_month_cut_short_shows_spend_to_date_and_no_rate(self):
        for month in ("2026-02", "2026-04"):
            m = self.by[month]
            self.assertFalse(m["complete"])
            self.assertIsNone(m["deficit"])
            self.assertIsNone(m["deficit_cheque"])
        self.assertEqual(self.by["2026-04"]["spend"], Decimal("700.00"))

    def test_pending_after_the_last_posted_day_still_counts_in_its_month(self):
        """The window ends at the newest POSTED row; an authorisation from the
        next day belongs to the same calendar month, not to nowhere."""
        later = PENDING + [_T(date(2026, 4, 23), "SYNTH FUEL", "-500.00", "Fuel")]
        table = analyse.month_table(ROWS, WINDOW, gaps=[], today=date(2026, 4, 24), pending=later)
        self.assertEqual(table[-1]["pending"], Decimal("599.90"))

    def test_pending_sits_in_its_month_apart_from_spend(self):
        self.assertEqual(self.by["2026-04"]["pending"], Decimal("99.90"))
        self.assertEqual(self.by["2026-03"]["pending"], Decimal("0.00"))

    def test_the_report_renders_the_table(self):
        import emit

        body = emit.month_table_markdown(self.table)
        self.assertIn("| 2026-03 |", body)
        self.assertIn("to date", body)
        self.assertIn("| R29,018.06 | R28,527.10 |", body)  # March: a surplus on both


if __name__ == "__main__":
    unittest.main()
