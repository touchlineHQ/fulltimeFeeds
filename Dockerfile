# Use an official, lightweight Python image
FROM python:3.12-slim

# Install Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
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

# Tell the AWS CLI to point to Cloudflare R2 instead of Amazon S3
# ENV AWS_DEFAULT_REGION=auto
# ENV AWS_OUTPUT=json

# Fetch league data, add demo feeds, then publish everything to Cloudflare R2.
CMD python scraper/scrape.py && python scraper/demo.py && python upload.py