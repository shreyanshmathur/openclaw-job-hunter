"""Researched defaults, hard maxima and hard minima (design 4.1, 4.2.1).

- DEFAULTS: the numbers of the committed `config.example.json` (loaded from that file, so the two never
  drift; tests/test_ceilings.py checks the file exists and parses).
- CLASSES: dotted-path pattern -> key class. `L` limit key (higher is looser), `F` floor key (lower is
  looser), `A` authority key, `fixed` (the default always wins), `bounded` (free within BOUNDS), anything
  else is free. `*` matches exactly one path segment; `[]` marks the elements of a list.
- HARD_MAX (limit keys) and HARD_MIN (floor keys): never-exceed values from the research notes
  (linkedin-limits.md 6.1, gmail-limits.md 8). A list value applies element-wise.
- AUTHORITY_STRICT: the strictest value of each authority key (4.1).

Effective values are computed in jobhunter.config; nothing else reads these tables directly.
"""
from __future__ import annotations

import copy
import json
import os

from . import paths

EXAMPLE_FILE = os.path.join(paths.REPO, "config.example.json")


def _load_defaults() -> dict:
    with open(EXAMPLE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


DEFAULTS: dict = _load_defaults()


def defaults() -> dict:
    """A deep copy of DEFAULTS (callers may mutate it)."""
    return copy.deepcopy(DEFAULTS)


LANE_CYCLES_MAX = {"scout": [3, 4], "evaluator": [12, 16], "applier": [5, 6], "outreach": [5, 6],
                   "replies": [3, 4]}

# ---------------------------------------------------------------- hard maxima (limit keys)
HARD_MAX: dict = {
    # Gmail (gmail-limits.md 8, "never exceed")
    "gmail.ceilings.*.cold_day": 30, "gmail.ceilings.*.followup_day": 15, "gmail.ceilings.*.total_day": 50,
    "gmail.ceilings.*.cold_week": 150, "gmail.ceilings.*.hour": 8, "gmail.ceilings.*.cycle": 5,
    "gmail.warmup.week1_cold_day": 5, "gmail.warmup.step_per_week": 5, "gmail.max_links": 3,
    "gmail.bounce_stop.per_24h": 2, "gmail.bounce_stop.rolling_rate_pause": 0.02,
    "gmail.bounce_stop.rolling_rate_stop": 0.05, "gmail.complaint_stop.per_30d": 2,
    "gmail.reply_rate_floor.after_sends": 100,
    # LinkedIn (linkedin-limits.md 6.1, "max, never exceed")
    "linkedin.ceilings.*.invites.cycle": 4, "linkedin.ceilings.*.invites.hour": 6,
    "linkedin.ceilings.*.invites.day": 20, "linkedin.ceilings.*.invites.week": 80,
    "linkedin.ceilings.*.notes_free_month": 3,
    "linkedin.ceilings.*.messages.cycle": 6, "linkedin.ceilings.*.messages.hour": 8,
    "linkedin.ceilings.*.messages.day": 30, "linkedin.ceilings.*.messages.week": 140,
    "linkedin.ceilings.*.inmail.day": 3,
    "linkedin.ceilings.*.easy_apply.cycle": 4, "linkedin.ceilings.*.easy_apply.hour": 4,
    "linkedin.ceilings.*.easy_apply.day": 15, "linkedin.ceilings.*.easy_apply.week": 70,
    "linkedin.ceilings.*.withdraw.cycle": 5, "linkedin.ceilings.*.withdraw.day": 15,
    "linkedin.ceilings.*.profile_views.cycle": 20, "linkedin.ceilings.*.profile_views.hour": 30,
    "linkedin.ceilings.*.profile_views.day": 100, "linkedin.ceilings.*.profile_views.week": 500,
    "linkedin.ceilings.*.people_search.day": 15, "linkedin.ceilings.*.people_search.month": 200,
    "linkedin.ceilings.*.content_search.day": 8, "linkedin.ceilings.*.content_search.week": 30,
    "linkedin.ceilings.*.job_search_pages.day": 100,
    "linkedin.ceilings.*.writes_total_day": 60, "linkedin.ceilings.*.actions_total_day": 250,
    "linkedin.ceilings.*.pending_invites_stop": 400, "linkedin.ceilings.*.cycles_day": 8,
    "linkedin.note_max_chars.free": 200, "linkedin.note_max_chars.premium": 300,
    "linkedin.engagement_likes_comments": 0, "linkedin.max_invites_per_company_week": 3,
    # Boards and applications
    "boards.global.apps_day": 25, "boards.global.apps_week": 120, "boards.global.apps_hour": 6,
    "boards.per_company.apps_day": 1, "boards.per_company.apps_30d": 2, "boards.per_company.apps_90d": 3,
    "boards.per_company.same_role_jaccard_block": 0.6, "boards.per_company.second_role_max_jaccard": 0.3,
    "boards.per_agency.emails_day": 1, "boards.per_agency.emails_30d": 3, "boards.per_agency.apps_day": 2,
    "boards.per_agency.apps_30d": 6,
    "boards.sites.*.pages_day": 40, "boards.sites.*.day": 25, "boards.sites.*.week": 120, "boards.sites.*.hour": 6,
    "boards.sites.naukri.day": 50, "boards.sites.naukri.month_stop": 130, "boards.sites.cutshort.week": 15,
    "boards.sites.yc.week": 7, "boards.sites.indeed.day": 0, "boards.sites.glassdoor.day": 0,
    # Sources, evaluator, outreach, QC, other
    "sources.remotive_max_calls_day": 4, "evaluator.batch_size": 20,
    "outreach.per_job_max_contacts": 2, "outreach.research.max_page_loads": 9,
    "outreach.research.facts_max_age_days": 14, "outreach.research.hook_max_age_days": 180,
    "outreach.research.hook_preferred_age_days": 90,
    "qc.max_rewrites": 2, "qc.max_human_edits": 3, "qc.lint.max_soft_hits": 2, "qc.lint.max_warnings": 2,
    "qc.lint.li_connect_hard": 200, "qc.review.max_tries": 2,
    "resume.max_pages": 2, "sheets.max_ops_per_post": 500,
    "clock.max_skew_minutes": 10, "clock.max_backward_minutes": 5, "browser.max_tabs": 5,
    "dispatch.lanes.scout.cycles_per_day": LANE_CYCLES_MAX["scout"],
    "dispatch.lanes.evaluator.cycles_per_day": LANE_CYCLES_MAX["evaluator"],
    "dispatch.lanes.applier.cycles_per_day": LANE_CYCLES_MAX["applier"],
    "dispatch.lanes.outreach.cycles_per_day": LANE_CYCLES_MAX["outreach"],
    "dispatch.lanes.replies.cycles_per_day": LANE_CYCLES_MAX["replies"],
    "linkedin.warmup_weeks[].*": 30,
    # email codes, site accounts and the CAPTCHA hand-off (FEATURES-OTP-ACCOUNTS-CAPTCHA 1.3)
    "otp.window_minutes": 10, "otp.max_uses_day_per_site": 6, "otp.max_uses_day": 20, "otp.failure_breaker_24h": 5,
    "accounts.max_new_day": 5, "accounts.max_new_week": 20, "accounts.failure_breaker_24h": 3,
    "captcha.timeout_minutes": 240, "captcha.max_tasks_day": 10, "captcha.max_open": 3,
    "captcha.repeat_breaker_per_site_day": 5, "captcha.screenshot_retention_days": 7,
    # stop-rule thresholds (4.2.1 class F: only stricter): more aged invites before the acceptance check runs
    # is looser
    "linkedin.adaptive.min_invites_aged_7d": 20,
}

# ---------------------------------------------------------------- hard minima (floor keys)
HARD_MIN: dict = {
    "gmail.ceilings.*.min_gap_minutes": 3, "gmail.ceilings.*.company_cooldown_days": 30,
    "gmail.followup_after_business_days": [3, 5], "gmail.fetch_every_minutes": 15,
    "gmail.bounce_stop.pause_hours": 24, "gmail.complaint_stop.first_complaint_cut_pct": 50,
    "gmail.complaint_stop.cut_days": 7, "gmail.reply_rate_floor.min_rate": 0.02,
    "linkedin.delays_sec.write_floor": 45, "linkedin.delays_sec.profile_view_floor": 15,
    "linkedin.delays_sec.easy_apply_floor": 180, "linkedin.delays_sec.write": [45, 45],
    "linkedin.delays_sec.profile_view": [15, 15], "linkedin.delays_sec.easy_apply": [180, 180],
    "linkedin.delays_sec.search_page": [5, 5], "linkedin.delays_sec.between_cycles_min": [45, 45],
    "linkedin.delays_sec.post_accept_message_days": [1, 1], "linkedin.delays_sec.followup_days": [7, 7],
    "linkedin.adaptive.half_below": 0.40, "linkedin.adaptive.pause_below": 0.25,
    "linkedin.adaptive.pause_days": 14, "linkedin.withdraw_older_than_days": 14,
    "boards.per_company.second_role_min_gap_days": 1, "boards.site_gap_minutes": [3, 5],
    "sources.poll_hours.ats": 6, "sources.poll_hours.remote_boards": 3, "sources.poll_hours.yc": 24,
    "sources.poll_hours.hn": 24, "sources.poll_hours.serpapi": 24, "sources.per_host_min_interval_ms": 1000,
    "evaluator.thresholds.apply": 60, "evaluator.thresholds.borderline": 45,
    "outreach.target_skip_days": 30, "qc.review.min_weighted": 4.0, "qc.review.min_core": 4,
    "qc.review.min_any": 3, "qc.golden_min_agreement": 18, "exclusions.min_keep_ratio": 0.5,
    "browser.lease_minutes": 35, "browser.dwell_seconds": [5, 20], "approval.approval_ttl_hours": 12,
    "active_hours.battery_floor_pct": 10, "dispatch.lanes.*.min_spacing_minutes": 45,
    "otp.poll_seconds": 10, "accounts.password_length": 16,
}

# ---------------------------------------------------------------- classes
CLASSES: dict = {}
for _p in HARD_MAX:
    CLASSES[_p] = "L"
for _p in HARD_MIN:
    CLASSES[_p] = "F"
CLASSES.update({
    "approval.mode": "A", "approval.per_channel.linkedin": "A", "gmail.tier": "A", "linkedin.tier": "A",
    "channels.linkedin.enabled": "A",
    "schema_version": "fixed", "browser.profile": "fixed", "qc.review.agent": "fixed",
    "browser.require_display": "fixed", "browser.type_slowly": "fixed",
    "outreach.referral_ask_only_after_reply": "fixed", "boards.sites.*.risk": "fixed",
    "captcha.handoff": "A", "otp.after_use": "bounded", "accounts.key_store": "bounded",
    "gmail.ceilings.*.gap_jitter_minutes": "bounded", "boards.daily_jitter_pct": "bounded",
    "evaluator.years_tolerance.below": "bounded", "evaluator.years_tolerance.above": "bounded",
    "qc.review.timeout_s": "bounded", "dispatch.lanes.*.skip_probability": "bounded",
    # stop-rule windows (4.2.1 class F): a longer window dilutes recent bounces or refusals, a shorter one
    # starves the check (the bounce rate needs 20 sends; acceptance counts invites aged 7 days or more)
    "gmail.bounce_stop.rolling_window_sends": "bounded", "linkedin.adaptive.acceptance_window_days": "bounded",
})

BOUNDS: dict = {
    "gmail.ceilings.*.gap_jitter_minutes": (0, 20), "boards.daily_jitter_pct": (10, 40),
    "evaluator.years_tolerance.below": (0, 5), "evaluator.years_tolerance.above": (0, 5),
    "qc.review.timeout_s": (60, 300), "dispatch.lanes.*.skip_probability": (0.0, 0.5),
    "gmail.bounce_stop.rolling_window_sends": (20, 100), "linkedin.adaptive.acceptance_window_days": (30, 60),
    # bounded string keys: the allowed values (anything else gives the default)
    "otp.after_use": ("leave", "mark_read", "archive"), "accounts.key_store": ("auto", "keychain", "file"),
}

# ---------------------------------------------------------------- email finder (enrich.*)
# One source for the numbers: jobhunter.enrich.settings (U10, ENRICH-SPEC 8.3 and 14). Limit keys can only be
# lowered by the file, floor keys only raised; config raise moves the baseline up to these hard values.
# settings.clamp() applies the same rules again when the finder reads its block (defence in depth).
try:
    from .enrich import settings as _enrich
except ImportError:      # pragma: no cover  (the finder package ships with the repo)
    _enrich = None
if _enrich is not None:
    for _k, _v in _enrich.HARD_MAX.items():
        HARD_MAX["enrich." + _k] = _v
    for _p, _caps in _enrich.PROVIDER_HARD_MAX.items():
        for _k, _v in _caps.items():
            HARD_MAX["enrich.providers.%s.%s" % (_p, _k)] = _v
    for _k, _v in _enrich.HARD_MIN.items():
        HARD_MIN["enrich." + _k] = _v
    for _p, _v in _enrich.PROVIDER_HARD_MIN.items():
        HARD_MIN["enrich.providers.%s.min_interval_s" % _p] = _v
    for _p in HARD_MAX:
        if _p.startswith("enrich."):
            CLASSES[_p] = "L"
    for _p in HARD_MIN:
        if _p.startswith("enrich."):
            CLASSES[_p] = "F"
    for _k, _v in _enrich.BOUNDS.items():
        CLASSES["enrich." + _k] = "bounded"
        BOUNDS["enrich." + _k] = tuple(_v)
ENRICH_ITEM_SETS = {"enrich.chain": _enrich.CHAIN_ITEMS, "enrich.verifiers": _enrich.VERIFIER_ITEMS,
                    "enrich.reserve_chain": _enrich.RESERVE_ITEMS} if _enrich is not None else {}
ENRICH_KEY_STORES = ("auto", "keychain", "file")

# Authority keys: stricter value first.
AUTHORITY_STRICT = {
    "approval.mode": "human", "approval.per_channel.linkedin": "human", "gmail.tier": "conservative",
    "linkedin.tier": "conservative", "channels.linkedin.enabled": False,
    # false: a CAPTCHA on a form sends the job to the owner as before, without a hand-off task
    "captcha.handoff": False,
}

# approval.always_human: items can be added, the default four can never be removed
UNION_LISTS = ("approval.always_human",)

# lists of objects compared element-wise with the default list (a file can never add entries)
OBJECT_LISTS = ("linkedin.warmup_weeks",)


def match(pattern: str, path: str) -> bool:
    """Dotted-path pattern match: '*' matches one segment, 'a[].b' addresses keys of list elements."""
    pp = pattern.split(".")
    qq = path.split(".")
    if len(pp) != len(qq):
        return False
    return all(p == "*" or p == q for p, q in zip(pp, qq))


def lookup(table: dict, path: str):
    """Most specific entry of `table` for `path` (exact beats wildcard; fewer wildcards win)."""
    if path in table:
        return table[path]
    best = None
    best_score = None
    for pat, value in table.items():
        if "*" in pat and match(pat, path):
            score = pat.count("*")
            if best_score is None or score < best_score:
                best, best_score = value, score
    return best


def key_class(path: str) -> str:
    return lookup(CLASSES, path) or "free"
