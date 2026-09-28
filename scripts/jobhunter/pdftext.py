"""Resume text extraction for `profile import` (design 8 step 3).

- PDF: `pdftotext -layout` when it is installed, else a stdlib best-effort extractor (objects, object
  streams, FlateDecode, simple fonts with WinAnsi, Type0 fonts with a ToUnicode CMap, Tj/TJ/'/" text
  operators and line breaks from the text matrix).
- DOCX: `zipfile` + `xml.etree` over word/document.xml (paragraphs, tabs, breaks).
- TXT and MD: read as UTF-8.

`extract(path)` returns {"text", "method", "chars", "quality"}; quality is "good" or "poor". A poor result
means the person pastes the text into private/resume/resume.txt.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
import zlib
from xml.etree import ElementTree as ET

from .errors import Denied

MAX_INPUT_BYTES = 20_000_000
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ---------------------------------------------------------------- public
def extract(path: str, use_external: bool = True) -> dict:
    if not os.path.isfile(path):
        raise Denied("E_NOT_FOUND", "resume file not found", data={"path": path})
    if os.path.getsize(path) > MAX_INPUT_BYTES:
        raise Denied("E_VALIDATION", "resume file is larger than 20 MB", data={"path": path})
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        text, method = None, "stdlib_pdf"
        if use_external:
            text = _pdftotext(path)
            if text is not None:
                method = "pdftotext"
        if text is None or quality(text) == "poor":
            try:
                with open(path, "rb") as fh:
                    own = pdf_text(fh.read())
            except Exception:  # a broken PDF must not crash import; the person pastes the text instead
                own = ""
            if text is None or quality(own) == "good":
                text, method = own, "stdlib_pdf"
    elif ext == ".docx":
        text, method = docx_text(path), "docx_zip"
    elif ext in (".txt", ".md"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text, method = fh.read(), "text"
    else:
        raise Denied("E_VALIDATION", "resume must be a PDF, DOCX, TXT or MD file", data={"path": path})
    text = _tidy(text or "")
    return {"text": text, "method": method, "chars": len(text), "quality": quality(text)}


def quality(text: str) -> str:
    letters = sum(1 for c in text if c.isalpha())
    words = len(re.findall(r"[A-Za-z]{2,}", text))
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
    ratio = printable / max(1, len(text))
    replacement = text.count("\ufffd")
    if letters >= 300 and words >= 60 and ratio >= 0.97 and replacement <= 3:
        return "good"
    return "poor"


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n" if text.strip() else ""


def _pdftotext(path: str) -> str | None:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        res = subprocess.run([exe, "-layout", "-enc", "UTF-8", path, "-"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- DOCX
def docx_text(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise Denied("E_VALIDATION", "not a readable DOCX file: %s" % exc, data={"path": path})
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise Denied("E_VALIDATION", "DOCX document.xml is not valid XML: %s" % exc)
    lines = []
    for p in root.iter(W_NS + "p"):
        parts = []
        for run in p.iter(W_NS + "r"):      # runs only: w:tab inside w:pPr/w:tabs is a tab stop, not text
            for el in run:
                if el.tag == W_NS + "t":
                    parts.append(el.text or "")
                elif el.tag == W_NS + "tab":
                    parts.append("\t")
                elif el.tag in (W_NS + "br", W_NS + "cr"):
                    parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------- PDF tokenizer
_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]{}/%"


class _Ref:
    __slots__ = ("num",)

    def __init__(self, num: int):
        self.num = num

    def __repr__(self):
        return "Ref(%d)" % self.num


class _Name(str):
    pass


class _Op(str):
    pass


class _Lexer:
    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.i = pos
        self.n = len(data)

    def skip(self) -> None:
        d, n = self.d, self.n
        while self.i < n:
            c = d[self.i]
            if c in _WS:
                self.i += 1
            elif c == 0x25:     # % comment
                while self.i < n and d[self.i] not in b"\r\n":
                    self.i += 1
            else:
                break

    def token(self):
        """Next primitive token, or None at the end. Dicts and arrays come back as '<<', '>>', '[', ']'."""
        self.skip()
        d, n = self.d, self.n
        if self.i >= n:
            return None
        c = d[self.i]
        if c == 0x2F:           # /name
            j = self.i + 1
            while j < n and d[j] not in _WS and d[j] not in _DELIM:
                j += 1
            raw = d[self.i + 1:j]
            self.i = j
            name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
            return _Name(name.decode("latin-1"))
        if c == 0x28:           # (string)
            return self._literal()
        if c == 0x3C:
            if self.i + 1 < n and d[self.i + 1] == 0x3C:
                self.i += 2
                return "<<"
            j = d.find(b">", self.i)
            if j < 0:
                j = n
            hx = re.sub(rb"[^0-9A-Fa-f]", b"", d[self.i + 1:j])
            if len(hx) % 2:
                hx += b"0"
            self.i = j + 1
            return bytes.fromhex(hx.decode("ascii"))
        if c == 0x3E:
            if self.i + 1 < n and d[self.i + 1] == 0x3E:
                self.i += 2
                return ">>"
            self.i += 1
            return self.token()
        if c in b"[]{}":
            self.i += 1
            return chr(c)
        j = self.i
        while j < n and d[j] not in _WS and d[j] not in _DELIM:
            j += 1
        if j == self.i:
            self.i += 1
            return self.token()
        word = d[self.i:j]
        self.i = j
        try:
            if re.match(rb"^[+-]?\d+$", word):
                return int(word)
            if re.match(rb"^[+-]?(\d+\.\d*|\.\d+|\d+)$", word):
                return float(word)
        except ValueError:
            pass
        return _Op(word.decode("latin-1"))

    def _literal(self) -> bytes:
        d, n = self.d, self.n
        self.i += 1
        depth = 1
        out = bytearray()
        while self.i < n:
            c = d[self.i]
            if c == 0x5C:
                self.i += 1
                if self.i >= n:
                    break
                e = d[self.i]
                esc = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12, 0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
                if e in esc:
                    out.append(esc[e])
                    self.i += 1
                elif 0x30 <= e <= 0x37:
                    j = self.i
                    while j < n and j < self.i + 3 and 0x30 <= d[j] <= 0x37:
                        j += 1
                    out.append(int(d[self.i:j], 8) & 0xFF)
                    self.i = j
                elif e in b"\r\n":
                    self.i += 1
                    if e == 0x0D and self.i < n and d[self.i] == 0x0A:
                        self.i += 1
                else:
                    out.append(e)
                    self.i += 1
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    self.i += 1
                    break
            out.append(c)
            self.i += 1
        return bytes(out)

    def value(self):
        """Parse one object (dict, array, ref, primitive)."""
        t = self.token()
        return self._value_from(t)

    def _value_from(self, t):
        if t == "<<":
            out = {}
            while True:
                k = self.token()
                if k is None or k == ">>":
                    return out
                v = self.value()
                if isinstance(k, _Name):
                    out[str(k)] = v
        if t == "[":
            arr = []
            while True:
                save = self.i
                x = self.token()
                if x is None or x == "]":
                    return arr
                self.i = save
                arr.append(self.value())
        if isinstance(t, int):
            save = self.i
            t2 = self.token()
            if isinstance(t2, int):
                t3 = self.token()
                if t3 == "R":
                    return _Ref(t)
            self.i = save
            return t
        return t


# ---------------------------------------------------------------- PDF objects
_OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")


class _Pdf:
    def __init__(self, data: bytes):
        self.d = data
        self.objs: dict[int, object] = {}
        self.streams: dict[int, bytes] = {}
        self._scan()

    def _scan(self) -> None:
        d = self.d
        for m in _OBJ_RE.finditer(d):
            num = int(m.group(1))
            lx = _Lexer(d, m.end())
            try:
                val = lx.value()
            except (ValueError, IndexError, RecursionError):
                continue
            self.objs[num] = val
            lx.skip()
            if isinstance(val, dict) and d.startswith(b"stream", lx.i):
                start = lx.i + 6
                if d[start:start + 2] == b"\r\n":
                    start += 2
                elif d[start:start + 1] in (b"\n", b"\r"):
                    start += 1
                length = val.get("Length")
                end = -1
                if isinstance(length, int) and d[start + length:start + length + 20].lstrip().startswith(
                        b"endstream"):
                    end = start + length
                if end < 0:
                    end = d.find(b"endstream", start)
                    if end < 0:
                        continue
                    while end > start and d[end - 1:end] in (b"\n", b"\r"):
                        end -= 1
                self.streams[num] = d[start:end]
        for num, val in list(self.objs.items()):
            if isinstance(val, dict) and val.get("Type") == "ObjStm" and num in self.streams:
                self._objstm(val, self.decoded(num))

    def _objstm(self, head: dict, data: bytes | None) -> None:
        if not data:
            return
        n, first = head.get("N"), head.get("First")
        if not isinstance(n, int) or not isinstance(first, int):
            return
        lx = _Lexer(data)
        pairs = []
        for _ in range(n):
            a, b = lx.token(), lx.token()
            if not isinstance(a, int) or not isinstance(b, int):
                return
            pairs.append((a, b))
        for num, off in pairs:
            if num in self.objs:
                continue
            try:
                self.objs[num] = _Lexer(data, first + off).value()
            except (ValueError, IndexError, RecursionError):
                continue

    def get(self, v):
        seen = 0
        while isinstance(v, _Ref) and seen < 20:
            v = self.objs.get(v.num)
            seen += 1
        return v

    def decoded(self, num: int) -> bytes | None:
        raw = self.streams.get(num)
        head = self.objs.get(num)
        if raw is None or not isinstance(head, dict):
            return None
        filt = self.get(head.get("Filter"))
        filters = filt if isinstance(filt, list) else ([filt] if filt else [])
        data = raw
        for f in filters:
            f = str(self.get(f))
            if f in ("FlateDecode", "Fl"):
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    try:
                        data = zlib.decompressobj().decompress(data)
                    except zlib.error:
                        return None
            elif f in ("ASCIIHexDecode", "AHx"):
                hx = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
                data = bytes.fromhex((hx + (b"0" if len(hx) % 2 else b"")).decode())
            else:
                return None
        return data

    def stream_of(self, ref) -> bytes | None:
        if isinstance(ref, _Ref):
            return self.decoded(ref.num)
        return None

    def pages(self) -> list[dict]:
        out: list[dict] = []
        root = None
        for val in self.objs.values():
            if isinstance(val, dict) and val.get("Type") == "Catalog":
                root = val
                break

        def walk(node, inherited, depth):
            node = self.get(node)
            if not isinstance(node, dict) or depth > 30:
                return
            res = node.get("Resources", inherited)
            if node.get("Type") == "Pages" or "Kids" in node:
                for kid in self.get(node.get("Kids")) or []:
                    walk(kid, res, depth + 1)
            elif node.get("Type") == "Page":
                page = dict(node)
                page["Resources"] = res
                out.append(page)

        if root is not None:
            walk(root.get("Pages"), None, 0)
        if not out:
            out = [v for v in self.objs.values() if isinstance(v, dict) and v.get("Type") == "Page"]
        return out


# ---------------------------------------------------------------- fonts
def _parse_cmap(data: bytes) -> tuple[dict, int]:
    """ToUnicode CMap -> ({code: text}, code byte width)."""
    width = 1
    m = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", data)
    if m:
        width = max(1, len(m.group(1)) // 2)
    out: dict[int, str] = {}

    def u(hexs: bytes) -> str:
        b = bytes.fromhex(hexs.decode())
        try:
            return b.decode("utf-16-be")
        except UnicodeDecodeError:
            return ""

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[int(src, 16)] = u(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)", block):
            a, b = int(lo, 16), int(hi, 16)
            if b - a > 5000:
                continue
            if rest.startswith(b"["):
                dsts = re.findall(rb"<([0-9A-Fa-f]*)>", rest)
                for k, dst in enumerate(dsts):
                    out[a + k] = u(dst)
            else:
                base = rest[1:-1]
                if not base:
                    continue
                start = int(base, 16)
                nbytes = len(base) // 2
                for k in range(b - a + 1):
                    try:
                        out[a + k] = (start + k).to_bytes(nbytes, "big").decode("utf-16-be")
                    except (UnicodeDecodeError, OverflowError):
                        pass
    return out, width


class _Font:
    def __init__(self, pdf: _Pdf, fdict):
        fdict = pdf.get(fdict) if fdict is not None else None
        self.cmap: dict | None = None
        self.width = 1
        self.two_byte = False
        if isinstance(fdict, dict):
            self.two_byte = fdict.get("Subtype") == "Type0"
            tu = fdict.get("ToUnicode")
            data = pdf.stream_of(tu) if tu is not None else None
            if data:
                self.cmap, self.width = _parse_cmap(data)
            if self.two_byte:
                self.width = 2

    def decode(self, s: bytes) -> str:
        if self.cmap is not None:
            out = []
            w = self.width
            for i in range(0, len(s) - w + 1, w):
                code = int.from_bytes(s[i:i + w], "big")
                out.append(self.cmap.get(code, "" if self.two_byte else chr(code) if 32 <= code < 127 else ""))
            return "".join(out)
        if self.two_byte:
            return ""
        try:
            return s.decode("cp1252")
        except UnicodeDecodeError:
            return s.decode("latin-1")


# ---------------------------------------------------------------- content streams
def _page_text(pdf: _Pdf, page: dict) -> str:
    res = pdf.get(page.get("Resources")) or {}
    fonts_d = pdf.get(res.get("Font")) if isinstance(res, dict) else None
    fonts: dict[str, _Font] = {}
    if isinstance(fonts_d, dict):
        for name, ref in fonts_d.items():
            fonts[name] = _Font(pdf, ref)
    contents = pdf.get(page.get("Contents"))
    refs = contents if isinstance(contents, list) else [page.get("Contents")]
    data = b""
    for ref in refs:
        chunk = pdf.stream_of(ref)
        if chunk:
            data += chunk + b"\n"
    if not data:
        return ""
    lx = _Lexer(data)
    out: list[str] = []
    operands: list = []
    font = _Font(pdf, None)
    state = {"line_y": None, "ly": 0.0, "lx": 0.0, "lead": 0.0, "last_x": None}

    def place(s: str) -> None:
        """Append shown text, starting a new line when the baseline moved and a space when the run
        starts elsewhere on the same line."""
        if not s:
            return
        y, x = state["ly"], state["lx"]
        if state["line_y"] is not None and abs(y - state["line_y"]) > 0.5:
            if out and not out[-1].endswith("\n"):
                out.append("\n")
        elif (state["last_x"] is not None and x != state["last_x"] and out and
              not out[-1].endswith((" ", "\n")) and not s[:1] in (" ", ",", ".", ";", ":", ")")):
            out.append(" ")
        state["line_y"] = y
        state["last_x"] = x
        out.append(s)

    guard = 0
    while True:
        guard += 1
        if guard > 2_000_000:
            break
        save = lx.i
        t = lx.token()
        if t is None:
            break
        if t in ("<<", "["):
            lx.i = save
            operands.append(lx.value())
            continue
        if not isinstance(t, _Op):
            operands.append(t)
            continue
        op = str(t)
        nums = [v for v in operands if isinstance(v, (int, float))]
        if op == "BI":          # inline image: skip to EI
            j = data.find(b"EI", lx.i)
            lx.i = len(data) if j < 0 else j + 2
        elif op == "Tf" and len(operands) >= 2 and isinstance(operands[-2], _Name):
            font = fonts.get(str(operands[-2]), _Font(pdf, None))
        elif op == "BT":
            state["ly"] = state["lx"] = 0.0
        elif op in ("Td", "TD") and len(nums) >= 2:
            dx, dy = nums[-2], nums[-1]
            if op == "TD":
                state["lead"] = -dy
            state["lx"] += dx
            state["ly"] += dy
        elif op == "Tm" and len(nums) >= 6:
            state["lx"], state["ly"] = nums[-2], nums[-1]
        elif op == "TL" and nums:
            state["lead"] = nums[-1]
        elif op == "T*":
            state["ly"] -= state["lead"] or 12.0
        elif op == "Tj" and operands and isinstance(operands[-1], bytes):
            place(font.decode(operands[-1]))
        elif op in ("'", '"') and operands and isinstance(operands[-1], bytes):
            state["ly"] -= state["lead"] or 12.0
            place(font.decode(operands[-1]))
        elif op == "TJ" and operands and isinstance(operands[-1], list):
            parts = []
            for item in operands[-1]:
                if isinstance(item, bytes):
                    parts.append(font.decode(item))
                elif isinstance(item, (int, float)) and item < -200:
                    parts.append(" ")
            place("".join(parts))
        operands = []
    return "".join(out)


def pdf_text(data: bytes) -> str:
    if not data.startswith(b"%PDF"):
        raise Denied("E_VALIDATION", "not a PDF file")
    pdf = _Pdf(data)
    pages = pdf.pages()
    texts = [_page_text(pdf, p) for p in pages]
    return "\n\n".join(t.strip() for t in texts if t.strip())
