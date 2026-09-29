#!/usr/bin/env python3
"""test_store.py — the internal ledger.

store.py holds what cannot be regenerated from the bank exports: the gap
register, decisions the owner has committed to, review answers already given,
and the FACTS provenance mirror the advisor lens checks against.

Excel remains the thing the owner reads. This is plumbing, not a rival source of
truth.

Two things are pinned here that a sibling pipeline got wrong and this one must
not inherit:
  · money is stored as INTEGER CENTS, never a float column
  · the schema carries PRAGMA user_version, so an additive migration is
    possible at all (that pipeline has no migration path whatsoever)
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
import store  # noqa: E402
import verify  # noqa: E402


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ledger.sqlite")
        self.conn = store.connect(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSchema(_StoreCase):
    def test_connect_is_idempotent(self):
        """connect() runs the schema every time. Re-connecting must not raise
        and must not lose data."""
        store.record_saving(
            self.conn, "Plan downgrade", Decimal("1000"), date(2026, 8, 2)
        )
        self.conn.close()
        self.conn = store.connect(self.db)
        self.assertEqual(len(store.savings_ledger(self.conn)), 1)

    def test_schema_version_is_stamped(self):
        got = self.conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(got, store.SCHEMA_VERSION)

    def test_wal_is_enabled(self):
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_money_columns_are_integer_cents(self):
        """A float column would reintroduce the drift model.py exists to avoid."""
        cols = {
            r[1]: r[2]
            for r in self.conn.execute("PRAGMA table_info(decision)").fetchall()
        }
        self.assertEqual(cols["monthly_cents"], "INTEGER")

    def test_table_counts_reports_every_table(self):
        counts = store.table_counts(self.conn)
        for table in ("import_run", "gap", "decision", "review", "fact"):
            self.assertIn(table, counts)
            self.assertEqual(counts[table], 0)


class TestSavingsLedger(_StoreCase):
    def test_records_and_reads_back_a_commitment(self):
        store.record_saving(
            self.conn, "Plan downgrade", Decimal("1000"), date(2026, 8, 2)
        )
        led = store.savings_ledger(self.conn)
        self.assertEqual(len(led), 1)
        row = led[0]
        self.assertEqual(row["name"], "Plan downgrade")
        self.assertEqual(row["monthly"], Decimal("1000.00"))
        self.assertEqual(row["committed_on"], date(2026, 8, 2))
        self.assertIsNone(row["executed_on"])
        self.assertFalse(row["executed"])

    def test_recording_the_same_decision_twice_updates_it(self):
        store.record_saving(
            self.conn, "Sales tool", Decimal("912.40"), date(2026, 8, 1)
        )
        store.record_saving(
            self.conn,
            "Sales tool",
            Decimal("912.40"),
            date(2026, 8, 1),
            note="still billing",
        )
        led = store.savings_ledger(self.conn)
        self.assertEqual(len(led), 1, "natural key is the service name")
        self.assertEqual(led[0]["note"], "still billing")

    def test_marking_executed_sets_the_date_and_clears_the_flag(self):
        store.record_saving(
            self.conn, "Plan downgrade", Decimal("1000"), date(2026, 8, 2)
        )
        store.mark_executed(self.conn, "Plan downgrade", date(2026, 8, 21))
        row = store.savings_ledger(self.conn)[0]
        self.assertTrue(row["executed"])
        self.assertEqual(row["executed_on"], date(2026, 8, 21))

    def test_marking_an_unknown_decision_raises(self):
        with self.assertRaises(store.StoreError):
            store.mark_executed(self.conn, "Nope", date(2026, 8, 21))

    def test_cents_round_trip_exactly(self):
        store.record_saving(self.conn, "Playlist sync", Decimal("88.15"), date(2026, 7, 21))
        self.assertEqual(
            store.savings_ledger(self.conn)[0]["monthly"], Decimal("88.15")
        )


class TestGapRegister(_StoreCase):
    """The gap register lives here as DATA. A hand-typed coverage line on the
    real workbook went stale while a newer one sat below it. Prose cannot be a
    register; it must be emitted from this table."""

    GAPS = [  # synthetic
        verify.Gap(date(2025, 6, 1), date(2025, 6, 30)),
        verify.Gap(date(2026, 1, 10), date(2026, 1, 20)),
        verify.Gap(date(2026, 3, 10), date(2026, 3, 21)),
    ]

    def test_records_and_reads_back(self):
        store.record_gaps(self.conn, "10000000001", self.GAPS)
        got = store.gaps(self.conn, "10000000001")
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0].start, date(2025, 6, 1))
        self.assertIsInstance(got[0], verify.Gap)

    def test_reimporting_the_same_gaps_does_not_duplicate(self):
        store.record_gaps(self.conn, "10000000001", self.GAPS)
        store.record_gaps(self.conn, "10000000001", self.GAPS)
        self.assertEqual(len(store.gaps(self.conn, "10000000001")), 3)

    def test_gaps_are_returned_in_date_order(self):
        store.record_gaps(self.conn, "10000000001", list(reversed(self.GAPS)))
        got = store.gaps(self.conn, "10000000001")
        self.assertEqual([g.start for g in got], [g.start for g in self.GAPS])

    def test_accounts_are_kept_separate(self):
        store.record_gaps(self.conn, "10000000001", self.GAPS[:1])
        store.record_gaps(self.conn, "400000xxxx0001", self.GAPS[1:])
        self.assertEqual(len(store.gaps(self.conn, "10000000001")), 1)
        self.assertEqual(len(store.gaps(self.conn, "400000xxxx0001")), 2)
        self.assertEqual(len(store.gaps(self.conn)), 3, "no account = all accounts")


class TestReviewQueue(_StoreCase):
    def test_an_answer_given_once_is_not_asked_again(self):
        store.record_review(
            self.conn, "YOCO *SOMETHING", "Restaurants", date(2026, 8, 20)
        )
        self.assertEqual(
            store.review_answer(self.conn, "YOCO *SOMETHING"), "Restaurants"
        )
        self.assertIsNone(store.review_answer(self.conn, "UNSEEN MERCHANT"))

    def test_answers_are_keyed_on_the_exact_description(self):
        store.record_review(self.conn, "A  B", "Groceries", date(2026, 8, 20))
        self.assertIsNone(
            store.review_answer(self.conn, "A B"),
            "whitespace is load-bearing in FNB descriptions",
        )


class TestFactMirror(_StoreCase):
    """Every advisor number carries source + verified date + confidence, or it
    is not emitted. The owner's own rule: anything verified >60 days ago is STALE."""

    def test_records_provenance_and_flags_staleness(self):
        store.record_fact(
            self.conn,
            "monthly_income",
            "30000.00",
            source="bank export + insurer letter",
            verified_on=date(2026, 7, 31),
            confidence="HIGH",
        )
        fresh = store.facts(self.conn, today=date(2026, 8, 20))
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["age_days"], 20)
        self.assertFalse(fresh[0]["stale"])

        aged = store.facts(self.conn, today=date(2026, 10, 20))
        self.assertTrue(aged[0]["stale"], ">60 days is stale per FACTS.md")

    def test_confidence_is_constrained(self):
        with self.assertRaises(store.StoreError):
            store.record_fact(
                self.conn,
                "x",
                "1",
                source="s",
                verified_on=date(2026, 8, 20),
                confidence="PRETTY SURE",
            )


class TestImportRun(_StoreCase):
    def test_records_a_run_and_its_chain_verdict(self):
        store.record_run(
            self.conn,
            run_at="2026-08-20T15:04:05",
            source_slug="export-2026-03-01_to_03-09",
            account="10000000001",
            rows_parsed=60,
            rows_new=0,
            chain_ok=True,
            exit_code=0,
        )
        runs = store.import_runs(self.conn)
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["chain_ok"])
        self.assertEqual(runs[0]["rows_new"], 0)

    def test_the_same_run_timestamp_is_not_duplicated(self):
        for _ in range(2):
            store.record_run(
                self.conn,
                run_at="2026-08-20T15:04:05",
                source_slug="s",
                account="a",
                rows_parsed=1,
                rows_new=1,
                chain_ok=True,
                exit_code=0,
            )
        self.assertEqual(len(store.import_runs(self.conn)), 1)


class TestNeverTouchesExcel(unittest.TestCase):
    def test_store_does_not_import_openpyxl(self):
        """store.py owns SQLite and nothing else. writer.py owns the workbook."""
        with open(os.path.join(pipeline_dir(), "store.py"), encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("openpyxl", src)


if __name__ == "__main__":
    unittest.main()
