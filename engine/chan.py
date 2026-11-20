#!/usr/bin/env python3
"""hype-channels – the theme channels (Hype Ambiente, Hype Latina …) next to Hype Radio.

One process for all theme channels (systemd: hype-channels). Per channel that is switched on (Studio → the channel's page):
  - its own Liquidsoap (radio/channel.liq, filled in for the channel) – own sound settings, MP3 + internal 256k on Icecast
  - an ffmpeg that makes the HLS stream (app / website) from the internal 256k
  - a playout thread: picks from the channel's own clocks / hour plan (planner.py on chan_<id>.db) and keeps one item
    queued ahead in its Liquidsoap, writes the as-run, counts listeners.
Hype Radio (engine.py / radio.liq) is never touched: other ports, other mounts, other database. If this process dies,
Hype Radio plays on.

Small API on 127.0.0.1:8100 (the Studio engine passes the panel's calls on):
  GET  /status                  all channels: on / running / now / next / live from Hype Radio / listeners / level
  GET  /nowplaying/<id>         like Hype Radio's /api/nowplaying (apps / website)
  POST /track                   from a channel's Liquidsoap: an item started
  POST /cmd {ch, action, …}     enable / disable / skip / sim_on / sim_off / replan
"""
import os, sys, re, json, time, uuid, socket, random, signal, logging, threading, subprocess, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planner, chancfg, smartmix

ROOT = "/srv/hype"
TEMPLATE = "/etc/hype/channel.liq"
RUN = "/run/hype-ch"
HLS_SH = "/etc/hype/chan_hls.sh"
NGINX_LOG = "/var/log/nginx/stream.access.log"
TARGET_LUFS = -14.0
MIX_OUT = 4.0           # songs: the old one fades out over this many seconds under the new one (no smart mix here)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("chan")
BAD = re.compile(r"[\x00-\x1f\x7f]")


def item(c, path):
    r = c.execute("SELECT path, artist, title, folder, section, duration, cue_start, cue_mix, cue_end, lufs, has_video, "
                  "COALESCE(cues_edited, 0) FROM items WHERE path=? AND problem IS NULL", (path,)).fetchone()
    if not r: return None
    k = ("path", "artist", "title", "folder", "section", "duration", "cue_start", "cue_mix", "cue_end", "lufs", "has_video", "cues_edited")
    d = dict(zip(k, r)); d["id"] = uuid.uuid4().hex[:10]
    return d


def annotate(it, mix_out=None, jingle_db=0.0, smart=True, nxt=None):
    """The telnet line for one item: cue points, mix length, song gain (a simpler copy of Hype Radio's annotate)."""
    MIX_OUT_ = MIX_OUT if mix_out is None else mix_out          # Settings → Mixing of this channel
    cs = it["cue_start"] or 0.0; dur = it["duration"] or it["cue_end"] or 0; ce = it["cue_end"] or dur
    cm = it["cue_mix"] if it["cue_mix"] and it["cue_mix"] > cs else ce
    sm = None
    if smart:                                                     # Hype Radio's smart mix (smartmix.py), from the loudness map
        try: sm = smartmix.smart_mix(it, nxt)
        except Exception as e: log.warning("smart mix %s: %s", it.get("path"), e)
    if sm: cm, cross, fade = sm
    elif it["section"] in ("jingles", "ads"):
        cross = max(0.3, min(8.0, ce - cm)); fade = 0.0            # jingles play out to their END under the next song
    else:
        end_ = max(ce, dur or ce)
        if not it["cues_edited"] and end_ - cm < MIX_OUT_: cm = max(cs, end_ - MIX_OUT_)
        cross = max(0.3, min(6.0, MIX_OUT_, end_ - cm)); fade = cross
    ce = cm + cross
    gain = 0.0 if it["lufs"] is None else max(-12.0, min(12.0, TARGET_LUFS - it["lufs"]))
    if it["section"] in ("jingles", "ads"): gain += jingle_db
    if BAD.search(it["path"]): raise ValueError("file name with control characters")
    q = lambda v: BAD.sub(" ", str(v or "")).replace("\\", "/").replace('"', "'")
    meta = (f'liq_cue_in="{cs:.2f}",liq_cue_out="{ce:.2f}",liq_cross_duration="{cross:.2f}",liq_amplify="{gain:.2f} dB",'
            f'hype_fade="{fade:.2f}",artist="{q(it["artist"])}",title="{q(it["title"])}",hype_path="{q(it["path"])}",hype_id="{it["id"]}"')
    return f"annotate:{meta}:{os.path.join(ROOT, it['path'])}"


def duck_len(it):
    """How long the music stays down under a ducked pad / ID: until its voice ends (the smart mix point), as on Hype Radio."""
    cs = it.get("cue_start") or 0
    try: sm = smartmix.smart_mix(dict(it, section="jingles"))
    except Exception: sm = None
    if sm: return max(0.5, sm[0] - cs)
    cm = it.get("cue_mix")
    if cm and cm > cs: return cm - cs
    return max(0.5, (it.get("cue_end") or it.get("duration") or 5) - cs)


def akey(a):
    o = [x.strip().lower() for x in re.split(r"\s+(?:x|ft\.?|feat\.?|featuring|vs\.?|&|en|,)\s+|,", a or "", flags=re.I) if x.strip()]
    return [x for x in o if x not in ("club hype", "hype radio", "hype radio tv", "hype")] or o


class Channel:
    def __init__(self, cfg):
        self.cfg = cfg; self.id = cfg["id"]; self.lock = threading.RLock()
        self.liq = None; self.hls = None; self.liq_fail = 0; self.hls_fail = 0
        self.plan = []; self.runner = planner.ClockRunner(); self.now = None; self.pushed = None
        self.sim = False; self.auto_sim = False; self.listeners = {"mp3": 0, "hls": 0}; self.level = None; self.err = ""
        self.stop_ev = threading.Event(); self.thread = None
        self.songs_since_id = 0; self.pad_now = None; self.duck_t = None

    # ---------- the processes ----------
    def liq_file(self):
        os.makedirs(RUN, exist_ok=True)
        t = open(TEMPLATE).read()
        q = lambda v: str(v).replace('"', "'").replace("\\", "/")
        for k, v in (("@ID@", self.id), ("@NAME@", q(self.cfg["name"])), ("@GENRE@", q(self.cfg.get("genre") or "")),
                     ("@TELNET@", int(self.cfg["telnet"])), ("@API@", chancfg.API_PORT)):
            t = t.replace(k, str(v))
        p = os.path.join(RUN, self.id + ".liq")
        with open(p + ".tmp", "w") as f: f.write(t)
        os.replace(p + ".tmp", p); return p

    def start_procs(self):
        if not self.liq or self.liq.poll() is not None:
            if self.liq is not None:
                self.liq_fail += 1; log.warning("%s: radio engine stopped (exit %s) – starting again", self.id, self.liq.returncode)
            if self.liq_fail > 5: time.sleep(min(60, 5 * self.liq_fail))       # keeps failing: slow down, never a busy loop
            env = dict(os.environ, HOME="/root", XDG_RUNTIME_DIR="/run/hype")
            self.liq = subprocess.Popen(["nice", "-n", "2", "liquidsoap", self.liq_file()], env=env,
                                        stdout=subprocess.DEVNULL, stderr=open("/var/log/hype/ch-%s.err" % self.id, "a"))
            self.pushed = None
        if not self.hls or self.hls.poll() is not None:
            if self.hls is not None: self.hls_fail += 1
            self.hls = subprocess.Popen(["nice", "-n", "5", HLS_SH, self.id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_procs(self):
        for p in (self.hls, self.liq):
            if p and p.poll() is None:
                p.terminate()
                try: p.wait(8)
                except subprocess.TimeoutExpired: p.kill()
        self.liq = self.hls = None

    def tel(self, line, timeout=3):
        with socket.create_connection(("127.0.0.1", int(self.cfg["telnet"])), timeout=timeout) as s:
            s.sendall((line + "\nquit\n").encode()); data = b""
            while True:
                b = s.recv(4096)
                if not b: break
                data += b
        return data.decode(errors="ignore").replace("Bye!", "").replace("END", "").strip()

    # ---------- what plays ----------
    def recent(self, c, n=60):
        return [r[0] for r in c.execute("SELECT path FROM asrun ORDER BY started DESC LIMIT ?", (n,))]

    def pick(self, c, at):
        # this channel's Settings → Rotation: same song / same artist again after … (defaults: 3 h / 75 min, as Hype Radio)
        tsep = float(planner.get_setting(c, "title_sep_h", 3) or 3) * 3600; asep = float(planner.get_setting(c, "artist_sep_min", 75) or 0) * 60
        rec = {r[0] for r in c.execute("SELECT path FROM asrun WHERE started > ?", (at - tsep,))} | {p["path"] for p in self.plan}
        arts = []
        for (a,) in c.execute("SELECT artist FROM asrun WHERE started > ?", (at - asep,)): arts += akey(a)
        for p in self.plan: arts += akey(p.get("artist"))
        held = planner.held_songs(c, at) if hasattr(planner, "held_songs") else set()
        def ok(p_):
            if p_ in rec or p_ in held: return False
            a = c.execute("SELECT artist FROM items WHERE path=?", (p_,)).fetchone()
            return not (a and set(akey(a[0])) & set(arts))
        def pj(c_, folder):
            rows = [x[0] for x in c_.execute("SELECT path FROM items WHERE section IN ('jingles','ads') AND problem IS NULL AND (folder=? OR folder LIKE ?)", (folder, folder + "/%"))]
            rows = [x for x in rows if x not in rec] or rows
            return random.choice(rows) if rows else None
        for attempt in (ok, lambda p_: p_ not in rec, lambda p_: True):          # never silence because the rules are too tight
            try: p_ = self.runner.next(c, at, attempt, pj)
            except Exception as e: log.warning("%s: clock: %s", self.id, e); p_ = None
            if p_:
                it = item(c, p_)
                if it: return it
        # no clock / empty clock: any song of the channel's playlists, else nothing (the channel page says so)
        rows = [r[0] for r in c.execute("SELECT DISTINCT ref FROM playlist_sources WHERE kind='song'")]
        rows = [x for x in rows if x not in rec] or rows
        for p_ in random.sample(rows, min(len(rows), 5)):
            it = item(c, p_)
            if it: return it
        return None

    def refill(self, c):
        t = time.time()
        if self.now: t = max(t, self.now["started"] + self.len_of(self.now))
        for p in self.plan: t += self.len_of(p)
        while len(self.plan) < 5:
            it = self.pick(c, t)
            if not it: break
            it["at"] = t; self.plan.append(it); t += self.len_of(it)

    @staticmethod
    def len_of(it):
        cs = it.get("cue_start") or 0
        return max(3.0, (it.get("cue_mix") or it.get("cue_end") or it.get("duration") or 180) - cs)

    def fixed(self, c, n_queued):
        """Fixed times (Planning → Fixed times of this channel): due ones go in after the item that is queued now."""
        now = time.time()
        try: evs = planner.due_events(c, now, now + 5)
        except Exception as e: log.warning("%s: fixed times: %s", self.id, e); return
        for e in evs:
            paths = []
            if e["kind"] == "jingle": paths = [planner.resolve_jingle(c, e["ref"])]
            elif e["kind"] == "song": paths = [e["ref"]]
            elif e["kind"] in ("playlist", "folder"):
                pid = int(e["ref"]) if e["kind"] == "playlist" else planner.folder_playlist_id(c, e["ref"])
                k = e["amount"] if e.get("amount") and e["amount"] > 0 else len(planner.playlist_songs(c, pid))
                paths = [planner.take_from_playlist(c, pid, "start" if i == 0 else "continue", lambda x: True) for i in range(k)]
            its = [x for x in (item(c, p_) for p_ in paths if p_) if x]
            keep = 1 if (n_queued and self.plan) else 0                 # the item already queued in the radio engine stays first
            self.plan[keep:keep] = its
            c.execute("INSERT OR IGNORE INTO events_done(event_id, day) VALUES(?,?)", (e["id"], e["day"])); c.commit()
            log.info("%s: fixed time %s (%d items)", self.id, e.get("at"), len(its))

    def tick(self):
        with self.lock:
            try: n = len([x for x in self.tel("q.queue").split() if x.isdigit()])
            except OSError: return                                   # its Liquidsoap is (re)starting
            c = chancfg.connect(self.id)
            try:
                self.fixed(c, n)
                self.refill(c)
                while n == 0 and self.plan and not os.path.exists(os.path.join(ROOT, self.plan[0]["path"])):    # moved / deleted since it was planned
                    log.warning("%s: %s is gone – skipped", self.id, self.plan[0]["path"]); self.plan.pop(0); self.refill(c)
                if n == 0 and self.plan:
                    it = self.plan[0]
                    mo, jd, sm_on = self.mixcfg()
                    try: self.tel("q.push " + annotate(it, mo, jd, sm_on, self.plan[1] if len(self.plan) > 1 else None)); self.pushed = it["id"]; self.err = ""
                    except ValueError as e: log.warning("%s: %s – skipped", self.id, e); self.plan.pop(0)
                elif n == 0 and not self.plan:
                    self.err = "Nothing to play: give this channel a clock in Planning (or put songs in its playlists)."
            finally: c.close()

    def on_track(self, d):
        with self.lock:
            hid = d.get("hype_id"); it = None
            for i, p in enumerate(self.plan):
                if p["id"] == hid: it = p; del self.plan[:i + 1]; break
            c = chancfg.connect(self.id)
            try:
                if not it: it = item(c, d.get("hype_path") or "") or {"path": d.get("hype_path"), "artist": d.get("artist"), "title": d.get("title"), "section": "music", "folder": ""}
                it["started"] = time.time(); self.prev = self.now; self.now = it
                c.execute("INSERT INTO asrun(started, path, artist, title, folder, mode, kind) VALUES(?,?,?,?,?,?,?)",
                          (it["started"], it["path"], it.get("artist"), it.get("title"), it.get("folder"), "audio", it.get("section")))
                if self.runner.snap:
                    try: planner.save_clock_state(c, *self.runner.snap)
                    except Exception: pass
                c.commit()
            finally: c.close()
        log.info("%s ON AIR %s – %s", self.id, it.get("artist") or "", it.get("title") or it.get("path"))
        if not self.sim:
            try: self.over_intro(it)
            except Exception as e: log.warning("%s: over the intro: %s", self.id, e)

    def replan(self):
        with self.lock: self.plan = []; self.runner = planner.ClockRunner()

    def set_sim(self, on, auto=False):
        self.tel("var.set sim_on = %s" % ("true" if on else "false")); self.sim = bool(on); self.auto_sim = bool(on and auto)
        log.info("%s: live from Hype Radio %s", self.id, "ON" if on else "off")

    def mixcfg(self):
        """(mix length, jingles volume) from this channel's Settings → Mixing – read again every 30 s."""
        m = getattr(self, "_mix", None)
        if m and time.time() - m[0] < 30: return m[1]
        mo, jd, sm_on = MIX_OUT, -3.0, True
        try: mo = max(0.3, min(6.0, float(self.tel("var.get mix_out", 1).split()[0])))
        except (OSError, ValueError, IndexError): pass
        try:
            c = chancfg.connect(self.id); jd = float(planner.get_setting(c, "jingle_db", -3.0)); sm_on = bool(planner.get_setting(c, "smart_mix", True))
            smartmix.MIX_MAX = float(planner.get_setting(c, "mix_max", 5.0)); c.close()
        except Exception: pass
        self._mix = (time.time(), (mo, jd, sm_on)); return (mo, jd, sm_on)

    # ---------- settings (stored in the channel's own database) ----------
    # ID over soft intros – the same rules and settings as Hype Radio's (Settings → Mixing there): max per hour, minutes apart,
    # the ID's own level; it starts 0.4 s into the song and the intro dips to 50 % under the voice
    DEF = {"idi": {"on": False, "max": 3, "gap": 15, "db": -4.0, "folder": "", "duck": 0.5, "at": 0.4},
           "voice": {"on": True, "per_hour": 2, "max_plays": 3, "duck": 0.3}}
    def setting(self, k):
        c = chancfg.connect(self.id)
        try: v = planner.get_setting(c, "ch_" + k, None)
        finally: c.close()
        out = dict(self.DEF[k]); out.update(v if isinstance(v, dict) else {})
        if k == "idi" and (not out["folder"] or out["folder"] == self.id.upper()): out["folder"] = self.id.upper() + "/IDS"   # its own IDs (jingles-<id>/IDS)
        return out
    def set_setting(self, k, v):
        cur = self.setting(k); cur.update({x: v[x] for x in v if x in self.DEF[k]})
        c = chancfg.connect(self.id)
        try: planner.set_setting(c, "ch_" + k, cur)
        finally: c.close()
        return cur

    # ---------- pads: a jingle / ID / voice over the music, the music dips (duck) while it plays ----------
    def glide(self, to, secs=0.4):
        def run():
            try:
                cur = float((self.tel("var.get duck_lvl", 1) or "1").split()[0])
            except (OSError, ValueError): cur = 1.0
            for i in range(1, 6):
                v = cur + (to - cur) * i / 5
                try: self.tel("var.set duck_lvl = %.3f" % v, 1)
                except OSError: return
                time.sleep(secs / 5)
        threading.Thread(target=run, daemon=True).start()
    def fire(self, path, duck=None, kind="pad", gain_db=0.0):
        c = chancfg.connect(self.id)
        try: it = item(c, path)
        finally: c.close()
        if not it: raise ValueError("not in the library (or a problem file)")
        gain = (0.0 if it["lufs"] is None else max(-12.0, min(12.0, TARGET_LUFS - it["lufs"]))) + gain_db
        cs = it["cue_start"] or 0.0; ce = it["cue_end"] or it["duration"] or 0
        self.tel(f'pads.push annotate:liq_cue_in="{cs:.2f}",liq_cue_out="{ce:.2f}",liq_amplify="{gain:.2f} dB":{os.path.join(ROOT, it["path"])}')
        ln = max(1.0, ce - cs); self.pad_now = {"path": path, "title": it["title"] or os.path.basename(path), "kind": kind, "until": time.time() + ln}
        if duck:
            self.glide(float(duck)); t0 = time.time(); self.duck_t = t0
            threading.Timer(duck_len(it), lambda: self.duck_t == t0 and self.glide(1.0)).start()     # music back when the voice ends
        log.info("%s: %s %s%s", self.id, kind, os.path.basename(path), " (ducked)" if duck else "")
    def pad_stop(self):
        try: self.tel("pads.stop")
        finally: self.duck_t = None; self.glide(1.0); self.pad_now = None

    def voice_pick(self, c, max_len=None):
        st = {}
        try: st = json.load(open("/var/lib/hype/voice.json"))
        except (OSError, ValueError): pass
        cfg = self.setting("voice"); pf = "/var/lib/hype/voice_plays_%s.json" % self.id
        try: plays = json.load(open(pf))
        except (OSError, ValueError): plays = {}
        now = time.time()
        recent = [(t, p) for p, ts in plays.items() for t in ts if now - t < 3600]
        if len(recent) >= cfg["per_hour"]: return None
        who = lambda v: ((v.get("name") or "") + "|" + (v.get("city") or "")).lower()
        recent_who = {who(v) for v in st.values() for t, p in recent if v.get("path") == p}
        best = None
        for v in st.values():
            p_ = v.get("path")
            if v.get("station") != self.id or not v.get("air") or not p_ or now - v.get("received", now) > 14 * 86400: continue
            n = len(plays.get(p_, []))
            if n >= cfg["max_plays"] or who(v) in recent_who: continue
            r_ = c.execute("SELECT COALESCE(cue_end, duration, 0) - COALESCE(cue_start, 0) FROM items WHERE path=? AND problem IS NULL", (p_,)).fetchone()
            if not r_ or (max_len and (r_[0] or 0) + 0.4 + 1.0 > max_len): continue          # it has to fit in the soft intro
            key = (n, -v.get("received", 0))
            if best is None or key < best[0]: best = (key, p_)
        if not best: return None
        plays.setdefault(best[1], []).append(now)
        try: json.dump(plays, open(pf + ".tmp", "w")); os.replace(pf + ".tmp", pf)
        except OSError: pass
        return best[1]

    def over_intro(self, it):
        """A song starts: a listener voice or an ID over its soft intro – Hype Radio's rules (engine.py pick_voice_intro /
        pick_intro_id): only song -> song (never right after a jingle / ID / liner), never on a mix or a song set to "no sweeper",
        only when the intro is soft for 2.5 s+ and the voice ends 1 s before the song is at full level; IDs: max N per hour,
        M minutes apart, the one that rested longest (never the same within 20 min), at their own level."""
        if it.get("section") != "music": return
        prev = getattr(self, "prev", None) or {}
        if prev.get("section") != "music": return
        if any((x.get("folder") or "").upper().startswith(("MIXES", "RECORDINGS")) for x in (it, prev)): return
        now = time.time()
        if now - getattr(self, "ov_last", 0) < 180: return
        nl = smartmix.intro_len(it)
        if not nl or nl < 2.5: return
        c = chancfg.connect(self.id)
        try:
            r = c.execute("SELECT overlay_mode FROM items WHERE path=?", (it["path"],)).fetchone()
            if r and r[0] in ("none", "song"): return
            vc, ic = self.setting("voice"), self.setting("idi")
            p_, kind, duck, at, gdb = None, None, None, 0.4, 0.0
            if vc["on"]:
                p_ = self.voice_pick(c, max_len=nl); kind, duck = "voice", ic["duck"]
            if not p_ and ic["on"]:
                self.idi_times = [t for t in getattr(self, "idi_times", []) if now - t < 3600]
                if len(self.idi_times) >= int(ic["max"]) or (self.idi_times and now - self.idi_times[-1] < float(ic["gap"]) * 60): return
                used = getattr(self, "idi_used", {}); best = None
                for (jp,) in c.execute("SELECT path FROM items WHERE section='jingles' AND folder=? AND problem IS NULL", (ic["folder"],)).fetchall():
                    ji = item(c, jp)
                    if not ji or duck_len(ji) + float(ic["at"]) + 1.0 > nl: continue
                    la = c.execute("SELECT MAX(started) FROM asrun WHERE path=? OR over=?", (jp, "ID: " + (ji.get("title") or ""))).fetchone()[0] or 0
                    rested = max(la, used.get(jp, 0))
                    if now - rested < 20 * 60: continue
                    if best is None or rested < best[0]: best = (rested, jp, ji.get("title") or os.path.basename(jp))
                if not best: return
                p_, kind, duck, at, gdb = best[1], "ID", ic["duck"], float(ic["at"]), float(ic["db"])
                self.idi_times.append(now); used[p_] = now; self.idi_used = used
            if not p_: return
            self.ov_last = now
            t_ = c.execute("SELECT title FROM items WHERE path=?", (p_,)).fetchone()
            c.execute("UPDATE asrun SET over=? WHERE id=(SELECT max(id) FROM asrun WHERE path=?)", (("Voice" if kind == "voice" else "ID") + ": " + ((t_ or [""])[0] or os.path.basename(p_)), it["path"])); c.commit()
        finally: c.close()
        def go():
            try: self.fire(p_, duck, kind, gdb)
            except Exception as e: log.warning("%s: %s over the intro: %s", self.id, kind, e)
        threading.Timer(at, go).start()

    # ---------- the playlist (Next) by hand ----------
    def plan_edit(self, a, d):
        with self.lock:
            keep = 1 if (self.plan and self.pushed == self.plan[0]["id"]) else 0          # the item queued in the radio engine stays
            if a == "insert":
                c = chancfg.connect(self.id)
                try: it = item(c, str(d.get("path")))
                finally: c.close()
                if not it: raise ValueError("not in the library")
                it["manual"] = True
                if d.get("where") == "end": self.plan.append(it)
                else: self.plan.insert(keep, it)
            elif a in ("remove", "up", "down"):
                i = next((k for k, p in enumerate(self.plan) if p["id"] == d.get("id")), None)
                if i is None or i < keep: raise ValueError("that one is already queued – use Skip")
                if a == "remove": self.plan.pop(i)
                else:
                    j = i + (-1 if a == "up" else 1)
                    if j < keep or j >= len(self.plan): return
                    self.plan[i], self.plan[j] = self.plan[j], self.plan[i]
            t = time.time()
            if self.now: t = max(t, self.now["started"] + self.len_of(self.now))
            for p in self.plan: p["at"] = t; t += self.len_of(p)

    # ---------- the loop ----------
    def run(self):
        log.info("%s: on", self.id); last_aud = 0
        while not self.stop_ev.is_set():
            try:
                self.start_procs(); self.tick()
                try:
                    v = self.tel("radio.level", 1).split()
                    vals = [float(x) for x in v]; self.level = vals[:2] if len(vals) >= 2 else None; self.meter = vals
                except (OSError, ValueError): self.level = None
                # watchdog: the sound chain went NaN ("not a number" – the stream is then silent, 20 Nov Hype Latina) ->
                # restart this channel's radio engine; its playlist stays and goes on with the next item
                if self.level and any(x != x for x in self.level):
                    self.nan_n = getattr(self, "nan_n", 0) + 1
                    if self.nan_n >= 3:
                        log.warning("%s: sound broke (NaN) during %s – radio engine restarted", self.id, (self.now or {}).get("path"))
                        self.err = "The sound broke at %s and the radio engine restarted by itself." % time.strftime("%H:%M")
                        self.nan_n = 0; self.liq.terminate()
                        try: self.liq.wait(8)
                        except subprocess.TimeoutExpired: self.liq.kill()
                        self.liq_fail -= 1          # not a crash: no slow-down
                else: self.nan_n = 0
                # watchdog 2: stuck – the song on air should have ended long ago and it is silent (e.g. the queued file was
                # moved / deleted after it was queued: the radio engine keeps retrying it) -> clear the queue, go on with the next
                nw = self.now or {}
                if (not self.sim and nw.get("started") and time.time() - nw["started"] > self.len_of(nw) + 20
                        and self.level and max(self.level) < 0.001):
                    self.stuck_n = getattr(self, "stuck_n", 0) + 1
                    if self.stuck_n >= 5:
                        log.warning("%s: stuck after %s – queue cleared, going on", self.id, nw.get("path"))
                        self.stuck_n = 0
                        try: self.tel("q.flush_and_skip")
                        except OSError: pass
                        with self.lock:
                            if self.plan and self.pushed == self.plan[0]["id"]: self.plan.pop(0)
                            self.pushed = None; self.now = dict(nw, started=time.time())
                else: self.stuck_n = 0
                if time.time() - last_aud >= 15:
                    last_aud = time.time(); self.count_listeners()
                if self.cfg.get("join_live"):            # "join Hype Radio's live shows by itself": on while Hype Radio's studio is on air
                    ml = MAIN_LIVE["on"]
                    if ml and not self.sim: self.set_sim(True, auto=True)
                    elif not ml and self.sim and self.auto_sim: self.set_sim(False)
            except Exception as e:
                log.warning("%s: %s", self.id, e); time.sleep(2)
            self.stop_ev.wait(1)
        self.stop_procs(); log.info("%s: off", self.id)

    def count_listeners(self):
        mp3 = 0
        try:
            st = json.load(urllib.request.urlopen("http://127.0.0.1:8000/status-json.xsl", timeout=3))
            src = st.get("icestats", {}).get("source") or []
            if isinstance(src, dict): src = [src]
            mp3 = sum(int(s.get("listeners") or 0) for s in src if str(s.get("listenurl", "")).endswith("/" + self.cfg["mount"]))
        except Exception: pass
        hls = 0
        try:
            with open(NGINX_LOG, "rb") as f:
                f.seek(0, 2); f.seek(max(0, f.tell() - 1_500_000)); tail = f.read().decode(errors="ignore").splitlines()
            ips, now = set(), time.time(); pat = "/ch/%s/" % self.id
            for ln in reversed(tail):
                if pat not in ln: continue
                m = re.match(r'(\S+) .*?\[([^\]]+)\]', ln)
                if not m: continue
                try: t = time.mktime(time.strptime(m.group(2).split()[0], "%d/%b/%Y:%H:%M:%S"))
                except ValueError: continue
                if now - t > 30: break
                ips.add(m.group(1))
            hls = len(ips)
        except OSError: pass
        self.listeners = {"mp3": mp3, "hls": hls}
        try:
            c = chancfg.connect(self.id); c.execute("INSERT OR REPLACE INTO aud_samples VALUES(?,?,?,?)", (time.time(), mp3 + hls, 0, 0)); c.commit(); c.close()
        except Exception: pass

    def status(self):
        def brief(x):
            if not x: return None
            return {"id": x.get("id"), "manual": bool(x.get("manual")), "artist": x.get("artist") or "", "title": x.get("title") or os.path.basename(x.get("path") or ""), "path": x.get("path"),
                    "section": x.get("section"), "started": x.get("started"), "at": x.get("at"), "len": round(self.len_of(x))}
        return {"id": self.id, "name": self.cfg["name"], "color": self.cfg["color"], "logo": self.cfg.get("logo"), "genre": self.cfg.get("genre"), "enabled": True,
                "running": bool(self.liq and self.liq.poll() is None), "hls": bool(self.hls and self.hls.poll() is None),
                "now": brief(self.now), "next": [brief(p) for p in self.plan[:5]], "sim": self.sim, "listeners": self.listeners,
                "level": self.level, "meter": getattr(self, "meter", None), "error": self.err, "mount": self.cfg["mount"], "join_live": bool(self.cfg.get("join_live")), "main_live": MAIN_LIVE["on"],
                "pad": self.pad_now if self.pad_now and self.pad_now["until"] > time.time() else None, "queued": self.pushed,
                "idi": self.setting("idi"), "voice": self.setting("voice"), "history": self.last_played()}

    def nowplaying(self):
        """The same answer as Hype Radio's /api/nowplaying/hype_radio (AzuraCast style): now playing, next, the last 10 songs
        (song_history, "recently played" on the website / apps), covers from the shared cover store (else the station logo).
        Only songs: while a jingle / ID plays, "now playing" stays the last song."""
        import hashlib
        logo = "https://stream.hyperadio.nl/logo/%s.png" % self.id; t = time.time()
        def art(a, ti):
            k = hashlib.sha1(("%s|%s" % ((a or "").lower().strip(), (ti or "").lower().strip())).encode()).hexdigest()[:16]
            return "https://stream.hyperadio.nl/cover/%s.jpg" % k if a and ti and os.path.exists("/srv/hype-data/covers/%s.jpg" % k) else logo
        def song(a, ti, folder=""):
            a, ti = a or "", ti or ""
            return {"id": hashlib.md5((a + ti).encode()).hexdigest(), "text": (a + " - " + ti) if a else ti, "artist": a, "title": ti, "album": "",
                    "genre": folder or "", "art": art(a, ti), "custom_fields": {}}
        rows = []
        try:
            c = chancfg.connect(self.id)
            rows = c.execute("SELECT started, artist, title, folder, path FROM asrun WHERE section_ok(path) ORDER BY started DESC LIMIT 11".replace(
                "section_ok(path)", "(path LIKE 'music/%' OR path LIKE 'music-%')")).fetchall(); c.close()
        except Exception: pass
        n = self.now or {}
        if n.get("section") != "music" and rows:
            r0 = rows[0]; n = {"artist": r0[1], "title": r0[2], "folder": r0[3], "started": r0[0], "path": r0[4], "section": "music"}
        dur = round(self.len_of(n)) if n.get("path") else 0; el = max(0, t - (n.get("started") or t))
        nxt = next((x for x in self.plan if x and x.get("section") == "music"), None)
        hist = [{"played_at": int(r[0]), "duration": 0, "playlist": r[3] or "", "is_request": False, "song": song(r[1], r[2], r[3])}
                for r in rows if int(r[0]) != int(n.get("started") or 0)][:10]
        L = self.listeners or {}
        return {"station": {"id": self.id, "name": self.cfg["name"], "shortcode": self.id, "listen_url": "https://stream.hyperadio.nl/" + self.cfg["mount"],
                            "hls_url": "https://stream.hyperadio.nl/%s.m3u8" % self.id, "hls_enabled": True, "is_public": True, "logo": logo,
                            "mounts": [{"name": "MP3 128", "url": "https://stream.hyperadio.nl/" + self.cfg["mount"], "bitrate": 128, "format": "mp3", "is_default": True}]},
                "live": {"is_live": bool(self.sim), "streamer_name": "", "broadcast_start": None},
                "now_playing": {"sh_id": int(n.get("started") or 0), "played_at": int(n.get("started") or t), "duration": dur, "elapsed": round(el),
                                "remaining": round(max(0, dur - el)), "playlist": n.get("folder") or "", "streamer": "", "is_request": False,
                                "song": song(n.get("artist"), n.get("title"), n.get("folder")), "type": "song"},
                "playing_next": {"song": song(nxt.get("artist"), nxt.get("title"), nxt.get("folder"))} if nxt else None,
                "song_history": hist, "listeners": {"current": (L.get("mp3") or 0) + (L.get("hls") or 0)}, "is_online": bool(self.liq and self.liq.poll() is None),
                "hype": {"source": "hype-studio", "station": self.id}}

    def last_played(self, n=3):
        """The last songs (as on Hype Radio's playlist: the 2 before the one on air)."""
        try:
            c = chancfg.connect(self.id)
            rows = c.execute("SELECT started, artist, title, path FROM asrun ORDER BY started DESC LIMIT ?", (n,)).fetchall(); c.close()
            return [{"started": r[0], "artist": r[1] or "", "title": r[2] or os.path.basename(r[3] or ""), "path": r[3]} for r in rows]
        except Exception: return []


MAIN_LIVE = {"on": False}
def main_live_watch():
    """Is Hype Radio's studio on air (LIVE)? Read-only question to Hype Radio's radio engine, every 3 s."""
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 1234), timeout=2) as s:
                s.sendall(b"live.source\nquit\n"); data = b""
                while True:
                    b = s.recv(1024)
                    if not b: break
                    data += b
            MAIN_LIVE["on"] = b"studio" in data
        except OSError: pass
        time.sleep(3)

CH = {}
def sync():
    """Start / stop channels to match channels.json."""
    want = {c["id"]: c for c in chancfg.load() if c.get("enabled")}
    for cid in list(CH):
        if cid not in want:
            ch = CH.pop(cid); ch.stop_ev.set()
    for cid, cfg in want.items():
        if cid not in CH:
            ch = Channel(cfg); CH[cid] = ch
            ch.thread = threading.Thread(target=ch.run, daemon=True, name="ch-" + cid); ch.thread.start()
        else: CH[cid].cfg.update(cfg)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/status":
            out = []
            for c in chancfg.load():
                out.append(CH[c["id"]].status() if c["id"] in CH else {"id": c["id"], "name": c["name"], "color": c["color"], "logo": c.get("logo"), "genre": c.get("genre"),
                                                                         "enabled": False, "running": False, "mount": c["mount"]})
            return self.send(200, {"channels": out})
        m = re.match(r"^/nowplaying/([a-z0-9]+)$", p)
        if m and m.group(1) in CH: return self.send(200, CH[m.group(1)].nowplaying())
        return self.send(404, {"error": "unknown"})
    def do_POST(self):
        try: d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError: return self.send(400, {"error": "bad json"})
        p = self.path.split("?")[0]
        if p == "/track":
            ch = CH.get(str(d.get("ch")))
            if ch: threading.Thread(target=ch.on_track, args=(d,), daemon=True).start()
            return self.send(200, {"ok": True})
        if p == "/cmd":
            cid, a = str(d.get("ch") or ""), str(d.get("action") or "")
            if a in ("enable", "disable"):
                chs = chancfg.load()
                for c in chs:
                    if c["id"] == cid: c["enabled"] = a == "enable"
                chancfg.save(chs); sync(); return self.send(200, {"ok": True})
            ch = CH.get(cid)
            if not ch: return self.send(400, {"ok": False, "error": "that channel is off"})
            try:
                if a == "skip": ch.tel("radio.skip")
                elif a == "join_live":
                    chs = chancfg.load()
                    for c in chs:
                        if c["id"] == cid: c["join_live"] = bool(d.get("on"))
                    chancfg.save(chs); ch.cfg["join_live"] = bool(d.get("on"))
                    if not d.get("on") and ch.auto_sim: ch.set_sim(False)
                elif a == "sim_on": ch.set_sim(True)
                elif a == "sim_off": ch.set_sim(False)
                elif a == "replan": ch.replan()
                elif a == "pad": ch.fire(str(d.get("path")), float(d["duck"]) if d.get("duck") else None)
                elif a == "pad_stop": ch.pad_stop()
                elif a in ("insert", "remove", "up", "down"): ch.plan_edit(a, d)
                elif a == "set": ch.set_setting(str(d.get("what")), d.get("value") or {}) if d.get("what") in ("idi", "voice") else None
                else: return self.send(400, {"ok": False, "error": "unknown action"})
            except ValueError as e: return self.send(400, {"ok": False, "error": str(e)})
            except OSError as e: return self.send(503, {"ok": False, "error": "the channel's radio engine is starting – try again (%s)" % e})
            return self.send(200, {"ok": True, "status": ch.status()})
        return self.send(404, {"error": "unknown"})


def main():
    os.makedirs(RUN, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", chancfg.API_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=main_live_watch, daemon=True).start()
    def bye(*_):
        for ch in CH.values(): ch.stop_ev.set()
        for ch in CH.values():
            if ch.thread: ch.thread.join(12)
        os._exit(0)
    signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)
    log.info("hype-channels started")
    while True:
        try: sync()
        except Exception as e: log.warning("sync: %s", e)
        time.sleep(20)


if __name__ == "__main__":
    main()
