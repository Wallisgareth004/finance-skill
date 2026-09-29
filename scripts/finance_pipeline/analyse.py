#!/usr/bin/env python3
"""analyse.py — CFO, analyst and advisor. Pure functions over a reconciled ledger.

IN   reconciled rows (the cheque sheet, incoming rows, card rows), a window,
     the gap register
OUT  the CFO, analyst and advisor results as plain dicts; month_table();
     idle_cash()

═══ THE REFUSAL IS THE PRODUCT ═══
The most important behaviour here is declining to answer. The shape, as found
in the owner's real model: a window with a hole in it. Dividing the window's
spend by its ELAPSED days instead of its COVERED days claims coverage that does
not exist and understates the monthly burn by the size of the hole. The same
bug was live in three places at once: a dashboard formula
(`=(D21-B21+1)/30.44`), a sibling pipeline's week counter, and any naive read
of a by-month sheet.

Spend in `cfo()` is narrower than the window's raw debits, because savings sweeps
and isolated one-off trips are excluded from the run-rate. Quote whichever
figure you mean, and say which.

═══ INCOME IS A DEFINITION, NOT AN INFERENCE ═══
The workbook's own notes: "Only the annuity counts as income. Savings transfers,
crypto sales and marketplace cash are asset conversions, NOT income." Booking
self-transfers as income is spec §3.3 — on the owner's data they inflated the
income line badly before this was pinned.

Income is the LAST annuity credit, never a windowed average: averaging a lumpy
annuity over a window that is not a whole number of months understates it.
"Last" means every
credit on the latest annuity DATE summed (a split payment is still one month's
income), and the result is sanity-checked against the median of earlier credits —
a small adjustment posted a day later must not become the income line.

═══ CREDITS INSIDE SPEND CATEGORIES ═══
The owner's real window held POSITIVE amounts in spend
categories — crypto sales, a cash deposit, an incoming transfer. Discarding them
overstated the monthly burn; netting all of them would understate it, because an
asset conversion is not a refund. So:

  · credits in ASSET_CONVERSION categories are reported separately, never netted
  · credits in any other NAMED spend category ARE netted (a refund or a reversal)
  · an UNCATEGORISED credit is never netted — it is quarantined (D3, below)
  · every total is always emitted, so none can hide

═══ RUNWAY CARRIES ITS ASSUMPTIONS OR IT IS NOT EMITTED ═══
A workbook runway formula divided a superseded balance, hardcoded into its
formula string, by the deficit — no inflation, no growth, no tax, no annuity
escalation — and presented the result as months of survival. runway() refuses
that base (_SUPERSEDED_BASE) and states every assumption beside its answer.

A cash runway is driven by CASH_BURN, not by the run-rate deficit. Savings
sweeps are excluded from the deficit because they are not consumption — but
they are real rand leaving the cheque account, so they belong in anything that
answers "how long until the account is empty".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import trips as trips_mod

DAYS_PER_MONTH = Decimal("30.44")
_CENTS = Decimal("0.01")

INCOME_CATEGORY = "Income — annuity"

# Debits that are not consumption. "Savings transfers" and
# "Savings (Bank Your Change)" are DIFFERENT categories and must never be
# conflated. Excluded from the deficit, INCLUDED in cash burn.
NON_SPEND = frozenset({"Savings (Bank Your Change)", "Savings transfers"})

# Reported separately so a one-off trip never becomes a run-rate (NOTES & GAPS!B26).
ISOLATED = frozenset({"Travel — trip"})

# D5, 2026-09-24: crypto in BOTH directions is an asset conversion. Only the sell
# side obeyed that before — every buy was a debit and landed in spend (a real
# report listed crypto buys under "Spend by category"). Now bought
# and sold go to one flow line; neither is spend, neither is income.
CRYPTO = "Crypto / investments"

# Credits here are asset conversions, not refunds. Never netted against spend.
ASSET_CONVERSION = frozenset({"People / transfers", "Cash / ATM", "Income — other"})

# ── D3, 2026-09-24: money that moved but was neither earned nor consumed ──────
# Before this, `_split` treated every credit outside ASSET_CONVERSION as a refund
# and netted it inside its category. One large unlabelled one-off credit netted
# `Uncategorised` deeply negative and printed a surplus as a deficit.
CAPITAL = frozenset({"Capital — one-off inflow"})
# Cheque → own card. Out of the deficit, INTO cash burn (it left the account).
TRANSFER = frozenset({"Transfer — card settlement"})
# A bounced debit order coming back. Paired with the debit it reverses; both leave
# spend. Unpaired, it is an unexplained credit and is quarantined.
REVERSAL = frozenset({"Reversal — unpaid debit"})
REVERSAL_LOOKBACK_DAYS = 10
# A credit nobody has named. Never netted. Reported, and past a threshold it blocks
# the deficit: an unknown credit might be income, and then the deficit is wrong by
# up to its size.
UNCATEGORISED = frozenset({"Uncategorised", ""})
# 5% of gross spend in the window — the same bar as the uncategorised-rows target.
# Below it, the worst case (every quarantined rand was really income) moves the
# deficit by less than 5% of spend; above it the figure is not defensible.
QUARANTINE_LIMIT = Decimal("0.05")
# Unlabelled DEBITS stay in spend (real money left). But above this share of
# spend the deficit is marked PROVISIONAL, with the bound stated: labelling could
# move it by up to their per-month total (2026-09-24, item 8 of the fact-check).
PROVISIONAL_LIMIT = Decimal("0.05")

# Listed on SUBSCRIPTIONS as a cost; NOTES & GAPS!B12 says it is "NOT a cost —
# your own savings pocket". Cutting it saves nothing.
NOT_A_COST = frozenset({"Bank Your Change"})

# SUBSCRIPTIONS!E is free text, not an enum. Values seen on the real sheet:
# CUT · CUT or go annual · PAUSE · SHOP IT · VERIFY · CALL <a number> · KEEP ·
# LOCKED KEEP · YOUR CALL · KEEP — <a caveat> · REVIEW — <a caveat>.
# Exact-matching "CUT" collapsed the owner's decided annual saving to a fraction
# of its real size, silently dropping the two largest lines (one marked
# "CALL …", one "PAUSE"). Anything not clearly KEEP or clearly CUT is
# surfaced as UNDECIDED rather than scored either way.
_DECIDED_CUT = ("CUT", "PAUSE", "CANCEL")
_DECIDED_KEEP = ("KEEP",)

# The confirmed figure, from a signed document; the runway refusal quotes it.
CONFIRMED_BASE = Decimal("1450000.00")
# A stale figure hardcoded into a workbook formula. Refused with a tolerance
# because the spelling varies between float, str and Decimal at call sites.
_SUPERSEDED_BASE = Decimal("1234567.89")
_SUPERSEDED_TOLERANCE = Decimal("1")


class AnalysisError(Exception):
    """Raised rather than emitting a number that would be quoted as canon."""


class _Unchecked:
    """Sentinel: the gap register was never consulted.

    An empty list means "checked, and there are no gaps". Omitting the argument
    used to be indistinguishable from that, and produced a confident deficit at
    100% coverage. Now it refuses.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNCHECKED"


UNCHECKED = _Unchecked()


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _amount(txn) -> Decimal:
    """Every amount that reaches arithmetic is finite. model.py guards this on
    the way in; nothing guarded it here, so Infinity in a spend category was
    silently dropped and NaN raised InvalidOperation from a comparison."""
    value = txn.amount
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AnalysisError(f"amount is not a number: {txn.amount!r}") from exc
    if not value.is_finite():
        raise AnalysisError(
            f"amount is not a finite number: {txn.amount!r} "
            f"({getattr(txn, 'description', '')[:40]!r})"
        )
    return value


def _as_date(value) -> date:
    """openpyxl hands back datetime for every sheet row; the
    parser hands back date. Comparing the two raises TypeError, which made every
    sheet-derived caller crash — and the sheet is the only possible source of a
    previous period."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise AnalysisError(f"date is not a date: {value!r}")


@dataclass
class Window:
    start: date
    end: date

    def __post_init__(self):
        self.start = _as_date(self.start)
        self.end = _as_date(self.end)
        if self.end < self.start:
            raise AnalysisError(
                f"window ends before it starts: {self.start} → {self.end}"
            )

    @property
    def elapsed_days(self) -> int:
        """Inclusive, matching verify.Gap.days."""
        return (self.end - self.start).days + 1

    def contains(self, value) -> bool:
        return self.start <= _as_date(value) <= self.end

    def __str__(self) -> str:
        return f"{self.start.isoformat()} → {self.end.isoformat()}"


@dataclass
class Coverage:
    window: Window
    elapsed_days: int
    covered_days: int
    intersecting: list = field(default_factory=list)

    def __post_init__(self):
        if self.elapsed_days < 1:
            raise AnalysisError(f"elapsed_days must be >= 1, got {self.elapsed_days}")
        if not 0 <= self.covered_days <= self.elapsed_days:
            raise AnalysisError(
                f"covered_days {self.covered_days} outside 0..{self.elapsed_days}. "
                "A Coverage built by hand bypasses the gap register — build it "
                "with coverage()."
            )

    @property
    def blocked(self) -> bool:
        return bool(self.intersecting)

    @property
    def coverage_pct(self) -> float:
        return self.covered_days / self.elapsed_days * 100


def coverage(window: Window, gaps) -> Coverage:
    """How much of `window` the data actually covers.

    Missing days are counted as a SET, so overlapping gaps never subtract the
    same day twice, and a gap hanging over either edge is clipped to the part
    inside the window.
    """
    if gaps is UNCHECKED:
        raise AnalysisError(
            "the gap register was not consulted. Pass the register — an empty "
            "list means 'checked, none found', which is a different claim."
        )
    if not isinstance(gaps, (list, tuple)):
        raise AnalysisError(f"gaps must be a list, got {type(gaps).__name__}")

    missing: set[date] = set()
    touching = []
    for gap in gaps:
        if gap is None:
            raise AnalysisError("gap register contains None")
        start, end = _as_date(gap.start), _as_date(gap.end)
        if end < start:
            # verify.find_gaps(min_days=0) genuinely emits these. An inverted gap
            # marks the window blocked while subtracting range(negative) = zero
            # days, so allow_blocked then returns the exact elapsed-day answer
            # this module exists to refuse.
            raise AnalysisError(f"gap ends before it starts: {start} → {end}")
        if not gap.intersects(window.start, window.end):
            continue
        touching.append(gap)
        lo, hi = max(start, window.start), min(end, window.end)
        for offset in range((hi - lo).days + 1):
            missing.add(date.fromordinal(lo.toordinal() + offset))

    return Coverage(
        window=window,
        elapsed_days=window.elapsed_days,
        covered_days=window.elapsed_days - len(missing),
        intersecting=touching,
    )


def per_month(total: Decimal, cov: Coverage, allow_blocked: bool = False) -> Decimal | None:
    """Scale a windowed total to a monthly rate — on COVERED days.

    Returns None when the window is blocked, unless the caller explicitly
    accepts a partial-coverage figure. `allow_blocked` exists so a report can
    show "this is what it would be, on the days we have" beside the refusal; it
    must never be defaulted on.
    """
    if cov.blocked and not allow_blocked:
        return None
    if cov.covered_days <= 0:
        return None
    return _q(total * DAYS_PER_MONTH / cov.covered_days)


def runway(
    base,
    deficit_per_month,
    variant: str,
    inflation: Decimal = Decimal("0"),
    growth: Decimal = Decimal("0"),
    tax_included: bool = False,
) -> dict:
    """Months of cover, with every assumption stated on the face of the result.

    Never a bare number, and never the string "never depletes" in a numeric
    field — DASHBOARD!D18:D20 does that today, so any downstream float() or SUM
    over that column breaks.
    """
    if base is not None:
        try:
            base = base if isinstance(base, Decimal) else Decimal(str(base))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AnalysisError(f"runway base is not a number: {base!r}") from exc
        if not base.is_finite():
            raise AnalysisError(f"runway base is not finite: {base!r}")
        if abs(base - _SUPERSEDED_BASE) < _SUPERSEDED_TOLERANCE:
            raise AnalysisError(
                f"R{base} is a superseded figure hardcoded into a workbook "
                f"formula. The confirmed figure is R{CONFIRMED_BASE}. "
                "Refusing to compute a runway on money that will not arrive."
            )

    assumptions = {
        "inflation": inflation,
        "growth": growth,
        "tax_included": tax_included,
        "deficit_variant": variant,
        "base": base,
    }
    if base is None:
        return {"months": None, "verdict": "unknown-base", "assumptions": assumptions}
    if deficit_per_month is None:
        return {"months": None, "verdict": "blocked", "assumptions": assumptions}
    if deficit_per_month >= 0:
        return {"months": None, "verdict": "surplus", "assumptions": assumptions}
    return {
        "months": _q(base / -deficit_per_month),
        "verdict": "depleting",
        "assumptions": assumptions,
    }


def _account(txn) -> str:
    """'card' for a card row (D12); 'cheque' for everything else."""
    return "card" if getattr(txn, "account", "") == "card" else "cheque"


def _spend_by_account(rows: list, isolate: dict | None = None) -> dict:
    """Net spend per account, by the same rules as the total."""
    out = {}
    for acct in sorted({_account(t) for t in rows}):
        net = _net_spend(_split([t for t in rows if _account(t) == acct], isolate))
        out[acct] = _q(sum(net.values(), Decimal("0")))
    return out


def _movements(txns: list) -> list:
    return [t for t in txns if getattr(t, "is_transaction", True)]


def _in_window(txns: list, window: Window) -> list:
    return [t for t in txns if window.contains(t.date)]


@dataclass
class Split:
    """Every rand in the window, assigned to exactly one bucket. Nothing is
    dropped, so nothing can silently vanish."""

    spend: dict = field(default_factory=dict)
    credits_netted: dict = field(default_factory=dict)
    asset_conversions: dict = field(default_factory=dict)
    isolated: dict = field(default_factory=dict)
    non_spend: dict = field(default_factory=dict)
    income_rows: list = field(default_factory=list)
    capital: dict = field(default_factory=dict)
    crypto_bought: Decimal = Decimal("0")
    crypto_sold: Decimal = Decimal("0")
    transfers: dict = field(default_factory=dict)
    reversed_debits: list = field(default_factory=list)
    quarantined: list = field(default_factory=list)


def _split(txns: list, isolate: dict | None = None) -> Split:
    """`isolate` (D14) maps id(row) -> an isolated-bucket name, for trip rows."""
    out = Split()
    debits = []  # (txn, category, amount) — candidates a reversal can cancel
    reversals = []
    for txn in txns:
        amount = _amount(txn)
        category = getattr(txn, "category", "") or ""

        trip = (isolate or {}).get(id(txn))
        if trip and amount < 0:
            out.isolated[trip] = out.isolated.get(trip, Decimal("0")) - amount
            continue

        if category == CRYPTO:
            if amount < 0:
                out.crypto_bought -= amount
            else:
                out.crypto_sold += amount
            continue

        if category in CAPITAL:
            out.capital[category] = out.capital.get(category, Decimal("0")) + amount
            continue

        if category in TRANSFER:
            out.transfers[category] = out.transfers.get(category, Decimal("0")) - amount
            continue

        if category in REVERSAL:
            if amount > 0:
                reversals.append(txn)
            else:
                out.quarantined.append(txn)
            continue

        if category in UNCATEGORISED and amount > 0:
            out.quarantined.append(txn)
            continue

        if category == INCOME_CATEGORY:
            if amount > 0:
                out.income_rows.append(txn)
            continue

        if category in ISOLATED:
            if amount < 0:
                out.isolated[category] = (
                    out.isolated.get(category, Decimal("0")) - amount
                )
            continue

        if category in NON_SPEND:
            if amount < 0:
                out.non_spend[category] = (
                    out.non_spend.get(category, Decimal("0")) - amount
                )
            continue

        if amount < 0:
            out.spend[category] = out.spend.get(category, Decimal("0")) - amount
            debits.append((txn, category, -amount))
        elif amount > 0:
            if category in ASSET_CONVERSION:
                out.asset_conversions[category] = (
                    out.asset_conversions.get(category, Decimal("0")) + amount
                )
            else:
                # A refund or a reversal genuinely reduces what was spent.
                out.credits_netted[category] = (
                    out.credits_netted.get(category, Decimal("0")) + amount
                )
    _pair_reversals(out, reversals, debits)
    return out


def _pair_reversals(out: Split, reversals: list, debits: list) -> None:
    """Cancel each reversal against the debit it reverses.

    Same amount, dated 0–REVERSAL_LOOKBACK_DAYS before the credit, closest first,
    each debit used once. The real shape: a debit order bounces, its MAGTAPE
    UNPAID reversal lands the same day, and the order is paid again a week
    later. Without pairing, the month carries two payments for one month of
    service.
    """
    used: set = set()
    for rev in sorted(reversals, key=lambda t: _as_date(t.date)):
        amount = _amount(rev)
        when = _as_date(rev.date)
        best = None
        for idx, (txn, category, debit) in enumerate(debits):
            if idx in used or debit != amount:
                continue
            lag = (when - _as_date(txn.date)).days
            if 0 <= lag <= REVERSAL_LOOKBACK_DAYS and (best is None or lag < best[0]):
                best = (lag, idx)
        if best is None:
            out.quarantined.append(rev)
            continue
        used.add(best[1])
        txn, category, debit = debits[best[1]]
        out.spend[category] -= debit
        out.reversed_debits.append(
            {
                "category": category,
                "amount": _q(debit),
                "debit_date": _as_date(txn.date),
                "reversal_date": when,
                "description": getattr(txn, "description", ""),
            }
        )


def _net_spend(split: Split) -> dict:
    net = dict(split.spend)
    for category, credit in split.credits_netted.items():
        net[category] = net.get(category, Decimal("0")) - credit
    return {k: _q(v) for k, v in net.items()}


def _income(split: Split) -> tuple:
    """The last annuity credit — every credit on the latest annuity DATE, summed.

    Returns (amount, note). `max(by date)` alone let list order decide between
    two same-day credits: a small adjustment beside the annuity flipped the
    verdict from surplus to depleting on identical data.
    """
    if not split.income_rows:
        return None, None
    latest = max(_as_date(t.date) for t in split.income_rows)
    same_day = [t for t in split.income_rows if _as_date(t.date) == latest]
    total = _q(sum((_amount(t) for t in same_day), Decimal("0")))

    earlier = sorted(
        _amount(t) for t in split.income_rows if _as_date(t.date) != latest
    )
    if len(earlier) >= 3:
        median = earlier[len(earlier) // 2]
        if median > 0 and total < median / 2:
            return None, (
                f"the latest {INCOME_CATEGORY} credit is R{total} against a "
                f"median of R{median} — implausible as a month's income. "
                "Check for a split or adjustment posting."
            )
    return total, None


def pending_block(rows: list) -> dict:
    """D11: card authorisations not yet posted, summarised apart from spend.

    Money out only (an authorisation is a debit); a pending refund would be a
    credit and is left out of the total rather than netted."""
    out = [r for r in rows if _amount(r) < 0]
    by_category: dict = {}
    for r in out:
        cat = getattr(r, "category", "") or "Uncategorised"
        by_category[cat] = by_category.get(cat, Decimal("0")) - _amount(r)
    return {
        "rows": len(out),
        "total": _q(sum((-_amount(r) for r in out), Decimal("0"))),
        "from": min((_as_date(r.date) for r in out), default=None),
        "to": max((_as_date(r.date) for r in out), default=None),
        "by_category": {k: _q(v) for k, v in sorted(by_category.items())},
    }


def cfo(
    txns: list,
    window: Window,
    cash: dict,
    today: date,
    gaps=UNCHECKED,
    itemised: list | None = None,
    pending: list | None = None,
    trips: list | None = None,
) -> dict:
    """Can we cover what is coming? Control, not commentary.

    `pending` (D11) is card spend authorised but not yet posted. It is
    summarised apart and never enters spend, the deficit or the runway."""
    today = _as_date(today)
    cov = coverage(window, gaps)
    rows = _in_window(_movements(txns), window)
    for row in rows:
        _amount(row)  # refuse NaN / Infinity before anything compares amounts
    # D14: trip rows found by date range; isolated ones leave spend here.
    trip_of = trips_mod.assign(trips or [], rows)
    isolate = {i: f"Trip — {t.label}" for i, t in trip_of.items() if t.isolate}
    split = _split(rows, isolate)

    net = _net_spend(split)
    spend = _q(sum(net.values(), Decimal("0")))
    non_spend_total = _q(sum(split.non_spend.values(), Decimal("0")))
    isolated = {k: _q(v) for k, v in split.isolated.items()}
    conversions = {k: _q(v) for k, v in split.asset_conversions.items()}

    income, income_note = _income(split)

    transfers = {k: _q(v) for k, v in split.transfers.items()}
    transfers_total = _q(sum(transfers.values(), Decimal("0")))
    quarantined_total = _q(
        sum((_amount(t) for t in split.quarantined), Decimal("0"))
    )
    crypto_net_out = _q(max(split.crypto_bought - split.crypto_sold, Decimal("0")))
    gross_spend = _q(sum(split.spend.values(), Decimal("0")))
    quarantine_blocks = quarantined_total > _q(gross_spend * QUARANTINE_LIMIT)

    spend_pm = per_month(spend, cov)
    # D12, 2026-09-27: card purchases are spend like any other purchase. With
    # them counted, the cheque-to-card settlement moves the same money again,
    # between the owner's own accounts, so it leaves cash burn too. Without any
    # card data it stays in: then it is the only trace of the card's spend.
    card_in_window = any(_account(t) == "card" for t in rows)
    burn_transfers = Decimal("0") if card_in_window else transfers_total
    # Cash burn includes savings sweeps and transfers: they are excluded from the
    # run-rate because they are not consumption, but they are real rand leaving
    # the cheque account, so they belong in "how long until it is empty".
    cash_burn_pm = per_month(
        _q(
            spend
            + non_spend_total
            + burn_transfers
            + crypto_net_out
            + sum(isolated.values(), Decimal("0"))
        ),
        cov,
    )
    deficit = (
        None
        if (spend_pm is None or income is None or quarantine_blocks)
        else _q(income - spend_pm)
    )
    cash_deficit = (
        None
        if (cash_burn_pm is None or income is None or quarantine_blocks)
        else _q(income - cash_burn_pm)
    )

    blocked_by = [str(g) for g in cov.intersecting]
    remedy = ""
    if blocked_by:
        first = cov.intersecting[0]
        remedy = (
            f"Export FNB for {first.start.isoformat()} → {first.end.isoformat()} "
            "before trusting any figure over this window."
        )

    if income is None:
        reason = income_note or f"no {INCOME_CATEGORY} credit inside {window}"
        blocked_by = blocked_by + [reason]
        if not remedy:
            remedy = (
                f"{reason} Widen the window or export the missing statement — "
                "do not read this as zero income."
            )

    unlabelled = [
        t
        for t in rows
        if (getattr(t, "category", "") or "") in UNCATEGORISED and _amount(t) < 0
    ]
    unlabelled_spend = _q(net.get("Uncategorised", Decimal("0")) + net.get("", Decimal("0")))
    unlabelled_pm = (
        _q(unlabelled_spend * DAYS_PER_MONTH / cov.covered_days)
        if cov.covered_days
        else None
    )
    is_provisional = bool(spend > 0 and unlabelled_spend > _q(spend * PROVISIONAL_LIMIT))
    # D13: rows the owner has been asked to label are unlabelled on purpose.
    # Stated apart, so a reader can see how much is waiting on him and how
    # much the rules still miss.
    held = [t for t in unlabelled if getattr(t, "held", False)]
    held_spend = _q(sum((-_amount(t) for t in held), Decimal("0")))

    if quarantine_blocks:
        reason = (
            f"R{quarantined_total} of uncategorised credits in the window — more "
            f"than {int(QUARANTINE_LIMIT * 100)}% of R{gross_spend} gross spend. An "
            "unnamed credit might be income, capital or a transfer; each changes "
            "the answer."
        )
        blocked_by = blocked_by + [reason]
        if not remedy:
            remedy = "Label the quarantined credits in rules.yaml, then re-run."

    available = cash.get("available")
    headroom = cash.get("card_headroom")
    if available is None or headroom is None:
        accessible = None
    else:
        accessible = _q(Decimal(str(available)) + Decimal(str(headroom)))

    itemised = itemised or []
    residual_amount = _q(
        sum((v for k, v in net.items() if k not in itemised), Decimal("0"))
    )

    return {
        "window": str(window),
        "today": today,
        "coverage": {
            "elapsed_days": cov.elapsed_days,
            "covered_days": cov.covered_days,
            "coverage_pct": round(cov.coverage_pct, 2),
            "gap_register_size": len(gaps),
            "gaps": blocked_by,
        },
        "rows_counted": len(rows),
        "income": income,
        "income_note": income_note,
        "annuity_credits": len(split.income_rows),
        "spend": spend,
        "spend_is_partial": cov.blocked,
        "spend_by_category": net,
        "spend_by_account": _spend_by_account(rows, isolate),
        "card_settlement_in_cash_burn": not card_in_window,
        "credits_netted": {k: _q(v) for k, v in split.credits_netted.items()},
        "asset_conversions": conversions,
        "non_spend": {k: _q(v) for k, v in split.non_spend.items()},
        "isolated": isolated,
        "capital": {k: _q(v) for k, v in split.capital.items()},
        "transfers": transfers,
        "crypto_flow": {
            "bought": _q(split.crypto_bought),
            "sold": _q(split.crypto_sold),
            "net_out": crypto_net_out,
        },
        "reversed_debits": split.reversed_debits,
        "provisional": {
            "is_provisional": is_provisional,
            "rows": len(unlabelled),
            "uncategorised_spend": unlabelled_spend,
            "per_month": unlabelled_pm,
            "share_of_spend": (
                round(float(unlabelled_spend / spend) * 100, 1) if spend > 0 else None
            ),
            "limit_pct": int(PROVISIONAL_LIMIT * 100),
            "held_rows": len(held),
            "held_spend": held_spend,
            "share_excluding_held": (
                round(float((unlabelled_spend - held_spend) / spend) * 100, 1)
                if spend > 0 else None
            ),
        },
        "quarantined_credits": {
            "total": quarantined_total,
            "limit": _q(gross_spend * QUARANTINE_LIMIT),
            "blocks_deficit": quarantine_blocks,
            "rows": [
                {
                    "date": _as_date(t.date),
                    "description": getattr(t, "description", ""),
                    "amount": _q(_amount(t)),
                }
                for t in split.quarantined
            ],
        },
        "spend_per_month": spend_pm,
        "cash_burn_per_month": cash_burn_pm,
        "deficit": deficit,
        "cash_deficit": cash_deficit,
        "deficit_blocked_by": blocked_by,
        "remedy": remedy,
        "accessible_cash": accessible,
        "runway": runway(
            base=accessible,
            deficit_per_month=cash_deficit,
            variant="cash",
            tax_included=False,
        ),
        "residual": {
            "amount": residual_amount,
            "categories": sorted(k for k in net if k not in itemised),
        },
        "pending": pending_block(pending or []),
        "trips": [_trip_block(t, rows, trip_of) for t in (trips or [])],
    }


def month_table(txns, window: Window, gaps, today, pending=None, trips=None) -> list:
    """F1, 2026-09-27: one row per calendar month inside `window`.

    Each row is cfo() over that month (clipped to the window), so every rule —
    the refusal, the quarantine, trips, card purchases — applies unchanged. A
    deficit is stated for COMPLETE months only: a month the window cuts short
    shows its spend to date, never a scaled-up monthly rate.

    Two deficits: `deficit` over all accounts (card purchases included, D12)
    and `deficit_cheque` over the cheque account alone, the definition the
    owner's earlier figures used, so those stay comparable.
    """
    out = []
    year, month = window.start.year, window.start.month
    while (year, month) <= (window.end.year, window.end.month):
        first = date(year, month, 1)
        last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
        lo, hi = max(first, window.start), min(last, window.end)
        part = Window(lo, hi)
        res = cfo(txns, part, cash={}, today=today, gaps=gaps, trips=trips)
        complete = lo == first and hi == last
        cheque = res["spend_by_account"].get("cheque", Decimal("0.00"))
        deficit = res["deficit"] if complete else None
        deficit_cheque = None
        if deficit is not None:
            deficit_cheque = _q(res["income"] - per_month(cheque, coverage(part, gaps)))
        # Pending is "now", not part of the posted window: an authorisation
        # dated after the newest posted row still belongs to its calendar month.
        mine = [p for p in (pending or []) if first <= _as_date(p.date) <= last and _amount(p) < 0]
        out.append(
            {
                "month": f"{year:04d}-{month:02d}",
                "from": lo,
                "to": hi,
                "complete": complete,
                "covered_days": res["coverage"]["covered_days"],
                "elapsed_days": res["coverage"]["elapsed_days"],
                "income": res["income"],
                "spend": res["spend"],
                "spend_cheque": cheque,
                "spend_card": res["spend_by_account"].get("card", Decimal("0.00")),
                "spend_by_category": res["spend_by_category"],
                "isolated": _q(sum(res["isolated"].values(), Decimal("0"))),
                "deficit": deficit,
                "deficit_cheque": deficit_cheque,
                "pending": _q(sum((-_amount(p) for p in mine), Decimal("0"))),
            }
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _trip_block(trip, rows: list, trip_of: dict) -> dict:
    """D14: one trip's cost, by category, and its net after reimbursement."""
    mine = [r for r in rows if trip_of.get(id(r)) is trip]
    by_category: dict = {}
    for r in mine:
        cat = getattr(r, "category", "") or "Uncategorised"
        by_category[cat] = by_category.get(cat, Decimal("0")) - _amount(r)
    cost = _q(sum((-_amount(r) for r in mine), Decimal("0")))
    paid = trips_mod.reimbursement(trip, rows)
    reimbursed = _q(_amount(paid)) if paid is not None else None
    return {
        "label": trip.label,
        "from": trip.start,
        "to": trip.end,
        "isolated": trip.isolate,
        "rows": len(mine),
        "cost": cost,
        "by_category": {k: _q(v) for k, v in sorted(by_category.items())},
        "reimbursed": reimbursed,
        "reimbursed_on": _as_date(paid.date) if paid is not None else None,
        "net": _q((reimbursed or Decimal("0")) - cost),
    }


def _merchant(txn) -> str:
    """parse.Txn.merchant strips the card mask and the posting date. Keying on
    the raw description instead produced false "new" and "stopped" merchants by
    the hundred in a single month, because most descriptions embed a mask and a
    date and are therefore unique by construction."""
    return getattr(txn, "merchant", None) or getattr(txn, "description", "")


def analyst(
    txns: list,
    prev: list,
    window: Window,
    prev_window: Window | None = None,
    gaps=UNCHECKED,
) -> dict:
    """What changed, and what drove it. Ranked by rand, never by percentage —
    a 300% rise on R12 is noise."""
    cov = coverage(window, gaps)
    now_rows = _in_window(_movements(txns), window)
    prev_rows = _movements(prev)
    if prev_window is not None:
        prev_rows = _in_window(prev_rows, prev_window)

    now_cat = _net_spend(_split(now_rows))
    prev_cat = _net_spend(_split(prev_rows))

    movements = []
    for category in set(now_cat) | set(prev_cat):
        delta = now_cat.get(category, Decimal("0")) - prev_cat.get(
            category, Decimal("0")
        )
        if delta:
            movements.append(
                {
                    "category": category,
                    "now": now_cat.get(category, Decimal("0")),
                    "prev": prev_cat.get(category, Decimal("0")),
                    "delta": delta,
                }
            )
    movements.sort(key=lambda m: abs(m["delta"]), reverse=True)

    now_names = {_merchant(t) for t in now_rows}
    prev_names = {_merchant(t) for t in prev_rows}

    # A gap makes every recurring debit inside it look cancelled.
    suppressed = cov.blocked
    stopped = [] if suppressed else sorted(prev_names - now_names)

    return {
        "movements": movements,
        "new_merchants": sorted(now_names - prev_names),
        "stopped_merchants": stopped,
        "stopped_suppressed_by_gap": suppressed,
        "coverage_pct": round(cov.coverage_pct, 2),
    }


def _decision_class(decision: str) -> str:
    text = (decision or "").strip().upper()
    if not text:
        return "undecided"
    if any(text.startswith(k) for k in _DECIDED_KEEP) or "LOCKED KEEP" in text:
        return "keep"
    if any(word in text for word in _DECIDED_CUT):
        return "cut"
    return "undecided"


def _rands(value: Decimal) -> str:
    return f"R{value:,.2f}"


_INTEREST = re.compile(r"\bINTEREST\b", re.I)


def idle_cash(available, rows: list, window: Window, settings) -> dict | None:
    """F4, 2026-09-27: cash above the owner's buffer, earning about nothing.

    `available` is the cheque account's available balance (from its export).
    Its earning rate is MEASURED: the interest credits in `rows` over the
    window, annualised, against the balance. None unless there is more than
    the buffer AND it earns less than `at_zero_below_pct` a year. The buffer,
    the rate it could earn elsewhere and the "about zero" line are the
    owner's, from a private file; without them there is no verdict.
    """
    if settings is None or available is None:
        return None
    available = Decimal(str(available))
    threshold = Decimal(str(settings["threshold"]))
    rate = Decimal(str(settings["rate_pct"]))
    zero_below = Decimal(str(settings.get("at_zero_below_pct", "1.0")))
    if available <= threshold:
        return None
    interest = sum(
        (_amount(r) for r in rows
         if _amount(r) > 0 and window.contains(r.date) and _INTEREST.search(r.description or "")),
        Decimal("0"),
    )
    earning = interest / available * Decimal(365) / window.elapsed_days * 100
    if earning >= zero_below:
        return None
    excess = available - threshold
    return {
        "available": _q(available),
        "threshold": threshold,
        "excess": _q(excess),
        "earning_pct": earning.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        "rate_pct": rate,
        "forgone_per_month": _q(excess * rate / 100 / 12),
        "forgone_per_day": _q(excess * rate / 100 / 365),
    }


def advisor(subs: list, savings_ledger: list, today: date, idle: dict | None = None) -> dict:
    """What to do, ranked by rand per unit of effort — and whether the owner
    kept their word since last time. `idle` (F4) is idle_cash()'s verdict: it
    becomes THE ONE MOVE when it is worth more per month than the next move."""
    today = _as_date(today)
    ledger = []
    for row in savings_ledger:
        committed = _as_date(row["committed_on"])
        executed_on = row.get("executed_on")
        if executed_on is not None:
            executed_on = _as_date(executed_on)
            if executed_on < committed:
                raise AnalysisError(
                    f"{row['name']}: executed {executed_on} before it was decided "
                    f"{committed}. Refusing to report that as compliance."
                )
        executed = bool(row.get("executed")) and executed_on is not None
        settled = executed_on if executed else today
        days_late = max((settled - committed).days, 0)
        monthly = row["monthly"]
        monthly = monthly if isinstance(monthly, Decimal) else Decimal(str(monthly))
        ledger.append(
            {
                "name": row["name"],
                "monthly": monthly,
                "committed_on": committed,
                "executed": executed,
                "executed_on": executed_on,
                "days_late": days_late,
                "burned": _q(monthly * days_late / DAYS_PER_MONTH),
            }
        )
    ledger.sort(key=lambda r: r["burned"], reverse=True)

    live = [s for s in subs if s["name"] not in NOT_A_COST]
    live.sort(key=lambda s: s["monthly"], reverse=True)

    cut, undecided = Decimal("0"), Decimal("0")
    undecided_lines = []
    for sub in live:
        klass = _decision_class(sub.get("decision", ""))
        if klass == "cut":
            cut += sub["monthly"] * 12
        elif klass == "undecided":
            undecided += sub["monthly"] * 12
            undecided_lines.append(
                {"name": sub["name"], "decision": sub.get("decision", "")}
            )

    unexecuted = [r for r in ledger if not r["executed"]]
    if unexecuted:
        next_best = unexecuted[0]["monthly"]
    elif undecided_lines:
        next_best = max(s["monthly"] for s in live if _decision_class(s.get("decision", "")) == "undecided")
    else:
        next_best = live[0]["monthly"] if live else Decimal("0")
    if idle and idle["forgone_per_month"] > next_best:
        move = (
            f"Move the idle cash: {_rands(idle['excess'])} sits above your "
            f"{_rands(Decimal(str(idle['threshold'])))} buffer earning "
            f"{idle['earning_pct']}% a year. At {idle['rate_pct']}% it would earn "
            f"{_rands(idle['forgone_per_month'])} a month (about "
            f"{_rands(idle['forgone_per_day'])} a day), forgone while it sits."
        )
    elif unexecuted:
        top = unexecuted[0]
        move = (
            f"Execute {top['name']} — decided {top['committed_on'].isoformat()}, "
            f"{top['days_late']} days late, R{top['burned']} burned so far."
        )
    elif undecided_lines:
        move = (
            f"Decide {undecided_lines[0]['name']} — still "
            f"{undecided_lines[0]['decision'] or 'unmarked'}."
        )
    elif live:
        move = f"Review {live[0]['name']} — R{live[0]['monthly']}/mo, the largest live line."
    else:
        move = "Nothing outstanding on the ledger."

    return {
        "ledger": ledger,
        "subscriptions": live,
        "annual_if_cut": _q(cut),
        "annual_if_undecided": _q(undecided),
        "undecided": undecided_lines,
        "the_one_move": move,
        "idle_cash": idle,
    }
