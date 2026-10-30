#!/bin/sh
# music + jingles for the studio's Mixcast USB fader: the automation (Pulse sink hype_return) as Opus on MediaMTX "return"
export XDG_RUNTIME_DIR=/run/hype
exec ffmpeg -hide_banner -loglevel error -nostdin -f pulse -fragment_size 3840 -i hype_return.monitor -ac 2 -ar 48000 \
  -c:a libopus -b:a 256k -vbr on -compression_level 10 -application audio -frame_duration 20 -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/return
