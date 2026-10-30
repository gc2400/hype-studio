# Hype Studio – radio engine (phase 2 · sound settings)
settings.init.allow_root.set(true)
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set("127.0.0.1")
settings.server.telnet.port.set(1234)
settings.log.file.set(true)
settings.log.file.path.set("/var/log/hype/liquidsoap.log")
settings.log.stdout.set(false)
settings.clock.log_delay.set(3600.)
settings.frame.audio.samplerate.set(48000)
settings.harbor.bind_addrs.set(["127.0.0.1"])

let json.parse (conf : {icecast_source : string}) = file.contents("/etc/hype/engine.json")
let json.parse (live : {harbor_pw : string}) = file.contents("/etc/hype/live.json")

# --- sound settings: changed live from the panel (Settings → Sound), kept in /var/lib/hype/sound.json ---
snd_drive    = interactive.float("snd_drive", min=0., max=10., description="Loudness (drive into the limiter, dB)", 5.)
snd_bass     = interactive.float("snd_bass", min=0., max=8., description="Bass warmth (dB)", 3.)
snd_bass_f   = interactive.float("snd_bass_f", min=50., max=160., description="Bass frequency (Hz)", 85.)
snd_treble   = interactive.float("snd_treble", min=-4., max=4., description="Treble (dB, minus = warmer)", -0.5)
snd_ratio    = interactive.float("snd_ratio", min=1., max=6., description="Compression ratio", 2.)
snd_thresh   = interactive.float("snd_thresh", min=-30., max=0., description="Compression threshold (dB)", -16.)
snd_attack   = interactive.float("snd_attack", min=1., max=100., description="Compression attack (ms)", 30.)
snd_release  = interactive.float("snd_release", min=50., max=1000., description="Compression release (ms)", 220.)
snd_ceiling  = interactive.float("snd_ceiling", min=-3., max=-0.1, description="Ceiling (dB)", -1.5)
snd_comp     = interactive.float("snd_comp", min=0., max=1., description="Compressor on (1) / off (0) – the limiter always stays on", 1.)
snd_monob    = interactive.float("snd_monob", min=0., max=1., description="Mono bass below 120 Hz on (1) / off (0)", 0.)
snd_mid      = interactive.float("snd_mid", min=-4., max=3., description="Upper mids 3.5 kHz (dB, minus = less harsh)", 0.)
# --- faders (panel): music, pads + sweepers, the studio while LIVE (1.0 = 0 dB) ---
vol_music  = interactive.float("vol_music", min=0., max=1.6, description="Music fader", 1.)
vol_pads   = interactive.float("vol_pads", min=0., max=1.6, description="Pads fader", 1.)
vol_studio = interactive.float("vol_studio", min=0., max=1.6, description="Studio fader", 1.)

# --- music: the engine pushes one song ahead into this queue ---
q = request.queue(id="q")
s = cue_cut(id="cue", cue_in_metadata="liq_cue_in", cue_out_metadata="liq_cue_out", on_cue_in=fun () -> (), on_cue_out=fun () -> (), q)
mix_out = interactive.float("mix_out", min=0.1, max=6., description="Mixing: fade-out of the old song (s)", 1.5)
mix_in = interactive.float("mix_in", min=0., max=3., description="Mixing: fade-in of the new song (s, 0 = full volume at once)", 0.)
cut_next = ref(false)   # "play now" in the panel/DJ decks: the next change is a quick cut instead of a mix
def db_of(m) =
  float_of_string(default=0., string.replace(pattern="[^0-9.-]", fun (_) -> "", m["liq_amplify"]))
end
def mix(a, b) =
  # a = the end of the old song (from its MIX point), b = the start of the new song.
  # The new song starts at its own full volume at once; the old one fades out under it over the whole overlap.
  # Two Liquidsoap 2.2 quirks handled here: the old tail arrives with the NEW song gain (liq_amplify) -> corrected,
  # and fade.out does nothing on the buffered tail -> our own ramp on the tail clock.
  sa = a.source
  comp = lin_of_dB(db_of(a.metadata) - db_of(b.metadata))
  d = if cut_next() then 0.25 else max(0.1, float_of_string(default=1.5, a.metadata["liq_cross_duration"])) end
  fin = if cut_next() then 0.05 else max(0.02, mix_in()) end
  # how long the old item fades: songs = the whole overlap; jingles = Settings (0 = plays out at full volume to its END)
  f = if cut_next() then 0.25 else float_of_string(default=d, a.metadata["hype_fade"]) end
  cut_next := false
  t0 = ref(-1.)
  def g() =
    t = time()
    if t0() < 0. then t0 := t end
    if f <= 0. then comp else comp * max(0., 1. - (t - t0()) / f) end
  end
  add(normalize=false, [fade.in(duration=fin, b.source), amplify(override=null(), g, sa)])
end
s = metadata.deduplicate(cross(duration=3., override_duration="liq_cross_duration", mix, s))   # cross() so OUR mix is used
duck_lvl = interactive.float("duck_lvl", min=0., max=1., description="music level under ducked pads / sweepers (the engine glides it)", 1.)
s = amplify(override="liq_amplify", 1., s)   # song gain AFTER the mix: the new song gain starts at the mix, so the old tail gets compensated in mix()
s = amplify(override=null(), {vol_music() * duck_lvl()}, s)   # override=null: do NOT apply the song gain (liq_amplify) a second time

def notify(m) =
  if m["hype_path"] != "" then
    data = json.stringify({hype_path = m["hype_path"], hype_id = m["hype_id"], artist = m["artist"], title = m["title"]})
    thread.run(fun () -> ignore(http.post(data=data, headers=[("Content-Type","application/json")], "http://127.0.0.1:8090/track")))
  end
end
s = source.on_metadata(s, notify)

# --- over: sweepers over a song intro / ducked pads – the music dips while they play ---
duck = ref(0.35)
overq = request.queue(id="over")
over = amplify(vol_pads, drop_metadata(amplify(override="liq_amplify", 1., overq)))
s = smooth_add(duration=0.4, p={duck()}, normal=s, special=over)   # (no longer used for ducking: the engine plays ducked pads/sweepers on the pads queue and dips duck_lvl)

# --- pads: jingles / FX played over the music without ducking ---
padq = request.queue(id="pads")
pads = amplify(vol_pads, drop_metadata(amplify(override="liq_amplify", 1., padq)))
s = add(normalize=false, [s, pads])

# --- broadcast processing: warm bass, light compression, loud but clean limiter ---
# Sound presets on ONE chain (switching chains is not possible here): snd_punch 0 = classic (the sliders), 1 = "Hype Punch":
# no rumble, punch at 100 Hz instead of deep sub, vocal presence + air, 3-band compression (the bass can't push the vocals down);
# the Loudness (drive) and Ceiling sliders work in both.
snd_punch = interactive.float("snd_punch", min=0., max=1., description="Sound chain: 0 = classic, 0.5 = classic multiband, 1 = Hype Punch", 0.)
# meters for Settings → Sound: how many dB the compressor and the limiter take off (on the automation chain)
dyn_pre = ref(fun () -> 0.)
dyn_mid = ref(fun () -> 0.)
dyn_post = ref(fun () -> 0.)
dyn_cg = ref(fun () -> 0.)
dyn_lg = ref(fun () -> 0.)
# 3-band compressor = Liquidsoap's compress.multiband, but its dry/wet mix no longer re-applies the song gain:
# the built-in one uses amplify() WITH the liq_amplify override, so every song's loudness correction was applied twice
def hype_multiband(~limit=true, ~wet=getter(1.), s, l)
  def band(low, band)
    high = if getter.is_constant(band.frequency) and getter.get(band.frequency) >= float_of_int(audio.samplerate()) / 2. then infinity else band.frequency end
    s = filter.iir.eq.low_high(low=low, high=high, s)
    s = compress(attack=band.attack, release=band.release, threshold=band.threshold, ratio=band.ratio, gain=band.gain, s)
    if limit then limiter(s) else s end
  end
  ls = list.mapi(fun (i, b) -> band(if i == 0 then 0. else list.nth(l,i-1).frequency end, b), l)
  c = add(normalize=false, ls)
  s = add(normalize=false, [amplify(override=null(), {1.-getter.get(wet)}, s), amplify(override=null(), wet, c)])
  if false then () else list.hd(l) end
  limiter(s)
end
def process(~meter=false, ~light=false, x) =
  p = fun () -> snd_punch() > 0.75     # 1 = Hype Punch
  x = filter.iir.eq.high(frequency=30., q=0.7, x)
  # mono bass: below 120 Hz left and right become the same (Linkwitz-Riley crossover) – off = the original signal, untouched
  # (only on the music + studio chains – the pads chain for the TV does not need it: saves CPU)
  x = if true then x else
    lo = filter.iir.eq.low(frequency=120., q=0.7071, filter.iir.eq.low(frequency=120., q=0.7071, mean(x)))   # the low end in mono (filtered as 1 channel = cheaper)
    hi = filter.iir.eq.high(frequency=120., q=0.7071, filter.iir.eq.high(frequency=120., q=0.7071, x))
    mbx = add(normalize=false, [audio_to_stereo(lo), hi])
    add(normalize=false, [amplify(override=null(), {if snd_monob() > 0.5 then 0. else 1. end}, x), amplify(override=null(), {if snd_monob() > 0.5 then 1. else 0. end}, mbx)])
  end
  x = filter.iir.eq.peak(frequency={if p() then 100. else snd_bass_f() end}, gain={if p() then 2. else snd_bass() end}, q=0.75, x)
  x = filter.iir.eq.peak(frequency=3500., gain={if p() then 1.5 else snd_mid() end}, q=0.9, x)
  x = filter.iir.eq.peak(frequency=9500., gain={if p() then 1. else snd_treble() end}, q=0.7, x)
  con = fun () -> snd_comp() > 0.5     # compressor switch (Settings → Sound)
  mb = fun () -> snd_punch() > 0.25      # 0.5 = the Classic sliders with multiband compression (bass, mids, highs each on their own)
  r1 = rms(duration=0.3, x)
  x = r1
  cb = compress(attack=snd_attack, release=snd_release, threshold=snd_thresh, ratio={if mb() or not con() then 1. else snd_ratio() end}, knee=6., x)
  x = cb
  x = hype_multiband(limit=false, wet={if mb() and con() then 1. else 0. end}, x, [
    {frequency=120., attack=25., release=220., ratio=3., threshold=-20., gain=4.},
    {frequency=4000., attack=12., release=160., ratio=2.5, threshold=-22., gain=5.},
    {frequency=20000., attack=5., release=100., ratio=2., threshold=-26., gain=5.}])
  # compressor off: about the same loudness as on (measured in the lab), so you compare the sound, not the volume
  x = amplify(override=null(), {if con() then 1. else if mb() then lin_of_dB(3.4) else lin_of_dB(-3.8) end end}, x)
  r2 = rms(duration=0.3, x)
  x = r2
  lm = limit(threshold={if p() then snd_ceiling() - 1. else snd_ceiling() end}, pre_gain=snd_drive, attack={if p() then 1. else 3. end}, release=120., x)
  x = lm   # punch is brighter: 1 dB more room so the MP3 never clips
  cs = compress(ratio=50., attack=0.5, release=60., knee=0., lookahead=4., threshold={snd_ceiling() - 0.3}, x)
  x = cs   # brick-wall safety stage (looks 4 ms ahead): no peak gets over the ceiling
  x = ladspa.fastlookaheadlimiter(input_gain=0., limit={snd_ceiling() - 0.2}, release_time=0.08, x)   # true look-ahead brickwall (swh-plugins): no peak ever goes over the ceiling
  r3 = rms(duration=0.3, x)
  if meter then
    dyn_pre := r1.rms
    dyn_mid := r2.rms
    dyn_post := r3.rms
    dyn_cg := cb.gain
    dyn_lg := fun () -> lm.gain() + cs.gain()
  end
  r3
end
prog = mksafe(process(meter=true, s))

# --- LIVE from the studio: the Mixcast mix comes in here (browser → MediaMTX → ffmpeg → harbor) ---
live_on = interactive.bool("live_on", false)
studio = input.harbor("studio", port=8005, password=live.harbor_pw, buffer=1.5, max=6.)
studio = amplify(vol_studio, studio)
studio = process(studio)   # the same sound + loudness as the music (was ~5 dB quieter)
# on air: the studio while LIVE is on and the studio is connected, else the automation (fades 0.6 s)
s = switch(track_sensitive=false, transitions=[fun (a, b) -> add(normalize=false, [fade.in(duration=0.6, b), fade.out(duration=0.6, a)]), fun (a, b) -> add(normalize=false, [fade.in(duration=0.6, b), fade.out(duration=0.6, a)])],
           [({live_on()}, studio), ({true}, prog)])
s = mksafe(s)
# level meter for the panel (what goes on air): peak per channel over the last 80 ms, read with "radio.level"
lu = lufs(window=3., s)
s = peak.stereo(id="meter", duration=0.08, lu)
def db_(v) = if v < 0.00003 then -90. else dB_of_lin(v) end end
def meter_level(_) =
  let (l, r) = s.peak()
  fa = dyn_pre()
  a = db_(fa())
  fb = dyn_mid()
  b = db_(fb())
  fc = dyn_post()
  c = db_(fc())
  quiet = a < -45.
  fcg = dyn_cg()
  flg = dyn_lg()
  # classic: the compressor's own gain reduction; multiband: estimated from the level before / after (+ its make-up gain)
  comp = if snd_comp() < 0.5 or quiet then 0. elsif snd_punch() > 0.25 then max(0., a - b + 4.5) else max(0., 0. - fcg()) end
  lim = if quiet then 0. else max(0., 0. - flg()) end
  "#{l} #{r} #{comp} #{lim} #{lu.lufs()}"
end
server.register(namespace="radio", "level", meter_level)
# what is really on air (for the Broadcast page): your studio mix or the automation
server.register(namespace="live", "source", fun (_) -> if live_on() and source.is_ready(studio) then "studio" else "automation" end)

# --- TV: pads + intro sweepers on their own (used when the TV plays a clip's own sound) ---
fx = mksafe(process(light=true, add(normalize=false, [over, pads])))

interactive.persistent("/var/lib/hype/sound.json")

# --- panel commands (telnet) ---
server.register(namespace="radio", "skip", fun (_) -> begin q.skip() "OK" end)
server.register(namespace="radio", "cut", fun (_) -> begin cut_next := true q.skip() "OK" end)
server.register(namespace="radio", "clear", fun (_) -> begin q.set_queue([]) "OK" end)
server.register(namespace="pads", "stop", fun (_) -> begin padq.set_queue([]) padq.skip() overq.set_queue([]) overq.skip() "OK" end)
server.register(namespace="over", "duck", fun (v) -> begin duck := float_of_string(v) "OK" end)

output.icecast(%mp3(bitrate=256, samplerate=44100, stereo=true), host="127.0.0.1", port=8000,
  password=conf.icecast_source, mount="test.mp3", name="Hype Radio TV - TEST", description="Hype Studio test", s)
output.pulseaudio(device="hype_air", clock_safe=false, s)
output.pulseaudio(device="hype_fx", clock_safe=false, fx)
# music + jingles for the studio (Mixcast USB fader) – always the automation, also while LIVE
output.pulseaudio(device="hype_return", clock_safe=false, prog)
