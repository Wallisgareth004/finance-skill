#!/usr/bin/env python3
"""test_ignored_files.py — D10: a file in the drop is never skipped in silence.

Found 2026-09-27: a bank-statement PDF sat in the drop beside the CSV exports.
run.py only looked at *.csv, so the PDF was skipped and appeared nowhere — not
in the run JSON, not in the reports. A file the owner put in the drop and the
pipeline never mentions is a coverage question nobody gets to ask.

Every non-CSV entry is now listed in the run JSON as `ignored_files`, with the
reason, and in notes-and-gaps.txt. (A pending-transactions PDF is not ignored:
pending.py reads it. See test_pending_pdf.py.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import make_cheque_csv, pipeline_dir, synthetic_workbook

RUN = os.path.join(pipeline_dir(), "run.py")


class TestIgnoredFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inbox = os.path.join(self.tmp, "in")
        self.reports = os.path.join(self.tmp, "reports")
        os.makedirs(self.inbox)
        make_cheque_csv(self.inbox)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        proc = subprocess.run(
            [
                sys.executable,
                RUN,
                "--input",
                self.inbox,
                "--model",
                synthetic_workbook(),
                "--reports",
                self.reports,
                "--db",
                os.path.join(self.tmp, "l.sqlite"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertNotIn("Traceback", proc.stderr)
        out = json.loads(proc.stdout)
        with open(
            os.path.join(out["reports"], "notes-and-gaps.txt"), encoding="utf-8"
        ) as fh:
            notes = fh.read()
        return proc.returncode, out, notes

    def _put(self, name, body=b"x"):
        with open(os.path.join(self.inbox, name), "wb") as fh:
            fh.write(body)

    def test_every_non_csv_file_is_listed_with_a_reason(self):
        self._put("notes.txt", b"remember the fibre")
        self._put("statement.pdf", b"%PDF-1.4 this is not a real PDF")
        code, out, notes = self._run()
        self.assertEqual(code, 0, out)
        listed = {row["file"]: row["why"] for row in out["ignored_files"]}
        self.assertEqual(set(listed), {"notes.txt", "statement.pdf"})
        self.assertTrue(all(why for why in listed.values()), listed)
        for name in ("notes.txt", "statement.pdf"):
            self.assertIn(f"IGNORED\t{name}", notes)

    def test_a_folder_in_the_drop_is_listed_not_walked(self):
        os.makedirs(os.path.join(self.inbox, "older exports"))
        _code, out, notes = self._run()
        self.assertEqual(
            [row["file"] for row in out["ignored_files"]], ["older exports"]
        )
        self.assertIn("folder", out["ignored_files"][0]["why"])
        self.assertIn("IGNORED\tolder exports", notes)

    def test_a_clean_drop_reports_an_empty_list(self):
        _code, out, notes = self._run()
        self.assertEqual(out["ignored_files"], [])
        self.assertNotIn("IGNORED", notes)


if __name__ == "__main__":
    unittest.main()
