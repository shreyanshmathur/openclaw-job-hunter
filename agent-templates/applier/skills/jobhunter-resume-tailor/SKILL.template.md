---
name: jobhunter-resume-tailor
description: Truthful resume tailoring per job; tailor JSON, modes, no-new-facts rules, resume build, R-* findings.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Resume tailoring

Read `__WS__/ref/resume_tailor.md` for the full rules. Every command starts with
`__PY__ __REPO__/scripts/jh.py`.

## Procedure

1. `resume plan --job <job_uid>` copies `base.json`, `jd.txt` and `facts.json` into your work folder and
   returns `mode` and `constraints`.
2. Mode `off`: do not tailor. Use `data.base_variant_uid` as the package's `resume_variant_uid`. When it is
   null the base resume has not passed QC yet: leave the job (`apply release --job <job_uid>`).
3. Otherwise write `__WS__/work/<cycle_id>/tailor-<job_uid>.json`:
   * `mode`: `light` unless `constraints.modes_allowed` includes `full` and rewording clearly helps.
   * `experience`: for the roles that matter, the 3 to 5 most relevant bullets in order, each
     `{"from": "E1.B2", "text": null}`; in `full` mode `text` may reword the same fact with the same numbers.
   * `skills_order`: base skills only, most relevant first.
   * `summary` (optional): one line citing `fact_ids`, using only numbers from those facts.
4. `resume build --job <job_uid> --file <tailor file>` returns `variant_uid`, `pdf_path`, `draft_uid`.
5. Exit 6 `E_QC_LINT_FAILED`: `data.lint.blocks` lists `R-*` findings. Fix exactly those and build again:
   * `R-NEW-NUMBER`, `R-SUMMARY-NUMBER`: use only the numbers of the cited base bullet or facts.
   * `R-NEW-SKILL`: remove the tool or skill; a skill the person lacks stays missing.
   * `R-LIGHT-REPHRASE`: set `text` to null in `light` mode.
   * `R-BULLET-UNKNOWN`, `R-ROLE-EMPTY`, `R-PAGES`: fix the selection.
   * `R-DASH`, `R-SPACED-HYPHEN`: rewrite without dashes.
6. Run QC on `draft_uid` (skill `jobhunter-qc-loop`). The variant goes into the package as
   `resume_variant_uid` once its draft has passed QC.

Never edit the PDF, never render a resume yourself, never upload anything but the staged file.
