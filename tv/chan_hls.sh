#!/bin/bash
# HLS (AAC 128k, 4 s pieces) for one theme channel, like radio_hls.sh for Hype Radio: from its internal 256k Icecast mount.
# Started / stopped by hype-channels (engine/chan.py). Served as https://stream.hyperadio.nl/<id>.m3u8 (pieces under /ch/<id>/).
ID="$1"; [[ "$ID" =~ ^[a-z][a-z0-9]{1,19}$ ]] || exit 2
D=/dev/shm/ch-$ID; mkdir -p $D && rm -f $D/*
exec ffmpeg -hide_banner -loglevel warning -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 2 \
  -i http://127.0.0.1:8000/$ID-rec.mp3 -vn -ac 2 -ar 48000 \
  -c:a aac -b:a 128k -profile:a aac_low \
  -f hls -hls_time 4 -hls_list_size 8 -hls_delete_threshold 8 -start_number $(( $(date +%s) / 2 )) \
  -hls_flags delete_segments+independent_segments+omit_endlist+program_date_time \
  -hls_base_url /ch/$ID/ -hls_segment_type mpegts -hls_segment_filename $D/c%06d.ts $D/$ID.m3u8
