#!/bin/bash
# Radio as HLS (AAC 128 kbps) for iPhone / the website: straight from what goes on air (Pulse sink hype_air, lossless),
# 4-second pieces in /dev/shm/radio-hls (served as https://stream.hyperadio.nl/radio.m3u8).
export XDG_RUNTIME_DIR=/run/hype
mkdir -p /dev/shm/radio-hls && rm -f /dev/shm/radio-hls/*
exec ffmpeg -hide_banner -loglevel warning -f pulse -fragment_size 3840 -i hype_air.monitor -ac 2 -ar 48000 \
  -c:a aac -b:a 128k -profile:a aac_low \
  -f hls -hls_time 4 -hls_list_size 8 -hls_flags delete_segments+independent_segments+omit_endlist+program_date_time \
  -hls_segment_type mpegts -hls_segment_filename /dev/shm/radio-hls/r%06d.ts /dev/shm/radio-hls/radio.m3u8
