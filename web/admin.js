'use strict';
const $ = id => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value; };
let currentTab = 'overview', overview = null, selectedJob = null, codeSnapshotAt = 0, serverSnapshot = 0, previewURL = null;
let loading = false, messageTimer = null;
function notice(message, bad = false) {
  text('admin-status', message); $('admin-status').hidden = !message;
  $('admin-status').classList.toggle('error', bad);
  if (messageTimer) clearTimeout(messageTimer);
  if (!bad) messageTimer = setTimeout(() => { $('admin-status').hidden = true; }, 6000);
}
async function api(path, method = 'GET', body = {}, raw = false) {
  const response = await fetch('/admin/api/' + path, { credentials: 'same-origin', method,
    ...(method !== 'GET' ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) });
  if (!response.ok) {
    let detail = 'Admin session expired or request failed. Reload to sign in again.';
    try { const data = await response.json(); detail = data.detail || detail; if (data.fields) detail += ' ' + Object.entries(data.fields).map(([key, value]) => key + ': ' + value).join('; '); } catch (_) {}
    throw new Error(detail);
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
  text('model-status', data.model_ready ? 'API key configured' : 'API key missing');
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
  const jobs = await api('jobs' + ($('filter').value ? '?state=' + encodeURIComponent($('filter').value) : ''));
  $('jobs').replaceChildren(); $('empty-jobs').hidden = jobs.length !== 0;
  for (const job of jobs) {
    const row = document.createElement('tr');
    for (const value of [time(job.created), job.name || (job.parent === 'diagnostic' ? 'Test receipt' : 'Christmas visitor')]) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    const state = document.createElement('td'), pill = document.createElement('span'); pill.className = 'pill ' + job.state; pill.textContent = job.state; state.append(pill); row.append(state);
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
  text('job-meta', job.state + ' · ' + job.id.slice(0, 8) + ' · created ' + time(job.created) + ' · updated ' + time(job.updated));
  text('job-message', job.message); text('job-reply', job.reply || 'No reply yet.');
  text('detail-error', job.error || ''); $('detail-error').hidden = !job.error;
  text('job-parent', job.parent ? 'Linked job: ' + job.parent : '');
  $('job-preview').hidden = !job.reply;
  if (job.reply) { $('job-preview').src = '/admin/api/jobs/' + job.id + '/preview'; $('job-preview').onerror = () => { $('job-preview').hidden = true; }; }
  $('job-actions').replaceChildren(); $('job-action-error').hidden = true;
  const buttons = [];
  if (['waiting', 'generating', 'ready'].includes(job.state)) buttons.push(['cancel', 'Cancel letter']);
  if (job.state === 'failed') buttons.push(['retry', job.reply ? 'Retry saved letter' : 'Retry generation']);
  if (job.state === 'uncertain') buttons.push(['review', 'Mark transmission reviewed']);
  if (['sent', 'failed', 'cancelled'].includes(job.state) && job.reply) buttons.push(['reprint', 'Reprint saved receipt']);
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
    review: 'Confirm you have checked the physical printer for a partial or complete receipt. This does not print anything or resume the queue.',
    reprint: 'Print another copy of this exact saved receipt. Check that a duplicate is wanted before confirming.'
  };
  if (!await confirmAction(action === 'reprint' ? 'Reprint this receipt?' : 'Confirm ' + action, descriptions[action], action === 'reprint' ? '/admin/api/jobs/' + job.id + '/preview' : null)) return;
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
  ['queue_limit', 'Maximum outstanding jobs', 'number', 1, 500], ['failed_codes_per_ip', 'Failed codes per IP / five minutes', 'number', 1, 100],
  ['retention_hours', 'Letter retention (hours)', 'number', 1, 168], ['cut', 'Cut paper after each receipt', 'checkbox']
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
$('filter').onchange = () => loadJobs().catch(error => notice(error.message, true));
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
    renderOverview(await api('overview'));
    if (currentTab === 'queue') await loadJobs();
    await loadHistory();
    if ($('job-dialog').open && selectedJob && !$('confirm-dialog').open) {
      const updated = await api('jobs/' + selectedJob.id);
      if (updated.updated !== selectedJob.updated || updated.state !== selectedJob.state) { selectedJob = updated; renderJob(updated); }
    }
  } catch (error) { notice(error.message, true); }
  finally { loading = false; }
}
refresh(); setInterval(refresh, 5000); setInterval(updateCountdowns, 1000);
