#!/usr/bin/env bash
# The HTML pages are presentation: building them must change no protocol
# surface. Builds exampleSite/ with [params.blyg] pages on and off and
# requires every file under blyg/ other than the pages themselves and the
# version stepper's script -- blyg.json, items/index.json, items/*.json,
# the pins, blogroll.opml, media -- to be byte-identical, apart from the
# two places the protocol names the page: each live item document's
# "page" (§5.8), and each feed.xml entry's <link> (§7).
#
# Run from anywhere; needs hugo, go and python3 on PATH. CI runs it.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cd "$repo/exampleSite"
hugo --minify --quiet --destination "$work/on"
HUGO_PARAMS_BLYG_PAGES=false hugo --minify --quiet --destination "$work/off"

python3 - "$work/on/blyg" "$work/off/blyg" <<'PY'
import re, sys
from pathlib import Path

on, off = Path(sys.argv[1]), Path(sys.argv[2])

def surfaces(root):
    """Every file but the HTML pages and the stepper's script."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
            and p.name != "index.html" and not p.name.startswith("version-nav.")}

def unlinked(feed):
    """feed.xml without its entries' <link>s (the channel keeps its own)."""
    return re.sub(rb"<item>.*?</item>",
                  lambda m: re.sub(rb"\s*<link>[^<]*</link>", b"", m.group(0)),
                  feed, flags=re.S)

# A live item document (not index.json, not a pin under items/{id}/).
PAGE_FIELD = re.compile(r"^items/(?!index\.json$)[^/]+\.json$")

def unpaged(doc):
    """An item document without its "page" (sorted after "origin")."""
    return re.sub(rb',"page":"[^"]*"', b"", doc)

problems = []
have, want = surfaces(on), surfaces(off)
for name in sorted(have ^ want):
    problems.append(f"{name}: only built with pages {'on' if name in have else 'off'}")
for name in sorted(have & want):
    a, b = (on / name).read_bytes(), (off / name).read_bytes()
    if name == "feed.xml":
        if unlinked(a) != unlinked(b):
            problems.append("feed.xml: differs by more than its entries' <link>s")
        if unlinked(a) == a:
            problems.append("feed.xml: no entry links its page with pages on")
    elif PAGE_FIELD.match(name):
        if unpaged(a) != b:
            problems.append(f"{name}: differs by more than its \"page\"")
        if unpaged(a) == a:
            problems.append(f"{name}: no \"page\" with pages on (§5.8)")
    elif a != b:
        problems.append(f"{name}: differs with pages on and off")
for p in problems:
    print(f"FAIL: {p}", file=sys.stderr)
if problems:
    sys.exit(1)
for name in ("blyg.json", "items/index.json", "feed.xml"):
    assert name in have, name
print(f"ok: {len(have)} surface files are the same with pages on and off "
      f"(item documents apart from \"page\", feed.xml from its entries' <link>s)")
PY
