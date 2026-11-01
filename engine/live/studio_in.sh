#!/bin/sh
# the studio's Mixcast mix (WebRTC → MediaMTX "studio") into Liquidsoap (harbor "studio"); waits while the studio is offline
PW=$(python3 -c 'import json;print(json.load(open("/etc/hype/live.json"))["harbor_pw"])')
while true; do
  # only start ffmpeg when the studio is really sending (a light check every second instead of a failing ffmpeg every 2 s)
  curl -sf -m 2 http://127.0.0.1:9997/v3/paths/get/studio 2>/dev/null | grep -q '"ready":true' || { sleep 1; continue; }
  ffmpeg -hide_banner -loglevel error -nostdin -rtsp_transport tcp -fflags nobuffer -flags low_delay -probesize 32768 -analyzeduration 0 \
    -i rtsp://127.0.0.1:8554/studio -vn -ac 2 -ar 48000 -c:a flac -compression_level 2 -f ogg -content_type application/ogg \
    "icecast://source:$PW@127.0.0.1:8005/studio" 2>/dev/null
  sleep 2
done
