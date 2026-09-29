#!/usr/bin/env python3
"""categorise.py — the rules engine.

IN   rules.yaml, plus rules.private.yaml and review_hold.private.yaml when
     beside it
OUT  a Ruleset: classify(txn) -> (category, rule_id, confidence)

═══ EVALUATION ORDER IS LOAD-BEARING ═══
    overrides (amount-conditional)  →  rules (first match wins)  →  fallback

Two ordering facts, both measured on the owner's data:
  • The food-delivery rule MUST precede the \\bUBER\\b rule, or every
    "Uber Eats" order lands in Transport.
  • Overrides run first, so an amount-conditional rule (a bill paid at a
    shop's till, above some threshold, say) resolves before the generic
    groceries rule can claim it.

═══ VOCABULARY ═══
The Excel model's own 32 labels, byte-exact — em dashes are U+2014 with a space
either side and lowercase after. Inventing a parallel taxonomy silently breaks
every SUMIFS in the workbook. Two additions only: `Refunds` (currently smeared
into Income — other, inflating income) and `Unknown — card, no merchant`.

═══ PRIVACY ═══
`rules.private.yaml` holds sensitive merchant patterns that resolve to
`Personal — discretionary`, matching the model's own convention
(NOTES & GAPS!B18). It is gitignored. Its contents must NEVER be printed,
logged, or included in any report — only the resulting category.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal

import yaml

import model

# D13, 2026-09-27: rule_id for a row whose label the owner has been asked for.
HELD = "held-for-owner"
HOLD_FILE = "review_hold.private.yaml"

VOCABULARY = {
    # the model's existing 32
    "Uncategorised",
    "Groceries",
    "Restaurants",
    "Personal — discretionary",
    "Fuel",
    "Digital subscriptions",
    "Food delivery",
    "Leisure",
    "Medical aid & health",
    "Connectivity",
    "Transport/Vehicle",
    "Home maintenance",
    "Tolls",
    "Savings (Bank Your Change)",
    "People / transfers",
    "Bank fees & interest",
    "Retail / clothing",
    "Cash / ATM",
    "Crypto / investments",
    "Income — other",
    "Business / side project",
    "Alcohol",
    "Gaming",
    "Fitness (gym)",
    "Entertainment",
    "Personal care",
    "Pets",
    "Travel — trip",
    "Home maintenance",
    "Insurance (policy)",
    "Income — annuity",
    "Savings transfers",
    # added by this skill — see spec §6
    "Refunds",
    "Unknown — card, no merchant",
    # added 2026-09-24 (D3) — OUTSIDE spend and income. Money that moved but was
    # neither earned nor consumed. analyse.py routes each to its own bucket.
    "Capital — one-off inflow",
    "Transfer — card settlement",
    "Reversal — unpaid debit",
    # added 2026-09-27 (D15) — split out of "Medical aid & health": a
    # discretionary habit watched on its own line, beside the fixed medical aid.
    "Supplements",
}


@dataclass
class Rule:
    id: str
    pattern: re.Pattern
    category: str
    confidence: str = "high"
    when_abs_amount_gte: Decimal | None = None
    when_abs_amount_lt: Decimal | None = None
    # SIGN conditions. Without these a rule cannot express "credits only", and an
    # OUTGOING payment to a Capitec recipient gets booked as INCOME — recreating
    # the exact defect this skill exists to fix (spec §3.3, "Income is overstated").
    when_amount_gt: Decimal | None = None
    when_amount_lt: Decimal | None = None
    note: str = ""

    def matches(self, text: str, amount: Decimal) -> bool:
        if not self.pattern.search(text):
            return False
        a = abs(amount)
        if self.when_abs_amount_gte is not None and a < self.when_abs_amount_gte:
            return False
        if self.when_abs_amount_lt is not None and a >= self.when_abs_amount_lt:
            return False
        if self.when_amount_gt is not None and amount <= self.when_amount_gt:
            return False
        if self.when_amount_lt is not None and amount >= self.when_amount_lt:
            return False
        return True


class Ruleset:
    def __init__(self, overrides, rules, fallback, held=frozenset()) -> None:
        self.overrides = overrides
        self.rules = rules
        self.fallback = fallback
        # D13: rows the owner has been asked to label. A rule that happens to
        # match one of them must not answer his question for him.
        self.held = frozenset(held)

    def classify(self, txn) -> tuple:
        """Return (category, rule_id, confidence). Unmatched -> fallback.
        A held row -> (fallback, HELD, "held") whatever the rules say."""
        if self.held and model.row_hash(txn) in self.held:
            return self.fallback, HELD, "held"
        text = (txn.description or "").upper()
        for rule in list(self.overrides) + list(self.rules):
            if rule.matches(text, txn.amount):
                return rule.category, rule.id, rule.confidence
        return self.fallback, None, "none"

    def apply(self, txn):
        """Classify `txn` and ATTACH the result to it, returning the txn.

        classify() returns a tuple, which is fine for tests but leaves the caller
        to remember to attach it — and writer.append_transactions() requires
        `txn.category`. This is the wiring between the two, so a transaction
        cannot reach the workbook uncategorised.
        """
        txn.category, txn.rule_id, txn.confidence = self.classify(txn)
        txn.held = txn.rule_id == HELD
        return txn

    def apply_all(self, txns) -> list:
        return [self.apply(t) for t in txns]

    def categories_used(self) -> set:
        used = {r.category for r in list(self.overrides) + list(self.rules)}
        used.add(self.fallback)
        return used


def _build(entries) -> list:
    out = []
    for e in entries or []:
        out.append(
            Rule(
                id=e["id"],
                pattern=re.compile(e["pattern"], re.I),
                category=e["category"],
                confidence=e.get("confidence", "high"),
                when_abs_amount_gte=(
                    Decimal(str(e["when_abs_amount_gte"]))
                    if "when_abs_amount_gte" in e
                    else None
                ),
                when_abs_amount_lt=(
                    Decimal(str(e["when_abs_amount_lt"]))
                    if "when_abs_amount_lt" in e
                    else None
                ),
                when_amount_gt=(
                    Decimal(str(e["when_amount_gt"])) if "when_amount_gt" in e else None
                ),
                when_amount_lt=(
                    Decimal(str(e["when_amount_lt"])) if "when_amount_lt" in e else None
                ),
                note=e.get("note", ""),
            )
        )
    return out


def default_rules_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "rules.yaml",
    )


def load(rules_path: str | None = None, private_path: str | None = None) -> Ruleset:
    rules_path = rules_path or default_rules_path()
    with open(rules_path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    overrides = _build(doc.get("overrides"))
    rules = _build(doc.get("rules"))

    # Private patterns take precedence over everything, and are never surfaced.
    if private_path is None:
        private_path = os.path.join(os.path.dirname(rules_path), "rules.private.yaml")
    if os.path.exists(private_path):
        with open(private_path, encoding="utf-8") as fh:
            priv = yaml.safe_load(fh) or {}
        overrides = _build(priv.get("overrides")) + overrides

    unknown = {r.category for r in overrides + rules if r.category not in VOCABULARY}
    if unknown:
        raise ValueError(
            f"rules use categories outside the model's vocabulary: {sorted(unknown)}. "
            "Every SUMIFS in the workbook matches on these strings byte-exactly."
        )

    # D13: the hold list sits beside the rules file, private and gitignored.
    held = set()
    hold_path = os.path.join(os.path.dirname(rules_path), HOLD_FILE)
    if os.path.exists(hold_path):
        with open(hold_path, encoding="utf-8") as fh:
            held = set((yaml.safe_load(fh) or {}).get("rows") or [])

    return Ruleset(overrides, rules, doc.get("fallback", "Uncategorised"), held)
