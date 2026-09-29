#!/usr/bin/env python3
"""test_bad_workbook_row.py — D8: a workbook the model cannot read exits 3, as JSON.

Found 2026-09-24 by the backtest chain replay (the rows D7 wrote with serial
dates). model.load_existing raised ModelReadError — correctly refusing to guess —
but run.py did not catch it, so the operator got a Python traceback, exit 1 (the
code for BAD INPUT PATH) and no JSON. The exit contract says every path prints
JSON; an unreadable ledger row is a verification failure: exit 3.
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

from helpers import make_cheque_csv, make_workbook, pipeline_dir


class TestBadRow(unittest.TestCase):
    def test_a_non_date_in_the_date_column_exits_3_with_json(self):
        tmp = tempfile.mkdtemp()
        try:
            book = make_workbook(
                os.path.join(tmp, "m.xlsx"),
                rows=[
                    (date(2026, 8, 1), "OK ROW", "Groceries", "-1.00", "10.00", "seed"),
                    ("46258", "SERIAL ROW", "Groceries", "-1.00", "9.00", "seed"),
                ],
            )
            inbox = os.path.join(tmp, "in")
            os.makedirs(inbox)
            make_cheque_csv(inbox)
            proc = subprocess.run(
                [sys.executable, os.path.join(pipeline_dir(), "run.py"), "--input", inbox,
                 "--model", book, "--reports", os.path.join(tmp, "r"),
                 "--db", os.path.join(tmp, "l.sqlite")],
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotIn("Traceback", proc.stderr)
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr[-800:])
            out = json.loads(proc.stdout)
            self.assertFalse(out["ok"])
            self.assertIn("row 5", out["error"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
