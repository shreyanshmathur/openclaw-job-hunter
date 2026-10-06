"""U1: the password store (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.9): the Keychain value never in an argv, the 0600 file
fallback and its refusals, Secret never shows itself. Uses the fake `security` (tests/fakes/u1/fake_security.py)."""
from __future__ import annotations

import os
import pickle
import unittest

import tests  # noqa: F401
from jobhunter import secretstore
from jobhunter.secretstore import Secret, StoreError
from tests.fakes.u1.fake_security import FakeSecurity
from tests.helpers import HomeTestCase

PW = "Kx7#fixture#Pw2q"
HOST = "kestrel.wd5.myworkdayjobs.com"
EMAIL = "sam.lee@example.com"


class TestSecret(unittest.TestCase):
    def test_never_shows_itself(self):
        s = Secret(PW)
        for text in (repr(s), str(s), "%s" % s, "{}".format(s), "%r" % [s]):
            self.assertNotIn(PW, text)
        with self.assertRaises(TypeError):
            pickle.dumps(s)
        with self.assertRaises(AttributeError):
            s.x = 1
        self.assertEqual(len(s), len(PW))
        self.assertEqual(s.reveal(), PW)


class TestKeychain(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.sec = FakeSecurity()
        secretstore.set_runner(self.sec)
        secretstore.set_backend("keychain")
        self.addCleanup(secretstore.set_runner, None)
        self.addCleanup(secretstore.set_backend, None)

    def test_value_only_on_stdin(self):
        self.assertEqual(secretstore.put(HOST, EMAIL, Secret(PW)), "keychain")
        self.assertEqual(secretstore.get(HOST, EMAIL), Secret(PW))
        self.assertTrue(secretstore.present(HOST, EMAIL))
        for argv in self.sec.argv_log:
            self.assertNotIn(PW, " ".join(argv))
        self.assertTrue(any(PW in s for s in self.sec.stdin_log))
        svc, acct = list(self.sec.items)[0]
        self.assertEqual(svc, "openclaw-job-hunter.%s.ats" % secretstore._install_id())
        self.assertEqual(acct, "%s|%s" % (HOST, EMAIL))
        self.assertTrue(secretstore.ref(HOST, EMAIL).endswith(acct))
        self.assertNotIn(PW, secretstore.ref(HOST, EMAIL))
        self.assertTrue(secretstore.delete(HOST, EMAIL))
        self.assertIsNone(secretstore.get(HOST, EMAIL))
        self.assertFalse(secretstore.delete(HOST, EMAIL))

    def test_locked_keychain(self):
        self.sec.locked = True
        with self.assertRaises(StoreError) as cm:
            secretstore.put(HOST, EMAIL, Secret(PW))
        self.assertEqual(cm.exception.kind, "unavailable")
        self.assertNotIn(PW, str(cm.exception))
        with self.assertRaises(StoreError):
            secretstore.get(HOST, EMAIL)

    def test_put_needs_a_secret(self):
        with self.assertRaises(TypeError):
            secretstore.put(HOST, EMAIL, PW)


class TestFile(HomeTestCase):
    def setUp(self):
        super().setUp()
        secretstore.set_backend("file")
        self.addCleanup(secretstore.set_backend, None)

    def test_mode_and_round_trip(self):
        secretstore.put(HOST, EMAIL, Secret(PW))
        path = secretstore.file_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(secretstore.get(HOST, EMAIL), Secret(PW))
        self.assertTrue(secretstore.delete(HOST, EMAIL))
        self.assertIsNone(secretstore.get(HOST, EMAIL))

    def test_refusals(self):
        secretstore.put(HOST, EMAIL, Secret(PW))
        path = secretstore.file_path()
        os.chmod(path, 0o644)
        with self.assertRaises(StoreError) as cm:
            secretstore.get(HOST, EMAIL)
        self.assertEqual(cm.exception.kind, "refused")
        os.chmod(path, 0o600)
        os.rename(path, path + ".real")
        os.symlink(path + ".real", path)
        with self.assertRaises(StoreError):
            secretstore.get(HOST, EMAIL)
        with self.assertRaises(StoreError):
            secretstore.put(HOST, EMAIL, Secret(PW))
        self.assertEqual(secretstore.availability("file")["available"], False)


if __name__ == "__main__":
    unittest.main()
