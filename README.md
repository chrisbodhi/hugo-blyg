# hugo-blyg

A Hugo Module that builds the static publish-side surfaces of the
[Blygger protocol](https://github.com/blygger/blygger-spec) v0.2, Level 1
— `blyg.json` (manifest), `feed.xml`, `items/index.json` (archive index),
and `items/{id}.json` (canonical item documents) — from a section of
ordinary Hugo Markdown content plus a small JSON ledger.

It comes in three parts:

- **Templates** (`layouts/`), imported as a Hugo Module, that shape every
  document and fail the build when content and ledger disagree.
- **Scripts** (`scripts/`, Python 3.11+, standard library only) that
  assign ids and keep the ledger (`blyg_stamp.py`), and check the built
  output against the spec (`blyg_validate.py`).
- **A GitHub Action** (`publish-from-issue/`) that turns an issue into a
  stamped fragment and opens a PR for it.

[`docs/conformance.md`](docs/conformance.md) is the spec checklist all
three are built and reviewed against. [`exampleSite/`](exampleSite) is the
smallest site that uses it, and what CI builds.

Requirements: Hugo (verified against 0.151.0; other versions are
untested), Go (Hugo Modules use it to fetch modules), and Python 3.11+
for the scripts.

## Setup

Hugo Modules merge an imported module's `layouts/`, `data/`, `static/`,
`archetypes/`, and `i18n/` into the site's own filesystem automatically.
They do **not** merge a module's own `hugo.toml`/`config.toml` — site-wide
configuration (`[outputFormats]`, `[mediaTypes]`, `[outputs]`, `[params]`)
has to be declared by whatever site imports this module (verified
empirically against Hugo 0.151.0). So this module owns all the actual
document-shaping logic — `layouts/partials/blyg/item.html` builds one item
document; `layouts/blyg/section.blygmanifest.json` and
`layouts/blyg/section.blygfeed.xml` build the four surfaces — and the site
supplies four things.

**1. The module.** If the site isn't a Hugo Module yet, `hugo mod init
<your-site-module-path>` first. Then:

```sh
hugo mod get github.com/chrisbodhi/hugo-blyg@v0.1.0
```

**2. Config**, in `hugo.toml` (or `config.toml`):

```toml
[module]
  [[module.imports]]
    path = "github.com/chrisbodhi/hugo-blyg"

[outputFormats]
  [outputFormats.blygmanifest]
    mediaType = "application/json"
    isPlainText = true          # load-bearing -- see below
    baseName = "blyg"
  [outputFormats.blygfeed]
    mediaType = "application/rss+xml"
    baseName = "feed"

[params.blyg]
  level = 1   # optional; see "Conformance level"
```

**3. A content section** (e.g. `content/blyg/_index.md`) whose own front
matter opts into these output formats and defines the render/list
behavior for its items:

```toml
+++
title = "blyg"
outputs = ["blygmanifest", "blygfeed"]

[[cascade]]
  [cascade.target]
    kind = "page"
  [cascade.build]
    render = "never"   # no live HTML permalink pages yet
    list = "always"    # but still enumerable via .Pages
+++
```

`outputs` is set here, per-page, rather than in the site's global
`[outputs]` table — `[outputs] section = [...]` applies to *every*
section (e.g. a site's `/blog/`, too), and this module's output formats
have no business running there. The cascade is scoped to `kind = "page"`
so the section page itself, which is where the surfaces are built, still
renders.

**4. A `<link rel="blyg">`** in every page's `<head>` (§12.1 step 4),
pointing at the section, so resolving a page never depends on probing
`/blyg/`:

```gotemplate
<link rel="blyg" href="{{ "blyg/" | absURL }}">
```

Then write an item (see "Content contract"), stamp it, build, and
validate — see "Scripts" for running them from a site.

### Why the output formats look like that

**`blygmanifest`'s `isPlainText = true` is not optional.** Without it,
Hugo runs the literal template text through `html/template`'s HTML
escaper — which corrupts a leading `<?xml ...?>` prolog and any
`<![CDATA[` marker even when the template contains no template actions
at all. It matches Hugo's own built-in JSON output format
(`output.JSONFormat` in the Hugo source), which sets the same flag for
the same reason.

**`blygfeed` deliberately does not set it.** The escaping bug is real,
but Hugo's own embedded `rss.xml` template (`tpl/tplimpl/embedded/
templates/rss.xml` in the Hugo source) shows a narrower fix: stay on
`html/template` (the default), and mark just the two spots that would
otherwise get mangled as pre-escaped, rather than opting the whole
document out of autoescaping:

```gotemplate
{{ printf "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" | safeHTML }}
...
<description>{{ .description | transform.XMLEscape | safeHTML }}</description>
```

`section.blygfeed.xml` follows that idiom. The payoff: every other
interpolated value (titles, ids, the site's own base URL) gets Hugo's
ordinary contextual autoescaping automatically — one less place a future
edit has to remember to escape by hand, and it's what a maintainer
who's read Hugo's own RSS template will already expect to see.

## Content contract

- One Markdown page per item, anywhere under the section that sets
  `outputs`/`cascade` as above.
- `blyg_id` in front matter, once assigned (a one-time process external
  to Hugo — see `scripts/blyg_stamp.py` below).
  A page without `blyg_id` is skipped from every blyg surface — lets a
  post exist in the section before it's been stamped. A page whose
  `blyg_id` has no ledger entry, or a ledger entry with no built page,
  fails the build: either would silently turn a published
  `items/{id}.json` into a 404, and §4 says it MUST stay 200 forever.
- `blyg_withdrawn = true` marks it withdrawn; the ledger, not this
  module, decides whether that's a fresh transition (§9 endcap) — this
  module only ever reads the ledger's `withdrawn` flag, it never writes
  it.
- `blyg_kind = "fragment"` or `"thread"` (defaults to `"thread"` when
  absent). The item document's `kind` comes from the **ledger**, and the
  build fails if the page disagrees: a kind change changes the document,
  so it is a publish event that needs a version bump (§5.2), which only
  the stamp script can record. Threads always carry a `transclusions`
  array (currently always `[]`); fragments omit the key entirely
  (§10.3). Any other value, including `"withdrawn"`, fails the build:
  `"withdrawn"` is a wire-level state this module derives from the
  ledger, never something a page authors directly.
- The body's SHA-256 must equal the ledger's `last_hash`, or the build
  fails. Shipping an unstamped edit would be a same-version stealth
  edit (§13.3).
- `draft = true`, a future `date`/`publishDate`, or an `expiryDate`
  would each make Hugo drop the page. Before an item is first stamped
  that just means "not published yet"; after, it would 404 a published
  item, so the stamp script refuses it and the build fails on the
  orphaned ledger entry. Withdrawal is the only exit (§9).
- `title` is never read. The item document schema (§5) has no title
  field at all — fragments and threads are microblog-style, not
  headlined essays. A feed entry's `<title>` is that publish event's
  changelog **note**, when it has one: §7 says an entry "keeps its own
  `blyg:version` and note", and the spec's own example carries the note
  in `<title>`. The withdrawal event of a currently-withdrawn item is
  titled `withdrawn`, literally, as §7 requires by name.

## Conformance level

`blyg.json`'s `"level"` field (§3) comes from the consuming site's own
`[params.blyg].level`, defaulting to `1` when unset. This module only
accepts `1` — it fails the build on anything else. L2's real constructs
(stub metadata, thread nesting, `forked_from`, webmention) arrive with
protocol 0.3 and none are built, so a higher level would be a false
conformance claim. Note the blogroll is optional at *every* level and
never changes the level, and generation provenance (§5.7) is L1 — it
only applies to versions that involved generation.

## The ledger

`data/blyg/ledger.json`, keyed by id, one entry per item:
`{path, created, version, kind, last_hash, withdrawn, changelog}`, where
each changelog entry is `{version, at, note, kind, pinned?}`. Produced
and maintained by `scripts/blyg_stamp.py` — the templates
only read it, via `site.Data.blyg.ledger` (Hugo's standard
`data/<path>` → `site.Data.<path>` mapping). `content_hash` in the item
document is the ledger's `last_hash`; the build re-hashes `.RawContent`
and fails on any mismatch rather than trusting the two to agree.

The per-changelog `kind` is ledger-private: it lets `feed.xml` label
every past publish event with the kind that version really had (an
endcap an item has since returned from still reads `withdrawn`). The
item document's `changelog` is rebuilt from the §5.2 members only
(`version`, `at`, `note`, `pinned`).

`data/blyg/media.json` maps every file under `static/blyg/media/` to its
SHA-256. The stamp script refuses any change to or deletion of a tracked
file, because a published media URL MUST always serve the same bytes (§5.4).

## Scripts

Python 3.11+, standard library only. Hugo ignores `scripts/`, `tests/`,
`docs/`, `exampleSite/` and `publish-from-issue/` — they aren't module
mounts — so they ride along with the module without touching the build.
Run the scripts from the consuming site's root: every default path
(`content/blyg/`, `data/blyg/`, `public/`, `static/`) is relative to the
working directory.

- `scripts/blyg_stamp.py` assigns ids and maintains the ledger. Run it
  after adding or editing an item; `--check` fails if it would change
  anything, which is the gate a site's CI runs before `hugo`. `--amend`
  compares against the ledger on the branch the site deploys from,
  `--published-ref` (default `origin/master`; pass `origin/main` if that's
  yours).
- `scripts/blyg_validate.py` checks the built `public/blyg/` against the
  ledger and the spec; a site's CI runs it after `hugo`.
- `scripts/blyg_from_issue.py` backs the action below.

The scripts ship inside the module, so run the copy at the exact version
the site's `go.mod` pins — the one its templates came from — rather than
a separate checkout that can drift. Go knows where that is (and follows a
local `replace`, too):

```sh
go mod download github.com/chrisbodhi/hugo-blyg
BLYG=$(go list -m -f '{{.Dir}}' github.com/chrisbodhi/hugo-blyg)
python3 "$BLYG/scripts/blyg_stamp.py"
```

In a site's CI, all three gates:

```yaml
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Locate hugo-blyg
        run: |
          go mod download github.com/chrisbodhi/hugo-blyg
          echo "BLYG=$(go list -m -f '{{.Dir}}' github.com/chrisbodhi/hugo-blyg)" >> "$GITHUB_ENV"
      - name: Check blyg content is stamped
        run: python3 "$BLYG/scripts/blyg_stamp.py" --check
      - name: Generate site
        run: hugo --minify
      - name: Validate blyg surface
        run: python3 "$BLYG/scripts/blyg_validate.py"
```

GitHub's `ubuntu-latest` runners come with Go installed.

## The publish-from-issue action

`publish-from-issue/action.yml` is a composite GitHub Action that turns
an issue into a new fragment: the issue body becomes the item body
(line endings normalized to LF before hashing), and the title only names
the file (`content/blyg/YYYY-MM-DD-<slug>.md`) and the PR, since items
have no title field (§5). It stamps the item, pushes `blyg/issue-N`, and
opens a PR against the default branch that closes the issue on merge.
Deploying stays the site's job, on that merge.

An issue is marked for blyg by the label, or — because not every client
can label an issue as it's filed (GitHub Mobile can't) — by opening it
with a title that starts with `title-prefix` (`blyg:` by default,
case-insensitive). The prefix is dropped from the title before it names
the file and the PR.

It publishes only when the issue is open, is marked for blyg, and was
filed by the repository owner or a login listed in `authors`. A second
event for the same issue finds the `blyg/issue-N` branch and stops; on
any failure it comments on the issue and leaves it open.

```yaml
on:
  issues:
    types: [opened, labeled]
permissions:
  contents: write
  pull-requests: write
  issues: write
concurrency:
  group: blyg-issue-${{ github.event.issue.number }}
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: chrisbodhi/hugo-blyg/publish-from-issue@v0.1.0
        with:
          authors: someone, someone-else   # optional; the owner is always allowed
          label: blyg                      # optional
          title-prefix: "blyg:"            # optional; "" for label only
          base-branch: main                # optional; defaults to the default branch
```

Pin the same tag as the site's `go.mod`: the action stamps with its own
copy of `blyg_stamp.py`, and the build checks that ledger with the
templates from `go.mod`'s version.

The repository must also allow Actions to open PRs (Settings → Actions →
General → Workflow permissions → "Allow GitHub Actions to create and
approve pull requests").

## The `resources.FromString` fan-out gotcha

`items/index.json` and every `items/{id}.json` are one-file-per-item, not
one-per-section, so they can't use Hugo's native "one output file per
(page, output format)" mechanism the way `blyg.json`/`feed.xml` do.
Instead they're published via
`{{ $r := resources.FromString "blyg/items/x.json" $content }}` — but
**that only actually writes the file once the resource is accessed**
(e.g. `$r.RelPermalink`, `$r.Content`). Creating the resource and never
touching it publishes nothing, silently. Every call site in this module
does `{{ $_ := $r.RelPermalink }}` immediately after creating one, purely
to force the write, discarding the value.

## Absolute URLs in `content_html`

The protocol requires `content_html` to be self-contained (§7). Render
hooks on the markdown image/link AST aren't enough on their own if the
consuming site has shortcodes that inject raw HTML with root-relative
URLs outside those AST nodes entirely (a real case in the site this was
built for: an `<img>` whose `src` went through `relURL`, and an injected
`<script src="/js/…">`). `item.html` instead rewrites
the *fully rendered* HTML string directly — in essence
`(src|href|…)=(["'])/([^/"'])` → `${1}=${2}{base}/${3}`, using a captured
"not another slash" character instead of a negative lookahead, because
Hugo's regex engine (RE2) has none. The attribute list also covers
`poster`, `cite`, `action` and friends, and two more passes handle every
candidate inside `srcset` and `url(...)` in inline styles. This catches
shortcode-injected markup and ordinary markdown images/links in one pass, so no render hooks are
needed. Page-relative URLs (`other/page`) can't be rewritten — an item
has no page of its own to be relative to — so
`scripts/blyg_validate.py` fails the build on them, and on anything
else the rewrite can't make self-contained.

## Media

`media` (§5.4) lists every object the rendered HTML embeds (`img`,
`source`, `video`, `audio`: `src`, `poster`, `srcset`) from the origin's
own `media/` directory, with a MIME type from the file extension and the
`alt` text when there is one. Embedding anything else from the origin
(e.g. `/img/…`) fails validation: only `media/` files are held
immutable, and §5.4 requires that of a media URL.

## Not yet built

Transclusion resolution (§10.2): the stamp script refuses any
`![[id]]` directive line until it's built, because every directive MUST
resolve. Also not built: pinned per-version JSON files served from
`static/blyg/items/{id}/v{n}.json` (`scripts/blyg_stamp.py pin <id>`
writes them; the templates don't read them back), the optional blogroll (§11), generation provenance (§5.7, only
needed once a version involves generation), and any live HTML permalink
page for an item (§8.4).

## Developing hugo-blyg

```sh
python3 -m unittest discover -s tests          # script tests

cd exampleSite                                 # the end-to-end gates
python3 ../scripts/blyg_stamp.py --check
hugo --minify
python3 ../scripts/blyg_validate.py
```

`exampleSite/go.mod` replaces the module with this checkout, so it always
builds what's in front of you. CI runs both, and also checks that an
unstamped body edit fails `hugo`.

To try a change against a real site before tagging it, point that site's
`go.mod` at a local checkout — the same `go list` recipe under "Scripts"
then finds the local scripts too — and drop the line before committing:

```
replace github.com/chrisbodhi/hugo-blyg => ../hugo-blyg
```

Releases are semver tags (`v0.1.0`, …); a site picks one up with
`hugo mod get github.com/chrisbodhi/hugo-blyg@<tag>`, and bumps the
publish-from-issue `uses:` ref to match.

## License

None chosen yet: the code is public to read, but no license has been
granted to reuse it.
