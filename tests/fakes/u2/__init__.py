"""U2 test doubles: a scripted HTTP client, and stand-ins for `jobhunter.profile` (U4) and `jobhunter.config`
(U1) so tests control the confirmed profile and the effective config. U1's keys, companies, people, exclusions
and breakers are used for real. Everything is fictional."""
from __future__ import annotations

import copy
import json
import os
import sys
import types
from unittest import mock

from jobhunter.errors import Denied
from jobhunter.sources.http import HttpError, NotFound, NotModified, RateLimited

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fixtures",
                        "discovery")


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURES, name)


def fixture_text(name: str) -> str:
    with open(fixture_path(name), "r", encoding="utf-8") as fh:
        return fh.read()


def fixture_json(name: str):
    return json.loads(fixture_text(name))


def profile_fixture() -> dict:
    return fixture_json("profile.json")


CONFIG = {
    "sources": {
        "api": {"greenhouse": True, "lever": True, "ashby": True, "smartrecruiters": True, "workable": True,
                "recruitee": True, "bamboohr": True, "workday": True, "yc": True, "himalayas": True,
                "remoteok": True, "remotive": True, "weworkremotely": True, "workingnomads": True,
                "hn_whoishiring": True, "jobicy": False, "serpapi_google_jobs": False},
        "poll_hours": {"ats": 12, "remote_boards": 6, "yc": 24, "hn": 24, "serpapi": 24},
        "remotive_max_calls_day": 4, "per_host_min_interval_ms": 1000, "max_age_days": 30,
        "targets_file": "private/targets.csv",
        "user_agent": "openclaw-job-hunter/2.0 (personal job search; +https://github.com/example/openclaw-job-hunter)",
    },
    "evaluator": {"thresholds": {"apply": 70, "borderline": 55}, "years_tolerance": {"below": 1, "above": 3},
                  "weights": {"role_family": 0.25, "skills": 0.25, "seniority": 0.15, "domain": 0.10,
                              "location": 0.10, "compensation": 0.10, "company": 0.05},
                  "batch_size": 15, "feasibility_alert": {"min_evaluations": 50, "gate_share": 0.40}},
    "boards": {"sites": {"naukri": {"discover": "off", "pages_day": 20}, "wellfound": {"discover": "browser",
                                                                                     "pages_day": 10},
                         "linkedin_jobs": {"discover": "off", "pages_day": 15},
                         "indeed": {"discover": "off", "pages_day": 10}}},
    "dispatch": {"lanes": {"scout": {"cycles_per_day": [2, 3]}}},
    "channels": {"linkedin": {"enabled": False}},
    "clock": {"max_skew_minutes": 10},
}


def config() -> dict:
    return copy.deepcopy(CONFIG)


class Deps:
    """Holds what the fake profile and config modules return; tests change the attributes."""

    def __init__(self, profile: dict | None, cfg: dict | None):
        self.profile = profile
        self.cfg = cfg


def install(case, profile: dict | None = None, cfg: dict | None = None) -> Deps:
    """Patch sys.modules['jobhunter.profile'] and ['jobhunter.config'] for the duration of the test.
    profile None means the fixture profile; set deps.profile = False to make load_confirmed raise
    E_PROFILE_UNCONFIRMED."""
    deps = Deps(profile_fixture() if profile is None else profile, config() if cfg is None else cfg)
    prof_mod = types.ModuleType("jobhunter.profile")

    def load_confirmed():
        if deps.profile is False:
            raise Denied("E_PROFILE_UNCONFIRMED", "profile not confirmed (fake)")
        return copy.deepcopy(deps.profile)

    def facts():
        if not deps.profile:
            return {}
        return {k: v["text"] for k, v in (deps.profile.get("facts") or {}).items()}

    prof_mod.load_confirmed = load_confirmed
    prof_mod.facts = facts
    cfg_mod = types.ModuleType("jobhunter.config")
    cfg_mod.load = lambda: copy.deepcopy(deps.cfg)
    patcher = mock.patch.dict(sys.modules, {"jobhunter.profile": prof_mod, "jobhunter.config": cfg_mod})
    patcher.start()
    case.addCleanup(patcher.stop)
    return deps


class FakeClient:
    """Stands in for sources.http.HttpClient. `routes` maps a URL (exact, or a prefix ending in '*') to a value:
    a dict/list/str payload, an exception instance to raise, or a callable(url, body) returning either."""

    def __init__(self, routes: dict | None = None, date: str | None = None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, object]] = []
        self.requests = 0
        self.first_date = date
        self.validators = ("W/\"etag-1\"", None)

    def _resolve(self, method: str, url: str, body=None, **kw):
        self.requests += 1
        self.calls.append((method, url, body if body is not None else kw))
        val = self.routes.get(url)
        if val is None:
            for k, v in self.routes.items():
                if k.endswith("*") and url.startswith(k[:-1]):
                    val = v
                    break
        if val is None:
            raise NotFound(404, url)
        if callable(val) and not isinstance(val, (dict, list, str)):
            val = val(url, body)
        if isinstance(val, BaseException):
            raise val
        return copy.deepcopy(val)

    def get_json(self, url, **kw):
        return self._resolve("GET", url, **kw)

    def get_text(self, url, **kw):
        return self._resolve("GET", url, **kw)

    def post_json(self, url, obj, **kw):
        return self._resolve("POST", url, obj, **kw)


__all__ = ["FakeClient", "install", "config", "profile_fixture", "fixture_json", "fixture_text", "fixture_path",
           "HttpError", "NotFound", "NotModified", "RateLimited"]
