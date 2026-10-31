// Hype Studio – the cue editor waveform: one waveform with zoom (whole song · 30 s · 10 s · 4 s).
// Zoomed in it follows the play line (or the marker you are setting), shows 0.01 s detail, and you can drag it sideways.
// o = {cue: () => CUE, media: () => <video>, path: () => file path, api, set: (k, t, done) => …, seek: t => …, label: el}
function CueWave(cv, o) {
  const LV = [0, 30, 10, 4];
  let lv = 0, V0 = 0, mode = 'p', peaks = [], fine = null, busy = false, press = null, curPath = null;
  const C = () => o.cue();
  const span = () => { const c = C(); return lv ? Math.min(LV[lv], c.d) : c.d; };
  const cssW = () => cv.getBoundingClientRect().width || 1;
  function win() {
    const c = C(); if (!c) return 0;
    const sp = span(), mx = Math.max(0, c.d - sp), cl = v => Math.max(0, Math.min(mx, v));
    if (!lv) return (V0 = 0);
    if (press && press.moved) return V0;                                   // dragging: the view holds still
    if (mode === 'free') return (V0 = cl(V0));
    if (mode !== 'p') return (V0 = cl((c[mode] || 0) - sp / 2));           // centred on the marker you are setting
    const m = o.media(), t = m.currentTime || 0;
    if (t < V0 + sp * 0.04 || t > V0 + sp * 0.96) V0 = cl(t - (m.paused ? sp / 2 : sp * 0.2));   // playing: pages along
    return (V0 = cl(V0));
  }
  function tAt(e) { const r = cv.getBoundingClientRect(); return win() + Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * span(); }
  function fetchFine() {
    const c = C(), p = o.path(); if (!c || !p || !lv || busy) return;
    const sp = span(), w = win();
    if (fine && fine.path === p && w >= fine.t0 - 0.01 && w + sp <= fine.t0 + fine.p.length * fine.step + 0.02) return;
    const t0 = Math.max(0, Math.round((w - sp / 2) * 10) / 10), d = Math.min(60, sp * 2);
    busy = true;
    o.api('wavezoom?path=' + encodeURIComponent(p) + '&t=' + t0 + '&d=' + d)
      .then(r => { busy = false; if (o.path() === p && r && r.p) { fine = {...r, path: p}; draw(); } }).catch(() => { busy = false; });
  }
  function draw() {
    const x = cv.getContext('2d'), W = cv.width, H = cv.height; x.clearRect(0, 0, W, H); const c = C(); if (!c) return;
    const sp = span(), t0 = win(), X = t => (t - t0) / sp * W;
    if (lv) {                                                               // grid: 1 s lines (5 s at 30 s zoom)
      const g = sp > 15 ? 5 : 1, minor = sp > 15 ? 1 : 0.5;
      for (let s = Math.ceil(t0 / minor) * minor; s < t0 + sp; s += minor) { const major = Math.abs(s / g - Math.round(s / g)) < 1e-6; x.fillStyle = major ? '#23496B' : '#122C45'; x.fillRect(X(s), 0, major ? 2 : 1, H); }
    }
    // the sound: one bar per 2 px, the loudest point in that slice
    const useFine = lv && fine && fine.path === o.path() && t0 >= fine.t0 - 0.05 && t0 + sp <= fine.t0 + fine.p.length * fine.step + 0.1;
    const src = useFine ? fine.p : peaks, st = useFine ? fine.step : c.d / Math.max(1, peaks.length), s0 = useFine ? fine.t0 : 0;
    if (src.length) {
      const cols = Math.floor(W / 3);
      for (let i = 0; i < cols; i++) {
        const a = t0 + i / cols * sp, b = t0 + (i + 1) / cols * sp;
        let j0 = Math.floor((a - s0) / st), j1 = Math.max(j0 + 1, Math.ceil((b - s0) / st)), v = 0;
        for (let j = Math.max(0, j0); j < Math.min(src.length, j1); j++) v = Math.max(v, src[j]);
        const h = Math.max(2, v * (H - 18)), mid = (a + b) / 2;
        x.fillStyle = mid < c.s || mid > c.e ? '#1A3550' : '#05A9C6'; x.fillRect(i * W / cols, (H - h) / 2, Math.max(1, W / cols - 1), h);
      }
    }
    [['s', '#3DDC97', 'START'], ['m', '#FFE000', 'MIX'], ['e', '#FF8A97', 'END']].forEach(([k, col, l]) => {
      const t = c[k]; if (t < t0 - 0.001 || t > t0 + sp + 0.001) return; const Xk = X(t), w = k === mode ? 6 : 4;
      x.fillStyle = col; x.fillRect(Xk - w / 2, 0, w, H); x.font = 'bold 22px Inter'; if (k === 'm' && c.auto) l = 'MIX AUTO';
      const tw = x.measureText(l).width, left = k === 's' ? Xk + 6 : Xk - tw - 6;
      x.fillText(l, Math.max(2, Math.min(W - tw - 2, left)), k === 'm' ? H - 10 : 24);
    });
    const p = o.media().currentTime || 0; if (p >= t0 && p <= t0 + sp) { x.fillStyle = '#fff'; x.fillRect(X(p) - 1.5, 0, 3, H); }
    if (o.label) o.label.textContent = lv ? LV[lv] + ' s' : 'song';
    if (lv) fetchFine();
  }
  cv.style.touchAction = 'none';
  cv.onpointerdown = e => {
    const c = C(); if (!c) return; const t = tAt(e), pxs = span() / cssW();   // seconds per screen pixel
    const near = ['s', 'm', 'e'].reduce((a, k) => Math.abs(c[k] - t) < Math.abs(c[a] - t) ? k : a, 's');
    press = {x: e.clientX, t, k: Math.abs(c[near] - t) <= 9 * pxs ? near : null, v0: win(), moved: false};
    cv.setPointerCapture(e.pointerId);
  };
  cv.onpointermove = e => {
    if (!press) { const c = C(); if (!c) return; const t = tAt(e), pxs = span() / cssW(); cv.style.cursor = ['s', 'm', 'e'].some(k => Math.abs(c[k] - t) <= 9 * pxs) ? 'ew-resize' : lv ? 'grab' : 'pointer'; return; }
    if (!press.moved && Math.abs(e.clientX - press.x) > 4) press.moved = true;
    if (!press.moved) return;
    if (press.k) { const r = cv.getBoundingClientRect(), t = press.v0 + Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * span(); mode = press.k; o.set(press.k, t, false); }
    else if (lv) { V0 = press.v0 - (e.clientX - press.x) / cssW() * span(); mode = 'free'; draw(); }
  };
  cv.onpointerup = e => {
    if (!press) return; const p = press; press = null;
    if (p.moved && p.k) { const r = cv.getBoundingClientRect(), t = p.v0 + Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * span(); o.set(p.k, t, true); }
    else if (!p.moved) { if (mode !== 'free') mode = 'p'; o.seek(p.t); }
    draw();
  };
  cv.onpointercancel = () => { press = null; };
  // the mouse wheel zooms (song · 30 s · 10 s · 4 s), keeping the spot under the mouse in place
  let wheelT = 0;
  cv.addEventListener('wheel', e => {
    const c = C(); if (!c) return; e.preventDefault();
    if (Date.now() - wheelT < 160) return; wheelT = Date.now();
    const r = cv.getBoundingClientRect(), frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), t = tAt(e);
    const d = e.deltaY < 0 ? 1 : -1, nl = Math.max(0, Math.min(LV.length - 1, lv + d)); if (nl === lv) return;
    lv = nl;
    if (lv) { const sp = span(); V0 = Math.max(0, Math.min(Math.max(0, c.d - sp), t - frac * sp)); mode = 'free'; } else mode = 'p';
    draw();
  }, {passive: false});
  return {
    draw,
    reset() { lv = lv || 0; V0 = 0; mode = 'p'; peaks = []; fine = null; draw(); },
    setPeaks(p) { peaks = p || []; draw(); },
    focus(k) { mode = k; draw(); },          // a marker is being set: show it
    follow() { mode = 'p'; draw(); },        // play / jump: follow the play line
    zoom(d) { const c = C(); const keep = c ? (mode === 'p' || mode === 'free' ? (o.media().currentTime || 0) : c[mode]) : 0;
      lv = Math.max(0, Math.min(LV.length - 1, lv + d)); if (mode === 'free') mode = 'p';
      if (c && lv) { const sp = span(); V0 = Math.max(0, Math.min(Math.max(0, c.d - sp), keep - sp / 2)); }
      draw(); },
    get level() { return lv; }
  };
}
