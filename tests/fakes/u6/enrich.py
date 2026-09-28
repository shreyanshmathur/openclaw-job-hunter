"""Fakes of the email finder (U10) functions U6 calls: enrich.verify.lookup (email verify), enrich.cache.state
(outreach address hints) and enrich.feedback.on_bounce / on_optout (reached through U1 hooks from replies).
Installed as sys.modules entries by U6TestCase, so U6 tests never run the real finder. Fictional data only."""
from __future__ import annotations

import types

LOOKUP: dict = {}        # address -> {grade, provider, call_id, bounced}
STATES: dict = {}        # contact_id -> request status
BOUNCES: list = []       # (contact_id, recipient) per on_bounce call
OPTOUTS: list = []       # contact ids per on_optout call
TRIP_ON_BOUNCE: list = []  # scopes on_bounce reports as tripped


def lookup(conn, address):
    return LOOKUP.get((address or "").strip().lower())


def state(conn, contact_id):
    return STATES.get(contact_id)


def on_bounce(conn, action_row, thread_row=None):
    get = (lambda r, k: (r.get(k) if isinstance(r, dict) else r[k]) if r is not None else None)
    BOUNCES.append((get(action_row, "contact_id") or get(thread_row, "contact_id"), get(action_row, "recipient")))
    return list(TRIP_ON_BOUNCE)


def on_optout(conn, contact_id):
    OPTOUTS.append(contact_id)
    return 1


verify = types.ModuleType("jobhunter.enrich.verify")
verify.lookup = lookup
cache = types.ModuleType("jobhunter.enrich.cache")
cache.state = state
feedback = types.ModuleType("jobhunter.enrich.feedback")
feedback.on_bounce, feedback.on_optout = on_bounce, on_optout

MODULES = {"jobhunter.enrich.verify": verify, "jobhunter.enrich.cache": cache, "jobhunter.enrich.feedback": feedback}


def reset() -> None:
    LOOKUP.clear()
    STATES.clear()
    BOUNCES.clear()
    OPTOUTS.clear()
    TRIP_ON_BOUNCE.clear()
