---
name: jobhunter-linkedin
description: LinkedIn invitations, post-accept messages and follow-ups under a gate token; toast-only proof; two-touch sequence; invitation manager reconcile, gauge and withdrawals. Inactive unless LinkedIn is enabled.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# LinkedIn (only when `preflight` says LinkedIn writes are on)

LinkedIn automation is against LinkedIn's User Agreement whatever the volume; the person enabled it knowingly.
Keep volume tiny, pace like a person, and stop at the first warning. Every command starts with
`__PY__ __REPO__/scripts/jh.py`. Every browser call uses the shapes in skill `jobhunter-gate` ("Browser calls
the guard accepts"): clicks and typing are act kinds on refs from the latest snapshot of the same tab, drivers
are act kind `evaluate` with the driver file's exact text as `fn`.

## Identity first

Once per cycle, open your own profile page (path `/in/me/` on linkedin.com), run `read_identity.js`, write its
result to a file and run `identity check --platform linkedin --file <f>`. Exit 5 means another account is
logged in: stop the cycle.

## The two-touch rule (code enforces it; plan with it)

* Invitation with a note (touch 1), then at most one message 1 to 3 days after acceptance (touch 2). No
  follow-up after that.
* Invitation without a note (not a touch), then the post-accept message (touch 1), then one `li_followup` after
  7 to 10 days without a reply (touch 2).
* A message to an existing 1st-degree connection is a first touch and needs the full research and QC.
* Free accounts have about 3 personalised notes a month; the note policy in `preflight` says whether to send
  with a note, without a note, or to hold the person for email.

## Invitation (kind `li_invite`, draft kind `li_invite_note`)

1. Open the profile (vanity URL). `detect_page.js` then `detect --file`.
2. Precheck: `gate precheck-plan --kind li_invite --contact <contact_uid>`, then run `read_li_invite_dialog.js`
   and write the precheck file with `profile_button` and `vanity_slug` from its `profile` object:
   `profile_button` is `Connect` when the primary button is Connect, or when it is Follow and
   `more_menu_has_connect` is true; otherwise report what you saw (`Pending`, `Message`, `Follow`, `None`).
   `gate precheck --kind li_invite --platform linkedin --contact <contact_uid> --file <f>`.
3. `pace wait --platform linkedin --kind write` until `remaining_s` is 0, then
   `gate reserve --kind li_invite --draft <draft_uid> --precheck <id> --platform linkedin --contact <contact_uid>`.
4. Preparatory clicks (fill, allowed under the token): "Connect" in the top card (or "More" then "Connect"
   inside the top card's menu; never a Connect button in a side rail, those belong to other people), then
   "Add a note". Take a fresh snapshot after each of these clicks (the menu and the dialog are new, and the
   dialog may sit inside a shadow root) and use only refs from it:
   `{"action": "act", "profile": "jobhunter", "kind": "click", "ref": "e31"}`.
5. Type the approved note into the note box, nothing else:
   `{"action": "act", "profile": "jobhunter", "kind": "type", "ref": "e45", "text": "<approved note>", "slowly": true}`.
6. Read back with `read_li_invite_dialog.js`: write `note_text` exactly to
   `__WS__/work/<cycle_id>/observed-<token>.txt`. If `email_required` or `limit_notice` is true, close the dialog
   and follow `jobhunter-stop-detect`. Then `gate arm <token> --observed-file <f>`. Exit 6 means the box did not
   hold the approved text: nothing was sent; close the dialog and move on.
7. `pace wait --platform linkedin --kind dwell` until `remaining_s` is 0, then click "Send" once, by the ref the
   latest snapshot shows for it.
8. Proof is the toast only: `read_toast.js` must show "Invitation sent" (or "Your invitation to <name> was
   sent"). Do not require "Pending": creator-mode profiles keep showing Follow. Toast seen:
   `gate confirm <token> --evidence-file <f>`. No toast, an error, or anything unclear:
   `gate unknown <token> --note-file <f>`. Never click Send again.

Without a note: the draft is still required for the ledger; skip steps 4b to 6 (no "Add a note", no typing),
read back an empty `note_text` and click "Send without a note".

## Post-accept message and follow-up (kinds `li_message`, `li_followup`)

Open the conversation from the profile's "Message" button (a preparatory click under the token), type the
approved text into the message box with `slowly: true`, read back with `read_compose.js` (`observed_text`,
the box text; it starts with `Subject:` only when the compose has a subject field, which a message to a
1st-degree connection does not), arm, dwell, click "Send" once, and confirm only when the message appears in
the conversation (`read_compose.js` `messages`). Precheck for `li_message`: `conversation_has_our_message`
(from the visible messages) and `connection_degree` (`read_li_invite_dialog.js` `profile.degree`); for
`li_followup`: `conversation_has_reply`.

## InMail (kind `inmail`, draft kind `inmail`; only when `preflight` says InMail writes are on)

An InMail has a subject, and the approved hash covers it, so the read-back must show the subject too.

1. Precheck: `gate precheck-plan --kind inmail --contact <contact_uid>`, then `conversation_has_our_message`
   from `read_compose.js` `messages` of any earlier conversation with the person (none open: false), and
   `gate precheck --kind inmail --platform linkedin --contact <contact_uid> --file <f>`.
2. `pace wait --platform linkedin --kind write` until `remaining_s` is 0, then
   `gate reserve --kind inmail --draft <draft_uid> --precheck <id> --platform linkedin --contact <contact_uid>`.
3. Open the compose from the profile's "Message" button (a preparatory click under the token).
   `draft show <draft_uid> --field subject` and `draft show <draft_uid> --field body` give the approved texts.
   Type the subject into the compose's subject field and the body into the message box, each with
   `slowly: true`, nothing else.
4. Read back with `read_compose.js`. `subject_field_present` must be true; without it this is not an InMail
   compose: close it and run `gate fail <token> --reason precondition_changed --evidence-file <f>`. Write its
   `observed_text` exactly to `__WS__/work/<cycle_id>/observed-<token>.txt`: the line `Subject: <subject>`, a
   blank line, then the body, the same shape as an email read-back. Never write `text` alone for an InMail;
   without the subject line it cannot match. Then `gate arm <token> --observed-file <f>`. Exit 6 means the
   subject or the body differs: nothing was sent; close the compose and move on.
5. `pace wait --platform linkedin --kind dwell`, click "Send" once, and confirm only when the message appears
   in the conversation (`read_compose.js` `messages`); anything else is `gate unknown`.

## Replies lane checks (read only)

* Invitation manager: open https://www.linkedin.com/mynetwork/invitation-manager/sent/ and run
  `read_sent_invites.js`. Report `people_count` with
  `usage gauge --platform linkedin --metric li_invites_sent_7d --value <n>`.
* Accepted: a thread in `invite_pending` whose person is no longer in the sent list and whose profile shows
  `1st` degree and a Message button was accepted. Write
  `{"thread_key": "li:<contact_uid>", "event": "invite_accepted", "observed_at": "<UTC time>"}` and run
  `reply record --file <f>`.
* Replies: open the conversation, read with `read_compose.js`, and record each new message from them with the
  reply record file (skill `jobhunter-replies`, `inbound_id: null`, `msg_ref` = the conversation message URN or
  the conversation URL plus the message time).
* Unknown invitations (`reconcile list`): found in the sent list or accepted means
  `reconcile resolve <token> --result found --method li_sent_invites --evidence-file <f>`; otherwise
  `--result not_found`. Only the person can free a LinkedIn slot.

## Withdrawals

Invitations older than 21 days may be withdrawn, oldest first, a few per cycle, only when `preflight` lists
them. A withdrawal is kind `li_withdraw` through the same gate: "Withdraw" on the sent list is a commit.
