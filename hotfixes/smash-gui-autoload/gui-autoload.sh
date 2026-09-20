#!/bin/bash
# Launch a yuzu-family emulator through its GUI and load the game with File > Load File, instead of command-line autoboot (-g).
# Workaround: autoboot starts the game before the emulator has indexed the update/DLC layers (Citron hangs on "Launching...",
# Eden loads the wrong version and gameplay stalls). If the GUI load does not work, fall back to the normal "-f -g" launch.
# usage: gui-autoload.sh <citron|eden> /path/to/rom [window-title-pattern]     (default pattern: "Super Smash")
# Blocks until the emulator exits and forwards TERM/INT/HUP to it, so ES's exit hotkey behaves as before.
# Tunables (optional file gui-autoload.conf next to this script):
#   GA_START_FLAGS   extra flags for the GUI start, e.g. "-f"                          (default: none)
#   GA_FULLSCREEN    f11    = press F11 once the game has started
#                    config = set the emulator's own UI fullscreen setting before starting (no F11)
#                    none                                                                (default: f11)
#   GA_HIDE          cover  = show a black always-on-top mpv window while the emulator GUI loads the game
#                    0      = do not hide                                                (default: cover)
EMU="$1"; ROM="$2"; PAT="${3:-Super Smash}"
[ -n "$EMU" ] && [ -n "$ROM" ] || { echo "usage: $0 <citron|eden> rom [title-pattern]" >&2; exit 2; }
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
focus_cover() {   # the window that holds keyboard focus is drawn on top, so give the focus to the cover to hide the emulator
  [ -n "$COVER" ] || return 0
  [ -n "$CW" ] || CW=$(cover_win)
  [ -n "$CW" ] && xdotool windowfocus $((CW)) 2>/dev/null
}
cover_stop() { [ -n "$COVER" ] && { kill "$COVER" 2>/dev/null; wait "$COVER" 2>/dev/null; }; COVER=""; }
cleanup() { cover_stop; rm -f "$MARK"; }
fallback() {   # GUI load did not work: do exactly what the old launch did
  echo "gui-autoload: GUI load failed, falling back to -f -g" >&2
  kill -TERM $PID 2>/dev/null; wait $PID 2>/dev/null; cleanup
  exec "./${EMU}.AppImage" -f -g "$ROM"
}
winid() { wmctrl -l 2>/dev/null | command grep -i "$EMU" | command grep -vi "EmulationStation" | head -1 | awk '{print $1}'; }
trap cleanup EXIT

# fullscreen via the emulator's own setting (ES regenerated the config just before this script ran)
if [ "$GA_FULLSCREEN" = config ]; then
  sed -i -E 's/^fullscreen[[:space:]]*=.*/fullscreen=true/; s/^fullscreen\\default[[:space:]]*=.*/fullscreen\\default=false/' /userdata/system/configs/yuzu/qt-config.ini
fi

cover_start
# shellcheck disable=SC2086
"./${EMU}.AppImage" $GA_START_FLAGS &     # GUI start, no -g
PID=$!
trap 'kill -TERM $PID 2>/dev/null; wait $PID 2>/dev/null; cleanup; exit 0' TERM INT HUP

# 1) wait for the emulator window (max ~40s)
WID=""
for i in $(seq 1 400); do
  sleep 0.1
  kill -0 $PID 2>/dev/null || { cleanup; exit 1; }
  WID=$(winid); [ -n "$WID" ] && break
done
[ -n "$WID" ] || fallback
for i in $(seq 1 60); do [ -n "$(cover_win)" ] && break; sleep 0.05; done
focus_cover

# 2) wait until the game-list scan has settled
if [ "$EMU" = citron ]; then
  for i in $(seq 1 80); do sleep 0.5; focus_cover; [ "$LOG" -nt "$MARK" ] && command grep -q DonePopulating "$LOG" 2>/dev/null && break; done
else
  last=-1; stable=0     # Eden has no explicit marker: wait until its non-noise log stops growing for 3s (min 6s)
  for i in $(seq 1 80); do
    sleep 0.5; focus_cover
    n=$(command grep -vc libusb_claim "$LOG" 2>/dev/null)
    if [ "$n" = "$last" ]; then stable=$((stable+1)); else stable=0; last=$n; fi
    [ $stable -ge 6 ] && [ $i -ge 12 ] && break
  done
fi
sleep 2

# 3) File > Load File: Ctrl+O, type the path, Enter (keys go to the focused emulator window, which stays under the cover)
BEFORE=$(wmctrl -l 2>/dev/null | awk '{print $1}' | sort)
xdotool windowfocus --sync $((WID)) 2>/dev/null
sleep 0.5
xdotool key --clearmodifiers ctrl+o
for i in $(seq 1 40); do
  sleep 0.1
  NEW=$(wmctrl -l 2>/dev/null | awk '{print $1}' | sort | comm -13 <(echo "$BEFORE") -)
  [ -n "$NEW" ] && break
done
for d in $NEW; do xdotool windowfocus --sync $((d)) 2>/dev/null; done
sleep 0.2
xdotool type --delay 6 -- "$ROM"
sleep 0.4
xdotool key Return
sleep 0.3
focus_cover

# 4) confirm the game actually started: the window title gains the game's name (max ~30s)
OK=""
for i in $(seq 1 60); do
  sleep 0.5
  kill -0 $PID 2>/dev/null || { cleanup; exit 1; }
  wmctrl -l 2>/dev/null | command grep -i "$EMU" | command grep -qi "$PAT" && { OK=1; break; }
done
[ -n "$OK" ] || fallback

# 5) fullscreen (hides the emulator's menu/status bars), then remove the cover and bring the game to the front
xdotool windowfocus --sync $((WID)) 2>/dev/null
if [ "$GA_FULLSCREEN" = f11 ]; then sleep 0.3; xdotool key --clearmodifiers F11; sleep 0.8; fi
cover_stop
wmctrl -i -a "$WID" 2>/dev/null

rm -f "$MARK"
wait $PID
