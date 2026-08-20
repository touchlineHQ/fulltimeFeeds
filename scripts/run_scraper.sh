#!/usr/bin/env bash
# One-shot daily scraper run for cron.
# Builds and runs the scraper container (scrape -> demo -> upload to R2),
# then cleans up the stopped container so repeated runs never collide.
set -euo pipefail

cd "$(dirname "$0")/.."

trap 'docker compose down >/dev/null 2>&1' EXIT

docker compose up --build
