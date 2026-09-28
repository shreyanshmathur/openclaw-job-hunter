#!/usr/bin/env python3
"""Personal data and secret scan (design 10.7), run by CI and the pre-commit hook.

    python3 tools/leakcheck.py              # every tracked and untracked-but-not-ignored text file
    python3 tools/leakcheck.py FILE ...     # only these files
    python3 tools/leakcheck.py --staged     # the staged snapshot (the git index), as the pre-commit hook runs it

Built-in rules:
  L-EMAIL     email addresses, except example.com, example.org, example.net, the reserved .example, .invalid,
              .test and .localhost names, and noreply addresses of the code hosts
  L-PHONE     phone numbers (+ and 10 to 14 digits, alone or in groups such as +91 00000 00000 or +91-0000000000,
              or grouped 3-3-4), except all-zero and 555-01xx placeholders
  L-APPSCRIPT Apps Script URLs with a deployment or project id (/macros/s/<id>/exec or /dev, the Workspace form
              /a/macros/<domain>/s/<id>, /d/<id>/edit)
  L-GDOC      Google Sheet, Doc and Drive ids in URLs (/d/<id>, /u/0/d/<id>, /folders/<id>, ?id=<id>)
  L-LINKEDIN  linkedin.com/in/<handle>, except handles starting with example or your-
  L-HEX64     64-hex strings (tokens, keys), except on lines that name a sha256
  L-SECRET    sk-ant-, AKIA, ghp_, xox tokens
  L-APPPW     16-letter Google app passwords on a line that talks about a password
  L-PROVKEY   an email finder provider's key: a header or parameter name (X-KEY, X-API-KEY, X-Tomba-Key,
              X-Tomba-Secret, apiKey, api_key, Authorization: Bearer) followed by a value of 16 or more
              characters that does not start with FAKE- (test fixtures use FAKE-KEY-0000000000000000 style keys)
Private denylist (never committed; only counts and positions are printed, never the term):
  L-PRIVATE   terms built at check time from private/ (config owner fields, profile.json, answers.json and the
              resume JSON: names, employers, schools, email addresses and their local parts, phone numbers in
              international and national form, and handles and personal domains of profile links such as
              github.com/<handle>) and from the optional file named by JH_LEAKCHECK_DENYLIST (one term per line,
              # comments), which lets a maintainer scan for their own data from a file kept outside the repo.
              A term of several words also matches with hyphens, underscores, dots or nothing between the words
              ("acme corp" finds Acme-Corp, acme_corp and AcmeCorp). Denylist lines that are too short to use are
              reported by line number on stderr.
Text files in UTF-16 or UTF-32 are decoded and scanned too. With --staged the files are read from the index, so
a file that was staged and then edited or deleted is scanned as it will be committed.
A line containing "leakcheck: ignore" is skipped. Exit 0 when clean, 1 with findings.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textcheck import REPO, decode_text, rel_paths, repo_files, split_args, staged_files  # noqa: E402

ENV_DENYLIST = "JH_LEAKCHECK_DENYLIST"
IGNORE_MARK = "leakcheck: ignore"
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
ALLOWED_EMAIL_TLDS = (".example", ".invalid", ".test", ".localhost")
ALLOWED_EMAILS = {"noreply@github.com", "noreply@anthropic.com", "actions@github.com"}
PHONE_RES = (re.compile(r"(?<![\w+])\+\d{10,14}(?!\d)"),
             re.compile(r"(?<![\w+])\+\d{1,3}[ .-]\(?\d{2,5}\)?[ .-]\d{3,5}[ .-]?\d{3,5}(?!\d)"),
             re.compile(r"(?<![\w.])\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])"))
# country code, then the national number in one or two groups: +91 00000 00000, +91-0000000000, +44 0000 000000
PHONE_GROUPED_RES = (re.compile(r"(?<![\w+])\+\d{1,3}[ .-]\d{3,5}[ .-]\d{4,6}(?![\d])"),
                     re.compile(r"(?<![\w+])\+\d{1,3}[ .-]\d{8,12}(?!\d)"))
_URL_PATH = r"(?:/[^\s\"'<>()]*?)?"
APPSCRIPT_RE = re.compile(r"script\.google\.com" + _URL_PATH + r"/(?:s|d|projects)/([A-Za-z0-9_-]{20,})")
GDOC_RE = re.compile(r"(?:docs|drive|sheets)\.google\.com" + _URL_PATH + r"(?:/d/(?:e/)?|/folders/|[?&]id=)"
                     r"([A-Za-z0-9_-]{20,})")
LINKEDIN_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9_%-]+)", re.I)
HEX64_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
SECRET_RES = (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
              re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"))
APPPW_RE = re.compile(r"(?<![A-Za-z])([a-z]{4})( ?)([a-z]{4})\2([a-z]{4})\2([a-z]{4})(?![A-Za-z])")
# header or parameter name, an optional closing quote, ':' or '=' (or a JSON "name": "value" pair), then the value
PROVKEY_RES = (re.compile(r"(?i)(?<![A-Za-z0-9_-])(?:x-key|x-api-key|x-tomba-key|x-tomba-secret|apikey|api_key|api-key)"
                          r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.+/=-]{16,})"),
               re.compile(r"(?i)(?<![A-Za-z0-9_-])authorization[\"']?\s*[:=]\s*[\"']?bearer\s+([A-Za-z0-9_.+/=-]{16,})"))
FAKE_KEY_PREFIX = "FAKE-"
PLACEHOLDER_WORDS = ("example", "your", "xxxx", "placeholder", "deployment", "sample", "fake", "dummy", "redacted")
ALPHABET_RUN = "abcdefghijklmnopqrstuvwxyz"
HEX_EXEMPT_FILES = ("drivers/manifest.json",)
NAME_KEYS = re.compile(r"^(first_name|last_name|full_name|linkedin_name|display_name|legal_name|preferred_name)$",
                       re.I)
WHOLE_KEYS = re.compile(r"^(company|employer|current_employer|organization|org|university|school|college|"
                        r"institution|email|contact_email|gmail_address|phone|contact_phone|mobile|"
                        r"linkedin_profile_url|linkedin_url|to)$", re.I)
LINK_KEYS = re.compile(r"^(links?|urls?|website|web_site|homepage|portfolio|portfolio_url|blog|github|github_url|"
                       r"gitlab|personal_site|site|profile_url|linkedin_profile_url|linkedin_url)$", re.I)
PERSON_PARENTS = ("contact", "basics", "owner", "signature", "person", "candidate")
# hosts whose first path part is a person's handle
HANDLE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "twitter.com", "x.com", "medium.com", "behance.net",
                "dribbble.com", "kaggle.com", "instagram.com", "leetcode.com", "hashnode.com", "dev.to",
                "huggingface.co", "codepen.io", "substack.com")
# hosting platforms: a subdomain (jane.github.io) is personal, the platform itself is not
PLATFORM_HOSTS = ("github.io", "gitlab.io", "medium.com", "substack.com", "hashnode.dev", "netlify.app",
                  "vercel.app", "pages.dev", "notion.site", "wordpress.com", "blogspot.com", "carrd.co",
                  "wixsite.com", "about.me", "linktr.ee", "google.com", "sites.google.com")
URL_IN_TEXT_RE = re.compile(r"(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,})((?:/[^\s\"'<>()]*)?)", re.I)


# ---------------------------------------------------------------- built-in rules
def _placeholder(s: str) -> bool:
    low = s.lower()
    return any(w in low for w in PLACEHOLDER_WORDS) or len(set(low.replace("-", "").replace("_", ""))) <= 2


def email_allowed(local: str, domain: str) -> bool:
    d = domain.lower()
    addr = "%s@%s" % (local.lower(), d)
    if addr in ALLOWED_EMAILS or d.endswith("users.noreply.github.com"):
        return True
    return any(d == a or d.endswith("." + a) for a in ALLOWED_EMAIL_DOMAINS) or d.endswith(ALLOWED_EMAIL_TLDS)


def phone_allowed(s: str) -> bool:
    digits = re.sub(r"\D", "", s)
    if len(set(digits[-10:])) <= 1 or re.search(r"555\D?01\d\d$", s) or digits.endswith("5550100"):
        return True
    rest = digits[1:] if s.startswith("+") else digits
    return set(rest[-9:]) <= {"0"}


def _phone_hits(line: str) -> list[tuple[int, str, str]]:
    out, spans = [], []

    def add(m):
        if phone_allowed(m.group(0)) or any(m.start() < e and s < m.end() for s, e in spans):
            return
        spans.append((m.start(), m.end()))
        out.append((m.start() + 1, "L-PHONE", "phone-like number"))

    for rx in PHONE_RES:
        for m in rx.finditer(line):
            add(m)
    for rx in PHONE_GROUPED_RES:
        for m in rx.finditer(line):
            if 10 <= len(re.sub(r"\D", "", m.group(0))) <= 15:
                add(m)
    return out


def scan_line(rel: str, line: str) -> list[tuple[int, str, str]]:
    """Built-in findings for one line: (col, rule, redacted excerpt)."""
    if IGNORE_MARK in line:
        return []
    out = []
    for m in EMAIL_RE.finditer(line):
        if not email_allowed(m.group(1), m.group(2)):
            out.append((m.start() + 1, "L-EMAIL", "email at %s" % m.group(2)))
    out += _phone_hits(line)
    for m in APPSCRIPT_RE.finditer(line):
        if not _placeholder(m.group(1)):
            out.append((m.start() + 1, "L-APPSCRIPT", "Apps Script deployment or project URL"))
    for m in GDOC_RE.finditer(line):
        if not _placeholder(m.group(1)):
            out.append((m.start() + 1, "L-GDOC", "Google Sheet or Drive id"))
    for m in LINKEDIN_RE.finditer(line):
        handle = m.group(1).lower()
        if not (handle.startswith("example") or handle.startswith("your-") or handle in ("your", "handle")):
            out.append((m.start() + 1, "L-LINKEDIN", "LinkedIn profile URL"))
    if rel.replace(os.sep, "/") not in HEX_EXEMPT_FILES and "sha256" not in line.lower():
        for m in HEX64_RE.finditer(line):
            if len(set(m.group(0).lower())) > 2:
                out.append((m.start() + 1, "L-HEX64", "64-hex string"))
    for rx in SECRET_RES:
        for m in rx.finditer(line):
            if not _placeholder(m.group(0)):
                out.append((m.start() + 1, "L-SECRET", "secret-like token %s..." % m.group(0)[:4]))
    for rx in PROVKEY_RES:
        for m in rx.finditer(line):
            if not m.group(1).startswith(FAKE_KEY_PREFIX):
                name = m.group(0)[:m.start(1) - m.start()].strip(" \t\"':=")   # never any part of the value
                out.append((m.start() + 1, "L-PROVKEY", "email finder provider key after %s" % name[:30]))
    if "password" in line.lower():
        for m in APPPW_RE.finditer(line):
            word = "".join(m.group(1, 3, 4, 5))
            if not _placeholder(word) and word not in ALPHABET_RUN and len(set(word)) > 4:
                out.append((m.start() + 1, "L-APPPW", "app-password-like string"))
    return out


# ---------------------------------------------------------------- private denylist
def _collect(obj, key: str, out: list, parent: str = "") -> None:
    """(key, value) pairs of string leaves whose key names a person, an organisation or a profile link. A
    {"value": ...} wrapper (profile.json) keeps the key of its field; "name" counts under contact-like objects."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            k = str(k)
            if k == "value":
                _collect(v, key, out, parent)
            else:
                _collect(v, k, out, key)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, key, out, parent)
    elif isinstance(obj, str):
        k = key or ""
        if NAME_KEYS.match(k) or WHOLE_KEYS.match(k):
            out.append((k, obj))
        elif k.lower() == "name" and parent.lower() in PERSON_PARENTS:
            out.append(("full_name", obj))
        elif LINK_KEYS.match(k) or (k.lower() == "url" and parent.lower() in ("links", "profiles")):
            out.append(("link", obj))


def _answers_pairs(doc) -> list:
    """(key, value) of private/answers.json entries ({"answers": [{"key", "value"}]})."""
    rows = doc.get("answers") if isinstance(doc, dict) else doc
    out: list = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and isinstance(r.get("key"), str):
            _collect({r["key"]: r.get("value")}, "", out)
    return out


def national_numbers(value: str) -> list[str]:
    """National forms of an international number, as people also write them: the digits after the country code
    group (+CC NNNNN NNNNN gives NNNNNNNNNN, without a trunk 0) and the last 10 digits of a longer number."""
    v = value.strip()
    digits = re.sub(r"\D", "", v)
    out = []
    m = re.match(r"^\+\s*(\d{1,3})[\s.()-]+(.*)$", v)
    if m:
        rest = re.sub(r"\D", "", m.group(2)).lstrip("0")
        if len(rest) >= 7:
            out.append(rest)
    if v.startswith("+") and len(digits) > 10:
        out.append(digits[-10:])
    return [d for d in out if d != digits]


def _host_terms(host: str, path: str) -> list[str]:
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    seg = path.strip("/").split("/")[0].split("?")[0].lstrip("@") if path else ""
    if host in ("linkedin.com",) or host.endswith(".linkedin.com"):
        m = re.match(r"^/?in/([^/?#]+)", path or "")
        return [m.group(1)] if m else []
    if host in HANDLE_HOSTS:
        return [seg] if seg else []
    for plat in PLATFORM_HOSTS:
        if host.endswith("." + plat):
            label = host[:-len(plat) - 1].split(".")[-1]
            return [host, label]
    if host in PLATFORM_HOSTS or "." not in host:
        return []
    return [host]   # a personal domain (portfolio, blog)


def _link_terms(v: str) -> list[str]:
    out = []
    for m in URL_IN_TEXT_RE.finditer(v):
        dom = m.group(1).lower()
        if dom.endswith((".png", ".jpg", ".pdf", ".json", ".md", ".txt")):
            continue
        out += _host_terms(dom, m.group(2) or "")
    return out


def _terms_from_value(key: str, v: str) -> list[str]:
    v = v.strip()
    if not v:
        return []
    if key == "link":
        return _link_terms(v)
    terms = [v]
    if "@" in v:
        terms.append(v.split("@", 1)[0])
    elif "linkedin.com/in/" in v.lower() or re.search(r"https?://|www\.", v, re.I):
        terms = _link_terms(v)
    elif not re.search(r"[A-Za-z]", v) and len(re.sub(r"\D", "", v)) >= 7:
        terms.append(re.sub(r"\D", "", v))
        terms.extend(national_numbers(v))
    elif NAME_KEYS.match(key or ""):
        terms.extend(p for p in re.split(r"[\s,.]+", v) if len(p) >= 3)
    return terms


def _placeholder_term(low: str) -> bool:
    """A denylist term that is a placeholder of the shipped examples: one of its words is a placeholder word
    (your-handle, you@example.com, Your Name), or it uses a single character (xxx, 000). A real name that merely
    contains such a word (YourStory Media, Sampleson Ltd) or uses two letters (Anna, Eve) is kept."""
    words = _TOKEN_RE.findall(low)
    if not words:
        return True
    if any(w in PLACEHOLDER_WORDS or w in ("you", "handle", "xxx", "todo", "tbd") for w in words):
        return True
    return len(set("".join(words))) <= 1


def private_terms(repo: str = REPO, env: dict | None = None, warn=None) -> list[str]:
    """Denylist terms from private/ (config.json owner fields, profile.json, answers.json, resume/*.json: names,
    organisations, contact fields and profile links) and from the file named by JH_LEAKCHECK_DENYLIST.
    Placeholders are dropped; denylist file lines that cannot be used are reported through `warn` (stderr)."""
    env = os.environ if env is None else env
    warn = warn or (lambda msg: print(msg, file=sys.stderr))
    values: list = []
    pdir = os.path.join(repo, "private")
    rdir = os.path.join(pdir, "resume")
    candidates = [os.path.join(pdir, "config.json"), os.path.join(pdir, "profile.json"),
                  os.path.join(pdir, "answers.json")]
    if os.path.isdir(rdir):
        candidates += sorted(os.path.join(rdir, f) for f in os.listdir(rdir) if f.endswith(".json"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        name = os.path.basename(path)
        if name == "config.json" and os.path.dirname(path) == pdir:
            _collect({"owner": doc.get("owner")} if isinstance(doc, dict) else {}, "", values)
        elif name == "answers.json" and os.path.dirname(path) == pdir:
            values += _answers_pairs(doc)
        else:
            _collect(doc, "", values)
    terms: list[str] = []
    for key, v in values:
        terms.extend(_terms_from_value(key, v))
    explicit: set = set()
    extra = env.get(ENV_DENYLIST, "")
    if extra:
        real = os.path.realpath(os.path.expanduser(extra))
        if real.startswith(os.path.realpath(repo) + os.sep):
            raise SystemExit("leakcheck: %s must point outside the repo" % ENV_DENYLIST)
        try:
            with open(real, "r", encoding="utf-8") as fh:
                for n, raw in enumerate(fh, 1):
                    t = raw.strip()
                    if not t or t.startswith("#"):
                        continue
                    low = t.lower()
                    if len(low) < 3 or not _TOKEN_RE.search(low):
                        warn("leakcheck: %s line %d is not used (fewer than 3 characters)" % (ENV_DENYLIST, n))
                        continue
                    terms.append(t)
                    explicit.add(low)
        except OSError as exc:
            raise SystemExit("leakcheck: cannot read %s: %s" % (ENV_DENYLIST, exc))
    clean: list[str] = []
    for t in terms:
        low = t.lower().strip()
        if len(low) < 3 or (low not in explicit and _placeholder_term(low)):
            continue
        if "@" in low:
            loc, _, dom = low.partition("@")
            if email_allowed(loc, dom):
                continue
        digits = re.sub(r"\D", "", low)
        if not re.search(r"[a-z]", low) and (len(digits) < 7 or phone_allowed(low)):
            continue
        if not re.search(r"[a-z]", low):
            low = digits
        if low not in clean:
            clean.append(low)
    return clean


# A maintainer denylist easily holds more terms than the re module's own cache (512 entries). Building each
# pattern inside the per-line loop then recompiled every term on every line (minutes for the whole repo), so
# the patterns are compiled once per run (term_regex) and a Denylist indexes the word terms by their first
# run of letters and digits: a line is only tested against the terms whose first run starts one of its tokens.
_TERM_RX: dict[str, re.Pattern] = {}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NON_DIGIT_RE = re.compile(r"\D")
_PARTS_RE = re.compile(r"[a-z0-9]+|[^a-z0-9]+")
_SEPARATORS = set(" \t._-")
SEP_RX = r"[\s._-]*"


def term_regex(term: str) -> re.Pattern:
    """Case-folded pattern of a term, on word boundaries. Between two words, spaces, hyphens, underscores and dots
    are interchangeable and may be left out (acme corp = acme-corp = acme_corp = AcmeCorp); other characters
    (@, /, +) and leading or trailing punctuation must match literally."""
    rx = _TERM_RX.get(term)
    if rx is None:
        parts = _PARTS_RE.findall(term)
        body = []
        for i, p in enumerate(parts):
            inner = 0 < i < len(parts) - 1
            if inner and not p[0].isalnum() and set(p) <= _SEPARATORS:
                body.append(SEP_RX)
            else:
                body.append(re.escape(p))
        rx = _TERM_RX[term] = re.compile(r"(?<![a-z0-9])" + "".join(body) + r"(?![a-z0-9])")
    return rx


class Denylist:
    """The private terms, compiled once. Term numbers (1-based, in list order) are what findings print."""

    def __init__(self, terms: list[str]):
        self.terms = list(terms)
        self.digit_terms: list[tuple[int, str]] = []
        self.by_token: dict[str, list[tuple[int, str, re.Pattern]]] = {}
        self.unindexed: list[tuple[int, str, re.Pattern]] = []
        self.max_first = 0
        for i, t in enumerate(self.terms, 1):
            if t.isdigit():
                if len(t) >= 7:
                    self.digit_terms.append((i, t))
                continue
            # a match starts on a boundary, so some token of the line starts with the term's first run (it can be
            # longer when the words are written together: AcmeCorp)
            m = _TOKEN_RE.match(t)
            entry = (i, t, term_regex(t))
            if m:
                self.by_token.setdefault(m.group(0), []).append(entry)
                self.max_first = max(self.max_first, len(m.group(0)))
            else:
                self.unindexed.append(entry)

    def __len__(self) -> int:
        return len(self.terms)

    def scan(self, line: str) -> list[tuple[int, int]]:
        if IGNORE_MARK in line or not self.terms:
            return []
        low = line.lower()
        out = []
        if self.digit_terms:
            digits = _NON_DIGIT_RE.sub("", line)
            hit = [(i, t) for i, t in self.digit_terms if t in digits]
            # a national form inside the matched international number is the same finding
            out += [(1, i) for i, t in hit if not any(t != u and t in u for _, u in hit)]
        cands = list(self.unindexed)
        if self.by_token:
            seen = set()
            for tok in set(_TOKEN_RE.findall(low)):
                for k in range(1, min(len(tok), self.max_first) + 1):
                    hit = self.by_token.get(tok[:k])
                    if hit and tok[:k] not in seen:
                        seen.add(tok[:k])
                        cands += hit
        for i, _t, rx in cands:
            m = rx.search(low)
            if m:
                out.append((m.start() + 1, i))
        out.sort(key=lambda h: h[1])
        return out


def as_denylist(terms) -> Denylist:
    return terms if isinstance(terms, Denylist) else Denylist(terms or [])


def scan_private(line: str, terms) -> list[tuple[int, int]]:
    """(col, term index) for each denylist hit. Digit-only terms match the line's digits; word terms match
    case-insensitively on word boundaries. `terms` is a Denylist (compiled once) or a list of terms."""
    return as_denylist(terms).scan(line)


# ---------------------------------------------------------------- driver
def check_data(rel: str, data: bytes, terms) -> list[str]:
    text, _enc = decode_text(rel, data)
    if text is None:
        return []
    deny = as_denylist(terms)
    out = []
    for ln, line in enumerate(text.split("\n"), 1):
        for col, rule, what in scan_line(rel, line):
            out.append("%s:%d:%d: %s %s" % (rel, ln, col, rule, what))
        for col, idx in deny.scan(line):
            out.append("%s:%d:%d: L-PRIVATE private denylist term #%d" % (rel, ln, col, idx))
    return out


def check_file(repo: str, rel: str, terms) -> list[str]:
    path = os.path.join(repo, rel)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return ["%s:0:0: L-READ %s" % (rel, exc)]
    return check_data(rel, data, terms)


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    repo, staged, args = split_args(list(sys.argv[1:] if argv is None else argv))
    terms = Denylist(private_terms(repo, env))   # compiled once for the whole run
    problems = []
    if staged:
        try:
            snapshot = staged_files(repo, rel_paths(repo, args) if args else None)
        except (OSError, subprocess.CalledProcessError) as exc:
            print("leakcheck: cannot read the git index: %s" % exc, file=sys.stderr)
            return 2
        for rel, data in snapshot:
            problems.extend(check_data(rel, data, terms))
        count = len(snapshot)
    else:
        files = rel_paths(repo, args) if args else repo_files(repo)
        for rel in files:
            problems.extend(check_file(repo, rel, terms))
        count = len(files)
    for p in problems:
        print(p)
    print("leakcheck: %d %sfile(s), %d private term(s), %d finding(s)"
          % (count, "staged " if staged else "", len(terms), len(problems)), file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
