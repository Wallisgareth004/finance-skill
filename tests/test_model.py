#!/usr/bin/env python3
"""test_model.py — the dedupe gate.

Nothing may be imported until this module exists. The live TRANSACTIONS sheet
already held rows tagged with an export slug that re-parsing the drop folder
regenerates exactly — so without dedupe an import duplicates them.

These tests pin two defects that were measured in the planned interface on
2026-08-20 and would each have corrupted the ledger:

  1. `str(amount)` DOES NOT MATCH. openpyxl returns int for whole rands and
     float for everything else, so on a real sheet more than half of the
     amounts stringify to a form the parser's Decimal never produces (`-3` vs
     `-3.00`, `-615.3` vs `-615.30`). Re-importing a real cheque export with a
     naive str() key re-inserted most of its rows AS DUPLICATES. Money must be
     compared as integer cents.

  2. A `set` LOSES MULTIPLICITY. On a real ledger a few percent of rows share
     an identical (date, description, amount) triple, and every such group was
     proven genuine: all-distinct balances, sitting on consecutive links of the
     balance chain. Those second-or-later occurrences are real payments that a
     set cannot represent — a toll passed twice in a day is the common case.
     A Counter is required.
"""

from __future__ import annotations

import os
import re
import unittest
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import model  # noqa: E402



class _T:
    """The five attributes model.key() is allowed to read. Deliberately not
    parse.Txn — the dedupe key must not quietly depend on anything else."""

    def __init__(self, d, desc, amt, is_transaction=True):
        self.date = d
        self.description = desc
        # Raw int/float pass through UNCONVERTED. Wrapping every value in
        # Decimal made the sheet side and the CSV side identical before key()
        # ever saw them, so the int-vs-Decimal branch these tests are named for
        # was never exercised — and Decimal(-615.3) is the Decimal(float) route
        # model.py exists to forbid.
        self.amount = amt if isinstance(amt, (int, float)) else Decimal(amt)
        self.is_transaction = is_transaction


class TestKey(unittest.TestCase):
    def test_money_is_compared_as_integer_cents(self):
        """The sheet stores -3 as int and -615.3 as float. The parser produces
        Decimal('-3.00') and Decimal('-615.30'). str() never reconciles these."""
        self.assertEqual(model.amount_cents(-3), -300)
        self.assertEqual(model.amount_cents(-3.0), -300)
        self.assertEqual(model.amount_cents(Decimal("-3.00")), -300)
        self.assertEqual(model.amount_cents(-615.3), -61530)
        self.assertEqual(model.amount_cents(Decimal("-615.30")), -61530)
        self.assertEqual(model.amount_cents(3517.6), 351760)
        self.assertEqual(model.amount_cents(47250), 4725000)

    def test_int_and_decimal_forms_produce_the_same_key(self):
        sheet_side = model.key(_T(datetime(2026, 3, 12), "#INT PYMT FEE", -3))
        csv_side = model.key(_T(date(2026, 3, 12), "#INT PYMT FEE", "-3.00"))
        self.assertEqual(sheet_side, csv_side)

    def test_trailing_zero_cents_produce_the_same_key(self):
        sheet_side = model.key(_T(datetime(2026, 3, 6), "HARBOUR TAVERN", -615.3))
        csv_side = model.key(_T(date(2026, 3, 6), "HARBOUR TAVERN", "-615.30"))
        self.assertEqual(sheet_side, csv_side)

    def test_datetime_and_date_produce_the_same_key(self):
        """openpyxl returns datetime at midnight; the parser returns date."""
        self.assertEqual(
            model.key(_T(datetime(2026, 8, 12, 0, 0), "X", "-1.00")),
            model.key(_T(date(2026, 8, 12), "X", "-1.00")),
        )

    def test_no_description_placeholder_matches_an_empty_description(self):
        """Blank-narrative sheet rows carry the writer-side placeholder '(no description)'.
        The parser emits ''. Latent today — no blank descriptions in the current
        drop — but it would silently re-insert every such row."""
        self.assertEqual(
            model.key(_T(date(2026, 3, 3), model.NO_DESCRIPTION, "-5.00")),
            model.key(_T(date(2026, 3, 3), "", "-5.00")),
        )

    def test_internal_whitespace_is_not_collapsed(self):
        """FNB descriptions are fixed-width — merchant [0:20], card mask
        [20:31], posting date [33:39]. Many real rows carry runs of 2+ spaces
        that are load-bearing. Collapsing them fuses distinct merchants."""
        a = model.key(_T(date(2026, 5, 16), "WOOLWORTHS   400000  16 MAY", "-1.00"))
        b = model.key(_T(date(2026, 5, 16), "WOOLWORTHS 400000 16 MAY", "-1.00"))
        self.assertNotEqual(a, b)


class TestNewRows(unittest.TestCase):
    def test_new_row_survives(self):
        t = _T(date(2026, 3, 20), "ZZZ BRAND NEW MERCHANT", "-1.23")
        self.assertEqual(len(model.new_rows([t], Counter())), 1)

    def test_genuine_repeats_within_a_batch_are_not_collapsed(self):
        """A real card export carried an exact twin — same day, same merchant,
        same amount, twice (synthetic values here). Only multiplicity separates
        them. A set loses one of the two payments."""
        t1 = _T(date(2026, 3, 7), "SEASIDE MARKET KIOSK", "-118.40")
        t2 = _T(date(2026, 3, 7), "SEASIDE MARKET KIOSK", "-118.40")
        self.assertEqual(len(model.new_rows([t1, t2], Counter())), 2)

    def test_existing_copies_are_consumed_exactly_once(self):
        """One copy on the sheet, two in the batch → exactly one is new."""
        t1 = _T(date(2026, 3, 7), "N3 TOLL PLAZA", "-27.00")
        t2 = _T(date(2026, 3, 7), "N3 TOLL PLAZA", "-27.00")
        existing = Counter({model.key(t1): 1})
        self.assertEqual(len(model.new_rows([t1, t2], existing)), 1)

    def test_two_existing_copies_absorb_two_batch_copies(self):
        t = _T(date(2026, 3, 7), "N3 TOLL PLAZA", "-27.00")
        existing = Counter({model.key(t): 2})
        self.assertEqual(model.new_rows([t, t], existing), [])

    def test_file_order_is_preserved(self):
        """Format A is newest-first and dates are NOT monotonic. Order is the
        balance chain's sequence and must survive dedupe untouched."""
        a = _T(date(2026, 3, 12), "A", "-1.00")
        b = _T(date(2026, 3, 8), "B", "-2.00")
        c = _T(date(2026, 3, 12), "C", "-3.00")
        out = model.new_rows([a, b, c], Counter())
        self.assertEqual([t.description for t in out], ["A", "B", "C"])

    def test_does_not_mutate_the_callers_counter(self):
        """new_rows must be safe to call twice — run.py reports before writing."""
        t = _T(date(2026, 3, 7), "N3 TOLL PLAZA", "-27.00")
        existing = Counter({model.key(t): 1})
        model.new_rows([t], existing)
        self.assertEqual(existing[model.key(t)], 1)
        self.assertEqual(model.new_rows([t], existing), [])


class TestNeverWrites(unittest.TestCase):
    @staticmethod
    def _source() -> str:
        with open(os.path.join(pipeline_dir(), "model.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_module_never_calls_save(self):
        self.assertIsNone(
            re.search(r"\.save\s*\(", self._source()),
            "model.py must never save a workbook — it would delete 4 charts",
        )

    def test_module_loads_read_only(self):
        """Assert on the CALL, not on the file text. The old assertion was
        already satisfied by the module docstring, so deleting read_only=True
        from the actual load_workbook call kept the test green — on the one
        guard standing between this module and four destroyed charts."""
        import ast

        tree = ast.parse(self._source())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "load_workbook"
        ]
        # D12 added the CREDIT CARD reader, so there is more than one call now:
        # EVERY one must be read-only, not just the first.
        self.assertGreaterEqual(len(calls), 1, "expected a load_workbook call")
        for call in calls:
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            for name in ("read_only", "data_only"):
                self.assertIn(name, kwargs, f"load_workbook must pass {name}")
                self.assertTrue(
                    isinstance(kwargs[name], ast.Constant) and kwargs[name].value is True,
                    f"{name} must be literally True",
                )


if __name__ == "__main__":
    unittest.main()
