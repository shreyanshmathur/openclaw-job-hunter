---
name: jobhunter-linkedin-posts
description: Read LinkedIn hiring posts from a content search (only when LinkedIn is enabled) and hand them to jh.py job add.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# LinkedIn hiring posts (read only)

`searches due` lists this site as `linkedin_posts` only when the person enabled LinkedIn. Every command starts
with `__PY__ __REPO__/scripts/jh.py`. You read posts; you never like, comment, repost, follow, connect or message.

## Budget

* Before opening each content search results page run
  `usage add --platform linkedin --metric content_search`. Exit 4 means the content search budget is used for
  today or this week: stop.
* One results page per search (the search's `page_budget`), newest first. Scroll it once or twice; do not open
  the posters' profiles.
* After each navigation run `__WS__/ref/drivers/detect_page.js` as skill `jobhunter-stop-detect` says. A
  commercial use limit warning, a checkpoint or a sign-in page is a stop.

## Which posts to keep

Keep a post only when a named person or company is hiring for a specific role that fits the search (for example
"We are hiring a data analyst in Bengaluru"). Skip posts that sell courses, share generic advice, repost job
lists, or ask people to comment "interested".

## What to copy

* `source_url`: the post's own URL (its "Copy link to post" address as shown, or the address of the post page if
  you opened it). The 19-digit activity id in that URL is `native_ids.post_id`.
* `title`: the role the post names, or "Hiring post: <team>" when no single role is named.
* `company`: the company the post hires for. `location`, `work_mode`, `remote_scope` as stated, else null.
* `jd_text`: the post text exactly as shown.
* `hiring_team`: the author, `{"name": "...", "title": "<their headline>", "linkedin_url": "<their profile URL
  as shown on the post>", "relation": "poster"}`.
* `apply_email`: an address the post gives for CVs ("send your CV to x@y"), and then `apply_route_hint:
  "email"`; otherwise `apply_route_hint: "human"`.
* `posted_at`: from the post age ("2d" means two days ago) as `YYYY-MM-DD`.

## Ingest file

Write `__WS__/work/<cycle_id>/linkedin_posts.json` and run `job add --file <that file>`:

```json
{
  "source": "linkedin_post",
  "discovered_via": "browser",
  "jobs": [{
    "source_url": "https://www.linkedin.com/posts/example-person_hiring-activity-0000000000000000000-abcd",
    "company": "Kestrel Commerce",
    "title": "Data Analyst",
    "location": "Bengaluru",
    "work_mode": "hybrid",
    "posted_at": "2026-09-25",
    "apply_route_hint": "email",
    "apply_email": "careers@example.com",
    "jd_text": "The post text as shown",
    "hiring_team": [{"name": "Example Person", "title": "Head of Analytics",
                     "linkedin_url": "https://www.linkedin.com/in/example-person", "relation": "poster"}],
    "native_ids": {"post_id": "0000000000000000000"}
  }]
}
```

The source is `linkedin_post` (singular) for posts. Code keys each post by its activity id, so the same post is
never added twice.
