#!/usr/bin/env python3
"""test_emit.py — rendering. Structured results in, text out.

Task 7 in the plan was a paste-ready TSV emitter, on the assumption the owner would
paste rows into Excel by hand. Spec §8b superseded that: writer.py appends to the
workbook directly. So this module does the two jobs that survived.

  1. PREVIEW — render exactly what writer.py would append, BEFORE it is written.
     This is the dry-run gate. It must mirror writer's column contract precisely,
     or the preview is worse than none.

  2. REPORTS — the markdown the CFO/analyst/advisor lenses are read as, and the
     NOTES & GAPS text, EMITTED from the gap register rather than parsed back
     out of it. The real workbook's NOTES & GAPS sheet once held two coverage
     lines that contradicted each other, the stale one above the fresh one.
     That is what happens when prose is the register.

A blocked deficit must be unmissable in the rendered output. A refusal that
reads like a rounding note is a refusal nobody acts on.
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import analyse  # noqa: E402
import emit  # noqa: E402
import verify  # noqa: E402

# Synthetic: a 90-day window with a 12-day hole in it.
GAP3 = verify.Gap(date(2026, 3, 10), date(2026, 3, 21))
WINDOW = analyse.Window(date(2026, 2, 1), date(2026, 5, 1))


class _Row:
    """Exactly the six attributes writer._row_xml reads."""

    def __init__(self, d, desc, cat, amt, bal, slug):
        self.date = d
        self.description = desc
        self.category = cat
        self.amount = Decimal(amt)
        self.balance = Decimal(bal) if bal is not None else None
        self.source_slug = slug


ROW = _Row(
    date(2026, 3, 12),
    "210.00 HARBOUR PHAR 400000*0001  09 MAR",
    "Personal care",
    "-54.21",
    "2215.40",
    "export-2026-03-04_to_03-12",
)
CARD_ROW = _Row(
    date(2026, 3, 7),
    "SEASIDE MARKET KIOSK",
    "Travel — trip",
    "-118.40",
    None,
    "export-2026-02-06_to_03-10",
)


class TestPreviewTsv(unittest.TestCase):
    def test_row_shape(self):
        line = emit.transactions_tsv([ROW]).strip().split("\n")[0]
        f = line.split("\t")
        self.assertEqual(len(f), 6)
        self.assertRegex(f[0], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(f[3], r"^-?\d+\.\d{2}$")
        self.assertRegex(f[5], r"^export-\d{4}-\d{2}-\d{2}_to_\d{2}-\d{2}$")

    def test_column_order_matches_the_sheet(self):
        f = emit.transactions_tsv([ROW]).strip().split("\t")
        self.assertEqual(f[1], ROW.description)
        self.assertEqual(f[2], "Personal care")
        self.assertEqual(f[4], "2215.40")

    def test_a_missing_balance_renders_as_an_empty_field(self):
        """writer omits column E entirely for card rows. The preview must show
        the same absence, not a zero — the card's Balance column is the literal
        string '0' in the export and is discarded."""
        f = emit.transactions_tsv([CARD_ROW]).strip().split("\t")
        self.assertEqual(len(f), 6)
        self.assertEqual(f[4], "")

    def test_trailing_zero_cents_are_preserved(self):
        row = _Row(
            date(2026, 8, 6),
            "X",
            "Fuel",
            "-615.30",
            "1000.00",
            "export-2026-03-01_to_03-09",
        )
        f = emit.transactions_tsv([row]).strip().split("\t")
        self.assertEqual(f[3], "-615.30")

    def test_an_uncategorised_row_is_refused(self):
        """writer writes category into column C and every SUMIFS in the workbook
        matches it byte-exactly. An empty string writes an empty cell and
        silently zeroes a category."""
        row = _Row(
            date(2026, 8, 12), "X", "", "-1.00", "1.00", "export-2026-03-01_to_03-09"
        )
        with self.assertRaises(emit.EmitError):
            emit.transactions_tsv([row])

    def test_tabs_and_newlines_in_a_description_cannot_break_the_grid(self):
        row = _Row(
            date(2026, 8, 12),
            "A\tB\nC",
            "Fuel",
            "-1.00",
            "1.00",
            "export-2026-03-01_to_03-09",
        )
        out = emit.transactions_tsv([row]).strip()
        self.assertEqual(len(out.split("\n")), 1)
        self.assertEqual(len(out.split("\t")), 6)

    def test_order_is_preserved(self):
        rows = emit.transactions_tsv([ROW, CARD_ROW]).strip().split("\n")
        self.assertIn("HARBOUR", rows[0])
        self.assertIn("SEASIDE", rows[1])


class TestNotesBlock(unittest.TestCase):
    def test_emits_the_register_not_prose(self):
        text = emit.notes_block(
            gaps=[GAP3],
            sources=["export-2026-03-01_to_03-09"],
            coverage_start=date(2025, 1, 1),
            coverage_end=date(2026, 5, 1),
            date_stamp=date(2026, 8, 20),
        )
        self.assertIn("2026-03-10", text)
        self.assertIn("12 days", text)
        self.assertIn("2026-08-20", text)

    def test_the_gap_count_agrees_with_the_register(self):
        """Two typed coverage lines once disagreed about the gap count on the
        same sheet. The count must be derived, never typed."""
        text = emit.notes_block(
            gaps=[GAP3, verify.Gap(date(2026, 1, 10), date(2026, 1, 20))],
            sources=[],
            coverage_start=date(2025, 1, 1),
            coverage_end=date(2026, 5, 1),
            date_stamp=date(2026, 8, 20),
        )
        self.assertIn("2 gaps", text)

    def test_no_gaps_says_so_explicitly(self):
        text = emit.notes_block(
            gaps=[],
            sources=[],
            coverage_start=date(2025, 1, 1),
            coverage_end=date(2026, 5, 1),
            date_stamp=date(2026, 8, 20),
        )
        self.assertIn("no gaps", text.lower())


class TestCfoMarkdown(unittest.TestCase):
    CASH = {"available": Decimal("615.25"), "card_headroom": Decimal("915.00")}

    def _out(self, gaps):
        txns = [
            type(
                "T",
                (),
                {
                    "date": date(2026, 4, 20),
                    "description": "ANNUITY PAYOUT 000123",
                    "amount": Decimal("30000.00"),
                    "category": "Income — annuity",
                    "is_transaction": True,
                },
            )(),
            type(
                "T",
                (),
                {
                    "date": date(2026, 4, 21),
                    "description": "SHOP",
                    "amount": Decimal("-5000.00"),
                    "category": "Groceries",
                    "is_transaction": True,
                },
            )(),
        ]
        return analyse.cfo(
            txns=txns, gaps=gaps, window=WINDOW, cash=self.CASH, today=date(2026, 8, 20)
        )

    def test_a_blocked_deficit_is_unmissable(self):
        md = emit.cfo_markdown(self._out([GAP3]))
        self.assertIn("REFUSED", md.upper())
        self.assertIn("2026-03-10", md)
        self.assertNotIn("R-", md.replace("R-0.00", ""))

    def test_a_clean_deficit_is_stated(self):
        md = emit.cfo_markdown(self._out([]))
        self.assertIn("Deficit", md)
        self.assertNotIn("REFUSED", md.upper())

    def test_coverage_is_always_shown(self):
        md = emit.cfo_markdown(self._out([GAP3]))
        self.assertIn("78", md)
        self.assertIn("90", md)

    def test_never_emits_never_depletes_as_a_number(self):
        """DASHBOARD!D18:D20 puts the string 'never depletes' in a numeric
        column, so any downstream float() or SUM breaks."""
        md = emit.cfo_markdown(self._out([]))
        self.assertNotIn("never depletes", md)


class TestAdvisorMarkdown(unittest.TestCase):
    def test_an_unexecuted_commitment_leads_with_what_it_cost(self):
        out = analyse.advisor(
            subs=[],
            savings_ledger=[
                {
                    "name": "Anthropic Max→Pro",
                    "monthly": Decimal("1595"),
                    "committed_on": date(2026, 8, 2),
                    "executed_on": None,
                    "executed": False,
                    "note": "",
                }
            ],
            today=date(2026, 8, 20),
        )
        md = emit.advisor_markdown(out)
        self.assertIn("18 days", md)
        self.assertIn("943.17", md)
        self.assertIn("Anthropic", md)

    def test_the_one_move_is_rendered(self):
        out = analyse.advisor(subs=[], savings_ledger=[], today=date(2026, 8, 20))
        self.assertIn("THE ONE MOVE", emit.advisor_markdown(out).upper())


class TestPrivacy(unittest.TestCase):
    def test_emit_never_reads_the_private_rules(self):
        """rules.private.yaml patterns must never be printed, logged, or
        surfaced in a report — only the resulting category."""
        with open(os.path.join(pipeline_dir(), "emit.py"), encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("rules.private", src)


if __name__ == "__main__":
    unittest.main()
