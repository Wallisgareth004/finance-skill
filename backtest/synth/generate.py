#!/usr/bin/env python3
"""generate.py — a synthetic twin of the real data, for the public backtest.

Writes fake FNB-format exports with every quirk the real ones have, a small
synthetic workbook to import them into, the known-good workbook the replay must
reproduce, and a replay config whose oracles are computed HERE, from the truth
ledger, independently of the pipeline.

    python backtest/synth/generate.py [--out backtest/synth/out]

Deterministic: seeded, so the same bytes every run.

What it deliberately contains (each is a defect the real data exposed):
  · overlapping exports inside one drop                          (D1)
  · a large one-off capital inflow                               (D3)
  · a card settlement and a bounced debit order + its reversal   (D3)
  · crypto buys and a sell                                       (D5)
  · a savings export beside the cheque export                    (D6)
  · a pending (unsettled) head row that settles next drop        (D9)
  · a genuine repeated triple — two identical tolls on one day
  · the 6-line preamble, leading spaces, YYYY/MM/DD, newest-first rows, the
    card's literal `0` balance column, 40-char padded card descriptions
And, since 27 Sep, one case for each later fix and feature:
  · a note file in the last drop, listed as ignored                (D10)
  · a pending-transactions PDF in the last drop                    (D11)
  · card purchases that the card settlement pays, counted once     (D12)
  · an unlabelled row held for the owner, and a later twin labelled (D13)
  · a trip by date range, isolated, paid back by a client          (D14)
  · a supplement-store purchase                                    (D15)
  · caps, idle cash and settings files, and oracles for the month table,
    the caps, the recurring charges and the idle-cash move        (F1-F4)
Every reference is computed HERE from the truth ledger's own labels, sharing
no code with analyse.py.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(SKILL, "tests"))
from helpers import make_pending_pdf, make_workbook, pipeline_dir  # noqa: E402

CHEQUE, SAVINGS, CARD = "10000000001", "10000000002", "400000******0001"
MASK = "400000*0001"
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
START, END = date(2026, 1, 1), date(2026, 4, 30)
SEED_END = date(2026, 1, 31)  # the starting workbook already holds January

# (merchant, category, low, high) — every one covered by publish/rules.public.yaml
MERCHANTS = [
    ("WOOLWORTHS", "Groceries", 80, 900),
    ("CHECKERS", "Groceries", 60, 700),
    ("PICK N PAY", "Groceries", 50, 600),
    ("ENGEN SANDTON", "Fuel", 400, 900),
    ("SASOL RIVONIA", "Fuel", 400, 900),
    ("NANDOS ROSEBANK", "Restaurants", 120, 450),
    ("SPUR MENLYN", "Restaurants", 150, 500),
    ("UBER EATS", "Food delivery", 90, 300),
    ("UBER TRIP", "Transport/Vehicle", 60, 250),
    ("CLICKS", "Medical aid & health", 50, 400),
    ("TAKEALOT", "Retail / clothing", 100, 1200),
]
# D14: the synthetic trip, as the trips file will describe it.
TRIP = {"label": "Coast", "from": date(2026, 3, 16), "to": date(2026, 3, 18),
        "categories": ("Uncategorised", "Restaurants", "Fuel", "Tolls")}
# D12: March card purchases, paid off by the cheque account's 25 Mar settlement.
CARD_MARCH = [
    (date(2026, 3, 8), "COFFEE CORNER CAFE", Decimal("-430.00"), "Restaurants"),
    (date(2026, 3, 9), "SHELL SYNTHVILLE", Decimal("-2000.00"), "Fuel"),
    (date(2026, 3, 26), "PAYMENT - THANK YOU", Decimal("2430.00"), "Transfer — card settlement"),
]
IDLE = {"threshold": Decimal("100000"), "rate_pct": Decimal("7.5")}
CAP = {"name": "Eating out", "monthly": 1000, "categories": ["Restaurants", "Food delivery"]}

SPEND_EXCLUDED = {
    "Savings (Bank Your Change)",
    "Crypto / investments",
    "Transfer — card settlement",
    "Capital — one-off inflow",
    "Income — annuity",
    "Reversal — unpaid debit",
    "Savings transfers",
}


@dataclass
class T:
    date: date
    amount: Decimal
    description: str
    category: str
    balance: Decimal = Decimal("0")
    reversed: bool = False  # a debit cancelled by a reversal


def pos(merchant: str, d: date) -> str:
    """Cheque POS narrative: merchant[0:20] · mask[20:31] · posting date."""
    return f"{merchant[:20]:<20}{MASK}  {d.day:02d} {MON[d.month - 1]}"


def truth_ledger(rng: random.Random) -> list:
    rows: list = []
    d = START
    while d <= END:
        for _ in range(rng.randint(1, 3)):
            m, cat, lo, hi = rng.choice(MERCHANTS)
            cents = rng.randint(lo * 100, hi * 100)
            rows.append(T(d, Decimal(-cents) / 100, pos(m, d), cat))
        if d.day == 20:
            rows.append(
                T(d, Decimal("30000.00"), "ANNUITY PAYOUT 000123", "Income — annuity")
            )
        if d.day == 2:
            rows.append(
                T(
                    d,
                    Decimal("-1850.00"),
                    "MEDSCHEME DEBIT ORDER",
                    "Medical aid & health",
                )
            )
            rows.append(
                T(d, Decimal("-599.00"), "NETFLIX.COM", "Digital subscriptions")
            )
        if d.weekday() == 4:
            rows.append(
                T(
                    d,
                    Decimal("-100.00"),
                    f"BYC DEBIT             {SAVINGS}",
                    "Savings (Bank Your Change)",
                )
            )
        if d.day == 25:
            rows.append(
                T(d, Decimal("-5.00"), "#MONTHLY ACCOUNT FEE", "Bank fees & interest")
            )
        if d == date(2026, 2, 14):  # a genuine repeated triple: two tolls, one day
            rows.append(T(d, Decimal("-27.00"), "N1 TOLL PLAZA", "Tolls"))
            rows.append(T(d, Decimal("-27.00"), "N1 TOLL PLAZA", "Tolls"))
        if d == date(2026, 3, 10):
            rows.append(
                T(
                    d,
                    Decimal("1500000.00"),
                    "LUMP SUM TRANSFER 000789",
                    "Capital — one-off inflow",
                )
            )
        if d == date(2026, 3, 12):
            rows.append(
                T(d, Decimal("-20000.00"), pos("LUNO", d), "Crypto / investments")
            )
            rows.append(
                T(d, Decimal("-5000.00"), pos("LUNO", d), "Crypto / investments")
            )
        if d == date(2026, 3, 18):
            rows.append(
                T(d, Decimal("3000.00"), "LUNO BX00TESTX AB12C", "Crypto / investments")
            )
        if d == date(2026, 3, 25):
            rows.append(
                T(
                    d,
                    Decimal("-2430.00"),
                    "CREDIT CARD PAYMENT",
                    "Transfer — card settlement",
                )
            )
        if d == date(2026, 3, 3):  # a bounced debit order, reversed, re-paid 9 Mar
            first = T(
                d,
                Decimal("-899.00"),
                "FIBRECO DEBIT ORDER",
                "Connectivity",
                reversed=True,
            )
            rows.append(first)
            rows.append(
                T(d, Decimal("899.00"), "MAGTAPE UNPAID", "Reversal — unpaid debit")
            )
            rows.append(
                T(
                    d,
                    Decimal("-8.00"),
                    "#ITEM UNPAID NO FUNDS 07",
                    "Bank fees & interest",
                )
            )
        if d == date(2026, 3, 9):
            rows.append(T(d, Decimal("-899.00"), "FIBRECO DEBIT ORDER", "Connectivity"))
        if d in (date(2026, 2, 19), date(2026, 4, 11)):  # never-seen merchants
            # D13: the first is held for the owner's label; a rule labels the second.
            label = "Uncategorised" if d.month == 2 else "Groceries"
            rows.append(T(d, Decimal("-340.00"), pos("MYSTERY TRADERS", d), label))
        if d == date(2026, 3, 6):  # D15
            rows.append(T(d, Decimal("-420.00"), pos("SYNTH VITAMIN SHOP", d), "Supplements"))
        if d == date(2026, 3, 16):  # D14: the trip
            rows.append(T(d, Decimal("-2000.00"), pos("COASTAL DIVE CENTRE", d), "Uncategorised"))
        if d == date(2026, 3, 17):
            rows.append(T(d, Decimal("-800.00"), pos("ENGEN COAST", d), "Fuel"))
        if d == date(2026, 3, 18):
            rows.append(T(d, Decimal("-100.00"), "N2 TOLL PLAZA", "Tolls"))
        if d == date(2026, 3, 20):  # the client pays the trip back
            rows.append(T(d, Decimal("900.00"), "CLIENT REIMBURSE 000456", "Income — other"))
        d += timedelta(days=1)

    balance = Decimal("20000.00")
    for t in rows:  # chronological; balance AFTER the row
        balance += t.amount
        t.balance = balance
    return rows


PREAMBLE = (
    "ACCOUNT TRANSACTION HISTORY\n\nName:, Test, User\n"
    "Account:, {acct}, [{kind}]\nBalance:, {bal}, {avail}\n\n"
    "Date, Amount, Balance, Description\n"
)


def write_cheque(path, rows, pending=None):
    """Newest first. `pending` is a row printed as an unsettled head row."""
    newest = rows[-1].balance
    lines = [
        PREAMBLE.format(
            acct=CHEQUE, kind="Easy Account", bal=newest, avail=newest - 500
        )
    ]
    if pending is not None:
        lines.append(
            f"{pending.date:%Y/%m/%d}, {pending.amount}, 0.00, {pending.description}\n"
        )
    for t in reversed(rows):
        lines.append(f"{t.date:%Y/%m/%d}, {t.amount}, {t.balance}, {t.description}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def write_savings(path, sweeps):
    balance, out = Decimal("1000.00"), []
    for t in sweeps:
        balance += -t.amount
        out.append((t.date, -t.amount, balance, f"BYC CREDIT            {CHEQUE}"))
    lines = [
        PREAMBLE.format(
            acct=SAVINGS, kind="Savings Account", bal=balance, avail=balance
        )
    ]
    lines += [f"{d:%Y/%m/%d}, {a}, {b}, {x}\n" for d, a, b, x in reversed(out)]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def write_card(path, rng, lo, hi):
    lines = [
        PREAMBLE.format(
            acct=CARD, kind="FNB Aspire Credit Account", bal="-1234.56", avail="N/A"
        )
    ]
    d = hi
    while d >= lo:
        if rng.random() < 0.3:
            desc = f"{'iK *Coffee Corner':<24}{'Johannesburg':<16}"
            lines.append(f"{d:%Y/%m/%d}, -{rng.randint(20, 90)}.00, 0, {desc}\n")
        d -= timedelta(days=1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def slice_(rows, lo, hi):
    return [t for t in rows if lo <= t.date <= hi]


def sheet_row(t, source):
    return (t.date, t.description, t.category, str(t.amount), str(t.balance), source)


def write_card_rows(path, rows):
    """A card export holding exactly `rows` (date, description, amount, _)."""
    lines = [PREAMBLE.format(acct=CARD, kind="FNB Aspire Credit Account",
                             bal="-1234.56", avail="N/A")]
    for d, desc, amount, _cat in sorted(rows, reverse=True):
        lines.append(f"{d:%Y/%m/%d}, {amount}, 0, {desc:<24}{'Synthville':<16}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def trip_rows(rows):
    """D14 reference: debits inside the trip's dates, in its categories."""
    return [t for t in rows if t.amount < 0 and TRIP["from"] <= t.date <= TRIP["to"]
            and t.category in TRIP["categories"]]


def month_reference(rows, year, month):
    """F1/D12/D14 reference for one complete month: (all accounts, cheque only).

    Spend = debits outside SPEND_EXCLUDED, not reversed, not on the isolated
    trip; all-accounts adds the card purchases. Income − spend × 30.44 / days.
    """
    in_month = [t for t in rows if t.date.year == year and t.date.month == month]
    trip = {id(t) for t in trip_rows(in_month)}
    income = sum(t.amount for t in in_month if t.category == "Income — annuity")
    cheque = sum(-t.amount for t in in_month if t.amount < 0 and id(t) not in trip
                 and t.category not in SPEND_EXCLUDED and not t.reversed)
    card = sum(-a for d, _x, a, cat in CARD_MARCH
               if a < 0 and (d.year, d.month) == (year, month) and cat not in SPEND_EXCLUDED)
    days = ((date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1))
            - date(year, month, 1)).days

    def deficit(spend):
        return income - (spend * Decimal("30.44") / days).quantize(Decimal("0.01"))

    return deficit(cheque + card), deficit(cheque), card


def month_deficit(rows, year, month):
    """Independent reference: annuity − spend × 30.44 / days-in-month.

    Spend = every debit not in SPEND_EXCLUDED, minus debits a reversal cancelled.
    Written from the truth ledger's own labels — it shares no code with analyse.py.
    """
    in_month = [t for t in rows if t.date.year == year and t.date.month == month]
    income = sum(t.amount for t in in_month if t.category == "Income — annuity")
    spend = sum(
        -t.amount
        for t in in_month
        if t.amount < 0 and t.category not in SPEND_EXCLUDED and not t.reversed
    )
    days = (
        (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1))
        - date(year, month, 1)
    ).days
    per_month = (spend * Decimal("30.44") / days).quantize(Decimal("0.01"))
    return income - per_month


# The public example rules: publish/rules.public.yaml in the private skill,
# rules.yaml at the root of the public tree. Never the author's private rules.
RULES = next(
    p for p in (os.path.join(SKILL, "publish", "rules.public.yaml"), os.path.join(SKILL, "rules.yaml"))
    if os.path.exists(p)
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    args = ap.parse_args(argv)
    out = args.out
    rng = random.Random(2026)
    rows = truth_ledger(rng)

    for sub in ("drops/d1-2026-02-19", "drops/d2-2026-03-31", "drops/d3-2026-04-30"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    seed = slice_(rows, START, SEED_END)
    make_workbook(
        os.path.join(out, "start.xlsx"), [sheet_row(t, "seed-2026-01") for t in seed]
    )
    make_workbook(
        os.path.join(out, "known_good.xlsx"), [sheet_row(t, "truth") for t in rows]
    )

    # drop 1: cheque 1 Feb → 18 Feb, 19 Feb's first row printed as PENDING;
    # plus the savings export and a card export.
    d1 = os.path.join(out, "drops/d1-2026-02-19")
    settled = slice_(rows, date(2026, 2, 1), date(2026, 2, 18))
    pending = slice_(rows, date(2026, 2, 19), date(2026, 2, 19))[0]
    write_cheque(os.path.join(d1, f"{CHEQUE}.csv"), settled, pending=pending)
    write_savings(
        os.path.join(d1, f"{SAVINGS}.csv"),
        [
            t
            for t in rows
            if t.category == "Savings (Bank Your Change)"
            and t.date <= date(2026, 2, 18)
        ],
    )
    write_card(
        os.path.join(d1, "400000______0001.csv"),
        rng,
        date(2026, 2, 1),
        date(2026, 2, 18),
    )

    # drop 2: 19 Feb → 31 Mar — the pending row settles here.
    write_cheque(
        os.path.join(out, "drops/d2-2026-03-31", f"{CHEQUE}.csv"),
        slice_(rows, date(2026, 2, 19), date(2026, 3, 31)),
    )

    # drop 3: the overlapping pair — 20 Mar → 30 Apr and 20 → 30 Apr.
    d3 = os.path.join(out, "drops/d3-2026-04-30")
    write_cheque(
        os.path.join(d3, f"{CHEQUE}-2.csv"), slice_(rows, date(2026, 3, 20), END)
    )
    write_cheque(
        os.path.join(d3, f"{CHEQUE}.csv"), slice_(rows, date(2026, 4, 20), END)
    )
    # D12: the card's March purchases and its payment received. Card rows are
    # analysed, never written, so they must be in the drop that is analysed.
    write_card_rows(os.path.join(d3, "400000______0001.csv"), CARD_MARCH)
    # D10: a file that is not an export; D11: card spend not yet posted.
    with open(os.path.join(d3, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("synthetic: remember to export the card next time\n")
    pending_rows = [("01 May 2026 09:15:00", "SYNTH CAFE", "99.90"),
                    ("02 May 2026 17:40:00", "SHELL SYNTHVILLE", "650.00")]
    make_pending_pdf(os.path.join(d3, "OnlineBankingDownload.pdf"), pending_rows)

    # The rules, the hold list and the owner's files, as the private skill has them.
    import shutil
    import yaml

    pipeline_dir()
    import model  # noqa: E402  (the pipeline, for the hold list's row hash)

    rules_out = os.path.join(out, "rules.yaml")
    shutil.copy(RULES, rules_out)
    with open(rules_out, "a", encoding="utf-8") as fh:
        fh.write("\n# synthetic: the owner labelled this merchant after being asked (D13)\n"
                 "overrides:\n  - id: mystery\n    pattern: 'MYSTERY TRADERS'\n"
                 "    category: \"Groceries\"\n")
    held = next(t for t in rows if t.description.startswith("MYSTERY") and t.date.month == 2)
    with open(os.path.join(out, "review_hold.private.yaml"), "w", encoding="utf-8") as fh:
        fh.write(f"rows:\n  - {model.row_hash(held)}\n")
    with open(os.path.join(out, "trips.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"trips": [{"label": TRIP["label"], "from": TRIP["from"],
                                   "to": TRIP["to"], "isolate": True,
                                   "categories": list(TRIP["categories"]),
                                   "reimbursement": {"pattern": "REIMBURSE", "within_days": 10}}]},
                       fh, allow_unicode=True)
    with open(os.path.join(out, "caps.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"season": "Synthetic", "caps": [CAP]}, fh, allow_unicode=True)
    with open(os.path.join(out, "advisor.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"idle_cash": {k: str(v) for k, v in IDLE.items()}}, fh)

    april = len(slice_(rows, date(2026, 4, 1), END))
    march_ref = month_deficit(rows, 2026, 3)
    march_all, march_cheque, march_card = month_reference(rows, 2026, 3)
    trip = trip_rows(rows)
    trip_cost = sum(-t.amount for t in trip)
    trip_back = next(t.amount for t in rows if t.description.startswith("CLIENT REIMBURSE"))
    march = [t for t in rows if (t.date.year, t.date.month) == (2026, 3)]
    trip_ids = {id(t) for t in trip}
    cap_actual = sum(-t.amount for t in march if t.amount < 0 and id(t) not in trip_ids
                     and t.category in CAP["categories"])
    cap_actual += sum(-a for d, _x, a, cat in CARD_MARCH if a < 0 and cat in CAP["categories"])
    available = rows[-1].balance - 500  # write_cheque prints available = newest - 500
    forgone = ((available - IDLE["threshold"]) * IDLE["rate_pct"] / 100 / 12).quantize(Decimal("0.01"))
    crypto_bought = sum(
        -t.amount for t in rows if t.category == "Crypto / investments" and t.amount < 0
    )
    config = {
        "rules": rules_out,
        "replays": [
            {
                "name": "SYNTHETIC — three drops onto a January workbook",
                "start_workbook": "start.xlsx",
                "known_good_workbook": "known_good.xlsx",
                "known_good_exact": True,
                "rules": rules_out,
                "args": ["--trips", "trips.yaml", "--caps", "caps.yaml",
                         "--settings", "advisor.yaml"],
                # Each drop names the files written THIS run, so a file left in
                # out/ by an older generator never joins a replay.
                "drops": [
                    {"path": "drops/d1-2026-02-19",
                     "files": [f"{CHEQUE}.csv", f"{SAVINGS}.csv", "400000______0001.csv"]},
                    {"path": "drops/d2-2026-03-31", "files": [f"{CHEQUE}.csv"]},
                    {"path": "drops/d3-2026-04-30",
                     "files": [f"{CHEQUE}-2.csv", f"{CHEQUE}.csv", "400000______0001.csv",
                               "notes.txt", "OnlineBankingDownload.pdf"]},
                ],
                "oracles": {
                    "last_drop_new_rows": april,
                    "charts": 4,
                    "annuity": {"window": ["2026-01-01", "2026-04-30"], "credits": 4},
                    "headline": {
                        "window": ["2026-02-01", "2026-04-30"],
                        "capital": {
                            "category": "Capital — one-off inflow",
                            "amount": "1500000.00",
                        },
                        "crypto_bought": str(crypto_bought),
                    },
                    "month": {
                        "label": "March 2026",
                        "window": ["2026-03-01", "2026-03-31"],
                        "reference": str(march_ref),
                        "tolerance": "0.01",
                        "why_tolerance": "±R0.01: same rows, same definition, computed "
                        "independently from the truth ledger's own labels.",
                    },
                    "uncategorised": {
                        "from": "2026-02-01",
                        "to": "2026-04-30",
                        "limit_pct": 5,
                    },
                    # D10-D15 and F1-F4, graded on the last drop's own run
                    "summary": {
                        "ignored_files": ["notes.txt"],
                        "pending": {"rows": 2, "total": "749.90"},
                        "month": {"month": "2026-03", "deficit": str(march_all),
                                  "deficit_cheque": str(march_cheque),
                                  "spend_card": str(march_card), "supplements": "420.00"},
                        "trip": {"label": TRIP["label"], "isolated": True,
                                 "cost": str(trip_cost), "net": str(trip_back - trip_cost)},
                        "cap": {"month": "2026-03", "name": CAP["name"],
                                "actual": str(cap_actual)},
                        "subscriptions_include": ["NETFLIX.COM", "MEDSCHEME DEBIT ORDER"],
                        "idle_forgone_per_month": str(forgone),
                    },
                    "held": {"unlabelled": ["2026-02-19"], "labelled": ["2026-04-11"],
                             "description": "MYSTERY TRADERS", "label": "Groceries"},
                },
            }
        ],
    }
    with open(os.path.join(out, "replays.yaml"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)  # JSON is valid YAML
    print(
        f"synthetic twin written to {out}: {len(rows)} truth rows, "
        f"{april} expected new in the last drop, March reference R{march_ref}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
