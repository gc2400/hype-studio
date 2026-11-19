/* Hype Studio – the web browser view (used by the panel's browser window and by browser.html in its own tab / window).
   The real browser runs on the server (engine/hypeweb.py); this shows its picture (WebRTC, MediaMTX path "web") and sends
   clicks, scrolling and typing to it. Needs: api(), cmd(), toast(), and getState() -> the engine state (media / web). */
const HB_CSS = `
.hb .wbar{display:flex;gap:6px;align-items:center;margin-bottom:6px}
.hb .wbar button{width:34px;height:34px;border-radius:9px;border:1px solid var(--line2);background:var(--deep);color:var(--soft);cursor:pointer;font-weight:800;flex:none}
.hb .wbar button:hover{color:var(--b);border-color:var(--b)}
.hb .wair,.hb .wsnd{width:auto!important;padding:0 12px}.hb .wair{color:#fff!important;background:#3A0A14!important;border-color:#FF4D6A!important}.hb .wair.on{background:#FF4D6A!important;color:#1A0207!important}
.hb .wurl{flex:1;min-width:0;height:34px;border-radius:9px;border:1px solid var(--line2);background:#071A2B;color:var(--txt);padding:0 10px;font:inherit;font-size:13px}
.hb .wtabs{display:flex;gap:4px;margin-bottom:6px;overflow-x:auto}
.hb .wtab{display:inline-flex;align-items:center;gap:6px;max-width:200px;height:28px;padding:0 6px 0 10px;border-radius:8px;border:1px solid var(--line2);background:var(--deep);color:var(--soft);font-size:12px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:none}
.hb .wtab.on{border-color:var(--b);color:var(--txt)}.hb .wtab b{font-weight:800;color:var(--mut);padding:0 4px}.hb .wtab b:hover{color:#FF8A97}.hb .wnew{padding:0 10px}
.hb .wview{position:relative;aspect-ratio:16/9;background:#000;border-radius:9px;overflow:hidden}
.hb video{position:absolute;inset:0;width:100%;height:100%;object-fit:fill}
.hb .whit{position:absolute;inset:0;outline:none}.hb .whit:focus{box-shadow:inset 0 0 0 2px var(--b)}
.hb .wmsg{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);color:var(--mut);font-size:13px;pointer-events:none}
.hb .wnote{margin-top:6px;font-size:11px;color:var(--mut)}`;
function HypeBrowser(root, o) {
  if (!document.getElementById('hbcss')) { const st = document.createElement('style'); st.id = 'hbcss'; st.textContent = HB_CSS; document.head.appendChild(st); }
  root.classList.add('hb');
  root.innerHTML = `
  <div class="wbar">
    <button data-w="back" title="Back">←</button><button data-w="fwd" title="Forward">→</button><button data-w="reload" title="Reload">↻</button>
    <input class="wurl" placeholder="Type an address or search…" autocomplete="off" spellcheck="false">
    <button data-w="air" class="wair" title="On air / off air (the TV Media scene + sound choice)">On air</button>
    <button data-w="snd" class="wsnd" title="Hear the browser here (only on this screen)">Sound off</button>
    ${o.popout ? '<button data-w="pop" title="Open in its own window">⧉</button>' : ''}
    <button data-w="off" title="Stop the browser (it also stops by itself after 30 min without use)">⏻</button>
    ${o.close ? '<button data-w="close" title="Close this window (the browser keeps running)">✕</button>' : ''}
  </div>
  <div class="wtabs"></div>
  <div class="wview"><video muted autoplay playsinline></video><div class="whit" tabindex="0" title="Click, scroll and type here – it goes to the browser"></div><div class="wmsg">Starting the browser…</div></div>
  <div class="wnote">Only pages you trust · sign in yourself (Studio never types passwords) · on air: your own content or with permission</div>`;
  const q = s => root.querySelector(s), url = q('.wurl'), hit = q('.whit'), vid = q('video'), msg = q('.wmsg');
  let rtc = null, open = false;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  function tabsRender(T) {
    q('.wtabs').innerHTML = (T || []).map(t => `<span class="wtab ${t.on ? 'on' : ''}" data-tab="${t.id}" title="${esc(t.url)}">${esc(t.title || 'New tab')}<b data-tclose="${t.id}" title="Close tab">×</b></span>`).join('') + '<span class="wtab wnew" data-tnew title="New tab">+</span>';
  }
  function web(a, d) {
    return o.cmd('web_' + a, d || {}).then(r => {
      if (r && r.ok === false) { o.toast(r.error, 1); return r; }
      if (r && r.web) { if (document.activeElement !== url) url.value = r.web.url || ''; tabsRender(r.web.tabs); }
      return r;
    });
  }
  function start() {
    open = true; msg.hidden = false; msg.textContent = 'Starting the browser…';
    return web('start').then(r => {
      if (r && r.ok === false) { msg.textContent = r.error || 'Could not start'; return; }
      if (!rtc) rtc = HypeRTC.keep('web', async alive => { const live = await o.api('live'); const r_ = await HypeRTC.play('web', {key: live.studio_key, video: true, videoBufferMs: 30}); if (!alive()) return r_; vid.srcObject = r_.stream; msg.hidden = true; return r_; },
        (n, st) => { if (/error|failed/.test(st)) { msg.hidden = false; msg.textContent = 'Connecting the picture…'; } });
      setTimeout(() => hit.focus(), 300);
    });
  }
  function stop() { open = false; if (rtc) { rtc.close(); rtc = null; } vid.srcObject = null; }
  const pos = e => { const r = hit.getBoundingClientRect(); return {x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height}; };
  let lastClick = 0;
  hit.addEventListener('click', e => { const p = pos(e), n = Date.now() - lastClick < 350 ? 2 : 1; lastClick = Date.now(); hit.focus(); web('click', Object.assign(p, {n})).then(() => setTimeout(() => web('info'), 900)); });
  let dy = 0, wt = null;
  hit.addEventListener('wheel', e => { e.preventDefault(); dy += e.deltaY; const p = pos(e); clearTimeout(wt); wt = setTimeout(() => { const v = dy; dy = 0; web('wheel', Object.assign(p, {dy: v})); }, 90); }, {passive: false});
  const SPECIAL = ['Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'];
  hit.addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey) { if (e.key === 'v') { navigator.clipboard && navigator.clipboard.readText().then(t => t && web('type', {text: t})).catch(() => {}); e.preventDefault(); } return; }
    if (e.key.length === 1 || SPECIAL.includes(e.key)) { e.preventDefault(); web('key', {key: e.key}); }
  });
  url.addEventListener('keydown', e => { if (e.key === 'Enter') { web('nav', {url: url.value}); url.blur(); hit.focus(); } });
  q('.wtabs').addEventListener('click', e => {
    const t = e.target;
    if (t.dataset.tclose) { e.stopPropagation(); return web('tabclose', {id: t.dataset.tclose}); }
    if (t.closest('[data-tnew]')) return web('tabnew').then(() => { url.value = ''; url.focus(); });
    const tb = t.closest('[data-tab]'); if (tb) web('tab', {id: tb.dataset.tab});
  });
  root.querySelectorAll('[data-w]').forEach(b => b.onclick = () => {
    const a = b.dataset.w;
    if (a === 'close') { stop(); return o.onClose && o.onClose(); }
    if (a === 'pop') { window.open('/panel/browser.html', 'hypebrowser', 'width=1280,height=860'); stop(); return o.onClose && o.onClose(); }
    if (a === 'off') { if (!confirm('Stop the browser? (if it is on air, it goes off air)')) return; return o.cmd('web_stop', {}).then(() => { stop(); o.toast('Browser stopped'); o.onClose && o.onClose(); }); }
    if (a === 'snd') { vid.muted = !vid.muted; b.textContent = vid.muted ? 'Sound off' : 'Sound on'; return; }
    if (a === 'air') { const S = o.getState(), cur = S && S.media;
      if (cur && cur.file === 'live:web') return o.cmd('media_stop', {}).then(() => o.toast('Browser off air'));
      const snd = o.sound() === 'off' ? 'off' : 'mute';
      if (o.sound() === 'duck') o.toast('Duck is not for the browser – it plays muted (choose Music off for its sound)');
      return o.cmd('stream_play', {id: 'web', scene: o.scene(), sound: snd}).then(r => r && r.ok === false ? o.toast(r.error, 1) : o.toast('Browser on air' + (snd === 'off' ? ' · its sound on radio + TV, the music waits' : ' · muted, the music goes on'))); }
    web(a).then(() => setTimeout(() => web('info'), 900));
  });
  setInterval(() => {
    if (!open) return; const S = o.getState(); if (!S) return;
    const on = S.media && S.media.file === 'live:web', b = q('.wair'); b.classList.toggle('on', !!on); b.textContent = on ? 'Off air' : 'On air';
    if (S.web && !S.web.on && !S.web.starting) { msg.hidden = false; msg.textContent = 'The browser is stopped – open it again'; }
  }, 1000);
  setInterval(() => { const S = o.getState(); if (open && !document.hidden && S && S.web && S.web.on) web('info'); }, 4000);      // address + tabs follow what the page does
  return {start, stop};
}
