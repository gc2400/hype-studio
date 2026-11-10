"""Backup relay to Streamerr (AzuraCast) – installed to /opt/hype by the updater, run by hype-relay.service.
Hype Studio's radio (the public MP3) goes live to the Streamerr station as a streamer/DJ, so every old Streamerr link
plays Hype Studio's sound; when this relay (or the whole server) stops, AzuraCast's AutoDJ takes over by itself.
Settings: Hype Studio → Settings → Backup (/etc/hype/relay.json, root only). Status: /run/hype/relay.json.
"""
import base64, json, os, socket, time, urllib.request

CFG = "/etc/hype/relay.json"; ST = "/run/hype/relay.json"
SRC = "http://127.0.0.1:8000/radio.mp3"

def cfg():
    try: return json.load(open(CFG))
    except Exception: return {}

def status(**k):
    os.makedirs("/run/hype", exist_ok=True)
    d = dict(k, t=time.time()); tmp = ST + ".tmp"; json.dump(d, open(tmp, "w")); os.replace(tmp, ST)

def creds(c):
    """AzuraCast accepts DJ user + password; its hint for clients without a user field is "user:password" as the password."""
    u, p = c.get("user") or "source", c.get("password") or ""
    if u and p.startswith(u + ":"): p = p[len(u) + 1:]
    return u, p

def run(c):
    """Icecast SOURCE client (like butt / Mixxx): one connection to the Streamerr DJ port, the radio MP3 copied into it.
    (ffmpeg's icecast output can't use mount "/", which AzuraCast's DJ port wants.) Returns an error text when it ends."""
    u, p = creds(c); mount = "/" + (c.get("mount") or "/").lstrip("/")
    s = socket.create_connection((c["host"], int(c.get("port") or 8000)), timeout=15)
    auth = base64.b64encode(("%s:%s" % (u, p)).encode()).decode()
    s.sendall(("SOURCE %s HTTP/1.0\r\nAuthorization: Basic %s\r\nUser-Agent: HypeStudio-relay/1.0\r\nContent-Type: audio/mpeg\r\n"
               "ice-name: %s\r\nice-public: 0\r\nice-audio-info: bitrate=128\r\n\r\n" % (mount, auth, c.get("name") or "Hype Radio TV")).encode())
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 4096:
        ch = s.recv(1024)
        if not ch: break
        head += ch
    line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200" not in line: s.close(); return "Streamerr answered: " + (line or "nothing") + " (check DJ login / Streamers enabled)"
    status(state="live", host=c["host"], since=time.time())
    src = urllib.request.urlopen(SRC, timeout=15); mt = os.path.getmtime(CFG)
    try:
        while True:
            b = src.read(8192)
            if not b: return "the local radio stream ended"
            s.sendall(b)
            if os.path.getmtime(CFG) != mt or not cfg().get("on"): return None       # settings changed / off
    except (OSError, socket.timeout) as e: return "connection lost: %s" % e
    finally:
        try: s.close(); src.close()
        except Exception: pass

def main():
    fails = 0
    while True:
        c = cfg()
        if not (c.get("on") and c.get("host") and c.get("password")): status(state="off"); time.sleep(5); continue
        status(state="connecting", host=c["host"]); t0 = time.time()
        try: err = run(c)
        except Exception as e: err = str(e)
        if err is None: fails = 0; continue
        fails = fails + 1 if time.time() - t0 < 60 else 1
        wait = min(300, 10 * fails)
        status(state="error", host=c["host"], error=err[:200], retry_in=wait); time.sleep(wait)

if __name__ == "__main__":
    main()
