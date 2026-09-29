#!/usr/bin/env python3
"""pending.py — card spend that has not posted yet, from FNB's "Transaction history" PDF.

IN   a PDF from the drop folder
OUT  (header, [PendingTxn]) — or PendingFormatError / PendingUnavailable, never a guess

═══ WHY A PDF AT ALL ═══
FNB's CSV export holds POSTED transactions only. A card purchase is an
AUTHORISATION for a few days before it posts, and during those days it exists
only in online banking's "Transaction history" download, which is a PDF. D11,
2026-09-27: several days of real card spend sat in such a PDF in the drop and
were invisible to the pipeline.

═══ THE LAYOUT, AND WHY ROWS ARE PAIRED BY SEQUENCE ═══
pypdf's layout mode prints each authorisation as two lines:

    25 Sep 2026 16:47:51    400000******0001    SYNTH BOOKS AND TOYS
                                                                 750.00

A page break can carry a row's amount to the top of the next page, so rows are
paired by their order across the whole document: each description line with
the amount line that follows it. If the lines do not alternate exactly, the
file is REFUSED — a mispaired amount is a wrong number that looks right.

═══ PENDING IS NEVER POSTED SPEND ═══
These rows are reported apart, as "pending, not yet posted". They are never
added to spend or the deficit, and never written to the workbook: an
authorisation can still be reversed or change amount, and the posted row will
arrive in a later export anyway.

═══ ONCE A ROW POSTS ═══
drop_posted() retires a pending row when a posted row carries the same card
(first six and last four digits), the same amount, a compatible merchant name
AND the same purchase date — FNB prints the purchase date at the end of a posted
card narrative ("… 400000*0001  25 SEP"). Amount and merchant alone are not
enough: on real data a toll of one fixed price posts several times a week, and
an earlier toll is not the one authorised today.

pypdf is imported only when a PDF is read. Without it the PDF is listed as
ignored with that reason (run.py), never skipped in silence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

TITLE = "Transaction history"

# "25 Sep 2026 16:47:51   400000******0001   DESCRIPTION"
_ROW = re.compile(
    r"^\s*(\d{1,2} [A-Z][a-z]{2} \d{4}) (\d{2}:\d{2}:\d{2})\s+(\d{6}\*+\d{4})\s+(\S.*?)\s*$"
)
# an amount alone on its line; a leading minus is money IN (a refund)
_AMOUNT = re.compile(r"^\s*(-?[\d,]+\.\d{2})\s*$")
_ACCOUNT = re.compile(r"Account Number\s*:\s*(\S+)")
# the purchase date and card at the end of a POSTED cheque card narrative
_POSTED_CARD = re.compile(r"(\d{6})\*(\d{4})\s+(\d{2})\s+([A-Z]{3})\s*$")


class PendingFormatError(Exception):
    """The PDF is not a Transaction history this module can read exactly."""


class PendingUnavailable(Exception):
    """pypdf is not installed, so the PDF could not be read at all."""


@dataclass
class PendingTxn:
    """One authorisation. `amount` is SIGNED like every other row here:
    money out is negative. `category` is filled in by the rules engine."""

    date: date
    time: str
    card: str
    description: str
    amount: Decimal
    category: str = ""
    rule_id: str | None = None
    confidence: str = "none"
    is_transaction: bool = True


def _layout_lines(path: str) -> list:
    try:
        import pypdf
    except ImportError as exc:
        raise PendingUnavailable(
            "pypdf is not installed, so pending card spend in this PDF was NOT read "
            "(pip install pypdf)"
        ) from exc
    try:
        reader = pypdf.PdfReader(path)
        pages = [
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages
        ]
    except Exception as exc:  # a damaged or encrypted PDF: say so, never guess
        raise PendingFormatError(
            f"the PDF could not be read ({type(exc).__name__})"
        ) from exc
    return [line for page in pages for line in page.splitlines() if line.strip()]


def read_pdf(path: str) -> tuple:
    """(header, rows) from a Transaction history PDF, or an exception that says why not."""
    lines = _layout_lines(path)
    if not lines or lines[0].strip() != TITLE:
        raise PendingFormatError(f'not an FNB "{TITLE}" download')
    header = {"account": None}
    for line in lines[:12]:
        m = _ACCOUNT.search(line)
        if m:
            header["account"] = m.group(1)

    # The header ends at the "Amount" column title. After it, every line must be
    # a row or an amount: a fragment that is neither (a mangled page split one
    # row in two) would otherwise drop a row while the rest still paired up.
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Amount") + 1
    except StopIteration as exc:
        raise PendingFormatError("no column header (Date and Time … Amount)") from exc
    sequence = []
    for n, line in enumerate(lines[start:], start + 1):
        m = _ROW.match(line)
        if m:
            sequence.append(("row", m.groups()))
            continue
        m = _AMOUNT.match(line)
        if m:
            sequence.append(("amount", m.group(1)))
            continue
        raise PendingFormatError(f"line {n} is neither an authorisation nor an amount")
    kinds = [kind for kind, _ in sequence]
    if kinds != ["row", "amount"] * (len(kinds) // 2):
        raise PendingFormatError(
            f"{kinds.count('row')} authorisations but {kinds.count('amount')} amounts, "
            "or out of order — refusing to pair them"
        )

    rows = []
    for (_, (day, clock, card, description)), (_, amount) in zip(
        sequence[::2], sequence[1::2]
    ):
        try:
            value = Decimal(amount.replace(",", ""))
            when = datetime.strptime(day, "%d %b %Y").date()
        except (InvalidOperation, ValueError) as exc:
            raise PendingFormatError(f"unreadable row: {exc}") from exc
        rows.append(PendingTxn(when, clock, card, description, -value))
    return header, rows


def _merchant_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _same_merchant(pending_desc: str, posted_desc: str) -> bool:
    """FNB cuts a posted merchant to 20 characters, so compare on the shorter:
    one must be a prefix of the other, over at least 6 letters or digits."""
    a, b = _merchant_key(pending_desc), _merchant_key(posted_desc[:20])
    n = min(len(a), len(b))
    return n >= 6 and a[:n] == b[:n]


def drop_posted(rows: list, posted: list) -> tuple:
    """(still pending, number retired). `posted` is any rows with date,
    description and amount (sheet rows or parsed exports). Each posted row
    retires at most one pending row."""
    used, still, retired = set(), [], 0
    for row in rows:
        first6, last4 = row.card[:6], row.card[-4:]
        stamp = row.date.strftime("%d %b").upper()
        match = None
        for i, p in enumerate(posted):
            if i in used or Decimal(str(p.amount)) != row.amount:
                continue
            m = _POSTED_CARD.search(p.description or "")
            if not m or (m.group(1), m.group(2)) != (first6, last4):
                continue
            if f"{m.group(3)} {m.group(4)}" != stamp:
                continue
            if _same_merchant(row.description, p.description):
                match = i
                break
        if match is None:
            still.append(row)
        else:
            used.add(match)
            retired += 1
    return still, retired
