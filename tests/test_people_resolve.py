"""people.resolve (design 2.4.3, M2): one contact per person across email, Gmail folding, +tags, LinkedIn
slug, opaque member ids and name at a company; merges keep person-level dedup; split undoes."""
from __future__ import annotations

import tests  # noqa: F401
from jobhunter import canon, companies, db, gate, keys, people
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase, insert_action

# joined at run time so the repo holds no free-mail address or real-looking profile URL
GMAIL, GOOGLEMAIL = "gmail.com", "googlemail.com"
LI_IN = "https://www.linkedin.com/in/"


class PeopleCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        with db.tx(self.conn):
            self.co = companies.resolve(self.conn, name="Kestrel Commerce", domain="kestrel.example", source="job_board")
        self.co_uid = self.conn.execute("SELECT company_uid FROM companies WHERE id = ?", (self.co,)).fetchone()[0]

    def resolve(self, email=None, linkedin_url=None, full_name=None, **fields):
        pk = keys.person_keys(email=email, linkedin_url=linkedin_url, full_name=full_name,
                              company_uid=self.co_uid if full_name else None)
        f = dict(fields, full_name=full_name, company_id=self.co, email=keys.normalize_email(email) if email else None,
                 linkedin_url=linkedin_url)
        with db.tx(self.conn):
            return people.resolve(self.conn, keys=pk, fields=f)

    def live(self):
        return self.conn.execute("SELECT count(*) FROM contacts WHERE merged_into IS NULL").fetchone()[0]


class TestResolve(PeopleCase):
    def test_opaque_id_then_vanity_slug_is_one_person(self):
        a = self.resolve(linkedin_url=LI_IN + "ACoAABcdEfGh123")
        row = self.conn.execute("SELECT needs_vanity, li_slug FROM contacts WHERE id = ?", (a,)).fetchone()
        self.assertEqual(tuple(row), (1, None))
        b = self.resolve(linkedin_url=LI_IN + "example-alex-rivera")
        self.assertNotEqual(a, b)          # nothing links them yet
        pk = keys.person_keys(linkedin_url=LI_IN + "ACoAABcdEfGh123") + \
            keys.person_keys(linkedin_url=LI_IN + "example-alex-rivera")
        with db.tx(self.conn):
            c = people.resolve(self.conn, keys=pk, fields={})
        self.assertEqual(c, min(a, b))
        self.assertEqual(self.live(), 1)
        row = self.conn.execute("SELECT needs_vanity, li_slug FROM contacts WHERE id = ?", (c,)).fetchone()
        self.assertEqual(tuple(row), (0, "example-alex-rivera"))

    def test_gmail_folding_and_plus_tags(self):
        a = self.resolve(email="alex.rivera@" + GMAIL)
        self.assertEqual(self.resolve(email="AlexRivera+jobs@" + GOOGLEMAIL), a)
        b = self.resolve(email="alex.rivera@kestrel.example")
        self.assertEqual(self.resolve(email="alex.rivera+x@kestrel.example"), b)
        self.assertNotEqual(a, b)

    def test_second_address_same_name_same_company_is_fuzzy_same_person(self):
        a = self.resolve(email="alex.rivera@kestrel.example", full_name="Alex Rivera")
        b = self.resolve(email="arivera@kestrel.example", full_name="Alex Rivera")
        self.assertEqual(a, b)
        self.assertTrue(self.conn.execute("SELECT 1 FROM human_tasks WHERE question LIKE 'New identity%'").fetchone())
        insert_action(self.conn, kind="cold_email", contact_id=a, company_id=self.co)
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "cold_email", contact_id=b, company_id=None)
        self.assertIn("E_DUP_PERSON", [h["code"] for h in hits])

    def test_merge_keeps_blocking_actions_visible(self):
        a = self.resolve(email="alex.rivera@kestrel.example")
        b = self.resolve(linkedin_url=LI_IN + "example-alex-rivera")
        insert_action(self.conn, kind="cold_email", contact_id=a, company_id=self.co, reserved_at=self.clock.ago(days=30))
        insert_action(self.conn, kind="li_invite", contact_id=b, company_id=self.co, platform="linkedin",
                      reserved_at=self.clock.ago(days=20))
        pk = keys.person_keys(email="alex.rivera@kestrel.example",
                              linkedin_url=LI_IN + "example-alex-rivera")
        with db.tx(self.conn):
            c = people.resolve(self.conn, keys=pk, fields={})
        self.assertEqual(people.group(self.conn, c), [min(a, b), max(a, b)])
        # both first touches still exist (one stays on the merged row because of the unique index)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions WHERE first_touch = 1").fetchone()[0], 2)
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "inmail", contact_id=c)
        self.assertIn("E_DUP_PERSON", [h["code"] for h in hits])

    def test_dnc_is_sticky_and_split(self):
        a = self.resolve(email="alex.rivera@kestrel.example", do_not_contact=1, dnc_reason="opt_out")
        b = self.resolve(email="alex.rivera@kestrel.example", title="Head of Analytics")
        self.assertEqual(a, b)
        row = self.conn.execute("SELECT do_not_contact, title FROM contacts WHERE id = ?", (a,)).fetchone()
        self.assertEqual(tuple(row), (1, "Head of Analytics"))
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES "
                              "('email:second@kestrel.example', ?, 'email', ?)", (a, canon.now()))
            new = people.split(self.conn, a, ["email:second@kestrel.example"], "human")
        self.assertNotEqual(new, a)
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", people.split, self.conn, new, ["email:second@kestrel.example"], "human")
        self.assertDenied("E_VALIDATION", self.resolve)


class TestMergeAddress(PeopleCase):
    """people.merge: the survivor keeps the stronger address (grade A > B > C, then published > pattern > human >
    provider) as one group, and the email finder's requests move with the person (hooks.on_contact_merge)."""

    def contact(self, name, email, grade, source, mx=1):
        from tests.helpers import insert_contact
        cid = insert_contact(self.conn, self.co, full_name=name, email=email)
        self.conn.execute("UPDATE contacts SET email_grade = ?, email_source = ?, email_mx_ok = ?, "
                          "email_evidence_url = ? WHERE id = ?",
                          (grade, source, mx, "https://kestrel.example/team" if source == "published" else None, cid))
        return cid

    def row(self, cid):
        return self.conn.execute("SELECT email, email_grade, email_source, email_evidence_url, email_invalid "
                                 "FROM contacts WHERE id = ?", (cid,)).fetchone()

    def test_stronger_address_moves_to_the_survivor(self):
        keep = self.contact("Alex Rivera", "arivera@kestrel.example", "B", "human")
        lose = self.contact("Alex Rivera", "alex.rivera@kestrel.example", "A", "published")
        ts = canon.now()
        self.conn.execute("INSERT INTO enrich_requests (request_uid, contact_id, status, created_by, started_at, "
                          "created_at, updated_at) VALUES ('EAAAAAAA', ?, 'not_found', 'system', ?, ?, ?)",
                          (lose, ts, ts, ts))
        with db.tx(self.conn):
            people.merge(self.conn, lose, keep, "human", False)
        self.assertEqual(tuple(self.row(keep)), ("alex.rivera@kestrel.example", "A", "published",
                                                 "https://kestrel.example/team", 0))
        self.assertEqual(self.conn.execute("SELECT contact_id FROM enrich_requests").fetchone()[0], keep)

    def test_source_breaks_a_grade_tie_and_the_survivor_wins_a_full_tie(self):
        keep = self.contact("Alex Rivera", "arivera@kestrel.example", "B", "human")
        lose = self.contact("Alex Rivera", "alex.rivera@kestrel.example", "B", "pattern")
        with db.tx(self.conn):
            people.merge(self.conn, lose, keep, "human", False)
        self.assertEqual(tuple(self.row(keep))[:3], ("alex.rivera@kestrel.example", "B", "pattern"))
        keep2 = self.contact("Jordan Lee", "jlee@kestrel.example", "A", "published")
        lose2 = self.contact("Jordan Lee", "jordan.lee@kestrel.example", "A", "published")
        with db.tx(self.conn):
            people.merge(self.conn, lose2, keep2, "human", False)
        self.assertEqual(self.row(keep2)[0], "jlee@kestrel.example")

    def test_a_bounced_address_loses_and_an_empty_one_is_filled(self):
        keep = self.contact("Alex Rivera", "arivera@kestrel.example", "A", "published")
        self.conn.execute("UPDATE contacts SET email_invalid = 1 WHERE id = ?", (keep,))
        lose = self.contact("Alex Rivera", "alex.rivera@kestrel.example", "C", "human")
        with db.tx(self.conn):
            people.merge(self.conn, lose, keep, "human", False)
        self.assertEqual(tuple(self.row(keep))[:3] + (self.row(keep)[4],),
                         ("alex.rivera@kestrel.example", "C", "human", 0))
        keep2 = self.contact("Jordan Lee", None, None, None, mx=None)
        lose2 = self.contact("Jordan Lee", "jordan.lee@kestrel.example", "B", "human")
        with db.tx(self.conn):
            people.merge(self.conn, lose2, keep2, "human", False)
        self.assertEqual(self.row(keep2)[0], "jordan.lee@kestrel.example")
        self.assertEqual(people.address_rank(None), (0, 0, 0))


if __name__ == "__main__":
    import unittest
    unittest.main()
