"""Smart mix for the theme channels (hype-channels): the SAME code as Hype Radio's engine (engine.py: lm_info … smart_mix),
copied here so the channels mix songs exactly like Hype Radio does – from each file's loudness map: a soft intro rises under a
long outro, a hard start comes in when the old song has dropped ~12 dB, an abrupt end changes tight, jingles ring out.
Keep in step with engine.py when the smart mix there changes."""
import json, sqlite3, threading
LIB = "/srv/hype-data/library.db"
SMART_MIX = True     # per channel: Settings → Mixing → Smart mix (chan.py sets it)
MIX_MAX = 5.0        # the longest overlap (s)
_C = threading.local()
def dbc():                  # one read connection to the library per thread
    c = getattr(_C, "c", None)
    if c is None: c = _C.c = sqlite3.connect(LIB, timeout=30)
    return c

LM_CACHE = {}
LM_STAMP = {}
def lm_info(path):
    """(loudness map, LUFS, mix_set) of a file - cached; cleared when its cue points change, and read again when the
    file was indexed again (replaced by a new version: before, the old file's map stayed in memory until a restart)."""
    try: stamp = (dbc().execute("SELECT indexed_at FROM items WHERE path=?", (path,)).fetchone() or [None])[0]
    except Exception: stamp = None
    if path in LM_CACHE and LM_STAMP.get(path) == stamp: return LM_CACHE[path]
    LM_STAMP[path] = stamp; FINE_CACHE.pop(path, None)
    try:
        r = dbc().execute("SELECT lmap, lufs, COALESCE(mix_set, 0) FROM items WHERE path=?", (path,)).fetchone()
        v = ([int(x) for x in r[0].split(",")] if r and r[0] and r[0] != "-" else None, r[1] if r else None, bool(r and r[2]))
    except Exception: v = (None, None, False)
    if len(LM_CACHE) > 4000: LM_CACHE.clear()
    LM_CACHE[path] = v; FINE_CACHE.pop(path, None); return v
FINE_CACHE = {}
def fine_info(path):
    """The 0.1 s loudness map of the first and last seconds (see indexer.fine_map) as a lookup M(t), or None."""
    if path in FINE_CACHE: return FINE_CACHE[path]
    f = None
    try:
        r = dbc().execute("SELECT fine FROM items WHERE path=?", (path,)).fetchone()
        if r and r[0] and r[0] != "-":
            d = json.loads(r[0]); h, t0, t = d["h"], d["t0"], d["t"]
            def f(x, h=h, t0=t0, t=t):
                j = int(round((x - t0) * 10))
                if 0 <= j < len(t): return t[j]
                i = int(round(x * 10))
                return h[i] if 0 <= i < len(h) else None
    except Exception: f = None
    if len(FINE_CACHE) > 4000: FINE_CACHE.clear()
    FINE_CACHE[path] = f; return f
M_LAG = 0.35   # the momentary loudness looks back 400 ms: a sound that stops at T has dropped 6-12 dB at about T + 0.3-0.4 s
def fine_stop(f, frm, to, level):
    """The real moment the sound falls under `level` (dB) for good between frm and to, or None (0.1 s accurate)."""
    if not f: return None
    t = round(frm, 1); last_loud = None
    while t <= to + 0.45:
        v = f(t)
        if v is None: return None
        if v >= level: last_loud = t
        t = round(t + 0.1, 1)
    if last_loud is None: return None
    for k in range(1, 8):          # first frame after the last loud one that is under level - 6: the stop is ~0.35 s before it
        v = f(round(last_loud + k / 10, 1))
        if v is None or v < level - 6: return max(frm, last_loud + k / 10 - M_LAG)
    return max(frm, last_loud + 0.1)

def fine_first_below(f, frm, to, level):
    """The real moment the sound first falls under `level` after frm (0.1 s accurate), or None."""
    if not f: return None
    t = round(frm, 1)
    while t <= to + 0.45:
        v = f(t)
        if v is None: return None
        if v < level: return max(frm, t - M_LAG)
        t = round(t + 0.1, 1)
    return None

def intro_len(it):
    """Seconds from START until the song is at its full level (0 = starts hard)."""
    lm, L, _ = lm_info(it.get("path"))
    if not lm or L is None: return None
    cs = it.get("cue_start") or 0.0
    for i in range(int(cs * 2), len(lm)):
        if lm[i] >= L - 4: return max(0.0, i / 2 - cs)     # an intro 4+ dB under the song's level counts as soft
    return 0.0
def smart_mix(it, nxt=None):
    """MIX + overlap from the loudness map: (cm, cross, fade) or None (map missing / MIX set by hand / switched off).
    The next item never starts before START or after END. The old one may ring out after END only when END was not set
    by hand (CLOSE / automatic END = the last loud moment): then its fading tail plays on under the next item, up to the
    file's real audible end. An END you set by hand in Hype Studio is a hard stop.
    Songs: a soft intro of the next song rises under a long outro, a hard start comes in when the old song has dropped
    ~12 dB, an abrupt end changes tight (no gap). Jingles: the next song starts where the voice ends, the echo rings out."""
    if not SMART_MIX or not it or not it.get("path"): return None
    lm, L, mset = lm_info(it["path"])
    if not lm or L is None or mset: return None
    cs = it.get("cue_start") or 0.0; dur = min(it.get("duration") or len(lm) / 2, len(lm) / 2)
    end = min(it.get("cue_end") or dur, dur)                 # END: the next item starts by here at the latest
    a = int(cs * 2)
    tail_end = end
    if not it.get("cues_edited"):                            # automatic END only: the fading tail may ring past it (CLOSE / your END = the end)
        aud = next((i for i in range(len(lm) - 1, a - 1, -1) if lm[i] > -50), None)
        if aud is not None: tail_end = max(end, min((aud + 1) / 2, end + 10.0, dur))
    b = max(a + 1, int(tail_end * 2))
    if b - a < 4: return None
    jingle = it.get("section") in ("jingles", "ads")
    nl = intro_len(nxt) if nxt and nxt.get("section") == "music" else None
    hard_next = nxt is None or nxt.get("section") != "music" or nl is None or nl < 1.5
    if jingle:
        voice = next((i for i in range(b - 1, a - 1, -1) if lm[i] >= L - 8), None)
        tv = (voice + 1) / 2 if voice is not None else end
        fs = fine_stop(fine_info(it["path"]), max(cs, tv - 2.0), tv, L - 8)   # exact: where the voice really ends
        if fs is not None: tv = fs - 0.1
        cm = min(max(cs, tv), end, tail_end - 0.3); cross = max(0.3, min(8.0, tail_end - cm))
        return cm, cross, 0.0          # jingles are made with their own ending: no fade, the echo plays out under the song
    strong = next((i for i in range(b - 1, a - 1, -1) if lm[i] >= L - 6), None)
    ts = (strong + 1) / 2 if strong is not None else tail_end
    outro = tail_end - ts
    fn = fine_info(it["path"])
    loud_end = fine_stop(fn, max(cs, ts - 1.0), ts, L - 6)                    # exact end of the loud part (0.1 s)
    drop12 = fine_first_below(fn, max(cs, (loud_end or ts) - 0.3), min(tail_end, ts + 3.0), L - 12)  # exact moment it has dropped ~12 dB
    if outro < 1.0:                                      # ends abruptly: tight change, no gap
        # a softer intro next starts a little earlier, so it is already there under the last loud second (no hole)
        stop = loud_end + 0.15 if loud_end is not None else tail_end
        cm = stop - (0.25 if hard_next else min(2.0, max(0.8, nl)))
    elif not hard_next:                                  # soft intro next: let it rise under the outro
        cm = max(ts, tail_end - min(outro, 6.0, nl + 1.5))
    else:                                                # hard start next: when the old song has dropped ~12 dB
        if drop12 is not None: cm = min(drop12 - 0.1, tail_end - 0.5)
        else:
            drop = next((i for i in range(strong if strong is not None else a, b) if lm[i] < L - 12), None)
            cm = min(max(ts, (drop / 2 if drop is not None else tail_end) - 0.3), tail_end - 0.5)
    cm = max(cs, min(cm, end))
    cross = max(0.3, min(MIX_MAX, 4.0 if hard_next else 6.0, tail_end - cm))
    return cm, cross, cross
