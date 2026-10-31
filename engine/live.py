"""Hype Studio – phase 4: live from the studio, cameras, guests, recording and restream.
Used by the engine (engine.py). Media goes through MediaMTX (WebRTC in, RTSP/RTMP inside the server)."""
import os, re, json, time, secrets, threading, subprocess, logging, urllib.request

log = logging.getLogger("live")
LIVE_CONF = "/etc/hype/live.json"            # studio key + harbor password (600)
GUESTS = "/var/lib/hype/guests.json"          # guest links (read by the login service for MediaMTX)
STATE = "/var/lib/hype/live_state.json"       # camera slots, recording/restream choices
RESTREAM = "/etc/hype/restream.json"          # destinations with stream keys (600)
REC_DIR = "/srv/hype/music/MIXES/Recordings"   # in the library (Mixes → Recordings): watch again, schedule like a mix
REC_STATE = "/var/lib/hype/rec_state.json"     # the recording that is running (survives an engine restart)
PROGRAM = "rtmp://127.0.0.1:1935/live/program"
RADIO = "http://127.0.0.1:8000/rec.mp3"     # the on-air sound at 256k, for the recordings (internal)
SOURCES = ["cam1", "cam2", "cam3"] + [f"guest{i}" for i in range(1, 7)]
CAMS = ("CAM 1", "CAM 2", "CAM 3", "CAM 4")

def load(p, d):
    try: return json.load(open(p))
    except Exception: return d

def save(p, d, mode=0o644):
    os.makedirs(os.path.dirname(p), exist_ok=True); t = p + ".tmp"
    with open(os.open(t, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f: json.dump(d, f, indent=1)
    os.replace(t, p)

def conf():
    c = load(LIVE_CONF, {})
    if not c.get("studio_key"):
        c = {"studio_key": secrets.token_urlsafe(18), "harbor_pw": secrets.token_urlsafe(18)}; save(LIVE_CONF, c, 0o600)
    return c

def mtx_paths():
    """Which streams are on MediaMTX right now (ready = someone is sending)."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:9997/v3/paths/list", timeout=2) as r:
            return {p["name"]: {"ready": p.get("ready", False), "tracks": p.get("tracks", []), "readers": len(p.get("readers", []))}
                    for p in json.loads(r.read()).get("items", [])}
    except Exception: return {}

class Proc:
    """An ffmpeg that keeps running (restarts after a drop) until stopped."""
    def __init__(self, name, args, restart=True):
        self.name, self.args, self.restart, self.p, self.stop_ = name, args, restart, None, False
        self.started = time.time(); self.err = ""
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        while not self.stop_:
            try:
                self.p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + self.args, stderr=subprocess.PIPE, text=True)
                _, err = self.p.communicate(); self.err = (err or "").strip()[-300:]
            except Exception as e: self.err = str(e)
            if self.stop_ or not self.restart: break
            log.warning("%s stopped (%s) – restarting in 3 s", self.name, self.err[-120:]); time.sleep(3)
    def stop(self):
        self.stop_ = True
        try:
            if self.p and self.p.poll() is None:
                self.p.stdin and self.p.stdin.close(); self.p.terminate(); self.p.wait(8)
        except Exception:
            try: self.p.kill()
            except Exception: pass
    def alive(self): return bool(self.p and self.p.poll() is None)

def feed(cam):
    """OBS input of a camera slot ("CAM 1" is the scene, "Cam 1 feed" the stream)."""
    return cam.replace("CAM", "Cam") + " feed"

def safe_name(t):
    return re.sub(r"[^A-Za-z0-9 _-]+", "", t or "").strip().replace(" ", "_")[:60] or "show"

class Live:
    def __init__(self, liq, tv, show_title):
        self.liq, self.tv, self.show_title = liq, tv, show_title
        conf(); self.st = load(STATE, {"cams": {"CAM 1": "cam1", "CAM 2": "cam2", "CAM 3": "guest1", "CAM 4": "guest2"}, "auto_record": True, "lip_sync": 0})
        self.st["cams"].setdefault("CAM 4", "guest2"); self.st.setdefault("lip_sync", 0)
        self.drops = 0; self.air = "automation"
        threading.Thread(target=self.watch_air, daemon=True).start()
        self.rec = None; self.rest = {}; self.live_since = None
        self.rec_restore()
        self.live = self.liq_live()
    # ---------- LIVE on/off ----------
    def liq_live(self):
        try: return self.liq.cmd("var.get live_on").split()[0] == "true"
        except Exception: return False
    def watch_air(self):
        """What is really on air (Liquidsoap): your studio mix, or the automation. Counts drop-outs while LIVE."""
        while True:
            try:
                a = (self.liq.cmd("live.source") or "").split()[0]
                if self.live and self.air == "studio" and a != "studio": self.drops += 1; log.warning("LIVE: studio signal dropped – automation on air")
                self.air = a
            except Exception: pass
            time.sleep(0.5)
            try: self.black_tick()
            except Exception as e: log.warning("cam black: %s", e)
    def black_tick(self):
        """Behind every camera there is a black box: a portrait phone camera shows upright on black (never stretched).
        It is only shown while that camera is sending - otherwise the CAM slate behind it shows, as before."""
        now = time.time()
        if now - getattr(self, "_bk_t", 0) < 3: return
        self._bk_t = now
        paths = mtx_paths()
        with self.tv.olock:
            cl = self.tv.client()
            if now - getattr(self, "_bk_map_t", 0) > 120 or not getattr(self, "_bk_map", None):
                m = []
                for sc in cl.get_scene_list().scenes:
                    items = sorted(cl.get_scene_item_list(sc["sceneName"]).scene_items, key=lambda i: i["sceneItemIndex"])
                    for k, it in enumerate(items):
                        if it["sourceName"] == "Cam black" and k + 1 < len(items) and items[k + 1]["sourceName"].endswith(" feed"):
                            m.append((sc["sceneName"], it["sceneItemId"], items[k + 1]["sourceName"].replace("Cam", "CAM").replace(" feed", "")))
                self._bk_map, self._bk_map_t, self._bk_on = m, now, {}
            for scene, iid, cam in self._bk_map:
                src = self.st["cams"].get(cam); on = bool(src and paths.get(src, {}).get("ready"))
                if self._bk_on.get((scene, iid)) != on:
                    cl.set_scene_item_enabled(scene, iid, on); self._bk_on[(scene, iid)] = on
    def set_live(self, on):
        self.liq.cmd(f"var.set live_on = {'true' if on else 'false'}"); self.live = on
        self.live_since = time.time() if on else None; self.drops = 0
        self.tv.live = on; self.tv.route_audio(force=True)      # TV sound while LIVE: your studio mix only (in sync with the cameras)
        if on and self.st.get("auto_record", True) and not self.rec: self.start_rec("live")
        if not on and self.rec and self.rec.get("why") == "live": self.stop_rec()
        for k, d in self.dests().items():
            if d.get("with_live"): self.restream(k, on)
        log.info("LIVE %s", "ON" if on else "OFF")
    # ---------- guests ----------
    def guests(self):
        return load(GUESTS, {"guests": {}}).get("guests", {})
    def guest_add(self, name, slot=None, hours=12, mode="video"):
        g = self.guests()
        slot = slot or next((s for s in SOURCES if s.startswith("guest") and s not in g), None)
        if not slot: raise ValueError("all 6 guest places are taken – remove one first")
        g[slot] = {"name": (name or "Guest").strip()[:60], "token": secrets.token_urlsafe(16), "exp": time.time() + hours * 3600, "created": time.time(),
                   "mode": "audio" if mode == "audio" else "video"}
        save(GUESTS, {"guests": g}, 0o600); return slot, g[slot]
    def guest_remove(self, slot):
        g = self.guests(); g.pop(slot, None); save(GUESTS, {"guests": g}, 0o600)
    def guest_vol(self, slot, vol):
        """Per-guest level 0–2 (200 % = +6 dB boost) – the Broadcast page plays each guest at this level to the Mixcast."""
        g = self.guests()
        if slot not in g: raise ValueError("no such guest")
        g[slot]["vol"] = max(0.0, min(2.0, vol)); save(GUESTS, {"guests": g}, 0o600)
    def guest_by_token(self, token):
        for slot, info in self.guests().items():
            if token and info.get("token") == token and info.get("exp", 0) > time.time(): return slot, info
        return None, None
    # ---------- camera slots (OBS CAM 1/2/3 read a stream from MediaMTX) ----------
    def set_cam(self, cam, src):
        if cam not in CAMS or src not in SOURCES: raise ValueError("unknown camera or source")
        self.st["cams"][cam] = src; save(STATE, self.st); self.apply_cams()
    def apply_cams(self):
        with self.tv.olock:
            try:
                cl = self.tv.client()
                for cam, src in self.st["cams"].items():
                    cl.set_input_settings(feed(cam), {"is_local_file": False, "input": f"rtsp://127.0.0.1:8554/{src}", "input_format": "rtsp",
                                                "ffmpeg_options": "rtsp_transport=tcp", "reconnect_delay_sec": 2, "buffering_mb": 0,
                                                "restart_on_activate": False, "close_when_inactive": False, "hw_decode": True, "clear_on_media_end": True}, True)
                    self.cam_delay(cl, feed(cam))
                self.sync_audio(cl)
            except Exception as e: log.warning("cams: %s", e); self.tv.cl = None
    def cam_delay(self, cl, cam):
        """Lip sync: < 0 = the picture waits (Render Delay, max 500 ms), > 0 = the sound waits (audio sync offset)."""
        ms = max(0, min(500, -int(self.st.get("lip_sync", 0))))
        try: cl.set_source_filter_settings(cam, "Delay", {"delay_ms": ms})
        except Exception:
            try: cl.create_source_filter(cam, "Delay", "gpu_delay", {"delay_ms": ms})
            except Exception as e: log.warning("cam delay %s: %s", cam, e)
    def sync_audio(self, cl):
        try: cl.set_input_audio_sync_offset("Studio audio", max(0, min(1500, int(self.st.get("lip_sync", 0)))))
        except Exception as e: log.warning("studio audio sync: %s", e)
    # ---------- recording ----------
    def start_rec(self, why="manual", name=None, what="both"):
        """what: both (video .mp4 + radio .mp3) | audio | video. Each file is written by its own systemd unit (hype-rec-*),
        outside the engine: an engine restart (update) never stops a recording."""
        if self.rec: return
        os.makedirs(REC_DIR, exist_ok=True)
        try:
            import pwd, grp; os.chown(REC_DIR, pwd.getpwnam("upload").pw_uid, grp.getgrnam("hype").gr_gid); os.chmod(REC_DIR, 0o2775)
        except Exception: pass
        base = os.path.join(REC_DIR, time.strftime("%Y-%m-%d %H%M ") + safe_name(name or self.show_title()))
        jobs = []
        if what in ("both", "video"):
            jobs.append(("hype-rec-video", ["-i", PROGRAM, "-c", "copy", "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", base + ".mp4"]))
        if what in ("both", "audio"):
            jobs.append(("hype-rec-audio", ["-i", RADIO, "-c", "copy", "-f", "mp3", base + ".mp3"]))
        units = []
        for unit, args in jobs:
            subprocess.run(["systemctl", "stop", unit], capture_output=True); subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
            r = subprocess.run(["systemd-run", "--unit=" + unit, "--collect", "--quiet", "-p", "Nice=5", "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
                                "-nostdin", "-y"] + args, capture_output=True, text=True)
            if r.returncode == 0: units.append(unit)
            else: log.warning("recording %s: %s", unit, r.stderr.strip()[-200:])
        self.rec = {"why": why, "start": time.time(), "file": os.path.basename(base), "units": units}
        save(REC_STATE, self.rec); log.info("recording %s", base)
    def rec_alive(self):
        if not self.rec: return False
        try: return all(subprocess.run(["systemctl", "is-active", "--quiet", u]).returncode == 0 for u in self.rec.get("units", [])) and bool(self.rec.get("units"))
        except Exception: return False
    def rec_restore(self):
        """After an engine restart: a recording that is still running is picked up again."""
        r = load(REC_STATE, None)
        if r and r.get("units"):
            self.rec = r
            if not self.rec_alive(): self.rec = None; save(REC_STATE, {})
            else: log.info("recording still running: %s", r.get("file"))
    def stop_rec(self):
        if not self.rec: return
        for u in self.rec.get("units", []): subprocess.run(["systemctl", "stop", u], capture_output=True)
        log.info("recording stopped: %s", self.rec["file"]); self.rec = None; save(REC_STATE, {})
    # ---------- restream ----------
    def dests(self):
        return load(RESTREAM, {"dests": {}}).get("dests", {})
    def save_dests(self, dests):
        clean = {}
        for k, d in (dests or {}).items():
            k = re.sub(r"[^a-z0-9_-]", "", k.lower())[:20] or secrets.token_hex(3)
            old = self.dests().get(k, {})
            key = d.get("key") if d.get("key") and "•" not in d.get("key") else old.get("key", "")
            clean[k] = {"name": (d.get("name") or k)[:40], "url": (d.get("url") or "").strip(), "key": key, "with_live": bool(d.get("with_live"))}
        save(RESTREAM, {"dests": clean}, 0o600)
    def restream(self, k, on):
        d = self.dests().get(k)
        if on and d and d.get("url"):
            if k in self.rest and self.rest[k].alive(): return
            target = d["url"].rstrip("/") + ("/" + d["key"] if d.get("key") else "")
            self.rest[k] = Proc("restream " + d["name"], ["-i", PROGRAM, "-c", "copy", "-f", "flv", target]); log.info("restream ON: %s", d["name"])
        elif not on and k in self.rest:
            self.rest.pop(k).stop(); log.info("restream OFF: %s", d["name"] if d else k)
    # ---------- status for the panel ----------
    def status(self):
        paths = mtx_paths(); g = self.guests(); c = conf()
        def on(p): return bool(paths.get(p, {}).get("ready"))
        return {
            "live": self.live, "live_since": self.live_since, "studio_online": on("studio"), "return_online": on("return"),
            "sources": {s: {"online": on(s), "name": (g.get(s) or {}).get("name") if s.startswith("guest") else {"cam1": "Studio cam 1", "cam2": "Studio cam 2", "cam3": "Studio cam 3"}[s]} for s in SOURCES},
            "cams": self.st["cams"], "lip_sync": self.st.get("lip_sync", 0), "air": self.air, "drops": self.drops, "auto_record": self.st.get("auto_record", True),
            "guests": {s: {"name": i["name"], "token": i["token"], "exp": i["exp"], "online": on(s), "vol": i.get("vol", 1.0), "mode": i.get("mode", "video")} for s, i in g.items()},
            "rec": {"since": self.rec["start"], "file": self.rec["file"], "ok": self.rec_alive()} if self.rec else None,
            "restream": {k: {"name": d["name"], "on": k in self.rest and not self.rest[k].stop_, "ok": k in self.rest and self.rest[k].alive(),
                             "with_live": d.get("with_live"), "url": d["url"], "key": ("•" * 8) if d.get("key") else ""} for k, d in self.dests().items()},
            "studio_key": c["studio_key"]}
    def settings(self, d):
        if "auto_record" in d: self.st["auto_record"] = bool(d["auto_record"])
        if "lip_sync" in d:
            self.st["lip_sync"] = max(-500, min(1500, int(d["lip_sync"])))
            with self.tv.olock:
                try:
                    cl = self.tv.client()
                    for cam in CAMS: self.cam_delay(cl, feed(cam))
                    self.sync_audio(cl)
                except Exception: self.tv.cl = None
        save(STATE, self.st)
        if "dests" in d: self.save_dests(d["dests"])
