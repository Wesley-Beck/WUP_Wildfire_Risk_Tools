/* Power lines and fire danger: line layers on the map, the corridor-vs-region analysis, ranked lines, trend maps */
'use strict';
(function () {
  const S = W.S;
  S.lines = { layers: null, layer: 'wup_transmission_hifld_2024', fc: null, analyses: [], summary: null, index: 'FWI',
    metric: 'days_p95', cellKey: 'days_p95_mean', ymetric: 'days_p90', task: null, msg: '', shown: false };
  const IDX = ['FWI', 'FWI_daily', 'ERC', 'BI', 'FPI'];
  const IDX_LABEL = { FWI: 'FWI hourly (daily max)', FWI_daily: 'FWI classic daily', ERC: 'ERC', BI: 'BI', FPI: 'FPI' };
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  /* yearly metrics: label + group (the order shown) */
  const YEARLY = {
    year_max: 'year: highest daily maximum', year_min: 'year: lowest daily minimum', year_max_doy: 'year: date of the maximum',
    year_min_doy: 'year: date of the minimum',
    season_max: 'season (Mar–Nov): highest daily maximum', season_min: 'season: lowest daily minimum', season_mean: 'season: mean daily maximum',
    min_season_mean: 'season: mean daily minimum (overnight recovery)', diurnal_range_mean: 'season: mean daily range (max − min)',
    spring_peak: 'spring peak (smoothed max)', summer_trough: 'summer trough (smoothed max)', fall_peak: 'fall peak (smoothed max)',
    spring_peak_doy: 'date of the spring peak', summer_trough_doy: 'date of the summer trough', fall_peak_doy: 'date of the fall peak',
    spring_peak_of_min: 'spring peak of the daily minimum', summer_trough_of_min: 'summer trough of the daily minimum', fall_peak_of_min: 'fall peak of the daily minimum',
    smoothed_max: 'season high of the smoothed max', smoothed_min: 'season low of the smoothed max',
    days_p90: 'days above the 90th percentile', days_p95: 'days above the 95th', days_p99: 'days above the 99th',
    days_min_p90: 'nights that do not recover (daily min above its 90th pct)',
    first_p90_doy: 'first day above the 90th', last_p90_doy: 'last day above the 90th', span_p90: 'first-to-last span (days)',
    core_first_p90_doy: 'robust season start (10% of high days)', core_last_p90_doy: 'robust season end (90%)', core_span_p90: 'robust season length (days)',
    any_p90: 'share of cells with any day above the 90th',
    peak_hour_lst: 'hour of the daily peak (LST)', trough_hour_lst: 'hour of the daily low (LST)' };
  MONTHS.forEach((m, i) => { const k = String(i + 1).padStart(2, '0'); YEARLY[`m${k}_max`] = `${m}: highest daily maximum`; YEARLY[`m${k}_min`] = `${m}: lowest daily minimum`; });
  const isDoy = k => /_doy$/.test(k);
  const LINE_METRICS = {
    days_p95: 'days/yr above the 95th pct', days_p90: 'days/yr above the 90th pct', days_p99: 'days/yr above the 99th pct',
    'days_p95.last_10y': 'days/yr above 95th, last 10 years', 'days_p90.tau': 'trend (Kendall τ): days above 90th',
    year_max: 'year maximum', year_min: 'year minimum', record_max: 'record maximum', record_min: 'record minimum',
    'year_max.trend_per_decade': 'trend: year max per decade', spring_peak: 'spring peak', fall_peak: 'fall peak', summer_trough: 'summer trough',
    span_p90: 'high-danger span (days)', first_p90_doy: 'first high-danger day', diurnal_range_mean: 'mean daily range',
    peak_hour_lst: 'hour of the daily peak (LST)', trough_hour_lst: 'hour of the daily low (LST)', days_min_p90: 'nights that do not recover (days/yr)',
    'days_min_p90.tau': 'trend (Kendall τ): nights that do not recover' };
  const CELL_FIELDS = {
    days_p95_mean: 'days/yr above the 95th percentile', days_p90_mean: 'days/yr above the 90th percentile', days_p99_mean: 'days/yr above the 99th',
    days_min_p90_mean: 'nights that do not recover (days/yr)', record_max: 'record maximum (whole period)', record_min: 'record minimum',
    record_max_year: 'year of the record maximum', year_max_mean: 'mean year maximum', year_min_mean: 'mean year minimum',
    season_mean_mean: 'mean daily max, Mar–Nov', min_season_mean_mean: 'mean daily min, Mar–Nov', diurnal_range_mean_mean: 'mean daily range',
    spring_peak_mean: 'spring peak', summer_trough_mean: 'summer trough', fall_peak_mean: 'fall peak',
    spring_peak_doy_median: 'date of the spring peak (day of year)', fall_peak_doy_median: 'date of the fall peak (day of year)',
    core_span_p90_mean: 'robust high-danger season length (days)', peak_hour_lst_median: 'hour of the daily peak (LST)',
    trough_hour_lst_median: 'hour of the daily low (LST)',
    days_p90_tau: 'trend (τ): days above the 90th', days_p95_tau: 'trend (τ): days above the 95th', days_min_p90_tau: 'trend (τ): nights that do not recover',
    year_max_tau: 'trend (τ): year maximum', year_min_tau: 'trend (τ): year minimum', season_mean_tau: 'trend (τ): mean daily max',
    first_p90_doy_tau: 'trend (τ): first high-danger day', span_p90_tau: 'trend (τ): span of the high-danger season',
    days_p90_trend: 'Sen slope: days above 90th per decade', year_max_trend: 'Sen slope: year max per decade',
    days_p90_sig: 'significant trend in days above 90th (FDR 10%): +1 rising, −1 falling',
    year_max_sig: 'significant trend in the year maximum (FDR 10%)', days_min_p90_sig: 'significant trend in nights that do not recover' };
  MONTHS.forEach((m, i) => { const k = String(i + 1).padStart(2, '0'); CELL_FIELDS[`m${k}_max_mean`] = `${m}: mean highest daily max`; CELL_FIELDS[`m${k}_min_mean`] = `${m}: mean lowest daily min`; });
  const DIVERGING = k => /_(trend|tau|sig)$/.test(k);
  const cfgQ = () => 'config=' + encodeURIComponent(JSON.stringify(S.cfg || {}));
  const doyLabel = d => d == null ? '—' : new Date(Date.UTC(2001, 0, 1 + Math.round(d))).toISOString().slice(5, 10);

  W.loadLines = async function () {
    try { S.lines.layers = (await W.api('/api/lines/layers')).layers; } catch (e) { S.lines.layers = []; }
    try { S.lines.analyses = (await W.api('/api/lines/analyses')).analyses; } catch (e) { S.lines.analyses = []; }
    if (!S.lines.layers.find(l => l.id === S.lines.layer) && S.lines.layers.length) S.lines.layer = S.lines.layers[0].id;
    await loadSummary();
    if (S.tab === 'LINES') W.renderTab();
  };

  function analysisId() {
    const src = S.status?.source?.active || '';
    const a = S.lines.analyses.filter(x => x.layer === S.lines.layer);
    return (a.find(x => x.source === src) || a[0])?.id || null;
  }

  async function loadSummary() {
    const aid = analysisId();
    S.lines.summary = null;
    if (!aid) return;
    try { S.lines.summary = await W.api(`/api/lines/analyses/${encodeURIComponent(aid)}`); } catch (e) { S.lines.summary = null; }
  }

  async function showLayer(on) {
    const m = W.map;
    S.lines.shown = on;
    if (!on) { ['lines-casing', 'lines-line'].forEach(l => m.getLayer(l) && m.setLayoutProperty(l, 'visibility', 'none')); return; }
    S.lines.fc = await W.api(`/api/lines/layers/${encodeURIComponent(S.lines.layer)}`);
    paintLines();
  }

  function metricOf(row) {
    const k = S.lines.metric, v = S.lines.index;
    return row ? row[`${v}.${k}`] : null;
  }

  function paintLines() {
    const m = W.map; if (!S.lines.fc) return;
    const rows = Object.fromEntries((S.lines.summary?.features || []).map(r => [r.id, r]));
    const vals = S.lines.fc.features.map(f => metricOf(rows[f.properties.id])).filter(x => x != null && isFinite(x));
    const lo = vals.length ? Math.min(...vals) : 0, hi = vals.length ? Math.max(...vals) : 1;
    const data = { type: 'FeatureCollection', features: S.lines.fc.features.map(f => {
      const x = metricOf(rows[f.properties.id]);
      return { type: 'Feature', geometry: f.geometry, properties: { id: f.properties.id, name: f.properties.name || '', kv: f.properties.voltage_kv ?? '',
        owner: f.properties.owner || '', km: f.properties.length_km, rank: rows[f.properties.id]?.rank ?? '', x: x ?? '',
        t: x == null || !isFinite(x) ? -1 : (hi > lo ? (x - lo) / (hi - lo) : 0.5) } }; }) };
    if (m.getSource('lines')) m.getSource('lines').setData(data);
    else {
      m.addSource('lines', { type: 'geojson', data });
      m.addLayer({ id: 'lines-casing', type: 'line', source: 'lines', layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#111214', 'line-width': ['interpolate', ['linear'], ['zoom'], 7, 4, 11, 8], 'line-opacity': 0.9 } });
      m.addLayer({ id: 'lines-line', type: 'line', source: 'lines', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: {
        'line-color': ['case', ['<', ['get', 't'], 0], '#e8e6df', ['interpolate', ['linear'], ['get', 't'], 0, W.RAMP[0], 0.4, W.RAMP[2], 0.7, W.RAMP[3], 1, W.RAMP[5]]],
        'line-width': ['interpolate', ['linear'], ['zoom'], 7, 2, 11, 4.5], 'line-opacity': 0.95 } });
      const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
      m.on('mousemove', 'lines-line', e => { const p = e.features[0].properties; m.getCanvas().style.cursor = 'pointer';
        pop.setLngLat(e.lngLat).setHTML(`<b>${W.esc(p.name || p.id)}</b>${p.kv !== '' ? ` · ${p.kv} kV` : ''}${p.owner ? `<br>${W.esc(p.owner)}` : ''}
          <br>${W.fmt(+p.km, 1)} km${p.rank !== '' ? ` · rank ${p.rank}` : ''}${p.x !== '' ? `<br>${W.esc(LINE_METRICS[S.lines.metric] || S.lines.metric)}: <b>${W.fmt(+p.x, 2)}</b>` : ''}`).addTo(m); });
      m.on('mouseleave', 'lines-line', () => { pop.remove(); m.getCanvas().style.cursor = ''; });
      m.on('click', 'lines-line', e => { S.lines.focus = e.features[0].properties.id; S.tab = 'LINES'; W.renderTab();
        const tr = document.querySelector(`tr[data-lid="${CSS.escape(S.lines.focus)}"]`); if (tr) tr.scrollIntoView({ block: 'center' }); });
    }
    ['lines-casing', 'lines-line'].forEach(l => m.setLayoutProperty(l, 'visibility', 'visible'));
    if (vals.length) W.legend(`${IDX_LABEL[S.lines.index]} · ${LINE_METRICS[S.lines.metric] || S.lines.metric} (lines)`, lo, hi, '');
  }

  async function paintCells() {
    const aid = analysisId(); if (!aid) return;
    try {
      const f = await W.api(`/api/lines/analyses/${encodeURIComponent(aid)}/cells?key=${encodeURIComponent(S.lines.index + '.' + S.lines.cellKey)}`);
      let lo = f.lo, hi = f.hi;
      const div = DIVERGING(S.lines.cellKey);
      if (div) { const a = Math.max(Math.abs(lo), Math.abs(hi)) || 1; lo = -a; hi = a; }
      W.paintValues(f.point_ids, f.values, lo, hi, false, div);
      W.legend(`${IDX_LABEL[S.lines.index]} · ${CELL_FIELDS[S.lines.cellKey]} (cells)`, lo, hi, div ? 'blue = falling · grey = no change · red = rising' : '', false, div);
      S.lines.cellsShown = true;
    } catch (e) { W.toast('Error: ' + W.esc(e.message)); }
  }

  async function runAnalysis() {
    const idx = [...document.querySelectorAll('[data-lidx]:checked')].map(x => x.dataset.lidx);
    const y0 = +$('ln-y0').value || null, y1 = +$('ln-y1').value || null;
    try {
      const t = await W.api('/api/lines/analyse', { json: { layer: S.lines.layer, config: S.cfg, indices: idx.length ? idx : null,
        years: y0 && y1 ? [y0, y1] : null } });
      S.lines.task = t.id; S.lines.msg = `<span class="spin"></span> analysing… (task #${t.id}, progress also in Data › Tasks)`;
      W.renderTab(); pollTask();
    } catch (e) { W.toast('Error: ' + W.esc(e.message)); }
  }

  async function pollTask() {
    if (!S.lines.task) return;
    let t;
    try { t = (await W.api('/api/tasks')).tasks.find(x => x.id === S.lines.task); } catch (e) { setTimeout(pollTask, 3000); return; }
    if (!t) { S.lines.task = null; return; }
    if (t.status === 'done') {
      S.lines.task = null; S.lines.msg = `Analysis finished: ${t.result?.features ?? ''} lines, ${t.result?.corridor_cells ?? ''} corridor cells, ${t.result?.years ?? ''} years.`;
      await W.loadLines(); if (S.lines.shown) paintLines(); return;
    }
    if (t.status === 'error' || t.status === 'cancelled') { S.lines.task = null; S.lines.msg = `<span style="color:#f0a35e">${W.esc(t.status)}: ${W.esc(t.error || '')}</span>`; if (S.tab === 'LINES') W.renderTab(); return; }
    S.lines.msg = `<span class="spin"></span> ${W.esc(t.note || 'analysing')} · ${t.total ? Math.round(100 * t.done / t.total) : 0}%`;
    const el = $('ln-msg'); if (el) el.innerHTML = S.lines.msg;
    setTimeout(pollTask, 2000);
  }

  /* a trend: Sen slope per decade, Kendall tau, and p corrected for serial correlation (Hamed-Rao) when available */
  function trendCell(t) {
    if (!t || (t.per_decade == null && t.tau == null)) return '<span class="r">—</span>';
    const pv = t.p_hr ?? t.p, sig = pv != null && pv < 0.05;
    const dir = t.tau != null ? (t.tau > 0 ? '↑' : t.tau < 0 ? '↓' : '·') : '';
    return `${dir} ${t.per_decade != null ? (t.per_decade > 0 ? '+' : '') + W.fmt(t.per_decade, 2) + '/dec' : ''} <small class="r">τ ${W.fmt(t.tau, 2)} · p ${W.fmt(pv, 3)}${sig ? ' ✓' : ''}</small>`;
  }
  const GROUPS = [
    ['Year (global)', ['year_max', 'year_max_doy', 'year_min', 'year_min_doy']],
    ['Season, Mar–Nov', ['season_max', 'season_min', 'season_mean', 'min_season_mean', 'diurnal_range_mean']],
    ['Seasonal peaks and troughs (15-day smoothed)', ['spring_peak', 'spring_peak_doy', 'summer_trough', 'summer_trough_doy', 'fall_peak', 'fall_peak_doy',
      'spring_peak_of_min', 'summer_trough_of_min', 'fall_peak_of_min']],
    ['How often it is high', ['days_p90', 'days_p95', 'days_p99', 'days_min_p90', 'any_p90']],
    ['High-danger season', ['first_p90_doy', 'last_p90_doy', 'span_p90', 'core_first_p90_doy', 'core_last_p90_doy', 'core_span_p90']],
    ['Hour of day', ['peak_hour_lst', 'trough_hour_lst']]];

  W.renderLines = function (b) {
    const L = S.lines, sm = L.summary, lay = (L.layers || []).find(l => l.id === L.layer);
    const ix = sm?.indices?.[L.index];
    const aid = analysisId();
    let html = `<div class="sect"><h2>Power lines and fire danger</h2>
      <div class="callout">Where do the lines run through the places, seasons and years with the highest fire danger, and is it increasing?
      For each index, every cell gets the <b>maximum and minimum at every timescale</b> (hour of day, day, month, season with its spring/fall peaks and summer trough, year, whole record),
      how often danger is high, when the high-danger season starts and ends, and the <b>trend</b> of each over the years (Theil–Sen, Mann–Kendall).
      The lines' cells (km-weighted) are compared with <b>the rest of the mainland WUP</b>, and with the same lines shifted to random places.
      This is fire-<i>weather</i> danger along the lines: not ignition probability, not consequence.
      ${S.status?.synthetic ? '<br><b style="color:#f0c27a">Demo mode: synthetic weather — the numbers say nothing about the real lines.</b>' : ''}</div>
      <div class="row" style="gap:6px;flex-wrap:wrap;align-items:center">
        <label>Layer <select id="ln-layer">${(L.layers || []).map(l => `<option value="${W.esc(l.id)}" ${l.id === L.layer ? 'selected' : ''}>${W.esc(l.name || l.id)} (${W.fmt(l.km, 0)} km)</option>`).join('')}</select></label>
        <label class="chk"><input type="checkbox" id="ln-show" ${L.shown ? 'checked' : ''}> show on map</label>
        <label class="btn sm" title="GeoJSON (WGS84), a zipped shapefile, or with pyogrio a GeoPackage / zipped .gdb">Import lines…<input type="file" id="ln-import" accept=".geojson,.json,.zip,.gpkg" hidden></label>
        <button class="btn sm" id="ln-dl" title="Fetch current transmission lines with voltage and owner (needs internet)">Download current lines…</button>
        ${lay?.origin === 'workspace' ? '<button class="btn sm ghost" id="ln-del">Delete layer</button>' : ''}
      </div>
      ${lay ? `<div class="hint">${W.esc(lay.source || '')}${lay.attribution ? ` · © ${W.esc(lay.attribution)}` : ''}<br>${lay.n_features} features · ${W.fmt(lay.km, 1)} km ·
        voltage known for ${lay.with_voltage ?? 0}, owner for ${lay.with_owner ?? 0}${lay.voltages_kv?.length ? ` · ${lay.voltages_kv.join(', ')} kV` : ''}${lay.notes ? '<br>' + W.esc(lay.notes) : ''}</div>` : ''}
      ${(L.layers || []).length > 1 ? `<div class="row" style="gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px">
        <label title="Owner/voltage missing in this layer are taken from another layer that follows the same routes (≥ 50% of the line within 150 m)">Fill gaps from
        <select id="ln-other">${L.layers.filter(l => l.id !== L.layer).map(l => `<option value="${W.esc(l.id)}">${W.esc(l.name || l.id)}</option>`).join('')}</select></label>
        <button class="btn sm" id="ln-cross">Cross-check → new layer</button></div>` : ''}
      ${lay?.cross_check ? `<div class="hint">Cross-check: owner filled for ${lay.cross_check.owner_filled}, voltage for ${lay.cross_check.voltage_filled}; voltage disagrees on ${lay.cross_check.voltage_disagrees} line(s). ${W.esc(lay.cross_check.rule)}.</div>` : ''}
      <div id="ln-dlbox"></div>
      <h3>Analyse</h3>
      <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
        ${IDX.map(v => `<label class="chk"><input type="checkbox" data-lidx="${v}" ${!sm || sm.indices?.[v] ? 'checked' : ''}> ${IDX_LABEL[v]}</label>`).join('')}
        <label>years <input id="ln-y0" type="number" style="width:64px" placeholder="first"> – <input id="ln-y1" type="number" style="width:64px" placeholder="last"></label>
        <button class="btn" id="ln-run" ${L.task ? 'disabled' : ''}>Analyse lines</button>
      </div>
      <div class="hint">Uses every saved year of the active source and configuration (a year counts for a cell when 90% of its March–November days are saved).
        Trends need ≥ 10 such years, so run the full record first (Run_Full_Record.bat). Daily maxima are cached per year in the workspace, so a re-run is quick.</div>
      <div id="ln-msg" class="hint">${L.msg || ''}</div>`;
    if (sm && ix && !ix.skipped) {
      const cor = sm.corridor, tr = ix.trends || {};
      const fmtk = (k, x) => isDoy(k) ? doyLabel(x) : (k === 'any_p90' ? W.pct(100 * (x ?? 0), 0) : W.fmt(x, 2));
      const row = k => `<tr><td>${W.esc(YEARLY[k] || k)}</td><td class="v">${fmtk(k, ix.corridor[k])}</td><td class="v">${fmtk(k, ix.region[k])}</td>
            <td>${trendCell(tr[k]?.corridor)}</td><td>${trendCell(tr[k]?.region)}</td><td>${trendCell(tr[k]?.corridor_minus_region)}</td></tr>`;
      const rec = ix.record || {};
      const nt = ix.null_test || {};
      html += `<h3>Results · ${W.esc(sm.layer?.name || '')} · ${W.esc(sm.source)} · ${sm.years[0]}–${sm.years[sm.years.length - 1]} <small class="r">(${W.esc(sm.created)})</small></h3>
        <div class="row" style="gap:6px;flex-wrap:wrap">${IDX.filter(v => sm.indices[v]).map(v => `<button class="btn sm ${v === L.index ? '' : 'ghost'}" data-lix="${v}">${IDX_LABEL[v]}</button>`).join('')}</div>
        <div class="hint">Corridor: ${cor.cells.toLocaleString()} cells (${W.pct(100 * cor.share_of_region_cells, 1)} of the mainland), ${W.fmt(cor.km_in_cells, 1)} km of line inside study cells
          (${W.fmt(cor.km_outside_cells, 1)} km over water or outside). Comparator: the other ${(cor.region_cells || 0).toLocaleString()} mainland cells.
          Thresholds of the daily maximum (all mainland cells, Mar–Nov, ${(ix.threshold_years || sm.reference_years).join('–')}):
          90th <b>${W.fmt(ix.thresholds.p90)}</b> · 95th <b>${W.fmt(ix.thresholds.p95)}</b> · 99th <b>${W.fmt(ix.thresholds.p99)}</b>${ix.threshold_min_p90 != null ? ` · 90th of daily minima <b>${W.fmt(ix.threshold_min_p90)}</b>` : ''}.
          Record: corridor max ${W.fmt(rec.corridor_max?.value)} (${rec.corridor_max?.year ?? '—'}), min ${W.fmt(rec.corridor_min?.value)} (${rec.corridor_min?.year ?? '—'});
          rest max ${W.fmt(rec.region_max?.value)} (${rec.region_max?.year ?? '—'}), min ${W.fmt(rec.region_min?.value)} (${rec.region_min?.year ?? '—'}).</div>
        <table class="t" style="margin-top:6px"><thead><tr><th></th><th style="text-align:right">Corridor</th><th style="text-align:right">Rest</th>
          <th>Corridor trend</th><th>Rest trend</th><th>Corridor − rest</th></tr></thead><tbody>
        ${GROUPS.map(([g, ks]) => { const rs = ks.filter(k => ix.corridor[k] != null || ix.region[k] != null);
          return rs.length ? `<tr><td colspan="6" style="padding-top:8px"><b>${g}</b></td></tr>` + rs.map(row).join('') : ''; }).join('')}
        </tbody></table>
        <div class="hint">Trend: ↑/↓ direction (Kendall τ), Sen slope per decade, p corrected for serial correlation (Hamed–Rao); ✓ = p &lt; 0.05.
          Mean seasonal cycle — corridor: spring peak ${W.fmt(ix.cycle.corridor.spring_peak.value)} (${ix.cycle.corridor.spring_peak.date || '—'}),
          summer trough ${W.fmt(ix.cycle.corridor.summer_trough.value)} (${ix.cycle.corridor.summer_trough.date || '—'}), fall peak ${W.fmt(ix.cycle.corridor.fall_peak.value)} (${ix.cycle.corridor.fall_peak.date || '—'}).</div>
        <div id="ln-cycle"></div>
        <h3>Each month: highest daily maximum and lowest daily minimum</h3>
        <table class="t"><thead><tr><th>Month</th><th style="text-align:right">Max corridor</th><th style="text-align:right">Max rest</th><th>Max trend (corridor)</th>
          <th style="text-align:right">Min corridor</th><th style="text-align:right">Min rest</th><th>Min trend (corridor)</th></tr></thead><tbody>
        ${MONTHS.map((m, i) => { const k = String(i + 1).padStart(2, '0'), a_ = `m${k}_max`, b_ = `m${k}_min`;
          if (ix.corridor[a_] == null && ix.region[a_] == null) return '';
          return `<tr><td>${m}</td><td class="v">${W.fmt(ix.corridor[a_])}</td><td class="v">${W.fmt(ix.region[a_])}</td><td>${trendCell(tr[a_]?.corridor)}</td>
            <td class="v">${W.fmt(ix.corridor[b_])}</td><td class="v">${W.fmt(ix.region[b_])}</td><td>${trendCell(tr[b_]?.corridor)}</td></tr>`; }).join('')}
        </tbody></table>
        <div class="row" style="gap:6px;align-items:center;margin-top:8px">Yearly: <select id="ln-ym">${Object.entries(YEARLY).filter(([k]) => ix.corridor[k] != null || ix.region[k] != null).map(([k, t]) => `<option value="${k}" ${k === L.ymetric ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
        <div id="ln-yearly"></div>
        ${Object.keys(nt).length ? `<h3>Do the lines run through more dangerous places than chance?</h3>
        <table class="t"><thead><tr><th>Cell climatology / trend</th><th style="text-align:right">Lines</th><th style="text-align:right">Rest</th>
          <th style="text-align:right">Same lines shifted (mean, 5–95%)</th><th style="text-align:right">p (higher)</th><th style="text-align:right">p (lower)</th></tr></thead><tbody>
        ${Object.entries(nt).map(([k, x]) => `<tr><td>${W.esc(CELL_FIELDS[k] || k)}</td><td class="v">${W.fmt(x.corridor)}</td><td class="v">${W.fmt(x.region)}</td>
          <td class="v">${W.fmt(x.shifted_mean)} <small class="r">(${W.fmt(x.shifted_p05)}–${W.fmt(x.shifted_p95)})</small></td>
          <td class="v">${W.fmt(x.p_higher, 3)}${x.p_higher < 0.05 ? ' ✓' : ''}</td><td class="v">${W.fmt(x.p_lower, 3)}${x.p_lower < 0.05 ? ' ✓' : ''}</td></tr>`).join('')}
        </tbody></table>
        <div class="hint">${Object.values(nt)[0]?.n_shifts ?? ''} copies of the whole layer moved 5–40 km in random directions (kept where ≥ 70% stays on the mainland); each crossed cell counted once.
          p (higher) = share of shifted copies at least as high as the real lines.</div>` : ''}
        <h3>Map</h3>
        <div class="row" style="gap:6px;flex-wrap:wrap;align-items:center">
          <label>Colour lines by <select id="ln-metric">${Object.entries(LINE_METRICS).map(([k, t]) => `<option value="${k}" ${k === L.metric ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
          <label>Paint cells <select id="ln-cell">${Object.entries(CELL_FIELDS).map(([k, t]) => `<option value="${k}" ${k === L.cellKey ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
          <button class="btn sm" id="ln-paint">Paint cells</button>
        </div>
        <div class="hint">Cell trends (false discovery rate 10%): ${['days_p90', 'year_max', 'days_min_p90'].map(k => { const c = ix.cell_trends?.[k]; return c ? `${W.esc(YEARLY[k])}: rising in ${c.cells_increasing_sig.toLocaleString()} and falling in ${c.cells_decreasing_sig.toLocaleString()} of ${c.cells_tested.toLocaleString()} cells (along the lines ${c.corridor_cells_increasing_sig} ↑ / ${c.corridor_cells_decreasing_sig} ↓)` : ''; }).filter(Boolean).join('; ')}.</div>
        <h3>Lines ranked by ${W.esc(IDX_LABEL[L.index])} days above the 95th percentile</h3>
        <div style="max-height:320px;overflow:auto"><table class="t"><thead><tr><th>#</th><th>Line</th><th style="text-align:right">kV</th><th>Owner</th><th style="text-align:right">km</th>
          <th style="text-align:right">days &gt;95th /yr</th><th style="text-align:right">last 10 y</th><th>trend days &gt;90th</th><th style="text-align:right">year max / min</th>
          <th style="text-align:right">record max / min</th><th style="text-align:right">spring / fall peak</th><th style="text-align:right">peak / low hour</th></tr></thead><tbody>
        ${[...sm.features].sort((a, b) => (b[`${L.index}.days_p95`] ?? -1) - (a[`${L.index}.days_p95`] ?? -1)).map((r, i) => `<tr data-lid="${W.esc(r.id)}" class="${r.id === L.focus ? 'fdi' : ''}">
          <td>${i + 1}</td><td><b>${W.esc(r.name || r.id)}</b><div class="r">${W.esc(r.county || '')}${r.sub_1 ? ` · ${W.esc(r.sub_1)} – ${W.esc(r.sub_2 || '')}` : ''}</div></td>
          <td class="v">${r.voltage_kv ?? (r.voltage_best_kv != null ? r.voltage_best_kv + '*' : '—')}${r.alt_voltage_disagrees ? ` <small class="r" title="the other layer says ${r.alt_voltage_kv} kV">≠${r.alt_voltage_kv}</small>` : ''}</td>
          <td class="r">${W.esc(r.owner || (r.owner_best ? r.owner_best + ' *' : '—'))}${r.owner && r.alt_owner && r.alt_owner.toLowerCase() !== String(r.owner).toLowerCase() ? `<br><small>other layer: ${W.esc(r.alt_owner)}</small>` : ''}</td><td class="v">${W.fmt(r.km_in_cells, 1)}</td>
          <td class="v">${W.fmt(r[`${L.index}.days_p95`], 1)}</td><td class="v">${W.fmt(r[`${L.index}.days_p95.last_10y`], 1)}</td>
          <td>${trendCell({ per_decade: r[`${L.index}.days_p90.trend_per_decade`], tau: r[`${L.index}.days_p90.tau`], p: r[`${L.index}.days_p90.p`], p_hr: r[`${L.index}.days_p90.p_hr`] })}</td>
          <td class="v">${W.fmt(r[`${L.index}.year_max`], 1)} / ${W.fmt(r[`${L.index}.year_min`], 1)}</td><td class="v">${W.fmt(r[`${L.index}.record_max`], 1)} / ${W.fmt(r[`${L.index}.record_min`], 1)}</td>
          <td class="v">${W.fmt(r[`${L.index}.spring_peak`], 1)} / ${W.fmt(r[`${L.index}.fall_peak`], 1)}</td>
          <td class="v">${W.fmt(r[`${L.index}.peak_hour_lst`], 0)} / ${W.fmt(r[`${L.index}.trough_hour_lst`], 0)}</td></tr>`).join('')}
        </tbody></table></div>
        <div class="hint">Downloads: ${['lines.csv', 'yearly_corridor_vs_region.csv', 'seasonal_cycle.csv', ...IDX.filter(v => sm.indices[v] && !sm.indices[v].skipped).map(v => `cells_${v}.csv`), 'lines_with_metrics.geojson', 'summary.json', 'README.txt']
          .map(n => `<a href="${W.API}/api/lines/analyses/${encodeURIComponent(aid)}/file/${n}">${n}</a>`).join(' · ')}
          <br>Caveats: ${sm.caveats.map(W.esc).join(' ')}</div>`;
    } else if (sm && ix?.skipped) {
      html += `<div class="callout">${W.esc(IDX_LABEL[L.index])}: ${W.esc(ix.skipped)}</div>`;
    } else if (!aid) {
      html += '<div class="hint" style="margin-top:8px">No analysis for this layer yet.</div>';
    }
    html += '</div>';
    b.innerHTML = html;
    $('ln-layer').onchange = async e => { L.layer = e.target.value; await loadSummary(); if (L.shown) await showLayer(true); W.renderTab(); };
    $('ln-show').onchange = e => showLayer(e.target.checked);
    $('ln-run').onclick = runAnalysis;
    $('ln-dl').onclick = renderDownload;
    if ($('ln-cross')) $('ln-cross').onclick = async () => {
      try { const m = await W.api(`/api/lines/layers/${encodeURIComponent(L.layer)}/crosscheck`, { json: { other: $('ln-other').value } });
        W.toast(`New layer: owner filled for ${m.cross_check.owner_filled}, voltage for ${m.cross_check.voltage_filled} line(s). Analyse it to rank with the filled attributes.`);
        L.layer = m.id; W.loadLines(); } catch (e) { W.toast('Error: ' + W.esc(e.message)); } };
    if ($('ln-del')) $('ln-del').onclick = async () => { await W.api(`/api/lines/layers/${encodeURIComponent(L.layer)}`, { method: 'DELETE' }); L.layer = 'wup_transmission_hifld_2024'; W.loadLines(); };
    $('ln-import').onchange = async e => { const file = e.target.files[0]; if (!file) return; const fd = new FormData(); fd.append('file', file);
      try { const r = await fetch(W.API + '/api/lines/layers/import', { method: 'POST', body: fd }).then(x => x.json());
        if (r.error) throw new Error(r.error);
        W.toast(`Imported ${r.n_features} line features (${W.fmt(r.km, 1)} km) from ${W.esc(file.name)}.`); L.layer = r.id; W.loadLines(); }
      catch (err) { W.toast('Import failed: ' + W.esc(err.message)); } };
    b.querySelectorAll('[data-lix]').forEach(x => x.onclick = () => { L.index = x.dataset.lix; W.renderTab(); if (L.shown) paintLines(); });
    if ($('ln-metric')) $('ln-metric').onchange = e => { L.metric = e.target.value; if (!L.shown) { L.shown = true; showLayer(true); } else paintLines(); };
    if ($('ln-cell')) $('ln-cell').onchange = e => { L.cellKey = e.target.value; paintCells(); };
    if ($('ln-paint')) $('ln-paint').onclick = paintCells;
    if ($('ln-ym')) $('ln-ym').onchange = e => { L.ymetric = e.target.value; drawYearly(); };
    b.querySelectorAll('tr[data-lid]').forEach(tr => tr.onclick = () => zoomTo(tr.dataset.lid));
    if (sm && ix && !ix.skipped) { drawCycle(); drawYearly(); }
    if (L.task) pollTask();
  };

  function zoomTo(id) {
    S.lines.focus = id;
    const f = S.lines.fc?.features.find(x => x.properties.id === id);
    if (!f) { showLayer(true).then(() => zoomTo(id)); return; }
    const cs = f.geometry.type === 'LineString' ? f.geometry.coordinates : f.geometry.coordinates.flat();
    const xs = cs.map(c => c[0]), ys = cs.map(c => c[1]);
    W.map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], { padding: 80, maxZoom: 11 });
    document.querySelectorAll('tr[data-lid]').forEach(tr => tr.classList.toggle('fdi', tr.dataset.lid === id));
  }

  async function drawCycle() {
    const box = $('ln-cycle'); if (!box) return;
    const aid = analysisId();
    const r = (await W.api(`/api/lines/analyses/${encodeURIComponent(aid)}/cycle?index=${S.lines.index}`)).rows;
    if (!r.length) return;
    const col = W.COLOR[S.lines.index === 'FWI_daily' ? 'FWI' : S.lines.index] || '#f0a24b';
    W.drawLineChart(box, { times: r.map(x => new Date(Date.UTC(2001, 0, 1 + x.doy))), daily: true,
      lines: [{ v: r.map(x => x.region), color: '#9aa0a6', w: 1.4, dash: '4 3', label: 'rest: daily max' }, { v: r.map(x => x.corridor), color: col, w: 2, label: 'lines: daily max' },
        ...(r.some(x => x.corridor_min != null && x.corridor_min !== x.corridor) ? [{ v: r.map(x => x.region_min), color: '#9aa0a6', w: 1, dash: '2 3', label: 'rest: daily min' },
          { v: r.map(x => x.corridor_min), color: col, w: 1.2, dash: '5 2', label: 'lines: daily min' }] : [])],
      title: `${IDX_LABEL[S.lines.index]}: mean seasonal cycle (15-day smoothed)`,
      height: 190, xFmt: d => d.toISOString().slice(5, 10), tip: true, tipTime: d => d.toISOString().slice(5, 10) });
  }

  async function drawYearly() {
    const box = $('ln-yearly'); if (!box) return;
    const aid = analysisId();
    const r = (await W.api(`/api/lines/analyses/${encodeURIComponent(aid)}/yearly?index=${S.lines.index}&metric=${S.lines.ymetric}`)).rows;
    if (!r.length) { box.innerHTML = ''; return; }
    const col = W.COLOR[S.lines.index === 'FWI_daily' ? 'FWI' : S.lines.index] || '#f0a24b';
    W.drawLineChart(box, { times: r.map(x => new Date(Date.UTC(x.year, 6, 1))), daily: true,
      lines: [{ v: r.map(x => x.region), color: '#9aa0a6', w: 1.4, dash: '4 3', label: 'rest of mainland' }, { v: r.map(x => x.corridor), color: col, w: 2, label: 'lines' }],
      title: `${IDX_LABEL[S.lines.index]}: ${YEARLY[S.lines.ymetric].replace(/ \(.*\)$/, '')}`, height: 180,
      xFmt: d => String(d.getUTCFullYear()), tip: true, tipTime: d => String(d.getUTCFullYear()) });
  }

  async function renderDownload() {
    const box = $('ln-dlbox'); if (!box) return;
    let srcs = [];
    try { srcs = (await W.api('/api/lines/sources')).sources; } catch (e) { box.innerHTML = `<div class="callout bad">${W.esc(e.message)}</div>`; return; }
    box.innerHTML = `<div class="callout">${srcs.map(s => `<div style="margin:4px 0"><button class="btn sm" data-lsrc="${W.esc(s.id)}">${W.esc(s.label)}</button>
      <span class="r">${W.esc(s.description)}</span></div>`).join('')}</div>`;
    box.querySelectorAll('[data-lsrc]').forEach(x => x.onclick = async () => {
      try { const t = await W.api('/api/lines/download', { json: { source: x.dataset.lsrc } });
        box.innerHTML = `<div class="hint"><span class="spin"></span> downloading (task #${t.id})…</div>`;
        const poll = async () => { const tt = (await W.api('/api/tasks')).tasks.find(y => y.id === t.id);
          if (!tt || tt.status === 'running' || tt.status === 'queued') { setTimeout(poll, 2000); return; }
          if (tt.status === 'done') { box.innerHTML = `<div class="hint">Saved layer <b>${W.esc(tt.result.name)}</b>: ${tt.result.n_features} features, ${W.fmt(tt.result.km, 1)} km, voltage for ${tt.result.with_voltage}, owner for ${tt.result.with_owner}.</div>`;
            S.lines.layer = tt.result.id; W.loadLines(); }
          else box.innerHTML = `<div class="callout bad">${W.esc(tt.status)}: ${W.esc(tt.error || '')}</div>`; };
        poll();
      } catch (e) { W.toast('Error: ' + W.esc(e.message)); } });
  }
})();
