#!/usr/bin/env python3
"""Hype Studio – keep the radio engines' sound settings files healthy (22 Nov: a hard power-off left sound.json as zeros,
and Liquidsoap refuses to start with a broken file – Hype Radio stayed silent after the restart).
For each file given: valid JSON -> keep a copy as <file>.good; broken (or empty) -> put the last good copy back
(the broken one is kept as <file>.broken-<time>); no good copy -> move it aside, the radio engine starts with its defaults.
Runs before every start of a radio engine (Hype Radio: ExecStartPre; the theme stations: hype-channels) and every 10 min (guard)."""
import json, os, shutil, sys, time
def ok(p):
    try:
        with open(p) as f: return isinstance(json.load(f), (dict, list))
    except Exception: return False
def check(p):
    good = p + ".good"
    if not os.path.exists(p): return "missing (defaults)"
    if ok(p):
        try:
            if not os.path.exists(good) or open(good, "rb").read() != open(p, "rb").read():
                shutil.copy2(p, good + ".tmp"); os.replace(good + ".tmp", good)
        except OSError: pass
        return "ok"
    bad = "%s.broken-%s" % (p, time.strftime("%Y%m%d-%H%M%S")); os.replace(p, bad)
    if ok(good): shutil.copy2(good, p); return "BROKEN -> restored the last good copy (%s kept)" % os.path.basename(bad)
    return "BROKEN -> moved aside, no good copy: starts with defaults (%s kept)" % os.path.basename(bad)
if __name__ == "__main__":
    for p in sys.argv[1:]: print("soundcheck %s: %s" % (p, check(p)), flush=True)
