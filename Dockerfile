# Use an official, lightweight Python image
FROM python:3.12-slim

# Install system dependencies for Tor, Playwright, and Git
RUN apt-get update && apt-get install -y --no-install-recommends \
    tor  \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Install Python packages
RUN pip install --no-cache-dir curl_cffi beautifulsoup4 playwright lxml boto3

# Install Playwright browser binaries and their system dependencies
RUN playwright install chromium --with-deps

# Copy your repository code into the container
COPY . /app

# Set environment variables expected by your scraper
ENV SOCKS_PROXY=socks5h://127.0.0.1:9050
ENV DEBUG=1

# Expose Tor SOCKS proxy port (optional, if you want to use it externally)
# EXPOSE 9050

# Tell the AWS CLI to point to Cloudflare R2 instead of Amazon S3
# ENV AWS_DEFAULT_REGION=auto
# ENV AWS_OUTPUT=json

# Start Tor in the background, wait for it, then run the python scripts
CMD tor & \
    echo "Waiting for Tor proxy to initialize..." && sleep 5 && \
    rm -rf calendars/ feeds/ && \
    python scraper/scrape.py && \
    python scraper/demo.py && \
    python upload.py
