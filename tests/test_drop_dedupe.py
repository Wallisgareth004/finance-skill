#!/usr/bin/env python3
"""test_drop_dedupe.py — D1: overlapping exports in ONE drop import once.

Measured on a real drop: two cheque exports of the same account, the shorter
one lying entirely inside the longer. Every row of the overlap sat in both
files. Dedupe ran against the workbook only, so both copies were "new" —
`--write` would have added every overlapping row twice. Each file's balance
chain is clean, so the chain cannot catch it.

The fix merges files of the same account BEFORE the workbook dedupe, as a
multiset union keyed on (date, description, amount, balance): a row appears as
many times as the file that holds it MOST often — never the sum. Genuine repeats
inside one file (two R27 tolls in a day) survive, because union takes the max.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest

from helpers import make_cheque_csv, pipeline_dir

pipeline_dir()
import detect  # noqa: E402
import model  # noqa: E402
import parse  # noqa: E402

BIG = [  # newest first, a clean chain
    ("2026/09/22", "-50.00", "850.00", "SHOP C"),
    ("2026/09/21", "-27.00", "900.00", "TOLL PLAZA"),
    ("2026/09/21", "-27.00", "927.00", "TOLL PLAZA"),  # genuine repeat
    ("2026/09/20", "-46.00", "954.00", "SHOP B"),
    ("2026/09/10", "-100.00", "1000.00", "SHOP A"),
]
SMALL = BIG[:4]  # the overlap: 20–22 Sep, including both tolls


class TestDedupeWithinDrop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parse(self, rows, name):
        return parse.parse_file(detect.sniff(make_cheque_csv(self.tmp, rows=rows, name=name)))

    def test_overlapping_files_contribute_each_row_once(self):
        merged, dropped = model.dedupe_drop(
            [self._parse(BIG, "a.csv"), self._parse(SMALL, "b.csv")]
        )
        self.assertEqual(len(merged), 5)
        self.assertEqual(dropped, 4)

    def test_a_genuine_repeat_inside_one_file_survives(self):
        merged, _ = model.dedupe_drop(
            [self._parse(BIG, "a.csv"), self._parse(SMALL, "b.csv")]
        )
        tolls = [t for t in merged if t.description == "TOLL PLAZA"]
        self.assertEqual(len(tolls), 2)

    def test_file_order_does_not_change_the_multiset(self):
        a, _ = model.dedupe_drop([self._parse(BIG, "a.csv"), self._parse(SMALL, "b.csv")])
        b, _ = model.dedupe_drop([self._parse(SMALL, "b.csv"), self._parse(BIG, "a.csv")])
        key = lambda t: (t.date, t.description, t.amount, t.balance)  # noqa: E731
        self.assertEqual(sorted(map(key, a)), sorted(map(key, b)))

    def test_same_triple_different_balance_is_two_rows(self):
        """Balance is part of the drop key: two files can each hold ONE R27 toll
        at different chain positions, and those are two real payments."""
        one = [("2026/09/21", "-27.00", "900.00", "TOLL PLAZA")]
        two = [("2026/09/21", "-27.00", "927.00", "TOLL PLAZA")]
        merged, dropped = model.dedupe_drop([self._parse(one, "a.csv"), self._parse(two, "b.csv")])
        self.assertEqual((len(merged), dropped), (2, 0))



if __name__ == "__main__":
    unittest.main()
