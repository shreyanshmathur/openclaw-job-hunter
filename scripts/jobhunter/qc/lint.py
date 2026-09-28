"""Deterministic outbound-text linter (design 5.2; port of research qc_lint_ref.py, writing-qc 7 and 8).

    lint(draft, ctx) -> {"pass": bool, "blocks": [[rule, detail]], "warns": [[rule, detail]], "metrics": {...}}

`lint` is pure given ctx["now"] (no database; without ctx["now"] it uses the current time). `build_ctx(conn,
draft_id)` builds ctx from stored rows only, never from the agent's copy of anything.

draft keys: channel, subject, body, recipient {first_name, last_name, company, locale}, hook {anchor, source_url,
  source_type, snippet, published_at, retrieved_at, fact_id}, claims [{text, fact_id}], links, is_reply,
  field_char_limit, payload (resume and application_package channels).
ctx keys: profile_facts {id: text}, research_facts {id: text}, flagged_facts [id], other_companies [name],
  names [str] (Latin-letter name exemption), config {qc.lint keys}, now (datetime or timestamp),
  signature (text appended by code; character and placeholder rules), recent_texts [body] (U-SIMILAR, O-OPTOUT-FIXED),
  dedup [[rule, detail]] (L-DEDUP-*), and for packages answers, form_drafts, variant, attachment.

Pass condition: zero blocks and at most config max_warnings warnings.
"""
from __future__ import annotations

import json
import re
import statistics
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import BANNED_FILE
from ..identity import PLACEHOLDERS as IDENTITY_PLACEHOLDERS

# ---------------------------------------------------------------- 1. character rules (block)
DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2043\u2212\u2e3a\u2e3b\ufe31\ufe32\ufe58\ufe63\uff0d]")
SPACED_HYPHEN = re.compile(r"(?<=\S)[ \t]+-{1,3}[ \t]+(?=\S)|--")
CURLY = re.compile("[\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u2032\u2033\u00b4]")
ELLIPSIS = re.compile(r"\u2026|\.{3,}")
INVISIBLE = re.compile("[\u00a0\u00ad\u180e\u200b-\u200f\u2028\u2029\u202a-\u202f\u205f-\u2064\u3000\ufeff]")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27bf\u2b00-\u2bff\u2300-\u23ff\ufe0f\u20e3]")
BULLETS = re.compile("[\u2022\u2023\u2219\u25aa\u25ab\u25cf\u25e6\u2043]")
MARKDOWN = re.compile(r"(\*\*|__|~~|`|^\s{0,3}#{1,6}\s|^\s*[-*+]\s+\S|^\s*\d+[.)]\s+\S|^\s*>\s)", re.M)
_ALREADY = (DASHES, CURLY, INVISIBLE, EMOJI, BULLETS)
_LETTERS = re.compile(r"[^\W\d_]+")

# ---------------------------------------------------------------- 2. placeholders (block)
PLACEHOLDERS = [
    re.compile(r"\{\{?[^{}\n]{0,40}\}?\}"),
    re.compile(r"[\[\]]"),
    re.compile(r"<\s*/?\s*[A-Za-z_][\w .-]{0,30}>"),
    re.compile(r"%\(?\w*\)?[sd]\b|\$\{\w+\}|\$[A-Za-z_]\w*"),
    re.compile(r"\b(TODO|TBD|TBA|FIXME|XXX+|lorem ipsum|placeholder|insert (name|company|role|link)|"
               r"your name|company name|hiring manager name|recipient name|first ?name|job title|role name)\b", re.I),
]
EMPTY_GREETING = re.compile(r"^(hi|hello|dear|hey)\s*,", re.I | re.M)


def _identity_placeholder_rx(value: str) -> str:
    """A shipped owner placeholder (identity.PLACEHOLDERS) as a pattern: exact, any case, and for a link also
    without the scheme or www and with a trailing slash."""
    core = re.sub(r"^https?://(www\.)?", "", value.strip(), flags=re.I).rstrip("/")
    if core != value.strip():
        return r"(?<![\w.-])(?:https?://)?(?:www\.)?" + re.escape(core) + r"/?(?![\w%-])"
    return r"(?<![\w.+-])" + re.escape(core) + r"(?![\w-])"


# Placeholder links and addresses (block): the shipped owner placeholders from identity.PLACEHOLDERS (the example
# config's you@example.com and linkedin.com/in/your-handle), any 'your-handle' style slug, and a LinkedIn profile
# link with no handle. Checked in every outbound text, including the signature appended by code.
PLACEHOLDER_LINKS = [re.compile(_identity_placeholder_rx(v), re.I) for v in IDENTITY_PLACEHOLDERS if v] + [
    re.compile(r"(?<![\w-])your[-_]?(?:handle|user[-_]?name|name|profile|id|site|website|domain|portfolio|github|"
               r"blog|link|url|email|page)(?![\w-])", re.I),
    re.compile(r"linkedin\.com/in/?(?![/\w%-])", re.I),
]

# ---------------------------------------------------------------- 4. structural patterns
STRUCT_BLOCK = {
    "S-AS-A": re.compile(r"(^|[.!?]\s+|\n)\s*as an? (?!result\b)\w+", re.I),
    "S-NEG-PARALLEL": re.compile(
        r"\bnot (just|only|merely|simply)\b[^.?!]{0,80}\bbut\b|\b(isn't|is not|wasn't|it's not) (just|only|merely) |"
        r"\b(it|this|that)('s| is| was) not (about )?[^.?!]{1,60}[,;]\s*(it|this|that)('s| is| was)\b", re.I),
    "S-COLON-REVEAL": re.compile(
        r"\b(here's|here is) (the thing|why|what|how)\b|\bthe (result|answer|kicker|catch|best part)\?|"
        r"(^|\n)\s*(ever wondered|what if|imagine)\b", re.I),
    "S-ING-TAIL": re.compile(
        r",\s+(highlighting|underscoring|showcasing|emphasi[sz]ing|ensuring|reflecting|demonstrating|fostering|"
        r"enabling|contributing to|paving the way|cementing|solidifying)\b", re.I),
    "S-SUMMARY-CLOSE": re.compile(r"(^|[.!?]\s+)(overall|ultimately|in short|all in all|to sum up|in essence),", re.I),
}
TRIPLET = re.compile(r"\b[\w'-]+(?: [\w'-]+)?, [\w'-]+(?: [\w'-]+)?,? and [\w'-]+\b")
WORD = re.compile(r"[A-Za-z0-9]+(?:['.,][A-Za-z0-9]+)*")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
URL = re.compile(r"https?://\S+|\b[\w-]+(?:\.[\w-]+)+/\S*")
ACRONYMS = {"UPI", "COD", "RTO", "API", "SQL", "AWS", "GCP", "B2B", "SAAS", "CEO", "CTO", "COO", "CFO", "VP", "ML",
            "AI", "LLM", "ETL", "KPI", "OKR", "GTM", "USA", "UK", "EU", "IIT", "NIT", "IIM", "MBA", "SDE", "SRE", "HR",
            "TA", "CRM", "ERP", "IST", "PST", "EST", "GMT", "LPA", "CTC", "PDF", "FAANG", "JSON", "HTTP", "REST",
            "HTML", "NASA", "DOCX"}
SUBJECT_GENERIC = re.compile(r"\b(quick question|opportunity|following up|checking in|job application|urgent|hello|hi)\b",
                             re.I)
OPTOUT = re.compile(r"\b(won't|will not|wouldn't|not) (write|email|e-mail|message|follow up|contact|reach out)"
                    r"( to you)? (again|further)\b|\bunsubscribe\b|\bopt out\b|\bno more (emails|messages|notes)\b|"
                    r"\b(rather|prefer) (i|that i|me) (not|stop)\b", re.I)

CHANNELS = {  # size unit, target lo..hi, hard max; questions (min, max); links max; subject required
    "email_cold":        dict(unit="words", lo=50, hi=120, hard=150, q=(1, 2), links=2, subject=True),
    "email_founder":     dict(unit="words", lo=40, hi=110, hard=140, q=(1, 2), links=2, subject=True),
    "email_recruiter":   dict(unit="words", lo=50, hi=120, hard=150, q=(1, 2), links=2, subject=True),
    "email_followup":    dict(unit="words", lo=15, hi=80, hard=100, q=(0, 1), links=1, subject=False),
    "email_application": dict(unit="words", lo=80, hi=200, hard=250, q=(0, 1), links=3, subject=True),
    "cover_note":        dict(unit="words", lo=80, hi=200, hard=250, q=(0, 1), links=3, subject=False),
    "li_connect":        dict(unit="chars", lo=90, hi=180, hard=200, q=(0, 1), links=0, subject=False),
    "li_message":        dict(unit="chars", lo=150, hi=500, hard=700, q=(1, 2), links=1, subject=False),
    "li_followup":       dict(unit="chars", lo=60, hi=350, hard=450, q=(0, 1), links=1, subject=False),
    "inmail":            dict(unit="chars", lo=200, hi=400, hard=800, q=(1, 2), links=1, subject=True),
    "form_answer":       dict(unit="words", lo=25, hi=150, hard=250, q=(0, 0), links=1, subject=False),
}
STRUCTURED_CHANNELS = ("resume", "application_package")
ALL_CHANNELS = tuple(CHANNELS) + STRUCTURED_CHANNELS
FOLLOWUP_CHANNELS = ("email_followup", "li_followup")
HOOK_TYPES = {"linkedin_post", "linkedin_article", "linkedin_profile", "job_post", "company_blog", "engineering_blog",
              "company_site", "news", "press_release", "github", "talk_or_podcast", "shared_context"}
INJECTION = re.compile(r"ignore (all |any )?(previous|prior|above) (instructions|prompts)|system prompt|"
                       r"you are (chatgpt|an ai|a language model)|as an ai|disregard (the|your) (rules|instructions)|"
                       r"approve this (message|draft)", re.I)
EEO_LABEL = re.compile(r"gender|ethnicity|race|veteran|disability|sexual orientation|pronoun", re.I)

DEFAULT_CFG = {"max_soft_hits": 2, "max_warnings": 2, "hook_max_age_days": 180, "hook_warn_age_days": 90,
               "retrieved_max_age_days": 14, "number_whitelist": ["10", "15", "20", "30"], "li_connect_hard": 200,
               "allowed_link_hosts": [], "similar_threshold": 0.6}

_banned_cache: dict | None = None


def banned_table() -> dict:
    """{category: (severity 'B'|'W', [compiled regex])} from qc/banned_phrases.json (cached)."""
    global _banned_cache
    if _banned_cache is None:
        with open(BANNED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        table = {}
        for cat, spec in data["categories"].items():
            sev = "B" if spec["severity"] == "block" else "W"
            table[cat] = (sev, [re.compile(r"(?<![\w'])" + p + r"(?![\w'])") for p in spec["patterns"]])
        _banned_cache = table
    return _banned_cache


# ---------------------------------------------------------------- helpers
def sanitize(text: str | None) -> str:
    """Safe auto-fixes only: curly quotes to ASCII, NBSP to space, trailing and double spaces, extra blank
    lines. Dashes are never auto-fixed: they force a rewrite."""
    t = unicodedata.normalize("NFC", text or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub("[\u2018\u2019\u201a\u201b\u2032\u00b4]", "'", t)
    t = re.sub("[\u201c\u201d\u201e\u201f\u2033]", '"', t)
    t = t.replace("\u00a0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _sentences(body: str):
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if lines and re.match(r"^(hi|hello|dear|hey)\b", lines[0], re.I) and len(lines[0]) < 40:
        lines = lines[1:]
    if lines and re.match(r"^(thanks|thank you|best|regards|best regards|warm regards|cheers|sincerely)[,.]?$",
                          lines[-1].strip(), re.I):
        lines = lines[:-1]
    text = " ".join(lines)
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text) if s.strip()], text


def _nums(s: str | None) -> set:
    return {n.replace(",", "") for n in NUMBER.findall(s or "")}


def _parse_when(v):
    """datetime (UTC) from a datetime, 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SSZ'; ValueError otherwise."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str):
        raise ValueError("not a date")
    s = v.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _name_tokens(names) -> set:
    out = set()
    for n in names or []:
        for tok in _LETTERS.findall(unicodedata.normalize("NFC", str(n or ""))):
            out.add(tok.casefold())
    return out


def _latin_letter_token(tok: str) -> bool:
    for ch in tok:
        if unicodedata.category(ch) not in ("Lu", "Ll", "Lt"):
            return False
        if ord(ch) > 127 and not unicodedata.name(ch, "").startswith("LATIN "):
            return False
    return True


def char_findings(text: str, names=None, where: str = "", markdown: bool = True) -> list:
    """Character rules (all block) on one text: C-DASH, C-SPACED-HYPHEN, C-CURLY, C-ELLIPSIS, C-INVISIBLE,
    C-EMOJI, C-BULLET, C-MARKDOWN (optional), C-NON-ASCII with the Latin-letter name exemption (minor 11)."""
    out = []
    suffix = (" " + where) if where else ""
    rules = [("C-DASH", DASHES), ("C-SPACED-HYPHEN", SPACED_HYPHEN), ("C-CURLY", CURLY), ("C-ELLIPSIS", ELLIPSIS),
             ("C-INVISIBLE", INVISIBLE), ("C-EMOJI", EMOJI), ("C-BULLET", BULLETS)]
    if markdown:
        rules.append(("C-MARKDOWN", MARKDOWN))
    for rid, rx in rules:
        for m in rx.finditer(text):
            out.append([rid, "U+%04X at %d%s" % (ord(m.group(0)[0]), m.start(), suffix) if ord(m.group(0)[0]) > 127
                        else "%r at %d%s" % (m.group(0), m.start(), suffix)])
    allowed = _name_tokens(names)
    exempt = set()
    for m in _LETTERS.finditer(text):
        tok = m.group(0)
        if any(ord(c) > 127 for c in tok) and _latin_letter_token(tok) and tok.casefold() in allowed:
            exempt.update(range(m.start(), m.end()))
    odd = {}
    for i, c in enumerate(text):
        if ord(c) > 127 and i not in exempt and not any(x.match(c) for x in _ALREADY):
            odd.setdefault(c, i)
    for c in sorted(odd):
        out.append(["C-NON-ASCII", "U+%04X %s%s" % (ord(c), unicodedata.name(c, "?"), suffix)])
    return out


def placeholder_findings(text: str, where: str = "") -> list:
    """P-PLACEHOLDER findings on one text: template tokens, an empty greeting, and placeholder links or
    addresses (PLACEHOLDER_LINKS; a match already reported by an earlier pattern is not repeated)."""
    out = []
    suffix = (" " + where) if where else ""
    for rx in PLACEHOLDERS:
        for m in rx.finditer(text):
            out.append(["P-PLACEHOLDER", repr(m.group(0)) + suffix])
    for m in EMPTY_GREETING.finditer(text):
        out.append(["P-PLACEHOLDER", repr(m.group(0)) + suffix])
    seen = []
    for rx in PLACEHOLDER_LINKS:
        for m in rx.finditer(text):
            if any(a <= m.start() and m.end() <= b for a, b in seen):
                continue
            seen.append((m.start(), m.end()))
            out.append(["P-PLACEHOLDER", "placeholder link %r%s" % (m.group(0), suffix)])
    return out


def trigrams(s: str) -> set:
    t = " ".join((s or "").lower().split())
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))}


def jaccard(a: set, b: set) -> float:
    u = a | b
    return (len(a & b) / len(u)) if u else 0.0


def opening(body: str, n: int = 60) -> str:
    """First n characters after the greeting line (the part a template would reuse)."""
    lines = [ln for ln in (body or "").splitlines() if ln.strip()]
    if lines and re.match(r"^(hi|hello|dear|hey)\b", lines[0], re.I) and len(lines[0]) < 40:
        lines = lines[1:]
    return " ".join(" ".join(lines).split())[:n]


def _optout_sentences(body: str) -> set:
    sents, _ = _sentences(body or "")
    return {" ".join(s.lower().split()) for s in sents if OPTOUT.search(s)}


def _result(blocks, warns, cfg, metrics) -> dict:
    ok = not blocks and len(warns) <= cfg["max_warnings"]
    return {"pass": ok, "blocks": blocks, "warns": warns, "metrics": metrics}


def _cfg(ctx: dict) -> dict:
    cfg = dict(DEFAULT_CFG)
    cfg.update((ctx or {}).get("config") or {})
    return cfg


def _now(ctx: dict) -> datetime:
    """ctx["now"] (build_ctx always sets it, so stored drafts lint the same way every time); the current
    time only when a caller passes no ctx (the reference linter's behaviour, used by selftest)."""
    v = (ctx or {}).get("now")
    if v is None:
        from ..canon import utcnow
        return utcnow()
    return _parse_when(v)


# ---------------------------------------------------------------- lint
def lint(draft: dict, ctx: dict) -> dict:
    """Lint one draft against ctx. Pure: same inputs, same result."""
    ctx = ctx or {}
    cfg = _cfg(ctx)
    ch = draft.get("channel")
    if ch == "resume":
        return _lint_resume(draft, ctx, cfg)
    if ch == "application_package":
        return _lint_package(draft, ctx, cfg)
    blocks: list = []
    warns: list = []

    def B(r, d=""):
        blocks.append([r, d])

    def W(r, d=""):
        warns.append([r, d])

    if ch not in CHANNELS:
        B("L-CHANNEL-UNKNOWN", str(ch))
        return _result(blocks, warns, cfg, {})
    spec = dict(CHANNELS[ch])
    if ch == "li_connect":
        spec["hard"] = min(int(cfg["li_connect_hard"]), 300)
    subject = draft.get("subject") or ""
    body = draft.get("body") or ""
    full = subject + "\n" + body

    # 1. characters (subject + body), and the code-appended signature (character and placeholder rules)
    blocks.extend(char_findings(full, ctx.get("names")))
    if ctx.get("signature"):
        blocks.extend(char_findings(ctx["signature"], ctx.get("names"), where="in signature", markdown=False))
        blocks.extend(placeholder_findings(ctx["signature"], where="in signature"))
    if full.count("!") > 1 or "!!" in full or "!" in subject or (ch == "li_connect" and "!" in body):
        B("C-EXCLAMATION", str(full.count("!")))
    for w in re.findall(r"\b[A-Z]{4,}\b", full):
        if w not in ACRONYMS:
            W("C-ALLCAPS", w)

    # 2. placeholders
    blocks.extend(placeholder_findings(full))

    # 3. banned phrases
    low = sanitize(full).lower()
    soft_hits = []
    for cat, (sev, pats) in banned_table().items():
        for rx in pats:
            for m in rx.finditer(low):
                if sev == "B":
                    B("B-" + cat.upper(), m.group(0))
                else:
                    soft_hits.append(m.group(0))
    if soft_hits:
        (B if len(soft_hits) > int(cfg["max_soft_hits"]) else W)("W-SOFT", ", ".join(soft_hits))

    # 4. structure
    sents, flat = _sentences(body)
    for rid, rx in STRUCT_BLOCK.items():
        for m in rx.finditer(flat):
            B(rid, m.group(0).strip())
    triplets = TRIPLET.findall(flat)
    if triplets:
        (B if len(triplets) > 1 else W)("S-TRIPLET", "; ".join(triplets))
    lens = [len(WORD.findall(s)) for s in sents]
    if len(lens) >= 5 and statistics.pstdev(lens) < 3.0:
        W("S-UNIFORM-RHYTHM", "sentence lengths " + str(lens))
    if lens and max(lens) > 35:
        B("S-LONG-SENTENCE", str(max(lens)))
    elif lens and max(lens) > 25:
        W("S-LONG-SENTENCE", str(max(lens)))
    i_starts = sum(1 for s in sents if re.match(r"^(i|i'm|i've|i'd|my)\b", s.strip(), re.I))
    if sents and i_starts / len(sents) > 0.5:
        W("S-I-HEAVY", "%d of %d sentences start with I/My" % (i_starts, len(sents)))
    if sents and ch.startswith("email") and ch != "email_followup" and re.match(r"^(i|i'm|i've|my)\b", sents[0], re.I):
        W("S-SELF-FIRST", "first sentence is about the sender, not the reader")
    if flat.count(";") > 1:
        W("S-SEMICOLONS", str(flat.count(";")))
    if len(re.findall(r"(?<!\d):(?!\d)", URL.sub(" ", flat))) > 1:
        W("S-COLONS", "more than one colon")
    if flat.count("(") > 1:
        W("S-PARENS", "more than one parenthetical")
    if ch != "form_answer":
        for para in [p for p in re.split(r"\n\s*\n", body) if p.strip()]:
            if len(WORD.findall(para)) > 60:
                W("S-LONG-PARAGRAPH", str(len(WORD.findall(para))) + " words")

    # 5. length, questions, links, subject
    n_words = len(WORD.findall(flat))
    n_chars = len(sanitize(body))
    size = n_words if spec["unit"] == "words" else n_chars
    limit = draft.get("field_char_limit")
    if limit:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0 and n_chars > int(limit * 0.95):
            B("L-FIELD-LIMIT", "%d > 95%% of %d" % (n_chars, limit))
    if size > spec["hard"]:
        B("L-TOO-LONG", "%d %s > %d" % (size, spec["unit"], spec["hard"]))
    elif size > spec["hi"] or size < spec["lo"]:
        W("L-OFF-TARGET", "%d %s, target %d to %d" % (size, spec["unit"], spec["lo"], spec["hi"]))
    q = flat.count("?")
    if not (spec["q"][0] <= q <= spec["q"][1]):
        B("L-QUESTIONS", "%d questions, allowed %d to %d" % (q, spec["q"][0], spec["q"][1]))
    links = URL.findall(body)
    if len(links) > spec["links"]:
        B("L-LINKS", "%d links, max %d" % (len(links), spec["links"]))
    allowed_hosts = [h.lower().lstrip(".") for h in (cfg.get("allowed_link_hosts") or []) if h]
    for u in links:
        host = urlparse(u if u.startswith("http") else "https://" + u).netloc.lower()
        if re.search(r"(bit\.ly|tinyurl\.com|tinyurl|t\.co|goo\.gl|rebrand\.ly|lnkd\.in)$", host) or "utm_" in u:
            B("L-LINK-SHORTENER-OR-TRACKING", u)
        elif allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
            B("L-LINK-NOT-ALLOWED", u)
    if spec["subject"]:
        sw = len(WORD.findall(subject))
        if not subject.strip():
            B("L-SUBJECT-MISSING")
        elif sw < 2 or sw > 6 or len(subject) > 50:
            B("L-SUBJECT-LENGTH", "%d words, %d chars" % (sw, len(subject)))
        if re.match(r"^\s*(re|fwd?|fw)\s*:", subject, re.I) and not draft.get("is_reply"):
            B("L-SUBJECT-FAKE-REPLY", subject)
        if SUBJECT_GENERIC.search(subject):
            B("L-SUBJECT-GENERIC", subject)

    # 6. identity: greeting name, company, cross-contamination
    r = draft.get("recipient") or {}
    fn = (r.get("first_name") or "").strip()
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    names = {fn.lower(), (r.get("last_name") or "").strip().lower()} - {""}
    comp = (r.get("company") or "").strip()
    if ch.startswith("email") and fn:
        g = re.match(r"^(hi|hello|dear)\s+(?:(?:mr|ms|mrs|dr)\.?\s+)?([A-Za-z][\w.'-]*)", first_line, re.I)
        if not g or g.group(2).lower().rstrip(",") not in names:
            B("I-GREETING-NAME", "greeting %r does not match recipient %r" % (first_line[:30], fn))
    elif ch.startswith(("li_", "inmail")) and fn and fn.lower() not in body[:60].lower():
        B("I-GREETING-NAME", "recipient first name not in the first 60 characters")
    g2 = re.match(r"^(?i:hi|hello|dear|hey)\s+(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?([A-Z][\w'-]*)", first_line)
    if g2 and names and g2.group(1).lower() not in names and g2.group(1).lower() != "team" \
            and g2.group(1).lower() not in comp.lower():
        B("I-WRONG-NAME", g2.group(1))
    short = (r.get("company_short") or "").strip()
    if comp and ch not in FOLLOWUP_CHANNELS and comp.lower() not in full.lower() \
            and not (short and short.lower() in full.lower()):
        W("I-COMPANY-MISSING", comp)
    own = " ".join(ctx.get("profile_facts", {}).values()).lower()
    for other in ctx.get("other_companies", []) or []:
        if other and other.lower() != comp.lower() and not (comp and other.lower() in comp.lower()) \
                and re.search(r"\b" + re.escape(other.lower()) + r"\b", low) and other.lower() not in own:
            B("I-WRONG-COMPANY", other)

    # 7. hook evidence (every first-touch channel)
    first_touch = ch not in FOLLOWUP_CHANNELS
    hook = draft.get("hook") or {}
    now = _now(ctx)
    if first_touch:
        for f in ("anchor", "source_url", "source_type", "snippet", "retrieved_at", "fact_id"):
            if not hook.get(f):
                B("H-MISSING-" + f.upper())
        if hook:
            if hook.get("source_type") and hook["source_type"] not in HOOK_TYPES:
                B("H-BAD-TYPE", hook["source_type"])
            su = hook.get("source_url") or ""
            if su and (not su.startswith("https://") or re.search(r"(google|bing|duckduckgo)\.\w+/|/search\b", su)):
                B("H-BAD-URL", su)
            if hook.get("source_type") == "linkedin_post" and su and \
                    not re.search(r"linkedin\.com/(posts|feed/update|pulse)/", su):
                B("H-URL-TYPE-MISMATCH", su)
            anc = (hook.get("anchor") or "").lower()
            snip = (hook.get("snippet") or "").lower()
            if anc:
                if anc not in snip:
                    B("H-ANCHOR-NOT-IN-SOURCE", anc)
                if anc not in low:
                    B("H-ANCHOR-NOT-IN-DRAFT", anc)
                if len(anc.split()) > 8:
                    B("H-ANCHOR-TOO-LONG", "copying the source; paraphrase and keep the anchor to 8 words or fewer")
                if len(anc) < 6:
                    B("H-ANCHOR-TOO-SHORT", anc)
                elif anc in low and sents:
                    lead = " ".join(sents[:2]).lower()
                    if anc not in lead and ch not in ("form_answer", "email_application", "cover_note"):
                        W("H-ANCHOR-LATE", "hook should land in the first 2 sentences")
            if INJECTION.search(hook.get("snippet") or "") or \
                    (hook.get("fact_id") and hook["fact_id"] in (ctx.get("flagged_facts") or [])):
                B("H-INJECTION-IN-SOURCE", "source text contains instruction-like content; pick another hook")
            if hook.get("fact_id") and hook["fact_id"] not in (ctx.get("research_facts") or {}):
                B("H-FACT-ID-UNKNOWN", hook["fact_id"])
            for key, lim_b, lim_w in (("published_at", cfg["hook_max_age_days"], cfg["hook_warn_age_days"]),
                                      ("retrieved_at", cfg["retrieved_max_age_days"], None)):
                if hook.get(key):
                    try:
                        age = (now - _parse_when(hook[key])).days
                    except ValueError:
                        B("H-BAD-DATE", key)
                        continue
                    if age > lim_b:
                        B("H-STALE-" + key.upper(), "%d days" % age)
                    elif lim_w and age > lim_w:
                        W("H-AGING-" + key.upper(), "%d days" % age)

    # 8. fact traceability
    known = set(str(x) for x in cfg["number_whitelist"])
    for t in list((ctx.get("profile_facts") or {}).values()) + list((ctx.get("research_facts") or {}).values()) \
            + [hook.get("snippet") or ""]:
        known |= _nums(t)
    stray = sorted(_nums(URL.sub(" ", body)) - known)
    if stray:
        B("F-UNTRACED-NUMBER", ", ".join(stray))
    pf = ctx.get("profile_facts") or {}
    for c in draft.get("claims") or []:
        fid = c.get("fact_id") if isinstance(c, dict) else None
        if fid not in pf:
            B("F-CLAIM-NO-FACT", str(fid))
        elif not _nums(c.get("text", "")) <= _nums(pf[fid]):
            B("F-CLAIM-NUMBER-MISMATCH", fid)
    if first_touch and ch != "li_connect" and not draft.get("claims"):
        W("F-NO-CLAIMS", "no candidate proof point declared")

    # 9. repetition across sends (U-SIMILAR, O-OPTOUT-FIXED) and dedup results from the state store
    thr = float(cfg.get("similar_threshold", 0.6))
    my_body, my_open = trigrams(body), trigrams(opening(body))
    my_optouts = _optout_sentences(body)
    for prev in ctx.get("recent_texts") or []:
        if not prev:
            continue
        sim_body = jaccard(my_body, trigrams(prev))
        prev_open = opening(prev)
        sim_open = jaccard(my_open, trigrams(prev_open)) if len(prev_open) >= 20 and len(opening(body)) >= 20 else 0.0
        if sim_body > thr or sim_open > thr:
            B("U-SIMILAR", "trigram similarity %.2f (opening %.2f) with a text sent in the last 30 days"
              % (sim_body, sim_open))
            break
    if my_optouts:
        for prev in ctx.get("recent_texts") or []:
            same = my_optouts & _optout_sentences(prev)
            if same:
                B("O-OPTOUT-FIXED", "the opt-out sentence %r was already used in the last 30 days" % sorted(same)[0])
                break
    for item in ctx.get("dedup") or []:
        rule, detail = (item[0], item[1] if len(item) > 1 else "") if isinstance(item, (list, tuple)) else (item, "")
        B(rule, detail)

    return _result(blocks, warns, cfg, {
        "words": n_words, "chars": n_chars, "sentences": len(sents), "questions": q,
        "sentence_lengths": lens, "soft_hits": len(soft_hits)})


# ---------------------------------------------------------------- resume channel (R-*)
def _norm_skill(s) -> str:
    return " ".join(str(s or "").lower().split())


def _roles(model: dict) -> dict:
    return {str(r.get("id")): r for r in (model or {}).get("roles") or [] if isinstance(r, dict)}


def _base_bullets(model: dict) -> dict:
    out = {}
    for r in (model or {}).get("roles") or []:
        for b in r.get("bullets") or []:
            if isinstance(b, dict) and b.get("id"):
                out[str(b["id"])] = str(b.get("text") or "")
    for p in (model or {}).get("projects") or []:
        for b in (p.get("bullets") or []) if isinstance(p, dict) else []:
            if isinstance(b, dict) and b.get("id"):
                out[str(b["id"])] = str(b.get("text") or "")
    return out


def _resume_names(ctx: dict, p: dict) -> list:
    """Names for the Latin-letter exemption of a resume: ctx names plus the resume's own names (payload.names
    from U4, and the contact block of the base model), so the candidate's name as spelled in base.json is
    allowed even when the config owner names are blank or spelled differently."""
    names = [str(n) for n in (ctx.get("names") or []) if n]
    names += [str(n) for n in (p.get("names") or []) if isinstance(n, str) and n]
    c = (p.get("base") or {}).get("contact") if isinstance(p.get("base"), dict) else None
    if isinstance(c, dict):
        names += [str(c[k]) for k in ("full_name", "first_name", "last_name") if isinstance(c.get(k), str) and c[k]]
    return names


def _lint_resume(draft: dict, ctx: dict, cfg: dict) -> dict:
    """R-* rules (design 5.2, 6.4). payload: {base, tailored, page_count, max_pages, allowed_skills}, where a
    model is {contact, summary {text, fact_ids}, roles [{id, employer, title, dates {start, end}, bullets [{id|from,
    text}]}], projects, skills [str], education [{id, ...}]}. body is the rendered plain text of the resume."""
    blocks, warns = [], []
    body = draft.get("body") or ""
    p = draft.get("payload") or {}
    base, tail = p.get("base") or {}, p.get("tailored") or {}
    for f in char_findings(body, _resume_names(ctx, p), markdown=False):
        blocks.append(["R-" + f[0], f[1]])
    for f in placeholder_findings(body):
        blocks.append(["R-" + f[0], f[1]])
    if not base or not tail:
        blocks.append(["R-MODEL-MISSING", "payload needs base and tailored models"])
        return _result(blocks, warns, cfg, {"chars": len(body)})
    bb = _base_bullets(base)
    broles, troles = _roles(base), _roles(tail)
    for rid, tr in troles.items():
        br = broles.get(rid)
        if br is None:
            blocks.append(["R-ROLE-UNKNOWN", rid])
            continue
        for key, rule in (("employer", "R-EMPLOYER-CHANGED"), ("title", "R-TITLE-CHANGED")):
            if (tr.get(key) or "") != (br.get(key) or ""):
                blocks.append([rule, "%s: %r != %r" % (rid, tr.get(key), br.get(key))])
        if (tr.get("dates") or {}) != (br.get("dates") or {}):
            blocks.append(["R-DATES-CHANGED", "%s: %s != %s" % (rid, tr.get("dates"), br.get("dates"))])
        if not (tr.get("bullets") or []):
            blocks.append(["R-ROLE-EMPTY", rid])
    for tr in list(troles.values()) + [x for x in (tail.get("projects") or []) if isinstance(x, dict)]:
        for b in tr.get("bullets") or []:
            if not isinstance(b, dict):
                continue
            src = str(b.get("from") or b.get("id") or "")
            if src not in bb:
                blocks.append(["R-BULLET-UNMAPPED", src or "(no base id)"])
                continue
            text = b.get("text")
            if text is None:
                continue
            extra = _nums(text) - _nums(bb[src])
            if extra:
                blocks.append(["R-NUMBER-NOT-IN-BASE", "%s: %s" % (src, ", ".join(sorted(extra)))])
    allowed = {_norm_skill(s) for s in (base.get("skills") or [])} | \
              {_norm_skill(s) for s in (p.get("allowed_skills") or [])}
    for s in tail.get("skills") or []:
        if _norm_skill(s) not in allowed:
            blocks.append(["R-NEW-SKILL", str(s)])
    if (tail.get("contact") or {}) != (base.get("contact") or {}):
        blocks.append(["R-CONTACT-CHANGED", "contact block differs from the base"])
    bedu = {str(e.get("id")): e for e in base.get("education") or [] if isinstance(e, dict)}
    for e in tail.get("education") or []:
        if not isinstance(e, dict) or bedu.get(str(e.get("id"))) != e:
            blocks.append(["R-EDUCATION-CHANGED", str(e.get("id") if isinstance(e, dict) else e)])
    summ = tail.get("summary") or {}
    if isinstance(summ, dict) and summ.get("text"):
        pf = ctx.get("profile_facts") or {}
        ids = [i for i in summ.get("fact_ids") or []]
        missing = [i for i in ids if i not in pf]
        if missing or not ids:
            blocks.append(["R-SUMMARY-NO-FACT", ", ".join(missing) or "no fact_ids"])
        else:
            extra = _nums(summ["text"]) - set().union(*[_nums(pf[i]) for i in ids])
            if extra:
                blocks.append(["R-SUMMARY-NUMBER", ", ".join(sorted(extra))])
    try:
        pages, max_pages = int(p.get("page_count") or 0), int(p.get("max_pages") or 2)
    except (TypeError, ValueError):
        pages, max_pages = 0, 2
    if pages < 1:
        blocks.append(["R-PAGES", "page count unknown"])
    elif pages > max_pages:
        blocks.append(["R-PAGES", "%d pages > %d" % (pages, max_pages)])
    return _result(blocks, warns, cfg, {"chars": len(body), "pages": pages})


# ---------------------------------------------------------------- application package (A-*)
def _nv(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ", ".join(_nv(x) for x in v)
    return " ".join(str(v if v is not None else "").lower().split())


def _lint_package(draft: dict, ctx: dict, cfg: dict) -> dict:
    """A-* rules (design 5.2, 6.5). payload is the 12.4 package; ctx supplies answers {key: {value, source,
    sensitive}}, form_drafts {uid: {kind, status, body}}, variant {uid, filename, sha256, qc_ok}, attachment."""
    blocks, warns = [], []
    p = draft.get("payload") or {}
    answers = ctx.get("answers") or {}
    forms = ctx.get("form_drafts") or {}
    usable_sources = ("resume", "profile", "user_confirmed")
    passed = ("qc_passed", "approved")
    fields = p.get("fields")
    if not isinstance(fields, list) or not fields:
        blocks.append(["A-NO-FIELDS", "package has no fields"])
        fields = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            blocks.append(["A-FIELD-SHAPE", str(i)])
            continue
        label = str(f.get("label") or "")
        value = f.get("value")
        vtext = _nv(value)
        where = "field %r" % label
        if isinstance(value, str):
            for g in char_findings(value, ctx.get("names"), where=where, markdown=False):
                blocks.append(g)
            for g in placeholder_findings(value):
                blocks.append(g)
        choices = f.get("choices")
        if choices and vtext and vtext not in {_nv(c) for c in choices}:
            blocks.append(["A-CHOICE", "%s: value is not one of the offered options" % where])
        key, fuid = f.get("answer_key"), f.get("form_answer_draft_uid")
        if EEO_LABEL.search(label) and vtext and key != "eeo_choice":
            blocks.append(["A-EEO", "%s: EEO fields only take the stored eeo_choice answer" % where])
        if key:
            a = answers.get(key)
            if a is None:
                blocks.append(["A-ANSWER-UNKNOWN", "%s: %s" % (where, key)])
                continue
            if a.get("source") not in usable_sources or not str(a.get("value") or "").strip():
                blocks.append(["A-ANSWER-UNCONFIRMED", "%s: %s" % (where, key)])
            if a.get("sensitive") and a.get("source") != "user_confirmed":
                blocks.append(["A-SENSITIVE", "%s: %s" % (where, key)])
            if _nv(a.get("value")) != vtext:
                blocks.append(["A-ANSWER-MISMATCH", "%s differs from the answer bank value of %s" % (where, key)])
        elif fuid:
            fd = forms.get(fuid)
            if fd is None or fd.get("kind") != "form_answer":
                blocks.append(["A-FREE-TEXT-NOT-QC", "%s: %s is not a form_answer draft" % (where, fuid)])
            elif fd.get("status") not in passed:
                blocks.append(["A-FREE-TEXT-NOT-QC", "%s: %s has status %s" % (where, fuid, fd.get("status"))])
            elif _nv(fd.get("body")) != vtext:
                blocks.append(["A-FREE-TEXT-MISMATCH", "%s differs from draft %s" % (where, fuid)])
        elif vtext:
            blocks.append(["A-FIELD-NO-SOURCE", "%s has a value but no answer_key or form_answer_draft_uid" % where])
    cover = p.get("cover_note_draft_uid")
    if cover:
        fd = forms.get(cover)
        if fd is None or fd.get("kind") != "cover_note" or fd.get("status") not in passed:
            blocks.append(["A-COVER-NOT-QC", str(cover)])
    v = ctx.get("variant")
    if not v:
        blocks.append(["A-ATTACHMENT", "resume variant %s not found" % p.get("resume_variant_uid")])
    else:
        if not v.get("qc_ok"):
            blocks.append(["A-ATTACHMENT", "resume variant %s has not passed QC" % v.get("uid")])
        att = ctx.get("attachment") or {}
        if att.get("filename") != v.get("filename") or att.get("sha256") != v.get("sha256"):
            blocks.append(["A-ATTACHMENT", "attachment filename or sha256 differs from the approved variant"])
    for item in ctx.get("dedup") or []:
        rule, detail = (item[0], item[1] if len(item) > 1 else "") if isinstance(item, (list, tuple)) else (item, "")
        blocks.append([rule, detail])
    return _result(blocks, warns, cfg, {"fields": len(fields)})


# ---------------------------------------------------------------- human-readable findings (5.4)
_EXPLAIN = {
    "C-DASH": "has a dash character (en dash, em dash or similar); replace it with a comma or a new sentence",
    "C-SPACED-HYPHEN": "uses a hyphen with spaces around it as a dash; replace it with a comma or a new sentence",
    "C-CURLY": "has curly quotes; use straight quotes",
    "C-ELLIPSIS": "has an ellipsis; end the sentence instead",
    "C-EMOJI": "has an emoji or symbol; remove it",
    "C-BULLET": "has a bullet character; write sentences instead",
    "C-MARKDOWN": "has markdown formatting; use plain text",
    "C-NON-ASCII": "has a character outside plain ASCII",
    "C-INVISIBLE": "has an invisible character; retype that part",
    "C-EXCLAMATION": "has too many exclamation marks",
    "P-PLACEHOLDER": "still has a placeholder or template text (or a placeholder link such as your-handle)",
    "L-TOO-LONG": "is too long for this channel",
    "L-QUESTIONS": "has the wrong number of questions for this channel",
    "L-LINKS": "has too many links",
}


def _line_of(text: str, detail: str) -> int | None:
    m = re.search(r" at (\d+)", detail or "")
    if not m:
        return None
    return text[:int(m.group(1))].count("\n") + 1


def explain(findings: list, text: str) -> list:
    """Plain sentences for the person (a human edit failed lint): 'Your edit has a dash character ... in line 2'."""
    out = []
    for rule, detail in findings:
        base = _EXPLAIN.get(rule)
        if base is None and rule.startswith("B-"):
            base = "uses the phrase %r, which reads as generic or AI-written; say it plainly" % detail
        if base is None:
            base = "fails rule %s (%s)" % (rule, detail)
        line = _line_of(text, detail) if rule.startswith("C-") else None
        out.append("Your edit " + base + ((" (line %d)" % line) if line else "") + ".")
    return out


# ---------------------------------------------------------------- context from stored rows
def build_ctx(conn, draft_id: int) -> dict:
    """ctx for lint() built by code from stored rows (design 5.2): profile facts, research facts linked to the
    draft's contact, company and job (stored snippets), other companies of drafts from the last 24 hours, names,
    the owner's own link hosts, texts sent in the last 30 days, dedup findings, and package inputs."""
    from .. import drafts as _drafts   # local import: drafts imports this module
    return _drafts.lint_context(conn, draft_id)
