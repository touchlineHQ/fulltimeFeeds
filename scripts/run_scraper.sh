#!/usr/bin/env bash
# One-shot daily scraper run for cron.
# Builds and runs the scraper container (scrape -> demo -> upload to R2),
# then cleans up the stopped container so repeated runs never collide.
set -euo pipefail

cd "$(dirname "$0")/.."

# Timestamped markers make skipped/never-started runs visible in the log
# (e.g. when the cron flock silently skips because a previous run held it).
echo "=== run_scraper start $(date -Is) ==="

trap 'echo "=== run_scraper end $(date -Is) rc=$? ==="; docker compose down >/dev/null 2>&1' EXIT

docker compose up --build
