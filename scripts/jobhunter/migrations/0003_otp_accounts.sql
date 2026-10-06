-- 0003_otp_accounts.sql [U1] email codes and sign-in links, ATS accounts, CAPTCHA hand-off.
-- Applied after 0002_enrich.sql. Never stores a code, a link, a password or a message body: hashes only.

-- 1. human task kind 'captcha' (rebuild: CHECK cannot be altered)
CREATE TABLE human_tasks_v3 (
  id INTEGER PRIMARY KEY, task_uid TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('apply_manually','answer_question','review_reply','resolve_unknown',
        'confirm_not_sent','reset_breaker','confirm_profile','relax_gate','relogin','confirm_company_merge',
        'confirm_agency','review_audit_mismatch','connect_mail','suggest_auto','captcha')),
  job_id INTEGER, draft_id INTEGER, thread_id INTEGER, action_id INTEGER, company_id INTEGER,
  question TEXT, detail TEXT,
  created_at TEXT NOT NULL, done_at TEXT, resolution TEXT
);
INSERT INTO human_tasks_v3 SELECT * FROM human_tasks;
DROP TABLE human_tasks;
ALTER TABLE human_tasks_v3 RENAME TO human_tasks;

-- 2. a token whose page showed a CAPTCHA after arm but before any commit may be released
DROP TRIGGER t_action_status_graph;
CREATE TRIGGER t_action_status_graph BEFORE UPDATE OF status ON actions
WHEN OLD.status <> NEW.status AND NOT (
     (OLD.status = 'reserved' AND NEW.status IN ('armed','unknown'))
  OR (OLD.status = 'reserved' AND NEW.status = 'failed' AND COALESCE(NEW.fail_reason, '') IN ('observed_text_mismatch',
        'not_attempted','precondition_changed','form_blocked_before_submit','smtp_rejected_before_data'))
  OR (OLD.status = 'armed' AND NEW.status IN ('sent','unknown','failed_after_click'))
  OR (OLD.status = 'armed' AND NEW.status = 'failed' AND COALESCE(NEW.fail_reason, '') IN ('smtp_rejected',
        'form_blocked_before_submit'))
  OR (OLD.status = 'unknown' AND NEW.status IN ('sent','failed_after_click'))
  OR (OLD.status = 'unknown' AND NEW.status = 'failed'
        AND COALESCE(NEW.fail_reason, '') IN ('not_found_twice','human_confirmed_not_sent')))
BEGIN SELECT RAISE(ABORT, 'E_BAD_TRANSITION'); END;

-- 3. notifications may carry one image (the CAPTCHA screenshot), sent with openclaw message send --media
ALTER TABLE notifications ADD COLUMN media_path TEXT;

-- 4. code requests: the agent's own trigger of a code or link, before any mailbox read
CREATE TABLE code_requests (
  id INTEGER PRIMARY KEY,
  request_uid TEXT NOT NULL UNIQUE,                -- 'O' + 7 base32 (acl class otp_req)
  platform TEXT NOT NULL,                          -- workday | icims | ... (canonical ATS key)
  site TEXT NOT NULL,                              -- consent site: platform key or 'host:<host>'
  host TEXT NOT NULL,                              -- tab host when requested (tenant host)
  tenant TEXT,                                     -- accounts.tenant_key(platform, url)
  purpose TEXT NOT NULL CHECK (purpose IN ('account_verify','signin','application_submit')),
  want TEXT NOT NULL CHECK (want IN ('code','link','either')),
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  action_id INTEGER REFERENCES actions(id),        -- the application token, when one is open
  account_id INTEGER REFERENCES ats_accounts(id),
  tab_id TEXT NOT NULL,                            -- CDP target id of the agent's tab
  route TEXT NOT NULL CHECK (route IN ('web_ui','app_password')),
  agent_id TEXT NOT NULL, cycle_id TEXT,
  requested_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', requested_at) IS requested_at),
  window_ends_at TEXT NOT NULL,                    -- requested_at + otp.window_minutes (max 10)
  status TEXT NOT NULL CHECK (status IN ('waiting','found','used','rejected','expired','cancelled','refused')),
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 40),
  fill_attempts INTEGER NOT NULL DEFAULT 0 CHECK (fill_attempts <= 2),
  resolved_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX i_code_req_site ON code_requests(site, requested_at);
-- one waiting request per tab: a second trigger on the same tab must cancel the first
CREATE UNIQUE INDEX u_code_req_waiting_tab ON code_requests(tab_id) WHERE status IN ('waiting','found');

-- 5. code uses: one row per code or link handed to a page; hashes only (HMAC-SHA256 with meta otp_salt)
CREATE TABLE code_uses (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES code_requests(id),
  kind TEXT NOT NULL CHECK (kind IN ('code','link')),
  value_hmac TEXT NOT NULL,                        -- hmac(otp_salt, code or link URL)
  message_hmac TEXT NOT NULL,                      -- hmac(otp_salt, Message-ID or Gmail message id)
  sender_domain TEXT NOT NULL,                     -- e.g. myworkday.com (no local part)
  received_at TEXT NOT NULL,                       -- the message's own time (INTERNALDATE or Gmail date)
  link_host TEXT,                                  -- host of the link (links only)
  outcome TEXT NOT NULL CHECK (outcome IN ('filled','accepted','rejected','unknown')),
  used_at TEXT NOT NULL, site TEXT NOT NULL, job_id INTEGER, cycle_id TEXT
);
CREATE UNIQUE INDEX u_code_use_message ON code_uses(message_hmac);     -- a message is used once, ever
CREATE UNIQUE INDEX u_code_use_value ON code_uses(value_hmac);         -- a code or link is used once, ever
CREATE UNIQUE INDEX u_code_use_request ON code_uses(request_id);       -- a request consumes one message
CREATE INDEX i_code_use_site_day ON code_uses(site, used_at);

-- 6. ATS accounts ledger (no password; the Keychain holds it)
CREATE TABLE ats_accounts (
  id INTEGER PRIMARY KEY,
  account_uid TEXT NOT NULL UNIQUE,                -- 'N' + 7 base32
  platform TEXT NOT NULL, site TEXT NOT NULL,
  host TEXT NOT NULL,                              -- e.g. kestrel.wd5.myworkdayjobs.com
  tenant TEXT NOT NULL,                            -- accounts.tenant_key (one account per tenant)
  email TEXT NOT NULL,                             -- always owner.gmail_address at creation
  store TEXT NOT NULL CHECK (store IN ('keychain','file')),
  secret_ref TEXT NOT NULL,                        -- '<service>|<account>' names only, never the value
  status TEXT NOT NULL CHECK (status IN ('creating','pending_verify','active','failed','locked','forgotten')),
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 40),
  created_at TEXT NOT NULL, verified_at TEXT, last_used_at TEXT, forgotten_at TEXT,
  created_job_id INTEGER REFERENCES jobs(id), created_cycle_id TEXT,
  failures INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX u_account_tenant ON ats_accounts(platform, tenant)
  WHERE status IN ('creating','pending_verify','active','locked');

-- 7. account terms log: every checkbox the create step saw and what it did
CREATE TABLE account_terms (
  id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES ats_accounts(id), site TEXT NOT NULL, host TEXT NOT NULL,
  label TEXT NOT NULL CHECK (length(label) <= 300), required INTEGER NOT NULL CHECK (required IN (0,1)),
  class TEXT NOT NULL CHECK (class IN ('standard_terms','unusual','other')),
  action TEXT NOT NULL CHECK (action IN ('ticked','left_unticked','refused')),
  at TEXT NOT NULL
);

-- 8. code steps: every browser write code made (counts for caps, Alerts rows and audit)
CREATE TABLE code_steps (
  id INTEGER PRIMARY KEY,
  step TEXT NOT NULL CHECK (step IN ('account_create','account_signin','code_fill','link_open','captcha_check',
        'captcha_screenshot','tab_close')),
  platform TEXT NOT NULL, site TEXT NOT NULL, host TEXT, job_id INTEGER, action_id INTEGER,
  request_id INTEGER, account_id INTEGER, captcha_id INTEGER,
  outcome TEXT NOT NULL CHECK (outcome IN ('ok','rejected','captcha','stop','unknown','refused','error')),
  detail TEXT CHECK (detail IS NULL OR length(detail) <= 200),     -- reason codes only, never page text
  agent_id TEXT, cycle_id TEXT, at TEXT NOT NULL
);
CREATE INDEX i_code_steps_site_day ON code_steps(site, step, at);

-- 9. CAPTCHA tasks
CREATE TABLE captcha_tasks (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL,                              -- 4 chars, alphabet of approvals.CODE_ALPHABET
  task_uid TEXT NOT NULL UNIQUE REFERENCES human_tasks(task_uid),
  platform TEXT NOT NULL, site TEXT NOT NULL, host TEXT NOT NULL,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  action_id INTEGER REFERENCES actions(id),
  token_outcome TEXT NOT NULL CHECK (token_outcome IN ('none','released','unknown')),
  tab_id TEXT,                                     -- CDP target id (null: tab unknown, timeout still applies)
  url_hmac TEXT,                                   -- hmac of the page URL (the URL itself is not stored)
  screenshot_path TEXT,                            -- state/captcha/<id>.png, deleted after retention
  status TEXT NOT NULL CHECK (status IN ('open','resolved','timed_out','cancelled')),
  opened_by TEXT NOT NULL CHECK (opened_by IN ('guard','agent','code')),
  opened_at TEXT NOT NULL, deadline_at TEXT NOT NULL,
  resolved_at TEXT, resolved_by TEXT, check_detail TEXT CHECK (check_detail IS NULL OR length(check_detail) <= 200),
  cycle_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX u_captcha_open_code ON captcha_tasks(code) WHERE status = 'open';
CREATE UNIQUE INDEX u_captcha_open_job ON captcha_tasks(job_id) WHERE status = 'open';
CREATE INDEX i_captcha_site_day ON captcha_tasks(site, opened_at);

-- 10. defaults (db.init_db writes missing meta rows; these are for existing installs)
INSERT OR IGNORE INTO meta (key, value, updated_at, updated_by)
  VALUES ('otp_salt', lower(hex(randomblob(32))), strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'migration');
