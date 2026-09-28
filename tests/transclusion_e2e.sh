#!/usr/bin/env bash
# End-to-end proof of §10.4 (no cascade) against a real Hugo build of a
# scratch copy of exampleSite/: editing, re-stamping, and then withdrawing
# the transcluded fragment leaves the thread's built content_html
# byte-identical, until the thread itself is re-stamped -- which then
# re-resolves to the fragment's then-latest version (§10.2).
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

fragment=5cx94j6wbmzrdnnjxvs9j1nkba
thread=3tvmngj59053w661mhapmy60mb
fragment_md=content/blyg/2026-09-01-first-fragment.md
thread_md=content/blyg/2026-09-03-transcluding-thread.md

stamp() { python3 "$repo/scripts/blyg_stamp.py" "$@" >/dev/null; }
build() {
  rm -rf public
  hugo --minify --quiet
  python3 "$repo/scripts/blyg_validate.py" >/dev/null
}
field() {  # field <id> <python expression over the item document `d`>
  python3 -c "import json,sys; d=json.load(open('public/blyg/items/$1.json')); print($2)"
}
fail() { echo "FAIL: $*" >&2; exit 1; }

python3 "$repo/scripts/blyg_stamp.py" --check >/dev/null
build
baked=$(field $thread 'd["content_html"]')
[ "$(field $thread 'd["transclusions"]')" = "[{'id': '$fragment', 'version': 1}]" ] \
  || fail "thread doesn't start out baking $fragment v1"

# 1. Edit the fragment and re-stamp: it moves to v2, the thread doesn't move.
sed -i.bak 's/^A fragment with/An edited fragment with/' "$fragment_md" && rm "$fragment_md.bak"
stamp
build
[ "$(field $fragment 'd["version"]')" = 2 ] || fail "fragment edit didn't bump it to v2"
[ "$(field $thread 'd["version"]')" = 1 ] || fail "fragment edit bumped the thread"
[ "$(field $thread 'd["content_html"]')" = "$baked" ] \
  || fail "fragment edit changed the thread's baked content_html (§10.4)"
echo "ok: an edited, re-stamped fragment leaves the thread byte-identical"

# 2. Withdraw the fragment: still no cascade.
sed -i.bak 's/^blyg_kind = "fragment"$/blyg_kind = "fragment"\nblyg_withdrawn = true/' "$fragment_md" \
  && rm "$fragment_md.bak"
stamp
build
[ "$(field $fragment 'd["kind"]')" = withdrawn ] || fail "fragment wasn't withdrawn"
[ "$(field $thread 'd["content_html"]')" = "$baked" ] \
  || fail "withdrawing the fragment changed the thread (§10.4)"
echo "ok: a withdrawn fragment leaves the thread byte-identical"

# 3. Republishing the thread must re-resolve -- and a withdrawn target
#    can't resolve, so the stamp refuses it (§10.2).
printf '\nA later note.\n' >> "$thread_md"
if python3 "$repo/scripts/blyg_stamp.py" >/dev/null 2>&1; then
  fail "stamp republished a thread that transcludes a withdrawn fragment"
fi
echo "ok: republishing against a withdrawn fragment is refused"

# 4. Bring the fragment back (v4) and republish the thread: it re-resolves.
sed -i.bak '/^blyg_withdrawn = true$/d' "$fragment_md" && rm "$fragment_md.bak"
stamp
build
[ "$(field $fragment 'd["version"]')" = 4 ] || fail "fragment didn't return as v4"
[ "$(field $thread 'd["version"]')" = 2 ] || fail "thread wasn't republished as v2"
[ "$(field $thread 'd["transclusions"]')" = "[{'id': '$fragment', 'version': 4}]" ] \
  || fail "republished thread didn't re-resolve to $fragment v4"
field $thread 'd["content_html"]' | grep -q 'data-blyg-version="4"><p>An edited fragment' \
  || fail "republished thread doesn't bake the edited fragment"
echo "ok: republishing the thread re-resolves to the then-latest version"
