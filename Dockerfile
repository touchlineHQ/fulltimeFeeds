# Use an official, lightweight Python image
FROM python:3.12-slim

# Install Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Install Python packages (curl_cffi pinned: its browser impersonation must
# track a recent Chrome to keep Full-Time's WAF happy)
RUN pip install --no-cache-dir curl_cffi==0.16.1 beautifulsoup4 playwright lxml boto3

# Install Playwright browser binaries and their system dependencies
RUN playwright install chromium --with-deps

# Copy your repository code into the container
COPY . /app

# Fetch league data, add demo feeds, then publish everything to Cloudflare R2.
# A scrape that had to fall back to cached data still publishes whatever fresh
# leagues it has, but its non-zero exit code is preserved so cron alerts.
CMD sh -c "python scraper/scrape.py; rc=$?; python scraper/demo.py && python upload.py; exit $rc"