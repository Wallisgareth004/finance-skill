#!/usr/bin/env python3
"""parse.py — FNB export parsers.

IN   a Detected FNB native export
OUT  [Txn] in file order (newest first), balances intact, unsettled head row
     marked

═══ WHY NOT THE csv MODULE (Format A) ═══
FNB does no quoting at all, and a handful of rows in every real Format-A export
contain unquoted commas:
    • SA decimal separators — an interest-rate row like `CR.INT.RATE   3,25000`
    • merchant names        — a card merchant like `SEASIDE TRADING,LDA`
    (synthetic examples of the real shapes)
csv.DictReader with 4 fieldnames pushes the overflow into restkey and SILENTLY
TRUNCATES the description at the comma. Because Description is the LAST column,
`line.split(',', 3)` is both correct and simpler. Do not "simplify" this back.

═══ WHY ROW ORDER IS SACRED ═══
Format A is newest-posting-first and the dates are NOT monotonic — they jump
backwards and forwards within a file. The BALANCE CHAIN, not the date, defines
the sequence:
    balance[i] == balance[i+1] + amount[i]
Sorting by date destroys it (which is exactly what happened when these rows were
pasted into the Excel model — every "break" in that block was an artifact of
re-sorting, not bad data). verify.check_chain() must run before any sort.

═══ WHY MERCHANTS ARE MATCHED BY PREFIX ═══
Descriptions are FIXED-WIDTH and FNB hard-truncates the merchant:
    cheque POS: merchant[0:20] · card mask[20:31] · posting date[33:39]
    card:       merchant[0:24] · location[24:40]
A foreign-currency purchase prefixes the original amount INTO the same 20-char
field, cutting the merchant to as few as ~11 characters. A truncated string
such as `Harbour Market C` is the COMPLETE merchant that will ever be seen.
Never match on equality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import detect

# "400000*0001  09 AUG" at end of a cheque POS description
CARD_MASK_RE = re.compile(r"\d{6}\*\d{4}\s+\d{2}\s+[A-Za-z]{3}\s*$")
# leading foreign/rand amount, e.g. "210.00 HARBOUR PHAR"
LEADING_AMOUNT_RE = re.compile(r"^\s*[\d,]+\.\d{2}\s+")
# informational rows carrying an interest RATE, not a transaction
NON_TXN_RE = re.compile(r"^CR\.INT\.RATE", re.I)


@dataclass
class Txn:
    """One parsed transaction — the data contract for the whole pipeline.

    date         posting date from the export (Format A is YYYY/MM/DD)
    amount       SIGNED. Debits negative, credits positive, on BOTH accounts.
                 Decimal, never float — the balance chain needs exact arithmetic.
    balance      running balance AFTER this transaction, cheque only. None on the
                 credit card, whose Balance column is the literal string "0".
    description  the raw narrative, verbatim. Never normalised — it is what the
                 categorisation rules match against and what lands in the sheet.
    merchant     sliced out of the fixed-width description (see _merchant).
                 Currently informational: the rules match on `description`.
    account      account number from the export preamble.
    source_slug  provenance tag, `export-YYYY-MM-DD_to_MM-DD`. Also the dedupe key.
    raw          the original CSV line, for debugging a bad parse.
    is_transaction  False for informational rows (CR.INT.RATE carries a RATE, not
                 a movement). Callers MUST filter on this before writing or summing.

    category / rule_id / confidence are filled in later by
    categorise.Ruleset.apply(). writer.append_transactions() requires `category`,
    so a Txn that has not been through the rules engine cannot be written.
    """

    date: date
    amount: Decimal
    balance: Decimal | None
    description: str
    merchant: str
    account: str
    source_slug: str
    raw: str = ""
    is_transaction: bool = field(default=True)
    # set by categorise.Ruleset.apply()
    category: str = ""
    rule_id: str | None = None
    confidence: str = "none"
    held: bool = False  # D13: waiting for the owner's label (categorise.HELD)


def _slug(txns: list) -> str:
    """Model convention: `export-YYYY-MM-DD_to_MM-DD` — year on the START date
    only. DASHBOARD!B67 matches this string literally, so the shape matters.
    """
    if not txns:
        return "export-empty"
    lo = min(t.date for t in txns)
    hi = max(t.date for t in txns)
    return f"export-{lo.isoformat()}_to_{hi.strftime('%m-%d')}"


def parse_file(det: "detect.Detected") -> list:
    if det.format != detect.FORMAT_A:
        raise NotImplementedError(
            f"{det.format} is archival and is not imported — only FORMAT_A. "
            "See spec §5: ~8.4% of PDF-derived descriptions are unrecoverable."
        )
    lines = detect._read_lines(det.path)
    out: list = []
    for line in lines[det.header_index + 1 :]:
        if not line.strip():
            continue
        parts = line.split(",", 3)  # maxsplit=3 — Description is the last column
        if len(parts) != 4:
            continue
        d_s, a_s, b_s, desc = (p.strip() for p in parts)
        try:
            d = datetime.strptime(d_s, "%Y/%m/%d").date()
            amount = Decimal(a_s)
        except (ValueError, InvalidOperation):
            continue  # not a data row

        balance = None
        if det.account_kind in detect.BALANCED_KINDS:
            try:
                balance = Decimal(b_s)
            except InvalidOperation:
                balance = None

        out.append(
            Txn(
                date=d,
                amount=amount,
                balance=balance,
                description=desc,
                merchant=_merchant(desc, det.account_kind),
                account=det.account_number or "",
                source_slug="",
                raw=line,
                is_transaction=not NON_TXN_RE.match(desc),
            )
        )

    _null_unsettled_head(out, det)

    slug = _slug(out)
    for t in out:
        t.source_slug = slug
    return out


def _null_unsettled_head(out: list, det: "detect.Detected") -> None:
    """Drop the balance on a single leading UNSETTLED row, proven from the chain.

    FNB puts not-yet-settled authorisations at the head of a cheque export with
    `Balance` printed as 0.00 and, sometimes, a posting date one day in the
    future (seen on a real export: a prepaid-airtime row dated tomorrow). Such a row
    carries no running balance, so reading the literal 0.00 as one breaks the
    chain and halts an otherwise clean import.

    Deciding this from the clock (`date > today`) was tried and reverted — it made
    parsing NON-DETERMINISTIC (the same bytes passed one day and failed the
    next), it missed pending rows dated *today*, and because it could null a row
    anywhere in the file it let an interior null split the chain into segments
    whose levels were never reconciled — hiding a real discrepancy in testing.

    So: prove it instead. Null row 0 ONLY when row 1's balance equals the
    preamble's `Balance:` — i.e. row 1 is demonstrably the newest settled row, so
    row 0 must sit above the settled ledger. Deterministic, head-only, and it
    cannot mask an interior break.
    """
    if getattr(det, "account_kind", None) not in detect.BALANCED_KINDS:
        return
    if len(out) < 2 or out[0].balance != 0:
        return
    if det.closing_balance is None:
        return
    if out[1].balance == det.closing_balance:
        out[0].balance = None


def _merchant(desc: str, kind: str | None) -> str:
    """Extract the merchant from a fixed-width description. See module docstring."""
    if kind == "card":
        return desc[:24].strip()
    if CARD_MASK_RE.search(desc):
        return LEADING_AMOUNT_RE.sub("", desc[:20]).strip()
    return LEADING_AMOUNT_RE.sub("", desc).strip()
