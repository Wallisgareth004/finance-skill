#!/usr/bin/env python3
"""test_trips.py — D14: a trip is isolated from the run-rate, and paid-back trips show their net cost.

A one-off trip is not a monthly habit. The model already isolated one trip by
CATEGORY (a Travel label on each merchant). That breaks for a trip whose rows
carry ordinary labels — its fuel, its tolls, its restaurants — or no label at
all. D14 (2026-09-27): a private trips file names each trip by DATE RANGE and
the categories that count as trip spend inside it.

  · isolate: true   the trip's rows leave spend (and the deficit) and are
                    reported apart, exactly like the Travel labels
  · isolate: false  they stay in spend; the trip is still reported
  · reimbursement   a credit matching a pattern, dated near the trip, is paired
                    with it, so the report shows the NET cost of the trip

Every row here is synthetic.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import trips  # noqa: E402

WINDOW = analyse.Window(date(2026, 3, 1), date(2026, 3, 31))


class _T:
    def __init__(self, d, desc, amt, category):
        self.date, self.description, self.amount = d, desc, Decimal(amt)
        self.category, self.is_transaction = category, True


def _rows():
    return [
        _T(date(2026, 3, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity"),
        _T(date(2026, 3, 2), "SHOP", "-800.00", "Groceries"),               # before the trip
        _T(date(2026, 3, 10), "DIVE CENTRE", "-600.00", "Uncategorised"),   # trip
        _T(date(2026, 3, 11), "SYNTH FUEL", "-300.00", "Fuel"),             # trip
        _T(date(2026, 3, 12), "N1 TOLL PLAZA", "-100.00", "Tolls"),         # trip
        _T(date(2026, 3, 12), "STREAMING", "-200.00", "Digital subscriptions"),  # at home
        _T(date(2026, 3, 1), "CLIENT CO PAYMENT", "900.00", "Income — other"),  # paid back
    ]


def _trip(isolate, reimbursed=True):
    return trips.Trip(
        label="Coast", start=date(2026, 3, 10), end=date(2026, 3, 13), isolate=isolate,
        categories=("Uncategorised", "Fuel", "Tolls", "Restaurants"),
        reimbursement_pattern="CLIENT CO" if reimbursed else None,
    )


def _cfo(trip_list):
    return analyse.cfo(txns=_rows(), window=WINDOW, cash={}, today=date(2026, 4, 1),
                       gaps=[], trips=trip_list)


class TestIsolate(unittest.TestCase):
    def test_an_isolated_trip_leaves_spend_and_is_reported_apart(self):
        out = _cfo([_trip(isolate=True)])
        self.assertEqual(out["spend"], Decimal("1000.00"))
        self.assertEqual(out["isolated"]["Trip — Coast"], Decimal("1000.00"))

    def test_only_the_listed_categories_inside_the_dates_are_the_trip(self):
        out = _cfo([_trip(isolate=True)])
        self.assertEqual(out["spend_by_category"]["Digital subscriptions"], Decimal("200.00"))
        self.assertEqual(out["spend_by_category"]["Groceries"], Decimal("800.00"))

    def test_a_trip_not_isolated_stays_in_spend_but_is_still_reported(self):
        out = _cfo([_trip(isolate=False)])
        self.assertEqual(out["spend"], Decimal("2000.00"))
        self.assertNotIn("Trip — Coast", out["isolated"])
        block = out["trips"][0]
        self.assertEqual((block["rows"], block["cost"], block["isolated"]),
                         (3, Decimal("1000.00"), False))


class TestReimbursement(unittest.TestCase):
    def test_a_matching_credit_is_paired_and_the_net_cost_shows(self):
        block = _cfo([_trip(isolate=False)])["trips"][0]
        self.assertEqual(block["reimbursed"], Decimal("900.00"))
        self.assertEqual(block["reimbursed_on"], date(2026, 3, 1))
        self.assertEqual(block["net"], Decimal("-100.00"), "paid back most of it")

    def test_no_pattern_means_no_pairing(self):
        block = _cfo([_trip(isolate=False, reimbursed=False)])["trips"][0]
        self.assertIsNone(block["reimbursed"])
        self.assertEqual(block["net"], Decimal("-1000.00"))

    def test_a_credit_far_from_the_trip_is_not_its_reimbursement(self):
        trip = _trip(isolate=False)
        trip.reimbursement_days = 5  # 1 Mar is 9 days before the trip
        self.assertIsNone(_cfo([trip])["trips"][0]["reimbursed"])


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_trips_file_is_read(self):
        path = os.path.join(self.tmp, "trips.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "trips:\n"
                "  - label: Coast\n    from: 2026-03-10\n    to: 2026-03-13\n    isolate: true\n"
                "    categories: [Fuel, Tolls]\n"
                "    reimbursement: {pattern: CLIENT CO, within_days: 20}\n"
            )
        [t] = trips.load(path)
        self.assertEqual((t.label, t.start, t.end, t.isolate), ("Coast", date(2026, 3, 10),
                                                                 date(2026, 3, 13), True))
        self.assertEqual((t.categories, t.reimbursement_pattern, t.reimbursement_days),
                         (("Fuel", "Tolls"), "CLIENT CO", 20))

    def test_no_file_means_no_trips(self):
        self.assertEqual(trips.load(os.path.join(self.tmp, "absent.yaml")), [])

    def test_a_trip_ending_before_it_starts_is_refused(self):
        path = os.path.join(self.tmp, "trips.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("trips:\n  - {label: Bad, from: 2026-03-13, to: 2026-03-10}\n")
        with self.assertRaises(trips.TripError):
            trips.load(path)


if __name__ == "__main__":
    unittest.main()
