#!/usr/bin/env python3
"""test_savings_account.py — D6: a savings export is not the cheque account.

Found 2026-09-24 replaying a real drop. detect.py classed every non-credit
account as "cheque", so the `[Savings Account]` export was queued for
TRANSACTIONS beside the cheque rows: that day's import-preview included the
savings account's own BYC CREDIT / interest lines. An import would have mixed
two accounts' ledgers on one sheet. It was never run.

Savings now has its own kind: parsed, balance-chained and anchored like cheque,
reported, and never written.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import make_cheque_csv, pipeline_dir, prototype_workbook

pipeline_dir()
import detect  # noqa: E402

SAVINGS_PREAMBLE_ACCOUNT = "Account:, 10000000002, [Savings Account]\n"


def make_savings_csv(directory, name="10000000002.csv"):
    path = make_cheque_csv(
        directory,
        rows=[
            ("2026/03/17", "183.00", "1143.88", "BYC CREDIT            10000000001"),
            ("2026/03/10", "-40.00", "960.88", "TRANSFER TO CHEQUE"),
        ],
        name=name,
    )
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    body = body.replace("Account:, 10000000001, [Easy Account]\n", SAVINGS_PREAMBLE_ACCOUNT)
    body = body.replace("Balance:, 2215.40, 615.25", "Balance:, 1143.88, 1143.88")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


class TestSavingsKind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detect_names_a_savings_account(self):
        det = detect.sniff(make_savings_csv(self.tmp))
        self.assertEqual(det.account_kind, "savings")

    def test_an_easy_account_is_still_cheque(self):
        det = detect.sniff(make_cheque_csv(self.tmp))
        self.assertEqual(det.account_kind, "cheque")

    @unittest.skipUnless(prototype_workbook(), "no workbook to dry-run against")
    def test_savings_rows_are_chained_but_never_queued(self):
        inbox = os.path.join(self.tmp, "in")
        os.makedirs(inbox)
        make_cheque_csv(inbox)
        make_savings_csv(inbox)
        copy = os.path.join(self.tmp, "m.xlsx")
        shutil.copy2(prototype_workbook(), copy)
        proc = subprocess.run(
            [sys.executable, os.path.join(pipeline_dir(), "run.py"), "--input", inbox,
             "--model", copy, "--reports", os.path.join(self.tmp, "r"),
             "--db", os.path.join(self.tmp, "l.sqlite")],
            capture_output=True, text=True, timeout=300,
        )
        out = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, out)
        preview = open(os.path.join(out["reports"], "import-preview.tsv"), encoding="utf-8").read()
        self.assertNotIn("BYC CREDIT", preview)
        self.assertEqual(out["rows"]["savings_new_not_written"], 2)
        savings_chain = [c for c in out["chain"] if c["file"] == "10000000002.csv"][0]
        self.assertEqual(savings_chain["checked"], 1)



if __name__ == "__main__":
    unittest.main()
