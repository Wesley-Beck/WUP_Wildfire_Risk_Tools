/* Known fires: catalog on the map, one-click checks against the computed indices */
'use strict';
(function () {
  const S = W.S;
  S.fires = { fc: null, sel: null, report: null, busy: false, wfOnly: true, pick: null };
  const RANK_COLOR = r => r == null ? 'var(--ink-3)' : r >= 90 ? '#e66767' : r >= 80 ? '#f0a35e' : r >= 50 ? '#c9b458' : '#7fa7c9';
  const ORDER = ['FWI_daily', 'FWI', 'FPI', 'ERC', 'BI'];
  const cfgQ = () => 'config=' + encodeURIComponent(JSON.stringify(S.cfg || {}));

  W.loadFires = async function () {
    try { S.fires.fc = await W.api('/api/fires?' + cfgQ()); } catch (e) { return; }
    const m = W.map;
    const pts = { type: 'FeatureCollection', features: S.fires.fc.features.filter(f => f.properties.lat != null).map(f => ({
      type: 'Feature', geometry: { type: 'Point', coordinates: [f.properties.lon, f.properties.lat] },
      properties: { id: f.properties.id, name: f.properties.name, acres: f.properties.acres || 0, type: f.properties.type, discovered: f.properties.discovered } })) };
    const polys = { type: 'FeatureCollection', features: S.fires.fc.features.filter(f => f.geometry && f.geometry.type !== 'Point') };
    if (m.getSource('fires')) { m.getSource('fires').setData(pts); m.getSource('fire-polys').setData(polys); }
    else {
      m.addSource('fires', { type: 'geojson', data: pts });
      m.addSource('fire-polys', { type: 'geojson', data: polys });
      m.addLayer({ id: 'fire-polys', type: 'line', source: 'fire-polys', paint: { 'line-color': '#ffd166', 'line-width': 1.5, 'line-dasharray': [2, 1] } });
      m.addLayer({ id: 'fires-pt', type: 'circle', source: 'fires', paint: {
        'circle-radius': ['interpolate', ['linear'], ['ln', ['+', 1, ['get', 'acres']]], 0, 4, 7, 11],
        'circle-color': ['match', ['get', 'type'], 'RX', '#8a887f', '#ffd166'], 'circle-opacity': 0.9,
        'circle-stroke-color': '#1c1e21', 'circle-stroke-width': 1.5 } });
      const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
      m.on('mousemove', 'fires-pt', e => { const p = e.features[0].properties; m.getCanvas().style.cursor = 'pointer';
        pop.setLngLat(e.lngLat).setHTML(`<b>${W.esc(p.name)}</b><br>${W.esc(String(p.discovered).replace('T', ' ').replace(':00Z', 'Z'))}<br>${p.acres ? W.fmt(+p.acres, 0) + ' ac' : 'size n/a'} · click to check`).addTo(m); });
      m.on('mouseleave', 'fires-pt', () => { pop.remove(); m.getCanvas().style.cursor = ''; });
      m.on('click', 'fires-pt', e => { if (S.fires.pick) return; W.openFire(e.features[0].properties.id); });
      m.on('click', e => {               // "set location" pick mode
        if (!S.fires.pick) return;
        const id = S.fires.pick; S.fires.pick = null; m.getCanvas().style.cursor = '';
        W.api(`/api/fires/${encodeURIComponent(id)}/location`, { json: { lat: e.lngLat.lat, lon: e.lngLat.lng } })
          .then(() => { W.toast('Location set (marked approximate).'); return W.loadFires(); }).then(() => W.openFire(id))
          .catch(err => W.toast('Error: ' + W.esc(err.message)));
      });
    }
    if (S.tab === 'FIRE') W.renderTab();
  };

  const fireById = id => S.fires.fc?.features.find(f => f.properties.id === id);
  W.openFire = function (id) {
    S.fires.sel = id; S.fires.report = null;
    S.tab = 'FIRE'; W.renderTab();
    const f = fireById(id);
    if (f && f.properties.lat != null) W.map.flyTo({ center: [f.properties.lon, f.properties.lat], zoom: 11 });
    const prev = S.fires.reports?.[id + '|' + JSON.stringify(S.cfg)];
    if (prev) { S.fires.report = prev; W.renderTab(); }
  };

  function statusOf(p) {
    if (p.lat == null) return '<span class="tag open">needs location</span>';
    if (!p.in_grid) return '<span class="tag caveat">outside grid</span>';
    if (!p.in_record) return '<span class="tag caveat">beyond AORC data</span>';
    return p.checked ? '<span class="tag fixed">checked</span>' : '';
  }
  const when = p => !p.discovered ? '—' : p.date_only ? `${p.discovered} <small>(date only)</small>` : W.esc(p.discovered.replace('T', ' ').replace(/:\d\dZ$/, 'Z'));

  W.renderFires = function (b) {
    const fc = S.fires.fc;
    const feats = (fc?.features || []).filter(f => !S.fires.wfOnly || f.properties.type !== 'RX')
      .sort((a, b) => String(b.properties.discovered).localeCompare(String(a.properties.discovered)));
    b.innerHTML = `<div class="sect"><h2>Known fires — do the indices make sense?</h2>
      <div class="callout">Pick a documented fire to see FWI (hourly FWI2025 and classic daily), FPI, ERC and BI at its cells on the discovery day,
      and how that day <b>ranks among all days of the same fire season</b> (Apr–Oct) at those cells. The rank needs that season saved:
      <b>Calculate this season</b> queues one job (one brick × ~7 months). This is a plausibility check on individual cases, not a skill score.
      ${S.status?.synthetic ? '<br><b style="color:#f0c27a">Demo mode: the weather is synthetic, so results say nothing about the real fires.</b>' : ''}</div>
      <div class="row" style="gap:6px;flex-wrap:wrap">
        <label class="btn sm" title="CSV, Excel or GeoJSON from NIFC (WFIGS/InFORM), MTBS or Michigan DNR — common column names are recognised">Import fires…<input type="file" id="fr-import" accept=".csv,.xlsx,.xls,.geojson,.json" hidden></label>
        <button class="btn sm" id="fr-seasons" title="One background job per fire year and brick">Calculate all fire seasons</button>
        <button class="btn sm" id="fr-report" title="Check every fire and save CSV + HTML + JSON reports in the workspace">Check all &amp; save report</button>
        <label class="chk"><input type="checkbox" id="fr-wf" ${S.fires.wfOnly ? 'checked' : ''}> wildfires only</label>
      </div>
      <div id="fr-msg" class="hint">${S.fires.msg || ''}</div>
      <div style="max-height:260px;overflow:auto;margin-top:6px"><table class="t"><thead><tr><th>Fire</th><th>Discovered (UTC)</th><th>County</th><th style="text-align:right">Acres</th><th>Cause</th><th></th><th></th></tr></thead><tbody>
      ${feats.map(f => { const p = f.properties; return `<tr class="${p.id === S.fires.sel ? 'fdi' : ''}" data-fid="${W.esc(p.id)}" style="cursor:pointer">
        <td><b>${W.esc(p.name)}</b>${p.origin === 'imported' ? ' <small class="r">imported</small>' : ''}</td><td>${when(p)}</td><td>${W.esc(p.county || p.cell_county || '')}</td>
        <td class="v">${p.acres != null ? W.fmt(+p.acres, p.acres < 10 ? 1 : 0) : '—'}</td><td class="r">${W.esc(p.cause || '')}</td><td>${statusOf(p)}</td>
        <td style="white-space:nowrap">${p.lat == null || p.location_precision === 'approximate' ? `<button class="btn sm ghost" data-locate="${W.esc(p.id)}" title="Click the fire's location on the map">Set location</button>` : ''}
          ${p.origin === 'imported' ? `<button class="btn sm ghost" data-del="${W.esc(p.id)}" title="Remove this imported record">✕</button>` : ''}</td></tr>`; }).join('')}
      </tbody></table></div>
      <div id="fr-detail"></div></div>`;
    b.querySelectorAll('tr[data-fid]').forEach(tr => tr.onclick = ev => { if (ev.target.closest('button')) return; W.openFire(tr.dataset.fid); });
    b.querySelectorAll('[data-locate]').forEach(x => x.onclick = () => { S.fires.pick = x.dataset.locate; W.map.getCanvas().style.cursor = 'crosshair';
      $('fr-msg').textContent = 'Click the fire\'s location on the map (Esc cancels).'; W.toast('Click the map where the fire started.'); });
    b.querySelectorAll('[data-del]').forEach(x => x.onclick = async () => { await W.api(`/api/fires/${encodeURIComponent(x.dataset.del)}`, { method: 'DELETE' }); if (S.fires.sel === x.dataset.del) S.fires.sel = null; W.loadFires(); });
    $('fr-wf').onchange = e => { S.fires.wfOnly = e.target.checked; W.renderTab(); };
    $('fr-import').onchange = async e => { const file = e.target.files[0]; if (!file) return; const fd = new FormData(); fd.append('file', file);
      try { const r = await fetch(W.API + '/api/fires/import', { method: 'POST', body: fd }).then(x => x.json());
        if (r.error) throw new Error(r.error);
        W.toast(`Imported ${r.added} fire(s) from ${W.esc(file.name)} · ${r.outside_study_area} outside the study area · ${r.skipped} skipped (duplicates or no date/location).`);
        W.loadFires(); } catch (err) { W.toast('Import failed: ' + W.esc(err.message)); } };
    $('fr-seasons').onclick = async () => { try { const r = await W.api('/api/fires/season-jobs', { json: { config: S.cfg } });
      W.toast(`${r.jobs.length} season job(s) queued (${r.jobs.reduce((a, j) => a + j.packets_total, 0).toLocaleString()} packets). Progress in Jobs; already-saved chunks are skipped.`); W.pollJobs(true); } catch (e) { W.toast('Error: ' + W.esc(e.message)); } };
    $('fr-report').onclick = runReport;
    renderDetail();
  };

  async function runReport() {
    $('fr-msg').innerHTML = '<span class="spin"></span> checking every fire…';
    try {
      const r = await W.api('/api/fires/report', { json: { config: S.cfg } });
      S.fires.reports = S.fires.reports || {};
      r.reports.forEach(x => { if (x.fire?.id) S.fires.reports[x.fire.id + '|' + JSON.stringify(S.cfg)] = x; });
      const s = r.summary;
      S.fires.msg = `${s.checked} checked · <b>${s.elevated}</b> elevated · ${s.mixed} mixed · ${s.not_elevated} not elevated · ${s.unranked} without a saved season ·
        <a href="${W.API}/api/exports/${r.html_id}">report (HTML)</a> · <a href="${W.API}/api/exports/${r.csv_id}">table (CSV)</a> · <a href="${W.API}/api/exports/${r.json_id}">full (JSON)</a> — also in the workspace exports folder`;
      $('fr-msg').innerHTML = S.fires.msg;
      W.loadFires();
    } catch (e) { $('fr-msg').textContent = 'Error: ' + e.message; }
  }

  async function runCheck(id) {
    S.fires.busy = true; renderDetail();
    try {
      const r = await W.api(`/api/fires/${encodeURIComponent(id)}/check`, { json: { config: S.cfg } });
      S.fires.report = r; S.fires.reports = S.fires.reports || {}; S.fires.reports[id + '|' + JSON.stringify(S.cfg)] = r;
      if (r.status === 'ok') { const f = fireById(id); if (f) f.properties.checked = true; }
    } catch (e) { S.fires.report = { status: 'error', message: e.message }; }
    S.fires.busy = false; if (S.tab === 'FIRE') W.renderTab();
  }

  function renderDetail() {
    const box = $('fr-detail'); if (!box) return;
    const f = fireById(S.fires.sel);
    if (!f) { box.innerHTML = '<div class="hint" style="margin-top:8px">Select a fire in the table or on the map (yellow dots; size ∝ acres).</div>'; return; }
    const p = f.properties, r = S.fires.report;
    let html = `<h3 style="margin-top:12px">${W.esc(p.name)} · ${when(p)}</h3>
      <div class="hint">${W.esc(p.county || '')} County · ${p.acres != null ? W.fmt(+p.acres, 0) + ' ac' : 'size n/a'} · cause ${W.esc(p.cause || 'n/a')} · location: ${W.esc(p.location_precision || '?')}${p.distance_km != null ? ` (cell ${p.cell}, ${W.fmt(p.distance_km * 1000, 0)} m from its centre)` : ''}<br>
      source: ${W.esc(p.source || '')}${p.notes ? '<br>' + W.esc(p.notes) : ''}</div>
      <div class="row" style="gap:6px;margin:8px 0;flex-wrap:wrap">
        <button class="btn" id="fr-check" ${p.in_grid && p.in_record && !S.fires.busy ? '' : 'disabled'}>${S.fires.busy ? '<span class="spin"></span> checking…' : 'Check this fire'}</button>
        <button class="btn sm" id="fr-season" ${p.in_grid && p.in_record ? '' : 'disabled'} title="Queue one job: this fire's brick(s), April–October of its year">Calculate this season</button>
        <button class="btn sm" id="fr-show" ${p.in_grid ? '' : 'disabled'} title="Select the fire's cells and chart the 18-day window in the index tabs">Open in charts</button>
        ${r?.status === 'ok' ? '<button class="btn sm ghost" id="fr-json">Download check (JSON)</button>' : ''}
      </div>`;
    if (r && r.fire?.id === p.id) {
      if (r.status !== 'ok') html += `<div class="callout bad">${W.esc(r.message || r.status)}</div>`;
      else {
        const v = r.verdict || {};
        html += `<div class="callout ${v.kind === 'elevated' ? 'good' : v.kind === 'not-elevated' ? 'bad' : ''}"><b>${W.esc(v.text || '')}</b></div>`;
        (r.notes || []).forEach(n => html += `<div class="callout">${W.esc(n)}</div>`);
        html += `<div class="hint">${W.esc(r.cells.basis)} · window ${W.esc(r.window.discovery_local_day)} −${r.window.days_before} d … +${r.window.days_after} d (${W.esc(r.window.origin)}) · season saved ${W.pct(100 * r.season.saved_fraction, 0)} · config ${W.esc(r.config_id)} · ${r.elapsed_s ?? ''} s</div>`;
        html += `<table class="t" style="margin-top:6px"><thead><tr><th>Index</th><th style="text-align:right">At discovery</th><th style="text-align:right">Discovery-day max</th><th style="text-align:right">3 days before (max)</th><th>Season rank at these cells</th></tr></thead><tbody>`;
        for (const k of ORDER.filter(k => k in r.indices)) { const e = r.indices[k];
          const sr = e.season_rank, rk = sr?.median;
          html += `<tr><td>${W.esc(e.label)}</td><td class="v">${W.fmt(e.at_discovery?.median)}</td><td class="v">${W.fmt(e.disc_day?.median)}${e.fwi_class ? ` <small class="r">${e.fwi_class}</small>` : ''}</td><td class="v">${W.fmt(e.prior3_max)}</td>
            <td>${sr ? `<span style="display:inline-block;width:${Math.max(2, rk)}px;max-width:100px;height:8px;border-radius:3px;background:${RANK_COLOR(rk)};vertical-align:0"></span> <b>${W.fmt(rk, 0)}</b>th <small class="r">(${W.fmt(sr.min, 0)}–${W.fmt(sr.max, 0)} across cells, ${sr.n_days} days) · ${W.esc(e.rank_class || '')}</small>` : '<small class="r">season not saved</small>'}</td></tr>`;
        }
        html += '</tbody></table><div id="fr-charts" class="fr-charts"></div>';
        if (r.reference_checks?.length) {
          html += `<h3>Reference values recorded for this fire</h3><table class="t"><thead><tr><th>Reference</th><th style="text-align:right">Recorded</th><th style="text-align:right">This tool</th><th style="text-align:right">Δ</th><th>Agrees</th></tr></thead><tbody>` +
            r.reference_checks.map(c => `<tr><td>${W.esc(c.label)}<div class="r">${W.esc(c.basis || '')}</div></td><td class="v">${W.fmt(c.value)}</td><td class="v">${W.fmt(c.ours)}<div class="r">${W.esc(c.at || '')}</div></td><td class="v">${W.fmt(c.diff)}</td>
              <td><span class="tag ${c.agrees ? 'fixed' : 'open'}">${c.agrees ? 'yes' : 'no'}</span> <small class="r">±${c.tolerance}</small></td></tr>`).join('') + '</tbody></table>';
        }
        const rs = r.reference_series;
        if (rs?.series?.length) {
          html += `<h3>Whole series from an earlier run</h3><div class="hint">${W.esc(rs.label || '')} · ${W.esc(rs.made_by || '')}<br>${W.esc(rs.start_up || '')} This tool: ${W.esc(rs.origin || '')}.</div>
            <table class="t"><thead><tr><th>Quantity</th><th style="text-align:right">n</th><th style="text-align:right">mean (earlier → this)</th><th style="text-align:right">mean Δ</th><th style="text-align:right">MAE</th><th style="text-align:right">r</th><th>Compared from</th></tr></thead><tbody>` +
            rs.series.map(x => x.skipped ? `<tr><td>${W.esc(x.quantity)}</td><td colspan="6" class="r">${W.esc(x.skipped)}</td></tr>` :
              `<tr><td>${W.esc(x.quantity)}<div class="r">${W.esc(x.system || '')} · cell ${x.cell} (${W.fmt(x.cell_distance_km * 1000, 0)} m from its pixel)</div></td><td class="v">${x.stats.n}</td>
               <td class="v">${W.fmt(x.stats.ref_mean)} → ${W.fmt(x.stats.ours_mean)}</td><td class="v">${W.fmt(x.stats.mean_diff)}</td><td class="v">${W.fmt(x.stats.mae)}</td><td class="v">${W.fmt(x.stats.r, 3)}</td><td><small class="r">${W.esc(String(x.compare_from).slice(0, 16))}</small></td></tr>`).join('') +
            '</tbody></table><div id="fr-refcharts" class="fr-charts"></div>';
        }
      }
    }
    box.innerHTML = html;
    $('fr-check').onclick = () => runCheck(p.id);
    $('fr-season').onclick = async () => { try { const j = await W.api(`/api/fires/${encodeURIComponent(p.id)}/season-job`, { json: { config: S.cfg } });
      W.toast(`Job #${j.id} queued: ${j.name} — ${j.packets_total.toLocaleString()} packets. Re-run the check when it finishes.`); W.pollJobs(true); } catch (e) { W.toast('Error: ' + W.esc(e.message)); } };
    $('fr-show').onclick = () => openInCharts(p);
    if ($('fr-json')) $('fr-json').onclick = () => W.download(`wup_fire_check_${p.id}_${r.config_id}.json`, JSON.stringify(r, null, 1), 'application/json');
    if (r?.status === 'ok' && r.fire?.id === p.id) drawCharts(r);
  }

  function drawRefCharts(r) {
    const box = $('fr-refcharts'); if (!box) return;
    box.innerHTML = '';
    for (const x of (r.reference_series?.series || []).filter(x => !x.skipped && (x.quantity === 'FWI' || x.quantity === 'FFMC' || x.quantity === 'FWI_daily'))) {
      const div = document.createElement('div'); box.appendChild(div);
      const fdi = x.quantity === 'FFMC' ? 'FWI' : (x.quantity === 'FWI_daily' ? 'FWI' : x.quantity);
      W.drawLineChart(div, { times: x.times.map(t => new Date(t.replace(' ', 'T') + (x.step === 'day' ? '' : 'Z'))),
        lines: [{ v: x.ref, color: '#9aa0a6', w: 1.4, dash: '4 3', label: 'earlier run' }, { v: x.ours, color: W.COLOR[fdi] || '#f0a24b', w: 1.8, label: 'this tool' }],
        title: `${x.quantity} · earlier run (dashed) vs this tool · ${x.step === 'day' ? 'daily' : 'hourly, UTC'}`, height: 165, daily: x.step === 'day',
        xFmt: d => d.toISOString().slice(5, 10), tip: true });
    }
  }

  function drawCharts(r) {
    drawRefCharts(r);
    const box = $('fr-charts'); if (!box) return;
    box.innerHTML = '';
    const ids = ORDER.filter(k => k in r.indices);
    for (const k of ids) {
      const e = r.indices[k], d = e.daily;
      const div = document.createElement('div'); box.appendChild(div);
      const fdi = k === 'FWI_daily' ? 'FWI' : k;
      const di = d.days.indexOf(r.window.discovery_local_day);
      W.drawLineChart(div, { times: d.days.map(x => new Date(x + 'T12:00:00Z')),
        lines: [{ v: d.median, color: W.COLOR[fdi], w: 2, label: 'median of fire cells' }, { v: d.origin, color: W.COLOR[fdi], w: 1.3, dash: '4 3', label: 'origin cell' }],
        bands: W.META[fdi]?.bands || [], title: `${e.label} · ${k === 'FWI_daily' ? 'value at 12:00 LST' : 'daily max'} (local standard days)`, height: 165,
        current: di < 0 ? 0 : di, daily: true, xFmt: x => x.toISOString().slice(5, 10), xNote: 'orange = discovery day', tipTime: x => x.toISOString().slice(0, 10) + ' (local day)', tip: true });
    }
  }

  function openInCharts(p) {
    W.setMode('cell');
    const r = S.fires.report?.fire?.id === p.id ? S.fires.report : null;
    const ids = r?.cells?.point_ids || [p.cell];
    W.selectMany(ids, p.name); W.fitSelection();
    const day = String(p.discovered).slice(0, 10);
    const st = new Date(day + 'T00:00:00Z'); st.setUTCDate(st.getUTCDate() - 14);
    $('t-date').value = st.toISOString().slice(0, 10); $('t-hour').value = 0; $('t-hours').value = 24 * 18; $('t-spin').value = 720;
    W.onWindowChanged(); S.tab = 'FWI'; $('m-var').value = 'FWI'; W.renderTab();
    setTimeout(() => W.run('auto'), 350);
  }

  document.addEventListener('keydown', e => { if (e.key === 'Escape' && S.fires.pick) { S.fires.pick = null; W.map.getCanvas().style.cursor = ''; W.toast('Location pick cancelled.'); } });
})();
