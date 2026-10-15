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
  showView('alertsView');
  await refreshAll();
}

function showView(id) {
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('hidden', v.id !== id));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === id));
}

async function refreshAll() {
  if (!me) return;
  const jobs = [loadAlerts(), loadReports(), loadMissing()];
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
  if (inc.review_status === 'dismissed') badges.push(h('span', { class: 'badge', text: 'dismissed' }));
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

async function loadAlerts() {
  const res = await api('/alert/recommended');
  if (!res.ok) return;
  renderList($('alertsList'), res.data, incidentCard, 'Nothing needs an alert right now.');
  const unreviewed = res.data.filter((i) => !i.review_status).length;
  $('alertsCount').textContent = unreviewed ? String(unreviewed) : '';
}

async function loadReports() {
  const res = await api('/incidents/all?limit=100');
  if (!res.ok) return;
  renderList($('reportsList'), res.data, incidentCard, 'No reports yet.');
}

async function reviewIncident(inc, action, note = '') {
  const res = await api(`/incidents/${inc.id}/review`, { method: 'POST', body: { action, note } });
  if (!res.ok) throw new Error(errText(res.data, 'Could not save'));
  toast(action === 'verify' ? `Report #${inc.id} marked verified.` : `Report #${inc.id} dismissed.`);
  refreshAll();
}

function dismissSheet(inc) {
  const note = h('textarea', { id: 'dismissNote', maxlength: '300', placeholder: 'Optional: why (e.g. duplicate, prank, already handled)' });
  openSheet({
    title: `Dismiss report #${inc.id}?`,
    body: h('div', {}, h('p', { class: 'hint', text: 'It leaves the recommended list. The report itself is kept.' }), note),
    confirmLabel: 'Dismiss',
    onConfirm: () => reviewIncident(inc, 'dismiss', note.value.trim()),
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
setInterval(() => { if (me && !sheetOpen()) refreshAll(); }, REFRESH_MS);

if (token) start(); else logout();
