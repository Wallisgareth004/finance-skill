"""Regression tests: "could not READ the file" must never be reported as
"no recognised FNB exports" — nor as a raw traceback.

WHY THESE EXIST (2026-09-03): a sibling pipeline's detect.py collapsed a
`TimeoutError [Errno 60]` from an iCloud-evicted file into "unrecognized", and
the run then reported "no dated data found" — telling its owner there was no
data when the truth was it could not open the file. This module is the same
author and the same shape, so it was checked. Its defect is a different symptom
of the same cause: `_read_lines` had no guard at all, so an unreadable export
either crashed the whole run with a traceback (killing every OTHER file in the
folder), or — with the folder listing intact but every file evicted — produced
the equally wrong sentence "no recognised FNB exports in <folder>".

With macOS's `Optimize Mac Storage` on, most of a synced folder can be stubs, so
this is not hypothetical.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import make_cheque_csv, pipeline_dir

RUN = os.path.join(pipeline_dir(), "run.py")
import detect  # noqa: E402
import run as runmod  # noqa: E402


class TestSniffDistinguishesUnreadable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.chmod(os.path.join(self.tmp, name), 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        detect.__dict__.pop("open", None)
        detect.is_dataless = detect._is_dataless_real

    def test_timeout_raises_unreadable_not_unknownformat(self):
        path = make_cheque_csv(self.tmp)

        def boom(*a, **k):
            raise TimeoutError(60, "Operation timed out")

        detect.open = boom
        with self.assertRaises(detect.Unreadable) as ctx:
            detect.sniff(path)
        self.assertIn("timed out", str(ctx.exception).lower())

    def test_unreadable_is_not_an_unknownformat(self):
        """They must be catchable separately, or the distinction is cosmetic."""
        self.assertFalse(issubclass(detect.Unreadable, detect.UnknownFormat))

    def test_permission_error_raises_unreadable(self):
        path = make_cheque_csv(self.tmp)
        os.chmod(path, 0o000)
        with self.assertRaises(detect.Unreadable):
            detect.sniff(path)

    def test_dataless_file_is_not_opened(self):
        path = make_cheque_csv(self.tmp)
        detect.is_dataless = lambda p: True

        def never(*a, **k):
            raise AssertionError("opened a dataless file instead of reporting it")

        detect.open = never
        with self.assertRaises(detect.Unreadable) as ctx:
            detect.sniff(path)
        self.assertIn("evicted", str(ctx.exception).lower())

    def test_uf_dataless_constant(self):
        self.assertEqual(detect.UF_DATALESS, 0x40000000)

    def test_readable_junk_csv_still_unknown_format(self):
        path = os.path.join(self.tmp, "junk.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("alpha,beta,gamma\n1,2,3\n")
        with self.assertRaises(detect.UnknownFormat):
            detect.sniff(path)

    def test_good_file_still_sniffs(self):
        self.assertEqual(detect.sniff(make_cheque_csv(self.tmp)).format, detect.FORMAT_A)


class TestCollectSeparatesUnreadable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.chmod(os.path.join(self.tmp, name), 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_unreadable_file_does_not_kill_the_other_files(self):
        good = make_cheque_csv(self.tmp, name="good.csv")
        bad = make_cheque_csv(self.tmp, name="bad.csv")
        os.chmod(bad, 0o000)
        entries, unrecognized, unreadable, _ignored = runmod._collect(self.tmp)
        self.assertEqual([e["file"] for e in entries], [os.path.basename(good)])
        self.assertEqual(unrecognized, [])
        self.assertEqual([u["file"] for u in unreadable], ["bad.csv"])


class TestRunExitContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.indir = os.path.join(self.tmp, "in")
        os.makedirs(self.indir)

    def tearDown(self):
        for name in os.listdir(self.indir):
            try:
                os.chmod(os.path.join(self.indir, name), 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return subprocess.run(
            [sys.executable, RUN, "--input", self.indir,
             "--reports", os.path.join(self.tmp, "r"),
             "--db", os.path.join(self.tmp, "l.sqlite")],
            capture_output=True, text=True, timeout=180,
        )

    def test_all_unreadable_exits_4_and_does_not_claim_no_exports(self):
        os.chmod(make_cheque_csv(self.indir), 0o000)
        out = self._run()
        self.assertNotIn("Traceback", out.stderr)
        payload = json.loads(out.stdout[out.stdout.index("{"):])
        err = payload.get("error", "").lower()
        self.assertNotIn(
            "no recognised fnb exports",
            err,
            "reported 'no exports' when it simply could not open them",
        )
        self.assertIn("could not read", err)
        self.assertTrue(payload.get("unreadable_files"))
        self.assertEqual(out.returncode, 4, f"stdout={out.stdout[-400:]}")

    def test_empty_folder_still_exits_2(self):
        out = self._run()
        payload = json.loads(out.stdout[out.stdout.index("{"):])
        self.assertIn("no recognised", payload["error"].lower())
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
