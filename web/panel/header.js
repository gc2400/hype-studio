/* Hype Studio – one header for every page. Computer: logo · AUTO/MANUAL · modes · radio/tv · clock ……… pages (right).
   Phone: logo · radio/tv · clock · ☰ (pages) / second row: the buttons. Keys: Alt+1 … Alt+8 */
(function () {
  const P = [[(() => { try { return (localStorage.getItem('hypeCh') || 'main') !== 'main'; } catch (e) { return false; } })() ? '/panel/channel.html' : '/panel/', 'Panel', 'onair'], ['/panel/library.html', 'Library', 'library'], ['/panel/plan.html', 'Planning', 'plan'], ['/panel/voice.html', 'Voice', 'voice'],
             ['/panel/history.html', 'Reports', 'history'], ['/panel/studio.html', 'Broadcast', 'studio'], ['/panel/hypelive.html', 'Hype Live', 'hypelive'], ['/panel/settings.html', 'Settings', 'settings'], ['/', 'Status', 'status']];
  // ---- the channel you work on: Hype Radio ("main") or a theme channel (Hype Ambiente, Hype Latina …) ----
  // Planning, Library (playlists), Reports (history / rights) and Settings → Sound then use that channel's own data: every /api call
  // for those paths gets ?ch=<id> (the engine ignores it everywhere else). Panel = the channel's own page (channel.html).
  let CH = 'main'; try { CH = localStorage.getItem('hypeCh') || 'main'; } catch (e) {}
  const CH_PATHS = /^\/(playlists|playlist|folders|padbanks|pads|listener_pads|library|clocks|shows|schedule|events|preview_hour|history|history\.csv|rights\.csv|rights_check|week|weekreport|level|audstats|aud24|audsongs|audtime|settings)$|^\/plan\//;
  if (CH !== 'main' && !window.__chFetch) {
    window.__chFetch = true; const F = window.fetch.bind(window);
    window.fetch = (u, o) => { try { if (typeof u === 'string' && u.includes('api/')) { const x = new URL(u, location.href), pth = x.pathname.replace(/^\/api/, '');
      if (CH_PATHS.test(pth) && !x.searchParams.has('ch')) { x.searchParams.set('ch', CH); u = x.pathname + x.search; } } } catch (e) {} return F(u, o); };
  }
  const f0 = location.pathname.split('/').pop().replace('.html', '') || 'index';
  if (CH !== 'main' && f0 === 'index' && location.pathname.startsWith('/panel')) { location.replace('/panel/channel.html'); return; }
  if (CH === 'main' && f0 === 'channel') { location.replace('/panel/'); return; }
  const f = f0;
  const cur = !location.pathname.startsWith('/panel') ? 'status' : f === 'index' || f === 'channel' ? 'onair' : f === 'help' ? 'settings' : f, home = cur === 'onair' && f !== 'channel';
  const hSkew = () => { try { return +localStorage.getItem('hypeSkew') || 0; } catch (e) { return 0; } };   // server-time offset kept between pages, so the clock never restarts
  const hNow = () => new Date(Date.now() - hSkew() * 1000).toTimeString().slice(0, 8);
  const A = home ? 'bAuto' : 'hAuto', M = home ? 'bManual' : 'hManual';     // On Air binds its own buttons
  const st = document.createElement('style');
  st.textContent = `
html{scrollbar-gutter:stable}
#hhead{position:sticky;top:0;z-index:60;background:#061423;box-sizing:border-box;width:100%;padding:10px 16px;display:flex;flex-wrap:wrap;align-items:center;gap:14px;font-family:Inter,system-ui,sans-serif;color:#F2F6FA;border-bottom:1px solid #102A42}
#hhead .hb{display:flex;align-items:center;gap:10px;flex:none;position:relative}#hhead .hb img{width:40px;height:40px}
#hhead .chsw{display:inline-flex;align-items:center;gap:8px;cursor:pointer;border:1px solid transparent;background:transparent;color:#F2F6FA;border-radius:10px;padding:4px 8px;font:900 17px Inter,system-ui,sans-serif;white-space:nowrap}
#hhead .chsw:hover,#hhead .chsw[aria-expanded=true]{background:#0F2B44;border-color:#23496B}#hhead .chsw .cd{width:10px;height:10px;border-radius:50%;flex:none}#hhead .cd.lg{width:24px!important;height:24px!important}#hhead .chsw .car{font-size:11px;color:#A9BCCD}
#hhead .chmenu{position:absolute;top:calc(100% + 6px);left:0;z-index:90;min-width:250px;background:#0B2236;border:1px solid #23496B;border-radius:14px;padding:6px;box-shadow:0 18px 40px rgba(0,0,0,.5);display:flex;flex-direction:column;gap:2px}
#hhead .chmenu button{display:flex;align-items:center;gap:10px;width:100%;text-align:left;border:0;background:transparent;color:#F2F6FA;font:700 14px Inter,system-ui,sans-serif;padding:10px;border-radius:10px;cursor:pointer}
#hhead .chmenu button:hover{background:#0F2B44}#hhead .chmenu button.cur{background:#143049}#hhead .chmenu .cd{width:10px;height:10px;border-radius:50%;flex:none}#hhead .chmenu small{margin-left:auto;color:#7F96AB;font-weight:700;font-size:11px}
#hhead.ch-theme .hctl,#hhead.ch-theme #hLive,#hhead.ch-theme #hRec,#hhead.ch-theme #stRadio,#hhead.ch-theme #stTv,#hhead.ch-theme .hnav a[data-k=voice],#hhead.ch-theme .hnav a[data-k=studio],#hhead.ch-theme .hnav a[data-k=hypelive]{display:none!important}
#hchbar{display:flex;align-items:center;gap:10px;justify-content:center;font:800 12px Inter,system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;padding:6px 12px;color:#061423}
#hhead .show{font-size:17px;font-weight:900;white-space:nowrap}
#hhead .hctl{display:flex;align-items:center;gap:6px;flex:none}
#hhead .btn{cursor:pointer;border-radius:10px;border:1px solid #23496B;background:#0F2B44;color:#F2F6FA;font:700 11px Inter,system-ui,sans-serif;min-height:32px;padding:0 10px}
#hhead .btn{transition:background .12s,border-color .12s,color .12s;display:inline-flex;align-items:center;justify-content:center;gap:6px;height:32px;line-height:1;letter-spacing:.02em;margin:0;box-shadow:none;text-decoration:none;white-space:nowrap;box-sizing:border-box}   /* the same header on every page (pages style .btn differently) */
#hhead .modes .btn{height:28px;min-height:28px;border:0}
#hhead .btn:hover{border-color:#05A9C6;background:#143049}
#hhead .btn.on{background:#05A9C6;border-color:#05A9C6;color:#061423}#hhead .btn.on:hover{background:#18BEDA}
#hhead .modes{display:inline-flex;gap:2px;background:#071A2B;border:1px solid #23496B;border-radius:12px;padding:3px;margin:0}
#hhead .modes .btn{border:0;background:transparent;color:#A9BCCD;min-height:28px;white-space:nowrap}
#hhead .modes .btn:hover{background:#0F2B44;color:#fff}#hhead .modes .btn.on:hover{background:#FFE95C;color:#061423}#hhead .modes .btn.on{background:#FFE000;color:#061423}#hhead .modes .btn{font:800 12px Inter,system-ui,sans-serif;border-radius:9px;padding:0 10px}
#hhead .modes .btn[data-mode=live].on{background:#FF4D5E;color:#fff}
#hhead .hst{display:flex;align-items:center;gap:8px;flex:none;position:relative}
#hhead .hdots{display:inline-flex;align-items:center;gap:6px;border:1px solid transparent;background:transparent;border-radius:999px;padding:7px 8px;cursor:pointer}
#hhead .hdots:hover,#hhead .hdots[aria-expanded=true]{background:#0F2B44;border-color:#23496B}
#hhead .hrec{display:inline-flex;align-items:center;gap:6px;border:1px solid #23496B;background:#0F2B44;color:#F2F6FA;border-radius:10px;padding:0 10px;height:32px;cursor:pointer;font:700 11px Inter,system-ui,sans-serif;letter-spacing:.04em;transition:background .12s,border-color .12s}
#hhead .hrec:hover{border-color:#FF4D5E;background:#2A1420}#hhead .hrec.on:hover{background:#FF6676}
#hhead .hrec i{width:9px;height:9px;border-radius:50%;background:#FF4D5E}
#hhead .hrec.on{background:#FF4D5E;border-color:#FF4D5E;color:#fff}#hhead .hrec.on i{background:#fff;animation:hrecp 1.2s infinite}
#hhead .hrec.ask{background:#3A0E18;border-color:#FF4D5E}
#hhead .hlive{display:inline-flex;align-items:center;gap:6px;border:1px solid #FF4D5E;background:transparent;color:#FF8A97;border-radius:10px;padding:0 11px;height:32px;cursor:pointer;font:800 11px Inter,system-ui,sans-serif;letter-spacing:.06em;white-space:nowrap}
#hhead .hlive:hover{background:#2A1420;color:#fff}#hhead .hlive.ask{background:#FFE000;border-color:#FFE000;color:#061423}
#hhead .hlive.on{background:#FF4D5E;color:#fff}#hhead .hlive.on:hover{background:#FF6676}#hhead .hlive.on.ask{background:#FFE000;border-color:#FFE000;color:#061423}
#hhead .hlive.off{opacity:.45;cursor:not-allowed}#hhead .hlive.off:hover{background:transparent;color:#FF8A97}
#hhead .hlive .t{font:700 12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
#hhead #hRecT:empty,#hhead #hLiveT:empty,#hhead .hrec b:empty{display:none}   /* no time shown = no empty space on the right */
#hhead #hRecT{font:700 12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@keyframes hrecp{50%{opacity:.25}}
#hhead .hpop{position:absolute;top:calc(100% + 8px);left:0;width:330px;max-width:calc(100vw - 24px);background:#0B2236;border:1px solid #23496B;border-radius:14px;box-shadow:0 18px 40px rgba(0,0,0,.45);padding:12px;z-index:70;font-size:13px}
#hhead .hpop h4{margin:10px 0 6px;font-size:11px;letter-spacing:.12em;color:#A9BCCD;text-transform:uppercase}#hhead .hpop h4:first-child{margin-top:0}
#hhead .hpop .r{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid #17344F}#hhead .hpop .r:last-child{border-bottom:0}
#hhead .hpop .r span{color:#A9BCCD}#hhead .hpop .r b{text-align:right;font-weight:700}
#hhead .hpop .ok{color:#3DDC97}#hhead .hpop .bad{color:#FF4D5E}#hhead .hpop .big{font:800 20px ui-monospace,Menlo,Consolas,monospace;color:#FFE000}
#hhead .hpop .note{color:#7F96AB;font-size:11px;margin-top:8px}
#hhead #stRadio,#hhead #stTv{display:inline-block;width:18px;height:18px;padding:0;border-radius:50%;font-size:0;background:#55697D;border:0;margin:0}
#hhead #stRadio[data-n],#hhead #stTv[data-n],#hhead #stLog[data-n]{width:auto;min-width:18px;height:18px;padding:0 5px;border-radius:9px;font:800 11px/18px Inter,system-ui,sans-serif;color:#061423;text-align:center;box-sizing:border-box}
#hhead #stRadio.bad[data-n],#hhead #stTv.bad[data-n],#hhead #stLog.bad[data-n]{color:#fff}
#hhead #stLog{display:inline-block;width:18px;height:18px;padding:0;border-radius:50%;font-size:0;border:0;margin:0;background:#3DDC97;box-shadow:0 0 6px rgba(61,220,151,.5)}
#hhead #stLog.warn{background:#FFB547;box-shadow:0 0 6px rgba(255,181,71,.6)}#hhead #stLog.bad{background:#FF4D5E;box-shadow:0 0 6px rgba(255,77,94,.6)}
#hhead #hBox{width:400px;max-height:72vh;overflow:auto}
#hhead .htabs{display:flex;gap:3px;background:#071A2B;border-radius:10px;padding:3px;margin-bottom:10px}
#hhead .htabs button{flex:1;border:0;background:transparent;color:#A9BCCD;border-radius:8px;min-height:28px;font:700 12px Inter,system-ui,sans-serif;cursor:pointer}
#hhead .htabs button.on{background:#05A9C6;color:#061423}#hhead .htabs button:hover:not(.on){background:#143049;color:#fff}
#hhead #hLogPop .m{display:flex;gap:9px;align-items:flex-start;padding:7px 0;border-bottom:1px solid #17344F}#hhead #hLogPop .m:last-child{border-bottom:0}
#hhead #hLogPop .m i{width:9px;height:9px;border-radius:50%;margin-top:5px;flex:none}
#hhead #hLogPop .m small{display:block;color:#7F96AB;font-size:11px;margin-top:1px}
#hhead #stRadio.ok,#hhead #stTv.ok{background:#3DDC97;box-shadow:0 0 6px rgba(61,220,151,.5)}#hhead #stRadio.bad,#hhead #stTv.bad{background:#FF4D5E;box-shadow:0 0 6px rgba(255,77,94,.5)}
#hhead #stClock{order:5;display:none;align-items:center;gap:5px;max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font:700 12px Inter,system-ui,sans-serif;color:#C9D7E3;background:#0F2B44;border:1px solid #23496B;border-radius:999px;padding:4px 10px;text-decoration:none}
#hhead #stClock:hover{border-color:#05A9C6;color:#fff}
#hhead .hst #clock{order:4}#hhead .hst>.hdots{order:1}
#hhead #clock{font:800 20px Inter,system-ui,sans-serif;font-variant-numeric:tabular-nums;letter-spacing:.2px;color:#FFE000;margin-left:4px}
#hhead .sp{flex:1}
#hhead .hb{order:1}#hhead .hst{order:2}#hhead .hctl{order:3}#hhead .hact{order:4;display:flex;align-items:center;gap:8px;flex:none}#hhead .sp{order:5}#hhead .hnav{order:6}#hhead .menu{order:7}
#hhead .hnav{display:flex;align-items:center;gap:2px;padding:0;border-radius:12px;background:transparent;border:1px solid transparent;flex:none}
#hhead .hnav a{font:700 13px/1 Inter,system-ui,sans-serif;color:#C9D7E3;text-decoration:none;padding:9px 11px;border-radius:9px;white-space:nowrap}
#hhead .hnav a:hover{background:#0F2B44;color:#fff}#hhead .hnav a.cur{background:#FFE000;color:#061423;font-weight:800}
#hhead .hnav a.out{padding:9px 10px;color:#7F96AB}#hhead .hnav a.out .lo{display:none}
#hhead a:focus-visible,#hhead .btn:focus-visible,#hhead .menu:focus-visible{outline:2px solid #05A9C6;outline-offset:1px}
#hhead .menu{display:none;cursor:pointer;border:1px solid #23496B;background:#0F2B44;color:#F2F6FA;border-radius:10px;width:44px;height:40px;font-size:20px;line-height:1}
@media (max-width:1180px){#hhead .hnav a{padding:9px 9px;font-size:12px}#hhead .show{display:none}}
@media (max-width:1180px){#hhead .chsw{font-size:14px;padding:4px 6px}}@media (max-width:600px){#hhead #chName{display:none}#hhead .chmenu{min-width:220px}}
@media (max-width:900px){
  #hhead{flex-wrap:wrap;gap:8px;padding:8px 12px}
  #hhead .hb{order:1}#hhead .hst{order:2}#hhead .hact{order:2}#hhead .sp{order:3}#hhead .menu{display:block;order:4}
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
@media (max-width:600px){
  #hhead{gap:6px;padding:6px 8px}
  #hhead .hb img{width:30px;height:30px}
  #hhead .hst{gap:2px}#hhead .hdots{padding:5px 4px;gap:4px}
  #hhead #clock{font-size:15px;margin-left:2px}
  #hhead .hact{gap:4px}#hhead .hlive{height:30px;padding:0 7px;font-size:10px;letter-spacing:.02em}#hhead .hrec{height:30px;padding:0 7px;font-size:10px}
  #hhead .hlive .t,#hhead .hrec #hRecT{font-size:10px}
  #hhead .menu{width:36px;height:32px;font-size:17px}
  #hhead .sp{display:none}#hhead .menu{margin-left:auto}
  #hhead .hctl{gap:5px}#hhead .modes{height:34px}#hhead .modes .btn{font-size:10.5px}
  #hhead .hpop{position:fixed;top:64px;left:12px;right:12px;width:auto}
}
#hhead .hnav .sep{width:1px;height:20px;background:#1E3A56;margin:0 3px}
/* wider than a phone, but the page links don't fit on the line any more: the ☰ menu, like on a phone (a drop-down on the right) */
#hhead.compact .menu{display:block}
#hhead.compact .hnav{display:none;position:absolute;top:calc(100% + 6px);right:12px;flex-direction:column;align-items:stretch;padding:6px;min-width:230px;z-index:80;background:#0B2236;border-color:#23496B;box-shadow:0 18px 40px rgba(0,0,0,.45)}
#hhead.compact.open .hnav{display:flex}
#hhead.compact .hnav a{padding:12px;font-size:14px}#hhead.compact .hnav .sep{display:none}#hhead.compact .hnav a.out .lo{display:inline}`;
  document.head.appendChild(st);
  const h = document.getElementById('hhead'); if (!h) return;
  document.body.insertBefore(h, document.body.firstChild);
  h.innerHTML = `<div class="hb"><img src="/panel/logo.png" alt="Hype radio tv"><button class="chsw" id="chSw" aria-haspopup="menu" aria-expanded="false" title="The channel you work on"><i class="cd" style="background:#05A9C6"></i><span id="chName">Hype Radio</span><span class="car">▾</span></button><div class="show" id="show" hidden>Hype Studio</div><div class="chmenu" id="chMenu" role="menu" hidden></div></div>
    <div class="hctl"><span class="modes" role="group" aria-label="Programming"><button class="btn sm" id="${A}">AUTO</button><button class="btn sm" id="${M}">MANUAL</button></span>
      <span class="modes" role="tablist" aria-label="Mode"><button class="btn sm" data-mode="onair">ON AIR</button><button class="btn sm" data-mode="dj">DJ</button><button class="btn sm" data-mode="live">LIVE</button></span></div>
    <div class="hst"><button class="hdots" id="hDots" aria-haspopup="dialog" aria-expanded="false" title="Streams, listeners and viewers"><span class="chip" id="stRadio">● RADIO</span><span class="chip" id="stTv">● TV</span><span class="chip" id="stLog">● LOG</span></button><a class="chip" id="stClock" href="/panel/plan.html" title="The hour plan that plays now – click: Planning">–</a><span id="clock">${hNow()}</span>
      <div class="hpop" id="hBox" role="dialog" aria-label="Status and log" hidden><div class="htabs" role="tablist"><button data-ht="s" role="tab">Streams</button><button data-ht="l" role="tab">Log</button></div><div id="hPop"></div><div id="hLogPop" hidden></div></div></div>
    <div class="hact">
      <button class="hlive" id="hLive" title="GO LIVE: your studio mix goes on air (click twice)"><b>GO LIVE</b><span class="t" id="hLiveT"></span></button>
      <button class="hrec" id="hRec" title="Record the TV output (Library → Recordings)"><i></i><b>REC</b><span id="hRecT"></span></button>
    </div>
    <span class="sp"></span>
    <button class="menu" id="hMenu" aria-label="Pages" aria-expanded="false">☰</button>
    <nav class="hnav" aria-label="Pages">${P.map(([u, t, k], i) => `<a href="${u}" data-k="${k}" class="${k === cur ? 'cur' : ''}" ${k === cur ? 'aria-current="page"' : ''} title="${t} (Alt+${i + 1})">${t}</a>`).join('')}<span class="sep"></span><a class="out" href="/logout" title="Log out" aria-label="Log out"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-3px"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg><span class="lo"> Log out</span></a></nav>`;
  // page changes: show the header exactly as it was (dots, numbers, AUTO/MANUAL, mode, GO LIVE, REC) until fresh data comes in
  const SNAP = () => [['A', h.querySelector('#' + A)], ['M', h.querySelector('#' + M)], ...[...h.querySelectorAll('.modes [data-mode]')].map(b => ['m_' + b.dataset.mode, b]),
    ...['stRadio', 'stTv', 'stLog', 'hLive', 'hLiveT', 'hRec', 'hRecT'].map(i => [i, h.querySelector('#' + i)])].filter(x => x[1]);
  try { const o = JSON.parse(sessionStorage.getItem('hypeHdr') || 'null'); if (o) SNAP().forEach(([k, el]) => { const v = o[k]; if (v) { el.className = v[0]; el.innerHTML = v[1]; } }); } catch (e) {}
  const snapSave = () => { try { const o = {}; SNAP().forEach(([k, el]) => { o[k] = [el.className, el.innerHTML]; }); sessionStorage.setItem('hypeHdr', JSON.stringify(o)); } catch (e) {} };
  setInterval(snapSave, 1000); addEventListener('pagehide', snapSave);
  // a theme channel: what only Hype Radio has goes away (AUTO/MANUAL, ON AIR/DJ/LIVE, GO LIVE, REC, the radio / TV dots, Voice, Broadcast, Hype Live)
  if (CH !== 'main') h.classList.add('ch-theme');
  // ---- channel switch ----
  const chSw = h.querySelector('#chSw'), chMenu = h.querySelector('#chMenu'); let CHS = [{id: 'main', name: 'Hype Radio', color: '#05A9C6', enabled: true, running: true}];
  function chPaint() {
    const c = CHS.find(x => x.id === CH) || CHS[0];
    const cd = chSw.querySelector('.cd'); cd.style.background = c.logo ? `center/cover url(${c.logo})` : c.color; cd.classList.toggle('lg', !!c.logo); h.querySelector('#chName').textContent = c.name;
    let bar = document.getElementById('hchbar');
    if (CH !== 'main') {
      if (!bar) { bar = document.createElement('div'); bar.id = 'hchbar'; h.after(bar); }
      bar.style.background = c.color; bar.textContent = 'You are working on ' + c.name + (c.enabled ? '' : ' (off)') + ' – Hype Radio is not affected';
    } else if (bar) bar.remove();
    chMenu.innerHTML = CHS.map(x => `<button role="menuitemradio" aria-checked="${x.id === CH}" class="${x.id === CH ? 'cur' : ''}" data-ch="${x.id}"><i class="cd${x.logo ? ' lg' : ''}" style="background:${x.logo ? `center/cover url(${x.logo})` : x.color}"></i>${x.name}<small>${x.id === 'main' ? 'main' : x.down ? 'service off' : !x.enabled ? 'off' : x.running ? 'on air' : 'starting'}</small></button>`).join('');
    chMenu.querySelectorAll('[data-ch]').forEach(b => b.onclick = () => {
      const to = b.dataset.ch; try { localStorage.setItem('hypeCh', to); } catch (e) {}
      const page = location.pathname.split('/').pop() || 'index.html';
      location.href = (to === 'main' && page === 'channel.html') ? '/panel/' : (to !== 'main' && (page === '' || page === 'index.html' || location.pathname === '/panel/')) ? '/panel/channel.html' : location.pathname + location.search; });
  }
  chSw.onclick = e => { e.stopPropagation(); const o = chMenu.hidden; chMenu.hidden = !o; chSw.setAttribute('aria-expanded', o); };
  document.addEventListener('click', e => { if (!chMenu.hidden && !chMenu.contains(e.target)) { chMenu.hidden = true; chSw.setAttribute('aria-expanded', false); } });
  const chLoad = () => fetch('/api/channels', {cache: 'no-store'}).then(r => r.ok ? r.json() : null).then(d => { if (d && d.channels) { CHS = [CHS[0], ...d.channels]; if (!CHS.some(x => x.id === CH)) { CH = 'main'; try { localStorage.setItem('hypeCh', 'main'); } catch (e) {} } chPaint(); } }).catch(() => {});
  chPaint(); chLoad(); setInterval(() => { if (!document.hidden) chLoad(); }, 30000);
  window.HYPE_CH = () => CH;
  h.querySelector('#hMenu').onclick = () => { const o = h.classList.toggle('open'); h.querySelector('#hMenu').setAttribute('aria-expanded', o); };
  // do the page links fit next to the rest? if not (they would wrap to a second line): the ☰ menu
  const nav = h.querySelector('.hnav'), hb = h.querySelector('.hb');
  function fitNav() {
    if (window.innerWidth <= 900) { h.classList.remove('compact'); return; }      // phone layout has its own menu
    const was = h.classList.contains('compact'); h.classList.remove('compact');
    const wraps = [nav, h.querySelector('.hst'), h.querySelector('.hctl'), h.querySelector('.hact')].some(x => x && x.offsetTop > hb.offsetTop + hb.offsetHeight / 2);   // anything on a 2nd line: pages into the ☰ menu
    h.classList.toggle('compact', wraps); if (!wraps && was) h.classList.remove('open');
  }
  let fitT; window.addEventListener('resize', () => { clearTimeout(fitT); fitT = setTimeout(fitNav, 80); });
  fitNav(); setTimeout(fitNav, 300); document.fonts && document.fonts.ready.then(fitNav);
  document.addEventListener('click', e => { if (h.classList.contains('compact') && h.classList.contains('open') && !h.contains(e.target)) { h.classList.remove('open'); h.querySelector('#hMenu').setAttribute('aria-expanded', false); } });
  // radio / tv dots: the meaning on hover (the classes are set by On Air or by this script)
  new MutationObserver(() => { const r = h.querySelector('#stRadio'), t = h.querySelector('#stTv');
    r.title = 'Radio: ' + (r.classList.contains('bad') ? 'problem / stopped' : 'on air') + (r.dataset.n ? ' · ' + r.textContent + ' listening now' : ''); t.title = 'TV: ' + (t.classList.contains('bad') ? 'problem' : 'on air') + (t.dataset.n ? ' · ' + t.textContent + ' watching now' : ''); })
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
      <div class="r"><span>Hype Live sync</span>${d.guard && d.guard.sync && d.guard.sync.active ? '<b class="ok">● one call a minute + ⚡ poke</b>' : '<b style="color:#8FA3B8">● old polls (Hype Live before 2.41)</b>'}</div>
      <div class="r"><span>Hype Live clock ping</span>${d.guard && d.guard.tick && d.guard.tick.ok === true ? '<b class="ok">● every minute</b>' : d.guard && d.guard.tick && d.guard.tick.ok === false ? '<b style="color:#FFB547">● failing ' + (d.guard.tick.fails || 0) + '× – ' + esc(d.guard.tick.error || '') + '</b>' : '<b style="color:#8FA3B8">● ' + esc((d.guard && d.guard.tick && d.guard.tick.error) || 'not yet') + '</b>'}</div>
      <div class="r"><span>Hype Live media link</span>${d.guard && d.guard.media_link && d.guard.media_link.ok && d.guard.media_link.age < 120 ? '<b class="ok">● ' + d.guard.media_link.n + ' listener photos/videos</b>' : '<b style="color:#FFB547">● ' + esc((d.guard && d.guard.media_link && d.guard.media_link.error) || 'not checked yet') + '</b>'}</div>
      <div class="r"><span>Hype Live alarm link</span>${d.guard && d.guard.heartbeat && d.guard.heartbeat.ok ? '<b class="ok">● connected</b>' : '<b style="color:#FFB547">● ' + esc((d.guard && d.guard.heartbeat && d.guard.heartbeat.answer) || 'off') + '</b>'}</div>
      ${d.guard && (d.guard.events || []).filter(e => e.level !== 'ok').length ? '<div class="note">Last: ' + esc(d.guard.events.filter(e => e.level !== 'ok')[0].text) + '</div>' : ''}
      <h4>Server</h4>
      <div class="r"><span>Load</span><b>${sv.load ?? '–'} / ${sv.cpus ?? '–'} cores</b></div>
      <div class="r"><span>Free on /srv</span><b>${sv.disk_free_gb ?? '–'} GB</b></div>
      ${sv.reboot ? `<div class="r"><span>Reboot</span><b style="color:#FFB547">● needed since ${new Date(sv.reboot.since * 1000).toLocaleDateString('en-GB', {day: 'numeric', month: 'short'})} – planned monthly, together</b></div>` : ''}
      <div class="note">The public radio still runs via Streamerr; once Hype Studio's own streams are live, all numbers come from here.</div>`;
  }
  let popT = null;
  // "Needs you" from Hype Live (messages / photos / voices / videos / forms waiting) as a badge on the Hype Live link
  (function needs() {
    const go = () => fetch('/api/hl_needs').then(r => r.json()).then(d => {
      const n = (d.needs && d.needs.total) || 0, fresh = Date.now() / 1000 - (d.t || 0) < 300;
      document.querySelectorAll('a[href="/panel/hypelive.html"]').forEach(a => {
        let b = a.querySelector('.hlneeds');
        if (!b) { b = document.createElement('b'); b.className = 'hlneeds'; b.style.cssText = 'margin-left:6px;background:#FF4D5E;color:#fff;border-radius:9px;padding:0 6px;font:800 10px/16px Inter,system-ui,sans-serif;display:inline-block;min-width:16px;text-align:center'; a.appendChild(b); }
        b.textContent = n; b.style.display = n > 0 && fresh ? 'inline-block' : 'none';   // (hidden alone loses against the inline display)
        const k = d.needs || {}; b.title = `Waiting in Hype Live: ${k.messages || 0} messages · ${k.photos || 0} photos · ${k.voices || 0} voices · ${k.videos || 0} videos · ${k.forms || 0} forms`;
      });
    }).catch(() => {});
    setTimeout(go, 1500); setInterval(() => { if (!document.hidden) go(); }, 30000);
  })();
  function popLoad() { fetch('/api/streams').then(r => r.json()).then(popRender).catch(() => { pop.innerHTML = '<div class="r"><span>Status</span><b class="bad">● no answer</b></div>'; }); }
  const box = h.querySelector('#hBox'); let TAB = 's';
  function boxTab(t) { TAB = t; h.querySelectorAll('[data-ht]').forEach(b => { b.classList.toggle('on', b.dataset.ht === t); b.setAttribute('aria-selected', b.dataset.ht === t); });
    pop.hidden = t !== 's'; lp.hidden = t !== 'l'; clearInterval(popT);
    if (t === 's') { popLoad(); popT = setInterval(popLoad, 5000); } else logSeenNow(); }
  function popOpen(o, t) { box.hidden = !o; dots.setAttribute('aria-expanded', o); clearInterval(popT); if (o) boxTab(t || TAB); }
  h.querySelectorAll('[data-ht]').forEach(b => b.onclick = e => { e.stopPropagation(); boxTab(b.dataset.ht); });
  // a click on the dots: the log when something new happened there, else the streams
  dots.onclick = e => { e.stopPropagation(); const t = e.target.closest('#stLog') ? 'l' : e.target.closest('#stRadio,#stTv') ? 's' : (h.querySelector('#stLog').dataset.n ? 'l' : 's');   // the 3rd dot opens the Log
    if (!box.hidden && TAB === t) return popOpen(false); popOpen(true, t); };
  document.addEventListener('click', e => { if (!box.hidden && !box.contains(e.target)) popOpen(false); });
  addEventListener('keydown', e => { if (e.key === 'Escape' && !box.hidden) { popOpen(false); dots.focus(); } });
  // ---- LOG: what Studio did (the guard's repairs + problems) and this page's messages – the dot is orange / red when
  //      something new happened since you last looked ----
  const lp = h.querySelector('#hLogPop'), LOG = []; let logSeen = 0;
  try { logSeen = +localStorage.getItem('hypeLogSeen') || 0; } catch (e) {}
  const LC = {bad: '#FF4D5E', alarm: '#FF4D5E', warn: '#FFB547', repair: '#FFB547', ok: '#3DDC97', info: '#05A9C6'};
  const lesc = t => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  function logDot() {
    const nw = LOG.filter(m => m.t > logSeen && m.src === 'guard'), now = LOG.some(m => m.now);
    const d = h.querySelector('#stLog'); d.className = 'chip ' + (now || nw.some(m => m.lvl === 'bad' || m.lvl === 'alarm') ? 'bad' : nw.some(m => m.lvl === 'repair' || m.lvl === 'warn') ? 'warn' : '');
    if (nw.length) { d.dataset.n = 1; d.textContent = nw.length > 99 ? '99+' : nw.length; } else { delete d.dataset.n; d.textContent = '● LOG'; }
    d.title = 'Log' + (now ? ' – a problem right now' : nw.length ? ' – ' + nw.length + ' new' : ' – all quiet');
  }
  function logRender() {
    if (lp.hidden) return;
    lp.innerHTML = (LOG.map(m => `<div class="m"><i style="background:${LC[m.lvl] || '#05A9C6'}"></i><div>${lesc(m.text)}<small>${new Date(m.t * 1000).toLocaleString('nl-NL', {weekday: 'short', hour: '2-digit', minute: '2-digit'})} · ${m.src === 'guard' ? 'Studio guard' : 'this page'}${m.now ? ' · going on now' : ''}</small></div></div>`).join('') || '<div class="note">Nothing yet.</div>') +
      '<div class="note">Repairs and problems from the Studio guard, plus warnings on this page.</div>';
  }
  function logLoad() {
    fetch('/api/guard').then(r => r.json()).then(g => {
      for (let i = LOG.length - 1; i >= 0; i--) if (LOG[i].src === 'guard') LOG.splice(i, 1);
      (g.events || []).forEach(e => LOG.push({t: e.t, text: e.text, lvl: e.level, src: 'guard'}));
      (g.problems || []).forEach(p => LOG.push({t: p.since, text: p.text, lvl: 'bad', src: 'guard', now: true}));
      LOG.sort((a, b) => b.t - a.t); logDot(); logRender();
    }).catch(() => {});
  }
  // this page's own messages (toasts) go into the log too
  const wrapToast = () => { if (typeof window.toast !== 'function' || window.toast._log) return; const t0 = window.toast;
    window.toast = function (t, err) { const r = t0.apply(this, arguments); if (!err) return r; LOG.unshift({t: Date.now() / 1000, text: String(t), lvl: err ? 'warn' : 'info', src: 'page'}); if (LOG.length > 120) LOG.length = 120; logRender(); return r; };
    window.toast._log = true; };
  setTimeout(wrapToast, 500); setTimeout(wrapToast, 3000);
  function logSeenNow() { logSeen = Date.now() / 1000; try { localStorage.setItem('hypeLogSeen', logSeen); } catch (e) {} logRender(); logDot(); logLoad(); }
  logLoad(); setInterval(logLoad, 30000);
  // radio listeners and TV viewers right now, in the radio and TV dots
  function audLoad() { fetch('/api/audience').then(r => r.json()).then(d => {
    const put = (id, n, what) => { const el = h.querySelector(id); if (n == null) return; el.dataset.n = 1; el.textContent = n; el.dataset.what = what; };
    put('#stRadio', d.radio && d.radio.current, 'listeners'); put('#stTv', d.tv && d.tv.current, 'viewers');
    h.querySelector('#stRadio').title = 'Radio: ' + (d.radio ? d.radio.current + ' listening now' : '–'); h.querySelector('#stTv').title = 'TV: ' + (d.tv ? d.tv.current + ' watching now' : '–');
  }).catch(() => {}); }
  audLoad(); setInterval(() => { if (!document.hidden) audLoad(); }, 5000);   // every 5 s (was 15), and at once when you come back to the tab
  document.addEventListener('visibilitychange', () => { if (!document.hidden) audLoad(); });
  // ---- REC: start at once, stop with a second click (so it never stops by accident) ----
  const rb = h.querySelector('#hRec'); let REC = null, recAsk = 0;
  const fmtT = s => { s = Math.max(0, Math.floor(s)); const hh = Math.floor(s / 3600), mm = Math.floor(s % 3600 / 60), ss = s % 60; return (hh ? hh + ':' + String(mm).padStart(2, '0') : mm) + ':' + String(ss).padStart(2, '0'); };
  function recPaint() {
    const on = !!(REC && REC.since); rb.classList.toggle('on', on); rb.classList.toggle('ask', !on && false);
    if (recAsk && Date.now() < recAsk) { rb.classList.add('ask'); rb.querySelector('b').textContent = 'STOP?'; }
    else { rb.classList.remove('ask'); rb.querySelector('b').textContent = on ? '' : 'REC'; recAsk = 0; }   // recording: the red button with only its time (keeps the header the same width)
    h.querySelector('#hRecT').textContent = on ? fmtT(Date.now() / 1000 - REC.since) : '';
    rb.title = on ? 'Recording since ' + new Date(REC.since * 1000).toTimeString().slice(0, 5) + ' – click twice to stop' : 'Record the TV output (Library → Recordings)';
  }
  function recLoad() { fetch('/api/live').then(r => r.json()).then(d => { REC = d.rec; LVH = d; recPaint(); livePaint(); }).catch(() => {}); }
  // ---- GO LIVE / END LIVE: always a second click (going live or ending by accident would be worse than one extra click) ----
  const lb = h.querySelector('#hLive'); let LVH = null, liveAsk = 0;
  const hTip = t => { if (typeof window.toast === 'function') return window.toast(t, 1);      // the page's own message, else a small one here
    let d = document.getElementById('hTipBox'); if (!d) { d = document.createElement('div'); d.id = 'hTipBox'; d.setAttribute('role', 'status');
      d.style.cssText = 'position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#0F2B44;border:1px solid #23496B;color:#F2F6FA;padding:10px 16px;border-radius:12px;font:600 13px Inter,system-ui,sans-serif;z-index:99'; document.body.appendChild(d); }
    d.textContent = t; d.hidden = false; clearTimeout(hTip.t); hTip.t = setTimeout(() => { d.hidden = true; }, 3500); };
  function livePaint() {
    const on = !!(LVH && LVH.live), ask = liveAsk && Date.now() < liveAsk; if (!ask) liveAsk = 0;
    window.HYPE_LIVE = on; document.body.classList.toggle('onlive', on);      // (cue.js: CUE buttons + previews on the cue output)
    lb.classList.toggle('on', on); lb.classList.toggle('ask', !!ask); lb.classList.toggle('off', !on && !(LVH && LVH.studio_online));
    lb.querySelector('b').textContent = ask ? (on ? 'END LIVE?' : 'GO LIVE?') : (on ? 'LIVE' : 'GO LIVE');
    h.querySelector('#hLiveT').textContent = on && LVH.live_since && !ask ? fmtT(Date.now() / 1000 - LVH.live_since) : '';
    lb.title = on ? 'You are LIVE – click twice to END LIVE (back to the automation)' : (LVH && !LVH.studio_online ? 'The studio is not connected – connect it on Broadcast first' : 'GO LIVE: your studio mix goes on air (click twice)');
  }
  lb.onclick = () => {
    if (!(LVH && LVH.live) && !(LVH && LVH.studio_online)) { liveAsk = 0; hTip('Connect the studio first (Broadcast) – then you can go live'); return; }   // never live without the studio
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
  addEventListener('keydown', e => { if (e.altKey && !e.ctrlKey && e.key >= '1' && e.key <= '9') { e.preventDefault(); location.href = P[+e.key - 1][0]; } });
  setInterval(() => { const c = h.querySelector('#clock'); if (c) c.textContent = hNow(); }, 250);
  if (home) return;                                                    // On Air updates all of this itself
  const q = s => h.querySelector(s); let S = null, skew = 0;
  const api = (p, body) => fetch('/api/' + p, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
  let MODE = 'onair'; try { MODE = localStorage.getItem('hypeMode') || 'onair'; } catch (e) {}
  function paint() {
    if (!S) return;
    q('#' + A).classList.toggle('on', !!S.auto); q('#' + M).classList.toggle('on', !S.auto);
    q('#stRadio').className = 'chip ' + (S.stopped ? 'bad' : 'ok'); q('#stTv').className = 'chip ' + (S.scene ? 'ok' : 'bad');
    q('#stClock').textContent = '◷ ' + (S.clock || 'no hour plan');
    const m = S.dj && S.dj.on ? 'dj' : MODE;
    h.querySelectorAll('.modes [data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  }
  function load() { api('state').then(d => { S = d; skew = Date.now() / 1000 - d.server_time; try { localStorage.setItem('hypeSkew', skew); } catch (e) {} paint(); }).catch(() => { q('#stRadio').className = 'chip bad'; }); }
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
  q('#' + A).onclick = () => api('cmd/auto', {on: true}).then(load);
  q('#' + M).onclick = () => api('cmd/auto', {on: false}).then(load);
  h.querySelectorAll('.modes [data-mode]').forEach(b => b.onclick = () => {
    const m = b.dataset.mode, wasDj = S && S.dj && S.dj.on;
    try { localStorage.setItem('hypeMode', m); } catch (e) {}
    const go = () => { location.href = '/panel/'; };
    if (m === 'dj' && !wasDj) api('cmd/dj', {on: true}).then(go, go); else if (m !== 'dj' && wasDj) api('cmd/dj', {on: false}).then(go, go); else go();
  });
})();
