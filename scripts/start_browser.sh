#!/usr/bin/env bash
# Start a Chrome-family browser with a debugging port, for the probe to attach to.
#
# Attaching to a browser you started is the one mode Cloudflare has not refused:
# Playwright launching a browser sets navigator.webdriver and --enable-automation,
# and every launched mode is challenged. This starts an ordinary browser instead.
#
#   ./scripts/start_browser.sh          # start it (or report one already running)
#   ./scripts/start_browser.sh --stop   # stop the one this script started
#
# Then load the results page in the window once by hand before probing.
set -uo pipefail

PORT="${FULLTIME_CDP_PORT:-9222}"
PROFILE="${FULLTIME_CHROME_PROFILE:-$HOME/.fulltime-chrome}"
PIDFILE="$PROFILE/.launcher.pid"

# Every name a Chrome-family browser ships under, across distros and packaging.
CANDIDATES=(
    google-chrome google-chrome-stable google-chrome-beta
    chromium chromium-browser chromium-freeworld
    brave-browser brave microsoft-edge microsoft-edge-stable vivaldi
    /usr/bin/google-chrome /usr/bin/chromium /snap/bin/chromium
    /var/lib/flatpak/exports/bin/com.google.Chrome
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    "/Applications/Chromium.app/Contents/MacOS/Chromium"
)

port_open() {
    curl -sS --max-time 3 "http://localhost:$PORT/json/version" >/dev/null 2>&1
}

if [ "${1:-}" = "--stop" ]; then
    if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Stopped the browser started from $PIDFILE"
        rm -f "$PIDFILE"
    else
        echo "Nothing to stop (no live PID in $PIDFILE)"
    fi
    exit 0
fi

if port_open; then
    echo "A browser is already listening on port $PORT:"
    curl -sS "http://localhost:$PORT/json/version" | sed 's/^/  /'
    exit 0
fi

BROWSER=""
for candidate in "${CANDIDATES[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then BROWSER="$candidate"; break; fi
    if [ -x "$candidate" ]; then BROWSER="$candidate"; break; fi
done

if [ -z "$BROWSER" ]; then
    echo "No Chrome-family browser found. Tried:" >&2
    printf '  %s\n' "${CANDIDATES[@]}" >&2
    echo >&2
    echo "If yours is elsewhere, start it by hand:" >&2
    echo "  <your-browser> --remote-debugging-port=$PORT --user-data-dir=\"$PROFILE\"" >&2
    exit 1
fi

echo "Using $BROWSER"
mkdir -p "$PROFILE"
"$BROWSER" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE" \
    --no-first-run --no-default-browser-check >"$PROFILE/browser.log" 2>&1 &
echo $! > "$PIDFILE"

for _ in $(seq 1 20); do
    if port_open; then
        echo "Debugging port $PORT is up (pid $(cat "$PIDFILE"))."
        echo
        echo "Now, in that window, open the results page once by hand:"
        echo "  https://fulltime.thefa.com/results/1/100000.html?selectedSeason=918978398&selectedFixtureGroupKey="
        echo "Confirm it shows results, then run the probe."
        exit 0
    fi
    sleep 1
done

echo "Browser started but port $PORT never opened — see $PROFILE/browser.log" >&2
exit 1
