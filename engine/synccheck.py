"""Daily sync check – installed to /opt/hype by the updater, run by hype-synccheck.timer (every night).
During the next music videos (mode clip: the TV shows the video, the sound is the radio) it measures the TV program
(OBS output): is the picture in step with the sound? It needs 3 good measurements (max. 60 min), writes
/var/lib/hype/synccheck.json, and the guard raises "TV picture out of sync" in Hype Live when the median is off by more
than 120 ms. It only watches: nothing on air changes.
"""
import subprocess, json, sqlite3, time, urllib.request, os, statistics
import numpy as np

OUT = "/var/lib/hype/synccheck.json"; TMP = "/tmp/synccheck_prog.mkv"; SR = 8000
LIMIT_MS = 120

def now():
    return json.load(urllib.request.urlopen("http://127.0.0.1:8090/state", timeout=5))["now"]

def pcm(f, ss, t):
    a = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % ss, "-i", f, "-t", str(t), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    return np.frombuffer(subprocess.run(a, capture_output=True, timeout=60).stdout, dtype=np.int16).astype(np.float32)

def frames(f, ss, t, fps, w=96, h=54):
    a = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % ss, "-i", f, "-t", str(t), "-vf", "fps=%s,scale=%d:%d,format=gray" % (fps, w, h), "-f", "rawvideo", "-"]
    d = np.frombuffer(subprocess.run(a, capture_output=True, timeout=60).stdout, dtype=np.uint8)
    d = d[:len(d) // (w * h) * w * h].reshape(-1, h, w).astype(np.float32)
    d = d[:, h // 5: h - h // 5, w // 5: w - w // 5]; d = d - d.mean(axis=(1, 2), keepdims=True)
    return d / (d.std(axis=(1, 2), keepdims=True) + 1e-3)

def measure():
    """One measurement on the music video playing now -> dict, or None (not a music video / not sure)."""
    n = now()
    if n.get("mode") != "clip" or not n.get("path"): return None
    if time.time() - n.get("started", 0) < 20: return None               # not in the first 20 s (the clip is still settling)
    c = sqlite3.connect("file:/srv/hype-data/library.db?mode=ro", uri=True)
    r = c.execute("SELECT path, tv_path, COALESCE(cue_start, 0) FROM items WHERE path=?", (n["path"],)).fetchone()
    if not r or not r[1] or not os.path.exists(r[1]): return None
    path, tv, cue = r
    t_cap = time.time()
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", "rtmp://127.0.0.1/live/program", "-t", "7", "-c", "copy", TMP], timeout=40)
    if now().get("path") != n["path"]: return None
    E = cue + (t_cap - n["started"]) + 2.0
    seg = pcm(TMP, 2.0, 3.0); seg = seg - seg.mean()
    w0 = max(0, E - 4.0); song = pcm("/srv/hype/" + path, w0, 11.0)
    if len(seg) < SR * 2 or len(song) < len(seg): return None
    cc = np.correlate(song, seg, mode="valid"); k = int(np.argmax(cc)); apos = w0 + k / SR
    ac = float(cc[k] / (np.linalg.norm(seg) * np.linalg.norm(song[k:k + len(seg)]) + 1e-9))
    P = frames(TMP, 2.0, 2.0, 25)
    T0 = max(0, apos - 1.5); T = frames(tv, T0, 4.0, 100)
    if not len(P) or not len(T): return None
    best = None
    for o in np.arange(-1.0, 1.0001, 0.01):
        idx = [int(round((apos + i / 25 + o - T0) * 100)) for i in range(len(P))]
        if min(idx) < 0 or max(idx) >= len(T): continue
        v = float(np.mean([np.mean(P[i] * T[j]) for i, j in enumerate(idx)]))
        if best is None or v > best[0]: best = (v, o)
    if not best: return None
    res = {"t": time.time(), "song": "%s – %s" % (n.get("artist", ""), n.get("title", "")), "ms": round(best[1] * 1000), "sound": round(ac, 2), "picture": round(best[0], 2)}
    return res if ac >= 0.5 and best[0] >= 0.5 else dict(res, unsure=True)     # a still / dark scene: not sure -> not counted

def main():
    good, all_ = [], []; done_paths = set(); t_end = time.time() + 3600
    while len(good) < 3 and time.time() < t_end:
        try:
            n = now()
            if n.get("path") in done_paths or n.get("mode") != "clip": time.sleep(10); continue
            m = measure()
            if m:
                all_.append(m); done_paths.add(n.get("path"))
                if not m.get("unsure"): good.append(m["ms"])
            else: time.sleep(10)
        except Exception as e:
            all_.append({"t": time.time(), "error": str(e)[:120]}); time.sleep(20)
    med = statistics.median(good) if good else None
    d = {"t": time.time(), "median_ms": med, "good": len(good), "ok": None if med is None else abs(med) <= LIMIT_MS,
         "limit_ms": LIMIT_MS, "measurements": all_}
    tmp = OUT + ".tmp"; json.dump(d, open(tmp, "w")); os.replace(tmp, OUT)
    try: os.remove(TMP)
    except OSError: pass
    print("sync check:", "no measurement" if med is None else "%+d ms (%d songs) – %s" % (med, len(good), "OK" if d["ok"] else "OUT OF SYNC"))

if __name__ == "__main__":
    main()
