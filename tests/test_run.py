#!/usr/bin/env python3
"""test_run.py — the orchestrator and its exit contract.

The exit contract is the whole point. From a sibling pipeline's repair,
2026-08-20: `"ok": True` was hardcoded, so a run in which every file failed
still exited 0 and was reported as fresh. This suite asserts the status is DERIVED, that every
exit path prints valid JSON rather than a traceback, and that a balance-chain
break is fatal rather than a warning.

    0  clean
    1  bad input
    2  no data
    3  parse or verification failure

Two divergences from that pipeline, both deliberate:

  · the DATED reports directory is this script's own default, not something the
    caller has to remember. That pipeline defaults to a flat `reports/` and only
    its docs add $(date), so anyone running it directly writes to the root,
    where stale files then shadow the fresh run.
  · `ok` is a boolean on each file entry, not `status.startswith("error")`.
    Rewording a status string silently restored the ok:True bug there.
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

RUN = os.path.join(pipeline_dir(), "run.py")
PROTOTYPE = prototype_workbook()  # see helpers.prototype_workbook (2026-08-25)


def _run(*args):
    proc = subprocess.run(
        [sys.executable, RUN, *args], capture_output=True, text=True, timeout=180
    )
    return proc


class _RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inbox = os.path.join(self.tmp, "in")
        self.reports = os.path.join(self.tmp, "reports")
        self.db = os.path.join(self.tmp, "ledger.sqlite")
        os.makedirs(self.inbox)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, *extra):
        return (
            "--input",
            self.inbox,
            "--reports",
            self.reports,
            "--db",
            self.db,
            *extra,
        )

    def assertJson(self, proc):
        self.assertNotIn("Traceback", proc.stderr, "a traceback reached the operator")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:  # pragma: no cover - diagnostic
            self.fail(f"stdout was not JSON:\n{proc.stdout}\n{proc.stderr}")


class TestExitContract(_RunCase):
    def test_bad_input_path_exits_1(self):
        proc = _run(
            "--input",
            os.path.join(self.tmp, "nope"),
            "--reports",
            self.reports,
            "--db",
            self.db,
        )
        self.assertEqual(proc.returncode, 1)
        out = self.assertJson(proc)
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["error"])

    def test_empty_folder_exits_2(self):
        proc = _run(*self._args())
        self.assertEqual(proc.returncode, 2)
        out = self.assertJson(proc)
        self.assertFalse(out["ok"])

    def test_a_broken_balance_chain_exits_3(self):
        """A chain break is FATAL, not a warning. It is the strongest
        correctness signal available — not one break across the real exports."""
        make_cheque_csv(
            self.inbox,
            rows=[
                ("2026/08/12", "-100.00", "900.00", "A"),
                ("2026/08/11", "-100.00", "5555.55", "B"),
                ("2026/08/10", "-100.00", "1111.11", "C"),
            ],
        )
        proc = _run(*self._args("--model", PROTOTYPE))
        self.assertEqual(proc.returncode, 3, proc.stdout)
        out = self.assertJson(proc)
        self.assertFalse(out["ok"])
        self.assertFalse(out["chain"][0]["ok"])
        self.assertGreater(len(out["chain"][0]["breaks"]), 0)

    def test_an_unreadable_file_is_reported_not_crashed(self):
        with open(os.path.join(self.inbox, "junk.csv"), "w") as handle:
            handle.write("this is not an FNB export\n")
        proc = _run(*self._args())
        self.assertIn(proc.returncode, (2, 3))
        out = self.assertJson(proc)
        self.assertIn("junk.csv", str(out["unrecognized_files"]))

    @unittest.skipUnless(PROTOTYPE and os.path.exists(PROTOTYPE), "prototype workbook not present")
    def test_a_clean_dry_run_exits_0(self):
        make_cheque_csv(self.inbox)
        proc = _run(*self._args("--model", PROTOTYPE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = self.assertJson(proc)
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])


class TestOkIsDerived(_RunCase):
    def test_ok_comes_from_a_boolean_not_a_status_string(self):
        """A sibling pipeline derived ok from status.startswith('error'), so rewording a
        status silently restored the bug where every file could fail and the
        run still exited 0."""
        with open(RUN, encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn('startswith("error")', src)
        self.assertNotIn("startswith('error')", src)
        # Every file entry carries its own boolean.
        self.assertIn('meta["ok"] = False', src)


@unittest.skipUnless(PROTOTYPE and os.path.exists(PROTOTYPE), "prototype workbook not present")
class TestReportsDirectory(_RunCase):
    def test_reports_are_written_to_a_dated_subdirectory(self):
        make_cheque_csv(self.inbox)
        proc = _run(*self._args("--model", PROTOTYPE))
        out = self.assertJson(proc)
        written = out["reports"]
        self.assertNotEqual(
            os.path.normpath(written),
            os.path.normpath(self.reports),
            "the script wrote to the reports root — that is how a 50-day-old "
            "file ends up shadowing today's run",
        )
        self.assertRegex(os.path.basename(written), r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(os.path.isdir(written))

    def test_a_latest_pointer_is_repointed_each_run(self):
        make_cheque_csv(self.inbox)
        out = self.assertJson(_run(*self._args("--model", PROTOTYPE)))
        latest = os.path.join(self.reports, "latest")
        self.assertTrue(os.path.exists(latest))
        self.assertEqual(os.path.realpath(latest), os.path.realpath(out["reports"]))

    def test_the_reports_carry_a_provenance_header(self):
        make_cheque_csv(self.inbox)
        out = self.assertJson(_run(*self._args("--model", PROTOTYPE)))
        body = open(os.path.join(out["reports"], "01-cfo.md"), encoding="utf-8").read()
        self.assertIn("Generated", body)
        self.assertIn("covered", body)


@unittest.skipUnless(PROTOTYPE and os.path.exists(PROTOTYPE), "prototype workbook not present")
class TestDryRunIsSafe(_RunCase):
    def test_dry_run_is_the_default_and_never_touches_the_workbook(self):
        import hashlib

        copy = os.path.join(self.tmp, "model.xlsx")
        shutil.copy2(PROTOTYPE, copy)
        before = hashlib.sha256(open(copy, "rb").read()).hexdigest()

        make_cheque_csv(self.inbox)
        out = self.assertJson(_run(*self._args("--model", copy)))
        self.assertTrue(out["dry_run"], "--dry-run must be the default posture")

        after = hashlib.sha256(open(copy, "rb").read()).hexdigest()
        self.assertEqual(before, after, "a dry run modified the workbook")

    def test_the_preview_shows_what_would_be_written(self):
        make_cheque_csv(self.inbox)
        out = self.assertJson(_run(*self._args("--model", PROTOTYPE)))
        preview = os.path.join(out["reports"], "import-preview.tsv")
        self.assertTrue(os.path.exists(preview))
        lines = [ln for ln in open(preview, encoding="utf-8").read().split("\n") if ln]
        self.assertEqual(len(lines) - 1, out["rows"]["new"], "preview must match")


class TestTheThreeLenses(_RunCase):
    def test_every_run_writes_the_cfo_analyst_and_advisor_reports(self):
        """H1, 2026-09-27: the analyst lens was tested but never called, so
        02-analyst.md was never written. It compares the window with the one
        of the same length before it."""
        make_cheque_csv(self.inbox)
        proc = _run(*self._args("--model", PROTOTYPE))
        out = self.assertJson(proc)
        for name in ("01-cfo.md", "02-analyst.md", "03-advisor.md"):
            self.assertTrue(os.path.exists(os.path.join(out["reports"], name)), name)
        with open(os.path.join(out["reports"], "02-analyst.md"), encoding="utf-8") as fh:
            self.assertIn("# Analyst — what changed", fh.read())


class TestCardIsNotWrittenToTransactions(_RunCase):
    def test_card_rows_are_reported_but_never_queued_for_transactions(self):
        """The card is a second account on its own sheet. model.load_existing
        reads TRANSACTIONS only, so treating card rows as new would re-import
        every one of them on every single run."""
        with open(RUN, encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn("account_kind", src)
        self.assertIn("cheque", src)


if __name__ == "__main__":
    unittest.main()


class DryRunIsMarkedInTheLedger(_RunCase):
    """2026-09-22: the ledger records every run by design (it is the run log and
    the gap register). Before today a dry run's import_run row was
    indistinguishable from a real import -- rows_new held what WOULD have been
    appended and nothing said it was not. Now flags_json carries "dry-run"."""

    def test_dry_run_row_is_flagged(self):
        import json
        import sqlite3

        make_cheque_csv(self.inbox)
        proc = _run(*self._args("--model", PROTOTYPE))  # no --write => dry run
        self.assertJson(proc)
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT flags_json FROM import_run").fetchall()
        conn.close()
        self.assertTrue(rows, "a dry run must still be recorded in import_run")
        for (flags,) in rows:
            self.assertIn("dry-run", json.loads(flags or "[]"), f"row not flagged: {flags!r}")
