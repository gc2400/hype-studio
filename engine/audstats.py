"""Audience details for History → Audience (like AzuraCast's statistics, but for the radio AND the TV, without doubles of
what Hype Live already reports: best / worst songs and listeners by hour stay in Hype Live).

Every 15 s the engine hands over who is listening / watching right now. From that this module keeps SESSIONS: one device
(internet address + player) from the moment it starts until it is gone for 90 s. A finished session is stored as one row:
radio / TV, stream, device type, app, country, city, start, length. The internet address itself is never stored (only a
daily code, to count different devices); it only lives in memory while the session runs.
Country / city: DB-IP Lite (CC BY 4.0), a local file, refreshed once a month – nothing is asked from outside per listener."""
import os, re, time, json, sqlite3, hashlib, threading, gzip, urllib.request, logging

log = logging.getLogger("hype")
DB = "/srv/hype-data/stats.db"
GEO = "/srv/hype-data/geo/dbip-city-lite.mmdb"
LIVE_FILE = "/var/lib/hype/aud_live.json"
STREAM_LOG = "/var/log/nginx/stream.access.log"
GAP = 90            # a device not seen for this long: its session has ended
KEEP_DAYS = 400
TVQ = {"v0": "TV 720p", "v1": "TV 1080p", "v2": "TV 480p", "v3": "TV 360p"}   # ladder.sh var_stream_map order
LOCK = threading.Lock()
LIVE = {}           # (kind, key) -> session
_geo = {"r": None, "m": 0}


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(kind TEXT, start REAL, end REAL, stream TEXT, dev TEXT, app TEXT,
                 os TEXT, cc TEXT, city TEXT, dk TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS s_end ON sessions(end)")
    # one row per song play: listeners (radio) / viewers (tv) when listeners started to hear it and when it ended;
    # src "h" = Hype Studio's own numbers, "a" = AzuraCast (imported from Hype Live, before the switch)
    c.execute("""CREATE TABLE IF NOT EXISTS plays(t REAL PRIMARY KEY, end REAL, artist TEXT, title TEXT, path TEXT,
                 r0 REAL, r1 REAL, v0 REAL, v1 REAL, fire INT, src TEXT)""")
    # the audience every 5 minutes (average + peak), for the day graph and the weekday x hour heatmap
    c.execute("CREATE TABLE IF NOT EXISTS aud5(t INTEGER PRIMARY KEY, r REAL, tv REAL, rmax REAL, tvmax REAL, src TEXT)")
    return c


# ---------------- country / city ----------------
def geo(ip):
    """(country code, city) for an internet address, or ("", "")."""
    if not ip or ip.startswith(("127.", "10.", "192.168.", "::1")): return "", ""
    try:
        m = os.path.getmtime(GEO)
        if _geo["r"] is None or m != _geo["m"]:
            import maxminddb
            _geo["r"] = maxminddb.open_database(GEO); _geo["m"] = m
        x = _geo["r"].get(ip) or {}
        return (x.get("country") or {}).get("iso_code", ""), ((x.get("city") or {}).get("names") or {}).get("en", "")
    except Exception:
        return "", ""


def geo_refresh():
    """Once a month: the newest DB-IP Lite city file (free, ~130 MB) – downloaded next to the old one, then swapped."""
    try:
        if os.path.exists(GEO) and time.time() - os.path.getmtime(GEO) < 32 * 86400: return
        url = time.strftime("https://download.db-ip.com/free/dbip-city-lite-%Y-%m.mmdb.gz")
        tmp = GEO + ".tmp"
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            f.write(gzip.decompress(r.read()))
        os.replace(tmp, GEO); log.info("country file updated (%s)", url.rsplit("/", 1)[-1])
    except Exception as e:
        log.warning("country file: %s", e)


# ---------------- device / app from the player name ----------------
def device(ua):
    """(device type, app) from a user agent."""
    u = ua or ""; l = u.lower()
    if re.search(r"smart-?tv|tizen|webos|web0s|bravia|aftb|aftm|aftt|afts|fire tv|appletv|apple tv|tvos|roku|android tv|googletv|crkey|hbbtv|kodi|playstation|xbox", l):
        dev = "TV / box"
    elif re.search(r"alexa|echo|sonos|bose|google-home|googlespeaker|denon|heos", l): dev = "Smart speaker"
    elif re.search(r"carplay|android auto|tesla", l): dev = "Car"
    elif "ipad" in l or ("android" in l and "mobile" not in l and "mozilla" in l): dev = "Tablet"
    elif re.search(r"iphone|ipod|mobile|android|cfnetwork|dalvik|exoplayer|okhttp", l): dev = "Phone"
    elif re.search(r"windows|macintosh|mac os x|x11|linux|cros", l) and "mozilla" in l: dev = "Computer"
    elif re.search(r"vlc|lavf|ffmpeg|foobar|winamp|itunes|musicbee|mpv|gstreamer|streamer|nsplayer|libmpv", l): dev = "Player / IPTV"
    else: dev = "Other"
    for pat, name in ((r"samsungbrowser", "Samsung Internet"), (r"edg/|edge/", "Edge"), (r"opr/|opera", "Opera"), (r"firefox|fxios", "Firefox"),
                      (r"crios|chrome/", "Chrome"), (r"applecoremedia|appletv", "Apple player (iPhone / iPad / TV)"),
                      (r"version/.*safari|mobile/.*safari", "Safari"), (r"exoplayer|dalvik|okhttp|stagefright", "Android app"),
                      (r"vlc", "VLC"), (r"kodi", "Kodi"), (r"lavf|ffmpeg", "FFmpeg (IPTV / restream)"), (r"alexa|echo", "Alexa"),
                      (r"sonos", "Sonos"), (r"tunein", "TuneIn"), (r"radiogarden|radio garden", "Radio Garden"), (r"itunes", "iTunes"),
                      (r"winamp", "Winamp"), (r"foobar", "foobar2000")):
        if re.search(pat, l): return dev, name
    first = re.split(r"[/ (;]", u.strip())[0] if u.strip() else ""
    return dev, (first[:40] or "Unknown")


# ---------------- who is there now ----------------
def hls_now(is_player, window=45):
    """{(kind, "ip|ua"): stream} from the last `window` s of the stream log: radio HLS pieces and TV pieces (with quality)."""
    out = {}; now = time.time()
    try:
        with open(STREAM_LOG, "rb") as f:
            f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 3_000_000)); lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError: return out
    for ln in reversed(lines):
        m = re.match(r'(\S+) \S+ \S+ \[([^\]]+)\] "GET (\S+)', ln)
        if not m: continue
        try: t = time.mktime(time.strptime(m.group(2).split()[0], "%d/%b/%Y:%H:%M:%S"))
        except ValueError: continue
        if now - t > window: break
        p = m.group(3); ua = re.findall(r'"([^"]*)"', ln); ua = ua[-1] if ua else ""
        if not p.endswith(".ts") or not is_player(ua): continue
        k = m.group(1) + "|" + ua
        if p.startswith("/tv/"):
            q = p.split("/")[2] if p.count("/") >= 3 else ""
            out.setdefault(("tv", k), TVQ.get(q, "TV"))       # newest line first: the quality it plays NOW
        elif re.match(r"^/r\d+\.ts", p):
            out.setdefault(("radio", k), "Radio HLS (iPhone / website)")
    return out


def update(mp3_keys, is_player):
    """Called every 15 s by the engine with the MP3 listeners ("ip|ua" or "connN|ua"). Starts / ends sessions."""
    now = time.time(); seen = hls_now(is_player)
    for k in mp3_keys or ():
        seen[("radio", k)] = "Radio MP3"
    ended = []
    with LOCK:
        for sk, stream in seen.items():
            s = LIVE.get(sk)
            if s and now - s["last"] <= GAP:
                s["last"] = now; s["stream"] = stream
            else:
                if s: ended.append(s)
                ip, _, ua = sk[1].partition("|")
                dev, app = device(ua); cc, city = geo("" if ip.startswith("conn") else ip)
                LIVE[sk] = {"kind": sk[0], "start": now, "last": now, "stream": stream, "dev": dev, "app": app, "cc": cc, "city": city,
                            "dk": hashlib.sha1((time.strftime("%Y-%m-%d") + sk[1]).encode()).hexdigest()[:12]}
        for sk in [k for k, s in LIVE.items() if now - s["last"] > GAP]:
            ended.append(LIVE.pop(sk))
        try:
            with open(LIVE_FILE + ".tmp", "w") as f: json.dump([[list(k), v] for k, v in LIVE.items()], f)
            os.chmod(LIVE_FILE + ".tmp", 0o600); os.replace(LIVE_FILE + ".tmp", LIVE_FILE)
        except Exception: pass
    if ended:
        c = db()
        c.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)",
                      [(s["kind"], s["start"], s["last"], s["stream"], s["dev"], s["app"], "", s["cc"], s["city"], s["dk"]) for s in ended])
        c.commit(); c.close()


def load_live():
    """After an engine restart: the sessions that were running go on (if they were seen in the last 90 s)."""
    try:
        for k, v in json.load(open(LIVE_FILE)):
            if time.time() - v["last"] <= GAP: LIVE[tuple(k)] = v
    except Exception: pass


def clean():
    c = db(); c.execute("DELETE FROM sessions WHERE end < ?", (time.time() - KEEP_DAYS * 86400,)); c.commit(); c.close()


# ---------------- the report ----------------
BUCKETS = [(30, "under 30 s"), (60, "30 s – 1 min"), (300, "1 – 5 min"), (600, "5 – 10 min"), (1800, "10 – 30 min"),
           (3600, "30 min – 1 h"), (7200, "1 – 2 h"), (10 ** 9, "over 2 h")]


def report(days=1, kind=""):
    """Sessions of the last `days` days (1 = today since 00:00), radio / tv / both. Running sessions count too."""
    now = time.time()
    if days <= 1:
        lt = time.localtime(now); t0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    else: t0 = now - days * 86400
    c = db()
    rows = c.execute("SELECT kind, start, end, stream, dev, app, cc, city, dk FROM sessions WHERE end >= ?", (t0,)).fetchall(); c.close()
    with LOCK: live = [dict(v) for v in LIVE.values()]
    rows = [dict(zip(("kind", "start", "end", "stream", "dev", "app", "cc", "city", "dk"), r)) for r in rows]
    rows += [dict(s, end=s["last"], live=True) for s in live]
    if kind in ("radio", "tv"): rows = [r for r in rows if r["kind"] == kind]
    for r in rows: r["dur"] = max(0.0, r["end"] - max(r["start"], t0)) + 15      # seen every 15 s: count the last interval too

    def group(key):
        g = {}
        for r in rows:
            k = r[key] or "?"
            x = g.setdefault(k, {"name": k, "sessions": 0, "devices": set(), "hours": 0.0})
            x["sessions"] += 1; x["devices"].add(r["dk"]); x["hours"] += r["dur"] / 3600
        out = sorted(({"name": x["name"], "sessions": x["sessions"], "devices": len(x["devices"]), "hours": round(x["hours"], 2)} for x in g.values()),
                     key=lambda x: -x["hours"])
        return out

    tot = {}
    for kd in ("radio", "tv"):
        rs = [r for r in rows if r["kind"] == kd]
        real = [r for r in rs if r["dur"] >= 60]        # a "visit" = stayed at least a minute (skips quick taps / bots)
        tot[kd] = {"devices": len({r["dk"] for r in rs}), "sessions": len(rs), "hours": round(sum(r["dur"] for r in rs) / 3600, 1),
                   "avg_min": round(sum(r["dur"] for r in real) / len(real) / 60, 1) if real else 0,
                   "now": sum(1 for r in rs if r.get("live"))}
    buckets = [{"name": n, "sessions": 0} for _, n in BUCKETS]
    for r in rows:
        for i, (lim, _) in enumerate(BUCKETS):
            if r["dur"] < lim: buckets[i]["sessions"] += 1; break
    cities = {}
    for r in rows:
        if r["city"]: cities.setdefault(r["cc"], {}).setdefault(r["city"], 0); cities[r["cc"]][r["city"]] += 1
    ctry = group("cc")
    for x in ctry: x["cities"] = sorted(cities.get(x["name"], {}).items(), key=lambda kv: -kv[1])[:5]
    nowl = sorted(({"kind": s["kind"], "stream": s["stream"], "dev": s["dev"], "app": s["app"], "cc": s["cc"], "city": s["city"],
                    "since": int(s["start"])} for s in live if not kind or s["kind"] == kind), key=lambda s: s["since"])
    return {"from": int(t0), "totals": tot, "now": nowl, "length": buckets, "devices": group("dev"), "apps": group("app"),
            "countries": ctry, "streams": group("stream")}


# ---------------- songs: gainers and losers ----------------
def add_plays(rows):
    """rows: (t, end, artist, title, path, r0, r1, v0, v1, fire, src)"""
    if not rows: return
    c = db(); c.executemany("INSERT OR IGNORE INTO plays VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows); c.commit(); c.close()


def add_aud5(rows):
    """rows: (t, r, tv, rmax, tvmax, src)"""
    if not rows: return
    c = db(); c.executemany("INSERT OR REPLACE INTO aud5 VALUES(?,?,?,?,?,?)", rows); c.commit(); c.close()


def own_since(table):
    """The first / last moment Hype Studio's own numbers cover in plays / aud5 (None, None when empty)."""
    c = db(); r = c.execute(f"SELECT MIN(t), MAX(t) FROM {table} WHERE src='h'").fetchone(); c.close()
    return r


def set_fire(items):
    """🔥 votes per play from Hype Live (sync): [{"t": play start, "n": votes}] – matched to the play that started within 30 s."""
    if not items: return
    c = db()
    for x in items:
        try: c.execute("UPDATE plays SET fire=? WHERE t BETWEEN ? AND ?", (int(x.get("n") or 0), float(x["t"]) - 30, float(x["t"]) + 30))
        except Exception: pass
    c.commit(); c.close()


def _lt(t): lt = time.localtime(t); return lt.tm_wday, lt.tm_hour


def _norms(c, t1, days=90):
    """The normal radio / TV change of a song per (source, weekday, hour), over the plays of the last 90 days before t1.
    A slot with few plays falls back to that hour on any day, then to the source's overall average."""
    acc = {}
    for t, r0, r1, v0, v1, src in c.execute("SELECT t, r0, r1, v0, v1, src FROM plays WHERE t >= ? AND t < ? AND r0 IS NOT NULL", (t1 - days * 86400, t1)):
        d, h = _lt(t); dr = (r1 or 0) - (r0 or 0); dv = (v1 or 0) - (v0 or 0) if v0 is not None and v1 is not None else 0
        for k in ((src, d, h), (src, None, h), (src, None, None)):
            a = acc.setdefault(k, [0, 0.0, 0.0]); a[0] += 1; a[1] += dr; a[2] += dv
    def norm(src, d, h):
        for k, need in (((src, d, h), 3), ((src, None, h), 3), ((src, None, None), 1)):
            a = acc.get(k)
            if a and a[0] >= need: return a[1] / a[0], a[2] / a[0]
        return 0.0, 0.0
    return norm


ART_SPLIT = re.compile(r"\s+(?:x|ft\.?|feat\.?|featuring|&|and|vs\.?|with)\s+|\s*,\s*", re.I)


def _plays(days):
    now = time.time(); c = db()
    rows = c.execute("SELECT t, end, artist, title, r0, r1, v0, v1, fire, src FROM plays WHERE t >= ? AND r0 IS NOT NULL ORDER BY t", (now - days * 86400,)).fetchall()
    norm = _norms(c, now); c.close(); out = []
    for t, end, ar, ti, r0, r1, v0, v1, fire, src in rows:
        d, h = _lt(t); nr, nv = norm(src, d, h)
        out.append({"t": t, "len": round((end or t) - t), "artist": ar or "", "title": ti or "", "src": src, "fire": fire,
                    "radio": round((r1 or 0) - (r0 or 0) - nr, 2), "tv": round(((v1 or 0) - (v0 or 0) if v0 is not None and v1 is not None else 0) - nv, 2),
                    "r0": r0, "r1": r1, "v0": v0, "v1": v1})
    return out


def songs(days=30, by="song"):
    """Gainers and losers: per song (or artist) the average radio / TV change against a normal song at that weekday + hour."""
    ps = _plays(days); g = {}
    for p in ps:
        keys = [p["artist"].strip()] if by != "artist" else [a.strip() for a in ART_SPLIT.split(p["artist"] or "") if a.strip()]
        for k0 in keys or [""]:
            k = (k0 + " – " + p["title"]) if by != "artist" else k0
            x = g.setdefault(k.lower(), {"name": k, "artist": p["artist"], "title": p["title"] if by != "artist" else "", "plays": 0, "radio": 0.0, "tv": 0.0, "fire": 0, "fired": 0, "last": 0})
            x["plays"] += 1; x["radio"] += p["radio"]; x["tv"] += p["tv"]; x["last"] = max(x["last"], p["t"])
            if p["fire"] is not None: x["fire"] += p["fire"]; x["fired"] += 1
    out = []
    for k, x in g.items():
        if x["plays"] < 3: continue
        r, v = x["radio"] / x["plays"], x["tv"] / x["plays"]; f = x["fire"] / x["fired"] if x["fired"] else None
        out.append({"key": k, "name": x["name"], "plays": x["plays"], "radio": round(r, 2), "tv": round(v, 2), "fire": round(f, 1) if f is not None else None,
                    "score": round(r + v + 0.5 * (f or 0), 2), "last": int(x["last"])})
    out.sort(key=lambda x: -x["score"])
    return {"days": days, "by": by, "measured": len(ps), "listed": len(out), "best": out[:25], "worst": list(reversed(out[-25:])) if len(out) > 25 else list(reversed(out)),
            "since": int(ps[0]["t"]) if ps else None}


def song_plays(key, days=90, by="song"):
    key = (key or "").lower(); out = []
    for p in _plays(days):
        names = [(p["artist"].strip() + " – " + p["title"]).lower()] if by != "artist" else [a.strip().lower() for a in ART_SPLIT.split(p["artist"] or "")]
        if key in names: out.append(p)
    return list(reversed(out))


def plays_csv(days=90):
    import csv, io
    f = io.StringIO(); w = csv.writer(f)
    w.writerow(["date", "time", "artist", "title", "length_s", "radio_start", "radio_end", "radio_vs_normal", "tv_start", "tv_end", "tv_vs_normal", "fire", "source"])
    for p in _plays(days):
        w.writerow([time.strftime("%Y-%m-%d", time.localtime(p["t"])), time.strftime("%H:%M:%S", time.localtime(p["t"])), p["artist"], p["title"], p["len"],
                    p["r0"], p["r1"], p["radio"], p["v0"], p["v1"], p["tv"], p["fire"] if p["fire"] is not None else "", "Hype Studio" if p["src"] == "h" else "AzuraCast"])
    return f.getvalue()


# ---------------- listeners by time ----------------
def bytime(view="today"):
    """The day graph (5-min points + "normal for this weekday"), peaks, best hour, different devices, weekday x hour heatmap."""
    now = time.time(); lt = time.localtime(now); today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    c = db()
    def day_points(d0):
        return {int((t - d0) // 300): (r, tv, rm, tm) for t, r, tv, rm, tm in c.execute("SELECT t, r, tv, rmax, tvmax FROM aud5 WHERE t >= ? AND t < ?", (d0, d0 + 86400))}
    def avg_points(days_list):
        acc = {}
        for d0 in days_list:
            for k, (r, tv, rm, tm) in day_points(d0).items():
                a = acc.setdefault(k, [0, 0.0, 0.0]); a[0] += 1; a[1] += r or 0; a[2] += tv or 0
        return {k: (a[1] / a[0], a[2] / a[0]) for k, a in acc.items()}
    if view in ("today", "yesterday"):
        d0 = today0 if view == "today" else today0 - 86400
        pts = day_points(d0); t0, t1 = d0, d0 + 86400
        normal = avg_points([d0 - 7 * 86400 * w for w in (1, 2, 3, 4)])
        series = [{"i": k, "r": round(v[0] or 0, 1), "tv": round(v[1] or 0, 1)} for k, v in sorted(pts.items())]
        peaks = {"r": max(((v[2] or 0, k) for k, v in pts.items()), default=(0, None)), "tv": max(((v[3] or 0, k) for k, v in pts.items()), default=(0, None))}
    else:
        n = 7 if view == "avg7" else 30; days = [today0 - 86400 * i for i in range(1, n + 1)]
        avg = avg_points(days); t0, t1 = today0 - n * 86400, today0; normal = {}
        series = [{"i": k, "r": round(v[0], 1), "tv": round(v[1], 1)} for k, v in sorted(avg.items())]
        rows = c.execute("SELECT t, rmax, tvmax FROM aud5 WHERE t >= ? AND t < ?", (t0, t1)).fetchall()
        peaks = {"r": max(((r or 0, t) for t, r, _ in rows), default=(0, None)), "tv": max(((tv or 0, t) for t, _, tv in rows), default=(0, None))}
    # heatmap + best hour: last 30 days
    heat = [[[0, 0.0, 0.0] for _ in range(24)] for _ in range(7)]
    for t, r, tv in c.execute("SELECT t, r, tv FROM aud5 WHERE t >= ?", (today0 - 30 * 86400,)):
        d, h = _lt(t); x = heat[d][h]; x[0] += 1; x[1] += r or 0; x[2] += tv or 0
    hm = [[{"r": round(x[1] / x[0], 1) if x[0] else None, "tv": round(x[2] / x[0], 1) if x[0] else None} for x in row] for row in heat]
    hours = [[0, 0.0] for _ in range(24)]
    for d in range(7):
        for h in range(24):
            x = heat[d][h]
            if x[0]: hours[h][0] += x[0]; hours[h][1] += x[1] + x[2]
    best = max(range(24), key=lambda h: hours[h][1] / hours[h][0] if hours[h][0] else -1)
    dev = {k: n for k, n in c.execute("SELECT kind, COUNT(DISTINCT dk) FROM sessions WHERE end >= ? AND start < ? GROUP BY kind", (t0, t1))}
    c.close()
    with LOCK:
        for s in LIVE.values():
            if t1 > now - 60: dev.setdefault(s["kind"], 0)
    def pk(x):
        v, k = x
        if k is None: return {"n": 0, "at": None}
        at = (t0 + k * 300) if view in ("today", "yesterday") else k
        return {"n": round(v), "at": int(at)}
    return {"view": view, "from": int(t0), "series": series, "normal": [{"i": k, "r": round(v[0], 1), "tv": round(v[1], 1)} for k, v in sorted(normal.items())],
            "peak_r": pk(peaks["r"]), "peak_tv": pk(peaks["tv"]), "best_hour": best if hours[best][0] else None,
            "devices": {"radio": dev.get("radio", 0), "tv": dev.get("tv", 0)}, "heat": hm}
