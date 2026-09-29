#!/usr/bin/env python3
"""test_store_hardening.py — one test per defect found by adversarial review.

An adversarial pass on 2026-08-20 confirmed fourteen defects in store.py by
execution. The worst three all falsified the module's own central claim — "the
ledger never loses a decision":

  · the ordinary idempotent re-seed WIPED executed_on and the note, because the
    upsert copied excluded.executed_on unconditionally
  · one datetime.now() stored '2026-08-21T02:26:02.235522', after which
    date.fromisoformat raised on read and EVERY row in the table became
    unreachable through the API — present, but unreadable
  · record_gaps only ever inserted, so a backfilled gap survived as a phantom
    and a moved gap left an overlapping twin, recording several times more
    "missing" days than were missing — the exact stale-prose bug the table
    exists to end

Plus a lock: a failed mark_executed left an open write transaction, and the next
connection blocked 5.43 seconds before reporting 'database is locked'.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal

from helpers import pipeline_dir

pipeline_dir()
import store  # noqa: E402
import verify  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ledger.sqlite")
        self.conn = store.connect(self.db)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestDecisionsSurvive(_Case):
    def test_re_recording_does_not_wipe_an_execution(self):
        store.record_saving(
            self.conn,
            "Plan downgrade",
            Decimal("1000"),
            date(2026, 8, 2),
            note="cancel in app",
        )
        store.mark_executed(self.conn, "Plan downgrade", date(2026, 8, 21))
        # The ordinary idempotent re-seed.
        store.record_saving(
            self.conn, "Plan downgrade", Decimal("1000"), date(2026, 8, 2)
        )
        row = store.savings_ledger(self.conn)[0]
        self.assertTrue(row["executed"], "the execution was lost")
        self.assertEqual(row["executed_on"], date(2026, 8, 21))
        self.assertEqual(row["note"], "cancel in app", "the note was lost")

    def test_an_execution_before_the_decision_is_refused(self):
        store.record_saving(self.conn, "X", Decimal("100"), date(2026, 8, 20))
        with self.assertRaises(store.StoreError):
            store.mark_executed(self.conn, "X", date(2026, 8, 2))


class TestDatesAreDates(_Case):
    def test_a_datetime_is_normalised_not_stored_with_a_time(self):
        """datetime is a date subclass, so this slipped through and made the
        whole table unreadable on the next read."""
        store.record_saving(
            self.conn, "X", Decimal("100"), datetime(2026, 8, 2, 2, 26, 2)
        )
        self.assertEqual(
            store.savings_ledger(self.conn)[0]["committed_on"], date(2026, 8, 2)
        )

    def test_a_date_shaped_string_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.record_saving(self.conn, "X", Decimal("100"), "21 July 2026")
        with self.assertRaises(store.StoreError):
            store.record_saving(self.conn, "X", Decimal("100"), "2026-08-02")

    def test_the_table_stays_readable_after_a_refused_write(self):
        store.record_saving(self.conn, "Good", Decimal("100"), date(2026, 8, 2))
        with self.assertRaises(store.StoreError):
            store.record_saving(self.conn, "Bad", Decimal("100"), "nonsense")
        self.assertEqual(len(store.savings_ledger(self.conn)), 1)


class TestNoStrandedTransactions(_Case):
    def test_a_failed_mark_executed_does_not_hold_the_write_lock(self):
        with self.assertRaises(store.StoreError):
            store.mark_executed(self.conn, "does-not-exist", date(2026, 8, 21))
        self.assertFalse(self.conn.in_transaction, "left an open transaction")

    def test_another_connection_can_still_write(self):
        with self.assertRaises(store.StoreError):
            store.mark_executed(self.conn, "does-not-exist", date(2026, 8, 21))
        other = sqlite3.connect(self.db, timeout=2.0)
        try:
            other.execute(
                "INSERT INTO decision (name, monthly_cents, committed_on) "
                "VALUES ('other', 100, '2026-08-02')"
            )
            other.commit()
        finally:
            other.close()

    def test_a_refused_amount_does_not_strand_a_transaction(self):
        with self.assertRaises(store.StoreError):
            store.record_saving(self.conn, "huge", Decimal("1E+20"), date(2026, 8, 2))
        self.assertFalse(self.conn.in_transaction)


class TestGapRegisterIsReplaced(_Case):
    A = verify.Gap(date(2026, 4, 25), date(2026, 5, 18))
    B = verify.Gap(date(2026, 7, 22), date(2026, 8, 3))
    B_MOVED = verify.Gap(date(2026, 7, 25), date(2026, 8, 3))

    def test_a_backfilled_gap_disappears(self):
        store.record_gaps(self.conn, "chq", [self.A, self.B])
        store.record_gaps(self.conn, "chq", [self.B])
        got = store.gaps(self.conn, "chq")
        self.assertEqual(len(got), 1, "the backfilled gap survived as a phantom")
        self.assertEqual(got[0].start, date(2026, 7, 22))

    def test_a_moved_gap_does_not_leave_a_twin(self):
        store.record_gaps(self.conn, "chq", [self.B])
        store.record_gaps(self.conn, "chq", [self.B_MOVED])
        got = store.gaps(self.conn, "chq")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].start, date(2026, 7, 25))
        self.assertEqual(
            sum(g.days for g in got), 10, "47 days recorded where 10 are real"
        )

    def test_replacing_one_account_leaves_the_other_alone(self):
        store.record_gaps(self.conn, "chq", [self.A])
        store.record_gaps(self.conn, "card", [self.B])
        store.record_gaps(self.conn, "chq", [])
        self.assertEqual(store.gaps(self.conn, "chq"), [])
        self.assertEqual(len(store.gaps(self.conn, "card")), 1)

    def test_an_inverted_gap_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.record_gaps(
                self.conn, "chq", [verify.Gap(date(2026, 8, 10), date(2026, 8, 1))]
            )

    def test_gaps_by_account_keeps_the_account(self):
        store.record_gaps(self.conn, "chq", [self.A])
        store.record_gaps(self.conn, "card", [self.B])
        by_account = store.gaps_by_account(self.conn)
        self.assertEqual(sorted(by_account), ["card", "chq"])


class TestSchemaGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_future_version_is_refused_without_mutating_the_file(self):
        """The schema was applied BEFORE the version check, so a refused database
        came back with v1 tables resurrected and WAL sidecars created."""
        path = os.path.join(self.tmp, "future.sqlite")
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA user_version=2")
        raw.commit()
        raw.close()

        with self.assertRaises(store.StoreError):
            store.connect(path)

        raw = sqlite3.connect(path)
        tables = [
            r[0]
            for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        raw.close()
        self.assertEqual(tables, [], f"the refused database was mutated: {tables}")

    def test_a_foreign_table_is_not_certified_as_ours(self):
        """CREATE TABLE IF NOT EXISTS is a no-op against a same-named foreign
        table, so the database got stamped v1 and failed at the first write."""
        path = os.path.join(self.tmp, "foreign.sqlite")
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE fact (id INTEGER PRIMARY KEY, blob TEXT)")
        raw.commit()
        raw.close()
        with self.assertRaises(store.StoreError):
            store.connect(path)

    def test_a_non_database_is_refused_as_a_store_error(self):
        path = os.path.join(self.tmp, "not-a-db.bin")
        with open(path, "wb") as handle:
            handle.write(b"PK\x03\x04not a database at all" * 100)
        with self.assertRaises(store.StoreError):
            store.connect(path)


class TestMoneyGuards(_Case):
    def test_an_amount_too_large_for_the_column_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.record_saving(self.conn, "huge", Decimal("1E+20"), date(2026, 8, 2))

    def test_an_unusable_decimal_is_a_store_error_not_a_raw_invalid_operation(self):
        for bad in (Decimal("1E+26"), Decimal("NaN"), Decimal("Infinity"), "abc", None):
            with self.assertRaises(store.StoreError, msg=f"accepted {bad!r}"):
                store.record_saving(self.conn, "x", bad, date(2026, 8, 2))

    def test_a_float_cannot_reach_the_cents_column(self):
        """SQLite INTEGER is affinity, not a constraint — a float inserted into
        it stays a float, and _rands would then do Decimal(float)."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO decision (name, monthly_cents, committed_on) "
                "VALUES ('f', 1000.5, '2026-08-02')"
            )


class TestRunHistory(_Case):
    def test_two_files_in_the_same_second_are_both_kept(self):
        """A two-file drop is the normal case. Keying on
        run_at alone silently overwrote the first with the second."""
        for slug, account, rows in (
            ("export-2026-03-01_to_03-09", "10000000001", 12),
            ("export-2026-01-06_to_03-10", "400000xxxx0001", 7),
        ):
            store.record_run(
                self.conn,
                run_at="2026-08-21T09:00:00",
                source_slug=slug,
                account=account,
                rows_parsed=rows,
                rows_new=rows,
                chain_ok=True,
                exit_code=0,
            )
        self.assertEqual(len(store.import_runs(self.conn)), 2)

    def test_flags_round_trip(self):
        store.record_run(
            self.conn,
            run_at="2026-08-21T09:00:00",
            source_slug="s",
            account="a",
            rows_parsed=1,
            rows_new=0,
            chain_ok=False,
            exit_code=3,
            flags=["chain break at row 12"],
        )
        run = store.import_runs(self.conn)[0]
        self.assertEqual(run["flags"], ["chain break at row 12"])
        self.assertFalse(run["chain_ok"])


class TestReviewAndFacts(_Case):
    def test_an_empty_category_is_refused(self):
        """'' is falsy, so a caller could not tell it from 'never asked'."""
        with self.assertRaises(store.StoreError):
            store.record_review(self.conn, "X", "", date(2026, 8, 20))
        # And the CHECK constraint is the backstop if the guard is ever removed.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO review (description, category, decided_on) "
                "VALUES ('X', '', '2026-08-20')"
            )

    def test_a_future_verified_date_is_not_fresh(self):
        store.record_fact(
            self.conn,
            "k",
            "1",
            source="s",
            verified_on=date(2027, 1, 1),
            confidence="HIGH",
        )
        fact = store.facts(self.conn, today=date(2026, 8, 20))[0]
        self.assertLess(fact["age_days"], 0)
        self.assertTrue(fact["stale"], "a future date is wrong, not fresh")

    def test_the_staleness_boundary(self):
        store.record_fact(
            self.conn,
            "k",
            "1",
            source="s",
            verified_on=date(2026, 6, 21),
            confidence="HIGH",
        )
        self.assertFalse(store.facts(self.conn, today=date(2026, 8, 20))[0]["stale"])
        self.assertTrue(store.facts(self.conn, today=date(2026, 8, 21))[0]["stale"])


if __name__ == "__main__":
    unittest.main()
