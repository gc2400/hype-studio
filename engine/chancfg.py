"""Hype theme channels (Hype Ambiente, Hype Latina …): the shared setup.

Every theme channel has
  - its own planning database /srv/hype-data/chan_<id>.db: playlists, clocks, hour plan, as-run, settings – the same tables
    as Hype Radio's library.db, so planner.py works on it unchanged. The SONGS are not copied: the library's items table is
    attached read-only and shown as the view "items".
  - its own radio engine (Liquidsoap, radio/channel.liq) with its own sound settings, MP3 + HLS stream.
  - one playout thread in hype-channels (engine/chan.py).
Hype Radio itself is channel "main" and is NOT handled here (engine.py, radio.liq) – a theme channel can never touch it.
"""
import json, os, re, sqlite3

CFG = "/var/lib/hype/channels.json"
LIB = "/srv/hype-data/library.db"
DATA = "/srv/hype-data"
API_PORT = 8100            # hype-channels' own small API (status / commands), 127.0.0.1 only
DEFAULT = [
    {"id": "ambiente", "name": "Hype Ambiente", "color": "#1E5BC6", "enabled": False, "telnet": 1301,
     "genre": "Papiamentu · Kombina · Tumba"},
    {"id": "latina", "name": "Hype Latina", "color": "#FF6A2B", "enabled": False, "telnet": 1302,
     "genre": "Reggaeton · Latin · Dembow"},
]
ID_RE = re.compile(r"^[a-z][a-z0-9]{1,19}$")


def load():
    """All theme channels (main excluded). A missing / broken file = the defaults (all off)."""
    try:
        with open(CFG) as f: chs = json.load(f)
        chs = [c for c in chs if isinstance(c, dict) and ID_RE.match(str(c.get("id", ""))) and c["id"] != "main"]
    except (OSError, ValueError):
        chs = []
    have = {c["id"] for c in chs}
    for d in DEFAULT:                                   # new defaults appear by themselves (off)
        if d["id"] not in have: chs.append(dict(d))
    for i, c in enumerate(chs):
        c.setdefault("telnet", 1301 + i); c.setdefault("color", "#05A9C6"); c.setdefault("name", "Hype " + c["id"].title())
        c.setdefault("enabled", False); c.setdefault("genre", ""); c.setdefault("join_live", False)
        c["logo"] = "/panel/ch-%s.png" % c["id"]
        c["mount"] = c["id"] + ".mp3"; c["rec_mount"] = c["id"] + "-rec.mp3"; c["hls_dir"] = "/dev/shm/ch-" + c["id"]
    return chs


def save(chs):
    keep = [{k: c[k] for k in ("id", "name", "color", "enabled", "telnet", "genre", "join_live") if k in c} for c in chs]
    tmp = CFG + ".tmp"
    with open(tmp, "w") as f: json.dump(keep, f, indent=1)
    os.replace(tmp, CFG)


def get(ch):
    for c in load():
        if c["id"] == ch: return c
    return None


def valid(ch):
    return bool(ch) and ch != "main" and get(ch) is not None


def db_path(ch):
    if not ID_RE.match(ch or ""): raise ValueError("bad channel")
    return os.path.join(DATA, "chan_%s.db" % ch)


def fold(s):
    import unicodedata
    return "".join(x for x in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(x)).lower()


_INIT = set()
def connect(ch):
    """The channel's planning database, with the shared library attached read-only (view "items")."""
    import planner
    c = sqlite3.connect(db_path(ch), timeout=30)
    c.create_function("fold", 1, fold, deterministic=True)
    c.execute("ATTACH DATABASE ? AS lib", (LIB,))
    c.execute("CREATE TEMP VIEW IF NOT EXISTS items AS SELECT * FROM lib.items")
    if ch not in _INIT:
        try: c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError: pass
        planner.init(c)
        for f in ("init_overlays", "init_shows", "init_ads", "init_screen"):
            try: getattr(planner, f)(c)
            except Exception: pass
        init_pads(c, ch)
        c.execute("""CREATE TABLE IF NOT EXISTS asrun(id INTEGER PRIMARY KEY, started REAL, path TEXT, artist TEXT,
                     title TEXT, folder TEXT, mode TEXT, clip TEXT, scene TEXT, kind TEXT, over TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS asrun_started ON asrun(started)")
        c.execute("CREATE INDEX IF NOT EXISTS asrun_path ON asrun(path)")
        c.execute("CREATE TABLE IF NOT EXISTS aud_samples(t REAL PRIMARY KEY, r INT, p INT, tv INT)")
        c.commit(); _INIT.add(ch)
    return c


def init_pads(c, ch):
    """A theme channel's own pad banks: only its own jingles folders (jingles-<id>/…), never Hype Radio's shows.
    20 Nov: the first version copied all of Hype Radio's banks – cleared once (nothing was set up on the channels yet)."""
    import planner
    c.executescript("""CREATE TABLE IF NOT EXISTS pad_banks(id INTEGER PRIMARY KEY, name TEXT, pos INT);
                       CREATE TABLE IF NOT EXISTS pad_items(bank_id INT, pos INT, path TEXT, label TEXT, color TEXT);""")
    if planner.get_setting(c, "ch_pads_v2", False): return
    c.execute("DELETE FROM pad_items"); c.execute("DELETE FROM pad_banks")
    for i, (f,) in enumerate(c.execute("SELECT DISTINCT folder FROM items WHERE section='jingles' AND problem IS NULL AND station=? ORDER BY folder", (ch,)).fetchall()):
        bid = c.execute("INSERT INTO pad_banks(name, pos) VALUES(?,?)", (f.split("/")[-1], i)).lastrowid
        for j, (p_,) in enumerate(c.execute("SELECT path FROM items WHERE section='jingles' AND folder=? AND problem IS NULL AND station=? ORDER BY title", (f, ch)).fetchall()):
            c.execute("INSERT INTO pad_items VALUES(?,?,?,?,?)", (bid, j, p_, None, None))
    planner.set_setting(c, "ch_pads_v2", True); c.commit()


def attach_like(src, dst):
    """planner's hour preview copies the database into memory: give the copy the same library view."""
    try:
        names = [r[1] for r in src.execute("PRAGMA database_list")]
        if "lib" in names:
            dst.execute("ATTACH DATABASE ? AS lib", (LIB,))
            dst.execute("CREATE TEMP VIEW IF NOT EXISTS items AS SELECT * FROM lib.items")
            dst.create_function("fold", 1, fold, deterministic=True)
    except sqlite3.Error:
        pass
