#!/usr/bin/env python3
"""test_caps.py — F2: each month's spend against the owner's monthly caps.

F2 (2026-09-27). A private caps file names each cap, its monthly amount and the
categories it covers ("eating out + delivery" = Restaurants + Food delivery).
The CFO report shows, per month, actual against cap. A cap naming a category
outside the vocabulary is REFUSED: a typo would otherwise sum to R0 and show a
cheerful ✅ for money that was never counted. Every figure here is synthetic.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import caps  # noqa: E402

CAPS_YAML = """
season: Test season
caps:
  - {name: Eating out, monthly: 900, categories: [Restaurants, Food delivery]}
  - {name: Groceries, monthly: 800, categories: [Groceries]}
"""

MONTHS = [
    {"month": "2026-03", "complete": True, "from": date(2026, 3, 1), "to": date(2026, 3, 31),
     "spend_by_category": {"Restaurants": Decimal("600.00"), "Food delivery": Decimal("400.00"),
                           "Groceries": Decimal("700.00")}},
    {"month": "2026-04", "complete": False, "from": date(2026, 4, 1), "to": date(2026, 4, 20),
     "spend_by_category": {"Groceries": Decimal("850.00")}},
]


class TestCaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "caps.yaml")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(CAPS_YAML)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_caps_file_is_read(self):
        season, cap_list = caps.load(self.path)
        self.assertEqual(season, "Test season")
        self.assertEqual([(c.name, c.monthly, c.categories) for c in cap_list], [
            ("Eating out", Decimal("900"), ("Restaurants", "Food delivery")),
            ("Groceries", Decimal("800"), ("Groceries",)),
        ])

    def test_a_category_outside_the_vocabulary_is_refused(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("caps:\n  - {name: Typo, monthly: 100, categories: [Restaurant]}\n")
        with self.assertRaises(caps.CapsError):
            caps.load(self.path)

    def test_no_file_means_no_caps(self):
        self.assertEqual(caps.load(os.path.join(self.tmp, "absent.yaml")), (None, []))

    def test_actual_against_cap_per_month(self):
        _, cap_list = caps.load(self.path)
        table = caps.table(MONTHS, cap_list)
        march = {line["name"]: line for line in table[0]["lines"]}
        self.assertEqual(march["Eating out"]["actual"], Decimal("1000.00"))
        self.assertEqual(march["Eating out"]["over"], Decimal("100.00"))
        self.assertEqual(march["Groceries"]["over"], Decimal("0.00"))
        april = {line["name"]: line for line in table[1]["lines"]}
        self.assertEqual(april["Eating out"]["actual"], Decimal("0.00"))
        self.assertEqual(april["Groceries"]["over"], Decimal("50.00"))
        self.assertFalse(table[1]["complete"])

    def test_the_report_marks_over_and_under(self):
        import emit

        _, cap_list = caps.load(self.path)
        body = emit.caps_markdown("Test season", caps.table(MONTHS, cap_list))
        self.assertIn("## Caps — Test season", body)
        self.assertIn("🔴 R1,000.00 (+R100.00)", body)
        self.assertIn("✅ R700.00", body)
        self.assertIn("2026-04 (1–20, to date)", body)


if __name__ == "__main__":
    unittest.main()
