"""companies.resolve (design 2.4.2, B3): one row per company across name, loose name, domain, label and ATS
tenant; merges in the same transaction repoint every reference; distinct pairs refuse; fuzzy merges open a
task; split undoes; the Gmail precheck query covers every alias."""
from __future__ import annotations

import tests  # noqa: F401
from jobhunter import canon, companies, db, gate
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase, insert_action, insert_contact, insert_draft, insert_job


class ResolveCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()

    def resolve(self, **kw):
        kw.setdefault("source", "job_board")
        with db.tx(self.conn):
            return companies.resolve(self.conn, **kw)

    def count(self):
        return self.conn.execute("SELECT count(*) FROM companies WHERE merged_into IS NULL").fetchone()[0]


class TestOneCompany(ResolveCase):
    def test_b3_variants_resolve_to_one_row(self):
        ids = {self.resolve(name="Kestrel Labs"), self.resolve(name="KestrelLabs Pvt Ltd"),
               self.resolve(domain="kestrellabs.com", source="careers_url"),
               self.resolve(domain="https://www.kestrellabs.in/careers", source="careers_url"),
               self.resolve(name="kestrel labs (W25)", ats="greenhouse", tenant="kestrellabs", source="ats"),
               self.resolve(domain="talent@kestrellabs.example", source="email")}
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.count(), 1)
        cid = ids.pop()
        aliases = {r[0] for r in self.conn.execute("SELECT alias_key FROM company_aliases WHERE company_id = ?", (cid,))}
        self.assertTrue({"id:kestrellabs", "dom:kestrellabs.com", "dom:kestrellabs.in",
                         "ats:greenhouse:kestrellabs"} <= aliases)
        q = companies.precheck_query(self.conn, cid)
        for term in ('"kestrel labs"', "kestrellabs", "kestrellabs.com", "kestrellabs.in"):
            self.assertIn(term, q)
        self.assertTrue(q.startswith("(") and " OR " in q)

    def test_tenant_case_is_kept_and_not_duplicated(self):
        """SmartRecruiters and Workday site ids are case-sensitive: the tenant is stored as given, and a spelling
        that differs only in case reuses the existing row (U2 polls every row)."""
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO ats_tenants (ats, tenant, company_id, active, source) VALUES "
                              "('smartrecruiters', 'KestrelCommerce', NULL, 1, 'targets_file')")
        a = self.resolve(name="Kestrel Commerce", ats="smartrecruiters", tenant="kestrelcommerce", source="ats")
        b = self.resolve(name="Harbor Pine", ats="Workday", tenant="HarborPine", source="ats")
        rows = [tuple(r) for r in self.conn.execute("SELECT ats, tenant, company_id FROM ats_tenants ORDER BY ats")]
        self.assertEqual(rows, [("smartrecruiters", "KestrelCommerce", a), ("workday", "HarborPine", b)])
        self.resolve(name="Harbor Pine", ats="workday", tenant="HarborPine", source="ats")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ats_tenants").fetchone()[0], 2)

    def test_noise_words_and_freemail(self):
        a = self.resolve(name="Tidemark Technologies")
        self.assertEqual(self.resolve(name="Tidemark"), a)
        g = self.resolve(name="Northwind Analytics", domain="recruiter.one@" + "gmail.com", source="email")
        row = self.conn.execute("SELECT domain FROM companies WHERE id = ?", (g,)).fetchone()
        self.assertIsNone(row[0])
        self.assertDenied("E_VALIDATION", self.resolve, domain="https://acme.github.io/jobs")
        self.assertIsNone(self.resolve(name="Harbor Pine", create=False))

    def test_agency_flag_only_from_list(self):
        a = self.resolve(name="Randstad India")
        b = self.resolve(name="Randstad")
        row = self.conn.execute("SELECT is_agency, agency_source FROM companies WHERE id = ?", (b,)).fetchone()
        self.assertEqual(tuple(row), (1, "bundled_list"))
        self.assertEqual(a, b)          # 'india' is a noise word, so the loose key joins them
        c = self.resolve(name="Leading MNC confidential client")
        self.assertEqual(self.conn.execute("SELECT is_agency FROM companies WHERE id = ?", (c,)).fetchone()[0], 0)


class TestMerge(ResolveCase):
    def two_companies(self):
        a = self.resolve(name="Kestrel Labs")
        b = self.resolve(domain="klabs.example", source="careers_url")
        self.assertNotEqual(a, b)
        return a, b

    def test_merge_repoints_everything_in_one_transaction(self):
        a, b = self.two_companies()
        pa = insert_contact(self.conn, company_id=a, full_name="Alex Rivera", email="alex@kestrellabs.example")
        pb = insert_contact(self.conn, company_id=b, full_name="Alex Rivera", email="alex.r@klabs.example")
        ua, ub = [self.conn.execute("SELECT company_uid FROM companies WHERE id = ?", (i,)).fetchone()[0] for i in (a, b)]
        self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'pname', ?)",
                          ("pname:alex.rivera@" + ua, pa, canon.now()))
        self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'pname', ?)",
                          ("pname:alex.rivera@" + ub, pb, canon.now()))
        j = insert_job(self.conn, company_id=b)
        insert_action(self.conn, kind="cold_email", company_id=b, contact_id=pb, reserved_at=self.clock.ago(days=10))
        self.conn.execute("UPDATE companies SET contact_state = 'active_thread' WHERE id = ?", (b,))
        merged = self.resolve(name="Kestrel Labs", domain="klabs.example")
        self.assertEqual(merged, min(a, b))
        loser = max(a, b)
        self.assertEqual(self.conn.execute("SELECT merged_into FROM companies WHERE id = ?", (loser,)).fetchone()[0], merged)
        self.assertEqual(self.conn.execute("SELECT company_id FROM jobs WHERE id = ?", (j,)).fetchone()[0], merged)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions WHERE company_id = ?", (merged,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT contact_state FROM companies WHERE id = ?", (merged,)).fetchone()[0],
                         "active_thread")
        m = self.conn.execute("SELECT strength FROM company_merges").fetchone()[0]
        self.assertEqual(m, "exact")
        # the two Alex Rivera contacts now collide on the pname key and are one person
        self.assertEqual(self.conn.execute("SELECT count(*) FROM contacts WHERE merged_into IS NULL").fetchone()[0], 1)
        # the cooldown trigger sees one company
        other = insert_contact(self.conn, company_id=merged, full_name="Sam Patel", email="sam@kestrellabs.example")
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "cold_email", contact_id=other, company_id=merged)
        self.assertIn("E_COMPANY_COOLDOWN", [h["code"] for h in hits])

    def test_distinct_pair_refuses_and_task_survives(self):
        a, b = self.two_companies()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO company_distinct (a_id, b_id, by, created_at) VALUES (?, ?, 'human', ?)",
                              (min(a, b), max(a, b), canon.now()))
        self.assertDenied("E_COMPANY_AMBIGUOUS", self.resolve, name="Kestrel Labs", domain="klabs.example")
        self.assertEqual(self.count(), 2)
        self.assertTrue(self.conn.execute("SELECT 1 FROM human_tasks WHERE kind = 'confirm_company_merge'").fetchone())

    def test_fuzzy_merge_opens_task_and_split_undoes(self):
        a = self.resolve(name="Harbor Pine Labs")         # loose key id:harborpine
        b = self.resolve(name="Harbor Pine Digital")      # same loose key -> resolves to a
        self.assertEqual(a, b)
        c = self.resolve(domain="harborpine.example", source="careers_url")
        self.assertEqual(c, a)                            # label key id:harborpine
        with db.tx(self.conn):
            new = companies.split(self.conn, a, ["dom:harborpine.example", "id:harborpine"], "human")
        self.assertNotEqual(new, a)
        self.assertTrue(self.conn.execute("SELECT 1 FROM company_distinct WHERE a_id = ? AND b_id = ?",
                                          (min(a, new), max(a, new))).fetchone())
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", companies.split, self.conn, a, ["dom:nothere.example"], "human")
        x = self.resolve(name="Juniper Coast Labs")                     # id:junipercoastlabs, loose id:junipercoast
        y = self.resolve(domain="jcl.example", source="careers_url")    # dom only (label too short)
        self.assertNotEqual(x, y)
        z = self.resolve(name="Juniper Coast Digital", domain="jcl.example")
        self.assertEqual(z, min(x, y))
        self.assertEqual(self.conn.execute("SELECT strength FROM company_merges ORDER BY id DESC").fetchone()[0], "fuzzy")
        self.assertTrue(self.conn.execute("SELECT 1 FROM human_tasks WHERE kind = 'confirm_company_merge' AND "
                                          "question LIKE 'Merged%'").fetchone())

    def test_merge_supersedes_second_open_email_draft(self):
        a, b = self.two_companies()
        d1 = insert_draft(self.conn, kind="cold_email", status="approved", company_id=a)
        d2 = insert_draft(self.conn, kind="cold_email", status="awaiting_approval", company_id=b)
        self.resolve(name="Kestrel Labs", domain="klabs.example")
        st = dict(self.conn.execute("SELECT id, status FROM drafts").fetchall())
        self.assertEqual((st[d1], st[d2]), ("approved", "superseded"))


class TestAliasFile(ResolveCase):
    def test_import_aliases_links_names_and_domains(self):
        import os
        from jobhunter import paths
        a = self.resolve(name="Kestrel Commerce")
        b = self.resolve(name="KL Commerce Group")
        self.assertNotEqual(a, b)
        with open(os.path.join(paths.private_dir(), "company_aliases.csv"), "w") as fh:
            fh.write("alias,company\n# comment\nKL Commerce Group,Kestrel Commerce\nkestrel-shop.example,Kestrel Commerce\n"
                     "broken line without comma\n")
        with db.tx(self.conn):
            self.assertTrue(companies.aliases_changed(self.conn))
            res = companies.import_aliases(self.conn)
        self.assertEqual((res["linked"], len(res["errors"])), (2, 1))
        self.assertEqual(self.resolve(name="KL Commerce Group"), min(a, b))
        self.assertEqual(self.resolve(domain="kestrel-shop.example", source="careers_url"), min(a, b))
        with db.tx(self.conn):
            self.assertFalse(companies.aliases_changed(self.conn))


class TestAddKeys(ResolveCase):
    """companies.add_keys: a company known without a name (only a tenant or a careers domain) gets the email
    domain of its people without a second company being created."""

    def test_a_nameless_company_gets_its_email_domain(self):
        cid = self.resolve(ats="lever", tenant="tidewater", source="ats")
        with db.tx(self.conn):
            out = companies.add_keys(self.conn, cid, domain="people@tidewater-labs.example", source="email")
        self.assertEqual(out, cid)
        self.assertEqual(self.resolve(domain="tidewater-labs.example", source="email"), cid)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.conn.execute("SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()[0],
                         "tidewater-labs.example")
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", companies.add_keys, self.conn, cid, source="email")
            self.assertDenied("E_VALIDATION", companies.add_keys, self.conn, cid, domain="x.example", source="guess")
            self.assertDenied("E_NOT_FOUND", companies.add_keys, self.conn, 9999, domain="x.example", source="email")

    def test_a_key_of_another_company_merges_by_the_resolve_rules(self):
        a = self.resolve(ats="lever", tenant="twl-hiring", source="ats")
        b = self.resolve(name="Tidewater Labs", domain="tidewater-labs.example", source="job_board")
        self.assertNotEqual(a, b)
        with db.tx(self.conn):
            out = companies.add_keys(self.conn, b, ats="lever", tenant="twl-hiring", source="ats")
        self.assertEqual(out, min(a, b))
        self.assertEqual(self.count(), 1)
        # a pair the owner marked as two companies is never merged
        c = self.resolve(name="Harbor Pine", source="job_board")
        d = self.resolve(domain="harborpine-group.example", source="careers_url")
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO company_distinct (a_id, b_id, by, created_at) VALUES (?, ?, 'human', ?)",
                              (min(c, d), max(c, d), canon.now()))
        with db.tx(self.conn):
            self.assertDenied("E_COMPANY_AMBIGUOUS", companies.add_keys, self.conn, c,
                              domain="harborpine-group.example", source="email")


if __name__ == "__main__":
    import unittest
    unittest.main()
