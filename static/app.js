'use strict';
// Citizen app. Rules this file follows on purpose:
// - Text from the server is shown with textContent only (via h()), never as HTML.
// - Crime and missing-child reports are NEVER stored on the phone: a stored
//   report is evidence someone could find. If they fail to send, we say so.
// - Fire, flood and medical reports that fail for lack of signal wait in an
//   on-phone outbox and send themselves when the connection returns.
// - Wording never promises that anyone will arrive: "Report received", nothing more.
// - The phone's location is held in memory only, never saved.

const NAIROBI = { lat: -1.2864, lon: 36.8172 };
const OUTBOX_KEY = 'eis_outbox';
const CRIME = new Set(['robbery', 'burglary', 'theft', 'assault', 'vandalism', 'suspicious_activity']);
// Only these may wait in the outbox. An allowlist, so a new type is never queued by accident.
const QUEUEABLE = new Set(['fire', 'flood', 'medical_emergency', 'accident']);
const TITLES = { fire: 'Report a fire', flood: 'Report a flood', medical_emergency: 'Medical emergency' };
const CATEGORY_NAMES = { hazard: 'Flood & fire', crime: 'Crime', medical: 'Medical' };

let config = { demo_mode: true, signup_enabled: false, emergency_numbers: '999 or 112' };
let here = null;          // {lat, lon, accuracy}: memory only
let nearbyIncidents = [];
const screens = ['home'];

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Helpers
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
  if (data && Array.isArray(data.detail)) return data.detail.map((d) => d.msg.replace(/^Value error, /, '')).join('; ');
  return fallback || 'Something went wrong';
}

// network: true when the request never reached the server (no signal).
async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); } catch (e) { return { ok: false, network: true, status: 0, data: null }; }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty */ }
  return { ok: res.ok, network: false, status: res.status, data };
}

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 5000);
}

const pad = (n) => String(n).padStart(2, '0');
function nowTimestamp() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
const label = (t) => String(t || '').replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
const coords = (p) => `${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`;

function getGPS() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) { reject(new Error('This phone cannot share its location.')); return; }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: Math.round(pos.coords.accuracy) }),
      (err) => reject(new Error(err.code === 1
        ? 'Location permission was refused. You can choose the place on a map instead.'
        : 'Could not find your location. You can choose the place on a map instead.')),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    );
  });
}

function emergencyNumbers() {
  return (config.emergency_numbers.match(/\d{3,}/g) || ['999', '112']);
}

// ---------------------------------------------------------------------------
// Screens
// ---------------------------------------------------------------------------

function showScreen(id) {
  if (screens[screens.length - 1] !== id) screens.push(id);
  render();
}

function back() {
  if (screens.length > 1) screens.pop();
  render();
}

function render() {
  const current = screens[screens.length - 1];
  document.querySelectorAll('.screen').forEach((s) => s.classList.toggle('hidden', s.id !== current));
  $('backBtn').classList.toggle('hidden', current === 'home');
  window.scrollTo(0, 0);
}

// Leaves at once for a harmless page. replace() means Back does not return here.
function quickExit() {
  document.querySelectorAll('input, textarea').forEach((el) => { if (el.type === 'checkbox') el.checked = false; else el.value = ''; });
  try { sessionStorage.clear(); } catch (e) { /* ignore */ }
  window.location.replace('https://www.google.com/search?q=weather+today');
}

// ---------------------------------------------------------------------------
// Outbox (fire / flood / medical only)
// ---------------------------------------------------------------------------

function shouldQueue(type) {
  return QUEUEABLE.has(type);
}

function readOutbox() {
  try { return JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]'); } catch (e) { return []; }
}

function writeOutbox(items) {
  try {
    if (items.length) localStorage.setItem(OUTBOX_KEY, JSON.stringify(items));
    else localStorage.removeItem(OUTBOX_KEY);
  } catch (e) { /* storage blocked: nothing we can do */ }
  updateOutboxBanner();
}

function updateOutboxBanner() {
  const n = readOutbox().length;
  $('outboxBanner').textContent = n ? `${n} report${n > 1 ? 's' : ''} waiting for signal. ${n > 1 ? 'They' : 'It'} will send automatically.` : '';
  $('outboxBanner').classList.toggle('hidden', !n);
}

async function flushOutbox() {
  const items = readOutbox();
  if (!items.length) return;
  const left = [];
  let sent = 0;
  for (const item of items) {
    const res = await api('/report/citizen', { method: 'POST', body: item.body });
    if (res.ok) { sent += 1; continue; }
    // No signal or a busy server: keep it. A rejected report (4xx) would
    // fail forever, so drop it rather than retry endlessly.
    if (res.network || res.status === 429 || res.status >= 500) left.push(item);
  }
  writeOutbox(left);
  if (sent) toast(`${sent} saved report${sent > 1 ? 's were' : ' was'} sent.`);
}

// Sends a citizen report; queues fire/flood/medical if there is no signal.
async function sendCitizenReport(body) {
  const res = await api('/report/citizen', { method: 'POST', body });
  if (res.network && shouldQueue(body.type)) {
    writeOutbox([...readOutbox(), { body, savedAt: Date.now() }]);
    return { ok: true, queued: true };
  }
  return res;
}

// ---------------------------------------------------------------------------
// Home: "Am I safe here?"
// ---------------------------------------------------------------------------

async function checkSafety() {
  $('checkError').textContent = '';
  $('checkBtn').disabled = true;
  $('checkBtn').textContent = 'Finding your location…';
  try {
    here = await getGPS();
  } catch (e) {
    $('checkError').textContent = e.message;
    $('checkBtn').disabled = false;
    $('checkBtn').textContent = 'Check my location';
    return;
  }
  $('checkBtn').textContent = 'Checking…';
  const res = await api(`/nearby?latitude=${here.lat}&longitude=${here.lon}`);
  $('checkBtn').disabled = false;
  $('checkBtn').textContent = 'Check again';
  if (!res.ok) { $('checkError').textContent = res.network ? 'No connection. Try again when you have signal.' : errText(res.data); return; }

  $('riskChips').replaceChildren(...Object.entries(res.data.risk).map(([cat, r]) =>
    h('span', { class: `chip ${r.severity}` }, `${CATEGORY_NAMES[cat] || cat}: ${r.severity}`)));
  nearbyIncidents = res.data.incidents;
  $('nearbyList').replaceChildren(...(nearbyIncidents.length
    ? nearbyIncidents.map((i) => h('div', { class: 'item' },
      h('div', { class: 'title' }, label(i.type)),
      h('div', { class: 'meta' }, `${i.distance_km} km away · ${i.timestamp}`)))
    : [h('p', { class: 'empty', text: 'No public reports near you in the last 7 days.' })]));
  $('safeResult').classList.remove('hidden');
}

async function loadChildren() {
  const res = await api('/missing-child/cases');
  if (!res.ok) return;
  const active = res.data.filter((c) => c.status === 'verified');
  $('childrenCard').classList.toggle('hidden', !active.length);
  $('childrenList').replaceChildren(...active.map((c) => h('div', { class: 'item' },
    h('div', { class: 'title' }, `${c.child_name}${c.age != null ? `, ${c.age}` : ''}`),
    h('div', { class: 'meta' }, [c.physical_description, c.clothing_description].filter(Boolean).join('. ')),
    h('div', { class: 'meta' }, `Last seen ${c.last_seen_area || 'nearby'} · ${c.last_seen_time}`))));
}

async function signup(ev) {
  ev.preventDefault();
  const out = $('signupResult');
  out.textContent = 'Finding your location…';
  try { if (!here) here = await getGPS(); } catch (e) { out.textContent = e.message; return; }
  const res = await api('/subscribers', {
    method: 'POST', body: { phone_number: $('signupPhone').value, latitude: here.lat, longitude: here.lon },
  });
  out.textContent = res.ok ? 'Done. You will get SMS alerts about danger near here.'
    : (res.network ? 'No connection. Try again.' : errText(res.data));
  if (res.ok) $('signupPhone').value = '';
}

// ---------------------------------------------------------------------------
// Fire / flood / medical: tile, then Send
// ---------------------------------------------------------------------------

const hazard = { type: null, loc: null };

function setHazardLoc(loc, how) {
  hazard.loc = loc;
  $('hazardLocation').textContent = how;
  $('hazardSendBtn').disabled = false;
  if (hazard.type === 'medical_emergency') loadFacilities(loc);
}

async function openHazard(type) {
  hazard.type = type;
  hazard.loc = null;
  $('hazardTitle').textContent = TITLES[type];
  $('hazardDesc').value = '';
  $('hazardEvidence').checked = false;
  $('hazardResult').textContent = '';
  $('hazardSendBtn').disabled = true;
  $('hazardSendBtn').textContent = 'Send report';
  $('hazardLocation').textContent = 'Finding your location…';
  const medical = type === 'medical_emergency';
  $('medicalHelp').classList.toggle('hidden', !medical);
  $('facilitiesCard').classList.add('hidden');
  if (medical) {
    $('medicalHelp').querySelector('.call-row').replaceChildren(...emergencyNumbers().map((n) =>
      h('a', { class: 'btn danger', href: `tel:${n}` }, `Call ${n}`)));
  }
  showScreen('hazard');
  try {
    here = await getGPS();
    if (hazard.type === type && !hazard.loc) setHazardLoc(here, `Using your location (accurate to about ${here.accuracy} m).`);
  } catch (e) {
    if (hazard.type === type && !hazard.loc) $('hazardLocation').textContent = e.message;
  }
}

async function sendHazard() {
  if (!hazard.loc) return;
  const btn = $('hazardSendBtn');
  btn.disabled = true;
  btn.textContent = 'Sending…';
  const body = {
    type: hazard.type, latitude: hazard.loc.lat, longitude: hazard.loc.lon,
    description: $('hazardDesc').value.trim(), timestamp: nowTimestamp(),
    has_evidence: $('hazardEvidence').checked,
  };
  const res = await sendCitizenReport(body);
  if (res.queued) {
    $('hazardResult').textContent = 'No signal. Saved on this phone; it will send by itself when you are back online.';
    btn.textContent = 'Saved';
  } else if (res.ok) {
    $('hazardResult').textContent = 'Report received.';
    btn.textContent = 'Sent';
  } else {
    $('hazardResult').textContent = errText(res.data, 'Not sent. Try again.');
    btn.disabled = false;
    btn.textContent = 'Send report';
  }
}

async function loadFacilities(loc) {
  const res = await api(`/facilities/nearest?latitude=${loc.lat}&longitude=${loc.lon}&limit=5`);
  if (!res.ok || !res.data.length) return;
  $('facilitiesList').replaceChildren(...res.data.map((f) => h('div', { class: 'item' },
    h('div', { class: 'title' }, f.name),
    h('div', { class: 'meta' }, `${label(f.amenity)} · ${f.distance_km} km${f.has_emergency === 'yes' ? ' · emergency care' : ''}`),
    h('div', { class: 'row' },
      h('a', {
        class: 'btn', target: '_blank', rel: 'noopener noreferrer',
        href: `https://www.google.com/maps/dir/?api=1&destination=${f.latitude},${f.longitude}`,
      }, 'Directions')))));
  $('facilitiesCard').classList.remove('hidden');
}

// ---------------------------------------------------------------------------
// Crime: discreet. Never GPS, never stored, small quiet confirmation.
// ---------------------------------------------------------------------------

let crimeLoc = null;

function resetCrimeForm() {
  $('crimeForm').reset();
  crimeLoc = null;
  $('crimeLocation').textContent = 'No place chosen yet.';
}

async function sendCrime(ev) {
  ev.preventDefault();
  const out = $('crimeResult');
  if (!crimeLoc) { out.textContent = 'Choose the place on the map first.'; return; }
  $('crimeSendBtn').disabled = true;
  const body = {
    type: $('crimeType').value, latitude: crimeLoc.lat, longitude: crimeLoc.lon,
    description: $('crimeDesc').value.trim(), timestamp: nowTimestamp(),
    visibility: $('crimeWarn').checked ? 'public' : 'officers_only',
  };
  const phone = $('crimePhone').value.trim();
  if (phone) body.reporter_phone = phone;
  const res = await api('/report/citizen', { method: 'POST', body });  // never the outbox
  $('crimeSendBtn').disabled = false;
  if (res.ok) {
    resetCrimeForm();
    out.textContent = res.data.phone_stored === false
      ? 'Sent. Your number could not be kept, so nobody can call you back.'
      : 'Sent.';
  } else {
    out.textContent = res.network ? 'Not sent: no connection. Nothing was saved. Try again.' : errText(res.data, 'Not sent. Try again.');
  }
}

// ---------------------------------------------------------------------------
// Missing child: step by step
// ---------------------------------------------------------------------------

let mcLoc = null;
let mcStepNow = 1;

function mcStep(n) {
  mcStepNow = n;
  document.querySelectorAll('#missing .step').forEach((s) => s.classList.toggle('hidden', Number(s.dataset.step) !== n));
  $('missingStep').textContent = `Step ${n} of 4`;
  window.scrollTo(0, 0);
}

function mcValid(step) {
  if (step === 2 && (!$('mcName').value.trim() || !$('mcPhysical').value.trim())) return 'Enter the child\'s name and what they look like.';
  if (step === 3 && !mcLoc) return 'Choose where the child was last seen on the map.';
  if (step === 3 && !$('mcTime').value) return 'Enter when the child was last seen.';
  return null;
}

function mcTimestamp() {
  return $('mcTime').value.replace('T', ' ').slice(0, 16);  // datetime-local -> "YYYY-MM-DD HH:MM"
}

async function sendMissing() {
  const out = $('mcResult');
  if (!$('mcPhone').value.trim()) { out.textContent = 'Enter your phone number so officers can reach you.'; return; }
  $('mcSendBtn').disabled = true;
  const age = $('mcAge').value;
  const res = await api('/missing-child/report', {
    method: 'POST',
    body: {
      child_name: $('mcName').value.trim(), age: age === '' ? null : Number(age),
      physical_description: $('mcPhysical').value.trim(), clothing_description: $('mcClothing').value.trim(),
      last_seen_latitude: mcLoc.lat, last_seen_longitude: mcLoc.lon, last_seen_area: $('mcArea').value.trim(),
      last_seen_time: mcTimestamp(), has_photo: $('mcHasPhoto').checked,
      reporter_phone: $('mcPhone').value.trim(), reporter_relationship: $('mcRelationship').value.trim(),
    },
  });
  $('mcSendBtn').disabled = false;
  if (res.ok) {
    document.querySelectorAll('#missing input, #missing textarea').forEach((el) => {
      if (el.type === 'checkbox') el.checked = false; else el.value = '';
    });
    mcLoc = null;
    $('mcLocation').textContent = 'No place chosen yet.';
    out.textContent = `Report received (case ${res.data.case_id}). If you have not yet, go to the nearest police station and get an OB number: officers need it before any alert can go out.`;
  } else {
    out.textContent = res.network ? 'Not sent: no connection. Nothing was saved. Try again.' : errText(res.data, 'Not sent. Try again.');
  }
}

// ---------------------------------------------------------------------------
// Map: loaded only when opened (saves data)
// ---------------------------------------------------------------------------

let leafletPromise = null;
function loadLeaflet() {
  if (window.L) return Promise.resolve();
  if (!leafletPromise) {
    leafletPromise = new Promise((resolve, reject) => {
      document.head.append(h('link', { rel: 'stylesheet', href: 'vendor/leaflet-1.9.4/leaflet.css' }));
      const s = h('script', { src: 'vendor/leaflet-1.9.4/leaflet.js' });
      s.addEventListener('load', resolve);
      s.addEventListener('error', () => { leafletPromise = null; reject(new Error('The map could not load. Check your connection.')); });
      document.head.append(s);
    });
  }
  return leafletPromise;
}

let map = null;
let mapLayer = null;
let pickHandler = null;
let picked = null;

async function openMap({ mode, hint, center, onPick }) {
  $('mapHint').textContent = hint;
  $('mapUseBtn').classList.toggle('hidden', mode !== 'pick');
  $('mapUseBtn').disabled = true;
  picked = null;
  pickHandler = mode === 'pick' ? onPick : null;
  showScreen('mapScreen');
  try { await loadLeaflet(); } catch (e) { $('mapHint').textContent = e.message; return; }
  const c = center || here || NAIROBI;
  if (!map) {
    map = L.map('map').setView([c.lat, c.lon], 15);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
    mapLayer = L.layerGroup().addTo(map);
    map.on('click', (e) => {
      if (!pickHandler) return;
      picked = { lat: e.latlng.lat, lon: e.latlng.lng };
      mapLayer.clearLayers();
      L.marker(e.latlng).addTo(mapLayer);
      $('mapUseBtn').disabled = false;
    });
  } else {
    map.setView([c.lat, c.lon], 15);
  }
  mapLayer.clearLayers();
  setTimeout(() => map.invalidateSize(), 50);
  if (mode === 'view') {
    if (here) L.circleMarker([here.lat, here.lon], { radius: 9, color: '#4c8dff', fillOpacity: 0.9 }).addTo(mapLayer).bindTooltip('You');
    nearbyIncidents.forEach((i) => L.circleMarker([i.latitude, i.longitude], {
      radius: 8, color: CRIME.has(i.type) ? '#9ca3af' : '#e5484d', fillOpacity: 0.7,
    }).addTo(mapLayer).bindTooltip(`${label(i.type)} · ${i.timestamp}`));
  }
}

function usePicked() {
  if (!picked || !pickHandler) return;
  const fn = pickHandler;
  back();
  fn(picked);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

$('backBtn').addEventListener('click', back);
$('exitBtn').addEventListener('click', quickExit);
$('checkBtn').addEventListener('click', checkSafety);
$('nearbyMapBtn').addEventListener('click', () => openMap({ mode: 'view', hint: 'You (blue) and public reports near you. Crime locations are approximate.' }));
document.querySelectorAll('[data-report]').forEach((b) => b.addEventListener('click', () => openHazard(b.dataset.report)));
document.querySelectorAll('[data-screen]').forEach((b) => b.addEventListener('click', () => {
  if (b.dataset.screen === 'missing') {
    mcStep(1);
    // In the demo the server refuses missing-child reports; say so up front.
    $('missingDemo').classList.toggle('hidden', !config.demo_mode);
    document.querySelectorAll('#missing .step').forEach((s) => s.classList.toggle('hidden', config.demo_mode || s.dataset.step !== '1'));
    $('missingStep').textContent = config.demo_mode ? '' : 'Step 1 of 4';
  }
  showScreen(b.dataset.screen);
}));
$('hazardSendBtn').addEventListener('click', sendHazard);
$('hazardPickBtn').addEventListener('click', () => openMap({
  mode: 'pick', hint: 'Tap where it is happening.', center: hazard.loc,
  onPick: (p) => setHazardLoc(p, `Chosen on the map (${coords(p)}).`),
}));
$('crimeForm').addEventListener('submit', sendCrime);
$('crimePickBtn').addEventListener('click', () => openMap({
  mode: 'pick', hint: 'Tap where it happened. Your phone\'s location is not used.', center: crimeLoc || NAIROBI,
  onPick: (p) => { crimeLoc = p; $('crimeLocation').textContent = `Chosen on the map (${coords(p)}).`; },
}));
document.querySelectorAll('#missing [data-next]').forEach((b) => b.addEventListener('click', () => {
  const problem = mcValid(mcStepNow);
  if (problem) { toast(problem); return; }
  mcStep(Number(b.dataset.next));
}));
$('mcPickBtn').addEventListener('click', () => openMap({
  mode: 'pick', hint: 'Tap where the child was last seen.', center: mcLoc,
  onPick: (p) => { mcLoc = p; $('mcLocation').textContent = `Chosen on the map (${coords(p)}).`; },
}));
$('mcSendBtn').addEventListener('click', sendMissing);
$('mapUseBtn').addEventListener('click', usePicked);
$('signupForm').addEventListener('submit', signup);
window.addEventListener('online', flushOutbox);
setInterval(flushOutbox, 30000);

(async function init() {
  const res = await api('/config');
  if (res.ok) config = res.data;
  $('demoBanner').classList.toggle('hidden', !config.demo_mode);
  $('signupForm').classList.toggle('hidden', !config.signup_enabled);
  $('signupClosed').classList.toggle('hidden', config.signup_enabled);
  document.querySelectorAll('.em-numbers').forEach((el) => { el.textContent = config.emergency_numbers; });
  updateOutboxBanner();
  loadChildren();
  flushOutbox();
})();
