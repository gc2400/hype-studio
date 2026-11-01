#!/bin/bash
# Hype Studio nightly backup -> Hetzner Storage Box (Helsinki). Login with /root/.ssh/hype_backup (key only).
#  1. settings: library (cue points, playlists, clocks, settings), state, configs, OBS scenes -> settings/YYYY-MM-DD.tar.gz, 30 days kept
#  2. media: /srv/hype (music, clips, jingles, branding, recordings) mirrored; changed/deleted files kept 30 days in media-old/
#  (the TV copies in /srv/hype-data/tv are not backed up: the indexer makes them again from the originals)
set -u
BOX="u706784@u706784.your-storagebox.de"; PORT=23; KEY=/root/.ssh/hype_backup
SSH="ssh -p $PORT -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30"
STATE=/var/lib/hype/backup.json; DAY=$(date +%F); T0=$(date +%s); TMP=$(mktemp -d)
status() { python3 -c 'import json,sys,time; d={"t":time.time(),"ok":sys.argv[1]=="1","msg":sys.argv[2],"secs":int(sys.argv[3]),"size":sys.argv[4]}
try: old=json.load(open(sys.argv[5]))
except Exception: old={}
d["last_ok"]=d["t"] if d["ok"] else old.get("last_ok"); json.dump(d,open(sys.argv[5],"w"))' "$1" "$2" "$(( $(date +%s) - T0 ))" "${3:-}" "$STATE"; }
fail() { echo "backup FAILED: $1"; status 0 "$1"; rm -rf "$TMP"; exit 1; }
trap 'rm -rf "$TMP"' EXIT
# 1. settings
mkdir -p "$TMP/s"
python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()' /srv/hype-data/library.db "$TMP/s/library.db" || fail "library copy"
tar -C / -czf "$TMP/$DAY.tar.gz" --ignore-failed-read -C "$TMP" s -C / var/lib/hype etc/hype etc/nginx/sites-enabled etc/systemd/system root/.config/obs-studio opt/mediamtx/mediamtx.yml opt/hype 2>/dev/null
[ -s "$TMP/$DAY.tar.gz" ] || fail "settings archive"
$SSH "$BOX" mkdir -p settings media media-old >/dev/null 2>&1
rsync -a -e "$SSH" "$TMP/$DAY.tar.gz" "$BOX:settings/" || fail "settings upload (storage box not reachable?)"
# keep 30 days of settings
OLD=$($SSH "$BOX" ls settings 2>/dev/null | sort | head -n -30)
for f in $OLD; do $SSH "$BOX" rm "settings/$f" >/dev/null 2>&1; done
# safety: never mirror an empty or unmounted library (rsync --delete would move the whole backup to media-old)
[ -d /srv/hype/music ] && [ -n "$(ls -A /srv/hype/music 2>/dev/null)" ] || fail "library missing (disk not mounted?) - media not synced"
# 2. media mirror (gentle: lowest priority, limited speed so the streams never feel it)
nice -n 19 ionice -c3 rsync -a --delete --backup --backup-dir="../media-old/$DAY" --bwlimit=40000 --exclude '.*' \
  -e "$SSH" /srv/hype/ "$BOX:media/" || fail "media sync"
OLDM=$($SSH "$BOX" ls media-old 2>/dev/null | sort | head -n -30)
for d in $OLDM; do $SSH "$BOX" rm -rf "media-old/$d" >/dev/null 2>&1; done
SIZE=$(du -sh "$TMP/$DAY.tar.gz" | cut -f1)
status 1 "settings $SIZE + media mirror" "$SIZE"
echo "backup OK: settings $SIZE, media in sync, $(( $(date +%s) - T0 )) s"
