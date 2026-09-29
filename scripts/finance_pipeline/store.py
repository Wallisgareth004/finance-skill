#!/usr/bin/env python3
"""store.py — the ONLY module that touches SQLite. It never touches Excel.

IN   the path of ledger.sqlite
OUT  the ledger: run log, gap register, decisions, review answers, facts

Excel stays the thing the owner reads and trusts. This holds what the bank exports
cannot regenerate:

  · the GAP REGISTER, as data rather than prose
  · DECISIONS the owner has committed to, and whether they were executed
  · REVIEW answers, so a categorisation question is asked once
  · a FACTS mirror, so every advisor number carries source + date + confidence

═══ WHY THE GAP REGISTER IS A TABLE ═══
The real workbook's NOTES & GAPS sheet held two coverage statements at once: an
older "...with two gaps" ABOVE a newer "...with THREE gaps". Both were typed
by hand, and the stale one was the one a reader met first. Prose cannot be a
register.

The register is REPLACED per account, never merged. An adversarial pass on
2026-08-20 proved that upserting alone reproduces the exact bug being fixed: a
backfilled gap survived as a phantom, and a gap whose start moved left an
overlapping twin, recording several times more "missing" days than were missing.

═══ WHY INTEGER CENTS ═══
Same reason as model.py. A REAL column would reintroduce the float drift that
leaves a sheet's sum with a tail such as ...00000118. SQLite's
"INTEGER" is affinity, not a constraint — a float inserted into it stays a float
— so the columns carry an explicit CHECK on typeof().

═══ WHY user_version, AND WHY IT IS READ FIRST ═══
A store whose every row can be re-derived from raw exports can skip migrations.
This ledger cannot: a decision the owner recorded exists nowhere else.

The version is read BEFORE the schema is applied. Applying first meant a
refused database had already been mutated — v1 tables resurrected alongside a
v2 schema, WAL sidecars created — leaving a migration script to face a shape
that neither version describes.

═══ DURABILITY ═══
`synchronous=NORMAL` under WAL trades durability for speed: a power loss can
lose transactions committed since the last checkpoint. That is the wrong trade
for a file whose contents cannot be regenerated, so this uses FULL.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import verify

SCHEMA_VERSION = 1

# The owner's own rule for facts: anything verified more than 60 days ago is STALE.
STALE_AFTER_DAYS = 60

CONFIDENCE = ("HIGH", "MEDIUM", "LOW", "UNVERIFIED", "CONTESTED")

_CENTS = Decimal("0.01")
# SQLite INTEGER is a signed 64-bit value. _cents accepted numbers ~1e7 times
# larger, which surfaced as an untyped sqlite3.OverflowError mid-transaction.
_MAX_CENTS = 2**63 - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_run (
    run_at        TEXT NOT NULL,
    source_slug   TEXT NOT NULL,
    account       TEXT NOT NULL,
    rows_parsed   INTEGER NOT NULL,
    rows_new      INTEGER NOT NULL,
    chain_ok      INTEGER NOT NULL,
    exit_code     INTEGER NOT NULL,
    flags_json    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_at, source_slug, account)
);

CREATE TABLE IF NOT EXISTS gap (
    account   TEXT NOT NULL,
    start     TEXT NOT NULL,
    end       TEXT NOT NULL,
    PRIMARY KEY (account, start)
);

CREATE TABLE IF NOT EXISTS decision (
    name           TEXT PRIMARY KEY,
    monthly_cents  INTEGER NOT NULL CHECK (typeof(monthly_cents) = 'integer'),
    committed_on   TEXT NOT NULL,
    executed_on    TEXT,
    note           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS review (
    description  TEXT PRIMARY KEY,
    category     TEXT NOT NULL CHECK (length(category) > 0),
    decided_on   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    source       TEXT NOT NULL,
    verified_on  TEXT NOT NULL,
    confidence   TEXT NOT NULL
);
"""

TABLES = ("import_run", "gap", "decision", "review", "fact")

# Columns each table must actually have. A database stamped user_version=0 but
# carrying a foreign table of the same name passes CREATE TABLE IF NOT EXISTS
# silently, gets stamped v1, and then fails at the first write.
_REQUIRED_COLUMNS = {
    "import_run": {"run_at", "source_slug", "account", "rows_parsed"},
    "gap": {"account", "start", "end"},
    "decision": {"name", "monthly_cents", "committed_on", "executed_on", "note"},
    "review": {"description", "category", "decided_on"},
    "fact": {"key", "value", "source", "verified_on", "confidence"},
}


class StoreError(Exception):
    """Raised rather than writing something the reader would have to guess at."""


def _cents(value) -> int:
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        if not dec.is_finite():
            raise StoreError(f"amount is not a finite number: {value!r}")
        cents = int(dec.quantize(_CENTS, rounding=ROUND_HALF_UP) * 100)
    except StoreError:
        raise
    except (InvalidOperation, ValueError, TypeError, ArithmeticError) as exc:
        # quantize() sat outside the guard, so Decimal("1E+26") escaped as a raw
        # decimal.InvalidOperation with no context for the operator.
        raise StoreError(f"amount is not a usable number: {value!r}") from exc
    if abs(cents) > _MAX_CENTS:
        raise StoreError(
            f"amount {value!r} exceeds what an SQLite INTEGER can hold "
            f"({_MAX_CENTS} cents)"
        )
    return cents


def _rands(cents: int) -> Decimal:
    if not isinstance(cents, int):
        raise StoreError(
            f"monthly_cents is {type(cents).__name__}, not int — the CHECK "
            "constraint should have prevented this row from being written"
        )
    return (Decimal(cents) / 100).quantize(_CENTS)


def _iso(value) -> str:
    """Dates only. A `datetime` is a `date` subclass, so passing datetime.now()
    used to store '2026-08-21T02:26:02.235522' — after which date.fromisoformat
    raised on read and every row in the table became unreachable through the
    API, present but unreadable."""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        raise StoreError(f"not a date: {value!r} ({type(value).__name__})")
    return value.isoformat()


def connect(path: str) -> sqlite3.Connection:
    """Open the ledger, applying the schema. Safe to call repeatedly."""
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")

        found = conn.execute("PRAGMA user_version").fetchone()[0]
        if found not in (0, SCHEMA_VERSION):
            raise StoreError(
                f"ledger schema is version {found}, this code expects "
                f"{SCHEMA_VERSION}. Migrate before writing — the decisions in "
                "this file cannot be regenerated from the bank exports. "
                "Nothing has been modified."
            )

        conn.executescript(SCHEMA)
        for table, required in _REQUIRED_COLUMNS.items():
            present = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            missing = required - present
            if missing:
                raise StoreError(
                    f"table {table} exists but is missing {sorted(missing)}. "
                    "This database was not created by this schema."
                )
        if found == 0:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise StoreError(f"{path} is not a usable ledger: {exc}") from exc
    except Exception:
        # Every error path used to leak the connection: 500 refused connects
        # held 163 open descriptors, each with a lock and WAL sidecars.
        conn.close()
        raise
    return conn


def table_counts(conn: sqlite3.Connection) -> dict:
    """Proof the write landed. Printed in run.py's JSON summary."""
    counts = {}
    for table in TABLES:
        if table not in _REQUIRED_COLUMNS:
            raise StoreError(f"{table!r} is not a known table")
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def _write(conn: sqlite3.Connection, sql: str, params, many: bool = False) -> sqlite3.Cursor:
    """Every write rolls back on failure. A raise used to strand an open
    transaction, and the next connection blocked for 5.43s before reporting
    'database is locked'."""
    try:
        cur = conn.executemany(sql, params) if many else conn.execute(sql, params)
        conn.commit()
        return cur
    except Exception:
        conn.rollback()
        raise


# ─────────────────────────── the savings ledger ───────────────────────────


def record_saving(
    conn: sqlite3.Connection,
    name: str,
    monthly,
    committed_on,
    executed_on=None,
    note: str = "",
) -> None:
    """Record a cut the owner has committed to. The service name is the natural key.

    Re-recording NEVER clears an execution. The plain upsert wiped executed_on
    and note on the ordinary idempotent re-seed, which is precisely the "ledger
    never loses a decision" claim falsified.
    """
    _write(
        conn,
        """
        INSERT INTO decision (name, monthly_cents, committed_on, executed_on, note)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            monthly_cents = excluded.monthly_cents,
            committed_on  = excluded.committed_on,
            executed_on   = COALESCE(excluded.executed_on, decision.executed_on),
            note          = CASE WHEN excluded.note = '' THEN decision.note
                                 ELSE excluded.note END
        """,
        (
            name,
            _cents(monthly),
            _iso(committed_on),
            _iso(executed_on) if executed_on else None,
            note,
        ),
    )


def mark_executed(conn: sqlite3.Connection, name: str, executed_on) -> None:
    stamp = _iso(executed_on)
    row = conn.execute(
        "SELECT committed_on FROM decision WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise StoreError(f"no decision recorded under {name!r}")
    if stamp < row["committed_on"]:
        raise StoreError(
            f"{name}: executed {stamp} before it was decided {row['committed_on']}"
        )
    _write(conn, "UPDATE decision SET executed_on = ? WHERE name = ?", (stamp, name))


def savings_ledger(conn: sqlite3.Connection) -> list:
    """Every committed cut, oldest commitment first."""
    out = []
    for row in conn.execute(
        "SELECT * FROM decision ORDER BY committed_on, name"
    ).fetchall():
        out.append(
            {
                "name": row["name"],
                "monthly": _rands(row["monthly_cents"]),
                "committed_on": date.fromisoformat(row["committed_on"]),
                "executed_on": (
                    date.fromisoformat(row["executed_on"])
                    if row["executed_on"]
                    else None
                ),
                "executed": bool(row["executed_on"]),
                "note": row["note"],
            }
        )
    return out


# ──────────────────────────── the gap register ────────────────────────────


def record_gaps(conn: sqlite3.Connection, account: str, gaps: list) -> None:
    """REPLACE the register for this account. Never merge.

    Merging reproduces the stale-prose bug: a backfilled gap survives as a
    phantom, and a gap whose start moves leaves an overlapping twin.
    """
    rows = []
    for gap in gaps:
        start, end = _iso(gap.start), _iso(gap.end)
        if end < start:
            raise StoreError(f"gap ends before it starts: {start} → {end}")
        rows.append((account, start, end))
    try:
        conn.execute("DELETE FROM gap WHERE account = ?", (account,))
        conn.executemany("INSERT INTO gap (account, start, end) VALUES (?, ?, ?)", rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def gaps(conn: sqlite3.Connection, account: str | None = None) -> list:
    """The gap register as verify.Gap objects — so Gap.intersects() and Gap.days
    come along for free and nothing re-implements them."""
    if account is None:
        rows = conn.execute("SELECT * FROM gap ORDER BY start, account").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM gap WHERE account = ? ORDER BY start", (account,)
        ).fetchall()
    return [
        verify.Gap(date.fromisoformat(r["start"]), date.fromisoformat(r["end"]))
        for r in rows
    ]


def gaps_by_account(conn: sqlite3.Connection) -> dict:
    """gaps() drops the account column, so a whole-ledger listing cannot say
    which account a gap belongs to."""
    out: dict = {}
    for row in conn.execute("SELECT * FROM gap ORDER BY account, start").fetchall():
        out.setdefault(row["account"], []).append(
            verify.Gap(date.fromisoformat(row["start"]), date.fromisoformat(row["end"]))
        )
    return out


# ───────────────────────────── the review queue ────────────────────────────


def record_review(
    conn: sqlite3.Connection, description: str, category: str, decided_on
) -> None:
    if not category:
        raise StoreError(
            "a review answer needs a category — an empty one is indistinguishable "
            "from never having been asked"
        )
    _write(
        conn,
        """
        INSERT INTO review (description, category, decided_on) VALUES (?, ?, ?)
        ON CONFLICT(description) DO UPDATE SET
            category = excluded.category, decided_on = excluded.decided_on
        """,
        (description, category, _iso(decided_on)),
    )


def review_answer(conn: sqlite3.Connection, description: str) -> str | None:
    """Keyed on the EXACT description. FNB descriptions are fixed-width and the
    internal whitespace distinguishes real merchants — normalising here would
    silently answer a question that was never asked."""
    row = conn.execute(
        "SELECT category FROM review WHERE description = ?", (description,)
    ).fetchone()
    return row["category"] if row else None


# ───────────────────────────── the FACTS mirror ────────────────────────────


def record_fact(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    source: str,
    verified_on,
    confidence: str,
) -> None:
    if confidence not in CONFIDENCE:
        raise StoreError(
            f"confidence must be one of {CONFIDENCE}, got {confidence!r}. "
            "A number without a confidence is a number that gets quoted as canon."
        )
    _write(
        conn,
        """
        INSERT INTO fact (key, value, source, verified_on, confidence)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value, source = excluded.source,
            verified_on = excluded.verified_on, confidence = excluded.confidence
        """,
        (key, str(value), source, _iso(verified_on), confidence),
    )


def facts(conn: sqlite3.Connection, today: date) -> list:
    today = today.date() if isinstance(today, datetime) else today
    out = []
    for row in conn.execute("SELECT * FROM fact ORDER BY key").fetchall():
        verified = date.fromisoformat(row["verified_on"])
        age = (today - verified).days
        out.append(
            {
                "key": row["key"],
                "value": row["value"],
                "source": row["source"],
                "verified_on": verified,
                "confidence": row["confidence"],
                "age_days": age,
                # A future verified_on is not "fresh", it is wrong.
                "stale": age > STALE_AFTER_DAYS or age < 0,
            }
        )
    return out


# ──────────────────────────────── run history ──────────────────────────────


def record_run(
    conn: sqlite3.Connection,
    run_at: str,
    source_slug: str,
    account: str,
    rows_parsed: int,
    rows_new: int,
    chain_ok: bool,
    exit_code: int,
    flags: list | None = None,
) -> None:
    """The primary key is (run_at, source_slug, account), not run_at alone.

    A two-file drop — the normal case — records both
    files in the same second, and keying on run_at alone silently overwrote the
    first with the second.
    """
    _write(
        conn,
        """
        INSERT INTO import_run
            (run_at, source_slug, account, rows_parsed, rows_new, chain_ok,
             exit_code, flags_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_at, source_slug, account) DO UPDATE SET
            rows_parsed = excluded.rows_parsed, rows_new = excluded.rows_new,
            chain_ok = excluded.chain_ok, exit_code = excluded.exit_code,
            flags_json = excluded.flags_json
        """,
        (
            run_at,
            source_slug,
            account,
            rows_parsed,
            rows_new,
            1 if chain_ok else 0,
            exit_code,
            json.dumps(flags or []),
        ),
    )


def import_runs(conn: sqlite3.Connection, limit: int = 50) -> list:
    rows = conn.execute(
        "SELECT * FROM import_run ORDER BY run_at DESC, source_slug LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "run_at": r["run_at"],
            "source_slug": r["source_slug"],
            "account": r["account"],
            "rows_parsed": r["rows_parsed"],
            "rows_new": r["rows_new"],
            "chain_ok": bool(r["chain_ok"]),
            "exit_code": r["exit_code"],
            "flags": json.loads(r["flags_json"]),
        }
        for r in rows
    ]
