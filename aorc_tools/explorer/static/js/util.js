/* WUP Grid Explorer — shared state and helpers */
'use strict';
const W = window.W = {};
const $ = id => document.getElementById(id);
W.$ = $;
W.API = (new URLSearchParams(location.search).get('api') || '').replace(/\/+$/, '');
W.FDI = ['FWI', 'FPI', 'ERC', 'BI'];
W.COLOR = { FWI: '#3987e5', FPI: '#199e70', ERC: '#d95926', BI: '#9085e9', alt: '#c98500' };
W.META = {
  FWI: { label: 'Fire Weather Index', units: '', dom: [0, 40], obsHour: 12,
         bands: [[0, 5, 'Low'], [5, 10, 'Moderate'], [10, 20, 'High'], [20, 30, 'Very high'], [30, 1e9, 'Extreme']] },
  FPI: { label: 'Fire Potential Index', units: '0–100', dom: [0, 100], obsHour: 13,
         bands: [[0, 25, 'Low'], [25, 50, 'Moderate'], [50, 75, 'High'], [75, 100, 'Very high']] },
  ERC: { label: 'Energy Release Component', units: 'BTU/ft²', dom: [0, 100], obsHour: 13, bands: [] },
  BI:  { label: 'Burning Index', units: '', dom: [0, 150], obsHour: 13, bands: [] },
  wx_tier: { label: 'Weather resolution tier', units: 'tier', dom: [0, 9], bands: [] },
  ndvi_tier: { label: 'NDVI resolution tier', units: 'tier', dom: [0, 9], bands: [] },
};
// single-hue sequential ramp for magnitude on the map
W.RAMP = ['#fde3c8', '#f6b983', '#ee8a4a', '#d9582a', '#a8331b', '#6b1d0f'];
// tier = data-quality state: status colours (always shown with a label)
W.TIER_COLOR = { 0: '#2fa36b', 1: '#9fb84a', 2: '#c98500', 3: '#d95926', 4: '#b8452d', 9: '#e66767' };

W.S = {
  status: null, catalog: null, grid: null, cells: null,
  mode: 'cell', sel: new Set(), selOrder: [], selInfo: null,
  cfg: null, fdis: ['FWI', 'FPI', 'ERC', 'BI'],
  series: null, stepById: new Map(), trace: null, traceKey: '',
  hourIdx: 0, focusPid: null, tab: 'FWI', pctMode: false, agg: 'hourly', overlay: {},
  field: { times: [], cache: new Map(), month: null }, coverage: null, jobs: [], playing: null,
  timeShow: 'utc', online: true,
};

W.fmt = (v, nd = 2) => (v == null || Number.isNaN(v)) ? '—' : (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : (+v).toFixed(nd));
W.pad2 = n => String(n).padStart(2, '0');
W.esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
W.pct = (v, nd = 2) => v == null ? '—' : (v === 0 ? '0%' : v < 0.01 ? '<0.01%' : (+v).toFixed(nd) + '%');
W.bytes = n => n > 1e9 ? (n / 1e9).toFixed(2) + ' GB' : n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n > 1e3 ? (n / 1e3).toFixed(0) + ' kB' : n + ' B';

W.api = async (path, opts = {}) => {
  if (opts.json !== undefined) { opts.method = opts.method || 'POST'; opts.headers = { 'content-type': 'application/json' }; opts.body = JSON.stringify(opts.json); delete opts.json; }
  const r = await fetch(W.API + path, opts);
  const ct = r.headers.get('content-type') || '';
  const j = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) { const e = new Error((j && j.error) || `${r.status} ${r.statusText}`); e.status = r.status; e.payload = j; throw e; }
  return j;
};

W.status = (msg, busy = false) => { /* status goes to the run message line */ const el = $('runmsg'); if (el) el.innerHTML = (busy ? '<span class="spin"></span> ' : '') + msg; };
W.toast = (html, ms = 4500) => {
  const t = document.createElement('div'); t.className = 'toast'; t.innerHTML = html; document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
};
W.download = (name, data, type = 'text/plain') => {
  const blob = data instanceof Blob ? data : new Blob([data], { type });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
};

/* ---------------- time ---------------- */
W.isoDate = s => new Date(String(s).replace(/Z$/, '') + (String(s).length <= 13 ? ':00' : '') + 'Z');
W.startTime = () => `${$('t-date').value}T${W.pad2($('t-hour').value)}`;
W.hours = () => Math.max(1, parseInt($('t-hours').value) || 24);
W.spin = () => Math.max(0, parseInt($('t-spin').value) || 0);
W.utcLabel = d => `${d.getUTCFullYear()}-${W.pad2(d.getUTCMonth() + 1)}-${W.pad2(d.getUTCDate())} ${W.pad2(d.getUTCHours())}Z`;
W.TZ = { CT: 'America/Menominee', ET: 'America/Detroit' };
W.STD = { CT: -6, ET: -5 };
const _fmtCache = {};
W.clockLabel = (d, zone) => {
  const tz = W.TZ[zone] || 'America/Detroit';
  const f = _fmtCache[tz] || (_fmtCache[tz] = new Intl.DateTimeFormat('en-US', { timeZone: tz, month: '2-digit', day: '2-digit', hour: '2-digit', hour12: false, timeZoneName: 'short' }));
  const p = Object.fromEntries(f.formatToParts(d).map(x => [x.type, x.value]));
  return `${p.month}-${p.day} ${p.hour === '24' ? '00' : p.hour}h ${p.timeZoneName}`;
};
W.lstLabel = (d, zone) => {
  const off = W.STD[zone] ?? -5; const l = new Date(d.getTime() + off * 3600e3);
  return `${W.pad2(l.getUTCMonth() + 1)}-${W.pad2(l.getUTCDate())} ${W.pad2(l.getUTCHours())}h ${zone === 'CT' ? 'CST' : 'EST'}`;
};
W.lstIso = (d, zone) => {        // ISO 8601 local standard time with its fixed offset, e.g. 2009-05-13T07:00-05:00
  const off = W.STD[zone] ?? -5; const l = new Date(d.getTime() + off * 3600e3);
  return `${l.getUTCFullYear()}-${W.pad2(l.getUTCMonth() + 1)}-${W.pad2(l.getUTCDate())}T${W.pad2(l.getUTCHours())}:00${off < 0 ? '-' : '+'}${W.pad2(Math.abs(off))}:00`;
};
W.timeLabel = (d, zone) => S().timeShow === 'clock' ? W.clockLabel(d, zone) : S().timeShow === 'lst' ? W.lstLabel(d, zone) : W.utcLabel(d);
function S() { return W.S; }

/* ---------------- selection encoding (share links) ---------------- */
W.encodeIds = ids => {           // sorted run-length: "a-b,c,d-e"
  const s = [...ids].sort((a, b) => a - b); const out = []; let i = 0;
  while (i < s.length) { let j = i; while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++; out.push(j > i ? `${s[i]}-${s[j]}` : `${s[i]}`); i = j + 1; }
  return out.join(',');
};
W.decodeIds = str => { const out = []; for (const part of String(str || '').split(',').filter(Boolean)) { const [a, b] = part.split('-').map(Number); if (b == null || isNaN(b)) out.push(a); else for (let x = a; x <= b; x++) out.push(x); } return out; };
W.b64url = {
  enc: async str => {
    const bytes = new TextEncoder().encode(str);
    let data = bytes;
    if (window.CompressionStream) { const cs = new Blob([bytes]).stream().pipeThrough(new CompressionStream('deflate-raw')); data = new Uint8Array(await new Response(cs).arrayBuffer()); }
    let b = ''; for (const x of data) b += String.fromCharCode(x);
    return (window.CompressionStream ? 'z' : 'p') + btoa(b).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },
  dec: async s => {
    const kind = s[0]; const b = atob(s.slice(1).replace(/-/g, '+').replace(/_/g, '/'));
    const bytes = Uint8Array.from(b, c => c.charCodeAt(0));
    if (kind === 'z') { const ds = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw')); return await new Response(ds).text(); }
    return new TextDecoder().decode(bytes);
  },
};

/* point-in-polygon for GeoJSON import (ray casting, holes respected) */
W.pointInPoly = (x, y, rings) => {
  let inside = false;
  for (const ring of rings) for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
  }
  return inside;
};
W.polysOf = gj => {
  const feats = gj.type === 'FeatureCollection' ? gj.features : gj.type === 'Feature' ? [gj] : [{ geometry: gj }];
  const polys = [];
  for (const f of feats) { const g = f.geometry; if (!g) continue;
    if (g.type === 'Polygon') polys.push(g.coordinates); else if (g.type === 'MultiPolygon') polys.push(...g.coordinates); }
  return polys;
};
