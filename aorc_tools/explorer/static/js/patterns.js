/* Patterns: fire-season windows year by year (staffing / funding dates), groupings of high-danger cells, daily vs hourly FWI */
'use strict';
(function () {
  const S = W.S;
  S.pat = { list: [], summary: null, index: 'FWI', area: 'WUP mainland', task: null, msg: '', cellKey: 'FWI_compare.kappa',
    drivers: null, dpart: 'all', dtask: null, dmsg: '' };
  const DCOL = { temperature: '#e8a04a', humidity: '#3987e5', wind: '#9085e9', rain_72h: '#199e70', fuel_dryness_BUI: '#d95926',
    fuel_moisture_1000h: '#d95926', greenness: '#7fb069' };
  const IDX = ['FWI', 'FWI_daily', 'ERC', 'BI', 'FPI'];
  const IDX_LABEL = { FWI: 'FWI hourly (daily max)', FWI_daily: 'FWI classic daily', ERC: 'ERC', BI: 'BI', FPI: 'FPI' };
  const NS = 'http://www.w3.org/2000/svg';
  const doyLabel = d => d == null || !isFinite(d) ? '—' : new Date(Date.UTC(2001, 0, 1 + Math.round(d))).toISOString().slice(5, 10);
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const CELLS = {
    'FWI_compare.kappa': 'daily vs hourly FWI: agreement on high days (kappa)', 'FWI_compare.rank_r': 'daily vs hourly FWI: rank correlation',
    'FWI_compare.daily_adequate': 'daily FWI adequate (1) or hourly needed (0)', 'FWI_compare.afternoon_excess': 'afternoon peak above the noon value (FWI)',
    'FWI_compare.hit_rate': 'share of hourly high days the daily FWI also flags', 'FWI_compare.false_alarm_ratio': 'daily high days the hourly system does not flag' };
  const IDX_CELLS = { spring_start_median: 'spring window opens (median date)', spring_end_median: 'spring window closes',
    fall_start_median: 'fall window opens', fall_end_median: 'fall window closes', spring_days_mean: 'spring window days / yr',
    fall_days_mean: 'fall window days / yr', spring_start_trend: 'trend: spring opening (days/decade)', fall_end_trend: 'trend: fall closing (days/decade)',
    spring_days_trend: 'trend: spring window days (per decade)', fall_days_trend: 'trend: fall window days (per decade)',
    large_group_days_per_year: `days/yr in a high-danger grouping ≥ 100 km²`, share_of_high_days_in_large_group: 'share of high days inside a large grouping' };

  W.loadPatterns = async function () {
    try { S.pat.list = (await W.api('/api/patterns')).analyses; } catch (e) { S.pat.list = []; }
    const src = S.status?.source?.active || '';
    const a = S.pat.list.find(x => x.source === src) || S.pat.list[0];
    S.pat.summary = null;
    if (a) try { S.pat.summary = await W.api(`/api/patterns/${encodeURIComponent(a.id)}`); } catch (e) { /* none yet */ }
    if (S.pat.summary && !S.pat.summary.indices[S.pat.index]) S.pat.index = Object.keys(S.pat.summary.indices)[0] || 'FWI';
    try { const dl = (await W.api('/api/drivers')).analyses; const d = dl.find(x => x.source === src) || dl[0];
      S.pat.drivers = d ? await W.api(`/api/drivers/${encodeURIComponent(d.id)}`) : null; } catch (e) { S.pat.drivers = null; }
    if (S.tab === 'PAT') W.renderTab();
  };

  const trendTxt = t => !t || t.tau == null ? '—' : `${t.tau > 0 ? '↑' : t.tau < 0 ? '↓' : '·'} ${t.per_decade != null ? (t.per_decade > 0 ? '+' : '') + W.fmt(t.per_decade, 1) + '/decade' : ''} <small class="r">p ${W.fmt(t.p_hr ?? t.p, 3)}${(t.p_hr ?? t.p) < 0.05 ? ' ✓' : ''}</small>`;

  /* year-by-year season windows: one row per year, spring and fall bars (median start->end, whiskers p10 start / p90 end) */
  function seasonStrip(box, rows, years) {
    box.innerHTML = '';
    const Wd = Math.max(360, box.clientWidth || 560), rh = 14, top = 26, left = 44, right = 10;
    const H = top + years.length * rh + 20;
    const x0 = 59, x1 = 334;                                   // Mar 1 .. Nov 30 (365-day calendar)
    const X = d => left + (Wd - left - right) * (d - x0) / (x1 - x0);
    const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', `0 0 ${Wd} ${H}`); svg.setAttribute('width', Wd); svg.setAttribute('height', H);
    const mk = (tag, a, text) => { const e = document.createElementNS(NS, tag); for (const k in a) e.setAttribute(k, a[k]); if (text != null) e.textContent = text; svg.appendChild(e); return e; };
    mk('rect', { x: 0, y: 0, width: Wd, height: H, fill: '#1c1e21' });
    mk('text', { x: left, y: 14, fill: '#eceae4', 'font-size': 12, 'font-weight': 600, 'font-family': 'Inter,system-ui,sans-serif' },
      'High-danger windows by year (median of the area\'s cells; whiskers 10–90%)');
    for (let m = 2; m <= 11; m++) {
      const d = (Date.UTC(2001, m, 1) - Date.UTC(2001, 0, 1)) / 864e5;
      if (d < x0 || d > x1) continue;
      mk('line', { x1: X(d), x2: X(d), y1: top - 4, y2: H - 18, stroke: 'rgba(255,255,255,0.08)' });
      mk('text', { x: X(d) + 2, y: H - 6, fill: '#9a978d', 'font-size': 10 }, MONTHS[m]);
    }
    const by = {}; rows.forEach(r => { (by[r.year] = by[r.year] || {})[r.window] = r; });
    years.forEach((y, i) => {
      const yy = top + i * rh;
      if (i % 2 === 0 || years.length < 25) mk('text', { x: left - 4, y: yy + 10, fill: '#9a978d', 'font-size': 10, 'text-anchor': 'end' }, String(y));
      for (const [w, col] of [['spring', '#7fb069'], ['fall', '#e8a04a']]) {
        const r = by[y]?.[w]; if (!r || r.start == null) continue;
        mk('line', { x1: X(r.start_p10 ?? r.start), x2: X(r.end_p90 ?? r.end), y1: yy + 6, y2: yy + 6, stroke: col, 'stroke-opacity': 0.45, 'stroke-width': 1 });
        mk('rect', { x: X(r.start), y: yy + 2, width: Math.max(2, X(r.end) - X(r.start)), height: 9, rx: 2, fill: col, 'fill-opacity': 0.25 + 0.65 * (r.share_with_window ?? 1) });
        const t = mk('title', {}); t.textContent = `${y} ${w}: ${doyLabel(r.start)} → ${doyLabel(r.end)} (10–90%: ${doyLabel(r.start_p10)} → ${doyLabel(r.end_p90)}), ${W.fmt(r.days, 0)} days, ${W.pct(100 * (r.share_with_window ?? 0), 0)} of cells`;
        svg.lastChild.previousSibling.appendChild(t);
      }
    });
    box.appendChild(svg);
  }

  async function runPatterns() {
    const idx = [...document.querySelectorAll('[data-pidx]:checked')].map(x => x.dataset.pidx);
    try {
      const t = await W.api('/api/patterns/run', { json: { config: S.cfg, indices: idx.length ? idx : null, season_pct: +$('pt-sp').value || 80, cluster_pct: +$('pt-cp').value || 95 } });
      S.pat.task = t.id; S.pat.msg = `<span class="spin"></span> running (task #${t.id})…`; W.renderTab(); poll();
    } catch (e) { W.toast('Error: ' + W.esc(e.message)); }
  }
  async function poll() {
    if (!S.pat.task) return;
    let t; try { t = (await W.api('/api/tasks')).tasks.find(x => x.id === S.pat.task); } catch (e) { setTimeout(poll, 3000); return; }
    if (!t) { S.pat.task = null; return; }
    if (t.status === 'done') { S.pat.task = null; S.pat.msg = 'Finished.'; await W.loadPatterns(); return; }
    if (t.status === 'error' || t.status === 'cancelled') { S.pat.task = null; S.pat.msg = `<span style="color:#f0a35e">${W.esc(t.status)}: ${W.esc(t.error || '')}</span>`; if (S.tab === 'PAT') W.renderTab(); return; }
    S.pat.msg = `<span class="spin"></span> ${W.esc(t.note || '')} · ${t.total ? Math.round(100 * t.done / t.total) : 0}%`;
    const el = $('pt-msg'); if (el) el.innerHTML = S.pat.msg;
    setTimeout(poll, 2000);
  }

  async function paint(key) {
    const sm = S.pat.summary; if (!sm) return;
    try {
      const f = await W.api(`/api/patterns/${encodeURIComponent(sm.id)}/cells?key=${encodeURIComponent(key)}`);
      let lo = f.lo, hi = f.hi; const div = /_trend$/.test(key);
      if (div) { const a = Math.max(Math.abs(lo), Math.abs(hi)) || 1; lo = -a; hi = a; }
      if (/daily_adequate/.test(key)) { lo = 0; hi = 1; }
      W.paintValues(f.point_ids, f.values, lo, hi, false, div);
      const lab = CELLS[key] || IDX_CELLS[key.split('.').slice(1).join('.')] || key;
      W.legend(lab, lo, hi, /_median$/.test(key) ? 'day of year (60 = Mar 1, 152 = Jun 1, 244 = Sep 1)' : '', false, div);
    } catch (e) { W.toast('Error: ' + W.esc(e.message)); }
  }

  W.renderPatterns = function (b) {
    const P = S.pat, sm = P.summary, ix = sm?.indices?.[P.index];
    let html = `<div class="sect"><h2>Patterns: fire seasons, groupings, daily vs hourly</h2>
      <div class="callout"><b>Season windows</b> — when the spring and fall high-danger windows open and close each year, per area: dates for seasonal staffing and funding,
      and how they are shifting. <b>Groupings</b> — on each day, the connected areas of cells above the 95th percentile: how many and how large.
      <b>Daily vs hourly FWI</b> — where the classic noon FWI agrees with the hourly system and where it misses the afternoon peak.
      <b>Driving variables</b> — per brick, how much of each index's variability temperature, humidity, wind, rain and fuel dryness explain, and which is most extreme on high days.
      ${S.status?.synthetic ? '<br><b style="color:#f0c27a">Demo mode: synthetic weather.</b>' : ''}</div>
      <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
        ${IDX.map(v => `<label class="chk"><input type="checkbox" data-pidx="${v}" ${!sm || sm.indices?.[v] ? 'checked' : ''}> ${IDX_LABEL[v]}</label>`).join('')}
        <label title="A season-window day: 15-day smoothed daily maximum at or above this regional percentile">season pct <input id="pt-sp" type="number" min="50" max="99" value="${sm?.season_pct ?? 80}" style="width:52px"></label>
        <label title="A grouping: connected cells at or above this regional percentile">grouping pct <input id="pt-cp" type="number" min="50" max="99.9" value="${sm?.cluster_pct ?? 95}" style="width:52px"></label>
        <button class="btn" id="pt-run" ${P.task ? 'disabled' : ''}>Run patterns</button></div>
      <div class="hint">Uses every saved year (the full record: Run_Full_Record.bat). Re-runs reuse the cached daily maxima.</div>
      <div id="pt-msg" class="hint">${P.msg || ''}</div>`;
    if (sm) {
      html += `<div class="row" style="gap:6px;flex-wrap:wrap;margin-top:6px">${IDX.filter(v => sm.indices[v] && !sm.indices[v].skipped).map(v => `<button class="btn sm ${v === P.index ? '' : 'ghost'}" data-pix="${v}">${IDX_LABEL[v]}</button>`).join('')}
        <small class="r" style="align-self:center">${W.esc(sm.source)} · ${sm.years[0]}–${sm.years[sm.years.length - 1]} · ${W.esc(sm.created)}</small></div>`;
    }
    if (ix && !ix.skipped) {
      const areas = Object.keys(ix.areas), A = ix.areas[P.area] || ix.areas[areas[0]];
      const env = A?.envelope || {}, tr = A?.trends || {};
      html += `<h3>Season windows</h3>
        <div class="row" style="gap:6px;align-items:center">Area <select id="pt-area">${areas.map(a => `<option ${a === P.area ? 'selected' : ''}>${W.esc(a)}</option>`).join('')}</select>
          <small class="r">threshold: smoothed daily max ≥ ${W.fmt(ix.thresholds.season)} (regional ${sm.season_pct}th percentile)</small></div>
        <table class="t" style="margin-top:6px"><thead><tr><th>Window</th><th>Envelope, whole record</th><th>Envelope, last 20 years</th><th>Opens</th><th>Closes</th><th>Days</th></tr></thead><tbody>
        ${['spring', 'fall'].map(w => `<tr><td><b>${w}</b></td>
          <td>${env[w]?.all_years ? `${env[w].all_years.from_date} → ${env[w].all_years.to_date} <small class="r">(median ${env[w].all_years.median_start} → ${env[w].all_years.median_end})</small>` : '—'}</td>
          <td>${env[w]?.last_20_years ? `${env[w].last_20_years.from_date} → ${env[w].last_20_years.to_date} <small class="r">(median ${env[w].last_20_years.median_start} → ${env[w].last_20_years.median_end})</small>` : '—'}</td>
          <td>${trendTxt(tr[`${w}_start`])}</td><td>${trendTxt(tr[`${w}_end`])}</td><td>${trendTxt(tr[`${w}_days`])}</td></tr>`).join('')}
        </tbody></table>
        <div class="hint">Envelope = 10th percentile of the cells' opening dates to the 90th percentile of their closing dates: staff or funding inside it covers most cell-windows.
          Trends: ↑ later / more days, ↓ earlier / fewer days (Sen slope; p corrected for serial correlation).</div>
        <div id="pt-strip"></div>`;
      const g = ix.groupings || {};
      html += `<h3>Groupings of high-danger cells (≥ ${W.fmt(ix.thresholds.grouping)}, regional ${sm.cluster_pct}th percentile)</h3>
        <div class="hint">Mean grouping size ${W.fmt(g.mean_group_km2, 1)} km² over all days with groupings. Trends: days with groupings ${trendTxt(g.trends?.days_with_groups)},
          mean size ${trendTxt(g.trends?.mean_group_km2)}, largest of the year ${trendTxt(g.trends?.largest_km2)}, days with one ≥ 100 km² ${trendTxt(g.trends?.days_with_large_group)}.</div>
        <div id="pt-hist"></div><div id="pt-gyear"></div>`;
    } else if (ix?.skipped) html += `<div class="callout">${W.esc(ix.skipped)}</div>`;
    const dv = sm?.daily_vs_hourly;
    if (dv) {
      html += `<h3>Daily vs hourly FWI</h3>
        <div class="hint">High days: classic daily FWI ≥ ${W.fmt(dv.thresholds.daily_p90)} or hourly daily max ≥ ${W.fmt(dv.thresholds.hourly_max_p90)} (each system's regional 90th percentile).</div>
        <table class="t"><tbody>
          <tr><td>Rank correlation (median cell)</td><td class="v">${W.fmt(dv.mainland.rank_r, 3)}</td><td>Kappa on high days</td><td class="v">${W.fmt(dv.mainland.kappa, 3)}</td></tr>
          <tr><td>Hourly high days the daily FWI also flags</td><td class="v">${W.pct(100 * (dv.mainland.hit_rate ?? 0), 0)}</td><td>Daily high days the hourly does not</td><td class="v">${W.pct(100 * (dv.mainland.false_alarm_ratio ?? 0), 0)}</td></tr>
          <tr><td>Afternoon peak above the noon value</td><td class="v">${W.fmt(dv.mainland.afternoon_excess, 2)}</td><td>Cells where daily is adequate</td><td class="v">${W.pct(100 * (dv.share_daily_adequate ?? 0), 0)}</td></tr>
        </tbody></table>
        <table class="t" style="margin-top:6px"><thead><tr><th>Month</th><th style="text-align:right">Kappa</th><th style="text-align:right">Hit rate</th><th style="text-align:right">Afternoon excess</th><th style="text-align:right">High days hourly / daily</th></tr></thead><tbody>
        ${Object.entries(dv.months).map(([m, x]) => `<tr><td>${MONTHS[+m - 1]}</td><td class="v">${W.fmt(x.kappa, 2)}</td><td class="v">${W.pct(100 * (x.hit_rate ?? 0), 0)}</td><td class="v">${W.fmt(x.afternoon_excess, 2)}</td>
          <td class="v">${W.pct(100 * (x.share_high_hourly ?? 0), 1)} / ${W.pct(100 * (x.share_high_daily ?? 0), 1)}</td></tr>`).join('')}</tbody></table>
        <div class="hint">Trend of high days per year: hourly ${trendTxt(dv.trends?.days_high_hourly)}, daily ${trendTxt(dv.trends?.days_high_daily)}, difference ${trendTxt(dv.trends?.difference)}.
          ${dv.notes.map(W.esc).join(' ')}</div>
        <div id="pt-hours"></div><div id="pt-dvy"></div>`;
    }
    html += driversHtml();
    if (sm) {
      html += `<h3>Map</h3><div class="row" style="gap:6px;align-items:center;flex-wrap:wrap">Paint cells <select id="pt-cell">
        ${Object.entries(CELLS).filter(() => !!dv).map(([k, t]) => `<option value="${k}" ${k === P.cellKey ? 'selected' : ''}>${t}</option>`).join('')}
        ${ix && !ix.skipped ? Object.entries(IDX_CELLS).map(([k, t]) => `<option value="${P.index}.${k}" ${`${P.index}.${k}` === P.cellKey ? 'selected' : ''}>${IDX_LABEL[P.index]}: ${t}</option>`).join('') : ''}
        </select><button class="btn sm" id="pt-paint">Paint</button></div>
        <div class="hint">Downloads: ${['season_windows.csv', 'groupings_yearly.csv', 'groupings_daily.csv', 'cells.csv', 'summary.json'].map(n => `<a href="${W.API}/api/patterns/${encodeURIComponent(sm.id)}/file/${n}">${n}</a>`).join(' · ')}</div>`;
    } else html += '<div class="hint" style="margin-top:8px">No pattern analysis yet.</div>';
    html += '</div>';
    b.innerHTML = html;
    $('pt-run').onclick = runPatterns;
    b.querySelectorAll('[data-pix]').forEach(x => x.onclick = () => { P.index = x.dataset.pix; W.renderTab(); });
    if ($('pt-area')) $('pt-area').onchange = e => { P.area = e.target.value; W.renderTab(); };
    if ($('pt-paint')) $('pt-paint').onclick = () => { P.cellKey = $('pt-cell').value; paint(P.cellKey); };
    if ($('pt-cell')) $('pt-cell').onchange = e => { P.cellKey = e.target.value; paint(P.cellKey); };
    if (ix && !ix.skipped) drawSeason(sm, ix);
    if (dv) drawCompare(dv);
    if (P.task) poll();
    if ($('dr-run')) $('dr-run').onclick = runDrivers;
    b.querySelectorAll('[data-dpart]').forEach(x => x.onclick = () => { P.dpart = x.dataset.dpart; W.renderTab(); });
    if (P.dtask) pollDrivers();
  };

  function driversHtml() {
    const P = S.pat, D = P.drivers, v = P.index;
    let h = `<h3>Driving variables per brick</h3>
      <div class="hint">For a fixed sample of cells per brick, the chain is re-run March–November of every year and the weather is read at the hour of each daily maximum.
        Bars: share of the index's day-to-day variability each driver explains (Shapley R² shares). "High days": which driver is most extreme on the brick's top-10% days.
        For FWI, the split between spread (ISI: wind + fine fuels) and build-up (BUI: deep-fuel dryness) is shown too. Statistical attribution; drivers are correlated.</div>
      <div class="row" style="gap:6px;align-items:center;flex-wrap:wrap"><label>cells per brick <input id="dr-n" type="number" min="2" max="100" value="${D?.cells_per_brick ?? 12}" style="width:52px"></label>
        <button class="btn sm" id="dr-run" ${P.dtask ? 'disabled' : ''}>Run drivers</button>
        ${D ? ['all', 'spring', 'summer', 'fall'].map(p => `<button class="btn sm ${p === P.dpart ? '' : 'ghost'}" data-dpart="${p}">${p}</button>`).join('') : ''}</div>
      <div id="dr-msg" class="hint">${P.dmsg || ''}</div>`;
    const R = D?.results?.[v];
    if (!R) return h + (D ? `<div class="hint">No driver results for ${W.esc(IDX_LABEL[v])}.</div>` : '<div class="hint">Not run yet (needs the weather again for the sample cells: about an hour on the full record with a local AORC store).</div>');
    const drv = D.drivers[v];
    h += `<div class="hint">${drv.map(k => `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${DCOL[k]};margin:0 4px 0 8px"></span>${W.esc(D.labels[k])}`).join('')}</div>
      <table class="t"><thead><tr><th>Brick</th><th>R²</th><th style="width:45%">Shares of the variability</th><th>Most extreme on high days</th>${v.startsWith('FWI') ? '<th>ISI / BUI</th>' : ''}</tr></thead><tbody>
      ${Object.entries(R).sort().map(([bk, parts]) => { const e = parts[P.dpart]; if (!e) return '';
        const tot = drv.reduce((a, k) => a + Math.max(0, e.shares[k] || 0), 0) || 1;
        const bar = drv.map(k => `<span title="${W.esc(D.labels[k])}: ${W.fmt(e.shares[k], 3)}" style="display:inline-block;height:10px;width:${(100 * Math.max(0, e.shares[k] || 0) / tot).toFixed(1)}%;background:${DCOL[k]}"></span>`).join('');
        const dom = Object.entries(e.dominant_on_high_days || {}).sort((a, b) => b[1] - a[1])[0];
        return `<tr><td>${bk}</td><td class="v">${W.fmt(e.r2, 2)}</td><td><div style="display:flex;width:100%;border-radius:3px;overflow:hidden">${bar}</div></td>
          <td>${dom ? `${W.esc(D.labels[dom[0]])} <small class="r">${W.pct(100 * dom[1], 0)}</small>` : '—'}</td>
          ${v.startsWith('FWI') ? `<td class="v">${W.fmt(e.fwi_split?.ISI, 2)} / ${W.fmt(e.fwi_split?.BUI, 2)}</td>` : ''}</tr>`; }).join('')}
      </tbody></table>
      <div class="hint">${W.esc(D.source)} · ${D.years.join('–')} · ${D.cells_per_brick} cells per brick · ${W.esc(D.created)} ·
        <a href="${W.API}/api/drivers/${encodeURIComponent(D.id)}/file/drivers.csv">drivers.csv</a> · <a href="${W.API}/api/drivers/${encodeURIComponent(D.id)}/file/driver_days.csv.gz">driver_days.csv.gz</a></div>`;
    return h;
  }

  async function runDrivers() {
    try { const t = await W.api('/api/drivers/run', { json: { config: S.cfg, cells_per_brick: +$('dr-n').value || 12 } });
      S.pat.dtask = t.id; S.pat.dmsg = `<span class="spin"></span> running (task #${t.id})…`; W.renderTab(); pollDrivers(); }
    catch (e) { W.toast('Error: ' + W.esc(e.message)); }
  }
  async function pollDrivers() {
    if (!S.pat.dtask) return;
    let t; try { t = (await W.api('/api/tasks')).tasks.find(x => x.id === S.pat.dtask); } catch (e) { setTimeout(pollDrivers, 3000); return; }
    if (!t) { S.pat.dtask = null; return; }
    if (t.status === 'done') { S.pat.dtask = null; S.pat.dmsg = 'Finished.'; await W.loadPatterns(); return; }
    if (t.status === 'error' || t.status === 'cancelled') { S.pat.dtask = null; S.pat.dmsg = `<span style="color:#f0a35e">${W.esc(t.status)}: ${W.esc(t.error || '')}</span>`; if (S.tab === 'PAT') W.renderTab(); return; }
    S.pat.dmsg = `<span class="spin"></span> ${W.esc(t.note || '')} · ${t.total ? Math.round(100 * t.done / t.total) : 0}%`;
    const el = $('dr-msg'); if (el) el.innerHTML = S.pat.dmsg;
    setTimeout(pollDrivers, 3000);
  }

  async function drawSeason(sm, ix) {
    const area = ix.areas[S.pat.area] ? S.pat.area : Object.keys(ix.areas)[0];
    try {
      const rows = (await W.api(`/api/patterns/${encodeURIComponent(sm.id)}/table/season_windows?index=${S.pat.index}&area=${encodeURIComponent(area)}`)).rows;
      if ($('pt-strip')) seasonStrip($('pt-strip'), rows, sm.years);
      const gy = (await W.api(`/api/patterns/${encodeURIComponent(sm.id)}/table/groupings_yearly?index=${S.pat.index}`)).rows;
      const hist = ix.groupings?.size_histogram_km2 || {}, tot = Object.values(hist).reduce((a, x) => a + x, 0) || 1;
      if ($('pt-hist')) { $('pt-hist').innerHTML = ''; $('pt-hist').appendChild(W.barRows(Object.entries(hist).map(([k, n]) => ({ label: `${k} km²`, value: 100 * n / tot, text: n.toLocaleString() })), { labelW: 110 })); }
      if ($('pt-gyear') && gy.length) W.drawLineChart($('pt-gyear'), { times: gy.map(r => new Date(Date.UTC(r.year, 6, 1))), daily: true,
        lines: [{ v: gy.map(r => r.mean_group_km2), color: '#e8a04a', w: 2, label: 'mean grouping (km²)' }, { v: gy.map(r => r.mean_largest_km2), color: '#9aa0a6', w: 1.4, dash: '4 3', label: 'mean daily largest (km²)' }],
        title: `${IDX_LABEL[S.pat.index]}: grouping size by year`, height: 170, xFmt: d => String(d.getUTCFullYear()), tip: true, tipTime: d => String(d.getUTCFullYear()) });
    } catch (e) { /* drawn when available */ }
  }

  function drawCompare(dv) {
    const hh = dv.peak_hour_histogram || {}, tot = Object.values(hh).reduce((a, x) => a + x, 0) || 1;
    if ($('pt-hours')) { $('pt-hours').innerHTML = '<div class="hint">Hour (LST) of the hourly FWI peak on high days</div>';
      $('pt-hours').appendChild(W.barRows(Object.entries(hh).filter(([, n]) => n > 0).map(([h, n]) => ({ label: `${String(h).padStart(2, '0')}:00`, value: 100 * n / tot, text: W.pct(100 * n / tot, 1) })), { labelW: 60 })); }
    const y = dv.yearly || [];
    if ($('pt-dvy') && y.length) W.drawLineChart($('pt-dvy'), { times: y.map(r => new Date(Date.UTC(r.year, 6, 1))), daily: true,
      lines: [{ v: y.map(r => r.days_high_hourly), color: '#3987e5', w: 2, label: 'hourly: high days' }, { v: y.map(r => r.days_high_daily), color: '#9aa0a6', w: 1.4, dash: '4 3', label: 'daily: high days' }],
      title: 'High-danger days per year (mainland mean): hourly vs classic daily FWI', height: 170, xFmt: d => String(d.getUTCFullYear()), tip: true, tipTime: d => String(d.getUTCFullYear()) });
  }
})();
