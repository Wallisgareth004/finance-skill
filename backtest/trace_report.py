#!/usr/bin/env python3
"""trace_report.py — every headline number in 01-cfo.md, rebuilt from rows.

    python backtest/trace_report.py --model COPY.xlsx --window 2026-02-01 2026-04-30 \
        --report reports/2026-05-01/01-cfo.md --out reports/2026-05-01

Writes 04-trace.tsv (every row in the window, its label and its bucket) and
04-trace.md (each headline figure, recomputed here, beside the figure the report
printed, with MATCH / MISMATCH). Exit 0 only if every figure matches.

Deliberately NOT analyse.py: the buckets are assigned by a plain table below and
summed with plain Decimal arithmetic. The labels come from the sheet plus the
same in-memory relabel a dry run applies — those are inputs, not results.

Since D12 (27 Sep) the report counts card purchases, so the trace reads what the
report read: the CREDIT CARD sheet and, with --input, the card exports of the
drop the report was made from (merged so each purchase counts once). With
--trips, an isolated trip's rows (inside its dates, in its categories) go to
their own bucket — assigned here by plain comparison, not by trips.assign().
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "finance_pipeline"))
import categorise  # noqa: E402
import detect  # noqa: E402
import model  # noqa: E402
import parse  # noqa: E402
import run as run_mod  # noqa: E402
import trips  # noqa: E402

Q = Decimal("0.01")
BUCKET = {  # label → bucket, for DEBITS; credits are handled below
    "Savings (Bank Your Change)": "non_spend",
    "Savings transfers": "non_spend",
    "Travel — trip": "isolated",
    "Crypto / investments": "crypto",
    "Transfer — card settlement": "transfer",
    "Capital — one-off inflow": "capital",
    "Income — annuity": "income",
    "Reversal — unpaid debit": "reversal",
}
CONVERSION = {"People / transfers", "Cash / ATM", "Income — other"}


def _d(v):
    return v.date() if isinstance(v, datetime) else v


def bucket_rows(rows):
    out = []
    for r in rows:
        cat, amt = r.category or "", r.amount
        b = BUCKET.get(cat)
        if b is None:
            if amt < 0:
                b = "spend"
            elif cat in ("", "Uncategorised"):
                b = "quarantine"
            elif cat in CONVERSION:
                b = "conversion"
            else:
                b = "refund"
        out.append([r, cat, amt, b])
    # reversal pairing: same amount, debit 0–10 days before, closest first
    used = set()
    for i, (r, cat, amt, b) in enumerate(out):
        if b != "reversal" or amt <= 0:
            continue
        best = None
        for j, (r2, cat2, amt2, b2) in enumerate(out):
            if j in used or b2 != "spend" or -amt2 != amt:
                continue
            lag = (_d(r.date) - _d(r2.date)).days
            if 0 <= lag <= 10 and (best is None or lag < best[0]):
                best = (lag, j)
        if best is None:
            out[i][3] = "quarantine"
        else:
            used.add(best[1])
            out[best[1]][3] = "reversed"
            out[i][3] = "reversal-paired"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--window", nargs=2, required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rules", default=None)
    ap.add_argument("--input", default=None, help="the drop the report was made from")
    ap.add_argument("--trips", default=None, help="the trips file the report used")
    args = ap.parse_args(argv)
    lo, hi = (date.fromisoformat(x) for x in args.window)

    ruleset = categorise.load(args.rules)
    rows = run_mod._sheet_transactions(args.model)
    run_mod._relabel_uncategorised(rows, ruleset)
    exports = []
    for name in sorted(os.listdir(args.input)) if args.input else []:
        if name.lower().endswith(".csv"):
            det = detect.sniff(os.path.join(args.input, name))
            if det.account_kind == "card":
                exports += ruleset.apply_all(parse.parse_file(det))
    cards = model.merge_card_rows(model.load_card_sheet(args.model), exports)
    run_mod._relabel_uncategorised(cards, ruleset)
    rows = [r for r in rows + cards if lo <= _d(r.date) <= hi]
    tagged = bucket_rows(rows)
    for trip in trips.load(args.trips):
        if not trip.isolate:
            continue
        for item in tagged:
            r, cat, amt, b = item
            if b == "spend" and trip.start <= _d(r.date) <= trip.end and (cat or "Uncategorised") in trip.categories:
                item[3] = "isolated"

    def total(bucket, sign=-1):
        return sum((sign * a for _, _, a, b in tagged if b == bucket), Decimal("0"))

    income_rows = [(r, a) for r, _, a, b in tagged if b == "income" and a > 0]
    last = max(_d(r.date) for r, _ in income_rows)
    income = sum((a for r, a in income_rows if _d(r.date) == last), Decimal("0"))
    spend = total("spend") - total("refund", sign=1)
    days = (hi - lo).days + 1
    spend_pm = (spend * Decimal("30.44") / days).quantize(Q)
    mine = {
        "Income (last annuity credit)": income.quantize(Q),
        "Spend in window": spend.quantize(Q),
        "Spend per month (covered days)": spend_pm,
        "Deficit": (income - spend_pm).quantize(Q),
        "Capital — one-off inflow": total("capital", sign=1).quantize(Q),
        "Transfer — card settlement": total("transfer").quantize(Q),
        "crypto bought": sum(
            (-a for _, _, a, b in tagged if b == "crypto" and a < 0), Decimal("0")
        ).quantize(Q),
        "Uncategorised credits": total("quarantine", sign=1).quantize(Q),
        "Unlabelled spend (PROVISIONAL)": sum(
            (-amt for _, cat, amt, b in tagged if b == "spend" and cat in ("", "Uncategorised")),
            Decimal("0"),
        ).quantize(Q),
    }

    text = open(args.report, encoding="utf-8").read()

    def printed(pattern):
        m = re.search(pattern, text)
        return Decimal(m.group(1).replace(",", "")) if m else None

    def signed(pattern):
        m = re.search(pattern, text)
        if not m:
            return None
        value = Decimal(m.group(2).replace(",", ""))
        return -value if m.group(1) else value

    theirs = {
        "Income (last annuity credit)": printed(
            r"Income \(last annuity credit\): \*\*R([\d,.]+)"
        ),
        "Spend in window": printed(r"Spend in window: \*\*R([\d,.]+)"),
        "Spend per month (covered days)": printed(
            r"Spend per month \(covered days\): \*\*R([\d,.]+)"
        ),
        "Deficit": signed(r"Deficit: (-?)R([\d,.]+)"),
        "Capital — one-off inflow": printed(r"Capital — one-off inflow: R([\d,.]+)"),
        # signed: with card rows in (D12) the two legs can net below zero
        "Transfer — card settlement": signed(r"Transfer — card settlement: (-?)R([\d,.]+)"),
        "crypto bought": printed(r"bought R([\d,.]+)"),
        "Uncategorised credits": printed(r"Uncategorised credits R([\d,.]+)"),
        "Unlabelled spend (PROVISIONAL)": printed(r"uncategorised rows? carr(?:y|ies) R([\d,.]+)"),
    }

    mine["Unlabelled per month (bound)"] = (
        mine["Unlabelled spend (PROVISIONAL)"] * Decimal("30.44") / days
    ).quantize(Q)
    theirs["Unlabelled per month (bound)"] = printed(r"by up to R([\d,.]+)/month")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "04-trace.tsv"), "w", encoding="utf-8") as fh:
        fh.write("date\tamount\tlabel\tbucket\n")  # descriptions omitted: privacy
        for r, cat, amt, b in sorted(tagged, key=lambda t: _d(t[0].date)):
            fh.write(f"{_d(r.date).isoformat()}\t{amt}\t{cat}\t{b}\n")
    lines = [
        f"# Trace — {lo} → {hi}, {len(tagged)} rows",
        "",
        "Each figure recomputed from 04-trace.tsv by plain grouping (not analyse.py).",
        "",
        "| Figure | Report | From rows | |",
        "|---|---|---|---|",
    ]
    ok = True
    for k, v in mine.items():
        match = theirs[k] == v
        ok &= match
        lines.append(
            f"| {k} | R{theirs[k]} | R{v} | {'✅ MATCH' if match else '❌ MISMATCH'} |"
        )
    counts = {}
    for _, _, _, b in tagged:
        counts[b] = counts.get(b, 0) + 1
    lines += [
        "",
        "Rows per bucket: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
    ]
    with open(os.path.join(args.out, "04-trace.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
