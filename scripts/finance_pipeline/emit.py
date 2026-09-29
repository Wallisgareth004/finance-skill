#!/usr/bin/env python3
"""emit.py — rendering. Structured results in, text out. Nothing is computed here.

IN   the results of analyse.py and the rows about to be written
OUT  the markdown reports, the NOTES & GAPS text and the import-preview TSV

═══ WHAT THIS MODULE IS, AND WHAT IT REPLACED ═══
Plan Task 7 specified a paste-ready TSV emitter, on the assumption the owner would
paste six blocks into five sheets by hand every run. Spec §8b superseded that on
2026-08-20 — writer.py appends to the workbook directly, having proved charts
survive direct zip/XML patching. Two jobs survived that change:

  1. PREVIEW. transactions_tsv() renders exactly what writer.py WOULD append,
     before anything is written. It mirrors writer._row_xml's column contract
     precisely — same six columns, same omission of Balance on card rows, same
     refusal of an empty category. A preview that diverges from the writer is
     worse than no preview, because it is believed.

  2. REPORTS. The markdown the three lenses are read as, and the NOTES & GAPS
     text — EMITTED from the gap register, never parsed back out of it.
     The real NOTES & GAPS sheet once held an older "two gaps" line and a newer
     "THREE gaps" line at the same time, the stale one above the fresh one.
     That is what prose-as-register costs.

═══ A REFUSAL MUST READ AS A REFUSAL ═══
When analyse.cfo() declines to state a deficit, the rendered output says REFUSED
and names the export to pull. A refusal rendered as a footnote is a refusal
nobody acts on — a dashboard cell politely admitted for weeks that the headline
was stale, and it changed nothing.

═══ PRIVACY ═══
This module only ever sees a category. Sensitive merchants resolve to
"Personal — discretionary" upstream, in a gitignored rules file this module does
not read, import, or name. There is a test asserting that.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

_CENTS = Decimal("0.01")

# writer._row_xml reads exactly these six, in this order.
COLUMNS = ("Date", "Description", "Category", "Amount", "Balance", "Source")


class EmitError(Exception):
    """Raised rather than rendering something the workbook would reject."""


def _money(value) -> str:
    """Two decimals, always. The parser's Decimal already carries the cents;
    quantize guards against a caller handing us a float."""
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(dec.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _rand(value) -> str:
    """Rands for human reading. Negative renders as -R1,234.56, never R-1,234.56."""
    if value is None:
        return "—"
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    dec = dec.quantize(_CENTS, rounding=ROUND_HALF_UP)
    sign = "-" if dec < 0 else ""
    return f"{sign}R{abs(dec):,.2f}"


def _flat(text: str) -> str:
    """A tab or newline inside a description would silently add a column or a
    row to the preview grid. Neither can reach the output."""
    return (text or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _iso(d) -> str:
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise EmitError(f"date is not a date: {d!r}")
    return d.isoformat()


def transactions_tsv(txns: list, header: bool = False) -> str:
    """Exactly what writer.append_transactions would put in the sheet.

    Six tab-separated fields per row. Balance is EMPTY for card rows — writer
    omits column E entirely there, because FNB writes the literal string "0" in
    the card export's Balance column and parse.py discards it.
    """
    lines = ["\t".join(COLUMNS)] if header else []
    for txn in txns:
        if not getattr(txn, "category", ""):
            raise EmitError(
                f"{_iso(txn.date)} {_flat(txn.description)[:40]!r} has no category. "
                "Column C is matched byte-exactly by every SUMIFS in the "
                "workbook; an empty cell silently zeroes a category. Run "
                "categorise.load().apply_all() first."
            )
        balance = getattr(txn, "balance", None)
        lines.append(
            "\t".join(
                (
                    _iso(txn.date),
                    _flat(txn.description),
                    txn.category,
                    _money(txn.amount),
                    "" if balance is None else _money(balance),
                    getattr(txn, "source_slug", ""),
                )
            )
        )
    return "\n".join(lines) + "\n"


def notes_block(
    gaps: list,
    sources: list,
    coverage_start: date,
    coverage_end: date,
    date_stamp: date,
    ignored: list | None = None,
) -> str:
    """The NOTES & GAPS text, derived from the register. The gap COUNT is
    computed, never typed — that is the whole reason the two typed lines
    disagreed. Every file the run did not read is named with its reason (D10)."""
    if gaps:
        count = f"{len(gaps)} gap" + ("s" if len(gaps) != 1 else "")
    else:
        count = "no gaps"
    lines = [
        f"COVERAGE\t{coverage_start.isoformat()} → {coverage_end.isoformat()}, "
        f"with {count}.",
    ]
    for i, gap in enumerate(gaps, 1):
        lines.append(f"GAP {i}\t{gap} MISSING — export this window.")
    if sources:
        lines.append(f"SOURCES\t{len(sources)} files: " + ", ".join(sources))
    for row in ignored or []:
        lines.append(f"IGNORED\t{row['file']} — {row['why']}")
    lines.append(f"UPDATED\t{date_stamp.isoformat()}")
    return "\n".join(lines) + "\n"


def cfo_markdown(out: dict) -> str:
    """Control. What we have, what is committed, and what we refuse to say."""
    cov = out["coverage"]
    lines = [
        "# CFO — control",
        "",
        f"Window {out['window']} · **{cov['covered_days']} of {cov['elapsed_days']} "
        f"days covered** ({cov['coverage_pct']}%)",
        "",
        f"- Accessible cash: **{_rand(out['accessible_cash'])}**",
        f"- Income (last annuity credit): **{_rand(out['income'])}**",
        f"- Spend in window: **{_rand(out['spend'])}** over {out['rows_counted']} rows",
    ]

    if out["deficit_blocked_by"]:
        lines += [
            "",
            "## 🔴 DEFICIT REFUSED",
            "",
            "This window cannot produce an honest monthly figure:",
            "",
        ]
        lines += [f"- {reason}" for reason in out["deficit_blocked_by"]]
        lines += ["", f"**{out['remedy']}**"]
    else:
        lines += [
            f"- Spend per month (covered days): **{_rand(out['spend_per_month'])}**",
            f"- **Deficit: {_rand(out['deficit'])} / month**",
        ]
        prov = out.get("provisional") or {}
        if prov.get("is_provisional"):
            rows = prov["rows"]
            lines.append(
                f"  - ⚠️ **PROVISIONAL**: {rows} uncategorised "
                f"{'rows carry' if rows != 1 else 'row carries'} "
                f"{_rand(prov['uncategorised_spend'])} "
                f"of spend ({prov['share_of_spend']}%, above the {prov['limit_pct']}% bar). "
                f"They are counted as spend; labelling them could move the deficit by "
                f"up to {_rand(prov['per_month'])}/month."
            )
            if prov.get("held_rows"):
                lines.append(
                    f"  - {prov['held_rows']} of them ({_rand(prov['held_spend'])}) wait "
                    "for your labels and are held on purpose; without them, "
                    f"{prov['share_excluding_held']}% of spend is unlabelled."
                )
        runway = out["runway"]
        if runway["months"] is not None:
            lines.append(
                f"- Runway on accessible cash: **{runway['months']} months** "
                f"({runway['verdict']})"
            )
        else:
            lines.append(f"- Runway: not stated ({runway['verdict']})")

    if out.get("months"):
        lines += ["", month_table_markdown(out["months"]).rstrip("\n")]
    if out.get("caps"):
        lines += ["", caps_markdown(out.get("caps_season"), out["caps"]).rstrip("\n")]

    by_category = out.get("spend_by_category") or {}
    if by_category:
        lines += ["", "## Spend by category", ""]
        for name, value in sorted(
            by_category.items(), key=lambda kv: kv[1], reverse=True
        ):
            lines.append(f"- {name}: {_rand(value)}")

    pend = out.get("pending") or {}
    if pend.get("rows"):
        lines += [
            "",
            "## Pending — not yet posted",
            "",
            f"{_rand(pend['total'])} over {pend['rows']} card authorisation"
            f"{'s' if pend['rows'] != 1 else ''}, {pend['from'].isoformat()} → "
            f"{pend['to'].isoformat()}. Not in spend, not in the deficit, never "
            "written to the workbook; each one drops out when it posts.",
            "",
        ]
        lines += [f"- {k}: {_rand(v)}" for k, v in pend["by_category"].items()]

    trip_blocks = out.get("trips") or []
    if trip_blocks:
        lines += ["", "## Trips", ""]
    for trip in trip_blocks:
        paid = (
            f" · paid back {_rand(trip['reimbursed'])} on {trip['reimbursed_on'].isoformat()}"
            if trip["reimbursed"] is not None
            else ""
        )
        where = (
            "isolated from the run-rate" if trip["isolated"]
            else "still in the run-rate (set isolate: true to take it out)"
        )
        lines.append(
            f"- **{trip['label']}**, {trip['from'].isoformat()} → {trip['to'].isoformat()}: "
            f"{_rand(trip['cost'])} over {trip['rows']} rows{paid} · net "
            f"{_rand(trip['net'])} · {where}"
        )
        lines += [f"  - {k}: {_rand(v)}" for k, v in trip["by_category"].items()]

    for label, key in (
        ("Isolated from the run-rate", "isolated"),
        ("Asset conversions — not income, not spend", "asset_conversions"),
        ("Capital — not income, not spend", "capital"),
        ("Transfers between own accounts — out of the deficit, in cash burn", "transfers"),
        ("Credits netted against spend", "credits_netted"),
        ("Not consumption, but it left the account", "non_spend"),
    ):
        block = out.get(key) or {}
        if block:
            lines += ["", f"## {label}", ""]
            lines += [f"- {k}: {_rand(v)}" for k, v in sorted(block.items())]

    flow = out.get("crypto_flow") or {}
    if flow.get("bought") or flow.get("sold"):
        lines += [
            "",
            "## Crypto flow — asset conversion, not spend, not income",
            "",
            f"- bought {_rand(flow['bought'])} · sold {_rand(flow['sold'])} · "
            f"net out {_rand(flow['net_out'])} (in cash burn only)",
        ]

    reversed_debits = out.get("reversed_debits") or []
    if reversed_debits:
        lines += ["", "## Reversed debit orders — removed from spend in pairs", ""]
        lines += [
            f"- {r['category']}: {_rand(r['amount'])} debited "
            f"{r['debit_date'].isoformat()}, returned {r['reversal_date'].isoformat()}"
            for r in reversed_debits
        ]

    quarantine = out.get("quarantined_credits") or {}
    if quarantine.get("rows"):
        verdict = (
            "**blocks the deficit**"
            if quarantine["blocks_deficit"]
            else f"under the {_rand(quarantine['limit'])} limit — reported, not blocking"
        )
        lines += [
            "",
            "## Quarantined credits — never netted against spend",
            "",
            f"Uncategorised credits {_rand(quarantine['total'])}: {verdict}.",
            "",
        ]
        lines += [
            f"- {r['date'].isoformat()} {_rand(r['amount'])}"
            for r in quarantine["rows"]
        ]

    # Only meaningful when something WAS itemised. With itemised=[] the residual
    # is the whole spend, and the old line rendered "not itemised above: <total>
    # across <every category>" directly under a section that itemised nothing.
    residual = out["residual"]
    if residual["categories"] and residual["amount"] != out["spend"]:
        lines += [
            "",
            f"_Not itemised above: {_rand(residual['amount'])} across "
            f"{', '.join(residual['categories'])}._",
        ]
    return "\n".join(lines) + "\n"


def _month_label(m: dict) -> str:
    """"2026-08" for a complete month; a part-month says which part."""
    if m["complete"] or not m.get("from"):
        return m["month"] + ("" if m["complete"] else " (to date)")
    span = f"{m['from'].day}–{m['to'].day}"
    return m["month"] + (f" ({span}, to date)" if m["from"].day == 1 else f" ({span} only)")


def month_table_markdown(table: list) -> str:
    """F1: the window by calendar month. A month the window cuts short shows
    spend to date and no deficit — a part-month is never scaled up."""
    lines = [
        "## Month by month",
        "",
        "| Month | Days | Income | Cheque spend | Card spend | Deficit, cheque account "
        "| Deficit, all accounts | Pending |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in table:
        label = _month_label(m)
        pending = _rand(m["pending"]) if m["pending"] else "—"
        lines.append(
            f"| {label} | {m['covered_days']} of {m['elapsed_days']} | {_rand(m['income'])} "
            f"| {_rand(m['spend_cheque'])} | {_rand(m['spend_card'])} "
            f"| {_rand(m['deficit_cheque'])} | {_rand(m['deficit'])} | {pending} |"
        )
    lines += [
        "",
        "_Deficits are per month, on covered days, for complete months only. The cheque "
        "column is the account alone (the earlier definition); the all-accounts column "
        "adds card purchases. Pending card spend is not in either._",
    ]
    return "\n".join(lines) + "\n"


def caps_markdown(season, table: list) -> str:
    """F2: every cap line against each month. A month the window cuts short is
    shown to date, not pro-rated."""
    if not table or not table[0]["lines"]:
        return ""
    heads = [_month_label(m) for m in table]
    lines = [
        f"## Caps — {season or 'monthly caps'}",
        "",
        "| Cap | Per month | " + " | ".join(heads) + " |",
        "|---|---|" + "---|" * len(heads),
    ]
    for i, cap in enumerate(table[0]["lines"]):
        cells = []
        for m in table:
            line = m["lines"][i]
            if line["over"] > 0:
                cells.append(f"🔴 {_rand(line['actual'])} (+{_rand(line['over'])})")
            else:
                cells.append(f"✅ {_rand(line['actual'])}")
        lines.append(f"| {cap['name']} | {_rand(cap['cap'])} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def analyst_markdown(out: dict) -> str:
    """What changed. Ranked by rand — a 300% rise on R12 is noise."""
    lines = ["# Analyst — what changed", "", "## Movements by rand", ""]
    for m in out["movements"][:15]:
        direction = "up" if m["delta"] > 0 else "down"
        lines.append(
            f"- **{m['category']}** {direction} {_rand(abs(m['delta']))} "
            f"({_rand(m['prev'])} → {_rand(m['now'])})"
        )
    if out["new_merchants"]:
        lines += ["", "## New merchants", ""]
        lines += [f"- {n}" for n in out["new_merchants"][:15]]
    if out["stopped_suppressed_by_gap"]:
        lines += [
            "",
            "## Stopped merchants — NOT REPORTED",
            "",
            "A coverage gap intersects this window. Every recurring debit inside "
            "it looks cancelled. Suppressed rather than guessed.",
        ]
    elif out["stopped_merchants"]:
        lines += ["", "## Stopped", ""]
        lines += [f"- {n}" for n in out["stopped_merchants"][:15]]
    return "\n".join(lines) + "\n"


def advisor_markdown(out: dict) -> str:
    """What to do — and whether the owner kept their word since last time."""
    lines = ["# Advisor — what to do", ""]

    if out["ledger"]:
        lines += ["## Did you keep your word?", ""]
        for row in out["ledger"]:
            if row["executed"]:
                lines.append(
                    f"- ✅ **{row['name']}** — {_rand(row['monthly'])}/mo, executed "
                    f"{row['executed_on'].isoformat()} "
                    f"({row['days_late']} days after deciding, "
                    f"{_rand(row['burned'])} burned)"
                )
            else:
                lines.append(
                    f"- ⚠️ **{row['name']}** — {_rand(row['monthly'])}/mo, decided "
                    f"{row['committed_on'].isoformat()}, **not executed**. "
                    f"{row['days_late']} days late. **{_rand(row['burned'])} burned.**"
                )
        lines.append("")

    if out["subscriptions"]:
        lines += [f"## Recurring charges ({len(out['subscriptions'])}, ranked by rand)", ""]
        for sub in out["subscriptions"][:20]:
            decision = sub.get("decision", "")
            found = (
                f" · ≈{_rand(sub['amount'])} a charge, {sub['charges']} charges in "
                f"{sub['months']} months, last {sub['last_seen'].isoformat()}"
                if "charges" in sub
                else ""
            )
            yearly = f" · {_rand(sub['monthly'] * 12)}/yr if cut"
            lines.append(
                f"- {sub['name']} — {_rand(sub['monthly'])}/mo{yearly}{found}"
                + (f" · {decision}" if decision else "")
            )
        lines += [
            "",
            f"**Annual saving if every CUT is executed: {_rand(out['annual_if_cut'])}** · "
            f"still undecided: {_rand(out['annual_if_undecided'])}/yr",
            "",
        ]

    lines += ["## THE ONE MOVE", "", out["the_one_move"], ""]
    return "\n".join(lines)
