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
SAVE_DIR="${RESULTS_HTML_DIR:-/app/state/results}"
# The browser exposes a debugging port so save_open_tabs.py can read the pages
# you have loaded. Nothing navigates through it — it only reads what is on
# screen, which is the same thing pressing Ctrl+S on each tab would write.
CDP_PORT="${VNC_CDP_PORT:-9222}"
SCREEN="${VNC_SCREEN:-1280x900x24}"

# Typing a URL into a remote browser from a phone is miserable, so the tabs
# that answer the usual questions are opened up front: what this machine gets
# from the results page, and what public IP it comes from — which can then be
# compared against the same page on the phone itself.
if [ "$#" -gt 0 ]; then
    URLS=("$@")
else
    # Every configured league's results page, read from LEAGUES rather than
    # written out here, so the tabs follow the scrape list. Save each one into
    # RESULTS_HTML_DIR and the scraper parses it; see "Saving results by hand".
    mapfile -t URLS < <(python3 -c "
import sys
sys.path.insert(0, '/app/scraper')
from unittest.mock import MagicMock
for mod in ('curl_cffi', 'curl_cffi.requests'):
    sys.modules.setdefault(mod, MagicMock())
import scrape
for season_id, _ in scrape.LEAGUES:
    print(f'{scrape.RESULTS_URL}?selectedSeason={season_id}&selectedFixtureGroupKey=')
" 2>/dev/null)
    if [ "${#URLS[@]}" -eq 0 ]; then
        echo "Could not read the league list — opening the site's front page." >&2
        URLS=("https://fulltime.thefa.com/")
    fi
fi

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

CHROME=""
for root in "${PLAYWRIGHT_BROWSERS_PATH:-}" "$HOME/.cache/ms-playwright" \
            /root/.cache/ms-playwright /opt/pw-browsers; do
    [ -n "$root" ] && [ -d "$root" ] || continue
    CHROME="$(find "$root" -name chrome -path '*chrome-linux*' 2>/dev/null | sort | tail -1)"
    [ -n "$CHROME" ] && break
done
[ -n "$CHROME" ] || { echo "No chromium in this image." >&2; exit 1; }

cleanup() { kill $(jobs -p) 2>/dev/null || true; }
trap cleanup EXIT
# As PID 1 in a container, a shell gets no default signal disposition: SIGINT is
# ignored unless something is explicitly listening, so Ctrl-C does nothing at
# all. Handling it here is what makes the instruction to press it true.
trap 'echo; echo "Shutting down ..."; exit 130' INT TERM

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
    websockify --web="$NOVNC_WEB" "$WEB_PORT" "localhost:$PORT" >/tmp/websockify.log 2>&1 &
    WEBSOCKIFY_PID=$!
    # Check it is serving rather than assuming: a port that never opened looks
    # exactly like a page that will not load.
    NOVNC_OK=""
    for _ in $(seq 1 20); do
        if curl -sf -o /dev/null "http://localhost:$WEB_PORT/vnc.html"; then
            NOVNC_OK=1
            break
        fi
        kill -0 "$WEBSOCKIFY_PID" 2>/dev/null || break
        sleep 0.5
    done
    if [ -z "$NOVNC_OK" ]; then
        echo "websockify is not serving on $WEB_PORT. Its output:" >&2
        tail -20 /tmp/websockify.log >&2
        echo "The native VNC port $PORT should still work." >&2
        WEB_PORT=""
    fi
else
    echo "novnc/websockify not in this image — rebuild for browser access." >&2
    WEB_PORT=""
fi

# Without a window manager the browser's window is never mapped and the VNC
# session shows nothing at all — which looks exactly like a browser that failed
# to start.
if command -v openbox >/dev/null 2>&1; then
    openbox >/tmp/openbox.log 2>&1 &
    sleep 1
else
    echo "NOTE: no window manager in this image (rebuild) — the screen may stay blank." >&2
fi

# A terminal in the session, for when the only device to hand is a phone.
if command -v xterm >/dev/null 2>&1; then
    xterm -geometry 100x24+0+600 -fa Monospace -fs 10 >/tmp/xterm.log 2>&1 &
fi

mkdir -p "$PROFILE" "$SAVE_DIR"

# Chromium records the hostname and pid holding a profile in these files, and
# refuses to start when they name someone else. Every `docker compose run` gets
# a fresh container hostname, so a profile kept in a volume always looks held by
# "another computer" after the first run — and a browser killed with the
# container never gets to clean them up. Nothing else can be using the profile
# here: this container just started, and it starts exactly one browser.
for lock in SingletonLock SingletonCookie SingletonSocket; do
    # -e alone is not enough: SingletonLock is a symlink to "hostname-pid",
    # which is not a real path, so a dangling link tests false and survives.
    if [ -e "$PROFILE/$lock" ] || [ -L "$PROFILE/$lock" ]; then
        rm -f "$PROFILE/$lock"
        CLEARED_LOCKS=1
    fi
done
[ -n "${CLEARED_LOCKS:-}" ] && echo "Cleared a stale profile lock from an earlier run."

WIDTH="${SCREEN%%x*}"; REST="${SCREEN#*x}"; HEIGHT="${REST%%x*}"
"$CHROME" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
    --no-sandbox --remote-debugging-port="$CDP_PORT" \
    --window-size="$WIDTH,$HEIGHT" --window-position=0,0 \
    "${URLS[@]}" >/tmp/chrome.log 2>&1 &
CHROME_PID=$!

sleep 3
if ! kill -0 "$CHROME_PID" 2>/dev/null; then
    echo "The browser exited immediately. Its output:" >&2
    tail -20 /tmp/chrome.log >&2
    echo >&2
    echo "Leaving the session up so you can look — there is a terminal in it." >&2
    BROWSER_FAILED=1
fi

# Save each results tab as it finishes loading, so the pages do not have to be
# saved by hand one at a time.
AUTOSAVE_STATE="off (VNC_AUTOSAVE=0) — save by hand with Ctrl+S into $SAVE_DIR"
if [ "${VNC_AUTOSAVE:-1}" != "0" ] && [ -z "${BROWSER_FAILED:-}" ]; then
    AUTOSAVE_STATE="into $SAVE_DIR as each tab finishes loading"
    RESULTS_HTML_DIR="$SAVE_DIR" python3 "$(dirname "$0")/save_open_tabs.py" \
        --endpoint "http://localhost:$CDP_PORT" --watch \
        || echo "Tab saving stopped; the session is unaffected." >&2 &
fi

cat <<EOF

  In a web browser (works on a phone):
    http://<this-host>:${WEB_PORT:-<rebuild for novnc>}/vnc.html

  Or with a native VNC client:
    <this-host>:$PORT       (RFB, not HTTP — a browser here shows nothing)

  Password:        $VNC_SECRET
                   (VNC truncates to 8 characters — this is what to type)
  Browser profile: $PROFILE (kept between runs)
  Tabs opened:     ${#URLS[@]} (one results page per configured league)
  Browser:         ${BROWSER_FAILED:+FAILED TO START — see above}${BROWSER_FAILED:-running}
  Saving tabs:     ${AUTOSAVE_STATE}

  Ctrl-C here when you are done.

EOF

wait
