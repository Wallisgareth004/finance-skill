#!/usr/bin/env python3
"""test_card_purchases.py — D12: credit-card purchases reach the analysis.

Found 2026-09-27. The pipeline analysed the cheque account only. The card is
paid from the cheque account by a settlement debit, which is (correctly) a
transfer between the owner's own accounts, excluded from spend. So the card's
purchases were in neither place: every rand spent on the card vanished from
spend, the deficit and the category totals.

Now card purchases are spend, by category, like any other purchase, and the
settlement stays a transfer. No double count, no omission: a month with card
purchases of R X and a settlement of R X has spend that includes X once. The
card's own "payment received" credit is the settlement's other leg, so the two
legs net to nothing in cash burn.

Card history comes from the workbook's CREDIT CARD sheet and from any card
export in the drop, merged so a purchase present in both counts once — even
when the sheet's copy of the description was truncated at a comma.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from helpers import make_card_csv, make_cheque_csv, make_workbook, pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import model  # noqa: E402

WINDOW = analyse.Window(date(2026, 3, 1), date(2026, 3, 31))
X = Decimal("1000.00")


class _T:
    def __init__(self, d, desc, amt, category, account="cheque"):
        self.date, self.description, self.amount = d, desc, Decimal(amt)
        self.category, self.account, self.is_transaction = category, account, True


def _month(card=True):
    rows = [
        _T(date(2026, 3, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity"),
        _T(date(2026, 3, 5), "SHOP", "-1000.00", "Groceries"),
        _T(date(2026, 3, 25), "CREDIT CARD PAYMENT", "-1000.00", "Transfer — card settlement"),
    ]
    if card:
        rows += [
            _T(date(2026, 3, 8), "iK *HARBOUR GRILL", "-430.00", "Restaurants", "card"),
            _T(date(2026, 3, 9), "SYNTH FUEL", "-570.00", "Fuel", "card"),
            _T(date(2026, 3, 25), "PAYMENT - THANK YOU", "1000.00",
               "Transfer — card settlement", "card"),
        ]
    return rows


def _cfo(rows):
    return analyse.cfo(txns=rows, window=WINDOW, cash={}, today=date(2026, 4, 1), gaps=[])


class TestSpendIncludesCardPurchasesOnce(unittest.TestCase):
    def test_card_purchases_are_spend_by_category(self):
        out = _cfo(_month())
        self.assertEqual(out["spend"], Decimal("1000.00") + X)
        self.assertEqual(out["spend_by_category"]["Restaurants"], Decimal("430.00"))
        self.assertEqual(out["spend_by_category"]["Fuel"], Decimal("570.00"))

    def test_the_settlement_stays_a_transfer_and_its_legs_net(self):
        out = _cfo(_month())
        self.assertNotIn("Transfer — card settlement", out["spend_by_category"])
        self.assertEqual(out["transfers"]["Transfer — card settlement"], Decimal("0.00"))

    def test_cash_burn_counts_the_card_money_once(self):
        out = _cfo(_month())
        expected = analyse._q((Decimal("1000.00") + X) * Decimal("30.44") / 31)
        self.assertEqual(out["cash_burn_per_month"], expected)

    def test_without_card_data_the_settlement_is_still_cash_out(self):
        """No card rows at all: the settlement is the only trace of the card's
        spend, so cash burn keeps it (the deficit cannot, it is a transfer)."""
        out = _cfo(_month(card=False))
        self.assertEqual(out["spend"], Decimal("1000.00"))
        expected = analyse._q((Decimal("1000.00") + X) * Decimal("30.44") / 31)
        self.assertEqual(out["cash_burn_per_month"], expected)

    def test_card_data_present_means_the_settlement_never_doubles_cash_burn(self):
        """The card's payment-received credit can carry another label (a sheet
        filed three of them as Savings transfers), so the settlement's legs do
        not always net. With the purchases counted, the settlement is left out
        of cash burn either way."""
        rows = _month()
        rows[-1].category = "Savings transfers"
        out = _cfo(rows)
        expected = analyse._q((Decimal("1000.00") + X) * Decimal("30.44") / 31)
        self.assertEqual(out["cash_burn_per_month"], expected)

    def test_spend_is_split_by_account(self):
        out = _cfo(_month())
        self.assertEqual(out["spend_by_account"], {"card": X, "cheque": Decimal("1000.00")})


class _Row:
    def __init__(self, d, desc, amt, category=""):
        self.date, self.description, self.amount = d, desc, Decimal(amt)
        self.category = category


class TestCardSheet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_card_sheet_is_read_below_its_header(self):
        path = make_workbook(os.path.join(self.tmp, "m.xlsx"), card_rows=[
            (date(2026, 3, 8), "iK *HARBOUR GRILL", "Restaurants", "-430.00"),
            (date(2026, 3, 9), "SEASIDE TRADING", "Groceries", "-583.20"),
        ])
        rows = model.load_card_sheet(path)
        self.assertEqual([(r.date, r.amount, r.category) for r in rows], [
            (date(2026, 3, 8), Decimal("-430.00"), "Restaurants"),
            (date(2026, 3, 9), Decimal("-583.20"), "Groceries"),
        ], "the totals row, which has no date, is not a purchase")
        self.assertTrue(all(r.account == "card" for r in rows))

    def test_a_workbook_without_a_card_sheet_has_no_card_rows(self):
        path = make_workbook(os.path.join(self.tmp, "m.xlsx"))
        self.assertEqual(model.load_card_sheet(path), [])

    def test_a_purchase_on_the_sheet_and_in_an_export_counts_once(self):
        sheet = [
            _Row(date(2026, 3, 9), "SEASIDE TRADING", "-583.20", "Groceries"),  # truncated at a comma
            _Row(date(2026, 3, 1), "OLD PURCHASE", "-10.00", "Groceries"),
        ]
        export = [
            _Row(date(2026, 3, 9), "SEASIDE TRADING,LDA     Port Harbour", "-583.20"),
            _Row(date(2026, 3, 20), "NEW PURCHASE", "-20.00"),
        ]
        merged = model.merge_card_rows(sheet, export)
        self.assertEqual(len(merged), 3)
        twin = [r for r in merged if r.amount == Decimal("-583.20")]
        self.assertEqual(len(twin), 1)
        self.assertEqual(twin[0].category, "Groceries", "a typed category is kept")

    def test_a_genuine_repeat_is_not_collapsed(self):
        sheet = [_Row(date(2026, 3, 7), "COFFEE CART", "-35.00", "Restaurants")]
        export = [_Row(date(2026, 3, 7), "COFFEE CART", "-35.00"),
                  _Row(date(2026, 3, 7), "COFFEE CART", "-35.00")]
        self.assertEqual(len(model.merge_card_rows(sheet, export)), 2)


class TestRunAnalysesTheCard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_card_export_in_the_drop_reaches_spend_and_is_never_written(self):
        inbox = os.path.join(self.tmp, "in")
        os.makedirs(inbox)
        make_cheque_csv(inbox)
        make_card_csv(inbox)  # two purchases: R138.00 and R583.20 on 9-10 Mar
        book = make_workbook(os.path.join(self.tmp, "m.xlsx"), rows=[
            (date(2026, 3, 1), "SEED SHOP", "Groceries", "-10.00", "2341.51", "seed"),
        ])
        proc = subprocess.run(
            [sys.executable, os.path.join(pipeline_dir(), "run.py"), "--input", inbox,
             "--model", book, "--reports", os.path.join(self.tmp, "r"),
             "--db", os.path.join(self.tmp, "l.sqlite"),
             "--window-start", "2026-03-01", "--window-end", "2026-03-31"],
            capture_output=True, text=True, timeout=180,
        )
        self.assertNotIn("Traceback", proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["card"]["rows"], 2)
        self.assertEqual(Decimal(out["card"]["spend"]), Decimal("721.20"))
        with open(os.path.join(out["reports"], "import-preview.tsv"), encoding="utf-8") as fh:
            self.assertNotIn("HARBOUR GRILL", fh.read(), "card rows are never written")


if __name__ == "__main__":
    unittest.main()
