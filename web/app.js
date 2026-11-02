'use strict';

// ---------------------------------------------------------------- constants

const AREAS = {
  hk_island: 'Hong Kong Island', kowloon: 'Kowloon', nt_east: 'New Territories East',
  nt_west: 'New Territories West', lantau: 'Lantau', islands: 'Islands',
};

// Where the area buttons sit on the "Starting where?" map (% of the map box).
const PIN_POS = {
  nt_west: [30, 33], nt_east: [76, 33], kowloon: [58, 57],
  hk_island: [61, 75], lantau: [17, 76], islands: [45, 92],
};

// "Leaving from" places. land: which landmass you start on, for ferry time.
const ORIGINS = [
  ['Hong Kong Island', [['Central', 22.2819, 114.1582, 'hk'], ['Causeway Bay', 22.2803, 114.1838, 'hk'], ['Quarry Bay', 22.2879, 114.2096, 'hk']]],
  ['Kowloon', [['Mong Kok', 22.3193, 114.1694, 'main'], ['Tsim Sha Tsui', 22.2976, 114.1722, 'main'], ['Kwun Tong', 22.3123, 114.2260, 'main']]],
  ['New Territories', [['Sha Tin', 22.3817, 114.1870, 'main'], ['Tsuen Wan', 22.3737, 114.1175, 'main'], ['Tseung Kwan O', 22.3074, 114.2600, 'main'], ['Yuen Long', 22.4459, 114.0347, 'main']]],
  ['Lantau', [['Tung Chung', 22.2890, 113.9414, 'lantau'], ['Mui Wo', 22.2647, 114.0010, 'lantau'], ['Discovery Bay', 22.2940, 114.0168, 'lantau']]],
  ['Islands', [['Lamma', 22.2268, 114.1100, 'island'], ['Cheung Chau', 22.2089, 114.0290, 'island'], ['Peng Chau', 22.2856, 114.0397, 'island']]],
];

const SCREENS = ['home', 'where', 'time', 'hard', 'result', 'go'];
const LOCK_MS = 30 * 60 * 1000;
const STORE_KEY = 'takeahike.session.v1';
const ADMIN_KEY = 'takeahike.admin';

// Admin mode (for testing): open the site with ?admin to turn it on, ?admin=off to turn it off.
// It only skips the reroll limit in this browser; it is not a security feature.
const ADMIN = (() => {
  const param = new URLSearchParams(location.search).get('admin');
  try {
    if (param === 'off') localStorage.removeItem(ADMIN_KEY);
    else if (param !== null) localStorage.setItem(ADMIN_KEY, '1');
    return localStorage.getItem(ADMIN_KEY) === '1';
  } catch (e) {
    return param !== null && param !== 'off';
  }
})();
const TILES = 'https://mapapi.geodata.gov.hk/gs/api/v1.0.0/xyz/{layer}/WGS84/{z}/{x}/{y}.png';

// ---------------------------------------------------------------- state

const state = {
  trails: [],
  byId: {},
  screen: 'home',
  history: [],
  answers: { area: 'any', time: 'any', rating: 'any' },
  origin: { name: 'Mong Kok', lat: 22.3193, lon: 114.1694, land: 'main' },
  rerolling: false,
  relaxed: false,
  session: null, // { createdAt, rerolledAt, picks: [id, id?], current }
};

let map = null;
let mapLayer = null;
let lockTimer = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------------------------------------------------------------- storage

function loadSession() {
  try {
    const s = JSON.parse(localStorage.getItem(STORE_KEY));
    if (s && Date.now() - s.createdAt < LOCK_MS) return s;
  } catch (e) { /* private mode or bad data: start fresh */ }
  return null;
}

function saveSession() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state.session)); } catch (e) { /* ignore */ }
}

function sessionLocked() {
  if (ADMIN) return false;
  const s = state.session;
  return !!(s && s.picks.length >= 2 && Date.now() - s.rerolledAt < LOCK_MS);
}

// ---------------------------------------------------------------- navigation

function show(name, { push = true } = {}) {
  if (push && name !== state.screen) state.history.push(state.screen);
  state.screen = name;
  const idx = SCREENS.indexOf(name);
  $$('.screen').forEach((el) => {
    const i = SCREENS.indexOf(el.dataset.screen);
    el.classList.toggle('is-active', i === idx);
    el.classList.toggle('is-before', i < idx);
    el.setAttribute('aria-hidden', i === idx ? 'false' : 'true');
    if (i === idx) el.scrollTop = 0;
  });
  if (name === 'result') setTimeout(fitMap, 500);
}

function back() {
  const prev = state.history.pop() || 'home';
  show(prev, { push: false });
}

// ---------------------------------------------------------------- picking

function land(trail) {
  return { hk_island: 'hk', lantau: 'lantau', islands: 'island' }[trail.area] || 'main';
}

function haversineKm(a, b) {
  const R = 6371, toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b[0] - a[0]), dLon = toRad(b[1] - a[1]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Rough public-transport estimate until we plug in real journey planning.
function travelMinutes(point, trail) {
  const o = state.origin;
  let mins = 15 + haversineKm([o.lat, o.lon], point) * 3.2;
  const tl = land(trail);
  if (tl === 'island') mins += 40;
  else if (o.land === 'island') mins += 35;
  else if ((o.land === 'lantau') !== (tl === 'lantau')) mins += 15;
  return Math.round(mins / 5) * 5;
}

function totalHours(trail) {
  return (travelMinutes(trail.start, trail) + travelMinutes(trail.end, trail)) / 60 + trail.hours;
}

function matches(trail, { area, time, rating }) {
  if (area !== 'any' && trail.area !== area) return false;
  if (rating !== 'any' && trail.rating !== rating) return false;
  if (time !== 'any') {
    const [lo, hi] = time.split('-').map(Number);
    const h = totalHours(trail);
    if (h <= lo || h > hi) return false;
  }
  return true;
}

function choose() {
  const used = new Set(state.session ? state.session.picks : []);
  const a = state.answers;
  // Relax the answers one at a time if nothing fits: time, then difficulty, then area.
  const tries = [a, { ...a, time: 'any' }, { ...a, time: 'any', rating: 'any' }, { area: 'any', time: 'any', rating: 'any' }];
  for (let i = 0; i < tries.length; i++) {
    const pool = state.trails.filter((t) => !used.has(t.id) && matches(t, tries[i]));
    if (pool.length) {
      state.relaxed = i > 0;
      return pool[Math.floor(Math.random() * pool.length)];
    }
  }
  return null;
}

function pickNow() {
  const trail = choose();
  if (!trail) return;
  const now = Date.now();
  if (state.rerolling && state.session) {
    state.session.picks.push(trail.id);
    state.session.current = state.session.picks.length - 1;
    if (!ADMIN) state.session.rerolledAt = now;
    state.rerolling = false;
  } else {
    state.session = { createdAt: now, rerolledAt: null, picks: [trail.id], current: 0 };
  }
  saveSession();
  renderResult();
  show('result');
}

function start() {
  state.session = state.session || loadSession();
  if (sessionLocked()) { renderResult(); show('result'); return; }
  // Already have a first pick in this half hour? Going again uses the reroll.
  state.rerolling = !ADMIN && !!(state.session && state.session.picks.length === 1);
  if (ADMIN) state.session = null; // admin: every LET'S GO starts fresh
  state.answers = { area: 'any', time: 'any', rating: 'any' };
  show('where');
}

// ---------------------------------------------------------------- rendering

function bind(name, value, root = document) {
  $$(`[data-bind="${name}"]`, root).forEach((el) => { el.textContent = value; });
}

function fmtHours(h) {
  const whole = Math.floor(h), mins = Math.round((h - whole) * 60);
  if (!whole) return `${mins} min`;
  return mins ? `${whole} h ${mins}` : `${whole} h`;
}

function fmtMinutes(m) {
  return m < 60 ? `about ${m} min` : `about ${fmtHours(m / 60)}`;
}

function current() {
  const s = state.session;
  return s ? state.byId[s.picks[s.current]] : null;
}

function renderResult() {
  const t = current();
  if (!t) return;
  const s = state.session;
  const two = s.picks.length > 1;
  // The pick to offer going back to: the other one of two, or (admin) the previous one.
  const last = s.picks.length - 1;
  const otherIndex = s.current === last ? last - 1 : last;
  const other = two ? state.byId[s.picks[otherIndex]] : null;
  const ordinal = (i) => (ADMIN ? `pick ${i + 1}` : `${i === 0 ? 'first' : 'second'} pick`);

  bind('pick-label', two ? `Today's hike · ${ordinal(s.current)}` : "Today's hike");
  bind('name', t.name);
  bind('area', t.place_note ? `${AREAS[t.area]} · ${t.place_note}` : AREAS[t.area]);
  bind('km', `${t.km} km`);
  bind('hours', fmtHours(t.hours));
  bind('climb', `${t.climb} m`);
  bind('rating', t.rating);
  bind('origin', state.origin.name);
  bind('travel', fmtMinutes(travelMinutes(t.start, t)));
  bind('hike', fmtHours(t.hours));
  $('[data-bind="out-and-back"]').hidden = !t.out_and_back;
  const relaxed = $('[data-bind="relaxed"]');
  if (relaxed) relaxed.hidden = !state.relaxed;

  // Elevation profile. At least 150 m of height range, so flat walks look flat.
  const e = t.elev, lo = Math.min(...e), hi = Math.max(...e);
  const range = Math.max(hi - lo, 150);
  const pts = e.map((v, i) => `${((i / (e.length - 1)) * 342).toFixed(1)} ${(60 - ((v - lo) / range) * 52).toFixed(1)}`);
  $('[data-bind="elev"]').setAttribute('d', `M0 64 L${pts.join(' L')} L342 64 Z`);
  bind('elev-left', `${e[0]} m`);
  bind('elev-right', `high point ${hi} m`);

  // Buttons: one reroll, then you can only switch between your two picks.
  $('[data-bind="reroll"]').hidden = two && !ADMIN;
  if (ADMIN) $('[data-bind="reroll"] .small').textContent = 'Admin mode: unlimited rerolls.';
  const sw = $('[data-bind="switch"]');
  sw.hidden = !two;
  if (two) {
    bind('other-name', other.name);
    bind('other-which', `Your ${ordinal(otherIndex)}`);
  }
  renderLock();

  // Directions page
  bind('meta', `From ${state.origin.name} · ${fmtHours(t.hours)} walk · ${t.km} km`);
  renderSteps(t);
  $('[data-bind="source"]').href = t.source_url;
  bind('save-label', 'Save directions to phone');

  drawMap(t);
}

// Route lists and stations get a bold label ("Citybus route 9"); full
// sentences ("Take public transport to...") already say what to do.
function stepLabel(s) {
  return s.routes ? s.mode : '';
}

function stepsFor(t) {
  if (t.steps && t.steps.length) return t.steps;
  return [{ mode: '', text: t.get_there || 'See the official trail page for transport.' }];
}

function finishStepsFor(t) {
  // Only when that's all the text says: longer ones ("walk 20 minutes back to...") are real directions.
  const sameAsStart = /^(transportation )?same as start/i.test(t.finish || '');
  if (t.loop || (t.out_and_back && (sameAsStart || !t.finish))) return [{ mode: '', text: "You're back where you started." }];
  if (sameAsStart) return [{ mode: '', text: 'You finish close to the start, so leave the way you came.' }];
  if (t.finish_steps && t.finish_steps.length) return t.finish_steps;
  return [{ mode: '', text: t.finish || 'Head out from the end of the route (see the map).' }];
}

function fillSteps(list, steps) {
  list.innerHTML = '';
  for (const s of steps) {
    const li = document.createElement('li');
    const body = document.createElement('span');
    const label = stepLabel(s);
    if (label) {
      const b = document.createElement('b');
      b.textContent = `${label} `;
      body.appendChild(b);
    }
    body.appendChild(document.createTextNode(s.text));
    li.appendChild(body);
    list.appendChild(li);
  }
}

function renderSteps(t) {
  $$('[data-steps="there"]').forEach((el) => fillSteps(el, stepsFor(t)));
  $$('[data-steps="finish"]').forEach((el) => fillSteps(el, finishStepsFor(t)));
}

function renderLock() {
  clearInterval(lockTimer);
  const el = $('[data-bind="lock"]');
  const update = () => {
    const left = state.session && state.session.rerolledAt ? LOCK_MS - (Date.now() - state.session.rerolledAt) : 0;
    if (ADMIN || left <= 0 || state.session.picks.length < 2) { el.hidden = true; clearInterval(lockTimer); return; }
    const m = Math.floor(left / 60000), sec = Math.floor((left % 60000) / 1000);
    el.hidden = false;
    el.textContent = `No rerolls for ${m}:${String(sec).padStart(2, '0')}`;
  };
  update();
  lockTimer = setInterval(update, 1000);
}

// ---------------------------------------------------------------- map

function drawMap(t) {
  if (typeof L === 'undefined') return;
  if (!map) {
    map = L.map('map', { zoomControl: false, attributionControl: false, scrollWheelZoom: false });
    L.tileLayer(TILES.replace('{layer}', 'basemap'), { maxZoom: 19 }).addTo(map);
    L.tileLayer(TILES.replace('{layer}', 'label/hk/en'), { maxZoom: 19 }).addTo(map);
    L.control.zoom({ position: 'bottomright' }).addTo(map);
  }
  if (mapLayer) mapLayer.remove();
  const dot = (cls) => L.divIcon({ className: `marker ${cls}`, iconSize: cls === 'start' ? [18, 18] : [16, 16] });
  mapLayer = L.layerGroup([
    L.polyline(t.route, { color: '#FFFFFF', weight: 9, opacity: 0.9 }),
    L.polyline(t.route, { color: '#1E2B22', weight: 5 }),
    L.marker(t.end, { icon: dot('end'), keyboard: false }),
    L.marker(t.start, { icon: dot('start'), keyboard: false }),
  ]).addTo(map);
  fitMap();
}

function fitMap() {
  const t = current();
  if (!map || !t) return;
  map.invalidateSize();
  map.fitBounds(L.latLngBounds(t.route), { padding: [28, 28] });
}

// ---------------------------------------------------------------- area map

async function renderAreaMap() {
  const box = $('#area-map');
  try {
    const regions = await (await fetch('data/regions.json')).json();
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', regions.viewBox);
    svg.setAttribute('aria-hidden', 'true');
    for (const [key, d] of Object.entries(regions.paths)) {
      const p = document.createElementNS(svgNS, 'path');
      p.setAttribute('d', d);
      p.dataset.region = key;
      svg.appendChild(p);
    }
    box.appendChild(svg);
    for (const [key, [x, y]] of Object.entries(PIN_POS)) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn pin';
      b.dataset.area = key;
      b.textContent = AREAS[key];
      b.style.left = `${x}%`;
      b.style.top = `${y}%`;
      const hl = (on) => $$(`path[data-region="${key}"]`, svg).forEach((p) => p.classList.toggle('hover', on));
      b.addEventListener('mouseenter', () => hl(true));
      b.addEventListener('mouseleave', () => hl(false));
      b.addEventListener('focus', () => hl(true));
      b.addEventListener('blur', () => hl(false));
      box.appendChild(b);
    }
  } catch (e) {
    box.remove(); // fall back to the plain list
  }
}

// ---------------------------------------------------------------- modals

function openModal(name) {
  const el = $(`[data-modal="${name}"]`);
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add('is-open'));
  const focusable = $('.round', el);
  if (focusable) focusable.focus();
}

function closeModals() {
  $$('[data-modal]').forEach((el) => {
    if (el.hidden) return;
    el.classList.remove('is-open');
    setTimeout(() => { el.hidden = true; }, 260);
  });
}

function renderOrigins() {
  const list = $('#origin-list');
  list.innerHTML = '';
  for (const [group, places] of ORIGINS) {
    const h = document.createElement('div');
    h.className = 'origin-group';
    h.textContent = group.toUpperCase();
    list.appendChild(h);
    for (const [name, lat, lon, landName] of places) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'origin';
      b.textContent = name;
      b.setAttribute('aria-pressed', String(state.origin.name === name));
      b.addEventListener('click', () => setOrigin({ name, lat, lon, land: landName }));
      list.appendChild(b);
    }
  }
}

function setOrigin(o) {
  state.origin = o;
  bind('origin', o.name);
  renderOrigins();
  if (current()) renderResult();
  closeModals();
}

function locate() {
  const btn = $('[data-action="locate"]');
  if (!navigator.geolocation) { btn.textContent = 'Location not available'; return; }
  btn.textContent = 'Finding you…';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const { latitude: lat, longitude: lon } = pos.coords;
      const onLantau = lon < 114.06 && lat < 22.35 && lat > 22.19;
      btn.textContent = 'Use my location';
      setOrigin({ name: 'your location', lat, lon, land: onLantau ? 'lantau' : 'main' });
    },
    () => { btn.textContent = "Couldn't get your location"; },
    { timeout: 10000 },
  );
}

// ---------------------------------------------------------------- save image

function wrap(ctx, text, maxWidth) {
  const words = text.split(/\s+/), lines = [];
  let line = '';
  for (const w of words) {
    const test = line ? `${line} ${w}` : w;
    if (ctx.measureText(test).width > maxWidth && line) { lines.push(line); line = w; } else line = test;
  }
  if (line) lines.push(line);
  return lines;
}

async function saveImage() {
  const t = current();
  if (!t) return;
  try { await document.fonts.ready; } catch (e) { /* draw with fallback fonts */ }
  const W = 1080, PAD = 80, inner = W - PAD * 2;
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  const font = (w, s, fam = 'Bricolage Grotesque') => `${w} ${s}px '${fam}', system-ui, sans-serif`;

  // Measure first so the image is as tall as it needs to be.
  ctx.font = font(800, 84); const nameLines = wrap(ctx, t.name, inner);
  ctx.font = font(400, 38);
  const measure = (steps) => steps.map((s) => {
    const label = stepLabel(s);
    return wrap(ctx, label ? `${label} ${s.text}` : s.text, inner - 160);
  });
  const height = (lines) => 120 + lines.reduce((h, l) => h + l.length * 52 + 34, 0);
  const there = measure(stepsFor(t));
  const after = measure(finishStepsFor(t));
  const H = Math.max(1920, 420 + nameLines.length * 96 + height(there) + height(after) + 260);
  c.width = W; c.height = H;

  ctx.fillStyle = '#FFCC00'; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#1E2B22';
  let y = PAD + 50;
  ctx.font = font(400, 44, 'Playwrite AU SA'); ctx.fillText('take a hike', PAD, y);
  ctx.font = font(800, 40); ctx.textAlign = 'right'; ctx.fillText('Emergency 999', W - PAD, y); ctx.textAlign = 'left';
  y += 130;
  ctx.font = font(800, 84); nameLines.forEach((l) => { ctx.fillText(l, PAD, y); y += 96; });
  ctx.font = font(600, 40); ctx.fillText(`From ${state.origin.name} · ${fmtHours(t.hours)} walk · ${t.km} km`, PAD, y); y += 70;

  const drawBox = (title, stepLines) => {
    const boxH = height(stepLines);
    ctx.fillStyle = '#FFFFFF';
    ctx.beginPath(); ctx.roundRect(PAD, y, inner, boxH, 40); ctx.fill();
    ctx.fillStyle = '#5B6159'; ctx.font = font(800, 32); ctx.fillText(title, PAD + 40, y + 70);
    let sy = y + 130;
    stepLines.forEach((lines, i) => {
      ctx.fillStyle = '#1E2B22';
      ctx.beginPath(); ctx.arc(PAD + 64, sy - 13, 24, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#FFFFFF'; ctx.font = font(800, 28); ctx.textAlign = 'center';
      ctx.fillText(String(i + 1), PAD + 64, sy - 3); ctx.textAlign = 'left';
      ctx.fillStyle = '#1E2B22'; ctx.font = font(400, 38);
      lines.forEach((l, j) => ctx.fillText(l, PAD + 120, sy + j * 52));
      sy += lines.length * 52 + 34;
    });
    y += boxH + 40;
  };
  drawBox('GETTING THERE', there);
  drawBox('WHEN YOU FINISH', after);
  ctx.font = font(400, 28); ctx.fillText('Trail info: hiking.gov.hk', PAD, H - PAD);

  const blob = await new Promise((r) => c.toBlob(r, 'image/png'));
  const file = new File([blob], `take-a-hike-${t.id}.png`, { type: 'image/png' });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: t.name });
    } else {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = file.name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    }
    bind('save-label', 'Saved ✓');
  } catch (e) { /* user closed the share sheet */ }
}

// ---------------------------------------------------------------- events

function onClick(e) {
  const el = e.target.closest('[data-action], [data-area], [data-time], [data-rating]');
  if (!el) return;
  if (el.dataset.area) { state.answers.area = el.dataset.area; show('time'); return; }
  if (el.dataset.time) { state.answers.time = el.dataset.time; show('hard'); return; }
  if (el.dataset.rating) { state.answers.rating = el.dataset.rating; pickNow(); return; }
  switch (el.dataset.action) {
    case 'start': start(); break;
    case 'pick-for-me':
      state.session = state.session || loadSession();
      if (sessionLocked()) { renderResult(); show('result'); break; }
      state.rerolling = !ADMIN && !!(state.session && state.session.picks.length === 1);
      if (ADMIN) state.session = null;
      state.answers = { area: 'any', time: 'any', rating: 'any' };
      pickNow();
      break;
    case 'back': back(); break;
    case 'go': show('go'); break;
    case 'reroll':
      state.rerolling = true;
      state.answers = { area: 'any', time: 'any', rating: 'any' };
      state.history = ['home'];
      show('where', { push: false });
      break;
    case 'switch': {
      const last = state.session.picks.length - 1;
      state.session.current = state.session.current === last ? last - 1 : last;
      saveSession();
      renderResult();
      break;
    }
    case 'save': saveImage(); break;
    case 'open-about': openModal('about'); break;
    case 'open-origin': renderOrigins(); openModal('origin'); break;
    case 'close-modal': closeModals(); break;
    case 'locate': locate(); break;
    default: break;
  }
}

async function init() {
  document.addEventListener('click', onClick);
  $('.admin-badge').hidden = !ADMIN;
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModals(); });
  show('home', { push: false });
  renderAreaMap();
  try {
    state.trails = await (await fetch('data/trails.json')).json();
    state.byId = Object.fromEntries(state.trails.map((t) => [t.id, t]));
  } catch (e) {
    $('[data-action="start"]').textContent = "Couldn't load trails";
  }
}

init();
