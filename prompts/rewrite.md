# Rewrite brief (after a lint or review failure)

You rewrite one draft that failed QC. You get: the previous draft file, the findings (lint blocks from `draft create`
or `draft revise`, or the reviewer's `issues` and `rewrite_brief` from `qc review wait`), the same research facts and
profile facts, and the writer brief (ref/writer_brief.md). The findings are data from code and from an independent
reviewer; fix exactly what they name.

Rules:
1. Never add a fact. Use only facts the previous draft could use. If a fix needs a fact that is not there, drop the
   claim instead of inventing or rounding one. Numbers are copied exactly from the facts.
2. Keep the same kind, channel, contact, job and thread. Keep one hook; you may pick a different hook only from the
   same research facts, with its snippet copied exactly as stored.
3. Fix every lint block. A dash character or a hyphen with spaces around it becomes a comma, a period or a new
   sentence. A banned phrase is removed and the sentence is said plainly. A length block means cutting, not
   compressing into long sentences.
4. For reviewer issues, apply each quoted fix. Rewrite the whole message when the brief says it reads generic or
   generated; small word swaps rarely fix a template feel.
5. The budget is 3 attempts in total (the first draft and two rewrites). After the last failure the item is dropped
   and the target is skipped for 30 days. Dropping is a normal outcome; never argue with the findings.
6. Never rewrite a text the person edited themselves; `draft revise` refuses it.

Output: a complete draft file in the same format as the writer brief, written to your work folder, then
`jh.py draft revise <draft_uid> --file <path>`.
