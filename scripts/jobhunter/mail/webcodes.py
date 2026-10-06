"""Email codes and sign-in links in Gmail on the web, route web_ui (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.4) [U9].

search(): code opens a new tab of the agent's own browser profile (the one with the owner's consented Gmail
session) on https://mail.google.com/mail/u/0/#search/<query>, waits for the list, checks the page with the Gmail
stop signatures (a match trips gmail as on every Gmail page), runs the read-only driver read_gmail_list.js, opens
at most 5 rows newest first, runs read_gmail_message.js and the fixed read-only link script pagefill.GMAIL_LINKS on
each, and closes the tab. The driver output stays inside this Python process: no model sees the message, the code
or the link. A message's time is its `date` (minute precision); jobhunter.otp accepts it from the minute of the
request on.

after_use(): opening the conversation already marked it read (mark_read); archive presses `e` on the open
conversation.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote

from .. import cdp, db, paths
from ..errors import Denied

MAX_MESSAGES = 5
GMAIL = "https://mail.google.com/mail/u/0/"
DRIVERS = os.path.join(paths.REPO, "drivers")
_THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def driver(name: str) -> str:
    with open(os.path.join(DRIVERS, name + ".js"), "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _gmail_scan(conn, s) -> None:
    """The Gmail page through the stop scan (gmail.json): a tripping match trips gmail (kept) and refuses."""
    from .. import detect
    page = s.page_text()
    payload = {"platform": "gmail", "url": page["url"] or None, "title": page["title"] or None, "http_status": None,
               "text": page["text"][:detect.MAX_TEXT]}
    verdict, sig = detect.match(detect.validate_payload(payload, "code"))
    if sig is not None and verdict == "stop" and sig.get("trip"):
        def _keep(c, pl=payload):
            detect.detect(c, pl, "code", None, open_captcha=False)
        db.defer_write(conn, _keep)
        raise Denied("E_STOP_DETECTED", "Gmail shows a stop (%s); Gmail is stopped and you are told" % sig.get("id"),
                     data={"matched": sig.get("id"), "reason_code": sig.get("reason_code")})


def search(conn, query: str, requested_at: str) -> list:
    """[{id, from, to, subject, received_at, text, links, thread_url}] newest first (at most 5)."""
    from .. import pagefill
    url = GMAIL + "#search/" + quote(query, safe="")
    tab = cdp.new_tab(url)
    out = []
    try:
        with cdp.connect(tab) as s:
            def loaded():
                v = s.evaluate(driver("read_gmail_list"))
                return v if isinstance(v, dict) and v.get("loaded") else None
            with db.tx(conn):
                _gmail_scan(conn, s)             # a security or sign-in page stops before anything is read
            lst = cdp.wait(loaded, 15.0, 0.5)
            with db.tx(conn):
                _gmail_scan(conn, s)
            if not isinstance(lst, dict) or not lst.get("loaded"):
                return []
            rows = [r for r in lst.get("rows") or [] if isinstance(r, dict) and r.get("thread_id") and
                    _THREAD_RE.match(str(r["thread_id"])) and (r.get("date") or "") >= requested_at[:17] + "00Z"]
            rows.sort(key=lambda r: r.get("date") or "", reverse=True)
            for r in rows[:MAX_MESSAGES]:
                turl = GMAIL + "#all/" + r["thread_id"]
                s.navigate(turl)

                def opened():
                    v = s.evaluate(driver("read_gmail_message"))
                    return v if isinstance(v, dict) and v.get("loaded") else None
                conv = cdp.wait(opened, 15.0, 0.5)
                with db.tx(conn):
                    _gmail_scan(conn, s)
                if not isinstance(conv, dict):
                    continue
                links = s.evaluate(pagefill.GMAIL_LINKS)
                links = links if isinstance(links, list) else []
                for m in conv.get("messages") or []:
                    if not isinstance(m, dict) or m.get("from_owner"):
                        continue
                    out.append({"id": m.get("msg_ref") or "%s:%s" % (r["thread_id"], m.get("date")),
                                "from": m.get("from"), "to": list(m.get("to") or []) + list(m.get("cc") or []),
                                "subject": conv.get("subject") or r.get("subject") or "",
                                "received_at": m.get("date"), "text": m.get("body") or "",
                                "links": [x for x in links if isinstance(x, dict)], "thread_url": turl})
    finally:
        cdp.close_tab(tab)
    return out


def after_use(thread_url: str | None, mode: str) -> None:
    """archive: open the conversation in a new tab and press `e`; mark_read already happened when it was opened."""
    if mode != "archive" or not thread_url or not thread_url.startswith(GMAIL + "#all/"):
        return
    tab = cdp.new_tab(thread_url)
    try:
        with cdp.connect(tab, budget_s=20) as s:
            s.press("e")
    finally:
        cdp.close_tab(tab)
