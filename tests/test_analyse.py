#!/usr/bin/env python3
"""test_analyse.py — the CFO, analyst and advisor lenses.

The single most important behaviour in the whole skill is the REFUSAL: no
deficit, no per-month figure, no runway may be stated for a window that a
coverage gap intersects.

This is not theoretical — the same shape was found in the owner's real model.
Illustrated here with synthetic numbers:

    window 2026-02-01 → 2026-05-01, debits R120,000.00
      ÷ 90 elapsed days  →  R40,586.67/mo spend
      ÷ 78 covered days  →  R46,830.77/mo spend

A 12-day hole sits inside that window. Dividing by elapsed calendar time claims
coverage that does not exist and understates the monthly spend by
R6,244.10 — about 13%. A workbook formula of the form
`=(D21-B21+1)/30.44` does exactly this, and so did a sibling pipeline's week
counter. Three instances of one bug.

The defence is structural, not vigilant: per_month() cannot be called without a
coverage object, and coverage() cannot be built without the gap register.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import verify  # noqa: E402

# Synthetic: a 90-day window with a 12-day hole in it.
GAP3 = verify.Gap(date(2026, 3, 10), date(2026, 3, 21))
WINDOW = analyse.Window(date(2026, 2, 1), date(2026, 5, 1))


class _T:
    def __init__(self, d, desc, amt, category, is_transaction=True):
        self.date = d
        self.description = desc
        self.amount = Decimal(amt)
        self.category = category
        self.is_transaction = is_transaction


def _spend(n, amt="-100.00", category="Groceries", day=1):
    return [
        _T(date(2026, 4, min(day + i, 28)), f"MERCHANT {i}", amt, category)
        for i in range(n)
    ]


class TestCoverage(unittest.TestCase):
    def test_elapsed_days_is_inclusive(self):
        cov = analyse.coverage(analyse.Window(date(2026, 8, 1), date(2026, 8, 3)), [])
        self.assertEqual(cov.elapsed_days, 3)
        self.assertEqual(cov.covered_days, 3)
        self.assertFalse(cov.blocked)

    def test_a_gap_inside_the_window_reduces_covered_days(self):
        cov = analyse.coverage(WINDOW, [GAP3])
        self.assertEqual(cov.elapsed_days, 90)
        self.assertEqual(cov.covered_days, 78, "90 elapsed less the 12-day hole")
        self.assertTrue(cov.blocked)
        self.assertEqual(cov.intersecting, [GAP3])

    def test_a_gap_outside_the_window_is_ignored(self):
        old = verify.Gap(date(2025, 6, 1), date(2025, 6, 30))
        cov = analyse.coverage(WINDOW, [old])
        self.assertEqual(cov.covered_days, cov.elapsed_days)
        self.assertFalse(cov.blocked)

    def test_a_gap_overlapping_the_edge_is_clipped_not_double_counted(self):
        """A gap running past the window end must only subtract the days that
        actually fall inside it."""
        edge = verify.Gap(date(2026, 4, 29), date(2026, 5, 9))
        cov = analyse.coverage(WINDOW, [edge])
        self.assertEqual(cov.covered_days, 90 - 3, "29, 30 Apr and 1 May only")

    def test_overlapping_gaps_do_not_subtract_the_same_day_twice(self):
        a = verify.Gap(date(2026, 2, 1), date(2026, 2, 10))
        b = verify.Gap(date(2026, 2, 5), date(2026, 2, 15))
        cov = analyse.coverage(WINDOW, [a, b])
        self.assertEqual(cov.covered_days, 90 - 15, "1–15 Feb is 15 distinct days")

    def test_coverage_pct_is_reported(self):
        cov = analyse.coverage(WINDOW, [GAP3])
        self.assertAlmostEqual(cov.coverage_pct, 78 / 90 * 100, places=6)


class TestPerMonth(unittest.TestCase):
    def test_divides_by_covered_days_not_elapsed(self):
        cov = analyse.coverage(WINDOW, [GAP3])
        got = analyse.per_month(Decimal("120000.00"), cov, allow_blocked=True)
        self.assertEqual(got, Decimal("46830.77"))

    def test_the_elapsed_day_answer_is_the_one_we_refuse_to_give(self):
        """Pinning the wrong number so a regression is unmistakable."""
        wrong = (Decimal("120000.00") / 90 * Decimal("30.44")).quantize(Decimal("0.01"))
        self.assertEqual(wrong, Decimal("40586.67"))
        cov = analyse.coverage(WINDOW, [GAP3])
        self.assertNotEqual(analyse.per_month(Decimal("120000.00"), cov, True), wrong)

    def test_returns_none_when_the_window_is_blocked(self):
        cov = analyse.coverage(WINDOW, [GAP3])
        self.assertIsNone(analyse.per_month(Decimal("120000.00"), cov))

    def test_refuses_a_window_with_no_covered_days(self):
        whole = verify.Gap(date(2026, 2, 1), date(2026, 5, 1))
        cov = analyse.coverage(WINDOW, [whole])
        self.assertEqual(cov.covered_days, 0)
        self.assertIsNone(analyse.per_month(Decimal("1"), cov, allow_blocked=True))


class TestCFO(unittest.TestCase):
    CASH = {"available": Decimal("615.25"), "card_headroom": Decimal("915.00")}

    def _cfo(self, txns, gaps):
        return analyse.cfo(
            txns=txns,
            gaps=gaps,
            window=WINDOW,
            cash=self.CASH,
            today=date(2026, 8, 20),
        )

    def test_deficit_refused_when_window_has_a_gap(self):
        out = self._cfo(_spend(5), [GAP3])
        self.assertIsNone(out["deficit"])
        self.assertIsNone(out["spend_per_month"])
        self.assertTrue(out["deficit_blocked_by"])
        self.assertIn("2026-03-10", str(out["deficit_blocked_by"]))

    def test_the_refusal_names_the_export_to_pull(self):
        out = self._cfo(_spend(5), [GAP3])
        self.assertIn("export", out["remedy"].lower())
        self.assertIn("2026-03-10", out["remedy"])

    ANNUITY = _T(date(2026, 4, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity")

    def test_deficit_computed_when_coverage_is_clean(self):
        out = self._cfo([self.ANNUITY] + _spend(5), [])
        self.assertIsNotNone(out["deficit"])
        self.assertIsNotNone(out["spend_per_month"])
        self.assertEqual(out["deficit_blocked_by"], [])

    def test_deficit_refused_when_no_annuity_credit_falls_in_the_window(self):
        """Distinct from a coverage gap, though on real data the two often
        coincide: a month's income can land inside the hole. A window with spend
        but no income must refuse — reporting income as zero would manufacture
        a swing the size of a month's income out of a missing file."""
        out = self._cfo(_spend(5), [])
        self.assertIsNone(out["income"])
        self.assertIsNone(out["deficit"])
        self.assertIn("Income — annuity", str(out["deficit_blocked_by"]))
        self.assertIn("do not read this as zero income", out["remedy"])

    def test_only_the_annuity_counts_as_income(self):
        """NOTES & GAPS!B9: savings transfers, crypto sales and marketplace cash
        are asset conversions, NOT income. Booking them as income is the exact
        defect the spec was written to fix (§3.3)."""
        txns = [
            _T(date(2026, 4, 20), "ANNUITY PAYOUT 000123", "30000.00", "Income — annuity"),
            _T(
                date(2026, 4, 21),
                "Magtape Credit Savings",
                "47250.00",
                "Income — other",
            ),
            _T(date(2026, 4, 22), "FNB APP TRANSFER", "705.00", "Savings transfers"),
        ]
        out = self._cfo(txns, [])
        self.assertEqual(out["income"], Decimal("30000.00"))

    def test_non_transactional_rows_are_excluded(self):
        """CR.INT.RATE carries a RATE, not a movement."""
        txns = _spend(2) + [
            _T(
                date(2026, 4, 5),
                "CR.INT.RATE   3,25000",
                "0.00",
                "Bank fees & interest",
                False,
            )
        ]
        out = self._cfo(txns, [])
        self.assertEqual(out["rows_counted"], 2)

    def test_savings_sweeps_are_not_counted_as_spend(self):
        """Bank Your Change is the owner's own savings pocket (NOTES & GAPS!B12), and
        Savings transfers is a separate category — never conflate the two."""
        txns = [
            _T(date(2026, 4, 5), "SHOP", "-100.00", "Groceries"),
            _T(date(2026, 4, 6), "BYC", "-200.00", "Savings (Bank Your Change)"),
            _T(date(2026, 4, 7), "TO SAVINGS", "-300.00", "Savings transfers"),
        ]
        out = self._cfo(txns, [])
        self.assertEqual(out["spend"], Decimal("100.00"))

    def test_the_one_off_trip_is_isolated_from_the_run_rate(self):
        txns = [
            _T(date(2026, 4, 5), "SHOP", "-100.00", "Groceries"),
            _T(date(2026, 4, 8), "TRIP", "-3875.40", "Travel — trip"),
        ]
        out = self._cfo(txns, [])
        self.assertEqual(out["spend"], Decimal("100.00"))
        self.assertEqual(out["isolated"]["Travel — trip"], Decimal("3875.40"))

    def test_accessible_cash_is_available_plus_card_headroom(self):
        out = self._cfo(_spend(1), [])
        self.assertEqual(out["accessible_cash"], Decimal("1530.25"))

    def test_runway_carries_its_assumptions_and_never_a_bare_division(self):
        out = self._cfo(_spend(30, amt="-1000.00"), [])
        runway = out["runway"]
        self.assertIn("assumptions", runway)
        for k in ("inflation", "growth", "tax_included", "deficit_variant"):
            self.assertIn(k, runway["assumptions"])

    def test_runway_is_refused_when_the_deficit_is(self):
        out = self._cfo(_spend(5), [GAP3])
        self.assertIsNone(out["runway"]["months"])

    def test_runway_rejects_the_superseded_figure(self):
        """A workbook formula hardcoded a superseded balance. The runway must
        refuse it rather than divide money that will not arrive."""
        with self.assertRaises(analyse.AnalysisError):
            analyse.runway(
                base=Decimal("1234567.89"),
                deficit_per_month=Decimal("-21000.00"),
                variant="cash",
            )

    def test_residual_is_asserted_so_spend_cannot_vanish(self):
        """Prototype B51's idea: whatever is not itemised must be named."""
        txns = [
            _T(date(2026, 4, 5), "A", "-100.00", "Groceries"),
            _T(date(2026, 4, 6), "B", "-50.00", "Cash / ATM"),
        ]
        out = analyse.cfo(
            txns=txns,
            gaps=[],
            window=WINDOW,
            cash=self.CASH,
            today=date(2026, 8, 20),
            itemised=["Groceries"],
        )
        self.assertEqual(out["residual"]["amount"], Decimal("50.00"))
        self.assertEqual(out["residual"]["categories"], ["Cash / ATM"])


class TestAdvisor(unittest.TestCase):
    LEDGER = [
        {
            "name": "Anthropic Max→Pro",
            "monthly": Decimal("1595"),
            "committed_on": date(2026, 8, 2),
            "executed_on": None,
            "executed": False,
            "note": "",
        }
    ]

    def test_savings_ledger_flags_unexecuted_commitment(self):
        out = analyse.advisor(
            subs=[], savings_ledger=self.LEDGER, today=date(2026, 8, 20)
        )
        item = out["ledger"][0]
        self.assertEqual(item["days_late"], 18)
        # SPEC CORRECTION 2026-08-20: the spec's §7.4 worked example says
        # "R950 burned", but its own stated formula gives
        # 1595 / 30.44 * 18 = 943.1669… The spec figure does not reconcile with
        # any reasonable month length (÷30 gives 957). Using the arithmetic.
        self.assertEqual(item["burned"], Decimal("943.17"))

    def test_an_executed_commitment_stops_burning(self):
        led = [dict(self.LEDGER[0], executed_on=date(2026, 8, 5), executed=True)]
        item = analyse.advisor(subs=[], savings_ledger=led, today=date(2026, 8, 20))[
            "ledger"
        ][0]
        self.assertEqual(item["days_late"], 3)
        self.assertEqual(item["burned"], Decimal("157.19"))

    def test_a_commitment_made_today_has_burned_nothing(self):
        led = [dict(self.LEDGER[0], committed_on=date(2026, 8, 20))]
        item = analyse.advisor(subs=[], savings_ledger=led, today=date(2026, 8, 20))[
            "ledger"
        ][0]
        self.assertEqual(item["days_late"], 0)
        self.assertEqual(item["burned"], Decimal("0.00"))

    def test_subscriptions_are_ranked_by_rand_not_percentage(self):
        subs = [
            {"name": "Playlist sync", "monthly": Decimal("88.15"), "decision": "CUT"},
            {"name": "AI assistant", "monthly": Decimal("1725.60"), "decision": "CUT"},
        ]
        out = analyse.advisor(subs=subs, savings_ledger=[], today=date(2026, 8, 20))
        self.assertEqual(
            [s["name"] for s in out["subscriptions"]], ["AI assistant", "Playlist sync"]
        )
        self.assertEqual(out["annual_if_cut"], Decimal("21765.00"))

    def test_bank_your_change_is_never_counted_as_a_saving(self):
        """The SUBSCRIPTIONS sheet total included the Bank Your Change sweep —
        but the workbook's own NOTES row says BYC is not a cost."""
        subs = [
            {
                "name": "Bank Your Change",
                "monthly": Decimal("571.35"),
                "decision": "CUT",
            }
        ]
        out = analyse.advisor(subs=subs, savings_ledger=[], today=date(2026, 8, 20))
        self.assertEqual(out["annual_if_cut"], Decimal("0.00"))

    def test_there_is_exactly_one_recommended_move(self):
        out = analyse.advisor(
            subs=[
                {"name": "AI assistant", "monthly": Decimal("1725.60"), "decision": "CUT"}
            ],
            savings_ledger=self.LEDGER,
            today=date(2026, 8, 20),
        )
        self.assertIsInstance(out["the_one_move"], str)
        self.assertTrue(out["the_one_move"])


class TestAnalyst(unittest.TestCase):
    def test_movements_are_ranked_by_rand_impact(self):
        now = [
            _T(date(2026, 4, 5), "A", "-1000.00", "Groceries"),
            _T(date(2026, 4, 6), "B", "-50.00", "Alcohol"),
        ]
        prev = [
            _T(date(2026, 5, 5), "A", "-400.00", "Groceries"),
            _T(date(2026, 5, 6), "B", "-10.00", "Alcohol"),
        ]
        out = analyse.analyst(txns=now, prev=prev, gaps=[], window=WINDOW)
        self.assertEqual(out["movements"][0]["category"], "Groceries")
        self.assertEqual(out["movements"][0]["delta"], Decimal("600.00"))

    def test_new_and_stopped_merchants_are_reported(self):
        now = [_T(date(2026, 4, 5), "BRAND NEW", "-10.00", "Groceries")]
        prev = [_T(date(2026, 5, 5), "OLD ONE", "-10.00", "Groceries")]
        out = analyse.analyst(txns=now, prev=prev, gaps=[], window=WINDOW)
        self.assertIn("BRAND NEW", out["new_merchants"])
        self.assertIn("OLD ONE", out["stopped_merchants"])

    def test_stopped_is_suppressed_when_a_gap_could_explain_it(self):
        """Every debit order due inside a gap looks cancelled purely because of
        the gap. A 'cancelled' flag that ignores gaps will be wrong."""
        now = []
        prev = [_T(date(2026, 5, 5), "OLD ONE", "-10.00", "Groceries")]
        out = analyse.analyst(txns=now, prev=prev, gaps=[GAP3], window=WINDOW)
        self.assertEqual(out["stopped_merchants"], [])
        self.assertTrue(out["stopped_suppressed_by_gap"])


if __name__ == "__main__":
    unittest.main()
