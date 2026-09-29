#!/usr/bin/env python3
"""test_crypto_is_not_spend.py — D5, synthetic rows.

The skill's own golden rule: crypto is an asset conversion, not consumption and
not income. Until 2026-09-24 only the SELL side obeyed it — a buy is a debit, and
every debit outside NON_SPEND/ISOLATED landed in spend. A real report listed
crypto buys under "Spend by category".

Now both directions go to one flow line: bought, sold, net. Neither is spend,
neither is income. Net outflow (bought − sold, floored at 0) stays in cash burn,
because it did leave the cheque account.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import emit  # noqa: E402

WINDOW = analyse.Window(date(2026, 9, 1), date(2026, 9, 30))
CRYPTO = "Crypto / investments"


class _T:
    def __init__(self, d, desc, amt, category):
        self.date = d
        self.description = desc
        self.amount = Decimal(amt)
        self.category = category
        self.is_transaction = True


def _cfo(extra):
    rows = [
        _T(date(2026, 9, 20), "ANNUITY", "30000.00", "Income — annuity"),
        _T(date(2026, 9, 5), "SHOP", "-10000.00", "Groceries"),
    ] + extra
    return analyse.cfo(txns=rows, window=WINDOW, cash={}, today=date(2026, 10, 1), gaps=[])


class TestCryptoFlow(unittest.TestCase):
    def setUp(self):
        self.out = _cfo(
            [
                _T(date(2026, 9, 11), "EXCHANGE BUY", "-12000.00", CRYPTO),
                _T(date(2026, 9, 11), "EXCHANGE BUY", "-7000.00", CRYPTO),
                _T(date(2026, 9, 2), "EXCHANGE SELL", "2875.60", CRYPTO),
            ]
        )

    def test_buys_are_not_spend(self):
        self.assertNotIn(CRYPTO, self.out["spend_by_category"])
        self.assertEqual(self.out["spend"], Decimal("10000.00"))

    def test_sells_are_not_income(self):
        self.assertEqual(self.out["income"], Decimal("30000.00"))

    def test_the_flow_line_carries_both_directions(self):
        flow = self.out["crypto_flow"]
        self.assertEqual(flow["bought"], Decimal("19000.00"))
        self.assertEqual(flow["sold"], Decimal("2875.60"))
        self.assertEqual(flow["net_out"], Decimal("16124.40"))
        self.assertNotIn(CRYPTO, self.out["asset_conversions"])

    def test_net_outflow_is_in_cash_burn(self):
        expected = (Decimal("26124.40") * Decimal("30.44") / 30).quantize(Decimal("0.01"))
        self.assertEqual(self.out["cash_burn_per_month"], expected)

    def test_net_inflow_never_reduces_cash_burn(self):
        out = _cfo([_T(date(2026, 9, 2), "EXCHANGE SELL", "5000.00", CRYPTO)])
        self.assertEqual(out["crypto_flow"]["net_out"], Decimal("0.00"))
        expected = (Decimal("10000") * Decimal("30.44") / 30).quantize(Decimal("0.01"))
        self.assertEqual(out["cash_burn_per_month"], expected)

    def test_the_report_shows_the_flow_line_not_a_spend_line(self):
        body = emit.cfo_markdown(self.out)
        spend_section = body.split("## Spend by category")[1].split("##")[0]
        self.assertNotIn(CRYPTO, spend_section)
        self.assertIn("Crypto flow", body)
        self.assertIn("bought R19,000.00", body)


if __name__ == "__main__":
    unittest.main()
