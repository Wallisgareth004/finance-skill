"""Parser tests.

THE CENTRAL HAZARD: FNB does no quoting at all, and a handful of rows in every
real export contain unquoted commas — SA decimal separators inside interest-rate
rows (`CR.INT.RATE   3,25000`) and merchant names (`SEASIDE TRADING,LDA`;
both synthetic here).
csv.DictReader pushes the overflow into restkey and SILENTLY TRUNCATES the
description. These tests exist so nobody "simplifies" parse.py back to csv.
"""

import shutil
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from helpers import make_card_csv, make_cheque_csv, pipeline_dir

pipeline_dir()
import detect  # noqa: E402
import parse  # noqa: E402


class TestCommaHazard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_interest_rate_row_not_truncated(self):
        p = make_cheque_csv(
            self.tmp,
            rows=[("2026/03/07", "0.00", "5120.35", "CR.INT.RATE   3,25000")],
        )
        txns = parse.parse_file(detect.sniff(p))
        self.assertEqual(len(txns), 1)
        self.assertIn("3,25000", txns[0].description)

    def test_merchant_with_comma_not_truncated(self):
        p = make_card_csv(
            self.tmp,
            rows=[
                (
                    "2026/03/09",
                    "-583.20",
                    "0",
                    "SEASIDE TRADING,LDA     Port Harbour    ",
                )
            ],
        )
        txns = parse.parse_file(detect.sniff(p))
        self.assertIn("TRADING,LDA", txns[0].description)


class TestTypesAndFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_amounts_are_decimal_not_float(self):
        t = parse.parse_file(detect.sniff(make_cheque_csv(self.tmp)))[0]
        self.assertIsInstance(t.amount, Decimal)
        self.assertEqual(t.amount, Decimal("-54.21"))

    def test_dates_parsed(self):
        t = parse.parse_file(detect.sniff(make_cheque_csv(self.tmp)))[0]
        self.assertEqual(t.date, date(2026, 3, 12))

    def test_card_balance_discarded(self):
        for t in parse.parse_file(detect.sniff(make_card_csv(self.tmp))):
            self.assertIsNone(
                t.balance, "card Balance column is a literal 0 placeholder, not data"
            )

    def test_cheque_balance_kept(self):
        t = parse.parse_file(detect.sniff(make_cheque_csv(self.tmp)))[0]
        self.assertEqual(t.balance, Decimal("2215.40"))

    def test_zero_amount_rows_kept_but_tagged_non_transactional(self):
        p = make_cheque_csv(
            self.tmp,
            rows=[("2026/03/07", "0.00", "5120.35", "CR.INT.RATE   3,25000")],
        )
        t = parse.parse_file(detect.sniff(p))[0]
        self.assertFalse(t.is_transaction)

    def test_row_order_preserved(self):
        """Format A is newest-first and dates are NOT monotonic. The balance
        chain defines the sequence, so order must survive parsing untouched."""
        p = make_cheque_csv(
            self.tmp,
            rows=[
                ("2026/03/12", "-54.21", "2215.40", "A"),
                ("2026/03/08", "-61.90", "2269.61", "B"),
                ("2026/03/12", "-3.00", "2331.51", "C"),
            ],
        )
        txns = parse.parse_file(detect.sniff(p))
        self.assertEqual([t.description for t in txns], ["A", "B", "C"])


class TestMerchantExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merchant_sliced_from_fixed_width(self):
        p = make_cheque_csv(
            self.tmp,
            rows=[
                (
                    "2026/03/12",
                    "-61.90",
                    "2269.61",
                    "WOOLWORTHS          400000*0001  11 MAR",
                )
            ],
        )
        self.assertEqual(parse.parse_file(detect.sniff(p))[0].merchant, "WOOLWORTHS")

    def test_forex_prefix_stripped_from_merchant(self):
        """Foreign-currency purchases prefix the original amount INTO the
        20-char merchant field, truncating the name."""
        p = make_cheque_csv(
            self.tmp,
            rows=[
                (
                    "2026/03/12",
                    "-54.21",
                    "2215.40",
                    "210.00 HARBOUR PHAR 400000*0001  09 MAR",
                )
            ],
        )
        self.assertTrue(
            parse.parse_file(detect.sniff(p))[0].merchant.startswith("HARBOUR")
        )

    def test_card_merchant_is_first_24_chars(self):
        p = make_card_csv(
            self.tmp,
            rows=[
                (
                    "2026/03/10",
                    "-138.00",
                    "0",
                    "iK *Harbour Grill       DURBAN          ",
                )
            ],
        )
        self.assertEqual(
            parse.parse_file(detect.sniff(p))[0].merchant, "iK *Harbour Grill"
        )

    def test_source_slug_matches_model_convention(self):
        """Model convention: export-YYYY-MM-DD_to_MM-DD — year on the START only."""
        txns = parse.parse_file(detect.sniff(make_cheque_csv(self.tmp)))
        self.assertRegex(
            txns[0].source_slug, r"^export-\d{4}-\d{2}-\d{2}_to_\d{2}-\d{2}$"
        )


if __name__ == "__main__":
    unittest.main()
