#!/usr/bin/env python3
"""test_subscriptions.py — F3: recurring charges found in the ledger, ranked by rand.

SUBS mode was unwired: run.py handed the advisor an empty list, so "what am I
paying for" had no answer. F3 (2026-09-27) finds them: the same merchant, in
two or more calendar months, at amounts within ±15% of each other. One merchant
can bill several subscriptions (an app store does), so charges are clustered by
amount and each cluster is its own line. Only commitment-like categories are
searched, so a loaf of bread bought twice is not a subscription. Ranked by rand
per month, each with what cutting it saves in a year. Every row is synthetic.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import subs  # noqa: E402


class _T:
    def __init__(self, d, desc, amt, category):
        self.date, self.description, self.amount = d, desc, Decimal(amt)
        self.category, self.is_transaction = category, True


def _monthly(day, desc, amt, cat, months=(7, 8, 9)):
    return [_T(date(2026, m, day), desc, amt, cat) for m in months]


ROWS = (
    _monthly(5, "APPLE.COM/BILL      400000*0001  05 JUL", "-50.00", "Digital subscriptions")
    + _monthly(9, "APPLE.COM/BILL      400000*0001  09 JUL", "-200.00", "Digital subscriptions")
    + _monthly(12, "NETFLIX.COM", "-199.00", "Digital subscriptions")
    + _monthly(2, "FIBRECO DEBIT ORDER", "-899.00", "Connectivity")
    + _monthly(15, "SYNTH AI TOOLS", "-420.00", "Business / side project", months=(8, 9))
    + [_T(date(2026, m, d), f"PREPAID AIRTIME 082000{d:04d}", "-29.00", "Connectivity")
       for m in (7, 8, 9) for d in (3, 18)]
    + [_T(date(2026, 8, 20), "ONE-OFF APP", "-999.00", "Digital subscriptions")]
    + _monthly(6, "WOOLWORTHS", "-88.00", "Groceries")
)


class TestFinder(unittest.TestCase):
    def setUp(self):
        self.found = subs.find(ROWS)
        self.by = {(s["name"], s["amount"]): s for s in self.found}

    def test_each_recurring_charge_is_found_once(self):
        self.assertEqual(sorted(self.by), sorted([
            ("APPLE.COM BILL", Decimal("50.00")),
            ("APPLE.COM BILL", Decimal("200.00")),
            ("NETFLIX.COM", Decimal("199.00")),
            ("FIBRECO DEBIT ORDER", Decimal("899.00")),
            ("SYNTH AI TOOLS", Decimal("420.00")),
            ("PREPAID AIRTIME", Decimal("29.00")),
        ]))

    def test_repeated_top_ups_are_summed_per_month(self):
        airtime = self.by[("PREPAID AIRTIME", Decimal("29.00"))]
        self.assertEqual((airtime["charges"], airtime["months"]), (6, 3))
        self.assertEqual(airtime["monthly"], Decimal("58.00"))

    def test_ranked_by_rand_with_the_annual_saving(self):
        self.assertEqual(self.found[0]["name"], "FIBRECO DEBIT ORDER")
        monthly = [s["monthly"] for s in self.found]
        self.assertEqual(monthly, sorted(monthly, reverse=True))
        for s in self.found:
            self.assertEqual(s["annual_if_cut"], s["monthly"] * 12)

    def test_a_one_off_and_a_grocery_habit_are_not_subscriptions(self):
        names = {s["name"] for s in self.found}
        self.assertNotIn("ONE-OFF APP", names)
        self.assertNotIn("WOOLWORTHS", names)

    def test_a_price_that_moves_more_than_fifteen_percent_is_another_line(self):
        rows = [_T(date(2026, 7, 1), "STREAMCO", "-100.00", "Digital subscriptions"),
                _T(date(2026, 8, 1), "STREAMCO", "-150.00", "Digital subscriptions")]
        self.assertEqual(subs.find(rows), [], "two prices, one month each: nothing recurs")
        rows.append(_T(date(2026, 9, 1), "STREAMCO", "-110.00", "Digital subscriptions"))
        [line] = subs.find(rows)
        self.assertEqual((line["charges"], line["months"]), (2, 2))

    def test_a_reference_glued_to_the_name_does_not_split_a_merchant(self):
        """A debit order's narrative can carry a new reference number every
        month, glued to the name: "FIBRECO123456789 NETCASH"."""
        rows = [_T(date(2026, m, 1), f"FIBRECO{m}0012345{m} NETCASH", "-899.00", "Connectivity")
                for m in (7, 8, 9)]
        [line] = subs.find(rows)
        self.assertEqual((line["name"], line["months"]), ("FIBRECO NETCASH", 3))

    def test_two_truncations_of_one_name_are_one_merchant(self):
        """FNB cuts a foreign-currency merchant shorter, so one store arrives
        as "STREAMCO.COM BIL" one month and "STREAMCO.COM BI" the next."""
        rows = [_T(date(2026, 7, 5), "STREAMCO.COM/BIL", "-60.00", "Digital subscriptions"),
                _T(date(2026, 8, 5), "STREAMCO.COM/BI", "-60.00", "Digital subscriptions")]
        [line] = subs.find(rows)
        self.assertEqual((line["name"], line["months"]), ("STREAMCO.COM BI", 2))

    def test_the_advisor_ranks_and_totals_them(self):
        import analyse

        out = analyse.advisor(subs=self.found, savings_ledger=[], today=date(2026, 9, 27))
        self.assertEqual(out["subscriptions"][0]["name"], "FIBRECO DEBIT ORDER")
        self.assertEqual(out["annual_if_undecided"], sum(s["annual_if_cut"] for s in self.found))


if __name__ == "__main__":
    unittest.main()
