/**
 * Gianluigi — make the workbook feel alive. [2026-08-13]
 *
 * Eyal: "if we change dates on the timeline or in the project tabs i will want
 * to see it happening live … if not nechama might think that it is not working."
 *
 * A thirty-minute cycle is indistinguishable from a broken one — there is no way
 * to tell "not yet" from "never". This posts the edited tab's name to
 * Gianluigi's /sync endpoint and writes the answer into a status cell, so an
 * edit produces visible confirmation in a few seconds.
 *
 * ── INSTALL ────────────────────────────────────────────────────────────────
 * 1. Open the Project Status workbook → Extensions → Apps Script.
 * 2. Paste this file over Code.gs and Save.
 * 3. Project Settings → Script Properties → add two:
 *        GIANLUIGI_URL    https://gianluigi-378037201341.europe-west1.run.app
 *        GIANLUIGI_TOKEN  <the SHEET_SYNC_TOKEN value>
 * 4. Run `installTrigger` once from the editor and accept the permissions
 *    prompt. (An INSTALLABLE trigger is required: a simple onEdit cannot call
 *    an external URL — UrlFetchApp needs authorisation a simple trigger has
 *    not got.)
 *
 * ── WHY IT CANNOT LOOP ─────────────────────────────────────────────────────
 * Google fires edit triggers on USER edits only; writes made through the Sheets
 * API — which is everything Gianluigi does — never fire them. The status-cell
 * guard below is belt and braces, not the actual defence.
 */

var STATUS_CELL = 'A1';           // written on the edited tab itself
var LOCK_WAIT_MS = 100;           // don't queue behind another edit; drop instead
var TIMEOUT_MS = 25000;

/**
 * Run this ONCE from the editor. It reports through the execution log, never
 * through a dialog.
 *
 * The first version ended with `SpreadsheetApp.getUi().alert(...)`, which is
 * wrong for a function you run from the editor: the dialog renders in the
 * SPREADSHEET tab, so the execution blocks waiting for a click nobody can see
 * and the editor shows it "running" until the six-minute limit kills it. The
 * trigger itself was already created by then — the alert was the only thing
 * still pending. [2026-08-13]
 */
function installTrigger() {
  var ss = SpreadsheetApp.getActive();
  var removed = 0;
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'onSheetEdit') {
      ScriptApp.deleteTrigger(t);
      removed++;
    }
  });
  ScriptApp.newTrigger('onSheetEdit').forSpreadsheet(ss).onEdit().create();

  // Prove the configuration too, while a human is watching the log — a missing
  // property otherwise shows up only as a silent no-op on the first real edit.
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('GIANLUIGI_URL');
  var token = props.getProperty('GIANLUIGI_TOKEN');
  Logger.log('Gianluigi sync installed (replaced ' + removed + ' old trigger(s)).');
  Logger.log('GIANLUIGI_URL   : ' + (url || 'MISSING — set it in Script Properties'));
  Logger.log('GIANLUIGI_TOKEN : ' + (token ? 'set (' + token.length + ' chars)'
                                           : 'MISSING — set it in Script Properties'));
  return 'installed';
}

/**
 * Optional: run this to test the endpoint without editing a cell.
 * Writes nothing to the sheet; just returns what the server said.
 */
function testSync() {
  Logger.log(callSync_('Meetings'));
}

function onSheetEdit(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  var tab = sheet.getName();

  // Never react to our own status write. Costs one comparison and removes the
  // need to reason about trigger semantics every time this file is read.
  if (e.range.getA1Notation() === STATUS_CELL) return;

  // NO COLUMN GUARD. An earlier version skipped edits to the hidden `_uid`
  // columns by reading the header — from ROW 1, which is the banner on the area
  // tabs and the title on the Timeline, so it inspected the wrong row entirely.
  //
  // It is gone rather than fixed, because it protected nothing. This call does
  // not send the edited cell; it asks the server to reconcile the whole
  // SURFACE, and the engine re-reads every row and applies its own rules
  // regardless of which cell was touched. Skipping the request would only mean
  // a slower sync for that edit, never a safer one — and one less thing to be
  // wrong about the row a header lives on. [2026-08-13]

  // DROP, don't queue. Pasting a column fires this once per cell; each would
  // otherwise wait its turn and hammer the endpoint long after the first call
  // already covered every one of them (the reconcile reads the sheet whole).
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(LOCK_WAIT_MS)) return;

  try {
    setStatus_(sheet, 'syncing…');
    var res = callSync_(tab);
    setStatus_(sheet, res);
  } catch (err) {
    // The message matters: a person needs to tell "the system rejected this"
    // from "the system never heard about it".
    setStatus_(sheet, 'not synced — ' + String(err).slice(0, 90));
  } finally {
    lock.releaseLock();
  }
}

function callSync_(tab) {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('GIANLUIGI_URL');
  var token = props.getProperty('GIANLUIGI_TOKEN');
  if (!url || !token) return 'not configured';

  var resp = UrlFetchApp.fetch(url.replace(/\/$/, '') + '/sync', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token },
    payload: JSON.stringify({ tab: tab }),
    muteHttpExceptions: true,
    followRedirects: true,
    validateHttpsCertificates: true,
    deadline: TIMEOUT_MS / 1000
  });

  var code = resp.getResponseCode();
  if (code === 401) return 'not synced — token rejected';
  if (code === 503) return 'not synced — sync is switched off';
  if (code >= 400) return 'not synced — server said ' + code;

  var body = {};
  try { body = JSON.parse(resp.getContentText()); } catch (ignored) {}
  return body.message || 'synced';
}

function setStatus_(sheet, message) {
  if (!message) return;
  var stamp = Utilities.formatDate(new Date(),
      SpreadsheetApp.getActive().getSpreadsheetTimeZone(), 'HH:mm');
  // Written as a NOTE, never as a cell value. Every one of these tabs is
  // regenerated from the database, and a value in A1 would either be wiped on
  // the next refresh or — worse on the area tabs — be read back as data.
  sheet.getRange(STATUS_CELL).setNote('Gianluigi ' + stamp + ' — ' + message);
}
