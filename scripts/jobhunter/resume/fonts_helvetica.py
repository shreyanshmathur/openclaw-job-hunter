"""Glyph widths of the base-14 fonts Helvetica and Helvetica-Bold (design 6.4).

Widths are in 1/1000 em, taken from the standard Adobe AFM files, and indexed by WinAnsiEncoding code
(0 to 255). Codes without a glyph in WinAnsi use the width of a space. `encode` maps text to WinAnsi
bytes and refuses characters that the encoding cannot show (the renderer never substitutes a glyph
silently).
"""
from __future__ import annotations

FONTS = ("Helvetica", "Helvetica-Bold")

# code -> width, for the printable ASCII range 32..126
_ASCII_REGULAR = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,   # 32-47
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,   # 48-63
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,  # 64-79
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,   # 80-95
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,   # 96-111
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,        # 112-126
]
_ASCII_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,   # 32-47
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,   # 48-63
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,   # 64-79
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,   # 80-95
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,   # 96-111
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,        # 112-126
]

# WinAnsi 128..159 (None = no glyph in WinAnsiEncoding)
_HIGH_128_REGULAR = [
    556, None, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, None, 611, None,
    None, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, None, 500, 667,
]
_HIGH_128_BOLD = [
    556, None, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333, 1000, None, 611, None,
    None, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, None, 500, 667,
]
# WinAnsi 160..255
_HIGH_160_REGULAR = [
    278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333,   # 160-175
    400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611,   # 176-191
    667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,  # 192-207
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,   # 208-223
    556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278,   # 224-239
    556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500,   # 240-255
]
_HIGH_160_BOLD = [
    278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333,   # 160-175
    400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611,   # 176-191
    722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,  # 192-207
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,   # 208-223
    556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278,   # 224-239
    611, 611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556,   # 240-255
]


def _table(ascii_w: list, w128: list, w160: list) -> list:
    space = ascii_w[0]
    out = [space] * 256
    for i, w in enumerate(ascii_w):
        out[32 + i] = w
    for i, w in enumerate(w128):
        out[128 + i] = space if w is None else w
    for i, w in enumerate(w160):
        out[160 + i] = w
    return out


WIDTHS: dict[str, list] = {
    "Helvetica": _table(_ASCII_REGULAR, _HIGH_128_REGULAR, _HIGH_160_REGULAR),
    "Helvetica-Bold": _table(_ASCII_BOLD, _HIGH_128_BOLD, _HIGH_160_BOLD),
}

# font metrics used for line placement (AFM Ascender, Descender, CapHeight)
ASCENT = {"Helvetica": 718, "Helvetica-Bold": 718}
DESCENT = {"Helvetica": -207, "Helvetica-Bold": -207}
CAP_HEIGHT = {"Helvetica": 718, "Helvetica-Bold": 718}

_UNDEFINED_128 = frozenset(128 + i for i, w in enumerate(_HIGH_128_REGULAR) if w is None)


def can_encode(text: str) -> bool:
    try:
        encode(text)
    except ValueError:
        return False
    return True


def encode(text: str) -> bytes:
    """WinAnsi (cp1252) bytes of text. Control characters and characters outside WinAnsi raise ValueError."""
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end]
        raise ValueError("character U+%04X cannot be shown with the standard PDF fonts" % ord(bad[0]))
    for b in raw:
        if b < 32 or b == 127 or b in _UNDEFINED_128:
            raise ValueError("control or undefined character 0x%02X in PDF text" % b)
    return raw


def string_width(text: str, font: str, size: float) -> float:
    """Width of text in points at the given size."""
    table = WIDTHS[font]
    total = 0
    for b in encode(text):
        total += table[b]
    return total * size / 1000.0
