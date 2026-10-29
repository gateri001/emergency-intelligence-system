'use strict';
// Officer console. Every piece of data is put on the page with textContent
// (via h() below), never as HTML markup, so text from the public (report
// descriptions, names) can never become markup or script. The page also
// runs under a strict Content-Security-Policy set by the server.

const TOKEN_KEY = 'officerToken';  // same keys as the public dashboard, so one sign-in covers both
const USER_KEY = 'officerUser';
const REFRESH_MS = 20000;
const SMS_MAX = 300;

let token = sessionStorage.getItem(TOKEN_KEY);
let me = null;                 // {username, role} from /me
let missingCases = [];         // cached for the approvals map
let sheetCleanup = null;

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function errText(data, fallback) {
  if (data && typeof data.detail === 'string') return data.detail;
  if (data && Array.isArray(data.detail)) return data.detail.map((d) => d.msg).join('; ');
  return fallback || 'Something went wrong';
}

async function api(path, { method = 'GET', body, form } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
    payload = new URLSearchParams(form);
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload });
  } catch (e) {
    return { ok: false, status: 0, data: { detail: 'No connection to the server' } };
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (res.status === 401 && token) { logout('Your session ended. Please sign in again.'); }
  return { ok: res.ok, status: res.status, data };
}

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 5000);
}

function isApprover() { return me && (me.role === 'admin' || me.role === 'owner'); }
function isOwner() { return me && me.role === 'owner'; }

const CRIME = new Set(['robbery', 'burglary', 'theft', 'assault', 'vandalism', 'suspicious_activity']);
const label = (t) => String(t || '').replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
const utc = (s) => (s ? Date.parse(String(s).replace(' ', 'T') + 'Z') : NaN);

// ---------------------------------------------------------------------------
// Sheet: one in-page dialog for every confirmation (no browser pop-ups)
// ---------------------------------------------------------------------------

function openSheet({ title, body, confirmLabel, danger = false, onConfirm, afterOpen, cancelLabel = 'Cancel' }) {
  closeSheet();
  $('sheetTitle').textContent = title;
  $('sheetBody').replaceChildren(body);
  $('sheetError').textContent = '';
  $('sheetCancel').textContent = cancelLabel;
  const confirmBtn = $('sheetConfirm');
  confirmBtn.className = `btn ${danger ? 'danger' : 'primary'}`;
  if (confirmLabel) {
    confirmBtn.textContent = confirmLabel;
    confirmBtn.classList.remove('hidden');
    confirmBtn.disabled = false;
    confirmBtn.onclick = async () => {
      confirmBtn.disabled = true;
      $('sheetError').textContent = '';
      try {
        await onConfirm();
        closeSheet();
      } catch (e) {
        $('sheetError').textContent = e.message;
        confirmBtn.disabled = false;
      }
    };
  } else {
    confirmBtn.classList.add('hidden');
    confirmBtn.onclick = null;
  }
  $('sheet').classList.remove('hidden');
  sheetCleanup = afterOpen ? afterOpen(confirmBtn) : null;
}

function closeSheet() {
  if (typeof sheetCleanup === 'function') sheetCleanup();
  sheetCleanup = null;
  $('sheet').classList.add('hidden');
  $('sheetBody').replaceChildren();
}

function sheetOpen() { return !$('sheet').classList.contains('hidden'); }

// A small map showing who an alert reaches. Returns {setRadius, destroy}.
function reachMap(container, lat, lon, radiusKm) {
  // The map needs a view before any layer is added: a circle can't measure
  // its bounds on a map that has no view yet (found in a real browser).
  const centre = L.latLng(lat, lon);
  const map = L.map(container, { zoomControl: false, attributionControl: true }).setView(centre, 13);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '© OpenStreetMap',
  }).addTo(map);
  L.marker(centre).addTo(map);
  const circle = L.circle(centre, { radius: radiusKm * 1000, color: '#e5484d', weight: 2 }).addTo(map);
  let km = radiusKm;
  // Bounds from the radius itself, so fitting never depends on render state.
  const fit = () => map.fitBounds(centre.toBounds(km * 2000), { padding: [10, 10] });
  setTimeout(() => { map.invalidateSize(); fit(); }, 50);
  return {
    setRadius(newKm) { km = newKm; circle.setRadius(km * 1000); fit(); },
    destroy() { map.remove(); },
  };
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ---------------------------------------------------------------------------
// Sign-in
// ---------------------------------------------------------------------------

async function login(ev) {
  ev.preventDefault();
  $('loginError').textContent = '';
  const username = $('username').value.trim();
  const res = await api('/token', { method: 'POST', form: { username, password: $('password').value } });
  $('password').value = '';
  if (!res.ok) { $('loginError').textContent = errText(res.data, 'Sign-in failed'); return; }
  token = res.data.access_token;
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(USER_KEY, username);
  await start();
}

function logout(message) {
  token = null; me = null;
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
  closeSheet();
  $('who').textContent = '';
  $('logoutBtn').classList.add('hidden');
  $('tabbar').classList.add('hidden');
  document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
  $('loginView').classList.remove('hidden');
  if (message) $('loginError').textContent = message;
}

async function start() {
  const res = await api('/me');
  if (!res.ok) { logout(); return; }
  me = res.data;
  $('who').textContent = `${me.username} · ${me.role}`;
  $('logoutBtn').classList.remove('hidden');
  $('tabbar').classList.remove('hidden');
  document.querySelectorAll('.approver-only').forEach((el) => el.classList.toggle('hidden', !isApprover()));
  document.querySelectorAll('.owner-only').forEach((el) => el.classList.toggle('hidden', !isOwner()));
  showView('incidentsView');
  await refreshAll();
}

function showView(id) {
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('hidden', v.id !== id));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === id));
}

async function refreshAll() {
  if (!me) return;
  const jobs = [loadIncidents(), loadReports(), loadUnits(), loadMissing()];
  if (isApprover()) jobs.push(loadApprovals());
  if (isOwner()) jobs.push(loadLog());
  await Promise.all(jobs);
}

// ---------------------------------------------------------------------------
// Incidents
// ---------------------------------------------------------------------------

function tierBadge(tier) {
  if (tier === 'critical') return h('span', { class: 'badge critical', text: 'critical' });
  if (tier === 'sms_recommended') return h('span', { class: 'badge sms', text: 'SMS recommended' });
  return h('span', { class: 'badge', text: 'in-app' });
}

function incidentCard(inc) {
  const when = inc.timestamp;
  const conf = `${Math.round((inc.confidence_score || 0) * 100)}% confidence`;
  const badges = [tierBadge(inc.alert_tier)];
  if (inc.visibility === 'officers_only') badges.push(h('span', { class: 'badge private', text: 'officers only' }));
  if (inc.review_status === 'verified') badges.push(h('span', { class: 'badge ok', text: 'verified' }));
  if (inc.review_status === 'dismissed') badges.push(h('span', { class: 'badge', text: inc.dismiss_reason ? `dismissed: ${label(inc.dismiss_reason).toLowerCase()}` : 'dismissed' }));
  if (inc.magnitude != null) badges.push(h('span', { class: 'badge', text: `${inc.magnitude} MW` }));

  const actions = h('div', { class: 'actions' });
  if (inc.review_status !== 'verified') {
    actions.append(h('button', { class: 'btn', type: 'button', onclick: () => reviewIncident(inc, 'verify') }, 'Verify'));
  }
  if (inc.review_status !== 'dismissed') {
    actions.append(h('button', { class: 'btn', type: 'button', onclick: () => dismissSheet(inc) }, 'Dismiss'));
  }
  actions.append(h('button', { class: 'btn danger', type: 'button', onclick: () => incidentAlertSheet(inc) }, 'Send alert…'));
  if (isApprover() && CRIME.has(inc.type)) {
    actions.append(h('button', { class: 'btn ghost', type: 'button', onclick: () => revealSheet(inc) }, 'Reporter number…'));
  }
  if (inc.cluster_id != null) {
    actions.append(h('button', { class: 'btn ghost', type: 'button', onclick: () => openIncident(inc.cluster_id) }, `Incident #${inc.cluster_id}`));
  }

  return h('div', { class: 'card' },
    h('div', { class: 'title' }, label(inc.type), ...badges),
    h('div', { class: 'meta' }, `#${inc.id} · ${inc.source} · ${when} · ${conf}`),
    inc.area ? h('div', { class: 'meta' }, inc.area) : null,
    inc.description ? h('div', { class: 'body' }, inc.description) : null,
    actions,
  );
}

function renderList(el, items, render, emptyText) {
  el.replaceChildren(...(items.length ? items.map(render) : [h('div', { class: 'empty', text: emptyText })]));
}

async function loadReports() {
  const res = await api('/incidents/all?limit=100');
  if (!res.ok) return;
  renderList($('reportsList'), res.data, incidentCard, 'No reports yet.');
}

async function reviewIncident(inc, action, note = '', reason = null) {
  const res = await api(`/incidents/${inc.id}/review`, { method: 'POST', body: { action, note, reason } });
  if (!res.ok) throw new Error(errText(res.data, 'Could not save'));
  toast(action === 'verify' ? `Report #${inc.id} marked verified.` : `Report #${inc.id} dismissed.`);
  refreshAll();
}

function reasonSelect() {
  return h('select', { 'aria-label': 'Why dismiss' },
    h('option', { value: '', text: 'Why? Choose one' }),
    ...REASONS.map(([value, text]) => h('option', { value, text })));
}

function dismissSheet(inc) {
  const reason = reasonSelect();
  const note = h('textarea', { id: 'dismissNote', maxlength: '300', placeholder: 'Optional note' });
  openSheet({
    title: `Dismiss report #${inc.id}?`,
    body: h('div', {},
      h('p', { class: 'hint', text: 'It leaves the recommended list. The report itself is kept. Only "prank" counts against the sender; honest mistakes cost nothing.' }),
      h('div', { class: 'field' }, reason), note),
    confirmLabel: 'Dismiss',
    onConfirm: () => {
      if (!reason.value) throw new Error('Choose why first.');
      return reviewIncident(inc, 'dismiss', note.value.trim(), reason.value);
    },
  });
}

function defaultIncidentMessage(inc) {
  const where = inc.area ? `near ${inc.area}` : 'in your area';
  if (inc.type === 'flood') return `FLOOD WARNING ${where}. Move to higher ground now and avoid rivers and drains. If in danger call 999 or 112.`;
  if (inc.type === 'fire') return `FIRE reported ${where}. Keep away and let responders through. If in danger call 999 or 112.`;
  if (CRIME.has(inc.type)) return `SECURITY ALERT ${where}: ${label(inc.type).toLowerCase()} reported. Stay alert and avoid the area if you can.`;
  if (inc.type === 'accident') return `ACCIDENT ${where}. Expect delays and keep the road clear for responders.`;
  return `ALERT ${where}: ${label(inc.type).toLowerCase()} reported. Stay safe and follow official guidance.`;
}

function incidentAlertSheet(inc) {
  const radius = h('input', { type: 'number', min: '0.5', max: '50', step: '0.5', value: '5', inputmode: 'decimal' });
  const message = h('textarea', { maxlength: String(SMS_MAX) });
  message.value = defaultIncidentMessage(inc);
  const counter = h('div', { class: 'meta' });
  const reach = h('div', { class: 'reach', text: 'Counting recipients…' });
  const warning = h('div', { class: 'warning hidden' });
  const mapEl = h('div', { class: 'mini-map' });
  let count = null;

  const body = h('div', {},
    h('div', { class: 'meta' }, `${label(inc.type)} · report #${inc.id}`),
    mapEl,
    h('div', { class: 'field' }, h('label', { text: 'Radius (km)' }), radius),
    h('div', { class: 'field' }, h('label', { text: 'Message (sent exactly as written)' }), message, counter),
    reach, warning,
  );

  openSheet({
    title: 'Send an SMS alert',
    body,
    confirmLabel: 'Send',
    danger: true,
    onConfirm: async () => {
      if (!count) throw new Error('Nobody is in this radius, so there is nothing to send.');
      const res = await api('/alert/broadcast', {
        method: 'POST', body: { incident_id: inc.id, message: message.value.trim(), radius_km: Number(radius.value) },
      });
      if (!res.ok) throw new Error(errText(res.data, 'Broadcast failed'));
      toast(`Sent to ${res.data.recipients_reached} people${res.data.failed_count ? `, ${res.data.failed_count} failed` : ''}.`);
    },
    afterOpen: (confirmBtn) => {
      const rm = reachMap(mapEl, inc.latitude, inc.longitude, Number(radius.value));
      const update = debounce(async () => {
        const km = Number(radius.value);
        counter.textContent = `${message.value.length}/${SMS_MAX}`;
        if (!(km > 0 && km <= 50) || !message.value.trim()) {
          confirmBtn.disabled = true; reach.textContent = 'Set a radius (0.5–50 km) and a message.'; return;
        }
        rm.setRadius(km);
        const res = await api('/alert/broadcast-preview', {
          method: 'POST', body: { incident_id: inc.id, message: message.value.trim(), radius_km: km },
        });
        if (!res.ok) { confirmBtn.disabled = true; reach.textContent = errText(res.data); return; }
        count = res.data.recipient_count;
        reach.textContent = `Reaches ${count} ${count === 1 ? 'person' : 'people'} within ${km} km`;
        confirmBtn.textContent = count ? `Send to ${count}` : 'Send';
        confirmBtn.disabled = !count;
        warning.textContent = res.data.reporter_safety_warning || '';
        warning.classList.toggle('hidden', !res.data.reporter_safety_warning);
      }, 300);
      radius.addEventListener('input', update);
      message.addEventListener('input', update);
      update();
      return () => rm.destroy();
    },
  });
}

function revealSheet(inc) {
  const reason = h('textarea', { minlength: '15', maxlength: '500', placeholder: 'Why the number is needed, e.g. "Investigating officer needs a witness statement"' });
  openSheet({
    title: `Open the reporter's number for #${inc.id}?`,
    body: h('div', {},
      h('div', { class: 'warning', text: 'Only when strictly needed. This person may be in danger if the number reaches the wrong people. Your name, the time and your reason are logged, and the owner reads the log.' }),
      h('div', { class: 'field' }, h('label', { text: 'Reason (at least 15 characters)' }), reason),
    ),
    confirmLabel: 'Open number',
    danger: true,
    onConfirm: async () => {
      const res = await api(`/incidents/${inc.id}/reveal-reporter`, { method: 'POST', body: { reason: reason.value.trim() } });
      if (!res.ok) throw new Error(errText(res.data, 'Could not open the number'));
      setTimeout(() => openSheet({
        title: `Reporter of #${inc.id}`,
        body: h('div', {},
          h('div', { class: 'secret', text: res.data.reporter_phone }),
          h('p', { class: 'hint', text: 'Do not write it down or share it. Close this when done.' })),
        cancelLabel: 'Close',
      }), 0);
    },
  });
}

// ---------------------------------------------------------------------------
// Incidents: reports of one event, grouped by the server (src/clustering.py)
// ---------------------------------------------------------------------------

let incidentState = 'open';
let units = [];

const STATUS_TEXT = {
  new: 'needs checking', verified: 'verified', assigned: 'unit assigned', en_route: 'en route',
  on_scene: 'on scene', resolved: 'resolved', dismissed: 'dismissed', merged: 'merged',
};
const NEXT_LABEL = {
  verified: 'Verify', assigned: 'Mark assigned', en_route: 'Mark en route', on_scene: 'Mark on scene',
  resolved: 'Resolve', new: 'Reopen',
};
const REASONS = [
  ['prank', 'Prank: deliberately false'], ['mistaken', 'Honest mistake'], ['duplicate', 'Duplicate'],
  ['unverifiable', 'Could not verify'], ['not_actionable', 'Not actionable'],
];
const ACTIVE = new Set(['verified', 'assigned', 'en_route', 'on_scene']);

function statusBadge(s) { return h('span', { class: `badge status ${s}`, text: STATUS_TEXT[s] || s }); }

function witnesses(c) {
  const reports = `${c.report_count} ${c.report_count === 1 ? 'report' : 'reports'}`;
  if (c.independent_count >= c.report_count) return reports;
  return `${reports} from ${c.independent_count} ${c.independent_count === 1 ? 'sender' : 'senders'}`;
}

function clusterCard(c) {
  const badges = [statusBadge(c.status), tierBadge(c.alert_tier)];
  if (c.escalate) badges.push(h('span', { class: 'badge critical', text: 'escalated' }));
  if (c.officers_only) badges.push(h('span', { class: 'badge private', text: 'officers only' }));
  if (c.needs_location) badges.push(h('span', { class: 'badge private', text: 'needs location' }));
  if (c.needs_translation) badges.push(h('span', { class: 'badge private', text: 'needs translation' }));
  if (utc(c.last_activity_at) < Date.now() - 24 * 3600 * 1000) badges.push(h('span', { class: 'badge', text: 'quiet 24h+' }));
  const people = [c.commander ? `Commander: ${c.commander}` : '',
    c.units_count ? `${c.units_count} ${c.units_count === 1 ? 'unit' : 'units'} attached` : ''].filter(Boolean);
  const open = () => openIncident(c.id);
  return h('div', {
    class: `card clickable${c.escalate ? ' escalated' : ''}`, role: 'button', tabindex: '0',
    onclick: open, onkeydown: (e) => { if (e.key === 'Enter') open(); },
  },
  h('div', { class: 'title' }, label(c.type), ...badges),
  h('div', { class: 'meta' }, `#${c.id} · ${witnesses(c)} · ${Math.round(c.confidence_score * 100)}% confidence · last ${c.last_report_at}`),
  c.area ? h('div', { class: 'meta' }, c.area) : null,
  people.length ? h('div', { class: 'meta' }, people.join(' · ')) : null,
  c.dismiss_reason ? h('div', { class: 'meta' }, `Dismissed: ${label(c.dismiss_reason).toLowerCase()}`) : null);
}

async function loadIncidents() {
  const res = await api(`/clusters?state=${incidentState}`);
  if (!res.ok) return;
  renderList($('incidentsList'), res.data, clusterCard,
    incidentState === 'open' ? 'No open incidents.' : 'No closed incidents yet.');
  if (incidentState === 'open') {
    // Only live ones: old satellite detections would keep the badge full forever.
    const dayAgo = Date.now() - 24 * 3600 * 1000;
    const unchecked = res.data.filter((c) => c.status === 'new' && utc(c.last_activity_at) >= dayAgo).length;
    $('incidentsCount').textContent = unchecked ? String(unchecked) : '';
  }
}

async function openIncident(id) {
  const res = await api(`/clusters/${id}`);
  if (!res.ok) { toast(errText(res.data, 'Could not open the incident')); return; }
  const d = res.data;
  const c = d.incident;

  // Every button here saves, then redraws this sheet from the server.
  async function act(path, body, done) {
    $('sheetError').textContent = '';
    const r = await api(path, { method: 'POST', body });
    if (!r.ok) { $('sheetError').textContent = errText(r.data, 'Could not save'); return; }
    if (done) toast(done);
    refreshAll();
    openIncident(id);
  }
  const button = (text, onclick, cls = 'btn') => h('button', { class: cls, type: 'button', onclick }, text);

  const statusRow = h('div', { class: 'row' });
  for (const s of d.allowed_next) {
    if (s === 'dismissed') continue;
    statusRow.append(button(NEXT_LABEL[s] || label(s),
      () => act(`/clusters/${id}/status`, { status: s }, `Incident #${id}: ${STATUS_TEXT[s]}.`),
      s === 'verified' ? 'btn primary' : 'btn'));
  }
  let dismissRow = null;
  if (d.allowed_next.includes('dismissed')) {
    const reason = reasonSelect();
    dismissRow = h('div', { class: 'row' }, reason, button('Dismiss', () => {
      if (!reason.value) { $('sheetError').textContent = 'Choose why it is being dismissed.'; return; }
      act(`/clusters/${id}/status`, { status: 'dismissed', reason: reason.value }, `Incident #${id} dismissed.`);
    }));
  }

  const unitRows = d.units.map((u) => h('div', { class: 'row' },
    h('span', { text: `${u.name}: ${label(u.status)}` }),
    u.status === 'assigned' ? button('En route', () => act(`/units/${u.id}/status`, { status: 'en_route' })) : null,
    u.status !== 'on_scene' ? button('On scene', () => act(`/units/${u.id}/status`, { status: 'on_scene' })) : null,
    button('Free', () => act(`/units/${u.id}/status`, { status: 'available' }), 'btn ghost')));
  let sendRow = null;
  if (ACTIVE.has(c.status)) {
    const free = units.filter((u) => u.status === 'available');
    if (free.length) {
      const pick = h('select', { 'aria-label': 'Unit to send' },
        ...free.map((u) => h('option', { value: String(u.id), text: u.kind ? `${u.name} (${u.kind})` : u.name })));
      sendRow = h('div', { class: 'row' }, pick, button('Send unit',
        () => act(`/units/${pick.value}/status`, { status: 'assigned', cluster_id: id }, 'Unit assigned.'), 'btn primary'));
    } else {
      sendRow = h('p', { class: 'hint', text: units.length ? 'No unit is free right now.' : 'No units yet: add them on the Units tab.' });
    }
  } else if (c.status === 'new') {
    sendRow = h('p', { class: 'hint', text: 'Verify the incident before sending anyone.' });
  }

  const mine = c.commander === me.username;
  const flag = (key, text) => h('button', {
    class: `btn small${c[key] ? ' on' : ''}`, type: 'button', 'aria-pressed': c[key] ? 'true' : 'false',
    onclick: () => act(`/clusters/${id}/flags`, { [key]: !c[key] }),
  }, text);

  const live = d.reports.filter((r) => r.review_status !== 'dismissed')
    .sort((a, b) => b.confidence_score - a.confidence_score);
  const alertBtn = live.length && ACTIVE.has(c.status)
    ? button('Send SMS alert…', () => incidentAlertSheet(live[0]), 'btn danger') : null;

  const mergeInput = h('input', { type: 'number', min: '1', inputmode: 'numeric', placeholder: 'Other incident #', 'aria-label': 'Other incident number' });
  const mergeRow = c.status === 'merged' ? null : h('div', { class: 'row' }, mergeInput, button('Merge into this', () => {
    const other = Number(mergeInput.value);
    if (!other) { $('sheetError').textContent = 'Type the other incident number first.'; return; }
    act(`/clusters/${id}/merge`, { other_id: other }, `Incident #${other} merged into #${id}.`);
  }));

  const reports = d.reports.map((r) => h('div', { class: 'mini-report' },
    h('div', {}, h('strong', { text: `#${r.id}` }), ` ${r.source} · ${r.timestamp} · ${Math.round(r.confidence_score * 100)}%`,
      r.review_status ? h('span', {
        class: r.review_status === 'verified' ? 'badge ok' : 'badge',
        text: r.dismiss_reason ? `${r.review_status}: ${label(r.dismiss_reason).toLowerCase()}` : r.review_status,
      }) : null),
    r.area ? h('div', { class: 'meta' }, r.area) : null,
    r.description ? h('div', { class: 'body' }, r.description) : null,
    d.reports.length > 1 ? h('div', { class: 'row' }, button('Different event: split off',
      () => act(`/incidents/${r.id}/detach`, undefined, `Report #${r.id} is now its own incident.`), 'btn small ghost')) : null));

  const timeline = h('ul', { class: 'timeline' }, ...d.timeline.map((e) => h('li', { class: e.kind },
    h('div', { class: 'when', text: `${e.at || ''} UTC${e.actor ? ` · ${e.actor}` : ''}` }),
    h('div', { text: e.text }))));

  const mapEl = h('div', { class: 'mini-map' });
  const body = h('div', {},
    h('div', {}, statusBadge(c.status), tierBadge(c.alert_tier), c.officers_only ? h('span', { class: 'badge private', text: 'officers only' }) : null),
    h('div', { class: 'meta' }, `${witnesses(c)} · ${Math.round(c.confidence_score * 100)}% confidence · ${c.first_report_at} to ${c.last_report_at}`),
    c.area ? h('div', { class: 'meta' }, c.area) : null,
    c.merged_into ? h('p', { class: 'warning', text: `Merged into incident #${c.merged_into}.` }) : null,
    mapEl,
    statusRow.childElementCount ? statusRow : null,
    dismissRow,
    h('div', { class: 'section-title', text: 'Units' }), ...unitRows, sendRow,
    h('div', { class: 'section-title', text: 'Command' }),
    h('div', { class: 'row' }, button(mine ? 'Release command' : (c.commander ? `Take over from ${c.commander}` : 'Take command'),
      () => act(`/clusters/${id}/commander`, { commander: mine ? null : me.username }, mine ? 'Command released.' : 'You are in command.')), alertBtn),
    h('div', { class: 'row' }, flag('escalate', 'Escalate'), flag('needs_location', 'Needs location'), flag('needs_translation', 'Needs translation')),
    h('div', { class: 'section-title', text: `Reports (${d.reports.length})` }), ...reports,
    mergeRow,
    h('div', { class: 'section-title', text: 'Timeline' }), timeline);

  openSheet({
    title: `${label(c.type)} · incident #${c.id}`,
    body,
    cancelLabel: 'Close',
    afterOpen: () => {
      const rm = reachMap(mapEl, c.latitude, c.longitude, 0.5);
      return () => rm.destroy();
    },
  });
}

// ---------------------------------------------------------------------------
// Units
// ---------------------------------------------------------------------------

async function loadUnits() {
  const res = await api('/units');
  if (!res.ok) return;
  units = res.data;
  renderList($('unitsList'), units, unitCard, 'No units yet.');
}

async function moveUnit(u, status) {
  const res = await api(`/units/${u.id}/status`, { method: 'POST', body: { status } });
  if (!res.ok) { toast(errText(res.data, 'Could not update the unit')); return; }
  toast(`${u.name}: ${label(status).toLowerCase()}.`);
  refreshAll();
}

function unitCard(u) {
  const cls = { available: 'badge ok', unavailable: 'badge' }[u.status] || 'badge sms';
  const actions = h('div', { class: 'actions' });
  if (u.cluster_id != null) {
    actions.append(h('button', { class: 'btn', type: 'button', onclick: () => openIncident(u.cluster_id) }, `Incident #${u.cluster_id}`));
  }
  if (u.status === 'assigned') actions.append(h('button', { class: 'btn', type: 'button', onclick: () => moveUnit(u, 'en_route') }, 'En route'));
  if (u.status === 'assigned' || u.status === 'en_route') {
    actions.append(h('button', { class: 'btn', type: 'button', onclick: () => moveUnit(u, 'on_scene') }, 'On scene'));
  }
  if (u.status !== 'available') actions.append(h('button', { class: 'btn ghost', type: 'button', onclick: () => moveUnit(u, 'available') }, 'Free'));
  if (u.status === 'available') actions.append(h('button', { class: 'btn ghost', type: 'button', onclick: () => moveUnit(u, 'unavailable') }, 'Off duty'));
  return h('div', { class: 'card' },
    h('div', { class: 'title' }, u.name, h('span', { class: cls, text: label(u.status).toLowerCase() })),
    h('div', { class: 'meta' }, [u.kind, u.updated_by ? `updated by ${u.updated_by}` : '', `${u.updated_at} UTC`].filter(Boolean).join(' · ')),
    actions);
}

async function addUnit(ev) {
  ev.preventDefault();
  const res = await api('/units', { method: 'POST', body: { name: $('unitName').value.trim(), kind: $('unitKind').value.trim() } });
  if (!res.ok) { toast(errText(res.data, 'Could not add the unit')); return; }
  $('unitName').value = '';
  $('unitKind').value = '';
  toast(`${res.data.name} added.`);
  loadUnits();
}


// ---------------------------------------------------------------------------
// Missing children
// ---------------------------------------------------------------------------

const MC_STATUS = {
  reported: ['pending verification', 'badge sms'],
  verified: ['active search', 'badge critical'],
  found_safe: ['found safe', 'badge ok'],
  found_deceased: ['found deceased', 'badge'],
  closed_false_report: ['false report', 'badge'],
};
const RESOLVED = new Set(['found_safe', 'found_deceased', 'closed_false_report']);

function missingCard(c) {
  const [statusText, statusClass] = MC_STATUS[c.status] || [c.status, 'badge'];
  const expired = c.status === 'verified' && !(utc(c.expires_at) > Date.now());
  const actions = h('div', { class: 'actions' });

  if (c.status === 'reported') {
    actions.append(h('button', { class: 'btn primary', type: 'button', onclick: () => verifySheet(c) }, 'Verify with OB number…'));
  }
  if (c.status === 'verified') {
    if (!expired) {
      actions.append(h('button', { class: 'btn danger', type: 'button', onclick: () => missingAlertSheet(c) },
        isOwner() ? 'Send alert…' : 'Propose alert…'));
    }
    actions.append(h('button', { class: 'btn', type: 'button', onclick: () => renewSheet(c) }, expired ? 'Renew (expired)…' : 'Renew 72h…'));
  }
  if (!RESOLVED.has(c.status)) {
    actions.append(
      h('button', { class: 'btn', type: 'button', onclick: () => resolveSheet(c, 'found_safe') }, 'Found safe'),
      h('button', { class: 'btn', type: 'button', onclick: () => resolveSheet(c, 'found_deceased') }, 'Found deceased'),
      h('button', { class: 'btn ghost', type: 'button', onclick: () => resolveSheet(c, 'closed_false_report') }, 'False report'),
    );
  }
  if (RESOLVED.has(c.status) && !c.followup_sent_at) {
    actions.append(h('button', { class: 'btn', type: 'button', onclick: () => followupSheet(c) }, 'Send "alert closed" SMS…'));
  }

  const details = [c.physical_description, c.clothing_description ? `Wearing: ${c.clothing_description}` : ''].filter(Boolean).join('\n');
  return h('div', { class: 'card' },
    h('div', { class: 'title' }, `${c.child_name}${c.age != null ? `, ${c.age}` : ''}`,
      h('span', { class: statusClass, text: statusText }),
      expired ? h('span', { class: 'badge private', text: 'expired' }) : null),
    h('div', { class: 'meta' }, `#${c.id} · last seen ${c.last_seen_area || 'at the map point'} · ${c.last_seen_time}`),
    h('div', { class: 'body' }, details),
    h('div', { class: 'meta' }, `Reporter: ${c.reporter_phone} (${c.reporter_relationship || 'relationship not given'})`),
    c.ob_number ? h('div', { class: 'meta' }, `OB number: ${c.ob_number}${c.status === 'verified' ? ` · public until ${c.expires_at} UTC` : ''}`) : null,
    actions,
  );
}

async function loadMissing() {
  const res = await api('/missing-child/cases/all');
  if (!res.ok) return;
  missingCases = res.data;
  renderList($('missingList'), res.data, missingCard, 'No cases on file.');
  const pending = res.data.filter((c) => c.status === 'reported').length;
  $('missingCount').textContent = pending ? String(pending) : '';
}

function verifySheet(c) {
  const ob = h('input', { placeholder: 'e.g. OB 12/15/10/2026', maxlength: '50' });
  openSheet({
    title: `Verify case #${c.id}`,
    body: h('div', {},
      h('p', { class: 'hint', text: 'Call the police station first and confirm this OB number belongs to this child. Verifying makes the case public for 72 hours.' }),
      h('div', { class: 'field' }, h('label', { text: 'Police OB number' }), ob)),
    confirmLabel: 'Verify and make public',
    onConfirm: async () => {
      const res = await api(`/missing-child/cases/${c.id}/verify`, { method: 'POST', body: { ob_number: ob.value } });
      if (!res.ok) throw new Error(errText(res.data, 'Could not verify'));
      toast(`Case #${c.id} verified.`);
      refreshAll();
    },
  });
}

function renewSheet(c) {
  const note = h('textarea', { maxlength: '300', placeholder: 'What the police said, e.g. "Called Kayole station, still missing"' });
  openSheet({
    title: `Renew case #${c.id} for 72 hours`,
    body: h('div', {},
      h('p', { class: 'hint', text: 'Re-confirm with the police that the child is still missing. A stale listing for a child who is already home causes its own harm.' }),
      h('div', { class: 'field' }, h('label', { text: 'Re-confirmation note' }), note)),
    confirmLabel: 'Renew',
    onConfirm: async () => {
      const res = await api(`/missing-child/cases/${c.id}/renew`, { method: 'POST', body: { note: note.value.trim() } });
      if (!res.ok) throw new Error(errText(res.data, 'Could not renew'));
      toast(`Case #${c.id} renewed.`);
      refreshAll();
    },
  });
}

function resolveSheet(c, status) {
  const text = {
    found_safe: 'Mark as found safe? Verified cases then show "found safe" publicly.',
    found_deceased: 'Mark as found deceased? This is never shown publicly; tell the family through a person, not the app.',
    closed_false_report: 'Close as a false report? It is removed from everything public.',
  }[status];
  openSheet({
    title: `Case #${c.id}: ${MC_STATUS[status][0]}`,
    body: h('p', { text: `${text} Pending alert proposals for this case are cancelled.` }),
    confirmLabel: 'Confirm',
    danger: status !== 'found_safe',
    onConfirm: async () => {
      const res = await api(`/missing-child/cases/${c.id}/status`, { method: 'POST', body: { status } });
      if (!res.ok) throw new Error(errText(res.data, 'Could not update'));
      toast(`Case #${c.id} updated.`);
      refreshAll();
    },
  });
}

function followupSheet(c) {
  openSheet({
    title: 'Close the alert for everyone who got it',
    body: h('p', { text: 'Sends a neutral "this alert is closed, please disregard" message to exactly the people the alert reached. Once only.' }),
    confirmLabel: 'Send',
    onConfirm: async () => {
      const res = await api(`/missing-child/cases/${c.id}/followup`, { method: 'POST' });
      if (!res.ok) throw new Error(errText(res.data, 'Could not send'));
      toast(`Follow-up sent to ${res.data.recipients_reached} people.`);
      refreshAll();
    },
  });
}

function missingAlertSheet(c) {
  const radius = h('input', { type: 'number', min: '1', max: '100', step: '1', value: '10', inputmode: 'decimal' });
  const preview = h('div', { class: 'preview', text: 'Preparing the message…' });
  const reach = h('div', { class: 'reach' });
  const mapEl = h('div', { class: 'mini-map' });
  const alone = isOwner();
  let ready = null;

  openSheet({
    title: alone ? `Send missing-child alert for case #${c.id}` : `Propose missing-child alert for case #${c.id}`,
    body: h('div', {},
      h('p', { class: 'hint', text: alone
        ? 'You can send alone. The message uses the first name only and never the reporter\'s number.'
        : 'Two-person rule: this creates a proposal. A different admin must approve it before anything is sent.' }),
      mapEl,
      h('div', { class: 'field' }, h('label', { text: 'Radius (km) around where the child was last seen' }), radius),
      preview, reach),
    confirmLabel: alone ? 'Send now' : 'Propose',
    danger: alone,
    onConfirm: async () => {
      if (!ready) throw new Error('Wait for the preview first.');
      const path = alone ? `/missing-child/cases/${c.id}/broadcast` : `/missing-child/cases/${c.id}/broadcast-proposals`;
      const res = await api(path, { method: 'POST', body: { radius_km: ready.radius_km, message: ready.message } });
      if (!res.ok) throw new Error(errText(res.data, 'Failed'));
      toast(alone ? `Sent to ${res.data.recipients_reached} people.` : `Proposal #${res.data.id} created; waiting for approval.`);
      refreshAll();
    },
    afterOpen: (confirmBtn) => {
      const rm = reachMap(mapEl, c.last_seen_latitude, c.last_seen_longitude, Number(radius.value));
      const update = debounce(async () => {
        const km = Number(radius.value);
        ready = null; confirmBtn.disabled = true;
        if (!(km > 0 && km <= 100)) { reach.textContent = 'Set a radius between 1 and 100 km.'; return; }
        rm.setRadius(km);
        const res = await api(`/missing-child/cases/${c.id}/broadcast-preview?radius_km=${km}`);
        if (!res.ok) { reach.textContent = errText(res.data); return; }
        ready = res.data;
        preview.textContent = res.data.message;
        reach.textContent = `Reaches ${res.data.recipient_count} ${res.data.recipient_count === 1 ? 'person' : 'people'} now`;
        confirmBtn.disabled = false;
      }, 300);
      radius.addEventListener('input', update);
      update();
      return () => rm.destroy();
    },
  });
}

// ---------------------------------------------------------------------------
// Approvals (admin / owner)
// ---------------------------------------------------------------------------

function proposalCard(p) {
  const mine = me && p.proposed_by === me.username;
  const actions = h('div', { class: 'actions' });
  if (mine) {
    actions.append(h('div', { class: 'meta', text: 'You proposed this. A different person must decide on it.' }));
  } else {
    actions.append(
      h('button', { class: 'btn danger', type: 'button', onclick: () => approveSheet(p) }, 'Approve and send…'),
      h('button', { class: 'btn', type: 'button', onclick: () => rejectSheet(p) }, 'Reject'),
    );
  }
  return h('div', { class: 'card' },
    h('div', { class: 'title' }, `Case #${p.case_id}`, h('span', { class: 'badge sms', text: 'needs approval' })),
    h('div', { class: 'meta' }, `Proposal #${p.id} by ${p.proposed_by} · ${p.radius_km} km · reaches ${p.recipient_count_now} now`),
    h('div', { class: 'preview', text: p.message }),
    actions,
  );
}

async function loadApprovals() {
  const res = await api('/missing-child/broadcast-proposals?status=pending');
  if (!res.ok) return;
  renderList($('approvalsList'), res.data, proposalCard, 'Nothing waiting.');
  const forMe = res.data.filter((p) => p.proposed_by !== me.username).length;
  $('approvalsCount').textContent = forMe ? String(forMe) : '';
}

function approveSheet(p) {
  const c = missingCases.find((x) => x.id === p.case_id);
  const mapEl = c ? h('div', { class: 'mini-map' }) : null;
  openSheet({
    title: `Approve and send proposal #${p.id}?`,
    body: h('div', {},
      h('p', { class: 'hint', text: `Proposed by ${p.proposed_by}. Check the case has a real OB number and the message is right. This sends immediately.` }),
      c ? h('div', { class: 'meta' }, `Child: ${c.child_name} · OB number: ${c.ob_number || 'missing'}`) : null,
      mapEl,
      h('div', { class: 'preview', text: p.message }),
      h('div', { class: 'reach', text: `Reaches about ${p.recipient_count_now} people within ${p.radius_km} km` })),
    confirmLabel: `Approve and send to ${p.recipient_count_now}`,
    danger: true,
    onConfirm: async () => {
      const res = await api(`/missing-child/broadcast-proposals/${p.id}/approve`, { method: 'POST' });
      if (!res.ok) throw new Error(errText(res.data, 'Could not approve'));
      toast(`Sent to ${res.data.recipients_reached} people.`);
      refreshAll();
    },
    afterOpen: () => {
      if (!c) return null;
      const rm = reachMap(mapEl, c.last_seen_latitude, c.last_seen_longitude, p.radius_km);
      return () => rm.destroy();
    },
  });
}

function rejectSheet(p) {
  openSheet({
    title: `Reject proposal #${p.id}?`,
    body: h('p', { text: 'Nothing is sent. The officer can propose again with a corrected radius or message.' }),
    confirmLabel: 'Reject',
    onConfirm: async () => {
      const res = await api(`/missing-child/broadcast-proposals/${p.id}/reject`, { method: 'POST' });
      if (!res.ok) throw new Error(errText(res.data, 'Could not reject'));
      toast(`Proposal #${p.id} rejected.`);
      refreshAll();
    },
  });
}

// ---------------------------------------------------------------------------
// Audit log (owner)
// ---------------------------------------------------------------------------

async function loadLog() {
  const res = await api('/audit-log?limit=200');
  if (!res.ok) return;
  renderList($('logList'), res.data, (e) => h('div', { class: 'card log-row' },
    h('div', {}, h('strong', { text: e.actor }), ` · ${label(e.action)} · ${e.target_type} #${e.target_id ?? '-'}`),
    h('div', { class: 'meta' }, `${e.created_at} UTC${e.detail ? ` · ${e.detail}` : ''}`),
  ), 'Nothing logged yet.');
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

$('loginForm').addEventListener('submit', login);
$('logoutBtn').addEventListener('click', () => logout());
$('sheetCancel').addEventListener('click', closeSheet);
$('sheet').addEventListener('click', (e) => { if (e.target === $('sheet')) closeSheet(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && sheetOpen()) closeSheet(); });
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => showView(t.dataset.view)));
document.querySelectorAll('.filter').forEach((b) => b.addEventListener('click', () => {
  incidentState = b.dataset.state;
  document.querySelectorAll('.filter').forEach((x) => x.classList.toggle('active', x === b));
  loadIncidents();
}));
$('unitForm').addEventListener('submit', addUnit);
setInterval(() => { if (me && !sheetOpen()) refreshAll(); }, REFRESH_MS);

if (token) start(); else logout();
