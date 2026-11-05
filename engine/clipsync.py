"""Lip sync per music video, measured: a few seconds after a clip starts, compare what is heard with what is seen in
OBS's own output (rtmp://127.0.0.1/live/program) and nudge the clip by the difference.

Why: every clip starts on a keyframe and OBS's media player needs a slightly different moment to get going each time,
so a clip can run 0.1–0.2 s early or late for the whole song (5 Nov: −180 … +70 ms between songs at one setting).

measure(song_file, tv_file, cue_start, started) -> (picture_vs_sound_ms, sound_match, picture_match) or None
  picture_vs_sound_ms: + = picture ahead of the sound, − = picture late (the sound comes first)
Sound and picture are both searched only near where the song should be (choruses repeat)."""
import subprocess, time, os
import numpy as np

SR = 8000
CAP = "/tmp/hype_clipsync.mkv"

def _pcm(f, ss, t):
    a = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % max(0, ss), "-i", f, "-t", "%.3f" % t,
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    return np.frombuffer(subprocess.run(a, capture_output=True, timeout=60).stdout, dtype=np.int16).astype(np.float32)

def _frames(f, ss, t, fps, w=96, h=54):
    a = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % max(0, ss), "-i", f, "-t", "%.3f" % t,
         "-vf", "fps=%s,scale=%d:%d,format=gray" % (fps, w, h), "-f", "rawvideo", "-"]
    d = np.frombuffer(subprocess.run(a, capture_output=True, timeout=60).stdout, dtype=np.uint8)
    if not len(d): return np.zeros((0, 1, 1), np.float32)
    d = d.reshape(-1, h, w).astype(np.float32)[:, h // 5: h - h // 5, w // 5: w - w // 5]   # the middle only (no logo / bars)
    d = d - d.mean(axis=(1, 2), keepdims=True)
    return d / (d.std(axis=(1, 2), keepdims=True) + 1e-3)

def measure(song_file, tv_file, cue_start, started):
    t_cap = time.time()
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", "rtmp://127.0.0.1/live/program",
                        "-t", "6", "-c", "copy", CAP], capture_output=True, timeout=40)
    if r.returncode != 0 or not os.path.exists(CAP): return None
    try:
        E = cue_start + (t_cap - started) + 2.0                    # about where the song is at t = 2.0 in the recording
        seg = _pcm(CAP, 2.0, 3.0); seg = seg - seg.mean()
        w0 = max(0, E - 4.0); song = _pcm(song_file, w0, 11.0)
        if len(seg) < SR * 2 or len(song) < len(seg): return None
        cc = np.correlate(song, seg, mode="valid"); k = int(np.argmax(cc)); apos = w0 + k / SR
        am = float(cc[k] / (np.linalg.norm(seg) * np.linalg.norm(song[k:k + len(seg)]) + 1e-9))
        P = _frames(CAP, 2.0, 2.0, 25)
        T0 = max(0, apos - 1.5); T = _frames(tv_file, T0, 4.0, 100)
        if len(P) < 20 or len(T) < 200: return None
        best = None
        for o in np.arange(-1.0, 1.0001, 0.01):
            idx = [int(round((apos + i / 25 + o - T0) * 100)) for i in range(len(P))]
            if min(idx) < 0 or max(idx) >= len(T): continue
            v = float(np.mean([np.mean(P[i] * T[j]) for i, j in enumerate(idx)]))
            if best is None or v > best[0]: best = (v, o)
        if not best: return None
        return round(best[1] * 1000), am, best[0]
    finally:
        try: os.remove(CAP)
        except OSError: pass
