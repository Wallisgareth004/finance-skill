#!/usr/bin/env python3
"""run_backtest.py — replay real (or synthetic) bank drops onto SANDBOX COPIES
of a workbook and grade the pipeline against written oracles.

    python backtest/run_backtest.py                       # private config if present
    python backtest/run_backtest.py --config backtest/replays.synthetic.yaml

═══ WHAT IT NEVER DOES ═══
It never opens a starting workbook or a known-good workbook for write. Each
replay copies its start workbook into backtest/.sandbox/<stamp>/<replay>/ and
runs `run.py --write` against that copy only. The script hashes every workbook
it was given before and after, and fails loudly if any of them changed.

═══ HOW A REPLAY RUNS ═══
For each drop, in order: copy its CSVs into a fresh inbox, run the real CLI
(`run.py --write`) as a subprocess, capture the exit code and the JSON summary.
Then re-run the last drop to prove idempotence. Then analyse the final sandbox
workbook through the pipeline's own read path (run._sheet_transactions +
run._relabel_uncategorised + verify.find_gaps + analyse.cfo) — the same code a
dry run uses — and check each oracle.

═══ ORACLES ═══
Every oracle in the config is optional and is graded PASS / FAIL / REPORT.
Since 27 Sep a `summary` block grades the last drop's own run JSON — ignored
files, pending card spend, the month table, trips, caps, recurring charges and
idle cash — and a `held` block checks that rows held for the owner stay
unlabelled on the final sheet while their later twins are labelled.
REPORT means "measured, not gated" (the uncategorised share before the D4 labels).
The scorecard is written to backtest/results/YYYY-MM-DD.md — gitignored, because
on the real config it holds real figures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
PIPELINE = os.path.join(SKILL, "scripts", "finance_pipeline")
sys.path.insert(0, PIPELINE)

import analyse  # noqa: E402
import categorise  # noqa: E402
import model  # noqa: E402
import run as run_mod  # noqa: E402
import verify  # noqa: E402

RUN = os.path.join(PIPELINE, "run.py")
PRIVATE = os.path.join(HERE, "replays.private.yaml")
SYNTHETIC = os.path.join(HERE, "replays.synthetic.yaml")
TXNS = model.TXNS_SHEET


# ── helpers ────────────────────────────────────────────────────────────────
def _expand(path: str, base: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _cents(value):
    return None if value is None else model.amount_cents(value)


def _rows(xlsx: str) -> list:
    """TRANSACTIONS as (date, description, amount_cents, balance_cents, category)."""
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    try:
        ws = wb[TXNS]
        ws.reset_dimensions()
        out = []
        for r in ws.iter_rows(min_row=model.FIRST_DATA_ROW, values_only=True):
            if r[model.COL_DATE] is None and r[model.COL_AMOUNT] is None:
                continue
            d = r[0].date() if isinstance(r[0], datetime) else r[0]
            desc = (r[1] or "").strip()
            if desc == model.NO_DESCRIPTION:
                desc = ""
            out.append((d.isoformat(), desc, _cents(r[3]), _cents(r[4]), r[2] or ""))
        return out
    finally:
        wb.close()


def _charts(xlsx: str) -> int:
    with zipfile.ZipFile(xlsx) as z:
        return sum(1 for n in z.namelist() if "charts/chart" in n)


def _other_sheets_diff(a: str, b: str) -> list:
    """Cell-for-cell compare of every sheet except TRANSACTIONS — formulas AND
    cached values. Returns a list of human-readable differences (empty = identical)."""
    diffs = []
    for data_only in (False, True):
        wa = openpyxl.load_workbook(a, read_only=True, data_only=data_only)
        wb = openpyxl.load_workbook(b, read_only=True, data_only=data_only)
        try:
            if wa.sheetnames != wb.sheetnames:
                return [f"sheet list differs: {wa.sheetnames} vs {wb.sheetnames}"]
            for name in wa.sheetnames:
                if name == TXNS:
                    continue
                sa, sb = wa[name], wb[name]
                sa.reset_dimensions()
                sb.reset_dimensions()
                ra = [tuple(r) for r in sa.iter_rows(values_only=True)]
                rb = [tuple(r) for r in sb.iter_rows(values_only=True)]
                if ra != rb:
                    kind = "values" if data_only else "formulas"
                    diffs.append(f"{name}: {kind} differ ({len(ra)} vs {len(rb)} rows)")
        finally:
            wa.close()
            wb.close()
    return diffs


def _cli(inbox, xlsx, workdir, *extra):
    proc = subprocess.run(
        [
            sys.executable,
            RUN,
            "--input",
            inbox,
            "--model",
            xlsx,
            "--reports",
            os.path.join(workdir, "reports"),
            "--db",
            os.path.join(workdir, "ledger.sqlite"),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    try:
        summary = json.loads(proc.stdout)
    except json.JSONDecodeError:
        summary = {"error": "stdout was not JSON", "stderr": proc.stderr[-2000:]}
    return proc.returncode, summary


def _inbox(workdir: str, drop: dict, base: str, n: int) -> str:
    inbox = os.path.join(workdir, f"inbox-{n:02d}")
    os.makedirs(inbox)
    src = _expand(drop["path"], base)
    # Every file, not only the CSVs: a real drop also holds PDFs and notes, and
    # the run must list or read them (D10, D11). Folders stay out, as in a drop.
    names = drop.get("files") or sorted(
        f for f in os.listdir(src) if os.path.isfile(os.path.join(src, f))
    )
    for name in names:
        shutil.copy2(os.path.join(src, name), inbox)
    return inbox


def _analyse(xlsx: str, rules: str | None, window: tuple, basis: str = "all") -> dict:
    """The dry-run analysis path, on the final sandbox workbook: TRANSACTIONS
    and the CREDIT CARD sheet (D12), relabelled in memory by today's rules.
    `basis: cheque` also computes the month table's cheque-only deficit."""
    ruleset = categorise.load(rules)
    rows = run_mod._sheet_transactions(xlsx)
    relabelled = run_mod._relabel_uncategorised(rows, ruleset)
    cards = model.load_card_sheet(xlsx)
    run_mod._relabel_uncategorised(cards, ruleset)
    dates = sorted(
        {(r.date.date() if isinstance(r.date, datetime) else r.date) for r in rows}
    )
    gaps = verify.find_gaps(dates)
    win = analyse.Window(date.fromisoformat(window[0]), date.fromisoformat(window[1]))
    out = analyse.cfo(txns=rows + cards, window=win, cash={}, today=date.today(), gaps=gaps)
    if basis == "cheque":
        [month] = analyse.month_table(rows + cards, win, gaps, date.today())
        out["deficit"] = month["deficit_cheque"]
    out["relabelled"] = relabelled
    out["_rows"] = rows
    return out


# ── grading ────────────────────────────────────────────────────────────────
class Card:
    def __init__(self, name):
        self.name = name
        self.lines = []  # (verdict, oracle, detail)

    def add(self, verdict, oracle, detail):
        self.lines.append((verdict, oracle, detail))

    def check(self, ok, oracle, detail):
        self.add("PASS" if ok else "FAIL", oracle, detail)

    @property
    def failed(self):
        return [line for line in self.lines if line[0] == "FAIL"]


def _check_summary(o: dict, summary: dict, card: Card) -> None:
    """The last drop's run JSON against the `summary` oracles (27 Sep)."""
    def dec(v):
        return None if v is None else Decimal(str(v))

    if "ignored_files" in o:
        got = [f["file"] for f in summary.get("ignored_files", [])]
        card.check(got == o["ignored_files"], "D10 every non-export file is listed",
                   f"ignored {got}")
    if "pending" in o:
        p = summary.get("pending") or {}
        ok = p.get("rows") == o["pending"]["rows"] and dec(p.get("total")) == dec(o["pending"]["total"])
        card.check(ok, "D11 pending card spend reported apart",
                   f"{p.get('rows')} rows, R{p.get('total')} (expected R{o['pending']['total']})")
    if "month" in o:
        want = o["month"]
        m = next((x for x in summary.get("months", []) if x["month"] == want["month"]), {})
        card.check(dec(m.get("spend_card")) == dec(want["spend_card"]),
                   f"D12 card purchases counted once ({want['month']})",
                   f"card spend R{m.get('spend_card')} (expected R{want['spend_card']})")
        card.check(dec((m.get("spend_by_category") or {}).get("Supplements")) == dec(want["supplements"]),
                   "D15 supplements on their own line",
                   f"R{(m.get('spend_by_category') or {}).get('Supplements')}")
        ok = dec(m.get("deficit")) == dec(want["deficit"]) and dec(m.get("deficit_cheque")) == dec(want["deficit_cheque"])
        card.check(ok, f"F1 month table, {want['month']}: all accounts and cheque only",
                   f"R{m.get('deficit')} / R{m.get('deficit_cheque')} (expected "
                   f"R{want['deficit']} / R{want['deficit_cheque']})")
    if "trip" in o:
        want = o["trip"]
        t = next((x for x in summary.get("trips", []) if x["label"] == want["label"]), {})
        ok = (t.get("isolated") == want["isolated"] and dec(t.get("cost")) == dec(want["cost"])
              and dec(t.get("net")) == dec(want["net"]))
        card.check(ok, "D14 trip isolated, reimbursement paired",
                   f"cost R{t.get('cost')}, net R{t.get('net')} (expected R{want['cost']}, R{want['net']})")
    if "cap" in o:
        want = o["cap"]
        month = next((x for x in summary.get("caps", []) if x["month"] == want["month"]), {})
        line = next((x for x in month.get("lines", []) if x["name"] == want["name"]), {})
        card.check(dec(line.get("actual")) == dec(want["actual"]), "F2 caps: actual against cap",
                   f"{want['name']} R{line.get('actual')} (expected R{want['actual']})")
    if "subscriptions_include" in o:
        names = (summary.get("subscriptions") or {}).get("names", [])
        missing = [n for n in o["subscriptions_include"] if n not in names]
        card.check(not missing, "F3 recurring charges found", f"missing {missing or 'none'}")
    if "idle_forgone_per_month" in o:
        idle = summary.get("idle_cash") or {}
        card.check(dec(idle.get("forgone_per_month")) == dec(o["idle_forgone_per_month"]),
                   "F4 idle cash costed",
                   f"R{idle.get('forgone_per_month')}/month (expected R{o['idle_forgone_per_month']})")


def _check_held(o: dict, final: list, card: Card) -> None:
    """D13: a held row stays unlabelled; its later twin is labelled."""
    rows = [r for r in final if r[1].startswith(o["description"])]
    held = [r[4] for r in rows if r[0] in o["unlabelled"]]
    later = [r[4] for r in rows if r[0] in o["labelled"]]
    ok = held and all(c in ("", "Uncategorised") for c in held) and later and all(
        c == o["label"] for c in later)
    card.check(bool(ok), "D13 a held row stays unlabelled; its later twin is labelled",
               f"held {held}, later {later}")


def replay(spec: dict, base: str, sandbox: str, card: Card) -> dict:
    name = spec["name"]
    workdir = os.path.join(sandbox, name)
    os.makedirs(workdir)
    start = _expand(spec["start_workbook"], base)
    xlsx = os.path.join(workdir, "model.xlsx")
    shutil.copy2(start, xlsx)
    rules = _expand(spec["rules"], base) if spec.get("rules") else None
    rules_args = ("--rules", rules) if rules else ()
    extra = [a if a.startswith("--") else _expand(a, base) for a in spec.get("args", [])]
    oracles = spec.get("oracles", {})

    steps, breaks, checked, codes = [], 0, 0, []
    for n, drop in enumerate(spec["drops"], 1):
        inbox = _inbox(workdir, drop, base, n)
        code, summary = _cli(inbox, xlsx, workdir, "--write", *rules_args, *extra)
        codes.append(code)
        for c in summary.get("chain", []):
            breaks += len(c.get("breaks", []))
            checked += c.get("checked", 0)
        rows = summary.get("rows", {})
        steps.append(
            {
                "drop": drop["path"],
                "exit": code,
                "new": rows.get("new"),
                "across_files": rows.get("duplicates_across_files"),
                "savings_not_written": rows.get("savings_new_not_written"),
                "card_not_written": rows.get("card_new_not_written"),
                "error": summary.get("error"),
                "summary": summary,
            }
        )
        if code != 0:
            card.add("FAIL", f"drop {n} exit code", f"{code} — {summary.get('error')}")

    card.check(all(c == 0 for c in codes), "every drop exits 0", f"exit codes {codes}")
    card.check(
        breaks == 0, "balance chain", f"{breaks} breaks in {checked} comparisons"
    )

    if "last_drop_new_rows" in oracles:
        got = steps[-1]["new"]
        card.check(
            got == oracles["last_drop_new_rows"],
            "new rows from the last drop",
            f"{got} (expected {oracles['last_drop_new_rows']}; "
            f"{steps[-1]['across_files']} collapsed across files)",
        )

    # idempotence: the same last drop, again
    inbox = _inbox(workdir, spec["drops"][-1], base, 99)
    code, summary = _cli(inbox, xlsx, workdir, "--write", *rules_args, *extra)
    again = summary.get("rows", {}).get("new")
    card.check(
        code == 0 and again == 0,
        "idempotent (re-run adds 0)",
        f"exit {code}, new {again}",
    )

    final = _rows(xlsx)
    keys4 = Counter((d, desc, a, b) for d, desc, a, b, _ in final)
    dups = sum(v - 1 for v in keys4.values() if v > 1)
    card.check(
        dups == 0,
        "0 duplicate keys (date, description, amount, balance)",
        f"{dups} duplicates across {len(final)} rows",
    )
    card.check(
        _charts(xlsx) == oracles.get("charts", 4),
        "charts intact",
        f"{_charts(xlsx)} charts (expected {oracles.get('charts', 4)})",
    )

    good = spec.get("known_good_workbook")
    compare = {}
    if good:
        good = _expand(good, base)
        want = Counter((d, desc, a, b) for d, desc, a, b, _ in _rows(good))
        missing, extra = want - keys4, keys4 - want
        exact = spec.get("known_good_exact", True)
        detail = (
            f"{sum(keys4.values())} rows vs known-good {sum(want.values())}; "
            f"missing {sum(missing.values())}, extra {sum(extra.values())}"
        )
        compare = {
            "missing": sorted(missing.elements())[:12],
            "extra": sorted(extra.elements())[:12],
        }
        if exact:
            card.check(
                not missing and not extra,
                "TRANSACTIONS = known-good (multiset)",
                detail,
            )
            diffs = _other_sheets_diff(xlsx, good)
            card.check(
                not diffs,
                "every other sheet cell-identical to known-good",
                "identical" if not diffs else "; ".join(diffs),
            )
        else:
            card.add(
                "REPORT", "TRANSACTIONS vs known-good (different start point)", detail
            )
            diffs = _other_sheets_diff(xlsx, good)
            card.add(
                "REPORT",
                "other sheets vs known-good",
                "identical" if not diffs else "; ".join(diffs),
            )

    # ── analysis oracles ──
    results = {"steps": steps, "rows": len(final), "compare": compare}
    if "annuity" in oracles:
        o = oracles["annuity"]
        out = _analyse(xlsx, rules, o["window"])
        card.check(
            out["annuity_credits"] == o["credits"],
            "annuity credits",
            f"{out['annuity_credits']} in {o['window'][0]} → {o['window'][1]} (expected {o['credits']})",
        )

    if "headline" in oracles:
        o = oracles["headline"]
        out = _analyse(xlsx, rules, o["window"])
        results["headline"] = {
            k: out[k]
            for k in (
                "spend",
                "income",
                "deficit",
                "cash_deficit",
                "spend_per_month",
                "cash_burn_per_month",
                "deficit_blocked_by",
                "capital",
                "crypto_flow",
                "transfers",
                "reversed_debits",
                "quarantined_credits",
                "coverage",
                "spend_by_category",
                "isolated",
                "non_spend",
                "relabelled",
                "provisional",
            )
        }
        prov = out.get("provisional") or {}
        card.check(
            out["deficit"] is not None,
            "deficit computed, not refused",
            f"{out['deficit']} over {o['window'][0]} → {o['window'][1]}"
            + (
                ""
                if out["deficit"] is not None
                else f" — blocked by {out['deficit_blocked_by']}"
            )
            + (
                f" — PROVISIONAL: {prov['rows']} unlabelled debits, "
                f"up to R{prov['per_month']}/month"
                if prov.get("is_provisional")
                else ""
            ),
        )
        negative = {k: v for k, v in out["spend_by_category"].items() if v < 0}
        cap = out["capital"]
        if "capital" in o:
            ok = not negative and cap.get(o["capital"]["category"]) == Decimal(
                str(o["capital"]["amount"])
            )
            card.check(
                ok,
                "capital (one-off inflow) NOT in spend",
                f"capital {cap}; negative spend categories {negative or 'none'}",
            )
        if "crypto_bought" in o:
            flow = out["crypto_flow"]
            ok = "Crypto / investments" not in out["spend_by_category"] and flow[
                "bought"
            ] == Decimal(str(o["crypto_bought"]))
            card.check(ok, "crypto NOT in spend", f"flow {flow}")

    if "summary" in oracles:
        _check_summary(oracles["summary"], steps[-1]["summary"], card)
    if "held" in oracles:
        _check_held(oracles["held"], final, card)

    if "month" in oracles:
        o = oracles["month"]
        out = _analyse(xlsx, rules, o["window"], o.get("basis", "all"))
        results["month"] = {
            k: out[k]
            for k in (
                "spend",
                "income",
                "deficit",
                "spend_per_month",
                "isolated",
                "non_spend",
            )
        }
        ref, tol = Decimal(str(o["reference"])), Decimal(str(o["tolerance"]))
        got = out["deficit"]
        if got is None:
            card.add(
                "FAIL",
                f"{o['label']} deficit vs reference",
                f"refused: {out['deficit_blocked_by']}",
            )
        else:
            verdict = "PASS" if abs(got - ref) <= tol else o.get("on_miss", "FAIL")
            card.add(
                verdict,
                f"{o['label']} deficit within ±R{tol} of reference R{ref}",
                f"pipeline R{got} (Δ R{got - ref}); spend R{out['spend']}, income R{out['income']}. "
                f"{o.get('why_tolerance', '')}",
            )

    if "uncategorised" in oracles:
        o = oracles["uncategorised"]
        rows = _analyse(xlsx, rules, (o["from"], o["to"]))["_rows"]
        lo, hi = date.fromisoformat(o["from"]), date.fromisoformat(o["to"])
        scoped = [
            r
            for r in rows
            if lo <= (r.date.date() if isinstance(r.date, datetime) else r.date) <= hi
        ]
        unc = sum(1 for r in scoped if r.category in ("", "Uncategorised"))
        share = Decimal(unc * 100) / max(len(scoped), 1)
        verdict = (
            "PASS"
            if share < Decimal(str(o["limit_pct"]))
            else ("REPORT" if o.get("report_only") else "FAIL")
        )
        card.add(
            verdict,
            f"uncategorised < {o['limit_pct']}%",
            f"{unc} of {len(scoped)} rows = {share:.1f}% ({o['from']} → {o['to']})"
            + (
                " — reported, not gated, until the D4 labels land"
                if o.get("report_only")
                else ""
            ),
        )
    return results


def scorecard(cards, results, config_path, hashes_ok) -> str:
    today = date.today().isoformat()
    total = sum(len(c.lines) for c in cards)
    fails = sum(len(c.failed) for c in cards)
    out = [
        f"# /finance backtest — {today}",
        "",
        f"Config `{os.path.basename(config_path)}` · {len(cards)} replays · "
        f"{total} oracles · **{fails} FAIL**",
        "",
        f"Source workbooks untouched (sha256 before = after): **{'yes' if hashes_ok else 'NO'}**",
        "",
    ]
    for card in cards:
        out += [f"## {card.name}", "", "| | Oracle | Measured |", "|---|---|---|"]
        for verdict, oracle, detail in card.lines:
            icon = {"PASS": "✅", "FAIL": "❌", "REPORT": "📋"}[verdict]
            out.append(
                f"| {icon} {verdict} | {oracle} | {str(detail).replace('|', '/')} |"
            )
        res = results[card.name]
        out += ["", "Drops replayed:", ""]
        for s in res["steps"]:
            out.append(
                f"- `{os.path.basename(s['drop'].rstrip('/'))}` → exit {s['exit']}, "
                f"{s['new']} new, {s['across_files']} collapsed across files, "
                f"savings not written {s['savings_not_written']}, card not written {s['card_not_written']}"
            )
        if res.get("headline"):
            h = res["headline"]
            out += [
                "",
                "Headline window:",
                "",
                f"- spend R{h['spend']} · income R{h['income']} · spend/month R{h['spend_per_month']}"
                f" · **deficit R{h['deficit']}/month** · cash deficit R{h['cash_deficit']}/month",
                f"- capital {h['capital']} · transfers {h['transfers']} · crypto {h['crypto_flow']}",
                f"- reversed debits {len(h['reversed_debits'])} · quarantined credits "
                f"R{h['quarantined_credits']['total']} (limit R{h['quarantined_credits']['limit']})",
                f"- rows relabelled in memory at analysis: {h['relabelled']}",
                (
                    f"- ⚠️ **PROVISIONAL**: {h['provisional']['rows']} unlabelled debits, "
                    f"R{h['provisional']['uncategorised_spend']} "
                    f"({h['provisional']['share_of_spend']}% of spend) — the deficit "
                    f"could move by up to R{h['provisional']['per_month']}/month once labelled"
                    if h["provisional"]["is_provisional"]
                    else "- deficit is firm (unlabelled spend under 5% of spend)"
                ),
            ]
        if res.get("compare") and (
            res["compare"]["missing"] or res["compare"]["extra"]
        ):
            out += [
                "",
                "First differences vs known-good (date, description, amount¢, balance¢):",
                "",
            ]
            out += [f"- missing {m}" for m in res["compare"]["missing"]]
            out += [f"- extra {m}" for m in res["compare"]["extra"]]
        out.append("")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=None)
    ap.add_argument("--results", default=os.path.join(HERE, "results"))
    args = ap.parse_args(argv)
    config = args.config or (PRIVATE if os.path.exists(PRIVATE) else SYNTHETIC)
    base = os.path.dirname(os.path.abspath(config))
    with open(config, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    if spec.get("generator"):
        # The synthetic twin: build the data, then run the config it wrote.
        gen = subprocess.run([sys.executable, _expand(spec["generator"], base)],
                             capture_output=True, text=True, timeout=300)
        if gen.returncode != 0:
            print(gen.stdout + gen.stderr, file=sys.stderr)
            return 1
        print(gen.stdout.strip())
        config = _expand(spec["generated_config"], base)
        base = os.path.dirname(config)
        with open(config, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)

    sources = set()
    for r in spec["replays"]:
        sources.add(_expand(r["start_workbook"], base))
        if r.get("known_good_workbook"):
            sources.add(_expand(r["known_good_workbook"], base))
    before = {p: _sha(p) for p in sources}

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    sandbox = os.path.join(HERE, ".sandbox", stamp)
    os.makedirs(sandbox)

    cards, results = [], {}
    for r in spec["replays"]:
        card = Card(r["name"])
        try:
            results[r["name"]] = replay(r, base, sandbox, card)
        except Exception as exc:  # a crash is a FAIL, never a silent skip
            card.add("FAIL", "replay ran to completion", f"{type(exc).__name__}: {exc}")
            results[r["name"]] = {"steps": [], "rows": 0, "compare": {}}
        cards.append(card)

    after = {p: _sha(p) for p in sources}
    hashes_ok = before == after
    body = scorecard(cards, results, config, hashes_ok)
    os.makedirs(args.results, exist_ok=True)
    path = os.path.join(args.results, f"{date.today().isoformat()}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print(body)
    print(f"scorecard: {path}\nsandbox:   {sandbox}")
    if not hashes_ok:
        print("🔴 A SOURCE WORKBOOK CHANGED DURING THE BACKTEST", file=sys.stderr)
        return 5
    return 1 if any(c.failed for c in cards) else 0


if __name__ == "__main__":
    sys.exit(main())
