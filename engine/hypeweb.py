"""🌐 The web browser for the media scene (19 Nov 2026).

A real Chromium (snap) on its own virtual screen (Xvfb :99), running as the separate user `hypeweb` – never root, no access
to the Hype files. It has its OWN sound server (hypeweb's user PulseAudio, sink "web"): it can never reach the on-air sinks.
Studio controls it through Chrome's DevTools protocol (127.0.0.1:9223) and captures screen + sound into MediaMTX
(rtsp://127.0.0.1:8554/web, H.264 on the Intel GPU + Opus). The panel watches that path (WebRTC) and puts it on air through
the LIVE-pad path (Media window, Mute / Music off).

Runs only on demand: started from the panel, stopped from the panel, or by itself after IDLE_STOP s without use while
it is not on air. See claude/Hype_Studio_Web_Browser_Plan.md."""
import json, os, re, subprocess, threading, time, urllib.request, urllib.parse, logging

log = logging.getLogger("engine")
USER = "hypeweb"; DISPLAY = ":99"; CDP = "http://127.0.0.1:9223"; W, H = 1920, 1080
URL_RTSP = "rtsp://127.0.0.1:8554/web"
IDLE_STOP = 1800
ST = {"on": False, "t_use": 0, "starting": False, "air": False}
LOCK = threading.RLock()

def uid():
    try: return int(subprocess.run(["id", "-u", USER], capture_output=True, text=True, timeout=5).stdout.strip())
    except Exception: return None

def _user(*a):
    return subprocess.run(["systemctl", "--user", "-M", USER + "@", *a], capture_output=True, text=True, timeout=20)

def _run_user(unit, *cmd, env=()):
    return subprocess.run(["systemd-run", "--user", "-M", USER + "@", "--unit=" + unit, "--collect",
                           *["--setenv=" + e for e in env], *cmd], capture_output=True, text=True, timeout=20)

def cdp_ok():
    try: urllib.request.urlopen(CDP + "/json/version", timeout=2).read(); return True
    except Exception: return False

def running(): return cdp_ok()

def start():
    """Virtual screen + browser + capture. Returns quickly once the browser answers (or raises)."""
    with LOCK:
        ST["t_use"] = time.time()
        if cdp_ok() and _cap_active(): ST["on"] = True; return True
        ST["starting"] = True
        try:
            u = uid()
            if u is None: raise RuntimeError("the browser user is missing on the server")
            _user("start", "pulseaudio.socket", "pulseaudio.service")       # its own sound server (sink "web")
            if _user("is-active", "hw-xvfb").stdout.strip() != "active":
                _user("reset-failed", "hw-xvfb")
                _run_user("hw-xvfb", "/usr/bin/Xvfb", DISPLAY, "-screen", "0", "%dx%dx24" % (W, H), "-nolisten", "tcp", "-ac")
                time.sleep(1.5)
            if not cdp_ok():
                _user("reset-failed", "hw-chrome")
                _run_user("hw-chrome", "/snap/bin/chromium", "--no-first-run", "--no-default-browser-check", "--kiosk",
                          "--window-position=0,0", "--window-size=%d,%d" % (W, H), "--autoplay-policy=no-user-gesture-required",
                          "--remote-debugging-port=9223", "--remote-allow-origins=http://127.0.0.1",
                          "--user-data-dir=/home/%s/profile" % USER, "--disable-features=Translate,MediaRouter",
                          "--password-store=basic", "https://www.google.com/", env=("DISPLAY=" + DISPLAY,))
                for _ in range(40):
                    if cdp_ok(): break
                    time.sleep(0.5)
                if not cdp_ok(): raise RuntimeError("the browser did not start")
            if not _cap_active():
                subprocess.run(["systemctl", "reset-failed", "hype-webcap"], capture_output=True, timeout=10)
                subprocess.run(["systemd-run", "--unit=hype-webcap", "--collect", "-p", "Restart=always", "-p", "RestartSec=2", "-p", "Nice=5",
                                "--setenv=DISPLAY=" + DISPLAY, "--setenv=PULSE_SERVER=unix:/run/user/%d/pulse/native" % u,
                                "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-vaapi_device", "/dev/dri/renderD128",
                                "-thread_queue_size", "512", "-f", "x11grab", "-draw_mouse", "0", "-framerate", "30", "-video_size", "%dx%d" % (W, H), "-i", DISPLAY,
                                "-thread_queue_size", "512", "-f", "pulse", "-i", "web.monitor",
                                "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-b:v", "5M", "-maxrate", "5M", "-g", "30", "-bf", "0",
                                "-c:a", "libopus", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-f", "rtsp", "-rtsp_transport", "tcp", URL_RTSP],
                               capture_output=True, text=True, timeout=20)
            ST["on"] = True; log.info("web browser started")
            return True
        finally: ST["starting"] = False

def _cap_active():
    return subprocess.run(["systemctl", "is-active", "hype-webcap"], capture_output=True, text=True, timeout=5).stdout.strip() == "active"

def stop(why="stopped"):
    with LOCK:
        subprocess.run(["systemctl", "stop", "hype-webcap"], capture_output=True, timeout=20)
        _user("stop", "hw-chrome", "hw-xvfb")
        ST["on"] = False; ST["air"] = False; log.info("web browser %s", why)

def watch(is_on_air):
    """Stops the browser after IDLE_STOP s without use, unless it is on air (is_on_air())."""
    while True:
        time.sleep(30)
        try:
            ST["air"] = bool(is_on_air())
            if ST["on"] and not ST["air"] and time.time() - ST["t_use"] > IDLE_STOP: stop("stopped (30 min not used)")
            elif ST["on"] and not running() and not ST["starting"]: ST["on"] = False
        except Exception as e: log.warning("web browser watch: %s", e)

# ---------------- control (Chrome DevTools protocol) ----------------
def _page():
    tabs = json.loads(urllib.request.urlopen(CDP + "/json", timeout=3).read())
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages: raise RuntimeError("no page open")
    return pages[0]

def cdp(method, params=None, page=None):
    import websocket
    p = page or _page()
    ws = websocket.create_connection(p["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == 1: return r.get("result") or {}
    finally: ws.close()

def info():
    try: p = _page(); return {"on": True, "url": p.get("url", ""), "title": p.get("title", "")}
    except Exception: return {"on": False, "url": "", "title": ""}

def nav(text):
    ST["t_use"] = time.time()
    t = (text or "").strip()
    if not t: return
    if re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I): url = t
    elif re.match(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/.*)?$", t): url = "https://" + t
    else: url = "https://www.google.com/search?q=" + urllib.parse.quote(t)
    if not re.match(r"^https?://", url, re.I): raise ValueError("only http(s) pages")
    cdp("Page.navigate", {"url": url})

def back(): ST["t_use"] = time.time(); cdp("Runtime.evaluate", {"expression": "history.back()"})
def forward(): ST["t_use"] = time.time(); cdp("Runtime.evaluate", {"expression": "history.forward()"})
def reload(): ST["t_use"] = time.time(); cdp("Page.reload", {})

def click(fx, fy, n=1):
    """fx, fy: 0..1 of the picture (the panel sends where you clicked in its view)."""
    ST["t_use"] = time.time(); x, y = max(0, min(W - 1, fx * W)), max(0, min(H - 1, fy * H)); p = _page()
    cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, p)
    for t in ("mousePressed", "mouseReleased"):
        cdp("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": n}, p)

def wheel(fx, fy, dy):
    ST["t_use"] = time.time()
    cdp("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": fx * W, "y": fy * H, "deltaX": 0, "deltaY": max(-3000, min(3000, dy))})

def type_text(s):
    ST["t_use"] = time.time()
    if s: cdp("Input.insertText", {"text": s[:500]})

KEYS = {"Enter": (13, "\r"), "Backspace": (8, ""), "Tab": (9, ""), "Escape": (27, ""), "Delete": (46, ""), "ArrowLeft": (37, ""),
        "ArrowRight": (39, ""), "ArrowUp": (38, ""), "ArrowDown": (40, ""), "PageUp": (33, ""), "PageDown": (34, ""), "Home": (36, ""),
        "End": (35, ""),}
def key(name):
    ST["t_use"] = time.time()
    if len(name) == 1:                      # a letter / digit / sign: a real key press (typing AND page shortcuts like YouTube's k / f / m)
        code, txt = (ord(name.upper()) if name.isalnum() and name.isascii() else 0), name
    elif name in KEYS: code, txt = KEYS[name]
    else: return
    p = _page()
    d = {"key": name, "code": name if len(name) > 1 else ("Space" if name == " " else "Key" + name.upper() if name.isalpha() else ""), "windowsVirtualKeyCode": code or (32 if name == " " else 0), "nativeVirtualKeyCode": code or (32 if name == " " else 0)}
    cdp("Input.dispatchKeyEvent", dict(d, type="keyDown", **({"text": txt} if txt else {})), p)
    cdp("Input.dispatchKeyEvent", dict(d, type="keyUp"), p)
