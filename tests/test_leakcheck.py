"""tools/leakcheck.py (U7): built-in rules, the private denylist from private/ and from an outside file."""
from __future__ import annotations

import json
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import paths

sys.path.insert(0, os.path.join(paths.REPO, "tools"))
import leakcheck  # noqa: E402


def found(line: str, rel: str = "docs/x.md") -> list[str]:
    return [r for _, r, _ in leakcheck.scan_line(rel, line)]


class TestBuiltins(unittest.TestCase):
    def test_emails(self):
        for ok in ("alex.rivera@example.com", "a@mail.example.org", "you@kestrel.example", "T123@jobhunter.invalid",
                   "noreply@anthropic.com", "x@host.test"):
            self.assertEqual(found("write to %s now" % ok), [], ok)
        self.assertEqual(found("write to someone@" + "realcompany.io"), ["L-EMAIL"])
        self.assertEqual(found("pip install openclaw@2026.9.5"), [])

    def test_phones(self):
        self.assertEqual(found("call +91" + "9876543210"), ["L-PHONE"])
        self.assertEqual(found("call (415) 867" + "-5309"), ["L-PHONE"])
        for ok in ("+10000000000", "+910000000000", "555-0100", "(555) 555-0123", "2026-09-27T05:00:00Z",
                   "version 2026.9.5", "id 1234567890123456"):
            self.assertEqual(found("n %s" % ok), [], ok)

    def test_google_and_linkedin_urls(self):
        real_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        self.assertEqual(found("https://docs.google.com/spreadsheets/d/%s/edit" % real_id), ["L-GDOC"])
        self.assertEqual(found("https://drive.google.com/file/d/%s/view" % real_id), ["L-GDOC"])
        self.assertEqual(found("https://script.google.com/macros/s/AKfycb%s/exec" % real_id), ["L-APPSCRIPT"])
        self.assertEqual(found("https://script.google.com/macros/s/<deployment-id>/exec"), [])
        self.assertEqual(found("https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID_EXAMPLE_VALUE/edit"), [])
        self.assertEqual(found("https://www.linkedin.com/in/" + "jane-doe-4a1b2c"), ["L-LINKEDIN"])
        for ok in ("https://www.linkedin.com/in/example-person", "https://www.linkedin.com/in/your-handle",
                   "https://www.linkedin.com/in/<handle>"):
            self.assertEqual(found(ok), [], ok)

    def test_secrets_and_hex(self):
        self.assertEqual(found("key sk-" + "ant-api03-Q7r2Z9x4W8v3T6y1"), ["L-SECRET"])
        self.assertEqual(found("AKIA" + "Q7R2Z9X4W8V3T6Y1"), ["L-SECRET"])
        self.assertEqual(found("token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2"), ["L-SECRET"])
        self.assertEqual(found("xoxb-" + "1234567890-abcdef"), ["L-SECRET"])
        hexs = "3f" * 31 + "a9"
        self.assertEqual(found("secret %s" % hexs), ["L-HEX64"])
        self.assertEqual(found('"sha256": "%s"' % hexs), [])
        self.assertEqual(found('"detect_page.js": "%s"' % hexs, rel="drivers/manifest.json"), [])
        self.assertEqual(found("zero %s" % ("0" * 64)), [])
        self.assertEqual(found("secret %s leakcheck: ignore" % hexs), [])

    def test_email_finder_provider_keys(self):
        # a real-looking value after a provider header or parameter name is refused; FAKE- fixture keys pass
        real = "Zq8LmN3pQr7s" + "Tu2vWx9y"
        for line in ("X-KEY: %s" % real, '"X-API-KEY": "%s"' % real, "x-api-key=%s" % real,
                     '"X-Tomba-Key": "%s"' % real, "X-Tomba-Secret: %s" % real, "apiKey: %s" % real,
                     "GET /v2/email-finder?domain=kestrel.example&api_key=%s" % real,
                     "Authorization: Bearer %s" % real, '{"Authorization": "Bearer %s"}' % real):
            self.assertEqual(found(line), ["L-PROVKEY"], line)
        for ok in ('"X-KEY": "FAKE-KEY-0000000000000000"', "X-Tomba-Secret=FAKE-SECRET-000000000000",
                   "Authorization: Bearer FAKE-KEY-0000000000000000", "api_key=%s", "api_key={key}",
                   "X-KEY: short1234", 'secret_headers=(("X-KEY", "api_key", ""),)',
                   'ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"', '--anthropic-api-key "$KEY"',
                   "header X-KEY with your own key", "X-KEY: %s leakcheck: ignore" % real):
            self.assertEqual(found(ok), [], ok)
        # the finding never shows any part of the value
        text = " ".join(x for _, _, x in leakcheck.scan_line("docs/x.md", "api_key=%s" % real))
        self.assertNotIn(real[:6], text)
        self.assertIn("api_key", text)

    def test_app_password(self):
        self.assertEqual(found("app password: qwrt " + "yupl mnbv cxzk"), ["L-APPPW"])
        self.assertEqual(found("app_password=qwrtyupl" + "mnbvcxzk"), ["L-APPPW"])
        self.assertEqual(found("app password: abcd efgh ijkl mnop"), [])
        self.assertEqual(found("when they have been here"), [])


class TestPrivateDenylist(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="jh-leak-")
        self.outside = tempfile.mkdtemp(prefix="jh-deny-")
        os.makedirs(os.path.join(self.repo, "private", "resume"))
        cfg = {"owner": {"first_name": "Quillon", "last_name": "Marrowby", "gmail_address": "qmarrowby@" + "gmail.com",
                         "signature": {"full_name": "Quillon Marrowby", "phone": "+44 7700" + " 900123"},
                         "notify": {"channel": "whatsapp", "to": "+10000000000"}},
               "timezone": "Europe/London"}
        with open(os.path.join(self.repo, "private", "config.json"), "w") as fh:
            json.dump(cfg, fh)
        with open(os.path.join(self.repo, "private", "resume", "base.json"), "w") as fh:
            json.dump({"experience": [{"company": "Zentrovia Labs", "title": "Analyst", "name": "Python"}],
                       "education": [{"university": "Harlowfield University"}]}, fh)
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("Built by quillon.\nWorked at Zentrovia Labs.\nPython is fine.\nCall 4477" + "00900123.\n"
                     "Analyst roles.\nNothing here.\n")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_terms_from_private(self):
        terms = leakcheck.private_terms(self.repo, env={})
        for t in ("quillon", "marrowby", "qmarrowby@" + "gmail.com", "qmarrowby", "zentrovia labs",
                  "harlowfield university", "447700900123"):
            self.assertIn(t, terms)
        self.assertNotIn("python", terms)       # skill names are not personal data
        self.assertNotIn("analyst", terms)
        self.assertNotIn("10000000000", terms)  # placeholder phone
        hits = leakcheck.check_file(self.repo, "README.md", terms)
        self.assertEqual(sorted(int(h.split(":")[1]) for h in hits), [1, 2, 4])
        for h in hits:
            self.assertIn("L-PRIVATE private denylist term #", h)
            self.assertNotIn("quillon", h.lower())   # the term itself is never printed

    def test_outside_denylist_file(self):
        deny = os.path.join(self.outside, "deny.txt")
        with open(deny, "w") as fh:
            fh.write("# my own data\nNothing here\n")
        terms = leakcheck.private_terms(self.repo, env={leakcheck.ENV_DENYLIST: deny})
        self.assertIn("nothing here", terms)
        self.assertTrue(any(":6:" in h for h in leakcheck.check_file(self.repo, "README.md", terms)))
        inside = os.path.join(self.repo, "deny.txt")
        with open(inside, "w") as fh:
            fh.write("x\n")
        with self.assertRaises(SystemExit):
            leakcheck.private_terms(self.repo, env={leakcheck.ENV_DENYLIST: inside})

    def test_large_denylist_is_fast(self):
        # more terms than the re module caches (512): each pattern must be compiled once per run, not per line
        deny = os.path.join(self.outside, "deny.txt")
        words = ["zq%04dvex" % i for i in range(700)]
        with open(deny, "w") as fh:
            fh.write("\n".join(words) + "\nNothing here\n")
        lines = ["Line %d of an ordinary page about setup, config and zq0005vex checks." % i for i in range(400)]
        lines += ["Nothing here", "zq0699vex and zq0001vex"]
        with open(os.path.join(self.repo, "BIG.md"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        terms = leakcheck.private_terms(self.repo, env={leakcheck.ENV_DENYLIST: deny})
        self.assertGreater(len(terms), 700)
        leakcheck._TERM_RX.clear()
        start = time.monotonic()
        hits = leakcheck.check_file(self.repo, "BIG.md", terms)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3.0, "leakcheck recompiles denylist patterns per line")
        self.assertEqual(len(hits), 400 + 1 + 2)
        # a second pass compiles nothing new
        calls = []
        real = re.compile
        with mock.patch.object(leakcheck.re, "compile", side_effect=lambda *a, **k: calls.append(a) or real(*a, **k)):
            leakcheck.check_file(self.repo, "BIG.md", terms)
        self.assertEqual(calls, [])

    def test_word_boundaries_kept(self):
        terms = ["zentrovia", "ann lee"]
        self.assertEqual(leakcheck.scan_private("at Zentrovia, then", terms), [(4, 1)])
        self.assertEqual(leakcheck.scan_private("zentrovias and xzentrovia", terms), [])
        self.assertEqual(leakcheck.scan_private("met Ann Lee today; joanne leel", terms), [(5, 2)])
        self.assertEqual(leakcheck.scan_private("ann lee at zentrovia", terms), [(12, 1), (1, 2)])  # term order
        deny = leakcheck.Denylist(["zentrovia", ".acme-x", "1234567"])
        self.assertEqual(deny.scan("see .acme-x now, call 123-4567"), [(5, 2), (1, 3)])
        self.assertEqual(deny.scan("see x.acme-xy"), [])
        self.assertEqual(deny.scan("zentrovia leakcheck: ignore"), [])

    def test_main_exit_codes(self):
        clean = os.path.join(self.repo, "clean.md")
        with open(clean, "w") as fh:
            fh.write("Alex Rivera at Kestrel Commerce, alex.rivera@example.com\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(leakcheck.main(["--repo", self.repo, clean], env={}), 0)
            self.assertEqual(leakcheck.main(["--repo", self.repo, os.path.join(self.repo, "README.md")], env={}), 1)



class TestMoreBuiltins(unittest.TestCase):
    def test_grouped_international_phones(self):
        for bad in ("+91 98765" + " 43210", "+91-98765" + "43210", "+44 7700" + " 900123", "(+91) x +91 9876" + "5 43210"):
            self.assertIn("L-PHONE", found("call %s today" % bad), bad)
        self.assertEqual(found("call +91 98765" + " 43210 now").count("L-PHONE"), 1)   # one finding per number
        for ok in ("+91 00000 00000", "+91-0000000000", "+1 2026 09", "v+1.2.3", "+5 1234567"):
            self.assertEqual(found("n %s" % ok), [], ok)

    def test_apps_script_and_google_id_url_forms(self):
        rid = "AKfycb" + "Q7r2Z9x4W8v3T6y1Lm0Np5Rs8Tu"
        sid = "1AbCdEfGhIjKlMnOpQrSt" + "UvWxYz0123456789"
        for url in ("https://script.google.com/a/macros/kestrel.example/s/%s/exec" % rid,
                    "https://script.google.com/macros/s/%s/dev" % rid,
                    "https://script.google.com/a/kestrel.example/macros/s/%s/exec" % rid,
                    "https://script.google.com/d/%s/edit" % sid,
                    "https://script.google.com/home/projects/%s/edit" % sid):
            self.assertEqual(found(url), ["L-APPSCRIPT"], url)
        for url in ("https://docs.google.com/spreadsheets/u/0/d/%s/edit" % sid,
                    "https://docs.google.com/spreadsheets/d/e/%s/pubhtml" % sid,
                    "https://drive.google.com/uc?id=%s&export=download" % sid,
                    "https://drive.google.com/uc?export=download&id=%s" % sid,
                    "https://drive.google.com/drive/u/1/folders/%s" % sid):
            self.assertEqual(found(url), ["L-GDOC"], url)
        for ok in ("https://script.google.com/a/macros/<domain>/s/<deployment-id>/exec",
                   "https://script.google.com/macros/s/<id>/dev", "https://docs.google.com/spreadsheets/u/0/d/<id>/edit",
                   "https://drive.google.com/uc?id=<id>", "https://script.google.com/home/start"):
            self.assertEqual(found(ok), [], ok)

    def test_utf16_files_are_scanned(self):
        d = tempfile.mkdtemp(prefix="jh-u16-")
        try:
            text = "name,contact\nSam,sam.q@" + "realmail.io\nSam,(415) 867" + "-5309\n"
            for enc in ("utf-16", "utf-16-le", "utf-16-be"):
                with open(os.path.join(d, "contacts.txt"), "wb") as fh:
                    fh.write(text.encode(enc))
                hits = leakcheck.check_file(d, "contacts.txt", [])
                self.assertEqual(sorted(h.split(" ")[1] for h in hits), ["L-EMAIL", "L-PHONE"], enc)
            with open(os.path.join(d, "blob.bin"), "wb") as fh:
                fh.write(b"\x00\x01\x02sam.q@" + b"realmail.io\x00\xff\x00\x10")
            self.assertEqual(leakcheck.check_file(d, "blob.bin", []), [])      # real binary data stays skipped
        finally:
            shutil.rmtree(d)


class TestDenylistVariants(unittest.TestCase):
    """Fictional private/ tree: separator variants, short names, national phone forms and profile links."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="jh-leak2-")
        self.outside = tempfile.mkdtemp(prefix="jh-deny2-")
        os.makedirs(os.path.join(self.repo, "private", "resume"))
        cfg = {"owner": {"first_name": "Eve", "last_name": "Quorrin", "gmail_address": "you@example.com",
                         "signature": {"full_name": "Eve Quorrin", "phone": "",
                                       "links": ["https://github.com/" + "evequorrin-dev",
                                                 "https://www.linkedin.com/in/your-handle"]},
                         "notify": {"channel": "whatsapp", "to": "+10000000000"}}}
        self.write("private/config.json", cfg)
        self.write("private/resume/base.json", {
            "contact": {"name": "Eve Quorrin", "email": "", "phone": "+91 98765" + " 43210",
                        "links": [{"label": "Portfolio", "url": "https://" + "quorrin-builds.dev/work"}]},
            "experience": [{"company": "Obsidrake Systems", "title": "Analyst", "name": "SQL"}],
            "education": [{"university": "Anna Pellworth Institute"}]})
        self.write("private/answers.json", {"version": 1, "answers": [
            {"key": "github_url", "value": "https://github.com/" + "eqlabs-studio"},
            {"key": "portfolio_url", "value": "https://" + "evequorrin.github.io/"},
            {"key": "current_city", "value": "Springfield"},
            {"key": "phone", "value": ""}]})
        self.write("private/profile.json", {"fields": {"contact": {"value": {"full_name": "Eve Q. Quorrin",
                                                                              "email": "eveq@" + "mailbox.io"}}}})

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def write(self, rel, obj):
        with open(os.path.join(self.repo, rel), "w") as fh:
            json.dump(obj, fh)

    def terms(self, env=None):
        return leakcheck.private_terms(self.repo, env=env or {})

    def test_sources_and_short_names(self):
        t = self.terms()
        for want in ("eve", "quorrin", "obsidrake systems", "anna pellworth institute", "919876543210", "9876543210",
                     "evequorrin-dev", "quorrin-builds.dev", "evequorrin.github.io", "evequorrin",
                     "eqlabs-studio", "eveq@" + "mailbox.io", "eveq"):
            self.assertIn(want, t)
        for never in ("your-handle", "10000000000", "0000000000", "you@example.com", "springfield", "sql", "analyst",
                      "github.com", "linkedin.com"):
            self.assertNotIn(never, t)

    def test_separator_variants(self):
        deny = leakcheck.Denylist(self.terms())
        for line in ("worked at Obsidrake-Systems", "worked at ObsidrakeSystems", "see obsidrake_systems",
                     "OBSIDRAKE.SYSTEMS", "obsidrake  systems"):
            self.assertTrue(deny.scan(line), line)
        self.assertEqual(deny.scan("obsidrakesystemsx and xobsidrake systems"), [])
        acme = leakcheck.Denylist(["acme corp"])
        for line in ("acme-corp", "acmecorp", "AcmeCorp", "Acme Corp", "acme_corp"):
            self.assertEqual(acme.scan("at %s now" % line), [(4, 1)], line)
        self.assertEqual(acme.scan("acmecorps and zacme corp"), [])
        self.assertEqual(leakcheck.Denylist(["a@" + "b.co"]).scan("mail a b co"), [])   # @ stays literal

    def test_national_phone_forms(self):
        deny = leakcheck.Denylist(self.terms())
        intl = "+91 98765" + " 43210"
        for line in ("call 98765 43210", "mobile: 98765" + "43210", intl, "0 98765-43210"):
            self.assertTrue(deny.scan(line), line)
        self.assertEqual(len(deny.scan(intl)), 1)                       # one finding, not two
        self.assertEqual(deny.scan("call 98765 4321"), [])

    def test_profile_links(self):
        deny = leakcheck.Denylist(self.terms())
        for line in ("https://github.com/" + "evequorrin-dev", "see quorrin-builds.dev", "EveQuorrin.GitHub.io",
                     "gh: eqlabs-studio"):
            self.assertTrue(deny.scan(line), line)

    def test_explicit_denylist_keeps_short_and_placeholder_like_terms(self):
        deny = os.path.join(self.outside, "deny.txt")
        with open(deny, "w") as fh:
            fh.write("# names\nAnna\nEve\nYourStory Media\nSampleson Ltd\nAl\n\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            t = leakcheck.private_terms(self.repo, env={leakcheck.ENV_DENYLIST: deny})
        for want in ("anna", "eve", "yourstory media", "sampleson ltd"):
            self.assertIn(want, t)
        self.assertNotIn("al", t)
        self.assertIn("line 6 is not used", err.getvalue())
        self.assertNotIn("Al", err.getvalue().replace("ENV", ""))   # the term itself is never printed
        d = leakcheck.Denylist(t)
        self.assertTrue(d.scan("YourStory-Media wrote"))
        self.assertTrue(d.scan("SamplesonLtd"))

    def test_private_placeholders_still_dropped(self):
        self.assertTrue(leakcheck._placeholder_term("your-handle"))
        self.assertTrue(leakcheck._placeholder_term("your name"))
        self.assertTrue(leakcheck._placeholder_term("xxxx"))
        for real in ("anna", "eve", "yourstory media", "sampleson ltd", "bob"):
            self.assertFalse(leakcheck._placeholder_term(real), real)


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env, check=True).stdout.decode()


@unittest.skipUnless(shutil.which("git"), "no git")
class TestStagedSnapshot(unittest.TestCase):
    """--staged reads the index: a staged leak is found after the working copy was edited or deleted."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="jh-staged-")
        self.env = dict(os.environ, HOME=self.repo, GIT_CONFIG_NOSYSTEM="1")
        self.env.pop("GIT_INDEX_FILE", None)
        self.env.pop("GIT_DIR", None)
        _git(self.repo, "init", "-q", env=self.env)
        self.leak = "contact sam.q@" + "realmail.io\n"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def put(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as fh:
            fh.write(text)

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {k: v for k, v in self.env.items() if k in ("HOME", "GIT_CONFIG_NOSYSTEM")}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = leakcheck.main(["--repo", self.repo] + list(args), env={})
        return rc, out.getvalue(), err.getvalue()

    def test_staged_then_edited(self):
        self.put("notes.md", self.leak)
        _git(self.repo, "add", "notes.md", env=self.env)
        self.put("notes.md", "redacted\n")
        self.assertEqual(self.run_main()[0], 0)                  # the working tree is clean
        rc, out, err = self.run_main("--staged")
        self.assertEqual(rc, 1)
        self.assertIn("notes.md:1:9: L-EMAIL", out)
        self.assertIn("1 staged file(s)", err)

    def test_staged_then_deleted(self):
        self.put("y.md", self.leak)
        self.put("ok.md", "fine\n")
        _git(self.repo, "add", "y.md", "ok.md", env=self.env)
        os.remove(os.path.join(self.repo, "y.md"))
        rc, out, _ = self.run_main("--staged")
        self.assertEqual(rc, 1)
        self.assertIn("y.md:1:9: L-EMAIL", out)
        rc, out, _ = self.run_main("--staged", os.path.join(self.repo, "ok.md"))
        self.assertEqual(rc, 0, out)

    def test_edit_fixed_but_not_restaged_still_fails(self):
        self.put("a.md", self.leak)
        _git(self.repo, "add", "a.md", env=self.env)
        self.put("a.md", "contact alex.rivera@example.com\n")
        self.assertEqual(self.run_main("--staged")[0], 1)
        _git(self.repo, "add", "a.md", env=self.env)
        self.assertEqual(self.run_main("--staged")[0], 0)

    def test_untracked_files_are_not_part_of_the_snapshot(self):
        self.put("scratch.md", self.leak)
        self.assertEqual(self.run_main("--staged")[0], 0)
        self.assertEqual(self.run_main()[0], 1)



@unittest.skipUnless(shutil.which("git") and os.path.exists("/bin/bash"), "no git or bash")
class TestPreCommitHook(unittest.TestCase):
    """The hook from tools/install_hooks.sh in a scratch repo: the staged snapshot is what gets checked."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jh-hook-")
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.repo, "tools"))
        for f in ("textcheck.py", "leakcheck.py", "check_gitignore.py", "install_hooks.sh"):
            shutil.copy2(os.path.join(paths.REPO, "tools", f), os.path.join(self.repo, "tools", f))
        with open(os.path.join(self.repo, "tools", "gen_drivers.py"), "w") as fh:
            fh.write("import sys\nsys.exit(0)\n")        # stand-in: the driver manifest is not under test here
        self.env = {"HOME": self.root, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com", "LANG": "C"}
        self.git("init", "-q")
        p = subprocess.run(["/bin/bash", os.path.join(self.repo, "tools", "install_hooks.sh")], env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.git("add", "tools")
        self.git("commit", "-q", "-m", "tools")
        self.leak = "contact sam.q@" + "realmail.io\n"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args, check=True):
        p = subprocess.run(["git", "-C", self.repo] + list(args), env=self.env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        if check and p.returncode != 0:
            raise AssertionError(p.stdout.decode())
        return p.returncode, p.stdout.decode()

    def put(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as fh:
            fh.write(text)

    def test_hook_uses_the_staged_snapshot(self):
        with open(os.path.join(self.repo, ".git", "hooks", "pre-commit")) as fh:
            hook = fh.read()
        self.assertIn("tools/leakcheck.py --staged", hook)
        self.assertIn("tools/textcheck.py --staged", hook)

    def test_staged_leak_edited_in_the_working_tree_is_refused(self):
        self.put("notes.md", self.leak)
        self.git("add", "notes.md")
        self.put("notes.md", "redacted\n")                  # the usual "fix" without git add
        rc, out = self.git("commit", "-m", "notes", check=False)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("L-EMAIL", out)
        self.assertIn("git add it again", out)
        self.assertNotEqual(self.git("rev-parse", "HEAD")[1], "")
        self.assertNotEqual(self.git("show", "HEAD:notes.md", check=False)[0], 0)   # nothing was committed
        self.git("add", "notes.md")
        rc, out = self.git("commit", "-q", "-m", "notes", check=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git("show", "HEAD:notes.md")[1], "redacted\n")

    def test_staged_then_deleted_is_refused(self):
        self.put("y.md", self.leak)
        self.git("add", "y.md")
        os.remove(os.path.join(self.repo, "y.md"))
        rc, out = self.git("commit", "-m", "y", check=False)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("y.md:1:9: L-EMAIL", out)

    def test_commit_all_checks_the_temporary_index(self):
        self.put("c.md", "fine\n")
        self.git("add", "c.md")
        self.git("commit", "-q", "-m", "c")
        self.put("c.md", self.leak)                         # modified, not staged: commit -a stages it
        rc, out = self.git("commit", "-a", "-m", "c2", check=False)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("c.md:1:9: L-EMAIL", out)
        rc, out = self.git("commit", "-m", "c3", "c.md", check=False)   # commit <path> uses a temporary index too
        self.assertNotEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
