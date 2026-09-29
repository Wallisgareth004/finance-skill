#!/usr/bin/env python3
"""test_pending_not_written.py — D9: an unsettled head row is never written.

Found 2026-09-24 by the backtest chain replay. A real export opened with an
unsettled airtime authorisation (Balance printed 0.00; parse nulls it). It was
queued and written with no balance. When the settled copy arrived in the next
export, dedupe matched it on (date, description, amount) and dropped it —
so the sheet kept the balance-less pending version for good. Had the settled
date differed, it would have imported twice; had the authorisation been
declined, the ledger would hold a payment that never happened.

A pending row is now reported and left for the export in which it settles.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import make_cheque_csv, make_workbook, pipeline_dir

ROWS = [  # newest first; row 0 is pending (0.00), row 1 equals the preamble balance
    ("2026/03/13", "-155.00", "0.00", "FNB APP PREPAID       AIRTIME 0000000000"),
    ("2026/03/12", "-54.21", "2215.40", "SHOP ONE"),
    ("2026/03/12", "-61.90", "2269.61", "SHOP TWO"),
    ("2026/03/11", "-3.00", "2331.51", "#INT PYMT FEE-47.13 SPTFY"),
]


class TestPending(unittest.TestCase):
    def test_the_pending_head_row_is_reported_not_queued(self):
        tmp = tempfile.mkdtemp()
        try:
            inbox = os.path.join(tmp, "in")
            os.makedirs(inbox)
            make_cheque_csv(inbox, rows=ROWS)
            book = make_workbook(os.path.join(tmp, "m.xlsx"))
            proc = subprocess.run(
                [sys.executable, os.path.join(pipeline_dir(), "run.py"), "--input", inbox,
                 "--model", book, "--reports", os.path.join(tmp, "r"),
                 "--db", os.path.join(tmp, "l.sqlite")],
                capture_output=True, text=True, timeout=120,
            )
            out = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 0, out)
            self.assertEqual(out["rows"]["new"], 3)
            self.assertEqual(out["rows"]["pending_not_written"], 1)
            preview = open(os.path.join(out["reports"], "import-preview.tsv"), encoding="utf-8").read()
            self.assertNotIn("AIRTIME", preview)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
