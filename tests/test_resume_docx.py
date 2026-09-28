"""U4: stdlib DOCX renderer (design 6.4, M18, 13.2)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

import tests  # noqa: F401
from jobhunter import pdftext
from jobhunter.resume import docx as D
from jobhunter.resume import model as M

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "profile")
EXPECTED_PARTS = {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels",
                  "word/styles.xml", "word/numbering.xml", "docProps/core.xml"}
CORE = {"cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
        "dc": "http://purl.org/dc/elements/1.1/"}


def base_model() -> dict:
    with open(os.path.join(FIX, "base.json"), "r", encoding="utf-8") as fh:
        return M.to_render_model(M.validate(json.load(fh)))


class TestDocx(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jh-docx-")
        self.path = os.path.join(self.dir, "Alex_Rivera_Resume.docx")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def render(self, model=None, opts=None) -> zipfile.ZipFile:
        D.render(model or base_model(), self.path, opts or {"page_size": "A4"})
        return zipfile.ZipFile(self.path)

    def test_parts_and_no_app_xml(self):
        with self.render() as zf:
            names = set(zf.namelist())
            self.assertEqual(names, EXPECTED_PARTS)
            self.assertNotIn("docProps/app.xml", names)
            for n in names:
                ET.fromstring(zf.read(n))          # every part is well-formed XML
            self.assertIsNone(zf.testzip())

    def test_core_properties_name_the_candidate_only(self):
        with self.render() as zf:
            core = ET.fromstring(zf.read("docProps/core.xml"))
            self.assertEqual(core.find("dc:creator", CORE).text, "Alex Rivera")
            self.assertEqual(core.find("cp:lastModifiedBy", CORE).text, "Alex Rivera")
            self.assertEqual(core.find("dc:title", CORE).text, "Alex Rivera Resume")
            blob = b"".join(zf.read(n) for n in zf.namelist()).lower()
            for word in (b"microsoft", b"libreoffice", b"reportlab", b"openclaw", b"jobhunter", b"<application",
                         b"appversion", b"claude"):
                self.assertNotIn(word, blob)

    def test_text_dates_and_no_dash(self):
        with self.render() as zf:
            doc = zf.read("word/document.xml").decode("utf-8")
        self.assertIn(">Jul 2022 to Present<", doc)
        self.assertIn(">Aug 2018 to May 2022<", doc)
        for ch in (chr(0x2013), chr(0x2014), "&#8211;", "&#8212;"):
            self.assertNotIn(ch, doc)
        text = pdftext.docx_text(self.path)
        self.assertIn("Data Analyst, Tidemark Logistics, Springfield\tJul 2022 to Present", text)
        self.assertIn("Built the COD return-risk model; RTO fell from 18% to 13% in two quarters.", text)

    def test_bullets_use_numbering_and_hyperlinks_are_external(self):
        with self.render() as zf:
            doc = zf.read("word/document.xml").decode("utf-8")
            rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
            numbering = zf.read("word/numbering.xml").decode("utf-8")
        self.assertIn('<w:numId w:val="1"/>', doc)
        self.assertIn('w:numFmt w:val="bullet"', numbering)
        self.assertIn('Target="mailto:alex.rivera@example.com" TargetMode="External"', rels)
        self.assertIn('Target="https://github.com/example-alex" TargetMode="External"', rels)
        self.assertIn('<w:hyperlink r:id="rIdL1">', doc)

    def test_page_size_and_determinism(self):
        with self.render(opts={"page_size": "Letter"}) as zf:
            self.assertIn('w:w="12240" w:h="15840"', zf.read("word/document.xml").decode())
        with open(self.path, "rb") as fh:
            first = fh.read()
        self.render(opts={"page_size": "Letter"}).close()
        with open(self.path, "rb") as fh:
            self.assertEqual(first, fh.read())
        with zipfile.ZipFile(self.path) as zf:
            self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist()))

    def test_non_ascii_name_is_escaped(self):
        m = base_model()
        m["contact"]["full_name"] = "Zo" + chr(0xEB) + " Rivera"
        with self.render(m) as zf:
            core = zf.read("docProps/core.xml")
            self.assertIn(b"Zo&#235; Rivera", core)
            self.assertEqual(ET.fromstring(core).find("dc:creator", CORE).text, "Zo" + chr(0xEB) + " Rivera")

    def test_refuses_a_dash(self):
        m = base_model()
        m["skills"].append("SQL " + chr(0x2014) + " advanced")
        with self.assertRaises(ValueError):
            D.render(m, self.path)


if __name__ == "__main__":
    unittest.main()
