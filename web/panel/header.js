/* Hype Studio – one header for every page. Computer: logo · AUTO/MANUAL · modes · radio/tv · clock ……… pages (right).
   Phone: logo · radio/tv · clock · ☰ (pages) / second row: the buttons. Keys: Alt+1 … Alt+8 */
(function () {
  const P = [['/panel/', 'Panel', 'onair'], ['/panel/library.html', 'Library', 'library'], ['/panel/plan.html', 'Planning', 'plan'],
             ['/panel/history.html', 'History', 'history'], ['/panel/studio.html', 'Broadcast', 'studio'], ['/panel/hypelive.html', 'Hype Live', 'hypelive'], ['/panel/settings.html', 'Settings', 'settings'], ['/', 'Status', 'status']];
  const f = location.pathname.split('/').pop().replace('.html', '') || 'index';
  const cur = !location.pathname.startsWith('/panel') ? 'status' : f === 'index' ? 'onair' : f, home = cur === 'onair';
  const A = home ? 'bAuto' : 'hAuto', M = home ? 'bManual' : 'hManual';     // On Air binds its own buttons
  const st = document.createElement('style');
  st.textContent = `
#hhead{position:sticky;top:0;z-index:60;background:#061423;box-sizing:border-box;width:100%;padding:10px 16px;display:flex;flex-wrap:wrap;align-items:center;gap:14px;font-family:Inter,system-ui,sans-serif;color:#F2F6FA;border-bottom:1px solid #102A42}
#hhead .hb{display:flex;align-items:center;gap:10px;flex:none}#hhead .hb img{width:40px;height:40px}
#hhead .show{font-size:17px;font-weight:900;white-space:nowrap}
#hhead .hctl{display:flex;align-items:center;gap:6px;flex:none}
#hhead .btn{cursor:pointer;border-radius:10px;border:1px solid #23496B;background:#0F2B44;color:#F2F6FA;font:700 11px Inter,system-ui,sans-serif;min-height:32px;padding:0 10px}
#hhead .btn{transition:background .12s,border-color .12s,color .12s;display:inline-flex;align-items:center;justify-content:center;gap:6px;height:32px;line-height:1;letter-spacing:.02em;margin:0;box-shadow:none;text-decoration:none;white-space:nowrap;box-sizing:border-box}   /* the same header on every page (pages style .btn differently) */
#hhead .modes .btn{height:28px;min-height:28px;border:0}
#hhead .btn:hover{border-color:#05A9C6;background:#143049}
#hhead .btn.on{background:#05A9C6;border-color:#05A9C6;color:#061423}#hhead .btn.on:hover{background:#18BEDA}
#hhead .modes{display:inline-flex;gap:3px;background:#071A2B;border-radius:10px;padding:3px;margin:0}
#hhead .modes .btn{border:0;background:transparent;color:#A9BCCD;min-height:28px;white-space:nowrap}
#hhead .modes .btn:hover{background:#143049;color:#fff}#hhead .modes .btn.on:hover{background:#FFE95C;color:#061423}#hhead .modes .btn.on{background:#FFE000;color:#061423}
#hhead .modes .btn[data-mode=live].on{background:#FF4D5E;color:#fff}
#hhead .hst{display:flex;align-items:center;gap:8px;flex:none;position:relative}
#hhead .hdots{display:inline-flex;align-items:center;gap:7px;border:1px solid transparent;background:transparent;border-radius:999px;padding:7px 8px;cursor:pointer}
#hhead .hdots:hover,#hhead .hdots[aria-expanded=true]{background:#0F2B44;border-color:#23496B}
#hhead .hrec{display:inline-flex;align-items:center;gap:6px;border:1px solid #23496B;background:#0F2B44;color:#F2F6FA;border-radius:10px;padding:0 10px;height:32px;cursor:pointer;font:700 11px Inter,system-ui,sans-serif;letter-spacing:.04em;transition:background .12s,border-color .12s}
#hhead .hrec:hover{border-color:#FF4D5E;background:#2A1420}#hhead .hrec.on:hover{background:#FF6676}
#hhead .hrec i{width:9px;height:9px;border-radius:50%;background:#FF4D5E}
#hhead .hrec.on{background:#FF4D5E;border-color:#FF4D5E;color:#fff}#hhead .hrec.on i{background:#fff;animation:hrecp 1.2s infinite}
#hhead .hrec.ask{background:#3A0E18;border-color:#FF4D5E}
#hhead .hlive{display:inline-flex;align-items:center;gap:6px;border:1px solid #FF4D5E;background:transparent;color:#FF8A97;border-radius:10px;padding:0 11px;height:32px;cursor:pointer;font:800 11px Inter,system-ui,sans-serif;letter-spacing:.06em;white-space:nowrap}
#hhead .hlive:hover{background:#2A1420;color:#fff}#hhead .hlive.ask{background:#FFE000;border-color:#FFE000;color:#061423}
#hhead .hlive.on{background:#FF4D5E;color:#fff}#hhead .hlive.on:hover{background:#FF6676}#hhead .hlive.on.ask{background:#FFE000;border-color:#FFE000;color:#061423}
#hhead .hlive .t{font:700 12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
#hhead #hRecT:empty,#hhead #hLiveT:empty{display:none}   /* no time shown = no empty space on the right */
#hhead #hRecT{font:700 12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@keyframes hrecp{50%{opacity:.25}}
#hhead .hpop{position:absolute;top:calc(100% + 8px);left:0;width:330px;max-width:calc(100vw - 24px);background:#0B2236;border:1px solid #23496B;border-radius:14px;box-shadow:0 18px 40px rgba(0,0,0,.45);padding:12px;z-index:70;font-size:13px}
#hhead .hpop h4{margin:10px 0 6px;font-size:11px;letter-spacing:.12em;color:#A9BCCD;text-transform:uppercase}#hhead .hpop h4:first-child{margin-top:0}
#hhead .hpop .r{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid #17344F}#hhead .hpop .r:last-child{border-bottom:0}
#hhead .hpop .r span{color:#A9BCCD}#hhead .hpop .r b{text-align:right;font-weight:700}
#hhead .hpop .ok{color:#3DDC97}#hhead .hpop .bad{color:#FF4D5E}#hhead .hpop .big{font:800 20px ui-monospace,Menlo,Consolas,monospace;color:#FFE000}
#hhead .hpop .note{color:#7F96AB;font-size:11px;margin-top:8px}
#hhead #stRadio,#hhead #stTv{display:inline-block;width:11px;height:11px;padding:0;border-radius:50%;font-size:0;background:#55697D;border:0;margin:0}
#hhead #stRadio.ok,#hhead #stTv.ok{background:#3DDC97;box-shadow:0 0 6px rgba(61,220,151,.5)}#hhead #stRadio.bad,#hhead #stTv.bad{background:#FF4D5E}
#hhead #stClock{display:none}
#hhead #clock{font:800 20px Inter,system-ui,sans-serif;font-variant-numeric:tabular-nums;letter-spacing:.2px;color:#FFE000;margin-left:4px}
#hhead .sp{flex:1}
#hhead .hnav{display:flex;align-items:center;gap:2px;padding:3px;border-radius:12px;background:rgba(10,24,40,.9);border:1px solid #1E3A56;flex:none}
#hhead .hnav a{font:700 13px/1 Inter,system-ui,sans-serif;color:#A9BCCD;text-decoration:none;padding:9px 12px;border-radius:9px;white-space:nowrap}
#hhead .hnav a:hover{background:#143049;color:#fff}#hhead .hnav a.cur{background:#FFE000;color:#061321}
#hhead .hnav a.out{padding:9px 10px;color:#7F96AB}#hhead .hnav a.out .lo{display:none}
#hhead a:focus-visible,#hhead .btn:focus-visible,#hhead .menu:focus-visible{outline:2px solid #05A9C6;outline-offset:1px}
#hhead .menu{display:none;cursor:pointer;border:1px solid #23496B;background:#0F2B44;color:#F2F6FA;border-radius:10px;width:44px;height:40px;font-size:20px;line-height:1}
@media (max-width:1180px){#hhead .hnav a{padding:9px 9px;font-size:12px}#hhead .show{display:none}}
@media (max-width:900px){
  #hhead{flex-wrap:wrap;gap:8px;padding:8px 12px}
  #hhead .hb{order:1}#hhead .hst{order:2}#hhead .sp{order:3}#hhead .menu{display:block;order:4}
  #hhead .hctl{order:5;width:100%;justify-content:space-between}
  #hhead .hctl{gap:6px;flex-wrap:nowrap}
  #hhead .hctl > .btn{flex:1 1 0;min-height:38px;height:38px;font-size:12px;padding:0 6px;display:inline-flex;align-items:center;justify-content:center;text-align:center;margin:0}
  #hhead .modes{flex:1.5 1 0;display:flex;height:38px;box-sizing:border-box}#hhead .modes .btn{flex:1 1 0;min-height:0;height:100%;font-size:11px;padding:0 4px;display:inline-flex;align-items:center;justify-content:center;margin:0}
  #hhead .hnav a.out .lo{display:inline}
  #hhead .hnav{order:6;display:none;width:100%;flex-direction:column;align-items:stretch;padding:6px}
  #hhead.open .hnav{display:flex}#hhead .hnav a{padding:13px 12px;font-size:15px}#hhead .hnav .sep{display:none}
  #hhead #clock{font-size:18px}
  #hhead .hrec{padding:0 9px}
  #hhead .hpop{position:fixed;top:64px;left:12px;right:12px;width:auto}
}
#hhead .hnav .sep{width:1px;height:20px;background:#1E3A56;margin:0 3px}`;
  document.head.appendChild(st);
  const h = document.getElementById('hhead'); if (!h) return;
  document.body.insertBefore(h, document.body.firstChild);
  h.innerHTML = `<div class="hb"><img src="/panel/logo.png" alt="Hype radio tv"><div class="show" id="show">Hype Studio</div></div>
    <div class="hctl"><button class="btn sm" id="${A}">AUTO</button><button class="btn sm" id="${M}">MANUAL</button>
      <span class="modes" role="tablist" aria-label="Mode"><button class="btn sm" data-mode="onair">ON AIR</button><button class="btn sm" data-mode="dj">DJ</button><button class="btn sm" data-mode="live">LIVE</button></span></div>
    <div class="hst"><button class="hdots" id="hDots" aria-haspopup="dialog" aria-expanded="false" title="Streams, listeners and viewers"><span class="chip" id="stRadio">● RADIO</span><span class="chip" id="stTv">● TV</span></button><span class="chip" id="stClock">–</span><span id="clock">--:--:--</span>
      <button class="hlive" id="hLive" title="GO LIVE: your studio mix goes on air (click twice)"><b>GO LIVE</b><span class="t" id="hLiveT"></span></button>
      <button class="hrec" id="hRec" title="Record the TV output (Library → Recordings)"><i></i><b>REC</b><span id="hRecT"></span></button>
      <div class="hpop" id="hPop" role="dialog" aria-label="Streams" hidden></div></div>
    <span class="sp"></span>
    <button class="menu" id="hMenu" aria-label="Pages" aria-expanded="false">☰</button>
    <nav class="hnav" aria-label="Pages">${P.map(([u, t, k], i) => `<a href="${u}" class="${k === cur ? 'cur' : ''}" ${k === cur ? 'aria-current="page"' : ''} title="${t} (Alt+${i + 1})">${t}</a>`).join('')}<span class="sep"></span><a class="out" href="/logout" title="Log out" aria-label="Log out"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-3px"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg><span class="lo"> Log out</span></a></nav>`;
  h.querySelector('#hMenu').onclick = () => { const o = h.classList.toggle('open'); h.querySelector('#hMenu').setAttribute('aria-expanded', o); };
  // radio / tv dots: the meaning on hover (the classes are set by On Air or by this script)
  new MutationObserver(() => { const r = h.querySelector('#stRadio'), t = h.querySelector('#stTv');
    r.title = 'Radio: ' + (r.classList.contains('bad') ? 'problem / stopped' : 'on air'); t.title = 'TV: ' + (t.classList.contains('bad') ? 'problem' : 'on air'); })
    .observe(h.querySelector('.hst'), {attributes: true, subtree: true, attributeFilter: ['class']});
  // ---- status window on the dots ----
  const pop = h.querySelector('#hPop'), dots = h.querySelector('#hDots');
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const yes = (v, a, b) => v ? '<b class="ok">● ' + a + '</b>' : '<b class="bad">● ' + b + '</b>';
  function popRender(d) {
    const r = d.radio || {}, t = d.tv || {}, p = d.public || {}, sv = d.server || {};
    pop.innerHTML = `<h4>Radio</h4>
      <div class="r"><span>Playout</span>${yes(r.on_air, 'on air', 'stopped')}</div>
      <div class="r"><span>Hype Studio stream (MP3 ${r.kbps || '–'}k)</span>${yes(r.ok, 'live', 'down')}</div>
      <div class="r"><span>Listeners on this stream</span><b class="big">${r.listeners ?? '–'}</b></div>
      <div class="r"><span>Own streams now (MP3 + HLS)</span><b class="big">${d.own ? d.own.current : '–'}</b></div>
      <div class="r"><span>· MP3 / HLS · peak today</span><b>${d.own ? d.own.mp3 + ' / ' + d.own.hls + ' · ' + d.own.peak_today : '–'}</b></div>
      <div class="r"><span>Public radio (Streamerr) now</span><b class="big">${p.ok ? p.listeners : '–'}</b></div>
      <h4>TV</h4>
      <div class="r"><span>OBS streaming</span>${yes(t.obs, 'yes', 'no')}</div>
      <div class="r"><span>TV stream (HLS)</span>${t.seg_age == null ? '<b class="bad">● no pieces</b>' : yes(t.seg_age < 8, 'live · ' + t.seg_age + ' s', 'late · ' + t.seg_age + ' s')}</div>
      <div class="r"><span>Viewers on this stream</span><b class="big">${t.viewers ?? '–'}</b></div>
      <div class="r"><span>Scene</span><b>${esc(t.scene || '–')}</b></div>
      <h4>Guard · alarm</h4>
      <div class="r"><span>Self-repair</span>${!d.guard || !d.guard.fresh ? '<b class="bad">● not running</b>' : (d.guard.problems || []).length ? '<b class="bad">● ' + d.guard.problems.map(x => esc(x.text)).join(' · ') + '</b>' : '<b class="ok">● all OK</b>'}</div>
      <div class="r"><span>Hype Live alarm link</span>${d.guard && d.guard.heartbeat && d.guard.heartbeat.ok ? '<b class="ok">● connected</b>' : '<b style="color:#FFB547">● ' + esc((d.guard && d.guard.heartbeat && d.guard.heartbeat.answer) || 'off') + '</b>'}</div>
      ${d.guard && (d.guard.events || []).filter(e => e.level !== 'ok').length ? '<div class="note">Last: ' + esc(d.guard.events.filter(e => e.level !== 'ok')[0].text) + '</div>' : ''}
      <h4>Server</h4>
      <div class="r"><span>Load</span><b>${sv.load ?? '–'} / ${sv.cpus ?? '–'} cores</b></div>
      <div class="r"><span>Free on /srv</span><b>${sv.disk_free_gb ?? '–'} GB</b></div>
      <div class="note">The public radio still runs via Streamerr; once Hype Studio's own streams are live, all numbers come from here.</div>`;
  }
  let popT = null;
  function popLoad() { fetch('/api/streams').then(r => r.json()).then(popRender).catch(() => { pop.innerHTML = '<div class="r"><span>Status</span><b class="bad">● no answer</b></div>'; }); }
  function popOpen(o) { pop.hidden = !o; dots.setAttribute('aria-expanded', o); clearInterval(popT); if (o) { popLoad(); popT = setInterval(popLoad, 5000); } }
  dots.onclick = e => { e.stopPropagation(); popOpen(pop.hidden); };
  document.addEventListener('click', e => { if (!pop.hidden && !pop.contains(e.target)) popOpen(false); });
  addEventListener('keydown', e => { if (e.key === 'Escape' && !pop.hidden) { popOpen(false); dots.focus(); } });
  // ---- REC: start at once, stop with a second click (so it never stops by accident) ----
  const rb = h.querySelector('#hRec'); let REC = null, recAsk = 0;
  const fmtT = s => { s = Math.max(0, Math.floor(s)); const hh = Math.floor(s / 3600), mm = Math.floor(s % 3600 / 60), ss = s % 60; return (hh ? hh + ':' + String(mm).padStart(2, '0') : mm) + ':' + String(ss).padStart(2, '0'); };
  function recPaint() {
    const on = !!(REC && REC.since); rb.classList.toggle('on', on); rb.classList.toggle('ask', !on && false);
    if (recAsk && Date.now() < recAsk) { rb.classList.add('ask'); rb.querySelector('b').textContent = 'STOP?'; }
    else { rb.classList.remove('ask'); rb.querySelector('b').textContent = 'REC'; recAsk = 0; }
    h.querySelector('#hRecT').textContent = on ? fmtT(Date.now() / 1000 - REC.since) : '';
    rb.title = on ? 'Recording since ' + new Date(REC.since * 1000).toTimeString().slice(0, 5) + ' – click twice to stop' : 'Record the TV output (Library → Recordings)';
  }
  function recLoad() { fetch('/api/live').then(r => r.json()).then(d => { REC = d.rec; LVH = d; recPaint(); livePaint(); }).catch(() => {}); }
  // ---- GO LIVE / END LIVE: always a second click (going live or ending by accident would be worse than one extra click) ----
  const lb = h.querySelector('#hLive'); let LVH = null, liveAsk = 0;
  function livePaint() {
    const on = !!(LVH && LVH.live), ask = liveAsk && Date.now() < liveAsk; if (!ask) liveAsk = 0;
    lb.classList.toggle('on', on); lb.classList.toggle('ask', !!ask);
    lb.querySelector('b').textContent = ask ? (on ? 'END LIVE?' : (LVH && !LVH.studio_online ? 'STUDIO OFFLINE – GO?' : 'GO LIVE?')) : (on ? '● LIVE' : 'GO LIVE');
    h.querySelector('#hLiveT').textContent = on && LVH.live_since && !ask ? fmtT(Date.now() / 1000 - LVH.live_since) : '';
    lb.title = on ? 'You are LIVE – click twice to END LIVE (back to the automation)' : (LVH && !LVH.studio_online ? 'The studio is not connected (Broadcast page) – going live now puts the automation on until it connects' : 'GO LIVE: your studio mix goes on air (click twice)');
  }
  lb.onclick = () => {
    if (!(liveAsk && Date.now() < liveAsk)) { liveAsk = Date.now() + 3000; livePaint(); return; }
    const on = !!(LVH && LVH.live); liveAsk = 0;
    fetch('/api/live/' + (on ? 'off' : 'on'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).then(r => r.json()).then(d => { LVH = d; REC = d.rec; livePaint(); recPaint(); if (window.hypeLiveChanged) hypeLiveChanged(d); }).catch(() => {});
  };
  setInterval(livePaint, 1000);
  rb.onclick = () => {
    const on = !!(REC && REC.since);
    if (on && !(recAsk && Date.now() < recAsk)) { recAsk = Date.now() + 3000; recPaint(); return; }
    recAsk = 0;
    fetch('/api/live/' + (on ? 'rec_stop' : 'rec_start'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).then(r => r.json()).then(d => { REC = d.rec; recPaint(); }).catch(() => {});
  };
  // On Air and Broadcast already poll /api/live: they hand REC over (window.hypeRec), so no second poll here
  window.hypeRec = (rec, d) => { REC = rec; recPaint(); if (d) { LVH = d; livePaint(); } };
  recLoad(); if (!home && cur !== 'studio') setInterval(recLoad, 5000); setInterval(recPaint, 1000);
  addEventListener('keydown', e => { if (e.altKey && !e.ctrlKey && e.key >= '1' && e.key <= '8') { e.preventDefault(); location.href = P[+e.key - 1][0]; } });
  if (home) return;                                                    // On Air updates all of this itself
  const q = s => h.querySelector(s); let S = null, skew = 0;
  const api = (p, body) => fetch('/api/' + p, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
  let MODE = 'onair'; try { MODE = localStorage.getItem('hypeMode') || 'onair'; } catch (e) {}
  function paint() {
    if (!S) return;
    q('#' + A).classList.toggle('on', !!S.auto); q('#' + M).classList.toggle('on', !S.auto);
    q('#stRadio').className = 'chip ' + (S.stopped ? 'bad' : 'ok'); q('#stTv').className = 'chip ' + (S.scene ? 'ok' : 'bad');
    const m = S.dj && S.dj.on ? 'dj' : MODE;
    h.querySelectorAll('.modes [data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  }
  function load() { api('state').then(d => { S = d; skew = Date.now() / 1000 - d.server_time; paint(); }).catch(() => { q('#stRadio').className = 'chip bad'; }); }
  load(); setInterval(load, 4000);
  // a new Hype Studio version was installed: offer a reload (never reloads by itself while you are working)
  let ver = null;
  const verCheck = () => fetch('/api/version', {cache: 'no-store'}).then(r => r.ok ? r.json() : null).then(d => {
    if (!d || !d.v) return; if (ver === null) { ver = d.v; return; }
    if (d.v !== ver && !document.getElementById('hUpd')) {
      const b = document.createElement('button'); b.id = 'hUpd'; b.textContent = '↻ Hype Studio was updated – click to reload';
      b.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:9999;background:#FFE000;color:#061423;border:0;border-radius:12px;padding:11px 18px;font:800 14px Inter,system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.45);cursor:pointer';
      b.onclick = () => location.reload(); document.body.appendChild(b);
    } }).catch(() => {});
  verCheck(); setInterval(verCheck, 60000);
  setInterval(() => { q('#clock').textContent = new Date(Date.now() - skew * 1000).toTimeString().slice(0, 8); }, 250);
  q('#' + A).onclick = () => api('cmd/auto', {on: true}).then(load);
  q('#' + M).onclick = () => api('cmd/auto', {on: false}).then(load);
  h.querySelectorAll('.modes [data-mode]').forEach(b => b.onclick = () => {
    const m = b.dataset.mode, wasDj = S && S.dj && S.dj.on;
    try { localStorage.setItem('hypeMode', m); } catch (e) {}
    const go = () => { location.href = '/panel/'; };
    if (m === 'dj' && !wasDj) api('cmd/dj', {on: true}).then(go, go); else if (m !== 'dj' && wasDj) api('cmd/dj', {on: false}).then(go, go); else go();
  });
})();
