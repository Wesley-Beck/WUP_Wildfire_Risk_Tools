/* Saving, sharing, exporting, importing */
'use strict';
(function () {
  const S = W.S;

  /* ---------------- state (views) ---------------- */
  W.getState = function () {
    const c = W.map.getCenter();
    return { v: 1, sel: W.encodeIds([...S.sel]), selName: S.selName || null, mode: S.mode, start: W.startTime(), hours: W.hours(), spin: W.spin(),
      cfg: S.cfg, fdis: S.fdis, tab: S.tab, focus: S.focusPid, hourIdx: S.hourIdx, agg: S.agg, pct: S.pctMode, show: S.timeShow,
      mapVar: $('m-var').value, overlay: S.overlay, view: { c: [+c.lng.toFixed(4), +c.lat.toFixed(4)], z: +W.map.getZoom().toFixed(2) },
      ran: !!S.series, dataSource: S.series?.source || null };
  };
  W.applyState = async function (st) {
    if (!st) return;
    $('t-date').value = st.start.slice(0, 10); $('t-hour').value = +st.start.slice(11, 13); $('t-hours').value = st.hours; $('t-spin').value = st.spin ?? 168;
    S.timeShow = st.show || 'utc'; $('t-show').value = S.timeShow;
    if (st.cfg) W.applyCfg(st.cfg);
    if (st.fdis) { S.fdis = st.fdis; document.querySelectorAll('.fdi').forEach(c => c.checked = S.fdis.includes(c.value)); }
    S.agg = st.agg || 'hourly'; S.pctMode = !!st.pct; S.overlay = st.overlay || {}; $('m-mode').value = S.pctMode ? 'pct' : 'raw';
    if (st.mapVar) $('m-var').value = st.mapVar;
    W.setMode(st.mode === 'all' ? 'all' : (st.mode || 'cell'));
    const ids = W.decodeIds(st.sel);
    if (ids.length) W.selectMany(ids, st.selName); else W.clearSel();
    if (st.view) W.map.jumpTo({ center: st.view.c, zoom: st.view.z });
    W.onWindowChanged();
    S.tab = st.tab || 'FWI';
    if (st.ran && ids.length) {
      await W.run(st.dataSource === 'live' ? 'live' : 'auto');
      if (st.focus != null && S.sel.has(st.focus)) W.setFocus(st.focus);
      if (st.hourIdx != null && S.series) W.setHour(Math.min(st.hourIdx, S.series.time.length - 1));
    }
    W.renderTab();
  };
  W.shareUrl = async function (st) {
    const enc = await W.b64url.enc(JSON.stringify(st || W.getState()));
    return `${location.origin}${location.pathname}${location.search}#s=${enc}`;
  };
  W.copyShareLink = async function (st) {
    const url = await W.shareUrl(st && st.v ? st : undefined);
    try { await navigator.clipboard.writeText(url); W.toast(`Link copied (${url.length} characters). It opens this view in any running copy of the explorer — send a snapshot to people who don't have it.`); }
    catch { window.prompt('Copy this link:', url); }
    history.replaceState(null, '', url);
  };
  W.saveSessionPrompt = async function () {
    const name = window.prompt('Name for this view:', S.selName ? `${S.selName} · ${W.startTime()}` : `view ${new Date().toISOString().slice(0, 16)}`);
    if (!name) return;
    await W.api('/api/sessions', { json: { name, state: W.getState() } });
    W.toast(`View “${W.esc(name)}” saved to the workspace (Save & share tab).`);
    if (S.tab === 'LIB') W.renderLibrary2();
  };
  W.autosave = function () { try { localStorage.setItem('wup-last-view', JSON.stringify(W.getState())); } catch { /* storage unavailable */ } };

  /* ---------------- exports ---------------- */
  function seriesRows() {
    const s = S.series; if (!s) return null;
    const ids = ['FWI', 'FWI_daily', 'FPI', 'ERC', 'BI', 'FWI_pct', 'FPI_pct', 'ERC_pct', 'BI_pct', 'wx_tier', 'ndvi_tier'].filter(id => S.stepById.has(id));
    const perCell = ids.every(id => S.stepById.get(id).values);
    const lines = [];
    if (perCell) {
      lines.push(['point_id', 'county', 'zone', 'time_utc', 'time_local_standard_iso', ...ids].join(','));
      s.point_ids.forEach((pid, k) => { const i = W.row(pid), zone = S.cells.zone[i];
        s.time.forEach((t, h) => lines.push([pid, S.cells.county[i], zone, t, W.lstIso(W.isoDate(t), zone), ...ids.map(id => S.stepById.get(id).values[k][h] ?? '')].join(','))); });
    } else {
      lines.push(['time_utc', ...ids.flatMap(id => [`${id}_mean`, `${id}_min`, `${id}_max`])].join(','));
      s.time.forEach((t, h) => lines.push([t, ...ids.flatMap(id => { const st = S.stepById.get(id); return [st.mean[h] ?? '', st.min[h] ?? '', st.max[h] ?? '']; })].join(',')));
    }
    return '# WUP Grid Explorer series · source=' + s.source + (s.synthetic ? ' (SYNTHETIC — not real data)' : '') + ' · config ' + (s.params.config_id || '') + '\n' + lines.join('\n');
  }
  W.exportSeriesCsv = function () {
    const csv = seriesRows(); if (!csv) return W.toast('Show a selection first.');
    const name = `wup_series_${S.series.time[0].slice(0, 13)}_${S.series.point_ids.length}cells.csv`;
    W.download(name, csv, 'text/csv'); W.api('/api/exports', { json: { name, kind: 'csv', content: csv } }).catch(() => {});
  };
  W.exportTraceCsv = function () {
    const tr = S.trace; if (!tr) return W.toast('Focus a cell first.');
    const ids = Object.keys(tr.steps);
    const csv = ['time_utc,' + ids.join(','), ...tr.time.map((t, h) => [t, ...ids.map(id => tr.steps[id][h] ?? '')].join(','))].join('\n');
    const name = `wup_trace_cell${tr.cell.point_id}_${tr.time[0].slice(0, 13)}.csv`;
    W.download(name, csv, 'text/csv'); W.api('/api/exports', { json: { name, kind: 'csv', content: csv } }).catch(() => {});
  };
  W.selectionGeoJSON = function (ids, name) {
    const h = S.grid.step_deg / 2, stats = {};
    if (S.series) for (const f of W.FDI) { const st = S.stepById.get(f); if (!st?.values) continue;
      S.series.point_ids.forEach((p, k) => { const v = st.values[k].filter(x => x != null); if (!v.length) return;
        (stats[p] = stats[p] || {})[`${f}_max`] = Math.max(...v); stats[p][`${f}_mean`] = +(v.reduce((a, b) => a + b, 0) / v.length).toFixed(3); }); }
    return { type: 'FeatureCollection', name: name || 'WUP selection',
      properties: { window_start_utc: W.startTime(), packets: W.hours(), config: S.cfg },
      features: ids.map(p => { const i = W.row(p), x = S.cells.lon[i], y = S.cells.lat[i];
        return { type: 'Feature', properties: { point_id: p, county: S.cells.county[i], zone: S.cells.zone[i], brick: `${S.cells.ci[i]}_${S.cells.cj[i]}`, ...(stats[p] || {}) },
          geometry: { type: 'Polygon', coordinates: [[[x - h, y - h], [x + h, y - h], [x + h, y + h], [x - h, y + h], [x - h, y - h]]] } }; }) };
  };
  W.exportSelectionGeoJSON = function () {
    if (!S.sel.size) return W.toast('Nothing selected.');
    const gj = JSON.stringify(W.selectionGeoJSON([...S.sel], S.selName));
    const name = `wup_selection_${S.sel.size}cells.geojson`;
    W.download(name, gj, 'application/geo+json'); W.api('/api/exports', { json: { name, kind: 'geojson', content: gj } }).catch(() => {});
  };
  W.exportNetCDF = async function () {
    if (!S.sel.size) return W.toast('Nothing selected.');
    W.status('writing NetCDF…', true);
    try {
      const r = await fetch(W.API + '/api/export/netcdf', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ point_ids: [...S.sel], start: W.startTime(), hours: W.hours(), spinup_hours: W.spin(), config: S.cfg }) });
      if (!r.ok) throw new Error((await r.json()).error);
      const name = (r.headers.get('content-disposition') || '').match(/filename="?([^";]+)/)?.[1] || 'wup_fdi.nc';
      W.download(name, await r.blob()); W.toast('NetCDF written (also saved in the workspace exports folder).');
    } catch (e) { W.toast('Error: ' + W.esc(e.message)); }
    W.status('');
  };

  /* ---------------- snapshot: one self-contained HTML ---------------- */
  W.snapshot = async function () {
    W.status('building snapshot…', true);
    const st = W.getState();
    const mapPng = await new Promise(res => { W.map.once('render', () => res(W.map.getCanvas().toDataURL('image/png'))); W.map.triggerRepaint(); });
    const charts = [];
    if (S.series) {
      const host = document.createElement('div'); host.style.cssText = 'position:absolute;left:-9999px;top:0;width:760px'; document.body.appendChild(host);
      for (const f of W.FDI) { if (!S.stepById.has(f)) continue;
        const box = document.createElement('div'); host.appendChild(box);
        const svg = W.buildFDIChart(f, box); if (svg) charts.push({ f, svg: W.svgString(svg).replace(/^<\?xml[^>]*>\s*/, '') }); }
      host.remove();
    }
    const flow = W.flowDiagram({ computedPct: S.status.coverage.percent_computed, perFdi: S.status.coverage.per_fdi, bricks: S.grid.bricks.length, cells: S.grid.n_cells, source: S.status.source.name, cfgLabel: S.cfgLabel });
    let day = '';
    try { const pid = S.focusPid ?? S.selOrder[0]; if (pid != null) { const rep = await W.api(`/api/timebase?pid=${pid}&date=${(S.series?.time?.[S.hourIdx] || W.startTime()).slice(0, 10)}`); day = W.svgString(W.clockStrip(rep)).replace(/^<\?xml[^>]*>\s*/, ''); } } catch { /* optional */ }
    const tr = S.trace, t = S.series?.time?.[S.hourIdx];
    let traceHtml = '';
    if (tr && t) { const h = tr.time.indexOf(t);
      if (h >= 0) traceHtml = '<table><tr><th>Step</th><th>Value</th><th>Units</th><th>Formula</th></tr>' + S.catalog.steps.filter(s => s.id in tr.steps).map(s =>
        `<tr><td>${W.esc(s.label)}</td><td class="v">${W.fmt(tr.steps[s.id][h], 3)}</td><td>${W.esc(s.units)}</td><td class="f">${W.esc(s.formula)}</td></tr>`).join('') + '</table>'; }
    const sel = S.sel.size ? `${S.sel.size.toLocaleString()} cells${S.selName ? ' — ' + W.esc(S.selName) : ''}` : 'none';
    const cnt = {}; for (const p of S.sel) { const c = S.cells.county[W.row(p)]; cnt[c] = (cnt[c] || 0) + 1; }
    const payload = { state: st, series: S.series, created: new Date().toISOString() };
    const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WUP fire danger snapshot</title><style>
body{margin:0;background:#141516;color:#eceae4;font:14px/1.5 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}main{max-width:900px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px;color:#b8b6ae;text-transform:uppercase;letter-spacing:.5px}
.meta{color:#8a887f;font-size:13px}.warn{background:#3a2a0d;border:1px solid #8a5a12;color:#f0c27a;padding:8px 12px;border-radius:8px;margin:12px 0}
img,svg{max-width:100%;height:auto;display:block;border-radius:8px;border:1px solid #2f3337;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:12.5px}td,th{border-bottom:1px solid #2f3337;padding:4px 6px;text-align:left;vertical-align:top}
th{color:#8a887f;font-size:11px;text-transform:uppercase}.v{font-family:ui-monospace,Menlo,Consolas,monospace;text-align:right}.f{font-family:ui-monospace,Menlo,Consolas,monospace;color:#8a887f;font-size:11.5px}
dl{display:grid;grid-template-columns:auto 1fr;gap:2px 14px}dt{color:#8a887f}dd{margin:0}</style></head><body><main>
<h1>Western Upper Peninsula fire danger — snapshot</h1><div class="meta">Created ${new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC with the WUP Grid Explorer</div>
${S.status.synthetic ? '<div class="warn">These values come from SYNTHETIC demo weather — not real data.</div>' : ''}
<dl><dt>Selection</dt><dd>${sel} ${Object.entries(cnt).map(([c, n]) => `${W.esc(c)} ${n}`).join(', ')}</dd>
<dt>Window</dt><dd>${W.esc(W.startTime())} UTC, ${W.hours()} hourly packets</dd><dt>Data</dt><dd>${W.esc(S.series?.source || '—')} · source ${W.esc(S.status.source.name)}</dd>
<dt>Configuration</dt><dd>${W.esc(S.cfgLabel)} <span class="meta">(${W.esc(S.series?.params?.config_id || '')})</span></dd></dl>
<h2>Map</h2><img alt="Map of the selection" src="${mapPng}">
${charts.map(c => `<h2>${c.f} · ${W.META[c.f].label}</h2>${c.svg}`).join('')}
${traceHtml ? `<h2>Every step · cell ${tr.cell.point_id} at ${W.esc(t)}</h2>${traceHtml}` : ''}
${day ? `<h2>Time &amp; daylight</h2>${day}` : ''}
<h2>Data flow</h2>${W.svgString(flow).replace(/^<\?xml[^>]*>\s*/, '')}
<p class="meta">To reopen this view interactively, import this file into the WUP Grid Explorer (Select area › Import).</p>
<script type="application/json" id="wup-session">${JSON.stringify(payload).replace(/</g, '\\u003c')}</script>
</main></body></html>`;
    const name = `wup_snapshot_${W.startTime()}_${S.sel.size}cells.html`;
    W.download(name, html, 'text/html');
    W.api('/api/exports', { json: { name, kind: 'snapshot', content: html } }).catch(() => {});
    W.status(''); W.toast(`Snapshot downloaded (${W.bytes(html.length)}) and saved in the workspace.`);
  };

  /* ---------------- import ---------------- */
  W.importFile = async function (file) {
    const text = await file.text();
    try {
      if (/\.html?$/i.test(file.name) || text.trimStart().startsWith('<')) {
        const doc = new DOMParser().parseFromString(text, 'text/html'); const el = doc.getElementById('wup-session');
        if (!el) throw new Error('no explorer data in this HTML file');
        const p = JSON.parse(el.textContent); await W.applyState(p.state); return W.toast('Snapshot view restored.');
      }
      const j = JSON.parse(text);
      if (j.v && j.start) { await W.applyState(j); return W.toast('View restored.'); }
      if (j.state) { await W.applyState(j.state); return W.toast('View restored.'); }
      if (j.point_ids || j.payload?.point_ids) { W.selectMany(j.point_ids || j.payload.point_ids, j.name); W.fitSelection(); return W.toast('Selection imported.'); }
      const polys = W.polysOf(j);
      if (polys.length) {
        const ids = W.cellsWhere(i => polys.some(rings => W.pointInPoly(S.cells.lon[i], S.cells.lat[i], rings)));
        if (!ids.length) return W.toast('No study cells fall inside those polygons.');
        W.setMode('cell'); W.selectMany(ids, file.name.replace(/\.(geo)?json$/i, '')); W.fitSelection();
        return W.toast(`Selected ${ids.length.toLocaleString()} cells inside ${polys.length} polygon(s).`);
      }
      throw new Error('unrecognised file');
    } catch (e) { W.toast('Import failed: ' + W.esc(e.message)); }
  };
})();
