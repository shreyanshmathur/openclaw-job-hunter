"""Stdlib PDF renderer for resumes (design 6.4, M18).

PDF 1.4, base-14 Helvetica and Helvetica-Bold with WinAnsiEncoding (widths in fonts_helvetica.py), A4 or
Letter, 17 mm margins, name header, contact line with link annotations, section headings with a thin
rule, bullets drawn as small vector circles, date ranges printed as "Jan 2020 to Present", and
zlib-compressed content streams. The document information holds only Title and Author: no Producer, no
Creator, no dates and no tool name anywhere in the file. Output is deterministic for the same input.
"""
from __future__ import annotations

import os
import zlib

from . import fonts_helvetica as FH
from . import model as M

PAGE_SIZES = {"A4": (595.28, 841.89), "LETTER": (612.0, 792.0)}
MARGIN = 17 * 72 / 25.4          # 17 mm
REG, BOLD = "Helvetica", "Helvetica-Bold"
FONT_KEYS = {REG: "F1", BOLD: "F2"}

NAME_SIZE = 18.0
HEADLINE_SIZE = 10.5
CONTACT_SIZE = 9.0
HEAD_SIZE = 11.0
BODY_SIZE = 9.8
LEADING = 1.28
BULLET_INDENT = 11.0
BULLET_RADIUS = 1.15
SEP = "  |  "


def _num(v: float) -> str:
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _pdf_string(text: str) -> bytes:
    raw = FH.encode(text)
    out = bytearray(b"(")
    for b in raw:
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif 32 <= b < 127:
            out.append(b)
        else:
            out += ("\\%03o" % b).encode("ascii")
    out += b")"
    return bytes(out)


def _info_string(text: str) -> bytes:
    """Document information text: literal ASCII, else UTF-16BE with BOM as a hex string."""
    if all(32 <= ord(c) < 127 for c in text):
        return b"(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii") + b")"
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Greedy word wrap by glyph widths. Words longer than a line are split by characters."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = w if not cur else cur + " " + w
        if FH.string_width(cand, font, size) <= width:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if FH.string_width(w, font, size) <= width:
            cur = w
            continue
        piece = ""
        for ch in w:
            if FH.string_width(piece + ch, font, size) > width and piece:
                lines.append(piece)
                piece = ch
            else:
                piece += ch
        cur = piece
    if cur:
        lines.append(cur)
    return lines or [""]


class _Doc:
    def __init__(self, page_size: str):
        self.w, self.h = PAGE_SIZES.get(str(page_size).upper(), PAGE_SIZES["A4"])
        self.left = MARGIN
        self.right = self.w - MARGIN
        self.top = self.h - MARGIN
        self.bottom = MARGIN
        self.cw = self.right - self.left
        self.pages: list[dict] = []
        self.new_page()

    def new_page(self) -> None:
        self.pages.append({"ops": [], "links": []})
        self.y = self.top

    @property
    def page(self) -> dict:
        return self.pages[-1]

    def ensure(self, height: float) -> None:
        if self.y - height < self.bottom and self.y < self.top:
            self.new_page()

    def text(self, x: float, baseline: float, s: str, font: str, size: float) -> None:
        if s:
            self.page["ops"].append(("text", x, baseline, s, font, size))

    def rule(self, x1: float, x2: float, y: float, width: float = 0.5) -> None:
        self.page["ops"].append(("line", x1, y, x2, y, width))

    def dot(self, cx: float, cy: float, r: float) -> None:
        self.page["ops"].append(("circle", cx, cy, r))

    def link(self, x1: float, y1: float, x2: float, y2: float, uri: str) -> None:
        self.page["links"].append((x1, y1, x2, y2, uri))


def _line_h(size: float) -> float:
    return size * LEADING


# ---------------------------------------------------------------- layout
def _para(doc: _Doc, text: str, font: str, size: float, x: float, width: float) -> None:
    for ln in wrap(text, font, size, width):
        doc.ensure(_line_h(size))
        doc.y -= _line_h(size)
        doc.text(x, doc.y + size * 0.22, ln, font, size)


def _bullet_height(text: str, width: float) -> float:
    return len(wrap(text, REG, BODY_SIZE, width - BULLET_INDENT)) * _line_h(BODY_SIZE) + 1.2


def _bullet(doc: _Doc, text: str) -> None:
    width = doc.cw - BULLET_INDENT
    lines = wrap(text, REG, BODY_SIZE, width)
    doc.ensure(len(lines) * _line_h(BODY_SIZE) + 1.2)
    for i, ln in enumerate(lines):
        doc.y -= _line_h(BODY_SIZE)
        base = doc.y + BODY_SIZE * 0.22
        if i == 0:
            doc.dot(doc.left + 4.0, base + BODY_SIZE * 0.32, BULLET_RADIUS)
        doc.text(doc.left + BULLET_INDENT, base, ln, REG, BODY_SIZE)
    doc.y -= 1.2


def _head_line_height(bold: str, rest: str, right: str, width: float) -> float:
    return _line_h(BODY_SIZE + 0.4) * (1 if _fits(bold, rest, right, width) else 2)


def _fits(bold: str, rest: str, right: str, width: float) -> bool:
    need = FH.string_width(bold, BOLD, BODY_SIZE + 0.4) + FH.string_width(rest, REG, BODY_SIZE + 0.4)
    if right:
        need += FH.string_width(right, REG, BODY_SIZE) + 12
    return need <= width


def _item_head(doc: _Doc, bold: str, rest: str, right: str, keep: float) -> None:
    """One item header: bold part, regular part, right-aligned date. Kept with `keep` points after it."""
    size = BODY_SIZE + 0.4
    height = _head_line_height(bold, rest, right, doc.cw)
    doc.ensure(height + keep + 3)
    doc.y -= 3
    right_w = FH.string_width(right, REG, BODY_SIZE) if right else 0.0
    if _fits(bold, rest, right, doc.cw):
        doc.y -= _line_h(size)
        base = doc.y + size * 0.22
        doc.text(doc.left, base, bold, BOLD, size)
        doc.text(doc.left + FH.string_width(bold, BOLD, size), base, rest, REG, size)
        if right:
            doc.text(doc.right - right_w, base, right, REG, BODY_SIZE)
        return
    doc.y -= _line_h(size)
    base = doc.y + size * 0.22
    avail = doc.cw - (right_w + 12 if right else 0)
    first = wrap(bold, BOLD, size, avail)[0]
    doc.text(doc.left, base, first, BOLD, size)
    if right:
        doc.text(doc.right - right_w, base, right, REG, BODY_SIZE)
    remainder = (bold[len(first):].strip() + " " + rest.lstrip(", ").strip()).strip()
    if remainder:
        _para(doc, remainder, REG, size, doc.left, doc.cw)


def _section_head(doc: _Doc, title: str, keep: float) -> None:
    height = _line_h(HEAD_SIZE) + 8
    doc.ensure(height + keep)
    doc.y -= 7
    doc.y -= _line_h(HEAD_SIZE)
    base = doc.y + HEAD_SIZE * 0.25
    doc.text(doc.left, base, title, BOLD, HEAD_SIZE)
    doc.y -= 2.5
    doc.rule(doc.left, doc.right, doc.y, 0.6)
    doc.y -= 1.5


def _first_bullet_h(doc: _Doc, bullets: list) -> float:
    return _bullet_height(bullets[0]["text"], doc.cw) if bullets else 0.0


def layout(model: dict, opts: dict | None = None) -> _Doc:
    opts = opts or {}
    doc = _Doc(opts.get("page_size") or "A4")
    c = model.get("contact") or {}
    # name
    doc.y -= NAME_SIZE * 1.05
    doc.text(doc.left, doc.y, c.get("full_name") or "", BOLD, NAME_SIZE)
    doc.y -= NAME_SIZE * 0.3
    if model.get("headline"):
        _para(doc, model["headline"], REG, HEADLINE_SIZE, doc.left, doc.cw)
    # contact line with link annotations (wraps at separators)
    items = M.contact_items(model)
    if items:
        rows: list[list] = [[]]
        width = 0.0
        sep_w = FH.string_width(SEP, REG, CONTACT_SIZE)
        for text, uri in items:
            w = FH.string_width(text, REG, CONTACT_SIZE)
            add = w + (sep_w if rows[-1] else 0)
            if rows[-1] and width + add > doc.cw:
                rows.append([])
                width = 0.0
                add = w
            rows[-1].append((text, uri, w))
            width += add
        for row in rows:
            doc.y -= _line_h(CONTACT_SIZE)
            base = doc.y + CONTACT_SIZE * 0.22
            x = doc.left
            for i, (text, uri, w) in enumerate(row):
                if i:
                    doc.text(x, base, SEP, REG, CONTACT_SIZE)
                    x += sep_w
                doc.text(x, base, text, REG, CONTACT_SIZE)
                if uri:
                    doc.link(x, base - 2.0, x + w, base + CONTACT_SIZE * 0.8, uri)
                x += w
    doc.y -= 2

    for sec in M.sections(model):
        title = M.SECTION_TITLES[sec]
        if sec == "summary":
            first_h = _line_h(BODY_SIZE) * 2
            _section_head(doc, title, first_h)
            _para(doc, model["summary"], REG, BODY_SIZE, doc.left, doc.cw)
        elif sec == "experience":
            roles = model["experience"]
            r0 = roles[0]
            _section_head(doc, title, _line_h(BODY_SIZE) * 2 + _first_bullet_h(doc, r0.get("bullets", [])))
            for r in roles:
                rest = ", " + r["employer"] + ((", " + r["location"]) if r.get("location") else "")
                _item_head(doc, r["title"], rest, M.format_range(r.get("dates"), opts),
                           _first_bullet_h(doc, r.get("bullets", [])))
                for b in r.get("bullets", []):
                    _bullet(doc, b["text"])
        elif sec == "projects":
            _section_head(doc, title, _line_h(BODY_SIZE) * 2)
            for p in model["projects"]:
                rest = (", " + p["role"]) if p.get("role") else ""
                _item_head(doc, p["name"], rest, M.format_range(p.get("dates"), opts),
                           _first_bullet_h(doc, p.get("bullets", [])))
                if p.get("link"):
                    doc.ensure(_line_h(BODY_SIZE))
                    doc.y -= _line_h(BODY_SIZE)
                    base = doc.y + BODY_SIZE * 0.22
                    shown = p["link"]
                    lines = wrap(shown, REG, BODY_SIZE - 0.6, doc.cw)
                    doc.text(doc.left, base, lines[0], REG, BODY_SIZE - 0.6)
                    doc.link(doc.left, base - 2, doc.left + FH.string_width(lines[0], REG, BODY_SIZE - 0.6),
                             base + BODY_SIZE * 0.8, p["link"])
                for b in p.get("bullets", []):
                    _bullet(doc, b["text"])
        elif sec == "skills":
            _section_head(doc, title, _line_h(BODY_SIZE))
            _para(doc, ", ".join(model["skills"]), REG, BODY_SIZE, doc.left, doc.cw)
        elif sec == "education":
            _section_head(doc, title, _line_h(BODY_SIZE) * 2)
            for e in model["education"]:
                rest = ", " + e["institution"] + ((", " + e["location"]) if e.get("location") else "")
                _item_head(doc, e["degree"], rest, M.format_range(e.get("dates"), opts), 0)
                for d in e.get("details", []):
                    _bullet(doc, d)
        elif sec == "certifications":
            _section_head(doc, title, _line_h(BODY_SIZE))
            for cert in model["certifications"]:
                rest = (", " + cert["issuer"]) if cert.get("issuer") else ""
                right = M.format_date(cert["date"], opts) if cert.get("date") else ""
                _item_head(doc, cert["name"], rest, right, 0)
        elif sec == "languages":
            _section_head(doc, title, _line_h(BODY_SIZE))
            _para(doc, ", ".join(model["languages"]), REG, BODY_SIZE, doc.left, doc.cw)
    return doc


# ---------------------------------------------------------------- writer
def _content(page: dict) -> bytes:
    out = [b"0 g 0 G"]
    k = 0.5523
    for op in page["ops"]:
        if op[0] == "text":
            _t, x, y, s, font, size = op
            out.append(b"BT /" + FONT_KEYS[font].encode() + b" " + _num(size).encode() + b" Tf " +
                       _num(x).encode() + b" " + _num(y).encode() + b" Td " + _pdf_string(s) + b" Tj ET")
        elif op[0] == "line":
            _t, x1, y1, x2, y2, w = op
            out.append(("0.35 G %s w %s %s m %s %s l S 0 G" % (_num(w), _num(x1), _num(y1), _num(x2), _num(y2)))
                       .encode())
        elif op[0] == "circle":
            _t, cx, cy, r = op
            pts = [
                "%s %s m" % (_num(cx + r), _num(cy)),
                "%s %s %s %s %s %s c" % (_num(cx + r), _num(cy + k * r), _num(cx + k * r), _num(cy + r),
                                         _num(cx), _num(cy + r)),
                "%s %s %s %s %s %s c" % (_num(cx - k * r), _num(cy + r), _num(cx - r), _num(cy + k * r),
                                         _num(cx - r), _num(cy)),
                "%s %s %s %s %s %s c" % (_num(cx - r), _num(cy - k * r), _num(cx - k * r), _num(cy - r),
                                         _num(cx), _num(cy - r)),
                "%s %s %s %s %s %s c" % (_num(cx + k * r), _num(cy - r), _num(cx + r), _num(cy - k * r),
                                         _num(cx + r), _num(cy)),
                "f",
            ]
            out.append(" ".join(pts).encode())
    return b"\n".join(out) + b"\n"


def _uri_string(uri: str) -> bytes:
    safe = "".join(c for c in uri if 32 < ord(c) < 127)
    return b"(" + safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii") + b")"


def build_pdf(doc: _Doc, title: str, author: str) -> bytes:
    objs: list[bytes] = []     # index i -> object i+1

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")          # 1, filled later
    pages_id = add(b"")         # 2, filled later
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    info = add(b"<< /Title " + _info_string(title) + b" /Author " + _info_string(author) + b" >>")
    kids = []
    for page in doc.pages:
        raw = _content(page)
        comp = zlib.compress(raw, 9)
        content_id = add(b"<< /Length " + str(len(comp)).encode() + b" /Filter /FlateDecode >>\nstream\n" +
                         comp + b"\nendstream")
        annots = []
        for (x1, y1, x2, y2, uri) in page["links"]:
            annots.append(add(("<< /Type /Annot /Subtype /Link /Rect [%s %s %s %s] /Border [0 0 0] /A << /S /URI "
                               "/URI " % (_num(x1), _num(y1), _num(x2), _num(y2))).encode() + _uri_string(uri) +
                              b" >> >>"))
        annot_part = b""
        if annots:
            annot_part = b" /Annots [" + b" ".join(b"%d 0 R" % a for a in annots) + b"]"
        page_id = add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources << /Font << /F1 %d 0 R "
                       "/F2 %d 0 R >> >> /Contents %d 0 R" % (pages_id, _num(doc.w), _num(doc.h), f1, f2,
                                                               content_id)).encode() + annot_part + b" >>")
        kids.append(page_id)
    objs[catalog - 1] = ("<< /Type /Catalog /Pages %d 0 R >>" % pages_id).encode()
    objs[pages_id - 1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids),
                                                                      len(kids))).encode()
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, catalog, info, xref))
    return bytes(out)


def render(model: dict, out_path: str, opts: dict | None = None) -> dict:
    """Write the PDF and return {"path", "pages", "bytes"}. Text that WinAnsi cannot show raises ValueError."""
    opts = opts or {}
    M.assert_printable(model)
    doc = layout(model, opts)
    c = model.get("contact") or {}
    name = (c.get("full_name") or ("%s %s" % (c.get("first_name") or "", c.get("last_name") or ""))).strip()
    data = build_pdf(doc, "%s Resume" % name if name else "Resume", name)
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, out_path)
    return {"path": out_path, "pages": len(doc.pages), "bytes": len(data)}


def count_pages(model: dict, opts: dict | None = None) -> int:
    return len(layout(model, opts or {}).pages)
