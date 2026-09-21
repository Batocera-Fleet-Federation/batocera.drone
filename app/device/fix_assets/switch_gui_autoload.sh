#!/bin/bash
# Load an Eden/Citron game through File > Load File after GUI indexing settles.
# Installed by Batocera Drone; invoked only by drone-switch-gui-launcher.py.
EMU="$1"; ROM="$2"; PAT="${3:-$(basename "$2")}";
[ -n "$EMU" ] && [ -n "$ROM" ] || { echo "usage: $0 <citron|eden> rom [window-title-pattern]" >&2; exit 2; }
GA_START_FLAGS=""; GA_FULLSCREEN="f11"; GA_HIDE="cover"
CONF="$(dirname "$(readlink -f "$0")")/gui-autoload.conf"; [ -f "$CONF" ] && . "$CONF"
LOG="/userdata/system/configs/yuzu/log/${EMU}_log.txt"
export DISPLAY="${DISPLAY:-:0}"
cd /userdata/system/rgs/emulators/switch || exit 1
MARK=$(mktemp /tmp/gui-autoload.XXXXXX)
COVER=""; CW=""

cover_start() {
  [ "$GA_HIDE" = cover ] && command -v mpv >/dev/null 2>&1 || return 0
  mpv --no-config --no-border --ontop --geometry=1920x1080+0+0 --no-audio --no-terminal \
      --no-input-default-bindings --input-conf=/dev/null --cursor-autohide=always --keep-open=always --loop-file=inf \
      "av://lavfi:color=c=black:s=1920x1080:r=2" >/dev/null 2>&1 &
  COVER=$!
}
cover_win() { wmctrl -lp 2>/dev/null | awk -v p="$COVER" '$3==p{print $1; exit}'; }
focus_cover() {
  [ -n "$COVER" ] || return 0
  [ -n "$CW" ] || CW=$(cover_win)
  [ -n "$CW" ] && xdotool windowfocus $((CW)) 2>/dev/null
}
cover_stop() { [ -n "$COVER" ] && { kill "$COVER" 2>/dev/null; wait "$COVER" 2>/dev/null; }; COVER=""; }
cleanup() { cover_stop; rm -f "$MARK"; }
fallback() {
  echo "gui-autoload: GUI load failed, falling back to -f -g" >&2
  kill -TERM "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; cleanup
  exec "./${EMU}.AppImage" -f -g "$ROM"
}
winid() { wmctrl -l 2>/dev/null | command grep -i "$EMU" | command grep -vi "EmulationStation" | head -1 | awk '{print $1}'; }
trap cleanup EXIT

if [ "$GA_FULLSCREEN" = config ]; then
  sed -i -E 's/^fullscreen[[:space:]]*=.*/fullscreen=true/; s/^fullscreen\default[[:space:]]*=.*/fullscreen\default=false/' /userdata/system/configs/yuzu/qt-config.ini
fi

cover_start
# shellcheck disable=SC2086
"./${EMU}.AppImage" $GA_START_FLAGS &
PID=$!
trap 'kill -TERM "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; cleanup; exit 0' TERM INT HUP

WID=""
for i in $(seq 1 400); do
  sleep 0.1
  kill -0 "$PID" 2>/dev/null || { cleanup; exit 1; }
  WID=$(winid); [ -n "$WID" ] && break
done
[ -n "$WID" ] || fallback
for i in $(seq 1 60); do [ -n "$(cover_win)" ] && break; sleep 0.05; done
focus_cover

if [ "$EMU" = citron ]; then
  for i in $(seq 1 80); do sleep 0.5; focus_cover; [ "$LOG" -nt "$MARK" ] && command grep -q DonePopulating "$LOG" 2>/dev/null && break; done
else
  last=-1; stable=0
  for i in $(seq 1 80); do
    sleep 0.5; focus_cover
    n=$(command grep -vc libusb_claim "$LOG" 2>/dev/null)
    if [ "$n" = "$last" ]; then stable=$((stable+1)); else stable=0; last=$n; fi
    [ "$stable" -ge 6 ] && [ "$i" -ge 12 ] && break
  done
fi
sleep 2

BEFORE=$(wmctrl -l 2>/dev/null | awk '{print $1}' | sort)
xdotool windowfocus --sync $((WID)) 2>/dev/null
sleep 0.5
xdotool key --clearmodifiers ctrl+o
NEW=""
for i in $(seq 1 40); do
  sleep 0.1
  NEW=$(wmctrl -l 2>/dev/null | awk '{print $1}' | sort | comm -13 <(echo "$BEFORE") -)
  [ -n "$NEW" ] && break
done
for dialog in $NEW; do xdotool windowfocus --sync $((dialog)) 2>/dev/null; done
sleep 0.2
xdotool type --delay 6 -- "$ROM"
sleep 0.4
xdotool key Return
sleep 0.3
focus_cover

OK=""
for i in $(seq 1 60); do
  sleep 0.5
  kill -0 "$PID" 2>/dev/null || { cleanup; exit 1; }
  wmctrl -l 2>/dev/null | command grep -i "$EMU" | command grep -Fqi -- "$PAT" && { OK=1; break; }
done
[ -n "$OK" ] || fallback

xdotool windowfocus --sync $((WID)) 2>/dev/null
if [ "$GA_FULLSCREEN" = f11 ]; then sleep 0.3; xdotool key --clearmodifiers F11; sleep 0.8; fi
cover_stop
wmctrl -i -a "$WID" 2>/dev/null

rm -f "$MARK"
wait "$PID"
