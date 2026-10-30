#!/usr/bin/env python3
"""Hype Studio library indexer.
Scans /srv/hype (music, clips, jingles, ads), reads tags/duration/loudness, finds cue points,
pairs an audio song with a clip of the same name automatically, and writes a SQLite library + JSON/CSV reports."""
import os, re, sys, json, time, sqlite3, subprocess, difflib, csv, unicodedata
ROOT = os.environ.get("HYPE_ROOT", "/srv/hype")
DB = os.environ.get("HYPE_DB", "/srv/hype-data/library.db")
WEB = os.environ.get("HYPE_WEB", "/var/www/studio")
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi", ".mpg", ".mpeg", ".ts"}
SECTIONS = ("music", "clips", "jingles", "ads")

def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS items(
      path TEXT PRIMARY KEY, section TEXT, folder TEXT, filename TEXT, size INT, mtime REAL,
      artist TEXT, title TEXT, duration REAL, has_video INT, has_audio INT, width INT, height INT,
      lufs REAL, peak REAL, cue_start REAL, cue_intro REAL, cue_mix REAL, cue_end REAL,
      mode TEXT, clip_path TEXT, clip_suggest TEXT, clip_score REAL,
      problem TEXT, indexed_at REAL, cues_edited INT DEFAULT 0, mode_set_by_user INT DEFAULT 0)""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(items)")]
    if "tv_path" not in cols: c.execute("ALTER TABLE items ADD COLUMN tv_path TEXT")
    if "fp" not in cols: c.execute("ALTER TABLE items ADD COLUMN fp TEXT")
    if "genre" not in cols: c.execute("ALTER TABLE items ADD COLUMN genre TEXT")
    if "kf" not in cols: c.execute("ALTER TABLE items ADD COLUMN kf TEXT")
    if "lmap" not in cols: c.execute("ALTER TABLE items ADD COLUMN lmap TEXT")   # loudness map: the loudest moment of every 0.5 s (for the AGC intro lift)
    if "added_at" not in cols:   # when the file first arrived (for "recently uploaded")
        c.execute("ALTER TABLE items ADD COLUMN added_at REAL"); c.execute("UPDATE items SET added_at=MIN(COALESCE(mtime, indexed_at), COALESCE(indexed_at, mtime))")
    c.execute("CREATE INDEX IF NOT EXISTS items_fp ON items(fp)")
    return c

def run(cmd, timeout=600):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\.(mp4|mov|mkv|m4v|webm|avi|mp3|m4a|wav|flac|aac|ogg)$", "", s)
    s = re.sub(r"[\(\[][^\)\]]*(official|video|oficial|clip|lyric|audio|visualizer|hd|4k|explicit|clean|remaster)[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"\b(official|music|video|oficial|videoclip|lyrics?|hd|4k|explicit|ft|feat|featuring|x)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def split_name(fn):
    base = os.path.splitext(fn)[0]
    # track numbers like "01. ", "03 - ", "07_" or "01 " – but never a name that starts with digits (50 Cent, 2Pac, 539, 21 Savage)
    base = re.sub(r"^(?:\d{1,3}\s*[._-]\s*|0\d\s+)(?=.+ - )", "", base)
    if " - " in base:
        a, t = base.split(" - ", 1)
        return a.strip(), t.strip()
    return "", base.strip()

def clean_title(t):
    t = re.sub(r"\s*[\(\[][^\)\]]*(official|video|oficial|clip|lyric|audio|visualizer|hd|4k)[^\)\]]*[\)\]]", "", t, flags=re.I)
    return t.strip() or t

def probe(path):
    r = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path], 120)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)

def loudness_and_cues(path, duration):
    """EBU R128 integrated loudness + momentary curve -> cue points."""
    r = run(["nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
             "-af", "ebur128=peak=true:framelog=info", "-f", "null", "-"], max(900, int((duration or 0) * 0.5)))
    txt = r.stderr
    mom = []
    for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET:[^M]*M:\s*(-?[\d.]+|-inf)", txt):
        v = m.group(2)
        mom.append((float(m.group(1)), -120.0 if v == "-inf" else float(v)))
    I = re.findall(r"I:\s*(-?[\d.]+) LUFS", txt)
    P = re.findall(r"Peak:\s*(-?[\d.]+|-inf) dBFS", txt)
    lufs = float(I[-1]) if I else None
    peak = None if not P or P[-1] == "-inf" else float(P[-1])
    cue_start = cue_end = cue_mix = None
    if mom and lufs is not None:
        loud = [t for t, v in mom if v > -45]
        if loud:
            cue_start = max(0.0, round(loud[0] - 0.4 - 0.1, 2))   # momentary window is 400 ms
            cue_end = round(min(duration or loud[-1], loud[-1] + 0.1), 2)
        # mix point: last moment the music is still near its normal level (integrated - 8 dB)
        strong = [t for t, v in mom if v > lufs - 8]
        if strong:
            mp = strong[-1]
            if cue_end and cue_end - mp > 20:   # long outro / spoken end: cap at 12 s before end
                mp = cue_end - 12
            cue_mix = round(max(cue_start or 0, mp), 2)
    return lufs, peak, cue_start, cue_mix, cue_end, loud_map(mom)

def loud_map(mom):
    """The loudest momentary level of every 0.5 s of the file, as whole dB ("-24,-23,...")."""
    if not mom: return None
    out = [-60] * (int(mom[-1][0] * 2) + 1)
    for t, v in mom:
        i = int(t * 2); out[i] = max(out[i], max(-60, int(round(v))))
    return ",".join(map(str, out))

def make_lmaps(c, n=15):
    """Songs indexed before the loudness map existed: measure a few per round (low priority)."""
    for p_, d_ in c.execute("""SELECT path, duration FROM items WHERE section='music' AND problem IS NULL AND has_audio=1
                               AND lmap IS NULL ORDER BY added_at DESC LIMIT ?""", (n,)).fetchall():
        try: lm = loudness_and_cues(os.path.join(ROOT, p_), d_)[5]
        except Exception as e: print("lmap:", p_, e, flush=True); lm = None
        c.execute("UPDATE items SET lmap=? WHERE path=?", (lm or "-", p_)); c.commit()

def fingerprint(path, size):
    """Same song = same fingerprint, whatever its name or folder (size + first and last MB)."""
    import hashlib
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20): f.seek(-(1 << 20), 2); h.update(f.read(1 << 20))
    return h.hexdigest()

def follow_move(c, rel, fp):
    """A file with this fingerprint was indexed under another path that no longer exists:
    it was moved or renamed -> keep its cue points, TV mode, clip link and play history."""
    for (old,) in c.execute("SELECT path FROM items WHERE fp=? AND path<>?", (fp, rel)).fetchall():
        if not os.path.exists(os.path.join(ROOT, old)):
            c.execute("DELETE FROM items WHERE path=?", (rel,))
            c.execute("UPDATE items SET path=? WHERE path=?", (rel, old))
            c.execute("UPDATE items SET clip_path=? WHERE clip_path=?", (rel, old))
            c.execute("UPDATE items SET clip_suggest=? WHERE clip_suggest=?", (rel, old))
            try: c.execute("UPDATE asrun SET path=? WHERE path=?", (rel, old))
            except sqlite3.OperationalError: pass
            print("moved:", old, "->", rel, flush=True)
            return True
    return False

def why_broken(path, size):
    """Tell what's wrong: an upload that stopped halfway is the usual cause (the MP4 index is missing)."""
    try:
        import struct
        with open(path, "rb") as f:
            pos, atoms = 0, []
            while pos + 8 <= size and len(atoms) < 50:
                f.seek(pos); hd = f.read(16)
                sz, typ = struct.unpack(">I4s", hd[:8]); typ = typ.decode("latin-1")
                if sz == 1: sz = struct.unpack(">Q", hd[8:16])[0]
                if sz < 8: break
                atoms.append(typ)
                if pos + sz > size:
                    return "upload not complete (%d of %d MB) – upload it again" % (size // 1000000, (pos + sz) // 1000000)
                pos += sz
            if atoms and "moov" not in atoms and atoms[0] == "ftyp": return "upload not complete (video index missing) – upload it again"
    except Exception: pass
    return "broken file (cannot be read) – upload it again or export it again"

def index_file(c, path, st):
    rel = os.path.relpath(path, ROOT)
    parts = rel.split(os.sep)
    section = parts[0]
    folder = "/".join(parts[1:-1])
    genre = parts[1] if len(parts) > 2 else ""
    fn = parts[-1]
    ext = os.path.splitext(fn)[1].lower()
    row = dict(path=rel, section=section, folder=folder, filename=fn, size=st.st_size, mtime=st.st_mtime,
               artist="", title="", duration=None, has_video=0, has_audio=0, width=None, height=None,
               lufs=None, peak=None, lmap=None, cue_start=None, cue_intro=None, cue_mix=None, cue_end=None,
               mode=None, problem=None, indexed_at=time.time(), tv_path=None, genre=genre, cues_edited=0)
    fp = fingerprint(path, st.st_size); row["fp"] = fp
    if not c.execute("SELECT 1 FROM items WHERE path=?", (rel,)).fetchone() and follow_move(c, rel, fp):
        c.execute("UPDATE items SET section=?, folder=?, genre=?, filename=?, size=?, mtime=? WHERE path=?",
                  (section, folder, genre, fn, st.st_size, st.st_mtime, rel))
        return
    info = probe(path)
    if not info:
        row["problem"] = why_broken(path, st.st_size)
        stem = os.path.splitext(fn)[0]; ar, _, ti = stem.partition(" - ")
        row["artist"], row["title"] = (ar.strip(), ti.strip()) if ti else ("", stem)   # so you can see WHICH file
    else:
        fmt = info.get("format", {})
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        try: row["duration"] = round(float(fmt.get("duration", 0)), 2)
        except: pass
        for s in info.get("streams", []):
            if s.get("codec_type") == "audio": row["has_audio"] = 1
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
                row["has_video"] = 1; row["width"] = s.get("width"); row["height"] = s.get("height")
        fa, ft = split_name(fn)
        if fa:   # Gianno's file names ("Artist - Title") are cleaner than the tags: they win
            row["artist"], row["title"] = fa, clean_title(ft)
        elif section != "music":   # jingles/ads: the FILE NAME is the name (their tags are often copied from another jingle)
            row["artist"], row["title"] = "", clean_title(ft or os.path.splitext(fn)[0])
        else:
            row["artist"] = (tags.get("artist") or tags.get("album_artist") or "").strip()
            row["title"] = clean_title((tags.get("title") or ft or "").strip())
        if not row["has_audio"] and not folder.upper().startswith("SHOW-INTROS"):   # show intros are picture-only on purpose
            row["problem"] = "no sound in this file"
        elif (row["duration"] or 0) < 1:
            row["problem"] = "too short / empty"
        elif section in ("music", "jingles", "ads") or (section == "clips"):
            lufs, peak, cs, cm, ce, lm = loudness_and_cues(path, row["duration"])
            row.update(lufs=lufs, peak=peak, cue_start=cs, cue_mix=cm, cue_end=ce, lmap=lm)
        if section == "music" and not row["artist"]:
            row["problem"] = row["problem"] or "no artist (name the file 'Artist - Title')"
    if section == "music" and not row["problem"]:
        row["mode"] = "clip" if row["has_video"] else "audio"
    old = c.execute("SELECT cues_edited, mode_set_by_user, mode, clip_path, cue_start, cue_intro, cue_mix, cue_end FROM items WHERE path=?", (rel,)).fetchone()
    if old:
        if old[0] == 1:  # keep hand-edited cue points (2 = imported from CLOSE: re-applied below)
            row.update(cue_start=old[4], cue_intro=old[5], cue_mix=old[6], cue_end=old[7], cues_edited=1)

    cols = ",".join(row.keys()); qs = ",".join("?" * len(row))
    upd = ",".join(f"{k}=excluded.{k}" for k in row.keys())
    c.execute(f"INSERT INTO items({cols}) VALUES({qs}) ON CONFLICT(path) DO UPDATE SET {upd}", list(row.values()))
    c.execute("UPDATE items SET added_at=? WHERE path=? AND added_at IS NULL", (time.time(), rel))
    if not row["problem"] and row.get("cues_edited") != 1:   # use its CLOSE cue points if we have them
        try:
            sys.path.insert(0, "/opt/hype"); import close_import
            close_import.db(); close_import.apply(c, rel)
        except Exception as e: print("close cues:", e, flush=True)

TVDIR = os.environ.get("HYPE_TV", "/srv/hype-data/tv")
TV_V = 2   # v2 = re-encoded with a keyframe every 0.5 s, so a clip can start (in sync) at any moment, also during a mix
LONG = 12 * 60     # longer than this = a mixtape / long video
TV_FAIL = {}       # path -> time of the last failed TV copy (try again after 6 h, never block the other files)
def tv_encode(src, tmp, dur=0):
    if dur and dur > LONG:     # mixtape: picture copied as it is (starts at its beginning, so the keyframe steps don't matter)
        return run(["nice", "-n", "19", "ionice", "-c3", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-c:v", "copy", "-f", "matroska", tmp], 1800)
    return run(["nice", "-n", "19", "ionice", "-c3", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", "scale='min(1920,iw)':-2:flags=bicubic", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-force_key_frames", "expr:gte(t,n_forced*0.5)", "-sc_threshold", "0", "-threads", "4", "-f", "matroska", tmp], max(1800, int((dur or 0) * 3)))

def make_tv_copies(c, upgrades=3):
    """Picture-only copy of every video (no sound track) with short keyframe steps. New videos first,
    then older copies are upgraded a few per scan - the songs coming up next on air first."""
    import hashlib
    os.makedirs(TVDIR, exist_ok=True)
    if "tv_v" not in [r[1] for r in c.execute("PRAGMA table_info(items)")]: c.execute("ALTER TABLE items ADD COLUMN tv_v INT DEFAULT 1"); c.commit()
    new = [r[0] for r in c.execute("SELECT path FROM items WHERE has_video=1 AND problem IS NULL AND (tv_path IS NULL OR tv_path='')")]
    try: soon = json.load(open("/var/lib/hype/upcoming.json"))
    except Exception: soon = []
    rank = {p_: i for i, p_ in enumerate(soon)}
    old = [r[0] for r in c.execute("SELECT path FROM items WHERE has_video=1 AND problem IS NULL AND tv_path<>'' AND COALESCE(tv_v,1)<?", (TV_V,))]
    old = sorted(old, key=lambda p_: rank.get(p_, 9999))[:upgrades]
    for path in new + old:
        if time.time() - TV_FAIL.get(path, 0) < 6 * 3600: continue
        out = os.path.join(TVDIR, hashlib.sha1(path.encode()).hexdigest()[:16] + ".mkv"); tmp = out + ".part.mkv"
        dur = (c.execute("SELECT duration FROM items WHERE path=?", (path,)).fetchone() or [0])[0] or 0
        try: r = tv_encode(os.path.join(ROOT, path), tmp, dur)
        except subprocess.TimeoutExpired:
            print("tv copy too slow, later again:", path, flush=True); TV_FAIL[path] = time.time()
            try: os.remove(tmp)
            except OSError: pass
            continue
        if r.returncode != 0: TV_FAIL[path] = time.time()
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 10000:
            os.replace(tmp, out); c.execute("UPDATE items SET tv_path=?, tv_v=?, kf=NULL WHERE path=?", (out, TV_V, path)); c.commit()
        else:
            print("tv copy failed:", path, (r.stderr or "")[-200:], flush=True)
            try: os.remove(tmp)
            except OSError: pass

def make_keyframes(c):
    """Keyframe times of every TV copy: the engine starts a clip exactly on a keyframe, so picture and radio stay in sync."""
    for path, tv in c.execute("SELECT path, tv_path FROM items WHERE tv_path IS NOT NULL AND tv_path<>'' AND (kf IS NULL OR kf='[]')").fetchall():
        if not os.path.exists(tv): continue
        r = run(["nice", "-n", "15", "ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                 "-show_entries", "frame=pts_time", "-of", "csv=p=0", tv], 900)
        kf = sorted({round(float(x), 3) for x in re.findall(r"\d+\.\d+|\d+", r.stdout)})
        c.execute("UPDATE items SET kf=? WHERE path=?", (json.dumps(kf), path)); c.commit()

def suggest_pairs(c):
    """Automatic pairing: an audio-only song plays its clip on TV when /clips has a clip with the same name
    (artist + title; case, accents, "official video" etc. don't matter; word order neither). No manual choices."""
    keys = {}
    for p_, f_, a_, t_ in c.execute("SELECT path, filename, artist, title FROM items WHERE section='clips' AND problem IS NULL AND has_video=1"):
        for k in {norm((a_ + " " + t_) if a_ else os.path.splitext(f_)[0]), norm(os.path.splitext(f_)[0])}:
            if k:
                keys.setdefault(k, p_); keys.setdefault(" ".join(sorted(k.split())), p_)
    for path, artist, title, filename in c.execute("SELECT path, artist, title, filename FROM items WHERE section='music' AND has_video=0 AND problem IS NULL").fetchall():
        k = norm(artist + " " + title) if artist else norm(os.path.splitext(filename)[0])
        clip = keys.get(k) or keys.get(" ".join(sorted(k.split())))
        c.execute("UPDATE items SET mode=?, clip_path=?, clip_suggest=NULL, clip_score=NULL, mode_set_by_user=0 WHERE path=?",
                  ("pair" if clip else "audio", clip, path))

def scan(full=False):
    c = db()
    seen = set(); n_new = 0
    for sec in SECTIONS:
        base = os.path.join(ROOT, sec)
        for dp, dn, fns in os.walk(base):
            for fn in fns:
                if fn.startswith("."): continue
                ext = os.path.splitext(fn)[1].lower()
                if ext not in AUDIO_EXT | VIDEO_EXT: continue
                p = os.path.join(dp, fn); rel = os.path.relpath(p, ROOT); seen.add(rel)
                try: st = os.stat(p)
                except FileNotFoundError: continue
                if time.time() - max(st.st_mtime, st.st_ctime) < 20: continue   # still uploading (FileZilla may keep the old file date: ctime shows the writing)
                old = c.execute("SELECT size, mtime FROM items WHERE path=?", (rel,)).fetchone()
                if old and not full and old[0] == st.st_size and abs(old[1] - st.st_mtime) < 1: continue
                index_file(c, p, st); n_new += 1; c.commit()
    # fingerprints for rows indexed before fingerprints existed
    for (p_,) in c.execute("SELECT path FROM items WHERE fp IS NULL").fetchall():
        f_ = os.path.join(ROOT, p_)
        if os.path.exists(f_): c.execute("UPDATE items SET fp=? WHERE path=?", (fingerprint(f_, os.path.getsize(f_)), p_))
    gone = [r[0] for r in c.execute("SELECT path FROM items") if r[0] not in seen]
    for g in gone:
        # copied to a new place and the old one deleted: hand the old row's data to the copy
        old = c.execute("SELECT fp, cues_edited, cue_start, cue_intro, cue_mix, cue_end, mode_set_by_user, mode, clip_path FROM items WHERE path=?", (g,)).fetchone()
        twin = c.execute("SELECT path FROM items WHERE fp=? AND path<>?", (old[0], g)).fetchone() if old and old[0] else None
        if twin:
            if old[1]: c.execute("UPDATE items SET cues_edited=1, cue_start=?, cue_intro=?, cue_mix=?, cue_end=? WHERE path=?", (old[2], old[3], old[4], old[5], twin[0]))
            if old[6]: c.execute("UPDATE items SET mode_set_by_user=1, mode=?, clip_path=? WHERE path=?", (old[7], old[8], twin[0]))
            c.execute("UPDATE items SET clip_path=? WHERE clip_path=?", (twin[0], g))
            try: c.execute("UPDATE asrun SET path=? WHERE path=?", (twin[0], g))
            except sqlite3.OperationalError: pass
            print("merged:", g, "->", twin[0], flush=True)
        c.execute("DELETE FROM items WHERE path=?", (g,))
    # folder/genre always follow the path (e.g. after folders were moved into a genre folder)
    for p_, f_, g_ in c.execute("SELECT path, folder, genre FROM items").fetchall():
        parts = p_.split("/"); nf = "/".join(parts[1:-1]); ng = parts[1] if len(parts) > 2 else ""
        if nf != f_ or (ng != (g_ or "")):
            c.execute("UPDATE items SET folder=?, genre=? WHERE path=?", (nf, ng, p_))
    suggest_pairs(c); c.commit()
    report(c)
    make_tv_copies(c)
    make_keyframes(c)
    close_inbox(c)
    try: make_lmaps(c)
    except Exception as e: print("lmap error", e, flush=True)
    return n_new, len(gone)

CLOSE_SEEN = [0.0]
def close_inbox(c):
    """New CLOSE cue files (.close, or a .zip of them) dropped in /srv/hype/inbox: read them and give every song its CLOSE in/out."""
    inbox = os.path.join(ROOT, "inbox"); stamp = 0.0
    for dp, dn, fns in os.walk(inbox):
        for fn in fns:
            if fn.lower().endswith((".close", ".zip")): stamp = max(stamp, os.path.getmtime(os.path.join(dp, fn)))
    if stamp <= CLOSE_SEEN[0]: return
    CLOSE_SEEN[0] = stamp
    try:
        sys.path.insert(0, "/opt/hype"); import close_import
        cc = close_import.db(); n = close_import.collect(cc); res = close_import.apply(cc)
        print("CLOSE cues: %d files read, %d songs set" % (n, sum(1 for r in res if r[2].startswith("applied"))), flush=True)
    except Exception as e: print("close inbox:", e, flush=True)

def report(c):
    q = lambda s, *a: c.execute(s, a).fetchall()
    summary = {
      "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
      "music": q("SELECT COUNT(*) FROM items WHERE section='music'")[0][0],
      "clips": q("SELECT COUNT(*) FROM items WHERE section='clips'")[0][0],
      "jingles": q("SELECT COUNT(*) FROM items WHERE section='jingles'")[0][0],
      "ads": q("SELECT COUNT(*) FROM items WHERE section='ads'")[0][0],
      "modes": dict(q("SELECT COALESCE(mode,'-'), COUNT(*) FROM items WHERE section='music' GROUP BY mode")),
      "pair_suggestions": q("SELECT COUNT(*) FROM items WHERE clip_suggest IS NOT NULL AND clip_path IS NULL")[0][0],
      "problems": q("SELECT COUNT(*) FROM items WHERE problem IS NOT NULL")[0][0],
      "folders": dict(q("SELECT folder, COUNT(*) FROM items WHERE section='music' GROUP BY folder")),
      "avg_lufs": (lambda v: round(v, 1) if v is not None else None)(q("SELECT AVG(lufs) FROM items WHERE section='music' AND lufs IS NOT NULL")[0][0]),
      "no_cues": q("SELECT COUNT(*) FROM items WHERE section='music' AND problem IS NULL AND cue_mix IS NULL")[0][0],
    }
    os.makedirs(WEB, exist_ok=True)
    json.dump(summary, open(WEB + "/library-summary.json.tmp", "w")); os.replace(WEB + "/library-summary.json.tmp", WEB + "/library-summary.json")
    rows = q("""SELECT section, folder, filename, artist, title, ROUND(duration,1), CASE has_video WHEN 1 THEN 'yes' ELSE 'no' END,
                mode, ROUND(lufs,1), cue_start, cue_mix, cue_end, clip_suggest, ROUND(clip_score*100), problem
                FROM items ORDER BY section, folder, artist, title""")
    with open(WEB + "/library.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Section", "Folder", "File", "Artist", "Title", "Length (s)", "Has video", "Mode", "Loudness (LUFS)",
                    "Cue start", "Mix point", "Cue end", "Suggested clip", "Match %", "Problem", "Check"])
        for r in rows: w.writerow(list(r) + [""])

if __name__ == "__main__":
    if "--watch" in sys.argv:
        while True:
            try: scan()
            except Exception as e: print("scan error", e, flush=True)
            time.sleep(30)
    else:
        print(scan("--full" in sys.argv))
