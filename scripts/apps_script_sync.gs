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

function installTrigger() {
  var ss = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'onSheetEdit') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('onSheetEdit').forSpreadsheet(ss).onEdit().create();
  SpreadsheetApp.getUi().alert('Gianluigi sync installed.');
}

function onSheetEdit(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  var tab = sheet.getName();

  // Never react to our own status write. Costs one comparison and removes the
  // need to reason about trigger semantics every time this file is read.
  if (e.range.getA1Notation() === STATUS_CELL) return;

  // Hidden identity columns are system-owned; an edit there is a paste
  // accident, and syncing on it would ask the engine to adopt whatever landed.
  var header = String(sheet.getRange(1, e.range.getColumn()).getValue() || '');
  if (header.charAt(0) === '_') return;

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
