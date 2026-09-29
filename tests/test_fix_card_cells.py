#!/usr/bin/env python3
"""test_fix_card_cells.py — truncated CREDIT CARD descriptions, fixed through the writer.

An older import read the card export with csv.DictReader, which cut every
description at its first comma: "SEASIDE TRADING" where the export says
"SEASIDE TRADING,LDA     Port Harbour". The parser is right and the sheet is
wrong, so the cells are fixed, not the code — through writer.py's contract:
backup first, change only the named cells, refuse if a cell does not read what
it is expected to read, verify (well-formed XML, charts intact, the new text
reads back), then an atomic replace. Synthetic workbook only.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile
from datetime import date
from decimal import Decimal

from helpers import make_workbook, pipeline_dir

pipeline_dir()
import model  # noqa: E402
import writer  # noqa: E402

CARD = [
    (date(2026, 3, 8), "iK *HARBOUR GRILL", "Restaurants", "-138.00"),
    (date(2026, 3, 9), "SEASIDE TRADING", "Travel — trip", "-583.20"),  # cut at a comma
]


class _E:
    def __init__(self, d, desc, amt):
        self.date, self.description, self.amount = d, desc, Decimal(amt)


EXPORT = [_E(date(2026, 3, 8), "iK *HARBOUR GRILL       DURBAN          ", "-138.00"),
          _E(date(2026, 3, 9), "SEASIDE TRADING,LDA     Port Harbour    ", "-583.20")]


class TestFixCardCells(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.book = make_workbook(os.path.join(self.tmp, "m.xlsx"), card_rows=CARD)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _charts(self):
        with zipfile.ZipFile(self.book) as z:
            return sum(1 for n in z.namelist() if "charts/chart" in n)

    def test_the_truncated_cells_are_found(self):
        fixes = model.truncated_card_cells(model.load_card_sheet(self.book), EXPORT)
        self.assertEqual(fixes, [("B10", "SEASIDE TRADING", "SEASIDE TRADING,LDA     Port Harbour")])

    def test_the_writer_fixes_only_those_cells_backing_up_first(self):
        charts = self._charts()
        fixes = model.truncated_card_cells(model.load_card_sheet(self.book), EXPORT)
        result = writer.fix_text_cells(self.book, model.CARD_SHEET, fixes)
        self.assertTrue(os.path.exists(result.backup_path))
        self.assertEqual((result.cells_fixed, result.charts_before, result.charts_after),
                         (1, charts, charts))
        rows = model.load_card_sheet(self.book)
        self.assertEqual([r.description for r in rows],
                         ["iK *HARBOUR GRILL", "SEASIDE TRADING,LDA     Port Harbour"])
        self.assertEqual([r.amount for r in rows], [Decimal("-138.00"), Decimal("-583.20")])
        self.assertEqual(model.truncated_card_cells(rows, EXPORT), [], "nothing left to fix")

    def test_a_cell_that_does_not_read_as_expected_is_refused_and_nothing_changes(self):
        with open(self.book, "rb") as fh:
            before = fh.read()
        with self.assertRaises(writer.WriteFailed):
            writer.fix_text_cells(self.book, model.CARD_SHEET, [("B10", "SOMETHING ELSE", "X")])
        with open(self.book, "rb") as fh:
            self.assertEqual(fh.read(), before, "the original is untouched")

    def test_an_unknown_sheet_is_refused(self):
        with self.assertRaises(writer.WriteFailed):
            writer.fix_text_cells(self.book, "NO SUCH SHEET", [("B10", "a", "b")])


if __name__ == "__main__":
    unittest.main()
