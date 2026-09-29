"""Unsettled ("pending") head rows on the cheque export.

WHY THIS FILE EXISTS — 2026-08-23.

FNB puts not-yet-settled card authorisations at the TOP of a cheque export with
`Balance` printed as 0.00, and sometimes a posting date one day in the future.
On 2026-08-23 that halted a clean import with EXIT=3:

    row 0: expected <row 1 balance + head amount>, got 0.00 (a prepaid-airtime row)

The FIRST fix nulled the balance whenever `balance == 0 and date > today`. An
adversarial review destroyed it, and these tests pin all three defects so it can
never come back:

  1. NON-DETERMINISM. The verdict depended on the wall clock — the same bytes
     parsed clean on the 23rd and threw EXIT=3 on the 24th.
  2. MASKED BREAKS. It could null a row ANYWHERE in the file. `check_chain`
     skips any pair touching a None, so an interior null splits the chain into
     segments whose absolute levels are never reconciled. A fabricated level
     jump passed clean in testing.
  3. WRONG CONDITION. Strict `>` missed pending rows dated TODAY — which is the
     common case, not the exotic one.

The shipped fix proves "unsettled" from the chain instead of the clock: null row
0 ONLY when row 1's balance equals the preamble's `Balance:`, which demonstrates
row 1 is the newest settled row and row 0 therefore sits above the ledger.
"""

import shutil
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import detect  # noqa: E402
import parse  # noqa: E402
import verify  # noqa: E402


def write_cheque(directory, closing, available, rows, name="10000000001.csv"):
    """Cheque export with a controllable preamble Balance. Rows are NEWEST FIRST."""
    body = "".join(f"{d}, {a}, {b}, {desc}\n" for d, a, b, desc in rows)
    text = (
        "ACCOUNT TRANSACTION HISTORY\n"
        "\n"
        "Name:, Test, User\n"
        "Account:, 10000000001, [Easy Account]\n"
        f"Balance:, {closing}, {available}\n"
        "\n"
        "Date, Amount, Balance, Description\n"
    ) + body
    path = f"{directory}/{name}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# A valid settled chain: balance[i] == balance[i+1] + amount[i]
SETTLED = [
    ("2026/03/22", "-137.60", "18420.35", "BYC DEBIT             10000000002"),
    ("2026/03/22", "-1215.80", "18557.95", "63.47 GAMESTORE *EU 400000*0001  18 MAR"),
    ("2026/03/22", "-265.00", "19773.75", "HARBOUR GRILL       400000*0001  20 MAR"),
    ("2026/03/20", "30000.00", "20038.75", "ANNUITY PAYOUT 000123"),
]


class TestUnsettledHeadRow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parse(self, head_date):
        rows = [
            (head_date, "-120.00", "0.00", "FNB APP PREPAID       AIRTIME 0000000000")
        ] + SETTLED
        p = write_cheque(self.tmp, "18420.35", "17905.10", rows)
        return parse.parse_file(detect.sniff(p))

    def test_head_row_is_nulled_when_row1_anchors_the_preamble(self):
        """The shape seen on 2026-08-23: row 1 == preamble Balance, so row 0 is unsettled."""
        txns = self._parse("2026/03/24")
        self.assertIsNone(txns[0].balance)
        self.assertEqual(txns[1].balance, Decimal("18420.35"))
        det = detect.sniff(write_cheque(self.tmp, "18420.35", "17905.10", []))
        det.account_kind = "cheque"
        self.assertTrue(verify.check_chain(txns, det).ok)

    def test_parse_is_deterministic_whatever_the_posting_date(self):
        """DEFECT 1. Yesterday, today and tomorrow must all give the same answer.

        The reverted fix returned a different verdict for identical bytes
        depending on when it was run. Nothing in parse.py may read the clock.
        """
        today = date.today()
        results = []
        for d in (today - timedelta(days=1), today, today + timedelta(days=1)):
            txns = self._parse(d.strftime("%Y/%m/%d"))
            results.append([t.balance for t in txns])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertIsNone(results[1][0])

    def test_pending_row_dated_today_does_not_halt(self):
        """DEFECT 3. `> today` missed this — and it is the COMMON case."""
        txns = self._parse(date.today().strftime("%Y/%m/%d"))
        self.assertIsNone(txns[0].balance)

    def test_interior_zero_balance_row_is_never_nulled(self):
        """DEFECT 2. An interior 0.00 row must NOT opt out — it would hide a break.

        Here an unexplained level jump sits under an interior 0.00 row.
        Under the reverted fix this passed clean. It must now be caught.
        """
        rows = [
            ("2026/03/22", "-137.60", "18420.35", "BYC DEBIT             10000000002"),
            ("2026/03/26", "-60.00", "0.00", "SOME PENDING THING"),
            ("2026/03/22", "-265.00", "5000.00", "FABRICATED LEVEL JUMP"),
            ("2026/03/20", "30000.00", "1236.70", "ANNUITY PAYOUT 000123"),
        ]
        p = write_cheque(self.tmp, "18420.35", "17905.10", rows)
        det = detect.sniff(p)
        txns = parse.parse_file(det)
        self.assertEqual(txns[1].balance, Decimal("0.00"))  # NOT nulled
        self.assertFalse(verify.check_chain(txns, det).ok)

    def test_head_zero_not_nulled_when_row1_does_not_anchor(self):
        """A 0.00 head row with no corroboration is a real error, not a pending row."""
        rows = [
            ("2026/03/24", "-120.00", "0.00", "FNB APP PREPAID       AIRTIME 999"),
            ("2026/03/22", "-137.60", "99999.99", "BYC DEBIT             10000000002"),
            ("2026/03/22", "-265.00", "18557.95", "HARBOUR GRILL"),
        ]
        p = write_cheque(self.tmp, "18420.35", "17905.10", rows)
        det = detect.sniff(p)
        txns = parse.parse_file(det)
        self.assertEqual(txns[0].balance, Decimal("0.00"))  # NOT nulled
        self.assertFalse(verify.check_chain(txns, det).ok)

    def test_at_most_one_row_ever_opts_out_of_the_chain(self):
        """run.py caps this at 1 and fails otherwise. Pin it at the parser too."""
        txns = self._parse("2026/03/24")
        self.assertLessEqual(sum(1 for t in txns if t.balance is None), 1)

    def test_the_pending_row_is_still_a_real_transaction(self):
        """Nulling the balance must not drop the row or its amount."""
        txns = self._parse("2026/03/24")
        self.assertEqual(len(txns), 5)
        self.assertEqual(txns[0].amount, Decimal("-120.00"))
        self.assertTrue(txns[0].is_transaction)


class TestAnchorIsWiredUp(unittest.TestCase):
    """check_anchor was DEAD CODE until 2026-08-23 — defined, tested, never called.

    run.py now treats a mismatch as fatal. Without it the top of every chain was
    unverified, which matters more once the head row can opt out.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_anchor_skips_a_nulled_head_and_checks_the_first_settled_row(self):
        rows = [
            (
                "2026/03/24",
                "-120.00",
                "0.00",
                "FNB APP PREPAID       AIRTIME 0000000000",
            )
        ] + SETTLED
        p = write_cheque(self.tmp, "18420.35", "17905.10", rows)
        det = detect.sniff(p)
        self.assertTrue(verify.check_anchor(parse.parse_file(det), det))

    def test_anchor_fails_when_preamble_disagrees_with_newest_settled_row(self):
        p = write_cheque(self.tmp, "11111.11", "17905.10", SETTLED)
        det = detect.sniff(p)
        self.assertFalse(verify.check_anchor(parse.parse_file(det), det))


if __name__ == "__main__":
    unittest.main()
