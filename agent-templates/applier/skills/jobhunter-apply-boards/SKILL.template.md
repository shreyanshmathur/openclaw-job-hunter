---
name: jobhunter-apply-boards
description: Recipes for opt-in job boards (Naukri, Instahyre, Foundit, Cutshort, Hirist, iimjobs, Wellfound, YC) and LinkedIn Easy Apply when enabled.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Applying through a job board

Boards are opt-in per site and only reach you when the site's apply mode is `browser` (the apply queue sends
every other mode to the person). Use this with skill `jobhunter-gate` (kind `application`); the platform is the
board id: `naukri`, `instahyre`, `foundit`, `cutshort`, `hirist`, `iimjobs`, `wellfound`, `yc`, or `linkedin`
for Easy Apply. Every command starts with `__PY__ __REPO__/scripts/jh.py`.

## Rules for every board

* You are logged in already in the `jobhunter` browser profile. A login page, an OTP prompt, "session expired"
  or a CAPTCHA is a stop: skill `jobhunter-stop-detect`. Never type a password or an OTP.
* Count every page you load: `usage add --platform site:<board> --metric page_view`. Exit 4 means the page
  budget is spent: release the job (`apply release --job <job_uid>`) and end work on that board.
* Before the token, run `read_applied_state.js` on the posting: "Applied", "Already applied" or an applied date
  means `already_done` through the precheck. Boards often show the badge only on the posting page.
* The board's experience band can differ from the employer's own posting. If the apply queue item names an
  employer page too, the package was built from the evaluated posting; do not apply when the board shows a
  different company or role.
* Screening questions that the package does not answer mean `job set-status <job_uid> --status needs_human
  --reason answer_missing`.
* One click on the final apply button. Proof is the board's own "Applied" state or success message read with
  `read_toast.js` and `read_applied_state.js`. Anything else is `gate unknown`.

## Per-board notes (verify on the page; layouts change)

| Board | Apply flow | Proof |
|---|---|---|
| Naukri | "Apply" may apply at once or open a chatbot of questions; external apply opens the company site (then use `jobhunter-apply-ats` or send to the person) | the posting shows "Applied" and an applied date |
| Instahyre | "Apply" or "Interested" on the opportunity | the card shows "Applied" |
| Foundit | "Apply" on the job page | "Applied" state on the job |
| Cutshort | "Apply" opens a short form with a note | success message, then "Applied" |
| Hirist, iimjobs | "Apply" on the job page; some roles ask questions | "Applied" state |
| Wellfound | "Apply" opens a note box; the note must be the approved cover note | "Applied" on the job card |
| YC Work at a Startup | "Apply" opens a message to the founder; the message must be the approved text | the job shows "Applied"; a weekly limit notice is a stop |
| LinkedIn Easy Apply | only when LinkedIn and Easy Apply writes are enabled; multi-step modal with "Next" and "Review" (fill), "Submit application" (commit) | "Your application was sent" and "Applied" on the job |

LinkedIn Easy Apply extras: pace with `pace wait --platform linkedin --kind easy_apply` before the reserve;
the note box in "Additional questions" is typed only from the package; "reached today's Easy Apply limit" or
"temporary pause" is a stop.
