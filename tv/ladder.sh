#!/bin/bash
# TV for the website (HLS: 720p listed first = players start there, then 1080p / 480p / 360p by bandwidth – 360p keeps slow connections playing; segment numbers from the clock = unique names, cacheable) + a 360p preview for the panel (WebRTC via MediaMTX "preview", with sound, no B-frames, low delay)
# Every quality carries the SAME sound IN it (a copy of OBS's AAC 192k). A separate audio track (EXT-X-MEDIA group) was
# smoother for Safari's quality switches, but IPTV partners (Xyfe) got no sound with it – so the sound stays in (5 Nov).
# + a small 360p picture for the guests ("guestview", 600 kbps, no sound: guests hear the mix-minus)
find /dev/shm/hls -name '*.ts' -mmin +10 -delete 2>/dev/null    # pieces left over from an earlier run
rm -rf /dev/shm/hls/vaudio      # (the separate audio track of 5 Nov is no longer made)
# the copied sound is not counted in the BANDWIDTH ffmpeg writes in master.m3u8 (it only knows the video bitrate): add it
# (OBS AAC 192k ≈ 212 kbps with overhead), so a player on a slow line doesn't pick a quality it can't keep up with
rm -f /dev/shm/hls/master.m3u8      # (wait for the NEW one ffmpeg writes)
( for i in $(seq 1 30); do sleep 1; [ -s /dev/shm/hls/master.m3u8 ] && grep -q BANDWIDTH /dev/shm/hls/master.m3u8 && break; done; sleep 1
  awk -F'BANDWIDTH=' 'NF>1 { split($2, r, ","); $0 = $1 "BANDWIDTH=" (r[1] + 212000) substr($2, length(r[1]) + 1) } { print }' /dev/shm/hls/master.m3u8 > /dev/shm/hls/master.tmp && mv /dev/shm/hls/master.tmp /dev/shm/hls/master.m3u8 ) &
# piece numbers: the same for every quality AND the audio track (players pair them by number) – "epoch" numbering gave the
# audio track other numbers than the pictures (3 s apart, sound out of sync, 5 Nov); START in the name keeps names unique per run
START=$(date +%s)
# -copyts: picture and sound keep OBS's own clock – without it ffmpeg started the audio track's clock separately from the
# pictures' (the pictures wait for the first keyframe): the separate audio track ran ~3.5 s apart in players (5 Nov)
exec ffmpeg -hide_banner -loglevel warning -copyts -fflags nobuffer -flags low_delay -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi -i rtmp://127.0.0.1/live/program \
 -filter_complex "[0:v]split=6[a][b][c][d][e][f];[a]scale_vaapi=w=1920:h=1080[v1];[b]scale_vaapi=w=1280:h=720[v2];[c]scale_vaapi=w=854:h=480[v3];[d]scale_vaapi=w=640:h=360[pv];[e]scale_vaapi=w=640:h=360[gv];[f]scale_vaapi=w=640:h=360[v4]" \
 -map "[v1]" -map "[v2]" -map "[v3]" -map "[v4]" -map 0:a -map 0:a -map 0:a -map 0:a \
 -c:v h264_vaapi -g 50 -b:v:0 4500k -maxrate:v:0 4500k -b:v:1 2500k -maxrate:v:1 2500k -b:v:2 1000k -maxrate:v:2 1000k -b:v:3 650k -maxrate:v:3 650k \
 -c:a copy -f hls -hls_time 2 -hls_list_size 6 -hls_flags delete_segments+independent_segments+program_date_time -hls_start_number_source generic -hls_segment_filename "/dev/shm/hls/v%v/s${START}_%d.ts" \
 -master_pl_name master.m3u8 -var_stream_map "v:1,a:1 v:0,a:0 v:2,a:2 v:3,a:3" /dev/shm/hls/v%v/index.m3u8 \
 -map "[pv]" -map 0:a -c:a libopus -b:a 128k -ar 48000 -ac 2 -c:v h264_vaapi -bf 0 -g 50 -async_depth 1 -b:v 600k -maxrate 600k -bufsize 600k -max_interleave_delta 100000 -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/preview \
 -map "[gv]" -an -c:v h264_vaapi -bf 0 -g 50 -async_depth 1 -b:v 600k -maxrate 600k -bufsize 600k -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/guestview
