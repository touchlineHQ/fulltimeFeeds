#!/usr/bin/env bash
# Put a real browser on this machine's screen, reachable over VNC.
#
# The scraper runs on a headless server, so there is no browser here to open a
# page in, check something, or use developer tools with. This starts one on a
# virtual display and serves that display over VNC, so you can drive it from a
# laptop or a phone on the same network.
#
#   docker compose run --rm --service-ports scraper scripts/vnc_browser.sh
#
# Then open http://<this-host>:6080/vnc.html in any browser — including a
# phone's. Port 5900 is there too for a native VNC client, but it speaks RFB,
# not HTTP, so a browser pointed at it sees nothing.
#
# The profile lives in /app/state, so whatever you do in it — sign in, accept a
# prompt, complete a challenge — is still there next time.
set -euo pipefail

PORT="${VNC_PORT:-5900}"
WEB_PORT="${VNC_WEB_PORT:-6080}"
PROFILE="${FULLTIME_CHROME_PROFILE:-/app/state/chrome-profile}"
URL="${1:-https://fulltime.thefa.com/}"
SCREEN="${VNC_SCREEN:-1280x900x24}"

if [ -z "${VNC_PASSWORD:-}" ]; then
    echo "Set VNC_PASSWORD (in .env) before starting." >&2
    echo "Note: docker compose treats an unquoted # in .env as a comment, so" >&2
    echo "quote the value if it contains one: VNC_PASSWORD='pa#ssword'" >&2
    echo "This puts a browser on your network; an unauthenticated one is a" >&2
    echo "browser anybody on that network can drive." >&2
    exit 1
fi

for binary in Xvfb x11vnc; do
    command -v "$binary" >/dev/null 2>&1 || {
        echo "$binary is missing — run 'docker compose build' to pick it up." >&2
        exit 1
    }
done

CHROME="$(find "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}" \
    -name chrome -path '*chrome-linux*' 2>/dev/null | sort | tail -1)"
[ -n "$CHROME" ] || { echo "No chromium in this image." >&2; exit 1; }

cleanup() { kill $(jobs -p) 2>/dev/null || true; }
trap cleanup EXIT

Xvfb :99 -screen 0 "$SCREEN" -nolisten tcp >/dev/null 2>&1 &
for _ in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done
[ -e /tmp/.X11-unix/X99 ] || { echo "Xvfb did not start." >&2; exit 1; }
export DISPLAY=:99

# RFB passwords are DES-based and truncate at 8 characters, silently: a longer
# one is stored as its first 8, and what you type will not match. Truncate here
# so the password that works is the one printed below.
VNC_SECRET="${VNC_PASSWORD:0:8}"
if [ "${#VNC_PASSWORD}" -gt 8 ]; then
    echo "NOTE: VNC passwords are limited to 8 characters by the protocol."
    echo "      Using the first 8 of VNC_PASSWORD: '$VNC_SECRET'"
    echo
fi

if ! x11vnc -storepasswd "$VNC_SECRET" /tmp/.vncpass >/tmp/storepasswd.log 2>&1; then
    echo "Could not store the VNC password:" >&2
    cat /tmp/storepasswd.log >&2
    exit 1
fi
[ -s /tmp/.vncpass ] || { echo "Password file is empty — refusing to start." >&2; exit 1; }

x11vnc -display :99 -rfbport "$PORT" -rfbauth /tmp/.vncpass \
    -forever -shared -noxdamage >/tmp/x11vnc.log 2>&1 &
X11VNC_PID=$!

sleep 2
if ! kill -0 "$X11VNC_PID" 2>/dev/null; then
    echo "x11vnc exited immediately:" >&2
    tail -20 /tmp/x11vnc.log >&2
    exit 1
fi

# Put the same display behind a web page, so any browser can reach it. The VNC
# password still applies — websockify only relays the RFB stream.
NOVNC_WEB=""
for candidate in /usr/share/novnc /usr/share/webapps/novnc; do
    [ -d "$candidate" ] && NOVNC_WEB="$candidate" && break
done
if [ -n "$NOVNC_WEB" ] && command -v websockify >/dev/null 2>&1; then
    websockify --web="$NOVNC_WEB" "$WEB_PORT" "localhost:$PORT" >/dev/null 2>&1 &
else
    echo "novnc/websockify not in this image — rebuild for browser access." >&2
    WEB_PORT=""
fi

mkdir -p "$PROFILE"
WIDTH="${SCREEN%%x*}"; REST="${SCREEN#*x}"; HEIGHT="${REST%%x*}"
"$CHROME" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
    --no-sandbox --window-size="$WIDTH,$HEIGHT" --window-position=0,0 "$URL" \
    >/dev/null 2>&1 &

cat <<EOF

  In a web browser (works on a phone):
    http://<this-host>:${WEB_PORT:-<rebuild for novnc>}/vnc.html

  Or with a native VNC client:
    <this-host>:$PORT       (RFB, not HTTP — a browser here shows nothing)

  Password:        $VNC_SECRET
                   (VNC truncates to 8 characters — this is what to type)
  Browser profile: $PROFILE (kept between runs)
  Opened:          $URL

  Ctrl-C here when you are done.

EOF

wait
