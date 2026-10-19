# finance-pipeline

A personal-finance pipeline for FNB (South Africa) bank exports. It reads the files
you download from online banking, proves they are internally consistent, categorises
every row, appends the new ones to an Excel model without breaking it, and reports
spend, income, the monthly deficit and the one move worth making.

**It refuses to give you a number it can't prove.** Most of the code exists to decide
when *not* to answer.

It runs entirely on your own machine. It needs no account, no API key and no network
connection, and a test proves that.

## What a run does

```
python scripts/finance_pipeline/run.py --input path/to/drop --model path/to/model.xlsx
```

1. **Reads the drop**: FNB CSV exports (cheque, savings and credit card) and FNB's
   "Transaction history" PDF, which lists card spend that has not posted yet. Every
   other file is listed with the reason it was not read.
2. **Proves each export**: every row's balance must equal the next row's balance plus
   its amount, and the file's stated balance must match its newest row. One break
   stops the run.
3. **Categorises** every row with an ordered rule table.
4. **Works out what is genuinely new** against the workbook, and with `--write` appends
   it: backup first, verify, then an atomic replace. Nothing is written without
   `--write`.
5. **Reports**, into a dated folder:

| File | What it holds |
|---|---|
| `01-cfo.md` | The position: coverage, income, spend, the deficit (or the refusal), a month-by-month table, caps against actual, pending card spend, trips |
| `02-analyst.md` | What changed against the previous window, ranked by rand |
| `03-advisor.md` | Recurring charges ranked by cost, and **THE ONE MOVE** |
| `notes-and-gaps.txt` | Coverage, every gap, every file not read and why |
| `import-preview.tsv` | Exactly what `--write` would append |
| `05-dashboard.pdf` | Four pages drawn from the same numbers (`--dashboard`) |

## The problem

Bank exports look simple and aren't:

- Rows are newest-first, and the dates are **not monotonic**. Sort them and you destroy the
  only integrity check you have.
- Descriptions are fixed-width and unquoted. Some contain commas, and a normal CSV reader
  silently truncates them.
- People export overlapping date ranges. Import both files and you have duplicates that
  each file's own checks can't see.
- Two identical rows (same day, same merchant, same amount) are often **two real
  payments**. A naive dedupe deletes real money.
- One large credit (an inheritance, a transfer) can swamp a month. If it's netted against
  spend, a real deficit prints as a large surplus. The report looks confident and is
  wrong.
- A credit card's purchases are paid by a single settlement from the cheque account.
  Count the purchases and the settlement and you double-count; drop the card and its
  spend vanishes.
- Card spend is *authorised* days before it *posts*. Until then it exists only in a PDF.
- The workbook has charts. `openpyxl`'s `save()` does not round-trip charts, so a normal
  write deletes them.

## Architecture

```
detect → parse → verify → categorise → model → analyse → emit → writer
                                                          (run.py orchestrates)
```

| Module | Job |
|---|---|
| `detect.py` | Identifies an export by its header signature, never its filename. Reads the preamble for the account, its kind (cheque / savings / card) and the balances. Tells "could not read the file" (exit 4) apart from "no data" (exit 2). |
| `parse.py` | Splits on the first three commas only, because the description is the last column. Keeps row order. Nulls the balance of an unsettled head row, but only when the next row proves it. |
| `verify.py` | The balance chain, the anchor check, and the gap register (runs of 3+ days with no rows). |
| `pending.py` | Card authorisations from the "Transaction history" PDF: pairs each row with its amount across page breaks, refuses anything it cannot pair exactly, and retires a row once it posts. |
| `categorise.py` | An ordered rule table: amount-conditional overrides, then first match wins, then a fallback. A byte-exact vocabulary, because the workbook's `SUMIFS` match on it. Rows already put to the owner for a label are held unlabelled until answered. |
| `model.py` | What is genuinely new: overlapping files merged as a multiset, then compared with the workbook as a `Counter` keyed on `(date, description, amount-in-cents)`. Also reads the card sheet and merges it with card exports, each purchase once. |
| `trips.py` · `caps.py` · `subs.py` | Trips by date range (isolated from the run-rate, reimbursements paired); monthly caps; recurring charges. |
| `analyse.py` | The CFO, analyst and advisor figures. Only refusal-aware functions can turn a total into a monthly rate. |
| `emit.py` · `dashboard.py` | The reports and the dashboard. Nothing is computed there. |
| `writer.py` | Appends rows (or fixes cells) by patching the sheet XML inside the `.xlsx` zip; every other part is copied byte-for-byte. Backup → temp copy → verify (well-formed XML, the real row count, the chart count, a real date style) → atomic replace. |
| `store.py` | The only SQLite: the run log, the gap register, decisions and whether they were kept. |

## The refusal logic

The deficit is `null` (and the report says **REFUSED** and names the export to pull)
when any of these holds:

1. **A coverage gap intersects the window.** Monthly rates are divided by *covered*
   days, never elapsed days. A 12-day hole in a 90-day window otherwise understates
   monthly spend by 13.3% (12 of 90 days).
2. **There is no income credit in the window.** Missing income is not read as zero.
3. **Uncategorised credits exceed 5% of gross spend.** A credit nobody has named might be
   income, capital or a transfer, and each one changes the answer. Uncategorised credits
   are **never** netted against spend. They are quarantined and listed.

Money that moved but was neither earned nor consumed has its own buckets, and is never
counted as spend or income: capital (one-off inflows), transfers between your own
accounts (the card settlement, both legs), savings sweeps, crypto in both directions,
and one-off trips kept out of the run-rate. Transfers, savings sweeps and net crypto
outflow still count in **cash burn**, because they left the account.

When uncategorised **debits** exceed 5% of spend, the deficit is still computed, since
that money really left. But the report marks it **PROVISIONAL** and states the bound:
labelling those rows could move the deficit by up to their monthly total.

A deficit is stated for **complete months only** in the month-by-month table. A month the
window cuts short shows its spend to date and nothing scaled up.

## Privacy design

- **Local only.** No network, no accounts, no keys. `tests/test_no_network.py` reads every
  module's syntax tree and fails if any imports a network library or reads an environment
  variable other than the pipeline's own `FINANCE_*` settings.
- **Real data never enters the repository.** `.gitignore` excludes exports, workbooks,
  statements, the ledger, reports and every private file, and the publish gate *refuses*
  (not warns) a tree that holds any of them, before anything else runs.
- **Two independent leak scanners** run over the tree before publication. One matches a
  token list built from the private data; the other shares no code with it, compares
  numbers as numbers in any spelling, derives identities from the machine, and finds
  secrets by shape and entropy. It prints file and line, never the matched text. (Both
  are private tools: they read the real data they guard.)
- **The owner's life stays in private files**: merchant rules, trips, caps, advisor
  settings and the list of rows awaiting a label. The public `rules.yaml` is a generic
  example set that covers the synthetic data.
- **Reports stay local and category-level** where it matters: pending card spend, trips
  and the dashboard never show a merchant.
- **Every test and every backtest fixture is synthetic.** Names, account numbers, dates,
  amounts and merchants are invented; only FNB's file format is copied.

## Run it on synthetic data

Tested on Python 3.14. Required: `openpyxl`, `pyyaml`. Optional: `pypdf` (the pending
PDF), `matplotlib` (the dashboard). Versions are pinned in `requirements.txt` (and
`requirements-dev.txt` for the tests). With [uv](https://docs.astral.sh/uv/):

```bash
# the test suite
uv run --with-requirements requirements-dev.txt python -m pytest tests -q

# the synthetic backtest: generates the twin, replays it, grades every oracle
uv run --with-requirements requirements.txt \
  python backtest/run_backtest.py --config backtest/replays.synthetic.yaml

# the pipeline on a folder of exports: a dry run unless you add --write
uv run --with-requirements requirements.txt \
  python scripts/finance_pipeline/run.py --input path/to/exports --model path/to/model.xlsx
echo "exit=$?"
```

Don't pipe `run.py` into `tail` without capturing `$?`.

| Exit | Meaning |
|---|---|
| 0 | clean |
| 1 | bad input |
| 2 | no data |
| 3 | parse or verification failure |
| 4 | a file could not be read. This is **not** the same as "no data". |

## Tests and the backtest

- **The public suite: 334 tests**, every one on synthetic data, none skipped. The
  real-data pins (they read the author's exports) are private and not in this
  repository.
- **The synthetic backtest: 24 oracles.** `backtest/run_backtest.py` replays three bank
  drops in date order onto **sandbox copies** of a workbook through the real CLI, then
  grades the result. `backtest/synth/generate.py` builds the drops with every quirk the
  real data has, and computes every expected value from its own truth ledger,
  independently of `analyse.py`. Source workbooks are hashed before and after; the run
  fails if any changed.

| Oracle | Synthetic result |
|---|---|
| every drop exits 0 · balance chain | ✅ 3 of 3 · 0 breaks in 277 comparisons |
| new rows from the overlapping last drop | ✅ 69 (26 overlap copies collapsed) |
| idempotent · duplicate keys · charts | ✅ re-run adds 0 · 0 in 291 rows · 4 of 4 |
| `TRANSACTIONS` = known-good · other sheets | ✅ 291 = 291 · cell-identical |
| annuity credits · capital and crypto not in spend | ✅ 4 of 4 · ✅ ✅ |
| a month's deficit against an independent reference | ✅ Δ R0.00 |
| D10 every non-export file listed · D11 pending reported apart | ✅ · ✅ |
| D12 card purchases counted once · D13 held row stays unlabelled | ✅ · ✅ |
| D14 trip isolated, reimbursement paired · D15 supplements | ✅ · ✅ |
| F1 month table, both deficits · F2 caps · F3 recurring charges · F4 idle cash | ✅ · ✅ · ✅ · ✅ |
| uncategorised under 5% | ✅ |

On the author's real data (private, not in this repo) the same harness replays real
drops onto a copy of the workbook as it stood before the import, reproduces the live
`TRANSACTIONS` sheet exactly (multiset-identical), keeps every other sheet cell-identical
and every chart, finds no balance-chain break, and leaves the source workbooks
byte-identical. `backtest/trace_report.py` rebuilds every headline figure in the CFO
report from rows with a separate, plain grouping, and exits non-zero on any mismatch.

## Defects found and fixed

The backtest was built after a dry run printed a confident, wrong monthly total.
Replaying real history then kept turning defects up. Every one has a failing test
written first, from the data that exposed it.

| | Defect | How it showed up | Fix |
|---|---|---|---|
| D1 | Overlapping exports in one drop imported twice | Every row of the overlap counted as new twice | Merge the files of one account as a multiset union on `(date, description, amount, balance)` before comparing with the workbook |
| D2 | The dry run judged coverage on the old workbook | A gap and "no income" that the incoming files filled | Gap register and analysis run on workbook ∪ incoming |
| D3 | Credits netted against spend inside each category | A large one-off credit, unlabelled, flipped the monthly total's sign | Capital, transfer and reversal labels; uncategorised credits quarantined, blocking above 5% |
| D5 | Crypto buys counted as spend | They appeared under "Spend by category" | One flow line (bought / sold / net), neither spend nor income |
| D6 | A savings export was classed as the cheque account | Its rows were queued for the cheque sheet | `savings` is its own kind: chain-checked, never written |
| D7 | The writer hardcoded the date cell style | On any other workbook, dates read back as serial numbers | Styles resolved from `styles.xml`; refuse if there is no date format |
| D8 | An unreadable workbook row escaped as a traceback | Exit 1, no JSON | Exit 3 with JSON |
| D9 | A pending row was written without a balance | The settled copy later deduped against it | Pending rows are reported and imported when they settle |
| D10 | A file in the drop that was not a CSV was skipped in silence | A statement PDF appeared nowhere | Every other file is listed with the reason |
| D11 | Card spend authorised but not posted was invisible | It lives only in the bank's PDF | Read the PDF, refuse anything it cannot pair exactly, report pending apart, never write it, retire rows as they post |
| D12 | Credit-card purchases never reached the analysis | The cheque side sees the card only as its settlement, a transfer | Card purchases are spend, once; the settlement stays a transfer, both legs |
| D13 | Categorisation coverage had decayed | Too much recent spend unlabelled | Rules for the obvious patterns; rows already put to the owner are held until answered |
| D14 | Trips were isolated only by merchant label | A trip's fuel, tolls and meals stayed in the run-rate | A trips file by date range; reimbursements paired so the net cost shows |
| D15 | Supplements sat inside the health line | A discretionary habit hidden in a fixed cost | Its own category |

D4, labelling never-seen merchants, is a human decision and is deliberately not
automated. Against the pre-fix code, the original 14-oracle backtest failed 6 of the 7
oracles it reached.

## Features

| | Feature |
|---|---|
| F1 | A month-by-month table in the CFO report: complete months only, a cheque-only and an all-accounts deficit side by side, pending card spend in its month |
| F2 | Monthly caps from a private file, each month's actual against every cap; a cap naming an unknown category is refused |
| F3 | Recurring charges found in the ledger: the same merchant in two or more months within ±15%, one line per price, ranked by rand with the annual saving if cut |
| F4 | Idle cash as **THE ONE MOVE**: cash above a buffer, in an account whose earning rate is *measured* from its own interest credits, with the interest forgone per month and per day |
| F5 | A four-page dashboard PDF drawn from the run's own numbers |

## Data

This repository contains **no real financial data**. Every export, workbook and figure in
it is synthetic. The real rules, ledger, reports, backtest results and private settings
are ignored by git and refused by the publish gate.
