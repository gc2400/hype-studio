"""Shared bits for the Hype Studio updater / export (runs on the server as root)."""
import os, hashlib
REPO = "/srv/hype-repo"
def mapping():
    out = []
    for ln in open(os.path.join(REPO, "deploy/files.txt")):
        ln = ln.strip()
        if not ln or ln.startswith("#"): continue
        r, l, m, u = [x.strip() for x in ln.split("|")]
        out.append({"repo": r, "live": l, "mode": int(m, 8), "units": [] if u == "-" else u.split(",")})
    return out
def sha(path):
    try: return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError: return None
