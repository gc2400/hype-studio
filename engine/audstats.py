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
