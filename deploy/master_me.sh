#!/bin/sh
# master_me (the LADSPA plugin behind Settings → Sound → master_me) is not part of Ubuntu: it lives in /usr/lib/ladspa.
#   master_me.sh ensure  – before every radio start: if the plugin is missing (new / reinstalled server), put back the copy
#                          from the repo (radio/ladspa, master_me 1.3.1, built on Ubuntu 24.04). An existing plugin is never touched.
#   master_me.sh build   – build it again from the official source (tag 1.3.1, ~5 min) and install it.
set -e
DST=/usr/lib/ladspa/master_me-ladspa.so
REPO=$(cd "$(dirname "$0")/.." && pwd)
case "$1" in
  ensure)
    [ -s "$DST" ] && exit 0
    mkdir -p /usr/lib/ladspa
    if [ -s "$REPO/radio/ladspa/master_me-ladspa.so" ]; then
      install -m 644 "$REPO/radio/ladspa/master_me-ladspa.so" "$DST"; echo "master_me was missing – installed the repo copy"
    else echo "master_me missing and no repo copy – run: $0 build" >&2; fi ;;
  build)
    apt-get install -y build-essential git >/dev/null
    T=$(mktemp -d); git clone -q --depth 1 --branch 1.3.1 --recursive --shallow-submodules https://github.com/trummerschlunk/master_me "$T/m"
    make -C "$T/m/plugin" ladspa HAVE_OPENGL=false -j"$(nproc)" >/dev/null
    install -m 644 "$T/m/bin/master_me-ladspa.so" "$DST"; rm -rf "$T"; echo "master_me built and installed" ;;
  *) echo "usage: $0 ensure|build" >&2; exit 2 ;;
esac
