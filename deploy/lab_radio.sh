#!/bin/bash
# Test a radio.liq on a SECOND Liquidsoap (other ports, mount lab.mp3, no Pulse outputs) – never touches the air.
# usage: lab_radio.sh candidate.liq   → exit 0 when the lab plays a song with sound (peak > 0.05, loudness a real number)
export XDG_RUNTIME_DIR=/run/hype
C="$1"; L=/tmp/lab_update.liq
cp /var/lib/hype/sound.json /tmp/lab_sound.json 2>/dev/null
sed -e "s/telnet.port.set(1234)/telnet.port.set(1244)/" -e "s/port=8005/port=8015/" -e "s/mount=\"test.mp3\"/mount=\"lab.mp3\"/" -e "s/mount=\"radio.mp3\"/mount=\"labradio.mp3\"/" \
    -e "s#/var/log/hype/liquidsoap.log#/tmp/lab_liq.log#" -e "s#/var/lib/hype/sound.json#/tmp/lab_sound.json#" \
    -e "s/device=\"hype_/device=\"lab_/" "$C" > $L
liquidsoap --check $L >/tmp/lab_check.log 2>&1 || { echo "FAIL: liquidsoap --check"; grep -v "^$" /tmp/lab_check.log | head -5; exit 1; }
for p in $(pgrep -f "liquidsoap /tmp/lab_update[.]liq"); do kill $p; done; sleep 1
setsid nohup liquidsoap $L >/tmp/lab_run.log 2>&1 &
for i in $(seq 1 40); do (exec 3<>/dev/tcp/127.0.0.1/1244) 2>/dev/null && break; sleep 1; done; sleep 2
T(){ python3 -c "
import socket,sys,time; s=socket.create_connection(('127.0.0.1',1244)); s.sendall((sys.argv[1]+'\nquit\n').encode()); time.sleep(.3); print(s.recv(400).decode().split('\r')[0])" "$1" 2>/dev/null; }
SONG=$(ls /srv/hype/music/*/*/*.mp3 2>/dev/null | head -1)
T "q.push annotate:liq_cue_in=\"20\",liq_amplify=\"0 dB\":$SONG" >/dev/null
sleep 12; OK=0; OUT=""
for i in 1 2 3 4 5 6; do v=$(T radio.level); OUT="$OUT | $v"
  python3 -c "
import sys,math; v=[float(x) for x in sys.argv[1].split()]; ok = max(v[0],v[1])>0.05 and (len(v)<5 or not math.isnan(v[4])); sys.exit(0 if ok else 1)" "$v" && OK=$((OK+1)); sleep 1; done
for p in $(pgrep -f "liquidsoap /tmp/lab_update[.]liq"); do kill $p; done
echo "lab levels:$OUT"
[ $OK -ge 3 ] && { echo "PASS ($OK/6 good readings)"; exit 0; } || { echo "FAIL: silent or NaN in the lab ($OK/6)"; exit 1; }
