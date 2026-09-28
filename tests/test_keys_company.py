"""Company keys (design 2.4.2, B3): alphanumeric name key, loose key, registrable domain and label, ATS
tenant; free-mail and hosting domains give no key; the bundled agency list."""
from __future__ import annotations

import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import keys

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "core", "keys_company.json")

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
    return {k for k, _ in keys.company_keys(**inp)}


class TestCompanyKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = load_fixture()

    def test_groups_are_connected(self):
        for group in self.fx["one_company"]:
            sets = [keyset(i) for i in group["inputs"]]
            with self.subTest(group=group["name"]):
                self.assertTrue(all(sets))
                # every input shares a key with the first one (resolve merges them into one row)
                for s in sets[1:]:
                    self.assertTrue(s & sets[0], (group["name"], s, sets[0]))

    def test_no_domain_key(self):
        for value in self.fx["no_domain_key"]:
            with self.subTest(value=value):
                self.assertEqual(keys.company_keys(domain=value), [])
                self.assertIsNone(keys.company_domain(value))

    def test_distinct_names(self):
        for a, b in self.fx["distinct"]:
            self.assertFalse(keyset(a) & keyset(b), (a, b))

    def test_key_kinds_and_rules(self):
        self.assertEqual(keys.company_keys("Kestrel Labs"), [("id:kestrellabs", "name"), ("id:kestrel", "loose")])
        self.assertEqual(keys.company_keys(domain="jobs.example"), [("dom:jobs.example", "dom")])   # generic label
        self.assertEqual(keys.company_keys(domain="abc.example"), [("dom:abc.example", "dom")])    # label too short
        self.assertEqual(keys.company_keys(ats="lever", tenant="hr"), [("ats:lever:hr", "ats")])
        self.assertEqual(keys.norm_name("The Kestrel Co."), "kestrel")
        self.assertEqual(keys.norm_name("Kestrel Commerce Inc."), "kestrelcommerce")
        self.assertEqual(keys.registrable_domain("careers.kestrel.co.in"), "kestrel.co.in")
        self.assertEqual(keys.registrable_domain("person@mail.kestrel.example"), "kestrel.example")
        self.assertIsNone(keys.registrable_domain("co.uk"))

    def test_agency_list(self):
        for name in self.fx["agencies"]:
            with self.subTest(name=name):
                self.assertTrue(keys.is_agency_name(name))
        self.assertFalse(keys.is_agency_name("Kestrel Commerce"))

    def test_agency_loose_key_only_for_qualifiers(self):
        # "hudson" and "antal" are listed agencies; a descriptive noise word makes another company
        for name in ("Hudson Labs", "Antal Technologies", "Hudson AI"):
            with self.subTest(name=name):
                self.assertFalse(keys.is_agency_name(name))
        for name in ("Hudson", "Randstad India", "Hudson Global"):
            with self.subTest(name=name):
                self.assertTrue(keys.is_agency_name(name))

    def test_dotted_and_long_legal_suffixes(self):
        want = [("id:kestrellabs", "name"), ("id:kestrel", "loose")]
        for name in ("Kestrel Labs K.K.", "Kestrel Labs S.A.", "Kestrel Labs, L.L.C.", "Kestrel Labs Incorporated",
                     "Kestrel Labs N.V.", "Kestrel Labs S.A.S.", "Kestrel Labs Pvt. Ltd.", "Kestrel Labs, Inc.",
                     "Kestrel Labs S.A"):
            with self.subTest(name=name):
                self.assertEqual(keys.company_keys(name), want)
        self.assertEqual(keys.company_keys("Tidemark Labs S.A."), [("id:tidemarklabs", "name"), ("id:tidemark", "loose")])
        self.assertEqual(keys.norm_name("K.C. Retail"), "kcretail")


if __name__ == "__main__":
    unittest.main()
