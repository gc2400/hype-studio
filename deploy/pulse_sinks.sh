#!/bin/sh
# The audio bus: hype_air (radio -> TV), hype_fx (pads / effects), hype_return. Waits until PulseAudio answers, then makes
# each sink that is missing. Used by hype-pulse (after start) AND before every radio start: on 16 Nov the server rebooted
# for a kernel update, PulseAudio was not ready after the old fixed 2 s, the sinks were never made and the radio could not
# start for 4.5 hours ("pulse_out(hype_fx): No such entity").
export HOME=/root XDG_RUNTIME_DIR=/run/hype
i=0
while [ $i -lt 30 ] && ! pactl info >/dev/null 2>&1; do sleep 1; i=$((i+1)); done
for s in hype_air:HypeAir hype_fx:HypeFX hype_return:HypeReturn; do
  n=${s%%:*}
  pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx "$n" || \
    pactl load-module module-null-sink sink_name=$n sink_properties=device.description=${s##*:} rate=48000 >/dev/null
done
exit 0
