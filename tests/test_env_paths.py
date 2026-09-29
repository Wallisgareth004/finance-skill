#!/usr/bin/env python3
"""test_env_paths.py — no machine layout in code: paths come from the environment.

2026-09-24 fact-check: run.py hardcoded a machine path to the workbook as its
default, and the real-data tests named folders on one machine's disk. Both now read
FINANCE_LIVE_MODEL / FINANCE_REAL_DATA_DIR. Unset, run.py refuses cleanly
(exit 1, JSON, the variable named) and real-data tests skip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import make_cheque_csv, pipeline_dir


def _run_without_model(extra_env=None):
    tmp = tempfile.mkdtemp()
    inbox = os.path.join(tmp, "in")
    os.makedirs(inbox)
    make_cheque_csv(inbox)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FINANCE_")}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, os.path.join(pipeline_dir(), "run.py"), "--input", inbox,
         "--reports", os.path.join(tmp, "r"), "--db", os.path.join(tmp, "l.sqlite")],
        capture_output=True, text=True, timeout=120, env=env,
    )


class TestNoHardcodedModel(unittest.TestCase):
    def test_unset_env_and_no_model_flag_exits_1_naming_the_variable(self):
        proc = _run_without_model()
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        out = json.loads(proc.stdout)
        self.assertFalse(out["ok"])
        self.assertIn("FINANCE_LIVE_MODEL", out["error"])

    def test_the_source_names_no_home_directory(self):
        with open(os.path.join(pipeline_dir(), "run.py"), encoding="utf-8") as fh:
            src = fh.read()
        import re

        # Built from parts so this file does not itself contain a machine path.
        home = "/" + "Users" + r"/\w+"
        tilde = "~" + r"/\w+"
        found = re.findall(home + "|" + tilde, src)
        self.assertEqual(found, [], "machine paths in run.py")


if __name__ == "__main__":
    unittest.main()
