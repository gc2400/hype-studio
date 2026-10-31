#!/bin/bash
# Radio as HLS (AAC 128 kbps) for iPhone / the website, 4-second pieces in /dev/shm/radio-hls
# (served as https://stream.hyperadio.nl/radio.m3u8).
# Source: the internal 256k on-air stream from Liquidsoap (Icecast rec.mp3) - its timing is sample-exact.
# (Before: a Pulse capture, whose clock jitter gave small hiccups/dropouts in the HLS sound.)
mkdir -p /dev/shm/radio-hls && rm -f /dev/shm/radio-hls/*
exec ffmpeg -hide_banner -loglevel warning -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 2 \
  -i http://127.0.0.1:8000/rec.mp3 -vn -ac 2 -ar 48000 \
  -c:a aac -b:a 128k -profile:a aac_low \
  -f hls -hls_time 4 -hls_list_size 8 -start_number $(( $(date +%s) / 4 )) \
  -hls_flags delete_segments+independent_segments+omit_endlist+program_date_time \
  -hls_segment_type mpegts -hls_segment_filename /dev/shm/radio-hls/r%06d.ts /dev/shm/radio-hls/radio.m3u8
