#!/usr/bin/env python3
"""Hype Studio login: password + 2-factor code (TOTP, any authenticator app).
nginx asks /_auth on every request (auth_request). Runs on 127.0.0.1:8091.
CLI:  hype_auth.py setpass <user>      set/replace a password (asks twice)
      hype_auth.py totp <user> <png>   new 2-factor secret, QR written to <png>
      hype_auth.py logout-all          end every session
"""
import base64, hashlib, hmac, html, json, os, secrets, struct, sys, time, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF = "/etc/hype/auth.json"
SESS = "/var/lib/hype/sessions.json"
COOKIE = "hype_session"
SESSION_DAYS = 30
LOCK = threading.Lock()
FAILS = {}  # ip -> [timestamps]

def load(p, d):
    try: return json.load(open(p))
    except Exception: return d

def save(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    t = p + ".tmp"
    with open(os.open(t, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: json.dump(d, f)
    os.replace(t, p)

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 300000).hex()

def check_pw(pw, stored):
    salt = stored.split("$")[0]
    return hmac.compare_digest(hash_pw(pw, salt), stored)

def totp(secret, t=None, step=30):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    c = int((t or time.time()) // step)
    h = hmac.new(key, struct.pack(">Q", c), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)

def check_totp(secret, code, user, conf):
    code = "".join(ch for ch in code if ch.isdigit())
    now = time.time()
    for off in (-30, 0, 30):
        if hmac.compare_digest(totp(secret, now + off), code):
            step = int((now + off) // 30)
            last = conf["users"][user].get("last_step", 0)
            if step <= last: return False          # code already used
            conf["users"][user]["last_step"] = step
            save(CONF, conf)
            return True
    return False

def too_many(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 900]
    return len(FAILS[ip]) >= 5

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Hype Studio – login</title><style>
:root{--bg:#0b0b10;--card:#15151d;--line:#2a2a36;--txt:#f2f2f5;--mut:#9a9aab;--acc:#ff2d6f;--err:#ff6b6b}
@media (prefers-color-scheme: light){:root{--bg:#f3f3f6;--card:#fff;--line:#dcdce4;--txt:#15151d;--mut:#666678}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--txt);
font:16px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
form{width:100%;max-width:360px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
h1{margin:0 0 4px;font-size:22px;letter-spacing:.5px}h1 b{color:var(--acc)}p{margin:0 0 20px;color:var(--mut);font-size:14px}
label{display:block;font-size:13px;color:var(--mut);margin:14px 0 6px}
input{width:100%;padding:12px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--txt);font-size:16px}
input:focus{outline:2px solid var(--acc);border-color:transparent}
#code{letter-spacing:6px;font-size:22px;text-align:center}
button{margin-top:22px;width:100%;padding:13px;border:0;border-radius:9px;background:var(--acc);color:#fff;font-weight:700;font-size:16px;cursor:pointer}
.err{color:var(--err);font-size:14px;margin-top:14px}
.keep{display:flex;gap:8px;align-items:center;margin-top:14px;font-size:14px;color:var(--mut)}.keep input{width:auto}
</style></head><body><form method="post" action="/login">
<h1>HYPE <b>STUDIO</b></h1><p>Log in with your password and the 6-digit code from your authenticator app.</p>
<input type="hidden" name="next" value="%(next)s">
<label for="u">User</label><input id="u" name="user" autocomplete="username" required value="%(user)s">
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="code">Code</label><input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" required placeholder="000000">
<label class="keep"><input type="checkbox" name="keep" value="1" checked> Keep me logged in on this device (30 days)</label>
<button type="submit">Log in</button>%(err)s</form></body></html>"""

LIVE_CONF = "/etc/hype/live.json"
GUESTS = "/var/lib/hype/guests.json"
STUDIO_PATHS = {"studio", "cam1", "cam2", "cam3", "mixminus", "return"}
def mtx_ok(d):
    """Local programs (OBS, ffmpeg) may do anything. The studio page needs the studio key.
    A guest link (token) may publish its own guestN slot and listen to the mix-minus and the other guests."""
    ip = d.get("ip") or ""
    if ip in ("127.0.0.1", "::1"): return True
    path, action = (d.get("path") or ""), (d.get("action") or "")
    q = urllib.parse.parse_qs(d.get("query") or "")
    key = (q.get("key") or [""])[0] or d.get("password") or d.get("token") or ""
    if not key: return False
    live = load(LIVE_CONF, {})
    if live.get("studio_key") and hmac.compare_digest(key, live["studio_key"]):
        if action == "read" and path in ("live/program", "preview"): return True     # the panel live preview
        return action in ("publish", "read") and (path in STUDIO_PATHS or path.startswith("guest"))
    g = load(GUESTS, {}).get("guests", {})
    for slot, info in g.items():
        if info.get("token") and hmac.compare_digest(key, info["token"]) and info.get("exp", 0) > time.time():
            if action == "publish": return path == slot
            if action == "read": return path == "mixminus" or (path.startswith("guest") and path != slot)
    return False

class H(BaseHTTPRequestHandler):
    def ip(s): return s.headers.get("X-Real-IP", s.client_address[0])

    def sid(s):
        for part in s.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE: return v
        return None

    def valid(s):
        sid = s.sid()
        if not sid: return None
        with LOCK:
            ss = load(SESS, {})
            e = ss.get(hashlib.sha256(sid.encode()).hexdigest())
        return e["user"] if e and e["exp"] > time.time() else None

    def send(s, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        s.send_response(code)
        for k, v in headers: s.send_header(k, v)
        s.send_header("Content-Type", ctype); s.send_header("Cache-Control", "no-store")
        s.send_header("Content-Length", str(len(body))); s.end_headers(); s.wfile.write(body)

    def page(s, nxt="/", user="", err=""):
        e = '<div class="err">%s</div>' % html.escape(err) if err else ""
        return PAGE.replace("%(next)s", html.escape(nxt, True)).replace("%(user)s", html.escape(user, True)).replace("%(err)s", e).encode()

    def do_GET(s):
        path = urllib.parse.urlparse(s.path)
        if path.path == "/_auth":
            u = s.valid()
            return s.send(200 if u else 401, headers=[("X-Hype-User", u or "")])
        if path.path == "/login":
            nxt = urllib.parse.parse_qs(path.query).get("next", ["/"])[0]
            if s.valid(): return s.send(302, headers=[("Location", safe(nxt))])
            return s.send(200, s.page(safe(nxt)))
        if path.path == "/logout":
            sid = s.sid()
            if sid:
                with LOCK:
                    ss = load(SESS, {}); ss.pop(hashlib.sha256(sid.encode()).hexdigest(), None); save(SESS, ss)
            return s.send(302, headers=[("Location", "/login"),
                          ("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax")])
        s.send(404)

    def do_POST(s):
        if urllib.parse.urlparse(s.path).path == "/mtx":
            n = min(int(s.headers.get("Content-Length", 0)), 8192)
            try: d = json.loads(s.rfile.read(n) or b"{}")
            except Exception: d = {}
            ok = mtx_ok(d)
            return s.send(200 if ok else 401, b"", "text/plain")
        if urllib.parse.urlparse(s.path).path != "/login": return s.send(404)
        n = min(int(s.headers.get("Content-Length", 0)), 4096)
        f = {k: v[0] for k, v in urllib.parse.parse_qs(s.rfile.read(n).decode(errors="replace")).items()}
        user, nxt, ip = f.get("user", "").strip().lower(), safe(f.get("next", "/")), s.ip()
        with LOCK:
            if too_many(ip):
                return s.send(429, s.page(nxt, user, "Too many wrong tries. Wait 15 minutes."))
            conf = load(CONF, {"users": {}})
            u = conf["users"].get(user)
            ok = bool(u) and check_pw(f.get("password", ""), u["pw"]) and \
                 bool(u.get("totp")) and check_totp(u["totp"], f.get("code", ""), user, conf)
            if not ok:
                FAILS.setdefault(ip, []).append(time.time())
                time.sleep(1)
                return s.send(401, s.page(nxt, user, "Wrong user, password or code."))
            FAILS.pop(ip, None)
            sid = secrets.token_urlsafe(32)
            days = SESSION_DAYS if f.get("keep") else 1
            ss = {k: v for k, v in load(SESS, {}).items() if v["exp"] > time.time()}
            ss[hashlib.sha256(sid.encode()).hexdigest()] = {"user": user, "exp": time.time() + days * 86400, "ip": ip, "at": int(time.time())}
            save(SESS, ss)
        age = f"; Max-Age={days * 86400}" if f.get("keep") else ""
        s.send(302, headers=[("Location", nxt),
               ("Set-Cookie", f"{COOKIE}={sid}; Path=/; Secure; HttpOnly; SameSite=Lax{age}")])

    def log_message(s, *a): pass

def safe(nxt):
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/login") else "/"

def cli():
    import getpass
    conf = load(CONF, {"users": {}})
    cmd = sys.argv[1]
    if cmd == "setpass":
        user = sys.argv[2].lower()
        pw = os.environ.get("HYPE_PW") or getpass.getpass("New password: ")
        if not os.environ.get("HYPE_PW") and getpass.getpass("Again: ") != pw: sys.exit("Not the same.")
        conf["users"].setdefault(user, {})["pw"] = hash_pw(pw); save(CONF, conf); print("Password set for", user)
    elif cmd == "totp":
        user, png = sys.argv[2].lower(), sys.argv[3]
        sec = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        conf["users"].setdefault(user, {})["totp"] = sec; conf["users"][user]["last_step"] = 0; save(CONF, conf)
        uri = f"otpauth://totp/Hype%20Studio:{user}?secret={sec}&issuer=Hype%20Studio&digits=6&period=30"
        import qrcode; qrcode.make(uri, box_size=10, border=3).save(png); os.chmod(png, 0o600)
        print("2-factor secret set for", user, "- QR written to", png)
    elif cmd == "logout-all":
        save(SESS, {}); print("All sessions ended.")

if __name__ == "__main__":
    if len(sys.argv) > 1: cli()
    else: ThreadingHTTPServer(("127.0.0.1", 8091), H).serve_forever()
