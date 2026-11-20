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
import planner, chancfg

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


def annotate(it):
    """The telnet line for one item: cue points, mix length, song gain (a simpler copy of Hype Radio's annotate)."""
    cs = it["cue_start"] or 0.0; dur = it["duration"] or it["cue_end"] or 0; ce = it["cue_end"] or dur
    cm = it["cue_mix"] if it["cue_mix"] and it["cue_mix"] > cs else ce
    if it["section"] in ("jingles", "ads"):
        cross = max(0.3, min(8.0, ce - cm)); fade = 0.0            # jingles play out to their END under the next song
    else:
        end_ = max(ce, dur or ce)
        if not it["cues_edited"] and end_ - cm < MIX_OUT: cm = max(cs, end_ - MIX_OUT)
        cross = max(0.3, min(6.0, MIX_OUT, end_ - cm)); fade = cross
    ce = cm + cross
    gain = 0.0 if it["lufs"] is None else max(-12.0, min(12.0, TARGET_LUFS - it["lufs"]))
    if BAD.search(it["path"]): raise ValueError("file name with control characters")
    q = lambda v: BAD.sub(" ", str(v or "")).replace("\\", "/").replace('"', "'")
    meta = (f'liq_cue_in="{cs:.2f}",liq_cue_out="{ce:.2f}",liq_cross_duration="{cross:.2f}",liq_amplify="{gain:.2f} dB",'
            f'hype_fade="{fade:.2f}",artist="{q(it["artist"])}",title="{q(it["title"])}",hype_path="{q(it["path"])}",hype_id="{it["id"]}"')
    return f"annotate:{meta}:{os.path.join(ROOT, it['path'])}"


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
        rec = set(self.recent(c, 40)) | {p["path"] for p in self.plan}
        arts = []
        for (a,) in c.execute("SELECT artist FROM asrun ORDER BY started DESC LIMIT 12"): arts += akey(a)
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
                if n == 0 and self.plan:
                    it = self.plan[0]
                    try: self.tel("q.push " + annotate(it)); self.pushed = it["id"]; self.err = ""
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
                it["started"] = time.time(); self.now = it
                c.execute("INSERT INTO asrun(started, path, artist, title, folder, mode, kind) VALUES(?,?,?,?,?,?,?)",
                          (it["started"], it["path"], it.get("artist"), it.get("title"), it.get("folder"), "audio", it.get("section")))
                if self.runner.snap:
                    try: planner.save_clock_state(c, *self.runner.snap)
                    except Exception: pass
                c.commit()
            finally: c.close()
        log.info("%s ON AIR %s – %s", self.id, it.get("artist") or "", it.get("title") or it.get("path"))

    def replan(self):
        with self.lock: self.plan = []; self.runner = planner.ClockRunner()

    def set_sim(self, on, auto=False):
        self.tel("var.set sim_on = %s" % ("true" if on else "false")); self.sim = bool(on); self.auto_sim = bool(on and auto)
        log.info("%s: live from Hype Radio %s", self.id, "ON" if on else "off")

    # ---------- the loop ----------
    def run(self):
        log.info("%s: on", self.id); last_aud = 0
        while not self.stop_ev.is_set():
            try:
                self.start_procs(); self.tick()
                try:
                    v = self.tel("radio.level", 1).split()
                    self.level = [float(x) for x in v[:2]] if len(v) >= 2 else None
                except (OSError, ValueError): self.level = None
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
            return {"artist": x.get("artist") or "", "title": x.get("title") or os.path.basename(x.get("path") or ""), "path": x.get("path"),
                    "section": x.get("section"), "started": x.get("started"), "at": x.get("at"), "len": round(self.len_of(x))}
        return {"id": self.id, "name": self.cfg["name"], "color": self.cfg["color"], "logo": self.cfg.get("logo"), "genre": self.cfg.get("genre"), "enabled": True,
                "running": bool(self.liq and self.liq.poll() is None), "hls": bool(self.hls and self.hls.poll() is None),
                "now": brief(self.now), "next": [brief(p) for p in self.plan[:5]], "sim": self.sim, "listeners": self.listeners,
                "level": self.level, "error": self.err, "mount": self.cfg["mount"], "join_live": bool(self.cfg.get("join_live")), "main_live": MAIN_LIVE["on"]}


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
        if m and m.group(1) in CH:
            s = CH[m.group(1)].status(); n = s["now"] or {}
            return self.send(200, {"station": {"name": s["name"], "listen_url": "https://stream.hyperadio.nl/" + s["mount"], "hls_url": "https://stream.hyperadio.nl/%s.m3u8" % s["id"], "logo": "https://stream.hyperadio.nl/logo/%s.png" % s["id"]},
                                   "now_playing": {"song": {"artist": n.get("artist", ""), "title": n.get("title", "")}, "played_at": int(n.get("started") or 0)},
                                   "playing_next": {"song": {"artist": (s["next"][0] or {}).get("artist", ""), "title": (s["next"][0] or {}).get("title", "")}} if s["next"] else None,
                                   "listeners": {"current": s["listeners"]["mp3"] + s["listeners"]["hls"]}, "live": {"is_live": s["sim"]}})
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
                else: return self.send(400, {"ok": False, "error": "unknown action"})
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
