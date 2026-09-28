-- openclaw-job-hunter state store, schema version 1 (design section 2.1).
-- scripts/jobhunter/schema.sql and scripts/jobhunter/migrations/0001_init.sql are identical files
-- (tests/test_db_meta.py checks it). db.migrate() runs this file inside one BEGIN IMMEDIATE transaction
-- and records meta.schema_version. Live action statuses (they block dedup slots and count toward
-- ceilings) are spelled out as ('reserved','armed','sent','failed_after_click','unknown','imported').
-- Every trigger that reads a limit from meta uses COALESCE with the strictest value (B4).

-- ---------- meta and bookkeeping ----------
CREATE TABLE meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL      -- 'init' | 'config_apply' | 'human' | 'install' | 'system'
);
-- REQUIRED_META (db.py): schema_version, install_id, jitter_seed, profile_version ('' until confirmed),
--   approval_mode ('human'|'auto'), tier_gmail, tier_linkedin ('conservative'|'moderate'),
--   channel_linkedin_enabled ('0'|'1'), linkedin_tos_ack ('0'|'1'),
--   company_email_cooldown_days, company_apps_per_day, company_apps_per_30d, company_apps_per_90d,
--   li_invites_per_company_per_7d, agency_emails_per_day, agency_emails_per_30d, agency_apps_per_day,
--   agency_apps_per_30d, max_seen_ts.
-- REQUIRED_FOR_QC: reviewer_prompt_sha256, qc_agents_md_sha256 (written by `install render-workspaces`).
-- Optional: 'raise:<dotted.path>' (PIN-gated loosening, 4.1), golden_last ('<n>/20|<model>|<prompt sha>|<ts>'),
--   last_digest_at, auto_suggested_at, pin_set_at, mail_connected_at, 'ats_human_queue:<ats>' ('1' after two missing
--   confirmation emails on that ATS, 4.5; cleared by the human with `config lower`/`config raise` semantics).
-- Writers: `init` writes defaults; `config apply` writes the trigger values from the effective config;
--   authority keys (approval_mode, tier_*, channel_linkedin_enabled, linkedin_tos_ack, raise:*) only through
--   human-only commands (PIN), except that `config apply` may LOWER them (auto->human, moderate->conservative, 1->0).

CREATE TABLE cycles (
  cycle_id TEXT PRIMARY KEY,
  lane TEXT NOT NULL CHECK (lane IN ('api','scout','evaluator','applier','outreach','replies','mailer','manual')),
  agent_id TEXT, slot_id INTEGER REFERENCES dispatch_slots(id),
  started_at TEXT NOT NULL, ended_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','ok','no_go','error','aborted_breaker','stopped_guard')),
  summary_json TEXT
);
CREATE TABLE locks (name TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE dispatch_slots (
  id INTEGER PRIMARY KEY, local_date TEXT NOT NULL,
  lane TEXT NOT NULL CHECK (lane IN ('scout','evaluator','applier','outreach','replies')),
  slot_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('planned','triggered','skipped','missed')),
  reason TEXT, triggered_at TEXT, cycle_id TEXT,
  UNIQUE (lane, slot_at)
);

-- ---------- companies ----------
CREATE TABLE companies (
  id INTEGER PRIMARY KEY,
  company_uid TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  domain TEXT,                                   -- registrable domain, never free-mail, ATS or hosting
  is_agency INTEGER NOT NULL DEFAULT 0 CHECK (is_agency IN (0,1)),
  agency_source TEXT CHECK (agency_source IN ('bundled_list','human')),
  contact_state TEXT NOT NULL DEFAULT 'none'
     CHECK (contact_state IN ('none','contacted','active_thread','do_not_contact')),
  contact_state_reason TEXT,
  merged_into INTEGER REFERENCES companies(id),  -- set on the losing row of a merge; its aliases moved
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (is_agency = 0 OR agency_source IS NOT NULL)
);
CREATE TABLE company_aliases (
  alias_key TEXT PRIMARY KEY,                    -- 'dom:<etld1>' | 'id:<alnum>' | 'ats:<ats>:<tenant>'
  company_id INTEGER NOT NULL REFERENCES companies(id),
  kind TEXT NOT NULL CHECK (kind IN ('dom','name','loose','label','tenant','ats','human')),
  source TEXT NOT NULL CHECK (source IN ('ats','email','careers_url','job_board','private_aliases','human',
                                         'exclusions','merge')),
  created_at TEXT NOT NULL
);
CREATE INDEX i_alias_company ON company_aliases(company_id);
CREATE TABLE company_merges (
  id INTEGER PRIMARY KEY,
  from_id INTEGER NOT NULL REFERENCES companies(id), to_id INTEGER NOT NULL REFERENCES companies(id),
  matched_keys TEXT NOT NULL,                    -- JSON array
  strength TEXT NOT NULL CHECK (strength IN ('exact','fuzzy')),
  by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE company_distinct (                  -- written by `companies split`; resolve never merges these
  a_id INTEGER NOT NULL REFERENCES companies(id), b_id INTEGER NOT NULL REFERENCES companies(id),
  by TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (a_id, b_id), CHECK (a_id < b_id)
);
CREATE TABLE ats_tenants (
  ats TEXT NOT NULL, tenant TEXT NOT NULL, company_id INTEGER REFERENCES companies(id),
  active INTEGER NOT NULL DEFAULT 1, validated_at TEXT, source TEXT NOT NULL,
  PRIMARY KEY (ats, tenant)
);

-- ---------- jobs ----------
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  job_uid TEXT NOT NULL UNIQUE,
  canonical_key TEXT NOT NULL UNIQUE,            -- best key at insert: ats > board > post > url (2.4)
  fingerprint TEXT NOT NULL,                     -- sha1(company_uid|norm_title|norm_city)
  source TEXT NOT NULL,                          -- sources.* id, 'linkedin_jobs', 'linkedin_post', board ids
  discovered_via TEXT NOT NULL CHECK (discovered_via IN ('api','browser','human')),
  source_url TEXT NOT NULL, apply_url TEXT,
  apply_route TEXT NOT NULL DEFAULT 'unknown'
     CHECK (apply_route IN ('ats_form','board_inapp','easy_apply','email','human','unknown')),
  apply_email TEXT,
  company_id INTEGER REFERENCES companies(id),
  company_name_raw TEXT NOT NULL,
  title TEXT NOT NULL, norm_title TEXT NOT NULL, role_key TEXT NOT NULL,
  location_raw TEXT, norm_city TEXT,
  work_mode TEXT NOT NULL DEFAULT 'unknown' CHECK (work_mode IN ('remote','hybrid','onsite','unknown')),
  remote_scope TEXT, employment_type TEXT,
  years_min REAL, years_max REAL,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_period TEXT,
  posted_at TEXT, discovered_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN (
     'new','prefilter_rejected','excluded','duplicate','eval_queued','evaluating','eligible','borderline',
     'rejected','apply_queued','awaiting_approval','applying','applied','apply_failed','needs_human','closed')),
  status_reason TEXT,                            -- reason_code (6.2) or transition reason
  duplicate_of INTEGER REFERENCES jobs(id),
  human_call TEXT CHECK (human_call IN ('apply_anyway','never')),
  claimed_by TEXT, claimed_until TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX i_jobs_status ON jobs(status, updated_at);
CREATE INDEX i_jobs_fp ON jobs(fingerprint, discovered_at);
CREATE INDEX i_jobs_company ON jobs(company_id);
CREATE TABLE job_keys (
  key TEXT PRIMARY KEY,                          -- 'ats:greenhouse:<id>', 'board:naukri:<id>', 'post:linkedin:<id>', 'url:<sha1>'
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  kind TEXT NOT NULL CHECK (kind IN ('ats','board','post','url','alias')),
  created_at TEXT NOT NULL
);
CREATE TABLE job_texts (job_id INTEGER PRIMARY KEY REFERENCES jobs(id), jd_text TEXT NOT NULL,
  jd_sha256 TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE job_hiring_team (
  job_id INTEGER NOT NULL REFERENCES jobs(id), contact_id INTEGER NOT NULL REFERENCES contacts(id),
  relation TEXT NOT NULL CHECK (relation IN ('hiring_manager','recruiter','poster','founder')),
  PRIMARY KEY (job_id, contact_id)
);
CREATE TABLE evaluations (                       -- one row per job; re-evaluation overwrites (history in events log)
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
  stage TEXT NOT NULL CHECK (stage IN ('prefilter','llm')),
  score INTEGER CHECK (score BETWEEN 0 AND 100),
  verdict TEXT NOT NULL CHECK (verdict IN ('apply','borderline','skip','human_only')),
  reason_code TEXT NOT NULL, reason_text TEXT NOT NULL,
  gates_failed TEXT NOT NULL DEFAULT '[]', scorecard_json TEXT, clamped INTEGER NOT NULL DEFAULT 0,
  profile_version TEXT NOT NULL, model TEXT, evaluated_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- ---------- people ----------
CREATE TABLE contacts (
  id INTEGER PRIMARY KEY, contact_uid TEXT NOT NULL UNIQUE,
  full_name TEXT, first_name TEXT, title TEXT,
  company_id INTEGER REFERENCES companies(id),
  role_type TEXT NOT NULL DEFAULT 'other'
     CHECK (role_type IN ('hiring_manager','recruiter','founder','employee','role_inbox','other')),
  locale TEXT,
  email TEXT, email_grade TEXT CHECK (email_grade IN ('A','B','C')), email_evidence_url TEXT,
  email_mx_ok INTEGER, email_invalid INTEGER NOT NULL DEFAULT 0,
  linkedin_url TEXT, li_slug TEXT, needs_vanity INTEGER NOT NULL DEFAULT 0,
  do_not_contact INTEGER NOT NULL DEFAULT 0, dnc_reason TEXT,
  merged_into INTEGER REFERENCES contacts(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE contact_keys (                      -- every identity of one person maps to one contact row
  key TEXT PRIMARY KEY,
  contact_id INTEGER NOT NULL REFERENCES contacts(id),
  kind TEXT NOT NULL CHECK (kind IN ('email','email_norm','li_slug','li_member','li_legacy','li_sales','pname')),
  created_at TEXT NOT NULL
);
CREATE INDEX i_contact_keys_contact ON contact_keys(contact_id);

CREATE TABLE research_facts (
  id INTEGER PRIMARY KEY, fact_uid TEXT NOT NULL UNIQUE,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('person','company','job')),
  subject_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  snippet TEXT NOT NULL CHECK (length(snippet) <= 300),
  source_type TEXT NOT NULL, source_url TEXT NOT NULL CHECK (source_url LIKE 'https://%'),
  published_at TEXT, retrieved_at TEXT NOT NULL,
  injection_flag INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX i_research_subject ON research_facts(subject_kind, subject_id);

-- ---------- drafts and QC ----------
CREATE TABLE drafts (
  id INTEGER PRIMARY KEY, draft_uid TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('cold_email','followup_email','application_email','li_invite_note',
        'li_message','li_followup','inmail','form_answer','cover_note','resume','application_package')),
  channel TEXT NOT NULL,                         -- linter channel id (email_cold, email_founder, li_connect, resume, ...)
  send_route TEXT NOT NULL CHECK (send_route IN ('mailer','browser','none')),   -- 'none' for parts of a package
  job_id INTEGER REFERENCES jobs(id), contact_id INTEGER REFERENCES contacts(id),
  company_id INTEGER REFERENCES companies(id), thread_key TEXT,
  parent_draft_id INTEGER REFERENCES drafts(id),
  recipient TEXT,                                -- code-derived: normalized email or LinkedIn slug
  subject TEXT, body TEXT,                       -- exact text; the signature is appended by code
  payload_json TEXT NOT NULL,
  attachment_variant_id INTEGER REFERENCES resume_variants(id),   -- application_email only
  text_sha256 TEXT NOT NULL,                     -- sha256(canonical_send_text(...)), attachment included
  attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 3),
  human_edits INTEGER NOT NULL DEFAULT 0 CHECK (human_edits BETWEEN 0 AND 3),
  status TEXT NOT NULL CHECK (status IN ('drafted','lint_failed','review_pending','review_failed','qc_passed',
        'awaiting_approval','approved','skipped_by_human','dropped_qc','expired','sent','superseded')),
  status_reason TEXT,
  approved_by TEXT CHECK (approved_by IN ('auto','human:chat','human:sheet','human:cli')),
  approved_at TEXT, expires_at TEXT, send_after TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (status NOT IN ('approved','sent') OR approved_by IS NOT NULL)
);
CREATE INDEX i_drafts_status ON drafts(status);
-- one open first-contact email draft per company, one open first-touch draft per person (minor 14)
CREATE UNIQUE INDEX u_open_email_draft_company ON drafts(company_id)
  WHERE kind IN ('cold_email','application_email') AND company_id IS NOT NULL
    AND status IN ('drafted','lint_failed','review_pending','review_failed','qc_passed','awaiting_approval','approved');
CREATE UNIQUE INDEX u_open_first_touch_draft_person ON drafts(contact_id)
  WHERE kind IN ('cold_email','li_invite_note','inmail') AND contact_id IS NOT NULL
    AND status IN ('drafted','lint_failed','review_pending','review_failed','qc_passed','awaiting_approval','approved');

CREATE TABLE approval_codes (
  code TEXT PRIMARY KEY,                         -- a code row is never deleted before 30 days
  draft_id INTEGER NOT NULL REFERENCES drafts(id),
  issued_at TEXT NOT NULL, closed_at TEXT
);
CREATE UNIQUE INDEX u_open_code_per_draft ON approval_codes(draft_id) WHERE closed_at IS NULL;

CREATE TABLE qc_results (
  id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL REFERENCES drafts(id), attempt INTEGER NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('lint','review','presend','human_edit_lint')),
  human_edit_no INTEGER NOT NULL DEFAULT 0,
  passed INTEGER NOT NULL,
  blocks_json TEXT NOT NULL DEFAULT '[]', warns_json TEXT NOT NULL DEFAULT '[]',
  review_json TEXT, weighted_score REAL, lowest_criterion TEXT, gates_failed TEXT,
  model_verdict TEXT, code_verdict TEXT, text_sha256 TEXT NOT NULL, reviewer_model TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (draft_id, attempt, stage, human_edit_no)
);
CREATE TABLE qc_jobs (
  id INTEGER PRIMARY KEY, qjob_uid TEXT NOT NULL UNIQUE,
  draft_id INTEGER NOT NULL REFERENCES drafts(id), attempt INTEGER NOT NULL, try_no INTEGER NOT NULL CHECK (try_no IN (1,2)),
  nonce TEXT NOT NULL, packet_path TEXT NOT NULL, session_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued','running','done','failed','timeout')),
  worker_pid INTEGER, heartbeat_at TEXT, started_at TEXT, finished_at TEXT,
  verdict TEXT CHECK (verdict IN ('pass','fail')), error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (draft_id, attempt, try_no)
);

-- ---------- resumes and applications ----------
CREATE TABLE resume_variants (
  id INTEGER PRIMARY KEY, variant_uid TEXT NOT NULL UNIQUE,
  job_id INTEGER REFERENCES jobs(id),            -- NULL for the base variant
  mode TEXT NOT NULL CHECK (mode IN ('base','light','full')),
  base_sha256 TEXT NOT NULL, tailor_json TEXT,
  pdf_path TEXT NOT NULL, docx_path TEXT, txt_path TEXT NOT NULL, pdf_sha256 TEXT NOT NULL,
  draft_id INTEGER REFERENCES drafts(id),
  created_at TEXT NOT NULL
);
CREATE TABLE staged_files (
  token TEXT PRIMARY KEY,                        -- the application action token the stage belongs to
  variant_id INTEGER NOT NULL REFERENCES resume_variants(id),
  path TEXT NOT NULL, sha256 TEXT NOT NULL, staged_at TEXT NOT NULL, removed_at TEXT
);
CREATE TABLE applications (
  id INTEGER PRIMARY KEY,
  action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id),
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  route TEXT NOT NULL, resume_variant_id INTEGER REFERENCES resume_variants(id),
  package_draft_id INTEGER REFERENCES drafts(id),
  confirmation TEXT, confirmation_email_at TEXT,
  outcome TEXT NOT NULL DEFAULT 'none' CHECK (outcome IN ('none','rejected','screening_call','interview',
        'offer','withdrawn','role_closed','ghosted')),
  outcome_at TEXT, notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- ---------- the outbox / ledger ----------
CREATE TABLE prechecks (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, platform TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('agent','code_imap')),
  contact_id INTEGER, company_id INTEGER, job_id INTEGER, thread_key TEXT,
  result TEXT NOT NULL CHECK (result IN ('clear','already_done','uncertain')),
  checks_json TEXT NOT NULL, cycle_id TEXT, created_at TEXT NOT NULL,
  used_by_action INTEGER UNIQUE
);
CREATE TABLE detections (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('agent','guard','code')),
  url TEXT, http_status INTEGER,
  verdict TEXT NOT NULL CHECK (verdict IN ('clear','stop','unknown_state')),
  code TEXT, matched TEXT, cycle_id TEXT, created_at TEXT NOT NULL
);

CREATE TABLE actions (
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('application','application_email','cold_email','followup_email',
        'li_invite','li_message','li_followup','inmail','li_withdraw','referral_ask')),
  route TEXT NOT NULL CHECK (route IN ('mailer','browser','import')),
  first_touch INTEGER NOT NULL CHECK (first_touch IN (0,1)),       -- computed by code, verified by trigger
  li_note INTEGER NOT NULL DEFAULT 0 CHECK (li_note IN (0,1)),
  li_msg_seq INTEGER CHECK (li_msg_seq IN (1,2)),                   -- message-bearing LinkedIn touch number
  platform TEXT NOT NULL,                                           -- gmail | linkedin | greenhouse | lever | ...
  agent_id TEXT,                                                    -- 'jobhunter-applier' | 'jobhunter-outreach' | 'system:mailer' | 'human'
  contact_id INTEGER REFERENCES contacts(id),
  company_id INTEGER REFERENCES companies(id),
  job_id INTEGER REFERENCES jobs(id),
  role_key TEXT, thread_key TEXT,
  recipient TEXT,                                                   -- normalized email or li slug; NULL for forms
  draft_id INTEGER REFERENCES drafts(id),
  approved_sha256 TEXT, observed_sha256 TEXT, attachment_sha256 TEXT,
  message_id TEXT UNIQUE,                                           -- RFC 5322 Message-ID set by the mailer
  precheck_id INTEGER REFERENCES prechecks(id),
  detect_id INTEGER REFERENCES detections(id),
  status TEXT NOT NULL CHECK (status IN ('reserved','armed','sent','failed','failed_after_click','unknown','imported')),
  fail_reason TEXT CHECK (fail_reason IS NULL OR fail_reason IN ('observed_text_mismatch','not_attempted',
        'precondition_changed','form_blocked_before_submit','smtp_rejected_before_data','smtp_rejected',
        'not_found_twice','human_confirmed_not_sent','platform_error_after_click')),
  reserved_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', reserved_at) IS reserved_at),  -- windows need it
  armed_at TEXT, expires_at TEXT NOT NULL,
  sent_at TEXT, resolved_at TEXT,
  cycle_id TEXT, lane TEXT,
  evidence TEXT, note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX i_actions_window ON actions(platform, kind, reserved_at);
CREATE INDEX i_actions_company ON actions(company_id, kind, reserved_at);
CREATE INDEX i_actions_contact ON actions(contact_id, kind);
CREATE INDEX i_actions_status ON actions(status, expires_at);

CREATE UNIQUE INDEX u_first_touch_person ON actions(contact_id)
  WHERE first_touch = 1 AND contact_id IS NOT NULL AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported');
CREATE UNIQUE INDEX u_application_job ON actions(job_id)
  WHERE kind IN ('application','application_email') AND job_id IS NOT NULL AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported');
CREATE UNIQUE INDEX u_followup_thread ON actions(thread_key)
  WHERE kind IN ('followup_email','li_followup') AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported');
CREATE UNIQUE INDEX u_li_message_person ON actions(contact_id)
  WHERE kind = 'li_message' AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported');
CREATE UNIQUE INDEX u_li_seq ON actions(contact_id, li_msg_seq)
  WHERE li_msg_seq IS NOT NULL AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported');
CREATE UNIQUE INDEX u_referral_person ON actions(contact_id) WHERE kind = 'referral_ask' AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported');
CREATE UNIQUE INDEX u_action_draft ON actions(draft_id) WHERE draft_id IS NOT NULL AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported');
CREATE UNIQUE INDEX u_action_precheck ON actions(precheck_id) WHERE precheck_id IS NOT NULL;
CREATE UNIQUE INDEX u_agent_open_token ON actions(agent_id)
  WHERE agent_id IS NOT NULL AND status IN ('reserved','armed');   -- one open token per agent (the guard relies on it)

-- first_touch must match the kind and the thread state (code computes it; this catches code bugs)
CREATE TRIGGER t_first_touch_rules BEFORE INSERT ON actions
WHEN (NEW.kind IN ('cold_email','li_invite','inmail') AND NEW.first_touch <> 1)
  OR (NEW.kind IN ('followup_email','li_followup','li_withdraw','application','referral_ask') AND NEW.first_touch <> 0)
  OR (NEW.kind = 'application_email' AND NEW.first_touch <>
        CASE WHEN NEW.contact_id IS NOT NULL
              AND COALESCE((SELECT role_type FROM contacts WHERE id = NEW.contact_id), 'other') <> 'role_inbox'
             THEN 1 ELSE 0 END)
  OR (NEW.kind = 'li_message' AND NEW.first_touch <>
        CASE WHEN EXISTS (SELECT 1 FROM threads t WHERE t.contact_id = NEW.contact_id AND t.channel = 'linkedin'
                          AND t.state = 'invite_accepted') THEN 0 ELSE 1 END)
BEGIN SELECT RAISE(ABORT, 'E_INTERNAL'); END;

-- at most two message-bearing LinkedIn touches per person (M3); seq must be the next number and at most 2
CREATE TRIGGER t_li_seq_rules BEFORE INSERT ON actions
WHEN ((NEW.kind IN ('li_message','li_followup','inmail') OR (NEW.kind = 'li_invite' AND NEW.li_note = 1))
       AND NEW.li_msg_seq IS NULL)
  OR (NEW.li_msg_seq IS NOT NULL AND NOT (NEW.kind IN ('li_message','li_followup','inmail')
                                           OR (NEW.kind = 'li_invite' AND NEW.li_note = 1)))
  OR (NEW.li_msg_seq IS NOT NULL AND NEW.li_msg_seq > 2)
  OR (NEW.li_msg_seq IS NOT NULL AND NEW.li_msg_seq <> 1 + (SELECT count(*) FROM actions a
        WHERE a.contact_id = NEW.contact_id AND a.li_msg_seq IS NOT NULL AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')))
BEGIN SELECT RAISE(ABORT, 'E_DUP_LI_TOUCH'); END;

-- a follow-up belongs to an existing open thread, the same person, company and recipient (M2 f)
CREATE TRIGGER t_followup_binding BEFORE INSERT ON actions
WHEN NEW.kind IN ('followup_email','li_followup')
 AND NOT EXISTS (SELECT 1 FROM threads t
                 WHERE t.thread_key = NEW.thread_key AND t.state = 'open' AND t.followup_action_id IS NULL
                   AND t.contact_id IS NEW.contact_id AND t.company_id IS NEW.company_id
                   AND NEW.recipient IS (SELECT a.recipient FROM actions a WHERE a.id = t.first_action_id))
BEGIN SELECT RAISE(ABORT, 'E_FOLLOWUP_BINDING'); END;

-- rows that lost a merge can never be referenced again
CREATE TRIGGER t_no_merged_refs BEFORE INSERT ON actions
WHEN (NEW.company_id IS NOT NULL AND (SELECT merged_into FROM companies WHERE id = NEW.company_id) IS NOT NULL)
  OR (NEW.contact_id IS NOT NULL AND (SELECT merged_into FROM contacts WHERE id = NEW.contact_id) IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'E_INTERNAL'); END;

-- The company rules below refuse new sends. Imported history (route 'import': an action that already
-- happened outside the ledger) is exempt from them, so it is always recorded and blocks what comes after.

-- company email cooldown (non-agency), fallback 365 days if the meta row is missing, empty, not a number
-- or not positive (B4)
CREATE TRIGGER t_company_email_cooldown BEFORE INSERT ON actions
WHEN NEW.kind IN ('cold_email','application_email') AND NEW.company_id IS NOT NULL AND NEW.route <> 'import'
 AND COALESCE((SELECT is_agency FROM companies WHERE id = NEW.company_id), 0) = 0
 AND EXISTS (SELECT 1 FROM actions a
             WHERE a.company_id = NEW.company_id AND a.kind IN ('cold_email','application_email')
               AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
               AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-' ||
                   COALESCE((SELECT CAST(CAST(value AS INTEGER) AS TEXT) FROM meta
                             WHERE key = 'company_email_cooldown_days' AND CAST(value AS INTEGER) > 0), '365')
                   || ' days'))
BEGIN SELECT RAISE(ABORT, 'E_COMPANY_COOLDOWN'); END;

-- per-company application caps (non-agency); fallback 1 for every window
CREATE TRIGGER t_company_app_caps BEFORE INSERT ON actions
WHEN NEW.kind IN ('application','application_email') AND NEW.company_id IS NOT NULL AND NEW.route <> 'import'
 AND COALESCE((SELECT is_agency FROM companies WHERE id = NEW.company_id), 0) = 0
 AND ((SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
         AND a.kind IN ('application','application_email') AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
         AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-1 days'))
       >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'company_apps_per_day'), '1') AS INTEGER)
   OR (SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
         AND a.kind IN ('application','application_email') AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
         AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-30 days'))
       >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'company_apps_per_30d'), '1') AS INTEGER)
   OR (SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
         AND a.kind IN ('application','application_email') AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
         AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-90 days'))
       >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'company_apps_per_90d'), '1') AS INTEGER))
BEGIN SELECT RAISE(ABORT, 'E_COMPANY_APP_CAP'); END;

-- agencies get their own caps instead of the company cooldown (M15); fallback 1
CREATE TRIGGER t_agency_caps BEFORE INSERT ON actions
WHEN NEW.company_id IS NOT NULL AND NEW.route <> 'import' AND COALESCE((SELECT is_agency FROM companies WHERE id = NEW.company_id), 0) = 1
 AND ((NEW.kind IN ('cold_email','application_email') AND (
         (SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
            AND a.kind IN ('cold_email','application_email') AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
            AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-1 days'))
          >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'agency_emails_per_day'), '1') AS INTEGER)
      OR (SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
            AND a.kind IN ('cold_email','application_email') AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
            AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-30 days'))
          >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'agency_emails_per_30d'), '1') AS INTEGER)))
   OR (NEW.kind IN ('application','application_email') AND (
         (SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
            AND a.kind IN ('application','application_email') AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
            AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-1 days'))
          >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'agency_apps_per_day'), '1') AS INTEGER)
      OR (SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
            AND a.kind IN ('application','application_email') AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
            AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-30 days'))
          >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'agency_apps_per_30d'), '1') AS INTEGER))))
BEGIN SELECT RAISE(ABORT, 'E_AGENCY_CAP'); END;

-- LinkedIn first touches per company per rolling 7 days (invites and first-touch messages); fallback 1
CREATE TRIGGER t_company_li_cap BEFORE INSERT ON actions
WHEN (NEW.kind = 'li_invite' OR (NEW.kind = 'li_message' AND NEW.first_touch = 1) OR NEW.kind = 'inmail')
 AND NEW.company_id IS NOT NULL AND NEW.route <> 'import'
 AND (SELECT count(*) FROM actions a WHERE a.company_id = NEW.company_id
        AND (a.kind IN ('li_invite','inmail') OR (a.kind = 'li_message' AND a.first_touch = 1))
        AND a.status IN ('reserved','armed','sent','failed_after_click','unknown','imported')
        AND a.reserved_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.reserved_at, '-7 days'))
     >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'li_invites_per_company_per_7d'), '1') AS INTEGER)
BEGIN SELECT RAISE(ABORT, 'E_COMPANY_LI_CAP'); END;

-- active conversation or do-not-contact blocks new first touches, follow-ups and LinkedIn messages
CREATE TRIGGER t_company_blocked BEFORE INSERT ON actions
WHEN (NEW.first_touch = 1 OR NEW.kind IN ('followup_email','li_followup','li_message','application',
                                          'application_email'))
 AND NEW.company_id IS NOT NULL AND NEW.route <> 'import'
 AND (SELECT contact_state FROM companies WHERE id = NEW.company_id) IN ('active_thread','do_not_contact')
BEGIN SELECT RAISE(ABORT, 'E_COMPANY_BLOCKED'); END;

CREATE TRIGGER t_contact_dnc BEFORE INSERT ON actions
WHEN NEW.contact_id IS NOT NULL AND NEW.route <> 'import'
 AND COALESCE((SELECT do_not_contact FROM contacts WHERE id = NEW.contact_id), 0) = 1
BEGIN SELECT RAISE(ABORT, 'E_CONTACT_DNC'); END;

-- a referral ask only in a thread where that person replied (6.1 step 9, outreach.referral_ask_only_after_reply)
CREATE TRIGGER t_referral_after_reply BEFORE INSERT ON actions
WHEN NEW.kind = 'referral_ask' AND NEW.route <> 'import'
 AND NOT EXISTS (SELECT 1 FROM threads t
                 WHERE t.thread_key = NEW.thread_key AND t.contact_id IS NEW.contact_id
                   AND t.reply_class IN ('positive','neutral','referral_offered')
                   AND t.state NOT IN ('closed','bounced'))
BEGIN SELECT RAISE(ABORT, 'E_PRECONDITION'); END;

-- action status graph (M1). 'failed' frees a slot and needs a code-checkable reason (a NULL reason is
-- refused: COALESCE keeps the NOT (...) from evaluating to NULL, which would let the update through).
CREATE TRIGGER t_action_status_graph BEFORE UPDATE OF status ON actions
WHEN OLD.status <> NEW.status AND NOT (
     (OLD.status = 'reserved' AND NEW.status IN ('armed','unknown'))
  OR (OLD.status = 'reserved' AND NEW.status = 'failed' AND COALESCE(NEW.fail_reason, '') IN ('observed_text_mismatch',
        'not_attempted','precondition_changed','form_blocked_before_submit','smtp_rejected_before_data'))
  OR (OLD.status = 'armed' AND NEW.status IN ('sent','unknown','failed_after_click'))
  OR (OLD.status = 'armed' AND NEW.status = 'failed' AND COALESCE(NEW.fail_reason, '') = 'smtp_rejected')
  OR (OLD.status = 'unknown' AND NEW.status IN ('sent','failed_after_click'))
  OR (OLD.status = 'unknown' AND NEW.status = 'failed'
        AND COALESCE(NEW.fail_reason, '') IN ('not_found_twice','human_confirmed_not_sent')))
BEGIN SELECT RAISE(ABORT, 'E_BAD_TRANSITION'); END;

CREATE TABLE reconcile_checks (
  id INTEGER PRIMARY KEY, action_id INTEGER NOT NULL REFERENCES actions(id),
  method TEXT NOT NULL CHECK (method IN ('imap_message_id','imap_sent_search','web_sent_search',
        'web_outbox_search','web_scheduled_search','li_sent_invites','li_conversation','ats_page',
        'inbox_confirmation','human')),
  result TEXT NOT NULL CHECK (result IN ('found','not_found','error')),
  detail TEXT, checked_at TEXT NOT NULL, by TEXT NOT NULL
);

-- ---------- conversations ----------
CREATE TABLE threads (
  id INTEGER PRIMARY KEY,
  thread_key TEXT NOT NULL UNIQUE,               -- 'em:<first action token>' | 'li:<contact_uid>'
  channel TEXT NOT NULL CHECK (channel IN ('email','linkedin')),
  contact_id INTEGER REFERENCES contacts(id), company_id INTEGER REFERENCES companies(id),
  job_id INTEGER REFERENCES jobs(id),
  first_action_id INTEGER NOT NULL REFERENCES actions(id),
  platform_ref TEXT,                             -- Gmail thread URL, or LinkedIn conversation URL
  gmail_thread_id TEXT, first_message_id TEXT,   -- mailer route
  subject TEXT,
  state TEXT NOT NULL CHECK (state IN ('open','invite_pending','invite_accepted','invite_withdrawn',
        'followed_up','replied','closed','bounced')),
  followup_due_at TEXT, followup_action_id INTEGER REFERENCES actions(id),
  last_outbound_at TEXT, last_checked_at TEXT,
  reply_class TEXT CHECK (reply_class IN ('positive','neutral','negative','not_hiring','referral_offered',
        'auto_ack','out_of_office','bounce','complaint','opt_out')),
  reply_at TEXT, reply_summary TEXT CHECK (length(reply_summary) <= 300),
  outcome TEXT NOT NULL DEFAULT 'none' CHECK (outcome IN ('none','rejected','screening_call','interview',
        'offer','ghosted','role_closed','referred')),
  needs_human INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE replies (
  id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
  received_at TEXT NOT NULL, classification TEXT NOT NULL,
  classified_by TEXT NOT NULL CHECK (classified_by IN ('code','agent','human')),
  summary TEXT NOT NULL CHECK (length(summary) <= 300),
  platform_msg_ref TEXT, created_at TEXT NOT NULL,
  UNIQUE (thread_id, platform_msg_ref)
);
CREATE TABLE inbound_messages (                  -- fetched by the mailer (IMAP) or reported by the replies lane
  id INTEGER PRIMARY KEY, msg_ref TEXT NOT NULL UNIQUE,
  channel TEXT NOT NULL CHECK (channel IN ('email','linkedin')),
  thread_id INTEGER REFERENCES threads(id), company_id INTEGER REFERENCES companies(id),
  from_domain TEXT, received_at TEXT NOT NULL,
  code_class TEXT CHECK (code_class IN ('auto_ack','out_of_office','bounce','application_confirmation')),
  packet_path TEXT,                              -- WS/outreach/inbox/reply-<id>.json while pending; deleted after
  status TEXT NOT NULL CHECK (status IN ('pending','classified','ignored')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- ---------- limits, breakers, exclusions ----------
CREATE TABLE counters (                          -- READ actions and gauges; writes are counted from `actions`
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, platform TEXT NOT NULL,
  metric TEXT NOT NULL,                          -- page_view, profile_view, people_search, content_search,
                                                 -- job_search_page, li_pending_invites, li_notes_quota_left,
                                                 -- li_invites_sent_7d (gauges)
  n INTEGER NOT NULL DEFAULT 1,
  kind TEXT NOT NULL DEFAULT 'inc' CHECK (kind IN ('inc','gauge')),
  cycle_id TEXT
);
CREATE INDEX i_counters ON counters(platform, metric, ts);
CREATE TABLE breakers (
  scope TEXT PRIMARY KEY,                        -- 'global','gmail','gmail.cold','linkedin','linkedin.invites',
                                                 -- 'linkedin.messages','linkedin.easy_apply','linkedin.search',
                                                 -- 'site:<name>','ats','api:<source>','pause:<area>'
  state TEXT NOT NULL CHECK (state IN ('closed','open')),
  reason_code TEXT, detail TEXT, evidence_path TEXT,
  tripped_at TEXT, min_cooldown_until TEXT,
  requires_human INTEGER NOT NULL DEFAULT 1, auto_close_at TEXT, resume_policy TEXT,
  tripped_by_cycle TEXT, reset_at TEXT, reset_by TEXT, reset_note TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE breaker_events (id INTEGER PRIMARY KEY, scope TEXT NOT NULL,
  event TEXT NOT NULL CHECK (event IN ('trip','reset','auto_close')), reason_code TEXT, detail TEXT,
  by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE warmup (
  platform TEXT NOT NULL, lane_kind TEXT NOT NULL,
  started_at TEXT NOT NULL, restarted_at TEXT, restart_reason TEXT, restart_week INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (platform, lane_kind)
);
CREATE TABLE exclusions (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('company','email','domain','linkedin','job_url')),
  value_raw TEXT NOT NULL, value_key TEXT NOT NULL, reason TEXT,
  source TEXT NOT NULL CHECK (source IN ('private_csv','reply_optout','complaint','bounce','human','forget')),
  active INTEGER NOT NULL DEFAULT 1, deactivated_at TEXT, deactivated_by TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (type, value_key)
);
CREATE TABLE target_skips (                      -- minor 13: a dropped target is not re-picked for 30 days
  target_key TEXT PRIMARY KEY,                   -- 'contact:<uid>' | 'job:<uid>' | 'company:<uid>'
  reason TEXT NOT NULL, drops INTEGER NOT NULL DEFAULT 1, until TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- ---------- human loop, auth, notifications, sheet ----------
CREATE TABLE human_tasks (
  id INTEGER PRIMARY KEY, task_uid TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('apply_manually','answer_question','review_reply','resolve_unknown',
        'confirm_not_sent','reset_breaker','confirm_profile','relax_gate','relogin','confirm_company_merge',
        'confirm_agency','review_audit_mismatch','connect_mail','suggest_auto')),
  job_id INTEGER, draft_id INTEGER, thread_id INTEGER, action_id INTEGER, company_id INTEGER,
  question TEXT, detail TEXT,
  created_at TEXT NOT NULL, done_at TEXT, resolution TEXT
);
CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  dedupe_key TEXT NOT NULL UNIQUE,               -- 'approval:A7K2', 'breaker:linkedin:2026-09-27', ...
  priority TEXT NOT NULL CHECK (priority IN ('high','normal','low')),
  kind TEXT NOT NULL CHECK (kind IN ('approval','question','alert','positive_reply','info','digest')),
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
  delivered_at TEXT, delivered_via TEXT, desktop_shown_at TEXT,
  suppressed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE grants_used (nonce TEXT PRIMARY KEY, command TEXT NOT NULL, used_at TEXT NOT NULL);
CREATE TABLE auth_attempts (id INTEGER PRIMARY KEY, at TEXT NOT NULL, ok INTEGER NOT NULL, command TEXT NOT NULL);
CREATE TABLE sources_state (
  source TEXT NOT NULL, tenant TEXT NOT NULL DEFAULT '',
  last_fetch_at TEXT, next_fetch_at TEXT, etag TEXT, last_modified TEXT,
  last_status INTEGER, consecutive_errors INTEGER NOT NULL DEFAULT 0, items_last INTEGER,
  PRIMARY KEY (source, tenant)
);
CREATE TABLE sheet_state (tab TEXT PRIMARY KEY, watermark TEXT, last_push_at TEXT, last_full_at TEXT,
  last_error TEXT, rows_pushed_total INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sheet_edits_applied (edit_id TEXT PRIMARY KEY, tab TEXT NOT NULL, row_id TEXT NOT NULL,
  col_key TEXT NOT NULL, value TEXT, result TEXT NOT NULL, applied_at TEXT NOT NULL);
CREATE TABLE sheet_deletes (id INTEGER PRIMARY KEY, tab TEXT NOT NULL, row_id TEXT NOT NULL,
  created_at TEXT NOT NULL, done_at TEXT, UNIQUE (tab, row_id));

-- ---------- status graphs for jobs and drafts (2.5) ----------
CREATE TRIGGER t_job_status_graph BEFORE UPDATE OF status ON jobs
WHEN OLD.status <> NEW.status AND NOT (
     (OLD.status = 'new' AND NEW.status IN ('prefilter_rejected','excluded','duplicate','eval_queued'))
  OR (OLD.status = 'eval_queued' AND NEW.status IN ('evaluating','excluded','closed'))
  OR (OLD.status = 'evaluating' AND NEW.status IN ('eligible','borderline','rejected','needs_human','eval_queued','excluded'))
  OR (OLD.status IN ('borderline','rejected','prefilter_rejected') AND NEW.status IN ('eligible','eval_queued','closed','excluded'))
  OR (OLD.status = 'eligible' AND NEW.status IN ('apply_queued','needs_human','closed','excluded','eval_queued'))
  OR (OLD.status = 'apply_queued' AND NEW.status IN ('awaiting_approval','applying','eligible','needs_human','closed','excluded'))
  OR (OLD.status = 'awaiting_approval' AND NEW.status IN ('apply_queued','eligible','closed','excluded'))
  OR (OLD.status = 'applying' AND NEW.status IN ('applied','apply_failed','needs_human'))
  OR (OLD.status = 'apply_failed' AND NEW.status IN ('eligible','closed','needs_human'))
  OR (OLD.status = 'needs_human' AND NEW.status IN ('eligible','closed','applied','excluded'))
  OR (OLD.status = 'excluded' AND NEW.status = 'eval_queued')
  OR (OLD.status = 'closed' AND NEW.status = 'eligible'))
BEGIN SELECT RAISE(ABORT, 'E_BAD_TRANSITION'); END;

CREATE TRIGGER t_draft_status_graph BEFORE UPDATE OF status ON drafts
WHEN OLD.status <> NEW.status AND NOT (
     (OLD.status = 'drafted' AND NEW.status IN ('lint_failed','review_pending','superseded'))
  OR (OLD.status IN ('lint_failed','review_failed') AND NEW.status IN ('drafted','dropped_qc','superseded'))
  OR (OLD.status = 'review_pending' AND NEW.status IN ('qc_passed','review_failed','superseded'))
  OR (OLD.status = 'qc_passed' AND NEW.status IN ('approved','awaiting_approval','superseded'))
  OR (OLD.status = 'awaiting_approval' AND NEW.status IN ('approved','skipped_by_human','drafted','expired','superseded'))
  OR (OLD.status = 'approved' AND NEW.status IN ('sent','expired','superseded')))
BEGIN SELECT RAISE(ABORT, 'E_BAD_TRANSITION'); END;

-- ---------- updated_at stamps (the Sheet sync relies on them, 7.4) ----------
-- Fires only when the statement left updated_at unchanged; recursive triggers are off, so the inner
-- UPDATE (which sets only updated_at) fires no status-graph trigger and no second stamp.
CREATE TRIGGER t_companies_touch AFTER UPDATE ON companies
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE companies SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_jobs_touch AFTER UPDATE ON jobs
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_evaluations_touch AFTER UPDATE ON evaluations
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE evaluations SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_contacts_touch AFTER UPDATE ON contacts
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE contacts SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_drafts_touch AFTER UPDATE ON drafts
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE drafts SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_qc_jobs_touch AFTER UPDATE ON qc_jobs
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE qc_jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_applications_touch AFTER UPDATE ON applications
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE applications SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_actions_touch AFTER UPDATE ON actions
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE actions SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_threads_touch AFTER UPDATE ON threads
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE threads SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_inbound_messages_touch AFTER UPDATE ON inbound_messages
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE inbound_messages SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_exclusions_touch AFTER UPDATE ON exclusions
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE exclusions SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER t_target_skips_touch AFTER UPDATE ON target_skips
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE target_skips SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE rowid = NEW.rowid; END;
