#!/usr/bin/env python3
"""Import CLOSE RadioTV cue files (*.close) into the Hype Studio library.
CLOSE names each cue file after the full path of the song (back-slashes -> '_'), e.g.
MUSIC_MUSIC_AFRO_AFRO-POWER_Rema - Oh No.mp4.close  with  [Data] Inicio=9 Fin=195 Pisador=... .
All cue files are stored (table close_cues) and matched to songs by file name, also songs uploaded later."""
import os, sys, re, sqlite3, zipfile, io, csv, time
DB = "/srv/hype-data/library.db"; INBOX = "/srv/hype/inbox"; REPORT = "/var/www/studio/close-import.csv"

def parse(text):
    d = {}
    for line in text.replace("\r", "").split("\n"):
        if "=" in line:
            k, v = line.split("=", 1); d[k.strip().lower()] = v.strip()
    num = lambda k: float(d[k].replace(",", ".")) if d.get(k, "") not in ("",) and re.match(r"^-?[\d.,]+$", d.get(k, "")) else None
    return {"inicio": num("inicio"), "fin": num("fin"), "pisador": d.get("pisador") or None,
            "pisador_inicio": num("pisadorinicio"), "pisador_fade": num("pisadorfade")}

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS close_cues(key TEXT PRIMARY KEY, inicio REAL, fin REAL, pisador TEXT,
                 pisador_inicio REAL, pisador_fade REAL, imported_at REAL)""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(items)")]
    for col, typ in [("overlay_path", "TEXT"), ("overlay_at", "REAL"), ("overlay_duck", "REAL"), ("close_key", "TEXT")]:
        if col not in cols: c.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
    return c

def collect(c):
    n = 0
    for dp, dn, fns in os.walk(INBOX):
        for fn in fns:
            p = os.path.join(dp, fn)
            if fn.lower().endswith(".close"):
                store(c, fn[:-6], open(p, "rb").read().decode("latin-1")); n += 1
            elif fn.lower().endswith(".zip"):
                with zipfile.ZipFile(p) as z:
                    for zi in z.namelist():
                        if zi.lower().endswith(".close"):
                            store(c, os.path.basename(zi)[:-6], z.read(zi).decode("latin-1")); n += 1
    c.commit(); return n

def store(c, key, text):
    d = parse(text)
    c.execute("INSERT OR REPLACE INTO close_cues VALUES(?,?,?,?,?,?,?)",
              (key, d["inicio"], d["fin"], d["pisador"], d["pisador_inicio"], d["pisador_fade"], time.time()))

def norm(s):
    """A file name without case, accents, 'ft.'/'x'/'&', (Official Video)-style tags and punctuation, for matching
    files that were renamed a little (CLOSE AUTODOWNLOADS: 'ARCÁNGEL X PESO PLUMA - LA CHAMBA.mp4' = 'Arcangel x Peso Pluma - La Chamba.mp4')."""
    import unicodedata
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\.(mp4|mp3|mov|mkv|m4v|webm|wav|m4a)$", "", s)
    s = re.sub(r"\((official|video|music|clean|explicit|lyric|visuali)[^)]*\)|\[[^]]*\]", "", s)
    s = re.sub(r"\b(ft|feat|featuring|x|and)\b\.?", " ", s).replace("&", " ")
    return re.sub(r"[^a-z0-9]+", "", s)

def apply(c, only_path=None):
    """Match by file name: the CLOSE key ends with '_' + the song's file name; else the same name written a little differently."""
    keys = c.execute("SELECT key, inicio, fin, pisador, pisador_inicio, pisador_fade FROM close_cues").fetchall()
    by_name = {}; by_norm = {}
    for k in keys:
        parts = k[0].split("_")
        for i in range(1, len(parts)):
            by_name.setdefault("_".join(parts[i:]).lower(), []).append(k)
        for i in range(1, len(parts)):   # same kind of file only: a song's .mp3 cues never go to its music video (.mp4)
            nm = "_".join(parts[i:]); by_norm.setdefault(norm(nm) + "|" + os.path.splitext(nm)[1].lower(), []).append(k)
    q = "SELECT path, filename, duration, cue_start, cue_mix, cue_end, cues_edited FROM items WHERE section IN ('music','jingles','ads','clips')"   # clips too: a music video starts at its CLOSE in-point
    rows = c.execute(q + (" AND path=?" if only_path else ""), ((only_path,) if only_path else ())).fetchall()
    out = []
    for path, fn, dur, cs, cm, ce, edited in rows:
        hits = by_name.get(fn.lower(), []) or by_norm.get(norm(fn) + "|" + os.path.splitext(fn)[1].lower(), [])
        if not hits: out.append([path, "", "no CLOSE cue file", "", "", "", ""]); continue
        hits.sort(key=lambda k: (k[1] is None, -len(k[0])))   # prefer one with a start point
        key, ini, fin, pis, pis_i, pis_f = hits[0]
        note = "applied" + (f" (also {len(hits)-1} other CLOSE folder(s))" if len(hits) > 1 else "")
        if edited == 1:
            out.append([path, key, "kept your own edited cues", ini, fin, cs, cm]); continue
        new_cs = ini if ini is not None else cs
        new_cm, new_ce = cm, ce
        if fin is not None and (dur is None or fin <= dur + 0.5):
            # CLOSE 'Fin' = hard end of the song (nothing after it is heard, like in CLOSE).
            # MIX = Fin, like in CLOSE.
            new_ce = fin
            new_cm = fin   # CLOSE has only an end: MIX = END (the smart mixer finds the exact moment, never after END)
        if new_cs is not None and new_cm is not None and new_cm <= new_cs:
            out.append([path, key, "skipped: start after end", ini, fin, cs, cm]); continue
        c.execute("""UPDATE items SET cue_start=?, cue_mix=?, cue_end=?, cues_edited=2, close_key=?,
                     overlay_path=?, overlay_at=?, overlay_duck=? WHERE path=?""",
                  (new_cs, new_cm, new_ce, key, pis, pis_i, pis_f, path))
        out.append([path, key, note, ini, fin, cs, cm])
    c.commit(); return out

if __name__ == "__main__":
    c = db(); n = collect(c); res = apply(c)
    with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";"); w.writerow(["Song", "CLOSE cue file", "Result", "CLOSE start", "CLOSE end (Fin)", "Hype start before", "Hype mix before"])
        w.writerows(res)
    os.chmod(REPORT, 0o644)
    applied = sum(1 for r in res if r[2].startswith("applied"))
    print(f"cue files read: {n} · songs: {len(res)} · applied: {applied} · no CLOSE file: {sum(1 for r in res if r[2]=='no CLOSE cue file')}")
