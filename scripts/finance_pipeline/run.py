#!/usr/bin/env python3
"""run.py — the orchestrator. Detect, parse, verify, categorise, dedupe, report.

IN   a drop folder of exports, the workbook, the rules, and the owner's private
     trips / caps / settings files
OUT  JSON on stdout, a dated reports folder, and with --write the new rows
     appended

═══ THE EXIT CONTRACT ═══
    0  clean
    1  bad input
    2  no data
    3  parse or verification failure
    4  a file could not be read (never the same as 2: see EXIT_UNREADABLE)

`ok` is DERIVED from a boolean on each file entry, never asserted and never
inferred from a status string. A sibling pipeline hardcoded `"ok": True` and then
recovered the truth by grepping status prefixes for "error" — so a run in which
every file failed still exited 0, and rewording a status would have silently
restored the bug.

⚠️ Never pipe this into `tail` or background it with `nohup … &` without
capturing `$?`. Both discard the exit status. That is exactly how a month-stale
run of a sibling pipeline was once reported as fresh.

═══ DRY RUN IS THE DEFAULT ═══
Nothing is written to the WORKBOOK without `--write`. Every run produces
`import-preview.tsv` showing precisely what `writer.append_transactions` would
append, rendered through the same column contract.
The LEDGER (ledger.sqlite) is updated on every run, dry or not — it is the run
log and the gap register, and recording dry runs is the point. Since 2026-09-22
a dry run is stamped `flags_json: ["dry-run"]` so the log can tell them apart.

═══ ONLY THE CHEQUE ACCOUNT IS WRITTEN ═══
The credit card is a second account on its own sheet, and spec §11 Q3 defers
merging it. `model.load_existing` reads TRANSACTIONS only, so treating card rows
as new would re-import every one of them on every run, for ever. Card files are
detected, parsed, chain-checked and ANALYSED (D12: card purchases are spend) —
and never queued for a write. Pending card spend (D11) is never written either.

═══ WHAT A RUN PRODUCES ═══
01-cfo.md (the position, the month table, caps, pending, trips), 02-analyst.md
(what changed against the previous window), 03-advisor.md (recurring charges
and THE ONE MOVE), notes-and-gaps.txt (coverage, every file
not read and why), import-preview.tsv (exactly what --write would append), and
with --dashboard, 05-dashboard.pdf. The owner's trips, caps and advisor
settings are optional private files beside the skill; without them those
sections are simply absent.

═══ THE DATED REPORT DIRECTORY IS THIS SCRIPT'S OWN DEFAULT ═══
A sibling pipeline defaults to a flat `reports/` and only its docs add
`$(date)`, so anyone invoking it directly writes to the root, where a stale
file then shadows the fresh one. Here the date is appended by the script, and
`latest` is repointed atomically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import analyse
import caps
import categorise
import dashboard
import detect
import emit
import model
import parse
import pending
import store
import subs
import trips
import verify
import writer

EXIT_CLEAN = 0
EXIT_BAD_INPUT = 1
EXIT_NO_DATA = 2
EXIT_PARSE_FAILURE = 3
# 2026-09-03: distinct from EXIT_NO_DATA on purpose. 2 means "we looked and there is
# nothing"; 4 means "we never got to look". Reporting the second as the first is how
# a sibling pipeline came to tell its owner there was no data when a file simply
# would not open. See detect.Unreadable.
EXIT_UNREADABLE = 4

# The workbook comes from --model or the FINANCE_LIVE_MODEL environment variable,
# read at run time. No machine path lives in this file (2026-09-24).
MODEL_ENV = "FINANCE_LIVE_MODEL"
# The skill root. `reports/` and `ledger.sqlite` are both gitignored there, so
# neither the ledger nor a report can be committed by accident.
SKILL_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_REPORTS = os.path.join(SKILL_ROOT, "reports")
DEFAULT_DB = os.path.join(SKILL_ROOT, "ledger.sqlite")
# D14: the owner's trips — private, gitignored, optional.
DEFAULT_TRIPS = os.path.join(SKILL_ROOT, "trips.private.yaml")
# F2: the owner's monthly caps — private, gitignored, optional.
DEFAULT_CAPS = os.path.join(SKILL_ROOT, "caps.private.yaml")
# F4: the owner's idle-cash buffer and rate — private, gitignored, optional.
DEFAULT_SETTINGS = os.path.join(SKILL_ROOT, "advisor.private.yaml")
WINDOW_DAYS = 90
STALE_AFTER_DAYS = 7


def _dated(base: str, today: date) -> str:
    """Always a dated subdirectory, even if the caller forgot."""
    stamp = today.isoformat()
    if os.path.basename(os.path.normpath(base)) == stamp:
        return base
    return os.path.join(base, stamp)


def _repoint_latest(base: str, target: str) -> None:
    link = os.path.join(base, "latest")
    tmp = link + ".tmp"
    try:
        if os.path.islink(tmp) or os.path.exists(tmp):
            os.remove(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, link)
    except OSError:
        # A filesystem without symlinks is not a reason to lose the run.
        pass


def _emit(summary: dict, code: int) -> int:
    print(json.dumps(summary, indent=2, default=str))
    return code


def _why_ignored(name: str, path: str) -> str:
    """The reason a drop entry that is not a CSV export was not read."""
    if os.path.isdir(path):
        return "a folder: exports inside it are not read (move them into the drop itself)"
    if name.startswith("."):
        return "a hidden system file"
    if name.lower().endswith(".pdf"):
        return "a PDF: only FNB CSV exports are read"
    return "not a CSV export"


def _read_pending(folder: str, ignored: list) -> tuple:
    """D11: a "Transaction history" PDF is READ, not ignored.

    Returns (pending rows, files read, still ignored). A PDF that is not one, or
    that cannot be read (pypdf missing, damaged file), stays in the ignored list
    with the reason — never skipped in silence (D10).
    """
    rows, files, still_ignored = [], [], []
    for item in ignored:
        path = os.path.join(folder, item["file"])
        if not (os.path.isfile(path) and item["file"].lower().endswith(".pdf")):
            still_ignored.append(item)
            continue
        try:
            _header, found = pending.read_pdf(path)
        except (pending.PendingFormatError, pending.PendingUnavailable) as exc:
            still_ignored.append({"file": item["file"], "why": f"a PDF, not read: {exc}"})
            continue
        rows.extend(found)
        files.append(item["file"])
    return rows, files, still_ignored


def _collect(folder: str) -> tuple:
    """Detect every CSV. Returns (entries, unrecognized, unreadable, ignored).

    2026-09-03: `unreadable` is a THIRD list, not a flavour of `unrecognized`.
    An unreadable file used to raise straight out of this function and take the whole
    run — every other export in the folder included — down with a raw traceback.

    D10, 2026-09-27: `ignored` is the FOURTH. Anything that is not a CSV used to
    be skipped without a word — a statement PDF in the drop appeared nowhere.
    Every such entry is now listed, with the reason, in the run JSON and in
    notes-and-gaps.txt.
    """
    entries, unrecognized, unreadable, ignored = [], [], [], []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or not name.lower().endswith(".csv"):
            ignored.append({"file": name, "why": _why_ignored(name, path)})
            continue
        try:
            det = detect.sniff(path)
        except detect.Unreadable as exc:
            unreadable.append({"file": name, "why": exc.reason})
            continue
        except detect.UnknownFormat as exc:
            unrecognized.append({"file": name, "why": str(exc)})
            continue
        entries.append({"file": name, "path": path, "detected": det, "ok": True})
    return entries, unrecognized, unreadable, ignored


def main(argv=None) -> int:
    started = time.time()
    ap = argparse.ArgumentParser(description="FNB exports → Excel model: verify, dedupe, report.")
    ap.add_argument("--input", required=True, help="folder of FNB CSV exports")
    ap.add_argument(
        "--model", default=None, help=f"the Excel workbook (default: ${MODEL_ENV})"
    )
    ap.add_argument("--reports", default=None, help="report base directory")
    ap.add_argument("--db", default=None, help="ledger.sqlite path")
    ap.add_argument("--rules", default=None, help="rules.yaml path")
    ap.add_argument(
        "--trips", default=None, help="trips file (default: trips.private.yaml if present)"
    )
    ap.add_argument(
        "--caps", default=None, help="caps file (default: caps.private.yaml if present)"
    )
    ap.add_argument(
        "--settings", default=None,
        help="advisor settings (default: advisor.private.yaml if present)",
    )
    ap.add_argument("--window-start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--window-end", default=None, help="YYYY-MM-DD")
    ap.add_argument(
        "--dashboard",
        action="store_true",
        help="also draw 05-dashboard.pdf beside the reports (needs matplotlib)",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="actually append to the workbook (default is a dry run)",
    )
    args = ap.parse_args(argv)

    today = date.today()
    dry_run = not args.write
    summary: dict = {"ok": False, "dry_run": dry_run, "today": today.isoformat()}

    if not os.path.isdir(args.input):
        summary["error"] = f"input folder not found: {args.input}"
        return _emit(summary, EXIT_BAD_INPUT)

    reports_base = args.reports or DEFAULT_REPORTS
    reports_dir = _dated(reports_base, today)

    entries, unrecognized, unreadable, ignored = _collect(args.input)
    pending_rows, pending_files, ignored = _read_pending(args.input, ignored)
    summary["unrecognized_files"] = unrecognized
    summary["unreadable_files"] = unreadable
    summary["ignored_files"] = ignored
    summary["files_detected"] = [
        {
            "file": e["file"],
            "account": e["detected"].account_number,
            "kind": e["detected"].account_kind,
            "ok": True,
        }
        for e in entries
    ]
    if not entries:
        # 2026-09-03: check "could not read" BEFORE "nothing here". If a file could not
        # be opened we know nothing about its contents, and saying "no exports" points
        # the finger at the owner's data instead of at the disk.
        if unreadable:
            summary["error"] = (
                f"could not read {len(unreadable)} file(s) in {args.input} — "
                "this is NOT 'no exports'. The bytes were never obtained, so nothing "
                "can be concluded about the data. Most likely cause: iCloud eviction "
                "(Optimize Mac Storage)."
            )
            return _emit(summary, EXIT_UNREADABLE)
        summary["error"] = f"no recognised FNB exports in {args.input}"
        return _emit(summary, EXIT_NO_DATA)

    # ── parse and verify ──────────────────────────────────────────────────
    ruleset = categorise.load(args.rules)
    ruleset.apply_all(pending_rows)
    chain_report, parsed_by_file, fatal = [], [], False
    for entry, meta in zip(entries, summary["files_detected"]):
        det = entry["detected"]
        try:
            txns = parse.parse_file(det)
        except Exception as exc:
            meta["ok"] = False
            meta["error"] = str(exc)
            fatal = True
            continue
        chain = verify.check_chain(txns, det)
        anchor_ok = verify.check_anchor(txns, det)
        # A row with balance None opts out of the chain. Exactly one may — the
        # unsettled head row (see parse._null_unsettled_head). More than that and
        # the chain has been split into segments whose levels are never
        # reconciled against each other, which is how a fabricated balance slips
        # through. Cap it rather than trusting `ok`.
        # Card exports carry the literal string `0` in every Balance cell, so the
        # whole file is legitimately unchecked — the cap applies to cheque only.
        opted_out = (
            sum(1 for t in txns if t.balance is None)
            if det.account_kind in detect.BALANCED_KINDS
            else 0
        )
        chain_report.append(
            {
                "file": entry["file"],
                "ok": chain.ok,
                "checked": chain.checked,
                "anchor_ok": anchor_ok,
                "unchecked_rows": opted_out,
                "breaks": [str(b) for b in chain.breaks],
            }
        )
        if not chain.ok:
            # A chain break is FATAL. Not one break across the author's real
            # exports makes it the strongest correctness signal available; a
            # warning wastes it.
            meta["ok"] = False
            meta["error"] = f"balance chain broken in {len(chain.breaks)} place(s)"
            fatal = True
            continue
        if det.account_kind in detect.BALANCED_KINDS and not anchor_ok:
            # The preamble's `Balance:` must equal the newest settled row. This
            # is the only check on the TOP of the file, and it was dead code
            # until 2026-08-23 — verify.py defined it and nothing ever called it.
            meta["ok"] = False
            meta["error"] = "anchor mismatch — preamble Balance != newest settled row"
            fatal = True
            continue
        if opted_out > 1:
            meta["ok"] = False
            meta["error"] = (
                f"{opted_out} rows carry no balance — only the unsettled head row "
                "may opt out of the chain"
            )
            fatal = True
            continue
        ruleset.apply_all(txns)
        meta["rows"] = len(txns)
        meta["slug"] = txns[0].source_slug if txns else "export-empty"
        parsed_by_file.append((entry, det, txns))

    summary["chain"] = chain_report
    if fatal:
        summary["error"] = "parse or verification failure — nothing was written"
        return _emit(summary, EXIT_PARSE_FAILURE)
    if not parsed_by_file:
        summary["error"] = "no transactions parsed"
        return _emit(summary, EXIT_NO_DATA)

    # ── the model, the gap register, and what is genuinely new ────────────
    args.model = args.model or os.environ.get(MODEL_ENV)
    if not args.model:
        summary["error"] = (
            f"no workbook: pass --model or set {MODEL_ENV}. Nothing was written."
        )
        return _emit(summary, EXIT_BAD_INPUT)
    if not os.path.exists(args.model):
        summary["error"] = f"model workbook not found: {args.model}"
        return _emit(summary, EXIT_BAD_INPUT)

    try:
        existing = model.load_existing(args.model)
    except model.ModelReadError as exc:
        # D8, 2026-09-24: this used to escape as a traceback with exit 1 (the
        # code for a bad input PATH). A ledger row we cannot read is a
        # verification failure, and every exit path prints JSON.
        summary["error"] = f"workbook unreadable — nothing was written: {exc}"
        return _emit(summary, EXIT_PARSE_FAILURE)

    # D1, 2026-09-24: merge overlapping exports of the SAME account first, then
    # dedupe the merged list against the workbook. Deduping each file against the
    # workbook separately counted every overlapping row once per file.
    by_account: dict = {}
    for entry, det, txns in parsed_by_file:
        account = (det.account_kind, det.account_number or entry["file"])
        by_account.setdefault(account, []).append(txns)

    cheque_accounts = {acct for kind, acct in by_account if kind == "cheque"}
    if len(cheque_accounts) > 1:
        # TRANSACTIONS is ONE account's ledger. Two cheque accounts in a drop
        # would interleave two balance chains on one sheet.
        summary["error"] = (
            f"{len(cheque_accounts)} different cheque accounts in one drop — "
            "TRANSACTIONS holds one account. Split the drop."
        )
        return _emit(summary, EXIT_BAD_INPUT)

    to_write, card_rows, savings_rows, across_files, unsettled = [], 0, 0, 0, 0
    card_incoming = []  # D12: analysed, never written
    for (kind, _account), files in by_account.items():
        merged, dropped = model.dedupe_drop(files)
        across_files += dropped
        if kind == "card":
            card_incoming.extend(t for t in merged if t.is_transaction)
        fresh = model.new_rows(merged, existing)
        if kind == "cheque":
            # Filter on is_transaction: the sheet holds a truncated
            # CR.INT.RATE row that can never match the parser's correct one.
            # D9, 2026-09-24: an unsettled head row (balance None, see
            # parse._null_unsettled_head) is an authorisation, not a posting.
            # Written, it kept its missing balance for ever once the settled
            # copy deduped against it. It is imported from the export in which
            # it settles.
            unsettled += sum(1 for t in fresh if t.is_transaction and t.balance is None)
            to_write.extend(
                [t for t in fresh if t.is_transaction and t.balance is not None]
            )
        elif kind == "savings":
            # D6: reported, chain-checked, never written to the cheque sheet.
            savings_rows += sum(1 for t in fresh if t.is_transaction)
        else:
            card_rows += len(fresh)

    # D2, 2026-09-24: judge coverage on what the ledger WOULD hold after this
    # import — workbook ∪ incoming — not on the workbook as it stands. On a real
    # drop the dry run reported a gap and "no annuity credit" while holding the
    # very rows that filled the gap and carried the credits.
    sheet_dates = sorted(
        {key[0] for key in existing} | {t.date.isoformat() for t in to_write}
    )
    gaps = verify.find_gaps([date.fromisoformat(d) for d in sheet_dates])

    summary["rows"] = {
        "parsed": sum(len(t) for _, _, t in parsed_by_file),
        "duplicates_across_files": across_files,
        "new": len(to_write),
        "card_new_not_written": card_rows,
        "savings_new_not_written": savings_rows,
        "pending_not_written": unsettled,
    }

    # ── freshness gate ────────────────────────────────────────────────────
    latest = max(
        (t.date for _, _, txns in parsed_by_file for t in txns),
        default=None,
    )
    if sheet_dates:
        latest = (
            max(latest, date.fromisoformat(sheet_dates[-1]))
            if latest
            else date.fromisoformat(sheet_dates[-1])
        )
    behind = (today - latest).days if latest else None
    summary["freshness"] = {
        "latest_data_date": latest.isoformat() if latest else None,
        "days_behind": behind,
        "stale": bool(behind is not None and behind > STALE_AFTER_DAYS),
    }

    # ── analysis ──────────────────────────────────────────────────────────
    end = date.fromisoformat(args.window_end) if args.window_end else (latest or today)
    start = (
        date.fromisoformat(args.window_start)
        if args.window_start
        else end - timedelta(days=WINDOW_DAYS - 1)
    )
    window = analyse.Window(start, end)

    try:
        sheet_txns = _sheet_transactions(args.model)
    except (ValueError, ArithmeticError) as exc:
        summary["error"] = f"workbook unreadable for analysis — nothing was written: {exc}"
        return _emit(summary, EXIT_PARSE_FAILURE)
    summary["relabelled_at_analysis"] = _relabel_uncategorised(sheet_txns, ruleset)
    # Same union as the gap register: analyse the ledger this run would leave.
    sheet_txns = sheet_txns + to_write
    # D12, 2026-09-27: the card's purchases are spend. They come from the
    # workbook's CREDIT CARD sheet and from any card export in this drop,
    # merged so each purchase counts once. Analysed only, never written.
    try:
        card_rows_all = model.merge_card_rows(model.load_card_sheet(args.model), card_incoming)
    except model.ModelReadError as exc:
        summary["error"] = f"CREDIT CARD sheet unreadable — nothing was written: {exc}"
        return _emit(summary, EXIT_PARSE_FAILURE)
    _relabel_uncategorised(card_rows_all, ruleset)
    # D11: pending card spend, minus anything that has since posted. Reported
    # apart from posted spend; never written (it is not in to_write).
    still_pending, retired = pending.drop_posted(
        pending_rows, [t for t in sheet_txns if t.amount < 0]
    )
    cash = _cash(parsed_by_file)
    try:
        trip_list = trips.load(args.trips or DEFAULT_TRIPS)
        season, cap_list = caps.load(args.caps or DEFAULT_CAPS)
    except (trips.TripError, caps.CapsError, KeyError, ValueError) as exc:
        summary["error"] = f"trips or caps file unusable — nothing was written: {exc}"
        return _emit(summary, EXIT_BAD_INPUT)
    cfo = analyse.cfo(
        txns=sheet_txns + card_rows_all, gaps=gaps, window=window, cash=cash,
        today=today, pending=still_pending, trips=trip_list,
    )
    summary["trips"] = cfo["trips"]
    # F1: the same ledger, month by month.
    cfo["months"] = analyse.month_table(
        sheet_txns + card_rows_all, window, gaps, today, pending=still_pending, trips=trip_list
    )
    summary["months"] = cfo["months"]
    # F2: each month against the caps.
    cfo["caps"], cfo["caps_season"] = caps.table(cfo["months"], cap_list), season
    summary["caps"] = cfo["caps"]
    in_window = [r for r in card_rows_all if window.contains(r.date)]
    summary["card"] = {
        "rows": len(in_window),
        "spend": cfo["spend_by_account"].get("card", "0.00"),
        "history_from": min((r.date for r in card_rows_all), default=None),
        "history_to": max((r.date for r in card_rows_all), default=None),
        "from_sheet_only": sum(r.source == "sheet" for r in card_rows_all),
        "from_exports_only": sum(r.source == "export" for r in card_rows_all),
        "on_both": sum(r.source == "both" for r in card_rows_all),
    }
    summary["pending"] = dict(cfo["pending"], files=pending_files, already_posted=retired)
    summary["coverage"] = cfo["coverage"]
    summary["deficit"] = cfo["deficit"]
    summary["deficit_blocked_by"] = cfo["deficit_blocked_by"]
    summary["annuity_credits"] = cfo["annuity_credits"]
    summary["analysed_over"] = "workbook + incoming rows" if to_write else "workbook"

    # ── the ledger ────────────────────────────────────────────────────────
    db_path = args.db or DEFAULT_DB
    conn = store.connect(db_path)
    try:
        for entry, det, txns in parsed_by_file:
            store.record_gaps(conn, det.account_number or entry["file"], [])
        store.record_gaps(conn, "TRANSACTIONS", gaps)
        run_at = datetime.now().replace(microsecond=0).isoformat()
        for entry, det, txns in parsed_by_file:
            store.record_run(
                conn,
                run_at=run_at,
                source_slug=txns[0].source_slug if txns else "export-empty",
                account=det.account_number or entry["file"],
                rows_parsed=len(txns),
                rows_new={
                    "cheque": len(to_write),
                    "savings": savings_rows,
                }.get(det.account_kind, card_rows),
                chain_ok=True,
                exit_code=EXIT_CLEAN,
                # A dry run must be distinguishable from an import in the run log.
                # Before 2026-09-22 it was not: rows_new recorded what WOULD have
                # been appended, and nothing marked that it was not appended.
                flags=(["dry-run"] if dry_run else None),
            )
        ledger = store.savings_ledger(conn)
        summary["ledger_counts"] = store.table_counts(conn)
    finally:
        conn.close()

    # F3, 2026-09-27: recurring charges found in the analysed window (this was
    # an empty list, so SUBS mode had nothing to say).
    found = subs.find([r for r in sheet_txns + card_rows_all if window.contains(r.date)])
    summary["subscriptions"] = {
        "found": len(found),
        "names": [s["name"] for s in found],
        "monthly_total": str(sum((s["monthly"] for s in found), Decimal("0"))),
    }
    # F4: idle cash above the buffer, measured from the account's own interest.
    idle = analyse.idle_cash(
        cash.get("available"), sheet_txns, window,
        _idle_settings(args.settings or DEFAULT_SETTINGS),
    )
    summary["idle_cash"] = idle
    advisor = analyse.advisor(subs=found, savings_ledger=ledger, today=today, idle=idle)

    # ── reports ───────────────────────────────────────────────────────────
    os.makedirs(reports_dir, exist_ok=True)
    header = (
        f"Generated {today.isoformat()} · latest data "
        f"{latest.isoformat() if latest else 'n/a'} · window {window} · "
        f"{cfo['coverage']['covered_days']} of "
        f"{cfo['coverage']['elapsed_days']} days covered\n\n"
    )
    _write_text(reports_dir, "01-cfo.md", header + emit.cfo_markdown(cfo))
    # The analyst lens: this window against the one of the same length before it.
    prev_window = analyse.Window(
        window.start - timedelta(days=window.elapsed_days), window.start - timedelta(days=1)
    )
    analyst = analyse.analyst(
        txns=sheet_txns + card_rows_all, prev=sheet_txns + card_rows_all, window=window,
        prev_window=prev_window, gaps=gaps,
    )
    _write_text(
        reports_dir, "02-analyst.md",
        header + f"Compared with {prev_window}.\n\n" + emit.analyst_markdown(analyst),
    )
    _write_text(reports_dir, "03-advisor.md", header + emit.advisor_markdown(advisor))
    _write_text(
        reports_dir,
        "notes-and-gaps.txt",
        emit.notes_block(
            gaps=gaps,
            sources=[t[2][0].source_slug for t in parsed_by_file if t[2]],
            coverage_start=date.fromisoformat(sheet_dates[0]) if sheet_dates else today,
            coverage_end=latest or today,
            date_stamp=today,
            ignored=ignored,
        ),
    )
    _write_text(
        reports_dir, "import-preview.tsv", emit.transactions_tsv(to_write, header=True)
    )
    _repoint_latest(reports_base, reports_dir)
    summary["reports"] = reports_dir
    if args.dashboard:
        # F5: drawn from the same numbers as the reports, into the same dated
        # folder — never into the repository.
        try:
            summary["dashboard"] = {
                "written": dashboard.render(
                    os.path.join(reports_dir, "05-dashboard.pdf"), cfo, cfo["months"],
                    cfo["caps"], cfo["caps_season"], today,
                )
            }
        except dashboard.DashboardUnavailable as exc:
            summary["dashboard"] = {"skipped": str(exc)}

    # ── the write ─────────────────────────────────────────────────────────
    if to_write and args.write:
        result = writer.append_transactions(args.model, to_write)
        summary["write"] = {
            "rows_added": result.rows_added,
            "first_row": result.first_row,
            "last_row": result.last_row,
            "backup": result.backup_path,
            "charts_before": result.charts_before,
            "charts_after": result.charts_after,
        }
    elif to_write:
        summary["write"] = {"skipped": "dry run — pass --write to append"}
    else:
        summary["write"] = {"skipped": "nothing new to append"}

    summary["elapsed_sec"] = round(time.time() - started, 1)
    if unreadable:
        # 2026-09-03: some files parsed, others could not be opened. Reporting that as
        # a clean run is the confident-and-wrong shape this skill exists to prevent —
        # a file it could not read is a coverage gap, and coverage gaps are fatal here.
        summary["ok"] = False
        summary["error"] = (
            f"could not read {len(unreadable)} file(s) — the run is INCOMPLETE. "
            "Do not treat the figures below as full coverage."
        )
        return _emit(summary, EXIT_UNREADABLE)
    summary["ok"] = True
    return _emit(summary, EXIT_CLEAN)


def _write_text(folder: str, name: str, body: str) -> None:
    with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
        handle.write(body)


def _sheet_transactions(xlsx_path: str) -> list:
    """Read TRANSACTIONS as analysis rows. Read-only, never saved."""
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb[model.TXNS_SHEET]
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()
        rows = []
        for values in ws.iter_rows(min_row=model.FIRST_DATA_ROW, values_only=True):
            if values[model.COL_DATE] is None and values[model.COL_AMOUNT] is None:
                continue
            rows.append(_SheetRow(values))
        return rows
    finally:
        wb.close()


def _relabel_uncategorised(rows: list, ruleset) -> int:
    """Give sheet rows stored as `Uncategorised` the label today's rules assign.

    IN MEMORY ONLY — the workbook is never touched. A row imported before its
    rule existed keeps its stale label on the sheet for ever (a large one-off
    credit did), and analysing it under that label is exactly D3. A category a
    human typed is never overridden: only the fallback bucket is re-read.
    """
    relabelled = 0
    for row in rows:
        if row.category not in ("", ruleset.fallback):
            continue
        category, rule_id, _ = ruleset.classify(row)
        row.held = rule_id == categorise.HELD
        if row.held:
            continue
        if rule_id is not None and category != row.category:
            row.category = category
            relabelled += 1
    return relabelled


class _SheetRow:
    """The five attributes analyse reads, off a sheet row."""

    __slots__ = ("date", "description", "category", "amount", "is_transaction", "held")

    def __init__(self, values):
        from decimal import Decimal

        self.date = values[model.COL_DATE]
        self.description = values[model.COL_DESCRIPTION] or ""
        self.category = values[2] or ""
        self.amount = Decimal(str(values[model.COL_AMOUNT]))
        self.is_transaction = True
        self.held = False


def _idle_settings(path: str) -> dict | None:
    """F4: the `idle_cash` block of the private advisor settings, or None."""
    if not os.path.exists(path):
        return None
    import yaml

    with open(path, encoding="utf-8") as fh:
        block = (yaml.safe_load(fh) or {}).get("idle_cash")
    if not block:
        return None
    return {k: Decimal(str(v)) for k, v in block.items()}


def _cash(parsed_by_file: list) -> dict:
    """Accessible cash from the exports' own preambles — available, not book,
    plus card headroom. Neither figure exists on the BALANCE sheet."""
    cash: dict = {}
    for _entry, det, _txns in parsed_by_file:
        if det.account_kind == "cheque" and det.available is not None:
            cash["available"] = det.available
        if det.account_kind == "card" and det.available is not None:
            cash["card_headroom"] = det.available
    return cash


if __name__ == "__main__":
    sys.exit(main())
