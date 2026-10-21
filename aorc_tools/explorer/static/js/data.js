/* Data tab: sources (online / offline / climate datasets), input stores, NDVI ingest, verification, research bundle, tasks */
'use strict';
(function () {
  const S = W.S, $ = id => document.getElementById(id);
  S.data = { inv: null, ds: null, plan: null, msg: '', parts: null, timer: null };
  const ST = { running: 'running', queued: 'paused', done: 'done', 'done-with-errors': 'paused', error: 'error', cancelled: 'paused' };
  const KIND = { 'aorc-download': 'AORC download', 'ndvi-ingest': 'NDVI ingest', verify: 'Verify', bundle: 'Bundle', 'bundle-check': 'Bundle check', 'dataset-localize': 'Dataset offline copy' };
  const BADGE = { PASS: 'fixed', WARN: 'open', FAIL: 'open', INFO: 'caveat' };
  const PART_ORDER = ['app', 'results', 'inputs_aorc', 'ndvi', 'inputs_datasets', 'catalog'];

  async function load(full) {
    const jobs = [W.api('/api/data').then(r => { S.data.inv = r; })];
    if (full || !S.data.ds) jobs.push(W.api('/api/datasets').then(r => { S.data.ds = r; }).catch(() => {}));
    if (full || !S.data.plan) jobs.push(W.api('/api/bundle/plan').then(r => { S.data.plan = r; if (!S.data.parts) S.data.parts = Object.keys(r.labels); }).catch(() => {}));
    await Promise.all(jobs);
  }

  W.openData = async function () { S.tab = 'DATA'; W.renderTab(); };

  W.renderData = async function (b) {
    b.innerHTML = '<div class="sect"><span class="spin"></span> reading the workspace…</div>';
    try { await load(true); } catch (e) { b.innerHTML = `<div class="sect callout bad">Error: ${W.esc(e.message)}</div>`; return; }
    draw(b);
  };

  function sourceRows() {
    const st = S.status || {}, av = st.source?.available || {}, act = st.source?.active, inv = S.data.inv;
    return Object.entries(av).map(([k, label]) => {
      const isA = k === act;
      let note = '';
      if (k === 'aorc-s3') note = inv.offline ? 'offline: local store only' : 'online (S3), keeps a local copy';
      else if (k === 'synthetic') note = 'demo weather (not real)';
      else { const d = (S.data.ds?.datasets || []).find(x => x.source === k); note = d ? `${d.preset} · ${d.status?.start ? String(d.status.start).slice(0, 10) + ' → ' + String(d.status.end).slice(0, 10) : (d.status?.error ? 'error' : '')}` : 'dataset'; }
      return `<tr class="${isA ? 'fdi' : ''}"><td><b>${W.esc(label)}</b> <small class="r mono">${W.esc(k)}</small></td><td class="r">${W.esc(note)}</td>
        <td style="white-space:nowrap;text-align:right">${isA ? '<span class="tag fixed">active</span>' : `<button class="btn sm" data-use="${W.esc(k)}">Use</button>`}</td></tr>`;
    }).join('');
  }

  function yearStrip(per, d0, d1, pick) {
    const ys = []; for (let y = d0; y <= d1; y++) ys.push(y);
    return `<div style="display:flex;flex-wrap:wrap;gap:2px;margin:6px 0">${ys.map(y => { const v = pick(per[y]); const a = v == null ? 0 : Math.max(0.12, Math.min(1, v));
      return `<span title="${y}: ${v == null ? 'nothing' : W.pct(100 * v, 1)}" style="width:14px;height:14px;border-radius:2px;background:${v ? `rgba(57,135,229,${a})` : 'var(--panel-3)'}"></span>`; }).join('')}</div>
      <div class="hint" style="display:flex;justify-content:space-between"><span>${d0}</span><span>${d1}</span></div>`;
  }

  function tasksHtml(list) {
    if (!list.length) return '<p class="hint">No data tasks yet.</p>';
    return list.map(t => {
      const r = t.result || {};
      let res = '';
      if (t.kind === 'verify' && r.overall) res = `<div class="row"><span class="tag ${BADGE[r.overall]}">${r.overall}</span> completeness ${W.pct(r.completeness_percent, 2)} · ${r.files_checked} files opened
        ${r.report_export_id ? `<a href="${W.API}/api/exports/${r.report_export_id}?inline=1" target="_blank">open report</a>` : ''}</div>
        <div class="row" style="gap:4px">${Object.entries(r.checks || {}).filter(([, v]) => v !== 'INFO').map(([k, v]) => `<span class="tag ${BADGE[v]}" title="${W.esc(k)}">${W.esc(k.replace(/_/g, ' '))}</span>`).join('')}</div>`;
      else if (t.kind === 'aorc-download' && r.downloaded != null) res = `<div class="hint">downloaded ${r.downloaded}, already there ${r.already_present}, errors ${r.n_errors} · store ${W.bytes(r.store_bytes)}</div>`;
      else if (t.kind === 'dataset-localize' && r.files) res = `<div class="hint">${r.files} files · ${W.bytes(r.bytes)} · ${W.esc(String(r.start).slice(0, 10))} → ${W.esc(String(r.end).slice(0, 10))}</div>`;
      else if (t.kind === 'bundle' && r.path) res = `<div class="hint mono">${W.esc(r.path)} · ${W.bytes(r.bytes)} · ${r.files} files</div>`;
      else if (t.kind === 'bundle-check' && r.status) res = `<div class="row"><span class="tag ${BADGE[r.status]}">${r.status}</span> ${r.ok} files verified, ${(r.bad || []).length} changed, ${(r.missing || []).length} missing</div>`;
      else if (t.kind === 'ndvi-ingest' && t.status.startsWith('done')) res = `<div class="hint">${W.esc(JSON.stringify(r).slice(0, 220))}</div>`;
      const act = t.status === 'running' ? `<button class="btn sm" data-task="${t.id}" data-act="cancel">Cancel</button>`
        : ['cancelled', 'error', 'done-with-errors'].includes(t.status) ? `<button class="btn sm" data-task="${t.id}" data-act="resume">Resume</button>` : '';
      return `<div class="job"><div class="top"><span class="st ${ST[t.status] || ''}">${W.esc(t.status)}</span><b class="grow">${W.esc(KIND[t.kind] || t.kind)}</b>
        <span class="r">#${t.id}</span>${act}${t.status !== 'running' ? `<button class="btn sm ghost" data-task="${t.id}" data-act="delete" title="Remove from the list (keeps files)">✕</button>` : ''}</div>
        <div class="hint">${W.esc(t.name)}</div>
        ${t.status === 'running' || t.status === 'queued' ? `<div class="bar" style="margin:5px 0"><i style="width:${Math.max(1, t.percent)}%"></i></div><div class="hint">${W.fmt(t.percent, 1)}% · ${W.esc(t.note || '')}</div>` : ''}
        ${t.error ? `<div class="callout bad" style="margin:4px 0">${W.esc(t.error)}</div>` : ''}${res}</div>`;
    }).join('');
  }

  function draw(b) {
    const inv = S.data.inv, a = inv.aorc, st = S.status || {}, set = st.settings || {};
    const d0 = +(set.domain_start || '1979').slice(0, 4), d1 = +(set.domain_end || '2026').slice(0, 4);
    const nd = inv.ndvi || {}, prods = nd.products || {};
    const ds = S.data.ds || { datasets: [], presets: {} };
    const pl = S.data.plan;
    b.innerHTML = `<div class="sect"><h2>Data: inputs, sources, verification and the research bundle</h2>
      <div class="callout">Everything the four indices need lives in this workspace: the <b>AORC input store</b> (every downloaded hour, so the record can be
      recomputed offline), the <b>NDVI inputs</b> for FPI, optional <b>climate-model datasets</b> (e.g. GLARM futures), the <b>results</b>, and verification reports.
      <b>Verify</b> checks the saved record; <b>Research bundle</b> packs it all into one zip anyone can unzip and run.</div>
      ${S.data.msg ? `<div class="callout">${S.data.msg}</div>` : ''}

      <h3>Weather source</h3>
      <table class="t"><tbody>${sourceRows()}</tbody></table>
      <div class="row"><label class="chk" title="Read AORC only from the local input store — no internet needed"><input type="checkbox" id="dt-offline" ${inv.offline ? 'checked' : ''}> offline (local AORC store only)</label>
        <label class="chk" title="Keep every AORC chunk downloaded while computing (needed for offline use and bundles)"><input type="checkbox" id="dt-keep" ${inv.keep_inputs ? 'checked' : ''}> keep downloaded AORC</label></div>

      <h3>AORC input store</h3>
      <div class="stats"><div class="stat"><b>${W.pct(a.percent, 2)}</b><span>of ${d0}–${d1}</span></div><div class="stat"><b>${W.bytes(a.bytes)}</b><span>on disk</span></div>
        <div class="stat"><b>${a.brick_chunks.toLocaleString()}</b><span>brick-chunks</span></div>
        <div class="stat"><b>${a.full_record_estimate_bytes ? W.bytes(a.full_record_estimate_bytes) : '60–130 GB'}</b><span>full record (est.)</span></div></div>
      ${yearStrip(a.per_year || {}, d0, d1, v => v && v.brick_chunks && v.expected ? v.brick_chunks / v.expected : null)}
      <div class="row"><label>Download</label><input type="date" id="dt-a0" value="${(set.domain_start || '1979-02-01').slice(0, 10)}"> → <input type="date" id="dt-a1" value="${(set.domain_end || '2026-09-30').slice(0, 10)}">
        <button class="btn sm primary" id="dt-adl" title="Fetch every brick for the period into the local store (skips what is there; resumable)">Download</button></div>
      <p class="hint mono">${W.esc(a.dir)}</p>

      <h3>NDVI for FPI</h3>
      <div class="row">${nd.available ? '<span class="tag fixed">envelope ready</span>' : '<span class="tag open">no NDVI yet — FPI is not computed</span>'}
        ${Object.entries(prods).map(([p, ys]) => `<span class="tag caveat" title="${W.esc(ys.join(', '))}">${W.esc(p)} ${ys.length ? ys[0] + '–' + ys[ys.length - 1] : ''} (${ys.length} y)</span>`).join(' ')}
        ${nd.climatology ? '<span class="tag caveat">DOY climatology</span>' : ''}</div>
      <div class="row"><label>From</label><select id="dt-nb">
          <option value="planetary">Microsoft Planetary Computer (no login)</option><option value="appeears">NASA AppEEARS (Earthdata login)</option>
          <option value="cdr">NOAA AVHRR CDR 5 km (pre-2000, opt-in)</option><option value="local">Local folder of rasters</option></select>
        <select id="dt-np"><option>MOD13Q1</option><option>MOD13A1</option><option>MOD13A2</option></select></div>
      <div class="row"><label>Years</label><input type="number" id="dt-n0" value="2000" min="1981" max="2030"> – <input type="number" id="dt-n1" value="${Math.min(d1, new Date().getFullYear())}" min="1981" max="2030">
        <button class="btn sm primary" id="dt-ndl">Build NDVI inputs</button></div>
      <div class="row" id="dt-cred" style="display:none"><input type="text" id="dt-nu" placeholder="Earthdata username" autocomplete="username"><input type="password" id="dt-npw" placeholder="password" autocomplete="current-password">
        <span class="hint">kept in memory for this task only, never written to disk</span></div>
      <div class="row" id="dt-fold" style="display:none"><input type="text" id="dt-nf" class="grow" placeholder="folder with MOD13 GeoTIFF/HDF/NetCDF files"></div>
      <p class="hint">MODIS (2000 →) is the locked FPI input. AVHRR CDR and the day-of-year climatology are used only when a configuration ticks
      “allow non-MODIS NDVI” (pre-2000 or future FPI). Written to <span class="mono">${W.esc(inv.static_dir)}</span>.</p>

      <h3>Climate datasets (futures, reanalyses, model runs)</h3>
      ${(ds.datasets || []).length ? `<table class="t"><thead><tr><th>Dataset</th><th>Period</th><th></th></tr></thead><tbody>
        ${ds.datasets.map(d => `<tr><td><b>${W.esc(d.name)}</b> <small class="r">${W.esc(d.preset)}${d.scenario ? ' · ' + W.esc(d.scenario) : ''}</small>
            ${d.local_copy ? ' <span class="tag fixed" title="Reads the offline copy in the workspace (bundled with inputs_datasets)">offline copy</span>' : ''}
            ${d.status?.warning ? ` <span class="tag open" title="${W.esc(d.status.warning)}">coarse time step</span>` : ''}
            <div class="r mono" style="overflow-wrap:anywhere">${W.esc((d.paths || []).join(' · ')).slice(0, 160)}</div></td>
          <td class="r" style="white-space:nowrap">${d.status?.error ? `<span class="tag open" title="${W.esc(d.status.error)}">error</span>`
            : `${W.esc(String(d.status?.start || '').slice(0, 10))}<br>→ ${W.esc(String(d.status?.end || '').slice(0, 10))}${d.status?.step_hours ? `<br>${W.fmt(d.status.step_hours, 0)}-hourly` : ''}`}</td>
          <td><div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end"><button class="btn sm" data-use="${W.esc(d.source)}">Use</button>
            ${d.local_copy ? '' : `<button class="btn sm" data-dsloc="${W.esc(d.slug)}" title="Copy the WUP window of every file into the workspace: works offline and goes into research bundles">Offline copy</button>`}
            <button class="btn sm ghost" data-dsdel="${W.esc(d.slug)}" title="Remove the definition (files and results stay)">Remove</button></div></td></tr>`).join('')}</tbody></table>` : '<p class="hint">None yet. GLARM (Xue et al.) and other RegCM, WRF, CMIP/CORDEX or ERA5 files can be added; they are regridded to the 800 m cells and run through the same four FDIs.</p>'}
      <details style="margin-top:6px"><summary class="btn sm" style="display:inline-block">Add a dataset…</summary>
        <div class="row"><label>Name</label><input type="text" id="dt-dn" class="grow" placeholder="GLARM RCP8.5 MPI-ESM-MR 2080–2099"></div>
        <div class="row"><label>Preset</label><select id="dt-dp">${Object.keys(ds.presets || {}).map(k => `<option value="${k}">${k}</option>`).join('')}</select><span class="hint" id="dt-dph"></span></div>
        <div class="row"><label>Scenario</label><input type="text" id="dt-dsc" placeholder="RCP8.5 / historical"><input type="text" id="dt-dg" placeholder="GCM (optional)"></div>
        <div class="row"><label>Files</label><textarea id="dt-df" rows="3" class="grow" placeholder="one path, glob (D:\\GLARM\\*.nc) or URL (https://…, s3://…) per line"></textarea></div>
        <div class="row"><button class="btn sm primary" id="dt-dadd">Add &amp; probe</button><span class="hint">opens the files, finds the variables and dates, and checks one brick</span></div>
      </details>

      <h3>Verify the saved record</h3>
      <p class="hint">Completeness of every year × 144-h chunk × brick, moisture-code continuity across chunks (start state = previous end state),
      value ranges, ERC/BI not clipped, no uniform fields, daily FWI changes only at 12:00 LST, FPI only in the MODIS era, NaN only where the weather tier is 9,
      FWI/ERC/BI coverage, and FPI coverage in April–October (needs NDVI).
      Uses the configuration in panel 3 (<span class="mono">${W.esc($('c-id')?.textContent || '')}</span>) and the active source.</p>
      <div class="row"><button class="btn sm primary" data-verify="quick" title="Catalog for every chunk + the first file of each year">Quick check</button>
        <button class="btn sm" data-verify="full" title="Opens every saved file">Full verification</button></div>

      <h3>Research bundle</h3>
      ${pl ? `<table class="t"><tbody>${PART_ORDER.filter(k => pl.labels[k]).map(k => { const lab = pl.labels[k], p = pl.parts[k] || { bytes: 0, files: 0 };
        return `<tr><td><label class="chk" style="white-space:normal;align-items:flex-start"><input type="checkbox" data-part="${k}" ${S.data.parts.includes(k) ? 'checked' : ''}> ${W.esc(lab)}</label></td><td class="v">${W.bytes(p.bytes)}</td><td class="v">${p.files.toLocaleString()} files</td></tr>`; }).join('')}
        <tr class="stage"><td>Total (NetCDF is already compressed, so the zip is about this size)</td><td class="v" id="dt-btot"></td><td></td></tr></tbody></table>
      <div class="row"><label>Save as</label><input type="text" id="dt-bp" class="grow" value="${W.esc(pl.default_path)}"></div>
      <div class="row"><button class="btn sm primary" id="dt-bmk">Make bundle</button><input type="text" id="dt-bc" class="grow" placeholder="path of a bundle zip to check"><button class="btn sm" id="dt-bck">Check</button></div>
      <p class="hint">The zip holds a README, a MANIFEST.json with the SHA-256 of every file and the record's provenance (configurations, domain, input coverage,
      NDVI products, latest verification, citations), the program, and the workspace. Unzip anywhere and start it; the catalog re-points itself.</p>` : '<p class="hint">…</p>'}

      <h3>Data tasks</h3><div id="dt-tasks">${tasksHtml(inv.tasks || [])}</div></div>`;
    wire(b);
    total();
    schedule();
  }

  function total() {
    const el = $('dt-btot'); if (!el || !S.data.plan) return;
    el.textContent = W.bytes(S.data.parts.reduce((s, k) => s + (S.data.plan.parts[k]?.bytes || 0), 0));
  }

  async function act(fn, ok) {
    try { const r = await fn(); S.data.msg = ok ? ok(r) : ''; } catch (e) { S.data.msg = `<b style="color:var(--bad)">${W.esc(e.message)}</b>`; }
    if (S.tab === 'DATA') { await load(false); draw($('tabbody')); }
  }

  function wire(b) {
    b.querySelectorAll('[data-use]').forEach(x => x.onclick = () => act(async () => { await W.api('/api/source', { json: { name: x.dataset.use } }); await W.refreshStatus(); S.series = null; S.trace = null; W.onSelectionChanged(); },
      () => `Source switched to <b>${W.esc(x.dataset.use)}</b>.`));
    $('dt-offline').onchange = e => act(async () => { await W.api('/api/data/offline', { json: { offline: e.target.checked } }); await W.refreshStatus(); },
      () => e.target.checked ? 'Offline: AORC is read only from the local store; hours not in it are reported as missing.' : 'Online: AORC comes from S3 and is kept locally.');
    $('dt-keep').onchange = e => act(() => W.api('/api/settings', { json: { keep_inputs: e.target.checked } }));
    $('dt-adl').onclick = () => act(() => W.api('/api/data/aorc', { json: { start: $('dt-a0').value + 'T00', end: $('dt-a1').value + 'T23' } }), r => `Download queued (task #${r.id}).`);
    const nb = $('dt-nb');
    nb.onchange = () => { $('dt-cred').style.display = nb.value === 'appeears' ? '' : 'none'; $('dt-fold').style.display = nb.value === 'local' ? '' : 'none'; $('dt-np').disabled = nb.value === 'cdr';
      if (nb.value === 'cdr') { $('dt-n0').value = 1981; $('dt-n1').value = 1999; } };
    $('dt-ndl').onclick = () => act(() => W.api('/api/data/ndvi', { json: { backend: nb.value, product: $('dt-np').value, years: [+$('dt-n0').value, +$('dt-n1').value],
      folder: $('dt-nf').value || undefined, username: $('dt-nu').value || undefined, password: $('dt-npw').value || undefined } }), r => `NDVI ingest queued (task #${r.id}).`);
    const dp = $('dt-dp');
    if (dp) { const hint = () => { $('dt-dph').textContent = (S.data.ds?.presets || {})[dp.value] || ''; }; dp.onchange = hint; hint(); }
    $('dt-dadd').onclick = () => act(() => W.api('/api/datasets', { json: { name: $('dt-dn').value, preset: dp.value, scenario: $('dt-dsc').value || undefined, gcm: $('dt-dg').value || undefined, paths: $('dt-df').value } }),
      r => `Added <b>${W.esc(r.dataset.name)}</b> (${W.esc(r.source)}): ${r.missing.length ? 'missing ' + W.esc(r.missing.join(', ')) : 'all variables found'}${r.model_resolution_km ? ` · model grid ≈ ${W.fmt(r.model_resolution_km, 1)} km` : ''}${r.status?.step_hours ? ` · ${W.fmt(r.status.step_hours, 0)}-hourly` : ''}.${r.status?.warning ? `<br><b style="color:#f0c27a">${W.esc(r.status.warning)}</b>` : ''}`);
    b.querySelectorAll('[data-dsloc]').forEach(x => x.onclick = () => act(() => W.api(`/api/datasets/${x.dataset.dsloc}/localize`, { method: 'POST' }), r => `Offline copy queued (task #${r.id}).`));
    b.querySelectorAll('[data-dsdel]').forEach(x => x.onclick = () => { if (confirm('Remove this dataset definition? (its files and any results stay)')) act(() => W.api(`/api/datasets/${x.dataset.dsdel}`, { method: 'DELETE' }).then(W.refreshStatus)); });
    b.querySelectorAll('[data-verify]').forEach(x => x.onclick = () => act(() => W.api('/api/data/verify', { json: { config: S.cfg, mode: x.dataset.verify } }), r => `Verification queued (task #${r.id}).`));
    b.querySelectorAll('[data-part]').forEach(x => x.onchange = () => { S.data.parts = [...b.querySelectorAll('[data-part]:checked')].map(y => y.dataset.part); total(); });
    if ($('dt-bmk')) {
      $('dt-bmk').onclick = () => act(() => W.api('/api/bundle', { json: { path: $('dt-bp').value, parts: S.data.parts } }), r => `Bundle queued (task #${r.id}) → <span class="mono">${W.esc(r.params.path)}</span>.`);
      $('dt-bck').onclick = () => act(() => W.api('/api/bundle/check', { json: { path: $('dt-bc').value || $('dt-bp').value } }), r => `Check queued (task #${r.id}).`);
    }
    b.querySelectorAll('[data-task]').forEach(x => x.onclick = () => act(() => x.dataset.act === 'delete' ? W.api(`/api/tasks/${x.dataset.task}`, { method: 'DELETE' })
      : W.api(`/api/tasks/${x.dataset.task}/${x.dataset.act}`, { method: 'POST' })));
  }

  function schedule() {
    clearTimeout(S.data.timer);
    const busy = (S.data.inv?.tasks || []).some(t => t.status === 'running' || t.status === 'queued');
    S.data.timer = setTimeout(async () => {
      if (S.tab !== 'DATA') return;
      try {
        const r = await W.api('/api/tasks'); const was = JSON.stringify((S.data.inv.tasks || []).map(t => [t.id, t.status]));
        S.data.inv.tasks = r.tasks.slice(0, 20);
        const el = $('dt-tasks'); if (el) { el.innerHTML = tasksHtml(S.data.inv.tasks); wire($('tabbody')); }
        if (was !== JSON.stringify(S.data.inv.tasks.map(t => [t.id, t.status]))) { await load(true); draw($('tabbody')); return; }
      } catch { /* server busy */ }
      schedule();
    }, busy ? 1500 : 6000);
  }
})();
