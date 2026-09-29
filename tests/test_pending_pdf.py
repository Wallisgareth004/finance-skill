#!/usr/bin/env python3
"""test_pending_pdf.py — D11: card spend that has not posted yet.

Found 2026-09-27: the drop held FNB's "Transaction history" PDF — card
authorisations that had not posted to the account yet, several days of real
spend. The CSV exports hold POSTED rows only, and run.py read CSVs only, so
the money was invisible (D10 made the PDF visible; this makes it read).

The contract, pinned here on synthetic PDFs:
  · every authorisation is read: date, card, description, amount
  · a row whose amount is carried to the next page is still paired correctly
  · anything that is not that layout is REFUSED with a reason, never guessed
  · pending spend is reported SEPARATELY as "pending, not yet posted": it is
    never in posted spend, never in the deficit, never written to the workbook
  · once a pending row posts, it drops out of pending: matched on card, amount,
    merchant and the purchase date printed in the posted narrative
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from helpers import make_cheque_csv, make_pending_pdf, pipeline_dir, synthetic_workbook

pipeline_dir()
import pending  # noqa: E402

RUN = os.path.join(pipeline_dir(), "run.py")
ROWS = [
    ("24 Sep 2026 10:15:00", "SYNTH CAFE", "90.00"),
    ("25 Sep 2026 16:47:51", "SYNTH BOOKS AND TOYS", "400.00"),
    ("25 Sep 2026 17:36:42", "SYNTH FUEL STATION", "300.00"),
    ("26 Sep 2026 09:01:02", "SYNTH CAFE", "90.00"),
]
TOTAL = Decimal("880.00")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestReadPdf(_Tmp):
    def test_every_authorisation_is_read(self):
        path = make_pending_pdf(os.path.join(self.tmp, "p.pdf"), ROWS)
        header, rows = pending.read_pdf(path)
        self.assertEqual(header["account"], "10000000001")
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(-r.amount for r in rows), TOTAL)
        self.assertEqual([r.date for r in rows][:2], [date(2026, 9, 24), date(2026, 9, 25)])
        self.assertEqual(rows[1].description, "SYNTH BOOKS AND TOYS")
        self.assertEqual(rows[1].card, "400000******0001")
        self.assertTrue(all(r.amount < 0 for r in rows), "authorisations are money out")

    def test_an_amount_carried_to_the_next_page_stays_with_its_row(self):
        path = make_pending_pdf(os.path.join(self.tmp, "p.pdf"), ROWS, rows_per_page=2)
        _, rows = pending.read_pdf(path)
        self.assertEqual([(r.description, -r.amount) for r in rows], [
            ("SYNTH CAFE", Decimal("90.00")),
            ("SYNTH BOOKS AND TOYS", Decimal("400.00")),
            ("SYNTH FUEL STATION", Decimal("300.00")),
            ("SYNTH CAFE", Decimal("90.00")),
        ])

    def test_a_different_pdf_is_refused_with_a_reason(self):
        path = make_pending_pdf(os.path.join(self.tmp, "p.pdf"), ROWS, title="Tax certificate")
        with self.assertRaises(pending.PendingFormatError) as ctx:
            pending.read_pdf(path)
        self.assertIn("Transaction history", str(ctx.exception))

    def test_rows_that_do_not_pair_up_are_refused_not_guessed(self):
        path = make_pending_pdf(os.path.join(self.tmp, "p.pdf"), ROWS, rows_per_page=2,
                                omit_amounts=(2,))
        with self.assertRaises(pending.PendingFormatError) as ctx:
            pending.read_pdf(path)
        self.assertIn("4 authorisations but 3 amounts", str(ctx.exception))

    def test_a_line_that_is_neither_a_row_nor_an_amount_is_refused(self):
        """A mangled page once turned one row into two fragments that matched
        nothing, and the rest still alternated: a row vanished in silence."""
        path = make_pending_pdf(os.path.join(self.tmp, "p.pdf"), ROWS,
                                stray="26 Sep 2026 09:01:02")
        with self.assertRaises(pending.PendingFormatError) as ctx:
            pending.read_pdf(path)
        self.assertIn("neither", str(ctx.exception))

    def test_a_file_that_is_not_a_pdf_is_refused(self):
        path = os.path.join(self.tmp, "p.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 not really")
        with self.assertRaises(pending.PendingFormatError):
            pending.read_pdf(path)


class _Posted:
    def __init__(self, d, description, amount):
        self.date, self.description, self.amount = d, description, Decimal(amount)


class TestOncePosted(_Tmp):
    def _pending(self):
        return pending.read_pdf(make_pending_pdf(os.path.join(self.tmp, "p.pdf"), ROWS))[1]

    def test_a_posted_row_with_the_same_purchase_date_retires_it(self):
        posted = [_Posted(date(2026, 9, 26), "SYNTH FUEL STATION  400000*0001  25 SEP", "-300.00")]
        still, matched = pending.drop_posted(self._pending(), posted)
        self.assertEqual(len(still), 3)
        self.assertEqual(matched, 1)

    def test_an_earlier_purchase_of_the_same_amount_does_not(self):
        """A R90 coffee bought on the 22nd and posted on the 24th is a
        different coffee from the one authorised on the 24th."""
        posted = [_Posted(date(2026, 9, 24), "SYNTH CAFE          400000*0001  22 SEP", "-90.00")]
        still, matched = pending.drop_posted(self._pending(), posted)
        self.assertEqual((len(still), matched), (4, 0))

    def test_another_card_does_not(self):
        posted = [_Posted(date(2026, 9, 26), "SYNTH FUEL STATION  400000*0009  25 SEP", "-300.00")]
        self.assertEqual(pending.drop_posted(self._pending(), posted)[1], 0)

    def test_one_posted_row_retires_one_pending_row(self):
        posted = [_Posted(date(2026, 9, 27), "SYNTH CAFE          400000*0001  24 SEP", "-90.00")]
        still, matched = pending.drop_posted(self._pending(), posted)
        self.assertEqual(matched, 1)
        self.assertEqual(sum(1 for r in still if r.description == "SYNTH CAFE"), 1)


class TestRunReportsPendingSeparately(_Tmp):
    def _run(self, with_pdf=True):
        inbox = os.path.join(self.tmp, "in" if with_pdf else "in-plain")
        os.makedirs(inbox)
        make_cheque_csv(inbox)
        if with_pdf:
            make_pending_pdf(os.path.join(inbox, "OnlineBankingDownload.pdf"), ROWS)
        proc = subprocess.run(
            [sys.executable, RUN, "--input", inbox, "--model", synthetic_workbook(),
             "--reports", os.path.join(self.tmp, "r-" + str(with_pdf)),
             "--db", os.path.join(self.tmp, f"l-{with_pdf}.sqlite")],
            capture_output=True, text=True, timeout=180,
        )
        self.assertNotIn("Traceback", proc.stderr)
        return proc.returncode, json.loads(proc.stdout)

    def test_pending_is_reported_and_kept_out_of_spend_and_the_write(self):
        code, out = self._run()
        _, plain = self._run(with_pdf=False)
        self.assertEqual(code, 0, out.get("error"))
        p = out["pending"]
        self.assertEqual((p["rows"], Decimal(p["total"])), (4, TOTAL))
        self.assertEqual((p["from"], p["to"]), ("2026-09-24", "2026-09-26"))
        self.assertEqual(p["files"], ["OnlineBankingDownload.pdf"])
        self.assertEqual(out["ignored_files"], [])
        self.assertEqual(out["rows"], plain["rows"], "pending never joins posted rows")
        with open(os.path.join(out["reports"], "import-preview.tsv"), encoding="utf-8") as fh:
            self.assertNotIn("SYNTH", fh.read(), "pending is never written")
        with open(os.path.join(out["reports"], "01-cfo.md"), encoding="utf-8") as fh:
            cfo = fh.read()
        self.assertIn("Pending — not yet posted", cfo)
        self.assertIn("R880.00", cfo)
        self.assertNotIn("SYNTH", cfo, "category level only: no merchant names")


class TestWithoutPypdf(_Tmp):
    def test_a_missing_pdf_library_is_named_not_silent(self):
        path = make_pending_pdf(os.path.join(self.tmp, "p.pdf"), ROWS)
        saved = sys.modules.get("pypdf")
        sys.modules["pypdf"] = None  # import pypdf now raises ImportError
        try:
            with self.assertRaises(pending.PendingUnavailable) as ctx:
                pending.read_pdf(path)
        finally:
            if saved is not None:
                sys.modules["pypdf"] = saved
            else:
                sys.modules.pop("pypdf", None)
        self.assertIn("pypdf", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
