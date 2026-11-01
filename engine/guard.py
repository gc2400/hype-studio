#!/usr/bin/env python3
"""Hype Studio guard – watches radio + TV around the clock, repairs what it can by itself, and reports to Hype Live.

Every 5 s it checks:
  radio   – Liquidsoap answers + the on-air level (silence = peak under -50 dB)
  engine  – the playout engine answers (/api/state)
  tv      – OBS is streaming and the TV stream (HLS) keeps getting new pieces
  stream  – the radio mount on Icecast exists
Repairs (each part at most 3 times per 30 minutes, then it only alarms):
  silence 25 s           → restart the engine (it queues the music again)
  still silent 60 s      → restart the radio (the engine restarts with it)
  Liquidsoap not answering 20 s → restart the radio
  engine not answering 30 s     → restart the engine
  OBS not streaming 20 s        → start streaming; OBS not answering 60 s → restart OBS
  no new TV pieces 20 s         → restart the TV ladder
Every 60 s a heartbeat goes to Hype Live (when a key is set): https://hyperadio.nl/?hype_studio=KEY (JSON).
Pause (deploys): touch /run/hype/guard_pause  (ignored when older than 15 min).
Status for the panel: /var/lib/hype/guard.json · settings: /var/lib/hype/guard_conf.json
"""
import json, os, socket, subprocess, time, urllib.request, logging, collections

STATUS = "/var/lib/hype/guard.json"
CONF = "/var/lib/hype/guard_conf.json"
PAUSE = "/run/hype/guard_pause"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("guard")
logging.getLogger("obsws_python").setLevel(logging.WARNING)   # (it logs the password on every connect)

DEFAULT = {"hl_url": "https://hyperadio.nl/", "hl_key": "", "heartbeat": True, "repair": True}
def conf():
    try: return dict(DEFAULT, **json.load(open(CONF)))
    except Exception: return dict(DEFAULT)

def telnet(cmd, timeout=3):
    s = socket.create_connection(("127.0.0.1", 1234), timeout=timeout)
    try:
        s.sendall((cmd + "\nquit\n").encode()); buf = b""
        while b"END" not in buf and b"Bye" not in buf:
            b_ = s.recv(4096)
            if not b_: break
            buf += b_
        return buf.decode("utf-8", "replace").split("\r\n")[0].strip()
    finally: s.close()

def http_json(url, timeout=4):
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read())

_OBS = {"cl": None}
def obs_streaming():
    """(streaming?, client) – one connection, made again when it breaks."""
    import obsws_python as obs
    for _ in range(2):
        try:
            if _OBS["cl"] is None:
                c = json.load(open("/etc/hype/engine.json"))
                _OBS["cl"] = obs.ReqClient(host="127.0.0.1", port=4455, password=c["obs_password"], timeout=4)
            return bool(_OBS["cl"].get_stream_status().output_active), _OBS["cl"]
        except Exception:
            _OBS["cl"] = None
    return None, None

def seg_age():
    d = "/dev/shm/hls/v0"
    try:
        t = max((os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d) if f.endswith(".ts")), default=0)
        return round(time.time() - t, 1) if t else None
    except OSError: return None

def systemctl(*a):
    return subprocess.run(["systemctl", *a], capture_output=True, text=True, timeout=60).returncode == 0

class Guard:
    def __init__(self):
        self.since = {}                    # problem id -> since (time)
        self.done = collections.defaultdict(list)   # part -> restart times
        self.events = collections.deque(maxlen=40)  # what happened (shown in the panel + sent to Hype Live)
        self.state = {}
        self.hb_t = 0; self.hb = {"ok": None, "t": 0, "answer": ""}
        self.step = {}
    def event(self, level, text):
        self.events.appendleft({"t": time.time(), "level": level, "text": text}); log.info("%s: %s", level, text)
    def bad(self, pid, is_bad):
        if is_bad: self.since.setdefault(pid, time.time())
        else:
            if pid in self.since and time.time() - self.since[pid] > 10: self.event("ok", PIDS.get(pid, pid) + " – fine again")
            self.since.pop(pid, None); self.step.pop(pid, None)
        return time.time() - self.since[pid] if is_bad else 0
    def may_restart(self, part):
        now = time.time(); self.done[part] = [t for t in self.done[part] if now - t < 1800]
        return len(self.done[part]) < 3
    def restart(self, part, unit, why):
        if not conf().get("repair", True): return self.event("warn", f"{why} – automatic repair is off")
        if not self.may_restart(part):
            if self.step.get(part + "_gaveup") is None:
                self.step[part + "_gaveup"] = 1; self.event("bad", f"{why} – restarted 3× in 30 min, stopped trying: needs you")
            return
        self.done[part].append(time.time()); self.event("repair", f"{why} → restarting {unit}")
        systemctl("restart", unit)

    def check(self):
        paused = os.path.exists(PAUSE) and time.time() - os.path.getmtime(PAUSE) < 900
        st = {"t": time.time(), "paused": paused}
        # --- engine ---
        eng = None
        try: eng = http_json("http://127.0.0.1:8090/state")
        except Exception: pass
        st["engine"] = bool(eng)
        stopped = bool(eng and eng.get("stopped")); live = bool(eng and eng.get("dj") and False)
        try: live = http_json("http://127.0.0.1:8090/live").get("air") == "studio"
        except Exception: pass
        st["stopped"] = stopped; st["live"] = live
        st["song"] = ((eng or {}).get("now") or {}).get("artist", "") + " – " + ((eng or {}).get("now") or {}).get("title", "") if eng and eng.get("now") else ""
        # --- radio ---
        level = None
        try:
            v = telnet("radio.level").split(); level = max(float(v[0]), float(v[1]))
        except Exception: pass
        st["liq"] = level is not None; st["level"] = level
        silent = level is not None and level < 0.003 and not stopped
        st["silent_s"] = round(self.bad("silence", silent))
        # --- icecast ---
        try:
            ice = http_json("http://127.0.0.1:8000/status-json.xsl")["icestats"].get("source") or []
            ice = ice if isinstance(ice, list) else [ice]
            m = next((x for x in ice if str(x.get("listenurl", "")).endswith("/radio.mp3")), None)    # the public MP3 stream
            st["mount"] = bool(m); st["listeners"] = int(m.get("listeners", 0)) if m else 0
        except Exception: st["mount"] = False
        # --- tv ---
        obs_on, cl = None, None
        try: obs_on, cl = obs_streaming()
        except Exception: pass
        st["obs"] = obs_on; st["seg_age"] = seg_age()
        try:
            rt = max((os.path.getmtime(os.path.join("/dev/shm/radio-hls", f)) for f in os.listdir("/dev/shm/radio-hls") if f.endswith(".ts")), default=0)
            st["radio_hls_age"] = round(time.time() - rt, 1) if rt else None
        except OSError: st["radio_hls_age"] = None
        if paused:
            for k in list(self.since): self.since[k] = time.time()   # don't count problems during a deploy
            self.state = st; return
        # --- decisions ---
        t = self.bad("liq_down", level is None)
        if t > 20: self.restart("radio", "hype-radio", "Radio engine (Liquidsoap) not answering"); self.since["liq_down"] = time.time()
        t = self.bad("engine_down", not eng)
        if t > 30: self.restart("engine", "hype-engine", "Playout engine not answering"); self.since["engine_down"] = time.time()
        t = st["silent_s"]
        if silent and not live:
            if t > 25 and self.step.get("silence", 0) == 0:
                self.step["silence"] = 1; self.restart("engine", "hype-engine", "Silence on air for 25 s")
            elif t > 60 and self.step.get("silence", 0) == 1:
                self.step["silence"] = 2; self.restart("radio", "hype-radio", "Still silence on air after 60 s")
        elif silent and live and t > 30 and not self.step.get("silence_live"):
            self.step["silence_live"] = 1; self.event("bad", "Silence while LIVE from the studio (no automatic repair while live)")
        t = self.bad("obs_down", obs_on is None)
        if t > 60: self.restart("obs", "hype-obs", "OBS not answering"); self.since["obs_down"] = time.time()
        t = self.bad("obs_off", obs_on is False)
        if t > 20 and cl is not None and not self.step.get("obs_off"):
            self.step["obs_off"] = 1; self.event("repair", "OBS was not streaming → start streaming")
            try: cl.start_stream()
            except Exception as e: self.event("warn", f"start streaming failed: {e}")
        elif t > 60: self.restart("obs", "hype-obs", "OBS still not streaming"); self.since["obs_off"] = time.time()
        sa = st["seg_age"]
        t = self.bad("hls_late", bool(obs_on) and (sa is None or sa > 20))
        if t > 20: self.restart("ladder", "hype-ladder", f"TV stream: no new pieces for {sa} s"); self.since["hls_late"] = time.time()
        self.bad("mount", not st["mount"])
        ra = st.get("radio_hls_age")
        t = self.bad("radio_hls", ra is None or ra > 20)
        if t > 20: self.restart("radio_hls", "hype-radio-hls", f"Radio HLS stream: no new pieces for {ra} s"); self.since["radio_hls"] = time.time()
        self.state = st

    def problems(self):
        out = []
        for pid, s in self.since.items():
            dur = time.time() - s
            if dur >= MIN_SHOW.get(pid, 15): out.append({"id": pid, "text": PIDS.get(pid, pid), "since": s, "level": "bad"})
        return out

    def write(self):
        d = dict(self.state, problems=self.problems(), events=list(self.events)[:20], heartbeat=self.hb, ok=not self.problems())
        tmp = STATUS + ".tmp"; json.dump(d, open(tmp, "w")); os.replace(tmp, STATUS)
        return d

    def heartbeat(self, d):
        c = conf()
        if not c.get("heartbeat") or not c.get("hl_key"): self.hb = {"ok": None, "t": 0, "answer": "no Hype Live key set"}; return
        if time.time() - self.hb_t < 60: return
        self.hb_t = time.time()
        body = {"v": 1, "source": "hype-studio", "t": int(time.time()), "ok": d["ok"],
                "radio": {"on_air": d.get("liq") and not d.get("stopped"), "silent_s": d.get("silent_s", 0), "song": d.get("song", ""), "stream": d.get("mount"), "listeners": d.get("listeners", 0), "live": d.get("live")},
                "tv": {"streaming": d.get("obs"), "seg_age": d.get("seg_age")},
                "problems": [{"id": p["id"], "text": p["text"], "since": int(p["since"])} for p in d["problems"]],
                "events": [{"t": int(e["t"]), "level": e["level"], "text": e["text"]} for e in d["events"][:10]]}
        try: sev = json.load(open("/var/lib/hype/screen_events.json"))
        except Exception: sev = []
        try:                                          # own streams: radio listeners + TV viewers (now, peak + unique today)
            au = http_json("http://127.0.0.1:8090/audience"); body["audience"] = {"radio": au["radio"], "tv": au["tv"]}
            body["radio"]["listeners"] = au["radio"]["current"]; body["tv"]["viewers"] = au["tv"]["current"]
        except Exception: pass
        if sev: body["screen_events"] = sev[:200]     # PIP showings etc. (Hype Live logs the ones whose brand is a sponsor)
        url = c["hl_url"].rstrip("/") + "/?hype_studio=" + urllib.request.quote(c["hl_key"])
        try:
            r = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "HypeStudio-Guard/1"})
            a = urllib.request.urlopen(r, timeout=10).read(200).decode("utf-8", "replace").strip()
            self.hb = {"ok": a.startswith("OK"), "t": time.time(), "answer": a[:80] if len(a) < 80 else "(Hype Live does not know this link yet – plugin update needed)"}
            if a.startswith("OK") and sev and "ev=" in a:   # Hype Live confirmed it stored them ("OK ev=N"): keep only what came in meanwhile
                try:
                    p_ = "/var/lib/hype/screen_events.json"; now_ev = json.load(open(p_))
                    tmp = p_ + ".guard.tmp"
                    with open(tmp, "w") as f_: json.dump(now_ev[len(sev[:200]):], f_)
                    os.replace(tmp, p_)   # atomic: the engine never reads half a file
                except Exception: pass
        except Exception as e:
            self.hb = {"ok": False, "t": time.time(), "answer": str(e)[:80]}

PIDS = {"silence": "Silence on air", "liq_down": "Radio engine not answering", "engine_down": "Playout engine not answering",
        "obs_down": "OBS not answering", "obs_off": "OBS not streaming", "hls_late": "TV stream stalled", "mount": "Radio stream (MP3) missing", "radio_hls": "Radio HLS stream stalled"}
MIN_SHOW = {"silence": 15, "mount": 30}

if __name__ == "__main__":
    g = Guard(); g.event("ok", "guard started")
    while True:
        try:
            g.check(); d = g.write(); g.heartbeat(d)
        except Exception as e: log.warning("check: %s", e)
        time.sleep(5)
