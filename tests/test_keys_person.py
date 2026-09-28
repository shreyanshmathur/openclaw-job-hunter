"""Person keys (design 2.4.3, M2): email and Gmail-folded keys, LinkedIn slug, opaque, legacy and Sales
Navigator ids (needs_vanity), pname name-at-company key; lnkd.in refused."""
from __future__ import annotations

import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import keys
from jobhunter.errors import Denied

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "core", "keys_person.json")

# The fixture spells free-mail domains and LinkedIn profile paths as placeholders so the repo holds no
# address or profile URL that the leak check would flag; they are filled in here at run time.
PLACEHOLDERS = {"{gmail}": "gmail.com", "{googlemail}": "googlemail.com", "{li_in}": "linkedin.com/in"}


def load_fixture() -> dict:
    with open(FIX, encoding="utf-8") as fh:
        text = fh.read()
    for k, v in PLACEHOLDERS.items():
        text = text.replace(k, v)
    return json.loads(text)



def keyset(inp: dict) -> set:
    return {k for k, _ in keys.person_keys(**inp)}


class TestPersonKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = load_fixture()

    def test_same_person(self):
        for group in self.fx["same_person"]:
            sets = [keyset(i) for i in group["inputs"]]
            with self.subTest(group=group["name"]):
                common = set.intersection(*sets)
                self.assertTrue(common, sets)

    def test_different(self):
        for a, b in self.fx["different"]:
            self.assertFalse(keyset(a) & keyset(b), (a, b))

    def test_opaque_ids(self):
        kinds = [keys.person_keys(linkedin_url=u)[0][1] for u in self.fx["needs_vanity"]]
        self.assertEqual(kinds, ["li_member", "li_legacy", "li_sales"])
        self.assertEqual(keys.person_keys(linkedin_url=self.fx["needs_vanity"][0])[0][0], "li_member:ACoAABcdEfGh123")

    def test_rejected(self):
        for value in self.fx["rejected"]:
            with self.subTest(value=value):
                with self.assertRaises(Denied) as cm:
                    if "@" in value and "://" not in value:
                        keys.person_keys(email=value)
                    else:
                        keys.person_keys(linkedin_url=value)
                self.assertEqual(cm.exception.code, "E_VALIDATION")

    def test_pname_rules(self):
        self.assertEqual(keys.pname_key("Ms. A. Rivera", "KAAAAAAA"), None)
        self.assertEqual(keys.pname_key("Jose Garcia Lopez", "KAAAAAAA"), "pname:jose.lopez@KAAAAAAA")
        self.assertEqual(keys.pname_key("J\u00f6rg M\u00fcller", "KAAAAAAA"), "pname:jorg.muller@KAAAAAAA")
        self.assertIsNone(keys.pname_key("Alex Rivera", None))
        self.assertEqual(keys.email_keys("Alex.Rivera+jobs@Kestrel.Example"),
                         [("email:alex.rivera+jobs@kestrel.example", "email"),
                          ("email_norm:alex.rivera@kestrel.example", "email_norm")])

    def test_pname_ignores_linkedin_name_trailers(self):
        # degrees, pronouns, generational suffixes and headline words after the name keep one person
        want = "pname:jamie.quill@KAAAAAAA"
        for name in ("Jamie Quill", "Jamie Quill, MBA", "Jamie Quill (She/Her)", "Jamie Quill PhD", "Jamie Quill Jr.",
                     "Jamie Quill - Hiring!", "Jamie Quill | Talent Partner", "Dr. Jamie Quill MBA PhD",
                     "Jamie Quill, CFA, FRM", "Jamie Quill \u2013 Recruiter"):
            with self.subTest(name=name):
                self.assertEqual(keys.pname_key(name, "KAAAAAAA"), want)
        self.assertEqual(keys.pname_key("Mary-Jane Quill", "KAAAAAAA"), "pname:mary.quill@KAAAAAAA")
        self.assertIsNone(keys.pname_key("Quill, MBA", "KAAAAAAA"))


if __name__ == "__main__":
    unittest.main()
