#!/bin/bash
# TV for the website (HLS: 720p listed first = players start there, then 1080p / 480p / 360p by bandwidth – 360p keeps slow connections playing; segment numbers from the clock = unique names, cacheable) + a 720p preview for the panel (WebRTC via MediaMTX "preview", no B-frames)
# + a small 360p picture for the guests ("guestview", 600 kbps, no sound: guests hear the mix-minus)
exec ffmpeg -hide_banner -loglevel warning -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi -i rtmp://127.0.0.1/live/program \
 -filter_complex "[0:v]split=6[a][b][c][d][e][f];[a]scale_vaapi=w=1920:h=1080[v1];[b]scale_vaapi=w=1280:h=720[v2];[c]scale_vaapi=w=854:h=480[v3];[d]scale_vaapi=w=1280:h=720[pv];[e]scale_vaapi=w=640:h=360[gv];[f]scale_vaapi=w=640:h=360[v4]" \
 -map "[v1]" -map "[v2]" -map "[v3]" -map "[v4]" -map 0:a -map 0:a -map 0:a -map 0:a \
 -c:v h264_vaapi -g 50 -b:v:0 4500k -maxrate:v:0 4500k -b:v:1 2500k -maxrate:v:1 2500k -b:v:2 1000k -maxrate:v:2 1000k -b:v:3 650k -maxrate:v:3 650k \
 -c:a:0 copy -c:a:1 copy -c:a:2 aac -b:a:2 128k -c:a:3 aac -b:a:3 96k -f hls -hls_time 2 -hls_list_size 6 -hls_flags delete_segments+independent_segments -hls_start_number_source epoch \
 -master_pl_name master.m3u8 -var_stream_map "v:1,a:1 v:0,a:0 v:2,a:2 v:3,a:3" /dev/shm/hls/v%v/index.m3u8 \
 -map "[pv]" -map 0:a -c:a libopus -b:a 128k -ar 48000 -ac 2 -c:v h264_vaapi -bf 0 -g 50 -b:v 1800k -maxrate 1800k -bufsize 1800k -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/preview \
 -map "[gv]" -an -c:v h264_vaapi -bf 0 -g 50 -b:v 600k -maxrate 600k -bufsize 600k -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/guestview
