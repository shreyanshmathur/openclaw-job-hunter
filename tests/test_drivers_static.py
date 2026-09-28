"""U6 drivers: static proof that every driver is a read-only page function, the manifest hashes match the exact
function text, and the embedded stop signatures are current (tools/gen_drivers.py --check)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import tests  # noqa: F401
from jobhunter import paths

DRIVERS = os.path.join(paths.REPO, "drivers")
EXPECTED = ("detect_page", "read_compose", "read_form", "read_toast", "read_li_invite_dialog", "read_identity",
            "read_applied_state", "read_sent_invites", "read_gmail_list", "read_gmail_message", "read_login_state")

# anything that could change a page, send data, persist data or run other code
FORBIDDEN = [
    r"\.click\s*\(", r"\.focus\s*\(", r"\.blur\s*\(", r"\.submit\s*\(", r"\.reset\s*\(", r"\.select\s*\(",
    r"dispatchEvent", r"\bnew\s+(Event|CustomEvent|KeyboardEvent|MouseEvent|InputEvent|PointerEvent|FocusEvent)\b",
    r"\bnew\s+(Function|XMLHttpRequest|WebSocket|EventSource|Worker|SharedWorker|Image)\b", r"\bFunction\s*\(",
    r"\beval\s*\(", r"\bimport\s*\(", r"\bfetch\s*\(", r"sendBeacon", r"XMLHttpRequest", r"WebSocket", r"postMessage",
    r"localStorage", r"sessionStorage", r"indexedDB", r"document\.cookie", r"\bcookieStore\b", r"navigator\.clipboard",
    r"execCommand", r"window\.open", r"\bopen\s*\(", r"location\.(assign|replace|reload)", r"\bhistory\.",
    r"setAttribute", r"removeAttribute", r"toggleAttribute", r"appendChild", r"insertBefore", r"removeChild",
    r"replaceChild", r"replaceWith", r"\.remove\s*\(", r"insertAdjacent", r"\.append\s*\(", r"\.prepend\s*\(",
    r"\bsetTimeout\b", r"\bsetInterval\b", r"requestAnimationFrame", r"scrollIntoView", r"\.scroll(To|By)?\s*\(",
    r"setSelectionRange", r"getOwnPropertyDescriptor", r"\.set\.call", r"defineProperty", r"\basync\b", r"\bawait\b",
    r"\bdocument\.write", r"innerHTML", r"outerHTML", r"attachShadow", r"\.requestSubmit", r"showModal",
    r"\bchrome\.", r"\bbrowser\.", r"\bprocess\.", r"\brequire\s*\(", r"MutationObserver", r"addEventListener",
]
# no assignment to any property (element.value = x, obj.a += 1, el.x++), nor through brackets
PROPERTY_WRITE = [r"\.[A-Za-z_$][\w$]*\s*(=(?!=)|[-+*/%&|^]=|\+\+|--)", r"\]\s*(=(?!=)|[-+*/%&|^]=|\+\+|--)"]


def load_gen():
    spec = importlib.util.spec_from_file_location("gen_drivers", os.path.join(paths.REPO, "tools", "gen_drivers.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def strip_strings_and_comments(src: str) -> str:
    """JS source with comments, string literals and regex literals blanked (good enough for our drivers)."""
    out, i, n = [], 0, len(src)
    prev = ""
    while i < n:
        c = src[i]
        if src.startswith("/*", i):
            j = src.index("*/", i + 2)
            i = j + 2
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "'\"`":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append('""')
            i = j + 1
            prev = '"'
            continue
        if c == "/" and prev in "(,=:[!&|?{};" + "\n":
            j = i + 1
            in_class = False
            while True:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            out.append("/r/")
            i = j
            prev = "r"
            continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


class TestDriversStatic(unittest.TestCase):
    def sources(self):
        out = {}
        for name in EXPECTED:
            with open(os.path.join(DRIVERS, name + ".js"), encoding="utf-8") as fh:
                out[name] = fh.read()
        return out

    def test_expected_drivers_only(self):
        found = sorted(os.path.splitext(f)[0] for f in os.listdir(DRIVERS) if f.endswith(".js"))
        self.assertEqual(found, sorted(EXPECTED))

    def test_shape_ascii_and_read_only(self):
        for name, src in self.sources().items():
            with self.subTest(driver=name):
                src.encode("ascii")
                self.assertNotRegex(src, "[" + chr(0x2010) + "-" + chr(0x2015) + "]")
                text = src.strip()
                self.assertTrue(text.startswith("() => {"), "a driver is one zero-argument arrow function")
                self.assertTrue(text.endswith("}"))
                self.assertTrue(src.endswith("}\n") and not src.startswith((" ", "\n")))
                code = strip_strings_and_comments(text)
                for rx in FORBIDDEN:
                    self.assertIsNone(re.search(rx, code), "%s uses %s" % (name, rx))
                for rx in PROPERTY_WRITE:
                    m = re.search(rx, code)
                    self.assertIsNone(m, "%s writes a property: %r" % (name, m.group(0) if m else ""))
                self.assertEqual(code.count("{"), code.count("}"))
                self.assertEqual(code.count("("), code.count(")"))
                self.assertIn("return", code)

    def test_stripper_catches_writes(self):
        bad = "() => { const ta = document.querySelector('textarea'); ta.value = 'x'; el.click(); }"
        code = strip_strings_and_comments(bad)
        self.assertIsNotNone(re.search(PROPERTY_WRITE[0], code))
        self.assertIsNotNone(re.search(FORBIDDEN[0], code))
        self.assertIsNone(re.search(PROPERTY_WRITE[0], strip_strings_and_comments("() => { x = a.b === c; }")))

    def test_manifest_matches_function_text(self):
        with open(os.path.join(DRIVERS, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(sorted(manifest), sorted(EXPECTED))
        for name, src in self.sources().items():
            with self.subTest(driver=name):
                self.assertEqual(manifest[name], hashlib.sha256(src.strip().encode("utf-8")).hexdigest())

    def test_generated_files_are_current(self):
        gen = load_gen()
        for path, content in gen.build().items():
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), content, "%s is stale: run python3 tools/gen_drivers.py" % path)

    def test_signatures_embedded_and_portable(self):
        gen = load_gen()
        src = self.sources()["detect_page"]
        sev, sig = gen.parse_block(src)
        self.assertEqual(sorted(sig), sorted(gen.FAMILIES))
        self.assertEqual(tuple(sev), gen.severity())
        self.assertEqual(tuple(sev), gen.SEVERITY_FALLBACK, "keep SEVERITY_FALLBACK equal to jobhunter.detect.SEVERITY")
        pairs = {fam: {(w, s[w]) for s in items for w in gen.WHERE if w in s} for fam, items in sig.items()}
        for fam, items in gen.BASELINE.items():
            for where, pat, _reason in items:
                self.assertIn((where, pat), pairs[fam])
        for fam, items in sig.items():
            ids = [s["id"] for s in items]
            for s in items:
                self.assertTrue(set(s) >= {"id", "reason_code", "trip", "job_needs_human"}, s)
                where = [w for w in gen.WHERE + ("probe",) if w in s]
                self.assertEqual(len(where), 1, s)
                self.assertTrue(s["trip"] or s["job_needs_human"], "%s: a stop trips or stops the job" % s["id"])
                if where[0] != "probe":
                    re.compile(s[where[0]])
                    self.assertIsNone(gen._PY_ONLY.search(s[where[0]]), s)
            self.assertEqual(ids[-len(gen.PROBES[fam]):], ["probe_" + p for p, _r in gen.PROBES[fam]])
        # the detect file's own signatures come first, in file order, with their ids
        self.assertEqual(sig["linkedin"][0]["id"], "li_checkpoint_url")
        self.assertIn(("text", "weekly invitation limit"), pairs["linkedin"])
        self.assertIn(("url", "/checkpoint/"), pairs["linkedin"])
        job_level = [s for s in sig["ats"] if s["job_needs_human"]]
        self.assertTrue(job_level and all(not s["trip"] and s["reason_code"] is None for s in job_level))

    def test_rank_matches_core_detect(self):
        """gen_drivers.rank orders hits like jobhunter.detect._rank: trip first, then severity, then order."""
        gen = load_gen()
        from jobhunter import detect
        sigs = [{"id": "a", "reason_code": "li_challenge", "trip": True, "job_needs_human": None},
                {"id": "b", "reason_code": "li_restricted", "trip": True, "job_needs_human": None},
                {"id": "c", "reason_code": None, "trip": False, "job_needs_human": "captcha_visible"},
                {"id": "d", "reason_code": "li_restricted", "trip": True, "job_needs_human": None}]
        ours = sorted(range(len(sigs)), key=lambda i: gen.rank(sigs[i], i))
        core = sorted(range(len(sigs)), key=lambda i: detect._rank(dict(sigs[i], verdict="stop"), i))
        self.assertEqual(ours, core)
        self.assertEqual([sigs[i]["id"] for i in ours], ["b", "d", "a", "c"])

    def test_extractor_reads_detect_files(self):
        gen = load_gen()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "linkedin.json"), "w") as fh:
                json.dump({"signatures": [{"id": "wall", "verdict": "stop", "trip": True, "reason_code": "li_challenge",
                                           "url": "/newwall"},
                                          {"verdict": "stop", "trip": True, "reason_code": "li_logged_out",
                                           "title": "^Oops"},
                                          {"verdict": "clear", "text": "fine"}, {"verdict": "stop", "text": "(?i)bad"},
                                          {"verdict": "stop", "http_status": [429]}],
                           "error_after_click": [{"text": "not a stop"}]}, fh)
            sig = gen.signatures(d)
        li = sig["linkedin"]
        by = {(w, s[w]): s for s in li for w in gen.WHERE if w in s}
        self.assertEqual(by[("url", "/newwall")]["id"], "wall")
        self.assertEqual(by[("url", "/newwall")]["reason_code"], "li_challenge")
        self.assertEqual(by[("title", "^Oops")]["id"], "linkedin_file_2")
        self.assertNotIn(("text", "fine"), by)
        self.assertNotIn(("text", "not a stop"), by)
        self.assertNotIn(("text", "(?i)bad"), by)
        self.assertEqual(li[0]["id"], "wall")
        self.assertIn("baseline_linkedin_1", [s["id"] for s in li])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_node_parses_every_driver(self):
        with tempfile.TemporaryDirectory() as d:
            for name, src in self.sources().items():
                path = os.path.join(d, name + ".js")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("const f = " + src.strip() + ";\n")
                res = subprocess.run([shutil.which("node"), "--check", path], capture_output=True, text=True,
                                     timeout=30)
                self.assertEqual(res.returncode, 0, "%s: %s" % (name, res.stderr))



# A tiny DOM for running a driver under node: enough of Element for read_compose.js (tagName, attributes,
# className, value, innerText, visibility, querySelectorAll with simple selectors, closest, contains,
# parentElement, childNodes). A string in a spec's children is a text node (nodeType 3, textContent).
FAKE_DOM_JS = r"""
function matchOne(el, sel) {
  sel = sel.trim();
  if (sel === '*') { return true; }
  const m = sel.match(/^([A-Za-z][A-Za-z0-9]*)?((?:\.[\w-]+|\[[^\]]+\])*)$/);
  if (!m) { throw new Error('unsupported selector ' + sel); }
  if (m[1] && m[1].toUpperCase() !== el.tagName) { return false; }
  const parts = m[2].match(/\.[\w-]+|\[[^\]]+\]/g) || [];
  return parts.every((p) => {
    if (p[0] === '.') { return (' ' + el.className + ' ').indexOf(' ' + p.slice(1) + ' ') >= 0; }
    const a = p.slice(1, -1).match(/^([\w-]+)(?:="([^"]*)")?$/);
    const v = el.getAttribute(a[1]);
    return a[2] === undefined ? v !== null : v === a[2];
  });
}
function matches(el, sel) { return sel.split(',').some((s) => matchOne(el, s)); }
class El {
  constructor(spec, parent) {
    this.tagName = (spec.tag || 'div').toUpperCase();
    this.attrs = spec.attrs || {};
    this.className = spec.cls || '';
    this.value = spec.value;
    this.ownText = spec.text;
    this.hidden = spec.hidden === true;
    this.parent = parent;
    this.shadowRoot = null;
    this.nodeType = 1;
    this.childNodes = (spec.children || []).map((c) => typeof c === 'string' ? {nodeType: 3, textContent: c} : new El(c, this));
    this.children = this.childNodes.filter((c) => c instanceof El);
  }
  get parentElement() { return this.parent && this.parent.tagName !== '#ROOT' ? this.parent : null; }
  getAttribute(n) { if (n === 'class') { return this.className; } return n in this.attrs ? String(this.attrs[n]) : null; }
  getBoundingClientRect() {
    let e = this; let shown = true;
    while (e) { if (e.hidden) { shown = false; } e = e.parent; }
    return shown ? {width: 100, height: 20} : {width: 0, height: 0};
  }
  get innerText() {
    if (this.ownText !== undefined) { return this.ownText; }
    if (this.childNodes.length !== this.children.length) {
      return this.childNodes.map((c) => c instanceof El ? c.innerText : c.textContent).join('');
    }
    return this.children.map((c) => c.innerText).filter((t) => t !== '').join('\n');
  }
  all() { const out = []; for (const c of this.children) { out.push(c); out.push(...c.all()); } return out; }
  querySelectorAll(sel) { return this.all().filter((e) => matches(e, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { let e = this; while (e && e.tagName !== '#ROOT') { if (matches(e, sel)) { return e; } e = e.parent; } return null; }
  contains(o) { while (o) { if (o === this) { return true; } o = o.parent; } return false; }
}
function installDom(spec) {
  const root = new El({tag: '#root', children: spec.children}, null);
  const body = root.children[0] || root;
  globalThis.document = {querySelectorAll: (s) => root.querySelectorAll(s), querySelector: (s) => root.querySelector(s),
    body: body, title: spec.title || ''};
  const u = new URL(spec.href);
  globalThis.location = {hostname: spec.host, href: spec.href, hash: u.hash, origin: u.origin, pathname: u.pathname,
    search: u.search};
}
"""


def run_driver(name: str, dom: dict) -> dict:
    """Run drivers/<name>.js under node against the fake DOM `dom` ({host, href, children}) and return its result."""
    with open(os.path.join(DRIVERS, name + ".js"), encoding="utf-8") as fh:
        src = fh.read().strip()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "run.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FAKE_DOM_JS + "\ninstallDom(" + json.dumps(dom) + ");\nconst f = " + src +
                     ";\nprocess.stdout.write(JSON.stringify(f()));\n")
        res = subprocess.run([shutil.which("node"), path], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


def li_page(*children) -> dict:
    return {"host": "www.linkedin.com", "href": "https://www.linkedin.com/messaging/thread/new/",
            "children": [{"tag": "body", "children": list(children)}]}


def subject_input(value: str, **kw) -> dict:
    spec = {"tag": "input", "cls": "msg-form__subject",
            "attrs": {"name": "subject", "type": "text", "placeholder": "Subject (optional)"}, "value": value}
    spec.update(kw)
    return spec


def message_box(text: str) -> dict:
    return {"tag": "div", "cls": "msg-form__contenteditable",
            "attrs": {"contenteditable": "true", "role": "textbox", "aria-label": "Write a message..."},
            "text": text}


BODY = "Hi Alex,\n\nYour post on pincode-level RTO models matched work I did on courier returns.\n\nSam"
SUBJECT = "Pincode-level RTO models"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestReadComposeInMail(unittest.TestCase):
    """read_compose.js on LinkedIn: an InMail compose (subject field) is read back as "Subject: <subject>", a
    blank line, the body, which the real gate canonicalises to the same text as the approved InMail draft."""

    def gate_observed(self, text: str) -> str:
        import importlib
        gate = importlib.import_module("jobhunter.gate")
        return gate._observed_canonical(None, {"kind": "inmail", "platform": "linkedin", "token": "T"}, text)

    def approved(self, subject, body) -> str:
        from jobhunter import canon
        return canon.canonical_send_text("inmail", subject, body, None, None)

    def test_inmail_compose_reads_subject_and_body(self):
        out = run_driver("read_compose", li_page(
            {"tag": "form", "cls": "msg-form", "children": [subject_input(SUBJECT), message_box(BODY)]}))
        self.assertEqual(out["platform"], "linkedin")
        self.assertTrue(out["box_present"])
        self.assertTrue(out["subject_field_present"])
        self.assertEqual(out["subject"], SUBJECT)
        self.assertEqual(out["text"], BODY)
        self.assertEqual(out["observed_text"], "Subject: " + SUBJECT + "\n\n" + BODY)
        self.assertEqual(self.gate_observed(out["observed_text"]), self.approved(SUBJECT, BODY))

    def test_wrong_subject_in_the_field_is_a_mismatch(self):
        out = run_driver("read_compose", li_page(
            {"tag": "form", "cls": "msg-form", "children": [subject_input("Quick question"), message_box(BODY)]}))
        self.assertNotEqual(self.gate_observed(out["observed_text"]), self.approved(SUBJECT, BODY))
        # and a read-back without the subject line never matches an InMail approved with a subject
        self.assertNotEqual(self.gate_observed(out["text"]), self.approved(SUBJECT, BODY))

    def test_empty_subject_field_matches_a_draft_without_subject(self):
        out = run_driver("read_compose", li_page(
            {"tag": "form", "cls": "msg-form", "children": [subject_input(""), message_box(BODY)]}))
        self.assertTrue(out["subject_field_present"])
        self.assertEqual(out["subject"], "")
        self.assertEqual(out["observed_text"], "Subject: \n\n" + BODY)
        self.assertEqual(self.gate_observed(out["observed_text"]), self.approved(None, BODY))
        self.assertNotEqual(self.gate_observed(out["observed_text"]), self.approved(SUBJECT, BODY))

    def test_subject_in_the_conversation_bubble_outside_the_form(self):
        out = run_driver("read_compose", li_page(
            {"tag": "div", "cls": "msg-overlay-conversation-bubble", "children": [
                {"tag": "div", "cls": "msg-form__header", "children": [subject_input(SUBJECT)]},
                {"tag": "form", "cls": "msg-form", "children": [message_box(BODY)]}]}))
        self.assertEqual(out["observed_text"], "Subject: " + SUBJECT + "\n\n" + BODY)

    def test_plain_message_ignores_a_subject_field_of_another_compose(self):
        out = run_driver("read_compose", li_page(
            {"tag": "div", "cls": "msg-overlay-conversation-bubble", "children": [
                {"tag": "form", "cls": "msg-form", "children": [subject_input("Other thread"),
                                                                message_box("Earlier draft")]}]},
            {"tag": "div", "cls": "msg-overlay-conversation-bubble", "children": [
                {"tag": "form", "cls": "msg-form", "children": [message_box(BODY)]}]}))
        self.assertFalse(out["subject_field_present"])
        self.assertIsNone(out["subject"])
        self.assertEqual(out["observed_text"], BODY)
        from jobhunter import canon
        self.assertEqual(canon.canonical_send_text("li_message", None, out["observed_text"], None, None),
                         canon.canonical_send_text("li_message", None, BODY, None, None))

    def test_hidden_or_non_text_subject_inputs_are_ignored(self):
        out = run_driver("read_compose", li_page(
            {"tag": "form", "cls": "msg-form", "children": [
                subject_input(SUBJECT, hidden=True),
                {"tag": "input", "attrs": {"name": "subject", "type": "hidden"}, "value": "x"},
                message_box(BODY)]}))
        self.assertFalse(out["subject_field_present"])
        self.assertEqual(out["observed_text"], BODY)

    def test_no_box_means_no_read_back(self):
        out = run_driver("read_compose", li_page(
            {"tag": "form", "cls": "msg-form", "children": [subject_input(SUBJECT)]}))
        self.assertFalse(out["box_present"])
        self.assertIsNone(out["observed_text"])
        self.assertFalse(out["subject_field_present"])

    def test_gmail_read_back_unchanged(self):
        out = run_driver("read_compose", {
            "host": "mail.google.com", "href": "https://mail.google.com/mail/u/0/#inbox?compose=new",
            "children": [{"tag": "body", "children": [{"tag": "div", "attrs": {"role": "dialog"}, "children": [
                {"tag": "input", "attrs": {"name": "subjectbox"}, "value": SUBJECT},
                {"tag": "div", "attrs": {"contenteditable": "true", "aria-label": "Message Body"}, "text": BODY}]}]}]})
        # no recipient in the window: the To line is empty, which gate arm refuses (one To address required)
        self.assertEqual(out["observed_text"], "Subject: " + SUBJECT + "\nTo:\n\n" + BODY)
        self.assertEqual((out["to"], out["cc"], out["bcc"]), ([], [], []))
        from jobhunter import gate
        rb = gate.email_readback(out["observed_text"])
        self.assertEqual((rb["subject"], rb["to"], rb["body"]), (SUBJECT, "", BODY))
        self.assertEqual(gate.recipient_problem({"recipient": "alex.rivera@kestrel.example"}, rb), "to_not_one_address")


def load_pages() -> dict:
    with open(os.path.join(paths.REPO, "tests", "fixtures", "browser", "gmail_web_pages.json"), encoding="utf-8") as fh:
        return json.load(fh)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestDetectPageRanking(unittest.TestCase):
    """detect_page.js picks the most severe matching signature, the way jh.py detect does."""

    def run_page(self, name):
        return run_driver("detect_page", load_pages()[name])

    def test_restriction_under_checkpoint_url_is_not_downgraded(self):
        out = self.run_page("detect_li_restricted_checkpoint")
        self.assertEqual(out["hint"], "stop")
        self.assertEqual(out["top"]["reason_code"], "li_restricted")
        self.assertTrue(out["top"]["trip"])
        self.assertEqual(out["top"]["scope"], "linkedin")
        self.assertGreater(len(out["matched"]), 1)
        self.assertEqual(out["matched"][0], out["top"]["where"] + ":" + out["top"]["id"])
        from jobhunter import detect
        verdict, sig = detect.match(out["detect_file"])
        self.assertEqual((verdict, sig["reason_code"]), ("stop", out["top"]["reason_code"]))

    def test_tripping_stop_beats_a_job_level_stop(self):
        out = self.run_page("detect_ats_captcha_and_block")
        self.assertEqual((out["hint"], out["top"]["reason_code"], out["top"]["scope"]), ("stop", "ats_blocked", "ats"))
        self.assertIn("text:ats_captcha", out["matched"])

    def test_job_level_stop_alone_is_needs_human(self):
        out = self.run_page("detect_ats_captcha")
        self.assertEqual(out["hint"], "needs_human")
        self.assertEqual((out["top"]["job_needs_human"], out["top"]["trip"], out["top"]["scope"]),
                         ("captcha_visible", False, None))

    def test_clear_and_board_login(self):
        out = self.run_page("detect_clear")
        self.assertEqual((out["hint"], out["top"], out["matched"]), ("clear", None, []))
        self.assertEqual(out["detect_file"]["platform"], "site:naukri")
        out = self.run_page("detect_board_login")
        self.assertEqual((out["hint"], out["top"]["scope"]), ("stop", "site:naukri"))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestGmailWebDrivers(unittest.TestCase):
    """The web_ui route read on recorded-shape Gmail pages: precheck counts, the Sent read of mail audit,
    replies and delivery failures, and the Sent-folder read-back after a send."""

    @classmethod
    def setUpClass(cls):
        cls.pages = load_pages()

    def run_page(self, driver, name):
        return run_driver(driver, self.pages[name])

    def test_precheck_counts_feed_the_gate_checks(self):
        from jobhunter import gate
        hit = self.run_page("read_gmail_list", "sent_search_hit")
        self.assertEqual((hit["loaded"], hit["count"], hit["complete"]), (True, 1, True))
        self.assertEqual(hit["query"], "in:sent to:alex.rivera@kestrel.example")
        self.assertEqual(hit["rows"][0]["to"], ["alex.rivera@kestrel.example"])
        self.assertEqual(hit["rows"][0]["thread_id"], "18c2f0000000a001")
        empty = self.run_page("read_gmail_list", "sent_search_empty")
        self.assertEqual((empty["loaded"], empty["count"], empty["empty"]), (True, 0, True))
        hidden = self.run_page("read_gmail_list", "hidden_rows_only")
        self.assertEqual(hidden["count"], 0, "rows of a hidden list view are not counted")
        unknown = self.run_page("read_gmail_list", "not_loaded")
        self.assertEqual((unknown["loaded"], unknown["count"]), (False, None))
        outbox = self.run_page("read_gmail_list", "outbox_empty")
        evidence = {"kind": "cold_email", "platform": "gmail", "observed_at": "2026-09-28T05:00:00Z", "checks": [
            {"name": "sent_to_address", "value": hit["count"]}, {"name": "sent_to_other_addresses", "value": 0},
            {"name": "sent_company_query", "value": empty["count"]}, {"name": "outbox_query", "value": outbox["count"]},
            {"name": "scheduled_query", "value": hidden["count"]}]}
        vals = gate._validate_checks("cold_email", evidence)
        self.assertEqual(gate._precheck_result(None, "cold_email", vals, {})[0], "already_done")
        vals["sent_to_address"] = 0
        self.assertEqual(gate._precheck_result(None, "cold_email", vals, {})[0], "clear")

    def test_sent_read_is_a_valid_mail_audit_file(self):
        from jobhunter.mail import audit
        out = self.run_page("read_gmail_list", "sent_audit")
        read = out["sent_read"]
        self.assertTrue(read["complete"])
        self.assertEqual([m["to"] for m in read["messages"]],
                         [["alex.rivera@kestrel.example"], ["priya.nair@tidemark.example"]])
        self.assertEqual(read["messages"][0]["date"][:10], "2026-09-27")
        parsed = audit.parse_web_read(read)
        self.assertEqual((parsed["purpose"], len(parsed["headers"])), ("audit", 2))
        self.assertTrue(parsed["headers"][0]["url"].startswith("https://mail.google.com/mail/u/0/#all/"))
        self.assertEqual(audit.parse_web_read(dict(read, purpose="history"))["purpose"], "history")

    def test_reply_and_our_message_in_a_conversation(self):
        out = self.run_page("read_gmail_message", "inbox_reply")
        self.assertTrue(out["loaded"])
        ours, theirs = out["messages"]
        self.assertTrue(ours["from_owner"])
        self.assertFalse(theirs["from_owner"])
        self.assertEqual((theirs["from"], theirs["msg_ref"], theirs["is_bounce"]),
                         ("alex.rivera@kestrel.example", "gm:18c2f0000000b002", False))
        self.assertEqual(theirs["body"], "Thanks Sam, happy to talk. Does Tuesday work? What is your notice period?")
        self.assertRegex(theirs["date"], r"^2026-09-2[78]T[0-9:]{8}Z$")   # the page's local time, in UTC

    def test_delivery_failure_names_the_failed_address(self):
        out = self.run_page("read_gmail_message", "bounce_notice")
        msg = out["messages"][0]
        self.assertTrue(msg["is_bounce"])
        self.assertEqual(msg["bounce_addresses"], ["jordan.lee@tidemark.example"])

    def test_sent_readback_matches_the_approved_text(self):
        from jobhunter import canon
        out = self.run_page("read_gmail_message", "sent_readback")
        from jobhunter import gate
        msg = out["messages"][0]
        rb = msg["readback_text"]
        head, _sep, body = rb.partition("\n\n")
        self.assertEqual(head, "Subject: Pincode-level RTO models\nTo: alex.rivera@kestrel.example")
        self.assertEqual((msg["to"], msg["cc"], msg["bcc"]), (["alex.rivera@kestrel.example"], [], []))
        body_src = self.pages["sent_readback"]["children"][0]["children"][1]["children"][1]["children"][3][
            "children"][0]["text"]
        parsed = gate.email_readback(rb)
        self.assertEqual(canon.canonical_send_text("cold_email", parsed["subject"], parsed["body"], None, None),
                         canon.canonical_send_text("cold_email", "Pincode-level RTO models", body_src, None, None))
        self.assertIsNone(gate.recipient_problem({"recipient": "alex.rivera@kestrel.example"}, parsed))

    def test_inline_reply_reads_back_the_re_subject(self):
        out = self.run_page("read_compose", "followup_reply_compose")
        self.assertEqual((out["subject"], out["subject_source"]), ("Re: Pincode-level RTO models", "thread"))
        self.assertTrue(out["observed_text"].startswith(
            "Subject: Re: Pincode-level RTO models\nTo: alex.rivera@kestrel.example\n\nHi Alex,"))
        self.assertEqual((out["to"], out["cc"], out["bcc"]), (["alex.rivera@kestrel.example"], [], []))


# Recipient read-back (gate.email_readback, gate.recipient_problem): the compose window and the Sent copy name
# every address with its field, so gate arm and gate confirm can hold the message to the reserved recipient alone.
TO_ADDR = "jordan.blake@kestrel.example"
CC_ADDR = "casey.morgan@kestrel.example"
BCC_ADDR = "riley.quinn@heron.example"
OWNER_ADDR = "sam.owner@example.com"


def chip(addr: str) -> dict:
    return {"tag": "div", "cls": "afV", "attrs": {"data-hovercard-id": addr},
            "children": [{"tag": "span", "attrs": {"email": addr, "name": addr}, "text": addr}]}


def recipient_row(kind: str, *addrs, typed: str = "", hidden: bool = False) -> dict:
    """One recipient row of a Gmail compose: its label, the address chips and the input ("To recipients")."""
    label = {"to": "To", "cc": "Cc", "bcc": "Bcc"}[kind]
    return {"tag": "div", "cls": "aH9", "hidden": hidden, "children": [{"tag": "span", "text": label}] +
            [chip(a) for a in addrs] + [{"tag": "input", "attrs": {"aria-label": label + " recipients"}, "value": typed}]}


def gmail_compose(*parts, body: str = BODY) -> dict:
    return {"host": "mail.google.com", "href": "https://mail.google.com/mail/u/0/#inbox?compose=new",
            "children": [{"tag": "body", "children": [{"tag": "div", "attrs": {"role": "dialog"}, "children": list(parts) + [
                {"tag": "input", "attrs": {"name": "subjectbox"}, "value": SUBJECT},
                {"tag": "div", "attrs": {"contenteditable": "true", "aria-label": "Message Body"}, "text": body}]}]}]}


def g2(addr: str) -> dict:
    return {"tag": "span", "cls": "g2", "attrs": {"email": addr, "name": addr}, "text": addr}


def gmail_sent(header: list, details: list | None = None) -> dict:
    """A Sent conversation with our one message: `header` is the header line's nodes (strings are text nodes,
    the field markers), `details` the rows of the open details table as (label, [addresses])."""
    msg = [{"tag": "span", "cls": "gD", "attrs": {"email": OWNER_ADDR, "name": "Sam Owner"}, "text": "Sam Owner"},
           {"tag": "div", "cls": "iw", "children": [{"tag": "span", "cls": "hb", "children": header}]}]
    if details:
        msg.append({"tag": "table", "cls": "ajC", "children": [
            {"tag": "tr", "children": [{"tag": "td", "cls": "gG", "text": label},
                                       {"tag": "td", "cls": "gL", "children": [g2(a) for a in addrs]}]}
            for label, addrs in details]})
    msg += [{"tag": "span", "cls": "g3", "attrs": {"title": "Sun, Sep 27, 2026, 10:02 AM"}, "text": "10:02 AM"},
            {"tag": "div", "cls": "a3s", "children": [{"tag": "div", "text": BODY}]}]
    return {"host": "mail.google.com", "href": "https://mail.google.com/mail/u/0/#sent/18c2f0000000c001",
            "title": "Sent Mail - %s - Gmail" % OWNER_ADDR,
            "children": [{"tag": "body", "children": [
                {"tag": "a", "attrs": {"aria-label": "Google Account: Sam Owner (%s)" % OWNER_ADDR}, "text": "S"},
                {"tag": "div", "attrs": {"role": "main"}, "children": [
                    {"tag": "h2", "cls": "hP", "attrs": {"data-legacy-thread-id": "18c2f0000000c001"}, "text": SUBJECT},
                    {"tag": "div", "cls": "adn ads", "attrs": {"data-message-id": "#msg-f:1718c2f0000000c001",
                                                             "data-legacy-message-id": "18c2f0000000c001"},
                     "children": msg}]}]}]}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestGmailRecipientReadback(unittest.TestCase):
    """read_compose.js and read_gmail_message.js give To, and Cc and Bcc when present, in the header lines
    gate.email_readback parses; gate.recipient_problem then passes only the reserved recipient alone."""

    def gate(self, text: str) -> tuple:
        from jobhunter import canon, gate
        rb = gate.email_readback(text)
        same = canon.canonical_send_text("cold_email", rb["subject"], rb["body"], None, None) == \
            canon.canonical_send_text("cold_email", SUBJECT, BODY, None, None)
        return rb, gate.recipient_problem({"recipient": TO_ADDR}, rb), same

    def compose(self, *parts, **kw) -> dict:
        return run_driver("read_compose", gmail_compose(*parts, **kw))

    def test_compose_to_alone_passes(self):
        out = self.compose(recipient_row("to", TO_ADDR))
        self.assertEqual((out["to"], out["cc"], out["bcc"]), ([TO_ADDR], [], []))
        self.assertEqual(out["observed_text"], "Subject: %s\nTo: %s\n\n%s" % (SUBJECT, TO_ADDR, BODY))
        rb, problem, same = self.gate(out["observed_text"])
        self.assertEqual((rb["to"], rb["cc"], rb["bcc"], problem, same), (TO_ADDR, None, None, None, True))

    def test_compose_cc_and_bcc_rows_are_named(self):
        out = self.compose(recipient_row("to", TO_ADDR), recipient_row("cc", CC_ADDR), recipient_row("bcc", BCC_ADDR))
        self.assertEqual((out["to"], out["cc"], out["bcc"]), ([TO_ADDR], [CC_ADDR], [BCC_ADDR]))
        head = out["observed_text"].partition("\n\n")[0]
        self.assertEqual(head.split("\n"), ["Subject: " + SUBJECT, "To: " + TO_ADDR, "Cc: " + CC_ADDR,
                                            "Bcc: " + BCC_ADDR])
        rb, problem, same = self.gate(out["observed_text"])
        self.assertEqual((rb["cc"], rb["bcc"], problem, same), (CC_ADDR, BCC_ADDR, "cc_or_bcc", True))

    def test_hidden_bcc_row_and_typed_cc_still_count(self):
        # a collapsed Bcc row keeps its chip in the page; an address typed in Cc is not a chip yet
        out = self.compose(recipient_row("to", TO_ADDR), recipient_row("cc", typed=CC_ADDR + ","),
                           recipient_row("bcc", BCC_ADDR, hidden=True))
        self.assertEqual((out["cc"], out["bcc"]), ([CC_ADDR], [BCC_ADDR]))
        self.assertEqual(self.gate(out["observed_text"])[1], "cc_or_bcc")
        # text in a recipient field that is no address is still a recipient as it stands
        out = self.compose(recipient_row("to", TO_ADDR), recipient_row("bcc", typed="Riley"))
        self.assertEqual(out["bcc"], ["Riley"])
        self.assertEqual(self.gate(out["observed_text"])[1], "cc_or_bcc")

    def test_second_or_other_to_address_is_refused(self):
        out = self.compose(recipient_row("to", TO_ADDR, CC_ADDR))
        self.assertEqual(out["to"], [TO_ADDR, CC_ADDR])
        self.assertIn("\nTo: %s, %s\n" % (TO_ADDR, CC_ADDR), out["observed_text"])
        self.assertEqual(self.gate(out["observed_text"])[1], "to_not_one_address")
        out = self.compose(recipient_row("to", CC_ADDR))
        self.assertEqual(self.gate(out["observed_text"])[1], "to_other_address")

    def test_address_outside_any_row_counts_as_to(self):
        # the collapsed summary (no field around it) names an address the rows do not show: it is never dropped
        summary = {"tag": "div", "cls": "aoD", "children": [{"tag": "span", "attrs": {"email": CC_ADDR}, "text": CC_ADDR}]}
        out = self.compose(summary, recipient_row("to", TO_ADDR))
        self.assertEqual(sorted(out["to"]), sorted([TO_ADDR, CC_ADDR]))
        self.assertEqual(self.gate(out["observed_text"])[1], "to_not_one_address")
        # the same address in the summary and its To row is one recipient
        summary = {"tag": "div", "cls": "aoD", "children": [{"tag": "span", "attrs": {"email": TO_ADDR}, "text": TO_ADDR}]}
        out = self.compose(summary, recipient_row("to", TO_ADDR))
        self.assertEqual(out["to"], [TO_ADDR])
        self.assertIsNone(self.gate(out["observed_text"])[1])

    def test_address_in_the_body_is_not_a_recipient(self):
        page = gmail_compose(recipient_row("to", TO_ADDR))
        box = page["children"][0]["children"][0]["children"][-1]
        box.pop("text")
        box["children"] = [{"tag": "div", "text": BODY}, {"tag": "span", "attrs": {"email": CC_ADDR}, "text": ""}]
        out = run_driver("read_compose", page)
        self.assertEqual((out["to"], out["cc"], out["bcc"]), ([TO_ADDR], [], []))

    def test_sent_copy_header_markers(self):
        out = run_driver("read_gmail_message", gmail_sent(["to ", g2(TO_ADDR), ", cc: ", g2(CC_ADDR), ", bcc: ",
                                                           g2(BCC_ADDR)]))
        msg = out["messages"][0]
        self.assertTrue(msg["from_owner"])
        self.assertEqual((msg["to"], msg["cc"], msg["bcc"]), ([TO_ADDR], [CC_ADDR], [BCC_ADDR]))
        self.assertEqual(msg["recipients"], [TO_ADDR, CC_ADDR, BCC_ADDR])
        self.assertEqual(msg["readback_text"].partition("\n\n")[0].split("\n"),
                         ["Subject: " + SUBJECT, "To: " + TO_ADDR, "Cc: " + CC_ADDR, "Bcc: " + BCC_ADDR])
        rb, problem, same = self.gate(msg["readback_text"])
        self.assertEqual((problem, same), ("cc_or_bcc", True))
        out = run_driver("read_gmail_message", gmail_sent(["to ", g2(TO_ADDR)]))
        msg = out["messages"][0]
        self.assertEqual((msg["to"], msg["cc"], msg["bcc"]), ([TO_ADDR], [], []))
        self.assertEqual(self.gate(msg["readback_text"])[1:], (None, True))

    def test_sent_copy_details_rows_and_unmarked_extra_address(self):
        out = run_driver("read_gmail_message", gmail_sent(
            ["to ", g2(TO_ADDR)], details=[("to:", [TO_ADDR]), ("bcc:", [BCC_ADDR])]))
        msg = out["messages"][0]
        self.assertEqual((msg["to"], msg["cc"], msg["bcc"]), ([TO_ADDR], [], [BCC_ADDR]))
        self.assertEqual(self.gate(msg["readback_text"])[1], "cc_or_bcc")
        # a second address with no field marker is a second To address, never dropped
        out = run_driver("read_gmail_message", gmail_sent(["to ", g2(TO_ADDR), ", ", g2(CC_ADDR)]))
        msg = out["messages"][0]
        self.assertEqual((msg["to"], msg["cc"]), ([TO_ADDR, CC_ADDR], []))
        self.assertEqual(self.gate(msg["readback_text"])[1], "to_not_one_address")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestLoginState(unittest.TestCase):
    """read_login_state.js on consented sites; its probe gives the installer's login verdicts."""

    @classmethod
    def setUpClass(cls):
        cls.pages = load_pages()

    def probe(self, name):
        return run_driver("read_login_state", self.pages[name])

    def verdict(self, site, probe, want=None):
        from jobhunter import install
        return install.login_verdict(site, probe, want)[0]

    def test_gmail(self):
        owner = self.pages["owner"]
        p = self.probe("login_gmail_ok")
        self.assertEqual((p["site"], p["state"], p["account_email"]), ("gmail", "ok", owner))
        self.assertEqual(p["identity"], {"platform": "gmail", "observed": {"account_email": owner}})
        self.assertEqual(self.verdict("gmail", p, owner), "ok")
        self.assertEqual(self.verdict("gmail", p, "other.sender@example.com"), "mismatch")
        p = self.probe("login_gmail_signin")
        self.assertEqual((p["state"], p["identity"]), ("logged_out", None))
        self.assertEqual(self.verdict("gmail", p, owner), "logged_out")
        p = self.probe("login_gmail_verify")
        self.assertEqual(p["state"], "checkpoint")
        self.assertEqual(self.verdict("gmail", p, owner), "checkpoint")

    def test_linkedin_and_boards(self):
        p = self.probe("login_linkedin_feed")
        self.assertEqual((p["site"], p["state"], p["feed_loaded"], p["nav_name"]), ("linkedin", "ok", True, "Sam Owner"))
        self.assertEqual(self.verdict("linkedin", p), "ok")
        p = self.probe("login_linkedin_checkpoint")
        self.assertEqual(p["state"], "checkpoint")
        self.assertEqual(self.verdict("linkedin", p), "checkpoint")
        p = self.probe("login_naukri_logged_out")
        self.assertEqual((p["site"], p["state"]), ("naukri", "logged_out"))
        self.assertEqual(self.verdict("naukri", p), "logged_out")


if __name__ == "__main__":
    unittest.main()
