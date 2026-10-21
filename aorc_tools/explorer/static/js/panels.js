/* Right-hand panels */
'use strict';
(function () {
  const S = W.S;
  const body = () => $('tabbody');
  const TIER_LABEL = { 0: 'native', 1: 'time-interp', 2: '3×3', 3: '7×7', 4: 'brick', 9: 'none' };
  const NDVI_LABEL = { 0: '250 m', 1: '500 m', 2: '1 km', 3: 'AVHRR 5 km', 4: 'climatology', 9: 'none' };

  W.renderTab = function () {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === S.tab));
    const b = body(); b.innerHTML = '';
    const f = { DAY: renderDaylight, FLOW: renderFlow, RES: renderResolution, COV: renderCoverage, JOBS: renderJobs, LIB: renderLibrary, FIRE: W.renderFires, LINES: W.renderLines, DATA: W.renderData }[S.tab];
    if (f) return f(b);
    renderFDI(b, S.tab);
  };

  /* ================================================================ FDI tabs */
  function stepOf(id) { return S.stepById.get(id); }
  const EXTRA_OVERLAYS = { FWI: { FWI_daily: 'classic daily FWI (12:00 LST, held to next noon)' } };
  const overlayLabel = (fdi, a) => EXTRA_OVERLAYS[fdi]?.[a] || S.series.alts?.[a] || a;
  function focusIdx() { return S.series && S.focusPid != null ? S.series.point_ids.indexOf(S.focusPid) : -1; }
  function cellSeries(id, k) { const st = stepOf(id); if (!st) return null; if (st.values && k >= 0) return st.values[k]; return null; }

  function aggregate(times, arrs, zones, fdi) {
    // arrs: array of value arrays (one per line, each aligned to `times`); zones: zone per line
    if (S.agg === 'hourly') return { times, arrs };
    const obs = W.META[fdi]?.obsHour ?? 13;
    const keyset = new Map();
    const out = arrs.map((a, li) => { const off = W.STD[zones[li]] ?? -5; const m = new Map();
      a.forEach((v, i) => { const l = new Date(times[i].getTime() + off * 3600e3); const k = l.toISOString().slice(0, 10); keyset.set(k, true);
        if (v == null) return;
        if (S.agg === 'daymax') m.set(k, Math.max(m.get(k) ?? -Infinity, v));
        else if (l.getUTCHours() === obs) m.set(k, v); });
      return m; });
    const keys = [...keyset.keys()].sort();
    return { times: keys.map(k => new Date(k + 'T12:00:00Z')), arrs: out.map(m => keys.map(k => m.has(k) ? m.get(k) : null)), daily: true };
  }

  function renderFDI(b, fdi) {
    if (S.mode === 'all' && !S.series) { b.innerHTML = `<div class="sect"><div class="callout">Entire-WUP view colours the map from saved results, one packet at a time (Map colour panel). To chart a time series, switch to Cell, Box, Brick or County and press <b>Show</b>.</div>${fieldNote()}</div>`; return; }
    if (!S.series) { b.innerHTML = `<div class="sect"><h2>${fdi} · ${W.META[fdi].label}</h2><div class="callout">Select cells on the map, set a window of hourly packets, then press <b>Show</b> (saved results if they exist, otherwise computed now) or <b>Explore live</b> (computed now, with comparison overlays). Each tab charts one index; the table under the chart lists every pipeline step for the focused cell at the selected packet.</div>${cardHtml(fdi)}</div>`; return; }
    const meta = W.META[fdi], col = W.COLOR[fdi];
    const sec = document.createElement('div'); sec.className = 'sect'; b.appendChild(sec);
    const alts = [...Object.keys(EXTRA_OVERLAYS[fdi] || {}).filter(a => stepOf(a)), ...Object.keys(S.series.alts || {}).filter(a => a.startsWith(fdi + '@'))];
    if (!(fdi in S.overlay)) S.overlay[fdi] = alts.includes(fdi + '_daily') ? fdi + '_daily' : alts.includes(fdi + '@daylight') ? fdi + '@daylight' : (alts[0] || '');
    const src = S.series.source === 'store' ? 'saved results' : S.series.source === 'live' ? 'computed live' : 'batch record';
    sec.innerHTML = `<div class="charthead"><h2 style="color:${col}">${fdi} <span style="color:var(--ink-2);font-weight:400">${meta.label}</span></h2>
        <span class="meta">${S.series.point_ids.length.toLocaleString()} cell${S.series.point_ids.length === 1 ? '' : 's'} · ${S.series.time.length} packets · ${src}${S.series.synthetic ? ' · <b style="color:#f0c27a">synthetic</b>' : ''}</span></div>
      <div class="charttools">
        <span class="seg"><button class="btn sm ${S.pctMode ? '' : 'active'}" data-pct="0">raw</button><button class="btn sm ${S.pctMode ? 'active' : ''}" data-pct="1" ${stepOf(fdi + '_pct') ? '' : 'disabled title="Build percentiles in the Coverage tab first"'}>percentile</button></span>
        <select id="aggsel" title="Time resolution of the chart"><option value="hourly">hourly</option><option value="daymax">daily max (local day)</option><option value="obs">daily @ ${meta.obsHour}:00 LST</option></select>
        <select id="ovsel" title="Overlay a comparison chain"><option value="">no overlay</option>${alts.map(a => `<option value="${a}">${W.esc(overlayLabel(fdi, a))}</option>`).join('')}</select>
        <button class="btn sm" id="t-prev" title="Previous index ([)">◀</button><button class="btn sm" id="t-next" title="Next index (])">▶</button>
        <button class="btn sm ghost" id="t-svg" title="Download chart as SVG">SVG</button><button class="btn sm ghost" id="t-png" title="Download chart as PNG">PNG</button><button class="btn sm ghost" id="t-csv" title="Download the series as CSV">CSV</button>
      </div>
      <div id="chartbox"></div><div class="stats" id="stats"></div><div id="fdinotes"></div><div id="tracebox"></div>`;
    $('aggsel').value = S.agg; $('ovsel').value = S.overlay[fdi] || '';
    sec.querySelectorAll('[data-pct]').forEach(x => x.onclick = () => { S.pctMode = x.dataset.pct === '1'; $('m-mode').value = S.pctMode ? 'pct' : 'raw'; W.paintMap(); W.renderTab(); });
    $('aggsel').onchange = e => { S.agg = e.target.value; drawFDIChart(fdi); };
    $('ovsel').onchange = e => { S.overlay[fdi] = e.target.value; drawFDIChart(fdi); };
    $('t-prev').onclick = () => W.cycleTab(-1); $('t-next').onclick = () => W.cycleTab(1);
    $('t-svg').onclick = () => W.download(`wup_${fdi}_${S.series.time[0].slice(0, 13)}.svg`, W.svgString(S.chartSvg), 'image/svg+xml');
    $('t-png').onclick = async () => W.download(`wup_${fdi}_${S.series.time[0].slice(0, 13)}.png`, await W.svgToPng(S.chartSvg));
    $('t-csv').onclick = () => W.exportSeriesCsv();
    drawFDIChart(fdi);
    $('fdinotes').innerHTML = cardHtml(fdi) + (S.series.notes?.length ? `<div class="callout">${S.series.notes.map(n => '• ' + W.esc(n)).join('<br>')}</div>` : '');
    W.renderTrace();
  }

  function drawFDIChart(fdi, box = $('chartbox'), withStats = true) {
    if (!box) return null;
    const id = S.pctMode ? fdi + '_pct' : fdi, st = stepOf(id), col = W.COLOR[fdi];
    const k = focusIdx(), times = S.series.time.map(W.isoDate);
    const pids = S.series.point_ids;
    const zoneOf = pid => S.cells.zone[W.row(pid)];
    const raw = [], zones = [], specs = [];
    if (st) {
      if (st.values && pids.length > 1 && pids.length <= 40) st.values.forEach((v, i) => { if (i !== k) { raw.push(v); zones.push(zoneOf(pids[i])); specs.push({ color: col, w: 0.8, a: 0.25 }); } });
      if (pids.length > 1) { raw.push(st.mean); zones.push(zoneOf(pids[0])); specs.push({ color: col, w: 1.6, a: 0.8, dash: '5 3', label: 'mean of selection' }); }
      if (k >= 0 && st.values) { raw.push(st.values[k]); zones.push(zoneOf(S.focusPid)); specs.push({ color: col, w: 2.2, label: `cell ${S.focusPid}` }); }
      else if (pids.length === 1) { raw.push((st.values && st.values[0]) || st.mean); zones.push(zoneOf(pids[0])); specs.push({ color: col, w: 2.2, label: `cell ${pids[0]}` }); }
      const ov = S.overlay[fdi];
      if (ov && !S.pctMode && stepOf(ov)) { const o = stepOf(ov); raw.push(k >= 0 && o.values ? o.values[k] : o.mean); zones.push(zoneOf(S.focusPid ?? pids[0])); specs.push({ color: W.COLOR.alt, w: 1.7, dash: '2 3', label: overlayLabel(fdi, ov) }); }
    }
    const agg = aggregate(times, raw, zones, fdi);
    const lines = agg.arrs.map((v, i) => ({ ...specs[i], v }));
    let envelope = null;
    if (st && !st.values && st.min && S.agg === 'hourly') envelope = { lo: st.min, hi: st.max, color: col };
    const tierRows = [];
    if (S.agg === 'hourly') {
      const tsel = id => { const t = stepOf(id); if (!t) return null; if (k >= 0 && t.values) return t.values[k]; return t.max; };
      const wx = tsel('wx_tier'); if (wx) tierRows.push({ label: 'wx', values: wx });
      if (fdi === 'FPI') { const nd = tsel('ndvi_tier'); if (nd) tierRows.push({ label: 'NDVI', values: nd }); }
    }
    const zone = zoneOf(S.focusPid ?? pids[0]);
    const svg = W.drawLineChart(box, {
      times: agg.times, lines, envelope, bands: S.pctMode ? [] : W.META[fdi].bands, units: S.pctMode ? 'percentile' : W.META[fdi].units,
      yFixed: S.pctMode ? [0, 100] : (fdi === 'FPI' ? [0, 100] : null), current: agg.daily ? 0 : S.hourIdx, tiers: tierRows,
      title: `${fdi}${S.pctMode ? ' percentile' : ''} · ${S.agg === 'hourly' ? 'hourly' : S.agg === 'daymax' ? 'daily max (local standard day)' : `daily at ${W.META[fdi].obsHour}:00 LST`}`,
      xFmt: d => agg.daily ? d.toISOString().slice(5, 10) : (S.timeShow === 'utc' ? d.toISOString().slice(5, 10) : W.timeLabel(d, zone).slice(0, 5)),
      xNote: agg.daily ? 'local standard date' : (S.timeShow === 'utc' ? 'UTC' : S.timeShow === 'clock' ? 'local clock' : 'local standard time'),
      tipTime: d => agg.daily ? d.toISOString().slice(0, 10) + ' (local day)' : `${W.utcLabel(d)} · ${W.clockLabel(d, zone)}`,
      onPick: agg.daily || !withStats ? null : i => W.setHour(i), height: 270, daily: !!agg.daily,
    });
    if (!withStats) return svg;
    S.chartSvg = svg;
    // headline stats
    const base = lines.find(L => L.label && L.label.startsWith('cell')) || lines.find(L => L.label === 'mean of selection');
    if (!base) { $('stats').innerHTML = ''; return svg; }
    const vals = base.v.filter(v => v != null);
    const mx = vals.length ? Math.max(...vals) : null, imx = base.v.indexOf(mx);
    const hiBand = W.META[fdi].bands[2]?.[0];
    const wxs = stepOf('wx_tier'); const nat = wxs?.counts ? (100 * (wxs.counts['0'] || 0) / Object.values(wxs.counts).reduce((a, b) => a + b, 0)) : null;
    $('stats').innerHTML = `<div class="stat"><b>${W.fmt(agg.daily ? base.v[base.v.length - 1] : base.v[S.hourIdx])}</b><span>${agg.daily ? 'last day' : 'at packet'}</span></div>
      <div class="stat"><b>${W.fmt(mx)}</b><span>max · ${imx >= 0 ? (agg.daily ? agg.times[imx].toISOString().slice(0, 10) : S.series.time[imx].slice(5, 16)) : ''}</span></div>
      <div class="stat"><b>${W.fmt(vals.reduce((a, b) => a + b, 0) / (vals.length || 1))}</b><span>mean</span></div>
      ${hiBand != null && !S.pctMode ? `<div class="stat"><b>${vals.filter(v => v >= hiBand).length}</b><span>${agg.daily ? 'days' : 'packets'} ≥ "High"</span></div>` : ''}
      ${fdi === 'FWI' && stepOf('FWI_daily') && !agg.daily ? (() => { const d = cellSeries('FWI_daily', k) || stepOf('FWI_daily').mean; return `<div class="stat"><b>${W.fmt(d?.[S.hourIdx])}</b><span>classic daily (noon LST)</span></div>`; })() : ''}
      ${nat != null ? `<div class="stat"><b>${W.fmt(nat, 2)}%</b><span>native weather</span></div>` : ''}`;
    return svg;
  }
  W.buildFDIChart = (fdi, box) => drawFDIChart(fdi, box, false);
  W.redrawChart = () => { if (W.FDI.includes(S.tab) && S.series) drawFDIChart(S.tab); };
  W.updateChartCursor = () => { if (S.chartSvg && S.chartSvg._setCurrent && S.agg === 'hourly') S.chartSvg._setCurrent(S.hourIdx); };

  function cardHtml(fdi) {
    const c = (S.catalog.fdi_cards || []).find(x => x.id === fdi); if (!c) return '';
    return `<div class="callout"><b>${c.name}</b> — ${W.esc(c.system)}<br>time: <b>${W.esc(c.time)}</b> · space: ${W.esc(c.space)}<br>${W.esc(c.how)}<br><span style="color:var(--ink-3)">status: ${W.esc(c.status)} · reported as ${W.esc(c.report)}</span></div>`;
  }
  function fieldNote() {
    return `<div class="callout">Saved results exist for ${W.pct(S.status?.coverage?.percent_computed, 4)} of all packets. Use the Jobs tab (or <b>Calculate &amp; save</b>) to add more; the Coverage tab shows exactly which years, months and bricks are done.</div>`;
  }

  /* effective source resolution of an AORC input for the packet's year (AORC v1.1 methods) */
  function resBadge(s, year) {
    let lab = s.res;
    if (s.res_eras) { const e = s.res_eras.find(([last]) => year <= last); if (e) lab = e[1]; }
    const coarse = /12 km|~4|4 km/.test(lab);
    return `<span class="res ${coarse ? 'r12' : 'r800'}" title="effective source resolution in ${year}: ${W.esc(lab)} (all eras: ${W.esc(s.res)}); delivered on the 800 m grid">${W.esc(lab)}</span>`;
  }

  /* ---------------- trace table ---------------- */
  W.renderTrace = function () {
    const box = $('tracebox'); if (!box) return;
    const fdi = S.tab;
    if (!S.trace) { box.innerHTML = `<div class="callout">${S.traceLoading ? '<span class="spin"></span> loading every step for the focused cell…' : 'Click a selected cell to focus it; its full step trace appears here.'}</div>`; return; }
    const tr = S.trace, t = S.series?.time?.[S.hourIdx];
    const h = tr.time.indexOf(t);
    if (h < 0) { box.innerHTML = `<div class="callout">Trace window does not include ${W.esc(t)}.</div>`; return; }
    const stageName = Object.fromEntries(S.catalog.stages.map(s => [s.id, s.label]));
    const d = W.isoDate(t), zone = tr.cell.zone;
    let html = `<h3>Every step · cell ${tr.cell.point_id} (${W.esc(tr.cell.county)}, ${zone}) · ${W.esc(W.utcLabel(d))} = ${W.esc(W.clockLabel(d, zone))}</h3>
      <div class="hint" style="margin:-2px 0 6px">trace ${tr.origin === 'store-state' ? 're-run from the saved chunk state (matches saved values)' : 'cold start with spin-up'} · ${tr.elapsed_s}s · config ${tr.params.config_id}</div>
      <table class="t"><thead><tr><th>Step</th><th style="text-align:right">Value</th><th>Units</th><th>Formula · source</th></tr></thead><tbody>`;
    let stage = null;
    const altsBy = {}; Object.keys(tr.alts || {}).forEach(a => { const base = a.split('@')[0]; (altsBy[base] = altsBy[base] || []).push(a); });
    for (const s of S.catalog.steps) {
      if (!(s.feeds.includes(fdi) || (s.stage === 'intake' && s.id !== 'DSWRF_surface') || s.stage === 'convert' || s.stage === 'time')) continue;
      if (!(s.id in tr.steps)) continue;
      if (s.stage === 'time' && fdi !== 'FWI' && s.id !== 'lst_hour') continue;
      if (s.stage !== stage) { stage = s.stage; html += `<tr class="stage"><td colspan="4">${W.esc(stageName[stage])}</td></tr>`; }
      const v = tr.steps[s.id][h];
      const badge = s.res ? resBadge(s, d.getUTCFullYear()) : s.state ? '<span class="res state">state</span>' : '';
      const val = s.id.endsWith('_tier') ? `${v} <span class="r">${(s.id === 'ndvi_tier' ? NDVI_LABEL : TIER_LABEL)[v] || ''}</span>` : s.units === '0/1' ? (v ? 'yes' : 'no') : W.fmt(v, 3);
      html += `<tr class="${s.fdi && s.id === fdi ? 'fdi' : ''}"><td>${W.esc(s.label)}${badge}</td><td class="v">${val}</td><td class="r">${W.esc(s.units || '')}</td><td><div class="f">${W.esc(s.formula || '')}</div><div class="r">${W.esc(s.ref || '')}${s.inputs ? ' · ← ' + W.esc(s.inputs.join(', ')) : ''}</div></td></tr>`;
      for (const a of (altsBy[s.id] || [])) if (a in tr.steps) html += `<tr class="alt"><td>↳ ${W.esc(tr.alts[a])}</td><td class="v">${W.fmt(tr.steps[a][h], 3)}</td><td class="r">${W.esc(s.units || '')}</td><td class="r">comparison chain, same start state</td></tr>`;
    }
    html += '</tbody></table>';
    if (tr.notes?.length) html += `<div class="callout">${tr.notes.map(n => '• ' + W.esc(n)).join('<br>')}</div>`;
    html += `<div class="row"><button class="btn sm" id="trace-csv">Download full trace (CSV)</button></div>`;
    box.innerHTML = html;
    $('trace-csv').onclick = () => W.exportTraceCsv();
  };

  /* ================================================================ Time & daylight */
  async function renderDaylight(b) {
    const pid = S.focusPid ?? S.selOrder[0] ?? 16564;
    const t = S.series?.time?.[S.hourIdx] || (W.startTime() + ':00Z');
    const date = t.slice(0, 10);
    const days = Math.max(1, Math.min(31, Math.ceil(W.hours() / 24)));
    b.innerHTML = `<div class="sect"><h2>Time &amp; daylight</h2><div class="hint"><span class="spin"></span> loading…</div></div>`;
    let rep;
    try { rep = await W.api(`/api/timebase?pid=${pid}&date=${date}&days=${days}`); } catch (e) { b.innerHTML = `<div class="sect callout bad">${W.esc(e.message)}</div>`; return; }
    if (S.tab !== 'DAY') return;
    const fx = rep.fixed, lg = rep.legacy;
    const ki = (S.catalog.known_issues || []).find(k => k.id === 'daylight');
    const sec = document.createElement('div'); sec.className = 'sect';
    sec.innerHTML = `<h2>Time &amp; daylight <span class="tag fixed">FIXED</span></h2>
      <div class="callout"><b>What was wrong.</b> ${W.esc(ki?.text || '')}</div>
      <div class="callout good"><b>What the tool does now.</b> For every packet it computes the sun's position at that cell's own latitude and longitude at the packet's UTC instant. DMC and DC dry only when the sun is up there. No time zone is needed for that physics; time zones only change which hours make up a "day" (daily views) and what the clock shows.</div>
      <h3>Cell ${pid} · ${W.esc(rep.county)} County · ${rep.zone} (${W.esc(rep.tz)}) · ${rep.date} UTC</h3>
      <div id="clockbox"></div>
      <div class="row"><button class="btn sm ghost" id="dl-svg">SVG</button><button class="btn sm ghost" id="dl-png">PNG</button><span class="hint">yellow = sun above the horizon (used now) · hatched = hours the legacy test treated as daylight · red = disagreement</span></div>
      <table class="t" style="margin-top:6px"><thead><tr><th></th><th>Sunrise</th><th>Sunset</th><th>Note</th></tr></thead><tbody>
        <tr><td>Fixed — UTC</td><td class="v">${fx.sunrise_utc}</td><td class="v">${fx.sunset_utc}</td><td class="r">AORC stamps are UTC</td></tr>
        <tr><td>Fixed — local standard (UTC${rep.std_offset_h})</td><td class="v">${fx.sunrise_lst}</td><td class="v">${fx.sunset_lst}</td><td class="r">cffdrs-ng convention (no DST)</td></tr>
        <tr><td>Fixed — local clock (${rep.clock_abbr})</td><td class="v">${fx.sunrise_clock}</td><td class="v">${fx.sunset_clock}</td><td class="r">${rep.dst ? 'daylight saving in effect' : 'standard time'}</td></tr>
        <tr class="alt"><td>Legacy — solar hours at ${lg.latitude}°N</td><td class="v">${W.fmt(lg.sunrise_solar_h, 2)} h</td><td class="v">${W.fmt(lg.sunset_solar_h, 2)} h</td><td class="r">then compared with the UTC hour ✗</td></tr>
      </tbody></table>
      <div class="stats"><div class="stat"><b>${rep.misclassified_hours}/24</b><span>hours wrong on ${rep.date}</span></div>
        ${rep.window ? `<div class="stat"><b>${W.fmt(100 * rep.window.misclassified_hours / rep.window.hours, 1)}%</b><span>wrong over ${rep.window.days} days</span></div>` : ''}
        <div class="stat"><b>${W.fmt(fx.day_length_h, 2)} h</b><span>day length here</span></div></div>
      <h3>Effect on the moisture codes (focused cell, this window)</h3><div id="impact"></div>
      <h3>Time zones in the study area</h3>
      <table class="t"><thead><tr><th>Zone</th><th>Counties</th><th>Standard</th><th>Summer clock</th><th>Cells</th></tr></thead><tbody>
        ${(S.status.zones || []).map(z => `<tr><td>${z.label}</td><td>${z.counties.join(', ')}</td><td class="v">UTC${z.std_offset_h}</td><td class="v">UTC${z.std_offset_h + 1}</td><td class="v">${(S.grid.zones?.[z.label] || 0).toLocaleString()}</td></tr>`).join('')}
      </tbody></table>
      <div class="callout">Where time zones matter: <b>daily views</b> (FDI tabs › "daily max" or "daily @ obs hour") group hours into each cell's <i>local standard</i> day, so a Central-time cell's day starts an hour later in UTC than an Eastern one; the daily FWI convention is the 12:00 LST observation and NFDRS uses 13:00 LST. Display can switch between UTC, local clock (with DST) and local standard time in the Time window panel. Isle Royale belongs to Keweenaw County and is on Eastern time.</div>
      <div class="row"><button class="btn sm" id="dl-tz">Show time zones on the map</button><button class="btn sm" id="dl-daily">Open daily (LST) view</button></div>`;
    b.innerHTML = ''; b.appendChild(sec);
    const svg = W.clockStrip(rep); $('clockbox').appendChild(svg); S.daySvg = svg;
    $('dl-svg').onclick = () => W.download(`wup_daylight_${pid}_${rep.date}.svg`, W.svgString(svg), 'image/svg+xml');
    $('dl-png').onclick = async () => W.download(`wup_daylight_${pid}_${rep.date}.png`, await W.svgToPng(svg));
    $('dl-tz').onclick = () => { $('ly-tz').checked = true; W.setLayer('tz', true); };
    $('dl-daily').onclick = () => { S.agg = 'daymax'; S.tab = 'FWI'; W.renderTab(); };
    renderImpact();
  }
  function renderImpact() {
    const box = $('impact'); if (!box) return;
    const tr = S.trace;
    if (!tr || !tr.steps['DMC@daylight']) { box.innerHTML = `<div class="hint">${S.traceLoading ? '<span class="spin"></span> computing…' : 'Focus a cell and press Show / Explore live to see legacy vs fixed DMC, DC and FWI here.'}</div>`; return; }
    const times = tr.time.map(W.isoDate);
    const used = tr.params.daylight, other = used === 'solar' ? 'legacy UTC test' : 'sun position (fixed)';
    const mine = used === 'solar' ? 'sun position (fixed)' : 'legacy UTC test';
    box.innerHTML = '';
    for (const k of ['DMC', 'DC', 'FWI']) {
      const d = document.createElement('div'); box.appendChild(d);
      const a = tr.steps[k], o = tr.steps[k + '@daylight'];
      const diff = a.map((v, i) => v != null && o[i] != null ? v - o[i] : null).filter(v => v != null);
      W.drawLineChart(d, { times, height: 150, title: `${k} · max |difference| ${W.fmt(Math.max(...diff.map(Math.abs)), 2)}`,
        lines: [{ v: a, color: W.COLOR.FWI, w: 2, label: mine }, { v: o, color: W.COLOR.alt, w: 1.6, dash: '2 3', label: other }], current: tr.time.indexOf(S.series?.time?.[S.hourIdx]) });
    }
    const dl = tr.steps.daylight_fixed, lg = tr.steps.daylight_legacy;
    const mis = dl.reduce((n, v, i) => n + (v !== lg[i] ? 1 : 0), 0);
    box.insertAdjacentHTML('beforeend', `<div class="hint">${mis} of ${dl.length} packets in this window had a different daylight answer. Both chains start from the same state at the window start; the gap grows as the codes carry forward.</div>`);
  }
  W.renderImpact = renderImpact;

  /* ================================================================ Data flow */
  function renderFlow(b) {
    const cov = S.status.coverage;
    const sec = document.createElement('div'); sec.className = 'sect flow';
    sec.innerHTML = `<h2>How the data flows</h2><div class="hint">AORC → bricks → packets → resolution ladder → three index chains → saved results → map, charts and exports. Click a box for details.</div>
      <div id="flowbox"></div><div class="row"><button class="btn sm ghost" id="fl-svg">SVG</button><button class="btn sm ghost" id="fl-png">PNG</button></div>
      <div class="callout" id="flowdetail">Each FDI box shows its time resolution and how much of 1979→ is calculated for the active configuration.</div>
      <h3>How each index is used by this tool</h3>
      <table class="t"><thead><tr><th>Index</th><th>Time</th><th>Space</th><th>How it is computed here</th><th>Reported as</th></tr></thead><tbody>
      ${S.catalog.fdi_cards.map(c => `<tr><td><span class="dot" style="background:${W.COLOR[c.id]}"></span> <b>${c.id}</b></td><td>${W.esc(c.time)}</td><td class="r">${W.esc(c.space)}</td><td>${W.esc(c.how)}<div class="r">${W.esc(c.status)}</div></td><td>${W.esc(c.report)}</td></tr>`).join('')}
      </tbody></table>
      <h3>Known issues &amp; caveats</h3>
      ${S.catalog.known_issues.map(k => `<div class="callout ${k.status.startsWith('OPEN') ? 'bad' : ''}"><span class="tag ${k.status.startsWith('FIXED') || k.status.startsWith('RESOLVED') ? 'fixed' : k.status.startsWith('OPEN') ? 'open' : 'caveat'}">${W.esc(k.status)}</span> ${W.esc(k.text)}</div>`).join('')}
      <h3>Checked against official implementations</h3>
      <table class="t"><thead><tr><th>What</th><th>Against</th><th>Result</th><th></th></tr></thead><tbody>
      ${(S.catalog.validation || []).map(v => `<tr><td><b>${W.esc(v.what)}</b></td><td class="r">${W.esc(v.against)}</td><td>${W.esc(v.result)}</td><td><span class="tag ${v.verdict === 'match' ? 'fixed' : v.verdict === 'does not match' ? 'open' : 'caveat'}">${W.esc(v.verdict)}</span></td></tr>`).join('')}
      </tbody></table><div class="hint">Re-run with scripts/compare_with_cffdrs_ng.py, compare_daily_fwi_xclim.py, compare_with_nfdrs4.py · details in docs/validation/README.md</div>
      <h3>Existing fire-danger products and how they relate</h3>
      <table class="t"><thead><tr><th>Product</th><th>Indices</th><th>Grid · time</th><th>Period · weather</th><th>Compare with</th></tr></thead><tbody>
      ${(S.catalog.existing_tools || []).map(t => `<tr><td><b>${W.esc(t.name)}</b></td><td>${W.esc(t.indices)}</td><td class="r">${W.esc(t.grid)} · ${W.esc(t.time)}</td><td class="r">${W.esc(t.period)} · ${W.esc(t.weather)}</td><td>${W.esc(t.compare)}</td></tr>`).join('')}
      </tbody></table><div class="hint">This tool: 800 m AORC cells (weather effectively 2.5–12 km, precipitation ~4–12 km), hourly, 1979–2026. See docs/existing_tools_comparison.md.</div>
      <details><summary class="btn sm" style="display:inline-block;margin-top:8px">Detailed steps (${S.catalog.steps.length})</summary>
        ${S.catalog.stages.map(st => `<h3>${W.esc(st.label)}</h3><div class="hint">${W.esc(st.note)}</div><table class="t">${S.catalog.steps.filter(s => s.stage === st.id).map(s => `<tr><td>${W.esc(s.label)}</td><td class="f">${W.esc(s.formula)}</td><td class="r">${W.esc(s.ref)}</td></tr>`).join('')}</table>`).join('')}
      </details>`;
    b.appendChild(sec);
    const svg = W.flowDiagram({ computedPct: cov.percent_computed, perFdi: cov.per_fdi, bricks: S.grid.bricks.length, cells: S.grid.n_cells,
      source: S.status.source.name, cfgLabel: S.cfgLabel || '' });
    $('flowbox').appendChild(svg); S.flowSvg = svg;
    $('fl-svg').onclick = () => W.download('wup_data_flow.svg', W.svgString(svg), 'image/svg+xml');
    $('fl-png').onclick = async () => W.download('wup_data_flow.png', await W.svgToPng(svg));
    const detail = {
      aorc: 'NOAA Analysis of Record for Calibration v1.1: one Zarr store per year on AWS S3 (public). Grid 1/120° (~930 m N–S × ~640 m E–W here), hourly.',
      brick: `The S3 store is chunked 144 h × 128 × 256. Reading one cell costs the same download as its whole brick, so the tool fetches and caches whole bricks (clipped to the study area). ${S.grid.bricks.map(x => `${x.key}: ${x.n_cells.toLocaleString()} cells`).join(' · ')}.`,
      packet: 'One cell at one hour — the smallest unit the indices are computed on. Temperature/humidity are instantaneous at the top of the hour; precipitation is the hour ending at the stamp.',
      ladder: 'If a packet’s own value is missing, the tool uses the next most specific value and records which: same cell interpolated across ≤3 h (not precipitation) → 3×3 neighbourhood (2.4 km) → 7×7 (6 km) → brick-wide mean → unavailable (indices NaN, state held). See the Resolution tab for the percentages.',
      convert: 'Kelvin → °C/°F, specific humidity + pressure → relative humidity (Magnus), U/V → wind speed (km/h for FWI, mph for NFDRS).',
      sun: 'Solar position from the NOAA equations at the cell’s latitude/longitude and the packet’s UTC instant; daylight when the zenith < 90.833°. Replaces the legacy UTC-hour test (Time & daylight tab).',
      fm: 'Dead-fuel moisture responds to the hourly equilibrium moisture content with 1, 10, 100 and 1000-hour time lags and carries forward hour to hour (the legacy option used instantaneous EMC multipliers). Against NFDRS4 this is where ERC/BI differ: NFDRS 2016 uses the Nelson stick model (see Checked against official implementations).',
      ndvi: 'MODIS NDVI 250 m (MOD13Q1) aggregated to the cell; where a composite is missing, 500 m then 1 km. None before 2000-02-18 (locked decision) unless the opt-in climatology tier is enabled.',
      cffwis: 'Hourly FWI2025 chain, identical to NRCan’s cffdrs-ng reference: FFMC every hour with 0.5 mm canopy interception; DMC/DC dry only while the sun is up at the cell; ISI, BUI and FWI every hour. Alongside it, the classic daily FWI (Van Wagner 1987, identical to xclim) is updated once per cell at 12:00 LST from noon weather and the 24 h rain — the form daily products report.',
      nfdrs: 'NFDRS spread component, ERC and BI from the Argonne port with the chosen (or per-cell mapped) fuel model and constant live-fuel moisture — derived indices reported as percentiles.',
      fpi: 'Burgan Fire Potential Index: relative greenness from NDVI sets the live/dead split, the hourly 10-h fuel moisture sets dead-fuel dryness. The daily Burgan form (Tmax/RHmin) is available as an option.',
      store: 'Background jobs save the four indices for every packet of every computed brick-chunk, plus the chain state at the chunk’s start and end, so runs continue across restarts and any packet can be re-traced exactly.',
      out: 'Charts, map colours, traces and known-fire checks read the saved results (or compute live). Views, selections, fire-check reports, CSV, NetCDF, PNG/SVG and self-contained HTML snapshots can be saved and shared.',
      tz: 'Gogebic and Iron counties are on Central time (UTC−6 standard); the other four on Eastern (UTC−5). Used for local-time display and for daily (local standard day) aggregations.',
      fuelmap: 'Optional data/static/nfdrs_fuel_model.csv (point_id, fuel_model) gives each cell its own NFDRS model; otherwise the chosen model (G) is used everywhere.',
    };
    svg.querySelectorAll('g[data-id]').forEach(g => g.addEventListener('click', () => { const id = g.dataset.id;
      $('flowdetail').innerHTML = id.startsWith('fdi-') ? cardHtml(id.slice(4)) : W.esc(detail[id] || id); }));
  }

  /* ================================================================ Resolution */
  function renderResolution(b) {
    const sec = document.createElement('div'); sec.className = 'sect';
    const wx = S.series?.steps?.find(s => s.id === 'wx_tier')?.counts, nd = S.series?.steps?.find(s => s.id === 'ndvi_tier')?.counts;
    const tierBars = (counts, labels) => { const tot = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
      return W.barRows(Object.keys(labels).filter(k => counts[k]).map(k => ({ label: `${k} · ${labels[k]}`, value: 100 * counts[k] / tot, color: W.TIER_COLOR[k], text: `${W.fmt(100 * counts[k] / tot, 3)}%` })), { labelW: 150 }); };
    sec.innerHTML = `<h2>Resolution: most specific first, then the next best</h2>
      <div class="callout">Every packet is computed at the cell's own hour whenever the input exists. When it does not, the ladder below picks the next most specific value and records the tier, so you can see exactly how much of any result is native.</div>
      <h3>Inputs</h3><div style="overflow:auto"><table class="t"><thead><tr><th>Input</th><th>Grid used</th><th>Where the information comes from</th><th>Effective resolution</th><th>Fallback ladder</th><th>Feeds</th></tr></thead><tbody>
      ${S.catalog.resolution.map(r => `<tr><td><b>${W.esc(r.input)}</b></td><td class="r">${W.esc(r.grid)}</td><td class="r">${W.esc(r.source)}</td><td>${W.esc(r.effective)}</td><td class="r">${W.esc(r.ladder)}</td><td>${r.used_by.map(f => `<span class="dot" title="${f}" style="background:${W.COLOR[f]}"></span>`).join(' ')}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="hint">Source: AORC v1.1 methods document (NOAA OWP). Where that document is inconsistent (URMA start year; wind source after 2015) both readings are shown.</div>
      <h3>Weather tiers in the current result</h3><div id="wxbars">${wx ? '' : '<div class="hint">Show a selection first.</div>'}</div>
      <h3>NDVI tiers in the current result (FPI)</h3><div id="ndbars">${nd ? '' : '<div class="hint">—</div>'}</div>
      <h3>Weather tiers across all saved results</h3><div id="stwx"><div class="hint">see Coverage</div></div>
      <h3>Index resolution</h3>
      <table class="t"><thead><tr><th>Index</th><th>Time</th><th>Space</th></tr></thead><tbody>${S.catalog.fdi_cards.map(c => `<tr><td><b>${c.id}</b></td><td>${W.esc(c.time)}</td><td class="r">${W.esc(c.space)}</td></tr>`).join('')}</tbody></table>`;
    b.appendChild(sec);
    const wl = Object.fromEntries(Object.entries(S.catalog.wx_tiers).map(([k, v]) => [k, v.label]));
    if (wx) $('wxbars').appendChild(tierBars(wx, wl));
    if (nd) $('ndbars').appendChild(tierBars(nd, S.catalog.ndvi_tiers));
    W.api('/api/coverage?config=' + encodeURIComponent(JSON.stringify(S.cfg))).then(c => {
      if (S.tab !== 'RES') return; const el = $('stwx'); el.innerHTML = '';
      if (Object.keys(c.wx_tiers).length) el.appendChild(tierBars(c.wx_tiers, wl)); else el.innerHTML = '<div class="hint">Nothing saved yet for this configuration.</div>';
    }).catch(() => {});
  }

  /* ================================================================ Coverage */
  async function renderCoverage(b) {
    b.innerHTML = `<div class="sect"><h2>How much has been calculated</h2><div class="hint"><span class="spin"></span> counting…</div></div>`;
    let c; try { c = await W.api('/api/coverage?config=' + encodeURIComponent(JSON.stringify(S.cfg))); } catch (e) { b.innerHTML = `<div class="sect callout bad">${W.esc(e.message)}</div>`; return; }
    S.coverage = c; if (S.tab !== 'COV') return;
    const sec = document.createElement('div'); sec.className = 'sect';
    const years = Object.keys(c.per_month);
    sec.innerHTML = `<h2>How much has been calculated</h2>
      <div class="stats"><div class="stat"><b>${W.pct(c.percent_computed, 4)}</b><span>of all packets</span></div><div class="stat"><b>${c.packets_computed.toLocaleString()}</b><span>packets saved</span></div>
        <div class="stat"><b>${c.domain.packets.toLocaleString()}</b><span>packets in domain</span></div><div class="stat"><b>${c.n_chunks.toLocaleString()}</b><span>brick-chunks</span></div></div>
      <div class="hint">Domain: ${c.domain.cells.toLocaleString()} cells × ${c.domain.hours.toLocaleString()} hours (${c.domain.start.slice(0, 10)} → ${c.domain.end.slice(0, 13)}). FPI domain starts ${c.domain.fpi_start.slice(0, 10)}. Source ${W.esc(c.source)} · configuration ${c.config_id}.</div>
      <h3>Saved files</h3>
      <div class="hint">${c.files.n} NetCDF file${c.files.n === 1 ? '' : 's'} · ${W.bytes(c.files.bytes)}${c.files.bytes_per_packet ? ` · ${c.files.bytes_per_packet} bytes per packet (all four indices)` : ''}${c.files.full_record_estimate_bytes ? ` · whole 1979→ record at this rate ≈ ${W.bytes(c.files.full_record_estimate_bytes)}` : ''}<br>
        folder: <span class="mono">${W.esc(c.files.dir)}</span> — open any file in Panoply, QGIS or xarray (see README.txt in the results folder).</div>
      <div class="row"><label for="cv-year">One file per year</label><select id="cv-year">${Object.entries(c.per_year).filter(([, v]) => v > 0).map(([y]) => `<option>${y}</option>`).join('') || '<option value="">no saved years</option>'}</select>
        <button class="btn sm" id="cv-export">Export year (hourly + daily max)</button></div><div class="hint" id="cv-exported"></div>
      <h3>By index</h3><div id="cv-fdi"></div>
      <h3>By year and month <span class="hint" style="text-transform:none;letter-spacing:0">(click a month to open it)</span></h3><div id="cv-hm" style="overflow:auto"></div>
      <h3>By brick</h3><div id="cv-brick"></div>
      <h3>Resolution tiers (saved results)</h3><div id="cv-tier"></div>
      <h3>Continuity of the moisture codes</h3><div class="hint">${Object.entries(c.continuity).map(([k, v]) => `${k}: <b>${v}</b> chunk${v === 1 ? '' : 's'}`).join(' · ') || 'nothing saved yet'} — "continuous" chunks start from the previous chunk's saved state; "spin-up" chunks cold-started with a warm-up.</div>
      <h3>Configurations with saved data</h3><table class="t"><tbody>${c.configs.map(x => `<tr><td class="mono">${x.config_id}</td><td>${W.esc(x.label)}</td><td class="v">${x.packets.toLocaleString()}</td></tr>`).join('') || '<tr><td class="hint">none</td></tr>'}</tbody></table>
      <h3>Percentiles</h3><div class="hint" id="cv-clim"></div><div class="row"><button class="btn sm" id="cv-build">Build percentiles from saved results</button></div>
      <h3>Legacy batch record (data/output)</h3><div class="hint">${c.batch_months.length ? c.batch_months.join(', ') : 'none found'}</div>`;
    b.innerHTML = ''; b.appendChild(sec);
    $('cv-fdi').appendChild(W.barRows(W.FDI.map(f => ({ label: `${f} calculated`, value: c.per_fdi[f].percent_computed, color: W.COLOR[f], text: W.pct(c.per_fdi[f].percent_computed, 4) }))
      .concat(W.FDI.map(f => ({ label: `${f} with a value`, value: c.per_fdi[f].percent_valid, color: W.COLOR[f], text: W.pct(c.per_fdi[f].percent_valid, 4) }))), { labelW: 130 }));
    const mmax = Math.max(1e-9, ...years.flatMap(y => c.per_month[y].filter(v => v != null)));
    let hm = `<table class="hm"><tr><th></th>${'JFMAMJJASOND'.split('').map(m => `<th>${m}</th>`).join('')}</tr>`;
    for (const y of years.slice().reverse()) hm += `<tr><td class="yl">${y}</td>${c.per_month[y].map((v, m) => v == null ? '<td></td>' :
      `<td data-ym="${y}-${W.pad2(m + 1)}" title="${y}-${W.pad2(m + 1)}: ${W.pct(v, 3)} calculated" style="background:${v > 0 ? `rgba(57,135,229,${0.25 + 0.75 * Math.min(1, v / 100)})` : '#2a2e32'}">${v > 0 ? '•' : ''}</td>`).join('')}</tr>`;
    $('cv-hm').innerHTML = hm + '</table>';
    $('cv-hm').querySelectorAll('td[data-ym]').forEach(td => td.onclick = () => { $('t-date').value = td.dataset.ym + '-01'; $('t-hour').value = 0; W.onWindowChanged(); W.toast(`Window set to ${td.dataset.ym}. Press Show.`); });
    $('cv-brick').appendChild(W.barRows(S.grid.bricks.map(x => ({ label: `brick ${x.key}`, value: c.per_brick[x.key], text: W.pct(c.per_brick[x.key], 4) })), { labelW: 100 }));
    const wl = Object.fromEntries(Object.entries(S.catalog.wx_tiers).map(([k, v]) => [k, v.label]));
    const tot = Object.values(c.wx_tiers).reduce((a, b) => a + b, 0);
    if (tot) $('cv-tier').appendChild(W.barRows(Object.keys(c.wx_tiers).map(k => ({ label: `weather ${k} · ${wl[k]}`, value: 100 * c.wx_tiers[k] / tot, color: W.TIER_COLOR[k], text: W.fmt(100 * c.wx_tiers[k] / tot, 3) + '%' })), { labelW: 170 }));
    else $('cv-tier').innerHTML = '<div class="hint">—</div>';
    $('cv-export').onclick = async () => { const y = $('cv-year').value; if (!y) return;
      $('cv-export').disabled = true; $('cv-exported').innerHTML = '<span class="spin"></span> merging and building daily maxima…';
      try { const r = await W.api('/api/export/year', { json: { year: +y, config: S.cfg } });
        $('cv-exported').innerHTML = `<a href="${W.API}/api/exports/${r.hourly_id}">hourly ${y}</a> (${W.bytes(r.hourly_bytes)}) · <a href="${W.API}/api/exports/${r.daily_id}">daily max ${y}</a> (${W.bytes(r.daily_bytes)}) — also in the workspace exports folder`; }
      catch (e) { $('cv-exported').textContent = 'Error: ' + e.message; }
      $('cv-export').disabled = false; };
    const cl = S.status.climatology;
    $('cv-clim').textContent = cl.available ? `Built ${cl.meta.built_at} from ${cl.meta.source_months?.length || 0} months (${cl.meta.method}).` : 'Not built yet for this configuration.';
    $('cv-build').onclick = async () => { $('cv-build').disabled = true; W.status('building percentiles…', true);
      try { const r = await W.api('/api/climatology/build', { json: { config: S.cfg } }); W.toast(`Percentiles built from ${Object.values(r.vars)[0]?.toLocaleString() || 0} values per index.`); await W.refreshStatus(); W.renderTab(); }
      catch (e) { W.toast('Error: ' + W.esc(e.message)); } W.status(''); };
  }

  /* ================================================================ Jobs */
  function renderJobs(b) {
    const sec = document.createElement('div'); sec.className = 'sect';
    const bricks = W.selectedBricks();
    const st = W.startTime().slice(0, 10), en = new Date(W.isoDate(W.startTime()).getTime() + (W.hours() - 1) * 3600e3).toISOString().slice(0, 10);
    sec.innerHTML = `<h2>Background calculations</h2>
      <div class="callout">A job calculates whole bricks (the S3 download unit) over a period and saves every packet to the workspace. Jobs keep running while you explore, continue automatically when the explorer is reopened, and skip anything already saved.</div>
      <h3>New job</h3>
      <div class="row"><label>Bricks</label><span class="mono" id="jb-bricks">${bricks.length ? bricks.join(', ') : 'select cells first, or'}</span><button class="btn sm" id="jb-all">all 5 bricks</button></div>
      <div class="row"><label for="jb-start">From</label><input type="date" id="jb-start" value="${st}"><label for="jb-end" style="min-width:auto">to</label><input type="date" id="jb-end" value="${en}"></div>
      <div class="seg"><button class="btn sm" data-p="window">this window</button><button class="btn sm" data-p="season">fire season Apr–Oct</button><button class="btn sm" data-p="year">whole year</button><button class="btn sm" data-p="modis">2000 → end</button><button class="btn sm" data-p="all">full record</button></div>
      <div class="row"><label for="jb-name">Name</label><input type="text" id="jb-name" placeholder="optional" class="grow"></div>
      <div class="row"><span class="hint" id="jb-est"></span></div>
      <div class="row"><button class="btn primary" id="jb-go">Queue job</button><span class="hint">configuration: <span class="mono">${W.esc(S.cfgLabel || '')}</span></span></div>
      <h3>Jobs</h3><div id="joblist"></div>
      <h3>Settings</h3>
      <div class="row"><label for="st-workers">Parallel bricks</label><input type="number" id="st-workers" min="1" max="5" value="${S.status.settings.workers}"></div>
      <div class="row"><label for="st-spin">Job spin-up (h)</label><input type="number" id="st-spin" min="0" max="2160" step="24" value="${S.status.settings.job_spinup_hours}"></div>
      <div class="row"><label for="st-end">Data end (UTC)</label><input type="text" id="st-end" value="${S.status.settings.domain_end}" style="width:130px"></div>
      <div class="row"><label class="chk"><input type="checkbox" id="st-resume" ${S.status.settings.auto_resume ? 'checked' : ''}> resume unfinished jobs when the explorer starts</label></div>
      <div class="row"><button class="btn sm" id="st-save">Save settings</button><span class="hint">workspace: <span class="mono">${W.esc(S.status.workspace)}</span></span></div>`;
    b.appendChild(sec);
    let selBricks = bricks;
    const est = () => { const a = new Date($('jb-start').value + 'T00:00Z'), z = new Date($('jb-end').value + 'T23:00Z'); const h = Math.max(0, (z - a) / 3600e3 + 1);
      const cells = S.grid.bricks.filter(x => selBricks.includes(x.key)).reduce((s, x) => s + x.n_cells, 0);
      const rate = S.lastRate || 180000; $('jb-est').textContent = selBricks.length ? `${selBricks.join(', ')} · ${cells.toLocaleString()} cells × ${h.toLocaleString()} h = ${(cells * h).toLocaleString()} packets · roughly ${fmtDur(cells * h / rate)} of compute${S.status.synthetic ? '' : ' plus S3 download time'}` : ''; };
    $('jb-all').onclick = () => { selBricks = S.grid.bricks.map(x => x.key); $('jb-bricks').textContent = selBricks.join(', '); est(); };
    sec.querySelectorAll('[data-p]').forEach(x => x.onclick = () => { const y = $('jb-start').value.slice(0, 4); const de = S.status.settings.domain_end.slice(0, 10);
      const p = x.dataset.p; if (p === 'season') { $('jb-start').value = `${y}-04-01`; $('jb-end').value = `${y}-10-31`; } if (p === 'year') { $('jb-start').value = `${y}-01-01`; $('jb-end').value = `${y}-12-31`; }
      if (p === 'modis') { $('jb-start').value = '2000-01-01'; $('jb-end').value = de; } if (p === 'all') { $('jb-start').value = '1979-02-01'; $('jb-end').value = de; }
      if (p === 'window') { $('jb-start').value = st; $('jb-end').value = en; } est(); });
    ['jb-start', 'jb-end'].forEach(id => $(id).onchange = est); est();
    $('jb-go').onclick = async () => { if (!selBricks.length) return W.toast('Select cells (or press "all 5 bricks") first.');
      try { const j = await W.api('/api/jobs', { json: { bricks: selBricks, start: $('jb-start').value + 'T00', end: $('jb-end').value + 'T23', config: S.cfg, name: $('jb-name').value || undefined } });
        W.toast(`Job #${j.id} queued: ${j.tasks_total} brick-chunks, ${j.packets_total.toLocaleString()} packets.`); W.pollJobs(true); } catch (e) { W.toast('Error: ' + W.esc(e.message)); } };
    $('st-save').onclick = async () => { try { await W.api('/api/settings', { json: { workers: +$('st-workers').value, job_spinup_hours: +$('st-spin').value, domain_end: $('st-end').value, auto_resume: $('st-resume').checked } }); await W.refreshStatus(); W.toast('Settings saved.'); } catch (e) { W.toast('Error: ' + W.esc(e.message)); } };
    W.drawJobList();
  }
  function fmtDur(s) { return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} days`; }
  W.fmtDur = fmtDur;
  W.drawJobList = function () {
    const el = $('joblist'); if (!el) return;
    if (!S.jobs.length) { el.innerHTML = '<div class="hint">No jobs yet.</div>'; return; }
    el.innerHTML = S.jobs.map(j => `<div class="job"><div class="top"><span class="st ${j.status}">${j.status}</span><b class="grow">#${j.id} ${W.esc(j.name)}</b><span class="mono">${W.fmt(j.percent, 1)}%</span></div>
      <div class="bar" style="margin:6px 0"><i style="width:${Math.min(100, j.percent)}%"></i></div>
      <div class="hint">${j.bricks.join(', ')} · ${j.start.slice(0, 13)} → ${j.end.slice(0, 13)} · ${j.tasks_done}/${j.tasks_total} chunks · ${(j.packets_done || 0).toLocaleString()} / ${j.packets_total.toLocaleString()} packets${j.eta_s != null ? ` · ~${fmtDur(j.eta_s)} left` : ''} · config ${j.config_id}${j.error ? `<br><span style="color:var(--bad)">${W.esc(j.error)}</span>` : ''}</div>
      <div class="row">${['running', 'queued', 'retrying'].includes(j.status) ? `<button class="btn sm" data-a="pause" data-id="${j.id}">Pause</button>` : ''}${['paused', 'error', 'cancelled', 'retrying'].includes(j.status) ? `<button class="btn sm" data-a="resume" data-id="${j.id}">${j.status === 'retrying' ? 'Retry now' : 'Resume'}</button>` : ''}
        ${['running', 'queued', 'paused', 'retrying'].includes(j.status) ? `<button class="btn sm danger" data-a="cancel" data-id="${j.id}">Cancel</button>` : `<button class="btn sm ghost" data-a="delete" data-id="${j.id}">Remove</button>`}</div></div>`).join('');
    el.querySelectorAll('[data-a]').forEach(x => x.onclick = async () => { const a = x.dataset.a, id = x.dataset.id;
      try { if (a === 'delete') await W.api(`/api/jobs/${id}`, { method: 'DELETE' }); else await W.api(`/api/jobs/${id}/${a}`, { method: 'POST' }); } catch (e) { W.toast(W.esc(e.message)); } W.pollJobs(true); });
  };

  /* ================================================================ Save & share */
  async function renderLibrary(b) {
    const sec = document.createElement('div'); sec.className = 'sect';
    sec.innerHTML = `<h2>Save &amp; share</h2>
      <h3>This view</h3>
      <div class="row"><button class="btn" id="lb-link">Copy share link</button><button class="btn" id="lb-save">Save view…</button><button class="btn" id="lb-snap">Download snapshot (HTML)</button></div>
      <div class="hint">A share link reopens the same selection, window, configuration and tab in anyone's copy of the explorer. A snapshot is one self-contained HTML file (map image, the four charts, data-flow and daylight diagrams, the step trace, and the data) that opens anywhere without the tool — and can be imported back.</div>
      <h3>Data</h3>
      <div class="row"><button class="btn sm" id="lb-csv">Series CSV</button><button class="btn sm" id="lb-trace">Trace CSV</button><button class="btn sm" id="lb-geo">Selection GeoJSON</button><button class="btn sm" id="lb-nc">NetCDF</button><button class="btn sm" id="lb-png">Map PNG</button></div>
      <h3>Selection</h3>
      <div class="row"><input type="text" id="lb-selname" placeholder="name for the current selection" class="grow"><button class="btn sm" id="lb-selsave">Save selection</button></div>
      <div class="list" id="lb-sels"></div>
      <h3>Saved views</h3><div class="list" id="lb-sess"></div>
      <h3>Files saved in the workspace</h3><div class="list" id="lb-exp"></div>
      <h3>Import</h3><div class="hint">GeoJSON polygons (cells whose centres fall inside are selected), a saved selection or view (.json), or a snapshot (.html) — use the Import button in the Select area panel, or drop a file anywhere on the map.</div>`;
    b.appendChild(sec);
    $('lb-link').onclick = W.copyShareLink; $('lb-save').onclick = W.saveSessionPrompt; $('lb-snap').onclick = W.snapshot;
    $('lb-csv').onclick = W.exportSeriesCsv; $('lb-trace').onclick = W.exportTraceCsv; $('lb-geo').onclick = W.exportSelectionGeoJSON;
    $('lb-nc').onclick = W.exportNetCDF; $('lb-png').onclick = W.mapPng;
    $('lb-selsave').onclick = async () => { if (!S.sel.size) return W.toast('Nothing selected.');
      const name = $('lb-selname').value || S.selName || `selection ${new Date().toISOString().slice(0, 16)}`;
      await W.api('/api/selections', { json: { name, selection: { mode: 'cells', point_ids: [...S.sel] } } }); W.toast(`Saved selection “${W.esc(name)}”.`); W.loadSavedSelections(); renderLibrary2(); };
    renderLibrary2();
  }
  async function renderLibrary2() {
    const [sels, sess, exps] = await Promise.all([W.api('/api/selections'), W.api('/api/sessions'), W.api('/api/exports')]).catch(() => [[], [], []]);
    if (!$('lb-sels')) return;
    $('lb-sels').innerHTML = sels.map(s => `<div class="it"><span class="nm">${W.esc(s.name)}</span><span class="hint">${s.n_cells.toLocaleString()} cells</span><button class="btn sm" data-sl="${s.id}">Load</button><button class="btn sm ghost" data-sg="${s.id}">GeoJSON</button><button class="btn sm ghost" data-sd="${s.id}">✕</button></div>`).join('') || '<div class="hint">none</div>';
    $('lb-sess').innerHTML = sess.map(s => `<div class="it"><span class="nm">${W.esc(s.name)}</span><span class="hint">${s.updated.slice(0, 16)}</span><button class="btn sm" data-vl="${s.id}">Open</button><button class="btn sm ghost" data-vk="${s.id}">Link</button><button class="btn sm ghost" data-vd="${s.id}">✕</button></div>`).join('') || '<div class="hint">none</div>';
    $('lb-exp').innerHTML = exps.map(e => `<div class="it"><span class="nm">${W.esc(e.name)}</span><span class="hint">${W.bytes(e.bytes)}</span><a class="btn sm" href="${W.API}/api/exports/${e.id}">Download</a></div>`).join('') || '<div class="hint">none</div>';
    const on = (attr, f) => document.querySelectorAll(`[${attr}]`).forEach(x => x.onclick = () => f(x.getAttribute(attr)));
    on('data-sl', async id => { const s = await W.api(`/api/selections/${id}`); W.selectMany(s.payload.point_ids, s.name); W.fitSelection(); });
    on('data-sg', async id => { const s = await W.api(`/api/selections/${id}`); W.download(`${s.name}.geojson`, JSON.stringify(W.selectionGeoJSON(s.payload.point_ids, s.name)), 'application/geo+json'); });
    on('data-sd', async id => { await W.api(`/api/selections/${id}`, { method: 'DELETE' }); W.loadSavedSelections(); renderLibrary2(); });
    on('data-vl', async id => { const s = await W.api(`/api/sessions/${id}`); await W.applyState(s.state); });
    on('data-vk', async id => { const s = await W.api(`/api/sessions/${id}`); await W.copyShareLink(s.state); });
    on('data-vd', async id => { await W.api(`/api/sessions/${id}`, { method: 'DELETE' }); renderLibrary2(); });
  }
  W.renderLibrary2 = renderLibrary2;
})();
