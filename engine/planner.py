#!/usr/bin/env python3
"""Hype Studio planner: playlists, hour clocks and the week schedule.
- playlist  = folders / single songs / other playlists, with loop and shuffle; remembers where it stopped
- clock     = ordered slots: playlist|folder (amount, start), jingle folder, single song, ad break
- schedule  = week grid (day 0=Mon … 6=Sun, hour 0–23) -> clock"""
import os, re, json, time, random, sqlite3, logging
log = logging.getLogger("planner")

ROOT = "/srv/hype"
COLS = "path, artist, title, folder, mode, clip_path, duration, lufs, cue_start, cue_mix, cue_end, section, has_video"

def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS playlists(id INTEGER PRIMARY KEY, name TEXT UNIQUE, loop INT DEFAULT 1, shuffle INT DEFAULT 1, updated REAL);
    CREATE TABLE IF NOT EXISTS playlist_sources(playlist_id INT, pos INT, kind TEXT, ref TEXT);
    CREATE TABLE IF NOT EXISTS playlist_state(playlist_id INT PRIMARY KEY, ord TEXT, pos INT DEFAULT 0, rounds INT DEFAULT 0, updated REAL);
    CREATE TABLE IF NOT EXISTS clocks(id INTEGER PRIMARY KEY, name TEXT UNIQUE, color TEXT DEFAULT '#05A9C6', updated REAL);
    CREATE TABLE IF NOT EXISTS clock_slots(clock_id INT, pos INT, kind TEXT, ref TEXT, amount INT DEFAULT 1, start TEXT DEFAULT 'continue');
    CREATE TABLE IF NOT EXISTS schedule(day INT, hour INT, clock_id INT, show TEXT, PRIMARY KEY(day, hour));
    CREATE TABLE IF NOT EXISTS clock_state(clock_id INT PRIMARY KEY, i INT, left_ INT, first INT, updated REAL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    for tbl, col, typ in [("playlists", "rotate", "INT DEFAULT 0"), ("playlist_sources", "amount", "INT DEFAULT 1"), ("clocks", "resume", "INT DEFAULT 0")]:
        if col not in [r[1] for r in c.execute(f"PRAGMA table_info({tbl})")]: c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
    c.commit()

def get_setting(c, key, default=None):
    try:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default
    except Exception: return default

def set_setting(c, key, value):
    c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value))); c.commit()

# ---------------- smart picking + hitting the hour ----------------
CTX = {"at": None, "fit": None}     # set by the ClockRunner while it picks: planned start time, seconds left to the hour
_HIST = {"t": 0, "last": {}, "recent": {}}
def history(c):
    """When each song last played (and its plays of the last 27 h), cached for 20 s."""
    if time.time() - _HIST["t"] > 20:
        try:
            last = dict(c.execute("SELECT path, MAX(started) FROM asrun GROUP BY path").fetchall()); rec = {}
            for p_, t_ in c.execute("SELECT path, started FROM asrun WHERE started > ?", (time.time() - 27 * 3600,)): rec.setdefault(p_, []).append(t_)
        except sqlite3.OperationalError: last, rec = {}, {}
        _HIST.update(t=time.time(), last=last, recent=rec)
    return _HIST

def song_lens(c, paths):
    if not paths: return {}
    q = f"SELECT path, COALESCE(cue_mix, cue_end, duration, 180) - COALESCE(cue_start, 0) FROM items WHERE path IN ({','.join('?' * len(paths))})"
    return dict(c.execute(q, paths).fetchall())

def fit_score(length, fit):
    """Best: the song ends right at the hour or a few seconds after (the top-of-the-hour sweeper follows)."""
    return abs(length - fit) + (0 if length >= fit else 45)      # ending before :00 is avoided (the TOH never starts early)

def take_smart(c, songs, ok, fit):
    """Smart pick: the song that rested longest (artist/title separation via ok), not the same song as yesterday around this time."""
    H = history(c); at = CTX.get("at") or time.time(); y0 = at - 86400
    cand = [p_ for p_ in songs if ok(p_)] or songs[:]
    fresh = [p_ for p_ in cand if not any(abs(t_ - y0) < 1800 for t_ in H["recent"].get(p_, []))]
    cand = sorted(fresh or cand, key=lambda p_: H["last"].get(p_, 0))
    if fit:
        pool = cand[:max(6, len(cand) // 3)]; L = song_lens(c, pool)
        return min(pool, key=lambda p_: (fit_score(L.get(p_, 180), fit), H["last"].get(p_, 0)))
    return random.choice(cand[:3]) if cand else None

def resolve_file(c, name):
    """A file named in a playlist (e.g. from CLOSE), found by file name anywhere in the library (extension may differ)."""
    if not name: return None
    if "/" in name and c.execute("SELECT 1 FROM items WHERE path=? AND problem IS NULL", (name,)).fetchone(): return name
    fn = name.split("/")[-1].lower(); stem = os.path.splitext(fn)[0]
    r = c.execute("SELECT path FROM items WHERE lower(filename)=? AND problem IS NULL", (fn,)).fetchone() or \
        c.execute("SELECT path FROM items WHERE lower(filename) LIKE ? AND problem IS NULL ORDER BY section='music' DESC", (stem.replace("%", "") + ".%",)).fetchone()
    return r[0] if r else None

def folder_where(ref):
    """Folder match: the folder itself, its subfolders, and the same folder uploaded without its genre folder (AFRO/AFRO-POWER = AFRO-POWER)."""
    base = ref.split("/")[-1]; top = ref.split("/")[0]
    if "/" in ref and base.upper() != top.upper():
        return "(folder=? OR folder LIKE ? OR folder=? OR folder LIKE ?)", (ref, ref + "/%", base, "%/" + base)
    if "/" in ref: return "(folder=? OR folder LIKE ?)", (ref, ref + "/%")      # CHILL/CHILL: never the loose files in CHILL
    return "(folder=? OR folder LIKE ?)", (ref, ref + "/%")

# ---------------- playlists ----------------
def playlist_songs(c, pid, seen=None):
    """All songs of a playlist in their natural order (folders sorted by artist/title)."""
    seen = seen or set()
    if pid in seen: return []          # no endless nesting
    seen.add(pid); out = []
    for kind, ref in c.execute("SELECT kind, ref FROM playlist_sources WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall():
        if kind == "folder" and ref.startswith("jingles/"):     # a jingles folder (e.g. the New Music / Throwback sweepers): every file in it
            jf = ref[len("jingles/"):]
            out += [r[0] for r in c.execute("""SELECT path FROM items WHERE section='jingles' AND problem IS NULL AND (folder=? OR folder LIKE ?)
                                             ORDER BY filename COLLATE NOCASE""", (jf, jf + "/%")).fetchall()]
        elif kind == "folder":
            w, a = folder_where(ref)
            rows = c.execute(f"""SELECT path, folder FROM items WHERE section='music' AND problem IS NULL AND mode IS NOT NULL
                       AND {w} ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE""", a).fetchall()
            if any((f or "").startswith(ref + "/") for _, f in rows):     # a folder with subfolders: only the songs IN the subfolders
                rows = [r for r in rows if r[1] != ref]
            out += [r[0] for r in rows]
        elif kind == "file":
            p_ = resolve_file(c, ref)
            if p_: out.append(p_)
        elif kind == "song":
            if c.execute("SELECT 1 FROM items WHERE path=? AND problem IS NULL", (ref,)).fetchone(): out.append(ref)
        elif kind == "playlist":
            out += playlist_songs(c, int(ref), seen)
    seenp = set(); res = []
    for p in out:
        if p not in seenp: seenp.add(p); res.append(p)
    return res

def playlist_order(c, pid):
    """The current round's order (shuffled once per round if shuffle is on); new songs join, removed ones drop out."""
    r = c.execute("SELECT loop, shuffle FROM playlists WHERE id=?", (pid,)).fetchone()
    if not r: return [], 0, 0, False, False          # the playlist was deleted (a clock slot / event still names it): nothing to take
    loop, shuffle = r
    songs = playlist_songs(c, pid)
    st = c.execute("SELECT ord, pos, rounds FROM playlist_state WHERE playlist_id=?", (pid,)).fetchone()
    if st:
        o_ = json.loads(st[0]); o_ = o_ if isinstance(o_, list) else []
        ordl = [p for p in o_ if p in set(songs)]
        new = [p for p in songs if p not in set(ordl)]
        if shuffle: random.shuffle(new)
        ordl += new; pos = min(st[1], len(ordl)); rounds = st[2]
    else:
        ordl = songs[:]; pos = 0; rounds = 0
        if shuffle: random.shuffle(ordl)
    return ordl, pos, rounds, bool(loop), bool(shuffle)

def write_state(c, pid, ordj, pos, rounds):
    """Every playlist position change goes through here. While the engine plans an item, CTX['touched'] collects the
    old rows, so a planned item that is dropped again (replan, new hour, restart) can give its songs back (restore_states)."""
    t = CTX.get("touched")
    if t is not None and pid not in t:
        r = c.execute("SELECT ord, pos, rounds, updated FROM playlist_state WHERE playlist_id=?", (pid,)).fetchone()
        t[pid] = list(r) if r else None
    c.execute("INSERT OR REPLACE INTO playlist_state VALUES(?,?,?,?,?)", (pid, ordj, pos, rounds, time.time()))

def restore_states(c, snap):
    """Put playlist positions back to before a planned item was taken (snap = {pid: old row or None})."""
    for pid, row in snap.items():
        if row is None: c.execute("DELETE FROM playlist_state WHERE playlist_id=?", (int(pid),))
        else: c.execute("INSERT OR REPLACE INTO playlist_state VALUES(?,?,?,?,?)", (int(pid), *row))
    c.commit()

def save_state(c, pid, ordl, pos, rounds):
    write_state(c, pid, json.dumps(ordl), pos, rounds)

def playlist_len(c, pid):
    """Items in one round: a rotation = the sum of its source amounts; otherwise its songs."""
    r = c.execute("SELECT rotate FROM playlists WHERE id=?", (pid,)).fetchone()
    if r and r[0]:
        return sum(max(1, a or 1) for (a,) in c.execute("SELECT amount FROM playlist_sources WHERE playlist_id=?", (pid,)))
    return len(playlist_songs(c, pid))

def take_rotate(c, pid, start, ok, depth):
    """Rotation (like CLOSE POWER = AFRO, LAT, CARIB, DUTCH): each turn takes the next source's amount, then the next source.
    Randomize = new source order every round. Empty sources are skipped."""
    srcs = c.execute("SELECT kind, ref, amount FROM playlist_sources WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall()
    if not srcs: return None
    shuffle = c.execute("SELECT shuffle FROM playlists WHERE id=?", (pid,)).fetchone()[0]
    st = c.execute("SELECT ord, pos, rounds FROM playlist_state WHERE playlist_id=?", (pid,)).fetchone()
    try: d = json.loads(st[0]) if st else {}
    except Exception: d = {}
    order = d.get("order") if isinstance(d, dict) else None; k = d.get("k", 0) if isinstance(d, dict) else 0
    pos, rounds = (st[1], st[2]) if st else (0, 0)
    def new_order(prev=None):
        o = list(range(len(srcs)))
        if shuffle:
            random.shuffle(o)
            if prev and len(o) > 1:
                # the sources that came at the end of the last round go to the second half of the new one, so the
                # same source never comes back right away (A B C D | B A D C, never ... C D | D ...)
                h = len(o) // 2; late = set(prev[-h:]) if h else set()
                o = [x for x in o if x not in late] + [x for x in o if x in late]
        return o
    if not order or sorted(order) != list(range(len(srcs))): order = new_order(); pos = 0; k = 0
    if start == "start": order = new_order(); pos = 0; k = 0
    elif start == "random": pos = random.randrange(len(order)); k = 0
    path = None
    for _ in range(len(srcs) + 1):
        if pos >= len(order): pos = 0; k = 0; rounds += 1; order = new_order(order)
        kind, ref, amt = srcs[order[pos]]
        path = take_source(c, kind, ref, ok, depth)
        k += 1
        if k >= max(1, amt or 1) or not path: pos += 1; k = 0
        if path: break
    if pos >= len(order): pos = 0; k = 0; rounds += 1; order = new_order(order)
    write_state(c, pid, json.dumps({"order": order, "k": k}), pos, rounds)
    return path

def take_source(c, kind, ref, ok, depth=0):
    if kind == "folder": return take_from_playlist(c, folder_playlist_id(c, ref), "continue", ok, depth + 1)
    if kind == "playlist": return take_from_playlist(c, int(ref), "continue", ok, depth + 1) if depth < 6 else None
    if kind == "song": return ref if c.execute("SELECT 1 FROM items WHERE path=? AND problem IS NULL", (ref,)).fetchone() else None
    if kind == "file": return resolve_file(c, ref)
    return None

def take_from_playlist(c, pid, start, ok, depth=0):
    r = c.execute("SELECT rotate FROM playlists WHERE id=?", (pid,)).fetchone()
    if r and r[0]: return take_rotate(c, pid, start, ok, depth)
    return take_merged(c, pid, start, ok)

def take_merged(c, pid, start, ok):
    """Next song of a playlist. start: 'start' | 'continue' | 'random' (only for the first song of a slot).
    ok(path) -> bool checks separation; we look ahead up to 12 songs and swap the first one that fits."""
    ordl, pos, rounds, loop, shuffle = playlist_order(c, pid)
    if not ordl: return None
    fit = CTX.get("fit")
    if shuffle and start != "start" and get_setting(c, "smart_pick", True):
        return take_smart(c, ordl, ok, fit)
    if start == "start": pos = 0; rounds += 1; ordl = reshuffle(ordl, shuffle) if shuffle else ordl
    elif start == "random": pos = random.randrange(len(ordl))
    if pos >= len(ordl):
        if not loop: save_state(c, pid, ordl, pos, rounds); return None
        pos = 0; rounds += 1
        if shuffle: ordl = reshuffle(ordl, True)
    pick = None
    okk = [i for i in range(pos, min(len(ordl), pos + 12)) if ok(ordl[i])]
    if okk and fit and shuffle:
        L = song_lens(c, [ordl[i] for i in okk]); pick = min(okk, key=lambda i: fit_score(L.get(ordl[i], 180), fit))
    elif okk: pick = okk[0]
    if pick is None: pick = pos
    if pick != pos: ordl[pos], ordl[pick] = ordl[pick], ordl[pos]
    path = ordl[pos]; save_state(c, pid, ordl, pos + 1, rounds)
    return path

def reshuffle(ordl, shuffle):
    """New round: shuffle again, but don't start with one of the last 3 songs of the previous round."""
    last = ordl[-3:]; new = ordl[:]; random.shuffle(new)
    for _ in range(5):
        if new[0] not in last: break
        random.shuffle(new)
    return new

def folder_playlist_id(c, folder):
    """A folder used directly in a clock behaves like a shuffled, looping playlist (hidden, named '@folder')."""
    name = "@" + folder
    r = c.execute("SELECT id FROM playlists WHERE name=?", (name,)).fetchone()
    if r: return r[0]
    cur = c.execute("INSERT INTO playlists(name, loop, shuffle, updated) VALUES(?,1,1,?)", (name, time.time()))
    c.execute("INSERT INTO playlist_sources(playlist_id, pos, kind, ref, amount) VALUES(?,?,?,?,?)", (cur.lastrowid, 0, "folder", folder, 1)); c.commit()
    return cur.lastrowid

# ---------------- clocks + schedule ----------------
def clock_for(c, t):
    lt = time.localtime(t)
    r = c.execute("SELECT clock_id FROM schedule WHERE day=? AND hour=?", (lt.tm_wday, lt.tm_hour)).fetchone()
    if r and r[0]: return r[0]
    r = c.execute("SELECT id FROM clocks ORDER BY id LIMIT 1").fetchone()
    return r[0] if r else None

def slots(c, cid):
    return c.execute("SELECT kind, ref, amount, start FROM clock_slots WHERE clock_id=? ORDER BY pos", (cid,)).fetchall()

class ClockRunner:
    """Walks through the clock of the time each planned item will start in.
    Hour clocks start at the top every hour; 'resume' clocks (CLOSE show clocks) go on where they left off."""
    def __init__(self, restore=None):
        self.key = None; self.st = {}; self.snap = None; self.jrun = 0; self.force_cid = None; self.skipped = []
        self.pos = None          # (run key, clock, slot, left, first) after the last item this runner gave out
        self.restore = restore   # the same, from the last item that really went on air (engine restart: go on from there)
        self.mix_key = None    # the clock run (hour/show) that already has its mixtape: one mix per run, the clock never goes round for a 2nd
    def _state(self, c, cid, resume):
        if cid not in self.st:
            r = c.execute("SELECT i, left_, first FROM clock_state WHERE clock_id=?", (cid,)).fetchone() if resume else None
            self.st[cid] = [r[0], r[1], bool(r[2])] if r else [0, 0, True]
        return self.st[cid]
    def next(self, c, at, ok, pick_jingle):
        key = runner_key(at)
        if self.force_cid and getattr(self, "force_until", 0) and at >= self.force_until: self.force_cid = None   # "run now" ends with the hour
        cid = self.force_cid or clock_for(c, at)
        if cid is None: return None
        r_ = c.execute("SELECT resume FROM clocks WHERE id=?", (cid,)).fetchone()
        resume = bool(r_ and r_[0])   # show clocks (like CLOSE) go on where they left off; hour clocks start at the top
        if key != self.key:
            self.key = key
            if not resume:
                rs, self.restore = self.restore, None
                n_sl = len(slots(c, cid))
                if rs and list(rs[0]) == list(key) and rs[1] == cid and 0 <= rs[2] <= n_sl:
                    self.st[cid] = [rs[2], rs[3], bool(rs[4])]     # same hour, same clock: on from where the radio was (not slot 1 again)
                else: self.st[cid] = [0, 0, True]
            elif cid in self.st and self.st[cid][1] >= 10**6: self.st[cid][1] = 0; self.st[cid][0] += 1   # a "fill" slot ends with the hour
        st = self._state(c, cid, resume)
        sl = slots(c, cid)
        if not sl: return None
        for _ in range(len(sl) * 3):
            if st[0] >= len(sl):
                if self.mix_key == key: return None          # this hour already had its mix: no 2nd round (the engine fills with songs)
                st[0] = 0                                    # clock done: run it again
            kind, ref, amount, start = sl[st[0]]
            if st[1] == 0:
                if amount and amount >= 999: st[1] = 10**6      # fill until the hour / show ends
                elif amount and amount > 0: st[1] = amount
                elif kind == "playlist" and str(ref).isdigit(): st[1] = max(1, playlist_len(c, int(ref)))   # all = one round
                elif kind == "folder": st[1] = max(1, len(playlist_songs(c, folder_playlist_id(c, ref))))
                else: st[1] = 1
                st[2] = True
            path = None
            try: path = self._take(c, at, ok, pick_jingle, cid, st, key, kind, ref, start)
            except Exception as e: log.warning("clock %s slot %d (%s %s): %s – skipped", cid, st[0] + 1, kind, ref, e); path = None   # a broken slot never stops the radio
            if path == "_SKIP_": continue
            if not path: self.skipped = (self.skipped + [(kind, ref)])[-60:]
            if path and not c.execute("SELECT 1 FROM items WHERE path=? AND section='music'", (path,)).fetchone():
                if self.jrun >= 2: path = None                   # never more than 2 jingles in a row (e.g. music folders still empty)
            if path:
                self.jrun = 0 if c.execute("SELECT 1 FROM items WHERE path=? AND section='music'", (path,)).fetchone() else self.jrun + 1
                st[1] -= 1
                if st[1] <= 0 or kind in ("song", "file", "jingle", "id", "showjingle"): st[0] += 1; st[1] = 0
                self.snap = (cid, st[0], st[1], st[2]) if resume else None
                self.pos = (list(key), cid, st[0], st[1], st[2])
                c.commit(); return path
            st[0] += 1; st[1] = 0                                # slot empty / playlist finished: next slot
        return None
    def _take(self, c, at, ok, pick_jingle, cid, st, key, kind, ref, start):
        """The item for one slot (None = nothing; "_SKIP_" = mixtape already played this hour: slot passed)."""
        path = None
        if kind in ("playlist", "folder"):
            pid = int(ref) if kind == "playlist" else folder_playlist_id(c, ref)
            left = (at // 3600 + 1) * 3600 - at
            CTX.update(at=at, fit=left if get_setting(c, "hit_hour", True) and 45 <= left <= 330 else None)
            try: path = take_from_playlist(c, pid, start if st[2] else "continue", ok)
            finally: CTX.update(at=None, fit=None)
        elif kind == "showjingle":                         # sweeper of the show on air (jingles/SHOWS/<show>), else the fallback folder
            s_ = show_at(at)
            path = (pick_jingle(c, show_folder(c, at, s_)) if s_ else None) or pick_jingle(c, ref or "SWEEPERS")
        elif kind == "song":
            path = ref
        elif kind == "file":
            path = resolve_file(c, ref)
        elif kind in ("jingle", "id"):
            path = pick_jingle(c, ref)
        elif kind == "adbreak":          # one sponsor spot per call; the slot's number = spots in this break
            brands = self.ad_brands if getattr(self, "ad_slot", None) == (cid, st[0]) else set()
            self.ad_slot = (cid, st[0]); self.ad_brands = brands
            pl = CTX.get("ad_planned") if CTX.get("ad_planned") is not None else {}
            got = pick_ads(c, at, 1, pl, brands)
            path = got[0] if got else None
            if path:
                r_b = c.execute("SELECT brand FROM ad_spots WHERE path=?", (path,)).fetchone()
                if r_b and r_b[0]: brands.add(r_b[0].strip().lower())
        st[2] = False
        if path and kind in ("playlist", "folder") and c.execute("SELECT 1 FROM items WHERE path=? AND folder LIKE 'MIXES%'", (path,)).fetchone():
            if self.mix_key == key: st[0] += 1; st[1] = 0; return "_SKIP_"   # only 1 mixtape per hour
            self.mix_key = key
        return path

def save_clock_state(c, cid, i, left, first):
    c.execute("INSERT OR REPLACE INTO clock_state VALUES(?,?,?,?,?)", (cid, i, left, int(bool(first)), time.time())); c.commit()

def default_setup(c):
    """First run: a test clock built from the folders that exist, scheduled on every hour."""
    if c.execute("SELECT COUNT(*) FROM clocks").fetchone()[0]: return
    folders = [r[0] for r in c.execute("SELECT DISTINCT folder FROM items WHERE section='music' AND folder<>'' ORDER BY folder")]
    cur = c.execute("INSERT INTO clocks(name, color, updated) VALUES('Test clock', '#05A9C6', ?)", (time.time(),)); cid = cur.lastrowid
    sl = [("id", "IDS", 1, "continue")]
    for i, f in enumerate(folders):
        sl.append(("folder", f, 2 if f.upper().startswith("CARIB") else 1, "continue"))
        if i % 3 == 2: sl.append(("jingle", "SWEEPERS", 1, "continue"))
    for i, s in enumerate(sl): c.execute("INSERT INTO clock_slots VALUES(?,?,?,?,?,?)", (cid, i, *s))
    for d in range(7):
        for h in range(24): c.execute("INSERT OR REPLACE INTO schedule VALUES(?,?,?,?)", (d, h, cid, ""))
    c.commit()

# ---------------- API helpers ----------------
def api_playlists(c):
    out = []
    for pid, name, loop, shuffle, rot in c.execute("SELECT id, name, loop, shuffle, rotate FROM playlists WHERE name NOT LIKE '@%' ORDER BY name COLLATE NOCASE").fetchall():
        src = [dict(kind=k, ref=r, amount=a or 1) for k, r, a in c.execute("SELECT kind, ref, amount FROM playlist_sources WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall()]
        for s_ in src:
            if s_["kind"] == "file": s_["found"] = bool(resolve_file(c, s_["ref"]))
        songs = playlist_songs(c, pid); st = c.execute("SELECT pos, rounds FROM playlist_state WHERE playlist_id=?", (pid,)).fetchone()
        out.append(dict(id=pid, name=name, loop=bool(loop), shuffle=bool(shuffle), rotate=bool(rot), sources=src, count=len(songs), pos=st[0] if st else 0))
    return out

def api_save_playlist(c, d):
    name = (d.get("name") or "").strip()
    if not name or name.startswith("@"): raise ValueError("give the playlist a name")
    if d.get("id"):
        pid = int(d["id"]); c.execute("UPDATE playlists SET name=?, loop=?, shuffle=?, rotate=?, updated=? WHERE id=?", (name, int(bool(d.get("loop"))), int(bool(d.get("shuffle"))), int(bool(d.get("rotate"))), time.time(), pid))
    else:
        pid = c.execute("INSERT INTO playlists(name, loop, shuffle, rotate, updated) VALUES(?,?,?,?,?)", (name, int(bool(d.get("loop", True))), int(bool(d.get("shuffle", True))), int(bool(d.get("rotate"))), time.time())).lastrowid
    c.execute("DELETE FROM playlist_sources WHERE playlist_id=?", (pid,))
    for i, s in enumerate(d.get("sources", [])):
        c.execute("INSERT INTO playlist_sources(playlist_id, pos, kind, ref, amount) VALUES(?,?,?,?,?)", (pid, i, s["kind"], str(s["ref"]), max(1, int(s.get("amount") or 1))))
    c.commit(); return pid

def api_preview_playlist(c, pid):
    """What really plays next: a rotation shows its turns (which source), a randomized playlist with Smart pick shows the
    songs that rested longest first (that is how it picks), an ordered playlist shows its order + position."""
    rot = c.execute("SELECT rotate, shuffle FROM playlists WHERE id=?", (pid,)).fetchone()
    if rot and rot[0]:
        srcs = c.execute("SELECT kind, ref, amount FROM playlist_sources WHERE playlist_id=? ORDER BY pos", (pid,)).fetchall()
        st = c.execute("SELECT ord, pos, rounds FROM playlist_state WHERE playlist_id=?", (pid,)).fetchone()
        try: d = json.loads(st[0]) if st else {}
        except Exception: d = {}
        order = d.get("order") if isinstance(d, dict) and sorted(d.get("order") or []) == list(range(len(srcs))) else list(range(len(srcs)))
        pos = st[1] if st else 0; k = d.get("k", 0) if isinstance(d, dict) else 0
        name = lambda s: (s[1] if s[0] != "playlist" else ((c.execute("SELECT name FROM playlists WHERE id=?", (int(s[1]),)).fetchone() or ["?"])[0]))
        turns = []
        for i, o in enumerate(order):
            if i < pos: continue
            n = max(1, srcs[o][2] or 1) - (k if i == pos else 0)
            turns += [dict(path="", artist="", title="next from " + name(srcs[o]), folder="")] * max(0, n)
        return dict(count=len(turns), pos=0, rounds=st[2] if st else 0, rotation=True, random_next=bool(rot[1]), songs=turns)
    ordl, pos, rounds, loop, shuffle = playlist_order(c, pid)
    if shuffle and get_setting(c, "smart_pick", True):
        H = history(c); ordl = sorted(ordl, key=lambda p_: H["last"].get(p_, 0)); pos = 0
    rows = {r[0]: r for r in c.execute(f"SELECT path, artist, title, folder FROM items WHERE path IN ({','.join('?'*len(ordl[:200]))})", ordl[:200])} if ordl else {}
    return dict(count=len(ordl), pos=pos, rounds=rounds, smart=bool(shuffle and get_setting(c, "smart_pick", True)), songs=[dict(path=p, artist=rows[p][1], title=rows[p][2], folder=rows[p][3]) for p in ordl[:200] if p in rows])

def api_clocks(c):
    out = []
    for cid, name, color, resume in c.execute("SELECT id, name, color, resume FROM clocks ORDER BY name COLLATE NOCASE").fetchall():
        out.append(dict(id=cid, name=name, color=color, resume=bool(resume), slots=[dict(kind=k, ref=r, amount=a, start=s) for k, r, a, s in slots(c, cid)]))
    return out

def api_save_clock(c, d):
    name = (d.get("name") or "").strip()
    if not name: raise ValueError("give the clock a name")
    if d.get("id"):
        cid = int(d["id"]); c.execute("UPDATE clocks SET name=?, color=?, resume=?, updated=? WHERE id=?", (name, d.get("color", "#05A9C6"), int(bool(d.get("resume"))), time.time(), cid))
    else:
        cid = c.execute("INSERT INTO clocks(name, color, resume, updated) VALUES(?,?,?,?)", (name, d.get("color", "#05A9C6"), int(bool(d.get("resume"))), time.time())).lastrowid
    c.execute("DELETE FROM clock_slots WHERE clock_id=?", (cid,))
    i = 0
    for s in d.get("slots", []):
        ref = str(s.get("ref", ""))
        if s.get("kind") == "playlist" and not (ref.isdigit() and c.execute("SELECT 1 FROM playlists WHERE id=?", (int(ref),)).fetchone()):
            log.warning("clock %s: playlist slot without a (known) playlist (%r) left out", name, ref); continue   # it could only fail on air
        c.execute("INSERT INTO clock_slots VALUES(?,?,?,?,?,?)", (cid, i, s["kind"], ref, int(s.get("amount") or 0), s.get("start", "continue"))); i += 1
    c.commit(); return cid

def api_schedule(c):
    return [dict(day=d, hour=h, clock_id=cid, show=sh) for d, h, cid, sh in c.execute("SELECT day, hour, clock_id, show FROM schedule")]

def api_set_schedule(c, d):
    for cell in d.get("cells", []):
        c.execute("INSERT OR REPLACE INTO schedule VALUES(?,?,?,?)", (int(cell["day"]), int(cell["hour"]), cell.get("clock_id"), cell.get("show", "")))
    c.commit()

def api_folders(c):
    music = [r[0] for r in c.execute("SELECT DISTINCT folder FROM items WHERE section='music' AND folder<>'' ORDER BY folder")]
    all_levels = sorted({"/".join(f.split("/")[:i]) for f in music for i in range(1, len(f.split("/")) + 1)})
    jingles = [r[0] for r in c.execute("SELECT DISTINCT folder FROM items WHERE section='jingles' AND folder<>'' ORDER BY folder")]
    counts = dict(c.execute("SELECT folder, COUNT(*) FROM items WHERE section='music' GROUP BY folder").fetchall())
    cnt = {f: sum(n for k, n in counts.items() if k == f or k.startswith(f + "/")) for f in all_levels}
    return dict(music=all_levels, counts=cnt, jingles=jingles)

# ---------------- intro sweepers (sweeper over the start of a song, music ducked) ----------------
def init_overlays(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS overlay_rules(id INTEGER PRIMARY KEY, name TEXT, enabled INT DEFAULT 1,
        match_kind TEXT, match_ref TEXT, jingle_ref TEXT, every INT DEFAULT 1, offset REAL DEFAULT 0.5,
        duck REAL DEFAULT 0.35, min_gap INT DEFAULT 8, pos INT DEFAULT 0);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(items)")]
    for col, typ in [("overlay_path", "TEXT"), ("overlay_at", "REAL"), ("overlay_duck", "REAL"), ("overlay_mode", "TEXT")]:
        if col not in cols: c.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
    c.commit()

def in_playlist(c, pid, path):
    return path in set(playlist_songs(c, int(pid)))

def rule_matches(c, rule, it):
    kind, ref = rule["match_kind"], rule["match_ref"] or ""
    f = it.get("folder") or ""
    if kind == "folder" and "*" in ref:                # wildcard: *CLASSICS* = every folder with CLASSICS in its name
        import fnmatch
        return fnmatch.fnmatch(f.upper(), ref.upper())
    if kind == "folder":
        base = ref.split("/")[-1]
        return f == ref or f.startswith(ref + "/") or ("/" in ref and (f == base or f.endswith("/" + base)))
    if kind == "playlist": return in_playlist(c, ref, it["path"])
    if kind == "song": return it.get("path") == ref
    if kind == "any": return True
    return False

def api_overlays(c):
    k = ["id", "name", "enabled", "match_kind", "match_ref", "jingle_ref", "every", "offset", "duck", "min_gap"]
    return [dict(zip(k, r)) for r in c.execute(f"SELECT {','.join(k)} FROM overlay_rules ORDER BY pos, id")]

def api_save_overlays(c, d):
    c.execute("DELETE FROM overlay_rules")
    for i, r in enumerate(d.get("rules", [])):
        c.execute("""INSERT INTO overlay_rules(name, enabled, match_kind, match_ref, jingle_ref, every, offset, duck, min_gap, pos)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""", (r.get("name") or "", int(bool(r.get("enabled", True))), r["match_kind"], str(r.get("match_ref") or ""),
                     r["jingle_ref"], max(1, int(r.get("every") or 1)), float(r.get("offset") or 0), float(r.get("duck") or 0.35), int(r.get("min_gap") or 0), i))
    c.commit()

def resolve_jingle(c, ref, avoid=()):
    """ref = a jingle file (jingles/...), a jingles folder name, or #SHOW (sweepers of the show on air)."""
    if ref == "#SHOW":
        s_ = show_at(time.time())
        r_ = resolve_jingle(c, show_folder(c, time.time(), s_), avoid) if s_ else None
        return r_ or resolve_jingle(c, "SWEEPERS", avoid)
    if ref.startswith("jingles/"):
        return ref if c.execute("SELECT 1 FROM items WHERE path=? AND problem IS NULL", (ref,)).fetchone() else None
    rows = [r[0] for r in c.execute("SELECT path FROM items WHERE section='jingles' AND problem IS NULL AND (folder=? OR folder LIKE ?)", (ref, ref + "/%"))]
    fresh = [p for p in rows if p not in avoid] or rows
    return random.choice(fresh) if fresh else None

def song_overlay_path(c, overlay_path):
    """Per-song sweeper; a CLOSE path (D:\\...\\NUMBER 1.MP3) is found by file name in /jingles."""
    if not overlay_path: return None
    if overlay_path.startswith("jingles/"): return overlay_path
    fn = re.split(r"[\\\\/]", overlay_path)[-1].lower()
    r = c.execute("SELECT path FROM items WHERE section='jingles' AND lower(filename)=?", (fn,)).fetchone()
    return r[0] if r else None

# ---------------- shows (from hyperadio.nl) + fixed times ----------------
WEEK = []   # [{day 0=Mon, start "HH:MM", end "HH:MM", title}] – filled by the engine from the website

def init_shows(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS show_clocks(title TEXT PRIMARY KEY, clock_id INT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, name TEXT, enabled INT DEFAULT 1, days TEXT DEFAULT '0123456',
        at TEXT, kind TEXT, ref TEXT, amount INT DEFAULT 1, hard INT DEFAULT 0, pos INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events_done(event_id INT, day TEXT, PRIMARY KEY(event_id, day));
    """); c.commit()

def mins(hhmm):
    h, m = hhmm.split(":"); return int(h) * 60 + int(m)

def show_folder(c, t, s_):
    """The jingles folder of the show on air: SHOWS/<show>. When the clock of that hour is a WEEKEND clock (e.g. NON-STOP
    WEEKEND) and a folder "SHOWS/<show> Weekend" has jingles, that one (its own IDs and sweepers)."""
    base = "SHOWS/" + s_["title"]
    try:
        cid = clock_for(c, t); r = c.execute("SELECT name FROM clocks WHERE id=?", (cid,)).fetchone() if cid else None
        if r and "WEEKEND" in (r[0] or "").upper() and c.execute("SELECT 1 FROM items WHERE section='jingles' AND problem IS NULL AND folder=?", (base + " Weekend",)).fetchone():
            return base + " Weekend"
    except Exception: pass
    return base

def show_at(t):
    """The website show on air at time t (handles 23:00–00:00 style ends)."""
    lt = time.localtime(t); m = lt.tm_hour * 60 + lt.tm_min
    for s in WEEK:
        if s["day"] != lt.tm_wday: continue
        a, b = mins(s["start"]), mins(s["end"]) or 1440
        if b <= a: b = 1440
        if a <= m < b: return s
    return None

_old_clock_for = clock_for
def clock_for(c, t):
    """1) the clock chosen for the website show at that time, 2) the hour grid, 3) the first clock."""
    s = show_at(t)
    if s:
        r = c.execute("SELECT clock_id FROM show_clocks WHERE title=?", (s["title"],)).fetchone()
        if r and r[0] and c.execute("SELECT 1 FROM clocks WHERE id=?", (r[0],)).fetchone(): return r[0]
    return _old_clock_for(c, t)

def repeated_hour(t):
    """True in the 2nd run of the hour that repeats when the clocks go back (e.g. the 2nd 02:00-03:00)."""
    a, b = time.localtime(t), time.localtime(t - 3600)
    return a.tm_hour == b.tm_hour and a.tm_yday == b.tm_yday

def runner_key(t):
    """A new clock run starts every hour and at every show change (e.g. 20:30). (The repeated hour when the clocks go back counts as its own hour.)"""
    lt = time.localtime(t); s = show_at(t)
    return (lt.tm_yday, lt.tm_hour + (100 if repeated_hour(t) else 0), s["title"] if s else None, s["start"] if s else None)

def api_shows(c):
    sc = dict(c.execute("SELECT title, clock_id FROM show_clocks").fetchall())
    titles = []
    for s in WEEK:
        if s["title"] not in titles: titles.append(s["title"])
    days = "Mon Tue Wed Thu Fri Sat Sun".split()
    out = []
    for t in titles:
        when = [f'{days[s["day"]]} {s["start"]}–{s["end"]}' for s in WEEK if s["title"] == t]
        out.append(dict(title=t, clock_id=sc.get(t), when=when))
    return dict(shows=out, week=WEEK)

def api_save_show_clocks(c, d):
    for title, cid in d.get("map", {}).items():
        c.execute("INSERT OR REPLACE INTO show_clocks VALUES(?,?)", (title, int(cid) if cid else None))
    c.commit()

def api_events(c):
    k = ["id", "name", "enabled", "days", "at", "kind", "ref", "amount", "hard"]
    return [dict(zip(k, r)) for r in c.execute(f"SELECT {','.join(k)} FROM events ORDER BY at, pos")]

def api_save_events(c, d):
    keep = []
    for i, e in enumerate(d.get("events", [])):
        at = (e.get("at") or "").strip()
        if not re.match(r"^(\d{1,2}|\*):\d{2}$", at): raise ValueError("time must be like 20:30 (or *:00 = every hour)")
        vals = (e.get("name") or "", int(bool(e.get("enabled", True))), e.get("days") or "0123456", at if at.startswith("*") else at.zfill(5), e["kind"], str(e.get("ref") or ""), int(e.get("amount") or 1), int(bool(e.get("hard"))), i)
        if e.get("id"):
            c.execute("UPDATE events SET name=?, enabled=?, days=?, at=?, kind=?, ref=?, amount=?, hard=?, pos=? WHERE id=?", vals + (int(e["id"]),)); keep.append(int(e["id"]))
        else:
            keep.append(c.execute("INSERT INTO events(name, enabled, days, at, kind, ref, amount, hard, pos) VALUES(?,?,?,?,?,?,?,?,?)", vals).lastrowid)
    if keep: c.execute(f"DELETE FROM events WHERE id NOT IN ({','.join('?'*len(keep))})", keep)
    else: c.execute("DELETE FROM events")
    c.commit()

def due_events(c, now, window_end):
    """Enabled events of today whose time falls between now-10 min and window_end, not done today.
    A fixed-time event of tomorrow (e.g. 00:00) is seen from late tonight, so it starts exactly at its time.
    (The hour that repeats when the clocks go back has its own key "… 02 b": its hourly events run again.)"""
    lt = time.localtime(now); day = time.strftime("%Y-%m-%d", lt)
    out = []
    H0 = int(now // 3600) * 3600       # hourly events: counted in real hours, so the repeated hour when the clocks go back gets its own run
    for e in api_events(c):
        if not e["enabled"]: continue
        hh, m = e["at"].split(":"); m = int(m)
        if hh == "*": ts = [H0 + m * 60, H0 + 3600 + m * 60]
        else: ts = [time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + d, int(hh), m, 0, 0, 0, -1)) for d in (0, 1)]   # (+ tomorrow: a 00:00 event is seen from 23:xx)
        for t in ts:
            tl = time.localtime(t)
            if str(tl.tm_wday) not in e["days"]: continue
            key = time.strftime("%Y-%m-%d %H", tl) + (" b" if repeated_hour(t) else "") if hh == "*" else time.strftime("%Y-%m-%d", tl)
            if now - 600 <= t <= window_end and not c.execute("SELECT 1 FROM events_done WHERE event_id=? AND day=?", (e["id"], key)).fetchone():
                out.append(dict(e, t=t, day=key))
    return out


# ---------------- hour preview (a dry run on a copy of the library) ----------------
def preview_hour(c, day=None, hour=None, clock=None):
    """What an hour will sound like + warnings. Runs on an in-memory copy, so nothing on air changes."""
    mem = sqlite3.connect(":memory:"); c.backup(mem)
    now = time.time(); start = (int(now) // 3600 + 1) * 3600
    if day is not None and hour is not None:
        t = (int(now) // 3600) * 3600
        for _ in range(24 * 8):
            lt = time.localtime(t)
            if lt.tm_wday == day and lt.tm_hour == hour and t >= now - 3600: start = t; break
            t += 3600
    r = ClockRunner(); r.force_cid = clock
    cid = clock or clock_for(mem, start)
    cname = (mem.execute("SELECT name FROM clocks WHERE id=?", (cid,)).fetchone() or ["no clock"])[0]
    recent, arts = [], []
    def akey(a):
        o = [x.strip().lower() for x in re.split(r"\s+(?:x|ft\.?|feat\.?|featuring|vs\.?|&|en|,)\s+|,", a or "", flags=re.I) if x.strip()]
        return [x for x in o if x not in ("club hype", "hype radio", "hype radio tv", "hype")] or o
    def ok(p_):
        if p_ in recent[-40:]: return False
        a = mem.execute("SELECT artist FROM items WHERE path=?", (p_,)).fetchone()
        return not (a and set(akey(a[0])) & set(arts[-12:]))
    def pj(c_, folder):
        rows = [x[0] for x in c_.execute("SELECT path FROM items WHERE section IN ('jingles','ads') AND problem IS NULL AND (folder=? OR folder LIKE ? OR ?='ADS')", (folder, folder + "/%", folder))]
        rows = [x for x in rows if x not in recent[-20:]] or rows
        return random.choice(rows) if rows else None
    t = start; items = []; warn = []
    # fixed times of this hour (top of the hour, Top 10 …) – they play after the song that is on air then
    lt0 = time.localtime(start); evs = []
    for e in api_events(mem):
        if not e["enabled"] or str(lt0.tm_wday) not in e["days"]: continue
        hh, mm = e["at"].split(":")
        if hh == "*" or int(hh) == lt0.tm_hour: evs.append((start + int(mm) * 60, e))
    evs.sort(key=lambda x: x[0]); queue = []
    while t < start + 3600 and len(items) < 60:
        while evs and evs[0][0] <= t + 1:
            et, e = evs.pop(0)
            if e["kind"] == "jingle": queue += [resolve_jingle(mem, e["ref"])]
            elif e["kind"] == "song": queue += [e["ref"]]
            elif e["kind"] in ("playlist", "folder"):
                pid = int(e["ref"]) if e["kind"] == "playlist" else folder_playlist_id(mem, e["ref"])
                n_ = e["amount"] if e["amount"] and e["amount"] > 0 else len(playlist_songs(mem, pid))
                queue += [take_from_playlist(mem, pid, "start" if i == 0 else "continue", lambda x: True) for i in range(n_)]
            queue = [x for x in queue if x]
        p_ = queue.pop(0) if queue else r.next(mem, t, ok, pj)
        if not p_: warn.append("Nothing to play from %s on – the clock's slots are empty." % time.strftime("%H:%M", time.localtime(t))); break
        row = mem.execute("SELECT artist, title, folder, section, COALESCE(cue_mix, cue_end, duration, 180) - COALESCE(cue_start, 0), has_video, mode FROM items WHERE path=?", (p_,)).fetchone()
        if not row: break
        ln = max(3.0, row[4] or 180); recent.append(p_); arts.extend(akey(row[0]))
        items.append(dict(t=t, artist=row[0], title=row[1], folder=row[2], section=row[3], length=round(ln), mode=row[6]))
        t += ln
    songs = sum(1 for x in items if x["section"] == "music"); jingles = len(items) - songs
    over = round(t - (start + 3600))
    miss = []
    for k, ref in r.skipped:
        lbl = {"folder": "folder " + ref, "file": "file " + ref, "song": "song " + ref}.get(k)
        if k == "playlist":
            n_ = mem.execute("SELECT name FROM playlists WHERE id=?", (int(ref),)).fetchone() if str(ref).isdigit() else None
            lbl = "playlist " + (n_[0] if n_ else str(ref))
        if k in ("jingle", "id", "showjingle"): lbl = "jingles " + (ref or "")
        if lbl and lbl not in miss: miss.append(lbl)
    if miss: warn.append("Empty, so skipped: " + ", ".join(miss[:8]) + (" …" if len(miss) > 8 else ""))
    if songs < 10: warn.append(f"Only {songs} songs in this hour.")
    if jingles > 8: warn.append(f"{jingles} jingles in one hour – a lot.")
    if over > 90: warn.append(f"The last song runs {over // 60}:{over % 60:02d} into the next hour.")
    return dict(start=start, clock=cname, items=items, songs=songs, jingles=jingles, over=over, warnings=warn,
                smart=bool(get_setting(c, "smart_pick", True)), hit_hour=bool(get_setting(c, "hit_hour", True)))


# ---------------- pad banks (Gianno's own pages of pads) ----------------
def init_pads(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pad_banks(id INTEGER PRIMARY KEY, name TEXT, pos INT);
    CREATE TABLE IF NOT EXISTS pad_items(bank_id INT, pos INT, path TEXT, label TEXT, color TEXT);
    """); c.commit()
    if not c.execute("SELECT COUNT(*) FROM pad_banks").fetchone()[0]:   # first time: one bank per jingles folder
        for i, (f,) in enumerate(c.execute("SELECT DISTINCT folder FROM items WHERE section='jingles' AND problem IS NULL ORDER BY folder").fetchall()):
            bid = c.execute("INSERT INTO pad_banks(name, pos) VALUES(?,?)", (f.split('/')[-1], i)).lastrowid
            for j, (p_,) in enumerate(c.execute("SELECT path FROM items WHERE section='jingles' AND folder=? AND problem IS NULL ORDER BY title", (f,)).fetchall()):
                c.execute("INSERT INTO pad_items VALUES(?,?,?,?,?)", (bid, j, p_, None, None))
        c.commit()

def sync_folder_banks(c):
    """A pad bank named like a jingles folder (IDS, SWEEPERS, NEW MUSIC …) follows that folder: new files are added at
    the end, deleted ones leave. Your order and colours stay. (Other banks are yours alone.)"""
    folders = {r[0] for r in c.execute("SELECT DISTINCT folder FROM items WHERE section='jingles' AND problem IS NULL")}
    tops = folders | {f.split("/")[0] for f in folders}
    changed = False
    for bid, name in c.execute("SELECT id, name FROM pad_banks").fetchall():
        if name not in tops: continue
        have = [r[0] for r in c.execute("SELECT path FROM pad_items WHERE bank_id=? ORDER BY pos", (bid,))]
        files = [r[0] for r in c.execute("""SELECT path FROM items WHERE section='jingles' AND problem IS NULL AND (folder=? OR folder LIKE ?)
                                            ORDER BY folder, title COLLATE NOCASE""", (name, name + "/%"))]
        gone = [p_ for p_ in have if p_ not in set(files)]; new = [p_ for p_ in files if p_ not in set(have)]
        if gone:
            c.executemany("DELETE FROM pad_items WHERE bank_id=? AND path=?", [(bid, p_) for p_ in gone]); changed = True
        n = len(have) - len(gone)
        for p_ in new[:max(0, 64 - n)]:
            c.execute("INSERT INTO pad_items VALUES(?,?,?,?,?)", (bid, n, p_, None, None)); n += 1; changed = True
    if changed: c.commit()

def api_padbanks(c):
    try: sync_folder_banks(c)
    except Exception: pass
    out = []
    for bid, name in c.execute("SELECT id, name FROM pad_banks ORDER BY pos, id").fetchall():
        items = []
        for p_, label, color in c.execute("SELECT path, label, color FROM pad_items WHERE bank_id=? ORDER BY pos", (bid,)).fetchall():
            r = c.execute("SELECT title, artist, duration, has_video, section, problem FROM items WHERE path=?", (p_,)).fetchone()
            items.append(dict(path=p_, label=label or (r[0] if r else p_.split('/')[-1]), color=color, duration=r[2] if r else None,
                              has_video=r[3] if r else 0, section=r[4] if r else None, missing=not r or bool(r[5])))
        out.append(dict(id=bid, name=name, items=items))
    return out

def api_save_padbanks(c, d):
    keep = []
    for i, b in enumerate(d.get("banks", [])):
        name = (b.get("name") or "").strip() or f"Bank {i + 1}"
        if b.get("id"): bid = int(b["id"]); c.execute("UPDATE pad_banks SET name=?, pos=? WHERE id=?", (name, i, bid))
        else: bid = c.execute("INSERT INTO pad_banks(name, pos) VALUES(?,?)", (name, i)).lastrowid
        keep.append(bid); c.execute("DELETE FROM pad_items WHERE bank_id=?", (bid,))
        for j, it in enumerate(b.get("items", [])[:64]):
            c.execute("INSERT INTO pad_items VALUES(?,?,?,?,?)", (bid, j, it["path"], it.get("label") or None, it.get("color") or None))
    if keep:
        q = ",".join("?" * len(keep))
        c.execute(f"DELETE FROM pad_items WHERE bank_id NOT IN ({q})", keep); c.execute(f"DELETE FROM pad_banks WHERE id NOT IN ({q})", keep)
    c.commit()

# ---------------- on-screen items (New Music, Throwback … images on TV when a song from a folder plays) ----------------
SCREEN_DIR = "/srv/hype/branding/screen"
# ---------------- sponsor spots / ad breaks ----------------
def init_ads(c):
    c.execute("""CREATE TABLE IF NOT EXISTS ad_spots(id INTEGER PRIMARY KEY, brand TEXT, path TEXT, start_day TEXT, end_day TEXT,
                 per_day INT DEFAULT 4, days TEXT DEFAULT '0123456', hours TEXT DEFAULT '', enabled INT DEFAULT 1, pos INT DEFAULT 0)"""); c.commit()

def ad_window(sp, t):
    """(start, end) of the hours this spot may play on the day of t ('' = 07:00–24:00; '16-18'; '22-2' over midnight)."""
    lt = time.localtime(t); day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", sp.get("hours") or "")
    a, b = (int(m.group(1)), int(m.group(2))) if m else (7, 24)
    if b <= a: b += 24
    return day0 + a * 3600, day0 + b * 3600

def ad_spots(c):
    init_ads(c)
    k = ["id", "brand", "path", "start_day", "end_day", "per_day", "days", "hours", "enabled"]
    return [dict(zip(k, r)) for r in c.execute(f"SELECT {','.join(k)} FROM ad_spots ORDER BY pos, id")]

def ad_played_today(c, t=None):
    """Plays per spot file today (from the as-run log)."""
    lt = time.localtime(t or time.time()); day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return dict(c.execute("SELECT path, COUNT(*) FROM asrun WHERE started >= ? AND kind != 'music' GROUP BY path", (day0,)).fetchall())

def pick_ads(c, t, n, planned=None, avoid_brands=None):
    """The n spots that are most 'behind schedule' at time t (X per day spread over their hours), max. 1 per brand,
    never longer than the max. break length together. planned = {path: count} already in Up next (counted as played)."""
    planned = planned if planned is not None else {}; avoid_brands = set(avoid_brands or ())
    lt = time.localtime(t); today = time.strftime("%Y-%m-%d", lt)
    played = ad_played_today(c, t); max_len = float(get_setting(c, "ad_max_len", 150)); out = []; total = 0.0
    cand = []
    for sp in ad_spots(c):
        if not sp["enabled"] or not sp["path"]: continue
        if sp["start_day"] and today < sp["start_day"]: continue
        if sp["end_day"] and today > sp["end_day"]: continue
        if str(lt.tm_wday) not in (sp["days"] or "0123456"): continue
        a, b = ad_window(sp, t)
        if not (a <= t < b): continue
        r = c.execute("SELECT COALESCE(cue_end, duration, 30) - COALESCE(cue_start, 0) FROM items WHERE path=? AND problem IS NULL", (sp["path"],)).fetchone()
        if not r: continue
        done = played.get(sp["path"], 0) + planned.get(sp["path"], 0)
        if done >= (sp["per_day"] or 0): continue
        target = (sp["per_day"] or 0) * min(1.0, (t - a) / max(1.0, b - a))
        cand.append((target - done + 0.5, sp, float(r[0] or 30)))
    for score, sp, ln in sorted(cand, key=lambda x: -x[0]):
        if len(out) >= n: break
        br = (sp["brand"] or "").strip().lower()
        if br and br in avoid_brands: continue
        if out and total + ln > max_len: continue
        out.append(sp["path"]); total += ln; avoid_brands.add(br); planned[sp["path"]] = planned.get(sp["path"], 0) + 1
    return out

def api_save_ads(c, d):
    init_ads(c); c.execute("DELETE FROM ad_spots")
    for i, s in enumerate(d.get("spots", [])):
        hrs = str(s.get("hours") or "").strip()
        if hrs and not re.match(r"^\d{1,2}\s*-\s*\d{1,2}$", hrs): raise ValueError("hours must be like 16-18 (or empty = 07–24)")
        for k in ("start_day", "end_day"):
            if s.get(k) and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(s[k])): raise ValueError("dates must be like 2026-11-01")
        c.execute("INSERT INTO ad_spots(brand, path, start_day, end_day, per_day, days, hours, enabled, pos) VALUES(?,?,?,?,?,?,?,?,?)",
                  ((s.get("brand") or "").strip()[:60], s.get("path") or "", s.get("start_day") or "", s.get("end_day") or "", max(0, min(96, int(s.get("per_day") or 0))),
                   "".join(ch for ch in "0123456" if ch in str(s.get("days") if s.get("days") is not None else "0123456")), hrs, int(bool(s.get("enabled", True))), i))
    for k, lo, hi in (("ad_max_len", 15, 600), ("ad_max_spots", 1, 10)):
        if k in d: set_setting(c, k, max(lo, min(hi, float(d[k]))))
    c.commit()

def init_screen(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS screen_rules(id INTEGER PRIMARY KEY, name TEXT, enabled INT DEFAULT 1, trigger TEXT, ref TEXT,
        file TEXT, delay REAL DEFAULT 3, duration REAL DEFAULT 10, every INT DEFAULT 1, min_gap INT DEFAULT 0, where_ TEXT DEFAULT 'all', pos INT DEFAULT 0);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(screen_rules)")]
    if "days" not in cols: c.execute("ALTER TABLE screen_rules ADD COLUMN days TEXT DEFAULT '0123456'")
    if "hours" not in cols: c.execute("ALTER TABLE screen_rules ADD COLUMN hours TEXT DEFAULT ''")
    c.commit()

def rule_now(r, t=None):
    """Is an ON SCREEN rule allowed now? days = '0123456' (Mon = 0), hours = '' (all day) or '16-18' (from 16:00 to 18:00; '22-2' over midnight)."""
    lt = time.localtime(t or time.time())
    if str(lt.tm_wday) not in (r.get("days") or "0123456"): return False
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", r.get("hours") or "")
    if not m: return True
    a, b = int(m.group(1)), int(m.group(2))
    return (a <= lt.tm_hour < b) if a < b else (lt.tm_hour >= a or lt.tm_hour < b)

def api_screen_rules(c):
    k = ["id", "name", "enabled", "trigger", "ref", "file", "delay", "duration", "every", "min_gap", "where_", "days", "hours"]
    return [dict(zip(k, r)) for r in c.execute(f"SELECT {','.join(k)} FROM screen_rules ORDER BY pos, id")]

def api_save_screen_rules(c, d):
    c.execute("DELETE FROM screen_rules")
    for i, r in enumerate(d.get("rules", [])):
        if r.get("trigger") == "time" and not re.match(r"^(\d{1,2}|\*):\d{2}$", str(r.get("ref") or "")): raise ValueError("time must be like 20:30 or *:15")
        hrs = str(r.get("hours") or "").strip()
        if hrs and not re.match(r"^\d{1,2}\s*-\s*\d{1,2}$", hrs): raise ValueError("hours must be like 16-18 (or empty = all day)")
        days = "".join(ch for ch in "0123456" if ch in str(r.get("days") if r.get("days") is not None else "0123456"))
        c.execute("""INSERT INTO screen_rules(name, enabled, trigger, ref, file, delay, duration, every, min_gap, where_, pos, days, hours) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (r.get("name") or "", int(bool(r.get("enabled", True))), r.get("trigger") or "folder", str(r.get("ref") or ""), r.get("file") or "",
                   float(r.get("delay") or 0), float(r.get("duration") or 0), max(1, int(r.get("every") or 1)), int(r.get("min_gap") or 0), r.get("where_") or "all", i,
                   days, hrs))
    c.commit()

def screen_files():
    os.makedirs(SCREEN_DIR, exist_ok=True)
    return sorted(f for f in os.listdir(SCREEN_DIR) if re.search(r"\.(png|jpe?g|gif|webp|webm|mp4|mov)$", f, re.I))
