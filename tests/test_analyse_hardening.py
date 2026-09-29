#!/usr/bin/env python3
"""test_analyse_hardening.py — one test per defect found by adversarial review.

An adversarial pass on 2026-08-20 ran 30 mutants against analyse.py and its
suite. Twelve survived, and seventeen defects were confirmed by execution. This
file pins every one of them, so the same holes cannot reopen.

The three that mattered most, because each of them silently produced a WRONG
NUMBER rather than an error:

  · `_in_window` raised TypeError on datetime — and every live sheet date is a
    datetime, so every sheet-derived caller crashed. The sheet is the only
    possible source of a previous period.
  · `analyst()` keyed merchant identity on the raw description. Most FNB
    descriptions embed a card mask and a posting date, so a single month
    produced false "new" and "stopped" merchants by the hundred.
  · Credits sitting in spend categories were discarded rather than decided
    about — on the owner's real window that overstated the monthly burn.

And three refusal bypasses, each of which returned the exact elapsed-day figure
the module exists to refuse: an inverted Gap, a hand-built Coverage, and an
unpassed gap register.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import verify  # noqa: E402

# Synthetic: a 90-day window with a 12-day hole in it.
WINDOW = analyse.Window(date(2026, 2, 1), date(2026, 5, 1))
GAP3 = verify.Gap(date(2026, 3, 10), date(2026, 3, 21))
CASH = {"available": Decimal("615.25"), "card_headroom": Decimal("915.00")}


class _T:
    def __init__(self, d, desc, amt, category, is_transaction=True, merchant=None):
        self.date = d
        self.description = desc
        self.amount = amt if isinstance(amt, Decimal) else Decimal(amt)
        self.category = category
        self.is_transaction = is_transaction
        if merchant is not None:
            self.merchant = merchant


ANNUITY = _T(date(2026, 4, 19), "ANNUITY PAYOUT 000123", "29800.00", "Income — annuity")


def _cfo(txns, gaps, **kw):
    return analyse.cfo(
        txns=txns, gaps=gaps, window=WINDOW, cash=CASH, today=date(2026, 8, 20), **kw
    )


class TestRefusalBypasses(unittest.TestCase):
    """Each of these previously returned the elapsed-day answer."""

    def test_an_inverted_gap_is_refused_not_silently_ignored(self):
        """verify.find_gaps(min_days=0) genuinely emits these. An inverted gap
        marked the window blocked while subtracting range(negative) = 0 days, so
        allow_blocked then handed back the elapsed-day figure."""
        inverted = verify.Gap(date(2026, 3, 21), date(2026, 3, 10))
        with self.assertRaises(analyse.AnalysisError):
            analyse.coverage(WINDOW, [inverted])

    def test_a_hand_built_coverage_cannot_claim_impossible_days(self):
        with self.assertRaises(analyse.AnalysisError):
            analyse.Coverage(WINDOW, elapsed_days=90, covered_days=9999)
        with self.assertRaises(analyse.AnalysisError):
            analyse.Coverage(WINDOW, elapsed_days=90, covered_days=-1)

    def test_an_unconsulted_gap_register_is_refused(self):
        """An empty list means 'checked, none found'. Omitting the argument is a
        different claim, and used to produce a confident deficit at 100%."""
        with self.assertRaises(analyse.AnalysisError):
            analyse.coverage(WINDOW, analyse.UNCHECKED)
        with self.assertRaises(analyse.AnalysisError):
            analyse.cfo(txns=[], window=WINDOW, cash=CASH, today=date(2026, 8, 20))

    def test_a_none_in_the_register_is_refused(self):
        with self.assertRaises(analyse.AnalysisError):
            analyse.coverage(WINDOW, [None])

    def test_gaps_must_be_a_list(self):
        with self.assertRaises(analyse.AnalysisError):
            analyse.coverage(WINDOW, GAP3)


class TestDateHandling(unittest.TestCase):
    def test_datetime_rows_do_not_crash(self):
        """Every live sheet date is a datetime, not a date. This raised
        TypeError on every sheet-derived caller."""
        rows = [
            _T(datetime(2026, 4, 20, 0, 0), "SHOP", "-100.00", "Groceries"),
            _T(datetime(2026, 4, 19, 0, 0), "ANNUITY", "29800.00", "Income — annuity"),
        ]
        out = _cfo(rows, [])
        self.assertEqual(out["rows_counted"], 2)
        self.assertEqual(out["spend"], Decimal("100.00"))

    def test_an_inverted_window_is_refused(self):
        """Previously reported coverage_pct 100.0 on a negative elapsed_days."""
        with self.assertRaises(analyse.AnalysisError):
            analyse.Window(date(2026, 5, 1), date(2026, 2, 1))

    def test_a_non_date_is_refused(self):
        with self.assertRaises(analyse.AnalysisError):
            analyse.Window("2026-02-01", date(2026, 5, 1))


class TestGapClipping(unittest.TestCase):
    def test_a_gap_starting_before_the_window_is_clipped(self):
        """The suite only ever tested a gap overflowing the END, so dropping the
        max() on the start survived mutation."""
        early = verify.Gap(date(2026, 1, 25), date(2026, 2, 3))
        cov = analyse.coverage(WINDOW, [early])
        self.assertEqual(cov.covered_days, 90 - 3, "1, 2 and 3 Feb only")


class TestCreditsInSpendCategories(unittest.TestCase):
    """The owner's real window held positive amounts in spend categories.
    Discarding them overstated the monthly burn."""

    def test_a_refund_nets_against_its_category(self):
        rows = [
            ANNUITY,
            _T(date(2026, 4, 5), "SHOP", "-500.00", "Groceries"),
            _T(date(2026, 4, 6), "SHOP REFUND", "200.00", "Groceries"),
        ]
        out = _cfo(rows, [])
        self.assertEqual(out["spend"], Decimal("300.00"))
        self.assertEqual(out["credits_netted"]["Groceries"], Decimal("200.00"))

    def test_an_asset_conversion_is_never_netted(self):
        """A LUNO sale is not a refund. Netting it would understate the burn."""
        rows = [
            ANNUITY,
            _T(date(2026, 4, 5), "SHOP", "-500.00", "Groceries"),
            _T(date(2026, 4, 6), "LUNO SELL", "12121.12", "Crypto / investments"),
        ]
        out = _cfo(rows, [])
        self.assertEqual(out["spend"], Decimal("500.00"))
        # 2026-09-24 (D5): crypto has its own flow line, both directions.
        self.assertEqual(out["crypto_flow"]["sold"], Decimal("12121.12"))
        self.assertNotIn("Crypto / investments", out["credits_netted"])

    def test_a_refunds_category_row_cannot_vanish(self):
        """categorise.VOCABULARY carries "Refunds" and analyse mentioned it
        nowhere — positive, so skipped by spend; not income, so skipped there
        too. It became invisible."""
        rows = [ANNUITY, _T(date(2026, 4, 6), "REFUND", "150.00", "Refunds")]
        out = _cfo(rows, [])
        self.assertEqual(out["credits_netted"]["Refunds"], Decimal("150.00"))


class TestIncomeSelection(unittest.TestCase):
    def test_two_credits_on_the_same_day_are_summed_not_picked_by_list_order(self):
        """max(by date) let list order decide: a small adjustment beside the
        annuity credit flipped the verdict from surplus to depleting."""
        rows = [
            _T(date(2026, 4, 19), "ADJUSTMENT", "125.00", "Income — annuity"),
            _T(date(2026, 4, 19), "ANNUITY PAYOUT 000123", "29800.00", "Income — annuity"),
            _T(date(2026, 4, 5), "SHOP", "-100.00", "Groceries"),
        ]
        forward = _cfo(rows, [])["income"]
        backward = _cfo(list(reversed(rows)), [])["income"]
        self.assertEqual(forward, Decimal("29925.00"))
        self.assertEqual(forward, backward, "list order must not change income")

    def test_an_implausibly_small_latest_credit_is_refused(self):
        rows = [
            _T(date(2026, 2, 20), "ANNUITY", "29800.00", "Income — annuity"),
            _T(date(2026, 3, 19), "ANNUITY", "29800.00", "Income — annuity"),
            _T(date(2026, 4, 19), "ANNUITY", "29800.00", "Income — annuity"),
            _T(date(2026, 5, 1), "ADJUSTMENT", "125.00", "Income — annuity"),
            _T(date(2026, 4, 5), "SHOP", "-100.00", "Groceries"),
        ]
        out = _cfo(rows, [])
        self.assertIsNone(out["income"])
        self.assertIn("implausible", out["income_note"])
        self.assertIsNone(out["deficit"])


class TestDeficitArithmetic(unittest.TestCase):
    """The deficit's arithmetic was asserted nowhere — a sign flip survived."""

    def test_deficit_is_income_minus_spend_per_month(self):
        rows = [ANNUITY] + [
            _T(date(2026, 4, 5), f"SHOP {i}", "-1000.00", "Groceries")
            for i in range(100)
        ]
        out = _cfo(rows, [])
        spend_pm = out["spend_per_month"]
        self.assertEqual(spend_pm, analyse._q(Decimal("100000") * Decimal("30.44") / 90))
        self.assertEqual(out["deficit"], analyse._q(Decimal("29800.00") - spend_pm))
        self.assertLess(out["deficit"], 0, "spend exceeds income here")

    def test_runway_depleting_branch_is_reachable_through_cfo(self):
        """Every runway test used a fixture with no annuity, so deficit was None
        and the 'depleting' branch was never reached through cfo."""
        rows = [ANNUITY] + [
            _T(date(2026, 4, 5), f"SHOP {i}", "-5000.00", "Groceries")
            for i in range(30)
        ]
        out = _cfo(rows, [])
        self.assertEqual(out["runway"]["verdict"], "depleting")
        self.assertIsNotNone(out["runway"]["months"])
        self.assertGreater(out["runway"]["months"], 0)

    def test_rounding_is_half_up_not_half_even(self):
        """Halfway cases are common on a real sheet, and the modes differ there."""
        self.assertEqual(analyse._q(Decimal("0.005")), Decimal("0.01"))
        self.assertEqual(analyse._q(Decimal("0.015")), Decimal("0.02"))


class TestCashBurnIncludesSweeps(unittest.TestCase):
    def test_savings_sweeps_leave_the_deficit_but_enter_the_cash_runway(self):
        """Real savings sweeps left the account while the module reported a
        surplus and no runway, because sweeps are excluded from the run-rate."""
        rows = [
            ANNUITY,
            _T(date(2026, 4, 5), "SHOP", "-5000.00", "Groceries"),
            _T(date(2026, 4, 6), "TO SAVINGS", "-20000.00", "Savings transfers"),
        ]
        out = _cfo(rows, [])
        self.assertEqual(out["spend"], Decimal("5000.00"))
        self.assertEqual(out["non_spend"]["Savings transfers"], Decimal("20000.00"))
        self.assertGreater(out["cash_burn_per_month"], out["spend_per_month"])
        self.assertLess(out["cash_deficit"], out["deficit"])


class TestPartialAndMissingInputs(unittest.TestCase):
    def test_spend_is_labelled_partial_when_the_window_is_blocked(self):
        """spend was printed as a confident total beside spend_per_month: None."""
        out = _cfo([ANNUITY], [GAP3])
        self.assertTrue(out["spend_is_partial"])

    def test_missing_cash_keys_give_unknown_not_zero(self):
        """cash={} produced accessible R0.00 and a confident 0.00-month runway."""
        out = analyse.cfo(
            txns=[ANNUITY], gaps=[], window=WINDOW, cash={}, today=date(2026, 8, 20)
        )
        self.assertIsNone(out["accessible_cash"])
        self.assertEqual(out["runway"]["verdict"], "unknown-base")
        self.assertIsNone(out["runway"]["months"])

    def test_non_finite_amounts_are_refused(self):
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.assertRaises(analyse.AnalysisError):
                _cfo([_T(date(2026, 4, 5), "X", bad, "Groceries")], [])


class TestSupersededBase(unittest.TestCase):
    def test_every_spelling_of_the_superseded_figure_is_refused(self):
        for base in (
            Decimal("1234567.89"),
            Decimal("1234567.9"),
            Decimal("1234567.90"),
            1234567.89,
            "1234567.89",
        ):
            with self.assertRaises(analyse.AnalysisError, msg=f"accepted {base!r}"):
                analyse.runway(
                    base=base, deficit_per_month=Decimal("-21000.00"), variant="cash"
                )

    def test_the_confirmed_base_is_accepted(self):
        out = analyse.runway(
            base=analyse.CONFIRMED_BASE,
            deficit_per_month=Decimal("-21000.00"),
            variant="cash",
        )
        self.assertEqual(out["verdict"], "depleting")
        self.assertEqual(analyse.CONFIRMED_BASE, Decimal("1450000.00"))


class TestAnalystIdentity(unittest.TestCase):
    def test_merchant_not_description_decides_identity(self):
        """Most FNB descriptions embed a card mask and posting date, so keying
        on description produced false 'new' merchants by the hundred in a month."""
        now = [
            _T(
                date(2026, 4, 5),
                "WOOLWORTHS          400000*0001  05 APR",
                "-100.00",
                "Groceries",
                merchant="WOOLWORTHS",
            )
        ]
        prev = [
            _T(
                date(2026, 1, 5),
                "WOOLWORTHS          400000*0001  05 JAN",
                "-100.00",
                "Groceries",
                merchant="WOOLWORTHS",
            )
        ]
        out = analyse.analyst(
            txns=now, prev=prev, window=WINDOW, prev_window=None, gaps=[]
        )
        self.assertEqual(out["new_merchants"], [])
        self.assertEqual(out["stopped_merchants"], [])

    def test_rows_outside_the_window_are_excluded(self):
        """analyst accepted a window and never filtered on it."""
        now = [
            _T(
                date(2020, 1, 1),
                "ANCIENT",
                "-99999.00",
                "Groceries",
                merchant="ANCIENT",
            )
        ]
        out = analyse.analyst(txns=now, prev=[], window=WINDOW, gaps=[])
        self.assertEqual(out["movements"], [])
        self.assertEqual(out["new_merchants"], [])


class TestAdvisorHardening(unittest.TestCase):
    def _row(self, **kw):
        base = {
            "name": "Anthropic Max→Pro",
            "monthly": Decimal("1595"),
            "committed_on": date(2026, 8, 2),
            "executed_on": None,
            "executed": False,
        }
        base.update(kw)
        return base

    def test_executed_before_committed_is_refused(self):
        """A data-entry inversion reported days_late 0 — perfect compliance."""
        with self.assertRaises(analyse.AnalysisError):
            analyse.advisor(
                subs=[],
                savings_ledger=[
                    self._row(
                        committed_on=date(2026, 8, 20),
                        executed_on=date(2026, 8, 2),
                        executed=True,
                    )
                ],
                today=date(2026, 8, 20),
            )

    def test_a_future_commitment_burns_nothing(self):
        item = analyse.advisor(
            subs=[],
            savings_ledger=[self._row(committed_on=date(2026, 9, 1))],
            today=date(2026, 8, 20),
        )["ledger"][0]
        self.assertEqual(item["days_late"], 0)
        self.assertEqual(item["burned"], Decimal("0.00"))

    def test_an_executed_on_without_the_flag_does_not_stop_the_clock(self):
        """The two fields disagreed and the module trusted neither consistently."""
        item = analyse.advisor(
            subs=[],
            savings_ledger=[self._row(executed_on=date(2026, 8, 5), executed=False)],
            today=date(2026, 8, 20),
        )["ledger"][0]
        self.assertFalse(item["executed"])
        self.assertEqual(item["days_late"], 18)

    def test_decisions_other_than_the_literal_CUT_are_scored(self):
        """Exact-matching "CUT" collapsed the owner's decided annual saving to a
        fraction of its real size — dropping the two largest lines, one marked
        "CALL …" and one marked "PAUSE"."""
        subs = [
            {"name": "Sales tool", "monthly": Decimal("912.40"), "decision": "PAUSE"},
            {
                "name": "Playlist sync",
                "monthly": Decimal("88"),
                "decision": "CUT or go annual",
            },
            {"name": "Garden service", "monthly": Decimal("965"), "decision": "CUT"},
        ]
        out = analyse.advisor(subs=subs, savings_ledger=[], today=date(2026, 8, 20))
        self.assertEqual(out["annual_if_cut"], Decimal("23584.80"))

    def test_an_ambiguous_decision_is_surfaced_not_dropped(self):
        subs = [
            {
                "name": "Insurance policy",
                "monthly": Decimal("2875.40"),
                "decision": "CALL the broker",
            }
        ]
        out = analyse.advisor(subs=subs, savings_ledger=[], today=date(2026, 8, 20))
        self.assertEqual(out["annual_if_cut"], Decimal("0.00"))
        self.assertEqual(out["annual_if_undecided"], Decimal("34504.80"))
        self.assertEqual(out["undecided"][0]["name"], "Insurance policy")

    def test_locked_keep_is_a_keep(self):
        subs = [
            {
                "name": "Virgin Active",
                "monthly": Decimal("1475"),
                "decision": "LOCKED KEEP",
            }
        ]
        out = analyse.advisor(subs=subs, savings_ledger=[], today=date(2026, 8, 20))
        self.assertEqual(out["annual_if_cut"], Decimal("0.00"))
        self.assertEqual(out["annual_if_undecided"], Decimal("0.00"))

    def test_subscriptions_sort_by_rand_not_by_name(self):
        """The old fixture's alphabetical order coincided with descending rand,
        so a sort-by-name mutant survived."""
        subs = [
            {"name": "Zebra", "monthly": Decimal("5000"), "decision": "KEEP"},
            {"name": "Apple", "monthly": Decimal("10"), "decision": "KEEP"},
        ]
        out = analyse.advisor(subs=subs, savings_ledger=[], today=date(2026, 8, 20))
        self.assertEqual([s["name"] for s in out["subscriptions"]], ["Zebra", "Apple"])

    def test_the_one_move_names_the_actual_worst_offender(self):
        """The old assertion was a tautology — isinstance(str) and truthy."""
        out = analyse.advisor(
            subs=[],
            savings_ledger=[
                self._row(name="Small", monthly=Decimal("10")),
                self._row(name="Large", monthly=Decimal("5000")),
            ],
            today=date(2026, 8, 20),
        )
        self.assertIn("Large", out["the_one_move"])
        self.assertNotIn("Small", out["the_one_move"])


if __name__ == "__main__":
    unittest.main()
