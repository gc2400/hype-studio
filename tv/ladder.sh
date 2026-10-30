#!/bin/bash
# TV for the website (HLS 1080/720/480) + a 720p preview for the panel (WebRTC via MediaMTX "preview", no B-frames)
exec ffmpeg -hide_banner -loglevel warning -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi -i rtmp://127.0.0.1/live/program \
 -filter_complex "[0:v]split=4[a][b][c][d];[a]scale_vaapi=w=1920:h=1080[v1];[b]scale_vaapi=w=1280:h=720[v2];[c]scale_vaapi=w=854:h=480[v3];[d]scale_vaapi=w=1280:h=720[pv]" \
 -map "[v1]" -map "[v2]" -map "[v3]" -map 0:a -map 0:a -map 0:a \
 -c:v h264_vaapi -g 50 -b:v:0 4500k -maxrate:v:0 4500k -b:v:1 2500k -maxrate:v:1 2500k -b:v:2 1000k -maxrate:v:2 1000k \
 -c:a:0 copy -c:a:1 copy -c:a:2 aac -b:a:2 128k -f hls -hls_time 2 -hls_list_size 6 -hls_flags delete_segments+independent_segments \
 -master_pl_name master.m3u8 -var_stream_map "v:0,a:0 v:1,a:1 v:2,a:2" /dev/shm/hls/v%v/index.m3u8 \
 -map "[pv]" -map 0:a -c:a libopus -b:a 128k -ar 48000 -ac 2 -c:v h264_vaapi -bf 0 -g 50 -b:v 1800k -maxrate 1800k -bufsize 1800k -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/preview
