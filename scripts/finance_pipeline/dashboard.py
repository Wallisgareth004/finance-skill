#!/usr/bin/env python3
"""dashboard.py — a four-page PDF drawn from the run's own numbers.

IN   the CFO result, month_table() rows, the caps table, the caps season
OUT  a PDF at the path given (run.py writes 05-dashboard.pdf beside the reports)

F5, 2026-09-27. The owner's first dashboard was a one-off script with every
number typed in. This one types nothing: each page is drawn from the same
results the reports print, so the picture and the text cannot disagree.

  1. Spend vs income, month by month — cheque and card spend stacked, pending
     card spend on top, income as a line
  2. This month against a normal month (the mean of the complete months), by
     category
  3. Caps — a normal month, this month and the cap, for every cap line
  4. Portfolio — a placeholder until holdings are wired in

CATEGORY LEVEL ONLY: nothing here receives a description or a merchant.
matplotlib is imported only when a dashboard is asked for; without it the run
reports DashboardUnavailable and carries on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

A4 = (210 / 25.4, 297 / 25.4)  # millimetres to inches
INK, TEAL, CORAL, GOLD, GREY = "#1f2a44", "#1b998b", "#e4572e", "#f3a712", "#9aa5b1"


class DashboardUnavailable(Exception):
    """matplotlib is not installed, so no dashboard was drawn."""


def _k(value) -> str:
    return f"R{float(value) / 1000:,.1f}k"


def _label(m: dict) -> str:
    """The month; a part-month says which part (the tables use the same words)."""
    if m["complete"]:
        return m["month"]
    if m["from"].day == 1:
        return m["month"] + "\n(to date)"
    return m["month"] + f"\n({m['from'].day}–{m['to'].day} only)"


def _normal(months: list, key) -> Decimal:
    """The mean of `key(month)` over complete months; 0 when there are none."""
    full = [m for m in months if m["complete"]]
    if not full:
        return Decimal("0")
    return sum((Decimal(key(m)) for m in full), Decimal("0")) / len(full)


def render(
    path: str, cfo: dict, months: list, caps_table: list, season, today: date
) -> str:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker
        from matplotlib.backends.backend_pdf import PdfPages
    except ImportError as exc:
        raise DashboardUnavailable(
            "matplotlib is not installed, so no dashboard was drawn (pip install matplotlib)"
        ) from exc

    plt.rcParams.update(
        {"font.size": 9, "axes.spines.top": False, "axes.spines.right": False}
    )
    current = months[-1] if months else None

    def page(title, subtitle):
        fig = plt.figure(figsize=A4)
        fig.text(0.07, 0.95, title, fontsize=18, weight="bold", color=INK)
        fig.text(0.07, 0.925, subtitle, fontsize=10, color="#555")
        fig.text(
            0.5,
            0.02,
            f"Drawn {today.isoformat()} from the /finance run · window "
            f"{cfo.get('window', '')} · category level only",
            ha="center",
            fontsize=7,
            color="#777",
        )
        return fig

    with PdfPages(path) as pdf:
        # ── 1 · spend vs income ──
        fig = page(
            "Spend vs income, month by month",
            "Cheque and card spend as posted; pending card spend on top",
        )
        ax = fig.add_axes([0.1, 0.12, 0.83, 0.62])
        labels = [_label(m) for m in months]
        cheque = [float(m["spend_cheque"]) for m in months]
        card = [float(m["spend_card"]) for m in months]
        pending = [float(m["pending"]) for m in months]
        ax.bar(labels, cheque, color=TEAL, label="cheque, posted")
        ax.bar(labels, card, bottom=cheque, color=INK, label="card, posted")
        ax.bar(
            labels,
            pending,
            bottom=[a + b for a, b in zip(cheque, card)],
            color=GOLD,
            label="pending, not yet posted",
        )
        income = [
            float(m["income"]) if m["income"] is not None else float("nan")
            for m in months
        ]
        ax.plot(labels, income, color=CORAL, marker="o", label="income")
        for i, total in enumerate(a + b + c for a, b, c in zip(cheque, card, pending)):
            ax.text(i, total * 1.02, _k(total), ha="center", color=INK, weight="bold")
        ax.legend(loc="upper left", frameon=False)
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: _k(v))
        )
        lines = [
            f"Income (last annuity credit): {_k(cfo['income'])}"
            if cfo.get("income")
            else "Income: not stated (see the CFO report)"
        ]
        if cfo.get("accessible_cash") is not None:
            lines.append(f"Accessible cash: {_k(cfo['accessible_cash'])}")
        pend = cfo.get("pending") or {}
        if pend.get("rows"):
            lines.append(
                f"Pending card spend: {_k(pend['total'])} over {pend['rows']} authorisations"
            )
        for n, text in enumerate(lines):
            fig.text(0.07, 0.88 - n * 0.025, text, fontsize=11, color=INK)
        pdf.savefig(fig)
        plt.close(fig)

        # ── 2 · this month vs a normal month ──
        fig = page(
            "This month against a normal month",
            "Normal = the mean of the complete months in the window",
        )
        ax = fig.add_axes([0.36, 0.15, 0.58, 0.71])
        if current:
            cats = set(current["spend_by_category"])
            for m in months:
                if m["complete"]:
                    cats |= set(m["spend_by_category"])
            normal = {
                c: _normal(months, lambda m, c=c: m["spend_by_category"].get(c, 0))
                for c in cats
            }
            now = {c: Decimal(current["spend_by_category"].get(c, 0)) for c in cats}
            top = sorted(cats, key=lambda c: -max(normal[c], now[c]))[:14][::-1]
            ys = range(len(top))
            ax.barh(
                [y + 0.2 for y in ys],
                [float(normal[c]) for c in top],
                height=0.4,
                color=GREY,
                label="normal month",
            )
            ax.barh(
                [y - 0.2 for y in ys],
                [float(now[c]) for c in top],
                height=0.4,
                color=CORAL,
                label=current["month"] + ("" if current["complete"] else " to date"),
            )
            ax.set_yticks(list(ys))
            ax.set_yticklabels(top)
            ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06), ncol=3, frameon=False)
            ax.xaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: _k(v))
            )
        pdf.savefig(fig)
        plt.close(fig)

        # ── 3 · caps ──
        fig = page(
            f"Caps — {season or 'monthly caps'}",
            "A normal month, this month, and the cap",
        )
        ax = fig.add_axes([0.36, 0.15, 0.58, 0.71])
        if caps_table and caps_table[0]["lines"]:
            names = [line["name"] for line in caps_table[0]["lines"]][::-1]
            full = [m for m in caps_table if m["complete"]]
            last = caps_table[-1]

            def actual(month, name):
                return next(
                    float(x["actual"]) for x in month["lines"] if x["name"] == name
                )

            normal = [
                sum(actual(m, n) for m in full) / len(full) if full else 0.0
                for n in names
            ]
            now = [actual(last, n) for n in names]
            cap = [
                next(float(x["cap"]) for x in last["lines"] if x["name"] == n)
                for n in names
            ]
            ys = range(len(names))
            ax.barh(
                [y + 0.2 for y in ys],
                normal,
                height=0.4,
                color=GREY,
                label="normal month",
            )
            ax.barh(
                [y - 0.2 for y in ys],
                now,
                height=0.4,
                color=[CORAL if a > c else TEAL for a, c in zip(now, cap)],
                label=last["month"] + ("" if last["complete"] else " to date"),
            )
            ax.scatter(
                cap, list(ys), marker="|", s=400, color=INK, label="cap", zorder=3
            )
            ax.set_yticks(list(ys))
            ax.set_yticklabels(names)
            ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06), ncol=3, frameon=False)
            ax.xaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: _k(v))
            )
        else:
            fig.text(0.07, 0.8, "No caps file: nothing to compare.", fontsize=12)
        pdf.savefig(fig)
        plt.close(fig)

        # ── 4 · portfolio ──
        fig = page("Portfolio", "Not wired to data yet")
        fig.text(
            0.07,
            0.8,
            "Holdings are not read by this pipeline yet. When a statement or an\n"
            "export is wired in, this page shows them, drawn like the others.",
            fontsize=12,
            color=INK,
        )
        pdf.savefig(fig)
        plt.close(fig)
    return path
