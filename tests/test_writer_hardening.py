"""Regressions from the 2026-08-20 adversarial review.

#1 CRITICAL — `_verify_readable` used openpyxl read_only, whose `max_row` comes
   straight from the <dimension> element the writer had just rewritten. It never
   looked at <sheetData>, so it could not detect a corrupt sheet — and the
   corrupt file was then committed over the live workbook while reporting
   success.

#2 CRITICAL — no control-character sanitisation. `detect._read_lines` falls back
   to latin-1, which NEVER raises, so any byte 0x00-0x1F in an FNB export decoded
   to a control character and reached the sheet unfiltered. A single NUL produced
   XML that Excel refuses to open.

#9 — Decimal() accepts NaN / Infinity / 1e999 from a CSV without raising.
"""

import os
import shutil
import tempfile
import unittest
import zipfile
from datetime import date
from decimal import Decimal

from helpers import pipeline_dir, prototype_workbook

pipeline_dir()
import writer  # noqa: E402

PROTOTYPE = prototype_workbook()  # see helpers.prototype_workbook (2026-08-25)


class _Row:
    def __init__(self, desc, amt="-1.00", bal="100.00", d=None):
        self.date = d or date(2026, 8, 20)
        self.description = desc
        self.category = "Groceries"
        self.amount = Decimal(amt)
        self.balance = Decimal(bal) if bal is not None else None
        self.source_slug = "export-2026-08-20_to_08-20"


class TestSanitisation(unittest.TestCase):
    def test_nul_byte_stripped(self):
        out = writer.xml_escape("FNB App Payment To Pat\x00Lee")
        self.assertNotIn("\x00", out)
        self.assertIn("Pat", out)

    def test_control_characters_stripped(self):
        out = writer.xml_escape("CAFE \x1b[31mRED\x01\x08\x0b\x1f")
        for bad in ("\x1b", "\x01", "\x08", "\x0b", "\x1f"):
            self.assertNotIn(bad, out)

    def test_legitimate_unicode_survives(self):
        self.assertIn("É", writer.xml_escape("CAFÉ"))
        self.assertIn("—", writer.xml_escape("Income — annuity"))

    def test_tab_and_newline_preserved(self):
        """0x09 and 0x0A are legal XML and must not be stripped."""
        self.assertIn("\t", writer.xml_escape("a\tb"))

    def test_overlong_string_capped(self):
        out = writer.sanitise("x" * 40000)
        self.assertLessEqual(len(out), writer.EXCEL_CELL_LIMIT)

    def test_xml_metacharacters_escaped(self):
        out = writer.xml_escape('a & b < c > d "e"')
        self.assertIn("&amp;", out)
        self.assertIn("&lt;", out)
        self.assertNotIn("<c", out)


class TestNonFiniteAmounts(unittest.TestCase):
    def test_nan_rejected(self):
        with self.assertRaises(writer.WriteFailed):
            writer._row_xml(10, _Row("x", amt="NaN"))

    def test_infinity_rejected(self):
        with self.assertRaises(writer.WriteFailed):
            writer._row_xml(10, _Row("x", amt="Infinity"))

    def test_finite_accepted(self):
        self.assertIn("-123.45", writer._row_xml(10, _Row("x", amt="-123.45")))


@unittest.skipUnless(PROTOTYPE and os.path.exists(PROTOTYPE), "prototype workbook not present")
class TestVerificationIsNotCircular(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.book = os.path.join(self.tmp, "model.xlsx")
        shutil.copy2(PROTOTYPE, self.book)
        import openpyxl
        wb = openpyxl.load_workbook(self.book, read_only=True)
        self.base_row = wb["TRANSACTIONS"].max_row
        wb.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_malformed_sheet_is_detected(self):
        """Inject broken XML directly, then assert verification catches it."""
        broken = os.path.join(self.tmp, "broken.xlsx")
        with (
            zipfile.ZipFile(self.book) as zin,
            zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as zout,
        ):
            for n in zin.namelist():
                data = zin.read(n)
                if n == writer.TXNS_SHEET:
                    data = data.replace(b"</sheetData>", b"<c r=", 1)
                zout.writestr(n, data)
        with self.assertRaises(writer.WriteFailed):
            writer._verify_readable(broken, expected_last_row=self.base_row)

    def test_dimension_lie_is_not_believed(self):
        """The old check read max_row from <dimension>. Prove we no longer do."""
        lying = os.path.join(self.tmp, "lying.xlsx")
        with (
            zipfile.ZipFile(self.book) as zin,
            zipfile.ZipFile(lying, "w", zipfile.ZIP_DEFLATED) as zout,
        ):
            for n in zin.namelist():
                data = zin.read(n)
                if n == writer.TXNS_SHEET:
                    data = data.replace(
                        f'<dimension ref="A1:F{self.base_row}"/>'.encode(),
                        b'<dimension ref="A1:F9999"/>',
                        1,
                    )
                zout.writestr(n, data)
        # dimension claims 9999; the real last row is far lower. Verification must use the rows.
        with self.assertRaises(writer.WriteFailed):
            writer._verify_readable(lying, expected_last_row=9999)
        writer._verify_readable(lying, expected_last_row=self.base_row)  # must NOT raise

    def test_nul_in_description_does_not_reach_the_live_file(self):
        import hashlib

        pre = hashlib.sha256(open(self.book, "rb").read()).hexdigest()
        res = writer.append_transactions(
            self.book, [_Row("FNB App Payment To Pat\x00Lee")]
        )
        self.assertEqual(res.rows_added, 1)
        # the write must SUCCEED with the byte stripped, not corrupt the file
        writer._verify_readable(self.book, expected_last_row=res.last_row)
        post = hashlib.sha256(open(self.book, "rb").read()).hexdigest()
        self.assertNotEqual(pre, post, "row should have been written")


if __name__ == "__main__":
    unittest.main()
