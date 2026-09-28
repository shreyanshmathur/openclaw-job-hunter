"""U6 test support: fake modules of other units (deps.py), a test case base class and small factories."""
from __future__ import annotations

import random
import sys
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, threads
from tests.helpers import HomeTestCase

from . import deps


class U6TestCase(HomeTestCase):
    """HomeTestCase with the fakes installed as jobhunter.<module> and a seeded RNG for due dates."""

    def setUp(self):
        super().setUp()
        deps.reset()
        self._patch = mock.patch.dict(sys.modules, deps.MODULES)
        self._patch.start()
        self._rng = threads._rng
        threads._rng = random.Random(7)

    def tearDown(self):
        threads._rng = self._rng
        self._patch.stop()
        super().tearDown()

    # helpers ----------------------------------------------------------------
    def enable_linkedin(self):
        from jobhunter import db
        with db.tx(self.conn):
            db.meta_set(self.conn, "channel_linkedin_enabled", "1", "human")
            db.meta_set(self.conn, "linkedin_tos_ack", "1", "human")

    def row(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def uid(self, table, rid):
        col = {"contacts": "contact_uid", "companies": "company_uid", "jobs": "job_uid"}[table]
        return self.conn.execute("SELECT %s FROM %s WHERE id = ?" % (col, table), (rid,)).fetchone()[0]


def set_config(path: str, value) -> None:
    deps.config_set(path, value)


def now() -> str:
    return canon.now()
