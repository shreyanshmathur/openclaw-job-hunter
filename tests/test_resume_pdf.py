"""U4: stdlib PDF renderer (design 6.4, M18, 13.2) and the stdlib PDF text extractor used by profile import."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import unittest
import zlib

import tests  # noqa: F401
from jobhunter import pdftext
from jobhunter.resume import model as M
from jobhunter.resume import pdf as P

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "profile")


def base_model() -> dict:
    with open(os.path.join(FIX, "base.json"), "r", encoding="utf-8") as fh:
        return M.to_render_model(M.validate(json.load(fh)))


def streams(data: bytes) -> list[bytes]:
    out = []
    for m in re.finditer(rb"/Filter /FlateDecode >>\nstream\n", data):
        start = m.end()
        end = data.index(b"\nendstream", start)
        out.append(zlib.decompress(data[start:end]))
    return out


class PdfCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jh-pdf-")
        self.path = os.path.join(self.dir, "Alex_Rivera_Resume.pdf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def render(self, model=None, opts=None) -> bytes:
        info = P.render(model or base_model(), self.path, opts or {"page_size": "A4"})
        self.info = info
        with open(self.path, "rb") as fh:
            return fh.read()


class TestPdfStructure(PdfCase):
    def test_header_trailer_and_xref(self):
        data = self.render()
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        self.assertTrue(data.endswith(b"%%EOF\n"))
        startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
        self.assertTrue(data[startxref:].startswith(b"xref\n0 "))
        count = int(re.match(rb"xref\n0 (\d+)\n", data[startxref:]).group(1))
        entries = re.findall(rb"(\d{10}) 00000 n \n", data[startxref:])
        self.assertEqual(len(entries), count - 1)
        for i, off in enumerate(entries, 1):
            self.assertTrue(data[int(off):].startswith(b"%d 0 obj\n" % i), i)
        self.assertEqual(self.info["pages"], 1)

    def test_metadata_has_no_producer_creator_or_tool_name(self):
        data = self.render()
        self.assertNotIn(b"/Producer", data)
        self.assertNotIn(b"/Creator", data)
        self.assertNotIn(b"/CreationDate", data)
        outside = re.sub(rb"stream\n.*?\nendstream", b"", data, flags=re.S).lower()
        for word in (b"python", b"reportlab", b"openclaw", b"jobhunter", b"job-hunter", b"libreoffice", b"claude"):
            self.assertNotIn(word, outside)
            if word != b"python":          # the fixture's skills line names Python
                for s in streams(data):
                    self.assertNotIn(word, s.lower())
        self.assertIn(b"/Title (Alex Rivera Resume)", data)
        self.assertIn(b"/Author (Alex Rivera)", data)

    def test_fonts_and_encoding(self):
        data = self.render()
        self.assertIn(b"/BaseFont /Helvetica /Encoding /WinAnsiEncoding", data)
        self.assertIn(b"/BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding", data)
        self.assertNotIn(b"/FontFile", data)

    def test_dates_are_words_and_no_dash_reaches_the_file(self):
        data = self.render()
        content = b"".join(streams(data))
        self.assertIn(b"(Jul 2022 to Present) Tj", content)
        self.assertIn(b"(Jan 2021 to Jun 2021) Tj", content)
        self.assertNotIn(b"\\226", content)     # WinAnsi en dash (octal escape)
        self.assertNotIn(b"\\227", content)     # WinAnsi em dash
        self.assertNotIn(b"\x96", content)
        self.assertNotIn(b"\x97", content)
        self.assertIsNone(re.search(rb"\(\s*-\s*\)", content))

    def test_bullets_are_vector_circles_and_rules_are_drawn(self):
        content = b"".join(streams(self.render()))
        self.assertGreaterEqual(content.count(b" c ") + content.count(b" c\n"), 4 * 8)
        self.assertIn(b" f", content)
        self.assertIn(b" l S", content)
        self.assertNotIn(b"\\225", content)     # no WinAnsi bullet glyph in text

    def test_link_annotations(self):
        data = self.render()
        self.assertIn(b"/Subtype /Link", data)
        self.assertIn(b"/URI (mailto:alex.rivera@example.com)", data)
        self.assertIn(b"/URI (https://www.linkedin.com/in/example-alex)", data)
        self.assertIn(b"/URI (https://github.com/example-alex/eta)", data)

    def test_deterministic(self):
        a = self.render()
        b = self.render()
        self.assertEqual(a, b)

    def test_letter_size(self):
        data = self.render(opts={"page_size": "Letter"})
        self.assertIn(b"/MediaBox [0 0 612 792]", data)
        data = self.render(opts={"page_size": "A4"})
        self.assertIn(b"/MediaBox [0 0 595.28 841.89]", data)

    def test_long_resume_breaks_pages(self):
        m = base_model()
        role = m["experience"][0]
        role["bullets"] = [{"id": "E1.B%d" % i, "text": "Built weekly returns forecasts in SQL and Python for 40 "
                                                        "warehouses and reviewed them with the operations team %d." % i}
                           for i in range(1, 90)]
        data = self.render(m)
        self.assertGreaterEqual(self.info["pages"], 3)
        self.assertEqual(data.count(b"/Type /Page "), self.info["pages"])
        self.assertIn(b"/Count %d" % self.info["pages"], data)
        self.assertEqual(P.count_pages(m, {}), self.info["pages"])

    def test_non_ascii_name_in_metadata(self):
        m = base_model()
        m["contact"]["full_name"] = "Zo" + chr(0xEB) + " Rivera"
        data = self.render(m)
        want = b"/Author <FEFF" + ("Zo" + chr(0xEB) + " Rivera").encode("utf-16-be").hex().upper().encode() + b">"
        self.assertIn(want, data)
        self.assertIn(b"(Zo\\353 Rivera) Tj", b"".join(streams(data)))

    def test_refuses_dash_and_unencodable_text(self):
        m = base_model()
        m["experience"][0]["bullets"][0]["text"] = "Built forecasts " + chr(0x2013) + " fast."
        with self.assertRaises(ValueError):
            P.render(m, self.path)
        m = base_model()
        m["experience"][0]["bullets"][0]["text"] = "Built forecasts " + chr(0x4E2D)
        with self.assertRaises(ValueError):
            P.render(m, self.path)

    def test_wrap(self):
        lines = P.wrap("one two three four five six seven eight nine ten", "Helvetica", 10, 60)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), "one two three four five six seven eight nine ten")
        long = P.wrap("x" * 200, "Helvetica", 10, 50)
        self.assertEqual("".join(long), "x" * 200)


class TestPdfText(PdfCase):
    def test_round_trip_with_the_stdlib_extractor(self):
        self.render()
        res = pdftext.extract(self.path, use_external=False)
        self.assertEqual(res["method"], "stdlib_pdf")
        text = res["text"]
        self.assertIn("Alex Rivera", text)
        self.assertIn("Data Analyst, Tidemark Logistics, Springfield Jul 2022 to Present", text)
        self.assertIn("Built the COD return-risk model; RTO fell from 18% to 13% in two quarters.", text)
        self.assertIn("alex.rivera@example.com", text)
        self.assertEqual(res["quality"], "good")

    def test_type0_font_with_tounicode_cmap(self):
        cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                b"2 beginbfchar <0001> <0048> <0002> <0069> endbfchar\n"
                b"1 beginbfrange <0010> <0012> <0041> endbfrange\n"
                b"endcmap CMapName currentdict /CMap defineresource pop end end\n")
        content = b"BT /F1 12 Tf 72 700 Td <00010002> Tj 0 -14 Td [<0010> -300 <00110012>] TJ ET\n"
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
            b"/Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type0 /BaseFont /Example /Encoding /Identity-H /ToUnicode 6 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(zlib.compress(cmap)) + zlib.compress(cmap) +
            b"\nendstream",
        ]
        data = b"%PDF-1.5\n" + b"".join(b"%d 0 obj\n" % i + o + b"\nendobj\n" for i, o in enumerate(objs, 1)) + \
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
        text = pdftext.pdf_text(data)
        self.assertEqual(text.split("\n"), ["Hi", "A BC"])

    def test_not_a_pdf(self):
        from jobhunter.errors import Denied
        with self.assertRaises(Denied):
            pdftext.pdf_text(b"hello")

    def test_quality(self):
        self.assertEqual(pdftext.quality("a few words"), "poor")
        self.assertEqual(pdftext.quality(("Data analyst with forecasting and risk models. " * 20)), "good")


if __name__ == "__main__":
    unittest.main()
