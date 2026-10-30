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
    stream.getTracks().forEach(t => { const tr = pc.addTransceiver(t, {direction: 'sendonly', streams: [stream]}); if (t.kind === 'video') preferH264(tr); });
    const offer = await pc.createOffer();
    const st = o.stereo !== false;
    if (o.audioKbps) offer.sdp = opusSdp(offer.sdp, o.audioKbps, st);
    await pc.setLocalDescription(offer);
    await exchange(`/live/${path}/whip?key=${encodeURIComponent(o.key || '')}`, pc, o.audioKbps ? (a => opusSdp(a, o.audioKbps, st)) : null);
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
    pc.ontrack = e => stream.addTrack(e.track);
    await pc.setLocalDescription(await pc.createOffer());
    await exchange(`/live/${path}/whep?key=${encodeURIComponent(o.key || '')}`, pc);
    return {pc, stream};
  }
  /* keep a connection alive: run(fn) again when it drops */
  function keep(name, start, onState) {
    let pc = null, stop = false, timer = null;
    const go = async () => {
      if (stop) return;
      try {
        onState && onState(name, 'connecting'); const r = await start(); pc = r.pc || r;
        pc.onconnectionstatechange = () => {
          onState && onState(name, pc.connectionState);
          // "disconnected" often heals by itself within seconds (a short network hiccup): only rebuild when it really failed
          const dead = () => ['failed', 'disconnected', 'closed'].includes(pc.connectionState);
          if (dead() && !stop) { clearTimeout(timer); timer = setTimeout(() => { if (!dead() || stop) return; try { pc.close(); } catch (e) {} go(); }, pc.connectionState === 'disconnected' ? 8000 : 1500); }
        };
      } catch (e) { onState && onState(name, 'error: ' + e.message); clearTimeout(timer); timer = setTimeout(go, 4000); }
    };
    go();
    return {get pc() { return pc; }, close() { stop = true; clearTimeout(timer); try { pc && pc.close(); } catch (e) {} onState && onState(name, 'off'); }};
  }
  /* level meter 0..1 for a stream */
  function meter(stream, cb) {
    try {
      const ac = new (window.AudioContext || window.webkitAudioContext)(), src = ac.createMediaStreamSource(stream), an = ac.createAnalyser();
      an.fftSize = 1024; src.connect(an); const buf = new Float32Array(an.fftSize);
      const tick = () => { an.getFloatTimeDomainData(buf); let m = 0; for (const v of buf) m = Math.max(m, Math.abs(v)); cb(m); requestAnimationFrame(tick); };
      tick(); return ac;
    } catch (e) { return null; }
  }
  return {publish, play, keep, meter};
})();
