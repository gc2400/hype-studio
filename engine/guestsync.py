"""Guest lip sync, measured (not guessed).

A guest's picture goes guest -> server -> OBS. Their voice takes a longer road: guest -> server -> your laptop ->
Mixcast -> your laptop -> server ("studio") -> OBS. How much longer depends on your laptop, the Mixcast and both
connections, so we measure it: while the guest talks, we record the guest's own sound (as it arrives at the server,
together with their picture) and the studio mix at the same moment, and find how much later the guest's voice is in
the studio mix (GCC-PHAT cross-correlation: robust against the Mixcast's EQ, the music and your voice in the mix).

measure("guest1") -> (delay_ms, confidence) or (None, why)
"""
import subprocess, threading, time
import numpy as np

SR = 8000
FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp",
      "-use_wallclock_as_timestamps", "1", "-fflags", "nobuffer", "-probesize", "32768", "-analyzeduration", "0"]


def _grab(path, t0, dur, out, key):
    # every audio frame gets the time it arrived here; both recordings are cut to start at the same moment t0
    cmd = FF + ["-i", f"rtsp://127.0.0.1:8554/{path}", "-vn", "-ac", "1",
                "-af", f"asetpts=PTS-{t0:.6f}/TB,aresample={SR}:async=1000:first_pts=0",
                "-copyts", "-t", str(dur), "-f", "s16le", "-ar", str(SR), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=dur + 12)
        out[key] = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        out[key] = np.zeros(0, np.float32)


def record(a_path, b_path, dur=10.0):
    t0 = time.time() + 2.5          # both ffmpegs are connected and receiving by then
    out = {}
    th = [threading.Thread(target=_grab, args=(p, t0, dur, out, k)) for k, p in (("a", a_path), ("b", b_path))]
    for t in th: t.start()
    for t in th: t.join()
    return out.get("a"), out.get("b")


def delay(a, b, max_ms=900, min_ms=0):   # >= 0: music that leaks into a guest's mic (speakers) is EARLIER in the mix – never picked
    """How many ms later a's sound is in b. -> (ms, confidence)"""
    n = min(len(a), len(b))
    if n < SR * 4: return None, 0.0
    a, b = a[:n], b[:n]
    a = np.diff(a, prepend=0.0); b = np.diff(b, prepend=0.0)       # tilt towards speech (less rumble / bass)
    N = 1 << int(np.ceil(np.log2(2 * n)))
    A, B = np.fft.rfft(a, N), np.fft.rfft(b, N)
    X = B * np.conj(A); X /= np.abs(X) + 1e-12                      # PHAT: only the timing counts, not the tone
    f = np.fft.rfftfreq(N, 1 / SR); X[(f < 200) | (f > 3600)] = 0   # the voice band
    cc = np.fft.irfft(X, N)
    lo, hi = int(min_ms * SR / 1000), int(max_ms * SR / 1000)
    lags = np.arange(lo, hi + 1)
    vals = cc[lags % N]
    i = int(np.argmax(vals)); peak = vals[i]
    noise = np.median(np.abs(vals)) + 1e-12
    return float(lags[i] * 1000 / SR), float(peak / noise)


def level_db(x):
    return -90.0 if x is None or not len(x) else float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9))


def measure(slot, studio="studio", dur=10.0):
    a, b = record(slot, studio, dur)
    if a is None or len(a) < SR * 4 or b is None or len(b) < SR * 4: return None, "no sound from the guest or the studio"
    if level_db(a) < -45: return None, "the guest was quiet"
    ms, conf = delay(a, b)
    if ms is None or conf < 8: return None, "not sure (conf %.1f)" % conf
    return ms, conf


if __name__ == "__main__":
    import sys
    print(measure(sys.argv[1] if len(sys.argv) > 1 else "testa", sys.argv[2] if len(sys.argv) > 2 else "testb"))
