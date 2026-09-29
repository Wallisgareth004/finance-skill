#!/usr/bin/env python3
"""writer.py — append transactions to the workbook, safely.

IN   the workbook, and rows to append (or cells to fix)
OUT  the same workbook, backed up first, verified, then atomically replaced

═══ THE CONTRACT ═══
    backup  →  write to a temp copy  →  verify  →  atomic replace
and if verification fails, the original is left exactly as it was. The workbook
on disk is never in a half-written state, because os.replace() is atomic.

═══ WHY NOT openpyxl ═══
The workbook contains FOUR CHARTS. openpyxl does not round-trip xl/charts/* or
xl/drawings/*; wb.save() deletes all four silently. Every write here patches the
sheet XML inside the zip and copies every other part through byte-for-byte.
verify() re-counts the charts afterwards and refuses the write if any went missing.

═══ THE THREE THINGS AN APPEND MUST ALSO UPDATE ═══
  1. <dimension ref="A1:F…">  — the declared used range
  2. <autoFilter ref="A3:F…"> — found STALE in the real file, short of the data,
     because an earlier manual append had not extended it. We fix it on every
     write.
  3. xl/calcChain.xml — Excel's cached formula-evaluation order. Once the model
     is formula-driven, adding rows invalidates it. Dropping it is free; Excel
     rebuilds it. Leaving it stale triggers the "we found a problem" repair prompt.

═══ CELL STYLES ═══
An earlier manual append used s="31" (yyyy-mm-dd h:mm:ss) for dates and no style at all
for Amount/Balance (General). The canonical styles used by the rest of the sheet
are s="21" (yyyy-mm-dd) and s="26" (#,##0.00). We write canonical.

Text is written as t="inlineStr" so sharedStrings.xml never has to be touched —
one less part to get wrong.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime

EXCEL_EPOCH = date(1899, 12, 30)

TXNS_SHEET = "xl/worksheets/sheet6.xml"  # TRANSACTIONS
CALC_CHAIN = "xl/calcChain.xml"

STYLE_DATE = "21"  # yyyy-mm-dd — in the LIVE workbook's style table only
STYLE_MONEY = "26"  # #,##0.00  — ditto
STYLES = "xl/styles.xml"

# Built-in numFmtIds (ECMA-376 §18.8.30). 14–17 are dates, 22 is date+time.
_BUILTIN_DATES = {"14", "15", "16", "17"}
_BUILTIN_DATETIMES = {"22"}
_BUILTIN_MONEY = {"2", "4"}  # 0.00 and #,##0.00

FIRST_DATA_ROW = 4


class WriteFailed(Exception):
    """Raised after the original has been restored. The workbook is unchanged."""


@dataclass
class WriteResult:
    rows_added: int
    first_row: int
    last_row: int
    backup_path: str
    charts_before: int
    charts_after: int


def excel_serial(d) -> int:
    if isinstance(d, datetime):
        d = d.date()
    return (d - EXCEL_EPOCH).days


# XML 1.0 forbids these outright. detect._read_lines falls back to latin-1, which
# NEVER raises — every byte 0x00-0x1F decodes to a control character and would reach
# the sheet unfiltered, producing XML that Excel refuses to open. Measured
# 2026-08-20: a single NUL in a description corrupted the workbook AND the write
# reported success.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
EXCEL_CELL_LIMIT = 32767


def sanitise(value) -> str:
    """Strip XML-illegal control characters and cap at Excel's cell limit."""
    text = _ILLEGAL_XML.sub("", str(value))
    if len(text) > EXCEL_CELL_LIMIT:
        text = text[: EXCEL_CELL_LIMIT - 1] + "\u2026"
    return text


def xml_escape(s: str) -> str:
    return (
        sanitise(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def backup(path: str, keep: int = 10) -> str:
    """Timestamped copy beside the original, oldest pruned beyond `keep`.

    A workbook like this is a few hundred KB, so ten generations cost a few MB,
    and a bad import is always one copy away from undone.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root, ext = os.path.splitext(path)
    dest = f"{root}.backup-{stamp}{ext}"
    shutil.copy2(path, dest)

    pattern = re.compile(
        re.escape(os.path.basename(root))
        + r"\.backup-\d{8}-\d{6}"
        + re.escape(ext)
        + r"$"
    )
    folder = os.path.dirname(path) or "."
    existing = sorted(f for f in os.listdir(folder) if pattern.match(f))
    for old in existing[:-keep]:
        os.remove(os.path.join(folder, old))
    return dest


def _check_amount(name: str, value) -> None:
    """Decimal() accepts NaN / Infinity / 1e999 from a CSV without raising.
    None of those are valid xsd:double, and NaN crashes the rules engine."""
    if value is None:
        return
    if not value.is_finite():
        raise WriteFailed(f"{name} is not a finite number: {value!r}")


def _xf_formats(styles_xml: str) -> list:
    """cellXfs index → (numFmtId, formatCode or '')."""
    custom = dict(
        re.findall(r'<numFmt\s+numFmtId="(\d+)"\s+formatCode="([^"]*)"', styles_xml)
    )
    block = re.search(r"<cellXfs[^>]*>(.*?)</cellXfs>", styles_xml, re.S)
    if not block:
        return []
    out = []
    for xf in re.findall(r"<xf\b[^>]*?/?>", block.group(1)):
        fid = re.search(r'numFmtId="(\d+)"', xf)
        fid = fid.group(1) if fid else "0"
        out.append((fid, custom.get(fid, "")))
    return out


def _bare(code: str) -> str:
    """A format code without quoted literals, escapes or [colour] blocks."""
    code = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", code)
    return code.lower()


def _is_date(fmt: tuple, allow_time: bool = False) -> bool:
    fid, code = fmt
    if fid in _BUILTIN_DATES or (allow_time and fid in _BUILTIN_DATETIMES):
        return True
    bare = _bare(code)
    if not ("y" in bare and "d" in bare):
        return False
    return allow_time or ("h" not in bare and "s" not in bare)


def _is_money(fmt: tuple) -> bool:
    fid, code = fmt
    return fid in _BUILTIN_MONEY or ("0.00" in code and not _is_date(fmt, True))


def resolve_styles(styles_xml: str, sheet_xml: str) -> tuple:
    """(date_style, money_style) that are REALLY a date and a 2-dp number here.

    D7, 2026-09-24: the ids used to be the constants above, which are correct in
    the live workbook and wrong elsewhere — in an older backup s=21 is
    `#,##0`, so appended dates read back as serials and the next run crashed.

    Preference: the canonical id if it qualifies → the style the sheet's own
    last data row uses in that column → the first qualifying xf. A date is
    required (refuse without one); money falls back to the last row's style,
    then General — a number in General is still the right number.
    """
    fmts = _xf_formats(styles_xml)

    def fmt(idx):
        i = int(idx)
        return fmts[i] if 0 <= i < len(fmts) else ("", "")

    def last_style(col):
        found = re.findall(rf'<c r="{col}(\d+)" s="(\d+)"', sheet_xml)
        rows = [(int(r), st) for r, st in found if int(r) >= FIRST_DATA_ROW]
        return max(rows)[1] if rows else None

    date_style = None
    for candidate in (STYLE_DATE, last_style("A")):
        if candidate is not None and _is_date(fmt(candidate)):
            date_style = candidate
            break
    if date_style is None:
        date_style = next((str(i) for i, f in enumerate(fmts) if _is_date(f)), None)
    if date_style is None:
        last = last_style("A")
        if last is not None and _is_date(fmt(last), allow_time=True):
            date_style = last
    if date_style is None:
        raise WriteFailed(
            "no date number format in xl/styles.xml — refusing to write dates that "
            "would read back as serial numbers"
        )

    money_style = None
    for candidate in (STYLE_MONEY, last_style("D")):
        if candidate is not None and _is_money(fmt(candidate)):
            money_style = candidate
            break
    if money_style is None:
        money_style = next((str(i) for i, f in enumerate(fmts) if _is_money(f)), None)
    if money_style is None:
        money_style = last_style("D") or "0"
    return date_style, money_style


def _row_xml(row_num: int, txn, styles: tuple = (STYLE_DATE, STYLE_MONEY)) -> str:
    """One TRANSACTIONS row: Date | Description | Category | Amount | Balance | Source."""
    style_date, style_money = styles
    _check_amount("amount", txn.amount)
    _check_amount("balance", txn.balance)
    bal = (
        ""
        if txn.balance is None
        else f'<c r="E{row_num}" s="{style_money}"><v>{txn.balance}</v></c>'
    )
    return (
        f'<row r="{row_num}" spans="1:6">'
        f'<c r="A{row_num}" s="{style_date}"><v>{excel_serial(txn.date)}</v></c>'
        f'<c r="B{row_num}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(txn.description)}</t></is></c>'
        f'<c r="C{row_num}" t="inlineStr"><is><t>{xml_escape(txn.category)}</t></is></c>'
        f'<c r="D{row_num}" s="{style_money}"><v>{txn.amount}</v></c>'
        f"{bal}"
        f'<c r="F{row_num}" t="inlineStr"><is><t>{xml_escape(txn.source_slug)}</t></is></c>'
        f"</row>"
    )


def _last_row(sheet_xml: str) -> int:
    rows = re.findall(r'<row r="(\d+)"', sheet_xml)
    return max(int(r) for r in rows) if rows else FIRST_DATA_ROW - 1


def _patch_sheet(
    sheet_xml: str, txns: list, styles: tuple = (STYLE_DATE, STYLE_MONEY)
) -> tuple[str, int, int]:
    start = _last_row(sheet_xml) + 1
    new_rows = "".join(_row_xml(start + i, t, styles) for i, t in enumerate(txns))
    end = start + len(txns) - 1

    if "</sheetData>" not in sheet_xml:
        raise WriteFailed("TRANSACTIONS sheet has no </sheetData> — refusing to write")
    sheet_xml = sheet_xml.replace("</sheetData>", new_rows + "</sheetData>", 1)

    sheet_xml = re.sub(
        r'<dimension ref="A1:F\d+"\s*/>', f'<dimension ref="A1:F{end}"/>', sheet_xml
    )
    # The real file's autofilter was found stale, short of the data. Fix it every time.
    sheet_xml = re.sub(r'(<autoFilter ref="A3:F)\d+(")', rf"\g<1>{end}\g<2>", sheet_xml)
    return sheet_xml, start, end


def _drop_calc_chain(parts: dict, order: list) -> list:
    if CALC_CHAIN not in parts:
        return order
    ct = parts["[Content_Types].xml"].decode("utf-8")
    parts["[Content_Types].xml"] = re.sub(
        r'<Override PartName="/xl/calcChain\.xml"[^>]*/>', "", ct
    ).encode("utf-8")
    rels = "xl/_rels/workbook.xml.rels"
    if rels in parts:
        parts[rels] = re.sub(
            r'<Relationship[^>]*Target="calcChain\.xml"[^>]*/>',
            "",
            parts[rels].decode("utf-8"),
        ).encode("utf-8")
    del parts[CALC_CHAIN]
    return [n for n in order if n != CALC_CHAIN]


def _count_charts(path: str) -> int:
    with zipfile.ZipFile(path) as z:
        return sum(1 for n in z.namelist() if "charts/chart" in n)


def append_transactions(
    xlsx_path: str, txns: list, keep_backups: int = 10
) -> WriteResult:
    """Append rows to TRANSACTIONS. Backs up first; restores on any failure.

    `txns` must expose: date, description, category, amount, balance, source_slug.
    """
    if not txns:
        raise WriteFailed("nothing to write")

    charts_before = _count_charts(xlsx_path)
    backup_path = backup(xlsx_path, keep=keep_backups)
    # NOTE the extension order: openpyxl refuses to open anything not ending
    # .xlsx, and _verify_readable() opens this temp file before we commit it.
    root, ext = os.path.splitext(xlsx_path)
    tmp = f"{root}.tmp-write{ext}"

    try:
        with zipfile.ZipFile(xlsx_path) as z:
            order = z.namelist()
            parts = {n: z.read(n) for n in order}

        sheet_xml = parts[TXNS_SHEET].decode("utf-8")
        styles = resolve_styles(parts[STYLES].decode("utf-8"), sheet_xml)
        patched, first, last = _patch_sheet(sheet_xml, txns, styles)
        parts[TXNS_SHEET] = patched.encode("utf-8")
        order = _drop_calc_chain(parts, order)

        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name in order:
                z.writestr(name, parts[name])

        charts_after = _count_charts(tmp)
        if charts_after != charts_before:
            raise WriteFailed(
                f"chart loss: {charts_before} -> {charts_after}. Write abandoned."
            )
        _verify_readable(tmp, expected_last_row=last)

        os.replace(tmp, xlsx_path)  # atomic
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        shutil.copy2(backup_path, xlsx_path)
        raise WriteFailed(f"{exc} — original restored from {backup_path}") from exc

    return WriteResult(
        rows_added=len(txns),
        first_row=first,
        last_row=last,
        backup_path=backup_path,
        charts_before=charts_before,
        charts_after=charts_after,
    )


# ── fixing text cells in place (the CREDIT CARD comma-truncation) ────────────
_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass
class FixResult:
    cells_fixed: int
    backup_path: str
    charts_before: int
    charts_after: int


def _sheet_part(parts: dict, sheet_name: str) -> str:
    """The zip part holding `sheet_name`, found through workbook.xml and its
    relationships — never assumed from the sheet's position."""
    import xml.etree.ElementTree as ET

    book = ET.fromstring(parts["xl/workbook.xml"])
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    rid = next(
        (s.get(f"{rel_ns}id") for s in book.iter(f"{_MAIN_NS}sheet") if s.get("name") == sheet_name),
        None,
    )
    if rid is None:
        raise WriteFailed(f"no sheet named {sheet_name!r}")
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    target = next((r.get("Target") for r in rels if r.get("Id") == rid), None)
    if target is None:
        raise WriteFailed(f"sheet {sheet_name!r} has no relationship target")
    return target.lstrip("/") if target.startswith("/") else "xl/" + target


def _shared_strings(parts: dict) -> list:
    import xml.etree.ElementTree as ET

    if "xl/sharedStrings.xml" not in parts:
        return []
    root = ET.fromstring(parts["xl/sharedStrings.xml"])
    return ["".join(t.text or "" for t in si.iter(f"{_MAIN_NS}t")) for si in root.iter(f"{_MAIN_NS}si")]


def _cell_text(cell_xml: str, shared: list) -> str:
    import xml.etree.ElementTree as ET

    cell = ET.fromstring(cell_xml)
    kind = cell.get("t")
    if kind == "s":
        return shared[int(cell.findtext("v"))]
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter("t"))
    return cell.findtext("v") or ""


def fix_text_cells(xlsx_path: str, sheet_name: str, fixes: list, keep_backups: int = 10) -> FixResult:
    """Replace the text of named cells: `fixes` is [(cell, expected, new)].

    The writer's contract, as for an append: backup first, then a temp copy,
    then verify — every part well-formed, the charts all there, each fixed cell
    reading back the new text — then an atomic replace. A cell that does not
    currently read `expected` is REFUSED and the workbook is left as it was.
    The new text is written inline, with the cell's own style, so a shared
    string another cell might use is never touched.
    """
    if not fixes:
        raise WriteFailed("nothing to fix")
    charts_before = _count_charts(xlsx_path)
    backup_path = backup(xlsx_path, keep=keep_backups)
    root, ext = os.path.splitext(xlsx_path)
    tmp = f"{root}.tmp-fix{ext}"
    try:
        with zipfile.ZipFile(xlsx_path) as z:
            order = z.namelist()
            parts = {n: z.read(n) for n in order}
        part = _sheet_part(parts, sheet_name)
        shared = _shared_strings(parts)
        xml = parts[part].decode("utf-8")
        for ref, expected, new in fixes:
            m = re.search(rf'<c r="{ref}"(?:\s[^>]*)?>.*?</c>', xml, re.S)
            if not m:
                raise WriteFailed(f"{sheet_name}!{ref} not found")
            current = _cell_text(m.group(0), shared)
            if current != expected:
                raise WriteFailed(
                    f"{sheet_name}!{ref} reads {current!r}, not {expected!r} — refusing"
                )
            style = re.search(r'\ss="(\d+)"', m.group(0).split(">", 1)[0])
            styled = f' s="{style.group(1)}"' if style else ""
            cell = (
                f'<c r="{ref}"{styled} t="inlineStr"><is><t xml:space="preserve">'
                f"{xml_escape(new)}</t></is></c>"
            )
            xml = xml[: m.start()] + cell + xml[m.end():]
        parts[part] = xml.encode("utf-8")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name in order:
                z.writestr(name, parts[name])
        charts_after = _count_charts(tmp)
        if charts_after != charts_before:
            raise WriteFailed(f"chart loss: {charts_before} -> {charts_after}. Fix abandoned.")
        _verify_fixed(tmp, part, fixes)
        os.replace(tmp, xlsx_path)  # atomic
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        shutil.copy2(backup_path, xlsx_path)
        raise WriteFailed(f"{exc} — original restored from {backup_path}") from exc
    return FixResult(len(fixes), backup_path, charts_before, charts_after)


def _verify_fixed(path: str, part: str, fixes: list) -> None:
    """Every part still parses, and every fixed cell reads its new text."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                try:
                    ET.fromstring(z.read(name))
                except ET.ParseError as exc:
                    raise WriteFailed(f"{name} is malformed after the fix: {exc}") from exc
        sheet = ET.fromstring(z.read(part))
    cells = {c.get("r"): c for c in sheet.iter(f"{_MAIN_NS}c")}
    for ref, _expected, new in fixes:
        text = "".join(t.text or "" for t in cells[ref].iter(f"{_MAIN_NS}t"))
        if text != sanitise(new):
            raise WriteFailed(f"{ref} reads back {text!r}, not the new text")


def _verify_readable(path: str, expected_last_row: int) -> None:
    """Verify the written workbook for real.

    THE TRAP THIS AVOIDS: openpyxl's read_only mode takes `max_row` straight from
    the sheet's <dimension> element — the very string _patch_sheet just rewrote.
    It never looks at <sheetData>. So a read_only row-count check is the writer
    grading its own homework: measured 2026-08-20, a NUL byte produced XML that
    Excel could not open while read_only max_row returned the expected number, and
    the corrupt file was committed over the original.

    So: parse the sheet XML properly, and count real <row> elements.
    """
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if TXNS_SHEET not in names:
            raise WriteFailed("TRANSACTIONS sheet part missing after write")
        raw = z.read(TXNS_SHEET)
        # 1. is it well-formed at all?
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise WriteFailed(f"written sheet XML is malformed: {exc}") from exc
        # 2. every other part must still parse too
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                try:
                    ET.fromstring(z.read(name))
                except ET.ParseError as exc:
                    raise WriteFailed(f"{name} is malformed after write: {exc}") from exc

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows = [int(r.get("r")) for r in root.iter(f"{ns}row") if r.get("r")]
    if not rows:
        raise WriteFailed("no rows found in the written sheet")
    actual = max(rows)
    if actual != expected_last_row:
        raise WriteFailed(
            f"row count wrong after write: expected {expected_last_row}, got {actual}"
        )
    if rows != sorted(rows):
        raise WriteFailed("rows are not in ascending order — Excel would report corruption")
