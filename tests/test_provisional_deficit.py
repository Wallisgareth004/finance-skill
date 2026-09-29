#!/usr/bin/env python3
"""test_provisional_deficit.py — a deficit resting on unlabelled spend says so.

2026-09-24: the owner's real report printed a firm-looking deficit while many
rows of the window had no category. Unlabelled debits ARE counted as spend,
which is the cautious choice. But a reader must see that labelling them could
move the figure, and by how much at most. The worst case is that every
unlabelled rand belongs outside spend (a transfer, a trip, a one-off), in which
case the deficit shrinks by exactly their per-month total.

Above 5% of spend (the same bar as the uncategorised-rows target) the deficit
is marked PROVISIONAL and the report states the bound. It is never refused on
these grounds: unlabelled debits are real money that left the account.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import emit  # noqa: E402

WINDOW = analyse.Window(date(2026, 8, 1), date(2026, 8, 31))


class _T:
    def __init__(self, d, desc, amt, category):
        self.date = d
        self.description = desc
        self.amount = Decimal(amt)
        self.category = category
        self.is_transaction = True


def _cfo(uncategorised: str):
    rows = [
        _T(date(2026, 8, 20), "ANNUITY", "30000.00", "Income — annuity"),
        _T(date(2026, 8, 5), "SHOP", "-10000.00", "Groceries"),
        _T(date(2026, 8, 6), "UNKNOWN", f"-{uncategorised}", "Uncategorised"),
    ]
    return analyse.cfo(txns=rows, window=WINDOW, cash={}, today=date(2026, 9, 1), gaps=[])


def _q(v):
    return v.quantize(Decimal("0.01"))


class TestProvisional(unittest.TestCase):
    def test_small_unlabelled_spend_is_firm(self):
        out = _cfo("400.00")  # 3.8% of R10,400
        self.assertFalse(out["provisional"]["is_provisional"])
        self.assertIsNotNone(out["deficit"])

    def test_material_unlabelled_spend_marks_the_deficit_provisional(self):
        out = _cfo("2000.00")  # 16.7% of R12,000
        p = out["provisional"]
        self.assertTrue(p["is_provisional"])
        self.assertEqual(p["rows"], 1)
        self.assertEqual(p["uncategorised_spend"], Decimal("2000.00"))
        self.assertEqual(p["per_month"], _q(Decimal("2000") * Decimal("30.44") / 31))
        self.assertIsNotNone(out["deficit"], "provisional is not refused")

    def test_the_bound_is_stated_in_the_report(self):
        body = emit.cfo_markdown(_cfo("2000.00"))
        self.assertIn("PROVISIONAL", body)
        self.assertIn("1 uncategorised row carries", body)
        self.assertIn("up to R1,963.87", body)

    def test_a_firm_deficit_is_not_labelled_provisional(self):
        self.assertNotIn("PROVISIONAL", emit.cfo_markdown(_cfo("400.00")))


if __name__ == "__main__":
    unittest.main()
