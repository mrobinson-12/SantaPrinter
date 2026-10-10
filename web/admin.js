'use strict';
const $ = id => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value; };
let currentTab = 'overview', overview = null, selectedJob = null, codeSnapshotAt = 0, serverSnapshot = 0, previewURL = null;
let loading = false, messageTimer = null, queuePage = 1, queueTotal = 0, smsDirty = false, smsSnapshot = null;
function notice(message, bad = false) {
  text('admin-status', message); $('admin-status').hidden = !message;
  $('admin-status').classList.toggle('error', bad);
  if (messageTimer) clearTimeout(messageTimer);
  if (!bad) messageTimer = setTimeout(() => { $('admin-status').hidden = true; }, 6000);
}
async function api(path, method = 'GET', body = {}, raw = false) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), ['diagnostics/ai-test', 'notifications/test'].includes(path) ? 60000 : 20000);
  let response;
  try { response = await fetch('/admin/api/' + path, { credentials: 'same-origin', method, signal: controller.signal,
    ...(method !== 'GET' ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) }); }
  catch (failure) { if (failure.name === 'AbortError') throw new Error('Request timed out. Check service health before repeating a print action.'); throw failure; }
  finally { clearTimeout(timer); }
  if (!response.ok) {
    let detail = 'Admin session expired or request failed. Reload to sign in again.';
    try { const data = await response.json(); detail = data.detail || detail; if (data.fields) detail += ' ' + Object.entries(data.fields).map(([key, value]) => key + ': ' + value).join('; '); } catch (_) {}
    throw Object.assign(new Error(detail), { status: response.status });
  }
  return raw ? response.blob() : response.json();
}
function time(value) { return value ? new Date(value * 1000).toLocaleString() : '—'; }
function countdown(value) {
  const now = serverSnapshot + (performance.now() - codeSnapshotAt) / 1000;
  const seconds = Math.max(0, Math.ceil(value - now));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
function updateCountdowns() {
  if (!overview) return;
  const codes = overview.codes;
  text('current-countdown', codes.current ? 'Rotates in ' + countdown(codes.current.rotates) : 'Not available');
  text('previous-countdown', codes.previous ? 'Expires in ' + countdown(codes.previous.expires) : 'No previous code');
}
function renderOverview(data) {
  overview = data; serverSnapshot = data.server_time; codeSnapshotAt = performance.now();
  text('actor', data.actor); const m = data.metrics;
  text('metric-total', Object.values(m).reduce((a, b) => a + b, 0));
  text('metric-queue', ['waiting', 'generating', 'ready', 'sending'].reduce((n, key) => n + (m[key] || 0), 0));
  text('metric-sent', m.sent || 0); text('metric-errors', (m.failed || 0) + (m.uncertain || 0));
  text('current-code', data.codes.current?.code || '—'); text('previous-code', data.codes.previous?.code || '—'); updateCountdowns();
  const delivered = data.codes.current?.delivered;
  text('delivery-status', data.codes.clock_warning ? 'Server clock moved backwards. Correct it before accepting codes.' : delivered ? 'Current code delivered to the webhook.' : 'Current code has not been delivered. The scheduler is retrying.');
  $('delivery-status').classList.toggle('error', !delivered);
  text('printer-status', data.printer.status); text('printer-checked', time(data.printer.at));
  text('model-status', data.health.last_ai_success ? 'Last AI success: ' + time(data.health.last_ai_success) : data.model_ready ? 'Key configured; connection not verified' : 'Prepared letters: API key missing');
  text('event-health', data.health.status);
  const h = data.health;
  text('health-details', `${h.paused_printing ? 'Printing manually paused. ' : ''}${h.paused_submissions ? 'Submissions paused. ' : ''}${h.hardware_hold ? 'Printer on automatic hold. ' : ''}Oldest waiting: ${h.oldest_waiting_age === null ? 'none' : Math.floor(h.oldest_waiting_age) + ' seconds'}. Free storage: ${Math.round(h.disk_free / 1048576)} MB. Last transmission: ${time(h.last_sent)}.`);
  text('worker-health', 'Workers: ' + (Object.entries(h.workers).map(([name, state]) => name.replaceAll('_', ' ') + ': ' + (state.healthy ? 'running' : 'stale')).join(' · ') || 'not started'));
  text('generation-health', `Recent prepared letters: ${h.recent_generation.filter(j => j.fallback).length} / ${h.recent_generation.length}. Last fallback: ${time(h.last_fallback)}. Last AI issue: ${h.last_ai_error || 'none'}.`);
  $('incidents').replaceChildren();
  for (const incident of data.incidents) {
    if (incident.acknowledged) continue;
    const card = document.createElement('div'); card.className = 'notice error';
    const description = document.createElement('p'); description.textContent = `${incident.summary} Affected: ${incident.affected}. Last checked: ${time(incident.checked)}.`; card.append(description);
    const actions = document.createElement('div'); actions.className = 'actions';
    function action(label, fn) { const b = document.createElement('button'); b.className = 'secondary'; b.textContent = label; b.onclick = fn; actions.append(b); }
    if (incident.kind === 'printer') action('Check printer again', () => $('check-printer').click());
    if (incident.kind === 'codes') action('Resend current code', () => $('resend').click());
    if (['uncertain', 'jobs', 'stalled'].includes(incident.kind)) action('View affected letters', () => { $('filter').value = incident.kind === 'uncertain' ? 'uncertain' : incident.kind === 'jobs' ? 'failed' : ''; queuePage = 1; $('tab-queue').click(); });
    if (['worker', 'disk', 'clock', 'app'].includes(incident.kind)) action('Open diagnostics', () => $('tab-diagnostics').click());
    if (!incident.acknowledged) action('Acknowledge alert', async () => { try { await api('incidents/' + incident.id + '/acknowledge', 'POST'); await refresh(); } catch (e) { notice(e.message, true); } });
    card.append(actions); $('incidents').append(card);
  }
  text('sms-state', !data.notifications.configured ? 'Twilio credentials are not configured on the host.' : data.notifications.tested ? 'Test messages delivered to all recipients.' : 'Save settings and send a test; wait for delivery to every recipient.');
  text('sms-enable', data.settings.sms_enabled ? 'Disable SMS' : 'Enable SMS');
  $('sms-enable').disabled = !data.settings.sms_enabled && !data.notifications.tested;
  $('sms-history').replaceChildren();
  for (const row of data.notifications.history.slice(0, 8)) { const p = document.createElement('p'); p.textContent = `${time(row.created)} · ${row.recipient} · ${row.event.startsWith('test:') ? 'test' : row.event} · ${row.status}`; $('sms-history').append(p); }
  if (!smsDirty) renderSMSSettings(data.settings);
  text('pause-submissions', data.settings.paused_submissions ? 'Resume submissions' : 'Pause submissions');
  text('pause-printing', data.settings.paused_printing ? 'Resume printing' : 'Pause printing');
}
function history(container, entries, audit = false) {
  const element = $(container); element.replaceChildren();
  if (!entries.length) { element.textContent = 'No events yet.'; return; }
  for (const row of entries) {
    const item = document.createElement('div'); item.className = 'audit-item';
    const main = document.createElement('div');
    main.textContent = audit ? row.action + (row.detail ? ' · ' + row.detail : '') : (row.ok ? 'Delivered · ' : 'Failed · ') + row.detail;
    const sub = document.createElement('small'); sub.textContent = time(row.at) + (audit ? ' · ' + row.actor : ' · interval ' + row.slot);
    item.append(main, sub); element.append(item);
  }
}
async function loadHistory() {
  const deliveries = await api('deliveries');
  history('recent-deliveries', deliveries.slice(0, 8)); history('all-deliveries', deliveries);
  if (currentTab === 'diagnostics') history('audit', await api('audit'), true);
}
async function loadJobs() {
  const query = new URLSearchParams({ page: queuePage, search: $('queue-search').value });
  if ($('filter').value) query.set('state', $('filter').value);
  const result = await api('jobs?' + query); const jobs = result.items; queueTotal = result.total;
  text('queue-page', `Page ${queuePage} · ${queueTotal} letters`); $('queue-prev').disabled = queuePage <= 1; $('queue-next').disabled = queuePage * result.page_size >= queueTotal;
  $('jobs').replaceChildren(); $('empty-jobs').hidden = jobs.length !== 0;
  for (const job of jobs) {
    const row = document.createElement('tr');
    for (const value of [time(job.created), job.name || (job.parent === 'diagnostic' ? 'Test receipt' : 'Christmas visitor')]) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    const state = document.createElement('td'), pill = document.createElement('span'); pill.className = 'pill ' + job.state; pill.textContent = job.state + (job.fallback ? ' · prepared letter' : ''); state.append(pill); row.append(state);
    const message = document.createElement('td'); message.textContent = job.message.slice(0, 90) + (job.message.length > 90 ? '…' : ''); row.append(message);
    const action = document.createElement('td'), button = document.createElement('button'); button.textContent = 'View letter'; button.addEventListener('click', () => openJob(job.id)); action.append(button); row.append(action); $('jobs').append(row);
  }
}
async function openJob(id) {
  try {
    selectedJob = await api('jobs/' + id); renderJob(selectedJob);
    if (!$('job-dialog').open) $('job-dialog').showModal();
  } catch (error) { notice(error.message, true); }
}
function renderJob(job) {
  text('job-title', job.name ? 'Letter for ' + job.name : 'Letter details');
  text('job-meta', job.state + (job.ip ? ' · IP ' + job.ip : '') + ' · ' + job.id.slice(0, 8) + ' · created ' + time(job.created) + ' · updated ' + time(job.updated));
  text('job-message', job.message); text('job-reply', job.reply || 'No reply yet.');
  text('detail-error', job.error || ''); $('detail-error').hidden = !job.error;
  text('job-parent', job.parent ? 'Linked job: ' + job.parent : '');
  $('job-preview').hidden = !job.reply;
  if (job.reply) { $('job-preview').src = '/admin/api/jobs/' + job.id + '/preview'; $('job-preview').onerror = () => { $('job-preview').hidden = true; }; }
  $('job-actions').replaceChildren(); $('job-action-error').hidden = true;
  const buttons = [];
  if (['waiting', 'generating', 'ready'].includes(job.state)) buttons.push(['cancel', 'Cancel letter']);
  if (job.state === 'failed') buttons.push(['retry', job.reply ? 'Retry saved letter' : 'Retry generation']);
  if (job.state === 'uncertain') buttons.push(['appeared', 'Receipt appeared'], ['missing', 'Missing or partial — reprint'], ['review-cancel', 'Cancel after inspection']);
  if (['sent', 'failed', 'cancelled'].includes(job.state) && job.reply) buttons.push(['reprint', 'Reprint saved receipt']);
  text('job-timings', `Reference ${job.id.slice(0, 8).toUpperCase()} · Generation attempts ${job.generation_attempts} · Print attempts ${job.send_attempts} · Ready ${time(job.ready_at)} · Sent ${time(job.sent_at)}${job.destination ? ' · Destination ' + job.destination : ''}${job.fallback ? ' · Prepared Santa letter' : ''}`);
  $('job-events').replaceChildren();
  for (const entry of job.events || []) { const p = document.createElement('p'); p.textContent = `${time(entry.at)} · ${entry.stage} · ${entry.code}`; $('job-events').append(p); }
  $('job-links').replaceChildren();
  for (const id of [job.parent !== 'diagnostic' ? job.parent : null, ...(job.reprints || [])].filter(Boolean)) { const b = document.createElement('button'); b.textContent = 'Linked receipt ' + id.slice(0, 8); b.onclick = () => openJob(id); $('job-links').append(b); }
  for (const [action, label] of buttons) {
    const button = document.createElement('button'); button.className = action === 'reprint' ? 'danger' : 'secondary'; button.textContent = label;
    button.addEventListener('click', () => actOnJob(job, action)); $('job-actions').append(button);
  }
}
function confirmAction(title, message, image = null) {
  text('confirm-title', title); text('confirm-text', message);
  $('confirm-preview').hidden = !image;
  if (image) $('confirm-preview').src = image;
  $('confirm-dialog').showModal();
  return new Promise(resolve => {
    $('confirm-yes').onclick = () => { $('confirm-dialog').close('yes'); };
    $('confirm-no').onclick = () => { $('confirm-dialog').close('no'); };
    $('confirm-dialog').onclose = () => resolve($('confirm-dialog').returnValue === 'yes');
    // Escape closes a dialog without changing returnValue; explicitly reset it.
    $('confirm-dialog').returnValue = 'no';
  });
}
async function actOnJob(job, action) {
  const descriptions = {
    cancel: 'This stops the job before transmission. A model call already in progress may still finish.',
    retry: job.reply ? 'Render the saved reply and queue it again. No new model call is needed.' : 'Generate a new reply and queue it for printing. This uses the OpenAI API.',
    appeared: 'Confirm you inspected the printer and found the complete receipt. This marks it sent and does not resume printing.',
    missing: 'Confirm you inspected the printer. This records the missing or partial receipt and queues one exact replacement. Printing remains paused until you resume it.',
    'review-cancel': 'Confirm you inspected the printer and want to cancel this letter without printing a replacement.',
    reprint: 'Print another copy of this exact saved receipt. Check that a duplicate is wanted before confirming.'
  };
  if (!await confirmAction(action === 'reprint' ? 'Reprint this receipt?' : 'Confirm ' + action, descriptions[action], ['reprint', 'missing'].includes(action) ? '/admin/api/jobs/' + job.id + '/preview' : null)) return;
  const buttons = [...$('job-actions').querySelectorAll('button')]; buttons.forEach(b => { b.disabled = true; });
  try { const updated = await api('jobs/' + job.id + '/' + action, 'POST'); selectedJob = updated; renderJob(updated); await refresh(); notice('Action recorded.'); }
  catch (error) { text('job-action-error', error.message); $('job-action-error').hidden = false; }
  finally { buttons.forEach(b => { b.disabled = false; }); }
}
const fields = [
  ['printer_host', 'Printer hostname or IP', 'text'], ['printer_port', 'Printer TCP port', 'number', 1, 65535],
  ['webhook_url', 'Code webhook URL', 'url'], ['font_size', 'Receipt font size (points)', 'number', 8, 18],
  ['visitor_title', 'Visitor heading', 'text'], ['visitor_intro', 'Visitor introduction', 'textarea'],
  ['browser_cooldown', 'Per-browser cooldown (seconds)', 'number', 0, 3600], ['global_per_minute', 'Letters per minute, globally', 'number', 1, 1000],
  ['ip_per_minute', 'Default submissions per IP / minute', 'number', 1, 1000], ['ip_per_day', 'Default submissions per IP / 24 hours', 'number', 1, 10000],
  ['queue_limit', 'Maximum outstanding jobs', 'number', 1, 500], ['failed_codes_per_ip', 'Failed codes per IP / five minutes', 'number', 1, 100],
  ['retention_hours', 'Letter retention (hours)', 'number', 1, 168], ['fallback_enabled', 'Use prepared letters when AI fails', 'checkbox'], ['cut', 'Cut paper after each receipt', 'checkbox']
];
function renderSettings(settings) {
  $('settings-fields').replaceChildren();
  for (const [name, labelText, type, min, max] of fields) {
    const div = document.createElement('div'), label = document.createElement('label'); label.htmlFor = 'setting-' + name; label.textContent = labelText;
    const input = document.createElement(type === 'textarea' ? 'textarea' : 'input'); input.id = 'setting-' + name; input.name = name;
    if (type !== 'textarea') input.type = type;
    if (type === 'checkbox') { input.checked = settings[name]; div.className = 'checkbox-row'; div.append(input, label); }
    else { input.value = settings[name]; input.required = true; if (min !== undefined) { input.min = min; input.max = max; input.step = '1'; } div.append(label, input); }
    if (['webhook_url', 'visitor_title', 'visitor_intro'].includes(name)) div.classList.add('wide');
    if (type === 'text' || type === 'textarea') input.maxLength = name === 'visitor_intro' ? 500 : name === 'visitor_title' ? 120 : 253;
    $('settings-fields').append(div);
  }
}
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault(); const body = {}, button = event.submitter;
  for (const [name, _, type] of fields) { const input = $('setting-' + name); body[name] = type === 'checkbox' ? input.checked : type === 'number' ? Number(input.value) : input.value; }
  button.disabled = true;
  try { await api('settings', 'PATCH', body); notice('Settings saved.'); await refresh(); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
async function toggleSetting(name) {
  try { await api('settings', 'PATCH', { [name]: !overview.settings[name] }); await refresh(); notice('Event control updated.'); }
  catch (error) { notice(error.message, true); }
}
$('pause-submissions').onclick = () => toggleSetting('paused_submissions');
$('pause-printing').onclick = () => toggleSetting('paused_printing');
$('resend').onclick = async () => { try { await api('codes/resend', 'POST'); notice('Current code scheduled for delivery.'); await refresh(); } catch (error) { notice(error.message, true); } };
$('check-printer').onclick = async () => {
  $('check-printer').disabled = true;
  try { await api('printer/status', 'POST'); await refresh(); }
  catch (error) { notice(error.message, true); }
  finally { $('check-printer').disabled = false; }
};
$('render-test').onclick = async () => {
  $('render-test').disabled = true;
  try {
    const blob = await api('diagnostics/preview', 'POST', { text: $('diagnostic-text').value }, true);
    if (previewURL) URL.revokeObjectURL(previewURL);
    previewURL = URL.createObjectURL(blob); $('diagnostic-preview').src = previewURL; $('diagnostic-preview').hidden = false; await loadHistory();
  } catch (error) { notice(error.message, true); }
  finally { $('render-test').disabled = false; }
};
$('print-test').onclick = async () => {
  if (!await confirmAction('Print a test receipt?', 'This queues a real physical receipt using the text above. No AI call is made.')) return;
  $('print-test').disabled = true;
  try { const job = await api('diagnostics/test-print', 'POST', { text: $('diagnostic-text').value }); notice('Test receipt queued.'); await openJob(job.id); }
  catch (error) { notice(error.message, true); }
  finally { $('print-test').disabled = false; }
};
$('filter').onchange = () => { queuePage = 1; loadJobs().catch(error => notice(error.message, true)); };
let searchTimer;
$('queue-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { queuePage = 1; loadJobs().catch(e => notice(e.message, true)); }, 250); };
$('queue-prev').onclick = () => { queuePage = Math.max(1, queuePage - 1); loadJobs().catch(e => notice(e.message, true)); };
$('queue-next').onclick = () => { queuePage++; loadJobs().catch(e => notice(e.message, true)); };
$('close-dialog').onclick = () => $('job-dialog').close();
for (const tab of document.querySelectorAll('[data-tab]')) {
  tab.addEventListener('click', async () => {
    currentTab = tab.dataset.tab;
    for (const other of document.querySelectorAll('[data-tab]')) {
      const active = other === tab; other.setAttribute('aria-selected', String(active)); other.tabIndex = active ? 0 : -1;
      $('view-' + other.dataset.tab).hidden = !active;
    }
    try { if (currentTab === 'settings') renderSettings((await api('overview')).settings); await refresh(); }
    catch (error) { notice(error.message, true); }
  });
  tab.tabIndex = tab.dataset.tab === currentTab ? 0 : -1;
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); const tabs = [...document.querySelectorAll('[data-tab]')];
    let index = tabs.indexOf(tab); index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[index].focus(); tabs[index].click();
  });
}
async function refresh() {
  if (loading) return; loading = true;
  try {
    renderOverview(await api('overview')); $('admin-connection').hidden = true;
    if (currentTab === 'queue') await loadJobs();
    if (currentTab === 'ip') await loadIPs();
    await loadHistory();
    if ($('job-dialog').open && selectedJob && !$('confirm-dialog').open) {
      const updated = await api('jobs/' + selectedJob.id);
      if (updated.updated !== selectedJob.updated || updated.state !== selectedJob.state) { selectedJob = updated; renderJob(updated); }
    }
  } catch (error) { text('admin-connection', 'Dashboard disconnected. Last values may be stale. ' + error.message); $('admin-connection').hidden = false; notice(error.message, true); }
  finally { loading = false; }
}
refresh(); setInterval(refresh, 5000); setInterval(updateCountdowns, 1000);

function editIP(row) {
  $('ip-address').value = row.ip; $('ip-minute').value = row.per_minute ?? '';
  $('ip-day').value = row.per_day ?? ''; $('ip-note').value = row.note || '';
  $('ip-blocked').checked = row.blocked; text('ip-rule-title', 'Edit rule for ' + row.ip);
  $('ip-address').focus();
}
async function loadIPs() {
  const data = await api('ip-controls');
  if (!$('ip-default-minute').value) $('ip-default-minute').value = data.defaults.per_minute;
  if (!$('ip-default-day').value) $('ip-default-day').value = data.defaults.per_day;
  $('ip-rows').replaceChildren(); $('ip-empty').hidden = !!data.addresses.length;
  for (const row of data.addresses) {
    const tr = document.createElement('tr');
    for (const value of [row.ip, `${row.minute_count || 0} / ${row.day_count || 0}`, `${row.effective_minute} / ${row.effective_day}`, (row.blocked ? 'Blocked' : row.has_rule ? 'Custom rule' : 'Default') + (row.note ? ' · ' + row.note : '')]) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    const td = document.createElement('td'), actions = document.createElement('div'); actions.className = 'actions';
    function button(label, action) { const b = document.createElement('button'); b.textContent = label; b.type = 'button'; b.onclick = async () => { b.disabled = true; try { await action(); } catch (error) { notice(error.message, true); } finally { b.disabled = false; } }; actions.append(b); }
    button('Edit', () => editIP(row));
    button(row.blocked ? 'Unblock' : 'Block', async () => {
      if (!await confirmAction(row.blocked ? 'Unblock IP?' : 'Block IP?', `${row.ip}: ${row.blocked ? 'allow submissions within its limits' : 'prevent new letter submissions across all browsers using this IP'}.`)) return;
      await api('ip-controls', 'PUT', { ip: row.ip, blocked: !row.blocked, per_minute: row.per_minute, per_day: row.per_day, note: row.note });
      await loadIPs(); notice('IP rule updated.');
    });
    if (row.has_rule) button('Use defaults', async () => {
      if (!await confirmAction('Restore default limits?', 'Remove the saved rule and any block for ' + row.ip + '. Recent usage counts are kept.')) return;
      await api('ip-controls/remove', 'POST', { ip: row.ip }); await loadIPs(); notice('IP uses the default limits.');
    });
    td.append(actions); tr.append(td); $('ip-rows').append(tr);
  }
}
$('ip-reset').onclick = () => { $('ip-rule-form').reset(); text('ip-rule-title', 'Block or customise an IP'); };
$('ip-defaults-form').onsubmit = async event => {
  event.preventDefault(); event.submitter.disabled = true;
  try { await api('settings', 'PATCH', { ip_per_minute: Number($('ip-default-minute').value), ip_per_day: Number($('ip-default-day').value) }); await loadIPs(); notice('Default IP limits saved.'); }
  catch (error) { notice(error.message, true); } finally { event.submitter.disabled = false; }
};
$('ip-rule-form').onsubmit = async event => {
  event.preventDefault(); event.submitter.disabled = true;
  try {
    await api('ip-controls', 'PUT', { ip: $('ip-address').value.trim(), blocked: $('ip-blocked').checked,
      per_minute: $('ip-minute').value ? Number($('ip-minute').value) : null,
      per_day: $('ip-day').value ? Number($('ip-day').value) : null, note: $('ip-note').value.trim() });
    await loadIPs(); notice('IP rule saved.');
  } catch (error) { notice(error.message, true); } finally { event.submitter.disabled = false; }
};

function addSMSWindow(value = { days: [4, 5, 6], start: '18:00', end: '22:00' }) {
  const row = document.createElement('div'); row.className = 'sms-window';
  const days = document.createElement('div'); days.className = 'sms-days';
  ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach((name, day) => { const label = document.createElement('label'), input = document.createElement('input'); input.type = 'checkbox'; input.value = day; input.checked = value.days.includes(day); label.append(input, document.createTextNode(name)); days.append(label); });
  row.append(days);
  for (const key of ['start', 'end']) { const label = document.createElement('label'); label.textContent = key === 'start' ? 'Start' : 'Finish'; const input = document.createElement('input'); input.type = 'time'; input.className = key; input.value = value[key]; input.required = true; label.append(input); row.append(label); }
  const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove hours'; remove.onclick = () => { row.remove(); smsDirty = true; }; row.append(remove); row.onchange = () => { smsDirty = true; }; $('sms-windows').append(row);
}
function renderSMSSettings(cfg) { const snapshot = JSON.stringify([cfg.sms_recipients, cfg.sms_schedule]); if (snapshot === smsSnapshot) return; smsSnapshot = snapshot; $('sms-recipients').value = cfg.sms_recipients.join('\n'); $('sms-windows').replaceChildren(); cfg.sms_schedule.forEach(addSMSWindow); }
$('sms-recipients').oninput = () => { smsDirty = true; };
$('sms-add-window').onclick = () => { addSMSWindow(); smsDirty = true; };
$('sms-form').onsubmit = async event => {
  event.preventDefault(); event.submitter.disabled = true;
  const windows = [...$('sms-windows').children].map(row => ({ days: [...row.querySelectorAll('input[type=checkbox]:checked')].map(input => Number(input.value)), start: row.querySelector('.start').value, end: row.querySelector('.end').value }));
  try { await api('settings', 'PATCH', { sms_recipients: $('sms-recipients').value.split(/[\n,]/).map(v => v.trim()).filter(Boolean), sms_schedule: windows }); smsDirty = false; await refresh(); notice('Notification settings saved. Send a test before enabling alerts.'); }
  catch (error) { notice(error.message, true); } finally { event.submitter.disabled = false; }
};
$('sms-test').onclick = async () => {
  if (smsDirty) { notice('Save notification settings before testing.', true); return; }
  if (!await confirmAction('Send test messages?', 'This sends one real SMS to each saved recipient using Twilio. Messaging charges may apply.')) return;
  $('sms-test').disabled = true;
  try { const result = await api('notifications/test', 'POST'); notice(result.detail); await refresh(); } catch (e) { notice(e.message, true); } finally { $('sms-test').disabled = false; }
};
$('sms-enable').onclick = () => toggleSetting('sms_enabled');
$('checklist-run').onclick = async () => {
  $('checklist-run').disabled = true;
  try { const result = await api('diagnostics/checklist'); $('checklist-results').replaceChildren(); for (const check of result.checks) { const item = document.createElement('li'); item.textContent = `${check.ok ? '✓' : 'Needs attention:'} ${check.name} — ${check.detail}`; $('checklist-results').append(item); } } catch (e) { notice(e.message, true); } finally { $('checklist-run').disabled = false; }
};
$('ai-test').onclick = async () => {
  if (!await confirmAction('Run paid AI test?', 'This makes one real AI request to verify connectivity. It does not print.')) return;
  $('ai-test').disabled = true;
  try { await api('diagnostics/ai-test', 'POST'); await refresh(); notice('AI connection verified.'); } catch (e) { notice(e.message, true); } finally { $('ai-test').disabled = false; }
};
