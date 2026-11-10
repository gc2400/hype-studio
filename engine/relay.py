"""Backup relay to Streamerr (AzuraCast) – installed to /opt/hype by the updater, run by hype-relay.service.
Hype Studio's radio (the public MP3) goes live to the Streamerr station as a streamer/DJ, so every old Streamerr link
plays Hype Studio's sound; when this relay (or the whole server) stops, AzuraCast's AutoDJ takes over by itself.
Settings: Hype Studio → Settings → Backup (/etc/hype/relay.json, root only). Status: /run/hype/relay.json.
"""
import json, os, subprocess, time, urllib.parse

CFG = "/etc/hype/relay.json"; ST = "/run/hype/relay.json"
SRC = "http://127.0.0.1:8000/radio.mp3"

def cfg():
    try: return json.load(open(CFG))
    except Exception: return {}

def status(**k):
    os.makedirs("/run/hype", exist_ok=True)
    d = dict(k, t=time.time()); tmp = ST + ".tmp"; json.dump(d, open(tmp, "w")); os.replace(tmp, ST)

def url(c):
    u = urllib.parse.quote(c.get("user") or "source", safe=""); p = urllib.parse.quote(c.get("password") or "", safe="")
    mount = "/" + (c.get("mount") or "/").lstrip("/")
    return "icecast://%s:%s@%s:%s%s" % (u, p, c["host"], int(c.get("port") or 8000), mount)

def main():
    proc, started, mt, fails = None, 0, None, 0
    while True:
        try: m = os.path.getmtime(CFG)
        except OSError: m = None
        c = cfg(); on = bool(c.get("on") and c.get("host") and c.get("password"))
        if proc and (m != mt or not on):                     # settings changed / switched off: stop the running relay
            proc.terminate()
            try: proc.wait(10)
            except Exception: proc.kill()
            proc = None
        mt = m
        if not on:
            status(state="off"); time.sleep(5); continue
        if proc is None:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-reconnect", "1", "-reconnect_streamed", "1",
                   "-i", SRC, "-c", "copy", "-f", "mp3", "-content_type", "audio/mpeg",
                   "-ice_name", c.get("name") or "Hype Radio TV", "-ice_public", "0", url(c)]
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True); started = time.time()
            status(state="connecting", host=c["host"])
        time.sleep(3)
        if proc.poll() is not None:                           # ended: wrong password, Streamerr down, DJ slot taken …
            err = (proc.stderr.read() or "").strip().splitlines()[-1:] if proc.stderr else []
            fails = fails + 1 if time.time() - started < 60 else 1
            status(state="error", host=c["host"], error=(err[0] if err else "stopped")[:200], retry_in=min(300, 10 * fails))
            proc = None; time.sleep(min(300, 10 * fails))
        elif time.time() - started > 8:
            fails = 0; status(state="live", host=c["host"], since=started)

if __name__ == "__main__":
    main()
