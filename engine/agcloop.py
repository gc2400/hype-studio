"""The AGC of the theme channels: the SAME code as Hype Radio's (engine.py intro_lift + agc_loop), one loop per channel on its
own radio engine (telnet port) – evens out the loudness BETWEEN songs, lifts quiet intros (look-ahead from the loudness map),
leaves the dynamics within a song alone. Settings → Sound → AGC of that channel sets its strength. Keep in step with engine.py."""
import socket, time

def intro_lift(now, st):
    """Look-ahead lift (from the song's loudness map): a quiet intro / breakdown is brought up to ~3 dB under the
    song's own level, and the lift is already gone when the beat drops (it looks 3 s ahead) - so no limiter hit."""
    lm = now.get("lmap"); lufs = now.get("lufs")
    if not lm or lufs is None or now.get("section") != "music": return 0.0
    t = (now.get("cue_start") or 0) + time.time() - (now.get("started") or 0)
    cm = now.get("cue_mix") or now.get("cue_end")
    if cm and t > cm - 1: return 0.0                              # the fade-out at the end stays a fade-out
    i = int(t * 2); top = min(8.0, 9.0 * st); ref = lufs - 3.0
    ahead = lm[max(0, i - 1): i + 10]                              # now ... 4.5 s ahead
    if not ahead or max(ahead[:3]) < -50: return 0.0               # silence / near-silence: never pull up noise
    # glide: the lift may be at most 2.5 dB higher per second still to go before a loud part (ends 0.5 s before it)
    want = top
    for j, v in enumerate(ahead):
        want = min(want, max(0.0, ref - v) + 2.5 * max(0.0, (j - 2) * 0.5))
    return max(0.0, want)
def agc_loop(port, AGC, get_now, alive=lambda: True):
    """Gated AGC on the music (broadcast style): evens out differences BETWEEN songs, leaves the dynamics WITHIN a song alone.
    - measures after the EQ (feedback), corrects for its own gain;
    - song-start hold: the first 15 s of a song (intro) never move it, only a real overshoot is caught;
    - gate: when the music falls 6+ dB under the song's body level (quiet intro, breakdown) it freezes;
    - slow up (~20 s), quick down (~1.5 s), fast catch (~0.6 s) when 3+ dB over target;
    - max boost +6 dB; holds under -40 dB; glides back to 0 dB when switched off."""
    TARGET, DT = -14.5, 0.2          # about the average level of a normalised song after the EQ: a normal song gets ~0 dB
    HOLD_S, GATE_DB = 15.0, 6.0
    avg = None                        # the level without our gain, smoothed over ~3 s (so single beats do not move it)
    body = None                       # the slow "body" level (~20 s) that the gate compares against
    sock = None; last_set = None; n = 0; lift_t = 0.0
    while alive():
        time.sleep(DT)
        E_now = get_now()
        try:
            if sock is None: sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            def ask(cmd):
                sock.sendall((cmd + "\n").encode()); buf = b""
                while b"END" not in buf:
                    b_ = sock.recv(512)
                    if not b_: raise OSError("closed")
                    buf += b_
                return buf.decode("utf-8", "replace").split("\r\n")[0].strip()
            if n % 25 == 0:
                try: AGC["strength"] = float(ask("var.get snd_agc").split()[-1])
                except Exception: pass
            n += 1
            v = ask("radio.level").split()
            if len(v) < 6: continue
            lvl = float(v[5])
            if lvl != lvl or AGC["db"] != AGC["db"]: AGC["db"] = 0.0; continue    # NaN from the radio: never pass it on as a gain
            AGC["level"] = lvl
            st = AGC["strength"]; up_max, down_max = min(6.0, 8.0 * st), -6.0 * st
            g = AGC["db"]; state = "ride"
            lift = AGC["lift"]
            lw = intro_lift(E_now, st) if (st > 0.01 and E_now) else 0.0
            age_ = time.time() - ((E_now or {}).get("started") or 0)
            # up max 2 dB/s, down at once (the look-ahead already started it early); at the very start of a song the lift
            # is there at once, so a quiet first second after a loud song doesn't sound like a gap
            lift = lw if (lw < lift or age_ < 1.5) else min(lw, lift + 2.0 * DT)
            if lift > 0.3: lift_t = time.time()
            if lift > 0.3 or time.time() - lift_t < 4.0:              # lifting (or just done): the song AGC keeps still, the 3 s meter still has the lift in it
                state = "lift" if lift > 0.3 else "hold"
                if lvl > TARGET + 4.0: lift = max(0.0, lift - 1.0)     # safety: still too loud -> lift away fast
            elif st <= 0.01: g += (0.0 - g) * DT / 1.5; state = "off"       # off: back to 0 dB
            elif lvl > -40.0 and E_now:
                raw = lvl - g                                             # the level without our own gain
                age = time.time() - (E_now.get("started") or 0)
                if age >= HOLD_S: body = raw if body is None else body + (raw - body) * DT / 20.0   # intros never shape the body
                if lvl > TARGET + 3.0:                                    # real overshoot: catch it fast, always
                    want = max(down_max, min(up_max, g - (lvl - TARGET)))
                    g += (want - g) * min(1.0, DT / 0.6); state = "catch"
                elif age < HOLD_S: state = "hold"                         # intro: judge the song by its body, not its intro
                elif body is not None and raw < body - GATE_DB: state = "gate"                 # quiet part: freeze
                else:
                    avg = raw if avg is None else avg + (raw - avg) * DT / 3.0
                    want = max(down_max, min(up_max, TARGET - avg))
                    tau = 20.0 if want > g else 1.5
                    g += (want - g) * min(1.0, DT / tau)
            else: state = "hold"
            AGC["state"] = state
            AGC["db"] = g; AGC["lift"] = lift; AGC["t"] = time.time()
            tot = max(-10.0, min(9.5, g + lift))
            if last_set is None or abs(tot - last_set) > 0.05:
                ask(f"var.set agc_g = {10 ** (tot / 20):.4f}"); last_set = tot
        except Exception as ex:
            try: sock.close()
            except Exception: pass
            sock = None; time.sleep(2)

