#!/usr/bin/env python3
"""Hype Studio updater – installs changes from GitHub (gc2400/hype-studio, branch main) by itself, also when nobody's laptop is on.

Every 60 s: fetch main. A new commit →
  1. safety: every file it changes must still be the same on the server as in the last installed commit
     (else "conflict": someone changed the server directly – run deploy/export.py and push first)
  2. a radio.liq change is tested in the lab first (deploy/lab_radio.sh) – no pass, no install
  3. backup → install → restart only what changed (radio last; the guard is paused meanwhile)
  4. check after 45 s (services active + guard OK); a problem → everything goes back (rollback)
  5. the result is written to the branch "status" (status.json + log), so it can be read from anywhere.
Commit message "[night]" = wait until 04:00–05:00 before installing (a radio restart = ~10 s silence).
"""
import os, sys, json, time, shutil, subprocess, datetime, traceback
sys.path.insert(0, "/srv/hype-repo/deploy")
REPO = "/srv/hype-repo"; STATUS_WT = "/srv/hype-status"
DEPLOYED = "/var/lib/hype/deployed_commit"; PAUSE = "/run/hype/guard_pause"
ENV = dict(os.environ, GIT_SSH_COMMAND="ssh -i /root/.ssh/hype_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes")
LOG = []
def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a); LOG.append(line); print(line, flush=True)
def git(*a, cwd=REPO, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, env=ENV, capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0: raise RuntimeError("git " + " ".join(a) + ": " + r.stderr.strip()[:300])
    return r.stdout.strip()
def sh(cmd, timeout=180):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout); return r.returncode, (r.stdout + r.stderr).strip()

def write_status(result, commit, extra=None):
    """status.json on the orphan branch 'status' (one small commit per update)."""
    try:
        if not os.path.isdir(STATUS_WT):
            git("fetch", "origin", "status", check=False)
            if git("rev-parse", "--verify", "-q", "origin/status", check=False):
                git("worktree", "add", "-f", STATUS_WT, "origin/status", check=False); git("checkout", "-B", "status", cwd=STATUS_WT)
            else:
                git("worktree", "add", "--detach", STATUS_WT); git("checkout", "--orphan", "status", cwd=STATUS_WT)
                git("rm", "-rf", "--quiet", ".", cwd=STATUS_WT, check=False)
        st = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "result": result, "commit": commit,
              "subject": git("log", "-1", "--format=%s", commit, check=False) if commit else "", "log": LOG[-60:]}
        if extra: st.update(extra)
        try: st["guard"] = {k: v for k, v in json.load(open("/var/lib/hype/guard.json")).items() if k in ("ok", "problems", "song", "obs", "seg_age", "listeners")}
        except Exception: pass
        st["services"] = {u: sh(f"systemctl is-active {u}")[1] for u in ("hype-radio", "hype-engine", "hype-obs", "hype-ladder", "hype-guard", "hype-indexer", "nginx")}
        json.dump(st, open(os.path.join(STATUS_WT, "status.json"), "w"), indent=1)
        with open(os.path.join(STATUS_WT, "history.log"), "a") as f: f.write(f'{st["time"]} {result} {commit[:8] if commit else ""} {st["subject"]}\n')
        git("add", "-A", cwd=STATUS_WT); git("-c", "user.name=Hype Studio server", "-c", "user.email=server@hype-studio", "commit", "-q", "-m", f"{result} {commit[:8] if commit else ''}", cwd=STATUS_WT, check=False)
        git("push", "-q", "origin", "HEAD:status", cwd=STATUS_WT, check=False)
    except Exception as e: print("status:", e, flush=True)

def deploy(cur, new):
    from common import mapping, sha
    m = {f["repo"]: f for f in mapping_at(new)}
    changed = [p for p in git("diff", "--name-only", cur, new).splitlines()] if cur else list(m)
    files = [m[p] for p in changed if p in m]
    msg = git("log", "-1", "--format=%B", new)
    log("update", cur[:8] if cur else "-", "→", new[:8], "·", len(files), "file(s):", ", ".join(f["repo"] for f in files) or "none (not server files)")
    if "[night]" in msg and not (4 <= datetime.datetime.now().hour < 5):
        log("waits for the night (04:00–05:00) – [night] in the commit message"); return "waiting"
    # 1. the server must not have been changed by hand since the last install
    for f in files:
        if cur and os.path.exists(f["live"]):
            r = subprocess.run(["git", "show", f"{cur}:{f['repo']}"], cwd=REPO, capture_output=True)
            if r.returncode == 0 and __import__("hashlib").sha256(r.stdout).hexdigest() != sha(f["live"]):
                log("CONFLICT:", f["live"], "was changed on the server – run deploy/export.py + push first"); return "conflict"
    git("checkout", "-q", "--detach", new)
    # 2. the radio in the lab first
    if any(f["repo"] == "radio/radio.liq" for f in files):
        code, out = sh(f"bash {REPO}/deploy/lab_radio.sh {REPO}/radio/radio.liq", 240); log("lab:", out.splitlines()[-1] if out else "")
        if code != 0: log(out[-600:]); git("checkout", "-q", "--detach", cur); return "lab-failed"
    # 3. backup + install
    bk = "/var/backups/hype/" + time.strftime("%Y%m%d-%H%M%S"); os.makedirs(bk, exist_ok=True)
    for f in files:
        if os.path.exists(f["live"]): os.makedirs(os.path.dirname(bk + f["live"]), exist_ok=True); shutil.copy2(f["live"], bk + f["live"])
    units = []
    def install(fs, src_root):
        for f in fs:
            src = os.path.join(src_root, f["repo"]) if src_root else bk + f["live"]
            if not os.path.exists(src): continue
            os.makedirs(os.path.dirname(f["live"]), exist_ok=True); shutil.copyfile(src, f["live"]); os.chmod(f["live"], f["mode"])
    def restart(fs):
        us = []
        for f in fs:
            for u in f["units"]:
                if u not in us: us.append(u)
        if "daemon-reload" in us: sh("systemctl daemon-reload")
        if "nginx-reload" in us:
            c, o = sh("nginx -t")
            if c != 0: raise RuntimeError("nginx -t failed: " + o[-200:])
            sh("systemctl reload nginx")
        late = [u for u in ("hype-engine", "hype-radio", "hype-updater") if u in us]
        for u in [u for u in us if u not in ("daemon-reload", "nginx-reload") and u not in late] + [u for u in late if u != "hype-updater"]:
            log("restart", u); sh(f"systemctl restart {u}", 90)
        return us
    open(PAUSE, "w").close()
    try:
        install(files, REPO); units = restart(files)
        # 4. check
        if [u for u in units if u not in ("daemon-reload", "nginx-reload", "hype-updater")]:
            time.sleep(45)
        bad = [u for u in units if u.startswith("hype-") and u != "hype-updater" and sh(f"systemctl is-active {u}")[1] != "active"]
        try: g = json.load(open("/var/lib/hype/guard.json")); gok = time.time() - g.get("t", 0) < 30
        except Exception: g, gok = {}, False
        if "hype-radio" in units and gok and (g.get("level") or 0) < 0.01:
            time.sleep(15)
            try: g = json.load(open("/var/lib/hype/guard.json"))
            except Exception: pass
            if (g.get("level") or 0) < 0.01: bad.append("radio silent after the update")
        if bad:
            log("PROBLEM after install:", bad, "→ rolling back"); install(files, None); restart(files); git("checkout", "-q", "--detach", cur)
            return "rolled-back"
    finally:
        try: os.remove(PAUSE)
        except OSError: pass
    log("installed OK", "· restarted:", ", ".join(u for u in units if u not in ("daemon-reload",)) or "nothing (pages reload by themselves)")
    return "ok" if "hype-updater" not in units else "ok-restart-updater"

def mapping_at(commit):
    from common import mapping
    git("checkout", "-q", "--detach", commit, check=False)   # read files.txt of that commit
    return mapping()

def main():
    os.makedirs("/var/backups/hype", exist_ok=True)
    while True:
        LOG.clear()
        try:
            git("fetch", "-q", "origin", "main")
            new = git("rev-parse", "origin/main")
            cur = open(DEPLOYED).read().strip() if os.path.exists(DEPLOYED) else ""
            if new != cur:
                res = deploy(cur, new)
                if res.startswith("ok"): open(DEPLOYED, "w").write(new)
                if res != "waiting" or not getattr(main, "told", None) == new:
                    write_status(res, new); main.told = new
                if res == "ok-restart-updater": log("the updater itself changed – restarting"); sh("systemctl restart --no-block hype-updater"); time.sleep(30)
                if res in ("conflict", "lab-failed", "rolled-back"):
                    time.sleep(600)            # don't hammer: try again in 10 min (or after a new commit)
        except Exception as e:
            log("error:", e); traceback.print_exc(); write_status("error", "", {"error": str(e)[:300]}); time.sleep(300)
        time.sleep(60)

if __name__ == "__main__":
    main()
