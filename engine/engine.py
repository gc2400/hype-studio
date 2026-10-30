#!/usr/bin/env python3
"""Hype Studio playout engine (phase 2).
Keeps the upcoming plan (hour clock + your own picks), feeds Liquidsoap one song ahead,
switches the TV in OBS on every song start, and serves the control panel API."""
import os, re, json, time, random, sqlite3, socket, threading, logging, uuid, base64, hashlib, subprocess, array, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import obsws_python as obs
import sys, urllib.parse; sys.path.insert(0, "/opt/hype"); import planner, live, tickers

ROOT = "/srv/hype"; DB = "/srv/hype-data/library.db"; WEB = "/var/www/studio"; WAVES = "/srv/hype-data/wave"
CONF = json.load(open("/etc/hype/engine.json"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("engine"); logging.getLogger("obsws_python").setLevel(logging.WARNING)

CLOCK_A = ["LAT-POWER", "AFRO-CURRENT", "CARIB-MIX", "ABC-CURRENT", "HIPHOP-RNB-MIX", "LAT-CURRENT", "#NEW-SWEEPER",
           "NEW", "ABC-POWER", "DUTCH-CURRENT", "LAT-GOLD", "AFRO-POWER", "UP-AFRO", "UP-AFRO", "#SWEEPER",
           "CARIB-POWER", "ABC-GOLD", "DUTCH-GOLD", "#THROWBACK-SWEEPER", "CLASSICS", "LAT-CURRENT",
           "HIPHOP-RNB-MIX", "ABC-CURRENT", "AFRO-GOLD", "CARIB-MIX"]
ARTIST_SEP = 75 * 60; TITLE_SEP = 3 * 3600; TARGET_LUFS = -14.0; PLAN_LEN = 10; MIX_MAX = 5.0   # separation + overlap: Settings
COLS = "path, artist, title, folder, mode, clip_path, duration, lufs, cue_start, cue_mix, cue_end, section, has_video, cues_edited"
LOCK = threading.RLock()

def fold(t):
    """For searching: lower case, no accents (Álvaro = alvaro, Bésame = besame, Ñejo = nejo)."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(t or "")) if not unicodedata.combining(ch)).lower()
def dbc():
    c = sqlite3.connect(DB, timeout=30)
    c.create_function("fold", 1, fold, deterministic=True)
    c.execute("""CREATE TABLE IF NOT EXISTS asrun(id INTEGER PRIMARY KEY, started REAL, path TEXT, artist TEXT,
                 title TEXT, folder TEXT, mode TEXT, clip TEXT, scene TEXT, kind TEXT)""")
    return c

def row_item(r, src="clock"):
    k = ["path", "artist", "title", "folder", "mode", "clip_path", "duration", "lufs", "cue_start", "cue_mix", "cue_end", "section", "has_video", "cues_edited"]
    d = dict(zip(k, r)); d["id"] = uuid.uuid4().hex[:10]; d["src"] = src
    if d["section"] == "jingles": d["mode"] = "jingle-video" if d["has_video"] else "jingle"
    return d

class Liq:
    def cmd(self, line):
        with socket.create_connection(("127.0.0.1", 1234), timeout=5) as s:
            s.sendall((line + "\nquit\n").encode()); data = b""
            while True:
                b = s.recv(4096)
                if not b: break
                data += b
        return data.decode(errors="ignore").replace("Bye!", "").replace("END", "").strip()
    def queue_len(self):
        return len([x for x in self.cmd("q.queue").split() if x.isdigit()])

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
                             AND (folder=? OR folder LIKE ? OR folder LIKE ?) AND (folder NOT LIKE 'MIXES%' OR ? LIKE 'MIXES%')""", (folder, "%/" + folder, folder + "/%", folder)).fetchall()
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
        def ok(path):
            if path in paths: return False
            r = c.execute("SELECT artist FROM items WHERE path=?", (path,)).fetchone()
            return not (r and set(artist_key(r[0])) & arts)
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
SNAPS_FILE = "/var/lib/hype/plan_snaps.json"; MINE_FILE = "/var/lib/hype/upnext_mine.json"
AV_LAG = 0.20        # the radio's sound reaches OBS this much after Liquidsoap starts a song (measured 30 Oct: ~0.2 s)
LM_CACHE = {}
def lm_info(path):
    """(loudness map, LUFS, mix_set) of a file - cached; cleared when its cue points change."""
    if path in LM_CACHE: return LM_CACHE[path]
    try:
        r = dbc().execute("SELECT lmap, lufs, COALESCE(mix_set, 0) FROM items WHERE path=?", (path,)).fetchone()
        v = ([int(x) for x in r[0].split(",")] if r and r[0] and r[0] != "-" else None, r[1] if r else None, bool(r and r[2]))
    except Exception: v = (None, None, False)
    if len(LM_CACHE) > 4000: LM_CACHE.clear()
    LM_CACHE[path] = v; return v
def intro_len(it):
    """Seconds from START until the song is at its full level (0 = starts hard)."""
    lm, L, _ = lm_info(it.get("path"))
    if not lm or L is None: return None
    cs = it.get("cue_start") or 0.0
    for i in range(int(cs * 2), len(lm)):
        if lm[i] >= L - 6: return max(0.0, i / 2 - cs)
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
    if it.get("cues_edited") != 1:                           # not your own END: the fading tail may ring past it
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
        cm = min(max(cs, tv), end, tail_end - 0.3); cross = max(0.3, min(8.0, tail_end - cm))
        return cm, cross, (min(cross, 1.5) if hard_next else cross)
    strong = next((i for i in range(b - 1, a - 1, -1) if lm[i] >= L - 6), None)
    ts = (strong + 1) / 2 if strong is not None else tail_end
    outro = tail_end - ts
    if outro < 1.0:                                      # ends abruptly: tight change, no gap
        cm = tail_end - 0.4
    elif not hard_next:                                  # soft intro next: let it rise under the outro
        cm = max(ts, tail_end - min(outro, 6.0, nl + 1.5))
    else:                                                # hard start next: when the old song has dropped ~12 dB
        drop = next((i for i in range(strong if strong is not None else a, b) if lm[i] < L - 12), None)
        cm = min(max(ts, (drop / 2 if drop is not None else tail_end) - 0.3), tail_end - 0.5)
    cm = max(cs, min(cm, end))
    cross = max(0.3, min(MIX_MAX, 4.0 if hard_next else 6.0, tail_end - cm))
    return cm, cross, cross
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
    ce = cm + cross
    # every song and jingle: -14 LUFS into the processing (the level the sound presets were tuned on);
    # pads + intro sweepers: -14 (a bit on top of the music, as before)
    gain = 0.0 if it["lufs"] is None else max(-12.0, min(12.0, (-14.0 if pad else TARGET_LUFS) - it["lufs"]))
    if it.get("section") in ("jingles", "ads"): gain += JINGLE_DB    # short, dense jingles sound louder than songs at the same LUFS (Settings → Mixing)
    q = lambda v: str(v).replace('"', "'")
    meta = (f'liq_cue_in="{cs:.2f}",liq_cue_out="{ce:.2f}",liq_cross_duration="{cross:.2f}",liq_amplify="{gain:.2f} dB",hype_fade="{fade:.2f}",'
            f'artist="{q(it["artist"])}",title="{q(it["title"])}",hype_path="{q(it["path"])}",hype_id="{it["id"]}"')
    return f"annotate:{meta}:{os.path.join(ROOT, it['path'])}"

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

class Screen:
    """On-screen items (Gianno's PNG/WebM graphics): shown by the Graphics page when a rule fires."""
    def __init__(self):
        self.active = []; self.count = {}; self.last = {}; self.done_min = set()
        self.np = None; self.top10 = None
        try:   # what was on screen before a restart stays on screen
            self.active = [a for a in json.load(open(SCREEN_STATE)) if a["end"] > time.time()]
            for a in self.active: a["file"] = tickers.as_ticker(a["file"])
        except Exception: pass
    def save(self):
        try: json.dump([a for a in self.active if a["end"] > time.time()], open(SCREEN_STATE, "w"))
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
            threading.Timer(max(0.0, dl), lambda: E.hold_scene(name, hold)).start(); log.info("scheduled scene: %s (%s)", name, r["trigger"]); return
        if f.startswith(("ticker:", "adbox:")):   # a ticker (News, Traffic …) or the ad box slideshow: duration 0 = 60 s
            self.add(r["id"], f, r["delay"] or 0, r["duration"] or 60, r["where_"]); log.info("ticker on screen: %s (%s)", f[7:], r["trigger"]); return
        dur = r["duration"] or max(5.0, (length or 180) - (r["delay"] or 0))    # 0 = the whole song, until the next song starts
        self.add(r["id"], f, r["delay"] or 0, dur, r["where_"]); log.info("on screen: %s (%s)", r["name"] or f, r["trigger"])
    def on_song(self, c, it):
        length = max(1.0, (it.get("cue_mix") or it.get("cue_end") or it.get("duration") or 180) - (it.get("cue_start") or 0))
        m = re.search(r"TOP10N(\d+)", os.path.basename(it.get("path") or ""), re.I)
        if m: self.top10 = (int(m.group(1)), time.time())      # the Top 10 number jingle: the next song gets that number
        elif it["section"] == "music":
            now = time.time()
            # artist + title in the Now Playing box (Music video scene only): after the start, and again halfway on longer songs
            at = [now + 5.0] + ([now + length / 2] if length > 150 else [])
            mix_no_artist = (it.get("folder") or "").upper().startswith("MIXES") and not (it.get("artist") or "").strip()
            self.np = None if mix_no_artist else {"id": uuid.uuid4().hex[:8], "title": it.get("title") or "", "artist": it.get("artist") or "", "at": at}   # a mix without artist: no box
            if self.top10 and now - self.top10[1] < 120:
                f = "TOP10/N%d.png" % self.top10[0]
                if os.path.exists(os.path.join(planner.SCREEN_DIR, f)): self.add(None, f, 0.0, max(5.0, length - 1), "all")
            self.top10 = None
        for r in planner.api_screen_rules(c):
            if not r["enabled"] or not r["file"]: continue
            t, ref = r["trigger"], r["ref"] or ""
            ok = (t == "any" and it["section"] == "music") or (t == "jingle" and it["section"] == "jingles" and (not ref or (it["folder"] or "").startswith(ref)))
            if t == "folder" and it["section"] == "music": ok = planner.rule_matches(c, {"match_kind": "folder", "match_ref": ref}, it)
            if t == "playlist" and it["section"] == "music" and ref: ok = planner.in_playlist(c, ref, it["path"])
            if ok: self.fire(r, length)
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
        return {"now": now, "items": out, "scene": scene, "np": self.np if scene == "Music video" else None, "ticker": tick,
                "slides": ["/adbox/" + urllib.parse.quote(f) for f in slides], "slide_s": 30,
                "slides_layer": "adbox" if scene == "Hype Screen" else "graphics"}   # Hype Screen: its own source under the Live Layer

ADBOX = "/srv/hype/branding/adbox"
PIP_DIR = "/srv/hype/branding/pip"          # frames for the PIP scene (full-frame PNG with a transparent window)
STINGER_SCENES = {"Hype Screen", "PIP"}
def pip_frames():
    try: return sorted(f for f in os.listdir(PIP_DIR) if f.lower().endswith(".png"))
    except OSError: return []
SCREEN_STATE = "/var/lib/hype/screen_active.json"

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
    if "CLASSICS" in f: return "throwback"          # only the CLASSICS folders get the Throwback jingle (not GOLD)
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
        return self.cl
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
                if getattr(self, "_tr", None) != tr:
                    try: cl.set_current_scene_transition(tr); self._tr = tr
                    except Exception as e: log.warning("transition %s: %s", tr, e)
                if prev != scene: cl.set_current_program_scene(scene)
                if prev == "PIP" and scene != "PIP": self.pip_done()
                if scene == "PIP" and prev != "PIP": self._pip_t = time.time(); self._pip_f = getattr(self, "pip", None)
                self.scene = scene; self.prog = scene
        except Exception as e: log.warning("OBS scene: %s", e); self.cl = None
        self.route_audio()
    def pip_done(self):
        """A PIP showing ended: log frame + seconds for Hype Live's sponsor report (the frame name = the sponsor, e.g. PIP_Brand.png)."""
        t0 = getattr(self, "_pip_t", None); f = getattr(self, "_pip_f", None); self._pip_t = None
        if not t0 or not f: return
        dur = round(time.time() - t0)
        if dur < 3: return
        name = os.path.splitext(os.path.basename(f))[0]
        ev = {"t": int(t0), "type": "pip", "item": name, "brand": re.sub(r"^PIP[_ -]*", "", name, flags=re.I).replace("_", " ").strip(), "dur": dur}
        try:
            p = "/var/lib/hype/screen_events.json"
            try: evs = json.load(open(p))
            except Exception: evs = []
            evs = (evs + [ev])[-500:]; json.dump(evs, open(p, "w"))
        except Exception as e: log.warning("screen event: %s", e)
    def pip_frame(self, frame):
        f = os.path.join(PIP_DIR, os.path.basename(frame))
        if os.path.exists(f) and getattr(self, "pip", None) != f:
            if getattr(self, "prog", None) == "PIP": self.pip_done(); self._pip_t = time.time(); self._pip_f = f   # another frame while PIP stays on
            self.client().set_input_settings("PIP frame", {"file": f}, True); self.pip = f
    def route_audio(self, force=False):
      """TV sound = the radio, except when a paired clip is on screen: then the clip's own sound (+ pads/sweepers).
      While LIVE: only your studio mix, straight from the studio stream (same path as the cameras = in sync), never a clip's sound."""
      with self.olock:
        live = bool(getattr(self, "live", False))
        # PIP too: the sponsor frame goes around the video, so the clip's sound plays on (no switch to the mp3 and back)
        split = bool(self.clip_sound and (scene_has_video(self.scene) or self.scene == "PIP")) and not live
        if (split, live) == (self.split, getattr(self, "_live_r", None)) and not force: return
        try:
            cl = self.client()
            cl.set_input_volume("Radio audio", vol_db=-100.0 if (split or live) else 0.0)
            cl.set_input_mute("FX audio", not split)
            try: cl.set_input_mute("Studio audio", not live)
            except Exception as e: log.warning("Studio audio: %s", e)
            if not split:
                for n in ("Clip A", "Clip B"): cl.set_input_mute(n, True)
            elif self.cur in ("Clip A", "Clip B"): cl.set_input_mute(self.cur, False)   # back to the video: its sound again (it was muted while away)
            self.split = split; self._live_r = live
        except Exception as e: log.warning("TV audio: %s", e); self.cl = None
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
    def show(self, mode, clip_file, cue_start, sound=False, gain=0.0, kf=None):
        """Switch the TV for a new song.
        - sound=True (paired song): the clip plays with its own sound, from its own cue point.
        - otherwise the clip is picture only and must match the radio: when the song starts later in the file (cue point),
          we jump to the next keyframe and start it exactly when the radio gets there (the previous picture stays up till then)."""
        t0 = time.time()
        with self.olock:
          try:
            cl = self.client(); self.gen += 1; gen = self.gen
            if mode in ("clip", "pair", "jingle-video") and clip_file:
                nxt = "Clip A" if self.cur == "Clip B" else "Clip B"; old = self.cur
                cl.set_input_settings(nxt, {"local_file": clip_file, "looping": False, "restart_on_activate": False,
                                            "close_when_inactive": False, "hw_decode": True}, True)
                cl.set_input_mute(nxt, not sound)
                if sound: cl.set_input_volume(nxt, vol_db=max(-20.0, min(15.0, gain)))
                cl.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                cl.trigger_media_input_action(nxt, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY")   # never left paused
                delay = 0.0; seek_k = None
                if not (cue_start and cue_start > 0.3):
                    try:
                        st0 = cl.get_media_input_status(nxt)
                        if (st0.media_cursor or 0) > 3000 and st0.media_state == "OBS_MEDIA_STATE_PLAYING": time.sleep(0.3)
                        if (cl.get_media_input_status(nxt).media_cursor or 0) > 3000: cl.set_media_input_cursor(nxt, 0)
                    except Exception: pass
                if not sound and not (cue_start and cue_start > 0.3):
                    # picture-only clip from the top: the radio's sound reaches OBS ~AV_LAG later (Liquidsoap + Pulse),
                    # so the picture waits that long (the previous picture stays up) and starts at 0 with it
                    seek_k = 0.0; delay = max(0.0, AV_LAG - (time.time() - t0))
                if cue_start and cue_start > 0.3 and self._wait_open(cl, nxt):
                    now_pos = cue_start + (time.time() - t0) - (0.0 if sound else AV_LAG)   # where the sound in OBS is now
                    k = next((x for x in kf if x >= now_pos + 0.3), None) if (kf and not sound) else None
                    if k is not None and k - now_pos < 12:   # jump to that keyframe the moment the radio gets there
                        seek_k = k; delay = max(0.0, k - now_pos - 0.12)
                    else:                                  # paired (own sound) or no keyframe list: start right away
                        cl.set_media_input_cursor(nxt, int(now_pos * 1000))
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
                def activate(nxt=nxt, old=old, seek_k=seek_k):
                    with self.olock:
                        if gen != self.gen: return          # a newer song already took over
                        try:
                            cl2 = self.client()
                            if seek_k is not None: cl2.set_media_input_cursor(nxt, int(seek_k * 1000))
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
        f = show_intro(SHOW.get("title"))
        if not f: return None
        try:
            cur = (self.client().get_input_settings("Show loop").input_settings or {}).get("local_file")   # what OBS really has (also after an OBS restart)
            if cur != f:
                self.client().set_input_settings("Show loop", {"local_file": f, "looping": True, "hw_decode": True}, True)
            self.loop_file = f
            return f
        except Exception as e: log.warning("branding: %s", e); return None
    def screenshot(self, width=640):
      with self.olock:
        r = self.client().get_source_screenshot(self.scene or "Hype Screen", "jpg", width, int(width * 9 / 16), 70)
        return base64.b64decode(r.image_data.split(",", 1)[1])

class Engine:
    def __init__(self):
        self.liq = Liq(); self.picker = Picker(); self.tv = TV(); self.runner = planner.ClockRunner()
        c0 = dbc(); planner.init(c0); planner.default_setup(c0); planner.init_overlays(c0); planner.init_shows(c0)
        planner.init_pads(c0); planner.init_screen(c0); self.screen = Screen()
        try: c0.execute("ALTER TABLE items ADD COLUMN mix_set INT DEFAULT 0"); c0.commit()    # 1 = MIX set by hand (smart mix leaves it alone)
        except sqlite3.OperationalError: pass
        c0.execute("UPDATE items SET mtime=0 WHERE folder LIKE 'MIXES%' AND problem LIKE 'no artist%'"); c0.commit()   # mixes need no artist: index again
        self.live = live.Live(self.liq, self.tv, lambda: SHOW.get("title") or "show")
        self.tv.live = self.live.live; self.tv.route_audio(force=True)
        load_rotation()
        global MIX_OUT
        try: MIX_OUT = float(self.liq.cmd("var.get mix_out").split()[-1])
        except Exception: pass
        try: self.liq.cmd("var.set duck_lvl = 1.0")   # never start with the music dipped
        except Exception: pass
        self.ov_last = 0; self.ov_count = {}; self.ov_recent = []
        self.last_kind = None
        self.plan = []; self.liq_next = None; self.now = {}; self.auto = True; self.stop_at_end = False; self.stopped = False
        self.snaps = []      # (item id, playlist positions before it was planned) for the planned items not on air yet
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
        try: self.aux = json.load(open("/var/lib/hype/aux.json"))
        except Exception: self.aux = {"1": [], "2": []}
        self.dj = False; self.deck = None; self.faders = {}; self.parked = None
        self.sgroups = planner.get_setting(c0, "scene_groups", [])
        try:   # after a restart: the song that is still playing
            c = dbc(); r = c.execute("SELECT path, started, scene FROM asrun ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                it = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (r[0],)).fetchone()
                if it and time.time() - r[1] < (it[6] or 0): self.now = dict(row_item(it, "?"), started=r[1], scene=r[2]); self.tv.scene = r[2]
        except Exception as e: log.warning("restore now: %s", e)
    # ---------- planning ----------
    def avoid(self):
        items = self.plan + ([self.liq_next] if self.liq_next else [])
        return {"paths": {i["path"] for i in items}, "artists": {a for i in items[-4:] for a in artist_key(i["artist"])}}
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
            if not self.plan:
                it = self.dj_take() if self.dj else self.aux_take()
                if not it:   # nothing in the AUX/decks: the radio never stops – the clock keeps the music going
                    it = self.picker.next_planned(c, self.avoid(), self.cursor(), self.runner) or self.picker.next(c, self.avoid())
                    if it: self.picker.note(it)
                if it: self.plan.append(it)
            return
        while self.auto and len(self.plan) < PLAN_LEN:
            if self.hour_events(c, self.cursor()): continue      # a new hour starts with its :00 items (Top of the hour), then its clock
            self.runner.snap = None
            hf = getattr(self, "hour_floor", 0)
            if hf and self.cursor() < hf and not any(p_.get("hour_T") == hf for p_ in self.plan + ([self.liq_next] if self.liq_next else [])):
                self.hour_floor = hf = 0      # its top of the hour isn't planned (any more): plan with the clock of the hour we are in
            at = max(self.cursor(), hf)
            ph = self.pending_hour(c, at)
            if ph: at = ph - 1        # the TOH of this hour hasn't played yet: keep planning with the old hour's clock
            if self.runner.mix_key != planner.runner_key(at) and self.mix_in_run(at): self.runner.mix_key = planner.runner_key(at)
            planner.CTX["touched"] = {}
            try: it = self.picker.next_planned(c, self.avoid(), at, self.runner)
            finally: touched = planner.CTX.pop("touched", None)
            if it and touched: self.snaps.append((it["id"], {str(k): v for k, v in touched.items()}))
            if it and self.runner.snap: it["snap"] = self.runner.snap
            if not it: it = self.picker.next(c, self.avoid())     # nothing in the clocks: built-in test clock
            if not it: break
            self.picker.note(it); self.place(it)
            guard = getattr(self, "_guard", 0) + 1; self._guard = guard
            if guard > 60: self._guard = 0; break          # (jingles skipped many times in a row: stop for this tick)
        self._guard = 0
        self.save_snaps()
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
                                                    "close_when_inactive": False, "clear_on_media_end": True, "hw_decode": True}, True)
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
    def hold_scene(self, name, hold=0):
        """Switch the TV to a scene (from a scheduled rule); after `hold` seconds back to AUTO SCENES (0 = stays)."""
        with LOCK:
            self.tv.auto = False; self.tv.set_scene(name)
        if hold and hold > 0:
            def back(n=name):
                with LOCK:
                    if self.tv.scene == n.split("@")[0] and not self.tv.auto:
                        self.tv.auto = True; self.tv.scene = None
                        if self.now: self.tv.set_scene(self.now.get("scene") or "Hype Screen")
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
        if self.is_jingle(it) and self.is_jingle(last) and not after_toh:
            log.info("skipped %s – no two jingles in a row", it.get("title")); return
        if it.get("section") == "music":
            sk = song_kind(it)
            if sk == "throwback" and sk not in held and it.get("src") == "clock" and not self.tb_done():
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
    def push_next(self, c):
        if self.liq_next or self.stop_at_end or self.stopped: return
        if getattr(self, "loop", False) and self.now.get("path"):   # LOOP: the song on air comes again
            it = {k: v for k, v in self.now.items() if k not in ("started", "scene")}; it.update(id=uuid.uuid4().hex[:10], src="loop")
            self.liq.cmd("q.push " + annotate(it)); self.liq_next = it; return
        if not self.plan: return
        T = self.plan[0].get("hour_T")
        if T and self.auto and self.now.get("path"):
            start = max(time.time(), self.now["started"] + self.length(self.now))
            if start < T - 5:          # never before :00 – one more song first (picked to end just after :00)
                f = self.filler(c, T - start)
                if f: self.picker.note(f); self.plan.insert(0, f); log.info("top of the hour would start %d s early – %s – %s first", T - start, f["artist"], f["title"])
        it = self.plan.pop(0); warm(c, it)
        if it.get("event"):
            c.execute("INSERT OR IGNORE INTO events_done VALUES(?,?)", it["event"]); c.commit()
            if it.get("hour_T") and self.auto:
                # the top of the hour goes out: the new hour's clock starts at slot 1 right after it
                keep = [p_ for p_ in self.plan if p_.get("event") == it["event"] or p_.get("src") != "clock"]
                k = 0
                while k < len(self.plan) and self.plan[k].get("event") == it["event"]: k += 1
                head = self.plan[:k]; rest = [p_ for p_ in self.plan[k:] if p_.get("src") != "clock"]
                dropped = len(self.plan) - len(head) - len(rest)
                self.plan = head + rest; self.runner = planner.ClockRunner(); self.held = {}; self.hour_floor = it["hour_T"]
                log.info("new hour: top of the hour queued, %d planned item(s) re-planned, clock from slot 1", dropped)
        if it.get("snap"):   # a resume clock goes on after the last item that really went out
            try: planner.save_clock_state(c, *it["snap"])
            except Exception as e: log.warning("clock state: %s", e)
        res = self.liq.cmd("q.push " + annotate(it, nxt=self.plan[0] if self.plan else None)); self.liq_next = it
        ids = [s_[0] for s_ in self.snaps]
        if it.get("id") in ids: self.snaps = self.snaps[ids.index(it["id"]) + 1:]     # it goes on air: its songs are really used
        self.save_snaps()
        log.info("queued %s – %s (%s) -> %s", it["artist"], it["title"], it["folder"], res.splitlines()[0] if res else "")
    def mix_in_run(self, at):
        """Is a mixtape on air / in Up next that starts in the same clock run (hour or show) as `at`? (after a restart or
        a replan the clock runner is new and does not know it - so a 2nd mix is never planned in the same hour)"""
        k = planner.runner_key(at); is_mix = lambda p_: (p_.get("folder") or "").upper().startswith("MIXES")
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
        for q, args in (("folder=?", (self.now.get("folder"),)), ("1=1", ())):
            rows = c.execute(f"SELECT {COLS} FROM items WHERE section='music' AND problem IS NULL AND folder NOT LIKE 'MIXES%' AND {q}", args).fetchall()
            cand = [row_item(r, "clock") for r in rows if r[0] not in paths and not (set(artist_key(r[1])) & arts)]
            if cand: return min(cand, key=lambda i: planner.fit_score(self.length(i), gap))
        return None
    def take_back_next(self):
        """Pull the already-queued song back out of Liquidsoap (it goes back to the top of the plan)."""
        if self.liq_next:
            self.liq.cmd("radio.clear")
            if self.liq_next.get("event"):     # a fixed time that was queued but taken back is not done yet
                try:
                    cc = dbc(); cc.execute("DELETE FROM events_done WHERE event_id=? AND day=?", tuple(self.liq_next["event"])); cc.commit()
                except Exception as e: log.warning("events_done: %s", e)
            if self.liq_next.get("src") != "loop": self.plan.insert(0, self.liq_next)   # a loop copy is just dropped
            self.liq_next = None
    def event_items(self, c, e):
        avoid = self.avoid(); paths = []
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
        items = []
        for p_ in paths:
            if not p_: continue
            r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (p_,)).fetchone()
            if r: items.append(row_item(r, "fixed"))
        c.commit(); return items
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
                for i, it in enumerate(items): it["event"] = ek; self.plan.insert(idx + i, it)
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
            self.push_next(c)
            if e["hard"]: time.sleep(0.3); self.liq.cmd("radio.skip")
    def tick(self):
        with LOCK:
            c = dbc()
            try: self.check_events(c)
            except Exception as ex: log.warning("events: %s", ex)
            self.refill(c)
            if self.stop_at_end and self.liq_next: self.take_back_next()
            if self.liq_next and self.liq.queue_len() == 0 and time.time() - self.liq_next.get("pushed", time.time()) > 5:
                pass
            self.push_next(c)
            if self.liq_next and "pushed" not in self.liq_next: self.liq_next["pushed"] = time.time()
            up = [p_["path"] for p_ in ([self.liq_next] if self.liq_next else []) + self.plan if p_.get("mode") in ("clip", "pair", "jingle-video")]
            if up != getattr(self, "_up_w", None):   # the indexer upgrades these clips' TV copies first
                self._up_w = up
                try: json.dump(up, open("/var/lib/hype/upcoming.json", "w"))
                except Exception: pass
    # ---------- song start (called by Liquidsoap) ----------
    def on_track(self, meta):
        path = meta.get("hype_path") or ""
        if not path: return
        with LOCK:
            if self.now.get("path") == path and time.time() - self.now.get("started", 0) < 15: return
            c = dbc()
            it = self.liq_next if self.liq_next and self.liq_next["path"] == path else None
            if it: self.liq_next = None
            else:
                r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (path,)).fetchone()
                if not r: log.info("not in library: %s", path); return
                it = row_item(r, "?")
            mode = it["mode"]; clip_file = None; start_at = 0; sound = False; gain = 0.0
            kf = None
            if mode == "jingle-video" or mode == "clip": clip_file, start_at, kf = tv_file(c, path), it["cue_start"] or 0, keyframes(c, path)
            elif mode == "pair" and it["clip_path"]:
                clip_file, start_at, sound, gain = pair_clip(c, it["clip_path"])
            scene = self.tv.show(mode, clip_file, start_at, sound, gain, kf)
            if mode == "pair" and it["clip_path"]:
                cd = c.execute("SELECT duration FROM items WHERE path=?", (it["clip_path"],)).fetchone()
                if cd and cd[0] and it["duration"] and cd[0] < it["duration"] - 1:
                    threading.Timer(max(1.0, cd[0] - 0.3), lambda p=path: self.now.get("path") == p and self.tv.show("audio", None, 0)).start()
            c.execute("INSERT INTO asrun(started,path,artist,title,folder,mode,clip,scene,kind) VALUES(?,?,?,?,?,?,?,?,?)",
                      (time.time(), path, it["artist"], it["title"], it["folder"], mode, it["clip_path"], scene, it["section"])); c.commit()
            self.now = dict(it, started=time.time(), scene=scene)
            if it["section"] == "music":   # loudness map for the AGC intro lift
                try:
                    lm = c.execute("SELECT lmap FROM items WHERE path=?", (path,)).fetchone()
                    if lm and lm[0] and lm[0] != "-": self.now["lmap"] = [int(x) for x in lm[0].split(",")]
                except Exception: pass
            if it.get("deck"): self.deck = it["deck"]
            elif self.dj: self.deck = None
            if it["section"] == "music": self.intro_sweeper(c, it)
            try: self.screen.on_song(c, it)
            except Exception as e: log.warning("screen: %s", e)
            log.info("ON AIR %s – %s [%s/%s] scene=%s", it["artist"], it["title"], it["folder"], mode, scene)
            hl_send_np(c, it)
            if it["section"] != "music": self.last_kind = "jingle"
            self.push_next(c)
    def intro_sweeper(self, c, it):
        """Sweeper over the start of a song with the music ducked: the song's own choice, else the first rule that fits."""
        prev = self.last_kind
        self.last_kind = "music"
        r = c.execute("SELECT overlay_mode, overlay_path, overlay_at, overlay_duck FROM items WHERE path=?", (it["path"],)).fetchone()
        mode, opath, oat, oduck = r if r else (None, None, None, None)
        if mode == "none": return
        jingle, offset, duck = None, 0.5, 0.35
        if mode == "song" and opath:                            # chosen for this song (e.g. the Top 10 number 1)
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
        def go():
            ji = row_item(jr, "intro"); self.liq.cmd("pads.push " + annotate(ji, True)); self.duck_for(((ji.get("cue_end") or ji.get("duration") or 5) - (ji.get("cue_start") or 0)), duck)
            log.info("intro sweeper %s over %s", jingle, it["title"])
        threading.Timer(max(0.0, offset), go).start()
    # ---------- panel commands ----------
    def cmd(self, action, d):
        with LOCK:
            c = dbc()
            if action == "next":
                if getattr(self, "loop", False):   # NEXT ends a loop
                    self.loop = False
                    if self.liq_next and self.liq_next.get("src") == "loop": self.liq.cmd("radio.clear"); self.liq_next = None
                self.stopped = False; self.stop_at_end = False; self.push_next(c); time.sleep(0.3); self.liq.cmd("radio.skip")
            elif action == "play":
                if not self.auto and not self.dj and not self.stopped:   # MANUAL → PLAY = back to the normal programming
                    return self.cmd_auto(c, True)
                self.stopped = False; self.stop_at_end = False; self.push_next(c)
                if not self.now or time.time() - self.now.get("started", 0) > (self.now.get("duration") or 0): self.liq.cmd("radio.skip")
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
                if pos == 0: self.take_back_next()
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
                if pos == 0: self.take_back_next()
                self.plan.insert(pos, it); self.push_next(c)
            elif action == "remove":
                if self.liq_next and self.liq_next["id"] == d["id"]: self.take_back_next()
                self.plan = [p for p in self.plan if p["id"] != d["id"]]; self.push_next(c)
            elif action == "move":
                if self.liq_next and d["id"] in (self.liq_next["id"],) or int(d["to"]) == 0: self.take_back_next()
                it = next((p for p in self.plan if p["id"] == d["id"]), None)
                if it: self.plan.remove(it); self.plan.insert(int(d["to"]), it)
                self.push_next(c)
            elif action == "scene":
                self.tv.auto = False; self.tv.set_scene(d["scene"])
            elif action == "autoscene":
                self.tv.auto = True; self.tv.scene = None
                if self.now: self.tv.set_scene(self.now.get("scene") or "Hype Screen")
            elif action == "media_play":
                return {"ok": True, "media": self.media.play(self, d)}
            elif action == "media_stop":
                self.media.stop(self, keep_scene=bool(d.get("keep_scene"))); return {"ok": True}
            elif action == "pad_unloop":
                self.pad_loops.pop(d.get("path"), None); self.duck_until = 0; self.liq.cmd("pads.stop"); self.pad_video_off()
            elif action == "pad":
                r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (d["path"],)).fetchone()
                if d["path"].startswith("jingles/VOICE/"):              # a listener's voice: always over ducked music, never a loop
                    d["duck"] = True; d["loop"] = False
                    try:
                        st = voice_state()
                        for v in st.values():
                            if v.get("path") == d["path"]: v["played"] = time.time()
                        voice_save(st)
                    except Exception: pass
                if r and d.get("video"): self.pad_video(c, row_item(r, "pad"), bool(d.get("loop")))
                if r and d.get("loop"): self.pad_loop(r, bool(d.get("duck")), float(planner.get_setting(c, "pad_duck", 0.25)))
                elif r:
                    if d.get("duck"):
                        pi = row_item(r, "pad"); self.liq.cmd("pads.push " + annotate(pi, True)); self.duck_for(((pi.get("cue_end") or pi.get("duration") or 5) - (pi.get("cue_start") or 0)), float(planner.get_setting(c, "pad_duck", 0.25)))
                    else: self.liq.cmd("pads.push " + annotate(row_item(r, "pad"), True))
            elif action == "set_cues":
                cs, cm, ce = float(d["cue_start"]), float(d["cue_mix"]), float(d["cue_end"]); auto = bool(d.get("mix_auto"))
                if auto: cm = min(max(cm, cs), ce)
                if not (0 <= cs <= cm <= ce): return {"ok": False, "error": "START must be before MIX, MIX before END"}
                c.execute("UPDATE items SET cue_start=?, cue_mix=?, cue_end=?, cues_edited=1, mix_set=? WHERE path=?", (cs, cm, ce, 0 if auto else 1, d["path"])); c.commit()
                LM_CACHE.pop(d["path"], None)
                for p_ in self.plan:
                    if p_["path"] == d["path"]: p_.update(cue_start=cs, cue_mix=cm, cue_end=ce)
            elif action == "show_intro":
                self.tv.first_show_seen = True; self.tv.show_changed(SHOW.get("title"))
            elif action == "reset_cues":
                c.execute("UPDATE items SET cues_edited=0, mix_set=0, mtime=0 WHERE path=?", (d["path"],)); c.commit(); LM_CACHE.pop(d["path"], None)
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
                self.pad_loops.clear(); self.duck_until = 0
                self.liq.cmd("pads.stop"); self.pad_video_off()
            elif action.startswith("aux_"):
                return self.aux_cmd(c, action, d)
            elif action == "dj":     # DJ mode on/off: AUX 1 = deck A, AUX 2 = deck B; off = back to the clock
                on = bool(d.get("on"))
                if on: self.park(); self.auto = False
                self.dj = on; self.aux_back()      # leaving DJ = MANUAL: your AUX keeps playing (AUTO / PLAY = back to the programming)
                self.plan = self.keep_mine()
                self.refill(c); self.push_next(c)
            elif action in ("dj_play", "dj_cue"):   # PLAY NOW (cut) / MIX (crossfade) this deck's next song, or load it as the next song
                k = str(d.get("deck", "1")); how = d.get("how", "mix")
                if not self.aux.get(k): return {"ok": False, "error": "this deck is empty – drag songs into it"}
                it = dict(self.aux[k].pop(0), id=uuid.uuid4().hex[:10], src="dj", deck=k); self.aux_save_file()
                self.take_back_next()
                for back in reversed([p_ for p_ in self.plan if p_.get("src") in ("dj", "aux")]):   # songs that were lined up go back on their deck
                    self.aux.setdefault(back["deck"], []).insert(0, back)
                self.plan = [p_ for p_ in self.plan if p_.get("src") not in ("dj", "aux")]
                self.plan.insert(0, it); self.refill(c); self.push_next(c); self.aux_save_file()
                if action == "dj_play": time.sleep(0.3); self.liq.cmd("radio.cut" if how == "cut" else "radio.skip")
            elif action == "element":      # put a graphic on screen until you take it off (or toggle it off)
                f = tickers.as_ticker(d.get("file") or "")
                if d.get("off"): self.screen.hide(d.get("id"))
                elif f in planner.screen_files() or f == "adbox:" or (f.startswith("ticker:") and tickers.get(f[7:])):
                    on = [e for e in self.screen.manual() if e["file"] == f]
                    if on: self.screen.hide(on[0]["id"])
                    else: self.screen.add(None, f, 0.0, 6 * 3600, d.get("where") or "all", manual=True)
                else: return {"ok": False, "error": "file not found in branding/screen"}
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
        if on == self.auto and not self.dj: return {"ok": True}
        if on:
            self.aux_back(); self.dj = False; self.auto = True; self.stopped = False; self.stop_at_end = False
            keep = self.keep_mine()
            t, parked = self.parked or (0, [])
            played = {r[0] for r in c.execute("SELECT path FROM asrun WHERE started>=?", (t,))} | ({self.now.get("path")} if self.now else set())
            if parked and time.time() - t < 15 * 60:     # back within 15 min: the same Up next as before (nothing reshuffled)
                self.plan = keep + [p_ for p_ in parked if p_["path"] not in played]
            else:                                          # longer away: plan fresh from the clock of this moment
                self.plan = keep; self.runner = planner.ClockRunner()
            self.parked = None
            self.tv.auto = True; self.tv.scene = None      # the TV follows the music again too
            if self.now: self.tv.set_scene(self.now.get("scene") or "Hype Screen")
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
        """What survives a re-plan: your own items and fixed times – but not the top of the hour (it is placed again at :00)."""
        self.hour_floor = 0
        hk = {p_.get("event") for p_ in self.plan if p_.get("hour_T")}
        return [p_ for p_ in self.plan if p_.get("src") in ("you", "fixed") and not (p_.get("event") and p_.get("event") in hk)]
    def park(self):
        """Leaving AUTO: the clock's Up next is put aside (not thrown away), so AUTO comes back with the same songs."""
        self.take_back_next()
        if self.auto: self.parked = (time.time(), [p_ for p_ in self.strip_hour(self.plan) if p_.get("src") == "clock"])
        hk = {p_.get("event") for p_ in self.plan if p_.get("hour_T")}
        self.plan = [p_ for p_ in self.plan if p_.get("src") in ("you", "fixed", "aux", "dj") and not (p_.get("event") and p_.get("event") in hk)]
        self.hour_floor = 0
    # ---------- AUX players: two lists you fill by dragging, fire into the log ----------
    def aux_save_file(self):
        try: os.makedirs("/var/lib/hype", exist_ok=True); json.dump(self.aux, open("/var/lib/hype/aux.json", "w"))
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
                self.take_back_next(); self.plan.insert(0, new); self.push_next(c)
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
            c = dbc()
            hist = [dict(zip(["started", "artist", "title", "folder", "mode", "scene"], r)) for r in
                    c.execute("SELECT started, artist, title, folder, mode, scene FROM asrun ORDER BY id DESC LIMIT 8")]
            cid = planner.clock_for(c, time.time()); cname = c.execute("SELECT name FROM clocks WHERE id=?", (cid,)).fetchone() if cid else None
            return {"show": SHOW["title"], "clock": cname[0] if cname else None, "now": n, "elapsed": round(el, 1), "upcoming": upcoming, "history": hist, "auto": self.auto,
                    "stop_at_end": self.stop_at_end, "stopped": self.stopped, "scene": self.tv.scene, "auto_scene": self.tv.auto, "aux": self.aux, "dj": {"on": self.dj, "deck": self.deck}, "loop": getattr(self, "loop", False), "faders": self.get_faders(), "elements": self.screen.manual(), "pad_loops": list(self.pad_loops), "media": self.media.cur, "pip_frames": pip_frames(), "pip": os.path.basename(getattr(self.tv, "pip", "") or ""), "scene_groups": self.sgroups,
                    "tv_sound": "clip" if self.tv.split else "radio",
                    "time": time.strftime("%H:%M:%S"), "server_time": time.time()}

def pair_clip(c, clip_path):
    """Paired song: the TV plays the clip itself, with its own sound, from its own start cue, at radio loudness."""
    r = c.execute("SELECT cue_start, lufs FROM items WHERE path=?", (clip_path,)).fetchone() or (0, None)
    off = float(planner.get_setting(c, "tv_clip_offset", 3.0))
    gain = (TARGET_LUFS - r[1] + off) if r[1] is not None else off
    return os.path.join(ROOT, clip_path), r[0] or 0, True, gain

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
def library(qs):
    c = dbc(); q = (qs.get("q", [""])[0]).strip(); sec = qs.get("section", ["music"])[0]; folder = qs.get("folder", [""])[0]
    sql = "SELECT path, artist, title, folder, mode, clip_path, clip_suggest, clip_score, duration, has_video, problem, overlay_mode, overlay_path, cue_start, cue_mix, cue_end, cues_edited, added_at, COALESCE(mix_set, 0) FROM items WHERE section=?"
    mixes = sec == "mixes"
    if sec in ("music", "mixes"): sec = "music"; sql += " AND folder " + ("" if mixes else "NOT ") + "LIKE 'MIXES%'"
    args = [sec]; sort = qs.get("sort", ["az"])[0]; recent = int(qs.get("recent", ["0"])[0] or 0)
    if recent: sql += " AND added_at > ?"; args.append(time.time() - recent * 86400)   # uploaded in the last N days
    if folder: sql += " AND (folder=? OR folder LIKE ?)"; args += [folder, folder + "/%"]
    kind = qs.get("kind", [""])[0]
    if sec == "music" and kind in KIND_SQL: sql += " AND " + KIND_SQL[kind]
    if qs.get("auto", ["0"])[0] == "1": sql += " AND COALESCE(cues_edited, 0) = 0"
    for w in q.lower().split():
        sql += " AND fold(artist||' '||title||' '||filename) LIKE ?"; args.append(f"%{fold(w)}%")
    sql += " ORDER BY " + ("added_at DESC, artist, title" if sort == "new" else "(problem IS NULL), problem, artist, title" if sort == "problems" else "artist, title") + " LIMIT 300"
    last = {r[0]: r[1] for r in c.execute("SELECT path, MAX(started) FROM asrun GROUP BY path")}
    k = ["path", "artist", "title", "folder", "mode", "clip_path", "clip_suggest", "clip_score", "duration", "has_video", "problem", "overlay_mode", "overlay_path", "cue_start", "cue_mix", "cue_end", "cues_edited", "added_at", "mix_set"]
    out = []
    for r in c.execute(sql, args):
        d = dict(zip(k, r)); d["last"] = last.get(d["path"]); out.append(d)
    folders = [r[0] for r in c.execute("SELECT DISTINCT folder FROM items WHERE section=? ORDER BY folder", (sec,))]
    if sec == "music": folders = [f for f in folders if (f or "").upper().startswith("MIXES") == mixes]
    all_folders = folders
    counts = dict(c.execute("SELECT section, COUNT(*) FROM items GROUP BY section").fetchall())
    counts["mixes"] = c.execute("SELECT COUNT(*) FROM items WHERE section='music' AND folder LIKE 'MIXES%'").fetchone()[0]
    counts["music"] = counts.get("music", 0) - counts["mixes"]
    psql = "SELECT COUNT(*) FROM items WHERE section=? AND problem IS NOT NULL" + ((" AND folder " + ("" if mixes else "NOT ") + "LIKE 'MIXES%'") if sec == "music" else "")
    return {"problems": c.execute(psql, (sec,)).fetchone()[0], "items": out, "folders": folders, "all_folders": all_folders, "counts": counts}

def waveform(path):
    os.makedirs(WAVES, exist_ok=True)
    f = os.path.join(WAVES, hashlib.sha1(path.encode()).hexdigest()[:16] + ".json")
    if os.path.exists(f): return json.load(open(f))
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.join(ROOT, path), "-map", "0:a:0", "-ac", "1", "-ar", "2000", "-f", "s16le", "-"],
                       capture_output=True, timeout=120)
    a = array.array("h"); a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    n = 400; step = max(1, len(a) // n)
    peaks = [round(max((abs(x) for x in a[i:i + step]), default=0) / 32768, 3) for i in range(0, step * n, step)][:n]
    json.dump(peaks, open(f, "w")); return peaks

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
SHOW_TITLES = {}
def load_week():
    """The week schedule from hyperadio.nl -> planner.WEEK (day, start, end, title)."""
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
        try: json.dump(week, open("/var/lib/hype/week.json", "w"))
        except Exception: pass

try: planner.WEEK[:] = json.load(open("/var/lib/hype/week.json"))   # known right at start (show jingles plan right after a restart)
except Exception: pass

def watch_show():
    """Current show from hyperadio.nl (Pro.Radio schedule) every minute; week schedule hourly; temperature every 10 minutes."""
    last_week = 0
    while True:
        if time.time() - last_week > 3600:
            try: load_week(); last_week = time.time()
            except Exception as e: log.warning("week: %s", e); last_week = time.time() - 3000
        try:
            with urllib.request.urlopen("https://www.hyperadio.nl/wp-json/proradio/v1/current-show", timeout=10) as r:
                d = json.loads(r.read())
            title = (d.get("title") or "").strip() or None
            if title != SHOW["title"]:
                log.info("show is now: %s", title)
                SHOW.update(title=title, start=d.get("start"), end=d.get("end"), image=d.get("image"))
                E.tv.show_changed(title)
                try: E.screen.on_show(title)
                except Exception as e: log.warning("screen show: %s", e)
        except Exception as e: log.warning("show: %s", e)
        if time.time() - WEATHER["at"] > 600:
            try:
                u = "https://api.open-meteo.com/v1/forecast?latitude=52.37&longitude=4.89&current=temperature_2m,weather_code&timezone=Europe%2FAmsterdam"
                with urllib.request.urlopen(u, timeout=10) as r: cur = json.loads(r.read())["current"]
                WEATHER.update(temp=round(cur["temperature_2m"]), code=cur.get("weather_code"), at=time.time())
            except Exception as e: log.warning("weather: %s", e)
        time.sleep(60)

SOUND_VARS = ["snd_drive", "snd_bass", "snd_bass_f", "snd_treble", "snd_mid", "snd_comp", "snd_monob", "snd_agc", "snd_ratio", "snd_thresh", "snd_attack", "snd_release", "snd_ceiling", "snd_punch", "mix_out", "mix_in"]
def load_rotation():
    """Artist / song separation and the longest overlap, from Settings."""
    global ARTIST_SEP, TITLE_SEP, MIX_MAX, JINGLE_FADE, JINGLE_DB, ADBOX_ALWAYS, SMART_MIX, AV_LAG
    c = dbc()
    ARTIST_SEP = 60 * float(planner.get_setting(c, "artist_sep_min", 75)); TITLE_SEP = 3600 * float(planner.get_setting(c, "title_sep_h", 3))
    JINGLE_FADE = float(planner.get_setting(c, "jingle_fade", 0.0)); JINGLE_DB = float(planner.get_setting(c, "jingle_db", -3.0))
    ADBOX_ALWAYS = bool(planner.get_setting(c, "adbox_always", True))
    SMART_MIX = bool(planner.get_setting(c, "smart_mix", True))
    AV_LAG = max(0.0, min(1.0, float(planner.get_setting(c, "av_lag", 0.20))))
    MIX_MAX = 8.0     # (the longest-overlap slider is gone: the overlap = the fade-out)
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
            if sock is None: sock = socket.create_connection(("127.0.0.1", 1234), timeout=2)
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
            lvl = float(v[5]); AGC["level"] = lvl
            st = AGC["strength"]; up_max, down_max = min(6.0, 8.0 * st), -6.0 * st
            g = AGC["db"]; state = "ride"
            lift = AGC["lift"]
            lw = intro_lift(E.now, st) if (st > 0.01 and E.now and not E.stopped) else 0.0
            lift = lw if lw < lift else min(lw, lift + 2.0 * DT)        # up max 2 dB/s, down at once (the look-ahead already started it early)
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
            LEVEL.update(l=0.0, r=0.0); time.sleep(0.5); continue
        try:
            if sock is None: sock = socket.create_connection(("127.0.0.1", 1234), timeout=2)
            sock.sendall(b"radio.level\n"); buf = b""
            while b"END" not in buf:
                b_ = sock.recv(256)
                if not b_: raise OSError("closed")
                buf += b_
            v = buf.decode(errors="ignore").split("END")[0].split()
            LEVEL["l"], LEVEL["r"] = float(v[0]), float(v[1])
            if len(v) >= 5: LEVEL["comp"], LEVEL["lim"], LEVEL["lufs"] = float(v[2]), float(v[3]), float(v[4])
        except Exception:
            try: sock and sock.close()
            except Exception: pass
            sock = None; time.sleep(1)
        time.sleep(0.1)

def history(qs, limit=2000):
    """What went on air (as-run log): one day, newest first, optional search and type (music / jingles / ads)."""
    day = (qs.get("day") or [time.strftime("%Y-%m-%d")])[0][:10]; q = (qs.get("q") or [""])[0].strip().lower(); typ = (qs.get("type") or [""])[0]
    t0 = time.mktime(time.strptime(day, "%Y-%m-%d")); t1 = t0 + 86400
    c = dbc(); out = []
    rows = c.execute("""SELECT a.started, a.path, a.artist, a.title, a.folder, a.mode, a.scene, i.section,
                        COALESCE(i.cue_mix, i.duration, 0) - COALESCE(i.cue_start, 0) FROM asrun a LEFT JOIN items i ON i.path = a.path
                        WHERE a.started >= ? AND a.started < ? ORDER BY a.started DESC LIMIT ?""", (t0, t1, limit)).fetchall()
    for st, path, ar, ti, fo, mo, sc, sec, ln in rows:
        sec = sec or ("jingles" if (path or "").startswith("jingles/") else "ads" if (path or "").startswith("ads/") else "music")
        if typ and sec != typ: continue
        if q and q not in f"{ar} {ti} {fo}".lower(): continue
        out.append({"t": st, "time": time.strftime("%H:%M:%S", time.localtime(st)), "artist": ar or "", "title": ti or "", "folder": fo or "",
                    "type": sec, "mode": mo or "", "scene": sc or "", "len": round(ln or 0), "path": path or ""})
    music = sum(1 for r in out if r["type"] == "music")
    return {"day": day, "items": out, "count": len(out), "songs": music}

def get_settings():
    c = dbc(); liq = Liq(); snd = {}
    for v in SOUND_VARS:
        try: snd[v] = float(liq.cmd("var.get " + v).split()[-1])
        except Exception: snd[v] = None
    return {"smart_pick": bool(planner.get_setting(c, "smart_pick", True)), "hit_hour": bool(planner.get_setting(c, "hit_hour", True)),
            "hour_preview": bool(planner.get_setting(c, "hour_preview", True)), "tv_clip_offset": float(planner.get_setting(c, "tv_clip_offset", 3.0)),
            "artist_sep_min": ARTIST_SEP / 60, "pad_duck": float(planner.get_setting(c, "pad_duck", 0.25)), "jingle_fade": JINGLE_FADE, "jingle_db": JINGLE_DB, "adbox_always": ADBOX_ALWAYS, "smart_mix": SMART_MIX, "av_lag": AV_LAG, "sound_presets": planner.get_setting(c, "sound_presets", {}) or {}, "title_sep_h": TITLE_SEP / 3600, "mix_max": MIX_MAX,
            "sound": snd}
def set_settings(d):
    global MIX_OUT
    c = dbc(); liq = Liq()
    for k in ("smart_pick", "hit_hour", "hour_preview"):
        if k in d: planner.set_setting(c, k, bool(d[k]))
    if "tv_clip_offset" in d: planner.set_setting(c, "tv_clip_offset", max(-10.0, min(10.0, float(d["tv_clip_offset"]))))
    if "artist_sep_min" in d: planner.set_setting(c, "artist_sep_min", max(0.0, min(360.0, float(d["artist_sep_min"]))))
    if "title_sep_h" in d: planner.set_setting(c, "title_sep_h", max(0.5, min(48.0, float(d["title_sep_h"]))))
    if "sound_presets" in d and isinstance(d["sound_presets"], dict):   # your own sound presets (Settings → Sound)
        clean = {}
        for n, v in list(d["sound_presets"].items())[:30]:
            if isinstance(v, dict): clean[str(n)[:40]] = {k: float(x) for k, x in v.items() if k in SOUND_VARS and isinstance(x, (int, float))}
        planner.set_setting(c, "sound_presets", clean)
    if "adbox_always" in d: planner.set_setting(c, "adbox_always", bool(d["adbox_always"]))
    if "smart_mix" in d: planner.set_setting(c, "smart_mix", bool(d["smart_mix"]))
    if "av_lag" in d: planner.set_setting(c, "av_lag", max(0.0, min(1.0, float(d["av_lag"]))))
    if "jingle_db" in d: planner.set_setting(c, "jingle_db", max(-12.0, min(3.0, float(d["jingle_db"]))))
    if "jingle_fade" in d: planner.set_setting(c, "jingle_fade", max(0.0, min(5.0, float(d["jingle_fade"]))))
    if "pad_duck" in d: planner.set_setting(c, "pad_duck", max(0.05, min(0.8, float(d["pad_duck"]))))
    if "mix_max" in d: planner.set_setting(c, "mix_max", max(0.5, min(8.0, float(d["mix_max"]))))
    load_rotation()
    for k, v in (d.get("sound") or {}).items():
        if k in SOUND_VARS: liq.cmd(f"var.set {k} = {float(v):.2f}")
        if k == "mix_out": MIX_OUT = max(0.3, min(8.0, float(v)))
    return get_settings()

LOWER_FILE = "/var/lib/hype/lower.json"
def lower_load():
    try: return json.load(open(LOWER_FILE))
    except Exception: return {"visible": False, "style": "A", "size": "auto", "name": "", "role": "", "handle": "", "recent": []}
LOWER = lower_load()
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
    LOWER["rev"] = LOWER.get("rev", 0) + 1
    os.makedirs(os.path.dirname(LOWER_FILE), exist_ok=True); json.dump(LOWER, open(LOWER_FILE, "w"))
    return {"ok": True, "lower": LOWER}

E = Engine()

class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query); p = u.path.replace("/api", "", 1)
        try:
            if p == "/state": return self.send(200, E.state())
            if p == "/library": return self.send(200, library(qs))
            if p == "/history": return self.send(200, history(qs))
            if p == "/media": return self.send(200, {"files": media_files(), "scenes": MEDIA_SCENES})
            if p == "/streams": return self.send(200, dict(streams_status(E), guard=guard_status(), own=listeners(), own_tv=viewers()))
            if p == "/smartmix":     # the cue editor: where smart mix puts MIX for these START / END
                c = dbc(); r = c.execute(f"SELECT {COLS} FROM items WHERE path=?", (qs["path"][0],)).fetchone()
                if not r: return self.send(404, {"error": "not found"})
                it = row_item(r); it["cue_start"] = float(qs.get("s", [it["cue_start"] or 0])[0]); it["cue_end"] = float(qs.get("e", [it["cue_end"] or it["duration"] or 0])[0])
                lm, L, mset = lm_info(it["path"]); LM_CACHE[it["path"]] = (lm, L, False)
                sm = smart_mix(it); LM_CACHE[it["path"]] = (lm, L, mset)
                return self.send(200, {"mix": round(sm[0], 1) if sm else None, "overlap": round(sm[1], 1) if sm else None, "intro": intro_len(it), "smart": SMART_MIX})
            if p == "/mp3hit":       # nginx mirrors every /radio.mp3 start here (address + player), for unique MP3 listeners
                MP3_HITS.append((time.time(), self.headers.get("X-Real-IP", ""), self.headers.get("X-UA", "")))
                del MP3_HITS[:-2000]
                return self.send(200, {"ok": True})
            if p == "/audience": return self.send(200, {"radio": listeners(), "tv": viewers(), "t": int(time.time())})
            if p in ("/nowplaying", "/nowplaying/hype_radio"): return self.send(200, nowplaying_api())
            if p == "/guard": return self.send(200, guard_status(full=True))
            if p == "/level": LEVEL["asked"] = time.time(); return self.send(200, {"l": LEVEL["l"], "r": LEVEL["r"], "comp": LEVEL.get("comp", 0), "lim": LEVEL.get("lim", 0), "lufs": LEVEL.get("lufs"), "agc": round(AGC["db"] + AGC["lift"], 1), "agc_state": AGC.get("state", ""), "agc_on": AGC["strength"] > 0.01})
            if p == "/history.csv":
                h = history(qs, limit=5000); import csv, io; b = io.StringIO(); w = csv.writer(b, delimiter=";")
                w.writerow(["date", "time", "artist", "title", "folder", "type", "length", "tv", "file"])
                for r in reversed(h["items"]): w.writerow([h["day"], r["time"], r["artist"], r["title"], r["folder"], r["type"], r["len"], r["scene"], r["path"]])
                return self.send(200, ("\ufeff" + b.getvalue()).encode("utf-8"), "text/csv; charset=utf-8")
            if p == "/wave": return self.send(200, waveform(qs["path"][0]))
            if p == "/preview.jpg": return self.send(200, E.tv.screenshot(int(qs.get("w", ["640"])[0])), "image/jpeg")
            if p == "/playlists": return self.send(200, planner.api_playlists(dbc()))
            if p == "/playlist": return self.send(200, planner.api_preview_playlist(dbc(), int(qs["id"][0])))
            if p == "/playlist_take":    # the next songs of a playlist (for Up next / AUX); they count as played in its order
                c = dbc(); pid = int(qs["id"][0]); n = int(qs.get("n", ["0"])[0] or 0)
                r = c.execute("SELECT loop, shuffle, rotate FROM playlists WHERE id=?", (pid,)).fetchone()
                if not r: return self.send(404, {"error": "not found"})
                if not r[1] and not r[2] and n <= 0: paths = planner.playlist_songs(c, pid)          # an ordered list (Top 10): all of it, from the top
                else:
                    paths = []
                    for i in range(max(1, n or 5)):
                        p_ = planner.take_from_playlist(c, pid, "start" if (i == 0 and not r[1] and not r[2]) else "continue", lambda x, P=paths: x not in P)
                        if p_ and p_ not in paths: paths.append(p_)
                    c.commit()
                return self.send(200, {"paths": paths})
            if p == "/clocks": return self.send(200, planner.api_clocks(dbc()))
            if p == "/schedule": return self.send(200, planner.api_schedule(dbc()))
            if p == "/folders": return self.send(200, planner.api_folders(dbc()))
            if p == "/overlays": return self.send(200, planner.api_overlays(dbc()))
            if p == "/overlay_songs":   # songs with their own intro sweeper (set per song in the Library) - or "never"
                c = dbc(); rows = c.execute("""SELECT i.path, i.artist, i.title, i.folder, i.overlay_mode, i.overlay_path, j.title, j.folder FROM items i
                                              LEFT JOIN items j ON j.path = i.overlay_path WHERE i.overlay_mode IN ('song', 'none') ORDER BY i.artist, i.title""").fetchall()
                return self.send(200, [dict(path=r[0], artist=r[1], title=r[2], folder=r[3], mode=r[4], sweeper=r[5], sweeper_title=r[6] or (r[5] or "").split("/")[-1], sweeper_folder=r[7]) for r in rows])
            if p == "/shows": return self.send(200, planner.api_shows(dbc()))
            if p == "/events": return self.send(200, planner.api_events(dbc()))
            if p == "/ads":
                c_ = dbc(); pl_ = planner.ad_played_today(c_)
                files = [{"path": r[0], "title": r[1], "folder": r[2], "len": r[3], "video": bool(r[4])} for r in c_.execute(
                    "SELECT path, title, folder, COALESCE(cue_end, duration, 0) - COALESCE(cue_start, 0), has_video FROM items WHERE section='ads' AND problem IS NULL ORDER BY folder, title")]
                return self.send(200, {"spots": [dict(s_, played_today=pl_.get(s_["path"], 0)) for s_ in planner.ad_spots(c_)], "files": files,
                                       "max_len": planner.get_setting(c_, "ad_max_len", 150), "max_spots": planner.get_setting(c_, "ad_max_spots", 3)})
            if p == "/lower": return self.send(200, dict(LOWER, scene=E.tv.scene))
            if p == "/settings": return self.send(200, get_settings())
            if p == "/padbanks":
                b = planner.api_padbanks(dbc()); played = {v.get("path"): v.get("played") for v in voice_state().values()}
                for bk in b:
                    for it in bk["items"]:
                        if it["path"].startswith("jingles/VOICE/"): it["voice"] = True; it["played"] = played.get(it["path"])
                return self.send(200, b)
            if p == "/live": return self.send(200, E.live.status())
            if p == "/guest_info":      # public (guest page): who am I, which other guests are on
                slot, info = E.live.guest_by_token((qs.get("t") or [""])[0])
                if not slot: return self.send(404, {"error": "This guest link is not valid (any more). Ask Hype Radio for a new one."})
                st = E.live.status()
                return self.send(200, {"slot": slot, "name": info["name"], "show": SHOW.get("title"), "live": st["live"], "mode": info.get("mode", "video"),
                                       "others": [s_ for s_, g_ in st["guests"].items() if s_ != slot and g_["online"]]})
            if p == "/screen": return self.send(200, E.screen.current(E.tv.scene))
            if p == "/screenrules": return self.send(200, {"rules": planner.api_screen_rules(dbc()), "files": planner.screen_files(),
                                                         "tickers": [{"id": t["id"], "label": t["label"]} for t in tickers.config() if t.get("on")],
                                                         "bars": [t["bar"] for t in tickers.config() if t.get("bar")], "pip": pip_frames()})
            if p == "/tickers": return self.send(200, tickers.status())
            if p == "/preview_hour":
                c = dbc()
                if not planner.get_setting(c, "hour_preview", True): return self.send(200, {"off": True})
                return self.send(200, planner.preview_hour(c, day=int(qs["day"][0]) if "day" in qs else None, hour=int(qs["hour"][0]) if "hour" in qs else None,
                                                            clock=int(qs["clock"][0]) if "clock" in qs else None))
            if p == "/graphics": return self.send(200, {"show": SHOW["title"], "show_image": show_name_image(SHOW["title"]), "start": SHOW["start"], "end": SHOW["end"], "temp": WEATHER["temp"], "code": WEATHER["code"]})
            if p == "/pads":
                c = dbc(); rows = c.execute("SELECT path, title, folder, duration, has_video FROM items WHERE section='jingles' AND problem IS NULL ORDER BY folder, title").fetchall()
                return self.send(200, [dict(zip(["path", "title", "folder", "duration", "has_video"], r)) for r in rows])
            self.send(404, {"error": "not found"})
        except Exception as e:
            log.warning("GET %s: %s", p, e); self.send(500, {"error": str(e)})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        try: d = json.loads(body or b"{}")
        except Exception: d = {}
        p = urlparse(self.path).path.replace("/api", "", 1)
        if p == "/track":
            self.send(200, {"ok": True}); threading.Thread(target=E.on_track, args=(d,), daemon=True).start(); return
        if p.startswith("/plan/"):
            a = p[6:]; c = dbc()
            try:
                with LOCK:
                    res = {"ok": True}
                    if a == "save_playlist": res["id"] = planner.api_save_playlist(c, d)
                    elif a == "delete_playlist":
                        c.execute("DELETE FROM playlists WHERE id=?", (d["id"],)); c.execute("DELETE FROM playlist_sources WHERE playlist_id=?", (d["id"],))
                        c.execute("DELETE FROM playlist_state WHERE playlist_id=?", (d["id"],)); c.commit()
                    elif a == "reset_playlist": c.execute("DELETE FROM playlist_state WHERE playlist_id=?", (d["id"],)); c.commit()
                    elif a == "save_clock": res["id"] = planner.api_save_clock(c, d)
                    elif a == "delete_clock":
                        c.execute("DELETE FROM clocks WHERE id=?", (d["id"],)); c.execute("DELETE FROM clock_slots WHERE clock_id=?", (d["id"],))
                        c.execute("UPDATE schedule SET clock_id=NULL WHERE clock_id=?", (d["id"],)); c.commit()
                    elif a == "set_schedule": planner.api_set_schedule(c, d)
                    elif a == "save_overlays": planner.api_save_overlays(c, d); res["note"] = "rules saved"
                    elif a == "save_show_clocks": planner.api_save_show_clocks(c, d)
                    elif a == "save_events": planner.api_save_events(c, d)
                    elif a == "save_padbanks": planner.api_save_padbanks(c, d)
                    elif a == "save_screen": planner.api_save_screen_rules(c, d)
                    elif a == "save_ads": planner.api_save_ads(c, d); E.replan()
                    elif a != "replan": return self.send(404, {"error": "unknown"})
                    if a in ("save_clock", "delete_clock", "set_schedule", "replan", "delete_playlist", "save_playlist", "reset_playlist", "save_show_clocks"): E.replan()
                return self.send(200, res)
            except ValueError as e: return self.send(400, {"error": str(e)})
            except sqlite3.IntegrityError: return self.send(400, {"error": "that name is already used"})
            except Exception as e: log.warning("plan %s: %s", a, e); return self.send(500, {"error": str(e)})
        if p == "/cmd/lower": return self.send(200, lower_cmd(d))
        if p.startswith("/live/"):
            a = p[6:]; L = E.live
            try:
                if a == "on": L.set_live(True)
                elif a == "off": L.set_live(False)
                elif a == "guest_add": slot, g = L.guest_add(d.get("name"), d.get("slot"), int(d.get("hours") or 12), d.get("mode") or "video"); return self.send(200, {"ok": True, "slot": slot, "token": g["token"]})
                elif a == "guest_remove": L.guest_remove(d["slot"])
                elif a == "cam": L.set_cam(d["cam"], d["src"])
                elif a == "guest_vol": L.guest_vol(d["slot"], float(d.get("vol", 1)))
                elif a == "rec_start": L.start_rec("manual")
                elif a == "rec_stop": L.stop_rec()
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
        if p == "/settings":
            try: return self.send(200, set_settings(d))
            except Exception as e: log.warning("settings: %s", e); return self.send(500, {"error": str(e)})
        if p.startswith("/cmd/"):
            try: return self.send(200, E.cmd(p[5:], d))
            except Exception as e: log.warning("cmd %s: %s", p, e); return self.send(500, {"error": str(e)})
        self.send(404, {"error": "not found"})
    def log_message(self, *a): pass

def resync_tv():
    """After an OBS or engine restart: show the clip of the song on air again, at the right spot."""
    n = E.now
    if not n or n.get("mode") not in ("clip", "pair", "jingle-video"): 
        if n and E.tv.auto: E.tv.scene = None; E.tv.set_scene(n.get("scene") or "Hype Screen")
        return
    try:
        cl = E.tv.client(); items = cl.get_scene_item_list("Music video").scene_items
        if any(i["sceneItemEnabled"] for i in items if i["sourceName"] in ("Clip A", "Clip B")) and (E.tv.scene == "Music video" or not E.tv.auto): return
        c = dbc(); el = time.time() - n["started"]
        if n["mode"] == "pair":
            if not n.get("clip_path"): return
            f, cs, snd, gain = pair_clip(c, n["clip_path"]); start = cs + el
        else:
            f, start, snd, gain = tv_file(c, n["path"]), (n.get("cue_start") or 0) + el, False, 0.0
        if not f: return
        E.tv.scene = None
        E.tv.show(n["mode"], f, start, snd, gain, None if snd else keyframes(c, n["path"]))
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
MEDIA_SCENES = ["MEDIA", "MEDIA FULL", "CAM 1 + MEDIA", "CAM 1 2 + MEDIA"]
MEDIA_CACHE = "/var/lib/hype/media_cache.json"
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
                                info["lufs"] = float(m[-1]) if m else None; json.dump(_MC, open(MEDIA_CACHE, "w"))
                            except Exception as e: log.warning("media loudness: %s", e)
                        threading.Thread(target=measure, daemon=True).start()
                except Exception as e: log.warning("media %s: %s", f, e)
            _MC[key] = info; changed = True
        out.append(dict(info, file=f))
    if changed:
        try: json.dump(_MC, open(MEDIA_CACHE, "w"))
        except Exception: pass
    return out

class Media:
    """What plays in the 'Media window' (used by the scenes MEDIA, MEDIA FULL, CAM 1 + MEDIA, CAM 1 2 + MEDIA).
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
        sc = d.get("scene") or (tv.scene if tv.scene in MEDIA_SCENES else "MEDIA")
        if sc in MEDIA_SCENES:
            with LOCK: tv.auto = False; tv.set_scene(sc)
        self.cur = {"file": f, "kind": info["kind"], "sound": sound, "loop": loop, "started": time.time(), "duration": info["duration"], "scene": sc}
        if info["kind"] == "video" and info["audio"] and sound != "mute":
            ln = max(1.0, info["duration"]); gain = 0.0 if info["lufs"] is None else max(-12.0, min(12.0, -14.0 - info["lufs"]))
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
    def stop(self, eng, keep_scene=False):
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

_ST = {"t": 0, "d": None, "pub": None, "pub_t": 0}
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
            ua = re.findall(r'"([^"]*)"', ln); out.add(m.group(1) + "|" + (ua[-1] if ua else ""))
    except OSError: pass
    return out
ICE_ADMIN = {"t": 0, "auth": None}
MP3_HITS = []     # (time, address, player) of every /radio.mp3 start (nginx mirrors it to /mp3hit; nothing waits on it)
def mp3_devices():
    """MP3 listeners as devices: Icecast's listener list (player + connected seconds) matched to the start nginx reported
    (address + player + time). An unmatched connection still counts, as its own device."""
    try:
        if not ICE_ADMIN["auth"] or time.time() - ICE_ADMIN["t"] > 3600:
            x = open("/etc/icecast2/icecast.xml").read()
            ICE_ADMIN.update(t=time.time(), auth=base64.b64encode((re.search(r"<admin-user>([^<]+)", x).group(1) + ":" + re.search(r"<admin-password>([^<]+)", x).group(1)).encode()).decode())
        r = urllib.request.Request("http://127.0.0.1:8000/admin/listclients?mount=/radio.mp3", headers={"Authorization": "Basic " + ICE_ADMIN["auth"]})
        x = urllib.request.urlopen(r, timeout=3).read().decode("utf-8", "replace")
        import html as _h
        out = set(); now = time.time()
        for blk in re.findall(r"<listener[^>]*>(.*?)</listener>", x, re.S):
            g = lambda tag: (re.search(f"<{tag}>([^<]*)</{tag}>", blk) or [None, ""])[1]
            ip, ua, lid = g("IP"), _h.unescape(g("UserAgent")), g("ID")
            try: start = now - float(g("Connected") or 0)
            except ValueError: start = now
            if ip and ip not in ("127.0.0.1", "::1"): out.add(ip + "|" + ua); continue     # a direct listener (port 8000)
            hit = min((h for h in MP3_HITS if h[2] == ua and abs(h[0] - start) < 8), key=lambda h: abs(h[0] - start), default=None)
            out.add((hit[1] + "|" + ua) if hit else ("conn" + lid + "|" + ua))
        return out
    except Exception as e:
        log.warning("mp3 listeners: %s", e); return None
def tv_viewer_ips(window=40):
    """Different devices (address + player) that fetched TV stream pieces in the last `window` s (test link + stream.hyperadio.nl/tv/)."""
    return log_devices("/var/log/nginx/stream.access.log", lambda ln: " /tv/" in ln and ".ts" in ln, window)
def hls_viewers(window=40): return len(tv_viewer_ips(window))
VIEW = {"t": 0, "d": None}; VSTATS_FILE = "/var/lib/hype/viewers.json"
def viewers():
    """TV viewers on Hype Studio's own video stream (all players: website, app, TV apps, VLC …): now, peak today, unique today."""
    if VIEW["d"] and time.time() - VIEW["t"] < 10: return VIEW["d"]
    ips = tv_viewer_ips(); now_n = len(ips); day = time.strftime("%Y-%m-%d")
    try: st = json.load(open(VSTATS_FILE))
    except Exception: st = {}
    if st.get("day") != day: st = {"day": day, "peak": 0, "peak_t": 0, "uniq": []}
    if now_n > st["peak"]: st["peak"], st["peak_t"] = now_n, int(time.time())
    u = set(st.get("uniq", []))
    for ip in ips: u.add(hashlib.sha1((day + ip).encode()).hexdigest()[:12])     # no IPs stored, only a daily hash
    st["uniq"] = sorted(u)
    try: json.dump(st, open(VSTATS_FILE, "w"))
    except Exception: pass
    d = {"current": now_n, "peak_today": st["peak"], "peak_time": st["peak_t"], "unique_today": len(u)}
    VIEW.update(t=time.time(), d=d); return d
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
    except Exception: pass
    _ST.update(t=time.time(), d=d); return d
LISTEN = {"t": 0, "d": None}
LSTATS_FILE = "/var/lib/hype/listeners.json"
def hls_radio_ips(window=30):
    """Devices (address + player) that fetched radio HLS pieces in the last `window` s."""
    return log_devices("/var/log/nginx/stream.access.log", lambda ln: " /r" in ln and ".ts " in ln, window)
def listeners():
    """Radio listeners on Hype Studio's own streams as DEVICES (address + player; MP3 and HLS together, no double count):
    now, peak today, unique today (only daily codes are stored, no addresses)."""
    if LISTEN["d"] and time.time() - LISTEN["t"] < 10: return LISTEN["d"]
    mp3s = mp3_devices()
    if mp3s is None:                                   # Icecast's list not readable: fall back to its connection count
        mp3s = set()
        try:
            ice = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/status-json.xsl", timeout=3).read())["icestats"].get("source") or []
            ice = ice if isinstance(ice, list) else [ice]
            n = sum(int(x.get("listeners", 0)) for x in ice if str(x.get("listenurl", "")).endswith("/radio.mp3"))
            mp3s = {f"conn{i}|" for i in range(n)}
        except Exception: pass
    hls = hls_radio_ips(); devs = mp3s | hls; now_n = len(devs)
    day = time.strftime("%Y-%m-%d")
    try: st = json.load(open(LSTATS_FILE))
    except Exception: st = {}
    if st.get("day") != day: st = {"day": day, "peak": 0, "peak_t": 0, "uniq": []}
    if now_n > st["peak"]: st["peak"], st["peak_t"] = now_n, int(time.time())
    u = set(st.get("uniq", []))
    for k in devs:
        if not k.startswith("conn"): u.add(hashlib.sha1((day + k).encode()).hexdigest()[:12])     # no addresses stored, only a daily code
    st["uniq"] = sorted(u)
    try: json.dump(st, open(LSTATS_FILE, "w"))
    except Exception: pass
    d = {"current": now_n, "mp3": len(mp3s), "hls": len(hls), "peak_today": st["peak"], "peak_time": st["peak_t"],
         "unique_today": max(len(u), st["peak"]), "counts": "devices"}
    LISTEN.update(t=time.time(), d=d); return d
def nowplaying_api():
    """AzuraCast-style /api/nowplaying/hype_radio, so Hype Live (and others) only need a different link."""
    L = listeners(); n = E.now or {}; t = time.time()
    def song(it, sid=""):
        it = it or {}; a, ti = it.get("artist") or "", it.get("title") or ""
        return {"id": sid or hashlib.md5((a + ti).encode()).hexdigest(), "text": (a + " - " + ti) if a else ti, "artist": a, "title": ti, "album": "", "genre": it.get("folder") or "", "art": "", "custom_fields": {}}
    dur = E.length(n) if n.get("path") else 0
    el = max(0, t - (n.get("started") or t))
    nxt = (E.plan[0] if E.plan else None) if not E.liq_next else E.liq_next
    hist = []
    try:
        for r in dbc().execute("SELECT started, artist, title, folder, kind FROM asrun ORDER BY started DESC LIMIT 11").fetchall()[1:]:
            hist.append({"played_at": int(r[0]), "duration": 0, "playlist": r[3] or "", "is_request": False, "song": song({"artist": r[1], "title": r[2], "folder": r[3]})})
    except Exception: pass
    return {"station": {"id": 1, "name": "Hype Radio TV", "shortcode": "hype_radio", "listen_url": "https://stream.hyperadio.nl/radio.mp3",
                        "hls_url": "https://stream.hyperadio.nl/radio.m3u8", "hls_enabled": True, "is_public": True,
                        "mounts": [{"name": "MP3 128", "url": "https://stream.hyperadio.nl/radio.mp3", "bitrate": 128, "format": "mp3", "is_default": True}]},
            "listeners": {"total": L["current"], "unique": L["current"], "current": L["current"]},
            "live": {"is_live": E.live.status().get("air") == "studio" if hasattr(E, "live") else False, "streamer_name": "", "broadcast_start": None},
            "now_playing": {"sh_id": int(n.get("started") or 0), "played_at": int(n.get("started") or t), "duration": round(dur), "elapsed": round(el), "remaining": round(max(0, dur - el)),
                            "playlist": n.get("folder") or "", "streamer": "", "is_request": False, "song": song(n),
                            "type": "song" if n.get("section") == "music" else "jingle" if n.get("section") == "jingles" else (n.get("section") or "")},
            "playing_next": {"song": song(nxt)} if nxt else None, "song_history": hist, "is_online": not E.stopped,
            "hype": {"listeners": L, "viewers": viewers(), "source": "hype-studio"}}

GUARD_CONF = "/var/lib/hype/guard_conf.json"
def hl_send_np(c, it):
    """Song info + sponsor proof of play to Hype Live (?hype_np=KEY, JSON). Only when switched on (Settings → 🛡 Alarm & Hype Live),
    because until go-live the public radio is still CLOSE."""
    try: g = json.load(open(GUARD_CONF))
    except Exception: return
    if not g.get("hl_key") or not (g.get("np") or (g.get("np_spots") and it.get("section") == "ads")): return
    if not g.get("np") and it.get("section") != "ads": return
    sec = it.get("section"); typ = "song" if sec == "music" else "spot" if sec == "ads" else "jingle"
    body = {"artist": it.get("artist") or "", "title": it.get("title") or "", "type": typ, "length": round(E.length(it), 1), "start": round(time.time(), 1), "path": it.get("path")}
    if typ == "spot":
        r = c.execute("SELECT brand FROM ad_spots WHERE path=?", (it.get("path"),)).fetchone()
        body["brand"] = (r[0] if r and r[0] else (it.get("folder") or "").split("/")[-1]) or it.get("title")
    if typ == "song":
        body["next"] = [{"artist": p_.get("artist") or "", "title": p_.get("title") or "", "type": "song" if p_.get("section") == "music" else "jingle"} for p_ in (([E.liq_next] if E.liq_next else []) + E.plan)[:3]]
    url = g.get("hl_url", "https://hyperadio.nl/").rstrip("/") + "/?hype_np=" + urllib.parse.quote(g["hl_key"])
    def go():
        try:
            r = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "HypeStudio/1"})
            a = urllib.request.urlopen(r, timeout=10).read(100).decode("utf-8", "replace")
            log.info("Hype Live np (%s %s): %s", typ, body.get("brand") or body["title"], a[:40])
        except Exception as e: log.warning("Hype Live np: %s", e)
    threading.Thread(target=go, daemon=True).start()

# ---------------- 🎙 voice messages from Hype Live → the VOICE pad bank ----------------
VOICE_DIR = os.path.join(ROOT, "jingles", "VOICE"); VOICE_STATE = "/var/lib/hype/voice.json"; VOICE_DAYS = 14
VOICE_BANK = "🎙 VOICE"
def voice_state():
    try: return json.load(open(VOICE_STATE))
    except Exception: return {}
def voice_save(st):
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
def voice_loop():
    """Every 20 s: new voice messages Hype Live marked 'To Hype Studio' are downloaded, made into a clean mp3 in
    jingles/VOICE and put on the 🎙 VOICE pad bank (they only play when you tap the pad). Then Hype Live gets the ack.
    Deleted/blocked messages (Hype Live 'remove') and messages older than 14 days are removed again."""
    while True:
        time.sleep(20)
        try:
            try: g = json.load(open(GUARD_CONF))
            except Exception: g = {}
            if not g.get("hl_key"): continue
            base = g.get("hl_url", "https://hyperadio.nl/").rstrip("/") + "/?hype_studio_voice=" + urllib.parse.quote(g["hl_key"])
            def get(url, data=None, timeout=20):
                r = urllib.request.Request(url, data=data, headers={"User-Agent": "HypeStudio/1"}, method="POST" if data is not None else "GET")
                return urllib.request.urlopen(r, timeout=timeout)
            try: d = json.loads(get(base).read(500000).decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code != 404: log.warning("voice: Hype Live %s", e.code)
                continue
            st = voice_state(); c = dbc(); changed = False
            for vid in d.get("remove") or []:
                if str(vid) in st: voice_drop(c, st, str(vid)); changed = True
            for vid, v in list(st.items()):
                if time.time() - v.get("received", 0) > VOICE_DAYS * 86400: voice_drop(c, st, vid); changed = True
            os.makedirs(VOICE_DIR, exist_ok=True)
            for it in d.get("items") or []:
                vid = re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("id") or ""))
                if not vid or vid in st: continue
                name = re.sub(r"[^\w .'-]", "", str(it.get("name") or "Listener"), flags=re.U).strip()[:30] or "Listener"
                city = re.sub(r"[^\w .'-]", "", str(it.get("city") or ""), flags=re.U).strip()[:30]
                src = os.path.join("/tmp", "voice_" + vid); fn = f"Voice - {name}{' (' + city + ')' if city else ''} {vid}.mp3"
                try:
                    with get(base + "&file=" + urllib.parse.quote(vid), timeout=60) as r, open(src, "wb") as f: f.write(r.read(30_000_000))
                    tmp = os.path.join(VOICE_DIR, "." + fn)
                    p_ = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-af", "highpass=f=80", "-ar", "44100", "-ac", "2", "-b:a", "192k", "-f", "mp3", tmp], timeout=120)
                    if p_.returncode != 0: raise RuntimeError("ffmpeg could not read it")
                    os.replace(tmp, os.path.join(VOICE_DIR, fn))
                finally:
                    try: os.remove(src)
                    except FileNotFoundError: pass
                st[vid] = {"path": os.path.relpath(os.path.join(VOICE_DIR, fn), ROOT), "name": name, "city": city, "source": it.get("source"),
                           "received": voice_when(it.get("received")), "acked": False, "in_bank": False}
                changed = True; log.info("voice: downloaded %s (%s)", vid, name)
            # indexed? -> onto the VOICE bank (newest first) -> ack
            for vid, v in st.items():
                if v.get("acked"): continue
                r = c.execute("SELECT problem FROM items WHERE path=?", (v["path"],)).fetchone()
                if not r: continue                                   # the indexer has not seen it yet
                if r[0]: log.warning("voice: %s unusable: %s", vid, r[0])
                elif not v.get("in_bank"):
                    bid = voice_bank(c)
                    c.execute("UPDATE pad_items SET pos=pos+1 WHERE bank_id=?", (bid,))
                    c.execute("INSERT INTO pad_items VALUES(?,?,?,?,?)", (bid, 0, v["path"], "🎙 " + v["name"] + (" · " + v["city"] if v["city"] else ""), "#0277b8"))
                    c.commit(); v["in_bank"] = True
                try: a = get(base + "&ack=" + urllib.parse.quote(vid), data=b"").read(100).decode("utf-8", "replace")
                except urllib.error.HTTPError as e: a = f"{e.code}"
                v["acked"] = True; changed = True; log.info("voice: ack %s -> %s", vid, a[:30])
            if changed: voice_save(st)
        except Exception as e: log.warning("voice: %s", e)

def guard_status(full=False):
    try: g = json.load(open("/var/lib/hype/guard.json"))
    except Exception: g = {"t": 0}
    g["fresh"] = time.time() - g.get("t", 0) < 20
    if full:
        try: c = json.load(open(GUARD_CONF))
        except Exception: c = {}
        g["conf"] = {"hl_url": c.get("hl_url", "https://hyperadio.nl/"), "has_key": bool(c.get("hl_key")), "heartbeat": c.get("heartbeat", True), "repair": c.get("repair", True),
                     "np": bool(c.get("np")), "np_spots": bool(c.get("np_spots"))}
    else:
        g = {k: g.get(k) for k in ("fresh", "ok", "problems", "heartbeat", "events")}; g["events"] = (g.get("events") or [])[:5]
    return g
def guard_save(d):
    try: c = json.load(open(GUARD_CONF))
    except Exception: c = {}
    if "hl_key" in d and str(d["hl_key"]).strip() and "•" not in str(d["hl_key"]): c["hl_key"] = str(d["hl_key"]).strip()[:80]
    if d.get("hl_key_clear"): c["hl_key"] = ""
    if "hl_url" in d and str(d["hl_url"]).startswith("https://"): c["hl_url"] = str(d["hl_url"]).strip()[:200]
    for k in ("heartbeat", "repair", "np", "np_spots"):
        if k in d: c[k] = bool(d[k])
    json.dump(c, open(GUARD_CONF, "w")); os.chmod(GUARD_CONF, 0o600)
    return guard_status(full=True)
def shutil_disk(p):
    import shutil; u = shutil.disk_usage(p); return round(u.free / 1e9)

HS_WD = {"last": 0, "fix": 0}
PAGES_MT = {}
def reload_changed_pages():
    """After an update of the graphics pages (Graphics / Ad box), OBS reloads them by itself (no old page in memory)."""
    for src, f in (("Graphics", "/var/www/overlay/index.html"), ("Ad box", "/var/www/overlay/adbox.html")):
        try: m = os.path.getmtime(f)
        except OSError: continue
        if PAGES_MT.get(f) is None: PAGES_MT[f] = m; continue
        if m != PAGES_MT[f]:
            PAGES_MT[f] = m
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
    threading.Thread(target=watch_show, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8090), H).serve_forever()
