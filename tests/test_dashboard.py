#!/usr/bin/env python3
"""test_dashboard.py — F5: a four-page dashboard PDF, built from the run's own numbers.

F5 (2026-09-27). The owner's first dashboard was a script with every number
typed in by hand. This one is drawn from the CFO result, the month table and
the caps — nothing typed, category level only (no merchant ever reaches it):

  1. spend vs income, month by month (posted, card and pending apart)
  2. this month against a normal month, by category
  3. each cap: a normal month, this month, the cap
  4. portfolio — a placeholder until holdings are wired in

It is written beside the other reports, never into the repository. matplotlib
is needed only for this; without it the run says so. Synthetic data only.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import dashboard  # noqa: E402

HAS_MPL = importlib.util.find_spec("matplotlib") is not None
HAS_PYPDF = importlib.util.find_spec("pypdf") is not None


def _month(m, complete, cheque, card, pending, cats):
    return {"month": f"2026-{m:02d}", "complete": complete, "from": date(2026, m, 1),
            "to": date(2026, m, 28 if complete else 20), "income": Decimal("30000.00"),
            "spend_cheque": Decimal(cheque), "spend_card": Decimal(card),
            "spend": Decimal(cheque) + Decimal(card), "pending": Decimal(pending),
            "deficit": Decimal("-1000.00") if complete else None, "deficit_cheque": None,
            "spend_by_category": {k: Decimal(v) for k, v in cats.items()},
            "covered_days": 28, "elapsed_days": 28, "isolated": Decimal("0.00")}


MONTHS = [
    _month(2, True, "24000", "1000", "0", {"Groceries": "9000", "Restaurants": "4000"}),
    _month(3, True, "27000", "2000", "0", {"Groceries": "11000", "Restaurants": "5000"}),
    _month(4, False, "30000", "500", "2100", {"Groceries": "7000", "Restaurants": "6000"}),
]
CAPS = [{"month": m["month"], "from": m["from"], "to": m["to"], "complete": m["complete"],
         "lines": [{"name": "Eating out", "cap": Decimal("3000"),
                    "actual": m["spend_by_category"]["Restaurants"],
                    "over": max(m["spend_by_category"]["Restaurants"] - 3000, 0)}]}
        for m in MONTHS]
CFO = {"income": Decimal("30000.00"), "accessible_cash": Decimal("60000.00"),
       "pending": {"total": Decimal("2100.00"), "rows": 3}, "window": "2026-02-01 → 2026-04-20"}


@unittest.skipUnless(HAS_MPL, "matplotlib is not installed")
class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = dashboard.render(os.path.join(self.tmp, "05-dashboard.pdf"), CFO, MONTHS,
                                     CAPS, "Test season", date(2026, 4, 21))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_pdf_is_written(self):
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(5), b"%PDF-")

    @unittest.skipUnless(HAS_PYPDF, "pypdf is not installed")
    def test_four_pages_drawn_from_the_numbers(self):
        import pypdf

        reader = pypdf.PdfReader(self.path)
        self.assertEqual(len(reader.pages), 4)
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        for words in ("Spend vs income", "normal month", "Caps", "Portfolio"):
            self.assertIn(words, text)
        self.assertIn("Restaurants", text)
        self.assertIn("Eating out", text)


@unittest.skipUnless(HAS_MPL, "matplotlib is not installed")
class TestRunDrawsItBesideTheReports(unittest.TestCase):
    def test_dashboard_flag_writes_the_pdf_into_the_dated_reports_folder(self):
        import json
        import subprocess
        import sys

        from helpers import make_cheque_csv, synthetic_workbook

        tmp = tempfile.mkdtemp()
        try:
            inbox = os.path.join(tmp, "in")
            os.makedirs(inbox)
            make_cheque_csv(inbox)
            proc = subprocess.run(
                [sys.executable, os.path.join(pipeline_dir(), "run.py"), "--input", inbox,
                 "--model", synthetic_workbook(), "--reports", os.path.join(tmp, "reports"),
                 "--db", os.path.join(tmp, "l.sqlite"), "--dashboard"],
                capture_output=True, text=True, timeout=180,
            )
            self.assertNotIn("Traceback", proc.stderr)
            out = json.loads(proc.stdout)
            written = out["dashboard"]["written"]
            self.assertEqual(os.path.dirname(written), out["reports"])
            self.assertTrue(os.path.isfile(written))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWithoutMatplotlib(unittest.TestCase):
    def test_a_missing_library_is_named(self):
        import sys

        saved = sys.modules.get("matplotlib")
        sys.modules["matplotlib"] = None
        try:
            with self.assertRaises(dashboard.DashboardUnavailable) as ctx:
                dashboard.render(os.path.join(tempfile.mkdtemp(), "x.pdf"), CFO, MONTHS, CAPS,
                                 None, date(2026, 4, 21))
        finally:
            if saved is not None:
                sys.modules["matplotlib"] = saved
            else:
                sys.modules.pop("matplotlib", None)
        self.assertIn("matplotlib", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
