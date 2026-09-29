#!/usr/bin/env python3
"""test_credits_never_net.py — D3, the structural half, on synthetic rows.

An UNCATEGORISED credit must never reduce spend. It is quarantined, reported,
and — past a threshold — it blocks the deficit, because a large credit we cannot
name might be income, capital or a transfer, and each of those changes the answer.

Before 2026-09-24 `_split` treated any credit outside ASSET_CONVERSION as a
refund and netted it inside its category. One large unlabelled one-off credit
in `Uncategorised` turned a real deficit into a printed surplus.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402

WINDOW = analyse.Window(date(2026, 8, 1), date(2026, 8, 31))


class _T:
    def __init__(self, d, desc, amt, category):
        self.date = d
        self.description = desc
        self.amount = Decimal(amt)
        self.category = category
        self.is_transaction = True


def _month(extra=()):
    rows = [
        _T(date(2026, 8, 20), "ANNUITY", "30000.00", "Income — annuity"),
        _T(date(2026, 8, 5), "SHOP A", "-10000.00", "Groceries"),
        _T(date(2026, 8, 6), "SHOP B", "-1000.00", "Uncategorised"),
    ]
    return rows + list(extra)


def _cfo(rows):
    return analyse.cfo(
        txns=rows, window=WINDOW, cash={}, today=date(2026, 9, 1), gaps=[]
    )


class TestUncategorisedCreditsAreQuarantined(unittest.TestCase):
    def test_a_large_uncategorised_credit_does_not_reduce_spend(self):
        out = _cfo(
            _month([_T(date(2026, 8, 9), "MYSTERY", "1000000.00", "Uncategorised")])
        )
        self.assertEqual(out["spend"], Decimal("11000.00"))
        self.assertEqual(out["spend_by_category"]["Uncategorised"], Decimal("1000.00"))
        self.assertEqual(out["quarantined_credits"]["total"], Decimal("1000000.00"))

    def test_a_large_uncategorised_credit_refuses_the_deficit(self):
        out = _cfo(
            _month([_T(date(2026, 8, 9), "MYSTERY", "1000000.00", "Uncategorised")])
        )
        self.assertIsNone(out["deficit"])
        self.assertTrue(
            any("uncategorised credit" in r.lower() for r in out["deficit_blocked_by"]),
            out["deficit_blocked_by"],
        )

    def test_a_trivial_uncategorised_credit_is_reported_but_does_not_block(self):
        out = _cfo(
            _month([_T(date(2026, 8, 26), "CREDIT INTEREST", "0.50", "Uncategorised")])
        )
        self.assertEqual(out["quarantined_credits"]["total"], Decimal("0.50"))
        self.assertEqual(out["spend"], Decimal("11000.00"))
        self.assertEqual(
            out["deficit"],
            _q(Decimal("30000") - Decimal("11000") * Decimal("30.44") / 31),
        )

    def test_an_empty_category_credit_is_quarantined_too(self):
        out = _cfo(_month([_T(date(2026, 8, 9), "BLANK", "900.00", "")]))
        self.assertEqual(out["quarantined_credits"]["total"], Decimal("900.00"))
        self.assertEqual(out["spend"], Decimal("11000.00"))

    def test_a_categorised_refund_still_nets(self):
        """The existing rule survives: a credit in a named spend category is a refund."""
        out = _cfo(
            _month([_T(date(2026, 8, 9), "SHOP A REFUND", "200.00", "Groceries")])
        )
        self.assertEqual(out["spend_by_category"]["Groceries"], Decimal("9800.00"))


class TestLabelsOutsideSpendAndIncome(unittest.TestCase):
    def test_capital_is_neither_income_nor_spend(self):
        out = _cfo(
            _month(
                [_T(date(2026, 8, 9), "LUMP SUM", "3000000.00", "Capital — one-off inflow")]
            )
        )
        self.assertEqual(out["income"], Decimal("30000.00"))
        self.assertEqual(out["spend"], Decimal("11000.00"))
        self.assertEqual(out["capital"]["Capital — one-off inflow"], Decimal("3000000.00"))
        self.assertIsNotNone(out["deficit"])

    def test_card_settlement_is_a_transfer_not_spend_but_it_is_cash_out(self):
        out = _cfo(
            _month(
                [
                    _T(
                        date(2026, 8, 9),
                        "CREDIT CARD PAYMENT",
                        "-3000.00",
                        "Transfer — card settlement",
                    )
                ]
            )
        )
        self.assertEqual(out["spend"], Decimal("11000.00"))
        self.assertEqual(
            out["transfers"]["Transfer — card settlement"], Decimal("3000.00")
        )
        # It left the cheque account, so it is in cash burn.
        self.assertEqual(
            out["cash_burn_per_month"],
            _q(Decimal("14000") * Decimal("30.44") / 31),
        )

    def test_a_reversal_cancels_its_matching_debit(self):
        rows = _month(
            [
                _T(date(2026, 8, 1), "FIBRE DEBIT ORDER", "-899.00", "Connectivity"),
                _T(
                    date(2026, 8, 1),
                    "MAGTAPE UNPAID",
                    "899.00",
                    "Reversal — unpaid debit",
                ),
                _T(date(2026, 8, 8), "FIBRE RE-PAID", "-899.00", "Connectivity"),
            ]
        )
        out = _cfo(rows)
        self.assertEqual(out["spend_by_category"]["Connectivity"], Decimal("899.00"))
        self.assertEqual(len(out["reversed_debits"]), 1)
        self.assertEqual(out["reversed_debits"][0]["category"], "Connectivity")

    def test_an_unmatched_reversal_is_quarantined_not_netted(self):
        rows = _month(
            [
                _T(
                    date(2026, 8, 15),
                    "MAGTAPE UNPAID",
                    "777.00",
                    "Reversal — unpaid debit",
                )
            ]
        )
        out = _cfo(rows)
        self.assertEqual(out["spend"], Decimal("11000.00"))
        self.assertEqual(out["reversed_debits"], [])
        self.assertEqual(out["quarantined_credits"]["total"], Decimal("777.00"))


def _q(v):
    return v.quantize(Decimal("0.01"))


if __name__ == "__main__":
    unittest.main()


class TestTheReportShowsEveryBucket(unittest.TestCase):
    def test_capital_transfers_reversals_and_quarantine_are_rendered(self):
        import emit

        rows = _month(
            [
                _T(date(2026, 8, 9), "LUMP SUM", "3000000.00", "Capital — one-off inflow"),
                _T(date(2026, 8, 9), "CARD", "-3000.00", "Transfer — card settlement"),
                _T(date(2026, 8, 1), "FIBRE", "-899.00", "Connectivity"),
                _T(date(2026, 8, 1), "MAGTAPE UNPAID", "899.00", "Reversal — unpaid debit"),
                _T(date(2026, 8, 26), "CREDIT INTEREST", "0.50", "Uncategorised"),
            ]
        )
        body = emit.cfo_markdown(_cfo(rows))
        self.assertIn("Capital — one-off inflow: R3,000,000.00", body)
        self.assertIn("Transfer — card settlement: R3,000.00", body)
        self.assertIn("Reversed debit orders", body)
        self.assertIn("Quarantined credits", body)
        self.assertIn("R0.50", body)
