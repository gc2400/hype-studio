#!/usr/bin/env python3
"""Hype Studio playout engine (phase 2).
Keeps the upcoming plan (hour clock + your own picks), feeds Liquidsoap one song ahead,
switches the TV in OBS on every song start, and serves the control panel API."""
import shutil, math, os, re, json, time, random, sqlite3, socket, threading, logging, uuid, base64, hashlib, subprocess, array, urllib.request, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
os.environ["TZ"] = "Europe/Amsterdam"; time.tzset()   # all show times are Amsterdam time, whatever the server's zone
import obsws_python as obs
import sys, urllib.parse; sys.path.insert(0, os.environ.get("HYPE_LIB") or "/opt/hype"); import planner, live, tickers, audstats, slideshow, hypeweb, chancfg

ROOT = "/srv/hype"; DB = "/srv/hype-data/library.db"; WEB = "/var/www/studio"; WAVES = "/srv/hype-data/wave"
CONF = json.load(open("/etc/hype/engine.json"))
# ---- channel mode (hype-engine@<id>): this same engine plays a theme station (Hype Latina, Hype Ambiente …) instead of Hype Radio.
# Its own planning database (chan_<id>.db), radio engine (telnet port), API port and state files; no TV, studio, recordings or
# Hype Live / website pushes (Hype Radio's engine keeps doing those). Unset = Hype Radio, exactly as before.
CH = os.environ.get("HYPE_CH") or None
TB_SWEEPER = CH is None     # (Settings → Throwback sweeper; loaded with the other settings)
CHC = chancfg.get(CH) if CH else None
if CH and not CHC: raise SystemExit("unknown theme channel %r" % CH)
LIQ_PORT = int(os.environ.get("HYPE_TELNET") or (CHC["telnet"] if CH else 1234))
API_PORT = int(os.environ.get("HYPE_PORT") or (chancfg.engine_port(CH) if CH else 8090))
ST_DIR = os.environ.get("HYPE_STATE") or ("/var/lib/hype/ch-%s" % CH if CH else "/var/lib/hype")
if CH: os.makedirs(ST_DIR, exist_ok=True)
def SP(p):
    """A state file of THIS engine: Hype Radio's own path, or the same name in the channel's state folder."""
    return os.path.join(ST_DIR, os.path.basename(p)) if CH else p
JDIR = "jingles-%s" % CH if CH else "jingles"            # the station's own jingles root (IDs, liners, listener voices)
VOICE_PFX = JDIR + "/VOICE/"; VT_PFX = JDIR + "/VOICETRACK/"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("engine"); logging.getLogger("obsws_python").setLevel(logging.WARNING)

CLOCK_A = ["LAT-POWER", "AFRO-CURRENT", "CARIB-MIX", "ABC-CURRENT", "HIPHOP-RNB-MIX", "LAT-CURRENT", "#NEW-SWEEPER",
           "NEW", "ABC-POWER", "DUTCH-CURRENT", "LAT-GOLD", "AFRO-POWER", "UP-AFRO", "UP-AFRO", "#SWEEPER",
           "CARIB-POWER", "ABC-GOLD", "DUTCH-GOLD", "#THROWBACK-SWEEPER", "CLASSICS", "LAT-CURRENT",
           "HIPHOP-RNB-MIX", "ABC-CURRENT", "AFRO-GOLD", "CARIB-MIX"]
ARTIST_SEP = 75 * 60; TITLE_SEP = 3 * 3600; TARGET_LUFS = -14.0; PLAN_LEN = 10; MIX_MAX = 5.0   # separation + overlap: Settings
COLS = "path, artist, title, folder, mode, clip_path, duration, lufs, cue_start, cue_mix, cue_end, section, has_video, cues_edited"
LOCK = threading.RLock()

def jdump(obj, path, mode=None, **kw):
    """Write a JSON state file atomically (tmp + replace): a crash or a reader never sees a half-written file."""
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w") as f: json.dump(obj, f, **kw)
    if mode is not None: os.chmod(tmp, mode)
    os.replace(tmp, path)
SCREEN_EV_LOCK = threading.Lock()   # screen_events.json: read-modify-write by the engine (the guard trims it too)

def fold(t):
    """For searching: lower case, no accents (Álvaro = alvaro, Bésame = besame, Ñejo = nejo)."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(t or "")) if not unicodedata.combining(ch)).lower()
TL = threading.local()       # the theme channel a panel request is for (?ch=latina): set only for the planning / report paths below
CH_PATHS = ("/playlists", "/playlist", "/folders", "/padbanks", "/pads", "/clocks", "/shows", "/schedule", "/events", "/preview_hour", "/history", "/history.csv",
            "/rights.csv", "/rights_check", "/week", "/weekreport", "/audstats", "/aud24", "/audsongs", "/audtime")
_STATION_COL = []
audstats.CHF = lambda: getattr(TL, "ch", None) or CH or ""     # History → Audience per station: a theme channel's own stats_<id>.db
def dbc():
    ch_ = getattr(TL, "ch", None) or CH
    if ch_: return chancfg.connect(ch_)       # a theme channel's own planning database (songs = the library)
    c = sqlite3.connect(DB, timeout=30)
    if not _STATION_COL:            # items.station ('' = Hype Radio, else a theme channel) – the indexer adds it too; whoever is first
        try:
            if "station" not in [r[1] for r in c.execute("PRAGMA table_info(items)")]: c.execute("ALTER TABLE items ADD COLUMN station TEXT DEFAULT ''"); c.commit()
            _STATION_COL.append(1)
        except sqlite3.OperationalError: pass
    c.create_function("fold", 1, fold, deterministic=True)
    if not _DBI[0]:      # once per engine run: WAL (readers never wait for the indexer), as-run table + its indexes
        try: c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as e: log.warning("WAL: %s", e)
        c.execute("""CREATE TABLE IF NOT EXISTS asrun(id INTEGER PRIMARY KEY, started REAL, path TEXT, artist TEXT,
                     title TEXT, folder TEXT, mode TEXT, clip TEXT, scene TEXT, kind TEXT)""")
        if "over" not in [r[1] for r in c.execute("PRAGMA table_info(asrun)")]: c.execute("ALTER TABLE asrun ADD COLUMN over TEXT")   # "+ ID …" over the intro (History)
        c.execute("CREATE INDEX IF NOT EXISTS asrun_path ON asrun(path)"); c.execute("CREATE INDEX IF NOT EXISTS asrun_started ON asrun(started)")
        c.execute("CREATE TABLE IF NOT EXISTS aud_samples(t REAL PRIMARY KEY, r INT, p INT, tv INT)")
        if "cue_check" not in [r[1] for r in c.execute("PRAGMA table_info(items)")]: c.execute("ALTER TABLE items ADD COLUMN cue_check INT DEFAULT 0")
        _ic = [r[1] for r in c.execute("PRAGMA table_info(items)")]
        for _col, _typ in (("tempo", "REAL"), ("tempo_src", "TEXT"), ("tempo_pending", "INT DEFAULT 0")):   # Library → Tempo (27 Nov)
            if _col not in _ic: c.execute(f"ALTER TABLE items ADD COLUMN {_col} {_typ}")   # audience every 15 s (History: who came / left per song)
        c.commit(); _DBI[0] = True
    return c
_DBI = [False]

def row_item(r, src="clock"):
    k = ["path", "artist", "title", "folder", "mode", "clip_path", "duration", "lufs", "cue_start", "cue_mix", "cue_end", "section", "has_video", "cues_edited"]
    d = dict(zip(k, r)); d["id"] = uuid.uuid4().hex[:10]; d["src"] = src
    if d["section"] == "jingles": d["mode"] = "jingle-video" if d["has_video"] else "jingle"
    return d

class Liq:
    def __init__(self, port=None): self.port = port or LIQ_PORT   # this engine's own radio engine (Hype Radio 1234; a theme station on Hype Radio's engine: its own port)
    def cmd(self, line):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall((line + "\nquit\n").encode()); data = b""
            while True:
                b = s.recv(4096)
                if not b: break
                data += b
        return data.decode(errors="ignore").replace("Bye!", "").replace("END", "").strip()
    def queue_len(self):
        return len([x for x in self.cmd("q.queue").split() if x.isdigit()])

def self_now_artist():
    try: return (E.now or {}).get("artist")
    except NameError: return None
def artist_key(a):
    """Each artist on its own: "Pop Smoke X 50 Cent x Roddy Ricch" → pop smoke · 50 cent · roddy ricch (x / X, ft., feat., featuring, vs, &, en, comma)."""
    out = [p.strip().lower() for p in re.split(r"\s+(?:x|ft\.?|feat\.?|featuring|vs\.?|&|en|,)\s+|,", a or "", flags=re.I) if p.strip()]
    return [p for p in out if p not in NOT_ARTISTS] or out          # "Club Hype x DJ Kim" = DJ Kim

NOT_ARTISTS = {"club hype", "hype radio", "hype radio tv", "hype"}     # our own show / station names are not an artist

class Picker:
    def __init__(self):
        self.slot = 0; self.recent = []; self.songs_since_jingle = 99; self.secs_since_jingle = 9999; self.last_hour = time.localtime().tm_hour
    def note(self, it):
        self.recent.append((time.time(), it["path"], artist_key(it["artist"])))
        self.recent = [r for r in self.recent if time.time() - r[0] < TITLE_SEP]
    def pick_song(self, c, folder, avoid):
        rows = c.execute(f"""SELECT {COLS} FROM items WHERE section='music' AND problem IS NULL AND mode IS NOT NULL
                             AND (folder=? OR folder LIKE ? OR folder LIKE ?) AND ((folder NOT LIKE 'MIXES%' AND folder NOT LIKE 'RECORDINGS%') OR ? LIKE 'MIXES%' OR ? LIKE 'RECORDINGS%')""", (folder, "%/" + folder, folder + "/%", folder, folder)).fetchall()
        if "/" in folder: rows = [r for r in rows if not any(x.startswith("_") for x in (r[3] or "")[len(folder) + 1:].split("/")) or not (r[3] or "").startswith(folder + "/")]   # (never an archive subfolder)
        if not rows: return None
        now = time.time(); paths = {r[1] for r in self.recent} | avoid["paths"]
        arts = {a for t, p, aa in self.recent if now - t < ARTIST_SEP for a in aa} | avoid["artists"]
        for strict in (True, False):
            cand = [r for r in rows if r[0] not in paths and (not strict or not (set(artist_key(r[1])) & arts))]
            if cand: return random.choice(cand)
        last = {p: t for t, p, a in self.recent}
        return min(rows, key=lambda r: last.get(r[0], 0))
    def pick_jingle(self, c, kind, avoid):
        folder = "IDS" if kind == "#ID" else "SWEEPERS"
        sub = {"#NEW-SWEEPER": "NEW", "#THROWBACK-SWEEPER": "THROWBACK", "#SWEEPER": "", "#ID": ""}[kind]
        rows = c.execute(f"SELECT {COLS} FROM items WHERE section='jingles' AND folder=? AND problem IS NULL", (folder,)).fetchall()
        if sub: rows = [r for r in rows if sub.lower() in (r[0] + r[2]).lower()] or rows
        elif folder == "SWEEPERS": rows = [r for r in rows if not re.search(r"new music|throwback", r[0] + r[2], re.I)] or rows
        # never the same jingle within the hour; a New Music / Throwback slot falls back to a generic sweeper
        hour = {p for t, p, a in self.recent if time.time() - t < 3600} | avoid["paths"]
        fresh = [r for r in rows if r[0] not in hour]
        if not fresh and sub and folder == "SWEEPERS":
            return self.pick_jingle(c, "#SWEEPER", avoid)
        return random.choice(fresh) if fresh else None
    def next_planned(self, c, avoid, at, runner):
        """Use Gianno's clocks/schedule; returns None when nothing is planned there."""
        now = time.time()
        paths = {r[1] for r in self.recent} | avoid["paths"]
        arts = {a for t, p, aa in self.recent if now - t < ARTIST_SEP for a in aa} | avoid["artists"]
        held = planner.held_songs(c, at)          # Top 10 songs etc.: not in the normal rotation around the hour they play
        def ok(path):
            if path in paths or path in held: return False
            r = c.execute("SELECT artist FROM items WHERE path=?", (path,)).fetchone()
            return not (r and set(artist_key(r[0])) & arts)
        near = set(avoid["artists"]) | set(artist_key((self_now_artist() or "")))
        def fallback(path):
            """No song in the slot fits the artist gap (a small folder where a few artists are on most songs – e.g. the theme
            stations): relax ONLY the artist gap. Never a song that played in the last hours or is planned, and never the same
            artist as one of the last songs before it."""
            if path in paths or path in held: return False
            r = c.execute("SELECT artist FROM items WHERE path=?", (path,)).fetchone()
            return not (r and set(artist_key(r[0])) & near)
        ok.fallback = fallback
        def pick_jingle(c_, folder):
            sec = "ads" if folder == "ADS" else "jingles"
            rows = c_.execute(f"SELECT path FROM items WHERE section=? AND problem IS NULL AND (folder=? OR folder LIKE ? OR ?='ADS')",
                              (sec, folder, folder + "/%", folder)).fetchall()
            hour = {p for t, p, a in self.recent if now - t < 3600} | avoid["paths"]
            fresh = [r[0] for r in rows if r[0] not in hour] or [r[0] for r in rows]
            return random.choice(fresh) if fresh else None
        path = runner.next(c, at, ok, pick_jingle)
        if not path: return None
        r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (path,)).fetchone()
        return row_item(r) if r else None
    def next(self, c, avoid):
        for _ in range(len(CLOCK_A) * 2):
            slot = CLOCK_A[self.slot % len(CLOCK_A)]; self.slot += 1
            if slot.startswith("#"):
                # at least 3 songs and 12 minutes of music between sweepers
                if self.songs_since_jingle < 3 or self.secs_since_jingle < 720: continue
                r = self.pick_jingle(c, slot, avoid)
                if r: self.songs_since_jingle = 0; self.secs_since_jingle = 0; return row_item(r)
            else:
                r = self.pick_song(c, slot, avoid)
                if r: self.songs_since_jingle += 1; self.secs_since_jingle += (r[6] or 180); return row_item(r)
        return None

ADBOX_ALWAYS = True # Settings → TV → ad box slideshow always on the Hype Screen
JINGLE_DB = -3.0    # Settings → Mixing → Jingles volume (dB compared to the songs)
JINGLE_FADE = 0.0   # Settings → Mixing → Jingles: fade-out (0 = plays out to its END)
MIX_OUT = 1.5     # Settings → Mixing → fade-out of the old song (= how long both songs play together)
# ---------------- smart mix (from the loudness map) ----------------
SMART_MIX = True     # Settings → Mixing → Smart mix
VOICE_AIR = {"per_hour": 2, "max_plays": 3, "intro": True}   # Settings → Listener voices (only voices marked ✓ OK for air in Hype Live)
ID_INTRO = {"on": True, "max": 3, "gap": 15, "db": -4.0}   # Settings → Mixing → ID over soft intros (max per hour, minutes apart)
ID_FOLDER = (CH.upper() + "/IDS") if CH else "IDS"    # the IDs: /srv/hype/jingles/IDS
ID_OFFSET, ID_DUCK = 0.2, 0.5   # the ID starts 0.2 s into the song (26 Nov, Gianno: was 0.4; the song now starts already dipped); the (already soft) intro dips to 50 % under the voice
PUSH_LEAD = 15.0      # the next item goes to Liquidsoap 15 s before the song on air ends (see push_next)
CLOCK_POS = SP("/var/lib/hype/clock_pos.json"); PLAN_FILE = SP("/var/lib/hype/upnext_plan.json")
SNAPS_FILE = SP("/var/lib/hype/plan_snaps.json"); MINE_FILE = SP("/var/lib/hype/upnext_mine.json")
TV_AUDIO_BUF = 0.20  # the radio's 0.2 s cushion on the line to OBS (radio.liq, 26 Nov): the videos start that much later too
AV_LAG = 0.20        # the radio's sound reaches OBS this much after Liquidsoap starts a song (measured 30 Oct: ~0.2 s)
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
def duck_len(it):
    """How long the music stays down under a ducked pad / intro sweeper: until the voice ends (smart mix point), so the
    music comes back right after it and the echo / silence at the end of the file plays over the music - like a mix."""
    cs = it.get("cue_start") or 0
    sm = smart_mix(dict(it, section="jingles"))
    if sm: return max(0.5, sm[0] - cs)
    cm = it.get("cue_mix")
    if cm and cm > cs: return cm - cs
    return max(0.5, (it.get("cue_end") or it.get("duration") or 5) - cs)
def annotate(it, pad=False, nxt=None):
    cs = it["cue_start"] or 0.0; dur = it["duration"] or it["cue_end"] or 0; ce = it["cue_end"] or dur
    cm = it["cue_mix"] if it["cue_mix"] and it["cue_mix"] > cs else ce
    sm = None if pad else smart_mix(it, nxt)
    # the next song starts at full volume at MIX; the old one plays on under it and fades out over MIX_OUT seconds
    if sm: cm, cross, fade = sm
    elif it.get("section") in ("jingles", "ads") and not pad:
        # jingles/sweepers: the next song starts at the jingle MIX; the jingle tail plays out to its END
        # (Settings → Mixing → Jingles fade-out: 0 = no fade, else it fades over that many seconds)
        cross = max(0.3, min(8.0, ce - cm)); fade = JINGLE_FADE
    else:
        end_ = max(ce, dur or ce)
        if not it.get("cues_edited") and it.get("section") == "music" and end_ - cm < MIX_OUT:
            # automatic cue points (not CLOSE's / yours) with the MIX right at the end of the file: start the next song a bit
            # earlier, so there is always a full fade-out to hear
            cm = max(cs, end_ - MIX_OUT)
        # songs: the old one plays on under the new one for the Mix length, fading out – up to the real end of the file
        # (the END cue is usually the last loud moment, often < 1 s after MIX: too short to hear a fade)
        cross = max(0.3, min(MIX_MAX, MIX_OUT, max(ce, dur or ce) - cm)); fade = cross
        # ...except when the music right after END is still loud (END cuts the song off on purpose, e.g. before the
        # next part): then END is a hard stop - the old song fades out between MIX and END and is silent at END
        if it.get("section") == "music" and ce < (dur or ce) - 0.5:
            lm, L, _m = lm_info(it["path"])
            w = lm[int(ce * 2):int(ce * 2) + 4] if lm else None
            if w and L is not None and sum(w) / len(w) >= L - 12:
                cross = max(0.3, min(MIX_MAX, MIX_OUT, ce - cm)); fade = cross
    vm = vt_mix_at(it["path"]) if not pad and it["path"].startswith(VT_PFX) else None
    vp = vt_pre(nxt["path"]) if not pad and nxt and str(nxt.get("path") or "").startswith(VT_PFX) else None
    if vp:      # the next item is a voice link that starts over the END of this song: earlier, and this song stays low under it
        end_ = max(ce, dur or ce); cm = max(cs + 1.0, min(cm, end_) - vp); cross = max(0.3, min(60.0, end_ - cm)); fade = cross
    if vm:      # a voice link that talks over the next song's intro: the song starts at the moment you started it while recording
        end_ = max(ce, dur or ce); cm = max(cs + 0.3, min(vm, end_ - 0.3)); cross = max(0.3, min(60.0, end_ - cm)); fade = 0.0
    ce = cm + cross
    # every song and jingle: -14 LUFS into the processing (the level the sound presets were tuned on);
    # pads + intro sweepers: -14 (a bit on top of the music, as before)
    gain = 0.0 if it["lufs"] is None else max(-12.0, min(12.0, (-14.0 if pad else TARGET_LUFS) - it["lufs"]))
    if it.get("section") in ("jingles", "ads"): gain += JINGLE_DB    # short, dense jingles sound louder than songs at the same LUFS (Settings → Mixing)
    # one telnet line: no control characters (a newline would split the command), no " or \ inside a quoted value
    if BAD_NAME.search(it["path"]): raise ValueError("file name with control characters: %r" % it["path"])
    q = lambda v: BAD_NAME.sub(" ", str(v)).replace("\\", "/").replace('"', "'")
    meta = (f'liq_cue_in="{cs:.2f}",liq_cue_out="{ce:.2f}",liq_cross_duration="{cross:.2f}",liq_amplify="{gain:.2f} dB",hype_fade="{fade:.2f}",' + ('hype_duck="0.35",' if vm else '') + ('hype_curve="1",' if it.get("section") == "music" and not pad and not vm and not vp else '') +
            (f'hype_iduck="{ID_DUCK:.2f}",hype_ihold="{ID_OFFSET + 1.2:.2f}",' if it.get("id_over") and not pad else '') + ('hype_tail="0.35",' if vp else '') +
            f'artist="{q(it["artist"])}",title="{q(it["title"])}",hype_path="{q(it["path"])}",hype_id="{it["id"]}"')
    return f"annotate:{meta}:{os.path.join(ROOT, it['path'])}"

BAD_NAME = re.compile(r"[\x00-\x1f\x7f]")
DECKS_FILE = SP("/var/lib/hype/decks.json")
# Preloading the next clip (paused on its keyframe). 19 Nov: with the picture-only TV copies OBS 32 played a clip on while it
# said PAUSED when we paused BEFORE seeking -> now seek, then pause, then check it stands still (else no preload).
PRELOAD = True
def annotate_deck(it):
    """A song for a LIVE deck: START → END, no automatic mix (you mix on the Mixcast), the same loudness as on air."""
    cs = it.get("cue_start") or 0.0; dur = it.get("duration") or it.get("cue_end") or 0; ce = it.get("cue_end") or dur
    gain = 0.0 if it.get("lufs") is None else max(-12.0, min(12.0, TARGET_LUFS - it["lufs"]))
    if it.get("section") in ("jingles", "ads"): gain += JINGLE_DB
    q = lambda v: BAD_NAME.sub(" ", str(v or "")).replace("\\", "/").replace('"', "'")
    meta = (f'liq_cue_in="{cs:.2f}",' + (f'liq_cue_out="{ce:.2f}",' if ce > cs else "") + f'liq_amplify="{gain:.2f} dB",'
            f'artist="{q(it.get("artist"))}",title="{q(it.get("title"))}",hype_path="{q(it["path"])}",hype_id="{it["id"]}"')
    return f"annotate:{meta}:{os.path.join(ROOT, it['path'])}"
def plain_len(it):
    """Length from the cue points only (no loudness maps) - for quick estimates over many songs."""
    return max(1.0, (it.get("cue_mix") or it.get("cue_end") or it.get("duration") or 180) - (it.get("cue_start") or 0))

def warm(c, it):
    files = [os.path.join(ROOT, it["path"])]
    r = c.execute("SELECT tv_path, clip_path FROM items WHERE path=?", (it["path"],)).fetchone()
    if r:
        if r[0]: files.append(r[0])
        if r[1]:
            t = c.execute("SELECT tv_path FROM items WHERE path=?", (r[1],)).fetchone()
            files.append(t[0] if t and t[0] else os.path.join(ROOT, r[1]))
    def go():
        for f in files:
            try:
                with open(f, "rb") as fh:
                    while fh.read(4 << 20): pass
            except OSError: pass
    threading.Thread(target=go, daemon=True).start()

CHART_HINT = {}   # path -> (chart playlist id, when) for chart songs that came in through a fixed-time block
class Screen:
    """On-screen items (Gianno's PNG/WebM graphics): shown by the Graphics page when a rule fires."""
    def __init__(self):
        self.active = []; self.count = {}; self.last = {}; self.done_min = set()
        self.upn_hour = time.strftime("%Y%m%d%H") if time.localtime().tm_min >= 2 else None
        self.upn_done = self.upn_hour            # a restart mid-hour: no extra Up next
        self.np = None; self.chart = None
        try:   # what was on screen before a restart stays on screen
            self.active = [a for a in json.load(open(SCREEN_STATE)) if a["end"] > time.time()]
            for a in self.active: a["file"] = tickers.as_ticker(a["file"])
        except Exception: pass
    def save(self):
        try: jdump([a for a in self.active if a["end"] > time.time()], SCREEN_STATE)
        except Exception: pass
    def add(self, rid, file, delay, dur, where="all", manual=False):
        file = tickers.as_ticker(file)     # a ticker's bar PNG = that ticker (bar + scrolling text)
        now = time.time(); self.active = [a for a in self.active if a["end"] > now][-20:]
        a = dict(id=uuid.uuid4().hex[:8], rid=rid, file=file, start=now + max(0.0, delay), end=now + max(0.0, delay) + max(1.0, dur), where=where, manual=manual)
        self.active.append(a)
        if rid: self.last[rid] = now
        self.save(); return a["id"]
    def manual(self):
        """Elements you put on screen yourself (Scenes → + ELEMENT): they stay until you take them off."""
        now = time.time(); return [dict(id=a["id"], file=a["file"]) for a in self.active if a.get("manual") and a["end"] > now]
    def hide(self, eid=None):
        now = time.time()
        for a in self.active:
            if a.get("manual") and (eid is None or a["id"] == eid): a["end"] = min(a["end"], now)
        self.save()
    def fire(self, r, length=None):
        if not planner.rule_now(r): return          # only on its days / hours (Planning → ON SCREEN)
        n = self.count.get(r["id"], 0) + 1; self.count[r["id"]] = n
        if n % max(1, r["every"] or 1): return
        if r["min_gap"] and time.time() - self.last.get(r["id"], 0) < r["min_gap"] * 60: return
        f = tickers.as_ticker(r["file"] or "")
        if f.startswith("scene:"):    # a rule can also switch the TV to a scene (0 s = it stays until AUTO SCENES)
            name, dl, hold = f[6:], r["delay"] or 0, r["duration"] or 0
            if not E.tv.auto: log.info("scheduled scene %s skipped: scenes are on manual (e.g. a live show)", name); return
            threading.Timer(max(0.0, dl), lambda: (also_fetch() if name == ALSO_SCENE else None, E.hold_scene(name, hold))).start(); log.info("scheduled scene: %s (%s)", name, r["trigger"]); return
        if f.startswith(("ticker:", "adbox:")):   # a ticker (News, Traffic …) or the ad box slideshow: duration 0 = 60 s
            self.add(r["id"], f, r["delay"] or 0, r["duration"] or 60, r["where_"]); log.info("ticker on screen: %s (%s)", f[7:], r["trigger"]); return
        dur = r["duration"] or max(5.0, (length or 180) - (r["delay"] or 0))    # 0 = the whole song, until the next song starts
        self.add(r["id"], f, r["delay"] or 0, dur, r["where_"]); log.info("on screen: %s (%s)", r["name"] or f, r["trigger"])
    def on_song(self, c, it):
        length = max(1.0, (it.get("cue_mix") or it.get("cue_end") or it.get("duration") or 180) - (it.get("cue_start") or 0))
        self.chart = None
        if it["section"] == "music":
            now = time.time()
            # artist + title in the Now Playing box (Music video scene only): after the start, and again halfway on longer songs
            at = [now + 5.0] + ([now + length / 2] if length > 150 else [])
            mix_no_artist = (it.get("folder") or "").upper().startswith(("MIXES", "RECORDINGS")) and not (it.get("artist") or "").strip()
            self.np = None if mix_no_artist else {"id": uuid.uuid4().hex[:8], "title": it.get("title") or "", "artist": it.get("artist") or "", "at": at, "start": now, "end": now + length}   # a mix without artist: no box
            h_ = CHART_HINT.get(it["path"])
            if not it.get("chart_pid") and h_ and now - h_[1] < 6 * 3600: it["chart_pid"] = h_[0]
            if it.get("chart_pid"):      # a chart song: its number (+ ▲ ▼ NEW RE) in the yellow square, the whole song
                try:
                    pid = it["chart_pid"]
                    r_ = c.execute("SELECT COALESCE(chart_num,1), COALESCE(chart_arrows,1) FROM playlists WHERE id=? AND COALESCE(chart,0)=1", (pid,)).fetchone()
                    pos = dict(planner.chart_positions(c, pid)).get(it["path"]) if r_ and r_[0] else None
                    if pos:
                        mv = planner.chart_move(c, pid, it["path"], pos) if r_[1] else None
                        planner.chart_snapshot(c, pid)          # this week's list = the base for next week's arrows
                        self.chart = {"num": pos, "mv": list(mv) if mv else None, "start": now, "end": now + length + 30}
                        log.info("chart #%d %s", pos, ("%s %s" % mv) if mv else "")
                except Exception as e: log.warning("chart number: %s", e)
        if it["section"] == "music":
            try: self.upnext_plan(c, it, length)
            except Exception as e: log.warning("up next: %s", e)
        for r in planner.api_screen_rules(c):
            if not r["enabled"] or not r["file"]: continue
            t, ref = r["trigger"], r["ref"] or ""
            ok = (t == "any" and it["section"] == "music") or (t == "jingle" and it["section"] == "jingles" and (not ref or (it["folder"] or "").startswith(ref)))
            if t == "folder" and it["section"] == "music": ok = planner.rule_matches(c, {"match_kind": "folder", "match_ref": ref}, it)
            if t == "playlist" and it["section"] == "music" and ref: ok = planner.in_playlist(c, ref, it["path"])
            if ok: self.fire(r, length)
    def upnext_plan(self, c, it, length):
        """The Up next PIP (scene "Up next": the music video in a window + now / next / then with start times):
        in the last N s of every Nth music video, and a few seconds into the first song of each hour (Settings → TV)."""
        end_s = float(planner.get_setting(c, "upnext_end_s", 0) or 0)       # 0 = off (Gianno: once an hour is enough)
        hour_s = float(planner.get_setting(c, "upnext_hour_s", 10) or 0)
        after_s = float(planner.get_setting(c, "upnext_after_s", 12) or 0)   # how far into the music video it comes
        hk = time.strftime("%Y%m%d%H"); first = getattr(self, "upn_hour", None) != hk; self.upn_hour = hk
        path = it["path"]
        if hour_s > 0 and getattr(self, "upn_done", None) != hk and it.get("mode") in ("clip", "pair") and length > after_s + hour_s + 15:
            self.upn_done = hk          # the first MUSIC VIDEO of the hour (an audio song first / too short: the next video gets it)
            threading.Timer(max(3.0, after_s), lambda: E.upnext_show(path, hour_s)).start()
        elif end_s > 0 and time.localtime().tm_min >= 30 and getattr(self, "upn_half", None) != hk and it.get("mode") in ("clip", "pair") and length > end_s + 40:
            self.upn_half = hk          # once in the second half of the hour: the last N s of a music video
            threading.Timer(max(1.0, length - end_s), lambda: E.upnext_show(path, end_s - 0.5)).start()
        # Hype Stations PIP: every hour at :MM (Settings → TV). The music video that is on at :MM gets it (or, when a jingle / audio
        # song plays then, the first music video that starts in the next 4 minutes)
        at = int(planner.get_setting(c, "alsoon_at", -1) if planner.get_setting(c, "alsoon_at", -1) is not None else -1)
        dur = float(planner.get_setting(c, "alsoon_s", 20) or 20)
        if 0 <= at <= 59 and it.get("mode") in ("clip", "pair") and getattr(self, "also_hour", None) != hk:
            now_ = time.time(); lt = time.localtime(now_)
            mark = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, at, 0, 0, 0, -1))
            if mark + 240 < now_: mark += 3600                       # :MM already well past this hour: the next hour's
            start = max(mark, now_ + max(3.0, after_s))               # at :MM, or a few seconds into a video that starts after :MM
            if start - now_ + dur + 5 < length and start - mark < 240:
                self.also_hour = time.strftime("%Y%m%d%H", time.localtime(mark))
                threading.Timer(start - now_, lambda: self.also_show(path, dur)).start()
    def also_show(self, path, dur):
        """Hype Stations PiP now (Settings → TV: every hour at :MM) – same rules as Up next: over the music video that planned it, AUTO, not LIVE."""
        n = E.now or {}          # (this is the Screen planner: what plays and the TV are the engine's)
        if n.get("path") != path or n.get("mode") not in ("clip", "pair") or E.live.live or E.stopped: return
        if not E.tv.auto or E.tv.scene != "Music video": return
        if n.get("chart_pid"): return
        also_fetch()
        if not ALSO_NOW["d"]: return                               # both theme stations off: nothing to show
        log.info("Hype Stations PIP for %.0f s", dur); E.hold_scene(ALSO_SCENE, dur)
    def on_show(self, title):
        c = dbc()
        for r in planner.api_screen_rules(c):
            if r["enabled"] and r["file"] and r["trigger"] == "show" and (not r["ref"] or r["ref"] == title): self.fire(r)
    def tick(self):
        lt = time.localtime(); key = time.strftime("%Y%m%d%H%M", lt)
        for r in planner.api_screen_rules(dbc()):
            if not (r["enabled"] and r["file"] and r["trigger"] == "time" and ":" in (r["ref"] or "")): continue
            hh, mm = r["ref"].split(":")
            if (hh == "*" or int(hh) == lt.tm_hour) and int(mm) == lt.tm_min and (r["id"], key) not in self.done_min:
                self.done_min.add((r["id"], key)); self.fire(r)
    def current(self, scene):
        now = time.time(); cam = bool(scene) and (scene.startswith("CAM") or scene.startswith("VID"))
        out = []; tick = None; ad_el = False
        for a in self.active:
            if a["end"] <= now: continue
            if a["where"] == "video" and not scene_has_video(scene): continue
            if a["where"] == "nocam" and cam: continue
            if a["where"] == "music_hype" and scene not in ("Music video", "Hype Screen"): continue
            if a["file"].startswith("adbox:"):
                if a["start"] <= now: ad_el = True
                continue
            if a["file"].startswith("ticker:"):
                if a["start"] <= now and (not tick or a["start"] > tick[0]):
                    g = tickers.get(a["file"][7:])
                    if g: tick = (a["start"], dict(g, key=a["id"]))
                continue
            try: v = int(os.path.getmtime(os.path.join(planner.SCREEN_DIR, a["file"])))   # a replaced file shows its new version
            except OSError: v = 0
            out.append(dict(id=a["id"], url="/screen/" + urllib.parse.quote(a["file"]) + "?v=%d" % v, start=a["start"], end=a["end"]))
        # ad box slideshow: always on the Hype Screen (Settings → TV), or as an element (+ ELEMENT / Planning → ON SCREEN) in any scene
        show_ad = ad_el or (scene == "Hype Screen" and ADBOX_ALWAYS)
        try: slides = sorted(f for f in os.listdir(ADBOX) if re.search(r"\.(png|jpe?g|webp)$", f, re.I)) if show_ad else []
        except OSError: slides = []
        if tick: tick = tick[1]
        else:      # no planned ticker: the one that is always on in this scene (e.g. News on the Hype Screen)
            g = tickers.always_for(scene); tick = dict(g, key="always-" + g["id"]) if g else None
        if LOWER.get("visible"): tick = None      # a lower third is on: the ticker makes way (same place on screen), comes back after
        voice = dict(VOICE_ON) if VOICE_ON and VOICE_ON.get("end", 0) > now else None
        upn = None
        if scene == "Up next":
            try: upn = E.upnext_data(); upn = dict(upn, until=getattr(E, "hold_until", 0)) if isinstance(upn, dict) else upn
            except Exception as e: log.warning("up next data: %s", e)
        also = None
        if scene == ALSO_SCENE:
            also = dict(also_now(), until=getattr(E, "hold_until", 0))
        ch = self.chart if self.chart and self.chart["end"] > now else None
        return {"also": also, "station": None if CH else TVF.get("ch"), "now": now, "items": out, "scene": scene, "np": self.np if scene == "Music video" else None, "chart": ch, "ticker": tick, "voice": voice, "upnext": upn, "jingle": bool(GFX["hide"]), "gfx": dict(GFX, hide=bool(GFX["hide"])),
                "slides": ["/adbox/" + urllib.parse.quote(f) for f in slides], "slide_s": 30,
                "slides_layer": "adbox" if scene == "Hype Screen" else "graphics"}   # Hype Screen: its own source under the Live Layer

COVERS = "/srv/hype-data/covers"; COVER_IDX = os.path.join(COVERS, "index.json")
COVER_Q = []; COVER_LOCK = threading.Lock(); COVER_MISS = {}
def cover_key(artist, title): return hashlib.sha1(("%s|%s" % ((artist or "").lower().strip(), (title or "").lower().strip())).encode()).hexdigest()[:16]
def cover_url(artist, title):
    """The song's cover from Hype Live's cover store (the same covers as Home / Hype Screen, incl. the ones fixed by hand)
    – None while it is looked up, when there is none, or when it was set to 🚫 (use the Hype artwork).
    A cover is asked again after a day, so a fix in Hype Live reaches Hype Studio."""
    if not (artist or "").strip() or not (title or "").strip(): return None
    k = cover_key(artist, title); f = os.path.join(COVERS, k + ".jpg")
    have = os.path.exists(f); old = not have or time.time() - os.path.getmtime(f) > 86400
    if old and time.time() - COVER_MISS.get(k, 0) > 86400:
        with COVER_LOCK:
            if k not in [x[0] for x in COVER_Q]: COVER_Q.append((k, artist, title))
    return "/api/cover/%s.jpg?v=%d" % (k, int(os.path.getmtime(f))) if have else None
def cover_worker():
    os.makedirs(COVERS, exist_ok=True)
    try: COVER_MISS.update(json.load(open(COVER_IDX)))
    except Exception: pass
    while True:
        time.sleep(3)          # at most one look-up per 3 s (Hype Live itself asks Apple at most once per 5 s)
        if hl_sync_active():      # Hype Live 2.42: the wanted covers ride along in the minute sync (no call per song)
            with COVER_LOCK: want = [{"artist": a, "title": t} for _, a, t in COVER_Q[:15]]
            try:
                if want != json.load(open(HL_WANT)): raise ValueError
            except Exception: jdump(want, HL_WANT)
            continue
        with COVER_LOCK: job = COVER_Q.pop(0) if COVER_Q else None
        if not job: continue
        k, artist, title = job; f = os.path.join(COVERS, k + ".jpg")
        try:
            d = json.loads(hl_call("hype_studio_cover", "&" + urllib.parse.urlencode({"artist": artist, "title": title}), timeout=15))
            if d.get("pending"): COVER_MISS[k] = time.time() - 86400 + 60      # Hype Live is still searching: again in 1 min
            elif d.get("url"):
                rs = urllib.request.urlopen(urllib.request.Request(d["url"], headers={"User-Agent": "HypeStudio/1"}), timeout=20)
                img = rs.read(5_000_000); want_ = int(rs.headers.get("Content-Length") or 0)
                # 19 Nov: some covers arrived cut off (half a picture in the player) – only a complete picture is kept
                if not img or (want_ and len(img) < want_) or (img[:3] == b"\xff\xd8\xff" and not img.rstrip(b"\x00\r\n ").endswith(b"\xff\xd9")):
                    raise ValueError("cover download incomplete (%d of %s bytes)" % (len(img), want_ or "?"))
                with open(f + ".tmp", "wb") as fh: fh.write(img)
                os.replace(f + ".tmp", f); COVER_MISS.pop(k, None)
            else:                                                              # none found, or 🚫 Wrong cover: no cover (Hype artwork)
                if os.path.exists(f): os.remove(f)
                COVER_MISS[k] = time.time()
        except Exception as e: log.info("cover %s – %s: %s", artist, title, e); COVER_MISS[k] = time.time() - 86400 + 300   # again in 5 min
        try: jdump(COVER_MISS, COVER_IDX)
        except Exception: pass
if not CH: threading.Thread(target=cover_worker, daemon=True).start()     # (a theme station: Hype Radio's engine fetches its covers)

ADBOX = "/srv/hype/branding/adbox"
PIP_DIR = "/srv/hype/branding/pip"          # frames for the PIP scene (full-frame PNG with a transparent window)
ALSO_SCENE = "Hype Stations"                  # PiP: the TV in a window + what Hype Latina / Hype Ambiente play now (Gianno's template PNG)
STINGER_SCENES = {"Hype Screen", "PIP", "Up next", ALSO_SCENE}
ALSO_NOW = {"t": 0, "d": {}}
def also_fetch():
    """What the theme stations play now (their own nowplaying feed), for the "Hype Stations" scene. A station that is off: no text."""
    out = {}
    for k in ("latina", "ambiente"):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/nowplaying/%s" % (chancfg.API_PORT, k), timeout=4) as r: song = (json.loads(r.read()).get("now_playing") or {}).get("song") or {}
            if song.get("title"): out[k] = {"title": song.get("title") or "", "artist": song.get("artist") or ""}
        except Exception: pass
    ALSO_NOW.update(t=time.time(), d=out)
def also_now():
    if time.time() - ALSO_NOW["t"] > 15 and not ALSO_NOW.get("busy"):      # refreshed in the background, every 15 s while it is asked for
        ALSO_NOW["busy"] = True
        def run():
            try: also_fetch()
            finally: ALSO_NOW["busy"] = False
        threading.Thread(target=run, daemon=True).start()
    return ALSO_NOW["d"]
def pip_frames():
    try: return sorted(f for f in os.listdir(PIP_DIR) if f.lower().endswith(".png"))
    except OSError: return []
SCREEN_STATE = SP("/var/lib/hype/screen_active.json")

def intro_kind(it):
    """A sweeper that announces a kind of song: 'new' (New Music) or 'throwback'."""
    if it.get("section") != "jingles": return None
    f, t = (it.get("folder") or "").upper(), (it.get("title") or "") + " " + os.path.basename(it.get("path") or "")
    if f.startswith("NEW MUSIC") or re.search(r"new music", t, re.I): return "new"
    if f.startswith("THROWBACK") or re.search(r"throwback|old school|rewind", t, re.I): return "throwback"
    return None

def song_kind(it):
    f = (it.get("folder") or "").upper()
    if f == "NEW" or f.startswith("NEW/") or f.startswith("NEW MUSIC") or f.endswith("/SUBMISSIONS"): return "new"
    if f == "CLASSICS" or f.startswith("CLASSICS/"): return "throwback"   # only the CLASSICS folder (and its subfolders) gets the Throwback jingle – not CHILL-CLASSICS, not GOLD (15 Nov)
    return None     # the ad box slideshow on the Hype Screen scene

def scene_has_video(scene):
    """Program scenes that show the music video (so the TV may play the clip's own sound)."""
    sc = scene or ""
    return sc == "Music video" or sc.startswith("VID") or "V" in sc.split()

class TV:
    SCENES = ["Music video", "Hype Screen", "Branding"]
    def __init__(self):
        self.cl = None; self.cur = "Clip B"; self.scene = None; self.loop_file = None; self.auto = True; self.first_show_seen = False
        self.clip_sound = False; self.split = None; self.olock = threading.RLock(); self.gen = 0
    def client(self):
        if self.cl is None:
            self.cl = obs.ReqClient(host="127.0.0.1", port=4455, password=CONF["obs_password"], timeout=5)
            try:   # which clip is on screen now (after a restart), so the next song loads into the other one
                on = [i["sourceName"] for i in self.cl.get_scene_item_list("Music video").scene_items if i["sceneItemEnabled"] and i["sourceName"] in ("Clip A", "Clip B")]
                if on: self.cur = on[0]
            except Exception: pass
            try: self.prog = self.cl.get_current_program_scene().current_program_scene_name   # what OBS really shows now
            except Exception: self.prog = None
            self._tr = None
            try: self.layer_alpha(self.cl)
            except Exception as e: log.warning("Live Layer transparency: %s", e)
            try: self.layer_top(self.cl)
            except Exception as e: log.warning("Live Layer on top: %s", e)
            try:   # the logo can be hidden while a jingle plays: an opacity filter "Hide" on the Logo source (off = visible)
                if not any(f["filterName"] == "Hide" for f in self.cl.get_source_filter_list("Logo").filters):
                    self.cl.create_source_filter("Logo", "Hide", "color_filter_v2", {"opacity": 0.0})
                    self.cl.set_source_filter_enabled("Logo", "Hide", False)
                self._logo_hidden = None
            except Exception as e: log.warning("logo hide filter: %s", e)
        return self.cl
    def jingle_graphics(self, hide):
        """While a jingle / sweeper / TOH plays: the graphics go - logo, Live Layer, show name, clock, Top 10 number - and come
        back with the music. ALL AT ONCE (Gianno, 24 Nov): one moment for everything (GFX["at"]), a 0.4 s fade in OBS and on the
        pages; back 0.6 s after the song starts."""
        gfx_want(bool(hide))
    def ll_hide(self, hide):
        """The Live Layer out of sight while a jingle / TOH plays and on the Branding scene (Gianno, 15 Nov): a "Hide" filter
        (opacity 0) on the source - the page keeps running, so nothing reloads and it is back at once after the jingle."""
        if getattr(self, "_ll_hidden", None) == hide: return
        try:
            with self.olock:
                cl = self.client()
                if not any(f["filterName"] == "Hide" for f in cl.get_source_filter_list("Live Layer").filters):
                    cl.create_source_filter("Live Layer", "Hide", "color_filter_v2", {"opacity": 0.0})
                cl.set_source_filter_enabled("Live Layer", "Hide", bool(hide))
            self._ll_hidden = hide
        except Exception as e: log.warning("Live Layer hide: %s", e); self.cl = None
    def layer_top(self, cl):
        """The Hype Live Layer is the top layer in every scene (above Graphics, the On Air box, logo, clock, Now Playing,
        the show intro): from Hype Live 2.25.2 it shows full-screen moments that must cover everything (New Year countdown,
        Merry Christmas, GOAL on match days). The rest of the time it is transparent."""
        # one exception, on top of it: "Top10 rank" (only the Top 10 number from the Graphics page) – it sits on the right
        # end of Hype Live's show name box during the Hype Top 10 and must be in front of it (Gianno, 9 Nov)
        moved = []
        for sc in cl.get_scene_list().scenes:
            items = cl.get_scene_item_list(sc["sceneName"]).scene_items
            ll = next((i for i in items if i["sourceName"] == "Live Layer"), None)
            tr = next((i for i in items if i["sourceName"] == "Top10 rank"), None)
            want = len(items) - (2 if tr else 1)
            if ll and ll["sceneItemIndex"] != want:
                cl.set_scene_item_index(sc["sceneName"], ll["sceneItemId"], want); moved.append(sc["sceneName"])
            if tr and tr["sceneItemIndex"] != len(items) - 1: cl.set_scene_item_index(sc["sceneName"], tr["sceneItemId"], len(items) - 1)
        if moved: log.info("Live Layer moved to the top in %d scene(s)", len(moved))
    LAYER_CSS = "html,body{background:transparent!important}:root{--key:transparent!important}"
    def layer_alpha(self, cl):
        """The Hype Live Layer page has a magenta background for a colour key (CLOSE could not do transparency). OBS can:
        the page's background is made transparent and the magenta key is switched off – a key also eats every pink /
        purple in the graphics (4 Nov: the holiday edges came out blotchy and see-through)."""
        for n in ("Live Layer", "Hype Screen page"):    # open the Hype Live pages on www.hyperadio.nl: they ask www for the exact time
            try:                                       # (holiday moments) – from hyperadio.nl the browser blocked that as another site
                u = cl.get_input_settings(n).input_settings.get("url") or ""
                if u.startswith("https://hyperadio.nl/"):
                    cl.set_input_settings(n, {"url": "https://www." + u[len("https://"):]}, True); log.info("%s: opened on www.hyperadio.nl", n)
            except Exception as e: log.warning("%s url: %s", n, e)
        s = cl.get_input_settings("Live Layer").input_settings
        if s.get("css") != self.LAYER_CSS:
            cl.set_input_settings("Live Layer", {"css": self.LAYER_CSS}, True); log.info("Live Layer: transparent background (no magenta key)")
        for f in cl.get_source_filter_list("Live Layer").filters:
            if "key" in f["filterKind"] and f["filterEnabled"]:
                cl.set_source_filter_enabled("Live Layer", f["filterName"], False); log.info("Live Layer: %s off", f["filterName"])
    def live_layer(self, on):
        """Show / hide the 'Live Layer' item in every scene (OBS keeps it that way, also after a restart)."""
        with self.olock:
            cl = self.client()
            for sc in cl.get_scene_list().scenes:
                for it in cl.get_scene_item_list(sc["sceneName"]).scene_items:
                    if it["sourceName"] == "Live Layer" and it["sceneItemEnabled"] != on: cl.set_scene_item_enabled(sc["sceneName"], it["sceneItemId"], on)
        self.ll_on = on
    def set_scene(self, scene):
      with self.olock:
        try:
            if scene and "@" in scene:          # "PIP@PIP_Advertise.png" = the PIP scene with that frame (branding/pip)
                scene, frame = scene.split("@", 1); self.pip_frame(frame)
            if scene == "Branding": self.branding_loop()      # make sure the show's loop is loaded (else Branding is black)
            if self.scene != scene:
                cl = self.client()
                # the Hype stinger (logo transition) only going into the Hype Screen / PIP scene; leaving it = the normal fade
                prev = getattr(self, "prog", None)     # the scene really on air (self.scene is sometimes reset to force a switch)
                tr = "Hype Stinger" if prev and scene in STINGER_SCENES else "Fade"   # only going INTO the Hype Screen / PIP
                try:
                    cl.set_current_scene_transition(tr); self._tr = tr     # every time (OBS may have another one set, e.g. after its restart)
                    if tr == "Hype Stinger" and not getattr(self, "_st_xf", False):
                        # the stinger's sound: CROSSFADE (the music goes on under it). "Fade out, then in" (OBS's default) made a
                        # ~0.1 s silence on TV at every switch into the Hype Screen (26 Nov: Matadora → Wine, D2, TEA → Probresa)
                        cl.set_current_scene_transition_settings({"audio_fade_style": 1}, True); self._st_xf = True
                except Exception as e: log.warning("transition %s: %s", tr, e)
                if prev != scene: cl.set_current_program_scene(scene)
                if prev == "PIP" and scene != "PIP": self.pip_done()
                if scene == "PIP" and prev != "PIP": self._pip_t = time.time(); self._pip_f = getattr(self, "pip", None)
                self.scene = scene; self.prog = scene
        except Exception as e: log.warning("OBS scene: %s", e); self.cl = None
        self.route_audio()
        try:   # clean screen during a jingle only in the Music video scene: another scene (cameras …) -> the logo back at once
            n = tv_item()
            self.jingle_graphics(bool(JINGLE_HIDE and self.scene == "Music video" and not E.live.live and n.get("path") and n.get("section") != "music"))
        except Exception: pass
        try: self.ll_hide(self.scene == "Branding")        # (jingles: the Live Layer goes with the other graphics, gfx_want)
        except Exception: pass
    def pip_done(self):
        """A PIP showing ended: log frame + seconds for Hype Live's sponsor report (the frame name = the sponsor, e.g. PIP_Brand.png)."""
        t0 = getattr(self, "_pip_t", None); f = getattr(self, "_pip_f", None); self._pip_t = None
        if not t0 or not f: return
        dur = round(time.time() - t0)
        if dur < 3: return
        name = os.path.splitext(os.path.basename(f))[0]
        ev = {"t": int(t0), "type": "pip", "item": name, "brand": re.sub(r"^PIP[_ -]*", "", name, flags=re.I).replace("_", " ").strip(), "dur": dur}
        try:
            p = SP("/var/lib/hype/screen_events.json")
            with SCREEN_EV_LOCK:
                try: evs = json.load(open(p))
                except FileNotFoundError: evs = []
                evs = (evs + [ev])[-500:]; jdump(evs, p)    # (unreadable file: exception -> logged, the file is NOT overwritten)
        except Exception as e: log.warning("screen event: %s", e)
    def pip_frame(self, frame):
        f = os.path.join(PIP_DIR, os.path.basename(frame))
        if os.path.exists(f) and getattr(self, "pip", None) != f:
            if getattr(self, "prog", None) == "PIP": self.pip_done(); self._pip_t = time.time(); self._pip_f = f   # another frame while PIP stays on
            self.client().set_input_settings("PIP frame", {"file": f}, True); self.pip = f
    def adopt_audio(self):
        """At an engine start: take over the TV sound as OBS has it now, instead of forcing radio sound.
        (22 Nov: a restart during a paired clip with its own sound switched the TV to the radio mid-song: a hiccup.)
        The next song routes as normal."""
        try:
            if not getattr(self, "live", False):
                with self.olock:
                    db_ = self.client().get_input_volume("Radio audio").input_volume_db
                if db_ is not None and db_ <= -90:
                    self.clip_sound = True; self.split = True; self._radio_db = -100.0; self._live_r = False; self._ext_r = False
                    log.info("TV sound: left as it was (the clip's own sound) until the next song"); return
        except Exception as e: log.warning("TV sound at start: %s", e)
        self.route_audio(force=True)
    def route_audio(self, force=False):
      """TV sound = the radio, except when a paired clip is on screen: then the clip's own sound (+ pads/sweepers).
      While LIVE: only your studio mix, straight from the studio stream (same path as the cameras = in sync), never a clip's sound."""
      with self.olock:
        live = bool(getattr(self, "live", False))
        ext = bool(getattr(self, "ext", False)) and not live    # a live stream with its own sound (Music off): its sound straight from OBS = in sync with its picture
        # PIP too: the sponsor frame goes around the video, so the clip's sound plays on (no switch to the mp3 and back)
        split = bool(self.clip_sound and (scene_has_video(self.scene) or self.scene in ("PIP", "Up next", ALSO_SCENE))) and not live and not ext
        if (split, live, ext) == (self.split, getattr(self, "_live_r", None), getattr(self, "_ext_r", None)) and not force: return
        try:
            cl = self.client()
            want = -100.0 if (split or live or ext) else 0.0
            if not force and want != getattr(self, "_radio_db", None) and getattr(self, "_radio_db", None) is not None:
                # a short ramp instead of a hard cut (22 Nov: the switch radio -> a paired clip's own sound clicked on TV,
                # right where the ID over the intro started)
                steps = (-2.0, -6.0, -14.0, -30.0) if want < 0 else (-30.0, -14.0, -6.0, -2.0)
                if want < 0: cl.set_input_mute("FX audio", not (split or ext))
                for db_ in steps: cl.set_input_volume("Radio audio", vol_db=db_); time.sleep(0.03)
            cl.set_input_volume("Radio audio", vol_db=want); self._radio_db = want
            cl.set_input_mute("FX audio", not (split or ext))
            try: cl.set_input_mute("Media video", not ext)       # (a media FILE's sound always comes through the radio)
            except Exception: pass
            try: cl.set_input_mute("Studio audio", not live)
            except Exception as e: log.warning("Studio audio: %s", e)
            if not split:
                for n in ("Clip A", "Clip B"): cl.set_input_mute(n, True)
            elif self.cur in ("Clip A", "Clip B"): cl.set_input_mute(self.cur, False)   # back to the video: its sound again (it was muted while away)
            self.split = split; self._live_r = live; self._ext_r = ext
        except Exception as e: log.warning("TV audio: %s", e); self.cl = None
    def preload(self, clip_file, cue_start, kf):
        """The next song's clip waits in the free player, paused at the keyframe where it will start (≥ 0.3 s after its
        cue point), so at the song's start it only needs PLAY – no seek at that moment. Test bench 5 Nov: a seek at the
        moment started the picture 166 ms differently from song to song, PLAY of a preloaded clip only 38 ms."""
        if not kf: return
        k = next((x for x in kf if x >= (cue_start or 0) + 0.3), None)
        if k is None or k - (cue_start or 0) > 3: return
        time.sleep(max(0.0, 2.0 - (time.time() - getattr(self, "t_show", 0))))   # the free player is hidden ~1.3 s after a switch
        with self.olock:
            try:
                cl = self.client(); nxt = "Clip A" if self.cur == "Clip B" else "Clip B"
                cl.set_input_settings(nxt, {"local_file": clip_file, "looping": False, "restart_on_activate": False,
                                            "close_when_inactive": False, "hw_decode": CLIP_HW()}, True)
                cl.set_input_mute(nxt, True)
                t_r = time.time(); cl.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                # wait until OBS really opened it (was a fixed 0.25 s: when opening took longer, PAUSE came too early and was
                # ignored – the clip then ran on hidden, 10-14 s ahead at its start, or did not play: 19 Nov, 5x in one hour)
                self._wait_open(cl, nxt); t_open = time.time() - t_r
                # SEEK first, then PAUSE (measured on air 19 Nov: PAUSE then seek – the old order – let OBS 32 play the picture-only
                # copy on while it SAID "paused" in 3 of 5 tries; seek then pause held 7 of 7). Then check it really stands still.
                held = False; tries = 0; st = st2 = None
                while tries < 3 and not held:
                    tries += 1
                    cl.set_media_input_cursor(nxt, int(k * 1000)); time.sleep(0.2)
                    cl.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE"); time.sleep(0.2)
                    st = cl.get_media_input_status(nxt); time.sleep(0.4); st2 = cl.get_media_input_status(nxt)
                    held = (st2.media_state == "OBS_MEDIA_STATE_PAUSED" and st.media_cursor is not None
                            and abs((st2.media_cursor or 0) - st.media_cursor) < 60 and abs(st2.media_cursor / 1000 - k) < 0.6)
                if not held:     # it won't stand still: no preload – the song starts its clip the normal way
                    cl.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP")
                    pre_diag({"ev": "preload_failed", "file": os.path.basename(clip_file), "tries": tries, "cur": st2.media_cursor if st2 else None, "k": k})
                    log.warning("preload: the clip would not stand still – it starts the normal way"); self.pre = None; return
                k = st2.media_cursor / 1000      # where it REALLY waits (OBS lands on a keyframe of its own)
                self.pre = {"file": clip_file, "name": nxt, "k": k, "t": time.time()}
                threading.Thread(target=self.pre_watch, args=(self.pre,), daemon=True).start()
                pre_diag({"ev": "preload", "file": os.path.basename(clip_file), "open_s": round(t_open, 2), "state": st2.media_state, "cur": st2.media_cursor, "k": k, "tries": tries})
            except Exception as e: log.warning("preload: %s", e); self.cl = None; self.pre = None
    def pre_watch(self, pre):
        """Measuring (19 Nov): does the waiting clip stay paused until its song starts? Logs the moment it moves + the scene then."""
        last = None
        while self.pre is pre:
            time.sleep(0.5)
            if self.pre is not pre: return
            try:
                with self.olock: st = self.client().get_media_input_status(pre["name"])
            except Exception: continue
            cur = (st.media_cursor or 0) / 1000
            moved = st.media_state != "OBS_MEDIA_STATE_PAUSED" or abs(cur - pre["k"]) > 0.25
            if moved and last != st.media_state:
                last = st.media_state
                pre_diag({"ev": "moved", "file": os.path.basename(pre["file"]), "after_s": round(time.time() - pre["t"], 1), "state": st.media_state, "cur": st.media_cursor, "k": pre["k"], "scene": self.scene, "cur_player": self.cur})
                log.warning("waiting clip moved %.1f s after preload: %s at %.1f s (scene %s)", time.time() - pre["t"], st.media_state, cur, self.scene)
    def _wait_open(self, cl, name):
        """Wait until OBS really restarted the new file (its status first still shows the old file)."""
        for i in range(60):
            try:
                st = cl.get_media_input_status(name)
                if st.media_state == "OBS_MEDIA_STATE_PLAYING" and (st.media_duration or 0) > 0 and (st.media_cursor or 0) < 1500: return True
                if i == 25 and st.media_state == "OBS_MEDIA_STATE_PLAYING":   # same file as before: OBS ignores RESTART
                    cl.set_media_input_cursor(name, 0)
            except Exception: pass
            time.sleep(0.05)
        return True
    LEVEL_OK = set()
    def clip_level(self, cl, name, gain):
        """A paired video's own sound: its level is set FIRST in the chain (an OBS "Level" gain filter before the compressor
        and limiter), the volume slider stays at 0 dB. Before, the slider came after the filters: a very loud video hit the
        compressor at full force and then was turned down - it ended up much quieter on TV than a quiet one (12 Nov)."""
        g = max(-30.0, min(30.0, gain))
        if name not in self.LEVEL_OK:
            fl = cl.get_source_filter_list(name).filters
            if not any(f["filterName"] == "Level" for f in fl): cl.create_source_filter(name, "Level", "gain_filter", {"db": g})
            cl.set_source_filter_index(name, "Level", 0); self.LEVEL_OK.add(name)
        cl.set_source_filter_settings(name, "Level", {"db": g}, True)
        cl.set_input_volume(name, vol_db=0.0)
    def show(self, mode, clip_file, cue_start, sound=False, gain=0.0, kf=None, t0=None):
        """Switch the TV for a new song.
        - sound=True (paired song): the clip plays with its own sound, from its own cue point.
        - otherwise the clip is picture only and must match the radio: when the song starts later in the file (cue point),
          we jump to the next keyframe and start it exactly when the radio gets there (the previous picture stays up till then)."""
        self.t_show = time.time(); t0 = t0 or self.t_show      # t0: when the radio started the song (a TV block: the station's start)
        with self.olock:
          try:
            cl = self.client(); self.gen += 1; gen = self.gen
            if mode in ("clip", "pair", "jingle-video") and clip_file:
                nxt = "Clip A" if self.cur == "Clip B" else "Clip B"; old = self.cur
                # preloaded (preload(): this clip waits paused at its keyframe in this player): only PLAY at the moment
                pre = getattr(self, "pre", None); self.pre = None; self.pre_last = pre
                use_pre = bool(not sound and pre and pre["file"] == clip_file and pre["name"] == nxt)
                if use_pre:      # it must still wait paused on its spot – else the normal start (a moved / ended clip = wrong or black picture)
                    try:
                        stp = cl.get_media_input_status(nxt)
                        if stp.media_state != "OBS_MEDIA_STATE_PAUSED" or abs((stp.media_cursor or 0) / 1000 - pre["k"]) > 0.3:
                            use_pre = False; log.warning("preloaded clip did not wait (%s at %.1f s, not %.1f s) – started the normal way", stp.media_state, (stp.media_cursor or 0) / 1000, pre["k"])
                    except Exception: use_pre = False
                if use_pre:
                    when = (pre["k"] - (cue_start or 0)) + AV_LAG - (time.time() - t0)   # the radio reaches the keyframe in OBS then
                    if when < -0.05: use_pre = False; log.info("preloaded clip too late – started the normal way")
                if not use_pre:
                  cl.set_input_settings(nxt, {"local_file": clip_file, "looping": False, "restart_on_activate": False,
                                              "close_when_inactive": False, "hw_decode": CLIP_HW()}, True)
                cl.set_input_mute(nxt, True)          # (its own sound comes on in activate(), once it is at its cue point)
                if sound: self.clip_level(cl, nxt, gain)
                if not use_pre:
                    cl.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                    cl.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY")   # never left paused
                delay = 0.0; seek_k = None; play_k = None
                if use_pre: play_k = pre["k"]; delay = max(0.0, when)
                if not use_pre and not (cue_start and cue_start > 0.3):
                    try:
                        st0 = cl.get_media_input_status(nxt)
                        if (st0.media_cursor or 0) > 3000 and st0.media_state == "OBS_MEDIA_STATE_PLAYING": time.sleep(0.3)
                        if (cl.get_media_input_status(nxt).media_cursor or 0) > 3000: cl.set_media_input_cursor(nxt, 0)
                    except Exception: pass
                if not use_pre and not sound and not (cue_start and cue_start > 0.3):
                    # picture-only clip from the top: the radio's sound reaches OBS ~AV_LAG later (Liquidsoap + Pulse),
                    # so the picture waits that long (the previous picture stays up) and starts at 0 with it
                    seek_k = 0.0; delay = max(0.0, AV_LAG - (time.time() - t0))
                if not use_pre and cue_start and cue_start > 0.3 and self._wait_open(cl, nxt):
                    now_pos = cue_start + (time.time() - t0) - (0.0 if sound else AV_LAG)   # where the sound in OBS is now
                    k = next((x for x in kf if x >= now_pos + 0.3), None) if (kf and not sound) else None
                    if k is not None and k - now_pos < 12:   # jump to that keyframe the moment the radio gets there
                        seek_k = k; delay = max(0.0, k - now_pos - 0.12)
                    else:                                  # paired (own sound) or no keyframe list: start right away
                        cl.set_media_input_cursor(nxt, int(now_pos * 1000))
                        # OBS sometimes ignores a seek right after opening a file: the clip would show from its very start.
                        # The old picture stays up until the new clip is really at its cue point (max ~2 s, seek again every 0.4 s).
                        t_try = time.time(); n_try = 0
                        while time.time() - t_try < 2.0:
                            time.sleep(0.1)
                            try: cur_ = (cl.get_media_input_status(nxt).media_cursor or 0) / 1000
                            except Exception: break
                            want_ = cue_start + (time.time() - t0) - (0.0 if sound else AV_LAG)
                            if want_ - 0.8 <= cur_ <= want_ + 2.0: break
                            if time.time() - t_try > 0.4 * (n_try + 1):
                                n_try += 1; cl.set_media_input_cursor(nxt, int(want_ * 1000))
                        if n_try: log.info("clip seek needed %d more tries (OBS ignored it)", n_try)
                        def recheck(nxt=nxt, t0=t0, cs=cue_start, g=gen):
                            with self.olock:
                                if g != self.gen: return
                                try:
                                    cl3 = self.client(); want = cs + (time.time() - t0)
                                    if (cl3.get_media_input_status(nxt).media_cursor or 0) < (want - 1.5) * 1000:
                                        cl3.set_media_input_cursor(nxt, int(want * 1000)); log.info("clip seek repeated (OBS ignored it)")
                                except Exception: pass
                        threading.Timer(0.7, recheck).start()
                self.cur = nxt; scene = "Music video"
                self.clip_sound = bool(sound)
                def activate(nxt=nxt, old=old, seek_k=seek_k, sound=sound, play_k=play_k):
                    with self.olock:
                        if gen != self.gen: return          # a newer song already took over
                        try:
                            cl2 = self.client()
                            if sound and not getattr(self, "live", False): cl2.set_input_mute(nxt, False)
                            if play_k is not None:       # preloaded: it waits paused at play_k – start it, no seek
                                try: st_d = cl2.get_media_input_status(nxt); pre_diag({"ev": "start", "file": os.path.basename((self.pre_last or {}).get("file", "")) if getattr(self, "pre_last", None) else "", "state": st_d.media_state, "cur": st_d.media_cursor, "k": play_k})
                                except Exception: pass
                                # 19 Nov: some preloaded clips had NOT stayed paused (10-14 s ahead) or did not start on PLAY (a black
                                # sweeper): check the waiting spot first, and look again after 0.5 s and 1.5 s
                                try:
                                    st0 = cl2.get_media_input_status(nxt)
                                    if abs((st0.media_cursor or 0) / 1000 - play_k) > 0.3:
                                        cl2.set_media_input_cursor(nxt, int(play_k * 1000)); log.info("preloaded clip had moved (%.1f s) – back to its start first", (st0.media_cursor or 0) / 1000 - play_k)
                                except Exception: pass
                                cl2.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY")
                                t_pl = time.time()
                                def verify_pre(nxt=nxt, k0=play_k, t1=t_pl, g=gen):
                                    with self.olock:
                                        if g != self.gen: return
                                        try:
                                            cl4 = self.client(); want = k0 + (time.time() - t1)
                                            st_ = cl4.get_media_input_status(nxt); cur = (st_.media_cursor or 0) / 1000
                                            if st_.media_state != "OBS_MEDIA_STATE_PLAYING":
                                                cl4.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY"); log.info("preloaded clip did not play (%s) – started again", st_.media_state)
                                            if abs(cur - want) > 0.5:
                                                cl4.set_media_input_cursor(nxt, int((want + 0.12) * 1000)); log.info("preloaded clip was %.1f s off – put back", cur - want)
                                        except Exception: pass
                                threading.Timer(0.5, verify_pre).start(); threading.Timer(1.5, verify_pre).start()   # LIVE: only the studio mix (the music is in it already) – never the clip's own sound on top
                            if seek_k is not None:
                                try: cl2.set_media_input_cursor(nxt, int(seek_k * 1000))
                                except Exception as e:      # OBS: "the media must be playing" (the clip was stopped) – start it, seek again; never leave the TV black
                                    log.warning("clip seek: %s – restarting the clip", e)
                                    try:
                                        cl2.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY")
                                        time.sleep(0.15); cl2.set_media_input_cursor(nxt, int(seek_k * 1000))
                                    except Exception as e2: log.warning("clip seek again: %s", e2)
                                t_seek = time.time()
                                def verify(nxt=nxt, k0=seek_k, t1=t_seek, g=gen):
                                    # OBS sometimes ignores a seek (the picture then runs late/early for the whole song):
                                    # check where the clip really is and put it right once more
                                    with self.olock:
                                        if g != self.gen: return
                                        try:
                                            cl4 = self.client(); want = k0 + (time.time() - t1)
                                            cur = (cl4.get_media_input_status(nxt).media_cursor or 0) / 1000
                                            if abs(cur - want) > 0.35:
                                                cl4.set_media_input_cursor(nxt, int((want + 0.12) * 1000))
                                                log.info("clip was %.1f s off – put back in sync", cur - want)
                                        except Exception: pass
                                threading.Timer(1.2, verify).start()
                            hides = []
                            for sc in ("Music video", "Clips window"):
                                try:
                                    sid = cl2.get_scene_item_id(sc, nxt).scene_item_id; oid = cl2.get_scene_item_id(sc, old).scene_item_id
                                    cl2.set_scene_item_index(sc, sid, len(cl2.get_scene_item_list(sc).scene_items) - 1 if sc == "Clips window" else 1)
                                    cl2.set_scene_item_enabled(sc, sid, True); hides.append((sc, oid))
                                except Exception as e: log.warning("clip in %s: %s", sc, e)
                            try:   # a clip in the camera scenes' video window: only the video, on black (no Hype Screen behind it)
                                cl2.set_scene_item_enabled("Clips window", cl2.get_scene_item_id("Clips window", "Clips black").scene_item_id, True)
                            except Exception: pass
                            if self.auto: self.set_scene(scene)
                            else: self.route_audio()
                            def done(h=hides, o=old, n=nxt, g=gen):
                                # hide the old clip the moment the new one really shows frames (max 0.8 s), so you never see
                                # the old video around / under the new one (different picture sizes)
                                t_end = time.time() + 0.8; c0 = None
                                while time.time() < t_end:
                                    try:
                                        with self.olock: cur = self.client().get_media_input_status(n).media_cursor or 0
                                    except Exception: break
                                    if c0 is None: c0 = cur
                                    elif cur > c0 + 70: break        # ~2 frames played
                                    time.sleep(0.03)
                                with self.olock:
                                    for sc_, oid in h: self._hide(oid, sc_)
                                    try: self.client().set_input_mute(o, True)
                                    except Exception: pass
                            threading.Thread(target=done, daemon=True).start()
                        except Exception as e: log.warning("OBS activate: %s", e); self.cl = None
                if delay > 0.05:
                    log.info("clip starts on its keyframe in %.1f s (in sync with the radio)", delay)
                    threading.Timer(delay, activate).start()
                else: activate()
                return scene
            elif mode == "jingle":
                scene = "Branding" if self.branding_loop() else "Hype Screen"; self._clear_window()
            else:
                scene = "Hype Screen"; self._clear_window()
            self.clip_sound = False
            if self.auto: self.set_scene(scene)
            else: self.route_audio()
            return scene
          except Exception as e:
            log.warning("OBS: %s", e); self.cl = None; return "?"
    def _hide(self, oid, sc="Music video"):
        try: self.client().set_scene_item_enabled(sc, oid, False)
        except Exception: pass
    def _clear_window(self):
        """No clip: the video window in the camera scenes shows the Hype Screen."""
        try:
            cl = self.client()
            for n in ("Clip A", "Clip B", "Clips black"):
                try: cl.set_scene_item_enabled("Clips window", cl.get_scene_item_id("Clips window", n).scene_item_id, False)
                except Exception: pass
        except Exception: pass
    def show_changed(self, title):
        """New show: play its intro full-screen once on TV (all scenes), then the branding loop follows the show."""
        self.loop_file = None
        if self.scene == "Branding": self.branding_loop()
        return   # Gianno: no full-screen intro at a show change – the intro is only the Branding loop
        f = show_intro(title)
        if not f or os.path.basename(f) == "default.mp4" or not self.first_show_seen: self.first_show_seen = True; return
        try:
            cl = self.client()
            cl.set_input_settings("Show intro", {"local_file": f, "looping": False, "hw_decode": True}, True)
            cl.trigger_media_input_action("Show intro", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
            ids = []
            for sc in self.SCENES:
                try:
                    sid = cl.get_scene_item_id(sc, "Show intro").scene_item_id; cl.set_scene_item_enabled(sc, sid, True); ids.append((sc, sid))
                except Exception: pass
            def hide():
                for sc, sid in ids:
                    try: self.client().set_scene_item_enabled(sc, sid, False)
                    except Exception: pass
            threading.Timer(4.3, hide).start()
            log.info("show intro: %s", os.path.basename(f))
        except Exception as e: log.warning("show intro: %s", e); self.cl = None
    def branding_loop(self):
        st_ = "/srv/hype/branding/stations/loop_%s.mp4" % TVF["ch"] if (TVF.get("ch") and not CH) else None   # a TV block: the station's loop
        f = st_ if st_ and os.path.isfile(st_) else show_intro(SHOW.get("title"))
        if not f: return None
        try:
          with self.olock:     # (also called from the show watcher thread: one OBS request at a time)
            cur = (self.client().get_input_settings("Show loop").input_settings or {}).get("local_file")   # what OBS really has (also after an OBS restart)
            try: mt = os.path.getmtime(f)
            except OSError: mt = 0
            if os.path.normpath(cur or "") != f or getattr(self, "_loop_mt", None) != (f, mt):
                # same name, new file (uploaded again): OBS only reopens a file when the path text changes –
                # "…/intros/./Intro_x.mp4" is the same file under another spelling
                tgt = f if cur != f else os.path.join(os.path.dirname(f), ".", os.path.basename(f))
                self.client().set_input_settings("Show loop", {"local_file": tgt, "looping": True, "hw_decode": True}, True)
                self.client().trigger_media_input_action("Show loop", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
            self._loop_mt = (f, mt)
            self.loop_file = f
            return f
        except Exception as e: log.warning("branding: %s", e); return None
    def screenshot(self, width=640):
      with self.olock:
        r = self.client().get_source_screenshot(self.scene or "Hype Screen", "jpg", width, int(width * 9 / 16), 70)
        return base64.b64decode(r.image_data.split(",", 1)[1])

class _Nop:
    """Channel mode: no TV / studio. Every method is a no-op; the data the engine reads has a plain value."""
    def __getattr__(self, k):
        if k.startswith("__"): raise AttributeError(k)
        return lambda *a, **kw: None
class NullTV(_Nop):
    def __init__(self):
        self.auto = True; self.scene = None; self.hold_song = False; self.cl = None; self.split = None; self.live = False
        self.first_show_seen = True; self.clip_sound = False; self.ll_on = False; self.olock = threading.RLock(); self.gen = 0
        self.cur = "Clip B"; self.prog = None; self.pip = ""
    def client(self): raise RuntimeError("no TV on a theme station")
class NullLive(_Nop):
    def __init__(self):
        self.live = False; self.st = {"cams": {}, "lip_sync": 0, "auto_record": False}; self.air = "automation"; self.rec = None
        self.song_scene = lambda: "Hype Screen"; self.on_audio = None; self.live_since = None
    def split(self, *a, **kw): return False
    def status(self, *a, **kw): return {"live": False, "air": "automation"}
class Engine:
    def __init__(self):
        self.liq = Liq(LIQ_PORT); self.picker = Picker(); self.tv = NullTV() if CH else TV()
        try: rs = json.load(open(CLOCK_POS))        # after a restart: the hour clock goes on where the radio was, not from slot 1
        except Exception: rs = None
        self.runner = planner.ClockRunner(restore=rs)
        c0 = dbc(); planner.init(c0); planner.default_setup(c0); planner.init_overlays(c0); planner.init_shows(c0)
        planner.init_pads(c0); planner.init_screen(c0); self.screen = Screen()
        try: c0.execute("ALTER TABLE items ADD COLUMN mix_set INT DEFAULT 0"); c0.commit()    # 1 = MIX set by hand (smart mix leaves it alone)
        except sqlite3.OperationalError: pass
        if not CH: c0.execute("UPDATE items SET mtime=0 WHERE (folder LIKE 'MIXES%' OR folder LIKE 'RECORDINGS%') AND problem LIKE 'no artist%'"); c0.commit()   # mixes need no artist: index again
        self.live = NullLive() if CH else live.Live(self.liq, self.tv, lambda: SHOW.get("title") or "show")
        if not CH:      # LIVE on a theme station (Broadcast → station): its radio engine, its studio input port
            self.live.liq_for = lambda cid: Liq(int(chancfg.get(cid)["telnet"]))
            self.live.port_for = lambda cid: chancfg.studio_port(cid) if tv_station_ok(cid) else None
            self.live.name_for = lambda cid: (chancfg.get(cid) or {}).get("name")
            t_ = self.live.target()
            try:
                os.makedirs("/run/hype", exist_ok=True)
                with open("/run/hype/studio_port", "w") as f_: f_.write(str(chancfg.studio_port(t_) if t_ else 8005))
            except OSError: pass
            if t_: self.live.live = self.live.liq_live()
        self.tv.live = self.live.live; self.tv.adopt_audio()
        self.live.song_scene = lambda: tv_scene()     # the automatic scene for what plays now (studio drop-out)
        self.decks = {"1": None, "2": None}; self.tv_deck = None; self._air_lost = None
        try: self.overlap = bool(json.load(open(DECKS_FILE)).get("overlap"))
        except Exception: self.overlap = False
        for k_ in ("1", "2"):
            try: self.liq.cmd(f"deck.stop {k_}"); self.liq.cmd(f"var.set deck_v{k_} = 1.0")    # (after an engine restart: no forgotten deck)
            except Exception: pass
        threading.Thread(target=self.deck_watch, daemon=True).start()
        load_rotation()
        global MIX_OUT
        try: MIX_OUT = float(self.liq.cmd("var.get mix_out").split()[-1])
        except Exception: pass
        try: self.liq.cmd("var.set duck_lvl = 1.0")   # never start with the music dipped
        except Exception: pass
        self.ov_last = 0; self.ov_count = {}; self.ov_recent = []
        self.idi_times = []; self.idi_used = {}      # IDs over soft intros: when they played (max per hour / minutes apart), which ID
        try:      # after a restart: the IDs / sweepers over intros of the last hour still count (9 Nov: an ID after every restart)
            rows = dbc().execute("SELECT started, over FROM asrun WHERE over IS NOT NULL AND started > ? ORDER BY started", (time.time() - 3600,)).fetchall()
            self.idi_times = [r[0] for r in rows if (r[1] or "").startswith("ID:")]
            if rows: self.ov_last = rows[-1][0]
        except Exception: pass
        self.last_kind = None
        try:      # after a restart: what played last (else an ID over the next intro is skipped – Bleeding, 9 Nov)
            lp = (dbc().execute("SELECT path FROM asrun ORDER BY id DESC LIMIT 1").fetchone() or [""])[0] or ""
            self.last_kind = "music" if lp.split("/")[0].startswith("music") else "jingle" if lp.split("/")[0].startswith("jingles") else None
        except Exception: pass
        self.missing = {}    # path -> when push_next found it missing / unplayable (not planned again for 10 min)
        self.plan = []; self.liq_next = None; self.now = {}; self.auto = True; self.stop_at_end = False; self.stopped = False
        self.snaps = []      # (item id, playlist positions before it was planned) for the planned items not on air yet
        try:      # what played in the last hours (title / artist separation) – the picker's memory was empty after every restart
            for t_, p_, a_ in c0.execute("SELECT started, path, artist FROM asrun WHERE started > ? AND path IS NOT NULL ORDER BY started", (time.time() - TITLE_SEP,)):
                self.picker.recent.append((t_, p_, artist_key(a_ or "")))
        except Exception as e: log.warning("recent plays: %s", e)
        kept = self.restore_plan(c0)
        if kept: pass        # Up next is back exactly as it was: its playlist positions stay used (snaps loaded with it)
        else:
          try:   # after a restart: what was planned but never played gives its songs back to the playlists
            for _id, snap in reversed(json.load(open(SNAPS_FILE))): planner.restore_states(c0, snap)
            os.remove(SNAPS_FILE); log.info("playlists: positions of the unplayed Up next given back")
          except FileNotFoundError: pass
          except Exception as e: log.warning("snaps: %s", e)
          try:   # the songs you had put in Up next come back
            for p_ in json.load(open(MINE_FILE)):
                if c0.execute("SELECT 1 FROM items WHERE path=? AND problem IS NULL", (p_.get("path"),)).fetchone(): self.plan.append(p_)
            if self.plan: log.info("Up next: %d song(s) you added are back after the restart", len(self.plan))
          except FileNotFoundError: pass
          except Exception as e: log.warning("up next (yours): %s", e)
        try: self.aux = json.load(open(SP("/var/lib/hype/aux.json")))
        except Exception: self.aux = {"1": [], "2": []}
        self.dj = getattr(self, "dj", False); self.deck = getattr(self, "deck", None); self.faders = {}; self.parked = None
        self.sgroups = planner.get_setting(c0, "scene_groups", [])
        self.layouts = {t: planner.get_setting(c0, t + "_layout", None) for t in ("cam", "pip", "media")}
        try:   # after a restart: the song that is still playing
            c = dbc(); r = c.execute("SELECT path, started, scene FROM asrun ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                it = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (r[0],)).fetchone()
                if it and time.time() - r[1] < (it[6] or 0):
                    self.now = dict(row_item(it, "?"), started=r[1], scene=r[2]); self.tv.scene = r[2]
                    a_ = getattr(self, "_aired", None) or {}
                    if a_.get("path") == r[0]: self.now.update({k_: a_[k_] for k_ in ("id", "src", "deck", "id_over", "event") if k_ in a_})   # (its deck / source survive the restart)
        except Exception as e: log.warning("restore now: %s", e)
    def restore_plan(self, c):
        """After an engine restart (update, repair): Up next comes back exactly as it was - same songs, same order - when it
        was saved less than 20 min ago. The queued song (Liquidsoap is cleared at start) goes back to the top, unless it
        already went on air. The clock goes on after the last planned item."""
        try: d = json.load(open(PLAN_FILE))
        except Exception: return False
        if time.time() - d.get("t", 0) > 20 * 60: return False
        # AUTO / MANUAL / DJ stay as they were (a restart never hands the air back to the clock by itself)
        if "auto" in d: self.auto = bool(d["auto"]); self.dj = bool(d.get("dj")); self.deck = d.get("deck")
        if d.get("now"): self._aired = d["now"]       # the song on air: its deck / source (merged when "now" is restored)
        items = ([d["next"]] if d.get("next") else []) + list(d.get("plan") or [])
        try:
            last = c.execute("SELECT path, started FROM asrun ORDER BY id DESC LIMIT 1").fetchone()
            if items and last and items[0].get("path") == last[0] and last[1] >= (items[0].get("pushed") or 0) - 2: self._aired = items.pop(0)   # it went on air
        except Exception: pass
        items = [i for i in items if i.get("path") and os.path.isfile(os.path.join(ROOT, i["path"]))]
        if not items: return False
        for i in items:
            i.pop("pushed", None)
            if isinstance(i.get("event"), list): i["event"] = tuple(i["event"])     # (JSON made it a list: it is compared / hashed as a tuple)
        self.plan = items
        for i in items: self.picker.note(i)          # (22 Nov: restored songs were not "recent" – after a replan they could be planned again)
        try: self.snaps = [tuple(x) for x in json.load(open(SNAPS_FILE))]
        except Exception: self.snaps = []
        pos = next((i["pos"] for i in reversed(items) if i.get("pos")), None)
        if pos: self.runner.restore = pos
        log.info("Up next: back as it was before the restart (%d items)", len(items))
        return True
    def save_plan(self):
        nw = {k_: self.now.get(k_) for k_ in ("path", "id", "src", "deck", "id_over", "event") if self.now.get(k_) is not None} if self.now else None
        sig = [i.get("id") for i in ([self.liq_next] if self.liq_next else []) + self.plan] + [self.auto, self.dj, self.deck, (nw or {}).get("path")]
        if sig == getattr(self, "_plan_sig", None) and time.time() - getattr(self, "_plan_t", 0) < 300: return
        self._plan_sig = sig; self._plan_t = time.time()
        try: jdump({"t": time.time(), "next": self.liq_next, "plan": self.plan, "now": nw, "auto": self.auto, "dj": self.dj, "deck": self.deck}, PLAN_FILE)
        except Exception as e: log.warning("save up next: %s", e)
    # ---------- planning ----------
    def avoid(self):
        items = self.plan + ([self.liq_next] if self.liq_next else [])
        now = time.time(); self.missing = {p_: t_ for p_, t_ in self.missing.items() if now - t_ < 600}
        return {"paths": {i["path"] for i in items} | set(self.missing), "artists": {a for i in items[-4:] for a in artist_key(i["artist"])}}
    @staticmethod
    def length(it):
        sm = smart_mix(it) if it.get("section") in ("music", "jingles", "ads") else None
        if sm: return max(1.0, sm[0] - (it.get("cue_start") or 0))
        return max(1.0, (it.get("cue_mix") or it.get("cue_end") or it.get("duration") or 180) - (it.get("cue_start") or 0))
    def cursor(self):
        """Estimated start time of the next item to plan."""
        n = self.now; t = time.time()
        if n: t = max(t, n["started"] + self.length(n))
        for it in ([self.liq_next] if self.liq_next else []) + self.plan: t += self.length(it)
        return t
    def dj_take(self):
        """DJ mode: the next song comes from the other deck (A = AUX 1, B = AUX 2); one deck empty → the same deck."""
        last = (self.plan[-1] if self.plan else self.liq_next or self.now or {}).get("deck") or self.deck or "1"
        other = "2" if last == "1" else "1"
        for k in (last, other):     # a deck plays its AUX like a playlist; when it runs out, the other deck takes over
            if self.aux.get(k):
                it = self.aux[k].pop(0); self.aux_save_file()
                return dict(it, id=uuid.uuid4().hex[:10], src="dj", deck=k)
        return None
    def aux_take(self):
        """MANUAL: AUX 1 plays top to bottom, then AUX 2."""
        for k in ("1", "2"):
            if self.aux.get(k):
                it = self.aux[k].pop(0); self.aux_save_file()
                return dict(it, id=uuid.uuid4().hex[:10], src="aux", deck=k)
        return None
    def aux_back(self):
        """Songs taken from an AUX but not played yet go back on top of their AUX (keeps your order)."""
        self.take_back_next()
        back = [p_ for p_ in self.plan if p_.get("src") in ("aux", "dj")]
        for b in reversed(back): self.aux.setdefault(b.get("deck") or "1", []).insert(0, b)
        self.plan = [p_ for p_ in self.plan if p_.get("src") not in ("aux", "dj")]
        if back: self.aux_save_file()
    def give_back(self, c):
        """Planned clock items that were dropped (replan, new hour, DJ/MANUAL…) never played: their playlists go back to
        where they were, so a rotation or ordered playlist really continues (nothing is skipped)."""
        present = {p_.get("id") for p_ in ([self.liq_next] if self.liq_next else []) + self.plan}
        n = 0
        while self.snaps and self.snaps[-1][0] not in present:
            planner.restore_states(c, self.snaps.pop()[1]); n += 1
        self.snaps = [s_ for s_ in self.snaps if s_[0] in present]     # dropped in the middle (later ones kept): just forget
        if n: log.info("playlists: %d dropped item(s) gave their place back", n)
    def save_snaps(self):
        try: json.dump(self.snaps, open(SNAPS_FILE + ".tmp", "w")); os.replace(SNAPS_FILE + ".tmp", SNAPS_FILE)
        except Exception as e: log.warning("snaps: %s", e)
        try:   # what YOU put in Up next survives a restart too
            mine = [{k: v for k, v in p_.items() if k not in ("snap",)} for p_ in self.plan if p_.get("src") == "you"]
            json.dump(mine, open(MINE_FILE + ".tmp", "w")); os.replace(MINE_FILE + ".tmp", MINE_FILE)
        except Exception as e: log.warning("up next (yours): %s", e)
    def refill(self, c):
        try: self.give_back(c)
        except Exception as e: log.warning("give back: %s", e)
        planner.CTX["ad_planned"] = {}        # spots already in Up next count as played (X per day stays right)
        for p_ in ([self.liq_next] if self.liq_next else []) + self.plan:
            if p_.get("section") == "ads": planner.CTX["ad_planned"][p_["path"]] = planner.CTX["ad_planned"].get(p_["path"], 0) + 1
        if not self.auto:
            left = ((self.now.get("started") or 0) + self.length(self.now) - time.time()) if self.now.get("path") else 0
            if not self.plan and not self.liq_next and left <= PUSH_LEAD + 2:      # DJ / MANUAL: the AUX keeps its songs (visible, movable) until the next one is really needed (one at a time)
                it = self.dj_take() if self.dj else self.aux_take()
                if not it:   # nothing in the AUX/decks: the radio never stops – the clock keeps the music going
                    it = self.picker.next_planned(c, self.avoid(), self.cursor(), self.runner) or self.picker.next(c, self.avoid())
                    if it: self.picker.note(it)
                if it: self.plan.append(it)
            return
        while self.auto and len(self.plan) < PLAN_LEN:
            try:
                if self.plan_one(c) == "stop": break
            except Exception as e:      # a broken slot / event never stops the radio: log it, stop planning for this tick (push_next still runs)
                log.warning("planning: %s", e); break
            guard = getattr(self, "_guard", 0) + 1; self._guard = guard
            if guard > 60: self._guard = 0; break          # (jingles skipped many times in a row: stop for this tick)
        self._guard = 0
        self.save_snaps()
    def plan_one(self, c):
        """Plan one more item at the end of Up next (refill's loop body). "stop" = nothing to plan."""
        if self.hour_events(c, self.cursor()): return      # a new hour starts with its :00 items (Top of the hour), then its clock
        self.runner.snap = None; self.runner.pos = None; self.runner.fixed = False
        hf = getattr(self, "hour_floor", 0)
        if hf and self.cursor() < hf and not any(p_.get("hour_T") == hf for p_ in self.plan + ([self.liq_next] if self.liq_next else [])):
            self.hour_floor = hf = 0      # its top of the hour isn't planned (any more): plan with the clock of the hour we are in
        at = max(self.cursor(), hf)
        ph = self.pending_hour(c, at)
        # the TOH of this hour hasn't played yet: keep planning with the old hour's clock - unless that TOH is already
        # in Up next (hour_floor): then what comes after it is the new hour's clock from slot 1, and it stays at the TOH
        if ph and hf != ph: at = ph - 1
        if self.runner.mix_key != planner.runner_key(at) and self.mix_in_run(at): self.runner.mix_key = planner.runner_key(at)
        planner.CTX["touched"] = {}
        try: it = self.picker.next_planned(c, self.avoid(), at, self.runner)
        finally: touched = planner.CTX.pop("touched", None)
        if it and touched: self.snaps.append((it["id"], {str(k): v for k, v in touched.items()}))
        if it and self.runner.snap: it["snap"] = self.runner.snap
        if it and self.runner.pos: it["pos"] = self.runner.pos
        if it and getattr(self.runner, "fixed", False):
            it["fixed"] = True     # from a fixed-order playlist (Top 10 countdown)
            pid_ = getattr(self.runner, "fixed_pid", None)
            if pid_ and (c.execute("SELECT COALESCE(chart,0) FROM playlists WHERE id=?", (pid_,)).fetchone() or [0])[0]: it["chart_pid"] = pid_
        if it: it["hour_k"] = list(planner.runner_key(at))    # which clock run (hour / show) picked it - kept at the top of the hour
        if not it: it = self.picker.next(c, self.avoid())     # nothing in the clocks: built-in test clock
        if not it: return "stop"
        self.picker.note(it); self.place(it)
    def pending_hour(self, c, at):
        """Start of the hour `at` falls in, when that hour begins with a hard :00 item (Top of the hour) that hasn't fired yet."""
        H0 = int(at // 3600) * 3600
        if time.time() >= H0 + 2: return None
        for e in planner.due_events(c, H0 - 1, H0 + 1):
            if e["hard"] and str(e["at"]).endswith(":00") and abs(e["t"] - H0) < 2: return H0
        return None
    @staticmethod
    def is_jingle(it): return bool(it) and it.get("section") == "jingles"
    def last_item(self):
        return self.plan[-1] if self.plan else (self.liq_next or (self.now if self.now and self.now.get("path") else None))
    def hour_events(self, c, at):
        """The moment Up next crosses into a new hour, that hour's :00 fixed items (Top of the hour …) go in first;
        the clock of the new hour then starts from its first slot right after them (the runner restarts per hour)."""
        last = self.plan[-1] if self.plan else (self.liq_next or (self.now if self.now.get("path") else None))
        if not last: return False
        H = int(at // 3600)
        if int((at - self.length(last)) // 3600) != H: T = H * 3600       # Up next just crossed into a new hour
        elif int((at + 5) // 3600) != H: T = (H + 1) * 3600               # ends 0-5 s before :00 (still fading out): the hour starts there
        else: return False
        placed = False
        for e in planner.due_events(c, T - 1, T + 1):
            if e["hard"] or not str(e["at"]).endswith(":00"): continue
            ek = (e["id"], e["day"])
            if any(p_.get("event") == ek for p_ in self.plan + ([self.liq_next] if self.liq_next else [])): continue
            items = self.event_items(c, e)
            for it in items: it["event"] = ek; self.plan.append(it)
            if items and not placed: items[0]["hour_T"] = T          # push_next keeps it from starting before HH:59
            if items: placed = True; log.info("hour %02d:00 starts with %s", time.localtime(T).tm_hour, ", ".join(i["title"] for i in items))
        if placed: self.hour_floor = T      # what comes after it is planned in the NEW hour's clock (from its first slot)
        return placed
    pad_loops = {}
    @property
    def media(self):
        if not hasattr(self, "_media"): self._media = Media()     # (the Media class is defined further down)
        return self._media
    def pad_loop(self, r, duck, level):
        """LOOP on: the pad repeats until you click it again / STOP PADS. One copy is always queued ahead, so it repeats without a gap."""
        pi = row_item(r, "pad"); path = pi["path"]; token = uuid.uuid4().hex
        ln = max(0.5, (pi.get("cue_end") or pi.get("duration") or 5) - (pi.get("cue_start") or 0))
        self.pad_loops[path] = token
        def run():
            self.liq.cmd("pads.push " + annotate(pi, True)); self.liq.cmd("pads.push " + annotate(pi, True))
            while self.pad_loops.get(path) == token:
                if duck: self.duck_for(ln + 0.3, level)
                t_end = time.time() + ln
                while time.time() < t_end and self.pad_loops.get(path) == token: time.sleep(0.05)
                if self.pad_loops.get(path) != token: break
                self.liq.cmd("pads.push " + annotate(pi, True))
        threading.Thread(target=run, daemon=True).start()
    def pad_video(self, c, pi, loop=False):
        """The pad's video full screen over the scene on air (OBS scene 'Pad video layer' sits in every scene).
        Gone when the pad ends (or with loop: when you stop it)."""
        if not pi.get("has_video"): return
        f = tv_file(c, pi["path"]); token = uuid.uuid4().hex; self.padv = token
        st = pi.get("cue_start") or 0; ln = max(0.5, (pi.get("cue_end") or pi.get("duration") or 5) - st)
        tv = self.tv
        try:
            with tv.olock:
                cl = tv.client()
                cl.set_input_settings("Pad video", {"is_local_file": True, "local_file": f, "looping": bool(loop), "restart_on_activate": False,
                                                    "close_when_inactive": False, "clear_on_media_end": True, "hw_decode": CLIP_HW()}, True)
                cl.set_input_mute("Pad video", True)
                cl.trigger_media_input_action("Pad video", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                if st > 0.2: cl.set_media_input_cursor("Pad video", int(st * 1000))
                cl.set_scene_item_enabled("Pad video layer", cl.get_scene_item_id("Pad video layer", "Pad video").scene_item_id, True)
        except Exception as e: log.warning("pad video: %s", e); tv.cl = None; return
        log.info("pad video on: %s%s", pi.get("title"), " (loop)" if loop else "")
        if not loop: threading.Timer(ln, lambda: self.padv == token and self.pad_video_off()).start()
    def pad_video_off(self):
        self.padv = None
        try:
            with self.tv.olock:
                cl = self.tv.client()
                cl.set_scene_item_enabled("Pad video layer", cl.get_scene_item_id("Pad video layer", "Pad video").scene_item_id, False)
                cl.trigger_media_input_action("Pad video", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP")
        except Exception as e: log.warning("pad video off: %s", e); self.tv.cl = None
    def duck_for(self, secs, level=0.35):
        """Dip the music while a ducked pad / intro sweeper plays: glide down (0.3 s), hold, glide back up (0.5 s).
        Done with one level knob (duck_lvl) – no source switching in Liquidsoap, so the music never hiccups."""
        self.duck_until = max(getattr(self, "duck_until", 0), time.time() + secs)
        if getattr(self, "_ducking", False): return
        self._ducking = True
        def run():
            try:
                cur = 1.0
                for i in range(1, 7): cur = 1 - (1 - level) * i / 6; self.liq.cmd(f"var.set duck_lvl = {cur:.3f}"); time.sleep(0.05)
                while time.time() < self.duck_until - 0.25: time.sleep(0.05)
                for i in range(1, 11): self.liq.cmd(f"var.set duck_lvl = {level + (1 - level) * i / 10:.3f}"); time.sleep(0.05)
            except Exception as e: log.warning("duck: %s", e)
            finally:
                try: self.liq.cmd("var.set duck_lvl = 1.0")
                except Exception: pass
                self._ducking = False
        threading.Thread(target=run, daemon=True).start()
    def upnext_show(self, path, dur):
        """Show the Up next PIP now – only over the music video that planned it, scenes on AUTO, not LIVE."""
        n = self.now or {}
        if n.get("path") != path or n.get("mode") not in ("clip", "pair") or self.live.live or self.stopped: return
        if not self.tv.auto or self.tv.scene != "Music video": return
        if re.search(r"top\s*10", SHOW.get("title") or "", re.I): return      # the Hype Top 10 has its own order: no Up next
        if self.now and self.now.get("chart_pid"): return                     # during any chart (Top 40 …): no Up next either
        if len(self.upnext_data()["items"]) < 3: return
        log.info("Up next PIP for %.0f s", dur); self.hold_scene("Up next", dur)
    def upnext_data(self):
        """The next 3 songs (design C: title + artist, no covers; jingles in between are skipped), with their start times."""
        n = self.now or {}; out = []
        t = (n.get("started") or time.time()) + (self.length(n) if n.get("path") else 0)
        for it in ([self.liq_next] if self.liq_next else []) + list(self.plan):
            if len(out) >= 3: break
            if it.get("section") == "music":
                out.append({"title": it.get("title") or "", "artist": it.get("artist") or "", "start": time.strftime("%H:%M", time.localtime(t))})
            t += self.length(it)
        return {"items": out}
    def hold_scene(self, name, hold=0):
        """Switch the TV to a scene (from a scheduled rule); after `hold` seconds back to AUTO SCENES (0 = stays)."""
        with LOCK:
            if not self.tv.auto: log.info("scheduled scene %s skipped: scenes are on manual", name); return   # never over a scene you chose (live show)
            self.tv.auto = False; self.tv.set_scene(name)
        self.hold_until = (time.time() + hold) if hold and hold > 0 else 0     # the graphics page hides the Up next text just BEFORE the scene goes back
        if hold and hold > 0:
            def back(n=name):
                with LOCK:
                    if self.tv.scene == n.split("@")[0] and not self.tv.auto:
                        self.tv.auto = True; self.tv.scene = None
                        if self.now: self.tv.set_scene(tv_scene())
            threading.Timer(hold, back).start()
    def place(self, it):
        """New Music / Throwback sweepers always go RIGHT BEFORE a song of their kind (a clock may list them after it):
        such a sweeper waits until the next matching song (max 4 songs), then plays in front of it – else it is dropped."""
        k = intro_kind(it)
        if k:
            self.held = getattr(self, "held", {}); self.held[k] = (it, 0); return
        held = getattr(self, "held", {})
        last = self.last_item()
        after_toh = bool(last and last.get("event") and "TOH" in (last.get("path") or "").upper())
        if self.is_jingle(it) and self.is_jingle(last) and not after_toh and not it.get("fixed"):   # your own playlist order always wins
            log.info("skipped %s – no two jingles in a row", it.get("title")); return
        if it.get("section") == "music":
            sk = song_kind(it)
            if sk == "throwback" and TB_SWEEPER and sk not in held and it.get("src") == "clock" and not self.tb_done():
                # a throwback song in the clock: a jingle from jingles/THROWBACK goes in front of it (once per clock hour,
                # like the New Music sweeper before the New Music song)
                try:
                    cc = dbc(); jp = planner.resolve_jingle(cc, "THROWBACK", list(self.avoid()["paths"]))
                    r = cc.execute(f"SELECT {COLS} FROM items WHERE path=?", (jp,)).fetchone() if jp else None
                    if r: held[sk] = (row_item(r, "clock"), 0); self.held = held
                except Exception as e: log.warning("throwback sweeper: %s", e)
            if sk and sk in held:
                hj = held.pop(sk)[0]
                if self.is_jingle(last) and not after_toh and self.plan and not last.get("event") and last.get("src") == "clock":
                    self.plan.pop()                      # the New Music / Throwback sweeper takes that jingle's place
                if not (self.is_jingle(self.last_item()) and not after_toh): self.plan.append(hj)
            for k_ in list(held):
                j, n = held[k_]; held[k_] = (j, n + 1)
                if n + 1 > 4: held.pop(k_)
        self.plan.append(it)
    def tb_done(self):
        """Has a Throwback sweeper played / been planned in the clock hour the next item falls in?"""
        at = self.cursor(); k = planner.runner_key(at)
        t = time.time()
        if self.now.get("path"): t = max(t, (self.now.get("started") or t) + self.length(self.now))
        for p_ in ([self.liq_next] if self.liq_next else []) + self.plan:
            if intro_kind(p_) == "throwback" and planner.runner_key(t) == k: return True
            t += self.length(p_)
        try:
            r = dbc().execute("SELECT MAX(started) FROM asrun WHERE folder LIKE 'THROWBACK%' OR (kind='jingles' AND lower(title) LIKE '%throwback%')").fetchone()
            if r and r[0] and planner.runner_key(r[0]) == k: return True
        except Exception: pass
        return False
    def replan(self):
        """Clocks/schedule/playlists changed: rebuild the planned (clock) items, keep yours."""
        self.plan = [p for p in self.plan if p["src"] == "you"]; self.runner = planner.ClockRunner(); self.hour_floor = 0
        self.refill(dbc())
    def push_next(self, c, force=False):
        """Queue the next item in Liquidsoap - only in the last PUSH_LEAD seconds of the song on air (or right away with
        force, for PLAY NOW / MIX / skip). Liquidsoap prepares a queued song at once and it can't be pulled back after
        that, so until then Up next stays fully yours: change, move, remove, nothing plays twice."""
        if self.liq_next or self.stop_at_end or self.stopped: return
        if not force and self.decks_hold(): return          # LIVE · Split: the decks play, the automation waits
        if not force and self.now.get("path") and self.plan:
            left = (self.now.get("started") or 0) + self.length(self.now) - time.time()
            if left > PUSH_LEAD: return                    # the tick (every second) queues it in time
        if getattr(self, "loop", False) and self.now.get("path"):   # LOOP: the song on air comes again
            it = {k: v for k, v in self.now.items() if k not in ("started", "scene")}; it.update(id=uuid.uuid4().hex[:10], src="loop")
            self.liq.cmd("q.push " + annotate(it)); self.liq_next = it; return
        if not self.plan: return
        T = self.plan[0].get("hour_T")
        if T and self.auto and self.now.get("path"):
            start = max(time.time(), self.now["started"] + self.length(self.now))
            if start < T - 900:        # a later hour's top of the hour at the front (should not happen): out, it comes back at its hour
                log.warning("top of the hour for %s was first in Up next %d s early - taken out, planned again at its hour", time.strftime("%H:%M", time.localtime(T)), T - start)
                hk_ = self.plan[0].get("event"); self.plan = [p_ for p_ in self.plan if not (hk_ and p_.get("event") == hk_)]; self.refill(c)
                if not self.plan: return
                if self.plan[0].get("hour_T"):          # still in front: play the first normal item before it
                    j = next((j for j, p_ in enumerate(self.plan) if not p_.get("hour_T") and not p_.get("event")), None)
                    if j is None: return
                    self.plan.insert(0, self.plan.pop(j))
                T = None
            if T and start < T - 5:    # never before :00 – one more song first (picked to end just after :00)
                f = self.filler(c, T - start)
                if f: self.picker.note(f); self.plan.insert(0, f); log.info("top of the hour would start %d s early – %s – %s first", T - start, f["artist"], f["title"])
        it = self.plan[0]
        if BAD_NAME.search(it.get("path") or "") or not os.path.isfile(os.path.join(ROOT, it.get("path") or "")):
            # renamed / deleted since it was planned (or a name Liquidsoap can't take): Liquidsoap would drop it -> dead air
            self.plan.pop(0); self.missing[it.get("path")] = time.time()
            log.warning("skipped %s – the file is missing / its name can't be played: %s", it.get("title"), it.get("path"))
            return self.push_next(c, force)
        try:
            it.pop("id_over", None); idp = self.pick_intro_voice(c, it) or self.pick_intro_id(c, it)
            if idp: it["id_over"] = idp; log.info("%s over the intro of %s: %s", "Voice" if idp.startswith(VOICE_PFX) else "ID", it["title"], idp)
        except Exception as e: log.warning("intro ID: %s", e)
        # push FIRST: only when Liquidsoap really took it, it leaves Up next and its fixed time counts as done
        try: res = self.liq.cmd("q.push " + annotate(it, nxt=self.plan[1] if len(self.plan) > 1 else None))
        except (socket.timeout, TimeoutError) as e:
            # sent, but no answer: it may be queued after all - count it as queued (never twice); if it never starts,
            # recover_stuck() queues the next one
            res = ""; log.warning("queue %s – %s: no answer from Liquidsoap (%s) – counted as queued", it.get("artist"), it.get("title"), e)
        except Exception as e:      # not reachable (restarting …): nothing was sent, it stays first in Up next
            log.warning("queue %s – %s failed (%s): stays first in Up next", it.get("artist"), it.get("title"), e); return
        self.plan.pop(0); self.liq_next = it; it["pushed"] = time.time(); warm(c, it)
        if it.get("mode") in ("clip", "jingle-video") and PRELOAD and not TVF["ch"]:
            try:
                cf, kf_ = tv_file(c, it["path"]), keyframes(c, it["path"])
                threading.Thread(target=self.tv.preload, args=(cf, it.get("cue_start") or 0, kf_), daemon=True).start()
            except Exception as e: log.warning("preload: %s", e)
        if it.get("event"):    # a fixed-time item never goes out twice (a copy of the same event + file left in Up next goes)
            ev_ = tuple(it["event"]); n_ = len(self.plan)
            self.plan = [p_ for p_ in self.plan if not (p_.get("event") and tuple(p_["event"]) == ev_ and p_.get("path") == it.get("path"))]
            if len(self.plan) < n_: log.info("removed %d copy(ies) of %s from Up next", n_ - len(self.plan), it.get("title"))
            try: c.execute("INSERT OR IGNORE INTO events_done VALUES(?,?)", it["event"]); c.commit()
            except Exception as e: log.warning("events_done: %s", e)
            if it.get("hour_T") and self.auto:
                # the top of the hour goes out: the new hour's clock starts at slot 1 right after it
                keep = [p_ for p_ in self.plan if p_.get("event") == it["event"] or p_.get("src") != "clock"]
                k = 0
                while k < len(self.plan) and self.plan[k].get("event") == it["event"]: k += 1
                head = self.plan[:k]; nk = list(planner.runner_key(it["hour_T"] + 1))
                nxt_h = next((j for j in range(k, len(self.plan)) if self.plan[j].get("hour_T")), len(self.plan))   # up to the NEXT top of the hour
                clock_after = [p_ for p_ in self.plan[k:nxt_h] if p_.get("src") == "clock"]
                if clock_after and all(p_.get("hour_k") == nk for p_ in clock_after):
                    # already planned with the new hour's clock (from its slot 1): Up next stays exactly as you saw it
                    self.hour_floor = it["hour_T"]
                    log.info("new hour: top of the hour queued, the %d planned item(s) after it stay (new hour's clock)", len(clock_after))
                else:
                    # (rare: planned before the new hour was known, e.g. right after a restart) plan them again from slot 1
                    # (a later hour's top of the hour is dropped too: it is placed again when Up next reaches that hour)
                    rest = [p_ for p_ in self.plan[k:] if p_.get("src") != "clock" and not p_.get("hour_T") and not (p_.get("event") and p_.get("event") != it["event"] and p_.get("src") == "fixed")]
                    dropped = len(self.plan) - len(head) - len(rest)
                    self.plan = head + rest; self.runner = planner.ClockRunner(); self.held = {}; self.hour_floor = it["hour_T"]
                    log.info("new hour: top of the hour queued, %d planned item(s) re-planned, clock from slot 1", dropped)
        if it.get("snap"):   # a resume clock goes on after the last item that really went out
            try: planner.save_clock_state(c, *it["snap"])
            except Exception as e: log.warning("clock state: %s", e)
        ids = [s_[0] for s_ in self.snaps]
        if it.get("id") in ids: self.snaps = self.snaps[ids.index(it["id"]) + 1:]     # it goes on air: its songs are really used
        self.save_snaps()
        log.info("queued %s – %s (%s) -> %s", it["artist"], it["title"], it["folder"], res.splitlines()[0] if res else "")
    def mix_in_run(self, at):
        """Is a mixtape on air / in Up next that starts in the same clock run (hour or show) as `at`? (after a restart or
        a replan the clock runner is new and does not know it - so a 2nd mix is never planned in the same hour)"""
        k = planner.runner_key(at); is_mix = lambda p_: (p_.get("folder") or "").upper().startswith(("MIXES", "RECORDINGS"))
        if self.now.get("path") and is_mix(self.now) and planner.runner_key(self.now.get("started") or 0) == k: return True
        t = max(time.time(), (self.now.get("started") or 0) + self.length(self.now)) if self.now.get("path") else time.time()
        for p_ in ([self.liq_next] if self.liq_next else []) + self.plan:
            if is_mix(p_) and planner.runner_key(t) == k: return True
            t += self.length(p_)
        return False
    def filler(self, c, gap):
        """A song (same folder as the one on air, else any music) whose length ends closest to the hour."""
        now = time.time(); av = self.avoid(); rec = self.picker.recent
        paths = {p for t, p, a in rec} | av["paths"]
        arts = {a for t, p, aa in rec if now - t < ARTIST_SEP for a in aa} | av["artists"] | set(artist_key(self.now.get("artist") or ""))
        theme = "COALESCE(station, '') = ''"      # never a theme channel's own songs (music-latina/…, music-ambiente/…)
        for q, args in (("folder=?", (self.now.get("folder"),)), (theme, ())):
            rows = c.execute(f"SELECT {COLS} FROM items WHERE section='music' AND problem IS NULL AND folder NOT LIKE 'MIXES%' AND folder NOT LIKE 'RECORDINGS%' AND {q}", args).fetchall()
            cand = [row_item(r, "clock") for r in rows if r[0] not in paths and not (set(artist_key(r[1])) & arts)]
            if cand: return min(cand, key=lambda i: planner.fit_score(plain_len(i), gap))   # cue length: no loudness maps for every song (runs under LOCK, just before :00)
        return None
    # ---------- the two DJ decks for LIVE (Broadcast page → Split) ----------
    # AUX 1 = deck 1 (laptop output 1 → Mixcast USB), AUX 2 = deck 2 (output 2 → Mixcast aux line). Each deck plays one song
    # to its END when you press play, then stops (the next song of that AUX waits on top). The automation waits meanwhile.
    # OVERLAP off: starting a deck fades the other one out. OVERLAP on: both play; the TV follows the deck you started last
    # (when that one ends, the TV goes to the one still playing). Pads + media go to the deck chosen on the Broadcast page.
    def deck_mode(self):
        if not self.live.split(): return False
        return not (self._air_lost and time.time() - self._air_lost > 15)     # the studio signal gone 15 s: the automation again
    def decks_hold(self):
        if not hasattr(self, "decks"): return False      # (still starting)
        return self.deck_mode() or any(self.decks.values())
    def deck_play(self, c, k, iid=None):
        L = self.aux.get(k) or []
        it = next((x for x in L if x.get("id") == iid), None) if iid else (L[0] if L else None)
        if not it: return {"ok": False, "error": "AUX %s is empty – drag songs into it" % k}
        path = os.path.join(ROOT, it.get("path") or "")
        if BAD_NAME.search(it.get("path") or "") or not os.path.isfile(path): return {"ok": False, "error": "file not found: " + (it.get("path") or "")}
        self.aux[k] = [x for x in L if x is not it]; self.aux_save_file()
        it = dict(it, id=uuid.uuid4().hex[:10], src="deck", deck=k)
        self.automation_off()
        o = "2" if k == "1" else "1"
        if not self.overlap and self.decks.get(o): self.deck_fade_stop(o, 1.5)
        if self.decks.get(k): self.liq.cmd(f"deck.stop {k}")       # this deck was playing: the new song replaces it
        self.liq.cmd(f"var.set deck_v{k} = 1.0")
        self.liq.cmd(f"d{k}.push " + annotate_deck(it))
        cs = it.get("cue_start") or 0; ce = it.get("cue_end") or it.get("duration") or 0
        self.decks[k] = {"it": it, "pushed": time.time(), "t0": None, "len": max(0.5, ce - cs)}
        log.info("deck %s: %s – %s", k, it.get("artist"), it.get("title"))
        return {"ok": True}
    def automation_off(self):
        """The first deck you start takes over from the automation: the song on the automation fades out (1.2 s)."""
        n = self.now or {}
        self.take_back_next()
        if n.get("src") in ("deck", "deck_gone") or not n.get("path") or time.time() > (n.get("started") or 0) + self.length(n) + 3: return
        self.now = dict(n, src="deck_gone")
        def go():
            try:
                for i in range(1, 13): self.liq.cmd(f"var.set duck_lvl = {1 - i / 12:.3f}"); time.sleep(0.1)
                self.liq.cmd("radio.clear"); self.liq.cmd("radio.skip"); time.sleep(0.3)
            finally:
                try: self.liq.cmd("var.set duck_lvl = 1.0")
                except Exception: pass
        threading.Thread(target=go, daemon=True).start()
    def deck_fade_stop(self, k, secs=1.0):
        D = self.decks.get(k)
        if not D: return
        self.decks[k] = None; self.deck_gone(k)
        def go():
            n = max(1, int(secs / 0.1))
            for i in range(1, n + 1):
                if self.decks.get(k): return          # started again meanwhile: leave it
                try: self.liq.cmd(f"var.set deck_v{k} = {1 - i / n:.3f}")
                except Exception: pass
                time.sleep(0.1)
            if self.decks.get(k): return
            try: self.liq.cmd(f"deck.stop {k}"); time.sleep(0.2); self.liq.cmd(f"var.set deck_v{k} = 1.0")
            except Exception: pass
        threading.Thread(target=go, daemon=True).start()
    def deck_gone(self, k):
        """Deck k ended / stopped: the TV goes to the other deck when it still plays."""
        if self.tv_deck != k: return
        o = "2" if k == "1" else "1"
        if self.decks.get(o) and self.decks[o].get("t0"): self.tv_deck = o; self.deck_show(o, fresh=False)
        else:
            self.tv_deck = None
            try: self.tv.show("audio", None, 0)
            except Exception as e: log.warning("deck TV: %s", e)
    def deck_show(self, k, fresh):
        D = self.decks[k]; it = D["it"]; c = dbc(); path = it["path"]; mode = it.get("mode")
        clip_file = None; start_at = 0; sound = False; gain = 0.0; kf = None
        if mode in ("jingle-video", "clip"): clip_file, start_at, kf = tv_file(c, path), it.get("cue_start") or 0, keyframes(c, path)
        elif mode == "pair" and it.get("clip_path"): clip_file, start_at, sound, gain = pair_clip(c, it["clip_path"])
        if not fresh: start_at += time.time() - D["t0"]; kf = None      # the other deck, half-way: its video at the same spot
        scene = self.tv.show(mode, clip_file, start_at, sound, gain, kf)
        self.now = dict(it, started=D["t0"], scene=scene)
        return c, scene
    def on_deck(self, d):
        t0 = time.time(); k = str(d.get("deck") or "")
        with LOCK:
            D = self.decks.get(k)
            if not D or D["it"]["id"] != d.get("hype_id") or D.get("t0"): return
            D["t0"] = t0; self.tv_deck = k
            try:
                c, scene = self.deck_show(k, fresh=True); it = D["it"]
                c.execute("INSERT INTO asrun(started,path,artist,title,folder,mode,clip,scene,kind) VALUES(?,?,?,?,?,?,?,?,?)",
                          (t0, it["path"], it.get("artist"), it.get("title"), it.get("folder"), it.get("mode"), it.get("clip_path"), scene, it.get("section"))); c.commit()
                log.info("DECK %s ON AIR %s – %s scene=%s", k, it.get("artist"), it.get("title"), scene)
                try: self.screen.on_song(c, it)
                except Exception as e: log.warning("screen: %s", e)
                hl_send_np(c, it); relay_title(it)
            except Exception as e: log.warning("deck %s start: %s", k, e)
    def deck_watch(self):
        while True:
            time.sleep(0.5)
            try:
                with LOCK:
                    now = time.time()
                    if self.live.split() and getattr(self.live, "air", "studio") != "studio": self._air_lost = self._air_lost or now
                    else: self._air_lost = None
                    for k, D in list(self.decks.items()):
                        if not D: continue
                        if D.get("t0") and now > D["t0"] + D["len"] + 0.3:
                            self.decks[k] = None; self.deck_gone(k)
                        elif not D.get("t0") and now - D["pushed"] > 12:
                            log.warning("deck %s: %s never started", k, D["it"].get("title")); self.decks[k] = None
            except Exception as e: log.warning("decks: %s", e)
    def decks_state(self):
        out = {}
        for k, D in self.decks.items():
            if D: out[k] = dict({x: D["it"].get(x) for x in ("id", "path", "artist", "title", "folder", "duration", "cue_start", "cue_end", "mode")}, started=D.get("t0"), air=round(D["len"], 1))
        return {"on": self.deck_mode(), "overlap": self.overlap, "tv": self.tv_deck, "playing": out,
                "pads_to": self.live.st.get("pads_to", "1")}
    def take_back_next(self):
        """Pull the already-queued song back out of Liquidsoap (it goes back to the top of the plan)."""
        if self.liq_next:
            self.liq.cmd("radio.clear")
            if time.time() - self.liq_next.get("pushed", time.time()) > 1.0:
                # Liquidsoap already prepared it (that can't be undone): when it starts it is skipped at once
                self.revoked = getattr(self, "revoked", set()) | {self.liq_next.get("id")}
            if self.liq_next.get("event"):     # a fixed time that was queued but taken back is not done yet
                try:
                    cc = dbc(); cc.execute("DELETE FROM events_done WHERE event_id=? AND day=?", tuple(self.liq_next["event"])); cc.commit()
                except Exception as e: log.warning("events_done: %s", e)
            if self.liq_next.get("src") != "loop": self.plan.insert(0, self.liq_next)   # a loop copy is just dropped
            self.liq_next = None
    def event_items(self, c, e):
        avoid = self.avoid(); paths = []
        try: paths = self._event_paths(c, e, avoid)
        except Exception as ex: log.warning("fixed time %s (%s %s): %s – skipped", e.get("at"), e.get("kind"), e.get("ref"), ex)   # e.g. a deleted playlist
        items = []
        for p_ in paths:
            if not p_ or p_ in self.missing: continue
            r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (p_,)).fetchone()
            if r: items.append(row_item(r, "fixed"))
        c.commit(); return items
    def _event_paths(self, c, e, avoid):
        paths = []
        if e["kind"] == "song": paths = [e["ref"]]
        elif e["kind"] == "adbreak":         # a fixed-time ad break: the spots that are due now (max. "how many")
            pl = {}
            for p_ in ([self.liq_next] if self.liq_next else []) + self.plan:
                if p_.get("section") == "ads": pl[p_["path"]] = pl.get(p_["path"], 0) + 1
            paths = planner.pick_ads(c, max(time.time(), e["t"]), int(e["amount"] or planner.get_setting(c, "ad_max_spots", 3)), pl)
            if not paths: log.info("ad break %s: no spots due", e["at"])
        elif e["kind"] == "jingle": paths = [planner.resolve_jingle(c, e["ref"], list(avoid["paths"]))]
        elif e["kind"] in ("playlist", "folder"):
            pid = int(e["ref"]) if e["kind"] == "playlist" else planner.folder_playlist_id(c, e["ref"])
            n = e["amount"] if e["amount"] and e["amount"] > 0 else len(planner.playlist_songs(c, pid))
            for i in range(n):   # a fixed-time block plays its playlist from the start (e.g. the Top 10)
                paths.append(planner.take_from_playlist(c, pid, "start" if i == 0 else "continue", lambda p: True))
            if (c.execute("SELECT COALESCE(chart,0) FROM playlists WHERE id=?", (pid,)).fetchone() or [0])[0]:
                for p_ in paths: CHART_HINT[p_] = (pid, time.time())      # a chart at a fixed time: its songs still get their number
        return paths
    def check_events(self, c):
        now = time.time(); n = self.now
        cur_end = n["started"] + self.length(n) if n else now
        base = 0
        for e in planner.due_events(c, now, max(now, cur_end) + 20 * 60):
            if e["hard"] and now < e["t"]: continue           # exactly on time: wait for the minute
            ek = (e["id"], e["day"])
            if any(p_.get("event") == ek for p_ in self.plan + ([self.liq_next] if self.liq_next else [])): continue   # already waiting
            if e["t"] > max(now, cur_end):
                # coming up later: put it in Up next at the right place already, so you see it (:00 items: see hour_events)
                if e["hard"] or (self.auto and str(e["at"]).endswith(":00")): continue
                items = self.event_items(c, e)
                if not items: continue
                t_ = max(now, cur_end) + (self.length(self.liq_next) if self.liq_next else 0); idx = 0
                for p_ in self.plan:
                    if t_ >= e["t"] - 20: break
                    t_ += self.length(p_); idx += 1
                if idx >= len(self.plan): continue           # the plan doesn't reach that time yet
                for i, it in enumerate(items): it["event"] = ek; it["event_t"] = e["t"]; self.plan.insert(idx + i, it)
                log.info("fixed time %s planned in Up next (#%d): %s", e["at"], idx + 1, ", ".join(i["title"] for i in items)); continue
            items = self.event_items(c, e)
            if not items:
                c.execute("INSERT OR IGNORE INTO events_done VALUES(?,?)", ek); c.commit(); continue
            for it in items: it["event"] = ek                 # marked done when it really goes out (survives a restart)
            self.take_back_next()
            for i, it in enumerate(items): self.plan.insert(base + i, it)
            base += len(items)
            log.info("fixed time %s %s: %s", e["at"], "(hard)" if e["hard"] else "(after this song)", ", ".join(i["title"] for i in items))
            if e["hard"] and str(e["at"]).endswith(":00") and self.auto:
                # top of the hour: what was planned with the old hour's clock goes; the new hour's clock starts at slot 1 after it
                keep = self.plan[:base]; rest = [p_ for p_ in self.plan[base:] if p_.get("src") != "clock"]
                dropped = len(self.plan) - len(keep) - len(rest)
                self.plan = keep + rest; self.runner = planner.ClockRunner(); self.held = {}; self.hour_floor = 0
                self.refill(c)
                log.info("new hour: %d planned item(s) of the old clock dropped, clock from slot 1", dropped)
            self.push_next(c, force=bool(e["hard"]))
            if e["hard"]: time.sleep(0.3); self.liq.cmd("radio.skip")
    def tick(self):
        with LOCK:
            c = dbc()
            try: self.check_events(c)
            except Exception as ex: log.warning("events: %s", ex)
            if self.decks_hold(): self.save_plan(); return      # LIVE · Split: your decks play – the automation waits
            try: self.refill(c)
            except Exception as ex: log.warning("refill: %s", ex)      # whatever goes wrong in planning, the queue below still runs
            if self.stop_at_end and self.liq_next: self.take_back_next()
            self.recover_stuck()
            self.dead_air(c)
            self.push_next(c)
            if self.liq_next and "pushed" not in self.liq_next: self.liq_next["pushed"] = time.time()
            self.save_plan()
            up = [p_["path"] for p_ in ([self.liq_next] if self.liq_next else []) + self.plan if p_.get("mode") in ("clip", "pair", "jingle-video")]
            if up != getattr(self, "_up_w", None):   # the indexer upgrades these clips' TV copies first
                self._up_w = up
                try: jdump(up, SP("/var/lib/hype/upcoming.json"))
                except Exception: pass
    def dead_air(self, c):
        """The radio is empty while the engine thinks a song is still playing: after the radio (Liquidsoap) and/or the engine
        restarted (e.g. Ubuntu's automatic updates restart services), the engine waits for the end of the song it remembers,
        but nothing plays. Nothing queued + Liquidsoap's queue empty + no sound for 5 s -> queue the next item now
        (on a silent radio it starts at once; if a song does play after all, the next one simply waits in the queue)."""
        now = time.time()
        quiet = (AGC.get("level") is not None and AGC["level"] < -55 and now - AGC.get("t", 0) < 3)
        if self.liq_next or self.stopped or self.stop_at_end or getattr(self.live, "live", False) or not quiet:
            self._dead_t = None; return
        if getattr(self, "_dead_t", None) is None: self._dead_t = now; return
        if now - self._dead_t < 5: return
        try:
            if self.liq.queue_len() != 0: return
        except Exception: return
        self._dead_t = None
        log.warning("dead air: the radio is empty (restarted?) – queuing the next item now")
        self.push_next(c, force=True)
    def recover_stuck(self):
        """The queued item never started (Liquidsoap dropped it: file unreadable, a failed request …): without this the
        engine would wait for it forever (dead air). Only when the song on air should have changed 10+ s ago (the estimate
        can be a few s early: smart mix with the real next item), the queued one was pushed 20+ s ago and Liquidsoap's
        queue is really empty - a normal change never gets here (the guard's silence repair comes after 25 s)."""
        n = self.liq_next
        if not n or self.stopped or self.stop_at_end: return
        now = time.time()
        if now - n.get("pushed", now) < 20: return
        if self.now.get("path") and now < (self.now.get("started") or 0) + self.length(self.now) + 10: return
        try:
            if self.liq.queue_len() != 0: return
        except Exception: return
        log.warning("queued %s – %s never started (Liquidsoap dropped it?) – queuing the next item", n.get("artist"), n.get("title"))
        try: self.liq.cmd("radio.clear")
        except Exception: pass
        self.revoked = getattr(self, "revoked", set()) | {n.get("id")}     # if it still turns up after all, it is skipped (no double)
        if n.get("src") != "loop": self.missing[n.get("path")] = now      # (not planned again for 10 min)
        self.liq_next = None
        self.push_next(dbc(), force=True)
    # ---------- song start (called by Liquidsoap) ----------
    def on_track(self, meta):
        t0 = time.time()      # the moment the radio started this song (the rest of this function can take ~1 s: OBS, database)
        path = meta.get("hype_path") or ""
        if not path: return
        rv = getattr(self, "revoked", set())
        if meta.get("hype_id") and meta["hype_id"] in rv:      # a song that was taken back after Liquidsoap prepared it
            rv.discard(meta["hype_id"]); log.info("skipped %s – it was taken back from Up next", meta.get("title"))
            with LOCK:
                try: self.push_next(dbc(), force=True)
                except Exception as e: log.warning("revoked: %s", e)
            self.liq.cmd("radio.skip"); return
        with LOCK:
            # the queued item, recognised by its id (also when the path came back changed, e.g. " -> ') or by its path
            id_next = bool(self.liq_next and meta.get("hype_id") and meta["hype_id"] == self.liq_next.get("id"))
            is_next = id_next or bool(self.liq_next and self.liq_next["path"] == path)
            if self.now.get("path") == path and time.time() - self.now.get("started", 0) < 15 and not id_next: return   # (same metadata twice; a short LOOP copy still counts)
            c = dbc()
            it = self.liq_next if is_next else None
            if it:
                self.liq_next = None; path = it["path"]     # (the real path, not the quoted copy from the metadata)
                if it.get("pos"):    # where the hour clock is, by what is really on air (read back after an engine restart)
                    try: jdump(it["pos"], CLOCK_POS)
                    except Exception as e: log.warning("clock pos: %s", e)
            else:
                r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (path,)).fetchone()
                if not r: log.info("not in library: %s", path); return
                it = row_item(r, "?")
            mode = it["mode"]; clip_file = None; start_at = 0; sound = False; gain = 0.0
            kf = None
            if mode == "jingle-video" or mode == "clip": clip_file, start_at, kf = tv_file(c, path), it["cue_start"] or 0, keyframes(c, path)
            elif mode == "pair" and it["clip_path"]:
                clip_file, start_at, sound, gain = pair_clip(c, it["clip_path"])
            if getattr(self.tv, "hold_song", False):      # a scene you held for the last song: this song is automatic again
                self.tv.hold_song = False; self.tv.auto = True; self.tv.scene = None
            if TVF["ch"]:       # a TV block: the TV follows a theme station – Hype Radio's song only goes on the radio
                scene = "Music video" if clip_file else "Hype Screen"
            else: scene = self.tv.show(mode, clip_file, start_at, sound, gain, kf)
            if mode == "pair" and it["clip_path"] and not TVF["ch"]:
                cd = c.execute("SELECT duration FROM items WHERE path=?", (it["clip_path"],)).fetchone()
                if cd and cd[0] and it["duration"] and cd[0] < it["duration"] - 1:
                    threading.Timer(max(1.0, cd[0] - 0.3), lambda p=path: self.now.get("path") == p and self.tv.show("audio", None, 0)).start()
            c.execute("INSERT INTO asrun(started,path,artist,title,folder,mode,clip,scene,kind) VALUES(?,?,?,?,?,?,?,?,?)",
                      (t0, path, it["artist"], it["title"], it["folder"], mode, it["clip_path"], scene, it["section"])); c.commit()
            # started = when the radio started it, not when this function got here (19 Nov: after a short video jingle the next
            # song's clip had no preload, show() took 1.1 s, and the 20-s check then "corrected" a clip that was right by 1.2 s)
            self.now = dict(it, started=t0, scene=scene)
            if it["section"] == "music":   # loudness map for the AGC intro lift
                try:
                    lm = c.execute("SELECT lmap FROM items WHERE path=?", (path,)).fetchone()
                    if lm and lm[0] and lm[0] != "-": self.now["lmap"] = [int(x) for x in lm[0].split(",")]
                except Exception: pass
            if it.get("deck"): self.deck = it["deck"]
            elif self.dj: self.deck = None
            if it["section"] == "music": self.intro_sweeper(c, it)
            elif (path or "").startswith(VOICE_PFX):      # a listener voice from an hour-plan slot: counted + the voice box on TV
                voice_played(path); voice_box(path, 0.3, max(2.0, (it.get("cue_end") or it.get("duration") or 8) - (it.get("cue_start") or 0)))
            elif (path or "").startswith(VT_PFX): vt_on_air(c, path, it)      # your own voice link (voice tracking)
            if JINGLE_HIDE and not TVF["ch"]: threading.Thread(target=self.tv.jingle_graphics, args=(it["section"] != "music" and scene == "Music video" and not self.live.live,), daemon=True).start()
            if not TVF["ch"]:
                try: self.screen.on_song(c, it)
                except Exception as e: log.warning("screen: %s", e)
            log.info("ON AIR %s – %s [%s/%s] scene=%s", it["artist"], it["title"], it["folder"], mode, scene)
            hl_send_np(c, it); relay_title(it)
            if it["section"] != "music": self.last_kind = "jingle"
            self.push_next(c)
    def pick_intro_voice(self, c, it):
        """A listener voice (✓ OK for air) over the soft intro of the next song: same places as an ID over the intro
        (song -> song, AUTO, no mixes, no own sweeper), the voice must be done 1 s before the intro reaches full level."""
        if not VOICE_AIR["intro"] or not self.auto or self.dj or it.get("section") != "music": return None
        if (it.get("folder") or "").upper().startswith(("MIXES", "RECORDINGS")) or it.get("src") in ("aux", "dj"): return None
        prev = self.now if self.now and self.now.get("path") else None
        if not prev or prev.get("section") != "music" or (prev.get("folder") or "").upper().startswith(("MIXES", "RECORDINGS")): return None
        r = c.execute("SELECT overlay_mode FROM items WHERE path=?", (it["path"],)).fetchone()
        if r and r[0] in ("none", "song"): return None
        if time.time() - self.ov_last < 180: return None
        nl = intro_len(it)
        if not nl or nl < 2.5: return None
        return voice_pick(c, time.time(), max_len=nl)
    def pick_intro_id(self, c, it):
        """An ID over the soft intro of the next song (AUTO): only song -> song (never after a jingle, ID, sweeper, TOH or ad),
        never on a mix or a song with its own sweeper / "never", at most N per hour and M minutes apart, and only when the
        ID's voice (its START -> MIX) is over 1 s before the intro reaches full level. The ID that has rested longest."""
        if not ID_INTRO["on"] or not self.auto or self.dj or it.get("section") != "music": return None
        if (it.get("folder") or "").upper().startswith(("MIXES", "RECORDINGS")) or it.get("src") in ("aux", "dj"): return None
        prev = self.now if self.now and self.now.get("path") else None
        if not prev or prev.get("section") != "music" or (prev.get("folder") or "").upper().startswith(("MIXES", "RECORDINGS")): return None
        r = c.execute("SELECT overlay_mode FROM items WHERE path=?", (it["path"],)).fetchone()
        if r and r[0] in ("none", "song"): return None
        now = time.time(); self.idi_times = [t for t in self.idi_times if now - t < 3600]
        if len(self.idi_times) >= ID_INTRO["max"]: return None
        if self.idi_times and now - self.idi_times[-1] < ID_INTRO["gap"] * 60: return None
        if now - self.ov_last < 180: return None                         # an intro sweeper just played
        nl = intro_len(it)
        if not nl or nl < 2.5: return None
        best = None
        for row in c.execute(f"SELECT {COLS} FROM items WHERE section='jingles' AND folder=? AND problem IS NULL", (ID_FOLDER,)).fetchall():
            ji = row_item(row, "intro"); voice = duck_len(ji)               # START -> MIX: the part with the voice
            if voice + ID_OFFSET + 1.0 > nl: continue
            la = c.execute("SELECT MAX(started) FROM asrun WHERE path=? OR over=?", (ji["path"], "ID: " + (ji.get("title") or ""))).fetchone()[0] or 0   # in the clock, or over an intro (also before a restart)
            rested = max(la, self.idi_used.get(ji["path"], 0))
            if now - rested < 20 * 60: continue                            # the same ID not twice within 20 min
            if best is None or rested < best[0]: best = (rested, ji["path"])
        return best[1] if best else None
    def intro_sweeper(self, c, it):
        """Sweeper over the start of a song with the music ducked: the song's own choice, else the first rule that fits."""
        prev = self.last_kind
        self.last_kind = "music"
        r = c.execute("SELECT overlay_mode, overlay_path, overlay_at, overlay_duck FROM items WHERE path=?", (it["path"],)).fetchone()
        mode, opath, oat, oduck = r if r else (None, None, None, None)
        if mode == "none": return
        jingle, offset, duck = None, 0.5, 0.35
        if it.get("id_over") and mode != "song" and prev == "music":     # ID over the soft intro (decided when it was queued)
            jingle, offset, duck = it["id_over"], ID_OFFSET, ID_DUCK
            if not jingle.startswith(VOICE_PFX): self.idi_times.append(time.time()); self.idi_used[jingle] = time.time()
        elif mode == "song" and opath:                            # chosen for this song (e.g. the Top 10 number 1)
            jingle = planner.song_overlay_path(c, opath); offset = oat or 0.3
            if oduck: duck = max(0.1, min(0.9, 1 - oduck / 100)) if oduck > 1 else oduck
        elif prev == "jingle":
            return                                                # a sweeper just played: no second one
        else:
            for rule in planner.api_overlays(c):
                if not rule["enabled"] or not planner.rule_matches(c, rule, it): continue
                n = self.ov_count.get(rule["id"], 0) + 1; self.ov_count[rule["id"]] = n
                if n % rule["every"]: return
                if time.time() - self.ov_last < rule["min_gap"] * 60: return
                jingle = planner.resolve_jingle(c, rule["jingle_ref"], self.ov_recent[-6:]); offset, duck = rule["offset"], rule["duck"]
                break
        if not jingle: return
        jr = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (jingle,)).fetchone()
        if not jr: return
        self.ov_last = time.time(); self.ov_recent.append(jingle)
        kind_ = ("Voice" if jingle.startswith(VOICE_PFX) else "ID") if it.get("id_over") and jingle == it.get("id_over") else "Sweeper"
        def go():
            ji = row_item(jr, "intro")
            if kind_ == "ID" and ji.get("lufs") is not None: ji["lufs"] = ji["lufs"] - ID_INTRO["db"]    # ID over the intro: its own level (Settings → Mixing)
            self.liq.cmd("pads.push " + annotate(ji, True)); self.duck_for(duck_len(ji), duck)
            if kind_ == "Voice":
                voice_played(jingle); voice_box(jingle, 0.2, max(2.0, (ji.get("cue_end") or ji.get("duration") or 8) - (ji.get("cue_start") or 0)))
                vv = voice_by_path(jingle); ji["title"] = ((vv.get("name") or "listener").split() or ["listener"])[0]
            log.info("intro sweeper %s over %s", jingle, it["title"])
            try:     # History shows it next to the song: "+ ID …" / "+ Sweeper …"
                cc = dbc(); cc.execute("UPDATE asrun SET over=? WHERE id=(SELECT max(id) FROM asrun WHERE path=?)", (kind_ + ": " + (ji.get("title") or os.path.basename(jingle)), it["path"])); cc.commit()
            except Exception as e: log.warning("history over: %s", e)
        threading.Timer(max(0.0, offset), go).start()
    # ---------- panel commands ----------
    def _live_layer_back(self, c):
        """Live Layer off is temporary: back to Music video, Hype Screen or AUTO SCENES turns it on again."""
        if not planner.get_setting(c, "live_layer", True):
            planner.set_setting(c, "live_layer", True); self.tv.live_layer(True); log.info("Live Layer back on")
    def cmd(self, action, d):
        if action.startswith("web_"):      # 🌐 the browser (hypeweb.py): never under the engine's lock (starting takes seconds)
            try:
                if action == "web_start": hypeweb.start(); return {"ok": True, "web": hypeweb.info()}
                if action == "web_stop":
                    if self.media.cur and self.media.cur.get("file") == "live:web":
                        with LOCK: self.media.stop(self)
                    hypeweb.stop(); return {"ok": True}
                if not hypeweb.running(): return {"ok": False, "error": "the browser is not running – open it first"}
                if action == "web_nav": hypeweb.nav(d.get("url"))
                elif action == "web_back": hypeweb.back()
                elif action == "web_fwd": hypeweb.forward()
                elif action == "web_reload": hypeweb.reload()
                elif action == "web_click": hypeweb.click(float(d["x"]), float(d["y"]), int(d.get("n") or 1))
                elif action == "web_wheel": hypeweb.wheel(float(d["x"]), float(d["y"]), float(d.get("dy") or 0))
                elif action == "web_type": hypeweb.type_text(str(d.get("text") or ""))
                elif action == "web_key": hypeweb.key(str(d.get("key") or ""))
                elif action == "web_tab": hypeweb.tab(str(d.get("id") or ""))
                elif action == "web_tabclose": hypeweb.tab_close(str(d.get("id") or ""))
                elif action == "web_tabnew": hypeweb.tab_new()
                elif action != "web_info": return {"ok": False, "error": "unknown"}
                return {"ok": True, "web": hypeweb.info()}
            except Exception as e: return {"ok": False, "error": str(e)[:160]}
        if action == "stream_play":      # 🔴 LIVE pad on air: a YouTube link is asked for first (seconds) – not while holding the engine's lock
            st_ = next((x for x in streams_load() if x["id"] == d.get("id")), None)
            if not st_: return {"ok": False, "error": "that live pad is gone"}
            if st_["id"] == "web" and not hypeweb.running(): return {"ok": False, "error": "open the browser first"}
            try: url_ = stream_resolve(st_["url"])
            except Exception as e: return {"ok": False, "error": str(e)[:160]}
            with LOCK:
                try: return {"ok": True, "media": self.media.play_stream(self, d, url_)}
                except ValueError as e: return {"ok": False, "error": str(e)}
        if action == "stream_test":      # 👁 does the link play right now? (ffprobe, nothing on air)
            st_ = next((x for x in streams_load() if x["id"] == d.get("id")), None)
            if not st_: return {"ok": False, "error": "that live pad is gone"}
            try: u_ = stream_resolve(st_["url"])
            except Exception as e: return {"ok": False, "error": str(e)[:160]}
            r_ = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "8000000", "-show_entries", "stream=codec_type,width,height", "-of", "json", u_], capture_output=True, text=True, timeout=25)
            try: ss_ = json.loads(r_.stdout or "{}").get("streams", [])
            except ValueError: ss_ = []
            if not ss_: return {"ok": False, "error": "No signal on that link right now" + (": " + r_.stderr.strip().splitlines()[-1][:100] if r_.stderr.strip() else "")}
            v_ = next((x for x in ss_ if x.get("codec_type") == "video"), {})
            return {"ok": True, "video": "%sx%s" % (v_.get("width"), v_.get("height")) if v_ else None, "audio": any(x.get("codec_type") == "audio" for x in ss_)}
        with LOCK:
            c = dbc()
            if action == "overlap":
                self.overlap = bool(d.get("on")); jdump({"overlap": self.overlap}, DECKS_FILE); return {"ok": True, "overlap": self.overlap}
            if action == "deck_stop":
                k_ = str(d.get("deck", "1"))
                if self.decks.get(k_): self.deck_fade_stop(k_, 0.8)
                return {"ok": True}
            if action == "deck_play" or (self.deck_mode() and (action == "dj_play" or (action == "aux_fire" and d.get("how") == "now"))):
                if not self.deck_mode(): return {"ok": False, "error": "the decks play only while LIVE with Split on (Broadcast page)"}
                return self.deck_play(c, str(d.get("deck") or d.get("aux") or "1"), d.get("id"))
            if action == "next":
                if getattr(self, "loop", False):   # NEXT ends a loop
                    self.loop = False
                    if self.liq_next and self.liq_next.get("src") == "loop": self.liq.cmd("radio.clear"); self.liq_next = None
                self.stopped = False; self.stop_at_end = False
                if not self.auto and not self.plan and not self.liq_next:     # DJ / MANUAL: the next song of the decks / AUX (taken only when needed)
                    it_ = self.dj_take() if self.dj else self.aux_take()
                    if it_: self.picker.note(it_); self.plan.append(it_)
                self.push_next(c, force=True); time.sleep(0.3); self.liq.cmd("radio.skip")
            elif action == "play":
                if not self.auto and not self.dj and not self.stopped:   # MANUAL → PLAY = back to the normal programming
                    return self.cmd_auto(c, True)
                self.stopped = False; self.stop_at_end = False
                gone = not self.now or time.time() - self.now.get("started", 0) > (self.now.get("duration") or 0)
                self.push_next(c, force=gone)
                if gone: self.liq.cmd("radio.skip")
            elif action == "stop_at_end":
                self.stop_at_end = not self.stop_at_end
                if self.stop_at_end: self.take_back_next()
                else: self.push_next(c)
            elif action == "stop":
                self.stop_at_end = True; self.take_back_next(); self.liq.cmd("radio.skip"); self.stopped = True
            elif action == "auto":
                return self.cmd_auto(c, bool(d.get("on", True)))
            elif action == "insert_many":      # a playlist dragged into Up next: its songs, in its own order
                pos = int(d.get("pos", 0)); its = []
                for p_ in d.get("paths") or []:
                    r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (p_,)).fetchone()
                    if r: its.append(row_item(r, "you"))
                if not its: return {"ok": False, "error": "nothing to add"}
                if pos == 0 and not d.get("keep"): self.take_back_next()     # keep: dropped just below the queued song
                self.plan[pos:pos] = its; self.push_next(c)
            elif action == "run_clock":        # "Run this clock now": it takes over for the rest of this hour
                cid = int(d["id"])
                if not c.execute("SELECT 1 FROM clocks WHERE id=?", (cid,)).fetchone(): return {"ok": False, "error": "clock not found"}
                if self.liq_next and self.liq_next.get("src") == "clock": self.take_back_next()
                self.plan = [p_ for p_ in self.plan if p_.get("src") != "clock" or p_.get("event")]
                self.runner = planner.ClockRunner(); self.runner.force_cid = cid; self.runner.force_until = (int(time.time() // 3600) + 1) * 3600
                self.hour_floor = 0; self.refill(c); self.push_next(c)
                log.info("clock %s runs now (until the next hour)", cid)
            elif action == "insert":
                r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (d["path"],)).fetchone()
                if not r: return {"ok": False, "error": "not found"}
                it = row_item(r, "you"); pos = int(d.get("pos", 0))
                if pos == 0 and not d.get("keep"): self.take_back_next()
                self.plan.insert(pos, it); self.push_next(c)
            elif action == "remove":
                if self.liq_next and self.liq_next["id"] == d["id"]: self.take_back_next()
                self.plan = [p for p in self.plan if p["id"] != d["id"]]; self.push_next(c)
            elif action == "move":
                if self.liq_next and d["id"] in (self.liq_next["id"],) or int(d["to"]) == 0 and not d.get("keep"): self.take_back_next()
                it = next((p for p in self.plan if p["id"] == d["id"]), None)
                if it: self.plan.remove(it); self.plan.insert(int(d["to"]), it)
                self.push_next(c)
            elif action == "scene":
                if d["scene"] in ("Music video", "Hype Screen", "Branding"):
                    # the scene that fits what plays now (e.g. Music video during a clip, back from the cameras): AUTO SCENES on.
                    # another one (e.g. Hype Screen during a clip): held for THIS song, the next song is automatic again
                    self._live_layer_back(c)
                    want = tv_scene()
                    hold = d["scene"] != want
                    self.tv.auto = not hold; self.tv.hold_song = hold; self.tv.set_scene(d["scene"])
                    return {"ok": True, "auto": not hold, "hold": hold}
                else: self.hold_until = 0; self.tv.auto = False; self.tv.hold_song = False; self.tv.set_scene(d["scene"])
            elif action == "keep_card":       # Settings → TV: test the "Keep listening" card of the station on TV now (on air)
                f_ = keep_card_file(TVF["ch"])
                if not f_: return {"ok": False, "error": "no card for this station (branding/keeplistening/%s.png)" % (TVF["ch"] or "hype")}
                if not E.tv.auto or E.live.live: return {"ok": False, "error": "only with the scenes on AUTO and not LIVE"}
                secs_ = float(planner.get_setting(c, "keep_s", 8) or 0) or 8.0
                threading.Thread(target=lambda: (keep_card(TVF["ch"], secs_, test=True), keep_back()), daemon=True).start()
                return {"ok": True, "secs": secs_}
            elif action == "tv_follow":       # On Air → TV: which station has the TV now (by hand); "" / None = the TV schedule again
                ch_ = d.get("ch") or None
                if ch_ and ch_ != "main" and not tv_station_ok(ch_): return {"ok": False, "error": "that station is not on Hype Radio's engine / is off"}
                b = tv_blocks(c); b["manual"] = ch_; planner.set_setting(c, "tv_blocks", b); TV_BLOCKS["t"] = 0
                return {"ok": True, "tv": tv_info()}
            elif action == "tv_blocks":       # Planning → TV: the week grid {"<weekday>-<hour>": station id}
                g = {str(k): str(v) for k, v in (d.get("grid") or {}).items() if re.match(r"^[0-6]-(1?[0-9]|2[0-3])$", str(k)) and v and str(v) != "main" and chancfg.valid(str(v))}
                b = tv_blocks(c); b["grid"] = g; planner.set_setting(c, "tv_blocks", b); TV_BLOCKS["t"] = 0
                return {"ok": True, "tv": tv_info()}
            elif action == "live_layer":      # the Hype Live Layer (shoutouts, polls, chat …) on / off in every scene
                on = bool(d.get("on")); planner.set_setting(c, "live_layer", on); self.tv.live_layer(on)
            elif action == "autoscene":      # the AUTO SCENES button: on <-> off (off = manual: the scene stays until you change it)
                if self.tv.auto and not d.get("on"):
                    self.tv.auto = False; self.tv.hold_song = False
                    return {"ok": True, "auto": False}
                self._live_layer_back(c)
                self.tv.auto = True; self.tv.hold_song = False; self.tv.scene = None
                if self.now: self.tv.set_scene(tv_scene())
                return {"ok": True, "auto": True}
            elif action == "media_play":
                res = self.media.play(self, d)
                try:
                    st_ = media_in_state(); f_ = os.path.basename(d.get("file") or "")
                    for v_ in st_.values():
                        if v_.get("file") == f_: v_["played"] = time.time(); v_["plays"] = v_.get("plays", 0) + 1; jdump(st_, MEDIA_IN)
                except Exception as e: log.warning("listener media played: %s", e)
                return {"ok": True, "media": res}
            elif action in ("listener_pin", "listener_hide"):      # LISTENERS pads: ★ keep it / × off the pads (the file stays)
                k_ = "pinned" if action == "listener_pin" else "hidden"; on_ = bool(d.get("on", True))
                if d.get("kind") == "voice":
                    st_ = voice_state()
                    for v_ in st_.values():
                        if v_.get("path") == d.get("id"): v_[k_] = on_
                    voice_save(st_)
                else:
                    st_ = media_in_state()
                    for v_ in st_.values():
                        if v_.get("file") == d.get("id"): v_[k_] = on_
                    jdump(st_, MEDIA_IN)
                return {"ok": True}
            elif action == "slideshow_new":     # Scenes → SLIDESHOW → + NEW: a new folder = a new scene within ~30 s
                kind = d.get("kind") if d.get("kind") in ("photos", "videos") else "photos"
                name = re.sub(r"[^\w .&()'-]", "", str(d.get("name") or ""), flags=re.U).strip()[:40]
                if not name: return {"ok": False, "error": "give it a name"}
                os.makedirs(os.path.join(slideshow.ROOT, kind, name), exist_ok=True)
                try:
                    import pwd, grp; os.chown(os.path.join(slideshow.ROOT, kind, name), pwd.getpwnam("upload").pw_uid, grp.getgrnam("hype").gr_gid)
                except Exception: pass
                os.chmod(os.path.join(slideshow.ROOT, kind, name), 0o2775)
                return {"ok": True, "scene": slideshow.scene_name(kind, name), "folder": "branding/slideshow/%s/%s" % (kind, name)}
            elif action == "slideshow_delete":   # Scenes → SLIDESHOW → ✎ → ×: the folder to branding/slideshow/.deleted, its scene goes
                f = os.path.normpath(str(d.get("folder") or "")).split("/")
                if len(f) != 4 or f[:2] != ["branding", "slideshow"] or f[2] not in ("photos", "videos") or f[3] in ("Listeners", "", ".", ".."):
                    return {"ok": False, "error": "this slideshow can't be deleted"}
                src = os.path.join(slideshow.ROOT, f[2], f[3])
                if not os.path.isdir(src): return {"ok": False, "error": "not found"}
                os.makedirs(os.path.join(slideshow.ROOT, ".deleted"), exist_ok=True)
                os.replace(src, os.path.join(slideshow.ROOT, ".deleted", time.strftime("%Y%m%d-%H%M%S-") + f[2] + "-" + f[3]))
                if self.tv.scene == slideshow.scene_name(f[2], f[3]): self.tv.auto = True; self.tv.scene = None; self.tv.set_scene(tv_scene())
                slideshow.STATE["force"] = True; log.info("slideshow deleted: %s", "/".join(f)); return {"ok": True}
            elif action == "fetch_url":      # a picture / video / sound dragged from a website onto the pads: the server fetches it
                return fetch_url(c, d)
            elif action == "seek":        # the on-air waveform (unlocked): jump to pos (seconds in the file); the video follows
                n = self.now
                if self.live.live: return {"ok": False, "error": "not while you are LIVE"}
                if not n or n.get("section") != "music": return {"ok": False, "error": "only in a song"}
                el = time.time() - n["started"] + (n.get("cue_start") or 0)
                end = n.get("cue_mix") or n.get("cue_end") or n.get("duration") or 0
                pos = max((n.get("cue_start") or 0) + 1, min(float(d.get("pos", el)), end - 8))
                if end - el < 10: return {"ok": False, "error": "too close to the mix – not now"}
                if abs(pos - el) < 0.5: return {"ok": True}
                try: moved = float(self.liq.cmd("radio.seek %.2f" % (pos - el)).split()[0])
                except Exception as e: return {"ok": False, "error": "the radio can't jump yet (update tonight) – %s" % str(e)[:60]}
                self.now["started"] = n["started"] - moved; RESYNC_N.pop(n.get("started"), None)
                log.info("jumped %+.1f s in %s (to %.0f s)", moved, n.get("title"), el + moved)
                threading.Thread(target=resync_tv, kwargs={"force": True}, daemon=True).start()
                return {"ok": True, "moved": moved}
            elif action == "media_vol":
                MEDIA_VOL["v"] = max(0.0, min(1.6, float(d.get("value", 1)))); planner.set_setting(dbc(), "media_vol", MEDIA_VOL["v"])
                return {"ok": True, "media_vol": MEDIA_VOL["v"]}
            elif action == "tv_sound_radio":     # the guard: TV silent while a paired video plays its own sound -> the radio's sound for this song
                if not self.tv.split: return {"ok": True, "acted": False}
                self.tv.clip_sound = False; self.tv.route_audio(force=True)
                log.warning("TV had no sound with the paired video's own sound – the TV takes the radio's sound for this song"); return {"ok": True, "acted": True}
            elif action == "stream_save":        # 🔴 LIVE pad: {id?, name, url}
                u_ = str(d.get("url") or "").strip(); n_ = re.sub(r"\s+", " ", str(d.get("name") or "")).strip()[:40] or "Live stream"
                if not re.match(r"^(https?|rtmps?|srt|rtsp)://[^\s]+$", u_): return {"ok": False, "error": "Not a stream link (https://….m3u8, rtmp://…, srt://… or a YouTube live)"}
                L_ = streams_load(True); sid = d.get("id") or uuid.uuid4().hex[:8]
                if sid == "web": return {"ok": False, "error": "that one is the browser"}
                L_ = [x for x in L_ if x["id"] != sid] + [{"id": sid, "name": n_, "url": u_}]
                jdump(L_, STREAMS); os.chmod(STREAMS, 0o600); return {"ok": True, "streams": L_}
            elif action == "stream_delete":
                L_ = [x for x in streams_load(True) if x["id"] != d.get("id")]; jdump(L_, STREAMS); return {"ok": True, "streams": L_}
            elif action == "media_stop":
                self.media.stop(self, keep_scene=bool(d.get("keep_scene"))); return {"ok": True}
            elif action == "media_delete":     # Pads → MEDIA → ✎ EDIT → ×: the file goes to branding/media/.deleted (can be put back by hand)
                f = os.path.basename(str(d.get("file") or ""))
                src = os.path.join(MEDIA_DIR, f)
                if not f or not os.path.isfile(src): return {"ok": False, "error": "file not found"}
                if self.media.cur and self.media.cur.get("file") == f: self.media.stop(self)
                os.makedirs(os.path.join(MEDIA_DIR, ".deleted"), exist_ok=True)
                os.replace(src, os.path.join(MEDIA_DIR, ".deleted", time.strftime("%Y%m%d-%H%M%S-") + f)); log.info("media deleted: %s", f)
                return {"ok": True}
            elif action == "pad_unloop":
                self.pad_loops.pop(d.get("path"), None); self.duck_until = 0; self.liq.cmd("pads.stop"); self.pad_video_off(); VOICE_ON.clear()
            elif action == "pad":
                r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (d["path"],)).fetchone()
                if d["path"].startswith(VOICE_PFX):              # a listener's voice: always over ducked music, never a loop
                    d["duck"] = True; d["loop"] = False
                    try:
                        st = voice_state(); who = {}
                        for v in st.values():
                            if v.get("path") == d["path"]: v["played"] = time.time(); who = v
                        voice_save(st)
                        if r:       # the voice message box on TV (Graphics, in the Now Playing spot) for as long as it plays
                            vi = row_item(r, "pad"); ln = max(2.0, (vi.get("cue_end") or vi.get("duration") or 10) - (vi.get("cue_start") or 0))
                            name = (who.get("name") or re.sub(r"^Voice - |\s*\(.*$|\s+\S+$", "", os.path.splitext(os.path.basename(d["path"]))[0]) or "Listener").split()[0]
                            VOICE_ON.update(name=name, source=who.get("source") or "", start=time.time() + 0.3, end=time.time() + 0.3 + ln)
                    except Exception as e: log.warning("voice box: %s", e)
                if r and d.get("video"): self.pad_video(c, row_item(r, "pad"), bool(d.get("loop")))
                if r and d.get("loop"): self.pad_loop(r, bool(d.get("duck")), float(planner.get_setting(c, "pad_duck", 0.25)))
                elif r:
                    if d.get("duck"):
                        pi = row_item(r, "pad"); self.liq.cmd("pads.push " + annotate(pi, True)); self.duck_for(duck_len(pi), float(planner.get_setting(c, "pad_duck", 0.25)))
                    else: self.liq.cmd("pads.push " + annotate(row_item(r, "pad"), True))
            elif action == "set_tempo":
                return tempo_set(d)
            elif action == "set_cues":
                cs, cm, ce = float(d["cue_start"]), float(d["cue_mix"]), float(d["cue_end"]); auto = bool(d.get("mix_auto"))
                if auto: cm = min(max(cm, cs), ce)
                if not (0 <= cs <= cm <= ce): return {"ok": False, "error": "START must be before MIX, MIX before END"}
                c.execute("UPDATE items SET cue_start=?, cue_mix=?, cue_end=?, cues_edited=1, mix_set=? WHERE path=?", (cs, cm, ce, 0 if auto else 1, d["path"])); c.commit()
                try: c.execute("UPDATE items SET cue_check=0 WHERE path=?", (d["path"],)); c.commit()   # checked by you
                except sqlite3.OperationalError: pass
                LM_CACHE.pop(d["path"], None)
                for p_ in self.plan:
                    if p_["path"] == d["path"]: p_.update(cue_start=cs, cue_mix=cm, cue_end=ce)
            elif action == "show_intro":
                self.tv.first_show_seen = True; self.tv.show_changed(SHOW.get("title"))
            elif action == "delete_item":      # Library: delete a file - it goes to the bin (/srv/hype-data/deleted, 30 days), never straight away
                rel = os.path.normpath(str(d.get("path") or "")).lstrip("/")
                if not rel or rel.startswith("..") or not lib_root_ok(rel): return {"ok": False, "error": "not a library file"}
                if (self.now or {}).get("path") == rel: return {"ok": False, "error": "it is on air right now"}
                if self.liq_next and self.liq_next.get("path") == rel: return {"ok": False, "error": "it is the next item (already queued) - try again in a minute"}
                src = os.path.join(ROOT, rel)
                # only ONE library file (never a folder: "music" would move the whole section to the bin)
                if not os.path.isfile(src) or not c.execute("SELECT 1 FROM items WHERE path=?", (rel,)).fetchone(): return {"ok": False, "error": "not a library file"}
                dst = os.path.join("/srv/hype-data/deleted", time.strftime("%Y-%m-%d"), rel); os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                c.execute("DELETE FROM items WHERE path=?", (rel,)); c.execute("UPDATE items SET clip_path=NULL, mode='audio' WHERE clip_path=?", (rel,)); c.commit()
                self.plan = [p_ for p_ in self.plan if p_.get("path") != rel]
                for k_ in list(self.aux): self.aux[k_] = [p_ for p_ in self.aux[k_] if p_.get("path") != rel]
                try: self.aux_save_file()
                except Exception: pass
                LM_CACHE.pop(rel, None); log.info("deleted (to the bin): %s", rel)
                self.refill(c)
            elif action == "rename_item":      # Library: rename a file (same folder, same type) – cues, history, playlists and pads keep it
                return self.rename_item(c, d)
            elif action == "reset_cues":
                c.execute("UPDATE items SET cues_edited=0, mix_set=0, mtime=0 WHERE path=?", (d["path"],)); c.commit(); LM_CACHE.pop(d["path"], None)
                try: c.execute("UPDATE items SET cue_check=0 WHERE path=?", (d["path"],)); c.commit()
                except sqlite3.OperationalError: pass
            elif action == "set_overlay":
                c.execute("UPDATE items SET overlay_mode=?, overlay_path=? WHERE path=?",
                          (d.get("mode") or None, d.get("jingle") if d.get("mode") == "song" else (None if d.get("mode") == "none" else c.execute("SELECT overlay_path FROM items WHERE path=?", (d["path"],)).fetchone()[0]), d["path"])); c.commit()
            elif action == "loop":     # loop the song on air (again and again) until you switch it off
                on = bool(d.get("on", not getattr(self, "loop", False)))
                if on != getattr(self, "loop", False):
                    self.loop = on
                    if on: self.take_back_next(); self.push_next(c)
                    elif self.liq_next and self.liq_next.get("src") == "loop": self.liq.cmd("radio.clear"); self.liq_next = None; self.push_next(c)
            elif action == "pads_stop":
                self.pad_loops.clear(); self.duck_until = 0; VOICE_ON.clear()
                self.liq.cmd("pads.stop"); self.pad_video_off()
                if self.media.cur: self.media.stop(self)      # STOP PADS also takes the media (picture / video) off
            elif action.startswith("aux_"):
                return self.aux_cmd(c, action, d)
            elif action == "dj":     # DJ mode on/off: AUX 1 = deck A, AUX 2 = deck B; off = back to the clock
                # The DJ view never switches AUTO off by itself: prepare your decks while the clock keeps playing.
                # MANUAL (or MIX / PLAY NOW on a deck) hands the air to the decks.
                on = bool(d.get("on")); self.dj = on
                if not self.auto:
                    self.aux_back(); self.plan = self.keep_mine()
                    self.refill(c); self.push_next(c)
            elif action in ("dj_play", "dj_cue"):   # PLAY NOW (cut) / MIX (crossfade) this deck's next song, or load it as the next song
                k = str(d.get("deck", "1")); how = d.get("how", "mix")
                if not self.aux.get(k): return {"ok": False, "error": "this deck is empty – drag songs into it"}
                if self.auto: self.park(); self.auto = False; self.dj = True      # starting a deck = the decks take over (AUTO is parked)
                it = dict(self.aux[k].pop(0), id=uuid.uuid4().hex[:10], src="dj", deck=k); self.aux_save_file()
                self.take_back_next()
                for back in reversed([p_ for p_ in self.plan if p_.get("src") in ("dj", "aux")]):   # songs that were lined up go back on their deck
                    self.aux.setdefault(back["deck"], []).insert(0, back)
                self.plan = [p_ for p_ in self.plan if p_.get("src") not in ("dj", "aux")]
                self.plan.insert(0, it); self.refill(c); self.push_next(c, force=(action == "dj_play")); self.aux_save_file()
                if action == "dj_play": time.sleep(0.3); self.liq.cmd("radio.cut" if how == "cut" else "radio.skip")
            elif action == "element":      # put a graphic on screen until you take it off (or toggle it off)
                f = tickers.as_ticker(d.get("file") or "")
                if d.get("off"): self.screen.hide(d.get("id"))
                elif f in planner.screen_files() or f == "adbox:" or (f.startswith("ticker:") and tickers.get(f[7:])):
                    on = [e for e in self.screen.manual() if e["file"] == f]
                    if on: self.screen.hide(on[0]["id"])
                    else: self.screen.add(None, f, 0.0, 6 * 3600, d.get("where") or "all", manual=True)
                else: return {"ok": False, "error": "file not found in branding/screen"}
            elif action == "cam_layout":     # the Cameras grid as you arranged it ('' = an empty place); hidden = scenes you took off
                L = {"cells": [str(x)[:80] for x in (d.get("cells") or [])][:120], "hidden": [str(x)[:80] for x in (d.get("hidden") or [])][:120]}
                if d.get("reset"): L = None
                tab = d.get("tab") if d.get("tab") in ("cam", "pip", "media") else "cam"     # Cameras / PIP / MEDIA each keep their own grid
                planner.set_setting(c, tab + "_layout", L); self.layouts[tab] = L
            elif action == "scene_groups":   # your own groups of scenes (e.g. per show)
                G = []
                for g in (d.get("groups") or [])[:30]:
                    nm = str(g.get("name") or "").strip()[:30]
                    if nm: G.append({"name": nm, "scenes": [str(x)[:40] for x in (g.get("scenes") or [])][:40]})
                planner.set_setting(c, "scene_groups", G); self.sgroups = G
            elif action == "fader":
                name = d.get("name"); v = max(0.0, min(1.6, float(d.get("value", 1))))
                if name not in ("vol_music", "vol_pads", "vol_studio"): return {"ok": False, "error": "unknown fader"}
                self.liq.cmd(f"var.set {name} = {v:.3f}"); self.faders[name] = v
            return {"ok": True}
    def cmd_auto(self, c, on):
        """AUTO = the clocks and programming. MANUAL = your AUX lists play (the song on air always plays on)."""
        if on == self.auto: return {"ok": True}
        if on:
            self.aux_back(); self.dj = False; self.auto = True; self.stopped = False; self.stop_at_end = False
            keep = self.keep_mine()
            t, parked = self.parked or (0, [])
            played = {r[0] for r in c.execute("SELECT path FROM asrun WHERE started>=?", (t,))} | ({self.now.get("path")} if self.now else set())
            if parked and time.time() - t < 15 * 60:     # back within 15 min: the same Up next as before (nothing reshuffled)
                recent_j = {r[0] for r in c.execute("SELECT path FROM asrun WHERE started>=? AND kind IN ('jingles','ads')", (time.time() - 3600,))}
                back = [p_ for p_ in parked if p_["path"] not in played and not (self.is_jingle(p_) and p_["path"] in recent_j)]
                while back and self.is_jingle(back[0]) and self.is_jingle(self.now): back.pop(0)    # never two jingles in a row
                self.plan = keep + back
            else:                                          # longer away: plan fresh from the clock of this moment
                self.plan = keep; self.runner = planner.ClockRunner()
            self.parked = None
            self.tv.auto = True; self.tv.scene = None      # the TV follows the music again too
            if self.now: self.tv.set_scene(tv_scene())
        else:
            self.park(); self.auto = False
        self.refill(c); self.push_next(c); return {"ok": True}
    @staticmethod
    def strip_hour(items):
        """Up next without the top-of-the-hour group (TOH + what belongs to it) and without what was planned after it."""
        out = []
        for p_ in items:
            if p_.get("hour_T"): break
            out.append(p_)
        return out
    def keep_mine(self):
        """What survives a re-plan: your own items and fixed times – but not the top of the hour (it is placed again at :00),
        and not a fixed time that is still to come (it is placed again at its own time: after DJ / MANUAL the Up next before
        it is different, so it would otherwise play too early)."""
        self.hour_floor = 0
        hk = {p_.get("event") for p_ in self.plan if p_.get("hour_T")}
        now = time.time()
        return [p_ for p_ in self.plan if p_.get("src") in ("you", "fixed") and not (p_.get("event") and p_.get("event") in hk)
                and not (p_.get("src") == "fixed" and p_.get("event_t", 0) > now + 30)]
    def park(self):
        """Leaving AUTO: the clock's Up next is put aside (not thrown away), so AUTO comes back with the same songs."""
        self.take_back_next()
        if self.auto: self.parked = (time.time(), [p_ for p_ in self.strip_hour(self.plan) if p_.get("src") == "clock"])
        hk = {p_.get("event") for p_ in self.plan if p_.get("hour_T")}
        self.plan = [p_ for p_ in self.plan if p_.get("src") in ("you", "fixed", "aux", "dj") and not (p_.get("event") and p_.get("event") in hk)]
        self.hour_floor = 0
    def rename_item(self, c, d):
        import indexer as _ix
        rel = os.path.normpath(str(d.get("path") or "")).lstrip("/")
        if not rel or rel.startswith("..") or not lib_root_ok(rel): return {"ok": False, "error": "not a library file"}
        src = os.path.join(ROOT, rel)
        if not os.path.isfile(src) or not c.execute("SELECT 1 FROM items WHERE path=?", (rel,)).fetchone(): return {"ok": False, "error": "not a library file"}
        if (self.now or {}).get("path") == rel: return {"ok": False, "error": "it is on air right now – rename it after it played"}
        if self.liq_next and self.liq_next.get("path") == rel: return {"ok": False, "error": "it is the next item (already queued) - try again in a minute"}
        ext = os.path.splitext(rel)[1]
        tdir = os.path.dirname(rel)
        if d.get("folder"):        # Library → Move to: the same file in another folder of the same section (cues, history, playlists stay)
            tdir = os.path.normpath(str(d["folder"])).strip("/")
            # a theme station's folder as the Library shows it ("music/LATINA/MERENGUE/MER-CURRENT") -> its real place on disk
            # ("music-latina/MERENGUE/MER-CURRENT") (24 Nov: moving inside Hype Latina said "choose a folder of the same section")
            tp_ = tdir.split("/")
            if len(tp_) >= 2 and os.path.isdir(os.path.join(ROOT, "%s-%s" % (tp_[0], tp_[1].lower()))) and not os.path.isdir(os.path.join(ROOT, tdir)):
                tdir = "/".join(["%s-%s" % (tp_[0], tp_[1].lower())] + tp_[2:])
            if tdir.split("/")[0] != rel.split("/")[0] or ".." in tdir.split("/") or not os.path.isdir(os.path.join(ROOT, tdir)):
                return {"ok": False, "error": "choose a folder of the same section"}
            if not d.get("name"): d = dict(d, name=os.path.splitext(os.path.basename(rel))[0])
        name = re.sub(r"\s+", " ", str(d.get("name") or "")).strip()
        if name.lower().endswith(ext.lower()): name = name[:-len(ext)].strip()
        if not name or len(name) > 180 or BAD_NAME.search(name) or any(ch in name for ch in '/\\:*?"<>|') or name.startswith("."):
            return {"ok": False, "error": 'not a valid name (no / \\ : * ? " < > |)'}
        fn = name + (d.get("ext") or ext); new = os.path.join(tdir, fn)
        if new == rel:     # same name (e.g. already renamed by FTP): the shown artist / title still follow the file name
            fa, ft = _ix.split_name(fn); row = c.execute("SELECT section FROM items WHERE path=?", (rel,)).fetchone()
            if fa or row[0] != "music":
                c.execute("UPDATE items SET artist=?, title=? WHERE path=?", (fa or "", _ix.clean_title(ft or name), rel)); c.commit()
            r_ = c.execute("SELECT artist, title FROM items WHERE path=?", (rel,)).fetchone()
            return {"ok": True, "path": rel, "artist": r_[0], "title": r_[1]}
        if os.path.exists(os.path.join(ROOT, new)) and new.lower() != rel.lower(): return {"ok": False, "error": "a file with that name is already there"}
        os.rename(src, os.path.join(ROOT, new))
        old_fn = os.path.basename(rel)
        try:
            fa, ft = _ix.split_name(fn)
            row = c.execute("SELECT section, artist, title FROM items WHERE path=?", (rel,)).fetchone()
            artist, title = (fa, _ix.clean_title(ft)) if fa else (row[1], row[2]) if row[0] == "music" else ("", _ix.clean_title(ft))
            c.execute("UPDATE items SET path=?, filename=?, artist=?, title=?, folder=? WHERE path=?", (new, fn, artist, title, _ix.split_path(new)[1], rel))   # (a theme channel's file: LATINA/…)
            for sql in ("UPDATE items SET clip_path=? WHERE clip_path=?", "UPDATE items SET overlay_path=? WHERE overlay_path=?",
                        "UPDATE asrun SET path=? WHERE path=?", "UPDATE pad_items SET path=? WHERE path=?", "UPDATE ad_spots SET path=? WHERE path=?",
                        "UPDATE events SET ref=? WHERE ref=?", "UPDATE playlist_sources SET ref=? WHERE kind='song' AND ref=?",
                        "UPDATE clock_slots SET ref=? WHERE kind IN ('song','file','jingle','id') AND ref=?", "UPDATE screen_rules SET file=? WHERE file=?",
                        "UPDATE overlay_rules SET jingle_ref=? WHERE jingle_ref=?"):
                try: c.execute(sql, (new, rel))
                except sqlite3.OperationalError: pass
            for sql in ("UPDATE playlist_sources SET ref=? WHERE kind='file' AND ref=?", "UPDATE clock_slots SET ref=? WHERE kind='file' AND ref=?"):
                try: c.execute(sql, (fn, old_fn))      # "file" slots point at a file name
                except sqlite3.OperationalError: pass
            for pid, ord_ in c.execute("SELECT playlist_id, ord FROM playlist_state WHERE ord LIKE ?", ("%" + os.path.basename(rel)[:40] + "%",)).fetchall():
                try:
                    o = json.loads(ord_)
                    if rel in o: c.execute("UPDATE playlist_state SET ord=? WHERE playlist_id=?", (json.dumps([new if x == rel else x for x in o]), pid))
                except Exception: pass
            c.commit()
        except Exception:
            c.rollback(); os.rename(os.path.join(ROOT, new), src); raise
        for lst in [self.plan] + list(self.aux.values()):
            for p_ in lst:
                if p_.get("path") == rel: p_.update(path=new, artist=artist, title=title)
                if p_.get("clip_path") == rel: p_["clip_path"] = new
        try: self.aux_save_file()
        except Exception: pass
        for ch_ in chancfg.load():      # the theme channels' own databases follow too (their history, pads, playlists, fixed times, hour plans)
            try:
                cc = sqlite3.connect(chancfg.db_path(ch_["id"]), timeout=30)
                for sql in ("UPDATE asrun SET path=? WHERE path=?", "UPDATE pad_items SET path=? WHERE path=?", "UPDATE events SET ref=? WHERE ref=?",
                            "UPDATE playlist_sources SET ref=? WHERE kind='song' AND ref=?",
                            "UPDATE clock_slots SET ref=? WHERE kind IN ('song','file','jingle','id') AND ref=?"):
                    try: cc.execute(sql, (new, rel))
                    except sqlite3.OperationalError: pass
                for pid, ord_ in cc.execute("SELECT playlist_id, ord FROM playlist_state WHERE ord LIKE ?", ("%" + os.path.basename(rel)[:40] + "%",)).fetchall():
                    try:
                        o = json.loads(ord_)
                        if rel in o: cc.execute("UPDATE playlist_state SET ord=? WHERE playlist_id=?", (json.dumps([new if x == rel else x for x in o]), pid))
                    except Exception: pass
                cc.commit(); cc.close()
            except Exception as e: log.warning("rename in channel %s: %s", ch_["id"], e)
        LM_CACHE.pop(rel, None); log.info("renamed: %s -> %s", rel, new)
        try:
            sc = audstats.db(); sc.execute("UPDATE plays SET path=? WHERE path=?", (new, rel)); sc.commit(); sc.close()
        except Exception: pass
        return {"ok": True, "path": new, "artist": artist, "title": title, "filename": fn}
    # ---------- AUX players: two lists you fill by dragging, fire into the log ----------
    def aux_save_file(self):
        try: os.makedirs(ST_DIR, exist_ok=True); jdump(self.aux, SP("/var/lib/hype/aux.json"))
        except Exception as e: log.warning("aux save: %s", e)
    def aux_cmd(self, c, action, d):
        a = str(d.get("aux", "1"))
        if a not in self.aux: return {"ok": False, "error": "no such AUX"}
        L = self.aux[a]
        def mk(path):
            r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (path,)).fetchone()
            return row_item(r, "aux") if r else None
        if action == "aux_add":
            paths = d.get("paths") or [d["path"]]; pos = int(d.get("pos", len(L)))
            for i, p_ in enumerate(paths):
                it = mk(p_)
                if it: L.insert(pos + i, it)
        elif action == "aux_remove": self.aux[a] = [x for x in L if x["id"] != d["id"]]
        elif action == "aux_move":
            it = next((x for x in L if x["id"] == d["id"]), None)
            if it: L.remove(it); L.insert(int(d["to"]), it)
        elif action == "aux_clear": self.aux[a] = []
        elif action == "aux_load":
            pid = int(d["playlist"]); paths = planner.playlist_songs(c, pid)
            if not d.get("append"): self.aux[a] = []
            self.aux[a] += [x for x in (mk(p_) for p_ in paths[:200]) if x]
        elif action == "aux_save":
            name = (d.get("name") or "").strip()
            if not name: return {"ok": False, "error": "give the playlist a name"}
            r = c.execute("SELECT id FROM playlists WHERE name=?", (name,)).fetchone()
            pid = planner.api_save_playlist(c, {"id": r[0] if r else None, "name": name, "loop": False, "shuffle": False, "rotate": False,
                                               "sources": [{"kind": "song", "ref": x["path"]} for x in L]})
            self.aux_save_file(); return {"ok": True, "id": pid}
        elif action == "aux_fire":   # one item: next / now / end of the log
            it = next((x for x in L if x["id"] == d["id"]), None)
            if not it: return {"ok": False, "error": "not found"}
            new = dict(it, id=uuid.uuid4().hex[:10], src="you"); how = d.get("how", "next")
            if self.dj: new.update(src="dj", deck=a)          # in DJ mode it stays this deck's song
            if how == "end": self.plan.append(new)
            else:
                self.take_back_next(); self.plan.insert(0, new); self.push_next(c, force=(how == "now"))
                if how == "now": time.sleep(0.3); self.liq.cmd("radio.cut")      # play now = interrupt (quick cut)
            if d.get("remove"): self.aux[a] = [x for x in self.aux[a] if x["id"] != it["id"]]
        elif action == "aux_all":    # the whole AUX plays after the song on air
            self.take_back_next()
            for i, it in enumerate(L): self.plan.insert(i, dict(it, id=uuid.uuid4().hex[:10], src="you"))
            if d.get("clear"): self.aux[a] = []
            self.push_next(c)
        if not self.auto and action in ("aux_add", "aux_move", "aux_remove", "aux_clear", "aux_load"):
            # MANUAL: the AUX is the running order – what's lined up follows your changes
            top = action == "aux_add" and str(d.get("pos", "")) == "0"
            if top or (self.liq_next and self.liq_next.get("src") == "clock"):
                self.take_back_next()      # put on top of the AUX (or the AUX was empty) → plays next
            n_new = len(d.get("paths") or [1]) if top else 0
            for b_ in reversed([p_ for p_ in self.plan if p_.get("src") in ("aux", "dj")]):
                k_ = b_.get("deck") or "1"; self.aux.setdefault(k_, []).insert(n_new if k_ == a else 0, b_)
            self.plan = self.keep_mine()
            self.refill(c); self.push_next(c)
        self.aux_save_file(); return {"ok": True}
    def get_faders(self):
        if not self.faders:
            for n in ("vol_music", "vol_pads", "vol_studio"):
                try: self.faders[n] = float(self.liq.cmd("var.get " + n).split()[0])
                except Exception: self.faders[n] = 1.0
        return self.faders
    def set_mode(self, c, d):
        mode = d["mode"]; clip = d.get("clip") if mode == "pair" else None
        c.execute("UPDATE items SET mode=?, clip_path=?, mode_set_by_user=1 WHERE path=?", (mode, clip, d["path"])); c.commit()
        for p in self.plan:
            if p["path"] == d["path"]: p["mode"], p["clip_path"] = mode, clip
    def state(self):
        with LOCK:
            n = dict(self.now); el = time.time() - n["started"] if n else 0
            upcoming = ([dict(self.liq_next, locked=True)] if self.liq_next else []) + self.plan
            upcoming = [dict(u, air=round(self.length(u), 1)) for u in upcoming]      # air = START → (smart) MIX: what is really heard
            if n.get("path"): n["air"] = round(self.length(n), 1)
            if n.get("section") == "music" and FIRE_NOW.get("t") and abs(float(FIRE_NOW["t"]) - (n.get("started") or 0)) < 30:
                n["fire"] = int(FIRE_NOW.get("n") or 0)       # 🔥 votes for the song on air (Hype Live sync, ~every minute)
            try:      # the cover (Hype Live's cover store) for the player; the next songs are fetched ahead so theirs is ready
                if n.get("section") == "music": n["cover"] = cover_url(n.get("artist"), n.get("title"))
                for u in [u for u in upcoming if u.get("section") == "music"][:3]: cover_url(u.get("artist"), u.get("title"))
            except Exception: pass
            c = dbc()
            hist = [dict(zip(["started", "artist", "title", "folder", "mode", "scene"], r)) for r in
                    c.execute("SELECT started, artist, title, folder, mode, scene FROM asrun ORDER BY id DESC LIMIT 8")]
            self.tv.ll_on = bool(planner.get_setting(c, "live_layer", True))
            cid = planner.clock_for(c, time.time()); cname = c.execute("SELECT name FROM clocks WHERE id=?", (cid,)).fetchone() if cid else None
            return {"show": SHOW["title"], "clock": cname[0] if cname else None, "now": n, "elapsed": round(el, 1), "upcoming": upcoming, "history": hist, "auto": self.auto,
                    "stop_at_end": self.stop_at_end, "stopped": self.stopped, "scene": self.tv.scene, "auto_scene": self.tv.auto, "aux": self.aux, "dj": {"on": self.dj, "deck": self.deck}, "loop": getattr(self, "loop", False), "faders": self.get_faders(), "elements": self.screen.manual(), "pad_loops": list(self.pad_loops), "media": self.media.cur, "streams": [{"id": x["id"], "name": x["name"], "yt": is_youtube(x["url"])} for x in streams_load(True)], "web": {"on": hypeweb.ST["on"], "starting": hypeweb.ST["starting"]}, "media_vol": MEDIA_VOL["v"], "decks": self.decks_state(), "stream_note": STREAM_NOTE if time.time() - STREAM_NOTE["t"] < 120 else None, "pip_frames": pip_frames(), "pip": os.path.basename(getattr(self.tv, "pip", "") or ""), "scene_groups": self.sgroups, "layouts": self.layouts,
                    "tv_sound": "clip" if self.tv.split else "radio", "live_layer": bool(planner.get_setting(c, "live_layer", True)),
                    "time": time.strftime("%H:%M:%S"), "server_time": time.time(), "slideshows": slideshow.STATE["list"]}

def pair_clip(c, clip_path):
    """Paired song: the TV plays the clip itself, with its own sound, from its own start cue, at radio loudness."""
    r = c.execute("SELECT cue_start, lufs, tv_path, COALESCE(tv_v, 1) FROM items WHERE path=?", (clip_path,)).fetchone() or (0, None, None, 1)
    off = float(planner.get_setting(c, "tv_clip_offset", 3.0))
    gain = (TARGET_LUFS - r[1] + off) if r[1] is not None else off
    # the TV copy (v3: keyframe every 0.5 s + the video's own sound) starts exactly on the START cue; the original file
    # has a keyframe only every few seconds (OBS can only start there: up to 5 s late)
    src = r[2] if (r[2] and r[3] >= 3 and os.path.exists(r[2])) else os.path.join(ROOT, clip_path)
    return src, r[0] or 0, True, gain

def keyframes(c, path):
    """Keyframe times of the TV copy (only valid for the TV copy, not the original file)."""
    r = c.execute("SELECT kf, tv_path FROM items WHERE path=?", (path,)).fetchone()
    try: return json.loads(r[0]) if r and r[0] and r[1] and os.path.exists(r[1]) else None
    except Exception: return None

def tv_file(c, path):
    r = c.execute("SELECT tv_path FROM items WHERE path=?", (path,)).fetchone()
    if r and r[0] and os.path.exists(r[0]): return r[0]
    return os.path.join(ROOT, path)

KIND_SQL = {"audio": "COALESCE(mode, 'audio') IN ('audio', 'pair')", "pair": "mode = 'pair'",
            "noclip": "COALESCE(mode, 'audio') = 'audio'", "video": "mode = 'clip'"}
def to_replace(c, folder="", q=""):
    """Library → ⚠ To replace: songs with something to improve, most played (90 days) first:
    no music video · video under 720p · audio under 190 kbps · a replaced file whose cues need a check."""
    clip_h = dict(c.execute("SELECT path, COALESCE(height, 0) FROM items WHERE section='clips'").fetchall())
    plays = dict(c.execute("SELECT path, COUNT(*) FROM asrun WHERE started > ? GROUP BY path", (time.time() - 90 * 86400,)).fetchall())
    sql = ("SELECT path, artist, title, folder, mode, clip_path, duration, has_video, COALESCE(height,0), size, COALESCE(cue_check,0), filename FROM items "
           "WHERE section='music' AND problem IS NULL AND folder NOT LIKE 'MIXES%' AND folder NOT LIKE 'RECORDINGS%'")
    args = []
    if folder: sql += " AND (folder=? OR folder LIKE ?)"; args += [folder, folder + "/%"]
    for w in q.lower().split(): sql += " AND fold(artist||' '||title||' '||filename) LIKE ?"; args.append(f"%{fold(w)}%")
    out = {}
    for path, ar, ti, fo, mode, cp, dur, hv, h, size, chk, fn in c.execute(sql, args):
        why = []
        if chk: why.append("check cues")
        if mode == "pair": vh = clip_h.get(cp or "", 0)
        elif hv: vh = h
        else: vh = None
        if vh is None: why.append("no video")
        elif 0 < vh < 720: why.append("%dp video" % vh)
        if not hv and dur and size:
            kbps = size * 8 / dur / 1000
            if kbps < 190: why.append("audio %d kbps" % kbps)
        if why: out[path] = (why, plays.get(path, 0))
    return out
def lib_show(ch=None, plan=False):
    """Which stations' songs a channel sees in the Library / Planning folders (Hype Radio = "main"). Default: Hype Radio only its own,
    a theme channel its own + Hype Radio's (e.g. LAT for Hype Latina)."""
    ch = ch or CH                 # (a theme station's own engine: its own station)
    ch = ch if ch and chancfg.valid(ch) else "main"
    try: v = planner.get_setting(sqlite3.connect(DB, timeout=30), "lib_show_" + ch, None)
    except Exception: v = None
    # not set (Library → Stations): the Library follows the channel you're on (only its own songs); Planning also offers Hype Radio's folders
    if not isinstance(v, list): v = ["main"] if ch == "main" else ([ch, "main"] if plan else [ch])
    return [x for x in v if x == "main" or chancfg.valid(x)] or ["main"]
def station_sql(ch=None, plan=False):
    sts = ["" if x == "main" else x for x in lib_show(ch, plan)]
    return "COALESCE(station, '') IN (%s)" % ",".join("'%s'" % x for x in sts)      # ids are [a-z0-9] only (chancfg.ID_RE)
def library(qs):
    c = dbc(); q = (qs.get("q", [""])[0]).strip(); sec = qs.get("section", ["music"])[0]; folder = qs.get("folder", [""])[0]
    chq = (qs.get("ch") or [""])[0]
    fix = qs.get("fix", ["0"])[0] == "1" and sec == "music"
    FIX = to_replace(c, folder, q) if fix else None
    sql = "SELECT path, artist, title, folder, mode, clip_path, clip_suggest, clip_score, duration, has_video, problem, overlay_mode, overlay_path, cue_start, cue_mix, cue_end, cues_edited, added_at, COALESCE(mix_set, 0), COALESCE(cue_check, 0), COALESCE(tempo, 1.0), COALESCE(tempo_pending, 0) FROM items WHERE section=?"
    mixes = sec == "mixes"; recs = sec == "recordings"
    if recs: sec = "music"; sql += " AND folder LIKE 'RECORDINGS%'"           # recordings (REC / GO LIVE / auto): a Library tab of their own
    elif sec in ("music", "mixes"): sec = "music"; sql += (" AND folder LIKE 'MIXES%'" if mixes else " AND folder NOT LIKE 'MIXES%' AND folder NOT LIKE 'RECORDINGS%'")
    if sec in ("music", "jingles"): sql += " AND " + station_sql(chq)      # theme channels: only the stations this channel shows
    args = [sec]; sort = qs.get("sort", ["az"])[0]; recent = int(qs.get("recent", ["0"])[0] or 0)
    if recent: sql += " AND added_at > ?"; args.append(time.time() - recent * 86400)   # uploaded in the last N days
    if folder: sql += " AND (folder=? OR folder LIKE ?)"; args += [folder, folder + "/%"]
    kind = qs.get("kind", [""])[0]
    if sec == "music" and kind in KIND_SQL: sql += " AND " + KIND_SQL[kind]
    if qs.get("auto", ["0"])[0] == "1": sql += " AND COALESCE(cues_edited, 0) = 0"
    if fix:
        sql += " AND path IN (SELECT value FROM json_each(?))"; args.append(json.dumps(list(FIX)))
    for w in q.lower().split():
        sql += " AND fold(artist||' '||title||' '||filename) LIKE ?"; args.append(f"%{fold(w)}%")
    total = c.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
    try: lim = max(1, min(1000, int(qs.get("limit", ["300"])[0]))); off = max(0, int(qs.get("offset", ["0"])[0]))
    except ValueError: lim, off = 300, 0
    if fix:        # most played first: fix what matters most
        sql += " ORDER BY (SELECT COUNT(*) FROM asrun a WHERE a.path = items.path AND a.started > %d) DESC, artist, title LIMIT ? OFFSET ?" % int(time.time() - 90 * 86400); args += [lim, off]
    else: sql += " ORDER BY " + ("added_at DESC, artist, title" if sort == "new" else "(problem IS NULL), problem, artist, title" if sort == "problems" else "artist, title") + " LIMIT ? OFFSET ?"; args += [lim, off]
    last = {r[0]: r[1] for r in c.execute("SELECT path, MAX(started) FROM asrun GROUP BY path")}
    k = ["path", "artist", "title", "folder", "mode", "clip_path", "clip_suggest", "clip_score", "duration", "has_video", "problem", "overlay_mode", "overlay_path", "cue_start", "cue_mix", "cue_end", "cues_edited", "added_at", "mix_set", "cue_check", "tempo", "tempo_pending"]
    out = []
    for r in c.execute(sql, args):
        d = dict(zip(k, r)); d["last"] = last.get(d["path"]); out.append(d)
        if fix: d["fix"], d["plays90"] = FIX.get(d["path"], ([], 0))
    folders = [r[0] for r in c.execute("SELECT DISTINCT folder FROM items WHERE section=? AND " + (station_sql(chq) if sec in ("music", "jingles") else "1=1") + " ORDER BY folder", (sec,))]
    if recs: folders = [f for f in folders if (f or "").upper().startswith("RECORDINGS")]
    elif sec == "music": folders = [f for f in folders if (f or "").upper().startswith("MIXES") == mixes and not (f or "").upper().startswith("RECORDINGS")]
    if sec in ("music", "jingles") and "main" in set(lib_show(chq)):     # Hype Radio's EMPTY folders too (22 Nov: MIXES/SATURDAY 21 was gone from the list once its mix was archived – nowhere to put the next one)
        base = os.path.join(ROOT, sec)
        for dp, dn, _ in os.walk(base):
            dn[:] = [x for x in dn if not x.startswith(".")]
            rel = os.path.relpath(dp, base)
            if rel == ".": continue
            if rel.count(os.sep) >= 2: dn[:] = []
            f = rel.replace(os.sep, "/"); U = f.upper()
            if sec == "music" and (U.startswith("MIXES") != mixes or (U.startswith("RECORDINGS") != recs)): continue
            if f not in folders: folders.append(f)
        folders.sort()
    all_folders = list(folders)
    if sec in ("music", "jingles") and not mixes and not recs:      # a theme channel's folders on disk (also still empty) – for Upload
        vis = set(lib_show(chq))
        for cfg in chancfg.load():
            base = os.path.join(ROOT, "%s-%s" % (sec, cfg["id"]))
            if cfg["id"] not in vis or not os.path.isdir(base): continue
            for dp, dn, _ in os.walk(base):
                dn[:] = [x for x in dn if not x.startswith(".")]
                rel = os.path.relpath(dp, base); depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth > 3: dn[:] = []; continue
                f = cfg["id"].upper() + ("" if rel == "." else "/" + rel.replace(os.sep, "/"))
                if f not in all_folders: all_folders.append(f)
        all_folders.sort()
    counts = dict(c.execute("SELECT section, COUNT(*) FROM items GROUP BY section").fetchall())
    counts["mixes"] = c.execute("SELECT COUNT(*) FROM items WHERE section='music' AND folder LIKE 'MIXES%'").fetchone()[0]
    counts["recordings"] = c.execute("SELECT COUNT(*) FROM items WHERE section='music' AND folder LIKE 'RECORDINGS%'").fetchone()[0]
    counts["mixes"] -= counts["recordings"]
    counts["music"] = counts.get("music", 0) - counts["mixes"] - counts["recordings"]
    psql = "SELECT COUNT(*) FROM items WHERE section=? AND problem IS NOT NULL" + ((" AND folder LIKE 'MIXES%'" if mixes else " AND folder NOT LIKE 'MIXES%' AND folder NOT LIKE 'RECORDINGS%'") if sec == "music" else "")
    fixn = None
    if sec == "music" and not mixes and not recs:
        fx = FIX if FIX is not None else to_replace(c); fixn = {"total": len(fx)}
        for why, _n in fx.values():
            for w in why: k_ = "no video" if w == "no video" else "low video" if w.endswith("p video") else "low audio" if w.startswith("audio") else "check cues"; fixn[k_] = fixn.get(k_, 0) + 1
    return {"fixn": fixn, "problems": c.execute(psql, (sec,)).fetchone()[0], "items": out, "folders": folders, "all_folders": all_folders, "station_roots": {x["id"].upper(): x["id"] for x in chancfg.load()}, "counts": counts, "total": total, "offset": off}

# ---------- upload from the Library page (WinSCP stays the main way; this is for a phone / someone without server access) ----------
def lib_root_ok(rel):
    """A file in one of the library's roots: Hype Radio's (music, clips, jingles, ads) or a theme channel's (music-latina, jingles-ambiente …)."""
    top = rel.split("/")[0]
    return top in ("music", "clips", "jingles", "ads") or bool(re.fullmatch(r"(music|jingles)-[a-z][a-z0-9]{1,19}", top))
UP_DIR = "/srv/hype-data/uploads"; UP_SECTIONS = ("music", "clips", "jingles", "ads")
UP_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".mp4", ".mov", ".mkv", ".m4v", ".webm"}
def up_check(qs):
    uid = qs.get("id", [""])[0]
    if not re.fullmatch(r"[a-f0-9]{16,64}", uid): raise ValueError("bad upload id")
    folder = os.path.normpath(qs.get("folder", [""])[0]).strip("/"); parts = folder.split("/")
    if folder == "_vt":           # a voice-tracking take (webm from the browser): staged outside the library until vt_save converts it
        name = os.path.basename(qs.get("name", [""])[0]).strip(); total = int(qs.get("total", ["0"])[0] or 0)
        if not re.fullmatch(r"vt_[0-9a-z_]{1,60}\.(webm|ogg|m4a|mp4)", name): raise ValueError("bad take name")
        if total <= 0 or total > 600 * 1024 ** 2: raise ValueError("take too big")
        os.makedirs(VT_IN, exist_ok=True); return uid, VT_IN, name, total
    media = folder == "branding/media" or (len(parts) == 4 and parts[:2] == ["branding", "slideshow"] and parts[2] in ("photos", "videos"))   # media pads / a slideshow
    if (parts[0] not in UP_SECTIONS and not re.match(r"^(music|jingles)-[a-z][a-z0-9]{1,19}$", parts[0]) and not media) or ".." in parts or folder.startswith("."): raise ValueError("choose a library folder")
    d = os.path.join(ROOT, folder)
    if not os.path.isdir(d): raise ValueError("folder not found")
    name = os.path.basename(qs.get("name", [""])[0]).replace("\x00", "").strip()
    ext = os.path.splitext(name)[1].lower()
    if media and ext not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".m4v"): raise ValueError("only pictures (png, jpg, webp) or videos (mp4, mov, webm)")
    if not name or name.startswith(".") or (not media and ext not in UP_EXT): raise ValueError("only audio / video files")
    if BAD_NAME.search(name): raise ValueError("the file name has control characters (e.g. a line break) - rename it")
    total = int(qs.get("total", ["0"])[0] or 0)
    if total <= 0 or total > 8 * 1024 ** 3: raise ValueError("file too big (max 8 GB)")
    return uid, d, name, total
def up_status(qs):
    uid = qs.get("id", [""])[0]
    if not re.fullmatch(r"[a-f0-9]{16,64}", uid): return {"ok": False, "error": "bad upload id"}
    dn = up_done(uid)
    if dn: return dn
    f = os.path.join(UP_DIR, uid + ".part")
    return {"ok": True, "have": os.path.getsize(f) if os.path.exists(f) else 0}
def up_done(uid):
    """An upload that already finished (its last piece arrived but the answer got lost): <id>.done says where it went."""
    try: dn = json.load(open(os.path.join(UP_DIR, uid + ".done")))
    except (OSError, ValueError): return None
    return {"ok": True, "done": True, "path": dn.get("path"), "have": dn.get("size", 0)}
def up_chunk(qs, rfile, n):
    uid = qs.get("id", [""])[0]
    if re.fullmatch(r"[a-f0-9]{16,64}", uid):
        dn = up_done(uid)
        if dn: rfile.read(n); return dn
    try: uid, d, name, total = up_check(qs)
    except ValueError as e:
        rfile.read(n); return {"ok": False, "error": str(e)}
    os.makedirs(UP_DIR, exist_ok=True)
    for f in os.listdir(UP_DIR):                               # unfinished uploads are given up after 2 days (and .done notes are forgotten)
        fp = os.path.join(UP_DIR, f)
        try:
            if time.time() - os.path.getmtime(fp) > 2 * 86400: os.remove(fp)
        except OSError: pass
    part = os.path.join(UP_DIR, uid + ".part"); have = os.path.getsize(part) if os.path.exists(part) else 0
    off = int(qs.get("off", ["0"])[0] or 0)
    if off != have: rfile.read(n); return {"ok": True, "have": have}          # resync (a piece was lost or sent twice)
    if have == 0 and shutil.disk_usage(ROOT).free < total + 5 * 1024 ** 3: rfile.read(n); return {"ok": False, "error": "not enough space on the server"}
    with open(part, "ab") as f:
        left = n
        while left > 0:
            b = rfile.read(min(1 << 20, left))
            if not b: break
            f.write(b); left -= len(b)
    have = os.path.getsize(part)
    if have > total: os.remove(part); return {"ok": False, "error": "received more than the file size - try again"}
    if have < total: return {"ok": True, "have": have}
    stem, ext = os.path.splitext(name); dst = os.path.join(d, name); k = 2
    rep = (qs.get("replace") or [""])[0]
    if rep:        # Library → To replace: the new file takes the place of the old one (same name; cues, history, playlists stay)
        try: full_old = lib_file(rep)
        except ValueError: os.remove(part); return {"ok": False, "error": "the song to replace is not there any more"}
        old_ext = os.path.splitext(rep)[1].lower()
        if ext.lower() == old_ext:
            dst = full_old
        else:      # another type (e.g. an MP3 replaced by its music video): the old file gets the new name and every link
            dst = os.path.splitext(full_old)[0] + ext.lower()         # follows it (rename_item); then the new file is written over it
            if os.path.exists(dst): os.remove(part); return {"ok": False, "error": "a file with that name and type is already there"}
            VIDX = (".mp4", ".mov", ".mkv", ".m4v", ".webm")
            if old_ext in VIDX and ext.lower() not in VIDX:
                # a music video replaced by a better AUDIO file: the video is kept as the song's paired clip (clips/<same name>):
                # the radio plays the new audio, the TV shows the video (paired by name), history / playlists stay with the song
                cdir = os.path.join(ROOT, "clips"); cb = os.path.basename(full_old); cst, cex = os.path.splitext(cb); cdst = os.path.join(cdir, cb); kk = 2
                while os.path.exists(cdst): cdst = os.path.join(cdir, "%s (%d)%s" % (cst, kk, cex)); kk += 1
                try: os.link(full_old, cdst)          # (same disk: instant, no copy)
                except OSError: shutil.copy2(full_old, cdst)
                try:
                    import pwd as _pw, grp as _gr; os.chown(cdst, _pw.getpwnam("upload").pw_uid, _gr.getgrnam("hype").gr_gid)
                except Exception: pass
                log.info("replaced by audio: the video stays as its paired clip -> %s", os.path.relpath(cdst, ROOT))
                to_audio = True
            else: to_audio = False
            try: r_ = E.rename_item(dbc(), {"path": rep, "name": os.path.splitext(os.path.basename(rep))[0], "ext": ext.lower()})
            except Exception as e: r_ = {"ok": False, "error": str(e)}
            if not r_ or r_.get("ok") is False: os.remove(part); return {"ok": False, "error": (r_ or {}).get("error") or "could not replace"}
            if to_audio:     # it is an audio song now: no own TV copy any more; the indexer pairs it with its clip by name
                try:
                    c_ = dbc(); c_.execute("UPDATE items SET tv_path=NULL, kf=NULL, align=NULL, mode='audio', mode_set_by_user=0, clip_path=NULL WHERE path=?", (r_.get("path"),)); c_.commit()
                except Exception as e: log.warning("replace by audio: %s", e)
        try:       # a replaced song starts at its own tempo again (the kept original of an older version is not needed)
            c_ = dbc(); t_ = c_.execute("SELECT tempo_src FROM items WHERE path=?", (os.path.relpath(dst, ROOT),)).fetchone()
            c_.execute("UPDATE items SET tempo=NULL, tempo_src=NULL WHERE path=?", (os.path.relpath(dst, ROOT),)); c_.commit()
            if t_ and t_[0] and t_[0].startswith(TEMPO_DIR + "/") and os.path.isfile(t_[0]): os.remove(t_[0])
        except Exception as e: log.warning("replace: tempo reset: %s", e)
        log.info("replaced from the Library page: %s -> %s", rep, os.path.relpath(dst, ROOT))
    else:
        while os.path.exists(dst): dst = os.path.join(d, f"{stem} ({k}){ext}"); k += 1
    shutil.move(part, dst)
    try:
        import pwd, grp; os.chown(dst, pwd.getpwnam("upload").pw_uid, grp.getgrnam("hype").gr_gid)
    except Exception: pass
    os.chmod(dst, 0o664)
    if os.path.dirname(dst) == MEDIA_DIR:       # complete already: the media pads may show it at once (they skip files changed < 20 s ago)
        t = time.time() - 30; os.utime(dst, (t, t))
    rel = os.path.relpath(dst, ROOT)
    try: jdump({"path": rel, "size": have}, os.path.join(UP_DIR, uid + ".done"))   # a retried last piece / status check gets "done" (no 2nd copy)
    except Exception as e: log.warning("upload done note: %s", e)
    log.info("uploaded from the Library page: %s (%.1f MB)", rel, total / 1048576)
    return {"ok": True, "have": have, "done": True, "path": rel}

# ---------- voice tracking: your own links between the songs of a playlist or of a locked hour ----------
VT_DIR = os.path.join(ROOT, JDIR, "VOICETRACK"); VT_IN = os.path.join(UP_DIR, "vt")     # (a theme station: its own jingles-<id>/VOICETRACK)
VT_MIX = {}          # voice link -> the second the next song starts under it (None = after the link, as a jingle)
VT_PRE = {}          # voice link -> how many seconds before the song before ends it starts (over the outro, the song low under it)
def vt_db(c):
    c.execute("CREATE TABLE IF NOT EXISTS vt(path TEXT PRIMARY KEY, pid INT, tv TEXT DEFAULT '', name TEXT DEFAULT '', video INT DEFAULT 0, created REAL)")
    cols = [r[1] for r in c.execute("PRAGMA table_info(vt)")]
    for col, typ in (("mix_at", "REAL"), ("pre", "REAL"), ("bed", "TEXT")):
        if col not in cols: c.execute("ALTER TABLE vt ADD COLUMN %s %s" % (col, typ))
    c.execute("CREATE TABLE IF NOT EXISTS vt_hours(pid INT PRIMARY KEY, date TEXT, hour INT, clock_id INT)")
    return c
def vt_load():
    if "_loaded" in VT_MIX: return True
    try:
        for p_, m, pr in vt_db(dbc()).execute("SELECT path, mix_at, pre FROM vt"):
            if m and m > 0: VT_MIX[p_] = m
            if pr and pr > 0: VT_PRE[p_] = pr
        VT_MIX["_loaded"] = 1; return True
    except Exception as e: log.warning("vt load: %s", e); return False
def vt_mix_at(path): return VT_MIX.get(path) if vt_load() else None
def vt_pre(path): return VT_PRE.get(path) if vt_load() else None
def vt_beds(c):
    """Music beds for voice links: the jingles in a folder with BED in its name (e.g. jingles/BEDS)."""
    return [{"path": p_, "title": t or os.path.basename(p_), "duration": d_} for p_, t, d_ in c.execute(
        "SELECT path, title, duration FROM items WHERE section='jingles' AND problem IS NULL AND UPPER(folder) LIKE '%BED%' ORDER BY title COLLATE NOCASE")]
def vt_list(c, pid):
    vt_db(c)
    r = c.execute("SELECT name, strict, shuffle FROM playlists WHERE id=?", (pid,)).fetchone()
    if not r: return {"ok": False, "error": "playlist not found"}
    src = c.execute("SELECT kind, ref FROM playlist_sources WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall()
    fixed = all(k == "song" for k, _ in src)
    paths = [ref for _, ref in src] if fixed else planner.playlist_songs(c, pid)
    out = []
    for i, p_ in enumerate(paths):
        row = c.execute("SELECT artist, title, section, duration, cue_start, cue_mix, cue_end, has_video, problem, cue_intro FROM items WHERE path=?", (p_,)).fetchone()
        v = c.execute("SELECT tv, name, video, mix_at, pre, bed FROM vt WHERE path=?", (p_,)).fetchone()
        it = {"i": i, "path": p_, "missing": not row}
        if row: it.update(artist=row[0], title=row[1], section=row[2], duration=row[3], cue_start=row[4] or 0, cue_mix=row[5], cue_end=row[6], video=bool(row[7]), problem=row[8], cue_intro=row[9])
        if v and v[3]: it["cue_mix"] = v[3]; it["over_intro"] = True
        if v and v[4]: it["pre"] = v[4]
        if v and v[5]: it["bed"] = os.path.splitext(os.path.basename(v[5]))[0]
        if v or p_.startswith(VT_PFX): it["vt"] = {"tv": (v or ("",))[0], "name": (v or ("", ""))[1], "video": bool((v or (0, 0, 0))[2])}
        out.append(it)
    h = c.execute("SELECT date, hour, clock_id FROM vt_hours WHERE pid=?", (pid,)).fetchone()
    return {"ok": True, "id": pid, "name": r[0], "fixed": fixed, "strict": bool(r[1]), "shuffle": bool(r[2]), "items": out,
            "hour": {"date": h[0], "hour": h[1], "start": time.mktime(time.strptime(h[0] + " %02d" % h[1], "%Y-%m-%d %H"))} if h else None,
            "beds": vt_beds(c)}
def vt_write(c, pid, paths):
    """The playlist becomes exactly this list (plays as listed, once, no shuffle)."""
    c.execute("DELETE FROM playlist_sources WHERE playlist_id=?", (pid,))
    for i, p_ in enumerate(paths): c.execute("INSERT INTO playlist_sources(playlist_id, pos, kind, ref, amount) VALUES(?,?,?,?,1)", (pid, i, "song", p_))
    c.execute("UPDATE playlists SET strict=1, shuffle=0, rotate=0, updated=? WHERE id=?", (time.time(), pid)); c.commit()
def vt_paths(c, pid):
    src = c.execute("SELECT kind, ref FROM playlist_sources WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall()
    return [r for k, r in src] if all(k == "song" for k, _ in src) else planner.playlist_songs(c, pid)
def vt_save(d):
    """A recorded take → a proper file in jingles/VOICETRACK/<playlist> (AAC, or H.264 + AAC with video), put in the list after song n."""
    pid = int(d["pid"]); after = int(d.get("after", -1)); take = os.path.basename(str(d.get("take") or ""))
    src = os.path.join(VT_IN, take)
    if not re.fullmatch(r"vt_[0-9a-z_]{1,60}\.(webm|ogg|m4a|mp4)", take) or not os.path.isfile(src): return {"ok": False, "error": "the take was not uploaded"}
    c = vt_db(dbc()); r = c.execute("SELECT name FROM playlists WHERE id=?", (pid,)).fetchone()
    if not r: return {"ok": False, "error": "playlist not found"}
    video = bool(d.get("video")); ts = float(d.get("trim_start") or 0); te = float(d.get("trim_end") or 0)
    pre = max(0.0, min(30.0, float(d.get("pre") or 0)))
    bed = str(d.get("bed") or ""); bed_db = max(-30.0, min(-3.0, float(d.get("bed_db") or -14)))
    if bed and not (bed.startswith("jingles/") and c.execute("SELECT 1 FROM items WHERE path=? AND section='jingles'", (bed,)).fetchone()): bed = ""
    safe = re.sub(r"[^A-Za-z0-9 _-]+", "", r[0]).strip()[:40] or "list"
    folder = os.path.join(VT_DIR, safe); os.makedirs(folder, exist_ok=True)
    base = "VT %s %s" % (safe, time.strftime("%m%d-%H%M%S")); dst = os.path.join(folder, base + (".mp4" if video else ".m4a"))
    cut = (["-ss", "%.2f" % ts] if ts > 0 else []) + (["-to", "%.2f" % te] if te > ts else [])
    a = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    if bed:     # your voice on a music bed (looped under it, 2.5 s more at the end, then it fades out)
        fc = ("[0:a]highpass=f=70,afade=t=in:d=0.03,apad=pad_dur=2.5[v];[1:a]volume=%.1fdB[b];[v][b]amix=inputs=2:duration=first:normalize=0[m0];"
              "[m0]areverse,afade=t=in:d=2.5,areverse[m]" % bed_db)
        ins = ["-i", src, "-stream_loop", "-1", "-i", os.path.join(ROOT, bed)]
        a = ["-filter_complex", fc, "-map", "[m]"] + (["-map", "0:v"] if video else []) + a
    else:
        ins = ["-i", src]; a = ["-af", "highpass=f=70,afade=t=in:d=0.03"] + a
    cmd = ["nice", "-n", "10", "ffmpeg", "-y", "-loglevel", "error"] + ins + cut + ((["-vf", "scale=-2:720,fps=30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-g", "15"] if video else ["-vn"]) + a + ["-movflags", "+faststart", dst + ".tmp" + (".mp4" if video else ".m4a")])
    p_ = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    tmp = dst + ".tmp" + (".mp4" if video else ".m4a")
    if p_.returncode or not os.path.isfile(tmp): log.warning("vt convert: %s", p_.stderr[-300:]); return {"ok": False, "error": "could not convert the take"}
    os.replace(tmp, dst); os.remove(src)
    try:
        import pwd, grp; uid_, gid_ = pwd.getpwnam("upload").pw_uid, grp.getgrnam("hype").gr_gid
        for x in (VT_DIR, folder, dst): os.chown(x, uid_, gid_)
    except Exception: pass
    os.chmod(dst, 0o664); rel = os.path.relpath(dst, ROOT)
    with LOCK:
        paths = vt_paths(c, pid); paths.insert(max(0, min(len(paths), after + 1)), rel); vt_write(c, pid, paths)
        ma = float(d.get("mix_at") or 0) - (ts if ts > 0 else 0); ma = ma if ma > 0.5 else None
        c.execute("INSERT OR REPLACE INTO vt(path, pid, tv, name, video, created, mix_at, pre, bed) VALUES(?,?,?,?,?,?,?,?,?)", (rel, pid, str(d.get("tv") or "")[:80], str(d.get("name") or "")[:80], int(video), time.time(), ma, pre or None, bed or None)); c.commit()
        if ma: VT_MIX[rel] = ma
        if pre: VT_PRE[rel] = pre
    log.info("voice link saved: %s (after item %d of playlist %d)", rel, after, pid)
    return {"ok": True, "path": rel}
def vt_cmd(a, d):
    c = vt_db(dbc())
    if a == "save": return vt_save(d)
    pid = int(d.get("pid") or 0); PV = None
    if a == "lock":              # the hour's songs, worked out before taking the engine's lock (the preview runs on a copy)
        try:
            at_ = time.mktime(time.strptime(str(d.get("date")) + " %02d" % int(d.get("hour", -1)), "%Y-%m-%d %H"))
            bc = planner.clock_for(c, at_); PV = (bc, planner.preview_hour(c, at=at_, clock=bc))
        except (ValueError, OverflowError): return {"ok": False, "error": "choose a date and an hour"}
    with LOCK:
        if a == "fix":           # a playlist with folders / shuffle → this exact list (the order you see)
            vt_write(c, pid, vt_paths(c, pid)); return {"ok": True}
        if a == "remove":        # a song or a voice link out of the list (a link's file goes too)
            paths = vt_paths(c, pid); p_ = str(d.get("path")); i = int(d.get("i", -1))
            if not (0 <= i < len(paths)) or paths[i] != p_: return {"ok": False, "error": "the list changed – reload"}
            paths.pop(i); vt_write(c, pid, paths)
            if p_.startswith(VT_PFX) and p_ not in paths:
                c.execute("DELETE FROM vt WHERE path=?", (p_,)); c.commit()
                try: os.remove(lib_file(p_))
                except (OSError, ValueError): pass
            return {"ok": True}
        if a == "move":          # a voice link one place up / down
            paths = vt_paths(c, pid); i = int(d.get("i", -1)); j = i + (1 if d.get("dir") == "down" else -1)
            if not (0 <= i < len(paths) and 0 <= j < len(paths)): return {"ok": False}
            paths[i], paths[j] = paths[j], paths[i]; vt_write(c, pid, paths); return {"ok": True}
        if a == "pre":           # how far over the end of the song before a link starts (0 = at its mix point)
            pr = max(0.0, min(30.0, float(d.get("pre") or 0))); p_ = str(d.get("path"))
            c.execute("UPDATE vt SET pre=? WHERE path=?", (pr or None, p_)); c.commit()
            if pr: VT_PRE[p_] = pr
            else: VT_PRE.pop(p_, None)
            return {"ok": True}
        if a == "tv":            # what the TV shows during a voice-only link: '' = automatic, 'scene:<name>', 'name' (lower third)
            c.execute("UPDATE vt SET tv=?, name=? WHERE path=?", (str(d.get("tv") or "")[:80], str(d.get("name") or "")[:80], str(d.get("path")))); c.commit(); return {"ok": True}
        if a == "lock":          # a clock hour on a date, built now and kept: a playlist + a clock for that one hour
            dt = str(d.get("date") or ""); hr = int(d.get("hour", -1))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt) or not 0 <= hr <= 23: return {"ok": False, "error": "choose a date and an hour"}
            at = time.mktime(time.strptime(dt + " %02d" % hr, "%Y-%m-%d %H"))
            if at < time.time(): return {"ok": False, "error": "that hour has started already"}
            if at > time.time() + 8 * 86400: return {"ok": False, "error": "at most 8 days ahead"}
            name = "VT " + time.strftime("%a %d %b %H:00", time.localtime(at))
            old = c.execute("SELECT id FROM playlists WHERE name=?", (name,)).fetchone()
            if old: return {"ok": True, "pid": old[0], "already": True}
            base_cid, pv = PV
            paths = [x["path"] for x in pv.get("items", []) if not x.get("fixed")]
            if not paths: return {"ok": False, "error": "the clock gives nothing for that hour"}
            pid = c.execute("INSERT INTO playlists(name, loop, shuffle, rotate, strict, updated) VALUES(?,0,0,0,1,?)", (name, time.time())).lastrowid
            vt_write(c, pid, paths)
            slots = [{"kind": "playlist", "ref": str(pid), "amount": 0, "start": "continue"}]
            slots += [dict(kind=k, ref=r, amount=a_, start=st) for k, r, a_, st in (planner.slots(c, base_cid) if base_cid else [])]   # after your list: the normal clock
            cid = planner.api_save_clock(c, {"name": name, "color": "#FF4D5E", "resume": False, "slots": slots})
            planner.api_set_schedule_date(c, {"cells": [{"date": dt, "hour": hr, "clock_id": cid}]})
            c.execute("INSERT OR REPLACE INTO vt_hours VALUES(?,?,?,?)", (pid, dt, hr, cid)); c.commit()
            E.replan(); return {"ok": True, "pid": pid}
        if a == "unlock":        # the hour goes back to its normal clock; the list and its voice links are deleted
            h = c.execute("SELECT date, hour, clock_id FROM vt_hours WHERE pid=?", (pid,)).fetchone()
            if not h: return {"ok": False, "error": "not a locked hour"}
            planner.api_set_schedule_date(c, {"cells": [{"date": h[0], "hour": h[1], "clock_id": None}]})
            c.execute("DELETE FROM clocks WHERE id=?", (h[2],)); c.execute("DELETE FROM clock_slots WHERE clock_id=?", (h[2],))
            for (p_,) in c.execute("SELECT path FROM vt WHERE pid=?", (pid,)).fetchall():
                try: os.remove(lib_file(p_))
                except (OSError, ValueError): pass
            c.execute("DELETE FROM vt WHERE pid=?", (pid,)); c.execute("DELETE FROM vt_hours WHERE pid=?", (pid,))
            c.execute("DELETE FROM playlists WHERE id=?", (pid,)); c.execute("DELETE FROM playlist_sources WHERE playlist_id=?", (pid,)); c.execute("DELETE FROM playlist_state WHERE playlist_id=?", (pid,)); c.commit()
            E.replan(); return {"ok": True}
    return {"ok": False, "error": "unknown"}
def vt_hours_list():
    c = vt_db(dbc()); now = time.time(); out = []
    for pid, dt, hr, cid in c.execute("SELECT pid, date, hour, clock_id FROM vt_hours ORDER BY date, hour").fetchall():
        at = time.mktime(time.strptime(dt + " %02d" % hr, "%Y-%m-%d %H"))
        if at + 3600 < now - 86400: continue
        n = c.execute("SELECT COUNT(*) FROM vt WHERE pid=?", (pid,)).fetchone()[0]
        out.append({"pid": pid, "date": dt, "hour": hr, "start": at, "links": n, "past": at + 3600 < now})
    return out
def vt_on_air(c, path, it):
    """A voice link starts: what the TV shows (a scene, or your name as a lower third) when you chose that for it."""
    try: r = vt_db(c).execute("SELECT tv, name, video FROM vt WHERE path=?", (path,)).fetchone()
    except Exception: return
    if not r or r[2]: return
    tv, name = r[0] or "", r[1] or ""
    if tv.startswith("scene:"):
        try: E.tv.set_scene(tv[6:])
        except Exception as e: log.warning("voice link scene: %s", e)
    elif tv == "name" and name.strip():
        lower_cmd({"action": "show", "name": name.strip(), "role": ""})
        dur = max(3.0, (it.get("cue_end") or it.get("duration") or 8) - (it.get("cue_start") or 0) - 0.5)
        threading.Timer(dur, lambda: E.now.get("path") == path and lower_cmd({"action": "hide"})).start()

def chan_replan(ch):
    try:
        rq = urllib.request.Request("http://127.0.0.1:%d/cmd" % chancfg.API_PORT, data=json.dumps({"ch": ch, "action": "replan"}).encode(),
                                    method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(rq, timeout=3).read()
    except Exception: pass               # the channel is off: it plans from scratch when it starts anyway

# ---------- Library → Tempo: a song a bit faster, the pitch stays (27 Nov) ----------
# Studio makes a faster copy of the song (sound + picture together, so lip sync stays) and puts it in the song's place; the
# original is kept in TEMPO_DIR and comes back when you choose "Original". START / MIX / END move with the new length.
TEMPO_OK = (1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07)    # (27 Nov, Gianno: steps of 0.01 up to 1.07)
TEMPO_DIR = "/srv/hype-data/tempo"
TEMPO_JOBS = {}            # path -> {"state": "busy"|"done"|"error", "msg": ..., "t": ...}
def _tempo_fps(f):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", f],
                           capture_output=True, text=True, timeout=20).stdout.strip().split("/")
        v = float(r[0]) / float(r[1] if len(r) > 1 else 1)
        return v if 10 <= v <= 120 else 25.0
    except Exception: return 25.0
def _tempo_render(src, dst, x, video):
    ext = os.path.splitext(dst)[1].lower()
    # gently: the on-air sound, the TV and OBS always come first (27 Nov: with 6 threads the load went to 7.6 and the
    # TV sound / lip sync suffered at the next song start)
    cmd = ["ionice", "-c", "3", "nice", "-n", "19", "ffmpeg", "-v", "error", "-y", "-threads", "2", "-filter_threads", "1", "-i", src]
    if video:
        fps = _tempo_fps(src)
        cmd += ["-filter_complex", f"[0:v:0]setpts=PTS/{x},fps={fps:.3f}[v];[0:a:0]rubberband=tempo={x}[a]", "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "2",
                "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]
    else:
        codec = {".mp3": ["libmp3lame", "-b:a", "320k"], ".m4a": ["aac", "-b:a", "256k"], ".aac": ["aac", "-b:a", "256k"],
                 ".flac": ["flac"], ".wav": ["pcm_s16le"], ".ogg": ["libvorbis", "-q:a", "7"], ".opus": ["libopus", "-b:a", "192k"]}.get(ext)
        if not codec: raise ValueError("tempo: unknown audio type " + ext)
        cmd += ["-map", "0:a:0", "-af", f"rubberband=tempo={x}", "-map_metadata", "0", "-c:a"] + codec
    cmd += ["-f", {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska", ".mp3": "mp3", ".m4a": "ipod", ".aac": "adts",
                   ".flac": "flac", ".wav": "wav", ".ogg": "ogg", ".opus": "opus"}[ext], dst]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 10000:
        raise RuntimeError("ffmpeg: " + (p.stderr or "no output").strip()[-300:])
def tempo_set(d):
    path = str(d.get("path") or ""); x = round(float(d.get("tempo") or 1.0), 3)
    if x not in TEMPO_OK: return {"ok": False, "error": "choose Original or 1.01 – 1.07"}
    c = dbc()
    r = c.execute("SELECT section, has_video, clip_path, COALESCE(tempo, 1.0), tempo_src, rowid FROM items WHERE path=?", (path,)).fetchone()
    if not r: return {"ok": False, "error": "song not found"}
    sec, hv, clip, cur, src, iid = r
    if sec != "music": return {"ok": False, "error": "tempo is for songs only"}
    if clip: return {"ok": False, "error": "this song plays with a separate (paired) video clip - tempo works for songs that are one file"}
    ext = os.path.splitext(path)[1].lower()
    if hv and ext not in (".mp4", ".m4v", ".mov", ".mkv"): return {"ok": False, "error": "tempo works for mp4 / mov / mkv videos"}
    if abs(x - cur) < 0.001: return {"ok": True, "tempo": x, "same": True}
    if (TEMPO_JOBS.get(path) or {}).get("state") == "busy": return {"ok": False, "error": "the faster version of this song is still being made"}
    TEMPO_JOBS[path] = {"state": "busy", "msg": "making the %s version…" % ("original" if x == 1.0 else "%g×" % x), "t": time.time(), "tempo": x}
    def go():
        try:
            full = lib_file(path); dname, base = os.path.split(full); tmp = os.path.join(dname, ".tempo_" + base)
            os.makedirs(TEMPO_DIR, exist_ok=True)
            orig = src if src and os.path.isfile(src) else None
            if x == 1.0:
                if not orig: raise RuntimeError("the original file is not there any more")
                shutil.copy2(orig, tmp)
            else:
                if not orig:                       # first time: keep the original
                    orig = os.path.join(TEMPO_DIR, "%s_%s" % (iid or abs(hash(path)), base))
                    try: os.link(full, orig)
                    except OSError: shutil.copy2(full, orig)
                _tempo_render(orig, tmp, x, bool(hv))
            c2 = dbc()
            row = c2.execute("SELECT cue_start, cue_intro, cue_mix, cue_end, tv_path, COALESCE(tempo, 1.0) FROM items WHERE path=?", (path,)).fetchone()
            f = (row[5] or 1.0) / x                      # times in the current version -> times in the new one
            sc = lambda v: None if v is None else round(v * f, 2)   # (f = old tempo / new tempo: always from the kept original)
            old_tv = row[4]
            c2.execute("UPDATE items SET cue_start=?, cue_intro=?, cue_mix=?, cue_end=?, cues_edited=1, tempo=?, tempo_src=?, tempo_pending=1, "
                       "tv_path=NULL, kf=NULL, align=NULL WHERE path=?",
                       (sc(row[0]), sc(row[1]), sc(row[2]), sc(row[3]), None if x == 1.0 else x, None if x == 1.0 else orig, path))
            c2.commit()
            os.replace(tmp, full)
            try:
                import pwd as _pw, grp as _gr; os.chown(full, _pw.getpwnam("upload").pw_uid, _gr.getgrnam("hype").gr_gid)
            except Exception: pass
            os.chmod(full, 0o664)
            if old_tv and os.path.isfile(old_tv) and old_tv.startswith("/srv/hype-data/"):
                try: os.remove(old_tv)                  # its TV copy is made again from the new file
                except OSError: pass
            if x == 1.0 and orig and orig.startswith(TEMPO_DIR + "/"):
                try: os.remove(orig)
                except OSError: pass
            LM_CACHE.pop(path, None)
            TEMPO_JOBS[path] = {"state": "done", "msg": "ready - Studio is measuring it now (about a minute)", "t": time.time(), "tempo": x}
            log.info("tempo %.2fx: %s", x, path)
        except Exception as e:
            try:
                if os.path.exists(tmp): os.remove(tmp)
            except Exception: pass
            TEMPO_JOBS[path] = {"state": "error", "msg": str(e)[:200], "t": time.time(), "tempo": x}
            log.warning("tempo %.2fx failed: %s: %s", x, path, e)
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True, "busy": True, "tempo": x}
def tempo_status(path):
    j = TEMPO_JOBS.get(path) or {}
    r = dbc().execute("SELECT COALESCE(tempo, 1.0), COALESCE(tempo_pending, 0) FROM items WHERE path=?", (path,)).fetchone()
    return {"ok": True, "state": j.get("state") or "idle", "msg": j.get("msg") or "", "tempo": r[0] if r else 1.0, "measuring": bool(r and r[1])}

def lib_file(path):
    """A file in the library (under ROOT) - never an absolute path or ../ outside it."""
    full = os.path.normpath(os.path.join(ROOT, str(path or "")))
    if not full.startswith(ROOT.rstrip("/") + "/") or not os.path.isfile(full): raise ValueError("not a library file")
    return full

def wave_zoom(path, t0, dur=6.0):
    """Fine waveform for the cue editor zoom: the loudest sample of every 10 ms from t0, over dur seconds."""
    t0 = max(0.0, t0); dur = max(1.0, min(60.0, dur))
    p = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", lib_file(path),
                        "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=30)
    a = array.array("h"); a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    return {"t0": t0, "step": 0.01, "p": [round(max((abs(x) for x in a[i:i + 80]), default=0) / 32768, 3) for i in range(0, len(a) - 79, 80)]}

# ---------- phone preview: iPhone / iPad Safari cannot show VP9 / AV1 / 10-bit video (only the sound plays) ----------
PVCLIP = "/srv/hype-data/pvclip"; _PV = {"busy": set()}
def pvclip(path):
    """A small H.264 copy (480p, with sound) of a video the phone cannot play – made once, then served by nginx (/pvclip/)."""
    full = lib_file(path)
    try: pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt", "-of", "json", full],
                                        capture_output=True, text=True, timeout=15).stdout or "{}")
    except Exception: pr = {}
    st = (pr.get("streams") or [{}])[0]
    if st.get("codec_name") == "h264" and st.get("pix_fmt") in ("yuv420p", "yuvj420p"): return {"ok": True, "original": True}
    h = hashlib.sha1((path + "|" + str(int(os.path.getmtime(full)))).encode()).hexdigest()[:16]
    out = os.path.join(PVCLIP, h + ".mp4")
    if os.path.exists(out): return {"ok": True, "ready": True, "url": "/pvclip/" + h + ".mp4"}
    if h not in _PV["busy"] and len(_PV["busy"]) < 2:
        _PV["busy"].add(h); os.makedirs(PVCLIP, exist_ok=True)
        def run(h=h, full=full, out=out):
            tmp = out + ".part.mp4"
            try:
                subprocess.run(["nice", "-n", "15", "ffmpeg", "-v", "error", "-y", "-i", full, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:480",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                                "-movflags", "+faststart", tmp], timeout=900, check=True)
                os.replace(tmp, out)
                for f in sorted(os.listdir(PVCLIP), key=lambda f: os.path.getmtime(os.path.join(PVCLIP, f)))[:-150]:   # keep the newest 150
                    try: os.remove(os.path.join(PVCLIP, f))
                    except OSError: pass
            except Exception as e:
                log.warning("phone preview %s: %s", path, e)
                try: os.remove(tmp)
                except OSError: pass
            finally: _PV["busy"].discard(h)
        threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "ready": False, "codec": st.get("codec_name")}

def start_exact(path, t):
    """Library EXACT START: the precise moment the sound starts near the START you set (within 0.4 s).
    In silence: the first sound after it. In music: the sharpest attack (a beat, a voice) nearest to it, just before its first sample."""
    t = max(0.0, float(t)); t0 = max(0.0, t - 0.6); SR, W = 16000, 80                 # 5 ms windows
    p = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", "2.2", "-i", lib_file(path),
                        "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True, timeout=30)
    a = array.array("h"); a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    n = len(a) // W
    if n < 10: return {"ok": False, "error": "No sound here"}
    env = []
    for k in range(n):
        seg = a[k * W:(k + 1) * W]; ms = sum(x * x for x in seg) / W
        env.append(10 * math.log10(ms / 1073741824.0) if ms > 0 else -120.0)
    i0 = min(n - 1, int(round((t - t0) * SR / W)))
    if env[i0] < -50:                                                                   # START sits in silence: go to the first sound
        for k in range(i0, n):
            if env[k] >= -50:
                thr = 32768 * 10 ** (-50 / 20)
                j = next((q for q in range(k * W, (k + 1) * W) if abs(a[q]) >= thr), k * W)
                return {"ok": True, "t": round(max(0.0, t0 + j / SR - 0.005), 2), "how": "first sound"}
        return {"ok": False, "error": "Only silence after START"}
    best, bi = -1e9, None
    for k in range(max(4, i0 - 80), min(n, i0 + 81)):
        rise = env[k] - min(env[k - 4:k])
        sc = min(rise, 30) - 0.15 * abs(k - i0)                                        # nearer wins when attacks are alike
        if rise >= 6 and sc > best: best, bi = sc, k
    if bi is None: return {"ok": False, "error": "No clear start near START – it stays"}
    floor = min(env[max(0, bi - 8):bi]); j = bi
    while j > 1 and bi - j < 20 and env[j - 1] > floor + 3: j -= 1                    # back to the foot of the attack
    return {"ok": True, "t": round(max(0.0, t0 + j * W / SR), 2), "how": "attack"}

def waveform(path):
    full = lib_file(path); os.makedirs(WAVES, exist_ok=True)
    f = os.path.join(WAVES, hashlib.sha1(path.encode()).hexdigest()[:16] + "_1600.json")
    if os.path.exists(f): return json.load(open(f))
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", full, "-map", "0:a:0", "-ac", "1", "-ar", "2000", "-f", "s16le", "-"],
                       capture_output=True, timeout=120)
    a = array.array("h"); a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    n = 1600; step = max(1, len(a) // n)     # 1600 points: about 0.1 s per point on a 3-minute song
    peaks = [round(max((abs(x) for x in a[i:i + step]), default=0) / 32768, 3) for i in range(0, step * n, step)][:n]
    jdump(peaks, f); return peaks

SHOW = {"title": None, "start": None, "end": None, "image": None}; WEATHER = {"temp": None, "code": None, "at": 0}
def norm_name(t): return re.sub(r"[^a-z0-9]", "", (t or "").lower())
def show_intro(title):
    """Intro_<show>.mp4 in /srv/hype/branding/intros (names compared without spaces/dashes), else default.mp4."""
    d = os.path.join(ROOT, "branding", "intros"); key = norm_name(title)
    try:
        for f in os.listdir(d):
            if norm_name(re.sub(r"^intro_?", "", f, flags=re.I).rsplit(".", 1)[0]) == key: return os.path.join(d, f)
    except OSError: pass
    f = os.path.join(ROOT, "branding", "default.mp4")
    return f if os.path.exists(f) else None
def show_name_image(title):
    d = os.path.join(ROOT, "branding", "shownames"); key = norm_name(title)
    try:
        for f in os.listdir(d):    # "01_Hype-Non-Stop.png" = show "Hype Non-Stop"
            if norm_name(re.sub(r"^\d+[_ .-]*", "", f.rsplit(".", 1)[0])) == key: return f
    except OSError: pass
    return None
def show_image_v(title):
    """Version of the show name PNG (its change time): a new file with the same name shows at once (no old cached picture)."""
    f = show_name_image(title)
    try: return int(os.path.getmtime(os.path.join(ROOT, "branding", "shownames", f))) if f else 0
    except OSError: return 0
SHOW_TITLES = {}
def load_week():
    """The week schedule from hyperadio.nl -> planner.WEEK (day, start, end, title); the theme channels' weeks too."""
    for c_ in chancfg.load():
        try: load_station_week(c_["id"])
        except Exception as e: log.warning("week schedule %s: %s", c_["id"], e)
    with urllib.request.urlopen("https://www.hyperadio.nl/wp-json/proradio/v1/schedule", timeout=15) as r: d = json.loads(r.read())
    order = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    week = []
    for day in d.get("posts", []):
        di = order.get((day.get("post_name") or day.get("post_title") or "").lower())
        if di is None: continue
        for sh in day.get("shows") or []:
            sid = (sh.get("show_id") or [None])[0]
            if not sid: continue
            if sid not in SHOW_TITLES:
                with urllib.request.urlopen(f"https://www.hyperadio.nl/wp-json/wp/v2/shows/{sid}", timeout=15) as r2:
                    import html; SHOW_TITLES[sid] = html.unescape(json.loads(r2.read())["title"]["rendered"]).strip()
            week.append({"day": di, "start": sh["show_time"], "end": sh["show_time_end"], "title": SHOW_TITLES[sid]})
    if week:
        planner.WEEK[:] = week; log.info("week schedule: %d show slots", len(week))
        try: jdump(week, SP("/var/lib/hype/week.json"))
        except Exception: pass

def load_station_week(sid):
    """A theme channel's shows (website: Shows → Station schedules, Hype Core 0.2.0): ?station=<id> -> /var/lib/hype/week_<id>.json,
    which planner.py reads (also in hype-channels). Only an answer that is REALLY marked for this station counts: an older
    website ignores ?station and returns Hype Radio's week – then the channel has no shows (its non-stop hour plan plays)."""
    with urllib.request.urlopen("https://www.hyperadio.nl/wp-json/proradio/v1/schedule?station=" + sid, timeout=15) as r: d = json.loads(r.read())
    order = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    week = []
    if d.get("station") == sid:
        for day in d.get("posts", []):
            if day.get("station") not in (None, sid): continue
            di = order.get((day.get("post_name") or day.get("post_title") or "").lower())
            if di is None: continue
            for sh in day.get("shows") or []:
                shid = (sh.get("show_id") or [None])[0]
                if not shid: continue
                if shid not in SHOW_TITLES:
                    with urllib.request.urlopen(f"https://www.hyperadio.nl/wp-json/wp/v2/shows/{shid}", timeout=15) as r2:
                        import html; SHOW_TITLES[shid] = html.unescape(json.loads(r2.read())["title"]["rendered"]).strip()
                week.append({"day": di, "start": sh["show_time"], "end": sh["show_time_end"], "title": SHOW_TITLES[shid]})
    f = "/var/lib/hype/week_%s.json" % sid
    try: old = json.load(open(f))
    except Exception: old = None
    if old != week:
        jdump(week, f); log.info("week schedule %s: %d show slots", sid, len(week))

try: planner.WEEK[:] = json.load(open("/var/lib/hype/week_%s.json" % CH if CH else "/var/lib/hype/week.json"))   # known right at start (show jingles plan right after a restart)
except Exception: pass
SPECIALS_F = SP("/var/lib/hype/specials.json"); SCHED_V = [""]
def load_specials():
    """One-off schedule changes for one date (Hype Live → 📅 Programming → ⭐ Specials, /wp-json/hype/v1/schedule)."""
    with urllib.request.urlopen(urllib.request.Request("https://www.hyperadio.nl/wp-json/hype/v1/schedule", headers={"User-Agent": "HypeStudio/1"}), timeout=15) as r:
        d = json.loads(r.read())
    sp = [x for x in (d.get("specials") or []) if isinstance(x, dict) and x.get("date") and x.get("from") and x.get("to")]
    if sp != planner.SPECIALS: log.info("schedule specials: %d", len(sp))
    planner.SPECIALS[:] = sp; jdump(sp, SPECIALS_F)
    return str(d.get("version") or "")
try: planner.SPECIALS[:] = json.load(open(SPECIALS_F))
except Exception: pass

def listener_slideshow():
    """The listeners' slideshow: photos/Listeners and videos/Listeners hold links to the listener photos / videos that are on
    the LISTENERS pads now (same days-to-keep, pinned, ×) – so 'Photos · Listeners' / 'Videos · Listeners' always show the
    current ones. Hype Studio keeps these two folders itself."""
    want = {"photos": {}, "videos": {}}
    for x in listener_pads()["items"]:
        if x["kind"] in ("photo", "video"): want["photos" if x["kind"] == "photo" else "videos"][x["id"]] = os.path.join(MEDIA_DIR, x["id"])
    for kind, files in want.items():
        d = os.path.join(slideshow.ROOT, kind, "Listeners"); os.makedirs(d, exist_ok=True)
        have = set(os.listdir(d))
        for f in have - set(files):
            if not os.path.islink(os.path.join(d, f)): continue      # only our own links go (a file someone put here stays)
            try: os.remove(os.path.join(d, f))
            except OSError: pass
        for f, src in files.items():
            if f not in have and os.path.exists(src):
                try: os.symlink(src, os.path.join(d, f))
                except OSError: pass

SINK_IDS = SP("/var/lib/hype/sink_ids.json")
def tv_audio_refresh():
    """After the audio bus was made new (reboot / PulseAudio restart: new sink numbers), OBS's 'Radio audio' / 'FX audio'
    keep listening to the old, gone sinks -> TV without sound (16 Nov). At engine start: if the sinks changed, OBS
    re-opens them (a normal engine restart changes nothing, so no blip)."""
    time.sleep(8)
    try:
        env = dict(os.environ, HOME="/root", XDG_RUNTIME_DIR="/run/hype")
        out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=10, env=env).stdout
        ids = {l.split()[1]: l.split()[0] for l in out.splitlines() if len(l.split()) > 1 and l.split()[1].startswith("hype_")}
        try: old = json.load(open(SINK_IDS))
        except Exception: old = None
        if ids and ids != old:
            with E.tv.olock:
                cl = E.tv.client()
                for n in ("Radio audio", "FX audio"):
                    dev = cl.get_input_settings(n).input_settings.get("device_id")
                    if dev: cl.set_input_settings(n, {"device_id": "default"}, True); cl.set_input_settings(n, {"device_id": dev}, True)
            jdump(ids, SINK_IDS); log.info("audio bus is new (%s): OBS re-opened Radio audio / FX audio", ids)
    except Exception as e: log.warning("tv audio refresh: %s", e)

def slideshow_loop():
    """SLIDESHOW scenes (branding/slideshow/photos|videos/<name>): folders -> scenes every 30 s; a Videos scene on air gets
    its next random video when one ends (checked every second)."""
    last = 0
    while True:
        time.sleep(1)
        try:
            if time.time() - last > 30 or slideshow.STATE.pop("force", False):
                last = time.time(); c = dbc()
                listener_slideshow()
                with E.tv.olock:
                    slideshow.sync(E.tv.client(), float(planner.get_setting(c, "ss_photo_s", 8)), float(planner.get_setting(c, "ss_fade_s", 1)),
                                   bool(planner.get_setting(c, "ss_random", True)))
            if slideshow.is_slideshow(E.tv.scene) or slideshow.STATE.get("cur"):
                with E.tv.olock:
                    cl = E.tv.client()
                    slideshow.drive(cl, cl.get_current_program_scene().current_program_scene_name)
        except Exception as e:
            log.warning("slideshow: %s", e); E.tv.cl = None; time.sleep(5)

def watch_show():
    """Current show from hyperadio.nl (Pro.Radio schedule) every minute; week schedule hourly; temperature every 10 minutes."""
    last_week = last_sp = 0
    while True:
        if time.time() - last_week > 3600:
            try: load_week(); last_week = time.time()
            except Exception as e: log.warning("week: %s", e); last_week = time.time() - 3000
        try: sv = open(HL_SCHED).read().strip()     # from the heartbeat / sync answer (X-Hype-Schedule, versions.schedule)
        except OSError: sv = ""
        try: sv = load_specials() or sv; last_sp = time.time()          # + the schedule feed itself every minute (its "version")
        except Exception as e: log.warning("specials: %s", e)
        if sv and sv != SCHED_V[0]:             # Hype Live says the programming changed (X-Hype-Schedule): re-read everything now
            if SCHED_V[0]: log.info("schedule changed in Hype Live (%s): re-reading", sv); last_week = last_sp = 0
            SCHED_V[0] = sv
            if time.time() - last_week > 3600:
                try: load_week(); last_week = time.time()
                except Exception as e: log.warning("week: %s", e)
        if time.time() - last_sp > 300:       # specials: every 5 min (a change made today is on screen within minutes)
            try: load_specials()
            except Exception as e: log.warning("specials: %s", e)
            last_sp = time.time()
        # the show switches EXACTLY at its start time from the week schedule (the website's "current show" answered up to
        # a minute late – the show name box changed late, Gianno 9 Nov); the website only decides when the schedule has no slot
        sched = week_slot()
        def switch(title, start, end, image, why):
            log.info("show is now: %s (%s)", title, why)
            SHOW.update(title=title, start=start, end=end, image=image)
            E.tv.show_changed(title)
            try: E.screen.on_show(title)
            except Exception as e: log.warning("screen show: %s", e)
        if sched and sched["title"] != SHOW["title"]:
            switch(sched["title"], sched["start"], sched["end"], None, "schedule")
        try:
            with urllib.request.urlopen("https://www.hyperadio.nl/wp-json/proradio/v1/current-show", timeout=10) as r:
                d = json.loads(r.read())
            title = (d.get("title") or "").strip() or None
            if title == SHOW["title"]: SHOW.update(start=d.get("start") or SHOW.get("start"), end=d.get("end") or SHOW.get("end"), image=d.get("image") or SHOW.get("image"))
            elif not week_slot(): switch(title, d.get("start"), d.get("end"), d.get("image"), "website")
        except Exception as e: log.warning("show: %s", e)
        if time.time() - WEATHER["at"] > 600:
            try:
                u = "https://api.open-meteo.com/v1/forecast?latitude=52.37&longitude=4.89&current=temperature_2m,weather_code&timezone=Europe%2FAmsterdam"
                with urllib.request.urlopen(u, timeout=10) as r: cur = json.loads(r.read())["current"]
                WEATHER.update(temp=round(cur["temperature_2m"]), code=cur.get("weather_code"), at=time.time())
            except Exception as e: log.warning("weather: %s", e)
        time.sleep(61 - time.time() % 60)        # wake 1 s after every full minute: a show starting at 20:00 shows at 20:00:01

def week_slot(t=None):
    """The show of the week schedule at this moment -> {title, start, end} or None (a gap / no schedule). A special wins."""
    sp = planner.special_at(t or time.time())
    if sp:
        iso = lambda x: time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(x)) + (lambda z: z[:3] + ":" + z[3:])(time.strftime("%z", time.localtime(x)))
        return {"title": sp["title"], "start": iso(sp["t0"]), "end": iso(sp["t1"])}
    lt = time.localtime(t or time.time()); hm = "%02d:%02d" % (lt.tm_hour, lt.tm_min)
    for sl in planner.WEEK:
        st, en = sl.get("start") or "", sl.get("end") or ""
        if sl.get("day") == lt.tm_wday and st <= hm < (en if en and en != "00:00" else "24:00"):
            day = time.strftime("%Y-%m-%d", lt); tz = time.strftime("%z", lt); tz = tz[:3] + ":" + tz[3:]
            return {"title": sl["title"], "start": "%sT%s:00%s" % (day, st, tz), "end": "%sT%s:00%s" % (day, en or "00:00", tz)}
    return None

SOUND_VARS = ["snd_drive", "snd_bass", "snd_bass_f", "snd_treble", "snd_mid", "snd_lowmid", "snd_hpf", "snd_width", "snd_comp", "snd_monob", "snd_agc", "snd_ratio", "snd_thresh", "snd_attack", "snd_release", "snd_ceiling", "snd_punch", "mm_target", "mm_comp", "mm_mb", "mm_lim", "mm_lev", "mm_kc_th", "mm_kc_att", "mm_kc_rel", "mm_lo_str", "mm_lo_f", "mm_lo_th", "mm_lo_att", "mm_lo_rel", "mm_hi_str", "mm_hi_f", "mm_hi_th", "mm_hi_att", "mm_hi_rel", "mm_lim_att", "mm_lim_rel", "mix_out", "mix_in"]
def load_rotation():
    """Artist / song separation and the longest overlap, from Settings."""
    global ARTIST_SEP, TITLE_SEP, MIX_MAX, JINGLE_FADE, JINGLE_DB, ADBOX_ALWAYS, SMART_MIX, AV_LAG, TB_SWEEPER
    c = dbc()
    TB_SWEEPER = bool(planner.get_setting(c, "tb_sweeper", CH is None))   # Throwback sweeper before CLASSICS songs: per station (off on the theme stations unless you turn it on)
    VOICE_AIR.update(per_hour=int(planner.get_setting(c, "voice_per_hour", 2)), max_plays=int(planner.get_setting(c, "voice_max_plays", 3)), intro=bool(planner.get_setting(c, "voice_intro", True)))
    ID_INTRO.update(on=bool(planner.get_setting(c, "id_intro", True)), max=int(planner.get_setting(c, "id_intro_max", 3)), gap=float(planner.get_setting(c, "id_intro_gap", 15)), db=float(planner.get_setting(c, "id_intro_db", -4.0)))
    ARTIST_SEP = 60 * float(planner.get_setting(c, "artist_sep_min", 75)); TITLE_SEP = 3600 * float(planner.get_setting(c, "title_sep_h", 3))
    JINGLE_FADE = float(planner.get_setting(c, "jingle_fade", 0.0)); JINGLE_DB = float(planner.get_setting(c, "jingle_db", -3.0))
    ADBOX_ALWAYS = bool(planner.get_setting(c, "adbox_always", True))
    SMART_MIX = bool(planner.get_setting(c, "smart_mix", True))
    AV_LAG = max(-0.5, min(1.0, float(planner.get_setting(c, "av_lag", 0.20)))) + TV_AUDIO_BUF
    global JINGLE_HIDE; JINGLE_HIDE = bool(planner.get_setting(c, "jingle_hide", True))
    MIX_MAX = 8.0     # (the longest-overlap slider is gone: the overlap = the fade-out)
JINGLE_HIDE = True     # Settings → TV: no logo / show name / clock while a jingle plays
LEVEL = {"l": 0.0, "r": 0.0, "asked": 0}
AGC = {"db": 0.0, "lift": 0.0, "level": None, "strength": 0.7, "t": 0}
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
def agc_loop():
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
    while True:
        time.sleep(DT)
        try:
            if sock is None: sock = socket.create_connection(("127.0.0.1", LIQ_PORT), timeout=2)
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
            lw = intro_lift(E.now, st) if (st > 0.01 and E.now and not E.stopped) else 0.0
            age_ = time.time() - ((E.now or {}).get("started") or 0)
            # up max 2 dB/s, down at once (the look-ahead already started it early); at the very start of a song the lift
            # is there at once, so a quiet first second after a loud song doesn't sound like a gap
            lift = lw if (lw < lift or age_ < 1.5) else min(lw, lift + 2.0 * DT)
            if lift > 0.3: lift_t = time.time()
            if lift > 0.3 or time.time() - lift_t < 4.0:              # lifting (or just done): the song AGC keeps still, the 3 s meter still has the lift in it
                state = "lift" if lift > 0.3 else "hold"
                if lvl > TARGET + 4.0: lift = max(0.0, lift - 1.0)     # safety: still too loud -> lift away fast
            elif st <= 0.01: g += (0.0 - g) * DT / 1.5; state = "off"       # off: back to 0 dB
            elif lvl > -40.0 and E.now and not E.stopped:
                raw = lvl - g                                             # the level without our own gain
                age = time.time() - (E.now.get("started") or 0)
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

def meter_loop():
    """On-air level for the panel meter: one open telnet connection, asked 10× a second, only while a panel is watching."""
    sock = None
    while True:
        if time.time() - LEVEL["asked"] > 5:
            if sock:
                try: sock.close()
                except Exception: pass
                sock = None
            LEVEL.update(l=0.0, r=0.0, ch=None); time.sleep(0.5); continue
        try:
            if sock is None: sock = socket.create_connection(("127.0.0.1", LIQ_PORT), timeout=2)
            sock.sendall(b"radio.level\n"); buf = b""
            while b"END" not in buf:
                b_ = sock.recv(256)
                if not b_: raise OSError("closed")
                buf += b_
            v = buf.decode(errors="ignore").split("END")[0].split()
            LEVEL["l"], LEVEL["r"] = float(v[0]), float(v[1])
            if len(v) >= 5: LEVEL["comp"], LEVEL["lim"], LEVEL["lufs"] = float(v[2]), float(v[3]), float(v[4])
            if len(v) >= 9: LEVEL["ch"] = {"music": float(v[6]), "pads": float(v[7]), "studio": float(v[8])}   # the mixer's channel meters
        except Exception:
            try: sock and sock.close()
            except Exception: pass
            sock = None; time.sleep(1)
        time.sleep(0.1)

AUD_LAG = 8.0     # listeners hear / see a song this long after it starts on air (stream buffers)
def aud_sampler():
    """Every 15 s: radio listeners on our own streams, on Streamerr (public radio) and TV viewers -> aud_samples (14 days)."""
    pub = {"t": 0, "n": None}; last_clean = 0; last_jobs = time.time() - 240
    while True:
        try:
            if time.time() - pub["t"] > 30:
                pub["t"] = time.time()
                try:
                    j = json.loads(urllib.request.urlopen("https://artemis.streamerr.co/api/nowplaying/hype_radio", timeout=5).read())
                    pub["n"] = int((j.get("listeners") or {}).get("current", 0))
                except Exception: pub["n"] = None
            r = listeners()["current"]; tv = viewers()["current"]
            try: jdump(MP3_HITS[-500:], MP3_HITS_FILE, mode=0o600)       # who started the MP3 when: kept over an engine restart (else running listeners lose their address)
            except Exception: pass
            try: audstats.update(LISTEN.get("mp3keys") or set(), is_player)      # sessions for History → Audience
            except Exception as e: log.warning("audience sessions: %s", e)
            chl_out = {}
            for ch_ in chancfg.load():                                          # the theme channels: each its own sessions (stats_<id>.db)
                if not ch_.get("enabled"): continue
                try: chl_out[ch_["id"]] = {k: v for k, v in ch_listeners(ch_).items() if k != "mp3keys"}
                except Exception as e: log.warning("listeners %s: %s", ch_["id"], e)
                TL.ch = ch_["id"]
                try: audstats.update(set(CHL.get(ch_["id"], {}).get("mp3keys") or []), is_player)
                except Exception as e: log.warning("audience sessions %s: %s", ch_["id"], e)
                finally: TL.ch = None
            if time.time() - CH_COV["t"] > 60:                                  # covers for the theme channels' songs too (website / apps art)
                CH_COV["t"] = time.time(); threading.Thread(target=ch_covers, daemon=True).start()
            try: os.makedirs("/run/hype", exist_ok=True); jdump(chl_out, "/run/hype/ch_listeners.json")      # hype-channels reads them (dots, now playing, its samples)
            except Exception: pass
            c = dbc(); c.execute("INSERT OR REPLACE INTO aud_samples VALUES(?,?,?,?)", (time.time(), r, pub["n"], tv))
            if time.time() - last_clean > 3600:
                last_clean = time.time(); c.execute("DELETE FROM aud_samples WHERE t < ?", (time.time() - 14 * 86400,))
                try: audstats.clean(); threading.Thread(target=audstats.geo_refresh, daemon=True).start()
                except Exception as e: log.warning("audience clean: %s", e)
            c.commit()
            if time.time() - last_jobs > 300 and not STATS_BUSY.locked():
                last_jobs = time.time(); threading.Thread(target=stats_jobs_safe, daemon=True).start()
        except Exception as e: log.warning("audience sample: %s", e)
        time.sleep(15)
HL_IMPORT = SP("/var/lib/hype/hl_stats_import.json")
STATS_BUSY = threading.Lock()
FIRE_NOW = {}     # {"t": start of the song on air, "n": 🔥 votes so far} from Hype Live's sync answer
def stats_jobs_safe():
    with STATS_BUSY:
        try: stats_jobs()
        except Exception as e: log.warning("audience stats: %s", e)
        for ch_ in chancfg.load():          # the theme channels: plays + 5-minute points in their own stats_<id>.db
            if not os.path.exists(chancfg.db_path(ch_["id"])): continue
            TL.ch = ch_["id"]
            try: stats_jobs()
            except Exception as e: log.warning("audience stats %s: %s", ch_["id"], e)
            finally: TL.ch = None
def stats_jobs():
    """Every 5 min: (1) each song that has ended -> one play row with the audience when listeners started to hear it and at
    its end (History → Audience → Songs); (2) the audience per 5 minutes (→ By time); (3) once: Hype Live's history."""
    now = time.time(); c = dbc()
    # 1. plays
    _, last = audstats.own_since("plays"); t_from = max(last or 0, now - 14 * 86400) + 1
    rows = c.execute("""SELECT a.started, a.artist, a.title, a.path, COALESCE(i.cue_mix, i.cue_end, i.duration, 0) - COALESCE(i.cue_start, 0)
                        FROM asrun a LEFT JOIN items i ON i.path = a.path WHERE a.started >= ? ORDER BY a.started""", (t_from - 86400,)).fetchall()
    starts = [r[0] for r in rows]
    smp = c.execute("SELECT t, r, p, tv FROM aud_samples WHERE t >= ? ORDER BY t", (t_from - 120,)).fetchall()
    # 20 Nov: per device – who heard the start and who heard the end, each with its own stream's delay (MP3 ~4 s, HLS ~15 s),
    # so a listener who leaves lands on the song he left on (not on the next one, as with the 15 s total + one fixed delay)
    rs = audstats.heard("radio", t_from - 86400, now); vs = audstats.heard("tv", t_from - 86400, now)
    out = []
    for k, (st, ar, ti, path, ln) in enumerate(rows):
        if st < t_from or not re.match(r"music(-[a-z0-9_]+)?/", path or "") or "/RECORDINGS/" in "/" + (path or ""): continue     # mixes count too (20 Nov); music-<channel>/ too
        end = starts[k + 1] if k + 1 < len(starts) else None
        if end is None or end + 60 > now: break                         # still playing / its end not heard by every player yet
        # cut off = not a real play: under 90 s AND under 80 % of the song (20 Nov: short songs that play to the end count too, from 30 s)
        if end - st < 30 or (end - st < 90 and end - st < 0.8 * (ln or 0)): continue
        if end - st < 90 and not ln: continue
        a0 = aud_at(smp, st + AUD_LAG); a1 = aud_at(smp, end + AUD_LAG)
        if not a0 or not a1: continue                                    # the engine was not counting then
        p0 = aud_at(smp, st + AUD_LAG, pub=True) or 0; p1 = aud_at(smp, end + AUD_LAG, pub=True) or 0       # Streamerr (public relay): only its total
        r0 = audstats.count_at(rs, st + 3) + p0; r1 = audstats.count_at(rs, end - 3) + p1
        v0 = audstats.count_at(vs, st + 3); v1 = audstats.count_at(vs, end - 3)
        out.append((st, end, ar or "", ti or "", path, r0, r1, v0, v1, None, "h"))
    audstats.add_plays(out)
    # 2. 5-minute points
    _, last5 = audstats.own_since("aud5"); b = int(max((last5 or 0) + 300, (now - 14 * 86400) // 300 * 300)); cut = int(now // 300 * 300)
    pts = []
    if b < cut:
        acc = {}
        for t, r, p_, tv in c.execute("SELECT t, r, p, tv FROM aud_samples WHERE t >= ? AND t < ?", (b, cut)):
            a = acc.setdefault(int(t // 300 * 300), [0, 0.0, 0.0, 0, 0]); rr = (r or 0) + (p_ or 0)
            a[0] += 1; a[1] += rr; a[2] += tv or 0; a[3] = max(a[3], rr); a[4] = max(a[4], tv or 0)
        pts = [(t, round(a[1] / a[0], 2), round(a[2] / a[0], 2), a[3], a[4], "h") for t, a in sorted(acc.items())]
    audstats.add_aud5(pts)
    # 3. Hype Live's history (13 months of plays, 400 days of audience) – once, before Studio's own numbers start (Hype Radio only)
    if getattr(TL, "ch", None): return
    try: st_ = json.load(open(HL_IMPORT))
    except Exception: st_ = {}
    if not st_.get("done") and now - st_.get("tried", 0) > 3600:
        st_["tried"] = now
        try:
            if "cut_plays" not in st_:
                st_["cut_plays"] = audstats.own_since("plays")[0] or now; st_["cut_aud"] = audstats.own_since("aud5")[0] or now
            n_p = n_a = 0
            for what, cut_ in (("plays", st_["cut_plays"]), ("aud", st_["cut_aud"])):
                frm = st_.get("next_" + what, 0)
                while frm is not None:
                    d = json.loads(hl_call("hype_studio_stats_export", "&what=" + what + "&from=" + str(int(frm)) + "&limit=3000", timeout=60))
                    items = d.get("items") or []
                    if what == "plays":
                        rows_ = []
                        for x in items:
                            t = float(x.get("t") or 0)
                            if not t or t >= cut_: continue
                            r0, r1 = (x.get("r0"), x.get("r1")) if x.get("r0") is not None else (0, x.get("radio"))
                            v0, v1 = (x.get("v0"), x.get("v1")) if x.get("v0") is not None else (0, x.get("tv"))
                            if r1 is None: continue
                            rows_.append((t, t + float(x.get("len") or 0), x.get("artist") or "", x.get("title") or "", "", r0, r1, v0, v1,
                                          x.get("fire"), "a" if x.get("s") == "a" else "h"))
                        audstats.add_plays(rows_); n_p += len(rows_)
                    else:
                        rows_ = [(int(float(x["t"]) // 300 * 300), x.get("r") or 0, x.get("tv") or 0, x.get("r") or 0, x.get("tv") or 0, "a" if x.get("s") == "a" else "h")
                                 for x in items if x.get("t") and float(x["t"]) < cut_]
                        audstats.add_aud5(rows_); n_a += len(rows_)
                    nxt = d.get("next"); st_["next_" + what] = nxt; frm = nxt if (nxt and items) else None
                    jdump(st_, HL_IMPORT)
            st_.update(done=True, plays=n_p + st_.get("plays", 0), aud=n_a + st_.get("aud", 0), t=now); st_.pop("error", None)
            log.info("Hype Live history imported: %d plays, %d audience points", n_p, n_a)
        except Exception as e:
            st_["error"] = str(e)[:200]; log.info("Hype Live history import not possible yet: %s", e)
        jdump(st_, HL_IMPORT)

def aud_at(samples, t, pub=False):
    """(radio total, tv) of the sample closest to t (within 40 s), or None. pub=True: only the Streamerr (public relay) count."""
    import bisect
    if not samples: return None
    i = bisect.bisect_left(samples, (t,))
    best = min((samples[j] for j in (i - 1, i) if 0 <= j < len(samples)), key=lambda x: abs(x[0] - t))
    if abs(best[0] - t) > 40: return None
    if pub: return best[2] or 0
    return ((best[1] or 0) + (best[2] or 0), best[3] or 0)

def history(qs, limit=2000):
    """What went on air (as-run log): one day, newest first, optional search and type (music / jingles / ads)."""
    day = (qs.get("day") or [time.strftime("%Y-%m-%d")])[0][:10]; q = (qs.get("q") or [""])[0].strip().lower(); typ = (qs.get("type") or [""])[0]
    t0 = time.mktime(time.strptime(day, "%Y-%m-%d")); t1 = t0 + 86400
    c = dbc(); out = []
    try: smp = c.execute("SELECT t, r, p, tv FROM aud_samples WHERE t >= ? AND t < ? ORDER BY t", (t0 - 120, t1 + 3600)).fetchall()
    except sqlite3.OperationalError: smp = []
    rows = c.execute("""SELECT a.started, a.path, a.artist, a.title, a.folder, a.mode, a.scene, i.section,
                        COALESCE(i.cue_mix, i.duration, 0) - COALESCE(i.cue_start, 0), a.over FROM asrun a LEFT JOIN items i ON i.path = a.path
                        WHERE a.started >= ? AND a.started < ? ORDER BY a.started DESC LIMIT ?""", (t0, t1, limit)).fetchall()
    for k_, (st, path, ar, ti, fo, mo, sc, sec, ln, ov) in enumerate(rows):
        # audience when it started and when the next item started (newest first: the next item is the row before)
        end = rows[k_ - 1][0] if k_ > 0 else (c.execute("SELECT MIN(started) FROM asrun WHERE started > ?", (st,)).fetchone() or [None])[0]
        a0 = aud_at(smp, st + AUD_LAG); a1 = aud_at(smp, (end if end else time.time()) + AUD_LAG) if (end or time.time() - st > 20) else None
        aud = {"r0": a0[0], "r1": a1[0], "v0": a0[1], "v1": a1[1], "live": end is None} if a0 and a1 else None
        sec = sec or ("jingles" if re.match(r"jingles(-[a-z0-9]+)?/", path or "") else "ads" if (path or "").startswith("ads/") else "music")
        if typ and sec != typ: continue
        if q and q not in f"{ar} {ti} {fo}".lower(): continue
        out.append({"t": st, "time": time.strftime("%H:%M:%S", time.localtime(st)), "artist": ar or "", "title": ti or "", "folder": fo or "",
                    "type": sec, "mode": mo or "", "scene": sc or "", "len": round(ln or 0), "path": path or "", "over": ov or "", "aud": aud})
    music = sum(1 for r in out if r["type"] == "music")
    return {"day": day, "items": out, "count": len(out), "songs": music}

def ch_liq():
    ch = getattr(TL, "ch", None); cfg = chancfg.get(ch) if ch else None
    return Liq(int(cfg["telnet"])) if cfg else Liq()
def get_settings():
    c = dbc(); liq = ch_liq(); snd = {}
    for v in SOUND_VARS:
        try: snd[v] = float(liq.cmd("var.get " + v).split()[-1])
        except Exception: snd[v] = None
    out = _settings_main(c, snd)
    if getattr(TL, "ch", None):        # a theme channel: what it has (Sound, Mixing, Rotation & planning) from its own database / radio engine
        g = lambda k, dflt: planner.get_setting(c, k, dflt)
        out.update({"channel": TL.ch, "sound": snd, "smart_pick": bool(g("smart_pick", True)), "hit_hour": bool(g("hit_hour", True)), "hour_preview": bool(g("hour_preview", True)),
                "artist_sep_min": float(g("artist_sep_min", 75)), "title_sep_h": float(g("title_sep_h", 3)),
                "strict_hold_before": float(g("strict_hold_before", 60)), "strict_hold_after": float(g("strict_hold_after", 60)),
                "jingle_db": float(g("jingle_db", -3.0)), "smart_mix": bool(g("smart_mix", True)), "mix_max": float(g("mix_max", 5.0)), "sound_presets": g("sound_presets", {}) or {}, "sound_presets_hidden": g("sound_presets_hidden", []) or []})
    return out
def _settings_main(c, snd):
    return {"smart_pick": bool(planner.get_setting(c, "smart_pick", True)), "hit_hour": bool(planner.get_setting(c, "hit_hour", True)),
            "hour_preview": bool(planner.get_setting(c, "hour_preview", True)), "tv_clip_offset": float(planner.get_setting(c, "tv_clip_offset", 3.0)),
            "jingle_hide": bool(planner.get_setting(c, "jingle_hide", True)), "upnext_hour_s": float(planner.get_setting(c, "upnext_hour_s", 10) or 0), "upnext_after_s": float(planner.get_setting(c, "upnext_after_s", 12) or 0), "upnext_end_s": float(planner.get_setting(c, "upnext_end_s", 0) or 0),
            "alsoon_at": int(planner.get_setting(c, "alsoon_at", -1) if planner.get_setting(c, "alsoon_at", -1) is not None else -1), "alsoon_s": float(planner.get_setting(c, "alsoon_s", 20) or 20), "keep_s": float(planner.get_setting(c, "keep_s", 8) or 0),
            "artist_sep_min": ARTIST_SEP / 60, "strict_hold_before": float(planner.get_setting(c, "strict_hold_before", 60)), "strict_hold_after": float(planner.get_setting(c, "strict_hold_after", 60)), "pad_duck": float(planner.get_setting(c, "pad_duck", 0.25)), "jingle_fade": JINGLE_FADE, "jingle_db": JINGLE_DB, "adbox_always": ADBOX_ALWAYS, "smart_mix": SMART_MIX, "av_lag": AV_LAG, "id_intro": ID_INTRO["on"], "id_intro_max": ID_INTRO["max"], "tb_sweeper": TB_SWEEPER, "voice_per_hour": VOICE_AIR["per_hour"], "listener_keep_days": float(planner.get_setting(c, "listener_keep_days", 7)), "ss_photo_s": float(planner.get_setting(c, "ss_photo_s", 8)), "ss_fade_s": float(planner.get_setting(c, "ss_fade_s", 1)), "ss_random": bool(planner.get_setting(c, "ss_random", True)), "tidy_names": bool(planner.get_setting(c, "tidy_names", True)), "voice_max_plays": VOICE_AIR["max_plays"], "voice_intro": VOICE_AIR["intro"], "voices": voice_overview(), "id_intro_gap": ID_INTRO["gap"], "id_intro_db": ID_INTRO["db"], "sound_presets": planner.get_setting(c, "sound_presets", {}) or {}, "sound_presets_hidden": planner.get_setting(c, "sound_presets_hidden", []) or [], "title_sep_h": TITLE_SEP / 3600, "mix_max": MIX_MAX,
            "sound": snd, "relay": relay_public()}
RELAY_CFG = "/etc/hype/relay.json"
def relay_cfg():
    try: return json.load(open(RELAY_CFG))
    except Exception: return {}
def relay_public():
    """Backup relay to Streamerr for the panel – never the password itself."""
    r = relay_cfg(); st = {}
    try: st = json.load(open("/run/hype/relay.json"))
    except Exception: pass
    return {"on": bool(r.get("on")), "host": r.get("host", ""), "port": r.get("port", 8000), "mount": r.get("mount", "/"), "user": r.get("user", ""),
            "password_set": bool(r.get("password")), "state": st.get("state", "off"), "error": st.get("error", ""), "since": st.get("since")}
def relay_save(d):
    r = relay_cfg()
    for k in ("host", "mount", "user"):
        if k in d: r[k] = str(d[k] or "").strip()
    if "port" in d: r["port"] = int(d["port"] or 8000)
    if d.get("password"): r["password"] = str(d["password"])          # empty = keep the saved one
    if "on" in d: r["on"] = bool(d["on"])
    tmp = RELAY_CFG + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: json.dump(r, f)
    os.replace(tmp, RELAY_CFG)
def relay_title(it):
    """The song title on the Streamerr station too (AzuraCast takes Icecast-style metadata from the live source)."""
    if CH: return
    r = relay_cfg()
    if not (r.get("on") and r.get("host") and r.get("password")): return
    def go():
        try:
            if it.get("section") != "music": return              # jingles / sweepers / ads: the song title stays
            song = "%s - %s" % (it.get("artist") or "", it.get("title") or "")
            q = urllib.parse.urlencode({"mount": "/" + (r.get("mount") or "/").lstrip("/"), "mode": "updinfo", "song": song})
            rq = urllib.request.Request("http://%s:%s/admin/metadata?%s" % (r["host"], int(r.get("port") or 8000), q))
            u, pw = r.get("user") or "source", r["password"]
            if pw.startswith(u + ":"): pw = pw[len(u) + 1:]               # "user:password" typed in the password field
            rq.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % (u, pw)).encode()).decode())
            urllib.request.urlopen(rq, timeout=5).read()
        except Exception as e: log.info("relay title: %s", e)
    threading.Thread(target=go, daemon=True).start()
def set_settings(d):
    global MIX_OUT
    if getattr(TL, "ch", None):        # a theme channel: its own sound, mixing, rotation + planning (in its own database / radio engine)
        liq = ch_liq(); c = dbc()
        for k, v in (d.get("sound") or {}).items():
            if k in SOUND_VARS: liq.cmd(f"var.set {k} = {float(v):.2f}")
        for k in ("smart_pick", "hit_hour", "hour_preview", "smart_mix"):
            if k in d: planner.set_setting(c, k, bool(d[k]))
        for k, lo, hi in (("mix_max", 0.5, 8.0), ("artist_sep_min", 0.0, 360.0), ("title_sep_h", 0.5, 48.0), ("strict_hold_before", 0.0, 240.0), ("strict_hold_after", 0.0, 240.0),
                          ("jingle_db", -12.0, 3.0)):
            if k in d: planner.set_setting(c, k, max(lo, min(hi, float(d[k]))))
        if "sound_presets" in d and isinstance(d["sound_presets"], dict):
            planner.set_setting(c, "sound_presets", {str(n)[:40]: {k: float(x) for k, x in v.items() if k in SOUND_VARS and isinstance(x, (int, float))}
                                                     for n, v in list(d["sound_presets"].items())[:30] if isinstance(v, dict)})
        if "sound_presets_hidden" in d and isinstance(d["sound_presets_hidden"], list):
            planner.set_setting(c, "sound_presets_hidden", [str(n)[:40] for n in d["sound_presets_hidden"][:30]])
        planner._HOLD.clear()
        return get_settings()
    c = dbc(); liq = Liq()
    if isinstance(d.get("relay"), dict): relay_save(d["relay"])
    for k in ("smart_pick", "hit_hour", "hour_preview"):
        if k in d: planner.set_setting(c, k, bool(d[k]))
    if "tv_clip_offset" in d: planner.set_setting(c, "tv_clip_offset", max(-10.0, min(10.0, float(d["tv_clip_offset"]))))
    if "jingle_hide" in d:
        planner.set_setting(c, "jingle_hide", bool(d["jingle_hide"])); load_rotation()
        if not d["jingle_hide"]: E.tv.jingle_graphics(False)
    if "upnext_hour_s" in d: planner.set_setting(c, "upnext_hour_s", max(0.0, min(60.0, float(d["upnext_hour_s"]))))
    if "upnext_after_s" in d: planner.set_setting(c, "upnext_after_s", max(3.0, min(120.0, float(d["upnext_after_s"]))))
    if "upnext_end_s" in d: planner.set_setting(c, "upnext_end_s", max(0.0, min(60.0, float(d["upnext_end_s"]))))
    if "alsoon_at" in d: planner.set_setting(c, "alsoon_at", max(-1, min(59, int(d["alsoon_at"]))))
    if "alsoon_s" in d: planner.set_setting(c, "alsoon_s", max(10.0, min(60.0, float(d["alsoon_s"]))))
    if "keep_s" in d: planner.set_setting(c, "keep_s", max(0.0, min(20.0, float(d["keep_s"]))))
    if "artist_sep_min" in d: planner.set_setting(c, "artist_sep_min", max(0.0, min(360.0, float(d["artist_sep_min"]))))
    for k in ("strict_hold_before", "strict_hold_after"):
        if k in d: planner.set_setting(c, k, max(0.0, min(240.0, float(d[k])))); planner._HOLD.clear()
    if "title_sep_h" in d: planner.set_setting(c, "title_sep_h", max(0.5, min(48.0, float(d["title_sep_h"]))))
    if "sound_presets" in d and isinstance(d["sound_presets"], dict):   # your own sound presets (Settings → Sound)
        clean = {}
        for n, v in list(d["sound_presets"].items())[:30]:
            if isinstance(v, dict): clean[str(n)[:40]] = {k: float(x) for k, x in v.items() if k in SOUND_VARS and isinstance(x, (int, float))}
        planner.set_setting(c, "sound_presets", clean)
    if "sound_presets_hidden" in d and isinstance(d["sound_presets_hidden"], list):   # built-in presets you removed (Settings → Sound)
        planner.set_setting(c, "sound_presets_hidden", [str(n)[:40] for n in d["sound_presets_hidden"][:30]])
    if "adbox_always" in d: planner.set_setting(c, "adbox_always", bool(d["adbox_always"]))
    if "smart_mix" in d: planner.set_setting(c, "smart_mix", bool(d["smart_mix"]))
    if "id_intro" in d: planner.set_setting(c, "id_intro", bool(d["id_intro"]))
    if "tb_sweeper" in d: planner.set_setting(c, "tb_sweeper", bool(d["tb_sweeper"]))
    if "voice_intro" in d: planner.set_setting(c, "voice_intro", bool(d["voice_intro"]))
    if "voice_per_hour" in d: planner.set_setting(c, "voice_per_hour", max(0, min(6, int(float(d["voice_per_hour"])))))
    if "ss_photo_s" in d: planner.set_setting(c, "ss_photo_s", max(2.0, min(60.0, float(d["ss_photo_s"])))); slideshow.STATE["sig"].clear()
    if "ss_fade_s" in d: planner.set_setting(c, "ss_fade_s", max(0.0, min(5.0, float(d["ss_fade_s"])))); slideshow.STATE["sig"].clear()
    if "ss_random" in d: planner.set_setting(c, "ss_random", bool(d["ss_random"])); slideshow.STATE["sig"].clear()
    if "tidy_names" in d: planner.set_setting(c, "tidy_names", bool(d["tidy_names"]))
    if "listener_keep_days" in d: planner.set_setting(c, "listener_keep_days", max(1, min(90, int(float(d["listener_keep_days"])))))
    if "voice_max_plays" in d: planner.set_setting(c, "voice_max_plays", max(1, min(20, int(float(d["voice_max_plays"])))))
    if "id_intro_max" in d: planner.set_setting(c, "id_intro_max", max(1, min(12, int(float(d["id_intro_max"])))))
    if "id_intro_gap" in d: planner.set_setting(c, "id_intro_gap", max(0.0, min(60.0, float(d["id_intro_gap"]))))
    if "id_intro_db" in d: planner.set_setting(c, "id_intro_db", max(-15.0, min(6.0, float(d["id_intro_db"]))))
    if "av_lag" in d: planner.set_setting(c, "av_lag", max(-0.5, min(1.0, float(d["av_lag"]))))
    if "jingle_db" in d: planner.set_setting(c, "jingle_db", max(-12.0, min(3.0, float(d["jingle_db"]))))
    if "jingle_fade" in d: planner.set_setting(c, "jingle_fade", max(0.0, min(5.0, float(d["jingle_fade"]))))
    if "pad_duck" in d: planner.set_setting(c, "pad_duck", max(0.05, min(0.8, float(d["pad_duck"]))))
    if "mix_max" in d: planner.set_setting(c, "mix_max", max(0.5, min(8.0, float(d["mix_max"]))))
    load_rotation()
    for k, v in (d.get("sound") or {}).items():
        if k in SOUND_VARS: liq.cmd(f"var.set {k} = {float(v):.2f}")
        if k == "mix_out": MIX_OUT = max(0.3, min(8.0, float(v)))
    return get_settings()

LOWER_FILE = SP("/var/lib/hype/lower.json")
def lower_load():
    try: return json.load(open(LOWER_FILE))
    except Exception: return {"visible": False, "style": "A", "size": "auto", "name": "", "role": "", "handle": "", "recent": []}
LOWER = lower_load()
VOICE_ON = {}       # a listener's voice message playing now: {name, source, start, end} -> the voice message box on TV
def lower_cmd(d):
    """Lower third: show / hide / update. Recent names are kept for one-tap reuse."""
    act = d.get("action", "show")
    for k in ("style", "size", "name", "role", "handle"):
        if k in d: LOWER[k] = str(d[k])[:120]
    if act == "show":
        if not LOWER["name"].strip(): return {"ok": False, "error": "type a name"}
        LOWER["visible"] = True; LOWER["shown_at"] = time.time()
        rec = {k: LOWER[k] for k in ("style", "size", "name", "role", "handle")}
        LOWER["recent"] = [rec] + [r for r in LOWER.get("recent", []) if r.get("name") != rec["name"]][:11]
    elif act == "hide": LOWER["visible"] = False
    elif act == "forget":      # remove a saved name from the quick buttons
        nm = str(d.get("forget") or "")
        LOWER["recent"] = [r for r in LOWER.get("recent", []) if r.get("name") != nm]
    LOWER["rev"] = LOWER.get("rev", 0) + 1
    os.makedirs(os.path.dirname(LOWER_FILE), exist_ok=True); jdump(LOWER, LOWER_FILE)
    return {"ok": True, "lower": LOWER}

E = Engine()

class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def ch_of(self, p):
        """?ch=<theme channel> on a planning / report / sound path -> that channel (main = None)."""
        ch = (parse_qs(urlparse(self.path).query).get("ch") or [""])[0]
        if not ch or ch == "main" or not chancfg.valid(ch): return None
        return ch if (p in CH_PATHS or p.startswith("/plan/") or p == "/settings") else None
    def eng_proxy(self, method):
        """/api/ch/<id>/… -> a theme station's own engine (Hype Radio's engine in channel mode): the panel works on it exactly
        as on Hype Radio. Answers whatever it answers (JSON, CSV, audio …)."""
        u = urlparse(self.path); m = re.match(r"^(?:/api)?/ch/([a-z][a-z0-9]{1,19})(/.*)$", u.path)
        if not m or CH: return False
        cid, rest = m.group(1), m.group(2) + ("?" + u.query if u.query else "")
        cfg = chancfg.get(cid)
        if not cfg or not cfg.get("engine"): self.send(404, {"ok": False, "error": "%s is not on Hype Radio's engine" % cid}); return True
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else None
        hd = {k: v for k, v in self.headers.items() if k.lower() in ("content-type", "range", "x-file-name", "x-path", "x-section", "x-folder")}
        try:
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (chancfg.engine_port(cid), rest), data=body, method=method, headers=hd)
            try: r = urllib.request.urlopen(rq, timeout=120)
            except urllib.error.HTTPError as e: r = e
            data = r.read(); self.send_response(r.status if hasattr(r, "status") else r.code)
            for k, v in r.headers.items():
                if k.lower() in ("content-type", "content-disposition", "content-range", "accept-ranges", "cache-control"): self.send_header(k, v)
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        except Exception as e: self.send(503, {"ok": False, "error": "%s's engine is not reachable (%s)" % (cfg["name"], e)})
        return True
    def do_GET(self):
        p = urlparse(self.path).path.replace("/api", "", 1)
        if p == "/channels": return self.chan_proxy("GET", "/status")
        if p.startswith("/ch/") and self.eng_proxy("GET"): return
        TL.ch = self.ch_of(p)
        try: return self._do_GET()
        finally: TL.ch = None
    def do_POST(self):
        p = urlparse(self.path).path.replace("/api", "", 1)
        if p == "/channels/cmd":
            n = int(self.headers.get("Content-Length", 0)); return self.chan_proxy("POST", "/cmd", self.rfile.read(n))
        if p.startswith("/ch/") and self.eng_proxy("POST"): return
        TL.ch = self.ch_of(p)
        try: return self._do_POST()
        finally: TL.ch = None
    def chan_proxy(self, method, path, body=None):
        """The theme channels' own process (hype-channels, 127.0.0.1:8100). Not running = every channel shows as off."""
        try:
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (chancfg.API_PORT, path), data=body, method=method, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(rq, timeout=5) as r: return self.send(200, json.loads(r.read() or b"{}"))
        except urllib.error.HTTPError as e:
            try: return self.send(e.code, json.loads(e.read() or b"{}"))
            except ValueError: return self.send(e.code, {"ok": False, "error": "channels: %s" % e})
        except Exception as e:
            if path == "/status": return self.send(200, {"channels": [dict(c, enabled=False, running=False, down=True) for c in chancfg.load()]})
            return self.send(503, {"ok": False, "error": "the theme channels service is not running (%s)" % e})
    def _do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query); p = u.path.replace("/api", "", 1)
        try:
            if p == "/state": return self.send(200, E.state())
            if p == "/tvnow": return self.send(200, {"now": E.now or None, "next": E.liq_next or None, "t": time.time()})
            if p == "/tv": return self.send(200, tv_info())
            if p == "/library": return self.send(200, library(qs))
            if p == "/upload/status": return self.send(200, up_status(qs))
            if p == "/history": return self.send(200, history(qs))
            if p == "/weekreport": return self.send(200, week_report(qs))
            if p == "/recap": return self.send(200, recap_data(int((qs.get("w") or ["1"])[0])))
            if p == "/top10": return self.send(200, top10_data() or {})
            if p == "/vt/list": return self.send(200, vt_list(dbc(), int((qs.get("pid") or ["0"])[0])))
            if p == "/vt/hours": return self.send(200, {"hours": vt_hours_list()})
            if p == "/hl_needs":     # the "Needs you" count for the header badge (from the last sync answer)
                try: s_ = json.load(open(HL_SYNC)); return self.send(200, {"needs": s_.get("needs") or {}, "t": s_.get("_t", 0)})
                except Exception: return self.send(200, {"needs": {}, "t": 0})
            if p == "/media":
                lst = {v.get("file"): v for v in media_in_state().values()}; fl = media_files()
                for m in fl:
                    v = lst.get(m["file"])
                    if v: m["listener"] = {k: v.get(k) for k in ("name", "city", "source", "caption", "added")}
                fl.sort(key=lambda m: (0, -(m["listener"].get("added") or 0)) if m.get("listener") else (1, 0))
                return self.send(200, {"files": fl, "scenes": MEDIA_SCENES})
            if p == "/streams":
                chs = []
                try:
                    st_ = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/status" % chancfg.API_PORT, timeout=2).read()).get("channels", [])
                    ice = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/status-json.xsl", timeout=2).read())["icestats"].get("source") or []
                    ice = ice if isinstance(ice, list) else [ice]
                    for x in st_:
                        m_ = next((y for y in ice if str(y.get("listenurl", "")).endswith("/" + x.get("mount", "-"))), None)
                        chs.append({"id": x["id"], "name": x["name"], "color": x["color"], "enabled": x.get("enabled"), "running": x.get("running"), "sim": x.get("sim"),
                                    "stream": bool(m_), "kbps": int(m_.get("bitrate", 0) or 0) if m_ else 0, "hls": x.get("hls"),
                                    "now": ((x.get("now") or {}).get("artist", "") + " – " + (x.get("now") or {}).get("title", "")) if x.get("now") else "",
                                    "own": {k: v for k, v in CHL.get(x["id"], {}).items() if k != "mp3keys"}, "error": x.get("error")})
                except Exception: pass
                return self.send(200, dict(streams_status(E), guard=guard_status(), own=listeners(), own_tv=viewers(), channels=chs))
            if p == "/smartmix":     # the cue editor: where smart mix puts MIX for these START / END
                c = dbc(); r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (qs["path"][0],)).fetchone()
                if not r: return self.send(404, {"error": "not found"})
                it = row_item(r); it["cue_start"] = float(qs.get("s", [it["cue_start"] or 0])[0]); it["cue_end"] = float(qs.get("e", [it["cue_end"] or it["duration"] or 0])[0])
                lm, L, mset = lm_info(it["path"]); LM_CACHE[it["path"]] = (lm, L, False)
                sm = smart_mix(it); LM_CACHE[it["path"]] = (lm, L, mset)
                return self.send(200, {"mix": round(sm[0], 1) if sm else None, "overlap": round(sm[1], 1) if sm else None, "intro": intro_len(it), "smart": SMART_MIX})
            if p == "/mp3hit":       # nginx mirrors every /radio.mp3 start here (address + player), for unique MP3 listeners
                MP3_HITS.append((time.time(), self.headers.get("X-Real-IP", ""), self.headers.get("X-UA", ""), (self.headers.get("X-Mount", "") or "/radio.mp3").split("?")[0]))
                del MP3_HITS[:-2000]
                return self.send(200, {"ok": True})
            if p == "/version":      # the installed commit (pages offer a reload when it changes)
                try: v = open("/var/lib/hype/deployed_commit").read().strip()[:12]
                except Exception: v = ""
                return self.send(200, {"v": v})
            if p == "/audsongs":
                days = max(1, min(400, int((qs.get("days") or ["30"])[0] or 30))); by = (qs.get("by") or ["song"])[0]
                def day_(k, plus=0):     # your own period: from / to as YYYY-MM-DD (to = that whole day)
                    v = (qs.get(k) or [""])[0]
                    try: return time.mktime(time.strptime(v[:10], "%Y-%m-%d")) + plus
                    except ValueError: return None
                t0, t1 = day_("from"), day_("to", 86400)
                if (qs.get("csv") or [""])[0]:
                    body = audstats.plays_csv(days, t0, t1).encode("utf-8-sig")
                    self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="hype-song-plays-{days}d.csv"'); self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                if (qs.get("key") or [""])[0]: return self.send(200, {"plays": audstats.song_plays(qs["key"][0], days, by, t0, t1)})
                try: imp = json.load(open(HL_IMPORT)) if not getattr(TL, "ch", None) else {"done": True}
                except Exception: imp = {}
                return self.send(200, dict(audstats.songs(days, by, t0, t1), imported={k: imp.get(k) for k in ("done", "plays", "aud", "error")}))
            if p == "/aud24": return self.send(200, audstats.last24(5, (qs.get("by") or ["song"])[0]))
            def per_(k, plus=0):        # your own period: from / to as YYYY-MM-DD (to = that whole day)
                v = (qs.get(k) or [""])[0]
                try: return time.mktime(time.strptime(v[:10], "%Y-%m-%d")) + plus
                except ValueError: return None
            if p == "/audtime": return self.send(200, audstats.bytime((qs.get("view") or ["today"])[0], per_("from"), per_("to", 86400)))
            if p == "/audstats":
                days = max(1, min(400, int((qs.get("days") or ["1"])[0] or 1))); kind = (qs.get("kind") or [""])[0]
                return self.send(200, audstats.report(days, kind, per_("from"), per_("to", 86400)))
            if p == "/audience": return self.send(200, {"radio": listeners(), "tv": viewers(), "t": int(time.time())})
            if p in ("/nowplaying", "/nowplaying/hype_radio"): return self.send(200, nowplaying_api())
            if p == "/guard": return self.send(200, guard_status(full=True))
            if p == "/level" and chancfg.valid((qs.get("ch") or [""])[0]):      # a theme channel's meters (Settings → Sound on that channel)
                try:
                    st = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/status" % chancfg.API_PORT, timeout=2).read())
                    x = next((y for y in st.get("channels", []) if y["id"] == qs["ch"][0]), {}); m = x.get("meter") or []
                except Exception: m = []
                f_ = lambda i: m[i] if len(m) > i else 0.0
                return self.send(200, {"l": f_(0), "r": f_(1), "comp": f_(2), "lim": f_(3), "lufs": m[4] if len(m) > 4 else None, "agc": round(((x.get("agc") or {}).get("db") or 0) + ((x.get("agc") or {}).get("lift") or 0), 1),
                                       "agc_state": (x.get("agc") or {}).get("state", ""), "agc_on": ((x.get("agc") or {}).get("strength") or 0) > 0.01, "ch": None})
            if p == "/level": LEVEL["asked"] = time.time(); return self.send(200, {"l": LEVEL["l"], "r": LEVEL["r"], "comp": LEVEL.get("comp", 0), "lim": LEVEL.get("lim", 0), "lufs": LEVEL.get("lufs"), "agc": round(AGC["db"] + AGC["lift"], 1), "agc_state": AGC.get("state", ""), "agc_on": AGC["strength"] > 0.01, "ch": LEVEL.get("ch")})
            if p in ("/rights.csv", "/rights_check"):
                return self.send(200, *rights_report(qs, p.endswith(".csv")))
            if p == "/history.csv":
                h = history(qs, limit=5000); import csv, io; b = io.StringIO(); w = csv.writer(b, delimiter=";")
                w.writerow(["date", "time", "artist", "title", "folder", "type", "length", "tv", "file", "over the intro", "radio start", "radio end", "tv start", "tv end"])
                for r in reversed(h["items"]):
                    a = r.get("aud") or {}
                    w.writerow([h["day"], r["time"], r["artist"], r["title"], r["folder"], r["type"], r["len"], r["scene"], r["path"], r.get("over", ""), a.get("r0", ""), a.get("r1", ""), a.get("v0", ""), a.get("v1", "")])
                return self.send(200, ("\ufeff" + b.getvalue()).encode("utf-8"), "text/csv; charset=utf-8")
            if p == "/wave": return self.send(200, waveform(qs["path"][0]))
            if p == "/pvclip": return self.send(200, pvclip(qs["path"][0]))
            if p == "/startexact": return self.send(200, start_exact(qs["path"][0], qs.get("t", ["0"])[0]))
            if p == "/tempo_status": return self.send(200, tempo_status(qs["path"][0]))
            if p == "/wavezoom": return self.send(200, wave_zoom(qs["path"][0], float(qs.get("t", ["0"])[0]), float(qs.get("d", ["6"])[0])))
            if p == "/preview.jpg": return self.send(200, E.tv.screenshot(int(qs.get("w", ["640"])[0])), "image/jpeg")
            if p == "/playlists":
                pls = planner.api_playlists(dbc())
                for p_ in pls:
                    if p_.get("chart"):
                        try: p_["web"] = chart_state(p_["id"]); p_["web"]["show_at"] = chart_show_at(p_["name"])
                        except Exception: p_["web"] = None
                return self.send(200, pls)
            if p == "/playlist": return self.send(200, planner.api_preview_playlist(dbc(), int(qs["id"][0])))
            if p == "/playlist_take":    # the next songs of a playlist (for Up next / AUX); they count as played in its order
                c = dbc(); pid = int(qs["id"][0]); n = min(200, int(qs.get("n", ["0"])[0] or 0))
                r = c.execute("SELECT loop, shuffle, rotate FROM playlists WHERE id=?", (pid,)).fetchone()
                if not r: return self.send(404, {"error": "not found"})
                if not r[1] and not r[2] and n <= 0: paths = planner.playlist_songs(c, pid)          # an ordered list (Top 10): all of it, from the top
                else:
                    paths = []; n = n if n > 0 else min(100, max(1, planner.playlist_len(c, pid) if r[2] else len(planner.playlist_songs(c, pid))))   # 0 = the whole list (one round)
                    for i in range(n):
                        p_ = planner.take_from_playlist(c, pid, "start" if (i == 0 and not r[1] and not r[2]) else "continue", lambda x, P=paths: x not in P)
                        if p_ and p_ not in paths: paths.append(p_)
                    c.commit()
                return self.send(200, {"paths": paths})
            if p == "/clocks": return self.send(200, planner.api_clocks(dbc()))
            if p == "/schedule": return self.send(200, planner.api_schedule(dbc()))
            if p == "/folders":
                d_ = planner.api_folders(dbc(), station_sql(TL.ch, plan=True))
                if not (TL.ch or CH):        # Hype Radio: also EMPTY folders (e.g. a show's mix folder like MIXES/SATURDAY 21 after its mix was archived – 22 Nov: you could not find it to put the next mix in)
                    base = os.path.join(ROOT, "music")
                    for dp, dn, fn in os.walk(base):
                        dn[:] = sorted(x for x in dn if not x.startswith("."))
                        rel = os.path.relpath(dp, base)
                        if rel == "." or rel.count(os.sep) > 2: continue
                        f_ = rel.replace(os.sep, "/")
                        if f_ not in d_["counts"]: d_["music"].append(f_); d_["counts"][f_] = 0
                    d_["music"] = sorted(set(d_["music"]))
                return self.send(200, d_)
            if p == "/lib_show": return self.send(200, {"show": lib_show((qs.get("ch") or [""])[0]), "stations": [{"id": "main", "name": "Hype Radio", "color": "#05A9C6"}] + [{"id": x["id"], "name": x["name"], "color": x["color"], "logo": x.get("logo")} for x in chancfg.load()],
                                                       "roots": sorted(d for d in os.listdir(ROOT) if re.match(r"^(music|jingles)-[a-z][a-z0-9]{1,19}$", d))})
            if p == "/overlays": return self.send(200, planner.api_overlays(dbc()))
            if p == "/overlay_songs":   # songs with their own intro sweeper (set per song in the Library) - or "never"
                c = dbc(); rows = c.execute("""SELECT i.path, i.artist, i.title, i.folder, i.overlay_mode, i.overlay_path, j.title, j.folder FROM items i
                                              LEFT JOIN items j ON j.path = i.overlay_path WHERE i.overlay_mode IN ('song', 'none') ORDER BY i.artist, i.title""").fetchall()
                return self.send(200, [dict(path=r[0], artist=r[1], title=r[2], folder=r[3], mode=r[4], sweeper=r[5], sweeper_title=r[6] or (r[5] or "").split("/")[-1], sweeper_folder=r[7]) for r in rows])
            if p == "/shows": return self.send(200, planner.api_shows(dbc()))
            if p == "/week": return self.send(200, planner.api_week(dbc(), int((qs.get("w") or ["0"])[0])))
            if p == "/events": return self.send(200, planner.api_events(dbc()))
            if p == "/ads":
                c_ = dbc(); pl_ = planner.ad_played_today(c_)
                files = [{"path": r[0], "title": r[1], "folder": r[2], "len": r[3], "video": bool(r[4])} for r in c_.execute(
                    "SELECT path, title, folder, COALESCE(cue_end, duration, 0) - COALESCE(cue_start, 0), has_video FROM items WHERE section='ads' AND problem IS NULL ORDER BY folder, title")]
                sp_ = []
                for s_ in planner.ad_spots(c_):
                    dl = planner.deal_for(s_)
                    if dl:
                        same = [x["path"] for x in planner.ad_spots(c_) if planner.deal_for(x) and planner.deal_for(x).get("id") == dl.get("id")]
                        s_ = dict(s_, deal={k: dl.get(k) for k in ("id", "name", "start", "end", "spots_per_day", "days", "hours", "paused")}, deal_played=sum(pl_.get(p, 0) for p in set(same)))
                    sp_.append(dict(s_, played_today=pl_.get(s_["path"], 0)))
                linked = {s_["deal"]["id"] for s_ in sp_ if s_.get("deal")}
                try: dt_ = json.load(open(HL_SPONSORS)).get("t", 0)
                except Exception: dt_ = 0
                return self.send(200, {"spots": sp_, "files": files, "deals_t": dt_,
                                       "deals_unlinked": [{k: d_.get(k) for k in ("id", "name", "brands", "spots_per_day", "start", "end")} for d_ in planner.HL_DEALS if d_.get("id") not in linked],
                                       "max_len": planner.get_setting(c_, "ad_max_len", 150), "max_spots": planner.get_setting(c_, "ad_max_spots", 3)})
            if p == "/lower": return self.send(200, dict(LOWER, scene=E.tv.scene, live_layer=getattr(E.tv, "ll_on", True)))
            if p == "/settings": return self.send(200, get_settings())
            if p == "/autorec": return self.send(200, autorec_status())
            if p == "/listener_pads": return self.send(200, listener_pads((qs.get("ch") or [""])[0] if chancfg.valid((qs.get("ch") or [""])[0]) else ""))
            if p == "/padbanks":
                b = planner.api_padbanks(dbc()); played = {v.get("path"): v.get("played") for v in voice_state().values()}
                for bk in b:
                    for it in bk["items"]:
                        if it["path"].startswith(VOICE_PFX): it["voice"] = True; it["played"] = played.get(it["path"])
                return self.send(200, b)
            if p == "/live": return self.send(200, E.live.status())
            if p == "/guest_info":      # public (guest page): who am I, which other guests are on
                slot, info = E.live.guest_by_token((qs.get("t") or [""])[0])
                if not slot: return self.send(404, {"error": "This guest link is not valid (any more). Ask Hype Radio for a new one."})
                st = E.live.status()
                return self.send(200, {"slot": slot, "name": info["name"], "show": SHOW.get("title"), "live": st["live"], "mode": info.get("mode", "video"),
                                       "others": [s_ for s_, g_ in st["guests"].items() if s_ != slot and g_["online"]],
                                       "muted": [s_ for s_, g_ in st["guests"].items() if s_ != slot and g_.get("muted")]})   # muted by the host: the other guests don't hear them either
            if p == "/screen": return self.send(200, E.screen.current(E.tv.scene))
            if p.startswith("/cover/"):
                f = os.path.join(COVERS, os.path.basename(p))
                if not re.fullmatch(r"[0-9a-f]{16}\.jpg", os.path.basename(p)) or not os.path.exists(f): return self.send(404, {"error": "no cover"})
                return self.send(200, open(f, "rb").read(), "image/jpeg")
            if p == "/screenrules": return self.send(200, {"rules": planner.api_screen_rules(dbc()), "files": planner.screen_files(),
                                                         "tickers": [{"id": t["id"], "label": t["label"]} for t in tickers.config() if t.get("on")],
                                                         "bars": [t["bar"] for t in tickers.config() if t.get("bar")], "pip": pip_frames()})
            if p == "/tickers": return self.send(200, tickers.status())
            if p == "/preview_hour":
                c = dbc()
                if not planner.get_setting(c, "hour_preview", True): return self.send(200, {"off": True})
                return self.send(200, planner.preview_hour(c, day=int(qs["day"][0]) if "day" in qs else None, hour=int(qs["hour"][0]) if "hour" in qs else None,
                                                            clock=int(qs["clock"][0]) if "clock" in qs else None))
            if p == "/graphics" and TVF["ch"]:
                nm_ = (chancfg.get(TVF["ch"]) or {}).get("name") or ""
                fi_ = "station_%s.png" % TVF["ch"]; fp_ = os.path.join(ROOT, "branding", "shownames", fi_)     # the station's own box (Gianno's PNG)
                try: v_ = int(os.path.getmtime(fp_)) if os.access(fp_, os.R_OK) and os.stat(fp_).st_mode & 0o004 else 0      # (only one the web server can read)
                except OSError: v_ = 0
                return self.send(200, {"show": nm_, "show_image": fi_ if v_ else None, "show_v": v_, "start": 0, "end": 0,      # (no box file: the text box with its name)
                                       "temp": WEATHER["temp"], "code": WEATHER["code"]})
            if p == "/graphics": return self.send(200, {"show": SHOW["title"], "show_image": show_name_image(SHOW["title"]), "show_v": show_image_v(SHOW["title"]), "start": SHOW["start"], "end": SHOW["end"], "temp": WEATHER["temp"], "code": WEATHER["code"]})
            if p == "/pads":
                c = dbc(); rows = c.execute("SELECT path, title, folder, duration, has_video FROM items WHERE section='jingles' AND problem IS NULL AND " + station_sql(TL.ch, plan=True) + " ORDER BY folder, title").fetchall()
                return self.send(200, [dict(zip(["path", "title", "folder", "duration", "has_video"], r)) for r in rows])
            self.send(404, {"error": "not found"})
        except Exception as e:
            log.warning("GET %s: %s", p, e); self.send(500, {"error": str(e)})
    def _do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if urlparse(self.path).path in ("/api/upload/chunk", "/upload/chunk"):      # raw file piece, not JSON
            try: return self.send(200, up_chunk(parse_qs(urlparse(self.path).query), self.rfile, n))
            except Exception as e: log.warning("upload: %s", e); return self.send(500, {"ok": False, "error": str(e)})
        body = self.rfile.read(n)
        try: d = json.loads(body or b"{}")
        except Exception: d = {}
        p = urlparse(self.path).path.replace("/api", "", 1)
        if p == "/track":
            self.send(200, {"ok": True}); threading.Thread(target=E.on_track, args=(d,), daemon=True).start(); return
        if p.startswith("/vt/"):
            try: return self.send(200, vt_cmd(p[4:], d))
            except (ValueError, KeyError) as e: return self.send(400, {"ok": False, "error": str(e)})
            except Exception as e: log.warning("vt %s: %s", p, e); return self.send(500, {"ok": False, "error": str(e)})
        if p == "/deck_track":
            self.send(200, {"ok": True}); threading.Thread(target=E.on_deck, args=(d,), daemon=True).start(); return
        if p.startswith("/plan/"):
            a = p[6:]; c = dbc()
            try:
                with LOCK:
                    res = {"ok": True}
                    if a == "save_playlist": res["id"] = planner.api_save_playlist(c, d)
                    elif a == "add_to_playlist":       # Library: one song to the end of a playlist (once)
                        pid = int(d["id"]); path = str(d["path"])
                        if not c.execute("SELECT 1 FROM items WHERE path=?", (path,)).fetchone(): res = {"ok": False, "error": "file not found"}
                        elif c.execute("SELECT 1 FROM playlist_sources WHERE playlist_id=? AND kind='song' AND ref=?", (pid, path)).fetchone(): res = {"ok": True, "already": True}
                        else:
                            n = c.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM playlist_sources WHERE playlist_id=?", (pid,)).fetchone()[0]
                            c.execute("INSERT INTO playlist_sources(playlist_id, pos, kind, ref, amount) VALUES(?,?,?,?,1)", (pid, n, "song", path))
                            c.execute("UPDATE playlists SET updated=? WHERE id=?", (time.time(), pid)); c.commit()
                    elif a == "delete_playlist":
                        c.execute("DELETE FROM playlists WHERE id=?", (d["id"],)); c.execute("DELETE FROM playlist_sources WHERE playlist_id=?", (d["id"],))
                        c.execute("DELETE FROM playlist_state WHERE playlist_id=?", (d["id"],))
                        c.execute("DELETE FROM clock_slots WHERE kind='playlist' AND ref=?", (str(int(d["id"])),)); c.commit()   # no clock slot points at a playlist that is gone
                    elif a == "reset_playlist": c.execute("DELETE FROM playlist_state WHERE playlist_id=?", (d["id"],)); c.commit()
                    elif a == "save_clock": res["id"] = planner.api_save_clock(c, d)
                    elif a == "delete_clock":
                        c.execute("DELETE FROM clocks WHERE id=?", (d["id"],)); c.execute("DELETE FROM clock_slots WHERE clock_id=?", (d["id"],))
                        c.execute("UPDATE schedule SET clock_id=NULL WHERE clock_id=?", (d["id"],))
                        c.execute("DELETE FROM schedule_date WHERE clock_id=?", (d["id"],)); c.execute("UPDATE show_clocks SET clock_id=NULL WHERE clock_id=?", (d["id"],)); c.commit()
                    elif a == "set_schedule": planner.api_set_schedule(c, d)
                    elif a == "set_schedule_date": planner.api_set_schedule_date(c, d)
                    elif a == "save_overlays": planner.api_save_overlays(c, d); res["note"] = "rules saved"
                    elif a == "save_show_clocks": planner.api_save_show_clocks(c, d)
                    elif a == "save_events": planner.api_save_events(c, d)
                    elif a == "save_padbanks": planner.api_save_padbanks(c, d)
                    elif a == "save_screen": planner.api_save_screen_rules(c, d)
                    elif a == "save_ads": planner.api_save_ads(c, d); (None if TL.ch else E.replan())
                    elif a != "replan": return self.send(404, {"error": "unknown"})
                    if a in ("save_clock", "delete_clock", "set_schedule", "set_schedule_date", "replan", "delete_playlist", "save_playlist", "reset_playlist", "save_show_clocks"):
                        if TL.ch: chan_replan(TL.ch)           # a theme channel plans again in its own process – Hype Radio is not touched
                        else: E.replan()
                return self.send(200, res)
            except ValueError as e: return self.send(400, {"error": str(e)})
            except sqlite3.IntegrityError: return self.send(400, {"error": "that name is already used"})
            except Exception as e: log.warning("plan %s: %s", a, e); return self.send(500, {"error": str(e)})
        if p == "/cmd/lower": return self.send(200, lower_cmd(d))
        if p.startswith("/live/"):
            a = p[6:]; L = E.live
            try:
                if a == "on":
                    if not L.live and not L.status().get("studio_online"):    # never on air without the studio: that would be silence / the automation
                        return self.send(409, dict(L.status(), error="The studio is not connected – open Broadcast and connect the studio first"))
                    L.set_live(True)
                elif a == "target": L.set_target(d.get("target") or None)     # Broadcast → which station you go LIVE on
                elif a == "off":
                    L.set_live(False)
                    E.cmd("autoscene", {"on": True})        # END LIVE: the TV follows the music again (no camera left on screen)
                elif a == "guest_add": slot, g = L.guest_add(d.get("name"), d.get("slot"), int(d.get("hours") or 12), d.get("mode") or "video"); return self.send(200, {"ok": True, "slot": slot, "token": g["token"]})
                elif a == "guest_remove": L.guest_remove(d["slot"])
                elif a == "cam": L.set_cam(d["cam"], d["src"])
                elif a == "guest_vol": L.guest_vol(d["slot"], float(d.get("vol", 1)))
                elif a == "guest_mute": L.guest_mute(d["slot"], bool(d.get("on")))
                elif a == "rec_start": L.start_rec("manual")
                elif a == "rec_stop": L.stop_rec()
                elif a == "new_key": L.new_studio_key()
                elif a == "restream": L.restream(d["key"], bool(d.get("on")))
                elif a == "settings": L.settings(d)
                else: return self.send(404, {"error": "unknown"})
                return self.send(200, dict(L.status(), ok=True))
            except ValueError as e: return self.send(400, {"error": str(e)})
            except Exception as e: log.warning("live %s: %s", a, e); return self.send(500, {"error": str(e)})
        if p == "/cmd/screen_test":
            f = tickers.as_ticker(d.get("file") or "")
            if f == "adbox:": E.screen.add(None, f, 0.5, 30.0); return self.send(200, {"ok": True})
            if f.startswith("ticker:"):
                if not tickers.get(f[7:]): return self.send(400, {"error": "this ticker has no items yet (check Settings → Tickers)"})
                E.screen.add(None, f, 0.5, max(20.0, float(d.get("duration") or 30))); return self.send(200, {"ok": True})
            if f not in planner.screen_files(): return self.send(400, {"error": "file not found in branding/screen"})
            E.screen.add(None, f, 0.5, float(d.get("duration") or 8)); return self.send(200, {"ok": True})
        if p == "/tickers":
            try: tickers.save(d.get("tickers")); return self.send(200, tickers.status())
            except Exception as e: return self.send(400, {"error": str(e)})
        if p == "/guard":
            return self.send(200, guard_save(d))
        if p == "/autorec":
            try: return self.send(200, autorec_save(d))
            except Exception as e: return self.send(400, {"error": str(e)})
        if p == "/hypelive/poke":     # ⚡ Hype Live: something changed -> one sync now (signature = HMAC-SHA256 of the body with the write key)
            import hmac, hashlib
            try: g = json.load(open(GUARD_CONF))
            except Exception: g = {}
            sig = (self.headers.get("X-Hype-Signature") or "").replace("sha256=", "").strip()
            keys = [k for k in (g.get("hl_key_write"), g.get("hl_key")) if k]
            good = any(hmac.compare_digest(hmac.new(k.encode(), body, hashlib.sha256).hexdigest(), sig) for k in keys) if sig else False
            if not good: return self.send(403, {"ok": False, "error": "bad signature"})
            if abs(time.time() - float(d.get("t") or 0)) > 120: return self.send(400, {"ok": False, "error": "too old"})
            open(HL_POKE, "w").write(str(time.time())); log.info("Hype Live poke: %s", ",".join(map(str, d.get("what") or [])))
            return self.send(200, {"ok": True})
        if p == "/reload_pages":    # panel: reload every page in OBS (graphics, ad box, Live Layer, Hype Screen) within ~10 s
            open(RELOAD_BTN, "w").write(str(time.time())); log.info("reload of all OBS pages asked from the panel")
            return self.send(200, {"ok": True})
        if p == "/chart_publish":   # 📤 Publish a chart to the website (via Hype Live): {id, now}
            try:
                wk, a = chart_send(int(d["id"]), force=True, now=bool(d.get("now")))
                ok_ = (a or "").startswith("OK")
                return self.send(200 if ok_ else 502, {"week": wk, "answer": (a or "")[:80], "web": chart_state(int(d["id"])), **({} if ok_ else {"error": "Hype Live: " + (a or "no answer")[:80]})})
            except Exception as e: return self.send(500, {"error": str(e)[:120]})
        if p == "/top10_send":      # the Hype Top 10 to Hype Live now
            try: wk, a = top10_send(force=True); return self.send(200, {"week": wk, "answer": (a or "")[:80]})
            except Exception as e: return self.send(500, {"error": str(e)})
        if p == "/recap_send":      # the weekly recap to Hype Live now (w = 0 this week so far, 1 last week)
            try: wk, a = recap_send(int(d.get("w", 1))); return self.send(200, {"week": wk, "answer": a[:80]})
            except Exception as e: return self.send(500, {"error": str(e)})
        if p == "/lib_show":        # Library: which stations' songs this channel sees
            ch = str(d.get("ch") or "main"); ch = ch if chancfg.valid(ch) else "main"
            show = [x for x in (d.get("show") or []) if x == "main" or chancfg.valid(x)] or ["main"]
            c = sqlite3.connect(DB, timeout=30); planner.set_setting(c, "lib_show_" + ch, show)
            return self.send(200, {"ok": True, "show": lib_show(ch)})
        if p == "/settings":
            try: return self.send(200, set_settings(d))
            except Exception as e: log.warning("settings: %s", e); return self.send(500, {"error": str(e)})
        if p.startswith("/cmd/"):
            try: return self.send(200, E.cmd(p[5:], d))
            except Exception as e: log.warning("cmd %s: %s", p, e); return self.send(500, {"error": str(e)})
        self.send(404, {"error": "not found"})
    def log_message(self, *a): pass

RESYNC_N = {}     # song start -> how often its clip was put back in step (max 2 per song)
# ---------------- TV blocks: the TV follows a theme station (Hype Radio's radio stays as it is) ----------------
# Planning → TV: per hour of the week which station has the TV; On Air → TV: by hand. Hype Radio's engine keeps driving
# OBS (the same players, preload and sync as always); during a block it takes the songs from the station's engine.
# TV sound: every radio engine has an output on the TV bus (hype_air) with its own level "tv_vol" – Hype Radio 1, a station
# 0; a block crossfades them. Hype Radio's radio stream never changes.
TVF = {"ch": None, "started": None, "pre": None, "fail": 0, "since": 0}
TV_BLOCKS = {"t": 0, "d": None}
def tv_blocks(c=None):
    """{"grid": {"<weekday 0-6>-<hour>": "<station id>"}, "manual": None | "main" | "<station id>"} (main database)."""
    if c is None and TV_BLOCKS["d"] is not None and time.time() - TV_BLOCKS["t"] < 5: return TV_BLOCKS["d"]
    d = planner.get_setting(c or sqlite3.connect(DB, timeout=30), "tv_blocks", None) or {}
    d = {"grid": dict(d.get("grid") or {}), "manual": d.get("manual")}
    TV_BLOCKS.update(t=time.time(), d=d); return d
def tv_station_ok(cid):
    cfg = chancfg.get(cid) if cid else None
    return bool(cfg and cfg.get("enabled") and cfg.get("engine"))
def tv_want():
    """The station that should have the TV now (None = Hype Radio). LIVE from the studio always wins."""
    if CH: return None
    if E.live.live:            # LIVE: from the studio on Hype Radio -> the TV is Hype Radio's; LIVE on a station -> the TV is that station's
        t_ = E.live.target()
        return t_ if t_ and tv_station_ok(t_) else None
    b = tv_blocks(); lt = time.localtime()
    w = b["manual"] if b["manual"] else b["grid"].get("%d-%d" % (lt.tm_wday, lt.tm_hour))
    return w if w and w != "main" and tv_station_ok(w) else None
def tv_get(cid, path, timeout=2):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (chancfg.engine_port(cid), path), timeout=timeout) as r: return json.loads(r.read())
def tv_level(port, v):
    try: Liq(port).cmd("var.set tv_vol = %.3f" % v)
    except Exception as e: log.warning("TV sound level (%d): %s", port, e)
def _logo(n):
    """branding/logo_corner_<n>: .webm first (VP9 with alpha - OBS plays it at the right speed; the png .mov with long still
    parts ran too fast in OBS, 22 Nov), then .mov."""
    for x in ("webm", "mov"):
        f = "/srv/hype/branding/logo_corner_%s.%s" % (n, x)
        if os.path.isfile(f): return f
    return None
LOGO_MAIN = _logo("hype") or "/srv/hype/branding/logo_corner.mov"   # (22 Nov: the 3D coin-spin logo; the old one stays as logo_corner.mov)
def tv_logo(cid, fade=True):
    """The corner logo on TV: the station's during its TV block (branding/logo_corner_<id>.mov, same size and spot as
    Hype Radio's), Hype Radio's animated logo otherwise. No file for the station: Hype Radio's stays.
    The change fades: the old logo out in 0.5 s, the new file in while nothing shows, then fades in in 0.5 s
    (a "Fade" colour filter on Logo; runs in its own thread so the sound handover is never held up)."""
    f = (_logo(cid) if cid else None) or LOGO_MAIN
    def op(x):
        with E.tv.olock: E.tv.client().set_source_filter_settings("Logo", "Fade", {"opacity": round(x, 3)}, True)
    def run():
      with TV_LOGO_LOCK:
        try:
            with E.tv.olock:
                cl = E.tv.client()
                if (cl.get_input_settings("Logo").input_settings or {}).get("local_file") == f: return
                if not any(x["filterName"] == "Fade" for x in cl.get_source_filter_list("Logo").filters):
                    cl.create_source_filter("Logo", "Fade", "color_filter_v2", {"opacity": 1.0})
            if fade:
                for k in range(9, -1, -1): op(k / 10.0); time.sleep(0.05)
            with E.tv.olock: E.tv.client().set_input_settings("Logo", {"local_file": f, "looping": True}, True)
            log.info("TV logo: %s", os.path.basename(f))
            if fade:
                time.sleep(0.35)               # (the new file is loaded and showing its first frame)
                for k in range(1, 11): op(k / 10.0); time.sleep(0.05)
        except Exception as e:
            log.warning("TV logo: %s", e)
            try: op(1.0)
            except Exception: pass
    threading.Thread(target=run, daemon=True).start()
TV_LOGO_LOCK = threading.Lock()
def tv_scene():
    """The automatic scene for what the TV shows now (Hype Radio's song, or during a block the station's)."""
    if TVF["ch"]:
        n = tv_now() or {}
        return "Music video" if n.get("mode") in ("clip", "jingle-video", "pair") else "Hype Screen"
    return (E.now or {}).get("scene") or "Hype Screen"
def tv_now():
    """The song the TV shows now: Hype Radio's, or during a block the station's."""
    if TVF["ch"]:
        try: return tv_get(TVF["ch"], "/tvnow").get("now") or {}
        except Exception: return {}
    return E.now
GFX = {"hide": None, "at": 0.0}        # the graphics during a jingle: hidden or not (None = not known yet after a start: the first call always applies), and the moment it takes effect
GFX_SRC = ("Logo", "Live Layer")      # the OBS side (the Graphics / Clock / Top 10 pages follow GFX themselves, same moment)
def gfx_want(hide):
    if GFX["hide"] == hide: return
    at = time.time() + 0.6          # (hide 0.6 s into the jingle, back 0.6 s after the song starts: every page has heard it by then)
    GFX.update(hide=hide, at=at)
    threading.Thread(target=_gfx_fade, args=(hide, at), daemon=True).start()
def _gfx_fade(hide, at):
    time.sleep(max(0.0, at - time.time()))
    if GFX["at"] != at: return                       # (changed again meanwhile)
    try:
        with E.tv.olock:
            cl = E.tv.client()
            for src in GFX_SRC:
                if not any(f["filterName"] == "JHide" for f in cl.get_source_filter_list(src).filters):
                    cl.create_source_filter(src, "JHide", "color_filter_v2", {"opacity": 1.0})
                if src == "Logo":
                    try: cl.set_source_filter_enabled("Logo", "Hide", False)      # (the old on/off switch: not used any more)
                    except Exception: pass
        for k in range(1, 9):                         # 0.4 s fade, all sources in the same step
            if GFX["at"] != at: return
            op = (1 - k / 8) if hide else k / 8
            with E.tv.olock:
                cl = E.tv.client()
                for src in GFX_SRC: cl.set_source_filter_settings(src, "JHide", {"opacity": round(op, 3)}, True)
            time.sleep(0.05)
    except Exception as e: log.warning("graphics fade: %s", e)
def CLIP_HW():
    """Hardware decoding for the music video players (Clip A/B) and the pad / ID video layer: OFF since 26 Nov (test) –
    every video start with the graphics chip's decoder (shared with the TV encoding) came with a ~0.1 s gap in the TV
    sound. Back on: touch /var/lib/hype/clip_hw_on (no restart needed)."""
    return os.path.exists("/var/lib/hype/clip_hw_on")
def tv_item():
    """What the TV shows now: during a TV block the station's item (its jingle hides the clock / show name like Hype Radio's),
    else Hype Radio's. (22 Nov: Hype Radio's jingles hid the clock + show box over Ambiente's songs, Ambiente's did not.)"""
    return (TVF.get("now") or {}) if TVF.get("ch") else (E.now or {})
def tv_handover(old, new):
    """Picture + sound to another station: the sound crossfades in ~1.2 s, the picture switches to what it plays right now."""
    ports = {None: LIQ_PORT}
    for x in (old, new):
        if x: ports[x] = int(chancfg.get(x)["telnet"])
    TVF.update(ch=new, started=None, pre=None, fail=0, since=time.time(), now=None)
    for i in range(1, 13):
        f = i / 12.0
        tv_level(ports[new], f)
        tv_level(ports[old], 1.0 - f)
        time.sleep(0.1)
    for x, prt in ports.items():
        if x != new: tv_level(prt, 0.0)
    log.info("TV: %s → %s", (chancfg.get(old) or {}).get("name", "Hype Radio") if old else "Hype Radio", (chancfg.get(new) or {}).get("name", "Hype Radio") if new else "Hype Radio")
    tv_logo(new)
    if not new:
        E.screen.np = None
        try: E.tv.jingle_graphics(False)
        except Exception: pass
        resync_tv(force=True)        # Hype Radio's song on air, at the right spot
def tv_show_station(n):
    """A station's song on the TV: the same show() as Hype Radio's songs (preloaded clip, keyframe sync)."""
    c = dbc(); path = n.get("path") or ""; mode = n.get("mode"); clip = None; start = n.get("cue_start") or 0; kf = None
    TVF["now"] = n
    if mode in ("clip", "jingle-video"): clip, kf = tv_file(c, path), keyframes(c, path)
    elif mode == "pair" and n.get("clip_path"):      # its own video, shown without its own sound (the station's sound is on TV)
        clip, start = pair_clip(c, n["clip_path"])[:2]; kf = keyframes(c, n["clip_path"])
    el = time.time() - (n.get("started") or time.time())
    if getattr(E.tv, "hold_song", False): E.tv.hold_song = False; E.tv.auto = True; E.tv.scene = None
    if el > 3 and clip:     # joined in the middle (block start): straight to the spot it is at now
        scene = E.tv.show("clip" if mode == "pair" else mode, clip, start + el, False, 0.0, kf)
    else:
        scene = E.tv.show("clip" if mode == "pair" else mode, clip, start, False, 0.0, kf, t0=n.get("started"))
    if n.get("section") == "music":
        ln = max(1.0, (n.get("cue_mix") or n.get("cue_end") or n.get("duration") or 180) - (n.get("cue_start") or 0)); now = time.time()
        E.screen.np = {"id": uuid.uuid4().hex[:8], "title": n.get("title") or "", "artist": n.get("artist") or "", "at": [now + 5.0] + ([now + ln / 2] if ln > 150 else []), "start": now, "end": now + max(5.0, ln - el)}
    else: E.screen.np = None
    if JINGLE_HIDE:
        try: E.tv.jingle_graphics(n.get("section") != "music" and scene == "Music video")
        except Exception: pass
    log.info("TV (%s): %s – %s [%s] scene=%s", TVF["ch"], n.get("artist"), n.get("title"), mode, scene)
KEEP_SCENE, KEEP_SRC, KEEP_DIR = "Keep Listening", "Keep listening card", "/srv/hype/branding/keeplistening"
def keep_card_file(cid):
    """branding/keeplistening/<id>.png (hype.png = Hype Radio): the station the TV is LEAVING ("… keeps playing!" + its QR)."""
    f = os.path.join(KEEP_DIR, "%s.png" % (cid or "hype"))
    return f if os.path.isfile(f) else None
def keep_scene_ready():
    """The OBS scene "Keep Listening" = only the full-screen card (no Live Layer, logo, clock or song text): made once."""
    with E.tv.olock:
        cl = E.tv.client()
        if KEEP_SCENE not in [x["sceneName"] for x in cl.get_scene_list().scenes]:
            cl.create_scene(KEEP_SCENE); log.info("OBS: scene %s made", KEEP_SCENE)
        have = [x["sourceName"] for x in cl.get_scene_item_list(KEEP_SCENE).scene_items]
        if KEEP_SRC not in have:
            if KEEP_SRC in [x["inputName"] for x in cl.get_input_list().inputs]: cl.create_scene_item(KEEP_SCENE, KEEP_SRC, True)
            else: cl.create_input(KEEP_SCENE, KEEP_SRC, "image_source", {"file": keep_card_file(None) or ""}, True)
        for n in ("Radio audio", "FX audio", "Studio audio"):   # the TV sound (as in every scene): the station keeps playing under the card
            if n not in have: cl.create_scene_item(KEEP_SCENE, n, True)
        if KEEP_VID not in have:        # on top of the still card: the same card as a slow-zoom video (keep_card_video)
            if KEEP_VID in [x["inputName"] for x in cl.get_input_list().inputs]: cl.create_scene_item(KEEP_SCENE, KEEP_VID, True)
            else: cl.create_input(KEEP_SCENE, KEEP_VID, "ffmpeg_source", {"local_file": "", "looping": False, "restart_on_activate": True,
                                                                           "close_when_inactive": True}, True)
KEEP_VID = "Keep listening zoom"; KEEP_RENDER = {}
def keep_card_video(cid, block=False):
    """The card as a 20 s video with a slow zoom (100 → 106 %): a moving picture, so a website player doesn't stall on the
    jump from a still image to the video after it (24 Nov). Made by Studio itself (nice, in the background) the first time,
    and again when the PNG is replaced; until it is ready the still card shows."""
    png = keep_card_file(cid)
    if not png: return None
    mp4 = os.path.splitext(png)[0] + "_zoom.mp4"
    if os.path.isfile(mp4) and os.path.getmtime(mp4) >= os.path.getmtime(png): return mp4
    if KEEP_RENDER.get(png) and time.time() - KEEP_RENDER[png] < 600: return None
    KEEP_RENDER[png] = time.time()
    def render():
        tmp = mp4 + ".tmp.mp4"
        r = subprocess.run(["nice", "-n", "15", "ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "25", "-i", png,
                            "-vf", "scale=3840:2160,zoompan=z='1+0.06*on/500':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=500:s=1920x1080:fps=25,format=yuv420p",
                            "-frames:v", "500", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-g", "50", "-an", tmp], capture_output=True, timeout=900)
        if r.returncode == 0 and os.path.getsize(tmp) > 10000: os.replace(tmp, mp4); log.info("keep listening card video made: %s", os.path.basename(mp4))
        else: log.warning("keep listening card video: %s", r.stderr[-300:]); KEEP_RENDER[png] = time.time()
    if block: render(); return mp4 if os.path.isfile(mp4) else None
    threading.Thread(target=render, daemon=True).start()
    return None
def keep_card(cid, secs=None, test=False):
    """The "Keep listening" card of the station the TV leaves, full screen, before the new station's picture: its sound keeps
    playing under the card (the viewer hears what the card says keeps playing), then picture + sound go over together.
    Only when the scenes are on AUTO and not LIVE (a live show's own start / your cameras are never covered).
    Returns True if it was shown (then the caller does the handover)."""
    if secs is None:
        try: secs = float(planner.get_setting(dbc(), "keep_s", 8) or 0)
        except Exception: secs = 8.0
    f = keep_card_file(cid)
    if secs <= 0 or not f or not E.tv.auto or getattr(E.live, "live", False) or E.stopped: return False
    try:
        keep_scene_ready()
        v = keep_card_video(cid)
        with E.tv.olock:
            cl = E.tv.client(); cl.set_input_settings(KEEP_SRC, {"file": f}, True)
            cl.set_input_settings(KEEP_VID, {"local_file": v or ""}, True)      # (no video yet: empty = the still card underneath shows)
        with LOCK: E.tv.auto = False; E.tv.set_scene(KEEP_SCENE)
        log.info("TV: keep listening card %s (%.0f s)%s", os.path.basename(f), secs, " – test" if test else "")
        time.sleep(secs)
        return True
    except Exception as e:
        log.warning("keep listening card: %s", e); return False
    finally:
        with LOCK:
            if E.tv.scene == KEEP_SCENE and not E.tv.auto:
                E.tv.auto = True; E.tv.scene = None        # the card stays on screen until the next picture is set
def keep_back():
    """After a card (test or a switch): the picture of whatever is on TV now."""
    TVF["started"] = None
    if not TVF["ch"]: resync_tv(force=True)
def tv_follow_step():
    want = tv_want()
    if want != TVF["ch"]:
        keep_card(TVF["ch"]); tv_handover(TVF["ch"], want)      # (tv_handover sets the new picture: Hype Radio's at once, a station's next step)
    cid = TVF["ch"]
    if not cid: return
    try: d = tv_get(cid, "/tvnow"); TVF["fail"] = 0
    except Exception as e:
        TVF["fail"] += 1
        if TVF["fail"] >= 20:          # ~5 s without an answer: the TV goes back to Hype Radio until the station answers again
            log.warning("TV: %s does not answer (%s) – back to Hype Radio", cid, e); tv_handover(cid, None); TVF["since"] = time.time()
        return
    n = d.get("now") or {}
    if E.live.live: return         # LIVE on this station: the cameras are on TV (the normal LIVE scenes), its sound is on the TV bus
    if n.get("path") and n.get("started") != TVF["started"] and E.tv.auto:      # (a scene you chose stays; back on AUTO it catches up)
        TVF["started"] = n.get("started"); tv_show_station(n)
    nx = d.get("next") or {}
    if PRELOAD and nx.get("path") and nx.get("mode") in ("clip", "jingle-video") and TVF["pre"] != nx.get("id") and E.tv.auto:
        TVF["pre"] = nx.get("id"); c = dbc()
        threading.Thread(target=E.tv.preload, args=(tv_file(c, nx["path"]), nx.get("cue_start") or 0, keyframes(c, nx["path"])), daemon=True).start()
def tv_follow_loop():
    time.sleep(10)
    tv_level(LIQ_PORT, 1.0)         # (after an engine restart: Hype Radio has the TV sound until a block says otherwise)
    tv_logo(None)                   # (Hype Radio's logo; fades only if OBS had another file)
    def keep_prep():                # the Keep listening scene + the cards' zoom videos, one after the other (quietly, at nice 15)
        try: keep_scene_ready()
        except Exception as e: log.warning("keep listening scene: %s", e)
        for cid_ in [None] + [c_["id"] for c_ in chancfg.load() if c_.get("engine")]:
            try: keep_card_video(cid_, block=True)
            except Exception as e: log.warning("keep listening card video: %s", e)
    threading.Thread(target=keep_prep, daemon=True).start()
    for cfg in chancfg.load():
        if cfg.get("engine"): tv_level(int(cfg["telnet"]), 0.0)
    n_ = 0
    while True:
        try: tv_follow_step()
        except Exception as e: log.warning("TV follow: %s", e)
        n_ += 1
        if n_ % 8 == 0:      # every 2 s: which station is LIVE from the studio (hype-channels: the station's now playing says LIVE)
            try:
                d_ = {"target": E.live.target(), "live": bool(E.live.live and E.live.air == "studio"), "t": time.time()}
                if d_ != TVF.get("lt"):
                    TVF["lt"] = d_; tmp = "/run/hype/live_target.json.tmp"; json.dump(d_, open(tmp, "w")); os.replace(tmp, "/run/hype/live_target.json")
            except Exception: pass
        time.sleep(0.25)
def tv_info():
    b = tv_blocks()
    return {"ch": TVF["ch"], "since": TVF["since"], "manual": b["manual"], "grid": b["grid"],
            "stations": [{"id": x["id"], "name": x["name"], "color": x.get("color"), "ok": tv_station_ok(x["id"])} for x in chancfg.load()]}

def resync_tv(force=False):
    """After an OBS or engine restart: show the clip of the song on air again, at the right spot. force: right now (after a jump)."""
    n = tv_now()
    if not n or n.get("mode") not in ("clip", "pair", "jingle-video"): 
        if n and E.tv.auto: E.tv.scene = None; E.tv.set_scene(n.get("scene") or "Hype Screen")
        return
    try:
        with E.tv.olock: items = E.tv.client().get_scene_item_list("Music video").scene_items   # (one OBS request at a time)
        c = dbc(); el = time.time() - n["started"]
        if n["mode"] == "pair":
            if not n.get("clip_path"): return
            f, cs, snd, gain = pair_clip(c, n["clip_path"]); start = cs + el
        else:
            f, start, snd, gain = tv_file(c, n["path"]), (n.get("cue_start") or 0) + el, False, 0.0
        if TVF["ch"]: snd = False      # a TV block: the station's sound, its clips are picture only
        if not f: return
        on = [i["sourceName"] for i in items if i["sceneItemEnabled"] and i["sourceName"] in ("Clip A", "Clip B")]
        if on and not force and (E.tv.scene == "Music video" or not E.tv.auto):
            # a clip is on screen – but is it this song's clip? After an OBS restart OBS comes back with the clip it had
            # saved (16 Nov: the previous song's video ran under the next song). Only then show the right one.
            with E.tv.olock: shown = E.tv.client().get_input_settings(on[0]).input_settings.get("local_file")
            if not shown: return
            if os.path.realpath(shown) == os.path.realpath(f):
                # the right clip – but still in step with the radio? (16 + 18 Nov: BOTZ / X (EQUIS) drifted, right after a video jingle)
                if n["mode"] == "jingle-video" or el < 10 or (n.get("cue_mix") or n.get("cue_end") or n.get("duration") or 999) - start < 15 or RESYNC_N.get(n.get("started"), 0) >= 2: return
                with E.tv.olock: cur_ = E.tv.client().get_media_input_status(on[0]).media_cursor
                if cur_ is None: return
                off_ = cur_ / 1000.0 - start
                if abs(off_) < 0.6: return
                RESYNC_N[n.get("started")] = RESYNC_N.get(n.get("started"), 0) + 1
                log.info("TV resync: %s was %.1f s %s the radio – put back in step", n["title"], abs(off_), "ahead of" if off_ > 0 else "behind")
            else:
                if el < 8: return      # the song just started: its clip is still being put on (17 Nov: 13 needless restarts of a clip at 0 s)
                log.info("TV resync: %s showed another clip (%s) – putting the right one on", on[0], os.path.basename(shown))
        keep = None if E.tv.auto else E.tv.scene      # a scheduled / manual scene (e.g. PIP shoutout) stays – and still goes back to AUTO after its time
        E.tv.scene = None
        E.tv.show(n["mode"], f, start, snd, gain, None if snd else keyframes(c, n["path"]))
        if keep is not None: E.tv.scene = keep
        log.info("TV resync: %s at %.0f s", n["title"], el)
    except Exception as e: log.warning("resync: %s", e); E.tv.cl = None

def ensure_adbox():
    """The ad box slideshow is its own browser source in the Hype Screen scene: above the Hype Screen page, below the Live Layer."""
    with E.tv.olock:
        try:
            cl = E.tv.client(); items = cl.get_scene_item_list("Hype Screen").scene_items
            names = [i["sourceName"] for i in items]
            if "Ad box" not in names:
                cl.create_input("Hype Screen", "Ad box", "browser_source", {"url": "http://127.0.0.1:8081/adbox.html", "width": 1920, "height": 1080, "fps_custom": True, "fps": 25, "reroute_audio": True}, True)
                items = cl.get_scene_item_list("Hype Screen").scene_items; log.info("OBS: Ad box source made")
            ad = next(i for i in items if i["sourceName"] == "Ad box")
            hs = next((i for i in items if i["sourceName"] == "Hype Screen page"), None)
            want = (hs["sceneItemIndex"] + 1) if hs else 1
            if ad["sceneItemIndex"] != want: cl.set_scene_item_index("Hype Screen", ad["sceneItemId"], want)
        except Exception as e: log.warning("ad box source: %s", e); E.tv.cl = None

MEDIA_DIR = "/srv/hype/branding/media"
MEDIA_SCENES = ["MEDIA FULL", "CAM 1 M", "CAM 1 2 M", "CAM 1 2 3 M", "CAM 1 M G", "CAM 1 2 3 M G", "M 1", "M 1 2", "M 1 2 3", "M 1 2 3 4", "CAM 1 M PIP", "M 1 LIVE MIX", "V M PIP"]   # M = the media window in place of the video; V M PIP = the music video big + media small
MEDIA_CACHE = SP("/var/lib/hype/media_cache.json")
_MC = {}
def media_files():
    """Pictures and videos in branding/media, with length / sound / loudness (measured once per file)."""
    global _MC
    if not _MC:
        try: _MC = json.load(open(MEDIA_CACHE))
        except Exception: _MC = {}
    out = []; changed = False
    try: names = sorted(os.listdir(MEDIA_DIR), key=str.lower)
    except OSError: names = []
    for f in names:
        ext = os.path.splitext(f)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".m4v"): continue
        full = os.path.join(MEDIA_DIR, f)
        try: st = os.stat(full)
        except OSError: continue
        if time.time() - st.st_mtime < 20: continue          # still uploading
        key = f"{f}|{int(st.st_mtime)}|{st.st_size}"
        info = _MC.get(key)
        if info is None:
            info = {"kind": "picture" if ext in (".png", ".jpg", ".jpeg", ".webp") else "video", "duration": 0, "audio": False, "lufs": None}
            if info["kind"] == "video":
                try:
                    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", full],
                                                   capture_output=True, text=True, timeout=30).stdout or "{}")
                    info["duration"] = round(float(pr.get("format", {}).get("duration") or 0), 2)
                    info["audio"] = any(x.get("codec_type") == "audio" for x in pr.get("streams", []))
                    if info["audio"]:      # loudness in the background (a long video takes a while)
                        def measure(info=info, full=full):
                            try:
                                m = re.findall(r"I:\s*(-?[\d.]+) LUFS", subprocess.run(["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostats", "-i", full, "-map", "0:a:0",
                                                "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True, timeout=1800).stderr)
                                info["lufs"] = float(m[-1]) if m else None; jdump(dict(_MC), MEDIA_CACHE)   # (a copy: the main thread may add files meanwhile)
                            except Exception as e: log.warning("media loudness: %s", e)
                        threading.Thread(target=measure, daemon=True).start()
                except Exception as e: log.warning("media %s: %s", f, e)
            _MC[key] = info; changed = True
        out.append(dict(info, file=f))
    if changed:
        try: jdump(dict(_MC), MEDIA_CACHE)
        except Exception: pass
    return out

class Media:
    """What plays in the 'Media window' (used by MEDIA FULL and the M scenes: the camera layouts with media in place of the video).
    The video's sound goes through the radio (pads queue), so radio and TV hear it. Sound: mute | duck (music down) | off (music off).
    Without loop a video stays on its last frame."""
    def __init__(self): self.cur = None; self.token = None
    def play(self, eng, d):
        f = os.path.basename(d.get("file") or ""); full = os.path.join(MEDIA_DIR, f)
        info = next((m for m in media_files() if m["file"] == f), None)
        if not info or not os.path.exists(full): raise ValueError("file not found in branding/media")
        sound = d.get("sound") if d.get("sound") in ("mute", "duck", "off") else "duck"
        loop = bool(d.get("loop")) and info["kind"] == "video"
        self.stop(eng, keep_scene=True)
        tv = eng.tv; token = uuid.uuid4().hex; self.token = token
        with tv.olock:
            cl = tv.client()
            vid = cl.get_scene_item_id("Media window", "Media video").scene_item_id; pic = cl.get_scene_item_id("Media window", "Media picture").scene_item_id
            if info["kind"] == "picture":
                cl.set_input_settings("Media picture", {"file": full}, True)
                cl.set_scene_item_enabled("Media window", pic, True); cl.set_scene_item_enabled("Media window", vid, False)
            else:
                cl.set_input_settings("Media video", {"is_local_file": True, "local_file": full, "looping": loop, "restart_on_activate": False,
                                                      "close_when_inactive": False, "clear_on_media_end": False, "hw_decode": True}, True)
                cl.set_input_mute("Media video", True)
                cl.trigger_media_input_action("Media video", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                cl.set_scene_item_enabled("Media window", vid, True); cl.set_scene_item_enabled("Media window", pic, False)
        manual = d.get("scene") == "manual"      # Manual: only load the media window – you switch the scenes yourself
        sc = (tv.scene or "") if manual else d.get("scene") or (tv.scene if tv.scene in MEDIA_SCENES else "MEDIA FULL")
        if not manual and sc in MEDIA_SCENES:
            with LOCK: tv.auto = False; tv.set_scene(sc)
        self.cur = {"file": f, "kind": info["kind"], "sound": sound, "loop": loop, "started": time.time(), "duration": info["duration"], "scene": "manual" if manual else sc, "manual": manual}
        if info["kind"] == "video" and info["audio"] and sound != "mute":
            ln = max(1.0, info["duration"]); gain = 0.0 if info["lufs"] is None else max(-12.0, min(12.0, -14.0 - info["lufs"]))
            gain += 20 * math.log10(max(0.01, MEDIA_VOL["v"]))      # the TV Media card's VOL
            ann = f'annotate:liq_cue_in="0",liq_amplify="{gain:.2f} dB",hype_fade="0":{full}'
            level = 0.0 if sound == "off" else float(planner.get_setting(dbc(), "pad_duck", 0.25))
            def run():
                eng.liq.cmd("pads.push " + ann)
                while self.token == token:
                    eng.duck_for(ln + 0.3, level)
                    t_end = time.time() + ln
                    while time.time() < t_end and self.token == token: time.sleep(0.05)
                    if self.token != token or not loop: break
                    eng.liq.cmd("pads.push " + ann)
                if self.token == token and not loop: self.cur = dict(self.cur, ended=True)
            threading.Thread(target=run, daemon=True).start()
        log.info("media on: %s (%s, sound %s%s) in %s", f, info["kind"], sound, ", loop" if loop else "", sc)
        return self.cur
    # ---------- live streams (🔴 LIVE pads): HLS / RTMP / YouTube live in the Media window ----------
    def play_stream(self, eng, d, url):
        st = next((x for x in streams_load() if x["id"] == d.get("id")), None)
        if not st: raise ValueError("that live pad is gone")
        sound = "off" if d.get("sound") == "off" else "mute"      # live: Mute (picture only, the music goes on) or Music off (its sound on radio + TV)
        if sound == "off":
            if getattr(eng.live, "live", False): raise ValueError("you are LIVE from the studio – a stream can only play muted now")
            try: ok_ = "true" in eng.liq.cmd("var.get ext_on") or "false" in eng.liq.cmd("var.get ext_on")
            except Exception: ok_ = False
            if not ok_: raise ValueError("'Music off' for live streams needs tonight's radio update – use Mute for now")
        was_auto = bool(eng.tv.auto)
        self.stop(eng, keep_scene=True)
        tv = eng.tv; token = uuid.uuid4().hex; self.token = token
        with tv.olock:
            cl = tv.client()
            vid = cl.get_scene_item_id("Media window", "Media video").scene_item_id; pic = cl.get_scene_item_id("Media window", "Media picture").scene_item_id
            cl.set_input_settings("Media video", {"is_local_file": False, "input": url, "input_format": "", "looping": False, "restart_on_activate": False,
                                                  "close_when_inactive": False, "clear_on_media_end": False, "hw_decode": True,
                                                  "reconnect_delay_sec": 2, "buffering_mb": 4}, True)
            cl.set_input_mute("Media video", True)
            cl.trigger_media_input_action("Media video", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
            cl.set_scene_item_enabled("Media window", vid, True); cl.set_scene_item_enabled("Media window", pic, False)
        manual = d.get("scene") == "manual"
        sc = (tv.scene or "") if manual else d.get("scene") or (tv.scene if tv.scene in MEDIA_SCENES else "MEDIA FULL")
        if not manual and sc in MEDIA_SCENES:
            with LOCK: tv.auto = False; tv.set_scene(sc)
        self.cur = {"file": "live:" + st["id"], "kind": "live", "name": st["name"], "sound": sound, "loop": False, "started": time.time(), "duration": None,
                    "scene": "manual" if manual else sc, "manual": manual, "was_auto": was_auto and not manual}
        if sound == "off":
            self.feed = StreamFeed(url); self.feed.start()
            eng.liq.cmd("var.set ext_on = true")
            tv.ext = True; tv.route_audio(force=True)
        threading.Thread(target=self.watch_stream, args=(eng, token, st), daemon=True).start()
        log.info("live stream on: %s (%s, sound %s) in %s", st["name"], st["url"][:80], sound, sc)
        return self.cur
    STREAM_GIVE_UP = 15     # s without picture -> back to the normal programme (AUTO SCENES) or only a warning (manual)
    def watch_stream(self, eng, token, st):
        """The stream must keep playing: OBS's media state every 2 s. Bad -> restart it (YouTube: a fresh link); still bad after
        STREAM_GIVE_UP s -> with AUTO SCENES the normal programme comes back; on manual you stay in control (only a warning).
        The radio never goes silent: Liquidsoap plays the music again by itself as soon as the stream's sound stops."""
        bad_t = None; tries = 0
        time.sleep(6)
        while self.token == token:
            try:
                with eng.tv.olock: state = eng.tv.client().get_media_input_status("Media video").media_state
            except Exception: state = "?"
            ok = state in ("OBS_MEDIA_STATE_PLAYING", "OBS_MEDIA_STATE_OPENING", "OBS_MEDIA_STATE_BUFFERING")
            fed = not getattr(self, "feed", None) or self.feed.alive()
            if ok and fed:
                if bad_t: log.info("live stream %s: back", st["name"]); self.cur = dict(self.cur or {}, error=None)
                bad_t = None; tries = 0
            else:
                now = time.time()
                if bad_t is None: bad_t = now; log.warning("live stream %s: no signal (%s%s) – reconnecting", st["name"], state, "" if fed else ", sound feed down")
                if self.cur: self.cur = dict(self.cur, error="no signal – reconnecting")
                if now - bad_t >= self.STREAM_GIVE_UP:
                    if self.cur and self.cur.get("was_auto"):
                        log.warning("live stream %s: no signal for %d s – back to the normal programme", st["name"], self.STREAM_GIVE_UP)
                        self.stop(eng); STREAM_NOTE.update(t=time.time(), text="Live stream %s stopped: no signal – back to the normal programme" % st["name"]); return
                    if self.cur and self.cur.get("error") != "no signal – you are on manual, stop it when you want":
                        log.warning("live stream %s: no signal – manual scenes, so it stays until you stop it", st["name"])
                        self.cur = dict(self.cur, error="no signal – you are on manual, stop it when you want")
                elif now - bad_t >= 3 + tries * 5 and tries < 3:
                    tries += 1
                    try:
                        url = stream_resolve(st["url"])
                        with eng.tv.olock:
                            cl = eng.tv.client(); cl.set_input_settings("Media video", {"input": url}, True)
                            cl.trigger_media_input_action("Media video", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                        if getattr(self, "feed", None): self.feed.url = url
                    except Exception as e: log.warning("live stream reconnect: %s", e)
            time.sleep(2)
    def stop(self, eng, keep_scene=False):
        if self.cur and self.cur.get("manual"): keep_scene = True     # Manual: the scene stays where you put it
        was_live = bool(self.cur and self.cur.get("kind") == "live"); live_off = was_live and self.cur.get("sound") == "off"
        if was_live:
            f_ = getattr(self, "feed", None); self.feed = None
            if f_: f_.stop()
            if live_off:
                try: eng.liq.cmd("var.set ext_on = false"); eng.liq.cmd("radio.skip")     # the automation goes on with the NEXT song (no half song)
                except Exception as e: log.warning("live stream stop: %s", e)
            eng.tv.ext = False; eng.tv.route_audio(force=True)
            try:
                with eng.tv.olock: eng.tv.client().set_input_settings("Media video", {"is_local_file": True}, True)
            except Exception: pass
            log.info("live stream off")
        had_sound = bool(self.cur and self.cur.get("kind") == "video" and self.cur.get("sound") != "mute")
        self.token = None; self.cur = None
        if had_sound:
            eng.duck_until = 0
            try: eng.liq.cmd("pads.stop")
            except Exception: pass
        with eng.tv.olock:
            try:
                cl = eng.tv.client()
                cl.trigger_media_input_action("Media video", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP")
                for n in ("Media video", "Media picture"):
                    cl.set_scene_item_enabled("Media window", cl.get_scene_item_id("Media window", n).scene_item_id, False)
            except Exception as e: log.warning("media stop: %s", e); eng.tv.cl = None
        if not keep_scene and eng.tv.scene in MEDIA_SCENES:
            with LOCK:
                eng.tv.auto = True; eng.tv.scene = None
                if eng.now: eng.tv.set_scene(eng.now.get("scene") or "Hype Screen")

def pre_diag(d):
    """Measuring the preloaded clips (19 Nov): one line per preload / start in /var/lib/hype/preload_diag.jsonl, with OBS's running
    time and the server load – to find why OBS sometimes didn't keep a clip paused / didn't start it."""
    try:
        up = time.time() - float(subprocess.run(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", "hype-obs"], capture_output=True, text=True, timeout=3).stdout.strip() or 0) / 1e6 - (time.time() - time.monotonic())
        d.update(t=round(time.time(), 2), obs_up_h=round(up / 3600, 1), load=os.getloadavg()[0])
        with open(SP("/var/lib/hype/preload_diag.jsonl"), "a") as f: f.write(json.dumps(d) + "\n")
    except Exception: pass
STREAMS = SP("/var/lib/hype/streams.json"); STREAM_NOTE = {"t": 0, "text": ""}
MEDIA_VOL = {"v": 1.0}     # TV Media card → VOL (0 – 1.6, like the pads): media videos' sound + a live stream / the browser with Music off
WEB_STREAM = {"id": "web", "name": "Browser", "url": "rtsp://127.0.0.1:8554/web"}
def streams_load(saved_only=False):
    try: L = json.load(open(STREAMS))
    except Exception: L = []
    return L if saved_only else L + [WEB_STREAM]       # the browser plays on air like a LIVE pad
def is_youtube(u): return bool(re.search(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", u or ""))
def stream_resolve(u):
    """The playable link: HLS / RTMP / SRT as given; a YouTube live -> its HLS link via yt-dlp (expires after hours: asked again on reconnect)."""
    if not is_youtube(u): return u
    r = subprocess.run(["yt-dlp", "-g", "-f", "best[height<=1080][protocol*=m3u8]/best[height<=1080]/best", "--no-warnings", u], capture_output=True, text=True, timeout=60)
    link = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not link: raise ValueError("YouTube: " + ((r.stderr or "no link").strip().splitlines() or ["no link"])[-1][:140])
    return link[0]
class StreamFeed:
    """The stream's sound into the radio (Liquidsoap harbor "ext" on 8006, like the studio's): ffmpeg, restarted when it stops."""
    def __init__(self, url): self.url = url; self.p = None; self.on = False; self.t_ok = time.time()
    def start(self):
        self.on = True; threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        pw = json.load(open("/etc/hype/live.json"))["harbor_pw"]
        while self.on:
            net = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4"] if self.url.startswith("http") else []
            self.p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *net, "-i", self.url, "-vn", "-af", "volume=%.3f" % MEDIA_VOL["v"], "-ac", "2", "-ar", "48000",
                                       "-c:a", "flac", "-compression_level", "2", "-f", "ogg", "-content_type", "application/ogg",
                                       "icecast://source:%s@127.0.0.1:8006/ext" % pw], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.t_ok = time.time(); self.p.wait()
            if self.on: time.sleep(2)
    def alive(self): return bool(self.p and self.p.poll() is None) or time.time() - self.t_ok < 5
    def stop(self):
        self.on = False
        try: self.p and self.p.terminate()
        except Exception: pass

_ST = {"t": 0, "d": None, "pub": None, "pub_t": 0}
# test tools, scripts and bots are no listeners / viewers (13 Nov: a stream test from ~30 cloud addresses with Python-urllib
# showed as +11 / -11 TV viewers on two songs). Real players (browsers, AVPlayer, ExoPlayer/okhttp, VLC, Lavf, IPTV boxes) stay.
NOT_PLAYER = re.compile(r"^(python-|python/|curl/|wget/|go-http-client|aiohttp|node-fetch|node/|axios/|undici|libwww-perl|httpie|postman|insomnia|guzzle|scrapy)"
                        r"|bot\b|bot/|crawl|spider|headlesschrome|phantomjs|uptime|pingdom|statuscake|site24x7|lighthouse|facebookexternalhit|whatsapp/|slurp"
                        r"|hypelive-alarm|hypelive|hypestudio", re.I)     # (our own checks: Hype Live's off-air alarm every minute - not a listener / viewer)
def is_player(ua): return not NOT_PLAYER.search(ua or "")
def log_devices(log_, keep, window):
    """Devices (internet address + app/browser) that fetched pieces in the last `window` s, from an nginx log.
    Address + user agent: two different phones on the same wifi count as 2; the same player on both links as 1."""
    out = set(); now = time.time()
    try:
        with open(log_, "rb") as f:
            f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 3_000_000)); lines = f.read().decode("utf-8", "replace").splitlines()
        for ln in reversed(lines):
            if not keep(ln): continue
            m = re.match(r'(\S+) \S+ \S+ \[([^\]]+)\]', ln)
            if not m: continue
            try: t = time.mktime(time.strptime(m.group(2).split()[0], "%d/%b/%Y:%H:%M:%S"))
            except ValueError: continue
            if now - t > window: break
            ua = re.findall(r'"([^"]*)"', ln); ua = ua[-1] if ua else ""
            if is_player(ua): out.add(m.group(1) + "|" + ua)
    except OSError: pass
    return out
ICE_ADMIN = {"t": 0, "auth": None}
MP3_HITS = []     # (time, address, player) of every /radio.mp3 start (nginx mirrors it to /mp3hit; nothing waits on it)
MP3_HITS_FILE = SP("/var/lib/hype/mp3hits.json")
try: MP3_HITS.extend(tuple(h) for h in json.load(open(MP3_HITS_FILE)) if time.time() - h[0] < 7 * 86400)
except Exception: pass
def mp3_devices(mount="/radio.mp3"):
    """MP3 listeners as devices: Icecast's listener list (player + connected seconds) matched to the start nginx reported
    (address + player + time). An unmatched connection still counts, as its own device."""
    try:
        if not ICE_ADMIN["auth"] or time.time() - ICE_ADMIN["t"] > 3600:
            x = open("/etc/icecast2/icecast.xml").read()
            ICE_ADMIN.update(t=time.time(), auth=base64.b64encode((re.search(r"<admin-user>([^<]+)", x).group(1) + ":" + re.search(r"<admin-password>([^<]+)", x).group(1)).encode()).decode())
        r = urllib.request.Request("http://127.0.0.1:8000/admin/listclients?mount=" + urllib.parse.quote(mount), headers={"Authorization": "Basic " + ICE_ADMIN["auth"]})
        x = urllib.request.urlopen(r, timeout=3).read().decode("utf-8", "replace")
        import html as _h
        out = set(); now = time.time()
        for blk in re.findall(r"<listener[^>]*>(.*?)</listener>", x, re.S):
            g = lambda tag: (re.search(f"<{tag}>([^<]*)</{tag}>", blk) or [None, ""])[1]
            ip, ua, lid = g("IP"), _h.unescape(g("UserAgent")), g("ID")
            if ua.startswith("HypeStudio") or (ip in ("127.0.0.1", "::1") and ua.startswith("Python-urllib")):
                continue        # our own readers on the server (the backup relay to Streamerr) are not listeners
            if not is_player(ua): continue      # test tools / bots
            try: start = now - float(g("Connected") or 0)
            except ValueError: start = now
            if ip and ip not in ("127.0.0.1", "::1"): out.add(ip + "|" + ua); continue     # a direct listener (port 8000)
            hit = min((h for h in MP3_HITS if h[2] == ua and abs(h[0] - start) < 8 and (h[3] if len(h) > 3 else "/radio.mp3") == mount), key=lambda h: abs(h[0] - start), default=None)
            out.add((hit[1] + "|" + ua) if hit else ("conn" + lid + "|" + ua))
        return out
    except Exception as e:
        log.warning("mp3 listeners: %s", e); return None
def tv_viewer_ips(window=40):
    """Different devices (address + player) that fetched TV stream pieces in the last `window` s (test link + stream.hyperadio.nl/tv/)."""
    return log_devices("/var/log/nginx/stream.access.log", lambda ln: " /tv/" in ln and ".ts" in ln, window)
def hls_viewers(window=40): return len(tv_viewer_ips(window))
VIEW = {"t": 0, "d": None}; VSTATS_FILE = SP("/var/lib/hype/viewers.json")
VIEW_LOCK = threading.Lock(); LISTEN_LOCK = threading.Lock()   # one request at a time recounts (public /nowplaying: many at once)
def viewers():
    """TV viewers on Hype Studio's own video stream (all players: website, app, TV apps, VLC …): now, peak today, unique today."""
    if VIEW["d"] and time.time() - VIEW["t"] < 4: return VIEW["d"]
    with VIEW_LOCK:
        if VIEW["d"] and time.time() - VIEW["t"] < 4: return VIEW["d"]     # another request just counted
        return _viewers()
def _viewers():
    ips = tv_viewer_ips(); now_n = len(ips); day = time.strftime("%Y-%m-%d")
    try: st = json.load(open(VSTATS_FILE))
    except Exception: st = {}
    if st.get("day") != day: aud_archive("tv", st); st = {"day": day, "peak": 0, "peak_t": 0, "uniq": []}
    if now_n > st["peak"]: st["peak"], st["peak_t"] = now_n, int(time.time())
    u = set(st.get("uniq", []))
    for ip in ips: u.add(hashlib.sha1((day + ip).encode()).hexdigest()[:12])     # no IPs stored, only a daily hash
    st["uniq"] = sorted(u)
    try: jdump(st, VSTATS_FILE)
    except Exception: pass
    d = {"current": now_n, "peak_today": st["peak"], "peak_time": st["peak_t"], "unique_today": len(u)}
    VIEW.update(t=time.time(), d=d); return d
AUD_DAYS = SP("/var/lib/hype/audience_days.json")
def aud_archive(kind, st):
    """A finished day's numbers (peak + unique devices) are kept for the week report (90 days)."""
    if not st.get("day"): return
    try: d = json.load(open(AUD_DAYS))
    except Exception: d = {}
    d.setdefault(st["day"], {})[kind] = {"peak": st.get("peak", 0), "unique": max(len(st.get("uniq", [])), st.get("peak", 0))}
    for k in sorted(d)[:-90]: d.pop(k, None)
    try: jdump(d, AUD_DAYS)
    except Exception as e: log.warning("audience archive: %s", e)

def week_report(qs):
    """Week report (Monday–Sunday) of the playout side: what played, rotation, stability, own streams.
    ?w=0 this week so far, 1 = last week (default), 2 = the week before …"""
    import datetime as D, collections as C
    w = int((qs.get("w") or ["1"])[0])
    today = D.date.today(); mon = today - D.timedelta(days=today.weekday()) - D.timedelta(weeks=w)
    t0 = time.mktime(mon.timetuple()); t1 = time.mktime((mon + D.timedelta(days=7)).timetuple())
    c = dbc()
    rows = c.execute("SELECT started, path, artist, title, folder, mode, kind FROM asrun WHERE started>=? AND started<? ORDER BY started", (t0, t1)).fetchall()
    chw = getattr(TL, "ch", None)
    music = [r for r in rows if re.match(r"music(-[a-z0-9_]+)?/", r[1] or "") and "/MIXES/" not in (r[1] or "") and "/RECORDINGS/" not in (r[1] or "")]
    songs = C.Counter(r[1] for r in music); arts = C.Counter()
    for r in music:
        for a in re.split(r"\s+(?:x|X|&|feat\.?|ft\.?|,)\s+", r[2] or ""):
            if a.strip(): arts[a.strip()] += 1
    title = {r[1]: "%s – %s" % (r[2] or "", r[3] or "") for r in music}
    genre = lambda p: (p.split("/") + ["", ""])[1]
    genres = C.Counter(genre(r[1]) for r in music)
    vids = sum(1 for r in music if r[5] in ("clip", "pair"))
    jingles = sum(1 for r in rows if re.match(r"jingles(-[a-z0-9_]+)?/", r[1] or ""))
    mixes = sum(1 for r in rows if "/MIXES/" in (r[1] or "") or "/RECORDINGS/" in (r[1] or ""))
    # rotation: active music (not archive / mixes / problem files)
    lib = [x[0] for x in c.execute("SELECT path FROM items WHERE section='music' AND problem IS NULL AND path NOT LIKE '%/MIXES/%' AND path NOT LIKE '%/RECORDINGS/%' AND path NOT LIKE '%ARC%/%' AND path NOT LIKE '%ARCHIVE%'")]
    played = set(songs); never = [p for p in lib if p not in played]
    folders = C.defaultdict(lambda: [0, 0, 0])      # folder -> songs, played songs, plays
    for p in lib:
        f = "/".join(p.split("/")[1:-1]); folders[f][0] += 1
        if p in played: folders[f][1] += 1; folders[f][2] += songs[p]
    rot = sorted(([f, n, pl, plays] for f, (n, pl, plays) in folders.items()), key=lambda x: x[0])
    days = (min(t1, time.time()) - t0) / 86400
    often = [[title[p], n] for p, n in songs.most_common() if n > max(3, 3 * days)][:15]       # more than 3× a day
    # stability: the guard's events of this week
    ev = []
    try:
        for ln in (open("/var/lib/hype/guard_events.jsonl") if not chw else ()):
            try: e = json.loads(ln)
            except ValueError: continue
            if t0 <= e.get("t", 0) < t1 and e.get("level") in ("bad", "repair", "warn"): ev.append(e)
    except OSError: pass
    # own streams: peak + unique devices per day (kept since 9 Nov 2026)
    try: aud = json.load(open(AUD_DAYS)) if not chw else {}
    except Exception: aud = {}
    if chw:         # a theme channel: peak (5-minute points) + unique devices per day from its own audience file
        try:
            sc = audstats.db()
            for i in range(7):
                d0 = time.mktime((mon + D.timedelta(days=i)).timetuple()); d1 = d0 + 86400
                pk = sc.execute("SELECT MAX(rmax) FROM aud5 WHERE t >= ? AND t < ?", (d0, d1)).fetchone()[0]
                un = sc.execute("SELECT COUNT(DISTINCT dk) FROM sessions WHERE end >= ? AND start < ?", (d0, d1)).fetchone()[0]
                if pk or un: aud[(mon + D.timedelta(days=i)).isoformat()] = {"radio": {"peak": int(pk or 0), "unique": max(int(un or 0), int(pk or 0))}}
            sc.close()
        except Exception as e: log.warning("week report audience %s: %s", chw, e)
    adays = [[(mon + D.timedelta(days=i)).isoformat(), aud.get((mon + D.timedelta(days=i)).isoformat(), {})] for i in range(7)]
    return {"week": mon.isocalendar()[1], "from": mon.isoformat(), "to": (mon + D.timedelta(days=6)).isoformat(), "w": w,
            "totals": {"songs": len(music), "unique_songs": len(songs), "videos": vids, "jingles": jingles, "mixes": mixes, "items": len(rows)},
            "top_songs": [[title[p], n] for p, n in songs.most_common(10)], "top_artists": arts.most_common(10),
            "genres": genres.most_common(), "too_often": often,
            "never": {"count": len(never), "of": len(lib), "examples": sorted(never)[:40]},
            "rotation": rot, "events": ev[-60:], "audience": adays}

def streams_status(eng):
    if _ST["d"] and time.time() - _ST["t"] < 5: return _ST["d"]
    d = {"time": time.time()}
    try:
        ice = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/status-json.xsl", timeout=3).read())["icestats"].get("source") or []
        ice = ice if isinstance(ice, list) else [ice]
        m = next((x for x in ice if str(x.get("listenurl", "")).endswith("/radio.mp3")), None)
        d["radio"] = {"ok": bool(m), "listeners": int(m.get("listeners", 0)) if m else 0, "kbps": int(m.get("bitrate", 0) or 0) if m else 0}
    except Exception as e: d["radio"] = {"ok": False, "error": str(e)[:80]}
    d["radio"]["on_air"] = not eng.stopped and bool(eng.now and eng.now.get("path"))
    d["radio"]["song"] = ((eng.now or {}).get("artist") or "") + " – " + ((eng.now or {}).get("title") or "") if eng.now else ""
    try:
        seg = max((os.path.getmtime(os.path.join("/dev/shm/hls/v0", f)) for f in os.listdir("/dev/shm/hls/v0") if f.endswith(".ts")), default=0)
        age = round(time.time() - seg, 1) if seg else None
    except OSError: age = None
    obs_on = None
    try:
        with eng.tv.olock: obs_on = bool(eng.tv.client().get_stream_status().output_active)
    except Exception: pass
    d["tv"] = {"ok": bool(obs_on) and age is not None and age < 8, "obs": obs_on, "seg_age": age, "viewers": hls_viewers(), "scene": eng.tv.scene or (eng.now or {}).get("scene")}
    rec = None
    try: rec = eng.live.status().get("rec")
    except Exception: pass
    d["rec"] = rec
    if time.time() - _ST["pub_t"] > 20:      # the public radio (Streamerr / AzuraCast) – until our own streams take over
        _ST["pub_t"] = time.time()
        try:
            j = json.loads(urllib.request.urlopen("https://artemis.streamerr.co/api/nowplaying/hype_radio", timeout=5).read())
            _ST["pub"] = {"ok": True, "listeners": (j.get("listeners") or {}).get("current", 0), "unique": (j.get("listeners") or {}).get("unique", 0),
                          "song": ((j.get("now_playing") or {}).get("song") or {}).get("text", "")}
        except Exception as e: _ST["pub"] = {"ok": False, "error": str(e)[:80]}
    d["public"] = _ST["pub"]
    try:
        la = os.getloadavg()[0]; du = shutil_disk("/srv")
        d["server"] = {"load": round(la, 2), "cpus": os.cpu_count(), "disk_free_gb": du}
        if os.path.exists("/var/run/reboot-required"):      # a security update waits for the monthly reboot (done together, never by itself)
            try: pk = open("/var/run/reboot-required.pkgs").read().split()
            except OSError: pk = []
            d["server"]["reboot"] = {"since": int(os.path.getmtime("/var/run/reboot-required")), "for": sorted(set(pk))[:4]}
    except Exception: pass
    _ST.update(t=time.time(), d=d); return d
LISTEN = {"t": 0, "d": None}
LSTATS_FILE = SP("/var/lib/hype/listeners.json")
def hls_radio_ips(window=30):
    """Devices (address + player) that fetched radio HLS pieces in the last `window` s."""
    return log_devices("/var/log/nginx/stream.access.log", lambda ln: " /r" in ln and ".ts " in ln, window)
def listeners():
    """Radio listeners on Hype Studio's own streams as DEVICES (address + player; MP3 and HLS together, no double count):
    now, peak today, unique today (only daily codes are stored, no addresses)."""
    if LISTEN["d"] and time.time() - LISTEN["t"] < 4: return LISTEN["d"]
    with LISTEN_LOCK:
        if LISTEN["d"] and time.time() - LISTEN["t"] < 4: return LISTEN["d"]
        return _listeners()
def _listeners():
    mp3s = mp3_devices()
    if mp3s is None:                                   # Icecast's list not readable: fall back to its connection count
        mp3s = set()
        try:
            ice = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/status-json.xsl", timeout=3).read())["icestats"].get("source") or []
            ice = ice if isinstance(ice, list) else [ice]
            n = sum(int(x.get("listeners", 0)) for x in ice if str(x.get("listenurl", "")).endswith("/radio.mp3"))
            mp3s = {f"conn{i}|" for i in range(n)}
        except Exception: pass
    hls = hls_radio_ips(); devs = mp3s | hls; now_n = len(devs); LISTEN["mp3keys"] = set(mp3s)
    day = time.strftime("%Y-%m-%d")
    try: st = json.load(open(LSTATS_FILE))
    except Exception: st = {}
    if st.get("day") != day: aud_archive("radio", st); st = {"day": day, "peak": 0, "peak_t": 0, "uniq": []}
    if now_n > st["peak"]: st["peak"], st["peak_t"] = now_n, int(time.time())
    u = set(st.get("uniq", []))
    for k in devs:
        if not k.startswith("conn"): u.add(hashlib.sha1((day + k).encode()).hexdigest()[:12])     # no addresses stored, only a daily code
    st["uniq"] = sorted(u)
    try: jdump(st, LSTATS_FILE)
    except Exception: pass
    d = {"current": now_n, "mp3": len(mp3s), "hls": len(hls), "peak_today": st["peak"], "peak_time": st["peak_t"],
         "unique_today": max(len(u), st["peak"]), "counts": "devices"}
    LISTEN.update(t=time.time(), d=d); return d
CH_COV = {"t": 0}
def ch_covers():
    """The theme channels' songs (last played + next) get a cover lookup in the shared cover store, like Hype Radio's do
    (cover_url queues the ones it does not have yet; the channel's now-playing feed then shows them)."""
    try:
        st = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/status" % chancfg.API_PORT, timeout=3).read()).get("channels", [])
    except Exception: return
    for x in st:
        if not x.get("enabled"): continue
        songs = [(y.get("artist"), y.get("title")) for y in [x.get("now")] + (x.get("next") or []) + (x.get("history") or []) if y and (y.get("path") or "").startswith("music")]
        for a, t_ in songs:
            try: cover_url(a, t_)
            except Exception: pass
CHL = {}          # theme channel id -> its listeners, counted exactly like Hype Radio's (listeners() above)
def ch_listeners(cfg):
    """A theme channel's listeners as DEVICES – the same rules as Hype Radio: MP3 from Icecast's listener list matched to
    nginx's starts (address + player), HLS from its own pieces (/ch/<id>/c….ts), test tools / bots and our own readers left
    out, someone on MP3 and HLS together counted once; peak today and unique today (only daily codes are stored)."""
    cid = cfg["id"]; mp3s = mp3_devices("/" + cfg["mount"])
    if mp3s is None:
        mp3s = set()
        try:
            ice = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/status-json.xsl", timeout=3).read())["icestats"].get("source") or []
            ice = ice if isinstance(ice, list) else [ice]
            n = sum(int(x.get("listeners", 0)) for x in ice if str(x.get("listenurl", "")).endswith("/" + cfg["mount"]))
            mp3s = {f"conn{i}|" for i in range(n)}
        except Exception: pass
    hls = log_devices("/var/log/nginx/stream.access.log", lambda ln: (" /ch/%s/c" % cid) in ln and ".ts " in ln, 30)
    devs = mp3s | hls; now_n = len(devs); day = time.strftime("%Y-%m-%d"); f = "/var/lib/hype/listeners_%s.json" % cid
    try: st = json.load(open(f))
    except Exception: st = {}
    if st.get("day") != day: st = {"day": day, "peak": 0, "peak_t": 0, "uniq": []}
    if now_n > st["peak"]: st["peak"], st["peak_t"] = now_n, int(time.time())
    u = set(st.get("uniq", []))
    for k in devs:
        if not k.startswith("conn"): u.add(hashlib.sha1((day + k).encode()).hexdigest()[:12])
    st["uniq"] = sorted(u)
    try: jdump(st, f)
    except Exception: pass
    d = {"current": now_n, "mp3": len(mp3s), "hls": len(hls), "peak_today": st["peak"], "peak_time": st["peak_t"], "unique_today": max(len(u), st["peak"]),
         "counts": "devices", "t": int(time.time()), "mp3keys": sorted(mp3s)}
    CHL[cid] = d; return d
def nowplaying_api():
    """AzuraCast-style /api/nowplaying/hype_radio, so Hype Live (and others) only need a different link."""
    n = E.now or {}; t = time.time()
    def song(it, sid=""):
        it = it or {}; a, ti = it.get("artist") or "", it.get("title") or ""
        cv = cover_url(a, ti) if a and ti else None      # the cover (public link), else the Hype artwork
        art = ("https://stream.hyperadio.nl/cover/" + cv.split("/api/cover/", 1)[1].split("?")[0]) if cv else "https://stream.hyperadio.nl/no-cover.png"
        return {"id": sid or hashlib.md5((a + ti).encode()).hexdigest(), "text": (a + " - " + ti) if a else ti, "artist": a, "title": ti, "album": "", "genre": it.get("folder") or "", "art": art, "custom_fields": {}}
    # only SONGS for the outside world (website, Hype Live, apps): while a jingle / sweeper / ad plays, "now playing" stays
    # the last song, and jingles are left out of the history and "next"
    rows = []
    try: rows = dbc().execute("SELECT started, artist, title, folder, path FROM asrun WHERE path LIKE ? ORDER BY started DESC LIMIT 11", ("music%" if CH else "music/%",)).fetchall()
    except Exception: pass
    if n.get("section") != "music" and rows:
        r0 = rows[0]; n = {"artist": r0[1], "title": r0[2], "folder": r0[3], "section": "music", "started": r0[0], "path": r0[4]}
        try: dur = E.length(dict(row_item(dbc().execute(f"SELECT {COLS} FROM items WHERE path=?", (r0[4],)).fetchone(), "")))
        except Exception: dur = 0
    else: dur = E.length(n) if n.get("path") else 0
    el = max(0, t - (n.get("started") or t))
    nxt = next((x for x in ([E.liq_next] if E.liq_next else []) + list(E.plan) if x.get("section") == "music"), None)
    hist = [{"played_at": int(r[0]), "duration": 0, "playlist": r[3] or "", "is_request": False, "song": song({"artist": r[1], "title": r[2], "folder": r[3]})}
            for r in rows if int(r[0]) != int(n.get("started") or 0)][:10]
    return {"station": {"id": 1, "name": "Hype Radio TV", "shortcode": "hype_radio", "listen_url": "https://stream.hyperadio.nl/radio.mp3",
                        "hls_url": "https://stream.hyperadio.nl/radio.m3u8", "hls_enabled": True, "is_public": True,
                        "mounts": [{"name": "MP3 128", "url": "https://stream.hyperadio.nl/radio.mp3", "bitrate": 128, "format": "mp3", "is_default": True}]},
            # listener / viewer numbers are NOT public any more (Hype Live gets them in the heartbeat, write key) – song info only
            "live": {"is_live": (E.live.status().get("air") == "studio" and not E.live.target()) if hasattr(E, "live") else False, "streamer_name": "", "broadcast_start": None},
            "now_playing": {"sh_id": int(n.get("started") or 0), "played_at": int(n.get("started") or t), "duration": round(dur), "elapsed": round(el), "remaining": round(max(0, dur - el)),
                            "playlist": n.get("folder") or "", "streamer": "", "is_request": False, "song": song(n),
                            "type": "song" if n.get("section") == "music" else "jingle" if n.get("section") == "jingles" else (n.get("section") or "")},
            "playing_next": {"song": song(nxt)} if nxt else None, "song_history": hist, "is_online": not E.stopped,
            "hype": {"source": "hype-studio",
                     # Hype TV: which station's programme the TV shows right now (None = Hype Radio) and which station the studio is LIVE on
                     "tv_station": None if CH else TVF.get("ch"), "live_station": None if CH else (E.live.target() if E.live.live else None)}}

GUARD_CONF = "/var/lib/hype/guard_conf.json"
def rights_report(qs, as_csv):
    """Reports → Rights: every song that went on air between two dates (music only, no jingles / ads) as a playlist for the
    music rights organisations (BumaStemra / Sena), and a check of the usual webradio rule: max 4 songs by the same artist
    within 3 hours, max 3 of them in a row."""
    def day(k, d):
        try: return time.mktime(time.strptime(qs.get(k, [d])[0], "%Y-%m-%d"))
        except Exception: return None
    t1 = day("from", time.strftime("%Y-%m-01")); t2 = day("to", time.strftime("%Y-%m-%d"))
    t1 = t1 or time.time() - 30 * 86400; t2 = (t2 or time.time()) + 86400
    c = dbc()
    rows = c.execute("""SELECT a.started, a.artist, a.title, a.path, a.kind, i.duration, i.cue_start, i.cue_end FROM asrun a LEFT JOIN items i ON i.path=a.path
                        WHERE a.started>=? AND a.started<? ORDER BY a.started""", (t1, t2 + 600)).fetchall()
    out = []
    for n, r in enumerate(rows):
        if r[0] >= t2 or (r[4] or "music") != "music": continue
        nxt = rows[n + 1][0] if n + 1 < len(rows) else None
        full = max(0.0, (r[7] or r[5] or 0) - (r[6] or 0))
        heard = min(full, nxt - r[0]) if nxt and full else (full or (nxt - r[0] if nxt else 0))
        out.append({"t": r[0], "artist": r[1] or "", "title": r[2] or "", "secs": int(round(heard)), "file": os.path.basename(r[3] or "")})
    if as_csv:
        import csv, io; b = io.StringIO(); w = csv.writer(b, delimiter=";")
        w.writerow(["Datum", "Starttijd", "Speelduur (sec)", "Artiest", "Titel", "Zender", "Bestand"])
        for o in out: w.writerow([time.strftime("%d-%m-%Y", time.localtime(o["t"])), time.strftime("%H:%M:%S", time.localtime(o["t"])), o["secs"], o["artist"], o["title"], "Hype Radio TV", o["file"]])
        return (("\ufeff" + b.getvalue()).encode("utf-8"), "text/csv; charset=utf-8")
    probs = []
    for i, o in enumerate(out):
        keys = set(artist_key(o["artist"]))
        if not keys: continue
        win = [x for x in out[max(0, i - 60):i + 1] if o["t"] - x["t"] < 3 * 3600 and keys & set(artist_key(x["artist"]))]
        if len(win) == 5: probs.append({"t": o["t"], "what": "5th song in 3 hours with %s" % o["artist"], "song": o["title"]})
        run = 1
        for x in reversed(out[max(0, i - 4):i]):
            if keys & set(artist_key(x["artist"])): run += 1
            else: break
        if run == 4: probs.append({"t": o["t"], "what": "4 songs in a row with %s" % o["artist"], "song": o["title"]})
    return ({"songs": len(out), "hours": round(sum(o["secs"] for o in out) / 3600, 1), "problems": probs[:200], "n_problems": len(probs)},)
def hl_send_np(c, it):
    """Song info + sponsor proof of play to Hype Live (?hype_np=KEY, JSON). Only when switched on (Settings → 🛡 Alarm & Hype Live),
    because until go-live the public radio is still CLOSE."""
    if CH: return            # a theme station: Hype Live / the website read its own now-playing feed
    try: g = json.load(open(GUARD_CONF))
    except Exception: return
    if not hl_key(g) or not (g.get("np") or (g.get("np_spots") and it.get("section") == "ads")): return
    if not g.get("np") and it.get("section") != "ads": return
    sec = it.get("section"); typ = "song" if sec == "music" else "spot" if sec == "ads" else "jingle"
    body = {"artist": it.get("artist") or "", "title": it.get("title") or "", "type": typ, "length": round(E.length(it), 1), "start": round(time.time(), 1), "path": it.get("path")}
    if typ == "spot":
        r = c.execute("SELECT brand FROM ad_spots WHERE path=?", (it.get("path"),)).fetchone()
        body["brand"] = (r[0] if r and r[0] else (it.get("folder") or "").split("/")[-1]) or it.get("title")
    if typ == "song":
        body["next"] = [{"artist": p_.get("artist") or "", "title": p_.get("title") or "", "type": "song" if p_.get("section") == "music" else "jingle"} for p_ in (([E.liq_next] if E.liq_next else []) + E.plan)[:3]]
    url = g.get("hl_url", "https://www.hyperadio.nl/").rstrip("/") + "/?hype_np="; hk = hl_key(g)
    def go():
        try:
            r = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "HypeStudio/1", "X-Hype-Key": hk})
            a = urllib.request.urlopen(r, timeout=10).read(100).decode("utf-8", "replace")
            log.info("Hype Live np (%s %s): %s", typ, body.get("brand") or body["title"], a[:40])
        except Exception as e: log.warning("Hype Live np: %s", e)
    threading.Thread(target=go, daemon=True).start()

# ---------------- automatic recordings (Settings → Auto recordings) ----------------
AUTOREC_FILE = SP("/var/lib/hype/autorec.json")
def autorec_load():
    try: return json.load(open(AUTOREC_FILE))
    except Exception: return {"rules": []}
def autorec_active(r, t=None):
    """Is this rule's window on now? kind show = while that show is on air (website schedule);
    kind time = on its days between start and end (an end before the start = over midnight)."""
    if not r.get("enabled", True): return False
    t = t or time.time(); lt = time.localtime(t)
    if r.get("kind") == "show":
        s_ = planner.show_at(t); return bool(s_ and r.get("show") and s_["title"] == r["show"])
    try:
        a = [int(x) for x in (r.get("start") or "00:00").split(":")]; b = [int(x) for x in (r.get("end") or "00:00").split(":")]
    except ValueError: return False
    m, a, b = lt.tm_hour * 60 + lt.tm_min, a[0] * 60 + a[1], b[0] * 60 + b[1]
    days = r.get("days") or "0123456"; d = str(lt.tm_wday); dprev = str((lt.tm_wday - 1) % 7)
    if a < b: return d in days and a <= m < b
    return (d in days and m >= a) or (dprev in days and m < b)          # over midnight: belongs to the day it started
def autorec_loop():
    """Starts / stops the automatic recordings. A recording you started yourself (REC / GO LIVE) is never touched."""
    last_clean = 0; started = None      # the rule we started this window (stopped by hand with REC = stays off until its window ends)
    while True:
        time.sleep(20)
        try:
            L = getattr(E, "live", None)
            if not L: continue
            rules = autorec_load().get("rules", [])
            act = next((r for r in rules if autorec_active(r)), None)
            cur = L.rec
            if cur and str(cur.get("why", "")).startswith("auto:"):
                rid = cur["why"][5:]
                if not act or str(act.get("id")) != rid:
                    L.stop_rec(); log.info("auto recording stopped (%s)", rid); cur = None
            if not act: started = None
            if act and not L.rec and started != act.get("id"):
                started = act.get("id"); nm = act.get("name") or (act.get("show") if act.get("kind") == "show" else "") or "Recording"
                L.start_rec("auto:" + str(act.get("id")), name=nm, what=act.get("what") or "both")
                log.info("auto recording started: %s", nm)
            if time.time() - last_clean > 3600:          # clean up: an auto recording older than its rule's "keep" days
                last_clean = time.time()
                for r in rules:
                    keep = int(r.get("keep_days") or 0); nm = live.safe_name(r.get("name") or r.get("show") or "")
                    if keep <= 0 or not nm: continue
                    for f in os.listdir(live.REC_DIR) if os.path.isdir(live.REC_DIR) else []:
                        fp = os.path.join(live.REC_DIR, f)
                        if os.path.splitext(f)[0].endswith(" " + nm) and time.time() - os.path.getmtime(fp) > keep * 86400:
                            os.remove(fp); log.info("auto recording removed (older than %d days): %s", keep, f)
        except Exception as e: log.warning("auto recordings: %s", e)
def autorec_status():
    d = autorec_load()
    shows = sorted({s_["title"] for s_ in planner.WEEK if s_.get("title")})
    files = []
    try:
        for f in sorted(os.listdir(live.REC_DIR), reverse=True)[:60]:
            fp = os.path.join(live.REC_DIR, f); files.append({"file": f, "mb": round(os.path.getsize(fp) / 1e6, 1), "t": os.path.getmtime(fp)})
    except OSError: pass
    L = getattr(E, "live", None); rec = L.rec if L else None
    return {"rules": d.get("rules", []), "shows": shows, "files": files, "free_gb": shutil_disk("/srv"),
            "rec": {"why": rec["why"], "file": rec["file"], "start": rec["start"]} if rec else None}
def autorec_save(d):
    rules = []
    for i, r in enumerate(d.get("rules") or []):
        kind = "show" if r.get("kind") == "show" else "time"
        rules.append({"id": str(r.get("id") or uuid.uuid4().hex[:8]), "enabled": bool(r.get("enabled", True)), "name": str(r.get("name") or "")[:60],
                      "kind": kind, "show": str(r.get("show") or "")[:80], "days": "".join(ch for ch in str(r.get("days") or "0123456") if ch in "0123456"),
                      "start": str(r.get("start") or "20:00")[:5], "end": str(r.get("end") or "22:00")[:5],
                      "what": r.get("what") if r.get("what") in ("both", "audio", "video") else "both", "keep_days": max(0, min(365, int(r.get("keep_days") or 0)))})
    json.dump({"rules": rules}, open(AUTOREC_FILE + ".tmp", "w")); os.replace(AUTOREC_FILE + ".tmp", AUTOREC_FILE)
    return autorec_status()

# ---------------- 🎙 voice messages from Hype Live → the VOICE pad bank ----------------
VOICE_DIR = os.path.join(ROOT, JDIR, "VOICE"); VOICE_STATE = "/var/lib/hype/voice.json"; VOICE_DAYS = 14
VOICE_BANK = "🎙 VOICE"; VOICE_FAIL = {}
def voice_state():
    try: return json.load(open(VOICE_STATE))
    except Exception: return {}
def voice_save(st):
    if CH: return            # Hype Radio's engine keeps the voices list (from Hype Live); a channel only reads it
    json.dump(st, open(VOICE_STATE + ".tmp", "w")); os.replace(VOICE_STATE + ".tmp", VOICE_STATE)
def voice_when(v):
    try: return float(v)
    except Exception: pass
    try:
        import datetime; return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception: return time.time()
def voice_bank(c):
    r = c.execute("SELECT id FROM pad_banks WHERE name=?", (VOICE_BANK,)).fetchone()
    if r: return r[0]
    pos = (c.execute("SELECT MAX(pos) FROM pad_banks").fetchone()[0] or 0) + 1
    bid = c.execute("INSERT INTO pad_banks(name, pos) VALUES(?,?)", (VOICE_BANK, pos)).lastrowid; c.commit(); return bid
def voice_drop(c, st, vid):
    v = st.pop(vid, None)
    if not v: return
    rel = v.get("path")
    if rel:
        try: os.remove(os.path.join(ROOT, rel))
        except FileNotFoundError: pass
        c.execute("DELETE FROM pad_items WHERE path=?", (rel,)); c.execute("DELETE FROM items WHERE path=?", (rel,)); c.commit()
    log.info("voice: removed %s", vid)
VOICE_PLAYS = SP("/var/lib/hype/voice_plays.json"); VOICE_TAKEN = []   # [(planned time, path, sender)] picked in the last minutes (one planning pass)
def voice_plays():
    try: return json.load(open(VOICE_PLAYS))
    except Exception: return {}
def voice_played(path):
    """A listener voice went on air (as a filler or over an intro): counts for 'max plays' and the per-hour rules."""
    try:
        p = voice_plays(); p.setdefault(path, []).append(time.time())
        for k in [k for k in p if not os.path.exists(os.path.join(ROOT, k))]: p.pop(k, None)
        json.dump(p, open(VOICE_PLAYS + ".tmp", "w")); os.replace(VOICE_PLAYS + ".tmp", VOICE_PLAYS)
    except Exception as e: log.warning("voice plays: %s", e)
def voice_by_path(path):
    for v in voice_state().values():
        if v.get("path") == path: return v
    return {}
def voice_who(v): return ((v.get("name") or "") + "|" + (v.get("city") or "")).lower()
def voice_box(path, delay, ln):
    """The voice message box on TV (first name + WhatsApp / website) while a listener voice plays."""
    v = voice_by_path(path)
    name = (v.get("name") or re.sub(r"^Voice - |\s*\(.*$|\s+\S+$", "", os.path.splitext(os.path.basename(path))[0]) or "Listener").split()[0]
    VOICE_ON.update(name=name, source=v.get("source") or "", start=time.time() + delay, end=time.time() + delay + ln)
def voice_overview():
    """For Settings: how many voices are on air / waiting / done."""
    st = voice_state(); pl = voice_plays(); n_air = n_done = 0
    for v in st.values():
        if not v.get("air"): continue
        if len(pl.get(v.get("path"), [])) >= VOICE_AIR["max_plays"]: n_done += 1
        else: n_air += 1
    return {"ok_for_air": n_air, "done": n_done, "total": len(st)}
def voice_pick(c, at, max_len=None):
    """A listener voice for an hour-plan slot (or over an intro, max_len = the intro): only ✓ OK for air in Hype Live,
    not expired, fewer than 'max plays'; at most N per hour, never the same sender twice within an hour, never two
    planned at once. Fewest plays first, then the newest message. None = nothing fits (the slot is skipped)."""
    if VOICE_AIR["per_hour"] <= 0: return None
    preview = bool(planner.CTX.get("preview")); now = time.time()
    VOICE_TAKEN[:] = [x for x in VOICE_TAKEN if now - x[3] < 600]
    st = voice_state(); pl = voice_plays()
    events = [(t, voice_who(v)) for v in st.values() for t in pl.get(v.get("path"), [])]
    planned = {u.get("path") for u in (getattr(E, "plan", None) or []) if (u.get("path") or "").startswith(VOICE_PFX) or (u.get("id_over") or "").startswith(VOICE_PFX)}
    planned |= {u.get("id_over") for u in (getattr(E, "plan", None) or []) if u.get("id_over")}
    events += [(at, voice_who(voice_by_path(p))) for p in planned if p and p.startswith(VOICE_PFX)]
    if not preview: events += [(x[0], x[2]) for x in VOICE_TAKEN]
    near = [w for t, w in events if abs(t - at) < 3600]
    if len(near) >= VOICE_AIR["per_hour"]: return None
    best = None
    for v in st.values():
        p = v.get("path")
        if not v.get("air") or not p or p in planned or any(x[1] == p for x in VOICE_TAKEN) or (v.get("station") or None) != CH: continue
        if now - v.get("received", now) > VOICE_DAYS * 86400: continue
        n = len(pl.get(p, []))
        if n >= VOICE_AIR["max_plays"] or voice_who(v) in near: continue
        r = c.execute(f"SELECT {COLS} FROM items WHERE path=? AND problem IS NULL", (p,)).fetchone()
        if not r: continue
        if max_len is not None and duck_len(row_item(r, "intro")) + ID_OFFSET + 1.0 > max_len: continue
        k = (n, -v.get("received", 0))
        if best is None or k < best[0]: best = (k, p, voice_who(v))
    if not best: return None
    if not preview: VOICE_TAKEN.append((at, best[1], best[2], now))
    return best[1]
planner.VOICE_PICK = voice_pick

# ---------------- Hype Live: sponsor deals, covers, weekly recap ----------------
HL_SPONSORS = SP("/var/lib/hype/hl_sponsors.json")
def hl_key(g, kind="write"):
    """Hype Live 2.40: read key (r…) for lists / files / sponsors / covers, write key (w…) for heartbeat, song link, acks,
    charts and the recap; the write key also reads. The old single key keeps working until it is switched off in Hype Live."""
    if kind == "read": return g.get("hl_key_read") or g.get("hl_key_write") or g.get("hl_key") or ""
    return g.get("hl_key_write") or g.get("hl_key") or ""
HL_IP = {"t": 0, "ip": None}
def hl_ip_log(url):
    """Log the address the website name resolves to, once and whenever it changes (website moved to Hetzner, 17 Nov)."""
    if time.time() - HL_IP["t"] < 300: return
    HL_IP["t"] = time.time()
    try:
        h = urllib.parse.urlsplit(url).hostname; ips = sorted({a[4][0] for a in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)})
        if ips != HL_IP["ip"]: log.info("Hype Live website %s -> %s", h, ", ".join(ips)); HL_IP["ip"] = ips
    except Exception: pass
def hl_call(param, extra="", data=None, timeout=20):
    """GET (or POST data) https://hyperadio.nl/?<param>=KEY<extra>: the write key for sending / acks, else the read key."""
    g = json.load(open(GUARD_CONF)); k = hl_key(g, "write" if (data is not None or "&ack=" in extra) else "read")
    if not k: raise RuntimeError("no Hype Live key")
    hl_ip_log(g.get("hl_url", "https://www.hyperadio.nl/"))
    url = g.get("hl_url", "https://www.hyperadio.nl/").rstrip("/") + "/?" + param + "=" + extra      # the key goes in X-Hype-Key (Hype Live 2.42), never in a URL
    r = urllib.request.Request(url, data=data, headers={"User-Agent": "HypeStudio/1", "X-Hype-Key": k, **({"Content-Type": "application/json"} if data is not None else {})},
                               method="POST" if data is not None else "GET")
    return urllib.request.urlopen(r, timeout=timeout).read(2_000_000).decode("utf-8", "replace")
# ---------------- listener photos & videos (approved in Hype Live) -> MEDIA pads ----------------
MEDIA_IN = SP("/var/lib/hype/media_in.json"); MEDIA_LINK = {"t": 0, "ok": None, "error": "", "n": 0}; MEDIA_FAIL = {}
def media_in_state():
    try: return json.load(open(MEDIA_IN))
    except Exception: return {}
def media_in_drop(st, mid):
    v = st.pop(mid, None)
    if not v: return
    f = v.get("file")
    if f:
        try:
            if E.media.cur and E.media.cur.get("file") == f: E.media.stop(E)
        except Exception: pass
        try: os.remove(os.path.join(MEDIA_DIR, f))
        except FileNotFoundError: pass
    log.info("listener media: removed %s", mid)
def media_apply(d, ack):
    """Apply a photo / video list from Hype Live (old poll or the 2.41 sync answer): remove, make new ones into ready
    16:9 media pads (blurred fill for portrait shots + the name card), then ack them. Nothing plays by itself."""
    import listener_media as LM
    st = media_in_state(); changed = False
    for mid in d.get("remove") or []:
        if str(mid) in st: media_in_drop(st, str(mid)); changed = True
    for it in d.get("items") or []:
        mid = re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("id") or ""))
        if not mid or time.time() - MEDIA_FAIL.get(mid, 0) < 3600: continue
        if mid not in st:
            kind = "video" if it.get("kind") == "video" else "photo"
            name = re.sub(r"[^\w .'-]", "", str(it.get("name") or "Listener"), flags=re.U).strip()[:30] or "Listener"
            city = re.sub(r"[^\w .'-]", "", str(it.get("city") or ""), flags=re.U).strip()[:30]
            fn = f"Listener - {name}{' (' + city + ')' if city else ''} {mid[-6:]}." + ("mp4" if kind == "video" else "jpg")
            src = os.path.join("/tmp", "lm_" + mid)
            try:
                with hl_open(it.get("url") or "", 120) as r, open(src, "wb") as f: f.write(r.read(60_000_000))
                if kind == "video": LM.make_video(src, os.path.join(MEDIA_DIR, fn), name, city, it.get("caption") or "", it.get("source") or "")
                else: LM.make_photo(src, os.path.join(MEDIA_DIR, fn), name, city, it.get("caption") or "", it.get("source") or "")
            except Exception as e:
                MEDIA_FAIL[mid] = time.time(); log.warning("listener media: %s failed (%s) – again in 1 h", mid, e); continue
            finally:
                try: os.remove(src)
                except FileNotFoundError: pass
            st[mid] = {"file": fn, "kind": kind, "name": name, "city": city, "source": it.get("source") or "", "caption": (it.get("caption") or "")[:110],
                       "received": voice_when(it.get("received")), "added": time.time()}
            changed = True; log.info("listener media: %s from %s ready as a media pad", kind, name)
        a = ack(mid)
        st[mid]["acked"] = True; changed = True; log.info("listener media: ack %s -> %s", mid, a[:30])
    MEDIA_LINK["n"] = len(st)
    if changed: jdump(st, MEDIA_IN)

def media_in_loop():
    """Old way (Hype Live before 2.41, or when the sync answer stops): poll every 30 s."""
    while True:
        time.sleep(30)
        if hl_sync_active(): continue
        try:
            try: d = json.loads(hl_call("hype_studio_media", timeout=20))
            except urllib.error.HTTPError as e:
                MEDIA_LINK.update(t=time.time(), ok=False, error="Hype Live answered %s" % e.code); continue
            MEDIA_LINK.update(t=time.time(), ok=True, error="")
            def ack(mid):
                try: return hl_call("hype_studio_media", "&ack=" + urllib.parse.quote(mid), data=b"").strip()
                except urllib.error.HTTPError as e: return str(e.code)
            media_apply(d, ack)
        except Exception as e: MEDIA_LINK.update(t=time.time(), ok=False, error=str(e)[:80]); log.warning("listener media: %s", e)

THUMBS = os.path.join(MEDIA_DIR, ".thumbs")
def media_thumb(f):
    """A small picture (320 px) of a media file for its pad: branding/media/.thumbs/<file>.jpg (made once)."""
    src = os.path.join(MEDIA_DIR, f); dst = os.path.join(THUMBS, f + ".jpg")
    try:
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src): return dst
        os.makedirs(THUMBS, exist_ok=True)
        video = os.path.splitext(f)[1].lower() in (".mp4", ".mov", ".webm", ".mkv", ".m4v")
        subprocess.run(["ffmpeg", "-v", "error", "-y"] + (["-ss", "1"] if video else []) + ["-i", src, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", dst],
                       capture_output=True, timeout=30)
        return dst if os.path.exists(dst) else None
    except Exception: return None
def listener_pads(qch=""):
    """The LISTENERS pads: listener voices, photos and videos, newest first. A pad leaves after N days (Settings) unless ★ pinned;
    × takes it off at once. The files themselves stay (jingles/VOICE, branding/media)."""
    c = dbc(); keep = float(planner.get_setting(c, "listener_keep_days", 7)) * 86400; now = time.time(); out = []
    plays = voice_plays()
    for v in voice_state().values():
        if not v.get("path") or not v.get("acked") or v.get("hidden"): continue
        if (v.get("station") or "") != (qch or ""): continue           # Hype Radio's listener pads vs a theme channel's
        when = v.get("received") or 0
        if not v.get("pinned") and now - when > keep: continue
        r = c.execute("SELECT duration, problem FROM items WHERE path=?", (v["path"],)).fetchone()
        if not r or r[1]: continue
        pl = plays.get(v["path"]) or []; last = max(pl + [v.get("played") or 0]) or None
        out.append({"kind": "voice", "id": v["path"], "name": v.get("name") or "Listener", "city": v.get("city") or "", "source": v.get("source") or "",
                    "received": when, "duration": r[0], "played": last, "plays": len(pl) + (1 if v.get("played") and not pl else 0),
                    "pinned": bool(v.get("pinned")), "air": bool(v.get("air")), "url": "/media/" + urllib.parse.quote(v["path"])})
    files = {m["file"]: m for m in media_files()}
    for v in media_in_state().values():
        f = v.get("file")
        if not f or f not in files or v.get("hidden"): continue
        when = v.get("received") or v.get("added") or 0
        if not v.get("pinned") and now - when > keep: continue
        th = media_thumb(f)
        out.append({"kind": v.get("kind") or "photo", "id": f, "name": v.get("name") or "Listener", "city": v.get("city") or "", "source": v.get("source") or "",
                    "caption": v.get("caption") or "", "received": when, "duration": files[f].get("duration") or 0, "played": v.get("played"), "plays": v.get("plays", 0),
                    "pinned": bool(v.get("pinned")), "url": "/media/branding/media/" + urllib.parse.quote(f),
                    "thumb": ("/media/branding/media/.thumbs/" + urllib.parse.quote(f) + ".jpg?v=" + str(int(os.path.getmtime(th)))) if th else None})
    out.sort(key=lambda x: -(x["received"] or 0))
    return {"items": out, "keep_days": keep / 86400}

WEB_MAX = 400_000_000
def fetch_url(c, d):
    """A picture / video / sound dragged from a website onto the pads. The server downloads the file (public http(s) only,
    max 400 MB): pictures + videos -> a 16:9 media pad (blurred fill, no name card) in branding/media; sounds ->
    jingles/WEB, added to the pad bank you dropped it on."""
    import ipaddress, socket
    url = str(d.get("url") or "").strip(); target = d.get("target") or "media"
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname: return {"ok": False, "error": "Not a web link"}
    try:
        for ai in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(ai[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast: return {"ok": False, "error": "That address is not allowed"}
    except socket.gaierror: return {"ok": False, "error": "That website can't be found"}
    if target == "media" and is_youtube(url):        # a YouTube video -> downloaded once (≤ 1080p) -> a normal media pad (only your own / with permission)
        def yt():
            import listener_media as LM
            tmpd = "/tmp/yt_" + uuid.uuid4().hex; os.makedirs(tmpd)
            try:
                r_ = subprocess.run(["yt-dlp", "--no-playlist", "--no-warnings", "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]",
                                     "--merge-output-format", "mp4", "--max-filesize", "400M", "-o", os.path.join(tmpd, "%(title).60B.%(ext)s"), url], capture_output=True, text=True, timeout=1800)
                got = [f_ for f_ in os.listdir(tmpd) if f_.endswith(".mp4")]
                if r_.returncode != 0 or not got: raise RuntimeError(((r_.stderr or "no file").strip().splitlines() or ["no file"])[-1][:160])
                base_ = re.sub(r"[^\w .()&'-]", "", os.path.splitext(got[0])[0], flags=re.U).strip()[:60] or "YouTube video"
                LM.make_video(os.path.join(tmpd, got[0]), os.path.join(MEDIA_DIR, f"YouTube - {base_}.mp4"), "", "", "", "", max_s=3600)
                log.info("YouTube video %s -> media pad YouTube - %s.mp4", url[:80], base_)
            except Exception as e:
                log.warning("YouTube video %s: %s", url[:80], e); STREAM_NOTE.update(t=time.time(), text="YouTube video not added: %s" % str(e)[:140])
            finally: shutil.rmtree(tmpd, ignore_errors=True)
        threading.Thread(target=yt, daemon=True).start()
        return {"ok": True, "kind": "video", "later": True, "file": "YouTube video"}
    if re.search(r"(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|facebook\.com|fb\.watch)", u.hostname or ""):
        return {"ok": False, "error": "Instagram / TikTok / Facebook pages can't be dropped – only direct picture / video / sound files (YouTube videos: on the MEDIA pads)"}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (HypeStudio)"})
    try: r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e: return {"ok": False, "error": "The website refused it (%s). Open the picture / video on its own and drag that, or save it and drop the file" % e.code}
    except Exception as e: return {"ok": False, "error": "Could not fetch it: %s" % str(e)[:80]}
    ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = os.path.splitext(u.path)[1].lower()
    kind = ("picture" if ct.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else
            "video" if ct.startswith("video/") or ext in (".mp4", ".mov", ".webm", ".m4v", ".mkv") else
            "audio" if ct.startswith("audio/") or ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac") else None)
    if not kind: return {"ok": False, "error": "That link is not a picture, video or sound file (it is %s)" % (ct or "a web page")}
    if target == "media" and kind == "audio": return {"ok": False, "error": "A sound goes on a sound bank, not on MEDIA"}
    if target == "bank" and kind != "audio": return {"ok": False, "error": "Pictures and videos go on the MEDIA pads"}
    base = re.sub(r"[^\w .()-]", "", urllib.parse.unquote(os.path.splitext(os.path.basename(u.path))[0]), flags=re.U).strip()[:50] or ("web " + time.strftime("%Y%m%d-%H%M%S"))
    tmp = os.path.join("/tmp", "web_" + uuid.uuid4().hex)
    n = 0
    with open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b: break
            n += len(b)
            if n > WEB_MAX: f.close(); os.remove(tmp); return {"ok": False, "error": "The file is bigger than 400 MB"}
            f.write(b)
    import listener_media as LM
    if kind == "picture":
        fn = f"Web - {base}.jpg"
        try: LM.make_photo(tmp, os.path.join(MEDIA_DIR, fn), "", "", "", "")
        finally: os.remove(tmp)
        log.info("web drop: picture %s -> %s", url[:80], fn); return {"ok": True, "kind": kind, "file": fn}
    if kind == "video":
        fn = f"Web - {base}.mp4"
        def work():
            try: LM.make_video(tmp, os.path.join(MEDIA_DIR, fn), "", "", "", "", max_s=900); log.info("web drop: video %s -> %s", url[:80], fn)
            except Exception as e: log.warning("web drop video: %s", e)
            finally:
                try: os.remove(tmp)
                except FileNotFoundError: pass
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "kind": kind, "file": fn, "later": True}
    # a sound: into jingles/WEB, onto the bank (it plays once the library has indexed it, ~1 min)
    os.makedirs(os.path.join(ROOT, "jingles", "WEB"), exist_ok=True)
    fn = f"{base}{ext if ext in ('.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac') else '.mp3'}"; rel = os.path.join("jingles", "WEB", fn)
    shutil.move(tmp, os.path.join(ROOT, rel)); os.chmod(os.path.join(ROOT, rel), 0o664)
    bid = d.get("bank_id")
    if bid:
        pos = (c.execute("SELECT MAX(pos) FROM pad_items WHERE bank_id=?", (bid,)).fetchone()[0] or 0) + 1
        c.execute("INSERT INTO pad_items VALUES(?,?,?,?,?)", (int(bid), pos, rel, base, None)); c.commit()
    log.info("web drop: sound %s -> %s", url[:80], rel); return {"ok": True, "kind": kind, "path": rel}

def hl_open(url, timeout=60):
    """Open a file link Hype Live gave (only on hyperadio.nl)."""
    if not re.match(r"^https://(www\.)?hyperadio\.nl/", url or ""): raise ValueError("not a Hype Live link")
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "HypeStudio/1"}), timeout=timeout)

# ---------------- Hype Top 10 -> Hype Live (for the website chart) ----------------
TOP10_SENT = SP("/var/lib/hype/top10_sent.json")
def chart_show_at(name):
    """The next start of the show this chart belongs to ('Hype Top 10' -> a show with 'Top 10' in its name), or 0."""
    m = re.search(r"top\s*\d+", name or "", re.I); key = re.sub(r"\s+", "", (m.group(0) if m else name or "")).lower(); now = time.time()
    if not key: return 0
    for k in range(0, 8 * 1440):
        t = (int(now) // 60 + k) * 60; s_ = planner.show_at(t)
        if s_ and key in re.sub(r"\s+", "", (s_.get("title") or "").lower()) and (planner.show_at(t - 60) or {}).get("title") != s_.get("title"): return t
    return 0
def chart_data(pid, now=False):
    """A chart as it is now in its playlist: numbers count down (first song = highest), for the website (via Hype Live).
    Week: "Now" = the week it is published in; "At the show" = the week of that show (13 Nov: "Now" on a Friday gave
    next week's number, because the next Top 10 show is next week)."""
    c = dbc(); r = c.execute("SELECT id, name FROM playlists WHERE id=? AND COALESCE(chart,0)=1", (pid,)).fetchone()
    if not r: return None
    items = []
    for p, n in planner.chart_positions(c, pid):
        it = c.execute("SELECT artist, title FROM items WHERE path=?", (p,)).fetchone()
        if it: items.append({"pos": n, "artist": it[0] or "", "title": it[1] or ""})
    items.sort(key=lambda x: x["pos"]); show_at = 0 if now else chart_show_at(r[1])
    return {"chart": {"id": pid, "name": r[1], "size": len(items)}, "week": planner.chart_week(show_at or None), "show_at": show_at,
            "publish": "now" if now else "show", "now": bool(now),     # Hype Live 2.49.4: publish "now" = live at once, whatever show_at says
            "playlist": r[1], "items": items, "sent_at": int(time.time())}
def web_charts():
    return dbc().execute("SELECT id, name FROM playlists WHERE COALESCE(chart,0)=1 AND COALESCE(chart_web,1)=1").fetchall()
def is_top10(name): return bool(re.search(r"top\s*10\b", name or "", re.I))
def top10_data():
    for pid, name in web_charts():
        if is_top10(name): return chart_data(pid)
    return None
WEB_WEEKS = {"t": 0, "d": None}
def web_weeks():
    """What the website really shows: /wp-json/hype/v1/top10/weeks (current = live week, waiting = {week, show_at}). Cached 60 s."""
    if time.time() - WEB_WEEKS["t"] > 60:
        WEB_WEEKS["t"] = time.time()
        try: WEB_WEEKS["d"] = json.loads(urllib.request.urlopen(urllib.request.Request("https://www.hyperadio.nl/wp-json/hype/v1/top10/weeks?nc=%d" % time.time(), headers={"User-Agent": "HypeStudio/1"}), timeout=8).read(200_000))
        except Exception: WEB_WEEKS["d"] = None
    return WEB_WEEKS["d"]
def chart_state(pid):
    """Website state of a chart: when it was last published, which week, and whether the list changed since."""
    try: last = json.load(open(TOP10_SENT)).get(str(pid)) or {}
    except Exception: last = {}
    d = chart_data(pid); sig = json.dumps(d["items"], sort_keys=True) if d else None
    return {"t": last.get("t", 0), "week": last.get("week", ""), "now": bool(last.get("now")), "changed": bool(sig and sig != last.get("sig")),
            "show_at": last.get("show_at") or 0, "next_show": chart_show_at(d["chart"]["name"]) if d else 0,
            **(lambda w: {"site_current": w.get("current"), "site_waiting": w.get("waiting")} if (w and d and is_top10(d["chart"]["name"])) else {})(web_weeks())}
def chart_send(pid, force=False, now=None):
    """One chart to Hype Live: the Hype Top 10 to ?hype_studio_top10 (as since 2.37), other charts to ?hype_studio_chart.
    Publish: now = live on the website at once; else from the start of its show. now=None (the automatic resend after a
    list change) keeps the last choice for the same week (18 Nov: a resend after "Now" sent the show time again)."""
    try: last = json.load(open(TOP10_SENT))
    except Exception: last = {}
    if "sig" in last: last = {}                        # old format (one chart)
    if now is None:
        prev = last.get(str(pid)) or {}
        now = bool(prev.get("now")) and prev.get("week") == planner.chart_week()
    d = chart_data(pid, now)
    if not d or not d["items"]: return None, "no songs"
    sig = json.dumps(d["items"], sort_keys=True)          # the list only: "changed" = the songs or their order changed
    if not force and last.get(str(pid), {}).get("sig") == sig: return d["week"], "unchanged"
    a = hl_call("hype_studio_top10" if is_top10(d["chart"]["name"]) else "hype_studio_chart", data=json.dumps(d).encode(), timeout=30).strip()
    if a.startswith("OK"): last[str(pid)] = {"sig": sig, "t": time.time(), "week": d["week"], "now": bool(now), "show_at": d["show_at"]}; jdump(last, TOP10_SENT)
    log.info("chart %s %s -> Hype Live: %s · sent %s", d["chart"]["name"], d["week"], a[:40],
             json.dumps({k: v for k, v in d.items() if k != "items"} | {"items": "%d songs, #1 %s" % (len(d["items"]), (d["items"][0]["title"] if d["items"] else ""))}, ensure_ascii=False))
    return d["week"], a
def top10_send(force=False):
    for pid, name in web_charts():
        if is_top10(name): return chart_send(pid, force)
    return None, "no Top 10 chart"
def top10_loop():
    """Every 10 min: a chart whose list changed (and stayed the same for 10 min, so half-done edits are never sent)
    goes to Hype Live. Hype Live puts it on the website from show_at (no spoilers before the show)."""
    seen = {}
    while True:
        time.sleep(600)
        for pid, name in web_charts():
            try:
                d = chart_data(pid); sig = json.dumps(d["items"], sort_keys=True) if d else None
                if sig and sig == seen.get(pid): chart_send(pid)
                seen[pid] = sig
            except urllib.error.HTTPError as e:
                if e.code != 404: log.warning("chart %s -> Hype Live: %s", name, e)
            except Exception as e: log.warning("chart %s -> Hype Live: %s", name, e)

RECAP_SENT = SP("/var/lib/hype/recap_sent.json")
def recap_data(w=1):
    """Last week's playout summary for Hype Live's weekly recap (top songs / artists / new music / genres, live hours, peaks)."""
    import datetime as D, collections as C
    r = week_report({"w": [str(w)]})
    mon = D.date.fromisoformat(r["from"]); t0 = time.mktime(mon.timetuple()); t1 = t0 + 7 * 86400
    c = dbc(); rows = c.execute("SELECT path, artist, title FROM asrun WHERE started>=? AND started<? AND path LIKE 'music/%'", (t0, t1)).fetchall()
    rows = [x for x in rows if "/MIXES/" not in x[0] and "/RECORDINGS/" not in x[0]]
    cnt = C.Counter(x[0] for x in rows); name = {x[0]: (x[1] or "", x[2] or "") for x in rows}
    top = [{"artist": name[p][0], "title": name[p][1], "plays": n} for p, n in cnt.most_common(10)]
    new = [{"artist": name[p][0], "title": name[p][1], "plays": n} for p, n in cnt.most_common() if p.split("/")[1:2] == ["NEW"]][:10]
    live_s, shows, on_at = 0.0, [], None          # live hours from the GO LIVE log
    try:
        for ln in open("/var/lib/hype/live_log.jsonl"):
            e = json.loads(ln)
            if e.get("on"):
                on_at = e["t"]
                if t0 <= e["t"] < t1 and e.get("show") and e["show"] not in shows: shows.append(e["show"])
            elif on_at:
                a, b = max(on_at, t0), min(e["t"], t1)
                if b > a: live_s += b - a
                on_at = None
    except OSError: pass
    au = r.get("audience") or []
    rp = max([(d.get("radio") or {}).get("peak", 0) for _, d in au] or [0]); tp = max([(d.get("tv") or {}).get("peak", 0) for _, d in au] or [0])
    ru = [(d.get("radio") or {}).get("unique", 0) for _, d in au if d.get("radio")]; tu = [(d.get("tv") or {}).get("unique", 0) for _, d in au if d.get("tv")]
    y, wk, _ = mon.isocalendar()
    return {"week": "%d-W%02d" % (y, wk), "from": r["from"], "to": r["to"],
            "songs_played": r["totals"]["songs"], "different_songs": r["totals"]["unique_songs"],
            "top_songs": top, "top_artists": [{"artist": a, "plays": n} for a, n in r["top_artists"][:10]], "new_music": new,
            "genres": [{"genre": g, "plays": n} for g, n in r["genres"][:10]],
            "live_hours": round(live_s / 3600, 1), "shows_live": shows[:10],
            # devices are only counted per day (a daily code, no addresses): unique_week = the average day
            "radio": {"peak": rp, "unique_week": round(sum(ru) / len(ru)) if ru else 0},
            "tv": {"peak": tp, "unique_week": round(sum(tu) / len(tu)) if tu else 0}}
def recap_send(w=1):
    d = recap_data(w); a = hl_call("hype_studio_recap", data=json.dumps(d).encode(), timeout=30).strip()
    log.info("weekly recap %s -> Hype Live: %s", d["week"], a[:40]); return d["week"], a
def recap_loop():
    """Every Monday from 05:30: last week's playout summary to Hype Live (once; tried again every 10 min if it fails)."""
    while True:
        try:
            lt = time.localtime()
            if lt.tm_wday == 0 and (lt.tm_hour, lt.tm_min) >= (5, 30):
                try: sent = json.load(open(RECAP_SENT))
                except Exception: sent = {}
                import datetime as D
                y, wk, _ = (D.date.today() - D.timedelta(days=7)).isocalendar(); key = "%d-W%02d" % (y, wk)
                if sent.get("week") != key:
                    week, a = recap_send(1)
                    if a.startswith("OK"): jdump({"week": week, "t": time.time()}, RECAP_SENT)
        except Exception as e: log.warning("weekly recap: %s", e)
        time.sleep(600)
def sponsors_load():
    try: planner.HL_DEALS = json.load(open(HL_SPONSORS)).get("sponsors") or []
    except Exception: planner.HL_DEALS = []
def sponsors_loop():
    """Every 10 min: the sponsor deals from Hype Live (spots per day, dates, days, hours, paused). The last good list is
    kept (also over a restart) when Hype Live can't be reached, so the ads never stop because the website is slow."""
    sponsors_load()
    while True:
        if hl_sync_active(): time.sleep(600); continue          # the minute sync brings sponsor changes
        try:
            d = json.loads(hl_call("hype_studio_sponsors"))
            if isinstance(d.get("sponsors"), list):
                d["t"] = time.time(); jdump(d, HL_SPONSORS)
                old = {x.get("id"): x.get("updated") for x in planner.HL_DEALS}
                planner.HL_DEALS = d["sponsors"]
                if old != {x.get("id"): x.get("updated") for x in planner.HL_DEALS}: log.info("Hype Live sponsor deals: %d", len(planner.HL_DEALS))
        except Exception as e: log.warning("Hype Live sponsor deals: %s", e)
        time.sleep(600)

# ---------------- Hype Live 2.41: one sync call a minute (in the guard's heartbeat) + ⚡ poke ----------------
HL_SYNC, HL_KNOWN, HL_ACKS = SP("/var/lib/hype/hl_sync.json"), SP("/var/lib/hype/hl_known.json"), SP("/var/lib/hype/hl_acks.json")
HL_POKE, HL_SCHED, HL_WANT = SP("/var/lib/hype/hl_poke"), SP("/var/lib/hype/hl_schedule_version"), SP("/var/lib/hype/hl_want_covers.json")
COVER_FAIL = {}       # cover key -> (failed downloads in a row, not before) - 24 Nov: one refused cover was tried every minute
def sync_covers(cv):
    """Covers from the sync answer ('artist|title' -> {url, fixed, none, pending}): saved like the old cover calls."""
    for key, v in (cv or {}).items():
        if not isinstance(v, dict) or v.get("pending") or "|" not in key: continue
        artist, title = key.split("|", 1); k = cover_key(artist, title); f = os.path.join(COVERS, k + ".jpg")
        n_, nxt_ = COVER_FAIL.get(k, (0, 0))
        if v.get("url") and time.time() < nxt_: continue         # failed before: wait (5 min, 10, 20 … up to 12 h) instead of every minute
        try:
            if v.get("url"):
                img = urllib.request.urlopen(urllib.request.Request(v["url"], headers={"User-Agent": "HypeStudio/1"}), timeout=10).read(5_000_000)
                with open(f + ".tmp", "wb") as fh: fh.write(img)
                os.replace(f + ".tmp", f); COVER_MISS.pop(k, None)
            else:                                    # none found, or 🚫 Wrong cover: the Hype artwork
                if os.path.exists(f): os.remove(f)
                COVER_MISS[k] = time.time()
        except Exception as e:
            COVER_FAIL[k] = (n_ + 1, time.time() + min(43200, 300 * 2 ** n_))
            if n_ < 2: log.info("cover %s: %s (try %d; next try in %d min)", key, e, n_ + 1, min(43200, 300 * 2 ** n_) // 60)
            COVER_MISS[k] = time.time() - 86400 + 300; continue
        COVER_FAIL.pop(k, None)
        with COVER_LOCK: COVER_Q[:] = [x for x in COVER_Q if x[0] != k]
    try: jdump(COVER_MISS, COVER_IDX)
    except Exception: pass
HL_SYNC_SEEN = {"t": 0, "n": 0}
def hl_sync_active():
    """True while the guard's heartbeat gets the sync answer (Hype Live 2.41+): the old polls then stay quiet."""
    try: return time.time() - os.path.getmtime(HL_SYNC) < 180
    except OSError: return False
def hl_ack_queue(kind, mid):
    """A pad is ready: the next sync call carries its ack (the engine is the only writer of this file)."""
    try: a = json.load(open(HL_ACKS))
    except Exception: a = {}
    if mid not in a.setdefault(kind, []): a[kind].append(mid)
    jdump(a, HL_ACKS); return "queued for the next sync"
def hl_sync_loop():
    """Handles each new sync answer the guard stored: acks confirmed, voice / media / sponsor changes applied (only the
    parts that changed come along), then 'known' moves on – only for parts that were handled completely, so a failed
    download comes again with the next sync."""
    last = 0
    while True:
        time.sleep(2)
        try: mt = os.path.getmtime(HL_SYNC)
        except OSError: continue
        if mt == last: continue
        last = mt
        try:
            d = json.load(open(HL_SYNC)); HL_SYNC_SEEN.update(t=time.time(), n=HL_SYNC_SEEN["n"] + 1)
            try: known = json.load(open(HL_KNOWN))
            except Exception: known = {}
            try: acks = json.load(open(HL_ACKS))
            except Exception: acks = {}
            for k in ("voice", "media"):
                done = set(((d.get("acked") or {}).get(k) or {}).keys())
                if done: acks[k] = [x for x in acks.get(k, []) if x not in done]
            jdump(acks, HL_ACKS)
            ver = d.get("versions") or {}; ok = {}
            try:
                vd = d.get("voice") or {}
                voice_apply(vd, lambda vid, it: hl_open(it.get("url") or "", 60), lambda vid: hl_ack_queue("voice", vid))
                st = voice_state(); ok["voice"] = all(re.sub(r"[^A-Za-z0-9_-]", "", str(i.get("id") or "")) in st for i in vd.get("items") or [])
            except Exception as e: ok["voice"] = False; log.warning("sync voice: %s", e)
            if "media" in d:
                try:
                    media_apply(d["media"] or {}, lambda mid: hl_ack_queue("media", mid))
                    st = media_in_state(); ok["media"] = all(re.sub(r"[^A-Za-z0-9_-]", "", str(i.get("id") or "")) in st for i in (d["media"] or {}).get("items") or [])
                except Exception as e: ok["media"] = False; log.warning("sync media: %s", e)
            MEDIA_LINK.update(t=time.time(), ok=True, error="")
            if isinstance(d.get("fire_now"), dict): FIRE_NOW.clear(); FIRE_NOW.update(d["fire_now"])
            if isinstance(d.get("stations"), dict):      # 🔥 per theme channel (Hype Live 2.53): votes per play + the song on air
                chf = {}
                for sid, sd in d["stations"].items():
                    if not isinstance(sd, dict) or not chancfg.valid(sid): continue
                    chf[sid] = sd.get("fire_now") if isinstance(sd.get("fire_now"), dict) else None
                    if sd.get("fire"):
                        TL.ch = sid
                        try: audstats.set_fire(sd["fire"])          # the channel's own stats_<id>.db (Reports → Audience → Songs)
                        except Exception as e: log.warning("sync fire %s: %s", sid, e)
                        finally: TL.ch = None
                try: os.makedirs("/run/hype", exist_ok=True); jdump(dict(chf, t=time.time()), "/run/hype/ch_fire.json")
                except Exception: pass
            if d.get("fire"):          # 🔥 votes per play (Hype Live keeps the votes; Studio's song report shows them per play)
                try: audstats.set_fire(d["fire"])
                except Exception as e: log.warning("sync fire: %s", e)
            if d.get("covers"):
                try: sync_covers(d["covers"])
                except Exception as e: log.warning("sync covers: %s", e)
            if isinstance(d.get("sponsors"), list):
                planner.HL_DEALS = d["sponsors"]; jdump({"sponsors": d["sponsors"], "t": time.time()}, HL_SPONSORS)
                log.info("sync: sponsor deals %d", len(d["sponsors"]))
            for k in ("voice", "media", "sponsors"):
                if k in ver and ok.get(k, True): known[k] = ver[k]
            jdump(known, HL_KNOWN)
        except Exception as e: log.warning("sync: %s", e)

def voice_apply(d, fetch, ack):
    """Apply a voice list from Hype Live (old poll or the 2.41 sync answer; d = {} only does the housekeeping): remove /
    expire, OK-for-air changes, download new ones as clean mp3s onto the 🎙 VOICE pads, then ack them."""
    st = voice_state(); c = dbc(); changed = False
    for vid in d.get("remove") or []:
        if str(vid) in st: voice_drop(c, st, str(vid)); changed = True
    for vid, v in list(st.items()):
        if time.time() - v.get("received", 0) > VOICE_DAYS * 86400: voice_drop(c, st, vid); changed = True
    os.makedirs(VOICE_DIR, exist_ok=True)
    # ✓ OK for air / ✕ Not for air from Hype Live (items not acked yet carry it; later changes come in "update")
    for u in list(d.get("items") or []) + list(d.get("update") or []):
        vid = re.sub(r"[^A-Za-z0-9_-]", "", str(u.get("id") or ""))
        if vid in st and "air" in u and bool(st[vid].get("air")) != bool(u.get("air")):
            st[vid]["air"] = bool(u.get("air")); changed = True; log.info("voice: %s %s", vid, "OK for air" if u.get("air") else "not for air")
    for it in d.get("items") or []:
        vid = re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("id") or ""))
        if not vid or vid in st or time.time() - VOICE_FAIL.get(vid, 0) < 3600: continue   # (a message that failed: again in 1 h)
        name = re.sub(r"[^\w .'-]", "", str(it.get("name") or "Listener"), flags=re.U).strip()[:30] or "Listener"
        city = re.sub(r"[^\w .'-]", "", str(it.get("city") or ""), flags=re.U).strip()[:30]
        src = os.path.join("/tmp", "voice_" + vid); fn = f"Voice - {name}{' (' + city + ')' if city else ''} {vid}.mp3"
        sta = str(it.get("station") or "").strip().lower(); sta = sta if sta not in ("", "radio", "main", "hype_radio") and chancfg.valid(sta) else ""
        vdir = os.path.join(ROOT, "jingles-" + sta, "VOICE") if sta else VOICE_DIR      # a theme channel's voice: its own jingles-<id>/VOICE
        os.makedirs(vdir, exist_ok=True); tmp = os.path.join(vdir, "." + fn)
        try:
            with fetch(vid, it) as r, open(src, "wb") as f: f.write(r.read(30_000_000))
            p_ = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-af", "highpass=f=80", "-ar", "44100", "-ac", "2", "-b:a", "192k", "-f", "mp3", tmp], timeout=120)
            if p_.returncode != 0: raise RuntimeError("ffmpeg could not read it")
            os.replace(tmp, os.path.join(vdir, fn))
        except Exception as e:      # one bad message never blocks the others (or their acks)
            VOICE_FAIL[vid] = time.time(); log.warning("voice: %s failed (%s) – tried again in 1 h", vid, e)
            try: os.remove(tmp)
            except OSError: pass
            continue
        finally:
            try: os.remove(src)
            except FileNotFoundError: pass
        st[vid] = {"path": os.path.relpath(os.path.join(vdir, fn), ROOT), "station": sta, "name": name, "city": city, "source": it.get("source"),
                   "received": voice_when(it.get("received")), "acked": False, "in_bank": False, "air": bool(it.get("air"))}
        changed = True; log.info("voice: downloaded %s (%s)", vid, name)
    # Hype Live still lists one we already have and acked (e.g. the ack went to the old website during the move to Hetzner): ack again
    for it in d.get("items") or []:
        vid = re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("id") or ""))
        if vid in st and st[vid].get("acked"): ack(vid)
    # indexed? -> onto the VOICE bank (newest first) -> ack
    for vid, v in st.items():
        if v.get("acked"): continue
        r = c.execute("SELECT problem FROM items WHERE path=?", (v["path"],)).fetchone()
        if not r: continue                                   # the indexer has not seen it yet
        if r[0]: log.warning("voice: %s unusable: %s", vid, r[0])
        elif not v.get("in_bank"):
            v["in_bank"] = True          # shown on the LISTENERS pads (from voice_state), no longer copied into a pad bank (15 Nov)
        a = ack(vid)
        v["acked"] = True; changed = True; log.info("voice: ack %s -> %s", vid, a[:30])
    if changed: voice_save(st)

def hl_get(url, data=None, timeout=20, key=None):
    r = urllib.request.Request(url, data=data, headers={"User-Agent": "HypeStudio/1", **({"X-Hype-Key": key} if key else {})}, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(r, timeout=timeout)
def voice_loop():
    """Old way (Hype Live before 2.41, or when the sync answer stops): poll every 20 s. With the sync call working,
    this loop only waits – the minute sync (and the ⚡ poke) bring the changes."""
    while True:
        time.sleep(20)
        if hl_sync_active(): continue
        try:
            try: g = json.load(open(GUARD_CONF))
            except Exception: g = {}
            if not hl_key(g): continue
            base = g.get("hl_url", "https://www.hyperadio.nl/").rstrip("/") + "/?hype_studio_voice="; kr, kw = hl_key(g, "read"), hl_key(g)
            try: d = json.loads(hl_get(base, key=kr).read(500000).decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code != 404: log.warning("voice: Hype Live %s", e.code)
                continue
            def ack(vid):
                try: return hl_get(base + "&ack=" + urllib.parse.quote(vid), data=b"", key=kw).read(100).decode("utf-8", "replace")
                except urllib.error.HTTPError as e: return str(e.code)
            voice_apply(d, lambda vid, it: hl_open(it["url"], 60) if "hype_file=" in (it.get("url") or "") else hl_get(base + "&file=" + urllib.parse.quote(vid), timeout=60, key=kr), ack)
        except Exception as e: log.warning("voice: %s", e)

def guard_status(full=False):
    try: g = json.load(open("/var/lib/hype/guard.json"))
    except Exception: g = {"t": 0}
    g["fresh"] = time.time() - g.get("t", 0) < 20
    if full:
        try: c = json.load(open(GUARD_CONF))
        except Exception: c = {}
        g["conf"] = {"hl_url": c.get("hl_url", "https://www.hyperadio.nl/"), "has_key": bool(c.get("hl_key")), "has_read": bool(c.get("hl_key_read")), "has_write": bool(c.get("hl_key_write")), "heartbeat": c.get("heartbeat", True), "repair": c.get("repair", True),
                     "np": bool(c.get("np")), "np_spots": bool(c.get("np_spots"))}
    else:
        g = {k: g.get(k) for k in ("fresh", "ok", "problems", "heartbeat", "events", "tick")}; g["events"] = (g.get("events") or [])[:5]
        g["media_link"] = dict(MEDIA_LINK, age=round(time.time() - MEDIA_LINK["t"]) if MEDIA_LINK["t"] else None)
        g["sync"] = {"active": hl_sync_active(), "handled": HL_SYNC_SEEN["n"]}
    return g
def guard_save(d):
    try: c = json.load(open(GUARD_CONF))
    except Exception: c = {}
    if "hl_key" in d and str(d["hl_key"]).strip() and "•" not in str(d["hl_key"]): c["hl_key"] = str(d["hl_key"]).strip()[:80]
    if d.get("hl_key_clear"): c["hl_key"] = ""
    for k_ in ("hl_key_read", "hl_key_write"):       # Hype Live 2.40: read key (r…) + write key (w…)
        if k_ in d and str(d[k_]).strip() and "•" not in str(d[k_]): c[k_] = str(d[k_]).strip()[:80]
    if "hl_url" in d and str(d["hl_url"]).startswith("https://"): c["hl_url"] = str(d["hl_url"]).strip()[:200]
    for k in ("heartbeat", "repair", "np", "np_spots"):
        if k in d: c[k] = bool(d[k])
    jdump(c, GUARD_CONF, mode=0o600)
    return guard_status(full=True)
def shutil_disk(p):
    import shutil; u = shutil.disk_usage(p); return round(u.free / 1e9)

HS_WD = {"last": 0, "fix": 0}
PAGES_MT = {}
RELOAD_REQ = SP("/var/lib/hype/reload_pages"); RELOAD_BTN = SP("/var/lib/hype/reload_pages_btn")
def reload_changed_pages():
    """After an update of the graphics pages (Graphics / Ad box), OBS reloads them by itself (no old page in memory)."""
    # + a reload of ALL pages (also the website ones: Live Layer, Hype Screen) when asked: the panel button (RELOAD_BTN) or an
    # update that changes deploy/reload_pages.txt (RELOAD_REQ) - so OBS never keeps running an old version of a page
    allp = ("Graphics", "Clock", "Ad box", "Live Layer", "Hype Screen page")
    for src, f in (("Graphics", "/var/www/overlay/index.html"), ("Clock", "/var/www/overlay/index.html"), ("Top10 rank", "/var/www/overlay/index.html"), ("Ad box", "/var/www/overlay/adbox.html")) \
                  + tuple((n, RELOAD_REQ) for n in allp) + tuple((n, RELOAD_BTN) for n in allp):
        try: m = os.path.getmtime(f)
        except OSError: continue
        k = src + f
        if PAGES_MT.get(k) is None:
            # first look after a (re)start: a page changed in the last 10 min came with the same update that restarted
            # the engine (installed just before it) -> OBS may still have the old one: reload it once
            PAGES_MT[k] = 0 if time.time() - m < 600 else m
            if PAGES_MT[k]: continue
        if m != PAGES_MT[k]:
            PAGES_MT[k] = m
            try:
                with E.tv.olock: E.tv.client().press_input_properties_button(src, "refreshnocache")
                log.info("graphics page %s changed – OBS reloaded it", src)
            except Exception as e: log.warning("reload %s: %s", src, e); E.tv.cl = None
def watch_hype_screen():
    """The Hype Screen page (website) sometimes hangs inside OBS and stays empty: then reload it."""
    if E.tv.scene != "Hype Screen" or time.time() - HS_WD["fix"] < 300: return
    try:
        from PIL import Image
        import io
        with E.tv.olock:
            r = E.tv.client().get_source_screenshot("Hype Screen page", "png", 64, 36, -1)
        im = Image.open(io.BytesIO(base64.b64decode(r.image_data.split(",", 1)[1]))).convert("RGBA")
        px = list(im.getdata()); lit = sum(1 for (r_, g_, b_, a_) in px if a_ > 20 and (r_ + g_ + b_) > 30)
        if lit < len(px) * 0.02:
            with E.tv.olock: E.tv.client().press_input_properties_button("Hype Screen page", "refreshnocache")
            HS_WD["fix"] = time.time(); log.warning("Hype Screen page was empty in OBS – reloaded it")
    except Exception as e: log.warning("hype screen check: %s", e); E.tv.cl = None

def loop():
    last_sync = 0
    while True:
        try: E.tick()
        except Exception as e: log.warning("tick: %s", e)
        if CH:                                   # a theme station: no TV / screens / pages; its week of shows (from the website, via Hype Radio's engine)
            if time.time() - last_sync > 60:
                last_sync = time.time()
                try: planner.WEEK[:] = json.load(open("/var/lib/hype/week_%s.json" % CH))
                except Exception: pass
            if int(time.time()) % 10 == 0:       # 🔥 for the song on air (Hype Live's sync per station, via Hype Radio's engine)
                try:
                    f_ = json.load(open("/run/hype/ch_fire.json")).get(CH)
                    if isinstance(f_, dict): FIRE_NOW.clear(); FIRE_NOW.update(f_)
                except Exception: pass
            time.sleep(1); continue
        if int(time.time()) % 15 == 0:
            try: E.screen.tick()
            except Exception as e: log.warning("screen tick: %s", e)
        if time.time() - HS_WD["last"] > 90:
            HS_WD["last"] = time.time()
            try: watch_hype_screen()
            except Exception as e: log.warning("hs watch: %s", e)
        if int(time.time()) % 10 == 0:
            try: reload_changed_pages()
            except Exception as e: log.warning("pages: %s", e)
        if time.time() - last_sync > 20:
            last_sync = time.time()
            try: resync_tv()
            except Exception as e: log.warning("resync: %s", e)
        time.sleep(1)

def main_channel():
    """Channel mode: only what plays (clock, queue, mixing, pads, IDs over intros, AGC, meter) + the panel API."""
    log.info("Hype Radio's engine for %s (radio engine :%d, API :%d)", CHC["name"], LIQ_PORT, API_PORT)
    try: E.liq.cmd("radio.clear")
    except Exception: pass
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=meter_loop, daemon=True).start()
    threading.Thread(target=agc_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", API_PORT), H).serve_forever()

if __name__ == "__main__" and CH: main_channel()
if __name__ == "__main__":
    try: E.liq.cmd("radio.clear")   # start clean: the engine decides what comes next
    except Exception: pass
    threading.Thread(target=loop, daemon=True).start()
    threading.Timer(5, lambda: E.live.apply_cams()).start()
    threading.Timer(7, ensure_adbox).start()
    tickers.start()
    threading.Thread(target=meter_loop, daemon=True).start()
    threading.Thread(target=agc_loop, daemon=True).start()
    threading.Thread(target=voice_loop, daemon=True).start()
    threading.Thread(target=autorec_loop, daemon=True).start()
    audstats.load_live(); threading.Thread(target=aud_sampler, daemon=True).start()
    threading.Thread(target=tv_audio_refresh, daemon=True).start()
    threading.Thread(target=sponsors_loop, daemon=True).start()
    threading.Thread(target=recap_loop, daemon=True).start()
    threading.Thread(target=media_in_loop, daemon=True).start()
    threading.Thread(target=hl_sync_loop, daemon=True).start()
    try: MEDIA_VOL["v"] = float(planner.get_setting(dbc(), "media_vol", 1.0))
    except Exception: pass
    threading.Thread(target=slideshow_loop, daemon=True).start()
    threading.Thread(target=hypeweb.watch, args=(lambda: bool(E.media.cur and E.media.cur.get("file") == "live:web"),), daemon=True).start()
    # (charts go to the website only when you press Publish in the chart – no automatic sending any more)
    threading.Thread(target=watch_show, daemon=True).start()
    threading.Thread(target=tv_follow_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", API_PORT), H).serve_forever()
