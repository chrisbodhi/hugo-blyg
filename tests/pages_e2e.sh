#!/usr/bin/env bash
# End-to-end proof of §8.4 against real Hugo builds of a scratch copy of
# exampleSite/, whose fragment has v1 pinned: the pinned page's content
# stays byte-identical to the pin through an edit, a kind change and a
# withdrawal (its chrome -- the links to the live page -- may follow the
# item, §8.4 rule 2); the live page moves with the kind (leaving a
# redirect) and cites only the pin; and a recorded pin whose file is gone
# fails the build.
#
# Run from anywhere (GNU sed); needs hugo, go and python3 on PATH. CI runs it.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$repo/exampleSite" "$work/site"
rm -rf "$work/site/public" "$work/site/resources"
cd "$work/site"
# exampleSite replaces the module with ../ -- point that at this checkout.
sed -i.bak "s#=> \.\./#=> $repo/#" go.mod && rm go.mod.bak

id=5cx94j6wbmzrdnnjxvs9j1nkba
md=content/blyg/2026-09-01-first-fragment.md
pinned=public/blyg/f/$id/v1/index.html

stamp() { python3 "$repo/scripts/blyg_stamp.py" "$@" >/dev/null; }
build() {
  rm -rf public
  hugo --minify --quiet
  python3 "$repo/scripts/blyg_validate.py" >/dev/null
}
frozen() {  # the pinned page still carries exactly the pin's content_html
  # (as markup: the page is minified with the rest of the site, the JSON
  # twin is the byte-exact citation -- see blyg_validate.carries)
  python3 - "$repo/scripts" "$pinned" "static/blyg/items/$id/v1.json" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import blyg_validate
page = open(sys.argv[2], encoding="utf-8").read()
pin = json.load(open(sys.argv[3], encoding="utf-8"))["content_html"]
sys.exit(0 if blyg_validate.carries(page, pin) else 1)
PY
}
versions() {  # the version line of a live page, tags stripped
  grep -o '<p class="\?blyg-meta"\?>v[^<]*\(<a [^>]*>[^<]*</a>\)*' "$1" | sed 's/<[^>]*>//g'
}
fail() { echo "FAIL: $*" >&2; exit 1; }

build
[ -f "$pinned" ] || fail "no page for the pinned v1"
frozen || fail "the pinned page's content isn't the pin's content_html"
[ "$(versions public/blyg/f/$id/index.html)" = "v1 · pinned: v1" ] \
  || fail "live page doesn't cite its pin"

# 1. Edit: v2 goes live, the v1 page doesn't move.
sed -i.bak 's/^A fragment with/An edited fragment with/' "$md" && rm "$md.bak"
stamp
build
frozen || fail "an edit changed the pinned page (§8.4 rule 2)"
[ "$(versions public/blyg/f/$id/index.html)" = "v2 · pinned: v1" ] \
  || fail "live page doesn't show v2 and cite only the pin"
[ ! -e "public/blyg/f/$id/v2" ] || fail "an unpinned version got a page (§8.4 rule 1)"
echo "ok: an edit leaves the pinned content unchanged"

# 2. Kind change: the live page moves to t/, f/ redirects, the pin stays put.
sed -i.bak 's/^blyg_kind = "fragment"$/blyg_kind = "thread"/' "$md" && rm "$md.bak"
stamp
build
frozen || fail "a kind change changed the pinned page"
[ "$(versions public/blyg/t/$id/index.html)" = "v3 · pinned: v1" ] \
  || fail "live page didn't move to t/"
grep -q "url=https://example.org/blyg/t/$id/" "public/blyg/f/$id/index.html" \
  || fail "the old f/ permalink doesn't redirect to t/"
grep -q "rel=\"\\?canonical\"\\? href=\"\\?https://example.org/blyg/t/$id/" "$pinned" \
  || fail "the pinned page isn't canonical to the moved live page"
echo "ok: a kind change moves the live page and leaves a redirect"

# 3. Withdrawal: the live page says so, the pinned page survives (§8.4 rule 1).
sed -i.bak 's/^blyg_kind = "thread"$/blyg_kind = "thread"\nblyg_withdrawn = true/' "$md" \
  && rm "$md.bak"
stamp
build
frozen || fail "withdrawal changed the pinned page"
grep -q "This item was withdrawn" "public/blyg/t/$id/index.html" \
  || fail "the live page doesn't say the item was withdrawn"
echo "ok: a withdrawn item keeps its pinned page"

# 4. A recorded pin whose file is gone must fail the build, not 404.
rm "static/blyg/items/$id/v1.json"
rm -rf public
if hugo --minify --quiet >/dev/null 2>&1; then
  fail "hugo built with a recorded pin's file missing"
fi
echo "ok: a missing pin file fails the build"
