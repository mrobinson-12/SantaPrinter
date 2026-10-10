'use strict';
const $ = id => document.getElementById(id);
const terminal = new Set(['sent', 'failed', 'uncertain', 'cancelled']);
let paused = false, busy = false, pending = null, polling = null, activeJob = null;
const draftKey = 'santa-draft-v1';
const setText = (id, text) => { $(id).textContent = text; };
function error(id, text) { setText(id, text || ''); $(id).hidden = !text; }
function counter() { setText('counter', `${$('message').value.length.toLocaleString()} / 1,000`); }
function saveDraft() {
  try { sessionStorage.setItem(draftKey, JSON.stringify({ name: $('name').value, message: $('message').value, pending, activeJob })); } catch (_) {}
}
try {
  const draft = JSON.parse(sessionStorage.getItem(draftKey) || '{}');
  $('name').value = draft.name || ''; $('message').value = draft.message || '';
  pending = draft.pending || null; activeJob = draft.activeJob || null;
} catch (_) {}
counter();
// Persist the wish and retry identifier, never the access code.
for (const id of ['name', 'message', 'code']) $(id).addEventListener('input', () => {
  if (id !== 'code') { pending = null; counter(); saveDraft(); }
  error(id + '-error', ''); $(id).removeAttribute('aria-invalid');
});
$('code').addEventListener('input', () => { $('code').value = $('code').value.replace(/[^0-9]/g, '').slice(0, 4); });
async function api(url, options = {}) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
  let response;
  try { response = await fetch(url, { credentials: 'same-origin', ...options, signal: controller.signal }); }
  catch (failure) { if (failure.name === 'AbortError') throw new TypeError('Connection timed out'); throw failure; }
  finally { clearTimeout(timer); }
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(body.detail || 'Something went wrong. Please try again.'), { fields: body.fields, status: response.status });
  return body;
}
async function refreshStatus() {
  try {
    const status = await api('/api/status');
    setText('title', status.visitor_title); setText('intro', status.visitor_intro);
    paused = status.paused_submissions; $('closed').hidden = !paused;
    $('submit').disabled = paused || busy;
  } catch (_) { /* A failed status poll must not discard a visitor's draft. */ }
}
function display(job) {
  $('letter-form').hidden = true; $('result').hidden = false;
  setText('letter-reference', 'Letter reference: ' + job.reference + (job.queue_position ? ' · Position ' + job.queue_position + ' among outstanding letters' : ''));
  setText('letter-delay', 'Your letter is taking a little longer. Your wish is saved; please keep this page open or ask the event team using your reference.'); $('letter-delay').hidden = !job.delayed;
  setText('job-status', job.label); $('job-status').classList.toggle('in-progress', !terminal.has(job.state));
  error('job-error', job.error);
  if (job.reply) { $('receipt').hidden = false; setText('reply', job.reply); }
  if (job.preview) {
    if ($('preview').getAttribute('src') !== job.preview) $('preview').src = job.preview;
    $('preview').hidden = false;
  }
  $('collect').hidden = job.state !== 'sent';
  $('another').hidden = !terminal.has(job.state);
  setText('result-heading', job.state === 'sent' ? 'A little magic, just for you.' : ['failed', 'uncertain'].includes(job.state) ? 'The event team can help.' : job.state === 'cancelled' ? 'Letter cancelled.' : 'Your letter is on its way.');
}
async function poll() {
  if (!activeJob) return;
  try {
    const job = await api('/api/letters/' + activeJob);
    display(job);
    if (!terminal.has(job.state)) polling = setTimeout(poll, 2000);
  } catch (failure) {
    if (failure.status === 404) { setText('result-heading', 'Letter no longer available.'); setText('letter-reference', 'Letter reference: ' + activeJob.slice(0, 8).toUpperCase()); error('job-error', 'This letter is no longer available. It may have expired. Ask the event team, or write a new letter.'); $('another').hidden = false; setText('job-status', 'Letter no longer available'); return; }
    error('job-error', 'Connection interrupted. We are checking your letter again; please do not resubmit.');
    polling = setTimeout(poll, 4000);
  }
}
$('letter-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy || paused) return;
  error('form-error', ''); let invalid = false, firstInvalid = null;
  for (const id of ['name', 'message', 'code']) {
    const value = $(id).value.trim();
    const issue = id === 'message' && !value ? 'Tell Santa your Christmas wish.' : id === 'code' && !/^[0-9]{4}$/.test(value) ? 'Enter the four-digit event code.' : '';
    error(id + '-error', issue);
    $(id).setAttribute('aria-invalid', issue ? 'true' : 'false');
    if (issue) { invalid = true; firstInvalid ||= $(id); }
  }
  if (invalid) { firstInvalid.focus(); return; }
  busy = true; $('submit').disabled = true; setText('submit', 'Sending your wish…');
  pending ||= crypto.randomUUID(); saveDraft();
  try {
    const job = await api('/api/letters', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      name: $('name').value.trim(), message: $('message').value.trim(), code: $('code').value,
      idempotency_key: pending
    }) });
    activeJob = job.id; saveDraft(); display(job); $('result-heading').focus();
    $('code').value = ''; poll();
  } catch (failure) {
    if (failure.fields) {
      for (const [field, text] of Object.entries(failure.fields)) if ($(field + '-error')) {
        error(field + '-error', text); $(field).setAttribute('aria-invalid', 'true');
      }
    }
    const text = failure instanceof TypeError ? 'Connection interrupted. Your wish is saved. Tap Send again to check this same submission.' : failure.message;
    if (text.includes('code is incorrect')) { error('code-error', text); $('code').setAttribute('aria-invalid', 'true'); $('code').focus(); }
    else error('form-error', text);
  } finally { busy = false; $('submit').disabled = paused; setText('submit', 'Send to Santa ↗'); }
});
$('another').addEventListener('click', () => {
  if (polling) clearTimeout(polling);
  $('letter-form').reset(); activeJob = null; pending = null;
  try { sessionStorage.removeItem(draftKey); } catch (_) {}
  $('result').hidden = true; $('letter-form').hidden = false; $('receipt').hidden = true;
  $('preview').hidden = true; $('preview').removeAttribute('src'); error('form-error', '');
  counter(); refreshStatus(); $('name').focus();
});
refreshStatus(); setInterval(refreshStatus, 15000);
if (activeJob) { $('letter-form').hidden = true; $('result').hidden = false; poll(); }
