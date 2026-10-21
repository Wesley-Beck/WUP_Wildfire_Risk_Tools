/* Boot and wiring */
'use strict';
(function () {
  const S = W.S;
  let selTimer = null, jobTimer = null;

  /* ---------------- configuration ---------------- */
  W.cfgFromUI = () => ({
    fuel_model: $('c-fuel').value, fuel_moisture: $('c-fm').value, daylight: $('c-dl').value, fwi_coefficients: $('c-coef').value,
    fpi_moisture: $('c-fpi').value, gap_fill: $('c-gap').checked, allow_ndvi_climatology: $('c-clim').checked, legacy_latitude: 46.5,
  });
  W.applyCfg = function (c) {
    $('c-fuel').value = c.fuel_model; $('c-fm').value = c.fuel_moisture; $('c-dl').value = c.daylight; $('c-coef').value = c.fwi_coefficients;
    $('c-fpi').value = c.fpi_moisture; $('c-gap').checked = !!c.gap_fill; $('c-clim').checked = !!c.allow_ndvi_climatology;
    onCfgChanged();
  };
  function onCfgChanged() {
    S.cfg = W.cfgFromUI();
    S.cfgLabel = `FM ${S.cfg.fuel_model} · ${S.cfg.fuel_moisture} · daylight ${S.cfg.daylight} · FWI ${S.cfg.fwi_coefficients} · FPI ${S.cfg.fpi_moisture}${S.cfg.gap_fill ? ' · gap-fill' : ''}`;
    const preset = Object.entries(S.status.presets).find(([, p]) => ['fuel_model', 'fuel_moisture', 'daylight', 'fwi_coefficients', 'fpi_moisture', 'gap_fill', 'allow_ndvi_climatology'].every(k => p[k] === S.cfg[k]));
    $('c-id').textContent = preset ? `${preset[1].id} (${preset[0]})` : 'custom';
    W.onSelectionChanged(true);
    if (W.map?.getSource && W.map.getSource('fires')) W.loadFires();      // 'checked' flags are per configuration
  }

  /* ---------------- selection & window ---------------- */
  W.setMode = function (m) {
    S.mode = m;
    document.querySelectorAll('#modeseg .btn[data-mode]').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
    if (m === 'box') W.map.dragPan.disable(); else W.map.dragPan.enable();
    $('selhint').textContent = { cell: 'Click a cell; shift-click adds. After Show, click a selected cell to focus its trace.', box: 'Drag a rectangle on the map.',
      brick: 'Click anywhere: the whole Zarr brick (what S3 downloads in one piece).', county: 'Click anywhere: the whole county.',
      all: 'Every cell. Show colours the map from saved results, packet by packet; Calculate & save adds more.' }[m];
    $('b-live').disabled = m === 'all';
    if (m === 'all') {
      W.clearSel(false); S.series = null; S.stepById = new Map(); S.trace = null;
      W.map.fitBounds([[S.grid.bbox[0], S.grid.bbox[1]], [S.grid.bbox[2], S.grid.bbox[3]]], { padding: 24, duration: 500 });
      W.renderTab();
    }
    W.onSelectionChanged();
  };
  W.selectedBricks = () => {
    if (S.mode === 'all') return S.grid.bricks.map(b => b.key);
    const set = new Set(); for (const p of S.sel) { const i = W.row(p); set.add(`${S.cells.ci[i]}_${S.cells.cj[i]}`); } return [...set].sort();
  };
  W.onSelectionChanged = function (cfgOnly) {
    const n = S.mode === 'all' ? S.cells.n : S.sel.size;
    const bricks = W.selectedBricks();
    const cnt = {};
    if (S.mode === 'all') Object.assign(cnt, S.grid.counties); else for (const p of S.sel) { const c = S.cells.county[W.row(p)]; cnt[c] = (cnt[c] || 0) + 1; }
    const zones = new Set(Object.keys(cnt).map(c => S.status.zones.find(z => z.counties.includes(c))?.label).filter(Boolean));
    $('selsum').innerHTML = `<dt>Cells</dt><dd>${n.toLocaleString()}${S.selName ? ' · ' + W.esc(S.selName) : ''}</dd><dt>Bricks</dt><dd>${bricks.join(', ') || '–'}</dd>
      <dt>Counties</dt><dd>${Object.entries(cnt).map(([c, k]) => `${W.esc(c)} ${k.toLocaleString()}`).join(', ') || '–'}</dd>
      <dt>Time zones</dt><dd>${[...zones].join(' + ') || '–'}${zones.size > 1 ? ' (daily views use each cell’s own day)' : ''}</dd>
      <dt>Packets</dt><dd>${(n * W.hours()).toLocaleString()}</dd><dt>Saved</dt><dd id="s-stored">…</dd><dt>Raw fetch</dt><dd id="s-fetch">…</dd>`;
    clearTimeout(selTimer);
    if (!n) { $('s-stored').textContent = $('s-fetch').textContent = '–'; return; }
    selTimer = setTimeout(async () => {
      try {
        const sel = S.mode === 'all' ? { mode: 'all' } : { mode: 'cells', point_ids: [...S.sel] };
        const est = await W.api('/api/select', { json: { selection: sel, start: W.startTime(), hours: W.hours(), spinup_hours: W.spin(), config: S.cfg } });
        S.selInfo = est;
        $('s-stored').innerHTML = est.stored_fraction >= 0.9999 ? '<b style="color:var(--good)">100% — Show reads saved results</b>' :
          est.stored_fraction > 0 ? `${W.fmt(100 * est.stored_fraction, 1)}% of bricks × chunks` : 'not yet — Show computes it live';
        $('s-fetch').textContent = `${est.raw_fetch_mb} MB · ${est.n_time_chunks} time chunk${est.n_time_chunks === 1 ? '' : 's'}`;
        $('b-live').disabled = S.mode === 'all' || !est.within_limits;
        if (!est.within_limits && S.mode !== 'all') W.status(`Live limit is ${S.status.limits.max_cells.toLocaleString()} cells — use Calculate & save.`);
      } catch (e) { $('s-fetch').textContent = '?'; }
    }, cfgOnly ? 50 : 250);
    W.autosave();
  };
  W.onWindowChanged = function () {
    const st = W.isoDate(W.startTime()), en = new Date(st.getTime() + (W.hours() - 1) * 3600e3);
    $('t-end').textContent = `${W.utcLabel(en)} (${W.hours()} packets)`;
    W.onSelectionChanged();
  };

  /* ---------------- run ---------------- */
  W.run = async function (mode) {
    if (S.mode === 'all') return W.loadField();
    if (!S.sel.size) { W.status('Select at least one cell first.'); return; }
    S.fdis = [...document.querySelectorAll('.fdi:checked')].map(c => c.value);
    if (!S.fdis.length) { W.status('Tick at least one index.'); return; }
    ['b-show', 'b-live'].forEach(id => $(id).disabled = true);
    W.status(`${mode === 'live' ? 'computing' : 'loading'} ${S.sel.size.toLocaleString()} cells × ${W.hours()} packets…`, true);
    const t0 = performance.now();
    try {
      const p = await W.api('/api/series', { json: { point_ids: S.selOrder, start: W.startTime(), hours: W.hours(), spinup_hours: W.spin(), fdis: S.fdis, config: S.cfg, mode } });
      S.series = p; S.stepById = new Map(p.steps.map(s => [s.id, s]));
      if (!(S.hourIdx < p.time.length)) S.hourIdx = 0;
      $('m-hour').max = p.time.length - 1; $('m-hour').value = S.hourIdx;
      if (S.focusPid == null || !S.sel.has(S.focusPid)) W.setFocus(p.point_ids[0]); else W.onFocusChanged();
      const src = p.source === 'store' ? 'saved results' : 'computed live';
      if (p.elapsed_s && p.source === 'live') S.lastRate = (p.point_ids.length * p.time.length) / Math.max(0.05, p.elapsed_s);
      W.status(`${p.point_ids.length.toLocaleString()} cells × ${p.time.length} packets · ${src} · ${p.elapsed_s}s server / ${((performance.now() - t0) / 1000).toFixed(1)}s total`);
      W.paintMap(); W.renderTab();
    } catch (e) { W.status('Error: ' + W.esc(e.message)); }
    ['b-show', 'b-live'].forEach(id => $(id).disabled = false);
    if (S.mode === 'all') $('b-live').disabled = true;
    W.autosave();
  };
  W.calcJob = async function () {
    const bricks = W.selectedBricks();
    if (!bricks.length) return W.status('Select cells (or Entire WUP) first.');
    const st = W.startTime(), en = new Date(W.isoDate(st).getTime() + (W.hours() - 1) * 3600e3).toISOString().slice(0, 13);
    try {
      const j = await W.api('/api/jobs', { json: { bricks, start: st, end: en, config: S.cfg, name: `${S.selName || bricks.join(', ')} · ${st.slice(0, 10)}` } });
      W.toast(`Job #${j.id} queued: ${bricks.length} brick${bricks.length > 1 ? 's' : ''} × ${j.tasks_total / bricks.length} chunks = ${j.packets_total.toLocaleString()} packets (period rounded to whole 144-h chunks). Progress in the Jobs tab.`);
      W.pollJobs(true);
    } catch (e) { W.status('Error: ' + W.esc(e.message)); }
  };

  /* ---------------- focus, packet, map ---------------- */
  W.onFocusChanged = function () {
    S.trace = null;
    if (S.series && S.focusPid != null) W.loadTrace();
    W.redrawChart(); W.renderTrace?.();
    if (S.tab === 'DAY') W.renderTab();
  };
  W.loadTrace = async function () {
    const key = `${S.focusPid}|${W.startTime()}|${W.hours()}|${JSON.stringify(S.cfg)}|${W.spin()}`;
    S.traceKey = key; S.traceLoading = true; W.renderTrace?.();
    try {
      const tr = await W.api('/api/trace', { json: { point_id: S.focusPid, start: W.startTime(), hours: W.hours(), spinup_hours: W.spin(), config: S.cfg } });
      if (S.traceKey !== key) return;
      S.trace = tr;
    } catch (e) { if (S.traceKey === key) W.toast('Trace: ' + W.esc(e.message)); }
    S.traceLoading = false;
    W.renderTrace(); if (S.tab === 'DAY') W.renderImpact();
  };
  W.setHour = function (h) {
    S.hourIdx = h; $('m-hour').value = h;
    if (S.mode === 'all') W.paintField(); else { W.paintMap(); W.updateChartCursor(); W.renderTrace(); }
  };
  W.currentTimeLabel = function () {
    const t = S.mode === 'all' ? S.field.times[S.hourIdx] : S.series?.time?.[S.hourIdx];
    if (!t) return '';
    const d = W.isoDate(t), zone = S.focusPid != null ? S.cells.zone[W.row(S.focusPid)] : 'ET';
    return S.timeShow === 'utc' ? W.utcLabel(d) : W.timeLabel(d, zone);
  };
  function mapVarId() { const v = $('m-var').value; return S.pctMode && W.FDI.includes(v) ? v + '_pct' : v; }
  W.valueForPid = function (pid) {
    if (S.mode === 'all') { const f = S.field.current; if (!f) return null; const i = W.row(pid); const arr = S.pctMode ? f.percentile_values : f.values; return arr ? arr[i] : null; }
    if (!S.series) return null; const k = S.series.point_ids.indexOf(pid); if (k < 0) return null;
    const st = S.stepById.get(mapVarId()); if (!st) return null;
    return st.values ? st.values[k][S.hourIdx] : st.mean[S.hourIdx];
  };
  W.paintMap = function () {
    if (S.mode === 'all') return W.paintField();
    if (!S.series) return;
    const v = $('m-var').value, id = mapVarId(), isTier = v.endsWith('_tier');
    const st = S.stepById.get(id);
    const [lo, hi] = S.pctMode && !isTier ? [0, 100] : (W.META[v]?.dom || [0, 100]);
    const vals = st ? (st.values ? st.values.map(r => r[S.hourIdx]) : S.series.point_ids.map(() => st.mean[S.hourIdx])) : null;
    W.paintValues(S.series.point_ids, vals, lo, hi, isTier);
    W.legend(`${v}${S.pctMode && !isTier ? ' percentile' : ''} · ${W.currentTimeLabel()}`, lo, hi, st ? '' : 'not in this result', isTier);
    $('m-time').textContent = W.currentTimeLabel();
  };
  /* entire-WUP field from saved results */
  W.loadField = async function () {
    const d = W.startTime();
    const y = +d.slice(0, 4), m = +d.slice(5, 7);
    W.status('listing saved hours…', true);
    try {
      const t = await W.api(`/api/field/times?year=${y}&month=${m}&config=${encodeURIComponent(JSON.stringify(S.cfg))}`);
      S.field.times = t.times; S.field.month = `${y}-${W.pad2(m)}`;
      if (!t.times.length) { W.status(`Nothing saved for ${S.field.month} with this configuration — use Calculate & save (Entire WUP) or open the Coverage tab.`); W.paintValues([], null); return; }
      const want = t.times.indexOf(d.slice(0, 13) + ':00');
      S.hourIdx = want >= 0 ? want : 0;
      $('m-hour').max = t.times.length - 1; $('m-hour').value = S.hourIdx;
      await W.paintField();
    } catch (e) { W.status('Error: ' + W.esc(e.message)); }
  };
  W.paintField = async function () {
    const t = S.field.times[S.hourIdx]; if (!t) return;
    const v = $('m-var').value, key = `${v}|${t}|${JSON.stringify(S.cfg)}`;
    let f = S.field.cache.get(key);
    if (!f) {
      W.status(`loading ${v} for ${t}…`, true);
      try { f = await W.api(`/api/field?var=${v}&time=${encodeURIComponent(t)}&config=${encodeURIComponent(JSON.stringify(S.cfg))}`); }
      catch (e) { W.status('Error: ' + W.esc(e.message)); return; }
      if (S.field.cache.size > 60) S.field.cache.delete(S.field.cache.keys().next().value);
      S.field.cache.set(key, f);
    }
    S.field.current = f;
    const isTier = v.endsWith('_tier'), [lo, hi] = S.pctMode && !isTier ? [0, 100] : (W.META[v]?.dom || [0, 100]);
    const arr = S.pctMode && !isTier ? f.percentile_values : f.values;
    W.paintValues(Array.from(S.cells.pid), arr, lo, hi, isTier);
    W.legend(`${v}${S.pctMode && !isTier ? ' percentile' : ''} · ${f.time} · saved results`, lo, hi, `${f.n.toLocaleString()} of ${S.cells.n.toLocaleString()} cells saved for this hour`, isTier);
    $('m-time').textContent = `${f.time} · median ${W.fmt(f.median)} · p98 ${W.fmt(f.p98)}`;
    W.status(`${v} at ${f.time}: ${f.n.toLocaleString()} cells from saved results`);
  };

  /* ---------------- status, jobs ---------------- */
  W.refreshStatus = async function () {
    S.status = await W.api('/api/status');
    const s = S.status, pill = $('srcpill');
    pill.className = 'pill ' + (s.synthetic ? 'synthetic' : 'aorc');
    const act = s.source?.active || '';
    pill.innerHTML = s.synthetic ? 'synthetic demo data ▾' : act.startsWith('ds-') ? `${W.esc(s.source.available?.[act] || act)} ▾`
      : (s.settings?.offline ? 'AORC v1.1 · local store (offline) ▾' : 'AORC v1.1 · S3 ▾');
    $('banner').style.display = s.synthetic ? 'block' : 'none';
    $('banner').textContent = 'Demo mode: synthetic weather shaped like AORC — not real data. Click the source badge to switch to the real archive.';
    const c = s.coverage;
    $('covpill').innerHTML = `calculated <b>${W.pct(c.percent_computed, 3)}</b> of 1979→`;
    return s;
  };
  W.pollJobs = async function (force) {
    try {
      const r = await W.api('/api/jobs'); S.jobs = r.jobs;
      const act = r.jobs.filter(j => j.status === 'running' || j.status === 'queued');
      const pill = $('jobpill');
      if (act.length) { const j = act[0]; pill.style.display = ''; pill.innerHTML = `<span class="spin"></span> job #${j.id} <b>${W.fmt(j.percent, 1)}%</b>${act.length > 1 ? ` (+${act.length - 1})` : ''}`; }
      else pill.style.display = 'none';
      if (S.tab === 'JOBS') W.drawJobList();
      const sig = JSON.stringify(act.map(j => [j.id, j.packets_done]));
      const changed = sig !== S._lastJobSig, finished = (S._lastActive || 0) > act.length;
      S._lastJobSig = sig; S._lastActive = act.length;
      if (changed || force) { await W.refreshStatus(); if ($('ly-cov').checked) W.paintCoverage(); }
      if (finished) { W.onSelectionChanged(true); if (S.tab === 'COV') W.renderTab(); W.toast('Background job finished — Show now reads the saved results.'); }
      clearTimeout(jobTimer); jobTimer = setTimeout(W.pollJobs, act.length ? 2000 : 8000);
    } catch { clearTimeout(jobTimer); jobTimer = setTimeout(W.pollJobs, 8000); }
  };
  W.cycleTab = function (d) {
    const i = W.FDI.indexOf(S.tab); S.tab = W.FDI[((i < 0 ? 0 : i) + d + 4) % 4];
    $('m-var').value = S.tab; W.paintMap(); W.renderTab();
  };

  /* ---------------- wiring ---------------- */
  function wire() {
    document.querySelectorAll('#modeseg .btn[data-mode]').forEach(b => b.onclick = () => W.setMode(b.dataset.mode));
    $('b-clear').onclick = () => { W.clearSel(); S.series = null; S.stepById = new Map(); S.trace = null; W.map.removeFeatureState({ source: 'cells' }); W.legend('no layer', 0, 1); W.renderTab(); W.status(''); };
    document.querySelectorAll('[data-hours]').forEach(b => b.onclick = () => { $('t-hours').value = b.dataset.hours; W.onWindowChanged(); });
    ['t-date', 't-hour', 't-hours', 't-spin'].forEach(id => $(id).addEventListener('change', W.onWindowChanged));
    $('t-show').onchange = e => { S.timeShow = e.target.value; W.redrawChart(); W.paintMap(); W.renderTrace(); };
    $('preset-pinery').onclick = () => { $('t-date').value = '2009-05-13'; $('t-hour').value = 0; $('t-hours').value = 216; $('t-spin').value = 168; W.onWindowChanged(); W.map.flyTo({ center: [-88.45, 46.68], zoom: 10.5 }); };
    ['c-fuel', 'c-fm', 'c-dl', 'c-coef', 'c-fpi', 'c-gap', 'c-clim'].forEach(id => $(id).addEventListener('change', onCfgChanged));
    $('c-reset').onclick = () => W.applyCfg(S.status.presets['hourly-default']);
    $('c-legacy').onclick = () => W.applyCfg(S.status.presets['legacy-batch']);
    $('b-show').onclick = () => W.run('auto'); $('b-live').onclick = () => W.run('live'); $('b-job').onclick = W.calcJob;
    $('m-var').onchange = () => { if (W.FDI.includes($('m-var').value) && S.tab !== $('m-var').value && W.FDI.includes(S.tab)) { S.tab = $('m-var').value; W.renderTab(); } W.paintMap(); };
    $('m-mode').onchange = () => { S.pctMode = $('m-mode').value === 'pct'; W.paintMap(); W.renderTab(); };
    $('m-hour').oninput = e => W.setHour(+e.target.value);
    $('m-play').onclick = () => { if (S.playing) { clearInterval(S.playing); S.playing = null; $('m-play').textContent = '▶'; return; }
      const max = +$('m-hour').max; if (!(max > 0)) return; $('m-play').textContent = '⏸';
      S.playing = setInterval(() => W.setHour((S.hourIdx + 1) % (max + 1)), S.mode === 'all' ? 700 : 160); };
    $('m-online').onchange = e => W.setOnline(e.target.checked);
    for (const [id, k] of [['ly-bricks', 'bricks'], ['ly-mesh', 'mesh'], ['ly-counties', 'counties'], ['ly-tz', 'tz'], ['ly-cov', 'cov'], ['ly-fires', 'fires']]) $(id).onchange = e => W.setLayer(k, e.target.checked);
    $('b-fit').onclick = () => W.map.fitBounds([[S.grid.bbox[0], S.grid.bbox[1]], [S.grid.bbox[2], S.grid.bbox[3]]], { padding: 24 });
    $('b-mappng').onclick = W.mapPng;
    document.querySelectorAll('.tab').forEach(t => t.onclick = () => { S.tab = t.dataset.tab; if (W.FDI.includes(S.tab)) { $('m-var').value = S.tab; W.paintMap(); } W.renderTab(); W.autosave(); });
    $('b-share').onclick = () => W.copyShareLink(); $('b-savesession').onclick = W.saveSessionPrompt; $('b-snapshot').onclick = W.snapshot;
    $('b-quit').onclick = async () => { if (!confirm('Stop the explorer? Unfinished jobs continue next time it starts.')) return;
      try { await W.api('/api/shutdown', { method: 'POST' }); } catch { /* server gone */ }
      document.body.innerHTML = '<div style="padding:40px;font:16px system-ui;color:#eceae4">The explorer has stopped. Close this tab, and start it again with WUP_Explorer whenever you like — your workspace is kept.</div>'; };
    $('srcpill').onclick = () => { S.tab = 'DATA'; W.renderTab(); };
    $('importfile').onchange = e => { if (e.target.files[0]) W.importFile(e.target.files[0]); e.target.value = ''; };
    $('savedsel').onchange = async e => { if (!e.target.value) return; const s = await W.api(`/api/selections/${e.target.value}`); W.setMode('cell'); W.selectMany(s.payload.point_ids, s.name); W.fitSelection(); e.target.value = ''; };
    const mapEl = $('map');
    mapEl.addEventListener('dragover', e => { e.preventDefault(); });
    mapEl.addEventListener('drop', e => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) W.importFile(f); });
    document.addEventListener('keydown', e => { if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
      if (e.key === 'ArrowRight' && S.series) W.setHour(Math.min(S.series.time.length - 1, S.hourIdx + 1));
      if (e.key === 'ArrowLeft' && S.series) W.setHour(Math.max(0, S.hourIdx - 1));
      if (e.key === ']') W.cycleTab(1); if (e.key === '[') W.cycleTab(-1); });
    window.addEventListener('beforeunload', W.autosave);
  }
  W.loadSavedSelections = async function () {
    try { const list = await W.api('/api/selections');
      $('savedsel').innerHTML = '<option value="">Saved selections…</option>' + list.map(s => `<option value="${s.id}">${W.esc(s.name)} (${s.n_cells.toLocaleString()})</option>`).join(''); } catch { /* ignore */ }
  };

  /* ---------------- boot ---------------- */
  async function boot() {
    try {
      const [status, catalog, grid, csv, counties, outline] = await Promise.all([W.api('/api/status'), W.api('/api/catalog'), W.api('/api/grid'),
        W.api('/api/points.csv'), W.api('/api/geo/wup_counties'), W.api('/api/geo/wup_outline')]);
      S.status = status; S.catalog = catalog; S.grid = grid; S.cells = W.parsePoints(csv); S.counties = counties; S.outline = outline;
      await W.refreshStatus();
      for (let h = 0; h < 24; h++) $('t-hour').insertAdjacentHTML('beforeend', `<option value="${h}">${W.pad2(h)}:00</option>`);
      $('t-hour').value = 0;
      $('c-fuel').innerHTML = Object.entries(status.fuel_models).map(([k, n]) => `<option value="${k}">${k} — ${W.esc(n)}</option>`).join('');
      $('fdichecks').innerHTML = W.FDI.map(f => `<label class="chk"><input type="checkbox" class="fdi" value="${f}" checked><span class="dot" style="background:${W.COLOR[f]}"></span>${f}</label>`).join('');
      await W.initMap(); W.addLayers(); wire();
      W.loadFires();
      W.map.fitBounds([[grid.bbox[0], grid.bbox[1]], [grid.bbox[2], grid.bbox[3]]], { padding: 24, duration: 0 });
      W.applyCfg(status.presets['hourly-default']);
      W.legend('no layer', 0, 1); W.setMode('cell'); W.onWindowChanged();
      W.loadSavedSelections(); W.pollJobs();
      const m = location.hash.match(/#s=([\w-]+)/);
      let st = null;
      if (m) { try { st = JSON.parse(await W.b64url.dec(m[1])); } catch { W.toast('That share link could not be read.'); } }
      if (!st) { try { st = JSON.parse(localStorage.getItem('wup-last-view') || 'null'); if (st) st.ran = false; } catch { st = null; } }
      if (st) await W.applyState(st); else W.renderTab();
    } catch (e) {
      console.error(e);
      $('overlaycard').innerHTML = `<h2 style="margin-top:0">The explorer's engine isn't running</h2><p>This page talks to the WUP Grid Explorer program. Start it with <b>WUP_Explorer</b> (double-click), or from a terminal:</p>
        <p><code>python -m aorc_tools.explorer</code></p><p>It opens this page automatically. To use a copy hosted elsewhere, add <code>?api=http://127.0.0.1:8765</code> to the address.</p><p class="hint">${W.esc(e.message)}</p>`;
      $('overlay').style.display = 'flex';
    }
  }
  boot();
})();
