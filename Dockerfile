# Use an official, lightweight Python image
FROM python:3.12-slim

# Install Playwright system dependencies
# xvfb + xauth let a headed browser run in the container: Cloudflare challenges
# headless Chromium on the pages carrying scores, so results need a real
# browser session rather than a headless one.
# x11vnc serves the virtual display over VNC, so a headless server can still
# have a browser someone drives by hand — see scripts/vnc_browser.sh. novnc and
# websockify put that same display behind a normal web page, because port 5900
# speaks RFB rather than HTTP: pointing a browser straight at it gets nothing,
# and a phone is unlikely to have a VNC client.
#
# openbox is not decoration: with no window manager on the display, an
# application window is not mapped and the VNC session shows an empty screen.
# xterm gives a terminal in that session, for a machine reached from a phone.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    xvfb \
    xauth \
    x11vnc \
    novnc \
    websockify \
    openbox \
    xterm \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Install Python packages (curl_cffi pinned: its browser impersonation must
# track a recent Chrome to keep Full-Time's WAF happy)
RUN pip install --no-cache-dir curl_cffi==0.16.1 beautifulsoup4 playwright lxml boto3 \
    websocket-client

# Optional, and off unless RESULTS_SESSION names it: nodriver drives Chrome
# without the attached-CDP pattern that Full-Time's challenge detects. Installed
# so the choice is a environment variable rather than a rebuild; see
# scraper/nodriver_session.py for what running it means.
RUN pip install --no-cache-dir nodriver

# Install Playwright browser binaries and their system dependencies
RUN playwright install chromium --with-deps

# Copy your repository code into the container
COPY . /app

# Fetch league data, add demo feeds, then publish everything to Cloudflare R2.
# A scrape that had to fall back to cached data still publishes whatever fresh
# leagues it has, but its non-zero exit code is preserved so cron alerts.
CMD sh -c "python scraper/scrape.py; rc=$?; python scraper/demo.py && python upload.py; exit $rc"