"""Integrity tests — the balance chain and coverage gaps.

The chain is the strongest signal in this system: measured on the author's real
cheque exports, not one break across every consecutive pair of rows. If it ever
breaks, the import must STOP — a silently wrong ledger is worse than no ledger.
"""

import shutil
import tempfile
import unittest
from datetime import date

from helpers import make_card_csv, make_cheque_csv, pipeline_dir

pipeline_dir()
import detect  # noqa: E402
import parse  # noqa: E402
import verify  # noqa: E402


class TestChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _chain(self, rows):
        p = make_cheque_csv(self.tmp, rows=rows)
        det = detect.sniff(p)
        return verify.check_chain(parse.parse_file(det), det)

    def test_good_chain_passes(self):
        res = self._chain(
            [
                ("2026/03/12", "-54.21", "2215.40", "A"),
                ("2026/03/12", "-61.90", "2269.61", "B"),
                ("2026/03/11", "-3.00", "2331.51", "C"),
            ]
        )
        self.assertTrue(res.ok, res.breaks)
        self.assertEqual(res.checked, 2)

    def test_broken_chain_detected(self):
        res = self._chain(
            [
                ("2026/03/12", "-54.21", "2215.40", "A"),
                ("2026/03/12", "-61.90", "9999.99", "B"),
                ("2026/03/11", "-3.00", "2331.51", "C"),
            ]
        )
        self.assertFalse(res.ok)
        self.assertEqual(len(res.breaks), 2)
        self.assertEqual(res.breaks[0].index, 0)

    def test_card_chain_skipped(self):
        det = detect.sniff(make_card_csv(self.tmp))
        res = verify.check_chain(parse.parse_file(det), det)
        self.assertTrue(res.ok)
        self.assertEqual(res.checked, 0, "card has no per-row balance anchor")

    def test_closing_balance_matches_preamble(self):
        p = make_cheque_csv(self.tmp)
        det = detect.sniff(p)
        txns = parse.parse_file(det)
        self.assertTrue(verify.check_anchor(txns, det))


class TestGaps(unittest.TestCase):
    def test_gap_found(self):
        dates = [
            date(2026, 2, 7),
            date(2026, 2, 8),
            date(2026, 2, 22),
            date(2026, 2, 23),
        ]
        gaps = verify.find_gaps(dates, min_days=3)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].start, date(2026, 2, 9))
        self.assertEqual(gaps[0].end, date(2026, 2, 21))
        self.assertEqual(gaps[0].days, 13)

    def test_weekend_is_not_a_gap(self):
        dates = [date(2026, 2, 27), date(2026, 3, 2)]
        self.assertEqual(verify.find_gaps(dates, min_days=3), [])

    def test_no_dates_no_gaps(self):
        self.assertEqual(verify.find_gaps([], min_days=3), [])

    def test_gap_intersects_window(self):
        g = verify.Gap(date(2026, 2, 9), date(2026, 2, 21))
        self.assertTrue(g.intersects(date(2026, 2, 1), date(2026, 3, 10)))
        self.assertFalse(g.intersects(date(2026, 2, 22), date(2026, 3, 10)))


if __name__ == "__main__":
    unittest.main()
