#!/usr/bin/env python3
"""trips.py — one-off trips, found by DATE RANGE and kept out of the run-rate.

IN   a private trips file (YAML); rows to classify
OUT  [Trip]; which rows belong to which trip; each trip's reimbursement

═══ WHY BY DATE, NOT ONLY BY LABEL ═══
The model isolates a trip by labelling its merchants with a Travel category.
That misses every trip row that carries an ordinary label — the fuel to get
there, the tolls, the restaurants — or none at all yet. D14, 2026-09-27: a trip
is a date range plus the categories that count as trip spend inside it. A debit
order that falls inside the dates (a streaming subscription, say) is not the
trip, so only the listed categories are.

═══ ISOLATE, OR ONLY REPORT ═══
`isolate: true` moves the trip's rows out of spend and the deficit, into the
same "isolated" bucket as the Travel labels (they stay in cash burn: the money
did leave). `isolate: false` leaves them in spend and still reports the trip,
for a trip whose treatment is not decided yet.

═══ REIMBURSEMENT ═══
A trip someone else paid for is not a cost. A credit whose description matches
the trip's pattern, dated within `within_days` of the trip (before or after),
is paired with it, and the report shows the NET cost. The credit keeps its own
label (it is not reclassified as income or anything else).

The trips file is private: trips are the owner's life. Without one, no trips.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

DEFAULT_CATEGORIES = ("Uncategorised", "Restaurants", "Fuel", "Tolls")


class TripError(Exception):
    """The trips file says something that cannot be right."""


@dataclass
class Trip:
    label: str
    start: date
    end: date
    isolate: bool = False
    categories: tuple = DEFAULT_CATEGORIES
    reimbursement_pattern: str | None = None
    reimbursement_days: int = 30

    def covers(self, when) -> bool:
        return self.start <= _day(when) <= self.end


def _day(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def load(path: str) -> list:
    """The trips in `path`, or [] if there is no such file."""
    if not path or not os.path.exists(path):
        return []
    import yaml

    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    out = []
    for entry in doc.get("trips") or []:
        start, end = entry.get("from"), entry.get("to")
        if not isinstance(start, date) or not isinstance(end, date):
            raise TripError(f"trip {entry.get('label')!r}: from/to must be YYYY-MM-DD dates")
        if end < start:
            raise TripError(f"trip {entry.get('label')!r} ends before it starts")
        back = entry.get("reimbursement") or {}
        out.append(
            Trip(
                label=str(entry["label"]),
                start=start,
                end=end,
                isolate=bool(entry.get("isolate", False)),
                categories=tuple(entry.get("categories") or DEFAULT_CATEGORIES),
                reimbursement_pattern=back.get("pattern"),
                reimbursement_days=int(back.get("within_days", 30)),
            )
        )
    return out


def assign(trips: list, rows: list) -> dict:
    """id(row) -> Trip for every DEBIT inside a trip's dates and categories.
    The first listed trip wins if two overlap."""
    out = {}
    if not trips:
        return out
    for row in rows:
        if row.amount >= 0:
            continue
        category = getattr(row, "category", "") or "Uncategorised"
        for trip in trips:
            if trip.covers(row.date) and category in trip.categories:
                out[id(row)] = trip
                break
    return out


def reimbursement(trip: Trip, rows: list):
    """The credit that paid for `trip`: matching pattern, within the days
    allowed either side, the one closest to the trip. None if there is none."""
    if not trip.reimbursement_pattern:
        return None
    rx = re.compile(trip.reimbursement_pattern, re.I)
    lo = trip.start - timedelta(days=trip.reimbursement_days)
    hi = trip.end + timedelta(days=trip.reimbursement_days)
    found = [
        r for r in rows
        if r.amount > 0 and lo <= _day(r.date) <= hi and rx.search(r.description or "")
    ]

    def distance(r):
        d = _day(r.date)
        return 0 if trip.covers(d) else min(abs((d - trip.start).days), abs((d - trip.end).days))

    return min(found, key=distance, default=None)
