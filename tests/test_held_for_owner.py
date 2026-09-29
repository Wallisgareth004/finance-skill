#!/usr/bin/env python3
"""test_held_for_owner.py — a question already put to the owner is not answered by a rule.

D13, 2026-09-27. The owner was sent a checklist of unlabelled rows to label
himself. New rules for obvious patterns (a restaurant chain, a car wash) would
also match some of those rows, answering questions that are his to answer. So
the rules engine takes a HOLD list: rows identified by a hash of (date,
description, amount), which stay `Uncategorised` whatever the rules say, marked
`held-for-owner`, until he answers. Rows not on the list — including every
future row from the same merchants — are labelled as usual.

The hold list is private (it identifies real rows). Without one, nothing is held.
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
import categorise  # noqa: E402
import model  # noqa: E402


class _T:
    def __init__(self, d, desc, amt):
        self.date, self.description, self.amount = d, desc, Decimal(amt)


RULES = """
rules:
  - id: restaurants
    pattern: 'SPUR'
    category: "Restaurants"
fallback: "Uncategorised"
"""


class TestHold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rules = os.path.join(self.tmp, "rules.yaml")
        with open(self.rules, "w", encoding="utf-8") as fh:
            fh.write(RULES)
        self.asked = _T(date(2026, 3, 5), "HARBOUR SPUR", "-110.00")
        self.later = _T(date(2026, 3, 25), "HARBOUR SPUR", "-110.00")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hold(self, *rows):
        with open(os.path.join(self.tmp, "review_hold.private.yaml"), "w", encoding="utf-8") as fh:
            fh.write("rows:\n" + "".join(f"  - {model.row_hash(r)}\n" for r in rows))

    def test_a_held_row_stays_unlabelled_and_says_why(self):
        self._hold(self.asked)
        ruleset = categorise.load(self.rules)
        self.assertEqual(ruleset.classify(self.asked)[:2], ("Uncategorised", categorise.HELD))

    def test_a_row_not_on_the_list_is_labelled_as_usual(self):
        self._hold(self.asked)
        ruleset = categorise.load(self.rules)
        self.assertEqual(ruleset.classify(self.later)[0], "Restaurants")

    def test_no_hold_file_holds_nothing(self):
        ruleset = categorise.load(self.rules)
        self.assertEqual(ruleset.classify(self.asked)[0], "Restaurants")

    def test_the_hash_ignores_what_the_dedupe_key_ignores(self):
        """Same identity as model.key: the workbook's int -3 and the parser's
        Decimal('-3.00') are one row, and so is a datetime at midnight."""
        from datetime import datetime

        a = _T(datetime(2026, 3, 5, 0, 0), "HARBOUR SPUR", "-110.0")
        self.assertEqual(model.row_hash(a), model.row_hash(self.asked))


if __name__ == "__main__":
    unittest.main()
