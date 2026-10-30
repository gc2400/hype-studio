#!/usr/bin/env python3
"""Copy the live server files into the repo checkout (/srv/hype-repo) – used after a change made directly on the server.
   python3 deploy/export.py && git -C /srv/hype-repo add -A && git commit -m "sync from server" && git push"""
import os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import REPO, mapping, sha
n = 0
for f in mapping():
    src, dst = f["live"], os.path.join(REPO, f["repo"])
    if not os.path.exists(src): print("missing on server:", src); continue
    if sha(src) != sha(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copy2(src, dst); n += 1; print("updated:", f["repo"])
print(n, "file(s) copied from the server")
