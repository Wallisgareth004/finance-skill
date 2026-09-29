#!/usr/bin/env python3
"""detect.py — identify which FNB export format a file is, by HEADER SIGNATURE.

IN   the path of a file from the drop
OUT  Detected (format, account number and kind, closing and available balance),
     or Unreadable / UnknownFormat

WHY NOT A LINE NUMBER: the preamble has been exactly 6 lines in every real
export measured, but a signature scan costs nothing and survives FNB changing
their layout. A wrong guess here corrupts everything downstream.

WHY NOT THE FILENAME: FNB names an export after the account number, so two
different exports (different date ranges, different contents) routinely share
a name. The `Account:` and `Balance:` preamble lines are the only reliable
identity signals.

Formats seen in the wild:
  FORMAT_A  native FNB online-banking export — cheque OR card. The only format
            this skill imports; materially higher fidelity than the others.
  FORMAT_B  statement-*.csv, produced by FNB_Analysis/process_statements.py from
            PDF statements. A noticeable share of its descriptions are unrecoverable
            ("(no description)", category labels leaked into the description
            column, card-mask-only rows). Archival.
  FORMAT_C  fnb_transactions.csv — same extractor, carries its own competing
            24-label taxonomy. Archival; do NOT join against it (it holds raw
            merchant strings the model deliberately aggregates for privacy).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

FORMAT_A = "fnb_native"
FORMAT_B = "pdf_statement"
FORMAT_C = "categorised_csv"

# Keys are the header line with ALL whitespace removed, lowercased. Whitespace
# stripping is essential: Format A's header is "Date, Amount, Balance, Description"
# with a leading space before each field after the first.
SIGNATURES = {
    "date,amount,balance,description": FORMAT_A,
    "date,description,amount,balance,sourcefile": FORMAT_B,
    "date,description,amount,category": FORMAT_C,
}

MAX_SCAN = 30

# Account kinds whose export carries a real running balance (the card prints "0").
BALANCED_KINDS = frozenset({"cheque", "savings"})


class UnknownFormat(Exception):
    """Raised rather than guessing. Guessing silently corrupts the ledger."""


# --- 2026-09-03 REPAIR: "could not READ it" is NOT "unrecognised format" --------------
# Checked because a sibling pipeline's detect.py (same author, same shape) collapsed a
# TimeoutError from an iCloud-evicted file into "unrecognized", and its run then
# reported "no dated data found" — telling its owner there was no data when the truth
# was that it could not open the file.
#
# This module's defect was a different symptom of the same cause: `_read_lines` had no
# guard at all, so ONE unreadable export raised a raw PermissionError/TimeoutError out
# of `_collect` and killed the entire run — every other file in the folder included.
# And with the folder listing intact but the files evicted, the surviving message would
# have been "no recognised FNB exports in <folder>", which is equally false.
#
# With macOS's `Optimize Mac Storage` on, most of a synced folder can be stubs, so this is
# not hypothetical. The dataless flag is checked FIRST so an evicted file is named in
# milliseconds rather than after a ~7-minute read timeout.
UF_DATALESS = 0x40000000  # sys/stat.h; what `ls -lO` prints as `dataless`


class Unreadable(Exception):
    """The bytes were never obtained — we cannot say anything about the contents.

    Deliberately NOT a subclass of UnknownFormat: callers must be able to catch the
    two separately, or the distinction is cosmetic and the wrong message ships.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _is_dataless_real(path: str) -> bool:
    """True if macOS/iCloud has evicted the contents and left a stub behind."""
    try:
        return bool(getattr(os.stat(path), "st_flags", 0) & UF_DATALESS)
    except OSError:
        return False


# module-level indirection so tests can substitute it
is_dataless = _is_dataless_real

DATALESS_REASON = (
    "iCloud-evicted (dataless stub) — the contents are not on this disk. "
    "This is NOT a missing or malformed export. Turn off Optimize Mac Storage "
    "or download the file, then re-run."
)


@dataclass
class Detected:
    path: str
    format: str
    header_index: int
    account_number: str | None = None
    account_kind: str | None = None  # 'cheque' | 'card'
    closing_balance: Decimal | None = None
    available: Decimal | None = None


def _read_lines(path: str) -> list[str]:
    """Decode defensively and normalise line endings in one step.

    Format A is bare LF; Formats B/C/D are CRLF. Format B contains one non-ASCII
    character (U+2014 EM DASH) which a latin-1 read would mangle, so utf-8-sig
    is tried first — it also absorbs a BOM if FNB ever starts emitting one.
    """
    if is_dataless(path):
        raise Unreadable(path, DATALESS_REASON)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:  # includes TimeoutError [Errno 60] on an evicted file
        raise Unreadable(path, f"{type(e).__name__}: {e}") from e
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    raise UnknownFormat(f"cannot decode {path}")


def _norm(line: str) -> str:
    return re.sub(r"\s+", "", line).lower()


def sniff(path: str) -> Detected:
    lines = _read_lines(path)
    for i, line in enumerate(lines[:MAX_SCAN]):
        fmt = SIGNATURES.get(_norm(line))
        if not fmt:
            continue
        det = Detected(path=path, format=fmt, header_index=i)
        if fmt == FORMAT_A:
            _parse_preamble(lines[:i], det)
        return det
    raise UnknownFormat(
        f"no known header signature in the first {MAX_SCAN} lines of {path}"
    )


def _parse_preamble(head: list[str], det: Detected) -> None:
    """Read account identity from the lines above the header.

    Account:, 10000000001, [Easy Account]              -> cheque
    Account:, 10000000002, [Savings Account]           -> savings
    Account:, 400000******0001, [FNB Aspire Credit Account] -> card
    Balance:, 2215.40, 615.25     -> closing / available
    Balance:, -1834.52, 915       -> current owing / available CREDIT
    (synthetic values; the layout is FNB's)
    """
    for line in head:
        parts = [p.strip() for p in line.split(",")]
        if not parts:
            continue
        if parts[0] == "Account:" and len(parts) >= 3:
            det.account_number = parts[1]
            kind = parts[2].strip("[]").lower()
            # 2026-09-24 (D6): "savings" is its own kind. Folding it into cheque
            # queued the savings account's rows for the cheque TRANSACTIONS sheet.
            if "credit" in kind:
                det.account_kind = "card"
            elif "savings" in kind:
                det.account_kind = "savings"
            else:
                det.account_kind = "cheque"
        elif parts[0] == "Balance:" and len(parts) >= 3:
            try:
                det.closing_balance = Decimal(parts[1])
                det.available = Decimal(parts[2])
            except InvalidOperation:
                pass
