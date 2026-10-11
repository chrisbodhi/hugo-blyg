#!/usr/bin/env bash
# End-to-end proof of §10.4 (no cascade) against a real Hugo build of a
# scratch copy of exampleSite/: editing, re-stamping, and then withdrawing
# the transcluded fragment leaves the thread's built content_html
# byte-identical, until the thread itself is re-stamped -- which then
# re-resolves to the fragment's then-latest version (§10.2). Then a thread
# quoting that thread bakes it with its own quote nested inside, and the
# quoted thread can't quote back (§10.2's closure check).
#
# Run from anywhere, with GNU or BSD (macOS) sed; needs hugo, go and
# python3 on PATH. CI runs it.
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
sed -i.bak '/^blyg_kind = "fragment"$/a\
blyg_withdrawn = true
' "$fragment_md" && rm "$fragment_md.bak"
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

# 5. Footnotes: every URL in content_html is absolute (§5.2), so a footnote
#    anchor points at its item's page -- and one quoted with a fragment
#    keeps pointing at the fragment's page, since the bake is the
#    fragment's own content_html.
printf '\nFootnoted.[^a]\n\n[^a]: A fragment footnote.\n' >> "$fragment_md"
printf '\nFootnoted too.[^b]\n\n[^b]: A thread footnote.\n' >> "$thread_md"
stamp
build
fragment_page="https://example.org/blyg/f/$fragment/"
thread_page="https://example.org/blyg/t/$thread/"
field $fragment 'd["content_html"]' | grep -q "href=\"$fragment_page#fn:1\"" \
  || fail "the fragment's footnote anchor isn't absolute against its own page"
field $thread 'd["content_html"]' | grep -q "href=\"$fragment_page#fn:1\"" \
  || fail "the thread's bake doesn't keep the fragment's absolute footnote anchor"
field $thread 'd["content_html"]' | grep -q "href=\"$thread_page#fn:1\"" \
  || fail "the thread's own footnote anchor isn't absolute against its page"
if field $thread 'd["content_html"]' | grep -q "href=\"#"; then
  fail "the thread's content_html still carries a relative #anchor"
fi
echo "ok: footnote anchors are absolute, quoted ones against their source's page"

# 6. A thread quoting that thread bakes the thread's content_html, its own
#    quote of the fragment nested inside (§10.2); only the direct quote is
#    provenance (§10.3). blyg_validate.py (in build) checks the bake is
#    byte-identical to the quoted thread's own content_html.
outer_md=content/blyg/2026-09-06-quoting-thread.md
printf '+++\ndate = "2026-09-06T12:00:00Z"\nblyg_kind = "thread"\n+++\nQuoting a thread.\n\n![[%s]]\n' \
  "$thread" > "$outer_md"
stamp
build
outer=$(sed -n 's/^blyg_id = "\(.*\)"$/\1/p' "$outer_md")
thread_version=$(field $thread 'd["version"]')
[ "$(field $outer 'd["transclusions"]')" = "[{'id': '$thread', 'version': $thread_version}]" ] \
  || fail "the quoting thread doesn't name exactly the thread it quotes"
field $outer 'd["content_html"]' \
  | grep -q "data-blyg-id=\"$thread\" data-blyg-version=\"$thread_version\"><p>A thread that quotes" \
  || fail "the quoting thread doesn't bake the quoted thread"
[ "$(field $outer 'd["content_html"].count("data-blyg-id=\"'$fragment'\"")')" = 1 ] \
  || fail "the quoted thread's own quote isn't nested in the bake"
echo "ok: a thread quoting a thread bakes its quote nested"

# 7. The quoted thread can't quote back (§10.2's closure check).
cp "$thread_md" "$work/thread.md.before"
printf '\n![[%s]]\n' "$outer" >> "$thread_md"
if err=$(python3 "$repo/scripts/blyg_stamp.py" 2>&1 >/dev/null); then
  fail "stamp let a thread quote a thread that quotes it"
fi
grep -q "MUST NOT quote one that quotes it" <<<"$err" || fail "refused, but not by the closure check: $err"
echo "ok: a quote cycle is refused"
cp "$work/thread.md.before" "$thread_md"

# 8. A partial transclusion (§10.1) bakes the selection's plain text, one
#    <p> per line, with a text-quote selector; a [[id]] link renders as an
#    anchor to the target's page. Both resolve at stamp time. The quote is
#    soft-wrapped across two lines and joined, and its apostrophe is the
#    typographer's, as the fragment's text content reads.
partial_md=content/blyg/2026-09-07-partial-quote.md
printf '+++\ndate = "2026-09-07T12:00:00Z"\nblyg_kind = "thread"\n+++\nSee [[%s]], which says:\n\n![[%s]]\n> a root-relative link and an image from the\n> origin\xe2\x80\x99s media directory\n\nAnd `[[%s]]` in code is text.\n' \
  "$fragment" "$fragment" "$fragment" > "$partial_md"
stamp
build
partial=$(sed -n 's/^blyg_id = "\(.*\)"$/\1/p' "$partial_md")
selection='a root-relative link and an image from the origin’s media directory'
fragment_version=$(field $fragment 'd["version"]')
[ "$(field $partial "d['transclusions'] == [{'id': '$fragment', 'version': $fragment_version, 'selector': {'exact': '$selection'}}]")" = True ] \
  || fail "the partial transclusion's provenance doesn't carry its selector"
field $partial 'd["content_html"]' \
  | grep -qF "<blockquote class=\"blyg-transclusion blyg-partial\" data-blyg-id=\"$fragment\" data-blyg-version=\"$fragment_version\"><p>a root-relative link and an image from the origin’s media directory</p></blockquote>" \
  || fail "the partial transclusion doesn't bake the selection as plain text"
field $partial 'd["content_html"]' | grep -q "See <a href=\"$fragment_page\">An edited fragment with a root-relative link" \
  || fail "the [[id]] link doesn't render as an anchor to the fragment's page"
field $partial 'd["content_html"]' | grep -qF "<code>[[$fragment]]</code>" \
  || fail "a [[id]] in code didn't stay text"
echo "ok: a partial transclusion bakes its selection, and [[id]] links its target"

# 9. A selection the source doesn't contain fails the build (§10.2) -- here
#    the ASCII apostrophe the typographer turned into a curly one.
sed -i.bak 's/origin’s media/origin'"'"'s media/' "$partial_md" && rm "$partial_md.bak"
stamp
rm -rf public
if err=$(hugo --minify 2>&1); then
  fail "hugo built a partial transclusion the source doesn't contain"
fi
grep -q "isn't in its text" <<<"$err" || fail "the build failed, but not on the selection: $err"
echo "ok: a misquoted selection fails the build"
