#!/usr/bin/env python3
"""test_supplements.py — D15: supplements are their own category.

Supplements were filed under "Medical aid & health", so the health line mixed a
fixed medical-aid debit order with a discretionary habit the owner wants to
watch on its own. D15 (2026-09-27): "Supplements" joins the vocabulary, and the
rules send supplement stores there. The owner's own merchant patterns live in
the private rules; the public example set matches only generic words. A
pharmacy stays health: its narrative cannot say what was bought.
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from decimal import Decimal

from helpers import SKILL_ROOT, pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import categorise  # noqa: E402

PUBLIC_RULES = next(
    p for p in (os.path.join(SKILL_ROOT, "publish", "rules.public.yaml"),
                os.path.join(SKILL_ROOT, "rules.yaml"))
    if os.path.exists(p) and "PUBLIC EXAMPLE" in open(p, encoding="utf-8").read()
)


class _T:
    def __init__(self, d, desc, amt, category=""):
        self.date, self.description, self.amount = d, desc, Decimal(amt)
        self.category, self.is_transaction = category, True


class TestSupplements(unittest.TestCase):
    def test_the_label_is_in_the_vocabulary(self):
        self.assertIn("Supplements", categorise.VOCABULARY)

    def test_a_supplement_store_is_supplements_and_a_pharmacy_is_health(self):
        rules = categorise.load(PUBLIC_RULES)
        self.assertEqual(rules.classify(_T(date(2026, 3, 1), "SYNTH VITAMIN SHOP", "-420.00"))[0],
                         "Supplements")
        self.assertEqual(rules.classify(_T(date(2026, 3, 1), "CLICKS", "-120.00"))[0],
                         "Medical aid & health")

    def test_supplements_are_spend_on_their_own_line(self):
        rows = [
            _T(date(2026, 3, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity"),
            _T(date(2026, 3, 2), "SYNTH VITAMIN SHOP", "-420.00", "Supplements"),
            _T(date(2026, 3, 1), "MEDSCHEME", "-580.00", "Medical aid & health"),
        ]
        out = analyse.cfo(txns=rows, window=analyse.Window(date(2026, 3, 1), date(2026, 3, 31)),
                          cash={}, today=date(2026, 4, 1), gaps=[])
        self.assertEqual(out["spend_by_category"]["Supplements"], Decimal("420.00"))
        self.assertEqual(out["spend_by_category"]["Medical aid & health"], Decimal("580.00"))
        self.assertEqual(out["spend"], Decimal("1000.00"))


if __name__ == "__main__":
    unittest.main()
