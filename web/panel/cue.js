// CUE (pre-listen) for the panel and the library: while LIVE, anything you listen to here plays on the cue output chosen on
// Broadcast → Setup → Audio devices (your headphones) – never on the laptop's normal output, which may be the Mixcast (on air).
// Not LIVE: previews play as always, and the CUE buttons are hidden.
(() => {
  const dev = () => { try { return localStorage.getItem('hypeStudio.dCue') || ''; } catch (e) { return ''; } };
  const live = () => !!window.HYPE_LIVE;
  const say = t => { if (typeof window.toast === 'function') window.toast(t, 1); };
  const st = document.createElement('style');
  st.textContent = `body:not(.onlive) .cueb{display:none!important}.cueb{display:inline-flex}
.cueb{position:absolute;left:3px;top:3px;font:900 10px Inter,system-ui,sans-serif;letter-spacing:.04em;line-height:1;padding:4px 5px;border-radius:6px;background:rgba(0,0,0,.45);color:#FFE000;cursor:pointer;align-items:center}
.cueb.on{background:#FFE000;color:#061423}
button.cueb{position:static;border:1px solid #5A4E00;min-height:30px;padding:0 8px;background:var(--deep,#061423)}
#cueBar{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:70;display:flex;align-items:center;gap:10px;background:#1E1A00;border:1px solid #FFE000;color:#FFE000;border-radius:12px;padding:8px 12px;font:700 13px Inter,system-ui,sans-serif;max-width:min(560px,92vw)}
#cueBar .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#F2F6FA}#cueBar .p{font:700 12px ui-monospace,Menlo,Consolas,monospace}
#cueBar button{border:1px solid #FFE000;background:transparent;color:#FFE000;border-radius:8px;min-height:28px;padding:0 10px;font-weight:900;cursor:pointer}`;
  document.head.appendChild(st);
  let A = null, cur = null, end = 0, bar = null;
  const fmt = s => { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
  function paint() {
    document.querySelectorAll('.cueb').forEach(b => b.classList.toggle('on', !!cur && b.dataset.cueurl === cur));
    if (!A) { if (bar) bar.hidden = true; return; }
    if (!bar) { bar = document.createElement('div'); bar.id = 'cueBar'; bar.setAttribute('role', 'status');
      bar.innerHTML = '<b>CUE</b><span class="t"></span><span class="p"></span><button type="button">Stop</button>'; document.body.appendChild(bar);
      bar.querySelector('button').onclick = () => stop(); }
    bar.hidden = false; bar.querySelector('.t').textContent = A._title || ''; bar.querySelector('.p').textContent = fmt(A.currentTime) + ' / ' + fmt(end || A.duration);
  }
  function stop() { if (A) { try { A.pause(); A.removeAttribute('src'); A.load(); } catch (e) {} } A = null; cur = null; paint(); }
  async function play(url, title, start, stopAt) {
    if (cur === url) { stop(); return; }                       // the same CUE again = off
    if (!live()) return say('CUE works while you are LIVE');
    const d = dev(); if (!d) return say('Choose a Cue output first: Broadcast → Setup → Audio devices');
    stop(); const a = new Audio(); a._title = title || ''; a.preload = 'auto';
    try { await a.setSinkId(d); } catch (e) { return say('The cue output is not available on this computer'); }
    A = a; cur = url; end = stopAt || 0;
    a.addEventListener('loadedmetadata', () => { if (start) a.currentTime = start; }, {once: true});
    a.ontimeupdate = () => { if (A !== a) return; if (end && a.currentTime >= end) return stop(); paint(); };
    a.onended = () => { if (A === a) stop(); };
    a.onerror = () => { if (A === a) { stop(); say('This file cannot be played here'); } };
    a.src = url; a.play().catch(() => {}); paint();
  }
  // every preview on the page (library player, cue points, listener media …): while LIVE only on the cue output
  async function guard(el) {
    if (!el || el.muted || el.dataset.nocue !== undefined || !el.setSinkId) return;
    if (!live()) { if (el.sinkId) { try { await el.setSinkId(''); } catch (e) {} } return; }
    const d = dev();
    if (!d) { if (!el.paused) { el.pause(); say('LIVE: previews only play on the cue output – choose it on Broadcast → Setup'); } return; }
    if (el.sinkId !== d) { const was = !el.paused; if (was) el.pause(); try { await el.setSinkId(d); if (was) el.play().catch(() => {}); } catch (e) { say('The cue output is not available on this computer'); } }
  }
  document.addEventListener('play', e => guard(e.target), true);
  setInterval(() => {      // going LIVE (or ending it) while something plays: moved to the right output at once
    document.querySelectorAll('audio,video').forEach(el => { if (!el.muted) guard(el); });
    if (A && !live()) stop();
  }, 1000);
  window.HypeCue = {play, stop, url: p => '/media/' + String(p).split('/').map(encodeURIComponent).join('/'), playing: () => cur};
})();
