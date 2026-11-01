/* Hype Studio – tiny WebRTC helper for the studio and guest pages.
   Sends (WHIP) and receives (WHEP) streams through MediaMTX on our own server: /live/<path>/whip|whep */
const HypeRTC = (() => {
  const ICE = [{urls: 'stun:stun.l.google.com:19302'}];
  function waitIce(pc, ms = 2000) {
    return new Promise(res => {
      if (pc.iceGatheringState === 'complete') return res();
      const t = setTimeout(res, ms);
      pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') { clearTimeout(t); res(); } });
    });
  }
  function opusSdp(sdp, kbps, st = true) {   // Opus quality: stereo + bitrate (studio = music quality, guests = clear voice)
    return sdp.replace(/a=fmtp:(\d+) ([^\r\n]*useinbandfec=1[^\r\n]*)/g, (m, pt, rest) => {
      rest = rest.split(';').filter(x => !/^(stereo|sprop-stereo|maxaveragebitrate|maxplaybackrate)=/.test(x)).join(';');
      return `a=fmtp:${pt} ${rest};${st ? 'stereo=1;sprop-stereo=1;' : ''}maxaveragebitrate=${kbps * 1000};maxplaybackrate=48000`; });
  }
  const stereo = (sdp, kbps) => opusSdp(sdp, kbps, true);
  function preferH264(tr) {
    try {
      const caps = RTCRtpSender.getCapabilities('video'); if (!caps || !tr.setCodecPreferences) return;
      const h = caps.codecs.filter(c => /h264/i.test(c.mimeType)), o = caps.codecs.filter(c => !/h264/i.test(c.mimeType));
      if (h.length) tr.setCodecPreferences([...h, ...o]);
    } catch (e) {}
  }
  async function exchange(url, pc, fixAnswer) {
    await waitIce(pc);
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/sdp'}, body: pc.localDescription.sdp});
    if (!r.ok) throw new Error(r.status === 401 || r.status === 403 ? 'not allowed (link or key not valid)' : 'server said ' + r.status);
    let sdp = await r.text(); if (fixAnswer) sdp = fixAnswer(sdp);    // the browser encodes what the ANSWER allows: stereo + bitrate must be in there
    await pc.setRemoteDescription({type: 'answer', sdp});
  }
  /* send a camera and/or microphone: publish('cam1', stream, {key, videoKbps: 4000, audioKbps: 256}) */
  async function publish(path, stream, o = {}) {
    const pc = new RTCPeerConnection({iceServers: ICE});
    try {      // a failed handshake must close it: an unclosed connection is never freed (Chrome stops at ~500)
      stream.getTracks().forEach(t => { const tr = pc.addTransceiver(t, {direction: 'sendonly', streams: [stream]}); if (t.kind === 'video') preferH264(tr); });
      const offer = await pc.createOffer();
      const st = o.stereo !== false;
      if (o.audioKbps) offer.sdp = opusSdp(offer.sdp, o.audioKbps, st);
      await pc.setLocalDescription(offer);
      await exchange(`/live/${path}/whip?key=${encodeURIComponent(o.key || '')}`, pc, o.audioKbps ? (a => opusSdp(a, o.audioKbps, st)) : null);
    } catch (e) { try { pc.close(); } catch (_) {} throw e; }
    // video adapts to the connection: on a weak line the browser lowers the resolution first and keeps the motion smooth
    pc.getSenders().forEach(s => { if (s.track && s.track.kind === 'video') { const p = s.getParameters(); p.encodings = p.encodings && p.encodings.length ? p.encodings : [{}];
      if (o.videoKbps) p.encodings[0].maxBitrate = o.videoKbps * 1000; p.degradationPreference = 'maintain-framerate'; p.encodings[0].priority = 'medium'; s.setParameters(p).catch(() => {}); }
      if (s.track && s.track.kind === 'audio') { const p = s.getParameters(); p.encodings = p.encodings && p.encodings.length ? p.encodings : [{}]; p.encodings[0].priority = 'high'; p.encodings[0].networkPriority = 'high';
        if (o.audioKbps) p.encodings[0].maxBitrate = o.audioKbps * 1000; s.setParameters(p).catch(() => {}); } });
    stream.getVideoTracks().forEach(t => { try { t.contentHint = 'motion'; } catch (e) {} });
    return pc;
  }
  /* receive: play('return', {key, video: false}) -> {pc, stream} */
  async function play(path, o = {}) {
    const pc = new RTCPeerConnection({iceServers: ICE}), stream = new MediaStream();
    pc.addTransceiver('audio', {direction: 'recvonly'}); if (o.video) pc.addTransceiver('video', {direction: 'recvonly'});
    pc.ontrack = e => {
      stream.addTrack(e.track);
      // music quality: a bigger playout buffer, so the browser never has to stretch/squeeze the sound to catch up
      // (that is what sounds "under water"); a few hundred ms extra delay does not matter for the return feed
      if (e.track.kind === 'audio' && e.receiver) { const ms = o.bufferMs ?? 300;
        try { if ('jitterBufferTarget' in e.receiver) e.receiver.jitterBufferTarget = ms; else e.receiver.playoutDelayHint = ms / 1000; } catch (_) {} }
      // a video that should play smoothly through small network bumps (the panel preview): a slightly bigger picture buffer
      if (e.track.kind === 'video' && e.receiver && o.videoBufferMs) { try { if ('jitterBufferTarget' in e.receiver) e.receiver.jitterBufferTarget = o.videoBufferMs; else e.receiver.playoutDelayHint = o.videoBufferMs / 1000; } catch (_) {} }
    };
    try {      // a failed handshake must close it (see publish)
      const offer = await pc.createOffer();
      offer.sdp = opusSdp(offer.sdp, 256, true);        // receive in STEREO (without stereo=1 Chrome folds Opus down to mono)
      await pc.setLocalDescription(offer);
      await exchange(`/live/${path}/whep?key=${encodeURIComponent(o.key || '')}`, pc);
    } catch (e) { try { pc.close(); } catch (_) {} throw e; }
    return {pc, stream};
  }
  /* keep a connection alive: run(fn) again when it drops. start(alive) may check alive() after its own awaits */
  function keep(name, start, onState) {
    let pc = null, stop = false, timer = null;
    const alive = () => !stop;
    const go = async () => {
      if (stop) return;
      try {
        onState && onState(name, 'connecting'); const r = await start(alive), me = r.pc || r;
        if (stop) { try { me.close(); } catch (e) {} return; }      // closed while it was connecting: never keep this one
        pc = me;
        me.onconnectionstatechange = () => {
          if (pc !== me || stop) return;                            // an old connection: not ours any more
          onState && onState(name, me.connectionState);
          // "disconnected" often heals by itself within seconds (a short network hiccup): only rebuild when it really failed
          const dead = () => ['failed', 'disconnected', 'closed'].includes(me.connectionState);
          if (dead()) { clearTimeout(timer); timer = setTimeout(() => { if (pc !== me || !dead() || stop) return; try { me.close(); } catch (e) {} go(); }, me.connectionState === 'disconnected' ? 8000 : 1500); }
        };
      } catch (e) { if (stop) return; onState && onState(name, 'error: ' + e.message); clearTimeout(timer); timer = setTimeout(go, 4000); }
    };
    go();
    return {get pc() { return pc; }, close() { stop = true; clearTimeout(timer); try { pc && pc.close(); } catch (e) {} pc = null; onState && onState(name, 'off'); }};
  }
  /* level meter 0..1 for a stream -> {ac, stop} */
  function meter(stream, cb) {
    try {
      const ac = new (window.AudioContext || window.webkitAudioContext)(), src = ac.createMediaStreamSource(stream), an = ac.createAnalyser();
      an.fftSize = 1024; src.connect(an); const buf = new Float32Array(an.fftSize);
      let raf = 0, on = true;
      const tick = () => { if (!on) return; an.getFloatTimeDomainData(buf); let m = 0; for (const v of buf) m = Math.max(m, Math.abs(v)); cb(m); raf = requestAnimationFrame(tick); };
      tick(); return {ac, stop() { on = false; cancelAnimationFrame(raf); try { ac.close(); } catch (e) {} }};      // stop() frees it (one per stream)
    } catch (e) { return null; }
  }
  return {publish, play, keep, meter};
})();
