"""Stdlib DOCX renderer for resumes (design 6.4, M18).

Written with `zipfile` only: `[Content_Types].xml`, `_rels/.rels`, `word/document.xml`,
`word/_rels/document.xml.rels` (styles, numbering and hyperlink targets), `word/styles.xml`,
`word/numbering.xml` and `docProps/core.xml` whose `dc:creator` and `cp:lastModifiedBy` are the
candidate's name. There is no `docProps/app.xml`, so no application name is recorded. Zip entries carry a
fixed timestamp, so the same input gives the same bytes.
"""
from __future__ import annotations

import os
import zipfile
from xml.sax.saxutils import escape

from . import model as M

PAGE_TWIPS = {"A4": (11906, 16838), "LETTER": (12240, 15840)}
MARGIN_TWIPS = 964          # 17 mm
FONT = "Arial"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _x(text: str) -> str:
    """XML text with every non-ASCII character as a character reference (the XML stays ASCII)."""
    return escape(text or "", {'"': "&quot;"}).encode("ascii", "xmlcharrefreplace").decode("ascii")


def _run(text: str, bold: bool = False, size_half_pts: int | None = None, style: str | None = None) -> str:
    props = []
    if style:
        props.append('<w:rStyle w:val="%s"/>' % style)
    if bold:
        props.append("<w:b/>")
    if size_half_pts:
        props.append('<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (size_half_pts, size_half_pts))
    rpr = "<w:rPr>%s</w:rPr>" % "".join(props) if props else ""
    return '<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, _x(text))


def _tab_run() -> str:
    return "<w:r><w:tab/></w:r>"


class _Doc:
    def __init__(self, page_size: str):
        self.w, self.h = PAGE_TWIPS.get(str(page_size).upper(), PAGE_TWIPS["A4"])
        self.body: list[str] = []
        self.links: list[str] = []

    @property
    def text_width(self) -> int:
        return self.w - 2 * MARGIN_TWIPS

    def link(self, url: str) -> str:
        self.links.append(url)
        return "rIdL%d" % len(self.links)

    def para(self, runs: str, style: str | None = None, tab_right: bool = False, bullet: bool = False,
             space_before: int | None = None, keep_next: bool = False) -> None:
        ppr = []
        if style:
            ppr.append('<w:pStyle w:val="%s"/>' % style)
        if keep_next:
            ppr.append("<w:keepNext/>")
        if bullet:
            ppr.append('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        if tab_right:
            ppr.append('<w:tabs><w:tab w:val="right" w:pos="%d"/></w:tabs>' % self.text_width)
        if space_before is not None:
            ppr.append('<w:spacing w:before="%d"/>' % space_before)
        p = "<w:pPr>%s</w:pPr>" % "".join(ppr) if ppr else ""
        self.body.append("<w:p>%s%s</w:p>" % (p, runs))


def _item(doc: _Doc, bold: str, rest: str, right: str, keep_next: bool) -> None:
    runs = _run(bold, bold=True) + (_run(rest) if rest else "")
    if right:
        runs += _tab_run() + _run(right)
    doc.para(runs, tab_right=bool(right), space_before=80, keep_next=keep_next)


def document_xml(model: dict, opts: dict | None = None) -> tuple[str, list[str], int, int]:
    opts = opts or {}
    doc = _Doc(opts.get("page_size") or "A4")
    c = model.get("contact") or {}
    doc.para(_run(c.get("full_name") or "", bold=True, size_half_pts=36), style="Title")
    if model.get("headline"):
        doc.para(_run(model["headline"], size_half_pts=21))
    items = M.contact_items(model)
    if items:
        parts = []
        for i, (text, uri) in enumerate(items):
            if i:
                parts.append(_run("  |  ", size_half_pts=18))
            if uri:
                rid = doc.link(uri)
                parts.append('<w:hyperlink r:id="%s">%s</w:hyperlink>' % (rid, _run(text, size_half_pts=18,
                                                                                   style="Hyperlink")))
            else:
                parts.append(_run(text, size_half_pts=18))
        doc.para("".join(parts))
    for sec in M.sections(model):
        doc.para(_run(M.SECTION_TITLES[sec]), style="SectionHeading", keep_next=True)
        if sec == "summary":
            doc.para(_run(model["summary"]))
        elif sec == "experience":
            for r in model["experience"]:
                rest = ", " + r["employer"] + ((", " + r["location"]) if r.get("location") else "")
                _item(doc, r["title"], rest, M.format_range(r.get("dates"), opts), keep_next=True)
                for b in r.get("bullets", []):
                    doc.para(_run(b["text"]), bullet=True)
        elif sec == "projects":
            for p in model["projects"]:
                rest = (", " + p["role"]) if p.get("role") else ""
                _item(doc, p["name"], rest, M.format_range(p.get("dates"), opts), keep_next=True)
                if p.get("link"):
                    rid = doc.link(p["link"])
                    doc.para('<w:hyperlink r:id="%s">%s</w:hyperlink>' % (rid, _run(p["link"], style="Hyperlink")))
                for b in p.get("bullets", []):
                    doc.para(_run(b["text"]), bullet=True)
        elif sec == "skills":
            doc.para(_run(", ".join(model["skills"])))
        elif sec == "education":
            for e in model["education"]:
                rest = ", " + e["institution"] + ((", " + e["location"]) if e.get("location") else "")
                _item(doc, e["degree"], rest, M.format_range(e.get("dates"), opts), keep_next=bool(e.get("details")))
                for d in e.get("details", []):
                    doc.para(_run(d), bullet=True)
        elif sec == "certifications":
            for cert in model["certifications"]:
                rest = (", " + cert["issuer"]) if cert.get("issuer") else ""
                right = M.format_date(cert["date"], opts) if cert.get("date") else ""
                _item(doc, cert["name"], rest, right, keep_next=False)
        elif sec == "languages":
            doc.para(_run(", ".join(model["languages"])))
    sect = ('<w:sectPr><w:pgSz w:w="%d" w:h="%d"/><w:pgMar w:top="%d" w:right="%d" w:bottom="%d" w:left="%d" '
            'w:header="709" w:footer="709" w:gutter="0"/></w:sectPr>'
            % (doc.w, doc.h, MARGIN_TWIPS, MARGIN_TWIPS, MARGIN_TWIPS, MARGIN_TWIPS))
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<w:document xmlns:w="%s" xmlns:r="%s"><w:body>%s%s</w:body></w:document>'
           % (NS_W, NS_R, "".join(doc.body), sect))
    return xml, doc.links, doc.w, doc.h


STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:styles xmlns:w="%s">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="%s" w:hAnsi="%s" w:cs="%s" w:eastAsia="%s"/>'
    '<w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="40" w:line="252" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    '</w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="60"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="36"/><w:szCs w:val="36"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="SectionHeading"><w:name w:val="Section Heading"/>'
    '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="60"/>'
    '<w:pBdr><w:bottom w:val="single" w:sz="4" w:space="1" w:color="595959"/></w:pBdr></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>'
    '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>'
    '<w:rPr><w:color w:val="1F3864"/></w:rPr></w:style>'
    '</w:styles>' % (NS_W, FONT, FONT, FONT, FONT))

NUMBERING_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:numbering xmlns:w="%s">'
    '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/>'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>'
    '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="284" w:hanging="170"/></w:pPr>'
    '<w:rPr><w:rFonts w:ascii="%s" w:hAnsi="%s"/></w:rPr></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    '</w:numbering>' % (NS_W, FONT, FONT))

CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/numbering.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    '<Override PartName="/docProps/core.xml" '
    'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '</Types>')

ROOT_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="%s/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" '
    'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
    'Target="docProps/core.xml"/>'
    '</Relationships>' % REL_BASE)


def document_rels_xml(links: list[str]) -> str:
    rels = ['<Relationship Id="rId1" Type="%s/styles" Target="styles.xml"/>' % REL_BASE,
            '<Relationship Id="rId2" Type="%s/numbering" Target="numbering.xml"/>' % REL_BASE]
    for i, url in enumerate(links, 1):
        rels.append('<Relationship Id="rIdL%d" Type="%s/hyperlink" Target="%s" TargetMode="External"/>'
                    % (i, REL_BASE, _x(url)))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s'
            '</Relationships>' % "".join(rels))


def core_xml(name: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>%s</dc:title><dc:creator>%s</dc:creator><cp:lastModifiedBy>%s</cp:lastModifiedBy>'
            '</cp:coreProperties>' % (_x("%s Resume" % name if name else "Resume"), _x(name), _x(name)))


def render(model: dict, out_path: str, opts: dict | None = None) -> dict:
    """Write the DOCX and return {"path", "bytes"}."""
    M.assert_printable(model)
    xml, links, _w, _h = document_xml(model, opts)
    c = model.get("contact") or {}
    name = (c.get("full_name") or ("%s %s" % (c.get("first_name") or "", c.get("last_name") or ""))).strip()
    parts = [
        ("[Content_Types].xml", CONTENT_TYPES_XML),
        ("_rels/.rels", ROOT_RELS_XML),
        ("word/document.xml", xml),
        ("word/_rels/document.xml.rels", document_rels_xml(links)),
        ("word/styles.xml", STYLES_XML),
        ("word/numbering.xml", NUMBERING_XML),
        ("docProps/core.xml", core_xml(name)),
    ]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, text in parts:
            info = zipfile.ZipInfo(arcname, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            info.create_system = 0
            zf.writestr(info, text.encode("utf-8"))
    os.replace(tmp, out_path)
    return {"path": out_path, "bytes": os.path.getsize(out_path)}
