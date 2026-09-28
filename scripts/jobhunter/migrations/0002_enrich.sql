-- 0002_enrich.sql [U10] email finder (enrichment). Applied after 0001_init.sql.
-- Additive only: new tables, two nullable contact columns, triggers. Nothing created by 0001 is changed.
-- LIVE action statuses are spelled out as in 0001. docs/EMAIL-FINDER.md explains the feature.

CREATE TABLE enrich_requests (
  id INTEGER PRIMARY KEY,
  request_uid TEXT NOT NULL UNIQUE,                  -- 'E' + 7 base32
  contact_id INTEGER REFERENCES contacts(id),        -- NULL after forget
  company_id INTEGER REFERENCES companies(id),
  status TEXT NOT NULL CHECK (status IN ('running','found','not_found','unavailable','forgotten')),
  next_step INTEGER NOT NULL DEFAULT 0,              -- 0 free pre-steps, 1 finders, 2 verifier, 3 done
  finders_called INTEGER NOT NULL DEFAULT 0,
  result_call_id INTEGER REFERENCES enrich_calls(id),
  result_source TEXT CHECK (result_source IN ('pattern','provider')),
  grade TEXT CHECK (grade IN ('A','B','C','X')),
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 40),
  sendable INTEGER NOT NULL DEFAULT 0 CHECK (sendable IN (0,1)),
  verify_pending INTEGER NOT NULL DEFAULT 0 CHECK (verify_pending IN (0,1)),
  retry_of INTEGER REFERENCES enrich_requests(id),
  created_by TEXT NOT NULL,                          -- agent id | 'human' | 'system'
  cycle_id TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX i_enrich_req_contact ON enrich_requests(contact_id);
CREATE INDEX i_enrich_req_started ON enrich_requests(started_at);
CREATE UNIQUE INDEX u_enrich_running_contact ON enrich_requests(contact_id)
  WHERE status = 'running' AND contact_id IS NOT NULL;
CREATE UNIQUE INDEX u_enrich_one_retry ON enrich_requests(retry_of) WHERE retry_of IS NOT NULL;

CREATE TABLE enrich_request_keys (                   -- the cache; no clear-text personal data
  key_hash TEXT PRIMARY KEY,                         -- sha256(install_id | key)
  request_id INTEGER NOT NULL REFERENCES enrich_requests(id),
  kind TEXT NOT NULL CHECK (kind IN ('lookup','pname','li','li_member','email_norm')),
  created_at TEXT NOT NULL
);
CREATE INDEX i_enrich_keys_request ON enrich_request_keys(request_id);

CREATE TABLE enrich_calls (                          -- one row per provider HTTP call: budget and provenance
  id INTEGER PRIMARY KEY,
  request_id INTEGER REFERENCES enrich_requests(id), -- NULL only for op 'account'
  provider TEXT NOT NULL CHECK (provider IN ('prospeo','hunter','tomba','getprospect','anymailfinder',
                                             'findymail','apollo','zerobounce')),
  op TEXT NOT NULL CHECK (op IN ('find_name_domain','find_linkedin','verify','account')),
  outcome TEXT NOT NULL CHECK (outcome IN ('inflight','hit','miss','invalid','auth_failed','quota_exhausted',
        'rate_limited','server_error','timeout_after_send','network_before_send','bad_response','in_progress',
        'unexpected_phone','unknown')),
  started_at TEXT NOT NULL, finished_at TEXT,
  http_status INTEGER, retry_after_s INTEGER,
  credits_charged REAL NOT NULL CHECK (credits_charged >= 0),
  email TEXT CHECK (email IS NULL OR email = lower(email)),
  email_domain TEXT,
  verification TEXT CHECK (verification IN ('valid','accept_all','unknown','invalid','none')),
  confidence INTEGER CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
  grade_hint TEXT CHECK (grade_hint IN ('B','C','X','rejected')),
  reject_reason TEXT CHECK (reject_reason IS NULL OR length(reject_reason) <= 40),
  raw_status TEXT CHECK (raw_status IS NULL OR length(raw_status) <= 40),
  source_url TEXT CHECK (source_url IS NULL OR source_url LIKE 'https://%'),
  source_urls_json TEXT NOT NULL DEFAULT '[]',
  bounced_at TEXT, purged_at TEXT,
  cycle_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (op = 'account' OR request_id IS NOT NULL)
);
CREATE INDEX i_enrich_calls_budget ON enrich_calls(provider, started_at);
CREATE INDEX i_enrich_calls_email ON enrich_calls(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX u_enrich_find_once ON enrich_calls(request_id, provider)
  WHERE request_id IS NOT NULL AND op IN ('find_name_domain','find_linkedin');
CREATE UNIQUE INDEX u_enrich_verify_once ON enrich_calls(request_id, provider)
  WHERE request_id IS NOT NULL AND op = 'verify';

CREATE TABLE enrich_provider_state (
  provider TEXT PRIMARY KEY,
  exhausted_until TEXT, exhausted_reason TEXT,
  next_call_at TEXT,
  consecutive_errors INTEGER NOT NULL DEFAULT 0,
  backoff_level INTEGER NOT NULL DEFAULT 0 CHECK (backoff_level BETWEEN 0 AND 3),
  reported_remaining REAL, reported_at TEXT,
  key_set_at TEXT,
  updated_at TEXT NOT NULL
);

ALTER TABLE contacts ADD COLUMN email_source TEXT
  CHECK (email_source IS NULL OR email_source IN ('published','pattern','provider','human'));
ALTER TABLE contacts ADD COLUMN email_enrich_call_id INTEGER REFERENCES enrich_calls(id);

-- budget: last line of defence; a missing meta row means budget 0 (fail closed)
CREATE TRIGGER t_enrich_budget BEFORE INSERT ON enrich_calls
WHEN NEW.op <> 'account' AND (
     (SELECT COALESCE(SUM(c.credits_charged), 0) FROM enrich_calls c
       WHERE c.provider = NEW.provider AND c.op <> 'account'
         AND c.started_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.started_at, '-31 days')) + NEW.credits_charged
       > CAST(COALESCE((SELECT value FROM meta WHERE key = 'enrich_budget_31d:' || NEW.provider), '0') AS REAL)
  OR (SELECT COALESCE(SUM(c.credits_charged), 0) FROM enrich_calls c
       WHERE c.provider = NEW.provider AND c.op <> 'account'
         AND c.started_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.started_at, '-1 days')) + NEW.credits_charged
       > CAST(COALESCE((SELECT value FROM meta WHERE key = 'enrich_day_credits:' || NEW.provider), '0') AS REAL)
  OR (SELECT count(*) FROM enrich_calls c
       WHERE c.provider = NEW.provider
         AND c.started_at > strftime('%Y-%m-%dT%H:%M:%SZ', NEW.started_at, '-1 days')) + 1
       > CAST(COALESCE((SELECT value FROM meta WHERE key = 'enrich_day_requests:' || NEW.provider), '0') AS INTEGER)
  OR (SELECT COALESCE(SUM(c.credits_charged), 0) FROM enrich_calls c
       WHERE c.provider = NEW.provider AND c.op <> 'account') + NEW.credits_charged
       > CAST(COALESCE((SELECT value FROM meta WHERE key = 'enrich_budget_lifetime:' || NEW.provider), '0') AS REAL))
BEGIN SELECT RAISE(ABORT, 'E_CEILING'); END;

-- a new call must reserve at least the documented maximum charge of its op
CREATE TRIGGER t_enrich_reserve_max BEFORE INSERT ON enrich_calls
WHEN NEW.op <> 'account' AND (NEW.outcome <> 'inflight' OR NEW.credits_charged <
       CASE WHEN NEW.provider = 'hunter' AND NEW.op = 'verify' THEN 0.5 ELSE 1 END)
BEGIN SELECT RAISE(ABORT, 'E_INTERNAL'); END;

-- privacy cap: finders that received this person's data, per request
CREATE TRIGGER t_enrich_max_finders BEFORE INSERT ON enrich_calls
WHEN NEW.op IN ('find_name_domain','find_linkedin')
 AND (SELECT count(*) FROM enrich_calls c WHERE c.request_id = NEW.request_id
        AND c.op IN ('find_name_domain','find_linkedin') AND c.outcome <> 'network_before_send')
     >= CAST(COALESCE((SELECT value FROM meta WHERE key = 'enrich_max_finders_per_person'), '0') AS INTEGER)
BEGIN SELECT RAISE(ABORT, 'E_CEILING'); END;

-- call outcome graph: inflight -> terminal; unknown only from inflight (housekeeping); no way back
CREATE TRIGGER t_enrich_call_outcome_graph BEFORE UPDATE OF outcome ON enrich_calls
WHEN OLD.outcome <> NEW.outcome AND NOT (OLD.outcome = 'inflight' OR (OLD.outcome = 'in_progress'
       AND NEW.outcome IN ('hit','invalid','unknown','miss')))
BEGIN SELECT RAISE(ABORT, 'E_BAD_TRANSITION'); END;

-- one retry per person, never a retry of a retry
CREATE TRIGGER t_enrich_no_second_retry BEFORE INSERT ON enrich_requests
WHEN NEW.retry_of IS NOT NULL AND (SELECT retry_of FROM enrich_requests WHERE id = NEW.retry_of) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'E_ALREADY_DONE'); END;

-- a provider address on a contact must match its call, be B or C, and not have bounced or been purged.
-- The update trigger fires only when one of the four values really changes, so a no-op
-- "SET email = COALESCE(email, ?)" by contact add does not trip on a bounced provider address.
CREATE TRIGGER t_contact_provider_email_ins BEFORE INSERT ON contacts
WHEN NEW.email_source = 'provider' AND (NEW.email_enrich_call_id IS NULL
  OR COALESCE(NEW.email_grade, '') NOT IN ('B','C')
  OR NOT EXISTS (SELECT 1 FROM enrich_calls c WHERE c.id = NEW.email_enrich_call_id
                 AND c.email = lower(NEW.email) AND c.bounced_at IS NULL AND c.purged_at IS NULL))
BEGIN SELECT RAISE(ABORT, 'E_ADDRESS_GRADE'); END;
CREATE TRIGGER t_contact_provider_email_upd BEFORE UPDATE OF email, email_grade, email_source, email_enrich_call_id
  ON contacts
WHEN NEW.email_source = 'provider'
 AND (OLD.email IS NOT NEW.email OR OLD.email_grade IS NOT NEW.email_grade
      OR OLD.email_source IS NOT NEW.email_source OR OLD.email_enrich_call_id IS NOT NEW.email_enrich_call_id)
 AND (NEW.email_enrich_call_id IS NULL OR COALESCE(NEW.email_grade, '') NOT IN ('B','C')
  OR NOT EXISTS (SELECT 1 FROM enrich_calls c WHERE c.id = NEW.email_enrich_call_id
                 AND c.email = lower(NEW.email) AND c.bounced_at IS NULL AND c.purged_at IS NULL))
BEGIN SELECT RAISE(ABORT, 'E_ADDRESS_GRADE'); END;

-- send-time last line for provider addresses (rules 11.3.1 and 11.3.3)
CREATE TRIGGER t_enrich_action_address BEFORE INSERT ON actions
WHEN NEW.kind IN ('cold_email','application_email') AND NEW.contact_id IS NOT NULL
 AND (SELECT email_source FROM contacts WHERE id = NEW.contact_id) = 'provider'
 AND (NEW.recipient IS NOT (SELECT lower(email) FROM contacts WHERE id = NEW.contact_id)
   OR EXISTS (SELECT 1 FROM enrich_calls c WHERE c.id = (SELECT email_enrich_call_id FROM contacts
                WHERE id = NEW.contact_id) AND (c.bounced_at IS NOT NULL OR c.purged_at IS NOT NULL))
   OR EXISTS (SELECT 1 FROM breakers b WHERE b.state = 'open' AND b.reason_code = 'bounce_strikes'
                AND (b.scope = 'enrich' OR b.scope = 'enrich:' || (SELECT c.provider FROM enrich_calls c
                     WHERE c.id = (SELECT email_enrich_call_id FROM contacts WHERE id = NEW.contact_id)))))
BEGIN SELECT RAISE(ABORT, 'E_ADDRESS_GRADE'); END;

CREATE TRIGGER t_enrich_requests_touch AFTER UPDATE ON enrich_requests
WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE enrich_requests SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = NEW.id; END;
CREATE TRIGGER t_enrich_calls_touch AFTER UPDATE ON enrich_calls
WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE enrich_calls SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = NEW.id; END;
