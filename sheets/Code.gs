/**
 * openclaw-job-hunter: Google Sheets mirror (container-bound Apps Script web app).
 *
 * The agent's local database on your computer is the source of truth. This script receives batched
 * updates from it and keeps this sheet readable: frozen headers, human column names, real dates, colors,
 * links, filters and a Dashboard. Setup guide: docs/GOOGLE-SHEETS.md in the repository.
 * Menu: Job Hunter > Set up or repair this sheet.
 *
 * Safety: every request must carry the connection secret in its body. A request with a wrong secret
 * changes nothing. The script can only touch this one spreadsheet (scope spreadsheets.currentonly).
 *
 * Keep the TABS list identical to scripts/jobhunter/sheets_labels.py (a test compares them).
 */

var APP = 'openclaw-job-hunter';
var SCHEMA_VERSION = 2;
var MAX_BODY_BYTES = 5 * 1024 * 1024;
var PROP_SECRET = 'JH_SECRET';
var PROP_META = 'JH_META';
var MD_TAB = 'jh_tab';
var MD_COL = 'jh_col';
var EDITS_TITLE = '_edits';
var PREALLOC_ROWS = 1000;
var AGENT_PROTECTION = 'Filled by the agent';

var PALETTE = {
  header: ['#1f3a5f', '#ffffff'], good: ['#d9ead3', '#274e13'], wait: ['#fff2cc', '#7f6000'],
  bad: ['#f4cccc', '#990000'], muted: ['#efefef', '#666666'], info: ['#cfe2f3', '#073763'],
  editable: '#fffbe6', idText: '#999999', section: '#e8eef7'
};

var STATUS_GROUPS = {
  good:  ['Applied', 'Sent', 'Accepted', 'Passed', 'Approved', 'Good fit', 'Resolved', 'Done', 'Connected'],
  wait:  ['Queued', 'Awaiting approval', 'Awaiting reply', 'Follow-up due', 'Submitting', 'Pending', 'Rewritten',
          'Borderline', 'Needs you', 'Invite pending', 'Warning'],
  bad:   ['Failed', 'Blocked', 'Stopped', 'Bounced', 'Dropped', 'Failed QC', 'Error', 'Opted out',
          'Unknown (checking)', 'Complaint', 'Open'],
  muted: ['Skipped', 'Duplicate', 'Closed', 'Expired', 'Not a fit', 'Withdrawn', 'No reply', 'Auto-reply',
          'Not run', 'Not interested', 'Not hiring', 'Out of office'],
  info:  ['Replied', 'Positive reply', 'Screening call', 'Interview', 'Offer', 'Referred', 'Referral offered',
          'Info']
};

var FORMATS = { date: 'd mmm yyyy', datetime: 'd mmm yyyy HH:mm', int: '0', score: '0', num2: '0.00',
                text: '@', long: '@', link: '@', status: '@', choice: '@', id: '@' };

function col(key, header, type, width, opts) {
  opts = opts || {};
  return { key: key, header: header, type: type, width: width, editable: !!opts.editable,
           choices: opts.choices || null, note: opts.note || '' };
}

var OUTCOMES = ['No response yet', 'Rejected', 'Screening call', 'Interview', 'Offer', 'Withdrawn', 'Role closed'];
// Follow-ups: a conversation cannot be withdrawn, but a person can refer you (threads.outcome).
var THREAD_OUTCOMES = ['No response yet', 'Rejected', 'Screening call', 'Interview', 'Offer', 'Referred',
                       'Role closed'];

var TABS = [
  { key: 'start', title: 'Start here', kind: 'render' },
  { key: 'dashboard', title: 'Dashboard', kind: 'render' },
  { key: 'approvals', title: 'Approvals', kind: 'table', freezeCols: 2, cols: [
    col('created', 'Created', 'datetime', 130), col('code', 'Code', 'text', 60),
    col('what', 'What', 'text', 150), col('to', 'To', 'text', 180), col('company', 'Company', 'text', 160),
    col('subject', 'Subject or first line', 'text', 240), col('message', 'Full text', 'long', 420),
    col('qc', 'QC score', 'num2', 80), col('expires', 'Expires', 'datetime', 130),
    col('status', 'Status', 'status', 150),
    col('decision', 'Your decision', 'choice', 130, { editable: true, choices: ['Approve', 'Skip'],
        note: 'Pick Approve or Skip. The agent picks this up within 20 minutes.' }),
    col('id', 'ID', 'id', 90) ] },
  { key: 'jobs', title: 'Jobs', kind: 'table', freezeCols: 2, cols: [
    col('found_on', 'Found on', 'datetime', 130), col('company', 'Company', 'text', 170),
    col('role', 'Role', 'text', 240), col('location', 'Location', 'text', 150),
    col('work_mode', 'Work mode', 'text', 90), col('source', 'Found via', 'text', 170),
    col('posting', 'Job posting', 'link', 110), col('fit', 'Fit score', 'score', 80),
    col('verdict', 'Verdict', 'status', 110), col('why', 'Why', 'long', 320),
    col('gates', 'Deal breakers', 'long', 200), col('status', 'Status', 'status', 150),
    col('applied_on', 'Applied on', 'date', 110),
    col('your_call', 'Your call', 'choice', 130, { editable: true, choices: ['Apply anyway', 'Never apply'],
        note: 'Apply anyway still goes through every safety check. Never apply closes the job.' }),
    col('id', 'ID', 'id', 90) ] },
  { key: 'skipped', title: 'Skipped by filters', kind: 'table', freezeCols: 2, cols: [
    col('found_on', 'Found on', 'datetime', 130), col('company', 'Company', 'text', 170),
    col('role', 'Role', 'text', 240), col('location', 'Location', 'text', 150),
    col('reason', 'Why it was skipped', 'long', 320), col('posting', 'Job posting', 'link', 110),
    col('your_call', 'Your call', 'choice', 130, { editable: true, choices: ['Apply anyway'],
        note: 'Apply anyway sends the job to the evaluator; every safety check still applies.' }),
    col('id', 'ID', 'id', 90) ] },
  { key: 'applications', title: 'Applications', kind: 'table', freezeCols: 2, cols: [
    col('applied_on', 'Applied on', 'datetime', 130), col('company', 'Company', 'text', 170),
    col('role', 'Role', 'text', 240), col('location', 'Location', 'text', 140), col('how', 'How', 'text', 170),
    col('posting', 'Job posting', 'link', 110), col('resume', 'Resume sent', 'text', 200),
    col('status', 'Status', 'status', 150), col('proof', 'Proof', 'long', 240), col('fit', 'Fit score', 'score', 80),
    col('follow_up', 'Follow-up', 'text', 140),
    col('outcome', 'Outcome', 'choice', 140, { editable: true, choices: OUTCOMES,
        note: 'Tell the agent what happened. It stops follow-ups once there is an outcome.' }),
    col('notes', 'Your notes', 'long', 240, { editable: true, note: 'Anything you want to remember.' }),
    col('id', 'ID', 'id', 90) ] },
  { key: 'outreach', title: 'Outreach', kind: 'table', freezeCols: 2, cols: [
    col('sent_on', 'Sent on', 'datetime', 130), col('channel', 'Channel', 'text', 130),
    col('person', 'Person', 'text', 140), col('their_role', 'Their role', 'text', 170),
    col('company', 'Company', 'text', 160), col('profile', 'Profile', 'link', 90),
    col('why_them', 'Why this person', 'long', 260), col('subject', 'Subject or first line', 'text', 220),
    col('message', 'Message', 'long', 360), col('qc', 'QC score', 'num2', 80),
    col('approved_by', 'Approved by', 'text', 120), col('status', 'Status', 'status', 140),
    col('reply', 'Reply', 'status', 130), col('follow_up_due', 'Follow-up due', 'date', 110),
    col('notes', 'Your notes', 'long', 220, { editable: true, note: 'Anything you want to remember.' }),
    col('id', 'ID', 'id', 110) ] },
  { key: 'followups', title: 'Follow-ups', kind: 'table', freezeCols: 2, cols: [
    col('started_on', 'Started on', 'datetime', 130), col('channel', 'Channel', 'text', 110),
    col('person', 'Person', 'text', 140), col('company', 'Company', 'text', 160),
    col('follow_up_due', 'Follow-up due', 'date', 110), col('follow_up_sent', 'Follow-up sent', 'datetime', 130),
    col('last_reply', 'Last reply', 'datetime', 130), col('reply_type', 'Reply type', 'status', 130),
    col('what_they_said', 'What they said', 'long', 300),
    col('next_step', 'Next step', 'long', 240), col('thread', 'Thread', 'link', 90),
    col('outcome', 'Outcome', 'choice', 140, { editable: true, choices: THREAD_OUTCOMES,
        note: 'Tell the agent what happened in this conversation.' }),
    col('id', 'ID', 'id', 130) ] },
  { key: 'qc', title: 'QC log', kind: 'table', freezeCols: 2, cols: [
    col('time', 'Time', 'datetime', 130), col('item', 'Item', 'text', 90), col('channel', 'Channel', 'text', 150),
    col('recipient', 'Recipient', 'text', 170), col('company', 'Company', 'text', 150),
    col('attempt', 'Attempt', 'text', 90), col('lint', 'Lint', 'status', 90),
    col('lint_findings', 'Lint findings', 'long', 280), col('reviewer', 'Reviewer', 'status', 90),
    col('score', 'Score', 'num2', 70), col('lowest', 'Lowest criterion', 'text', 150),
    col('gates_failed', 'Gates failed', 'text', 150), col('top_issue', 'Top issue', 'long', 280),
    col('final', 'Final action', 'status', 140), col('hook', 'Hook source', 'link', 100),
    col('hash', 'Text hash', 'text', 110), col('id', 'ID', 'id', 110) ] },
  { key: 'daily', title: 'Daily summary', kind: 'table', freezeCols: 1, cols: [
    col('date', 'Date', 'date', 110), col('jobs_found', 'Jobs found', 'int', 90),
    col('passed_filters', 'Passed filters', 'int', 100), col('evaluated', 'Evaluated', 'int', 90),
    col('good_fits', 'Good fits', 'int', 90), col('applications', 'Applications', 'int', 100),
    col('emails', 'Emails', 'int', 80), col('li_invites', 'LinkedIn invites', 'int', 110),
    col('li_messages', 'LinkedIn messages', 'int', 120), col('follow_ups', 'Follow-ups', 'int', 90),
    col('replies', 'Replies', 'int', 80), col('positive', 'Positive replies', 'int', 110),
    col('interviews', 'Interviews', 'int', 90), col('drafts', 'Drafts', 'int', 80),
    col('first_pass', 'Passed QC first try', 'int', 130), col('dropped', 'Dropped by QC', 'int', 110),
    col('stops', 'Stops', 'int', 70), col('cycles', 'Cycles run', 'int', 90), col('id', 'ID', 'id', 110) ] },
  { key: 'alerts', title: 'Alerts', kind: 'table', freezeCols: 1, cols: [
    col('time', 'Time', 'datetime', 130), col('area', 'Area', 'text', 140), col('what', 'What happened', 'long', 320),
    col('severity', 'Severity', 'status', 100), col('todo', 'What you need to do', 'long', 320),
    col('until', 'Paused until', 'datetime', 130), col('resolved', 'Resolved on', 'datetime', 130),
    col('status', 'Status', 'status', 100), col('id', 'ID', 'id', 90) ] },
  { key: 'settings', title: 'Limits and settings', kind: 'render' }
];

/* ------------------------------ entry points ------------------------------ */

function doGet() {
  return json_({ ok: true, app: APP, schema_version: SCHEMA_VERSION });
}

function doPost(e) {
  var out;
  try {
    if (!e || !e.postData || !e.postData.contents) return json_({ ok: false, error: 'empty_body' });
    if (e.postData.length > MAX_BODY_BYTES) return json_({ ok: false, error: 'too_large' });
    var req;
    try { req = JSON.parse(e.postData.contents); } catch (err) { return json_({ ok: false, error: 'bad_json' }); }
    if (!req || typeof req !== 'object') return json_({ ok: false, error: 'bad_json' });
    if (!checkSecret_(req.secret)) { Utilities.sleep(1000); return json_({ ok: false, error: 'bad_secret' }); }
    if (req.schema_version !== SCHEMA_VERSION) {
      return json_({ ok: false, error: 'schema_mismatch', expected: SCHEMA_VERSION });
    }
    var lock = LockService.getDocumentLock();
    if (!lock.tryLock(30000)) return json_({ ok: false, error: 'busy' });
    try { out = handle_(SpreadsheetApp.getActiveSpreadsheet(), req); } finally { lock.releaseLock(); }
  } catch (err) {
    out = { ok: false, error: 'exception', detail: String(err && err.stack || err).substring(0, 800) };
  }
  return json_(out);
}

function handle_(ss, req) {
  switch (req.action) {
    case 'ping':
      return { ok: true, app: APP, schema_version: SCHEMA_VERSION, tz: ss.getSpreadsheetTimeZone(),
               tabs: TABS.map(function (t) { return t.key; }) };
    case 'setup':
      ensureAll_(ss, true);
      return { ok: true };
    case 'format':
      TABS.forEach(function (t) { if (t.kind === 'table') formatTable_(ensureTab_(ss, t, false), t); });
      return { ok: true };
    case 'configure':
      if (req.timezone) {
        var tz = String(req.timezone);
        if (!/^[A-Za-z_]+(\/[A-Za-z0-9_+\-]+){0,2}$/.test(tz)) return { ok: false, error: 'bad_timezone' };
        ss.setSpreadsheetTimeZone(tz);
      }
      return { ok: true, tz: ss.getSpreadsheetTimeZone() };
    case 'sync':
      return sync_(ss, req);
    default:
      return { ok: false, error: 'unknown_action' };
  }
}

/* ---------------------------------- sync ---------------------------------- */

function sync_(ss, req) {
  ensureAll_(ss, false);
  ackEdits_(ss, asArray_(req.ack_edits));
  var deleted = deleteRows_(ss, asArray_(req.deletes));
  var byTab = dict_(), errors = [], created = 0, updated = 0;
  asArray_(req.ops).forEach(function (op) {
    var def = tabDef_(op && op.tab);
    if (!def || def.kind !== 'table') { errors.push({ tab: op && op.tab, id: op && op.id, error: 'unknown_tab' }); return; }
    (byTab[def.key] = byTab[def.key] || []).push(op);
  });
  Object.keys(byTab).forEach(function (key) {
    var res = upsert_(ss, tabDef_(key), byTab[key]);
    created += res.created; updated += res.updated; errors = errors.concat(res.errors);
  });
  var r = (req.render && typeof req.render === 'object') ? req.render : {};
  var meta = saveMeta_(r.start || {});
  if (r.dashboard) renderDashboard_(ss, r.dashboard);
  if (r.settings) renderSettings_(ss, asArray_(r.settings));
  renderStart_(ss, meta);
  return { ok: true, batch_id: req.batch_id || null, created: created, updated: updated, deleted: deleted,
           errors: errors, edits: pendingEdits_(ss) };
}

function asArray_(v) { return Array.isArray(v) ? v : []; }

// Lookup tables keyed by ids from requests have no prototype, so an id such as 'constructor' is just a key.
function dict_() { return Object.create(null); }

function deleteRows_(ss, dels) {
  // dels: [{tab, id}]. Rows are deleted bottom-up so earlier row numbers stay valid.
  var n = 0, byTab = dict_();
  dels.forEach(function (d) { if (d && d.tab && d.id) (byTab[d.tab] = byTab[d.tab] || dict_())[String(d.id)] = true; });
  Object.keys(byTab).forEach(function (key) {
    var def = tabDef_(key);
    if (!def || def.kind !== 'table') return;
    var sh = ensureTab_(ss, def, false), cols = columnMap_(sh, def), last = sh.getLastRow();
    if (last < 2 || !cols.id) return;
    var ids = sh.getRange(2, cols.id, last - 1, 1).getValues();
    for (var i = ids.length - 1; i >= 0; i--) {
      if (byTab[key][String(ids[i][0])]) { sh.deleteRow(i + 2); n++; }
    }
  });
  return n;
}

function upsert_(ss, def, ops) {
  var sh = ensureTab_(ss, def, false);
  var cols = columnMap_(sh, def);
  // Append below every row that has any content, so rows the person typed by hand are never overwritten.
  var last = Math.max(sh.getLastRow(), 1);
  var index = dict_();
  if (last >= 2) {
    var ids = sh.getRange(2, cols.id, last - 1, 1).getValues();
    for (var i = 0; i < ids.length; i++) { if (ids[i][0] !== '') index[String(ids[i][0])] = i + 2; }
  }
  var creates = [], createAt = dict_(), updated = 0, errors = [];
  ops.forEach(function (op) {
    if (typeof op.id !== 'string' || !op.id || op.id.length > 64 || typeof op.row !== 'object' || op.row === null) {
      errors.push({ tab: def.key, id: (op && op.id) || null, error: 'bad_op' });
      return;
    }
    if (index[op.id]) { updateRow_(sh, def, cols, index[op.id], op.row); updated++; }
    else if (createAt[op.id] !== undefined) { creates[createAt[op.id]] = op; }   // same id twice: keep the newest
    else { createAt[op.id] = creates.length; creates.push(op); }
  });
  if (creates.length) appendRows_(sh, def, cols, last + 1, creates);
  return { created: creates.length, updated: updated, errors: errors };
}

function appendRows_(sh, def, cols, start, ops) {
  ensureCapacity_(sh, start + ops.length);
  var width = sh.getLastColumn();
  var block = ops.map(function (op) {
    var row = new Array(width).fill('');
    def.cols.forEach(function (cd) {
      var c = cols[cd.key];
      if (!c) return;
      row[c - 1] = cd.key === 'id' ? op.id : cell_(cd, op.row[cd.key]);
    });
    return row;
  });
  sh.getRange(start, 1, ops.length, width).setValues(block);
  def.cols.forEach(function (cd) {
    if (cd.type !== 'link' || !cols[cd.key]) return;
    var rich = ops.map(function (op) { return [richLink_(op.row[cd.key])]; });
    sh.getRange(start, cols[cd.key], ops.length, 1).setRichTextValues(rich);
  });
}

function updateRow_(sh, def, cols, r, row) {
  // Agent columns only. Editable columns belong to the person once the row exists.
  var cells = [];
  def.cols.forEach(function (cd) {
    if (cd.editable || cd.key === 'id' || !cols[cd.key] || !(cd.key in row)) return;
    cells.push({ c: cols[cd.key], cd: cd, v: row[cd.key] });
  });
  cells.sort(function (a, b) { return a.c - b.c; });
  var run = [];
  function flush() {
    if (!run.length) return;
    sh.getRange(r, run[0].c, 1, run.length).setValues([run.map(function (x) { return cell_(x.cd, x.v); })]);
    run.forEach(function (x) { if (x.cd.type === 'link') sh.getRange(r, x.c).setRichTextValue(richLink_(x.v)); });
    run = [];
  }
  cells.forEach(function (x) {
    if (run.length && x.c !== run[run.length - 1].c + 1) flush();
    run.push(x);
  });
  flush();
}

function cell_(cd, v) {
  if (v === null || v === undefined) return '';
  switch (cd.type) {
    case 'date':
      // A local calendar date 'YYYY-MM-DD' becomes a date serial number, so no time zone can shift it.
      var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v));
      if (m) return (Date.UTC(+m[1], +m[2] - 1, +m[3]) - Date.UTC(1899, 11, 30)) / 86400000;
      var dd = new Date(v);
      return isNaN(dd.getTime()) ? safeText_(String(v)) : dd;
    case 'datetime':
      var d = new Date(v);
      return isNaN(d.getTime()) ? safeText_(String(v)) : d;
    case 'int': case 'score': case 'num2':
      var n = Number(v);
      return (v !== '' && isFinite(n)) ? n : '';
    case 'link':
      return safeText_(String((v && v.text) || ''));
    default:
      return safeText_(String(v));
  }
}

function safeText_(s) {
  s = String(s).substring(0, 5000);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function richLink_(v) {
  var b = SpreadsheetApp.newRichTextValue();
  var text = v && v.text ? String(v.text).substring(0, 200) : '';
  if (!v || !v.url || !/^https?:\/\//i.test(String(v.url)) || !text) return b.setText(text).build();
  return b.setText(text).setLinkUrl(0, text.length, String(v.url)).build();
}

/* ------------------------------ human edits ------------------------------ */

function onEdit(e) {
  try {
    if (!e || !e.range || e.range.getRow() < 2) return;
    var sh = e.range.getSheet();
    var def = defForSheet_(sh);
    if (!def || def.kind !== 'table') return;
    var cols = columnMap_(sh, def);
    if (!cols.id) return;
    var editable = {};
    def.cols.forEach(function (cd) { if (cd.editable && cols[cd.key]) editable[cols[cd.key]] = cd.key; });
    var r0 = e.range.getRow(), nr = e.range.getNumRows(), c0 = e.range.getColumn(), nc = e.range.getNumColumns();
    var ids = sh.getRange(r0, cols.id, nr, 1).getValues();
    var rows = [];
    for (var c = c0; c < c0 + nc; c++) {
      if (!editable[c]) continue;
      var vals = sh.getRange(r0, c, nr, 1).getValues();
      for (var i = 0; i < nr; i++) {
        if (ids[i][0] === '') continue;
        rows.push([Utilities.getUuid(), def.key, String(ids[i][0]), editable[c], String(vals[i][0]).substring(0, 4000),
                   new Date()]);
      }
    }
    if (rows.length) {
      // appendRow is atomic, so concurrent edits and the sync's deletes never overwrite each other.
      var log = editsSheet_(e.source);
      var lock = LockService.getDocumentLock();
      var locked = lock.tryLock(3000);
      try { rows.forEach(function (r) { log.appendRow(r); }); } finally { if (locked) lock.releaseLock(); }
    }
  } catch (err) {
    // Never break the person's editing. The next sync reconciles.
  }
}

function pendingEdits_(ss) {
  var log = editsSheet_(ss);
  var n = log.getLastRow() - 1;
  if (n < 1) return [];
  return log.getRange(2, 1, n, 6).getValues().filter(function (r) { return r[0] !== ''; }).map(function (r) {
    return { edit_id: String(r[0]), tab: String(r[1]), row_id: String(r[2]), col: String(r[3]),
             value: String(r[4]), at: r[5] instanceof Date ? r[5].toISOString() : String(r[5]) };
  });
}

function ackEdits_(ss, ackIds) {
  if (!ackIds.length) return;
  var want = dict_();
  ackIds.forEach(function (id) { want[String(id)] = true; });
  var log = editsSheet_(ss);
  var last = log.getLastRow();
  if (last < 2) return;
  var ids = log.getRange(2, 1, last - 1, 1).getValues();
  // Delete contiguous runs bottom-up, one call each; rows appended meanwhile are below the snapshot.
  var r = ids.length - 1;
  while (r >= 0) {
    if (!want[String(ids[r][0])]) { r--; continue; }
    var end = r;
    while (r - 1 >= 0 && want[String(ids[r - 1][0])]) r--;
    log.deleteRows(r + 2, end - r + 1);
    r--;
  }
}

function editsSheet_(ss) {
  var sh = ss.getSheetByName(EDITS_TITLE);
  if (!sh) {
    sh = ss.insertSheet(EDITS_TITLE);
    sh.getRange(1, 1, 1, 6).setValues([['edit_id', 'tab', 'row_id', 'col', 'value', 'at']]);
    sh.hideSheet();
  }
  return sh;
}

/* ------------------------- tabs, columns, formatting ------------------------- */

function ensureAll_(ss, reformat) {
  TABS.forEach(function (t, i) {
    var sh = ensureTab_(ss, t, reformat);
    if (reformat) { ss.setActiveSheet(sh); ss.moveActiveSheet(i + 1); }
  });
  editsSheet_(ss);
  // Remove the empty default first sheet a new spreadsheet comes with (any language).
  ss.getSheets().forEach(function (sh) {
    if (ss.getSheets().length <= 1) return;
    if (sh.getName() === EDITS_TITLE || defForSheet_(sh)) return;
    if (sh.getLastRow() === 0 && sh.getLastColumn() === 0 && /^(Sheet|Blad|Hoja|Feuille|Tabelle|Foglio|Planilha)\s?1$/.test(sh.getName())) {
      ss.deleteSheet(sh);
    }
  });
  if (reformat) ss.setActiveSheet(ensureTab_(ss, tabDef_('start'), false));
}

function tabDef_(key) {
  for (var i = 0; i < TABS.length; i++) if (TABS[i].key === key) return TABS[i];
  return null;
}

function defForSheet_(sh) {
  var md = sh.getDeveloperMetadata().filter(function (m) { return m.getKey() === MD_TAB; });
  return md.length ? tabDef_(md[0].getValue()) : null;
}

function findTab_(ss, def) {
  var found = ss.createDeveloperMetadataFinder().withKey(MD_TAB).withValue(def.key).find();
  for (var i = 0; i < found.length; i++) {
    var loc = found[i].getLocation();
    if (loc.getLocationType() === SpreadsheetApp.DeveloperMetadataLocationType.SHEET) return loc.getSheet();
  }
  return null;
}

function ensureTab_(ss, def, reformat) {
  var sh = findTab_(ss, def);
  var fresh = false;
  if (!sh) {
    sh = ss.getSheetByName(def.title) || ss.insertSheet(def.title);
    sh.addDeveloperMetadata(MD_TAB, def.key);
    fresh = true;
  }
  if (def.kind !== 'table') return sh;
  var cols = columnMap_(sh, def);
  var missing = def.cols.filter(function (cd) { return !cols[cd.key]; });
  if (missing.length) {
    var next = (fresh && sh.getLastColumn() === 0) ? 1 : sh.getLastColumn() + 1;
    var needed = next + missing.length - 1;
    if (needed > sh.getMaxColumns()) sh.insertColumnsAfter(sh.getMaxColumns(), needed - sh.getMaxColumns());
    missing.forEach(function (cd, k) {
      var c = next + k;
      sh.getRange(1, c).setValue(cd.header);
      var letter = columnLetter_(c);
      sh.getRange(letter + ':' + letter).addDeveloperMetadata(MD_COL, cd.key);
    });
    reformat = true;
  }
  if (fresh && sh.getMaxRows() < PREALLOC_ROWS) sh.insertRowsAfter(sh.getMaxRows(), PREALLOC_ROWS - sh.getMaxRows());
  if (reformat) formatTable_(sh, def);
  return sh;
}

function columnMap_(sh, def) {
  var map = dict_();
  sh.createDeveloperMetadataFinder().withKey(MD_COL)
    .withLocationType(SpreadsheetApp.DeveloperMetadataLocationType.COLUMN).find()
    .forEach(function (m) { map[m.getValue()] = m.getLocation().getColumn().getColumn(); });
  return map;
}

function ensureCapacity_(sh, lastNeeded) {
  // New rows go inside the existing ranges (above the last row), so validation, colors and the filter
  // stretch over them.
  var max = sh.getMaxRows();
  if (lastNeeded >= max) sh.insertRowsAfter(Math.max(max - 1, 1), Math.max(lastNeeded - max + 1, 500));
}

function formatTable_(sh, def) {
  var cols = columnMap_(sh, def);
  var maxRows = sh.getMaxRows();
  var lastCol = Math.max(sh.getLastColumn(), 1);
  var header = sh.getRange(1, 1, 1, lastCol);
  header.setFontWeight('bold').setBackground(PALETTE.header[0]).setFontColor(PALETTE.header[1])
        .setWrap(true).setVerticalAlignment('middle');
  sh.setRowHeight(1, 36);
  sh.setFrozenRows(1);
  sh.setFrozenColumns(def.freezeCols || 1);
  var rules = [];
  def.cols.forEach(function (cd) {
    var c = cols[cd.key];
    if (!c) return;
    var body = sh.getRange(2, c, maxRows - 1, 1);
    var letter = columnLetter_(c);
    sh.getRange(1, c).setValue(cd.header).setNote(cd.note || (cd.editable
        ? 'You can edit this column. The agent reads your changes.'
        : 'Filled by the agent. Your changes here are overwritten.'));
    sh.setColumnWidth(c, cd.width);
    body.setNumberFormat(FORMATS[cd.type] || '@');
    body.setWrapStrategy(cd.type === 'long' ? SpreadsheetApp.WrapStrategy.WRAP : SpreadsheetApp.WrapStrategy.CLIP);
    body.setVerticalAlignment('top');
    if (cd.type === 'id') body.setFontColor(PALETTE.idText).setFontSize(8);
    if (cd.type === 'int' || cd.type === 'score' || cd.type === 'num2') body.setHorizontalAlignment('right');
    if (cd.editable) body.setBackground(PALETTE.editable);
    if (cd.choices) {
      body.setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(cd.choices, true)
        .setAllowInvalid(false).setHelpText('Pick one of: ' + cd.choices.join(', ')).build());
    }
    if (cd.type === 'status') {
      Object.keys(STATUS_GROUPS).forEach(function (g) {
        var re = '^(' + STATUS_GROUPS[g].map(escapeRe_).join('|') + ')$';
        rules.push(SpreadsheetApp.newConditionalFormatRule()
          .whenFormulaSatisfied('=REGEXMATCH($' + letter + '2&"", "' + re + '")')
          .setBackground(PALETTE[g][0]).setFontColor(PALETTE[g][1]).setRanges([body]).build());
      });
    }
    if (cd.type === 'score') rules.push(gradient_(body, 0, 60, 100));
    if (cd.key === 'qc' || cd.key === 'score') rules.push(gradient_(body, 1, 3.5, 5));
    if (cd.key === 'follow_up_due') {
      rules.push(SpreadsheetApp.newConditionalFormatRule()
        .whenFormulaSatisfied('=AND($' + letter + '2<>"", $' + letter + '2<TODAY())')
        .setBackground(PALETTE.wait[0]).setFontColor(PALETTE.wait[1]).setRanges([body]).build());
    }
  });
  sh.setConditionalFormatRules(rules);
  // Remove our old warning-only protections first so repeated formatting does not stack them.
  sh.getProtections(SpreadsheetApp.ProtectionType.RANGE).forEach(function (p) {
    if (p.getDescription() === AGENT_PROTECTION) p.remove();
  });
  def.cols.forEach(function (cd) {
    if (cd.editable || cd.key === 'id' || !cols[cd.key]) return;
    sh.getRange(2, cols[cd.key], maxRows - 1, 1).protect().setDescription(AGENT_PROTECTION).setWarningOnly(true);
  });
  var full = sh.getRange(1, 1, maxRows, lastCol);
  if (!sh.getFilter()) full.createFilter();
  sh.getBandings().forEach(function (b) { b.remove(); });
  full.applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, true, false).setHeaderRowColor(PALETTE.header[0]);
  // Banding recolors the header background; keep the header text readable.
  header.setFontColor(PALETTE.header[1]).setFontWeight('bold');
}

function gradient_(range, lo, mid, hi) {
  return SpreadsheetApp.newConditionalFormatRule()
    .setGradientMinpointWithValue('#f4cccc', SpreadsheetApp.InterpolationType.NUMBER, String(lo))
    .setGradientMidpointWithValue('#fff2cc', SpreadsheetApp.InterpolationType.NUMBER, String(mid))
    .setGradientMaxpointWithValue('#d9ead3', SpreadsheetApp.InterpolationType.NUMBER, String(hi))
    .setRanges([range]).build();
}

function escapeRe_(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

function columnLetter_(c) {
  var s = '';
  while (c > 0) { var m = (c - 1) % 26; s = String.fromCharCode(65 + m) + s; c = Math.floor((c - 1) / 26); }
  return s;
}

/* ------------------------------ rendered tabs ------------------------------ */

function resetRenderTab_(sh) {
  var all = sh.getRange(1, 1, sh.getMaxRows(), sh.getMaxColumns());
  all.breakApart();
  sh.getBandings().forEach(function (b) { b.remove(); });
  sh.clear();
  sh.clearNotes();
  sh.setConditionalFormatRules([]);
  sh.setFrozenRows(0);
  sh.setFrozenColumns(0);
}

function renderStart_(ss, meta) {
  var sh = ensureTab_(ss, tabDef_('start'), false);
  resetRenderTab_(sh);
  var lines = [
    ['Your job hunt log'],
    ['This sheet shows what your job hunt agent did. The master copy lives on your computer; this is a readable view that updates about every 20 minutes.'],
    ['Agent state: ' + (meta.agent_state || 'unknown') + '    Last update: ' + fmtTime_(ss, meta.last_sync)],
    [''],
    ['Tabs'],
    ['Dashboard: today, this week and all time at a glance, limits used today, anything stopped.'],
    ['Approvals: messages and applications waiting for you. Pick Approve or Skip in "Your decision".'],
    ['Jobs: every job that passed your filters, with the fit score and the reason.'],
    ['Skipped by filters: jobs your filters removed in the last 30 days, with the reason. Pick "Apply anyway" to override.'],
    ['Applications: what was submitted, where, with which resume. Fill in "Outcome" when you hear back.'],
    ['Outreach: every email and LinkedIn message sent, to whom and why.'],
    ['Follow-ups: each conversation, when the one follow-up is due, and what they said.'],
    ['QC log: how each draft did in the quality check.'],
    ['Daily summary: one line per day.'],
    ['Alerts: anything that made the agent stop, and what to do about it.'],
    ['Limits and settings: the limits the agent works under and how much is used today.'],
    [''],
    ['How to read it'],
    ['Colors: green means done, amber means waiting, red means stopped or failed, grey means skipped, blue means a reply or an interview.'],
    ['You can edit the light yellow columns: Your decision, Your call, Outcome and Your notes. Everything else is filled by the agent and overwritten.'],
    ['You may sort, filter, rename tabs, move columns and add your own columns. Please do not delete rows or the ID column.'],
    ['Pause the agent: run ./jobhunter pause in the terminal, or send /jh pause in your chat.'],
    ['Something looks wrong? Job Hunter > Re-apply formatting, or run ./jobhunter sheet sync --full on your computer.']
  ];
  sh.getRange(1, 1, lines.length, 1).setValues(lines.map(function (l) { return [safeText_(l[0])]; }));
  sh.setColumnWidth(1, 900);
  sh.getRange(1, 1, lines.length, 1).setWrap(true).setVerticalAlignment('top');
  sh.getRange(1, 1).setFontSize(18).setFontWeight('bold').setFontColor(PALETTE.header[0]);
  sh.getRange(3, 1).setFontWeight('bold')
    .setBackground(/^Running/.test(meta.agent_state || '') ? PALETTE.good[0] : PALETTE.wait[0]);
  [5, 18].forEach(function (r) { sh.getRange(r, 1).setFontWeight('bold').setFontSize(12).setBackground(PALETTE.section); });
}

function renderDashboard_(ss, d) {
  var sh = ensureTab_(ss, tabDef_('dashboard'), false);
  resetRenderTab_(sh);
  sh.setColumnWidth(1, 240);
  sh.setColumnWidths(2, 5, 130);
  var r = 1;
  sh.getRange(r, 1).setValue('Dashboard').setFontSize(18).setFontWeight('bold').setFontColor(PALETTE.header[0]);
  var state = String(d.agent_state || 'unknown');
  sh.getRange(r, 2, 1, 4).merge().setValue(safeText_('Agent: ' + state)).setFontWeight('bold')
    .setHorizontalAlignment('center')
    .setBackground(/^Running/.test(state) ? PALETTE.good[0] : (/^Paused/.test(state) ? PALETTE.wait[0] : PALETTE.bad[0]))
    .setFontColor(/^Running/.test(state) ? PALETTE.good[1] : (/^Paused/.test(state) ? PALETTE.wait[1] : PALETTE.bad[1]));
  r += 1;
  sh.getRange(r, 1).setValue('Updated ' + fmtTime_(ss, d.updated_at)).setFontColor('#666666');
  r += 2;
  if (num_(d.undelivered_high) > 0) {
    sh.getRange(r, 1, 1, 5).merge().setValue(num_(d.undelivered_high) + ' important messages to you could not be ' +
      'delivered to your chat. Run ./jobhunter inbox on your computer or check the Alerts tab.')
      .setFontWeight('bold').setBackground(PALETTE.bad[0]).setFontColor(PALETTE.bad[1]).setWrap(true);
    sh.setRowHeight(r, 42);
    r += 2;
  }
  r = section_(sh, r, 'At a glance', asArray_(d.headline).map(function (h) { return [h.label, num_(h.value)]; }), 2, false);
  var act = d.activity || {};
  r = section_(sh, r, 'Activity', [act.columns ? [''].concat(act.columns) : []].concat(asArray_(act.rows)), 5, true);
  r = section_(sh, r, 'Funnel (all time)', asArray_(d.funnel).map(function (f) { return [f[0], num_(f[1])]; }), 2, false);
  var limits = asArray_(d.limits).map(function (l) {
    var used = num_(l.used), lim = Math.max(num_(l.limit), 1);
    var color = used >= lim ? '#cc0000' : (used >= 0.8 * lim ? '#e69138' : '#6aa84f');
    return [l.area, l.item, used + ' of ' + lim,
            '=SPARKLINE(' + used + ',{"charttype","bar";"max",' + lim + ';"color1","' + color + '"})'];
  });
  r = section_(sh, r, 'Limits used today', [['Area', 'Limit', 'Used', '']].concat(limits), 4, true);
  r = section_(sh, r, 'Safety', [['Area', 'State', 'Since', 'Reason', 'What to do']]
      .concat(asArray_(d.safety).map(function (s) { return [s.area, s.state, s.since || '', s.reason || '', s.todo || '']; })), 5, true);
  r = section_(sh, r, 'Needs your attention', asArray_(d.attention).map(function (a) { return [a.item, num_(a.count), a.where]; }), 3, false);
  r = section_(sh, r, 'Top reasons jobs were skipped (7 days)', asArray_(d.skip_reasons).map(function (s) { return [s[0], num_(s[1])]; }), 2, false);
  if (d.trend && d.trend.series) {
    var rows = asArray_(d.trend.series).map(function (s) {
      var vals = asArray_(s.values).map(num_);
      var arr = vals.join(',');
      return [s.label, '=SPARKLINE({' + arr + '},{"charttype","column";"color","#1f3a5f"})',
              vals.reduce(function (a, b) { return a + b; }, 0) + ' in 14 days'];
    });
    section_(sh, r, 'Last 14 days', rows, 3, false);
  }
  sh.getRange(1, 1, sh.getMaxRows(), 6).setVerticalAlignment('middle');
}

function section_(sh, r, title, rows, width, headerRow) {
  sh.getRange(r, 1, 1, width).setBackground(PALETTE.section);
  sh.getRange(r, 1).setValue(title).setFontWeight('bold').setFontSize(12);
  r++;
  var hasHeader = !!headerRow;
  rows = rows.filter(function (x) { return x && x.length; });
  if (!rows.length || (hasHeader && rows.length === 1)) {
    sh.getRange(r, 1).setValue('Nothing here yet.').setFontColor('#666666');
    return r + 2;
  }
  var block = rows.map(function (x) {
    var y = x.slice(0, width);
    while (y.length < width) y.push('');
    return y.map(function (v) {
      if (typeof v === 'number') return v;
      var s = String(v === null || v === undefined ? '' : v);
      return s.indexOf('=SPARKLINE(') === 0 ? s : safeText_(s);
    });
  });
  var range = sh.getRange(r, 1, block.length, width);
  range.setValues(block).setBorder(true, true, true, true, true, true, '#dddddd', null).setWrap(true);
  if (hasHeader) sh.getRange(r, 1, 1, width).setFontWeight('bold').setFontColor('#444444');
  return r + block.length + 1;
}

// rows: [Setting, Value, Used today, What it means, style?]. style 'section' is a heading row across the tab
// (How the agent works, Daily limits, Browser sites, Email finder); 'good', 'wait', 'bad' or 'muted' colours the
// Value cell (for example Allowed in green, Not allowed in grey, Stopped in red).
function renderSettings_(ss, rows) {
  var sh = ensureTab_(ss, tabDef_('settings'), false);
  resetRenderTab_(sh);
  var cell = function (v) { return safeText_(String(v === null || v === undefined ? '' : v)); };
  var styles = [];
  var block = [['Setting', 'Value', 'Used today', 'What it means']];
  rows.forEach(function (x) {
    x = asArray_(x);
    if (!x.length) return;
    block.push([cell(x[0]), cell(x[1]), cell(x[2]), cell(x[3])]);
    styles.push(String(x[4] || ''));
  });
  sh.getRange(1, 1, block.length, 4).setValues(block).setVerticalAlignment('top');
  sh.getRange(1, 1, 1, 4).setFontWeight('bold').setBackground(PALETTE.header[0]).setFontColor(PALETTE.header[1]);
  sh.setFrozenRows(1);
  sh.setColumnWidth(1, 280); sh.setColumnWidth(2, 220); sh.setColumnWidth(3, 110); sh.setColumnWidth(4, 520);
  if (block.length > 1) {
    sh.getRange(2, 1, block.length - 1, 4).setWrap(true)
      .setBorder(true, true, true, true, true, true, '#dddddd', null);
  }
  styles.forEach(function (st, i) {
    var r = i + 2;
    if (st === 'section') {
      sh.getRange(r, 1, 1, 4).merge().setFontWeight('bold').setFontSize(12).setBackground(PALETTE.section);
      if (r > 2) sh.setRowHeight(r, 30);
      sh.getRange(r, 1).setVerticalAlignment('bottom');
    } else if (['good', 'wait', 'bad', 'muted', 'info'].indexOf(st) >= 0) {
      sh.getRange(r, 2).setBackground(PALETTE[st][0]).setFontColor(PALETTE[st][1]).setFontWeight('bold');
    }
  });
}

/* ------------------------------ menu and secret ------------------------------ */

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Job Hunter')
    .addItem('Set up or repair this sheet', 'menuSetup')
    .addItem('Show connection secret', 'menuShowSecret')
    .addItem('Make a new secret', 'menuRotateSecret')
    .addItem('Re-apply formatting', 'menuFormat')
    .addToUi();
}

function menuSetup() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  ensureAll_(ss, true);
  renderStart_(ss, JSON.parse(PropertiesService.getScriptProperties().getProperty(PROP_META) || '{}'));
  var secret = secret_(false);
  showSecretDialog_(secret, 'Setup is done. Next: Deploy > New deployment > Web app (Execute as: Me, Who has ' +
    'access: Anyone). Then run ./jobhunter sheet connect on your computer and paste the web app URL and this secret.');
}

function menuShowSecret() { showSecretDialog_(secret_(false), 'Paste this when ./jobhunter sheet connect asks for it.'); }

function menuRotateSecret() {
  var ui = SpreadsheetApp.getUi();
  if (ui.alert('Make a new secret?', 'The agent stops updating this sheet until you run ./jobhunter sheet connect again.',
      ui.ButtonSet.OK_CANCEL) !== ui.Button.OK) return;
  showSecretDialog_(secret_(true), 'New secret. Run ./jobhunter sheet connect and paste it.');
}

function menuFormat() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  TABS.forEach(function (t) { if (t.kind === 'table') formatTable_(ensureTab_(ss, t, false), t); });
  SpreadsheetApp.getUi().alert('Formatting re-applied.');
}

function secret_(rotate) {
  var props = PropertiesService.getScriptProperties();
  var s = props.getProperty(PROP_SECRET);
  if (!s || rotate) {
    s = (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, '').toLowerCase();
    props.setProperty(PROP_SECRET, s);
  }
  return s;
}

function checkSecret_(given) {
  var want = PropertiesService.getScriptProperties().getProperty(PROP_SECRET);
  if (!want || typeof given !== 'string' || given.length !== want.length) return false;
  var diff = 0;
  for (var i = 0; i < want.length; i++) diff |= want.charCodeAt(i) ^ given.charCodeAt(i);
  return diff === 0;
}

function showSecretDialog_(secret, text) {
  var html = HtmlService.createHtmlOutput(
    '<p style="font-family:Arial,sans-serif;font-size:13px">' + escHtml_(text) + '</p>' +
    '<p style="font-family:monospace;font-size:14px;background:#f3f3f3;padding:8px;word-break:break-all">' +
    escHtml_(secret) + '</p>' +
    '<p style="font-family:Arial,sans-serif;font-size:12px;color:#666">Keep this secret private. Anyone with the ' +
    'web app URL and this secret can write to this sheet.</p>').setWidth(560).setHeight(260);
  SpreadsheetApp.getUi().showModalDialog(html, 'Job Hunter connection');
}

/* --------------------------------- helpers --------------------------------- */

function saveMeta_(start) {
  var props = PropertiesService.getScriptProperties();
  var meta = JSON.parse(props.getProperty(PROP_META) || '{}');
  if (start && start.agent_state) meta.agent_state = String(start.agent_state).substring(0, 120);
  meta.last_sync = new Date().toISOString();
  props.setProperty(PROP_META, JSON.stringify(meta));
  return meta;
}

function fmtTime_(ss, iso) {
  if (!iso) return 'never';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return 'unknown';
  return Utilities.formatDate(d, ss.getSpreadsheetTimeZone(), 'd MMM yyyy HH:mm');
}

function num_(v) { var n = Number(v); return isFinite(n) ? n : 0; }
function escHtml_(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
