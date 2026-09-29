"""Format detection tests.

WHY SIGNATURE-BASED: the preamble has been 6 lines in every real export
measured, but FNB names an export after the account number, so two different
exports routinely share a filename. Neither the filename nor a hardcoded line
number is a safe key.
"""

import os
import shutil
import tempfile
import unittest

from helpers import make_card_csv, make_cheque_csv, pipeline_dir

pipeline_dir()
import detect  # noqa: E402


class TestSniff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cheque_identified(self):
        d = detect.sniff(make_cheque_csv(self.tmp))
        self.assertEqual(d.format, detect.FORMAT_A)
        self.assertEqual(d.account_kind, "cheque")
        self.assertEqual(d.account_number, "10000000001")
        self.assertEqual(str(d.closing_balance), "2215.40")
        self.assertEqual(str(d.available), "615.25")

    def test_card_identified(self):
        d = detect.sniff(make_card_csv(self.tmp))
        self.assertEqual(d.account_kind, "card")
        self.assertEqual(d.account_number, "400000******0001")
        self.assertEqual(str(d.closing_balance), "-1834.52")

    def test_header_found_by_signature_not_line_number(self):
        p = make_cheque_csv(self.tmp)
        with open(p, encoding="utf-8") as fh:
            body = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("EXTRA BANNER LINE\n" + body)
        d = detect.sniff(p)
        self.assertEqual(d.header_index, 7)
        self.assertEqual(d.account_kind, "cheque")

    def test_unknown_file_rejected_loudly(self):
        p = os.path.join(self.tmp, "junk.csv")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("a,b,c\n1,2,3\n")
        with self.assertRaises(detect.UnknownFormat):
            detect.sniff(p)

    def test_pdf_statement_format_identified(self):
        p = os.path.join(self.tmp, "statement-x.csv")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("Date,Description,Amount,Balance,SourceFile\r\n")
            fh.write("2025-01-15,(no description),-0.50,1234.50,abc.pdf\r\n")
        self.assertEqual(detect.sniff(p).format, detect.FORMAT_B)


if __name__ == "__main__":
    unittest.main()
