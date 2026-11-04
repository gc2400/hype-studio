#!/bin/bash
# Weekly planned maintenance (hype-maintenance.timer, Monday 04:00 – the quietest hour).
# Ubuntu's automatic security updates no longer restart Hype's services (system/needrestart-hype.conf: that caused the
# dead air of 4 Nov). So once a week, at a planned moment, whatever still runs an old library is restarted here, in a
# safe order – and the server is rebooted only when an update asks for it (a new kernel, ~once a month).
# Never while LIVE, never during an update. Result: /var/lib/hype/maintenance.json + the journal.
set -u
LOG=(); log(){ echo "maintenance: $*"; LOG+=("$(date +%H:%M:%S) $*"); }
save(){ python3 - "$1" "${LOG[@]}" <<'PY'
import json, sys, time
json.dump({"t": int(time.time()), "result": sys.argv[1], "log": sys.argv[2:]}, open("/var/lib/hype/maintenance.json", "w"), indent=1)
PY
}
LIVE=$(curl -s -m 5 127.0.0.1:8090/live | python3 -c 'import json,sys; print(json.load(sys.stdin).get("live"))' 2>/dev/null)
if [ "$LIVE" = "True" ]; then log "LIVE from the studio – skipped (next week again)"; save skipped; exit 0; fi
if [ -e /run/hype/guard_pause ] && [ $(( $(date +%s) - $(stat -c %Y /run/hype/guard_pause) )) -lt 900 ]; then
  log "an update is being installed – skipped"; save skipped; exit 0; fi

if [ -e /var/run/reboot-required ]; then
  log "reboot asked for by: $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)"; save reboot; sync
  systemctl reboot; exit 0
fi

# the Hype services (+ nginx, mediamtx) that still use an old (replaced) library
NEED=$(needrestart -b -r l 2>/dev/null | sed -n 's/^NEEDRESTART-SVC: //p' | sed 's/\.service$//' | grep -E '^(hype-|nginx|mediamtx)' | sort -u | tr '\n' ' ')
if [ -z "$NEED" ]; then log "everything up to date – nothing to restart"; save ok; exit 0; fi
log "old libraries in use: $NEED"
has(){ case " $NEED " in *" $1 "*) return 0;; esac; return 1; }
# the audio bus restarted = the radio and OBS lose their sound connection: they go along
if has hype-pulse; then NEED="$NEED hype-radio hype-obs"; fi
mkdir -p /run/hype; touch /run/hype/guard_pause            # the guard doesn't count this as a problem
# order: the base first (audio bus, WebRTC hub, web server), then radio and TV, the engine last (it queues the music again)
ORDER="hype-pulse hype-mediamtx nginx hype-radio hype-radio-hls hype-obs hype-ladder hype-return hype-studio-in hype-stats hype-auth hype-indexer hype-engine"
DONE=""
for u in $ORDER $NEED; do
  has "$u" || continue
  case " $DONE " in *" $u "*) continue;; esac
  case "$u" in hype-updater|hype-guard) continue;; esac                  # these two last, after the pause is over
  case "$u" in hype-backup|hype-maintenance|hype-rec-*) continue;; esac   # one-shot jobs / a running recording: never
  systemctl restart "$u" && DONE="$DONE $u"; sleep 2
done
# wait until the radio plays again (max 60 s)
OK=0; for i in $(seq 1 30); do
  L=$(curl -s -m 2 127.0.0.1:8090/level | python3 -c 'import json,sys; d=json.load(sys.stdin); print(1 if max(d["l"], d["r"]) > 0.05 else 0)' 2>/dev/null)
  [ "$L" = 1 ] && OK=1 && break; sleep 2; done
rm -f /run/hype/guard_pause
for u in hype-guard hype-updater; do has "$u" && systemctl restart "$u" && DONE="$DONE $u"; done
log "restarted:$DONE"
if [ $OK = 1 ]; then log "radio plays again"; save ok; else log "radio still silent after 60 s – the guard takes over"; save warn; fi
