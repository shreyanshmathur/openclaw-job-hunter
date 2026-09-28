"""U10 key store: keychain backend through an injected runner (the key never appears in any argv), file
backend (0600, owner, no symlinks, atomic write), Secret redaction. Fake keys only."""
from __future__ import annotations

import json
import os
import pickle
import unittest

import tests  # noqa: F401
from jobhunter import paths
from jobhunter.enrich import keystore
from tests.fakes.u10 import FAKE_KEY, FAKE_SECRET, FIXTURES, install_guard, remove_guard
from tests.helpers import TempHome

with open(os.path.join(FIXTURES, "security", "transcripts.json"), "r", encoding="ascii") as _fh:
    T = json.load(_fh)


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class Runner:
    """Records argv and stdin; answers from the security transcripts."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, input_text):
        self.calls.append((list(argv), input_text))
        t = T[self.answers.pop(0)] if self.answers else T["find_absent"]
        return t["rc"], t["stdout"], t["stderr"]


class Base(unittest.TestCase):
    def setUp(self):
        self.home = TempHome(init_db=False).start()
        keystore.use_test_backend(None)

    def tearDown(self):
        keystore.set_runner(None)
        keystore.set_backend(None)
        self.home.stop()


class TestSecret(unittest.TestCase):
    def test_redacted(self):
        s = keystore.Secret(FAKE_KEY)
        for text in (repr(s), str(s), "%s" % s, "{}".format(s), f"{s}", repr([s]), repr({"k": s})):
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("FAKE", text)
        self.assertEqual(s.reveal(), FAKE_KEY)
        with self.assertRaises(TypeError):
            pickle.dumps(s)
        with self.assertRaises(AttributeError):
            s._v = "x"
        self.assertEqual(hash(s), hash(keystore.Secret("other-value-000000")))   # nothing about the value leaks


class TestKeychain(Base):
    def setUp(self):
        super().setUp()
        keystore.set_backend("keychain")

    def test_store_reads_key_from_stdin_never_argv(self):
        r = Runner(["add_ok", "add_ok"])
        keystore.set_runner(r)
        self.assertEqual(keystore.store("tomba", {"key": FAKE_KEY, "secret": FAKE_SECRET}), "keychain")
        self.assertEqual(len(r.calls), 2)
        for argv, stdin in r.calls:
            self.assertEqual(argv, [keystore.SECURITY, "-i"])
            self.assertNotIn(FAKE_KEY, " ".join(argv))
            self.assertNotIn(FAKE_SECRET, " ".join(argv))
            self.assertTrue(stdin.startswith("add-generic-password -U -s openclaw-job-hunter.enrich.tomba."))
        self.assertIn(FAKE_KEY, r.calls[0][1])
        self.assertIn(FAKE_SECRET, r.calls[1][1])

    def test_get_found_absent_and_locked(self):
        r = Runner(["find_ok"])
        keystore.set_runner(r)
        got = keystore.get("hunter")
        self.assertEqual(got["api_key"].reveal(), FAKE_KEY.strip())
        argv = r.calls[0][0]
        self.assertEqual(argv[:2], [keystore.SECURITY, "find-generic-password"])
        self.assertIn("openclaw-job-hunter.enrich.hunter.api_key", argv)
        self.assertEqual(argv[-1], "-w")
        self.assertIsNone(r.calls[0][1])
        keystore.set_runner(Runner(["find_absent"]))
        self.assertIsNone(keystore.get("hunter"))
        self.assertFalse(keystore.present("hunter"))
        keystore.set_runner(Runner(["find_locked"]))
        with self.assertRaises(keystore.KeystoreError) as cm:
            keystore.get("hunter")
        self.assertEqual(cm.exception.kind, "unavailable")
        keystore.set_runner(Runner(["find_locked"]))
        self.assertFalse(keystore.present("hunter"))

    def test_status_does_not_read_the_secret(self):
        r = Runner(["find_ok"])
        keystore.set_runner(r)
        st = keystore.status("hunter")
        self.assertEqual(st, {"key": True, "backend": "keychain", "error": None})
        self.assertNotIn("-w", r.calls[0][0])
        keystore.set_runner(Runner(["find_locked"]))
        self.assertEqual(keystore.status("hunter")["error"], "unavailable")

    def test_store_refused_and_delete(self):
        keystore.set_runner(Runner(["add_refused"]))
        with self.assertRaises(keystore.KeystoreError):
            keystore.store("hunter", {"api_key": FAKE_KEY})
        r = Runner(["delete_ok"])
        keystore.set_runner(r)
        self.assertTrue(keystore.delete("hunter"))
        self.assertEqual(r.calls[0][0][1], "delete-generic-password")
        keystore.set_runner(Runner(["find_absent"]))
        self.assertFalse(keystore.delete("hunter"))

    def test_prompt_fallback_argv_ends_with_w(self):
        seen = []
        keystore.store_prompt("hunter", runner=lambda argv: seen.append(argv) or 0)
        self.assertEqual(seen[0][-1], "-w")
        self.assertNotIn(FAKE_KEY, " ".join(seen[0]))

    def test_validation(self):
        from jobhunter.errors import Denied
        for bad in ("short", "has space in it 000000", "semi;colon000000000", "", "x" * 201):
            with self.subTest(bad=bad):
                with self.assertRaises(Denied):
                    keystore.validate("hunter", {"api_key": bad})
        with self.assertRaises(Denied):
            keystore.validate("tomba", {"key": FAKE_KEY})


class TestFileBackend(Base):
    def setUp(self):
        super().setUp()
        keystore.set_backend("file")

    def test_roundtrip_mode_600_atomic(self):
        self.assertIsNone(keystore.get("hunter"))
        self.assertEqual(keystore.store("hunter", {"api_key": FAKE_KEY}), "file")
        keystore.store("tomba", {"key": FAKE_KEY, "secret": FAKE_SECRET})
        path = keystore.key_file()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(paths.private_dir()).count("enrich_keys.json"), 1)
        self.assertFalse([f for f in os.listdir(paths.private_dir()) if ".tmp." in f])
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(sorted(doc["keys"]), ["hunter", "tomba"])
        self.assertEqual(keystore.get("tomba")["secret"].reveal(), FAKE_SECRET)
        self.assertTrue(keystore.delete("hunter"))
        self.assertIsNone(keystore.get("hunter"))
        self.assertEqual(keystore.file_mode_ok()[0], True)

    def test_wrong_mode_refused(self):
        keystore.store("hunter", {"api_key": FAKE_KEY})
        os.chmod(keystore.key_file(), 0o644)
        with self.assertRaises(keystore.KeystoreError) as cm:
            keystore.get("hunter")
        self.assertEqual(cm.exception.kind, "refused")
        self.assertFalse(keystore.present("hunter"))
        with self.assertRaises(keystore.KeystoreError):
            keystore.store("prospeo", {"api_key": FAKE_KEY})   # never silently overwritten
        self.assertFalse(keystore.file_mode_ok()[0])

    def test_symlink_refused(self):
        real = os.path.join(paths.private_dir(), "elsewhere.json")
        fd = os.open(real, os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": 1, "keys": {"hunter": {"api_key": FAKE_KEY}}}, fh)
        os.symlink(real, keystore.key_file())
        with self.assertRaises(keystore.KeystoreError):
            keystore.get("hunter")

    def test_wrong_owner_refused(self):
        keystore.store("hunter", {"api_key": FAKE_KEY})
        real_getuid = os.getuid
        try:
            os.getuid = lambda: real_getuid() + 1
            with self.assertRaises(keystore.KeystoreError):
                keystore.get("hunter")
        finally:
            os.getuid = real_getuid

    def test_never_from_environment(self):
        os.environ["HUNTER_API_KEY"] = FAKE_KEY
        try:
            self.assertIsNone(keystore.get("hunter"))
        finally:
            del os.environ["HUNTER_API_KEY"]


class TestBackendChoice(Base):
    def test_auto(self):
        import sys
        from jobhunter.enrich import settings
        settings.use_test_overrides({"key_store": "file"})
        try:
            self.assertEqual(keystore.backend(), "file")
            settings.use_test_overrides({"key_store": "auto"})
            want = "keychain" if (sys.platform == "darwin" and os.path.exists(keystore.SECURITY)) else "file"
            self.assertEqual(keystore.backend(), want)
        finally:
            settings.use_test_overrides(None)


if __name__ == "__main__":
    unittest.main()
