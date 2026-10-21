/* Self-contained SVG charts (all styling inline, so exported SVG/snapshots look identical) */
'use strict';
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const INK2 = '#b8b6ae', INK3 = '#8a887f', LINE = '#2f3337', LINE2 = '#3c4147', FONT = 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace';

  function mk(svg, tag, attrs, text) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    svg.appendChild(e); return e;
  }
  function txt(svg, x, y, s, o = {}) {
    return mk(svg, 'text', { x, y, fill: o.fill || INK2, 'font-size': o.size || 11, 'font-family': o.font || FONT, 'text-anchor': o.anchor || 'start', 'font-weight': o.weight }, s);
  }
  function niceStep(raw) { const p = Math.pow(10, Math.floor(Math.log10(raw || 1))); const r = raw / p; return (r < 1.5 ? 1 : r < 3.5 ? 2 : r < 7.5 ? 5 : 10) * p; }
  W.niceStep = niceStep;

  /**
   * opts: { times: Date[], lines: [{v, color, w, a, dash, label}], envelope: {lo, hi, color},
   *         bands, units, yFixed:[lo,hi], current, onPick(i), spinN, xFmt(d)->label, height,
   *         tiers: [{label, values:int[]}], title }
   */
  W.drawLineChart = function (box, o) {
    box.innerHTML = '';
    const W0 = Math.max(320, box.clientWidth || 560), nT = (o.tiers || []).length;
    const H = o.height || 260, tierH = nT ? 8 + nT * 10 : 0, m = { l: 46, r: 70, t: o.title ? 22 : 10, b: 30 + tierH };
    const n = o.times.length; if (!n) return null;
    let lo = Infinity, hi = -Infinity;
    for (const L of o.lines) for (const v of L.v) if (v != null && isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (o.envelope) for (const v of o.envelope.hi) if (v != null && isFinite(v)) hi = Math.max(hi, v);
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (o.yFixed) [lo, hi] = o.yFixed; else { lo = Math.min(0, lo); hi += (hi - lo) * 0.08 || 1; }
    const pw = W0 - m.l - m.r, ph = H - m.t - m.b;
    const x = i => m.l + pw * (n === 1 ? 0.5 : i / (n - 1));
    const y = v => m.t + ph * (1 - (v - lo) / (hi - lo));
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W0} ${H}`); svg.setAttribute('width', W0); svg.setAttribute('height', H);
    svg.setAttribute('class', 'chart'); svg.setAttribute('xmlns', NS);
    mk(svg, 'rect', { x: 0, y: 0, width: W0, height: H, fill: '#1c1e21' });
    if (o.title) txt(svg, m.l, 14, o.title, { fill: '#eceae4', size: 12, weight: 600, font: 'Inter,system-ui,sans-serif' });
    for (const [b0, b1, lab] of (o.bands || [])) {
      if (b0 > hi) continue; const y1 = y(Math.min(b1, hi)), y0 = y(Math.max(b0, lo));
      mk(svg, 'rect', { x: m.l, y: y1, width: pw, height: Math.max(0, y0 - y1), fill: 'rgba(255,255,255,0.025)' });
      mk(svg, 'line', { x1: m.l, x2: m.l + pw, y1, y2: y1, stroke: LINE, 'stroke-dasharray': '2 3' });
      if (y0 - y1 > 11) txt(svg, m.l + pw + 6, (y0 + y1) / 2 + 4, lab, { fill: INK3, size: 10 });
    }
    if (o.spinN > 0) { mk(svg, 'rect', { x: m.l, y: m.t, width: x(o.spinN - 1) - m.l, height: ph, fill: 'rgba(158,197,244,0.07)' }); txt(svg, m.l + 4, m.t + 12, 'spin-up', { size: 10, fill: INK3 }); }
    const st = niceStep((hi - lo) / 4);
    for (let v = Math.ceil(lo / st) * st; v <= hi + 1e-9; v += st) { mk(svg, 'line', { x1: m.l, x2: m.l + pw, y1: y(v), y2: y(v), stroke: LINE }); txt(svg, m.l - 6, y(v) + 4, W.fmt(v, st < 1 ? Math.min(4, Math.max(1, Math.ceil(-Math.log10(st) - 1e-9))) : 0), { anchor: 'end' }); }
    if (o.units) txt(svg, 4, m.t + 2, o.units, { size: 10, fill: INK3 });
    // x ticks: day boundaries (or every Nth day)
    const days = []; let last = null;
    for (let i = 0; i < n; i++) { const d = o.times[i]; const key = d.toISOString().slice(0, 10); if (key !== last && (o.daily || d.getUTCHours() === 0 || i === 0)) { days.push(i); last = key; } }
    const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(pw / 62))));
    days.forEach((i, k) => { mk(svg, 'line', { x1: x(i), x2: x(i), y1: m.t, y2: m.t + ph, stroke: LINE }); if (k % every === 0) txt(svg, x(i) + 3, m.t + ph + 13, o.xFmt ? o.xFmt(o.times[i]) : o.times[i].toISOString().slice(5, 10), { size: 10 }); });
    if (o.xNote) {                    // under the axis when there is room, else in the right margin (short)
      if (!nT) txt(svg, m.l + pw, m.t + ph + 26, o.xNote, { anchor: 'end', size: 10, fill: INK3 });
      else txt(svg, m.l + pw + 6, m.t + ph + 13, o.xNote.length > 11 ? o.xNote.replace('local standard time', 'LST').replace('local clock', 'clock').replace('local standard date', 'LST date') : o.xNote, { size: 10, fill: INK3 });
    }
    if (o.envelope) {
      const pts = []; for (let i = 0; i < n; i++) { const a = o.envelope.lo[i], b = o.envelope.hi[i]; if (a == null || b == null) continue; pts.push([x(i), y(a), y(b)]); }
      if (pts.length > 1) mk(svg, 'path', { d: 'M' + pts.map(p => `${p[0].toFixed(1)} ${p[2].toFixed(1)}`).join('L') + 'L' + pts.slice().reverse().map(p => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join('L') + 'Z', fill: o.envelope.color, 'fill-opacity': 0.14 });
    }
    for (const L of o.lines) {
      let d = '', pen = false;
      for (let i = 0; i < n; i++) { const v = L.v[i]; if (v == null || !isFinite(v)) { pen = false; continue; } d += (pen ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(Math.max(lo, Math.min(hi, v))).toFixed(1); pen = true; }
      if (n === 1 || L.markers) for (let i = 0; i < n; i++) if (L.v[i] != null) mk(svg, 'circle', { cx: x(i), cy: y(L.v[i]), r: 3, fill: L.color });
      mk(svg, 'path', { d, fill: 'none', stroke: L.color, 'stroke-width': L.w || 1.5, 'stroke-opacity': L.a ?? 1, 'stroke-dasharray': L.dash || '', 'stroke-linejoin': 'round' });
    }
    // legend (top-left inside plot) — identity is never colour-alone
    let ly = m.t + 9; const lx = m.l + 8;
    for (const L of o.lines) if (L.label) { mk(svg, 'line', { x1: lx, x2: lx + 22, y1: ly, y2: ly, stroke: L.color, 'stroke-width': Math.max(1.5, L.w || 1.5), 'stroke-dasharray': L.dash || '' }); txt(svg, lx + 28, ly + 4, L.label, { size: 10.5 }); ly += 14; }
    if (o.envelope) { mk(svg, 'rect', { x: lx, y: ly - 5, width: 22, height: 9, fill: o.envelope.color, 'fill-opacity': 0.2 }); txt(svg, lx + 28, ly + 4, 'min–max of selected cells', { size: 10.5 }); }
    // tier strips
    (o.tiers || []).forEach((T, k) => {
      const ty = m.t + ph + 22 + k * 10;
      txt(svg, m.l + pw + 6, ty + 7, T.label + ' tier', { size: 9, fill: INK3 });
      let i = 0;
      while (i < n) { const t = T.values[i]; let j = i; while (j + 1 < n && T.values[j + 1] === t) j++;
        if (t != null) mk(svg, 'rect', { x: x(i) - (i ? pw / (2 * (n - 1 || 1)) : 0), y: ty, width: Math.max(1, x(j) - x(i) + pw / Math.max(1, n - 1)), height: 7, fill: W.TIER_COLOR[t] || '#555', 'fill-opacity': t === 0 ? 0.45 : 0.95 });
        i = j + 1; }
    });
    const cur = mk(svg, 'line', { x1: x(o.current || 0), x2: x(o.current || 0), y1: m.t, y2: m.t + ph, stroke: '#e8a04a', 'stroke-width': 1.5 });
    const hov = mk(svg, 'line', { x1: 0, x2: 0, y1: m.t, y2: m.t + ph, stroke: 'rgba(255,255,255,0.35)', visibility: 'hidden' });
    box.appendChild(svg);
    if (o.onPick || o.tip !== false) {
      const tip = $('tip');
      const idxAt = ev => { const r = svg.getBoundingClientRect(); const px = (ev.clientX - r.left) * W0 / r.width; return Math.max(0, Math.min(n - 1, Math.round((px - m.l) / pw * (n - 1)))); };
      const hit = mk(svg, 'rect', { x: m.l, y: m.t, width: pw, height: ph + tierH, fill: 'transparent', style: 'cursor:crosshair' });
      hit.addEventListener('mousemove', ev => { const i = idxAt(ev); hov.setAttribute('x1', x(i)); hov.setAttribute('x2', x(i)); hov.setAttribute('visibility', 'visible');
        tip.style.display = 'block'; tip.style.left = (ev.clientX + 14) + 'px'; tip.style.top = (ev.clientY + 12) + 'px';
        tip.innerHTML = `<b>${W.esc(o.tipTime ? o.tipTime(o.times[i]) : W.utcLabel(o.times[i]))}</b><br>` + o.lines.filter(L => L.label).map(L => `<span style="color:${L.color}">■</span> ${W.esc(L.label)}: <b>${W.fmt(L.v[i])}</b>`).join('<br>') +
          (o.tiers || []).map(T => `<br>${T.label} tier: <b>${T.values[i] ?? '—'}</b>`).join(''); });
      hit.addEventListener('mouseleave', () => { hov.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; });
      if (o.onPick) hit.addEventListener('click', ev => o.onPick(idxAt(ev)));
    }
    svg._setCurrent = i => { cur.setAttribute('x1', x(i)); cur.setAttribute('x2', x(i)); };
    return svg;
  };

  W.svgString = svg => {
    const c = svg.cloneNode(true); c.setAttribute('xmlns', NS);
    c.querySelectorAll('rect[fill="transparent"]').forEach(e => e.remove());
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(c);
  };
  W.svgToPng = (svg, scale = 2) => new Promise(res => {
    const s = W.svgString(svg); const img = new Image(); const vb = svg.viewBox.baseVal;
    img.onload = () => { const cv = document.createElement('canvas'); cv.width = vb.width * scale; cv.height = vb.height * scale;
      const g = cv.getContext('2d'); g.scale(scale, scale); g.drawImage(img, 0, 0); cv.toBlob(res, 'image/png'); };
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s);
  });

  /* horizontal % bars as SVG (coverage, tiers) */
  W.barRows = function (rows, opts = {}) {
    const Wd = opts.width || 540, rh = 20, H = rows.length * rh + 6, lw = opts.labelW || 120;
    const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', `0 0 ${Wd} ${H}`); svg.setAttribute('width', '100%'); svg.setAttribute('xmlns', NS);
    rows.forEach((r, i) => {
      const y0 = 3 + i * rh;
      txt(svg, lw - 6, y0 + 13, r.label, { anchor: 'end', size: 11, font: 'Inter,system-ui,sans-serif' });
      mk(svg, 'rect', { x: lw, y: y0 + 4, width: Wd - lw - 70, height: 10, rx: 3, fill: '#2a2e32' });
      const f = Math.max(0, Math.min(1, (r.value || 0) / (r.max || 100)));
      if (f > 0) mk(svg, 'rect', { x: lw, y: y0 + 4, width: Math.max(2, (Wd - lw - 70) * f), height: 10, rx: 3, fill: r.color || '#3987e5' });
      txt(svg, Wd - 64, y0 + 13, r.text ?? W.pct(r.value), { size: 11 });
    });
    return svg;
  };
})();
