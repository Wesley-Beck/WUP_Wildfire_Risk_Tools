/* Data-flow diagram and the daylight clock strip (inline-styled SVG, exportable) */
'use strict';
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const SANS = 'Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif', MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace';
  const BG = '#1c1e21', BOX = '#232629', EDGE = '#3c4147', INK = '#eceae4', INK2 = '#b8b6ae', INK3 = '#8a887f';
  function mk(svg, tag, a, t) { const e = document.createElementNS(NS, tag); for (const k in a) if (a[k] != null) e.setAttribute(k, a[k]); if (t != null) e.textContent = t; svg.appendChild(e); return e; }
  function lines(svg, x, y, arr, o = {}) {
    arr.forEach((s, i) => mk(svg, 'text', { x, y: y + i * (o.lh || 14), fill: i === 0 ? (o.c0 || INK) : (o.c || INK2), 'font-size': i === 0 ? (o.s0 || 12) : (o.s || 10.5),
      'font-weight': i === 0 ? 600 : 400, 'font-family': i === 0 ? SANS : (o.f || SANS), 'text-anchor': o.anchor || 'middle' }, s));
  }
  function box(svg, x, y, w, h, title, sub = [], o = {}) {
    const g = document.createElementNS(NS, 'g'); svg.appendChild(g);
    if (o.id) { g.setAttribute('data-id', o.id); g.style.cursor = 'pointer'; }
    mk(g, 'rect', { x, y, width: w, height: h, rx: 7, fill: o.fill || BOX, stroke: o.stroke || EDGE, 'stroke-width': o.sw || 1 });
    lines(g, x + w / 2, y + 17, [title, ...sub], o);
    return g;
  }
  function arrow(svg, x1, y1, x2, y2, label, o = {}) {
    mk(svg, 'path', { d: `M${x1} ${y1} L${x2} ${y2}`, stroke: o.c || '#6b7078', 'stroke-width': 1.4, fill: 'none', 'marker-end': 'url(#ah)', 'stroke-dasharray': o.dash });
    if (label) mk(svg, 'text', { x: (x1 + x2) / 2 + (o.dx ?? 8), y: (y1 + y2) / 2 + 4, fill: INK3, 'font-size': 10, 'font-family': SANS, 'text-anchor': o.anchor || 'start' }, label);
  }

  /** Simple data-flow diagram. info: {computedPct, perFdi, bricks, cells, source, cfg} */
  W.flowDiagram = function (info) {
    const Wd = 580, H = 820;
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${Wd} ${H}`); svg.setAttribute('width', '100%'); svg.setAttribute('xmlns', NS);
    const defs = mk(svg, 'defs', {});
    const mkr = mk(defs, 'marker', { id: 'ah', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
    mk(mkr, 'path', { d: 'M0 0 L10 5 L0 10 z', fill: '#6b7078' });
    mk(svg, 'rect', { x: 0, y: 0, width: Wd, height: H, fill: BG });
    const cx = 290, bw = 330;
    box(svg, cx - bw / 2, 10, bw, 48, 'NOAA AORC v1.1 on S3', ['one Zarr store per year · 1979-02 →'], { id: 'aorc' });
    arrow(svg, cx, 58, cx, 82, 'fetch & cache one brick at a time');
    box(svg, cx - bw / 2, 84, bw, 48, 'Brick = 144 h × 128 × 256 cells', [`the unit S3 serves · ${info.bricks} bricks cover the WUP`], { id: 'brick' });
    arrow(svg, cx, 132, cx, 156);
    box(svg, cx - bw / 2, 158, bw, 48, 'Packet = 1 cell × 1 hour', [`${info.cells.toLocaleString()} cells · 7 AORC variables each`], { id: 'packet' });
    arrow(svg, cx, 206, cx, 230);
    box(svg, cx - bw / 2, 232, bw, 62, 'Resolution ladder (most specific first)', ['own value → same cell ±3 h → 3×3 (2.4 km)', '→ 7×7 (6 km) → brick mean → unavailable'], { id: 'ladder', s: 10.5 });
    arrow(svg, cx, 294, cx, 318);
    box(svg, cx - bw / 2, 320, bw, 40, 'Unit conversion', ['°C/°F · RH from q,T,p · wind speed'], { id: 'convert' });
    // three lanes
    const lx = [100, 290, 480], lw = 172;
    arrow(svg, cx - 60, 360, lx[0], 392); arrow(svg, cx, 360, lx[1], 392); arrow(svg, cx + 60, 360, lx[2], 392);
    box(svg, lx[0] - lw / 2, 394, lw, 62, 'Sun position per cell', ['at the packet’s UTC instant', '→ daylight yes/no'], { id: 'sun', s: 10 });
    box(svg, lx[1] - lw / 2, 394, lw, 62, 'Dead-fuel moisture', ['timelag 1 / 10 / 100 / 1000 h', 'hourly, carried forward'], { id: 'fm', s: 10 });
    box(svg, lx[2] - lw / 2, 394, lw, 62, 'MODIS NDVI ladder', ['250 m → 500 m → 1 km', '16-day → daily (2000 →)'], { id: 'ndvi', s: 10 });
    arrow(svg, lx[0], 456, lx[0], 480); arrow(svg, lx[1], 456, lx[1], 480); arrow(svg, lx[2], 456, lx[2], 480);
    arrow(svg, lx[1] + 50, 456, lx[2] - 40, 482, '10-h FM', { dx: -2, anchor: 'end' });
    box(svg, lx[0] - lw / 2, 482, lw, 62, 'CFFWIS (FWI2025)', ['hourly FFMC · DMC · DC → FWI', '+ classic daily FWI @ 12 LST'], { id: 'cffwis', s: 10 });
    box(svg, lx[1] - lw / 2, 482, lw, 62, 'NFDRS (fuel model)', ['spread component SC', '→ ERC · BI'], { id: 'nfdrs', s: 10 });
    box(svg, lx[2] - lw / 2, 482, lw, 62, 'Burgan FPI', ['greenness × dead-fuel dryness', 'extinction-adjusted'], { id: 'fpi', s: 10 });
    // FDI pills
    const pf = info.perFdi || {};
    const pill = (x, y, id, sub) => { const g = box(svg, x - 52, y, 104, 40, id, [sub], { fill: W.COLOR[id], stroke: W.COLOR[id], c0: '#0d0f10', c: '#0d0f10', s: 9.5, id: 'fdi-' + id }); return g; };
    arrow(svg, lx[0], 544, lx[0], 566); arrow(svg, lx[1] - 30, 544, lx[1] - 58, 566); arrow(svg, lx[1] + 30, 544, lx[1] + 58, 566); arrow(svg, lx[2], 544, lx[2], 566);
    pill(lx[0], 568, 'FWI', `hourly + daily · ${W.pct(pf.FWI?.percent_computed)}`);
    pill(lx[1] - 58, 568, 'ERC', `hourly · ${W.pct(pf.ERC?.percent_computed)}`);
    pill(lx[1] + 58, 568, 'BI', `hourly · ${W.pct(pf.BI?.percent_computed)}`);
    pill(lx[2], 568, 'FPI', `hourly×daily · ${W.pct(pf.FPI?.percent_computed)}`);
    for (const xx of [lx[0], lx[1] - 58, lx[1] + 58, lx[2]]) arrow(svg, xx, 608, cx + (xx - cx) * 0.35, 640);
    box(svg, cx - bw / 2, 642, bw, 48, 'Results store (workspace)', ['per brick × 144 h chunk + chain state · resumable jobs'], { id: 'store', s: 10.5 });
    arrow(svg, cx, 690, cx, 712, 'percentiles (post-pass)');
    box(svg, cx - bw / 2, 714, bw, 48, 'Map · charts · step trace · fire checks', ['save views & selections · reports · CSV / NetCDF / snapshot'], { id: 'out', s: 10.5 });
    // time-zone side input
    box(svg, 6, 642, 110, 62, 'County time zone', ['Gogebic, Iron: CT', 'others: ET'], { id: 'tz', s: 9.5, fill: '#20252c' });
    arrow(svg, 116, 690, cx - bw / 2, 738, 'local-time & daily views', { dash: '3 3', dx: -60 });
    box(svg, 464, 642, 110, 62, 'Fuel-model map', ['per cell if supplied', 'else model G'], { id: 'fuelmap', s: 9.5, fill: '#20252c' });
    arrow(svg, 519, 642, lx[1] + 86, 520, null, { dash: '3 3' });
    mk(svg, 'text', { x: cx, y: 790, fill: INK3, 'font-size': 10.5, 'font-family': SANS, 'text-anchor': 'middle' },
      `${W.pct(info.computedPct, 4)} of all ${info.cells.toLocaleString()} cells × hours since 1979-02 calculated for this configuration · source: ${info.source}`);
    mk(svg, 'text', { x: cx, y: 808, fill: INK3, 'font-size': 10, 'font-family': SANS, 'text-anchor': 'middle' }, `configuration ${info.cfgLabel || ''}`);
    return svg;
  };

  /** 24-hour clock strip for one cell & UTC date. rep = /api/timebase payload */
  W.clockStrip = function (rep) {
    const Wd = 640, H = 262, l = 150, r = 14, cw = (Wd - l - r) / 24;
    const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', `0 0 ${Wd} ${H}`); svg.setAttribute('width', '100%'); svg.setAttribute('xmlns', NS);
    const defs = mk(svg, 'defs', {});
    const pat = mk(defs, 'pattern', { id: 'hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
    mk(pat, 'rect', { width: 6, height: 6, fill: '#3a2a0d' }); mk(pat, 'line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: '#f0c27a', 'stroke-width': 2 });
    mk(svg, 'rect', { x: 0, y: 0, width: Wd, height: H, fill: BG });
    const x = h => l + h * cw;
    const rowY = { utc: 18, clock: 36, lst: 54, solar: 72, elev: 92, fixed: 172, legacy: 192, mis: 212 };
    const lab = (k, s) => mk(svg, 'text', { x: l - 8, y: rowY[k] + 4, fill: INK2, 'font-size': 10.5, 'font-family': SANS, 'text-anchor': 'end' }, s);
    lab('utc', 'UTC (AORC stamp)'); lab('clock', `Local clock (${rep.clock_abbr})`); lab('lst', `Local standard (UTC${rep.std_offset_h})`);
    lab('solar', 'Apparent solar time'); lab('fixed', 'Sun up (fixed test)'); lab('legacy', 'Legacy "daylight"'); lab('mis', 'Mismatch');
    mk(svg, 'text', { x: l - 8, y: 128, fill: INK2, 'font-size': 10.5, 'font-family': SANS, 'text-anchor': 'end' }, 'Sun elevation');
    rep.hours.forEach((h, i) => {
      const cxm = x(i) + cw / 2;
      mk(svg, 'text', { x: cxm, y: rowY.utc + 4, fill: INK, 'font-size': 9.5, 'font-family': MONO, 'text-anchor': 'middle' }, W.pad2(h.utc_hour));
      mk(svg, 'text', { x: cxm, y: rowY.clock + 4, fill: INK2, 'font-size': 9.5, 'font-family': MONO, 'text-anchor': 'middle' }, h.clock.slice(0, 2));
      mk(svg, 'text', { x: cxm, y: rowY.lst + 4, fill: INK2, 'font-size': 9.5, 'font-family': MONO, 'text-anchor': 'middle' }, W.pad2(h.lst_hour));
      mk(svg, 'text', { x: cxm, y: rowY.solar + 4, fill: INK3, 'font-size': 9, 'font-family': MONO, 'text-anchor': 'middle' }, W.pad2(Math.floor(h.solar_time_h)));
      if (h.daylight_fixed) mk(svg, 'rect', { x: x(i) + 0.5, y: rowY.fixed - 7, width: cw - 1, height: 14, fill: '#f0c27a', 'fill-opacity': 0.9 });
      if (h.daylight_legacy) mk(svg, 'rect', { x: x(i) + 0.5, y: rowY.legacy - 7, width: cw - 1, height: 14, fill: 'url(#hatch)' });
      if (h.daylight_fixed !== h.daylight_legacy) mk(svg, 'rect', { x: x(i) + 0.5, y: rowY.mis - 7, width: cw - 1, height: 14, fill: '#e66767' });
    });
    // elevation curve
    const ey = v => 150 - Math.max(-30, Math.min(70, v)) * 0.75;
    mk(svg, 'line', { x1: l, x2: Wd - r, y1: ey(-0.833), y2: ey(-0.833), stroke: '#6b7078', 'stroke-dasharray': '3 3' });
    mk(svg, 'text', { x: Wd - r, y: ey(-0.833) - 3, fill: INK3, 'font-size': 9, 'font-family': SANS, 'text-anchor': 'end' }, 'horizon');
    mk(svg, 'path', { d: rep.hours.map((h, i) => `${i ? 'L' : 'M'}${(x(i) + cw / 2).toFixed(1)} ${ey(h.elevation_deg).toFixed(1)}`).join(''), stroke: '#f0c27a', 'stroke-width': 2, fill: 'none' });
    // sunrise / sunset markers (UTC hours mod 24)
    const mark = (hh, s) => { const v = ((hh % 24) + 24) % 24; mk(svg, 'line', { x1: x(v), x2: x(v), y1: 84, y2: 220, stroke: '#9ec5f4', 'stroke-dasharray': '2 2' });
      mk(svg, 'text', { x: x(v) + 3, y: 162, fill: '#9ec5f4', 'font-size': 9.5, 'font-family': SANS }, s); };
    mark(rep.fixed.sunrise_utc_h, `sunrise ${rep.fixed.sunrise_utc}Z`);
    mark(rep.fixed.sunset_utc_h, `sunset ${rep.fixed.sunset_utc.replace('(+1d)', '')}Z`);
    mk(svg, 'text', { x: 8, y: 236, fill: INK3, 'font-size': 10, 'font-family': SANS },
      `${rep.county} County (${rep.zone}) · ${rep.lat.toFixed(3)}, ${rep.lon.toFixed(3)} · UTC day ${rep.date}`);
    mk(svg, 'text', { x: 8, y: 252, fill: rep.misclassified_hours ? '#e66767' : INK3, 'font-size': 10, 'font-family': SANS },
      `${rep.misclassified_hours} of 24 hours are classified differently by the legacy UTC-vs-local-sunrise test`);
    return svg;
  };
})();
