---
name: jobhunter-upload
description: Resume upload under an open token; resume stage, browser upload of the staged path only, verify the filename on the page, resume unstage.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Resume upload

Run every command with the exec tool; every command starts with `__PY__ __REPO__/scripts/jh.py`. Uploads happen
only inside the gate flow (skill `jobhunter-gate`), after `gate reserve` returned a token for this application.
Every browser call passes `"profile": "jobhunter"`. Files you write (evidence, notes) are whole files written
with the write tool under `__WS__/work/<cycle_id>/`, with absolute paths (never `~`, `@`, `..` or `$`).

## Procedure

1. `resume stage --variant <variant_uid> --token <token>` copies the approved PDF to the browser upload folder
   and returns `upload_path`, `filename` and `sha256`. The variant must be the one the approved package
   names; anything else is refused.
2. Upload with the page's own chooser, the staged path exactly as returned and the chooser button's `ref`
   from the latest snapshot:
   `{"action": "upload", "profile": "jobhunter", "paths": ["<upload_path from resume stage>"], "ref": "e12"}`,
   or `"inputRef"` with the file input's ref instead of `"ref"`. The guard refuses any other path, any
   other profile and any upload without the open token.
3. Verify: take a new `{"action": "snapshot", "profile": "jobhunter"}` and find `filename` in the page text (or
   a remove-file control next to it). Never trust the upload call's `ok: true` or `input.files.length`. If the
   filename does not appear, try the chooser once more; if it still does not appear, write the evidence file
   `__WS__/work/<cycle_id>/upload-<job_uid>.txt` with the write tool and run
   `gate fail <token> --reason form_blocked_before_submit --evidence-file __WS__/work/<cycle_id>/upload-<job_uid>.txt`
   while the token is still reserved (nothing was submitted).
4. After the application is confirmed, failed or marked unknown, always run `resume unstage --token <token>`.
   `cycle end` and housekeeping delete forgotten copies too, but do not rely on them.

Never upload the base PDF from `private/`, a file you wrote, or a file from a download folder.
