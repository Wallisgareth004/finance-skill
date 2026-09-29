#!/usr/bin/env python3
"""test_writer_styles.py — D7: the writer must not assume a style table.

Found 2026-09-24 by the backtest's chain replay. writer.py wrote every date with
s="21", "the canonical yyyy-mm-dd style" — true in the live workbook only. In an
older backup s=21 is `#,##0`, so every appended date read back as a serial
number, and the NEXT run died on them. The live file was never affected; every
other workbook — including any public user's — was.

The style is now resolved from xl/styles.xml: the canonical ids when they really
are a date / 2-dp number format, otherwise the style the sheet's own last data
row uses, otherwise the first matching format. No date format at all → refuse.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal

import openpyxl

from helpers import make_workbook, pipeline_dir

pipeline_dir()
import writer  # noqa: E402


class _Row:
    def __init__(self, d, desc, amt, bal):
        self.date = d
        self.description = desc
        self.category = "Groceries"
        self.amount = Decimal(amt)
        self.balance = Decimal(bal)
        self.source_slug = "export-2026-05-01_to_05-01"


def _read_back(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["TRANSACTIONS"]
    ws.reset_dimensions()
    rows = [r for r in ws.iter_rows(min_row=4, values_only=True) if r[0] is not None]
    wb.close()
    return rows


class TestDatesReadBackAsDates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.book = make_workbook(
            os.path.join(self.tmp, "m.xlsx"),
            rows=[(date(2026, 4, 30), "SEED", "Groceries", "-10.00", "990.00", "seed")],
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_appended_date_is_a_date_not_a_serial(self):
        writer.append_transactions(self.book, [_Row(date(2026, 5, 1), "SHOP", "-5.00", "985.00")])
        last = _read_back(self.book)[-1]
        self.assertIsInstance(last[0], datetime, f"date came back as {last[0]!r}")
        self.assertEqual(last[0].date(), date(2026, 5, 1))
        self.assertEqual(Decimal(str(last[3])), Decimal("-5.00"))

    def test_charts_survive(self):
        writer.append_transactions(self.book, [_Row(date(2026, 5, 1), "SHOP", "-5.00", "985.00")])
        self.assertEqual(writer._count_charts(self.book), 4)



if __name__ == "__main__":
    unittest.main()
