/* Map: cells, bricks, counties, selection, colouring */
'use strict';
(function () {
  const S = W.S;
  const TILES = {
    dark: ['https://a.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png', 'https://b.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png'],
  };
  let map, popup;

  W.parsePoints = function (csv) {
    const lines = csv.trim().split('\n'); const head = lines[0].split(','); const col = n => head.indexOf(n);
    const n = lines.length - 1;
    const c = { pid: new Int32Array(n), lat: new Float64Array(n), lon: new Float64Array(n), li: new Int32Array(n), lj: new Int32Array(n),
      ci: new Int16Array(n), cj: new Int16Array(n), county: new Array(n), zone: new Array(n), pidIndex: new Map(), n };
    const k = { pid: col('point_id'), lat: col('latitude'), lon: col('longitude'), li: col('lat_idx'), lj: col('lon_idx'), ci: col('ci'), cj: col('cj'), county: col('county'), zone: col('zone') };
    for (let i = 0; i < n; i++) { const f = lines[i + 1].split(',');
      c.pid[i] = +f[k.pid]; c.lat[i] = +f[k.lat]; c.lon[i] = +f[k.lon]; c.li[i] = +f[k.li]; c.lj[i] = +f[k.lj];
      c.ci[i] = +f[k.ci]; c.cj[i] = +f[k.cj]; c.county[i] = f[k.county]; c.zone[i] = f[k.zone]; c.pidIndex.set(c.pid[i], i); }
    return c;
  };
  W.row = pid => S.cells.pidIndex.get(pid);

  function cellsGeoJSON() {
    const c = S.cells, h = S.grid.step_deg / 2, feats = new Array(c.n);
    for (let i = 0; i < c.n; i++) { const x = c.lon[i], y = c.lat[i];
      feats[i] = { type: 'Feature', id: c.pid[i], properties: { pid: c.pid[i] }, geometry: { type: 'Polygon', coordinates: [[[x - h, y - h], [x + h, y - h], [x + h, y + h], [x - h, y + h], [x - h, y - h]]] } }; }
    return { type: 'FeatureCollection', features: feats };
  }
  function bricksGeoJSON() {
    return { type: 'FeatureCollection', features: S.grid.bricks.map((b, i) => { const [x0, y0, x1, y1] = b.full_bbox;
      return { type: 'Feature', id: i + 1, properties: { key: b.key }, geometry: { type: 'Polygon', coordinates: [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] } }; }) };
  }

  W.initMap = function () {
    map = new maplibregl.Map({
      container: 'map', center: [-88.9, 46.9], zoom: 7.3, minZoom: 5, maxZoom: 15, attributionControl: { compact: true },
      preserveDrawingBuffer: true,
      style: { version: 8, sources: { base: { type: 'raster', tiles: TILES.dark, tileSize: 256, attribution: '© OpenStreetMap contributors © CARTO' } },
               layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#111314' } }, { id: 'base', type: 'raster', source: 'base' }] },
    });
    W.map = map;
    map.boxZoom.disable();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
    popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
    map.on('error', e => { if (String(e?.error?.message || '').includes('cartocdn')) W.setOnline(false, true); });
    return new Promise(res => map.on('load', res));
  };

  W.setOnline = function (on, auto) {
    S.online = on; $('m-online').checked = on;
    if (map.getLayer('base')) map.setLayoutProperty('base', 'visibility', on ? 'visible' : 'none');
    if (auto && !W._offlineToast) { W._offlineToast = true; W.toast('Basemap tiles unreachable — using the built-in offline outline.'); }
  };

  W.addLayers = function () {
    map.addSource('outline', { type: 'geojson', data: S.outline });
    map.addSource('counties', { type: 'geojson', data: S.counties, promoteId: 'fips' });
    map.addSource('cells', { type: 'geojson', data: cellsGeoJSON(), promoteId: 'pid' });
    map.addSource('bricks', { type: 'geojson', data: bricksGeoJSON() });
    map.addLayer({ id: 'outline-fill', type: 'fill', source: 'outline', paint: { 'fill-color': '#22262a', 'fill-opacity': 0.55 } });
    map.addLayer({ id: 'tz-fill', type: 'fill', source: 'counties', layout: { visibility: 'none' },
      paint: { 'fill-color': ['match', ['get', 'zone'], 'CT', '#9085e9', '#3987e5'], 'fill-opacity': 0.22 } });
    const v = ['coalesce', ['feature-state', 'v'], -1];
    map.addLayer({ id: 'cells-fill', type: 'fill', source: 'cells', paint: {
      'fill-color': ['case', ['<', v, 0], 'rgba(255,255,255,0.025)', ['interpolate', ['linear'], v, 0, W.RAMP[0], 0.2, W.RAMP[1], 0.4, W.RAMP[2], 0.6, W.RAMP[3], 0.8, W.RAMP[4], 1, W.RAMP[5]]],
      'fill-opacity': ['case', ['<', v, 0], 1, 0.85] } });
    map.addLayer({ id: 'cells-tier', type: 'fill', source: 'cells', layout: { visibility: 'none' }, paint: {
      'fill-color': ['match', ['coalesce', ['feature-state', 't'], -1], 0, W.TIER_COLOR[0], 1, W.TIER_COLOR[1], 2, W.TIER_COLOR[2], 3, W.TIER_COLOR[3], 4, W.TIER_COLOR[4], 9, W.TIER_COLOR[9], 'rgba(0,0,0,0)'],
      'fill-opacity': 0.85 } });
    map.addLayer({ id: 'cells-line', type: 'line', source: 'cells', minzoom: 8.5, paint: { 'line-color': 'rgba(255,255,255,0.16)', 'line-width': ['interpolate', ['linear'], ['zoom'], 8.5, 0.2, 12, 0.8] } });
    map.addLayer({ id: 'cells-sel', type: 'line', source: 'cells', paint: { 'line-color': '#e8a04a', 'line-width': ['case', ['boolean', ['feature-state', 'sel'], false], 1.6, 0] } });
    map.addLayer({ id: 'cells-focus', type: 'line', source: 'cells', paint: { 'line-color': '#ffffff', 'line-width': ['case', ['boolean', ['feature-state', 'focus'], false], 2.6, 0] } });
    map.addLayer({ id: 'counties-line', type: 'line', source: 'counties', paint: { 'line-color': '#9a978d', 'line-width': 1.1, 'line-opacity': 0.75 } });
    map.addLayer({ id: 'bricks-cov', type: 'fill', source: 'bricks', layout: { visibility: 'none' }, paint: {
      'fill-color': ['interpolate', ['linear'], ['coalesce', ['feature-state', 'pct'], 0], 0, 'rgba(57,135,229,0.04)', 1, 'rgba(57,135,229,0.25)', 100, 'rgba(57,135,229,0.65)'] } });
    map.addLayer({ id: 'bricks-line', type: 'line', source: 'bricks', paint: { 'line-color': '#9ec5f4', 'line-width': 1.2, 'line-dasharray': [4, 3], 'line-opacity': 0.8 } });
    // HTML labels (no glyph server needed → works offline)
    S.brickLabels = S.grid.bricks.map(b => { const el = document.createElement('div'); el.className = 'maplabel brick'; el.dataset.key = b.key;
      el.textContent = `brick ${b.ci},${b.cj} · ${b.n_cells.toLocaleString()} cells`;
      return new maplibregl.Marker({ element: el, anchor: 'top-left', offset: [4, 4] }).setLngLat([b.clip_bbox[0], b.clip_bbox[3]]).addTo(map); });
    S.countyLabels = S.counties.features.map(f => { const el = document.createElement('div'); el.className = 'maplabel county';
      el.innerHTML = `${W.esc(f.properties.name)}<small>${f.properties.zone}</small>`;
      const c = centroid(f.geometry);
      return new maplibregl.Marker({ element: el }).setLngLat(c).addTo(map); });
    map.on('mousemove', 'cells-fill', e => { if (S.mode !== 'box') hover(e.features[0].id, e.lngLat); map.getCanvas().style.cursor = S.mode === 'box' ? 'crosshair' : 'pointer'; });
    map.on('mouseleave', 'cells-fill', () => { popup.remove(); map.getCanvas().style.cursor = ''; });
    map.on('click', 'cells-fill', e => {
      if (S.fires?.pick) return;                                           // picking a fire location
      if (map.getLayer('fires-pt') && map.getLayoutProperty('fires-pt', 'visibility') !== 'none' && map.queryRenderedFeatures(e.point, { layers: ['fires-pt'] }).length) return;
      onClick(e.features[0].id, e.originalEvent);
    });
    initBox();
  };
  function centroid(g) {
    const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
    let best = null, area = -1;
    for (const p of polys) { const r = p[0]; let a = 0, cx = 0, cy = 0;
      for (let i = 0, j = r.length - 1; i < r.length; j = i++) { const f = r[j][0] * r[i][1] - r[i][0] * r[j][1]; a += f; cx += (r[j][0] + r[i][0]) * f; cy += (r[j][1] + r[i][1]) * f; }
      if (Math.abs(a) > area) { area = Math.abs(a); best = [cx / (3 * a), cy / (3 * a)]; } }
    return best;
  }

  function hover(pid, lngLat) {
    const i = W.row(pid); if (i == null) return;
    const v = W.valueForPid(pid);
    const zone = S.cells.zone[i];
    popup.setLngLat(lngLat).setHTML(`<b>cell ${pid}</b> · ${W.esc(S.cells.county[i])} (${zone})<br>${S.cells.lat[i].toFixed(4)}, ${S.cells.lon[i].toFixed(4)}<br>brick ${S.cells.ci[i]},${S.cells.cj[i]}` +
      (v != null ? `<br>${W.esc($('m-var').value)}${S.pctMode ? ' pct' : ''}: <b>${W.fmt(v)}</b>` : '')).addTo(map);
    $('cellinfo').innerHTML = `<dt>point_id</dt><dd>${pid}</dd><dt>county / zone</dt><dd>${W.esc(S.cells.county[i])} · ${zone} (UTC${W.STD[zone]})</dd>
      <dt>lat, lon</dt><dd>${S.cells.lat[i].toFixed(5)}, ${S.cells.lon[i].toFixed(5)}</dd><dt>AORC index</dt><dd>${S.cells.li[i]}, ${S.cells.lj[i]}</dd>
      <dt>brick (ci,cj)</dt><dd>${S.cells.ci[i]}, ${S.cells.cj[i]} · local ${S.cells.li[i] % 128},${S.cells.lj[i] % 256}</dd>
      <dt>cell size</dt><dd>${S.grid.approx_cell_size_m.north_south} m N–S × ${S.grid.approx_cell_size_m.east_west_at_mean_lat} m E–W</dd>` +
      (v != null ? `<dt>${W.esc($('m-var').value)}</dt><dd>${W.fmt(v)} @ ${W.esc(W.currentTimeLabel())}</dd>` : '');
  }

  function onClick(pid, ev) {
    const i = W.row(pid);
    if (S.mode === 'brick') return W.selectMany(cellsWhere(j => S.cells.ci[j] === S.cells.ci[i] && S.cells.cj[j] === S.cells.cj[i]), `brick ${S.cells.ci[i]},${S.cells.cj[i]}`);
    if (S.mode === 'county') return W.selectMany(cellsWhere(j => S.cells.county[j] === S.cells.county[i]), `${S.cells.county[i]} County`);
    if (S.mode === 'all') return W.setFocus(pid);
    if (S.mode !== 'cell') return;
    if (ev.shiftKey || ev.ctrlKey || ev.metaKey) { toggle(pid); W.onSelectionChanged(); return; }
    if (S.series && S.sel.has(pid)) return W.setFocus(pid);
    W.clearSel(false); toggle(pid); W.onSelectionChanged();
  }
  function cellsWhere(f) { const out = []; for (let i = 0; i < S.cells.n; i++) if (f(i)) out.push(S.cells.pid[i]); return out; }
  W.cellsWhere = cellsWhere;
  function toggle(pid) {
    if (S.sel.has(pid)) { S.sel.delete(pid); S.selOrder = S.selOrder.filter(p => p !== pid); map.setFeatureState({ source: 'cells', id: pid }, { sel: false }); }
    else { S.sel.add(pid); S.selOrder.push(pid); map.setFeatureState({ source: 'cells', id: pid }, { sel: true }); }
  }
  W.clearSel = function (notify = true) {
    for (const p of S.sel) map.setFeatureState({ source: 'cells', id: p }, { sel: false });
    S.sel.clear(); S.selOrder = []; S.selName = null;
    if (S.focusPid != null) map.setFeatureState({ source: 'cells', id: S.focusPid }, { focus: false });
    S.focusPid = null;
    if (notify) W.onSelectionChanged();
  };
  W.selectMany = function (pids, name) {
    W.clearSel(false);
    for (const p of pids) { if (!S.cells.pidIndex.has(p)) continue; S.sel.add(p); S.selOrder.push(p); map.setFeatureState({ source: 'cells', id: p }, { sel: true }); }
    S.selName = name || null; W.onSelectionChanged();
  };
  W.setFocus = function (pid) {
    if (S.focusPid != null) map.setFeatureState({ source: 'cells', id: S.focusPid }, { focus: false });
    S.focusPid = pid;
    if (pid != null) map.setFeatureState({ source: 'cells', id: pid }, { focus: true });
    W.onFocusChanged();
  };
  W.fitSelection = function () {
    if (!S.sel.size) return;
    let x0 = 999, y0 = 999, x1 = -999, y1 = -999;
    for (const p of S.sel) { const i = W.row(p); x0 = Math.min(x0, S.cells.lon[i]); x1 = Math.max(x1, S.cells.lon[i]); y0 = Math.min(y0, S.cells.lat[i]); y1 = Math.max(y1, S.cells.lat[i]); }
    map.fitBounds([[x0 - 0.02, y0 - 0.02], [x1 + 0.02, y1 + 0.02]], { padding: 40, maxZoom: 12, duration: 600 });
  };

  function initBox() {
    const c = map.getCanvasContainer(), box = $('boxsel'); let start = null;
    const pt = e => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    c.addEventListener('mousedown', e => { if (S.mode !== 'box') return; start = pt(e); box.style.display = 'block'; Object.assign(box.style, { left: start[0] + 'px', top: start[1] + 'px', width: 0, height: 0 }); e.preventDefault(); });
    window.addEventListener('mousemove', e => { if (!start) return; const p = pt(e);
      Object.assign(box.style, { left: Math.min(start[0], p[0]) + 'px', top: Math.min(start[1], p[1]) + 'px', width: Math.abs(p[0] - start[0]) + 'px', height: Math.abs(p[1] - start[1]) + 'px' }); });
    window.addEventListener('mouseup', e => { if (!start) return; const p = pt(e); box.style.display = 'none';
      const a = map.unproject(start), b = map.unproject(p); start = null;
      const [x0, x1] = [Math.min(a.lng, b.lng), Math.max(a.lng, b.lng)], [y0, y1] = [Math.min(a.lat, b.lat), Math.max(a.lat, b.lat)];
      W.selectMany(cellsWhere(i => S.cells.lon[i] >= x0 && S.cells.lon[i] <= x1 && S.cells.lat[i] >= y0 && S.cells.lat[i] <= y1),
        `box ${x0.toFixed(2)},${y0.toFixed(2)} → ${x1.toFixed(2)},${y1.toFixed(2)}`); });
  }

  /* ---- colouring ---- */
  const cellColor = diverging => { const v = ['coalesce', ['feature-state', 'v'], -1], R = diverging ? W.DIVERGE : W.RAMP;
    const stops = R.flatMap((c, i) => [i / (R.length - 1), c]);
    return ['case', ['<', v, 0], 'rgba(255,255,255,0.025)', ['interpolate', ['linear'], v, ...stops]]; };
  W.paintValues = function (pids, values, lo, hi, isTier, diverging = false) {
    if (S.cellDiverging !== diverging) { map.setPaintProperty('cells-fill', 'fill-color', cellColor(diverging)); S.cellDiverging = diverging; }
    map.removeFeatureState({ source: 'cells' });
    for (const p of S.sel) map.setFeatureState({ source: 'cells', id: p }, { sel: true });
    if (S.focusPid != null) map.setFeatureState({ source: 'cells', id: S.focusPid }, { focus: true });
    map.setLayoutProperty('cells-tier', 'visibility', isTier ? 'visible' : 'none');
    if (!values) return;
    for (let k = 0; k < pids.length; k++) { const x = values[k]; if (x == null) continue;
      map.setFeatureState({ source: 'cells', id: pids[k] }, isTier ? { t: x } : { v: Math.max(0, Math.min(1, (x - lo) / (hi - lo))) }); }
  };
  W.legend = function (title, lo, hi, extra, isTier, diverging = false) {
    $('legend-title').textContent = title;
    if (isTier) {
      $('legend-bar').style.background = `linear-gradient(to right, ${[0, 1, 2, 3, 4, 9].map((t, i) => `${W.TIER_COLOR[t]} ${i * 16.6}%, ${W.TIER_COLOR[t]} ${(i + 1) * 16.6}%`).join(',')})`;
      $('legend-min').textContent = '0 native'; $('legend-mid').textContent = '2–4 neighbours'; $('legend-max').textContent = '9 none';
    } else {
      $('legend-bar').style.background = `linear-gradient(to right, ${(diverging ? W.DIVERGE : W.RAMP).join(',')})`;
      const nd = Math.abs(hi - lo) < 5 ? 2 : 0;
      $('legend-min').textContent = W.fmt(lo, nd); $('legend-mid').textContent = W.fmt((lo + hi) / 2, nd); $('legend-max').textContent = W.fmt(hi, nd);
    }
    $('legend-extra').textContent = extra || '';
  };
  W.setLayer = function (id, on) {
    if (id === 'bricks') { map.setLayoutProperty('bricks-line', 'visibility', on ? 'visible' : 'none'); S.brickLabels.forEach(m => m.getElement().style.display = on ? '' : 'none'); }
    if (id === 'mesh') map.setLayoutProperty('cells-line', 'visibility', on ? 'visible' : 'none');
    if (id === 'counties') { map.setLayoutProperty('counties-line', 'visibility', on ? 'visible' : 'none'); S.countyLabels.forEach(m => m.getElement().style.display = on ? '' : 'none'); }
    if (id === 'tz') map.setLayoutProperty('tz-fill', 'visibility', on ? 'visible' : 'none');
    if (id === 'fires') ['fires-pt', 'fire-polys'].forEach(l => map.getLayer(l) && map.setLayoutProperty(l, 'visibility', on ? 'visible' : 'none'));
    if (id === 'cov') { map.setLayoutProperty('bricks-cov', 'visibility', on ? 'visible' : 'none'); if (on) W.paintCoverage(); else S.brickLabels.forEach(m => { const b = S.grid.bricks.find(x => x.key === m.getElement().dataset.key); m.getElement().textContent = `brick ${b.ci},${b.cj} · ${b.n_cells.toLocaleString()} cells`; }); }
  };
  W.paintCoverage = async function () {
    try { S.coverage = await W.api('/api/coverage?config=' + encodeURIComponent(JSON.stringify(S.cfg))); } catch (e) { return; }
    S.grid.bricks.forEach((b, i) => map.setFeatureState({ source: 'bricks', id: i + 1 }, { pct: S.coverage.per_brick[b.key] || 0 }));
    S.brickLabels.forEach(m => { const k = m.getElement().dataset.key; const b = S.grid.bricks.find(x => x.key === k);
      m.getElement().textContent = `brick ${b.ci},${b.cj} · ${W.pct(S.coverage.per_brick[k], 3)} calculated`; });
  };
  W.mapPng = function () {
    map.once('render', () => map.getCanvas().toBlob(b => W.download(`wup_map_${Date.now()}.png`, b)));
    map.triggerRepaint();
  };
})();
