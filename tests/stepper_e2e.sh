#!/usr/bin/env bash
# The version stepper (assets/blyg/version-nav.js) in a real browser,
# against a build of exampleSite/ served locally: with JavaScript on it
# steps an item through its pins and live version in place, fetching only
# pinned versions (§8.4); with it off, every pin is still a plain link.
#
# Run from anywhere; needs hugo, go, python3, node and the playwright
# package (npm), with a Chromium it can launch. CI runs it.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
server=
trap '[ -n "$server" ] && kill "$server" 2>/dev/null; rm -rf "$work"' EXIT

port=${BLYG_STEPPER_PORT:-8765}
base="http://127.0.0.1:$port/"
cd "$repo/exampleSite"
hugo --minify --quiet --baseURL "$base" --destination "$work/public"
python3 -m http.server "$port" --bind 127.0.0.1 --directory "$work/public" \
  >/dev/null 2>&1 &
server=$!
for _ in $(seq 50); do
  curl -fs "$base" >/dev/null 2>&1 && break
  sleep 0.1
done

node "$repo/tests/stepper_e2e.mjs" "$base"
