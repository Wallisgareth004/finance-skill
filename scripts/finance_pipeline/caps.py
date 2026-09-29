#!/usr/bin/env python3
"""caps.py — the owner's monthly caps, and each month's spend against them.

IN   a private caps file (YAML); month_table() rows
OUT  (season, [Cap]); per month, actual against cap for every cap line

F2, 2026-09-27. A cap is a name, a monthly amount and the categories it covers
("eating out + delivery" = Restaurants + Food delivery). The caps are the
owner's decisions, so the file is private; without one there are no caps.

A cap naming a category outside the vocabulary is REFUSED: a typo would sum to
R0 and show a cheerful tick for money that was never counted. A month the
window cuts short is compared as it stands ("to date"), never pro-rated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

import categorise

_CENTS = Decimal("0.01")


class CapsError(Exception):
    """The caps file names something that cannot be counted."""


@dataclass
class Cap:
    name: str
    monthly: Decimal
    categories: tuple


def load(path: str) -> tuple:
    """(season, [Cap]) from `path`; (None, []) when there is no such file."""
    if not path or not os.path.exists(path):
        return None, []
    import yaml

    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    out = []
    for entry in doc.get("caps") or []:
        cats = tuple(entry.get("categories") or ())
        unknown = [c for c in cats if c not in categorise.VOCABULARY]
        if not cats or unknown:
            raise CapsError(
                f"cap {entry.get('name')!r}: unknown or missing categories {unknown or cats}"
            )
        out.append(Cap(str(entry["name"]), Decimal(str(entry["monthly"])), cats))
    return doc.get("season"), out


def table(months: list, caps: list) -> list:
    """Per month: every cap line with its actual spend and how far over it is."""
    out = []
    for m in months:
        by_cat = m.get("spend_by_category") or {}
        lines = []
        for cap in caps:
            actual = sum((Decimal(by_cat.get(c, 0)) for c in cap.categories), Decimal("0"))
            actual = actual.quantize(_CENTS, rounding=ROUND_HALF_UP)
            over = max(actual - cap.monthly, Decimal("0")).quantize(_CENTS)
            lines.append({"name": cap.name, "cap": cap.monthly, "actual": actual, "over": over})
        out.append({"month": m["month"], "from": m.get("from"), "to": m.get("to"),
                    "complete": m["complete"], "lines": lines})
    return out
