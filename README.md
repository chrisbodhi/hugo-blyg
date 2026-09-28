# hugo-blyg

A Hugo Module that builds the static publish-side surfaces of the
[Blygger protocol](https://github.com/blygger/blygger-spec) v0.2, Level 1
— `blyg.json` (manifest), `feed.xml`, `items/index.json` (archive index),
`items/{id}.json` (canonical item documents), and, optionally,
`blogroll.opml` (§11) — plus human-readable HTML pages for the feed,
each item, and each pinned version (§4, §8.4), from a section of
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

Hugo Modules merge an imported module's `content/`, `layouts/`, `data/`,
`static/`, `archetypes/`, and `i18n/` into the site's own filesystem
automatically (this module's `content/` holds only the content adapter
that adds the blyg pages).
They do **not** merge a module's own `hugo.toml`/`config.toml` — site-wide
configuration (`[outputFormats]`, `[mediaTypes]`, `[outputs]`, `[params]`)
has to be declared by whatever site imports this module (verified
empirically against Hugo 0.151.0). So this module owns all the actual
document-shaping logic — `layouts/partials/blyg/item.html` builds one item
document; `layouts/blyg/section.blygmanifest.json` and
`layouts/blyg/section.blygfeed.xml` build the four surfaces — and the site
supplies five things.

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
outputs = ["html", "blygmanifest", "blygfeed"]   # "html": the feed page

[[cascade]]
  [cascade.target]
    kind = "page"
  [cascade.build]
    render = "never"   # hugo-blyg adds id-based item pages itself
    list = "always"    # but still enumerable via .Pages
+++
```

`outputs` is set here, per-page, rather than in the site's global
`[outputs]` table — `[outputs] section = [...]` applies to *every*
section (e.g. a site's `/blog/`, too), and this module's output formats
have no business running there. The cascade is scoped to `kind = "page"`
so the section page itself, which is where the surfaces are built, still
renders. Items' own Hugo pages never render: their URLs would come from
file names, and an item's permalink comes from its id, so the module
adds those pages itself (see "HTML pages").

**4. A `<link rel="blyg">`** in every page's `<head>` (§12.1 step 4),
pointing at the section, so resolving a page never depends on probing
`/blyg/`:

```gotemplate
<link rel="blyg" href="{{ "blyg/" | absURL }}">
```

**5. A `baseof.html` with a `main` block**, which almost every Hugo
theme already has. The blyg pages are ordinary pages of the site: the
module's layouts only define `main`, so they render inside the site's
own head, header, navigation and styles. Two optional touches in its
`<head>` (see "HTML pages" for what they do):

```gotemplate
<link rel="canonical" href="{{ .Params.blyg_canonical | default .Permalink }}">
{{ partial "blyg/head.html" . }}
```

**CORS.** Protocol v0.2 §4 (SHOULD): public JSON/XML responses SHOULD
carry permissive CORS (`Access-Control-Allow-Origin: *`), since
cross-origin reading by other clients depends on it. On Apache, this
ships with the module as `static/blyg/.htaccess` — Hugo mounts an
imported module's `static/` into the site by default, so nothing more
is needed, and a site's own `static/blyg/.htaccess` still wins if it
wants a different policy. Every other host needs the equivalent added
on the site's own side, since `.htaccess` does nothing off Apache:

- **Netlify / Cloudflare Pages** — a `_headers` file:
  ```
  /blyg/*.json
    Access-Control-Allow-Origin: *
  /blyg/*.xml
    Access-Control-Allow-Origin: *
  ```
- **nginx** — in the `location` block serving `/blyg/`:
  ```nginx
  location /blyg/ {
    location ~ \.(json|xml)$ {
      add_header Access-Control-Allow-Origin "*";
    }
  }
  ```
- **S3 / CloudFront** — a CORS rule on the bucket (`AllowedOrigins: *`,
  `AllowedMethods: GET`) or, for CloudFront, a response-headers policy
  attached to the distribution/behavior serving `/blyg/*`.

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
  array (see "Transclusion"); fragments omit the key entirely
  (§10.3). Any other value, including `"withdrawn"`, fails the build:
  `"withdrawn"` is a wire-level state this module derives from the
  ledger, never something a page authors directly.
- The SHA-256 of `content_md` — the body, minus any `blyg-gen` markers
  (see "Generation provenance") — must equal the ledger's `last_hash`,
  or the build fails. Shipping an unstamped edit would be a
  same-version stealth edit (§13.3).
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
`{path, created, version, kind, last_hash, withdrawn, changelog,
transclusions?, generated?}`, where each changelog entry is `{version, at,
note, kind, pinned?}`, `transclusions` (threads with directives only) holds the
snapshots baked into the thread's latest version — see "Transclusion" —
and `generated` (versions with generated spans only) holds their
provenance — see "Generation provenance". Produced
and maintained by `scripts/blyg_stamp.py` — the templates
only read it, via `site.Data.blyg.ledger` (Hugo's standard
`data/<path>` → `site.Data.<path>` mapping). `content_hash` in the item
document is the ledger's `last_hash`; the build re-hashes `content_md`
(`.RawContent` minus any `blyg-gen` markers) and fails on any mismatch
rather than trusting the two to agree.

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
- `scripts/blyg_stamp.py pin <id>` pins an item's live version (§8):
  build first, then pin, then commit the new
  `static/blyg/items/{id}/v{n}.json` along with the ledger and rebuild
  to publish its page. A pin is irrevocable.
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

## Transclusion

A thread quotes one of the site's own fragments with a line consisting
solely of its id in a directive (§10.1):

```md
Something worth quoting:

![[5cx94j6wbmzrdnnjxvs9j1nkba]]
```

The same text inline, or inside a fenced code block, is inert. The
whitespace around a directive must be spaces and tabs: anything else
(a non-breaking space, say) is refused at stamp time, since Markdown
wouldn't read the line as a block of its own. Only
threads transclude, only same-origin fragments can be transcluded (0.2 is
local-only), and the reserved `![[id@vN]]` form is refused.

**Resolution happens in `blyg_stamp.py`, at publish time, never in the
build.** Hugo rebuilds everything on every deploy, so a template that
resolved `![[id]]` against the fragment's current page would silently
rewrite every thread quoting it whenever the fragment changed — a
same-version stealth edit (§13.3), and exactly the cascade §10.4 forbids.
Instead, whenever the stamp script creates a thread version, it resolves
each directive to the target's latest published version (after this same
run, so editing a fragment and the thread quoting it together bakes the
new version) and stores the snapshot in the thread's ledger entry:

```json
"transclusions": [
  {"id": "5cx94j6wbmzrdnnjxvs9j1nkba", "version": 1, "line": 5,
   "content_hash": "sha256:…", "content_md": "…the fragment's body at v1…"}
]
```

A directive that doesn't resolve to a currently-published fragment
(unknown id, draft, withdrawn, a thread) is a publish error. An
unchanged thread is never re-resolved: later edits, withdrawal or
pinning of the fragment leave it alone until the thread itself is
republished, which re-resolves every directive to the then-latest
versions. A withdrawn thread's snapshots are dropped (§9), and
`--amend` reverting a thread restores exactly the snapshots that shipped.
The one exception to "never re-resolved": if `--amend` undoes or rewrites
an unshipped fragment version that a thread baked, the thread's own
unshipped version is amended in the same run to re-resolve, since its
provenance would otherwise name a version that no longer exists, or
exists with other content (§10.3). `blyg_validate.py` backs that up by
checking every baked `{id, version}` against the fragment's changelog.

`item.html` bakes each snapshot where its directive line was, as
`<blockquote class="blyg-transclusion" data-blyg-id="…"
data-blyg-version="…">…</blockquote>` with no link inside, rendering the
stored `content_md` in the **fragment's own page context**. That is the
same rendering that produced the fragment's `content_html` (verified
byte-identical against Hugo 0.151.0, shortcodes and footnotes included),
and `blyg_validate.py` checks it byte for byte wherever that version's
HTML is still on the wire: the fragment's live document while it sits at
the baked version, or a pin. The item document's `transclusions` is the
`{id, version}` provenance, in directive order.

Storing the Markdown and rendering it, rather than capturing rendered
HTML, keeps stamping a single pass that needs no build. The trade-off
is the one every item's `content_html` already carries: a Hugo upgrade,
a markup-config change, or a changed shortcode can change the rendered
bytes without a version bump. The *content* baked into a thread can't
change, though, since `content_md` is fixed in the ledger and hash-checked
by both the stamp script and the build.

A directive line becomes a block of its own, so it can sit inside a list
item (indented to the item's content). One that Markdown would read as
part of an indented code block or a raw-HTML block fails the build
rather than bake somewhere unexpected; fence it instead.

## Blogroll

The blogroll (§11) is optional, and a publishing act: it lists the
feeds the site chooses to show, never everything it reads. Write them
into `data/blyg/blogroll.json` (or `.toml`/`.yaml`):

```json
{
  "title": "Someone's blogroll",
  "feeds": [
    { "text": "Interconnected",
      "xmlUrl": "https://interconnected.org/home/feed",
      "htmlUrl": "https://interconnected.org/home/" }
  ]
}
```

`title` is optional (it defaults to "*site title* blogroll"). Each entry
needs exactly `text` (the display title), `xmlUrl` (the feed), and
`htmlUrl` (the blyg's origin, or the site of a plain feed), with both
URLs absolute. Any other key fails the build: the file is plain OPML 2.0
with no extensions, so there is nowhere for it to go, and a misspelled
`xmlurl` would otherwise drop a required attribute without a word.
There's no need to mark a feed as a blyg; a reader resolving its
`xmlUrl` finds the feed's `<blyg:manifest>` itself.

With at least one entry, the build publishes `blogroll.opml` and adds
`"blogroll": "blogroll.opml"` to `blyg.json`. With none, or no file,
there is neither (§11: a blyg with nothing to show serves no blogroll).
`partial "blyg/head.html"` puts the `<link rel="blogroll">` §11 asks
for on the feed page (see "HTML pages").

## HTML pages

§4 says publishers SHOULD serve human-readable HTML next to the JSON.
The module adds it as ordinary pages of the site, rendered inside the
site's own `baseof.html` — so they look like the rest of the site — and
its layouts supply only the `main` block:

| Path under the origin | Page | Added by |
|---|---|---|
| `index.html` | the feed page: the 50 most recently updated live items, newest first | the section's `"html"` output (`layouts/blyg/section.html`) |
| `f/{id}/`, `t/{id}/` | an item's live permalink: a fragment's under `f/`, a thread's under `t/` (§8.4's paths) | the content adapter (`content/blyg/_content.gotmpl`) |
| `f/{id}/v{n}/`, `t/{id}/v{n}/` | a pinned version (§8.4), only while version *n* is pinned | the content adapter |

The content adapter is a Hugo content adapter the module mounts into the
site's `content/blyg/`. It adds one page per ledger entry and one per
pin, built but never listed, so they stay out of `.Pages`, the site's
own lists and feeds, and every blyg surface (§8.4 rule 4). Each item
page's `<title>` is the first line of the item's text, since items have
no title of their own. Each `feed.xml` entry's `<link>` is its item's
live page.

An item page shows the live content and a version line citing only the
live version and the pins, like "v6 · pinned: v2, v4": §8.4 forbids any
page offering or implying access to unpinned history, so there is no
version list. A withdrawn item's page stays up, says it was withdrawn,
and still cites its pins. The permalink follows the item's authored
kind, so an item whose kind changes moves between `f/` and `t/`, and the
old address becomes an alias that redirects to it.

A pinned page is built from the pin file, never re-rendered: its
content is that version's publish-time `content_html` (§8.4 rule 2),
marked as a frozen snapshot, and it links the live page and its
`v{n}.json` twin (rule 3). Built with `hugo --minify`, the page is
minified like every other page on the site, which changes the bytes'
spelling (quotes, whitespace) but not one element, attribute or word
of the content; `blyg_validate.py` compares it that way, and the JSON
twin remains the byte-exact citation. This is also where the templates
read pins back: a pin the ledger records whose
`static/blyg/items/{id}/v{n}.json` is missing, or names another id or
version, fails the build, since a pin MUST return 200 forever (§8). The
module reads pins from the site root's `static/`, which is where
`blyg_stamp.py pin` writes them by default.

**The `<head>`** is the site's. Two things there are worth adding (setup
step 5):

- `partial "blyg/head.html"` emits nothing on the site's other pages.
  On blyg pages it adds the feed's `rel="alternate"` (RSS
  autodiscovery, §12 step 6) and the item's or pin's JSON twin, plus, on
  the feed page, `<link rel="blogroll">` (§11) when a blogroll is served.
- A pinned page SHOULD be `rel="canonical"` to the live page (§8.4
  rule 3). It carries that URL as `.Params.blyg_canonical`, so a head
  that emits a canonical link should prefer it, as in step 5.
  `blyg_validate.py` checks a canonical link wherever there is one, and
  doesn't require one.

**Styling** is the site's too. The content (`partials/blyg/html/main.html`)
comes unstyled, with classes to hook: `blyg-feed`, `blyg-item`,
`blyg-meta`, `blyg-frozen` (the pinned-snapshot banner), and the wire
tokens `blyg-transclusion` and `blyg-tk-gen`. Override `main.html` in the
site's own `layouts/partials/blyg/html/` to change what the pages say.
Keep what §8.4 requires of a pinned page: the pin's `content_html`
untouched, the frozen marking, and the link to its JSON twin.

To turn the item pages off, set `pages = false` under `[params.blyg]`
(and drop `"html"` from the section's `outputs` if the feed page should
go too). Only do that on a site that has never served them with a pin:
a pinned page, once served, MUST keep returning 200 (§8.4 rule 1).

## Generation provenance

A version containing machine-generated prose SHOULD say so (§5.7): a
`generated` array on the item document, one entry per generated span,
and each span wrapped in `content_html` as `blyg-tk-gen`. The protocol
also requires that `content_md` carry no markup for it at all, so the
spans are marked in the source with this module's shortcode, whose
markers never reach the wire:

```md
Written by hand, with {{< blyg-gen model="some-model" >}}a generated phrase{{< /blyg-gen >}} inline.

{{< blyg-gen sources="5cx94j6wbmzrdnnjxvs9j1nkba, 7c9wk2mhq0v3xj8tn5rzfd41bg@v2" model="some-model" at="2026-09-04T11:30:00Z" >}}
A generated paragraph.
{{< /blyg-gen >}}
```

- **Inline** spans open and close on one line and render as
  `<span class="blyg-tk-gen">`. **Block** spans put each marker alone on
  its own line, with a blank line before the opening marker and after the
  closing one, and render as `<div class="blyg-tk-gen">`. The blank lines
  matter: `content_md` is the body with the marker lines removed, and
  without them the span would read as part of its neighbours' paragraphs.
- **Parameters**, all optional and all double-quoted: `sources` (a
  comma-separated list of this origin's item ids, each optionally
  `@vN`), `model`, and `at` (ISO 8601 UTC). Nothing else is accepted,
  since the array MUST NOT carry instructions or other authoring state
  (§5.7 rule 2).
- **Sources are pinned at publish time.** A bare id means the source's
  latest version published *before* this stamp run; content generated
  from a draft wasn't drawn from anything published. `id@vN` names an
  exact published version, and is required for an item's own earlier
  versions and for a source that is withdrawn now. As with transclusion,
  an unchanged item keeps its recorded sources whatever they do later;
  republishing it re-resolves bare ids.
- **Provenance is part of the version.** Changing only a parameter
  changes the item document but not `content_md`, so the stamp script
  treats it as a new version, and the build fails if the body's markers
  disagree with the ledger.
- Spans don't nest, can't be empty, and can't contain a transclusion
  directive: quotation is transclusion's alone (§5.7 rule 3). A thread
  quoting a fragment that has generated spans bakes them along with the
  quote, but lists nothing in its own `generated`; that provenance is
  the fragment version's.
- A withdrawal endcap carries no `generated`, and a pin carries its
  version's (§5.7 rules 4 and 5).

The markers count everywhere Hugo expands shortcodes, code fences
included. To write one literally, use Hugo's escaped form,
`{{</* blyg-gen */>}}`.

## Not yet built

Nothing on the 0.2 publish side. L2's constructs (stub metadata, thread
nesting, `forked_from`, webmention) arrive with protocol 0.3.

## Developing hugo-blyg

```sh
python3 -m unittest discover -s tests          # script tests

cd exampleSite                                 # the end-to-end gates
python3 ../scripts/blyg_stamp.py --check
hugo --minify
python3 ../scripts/blyg_validate.py
../tests/transclusion_e2e.sh                   # §10.4, on a scratch copy
../tests/pages_e2e.sh                          # §8.4, likewise
```

`exampleSite/go.mod` replaces the module with this checkout, so it always
builds what's in front of you. CI runs all of these, and also checks that
an unstamped body edit fails `hugo`. `transclusion_e2e.sh` edits and then
withdraws the fragment that `exampleSite`'s transcluding thread quotes,
rebuilding each time, and fails unless the thread's built `content_html`
stays byte-identical until the thread is itself re-stamped.
`pages_e2e.sh` edits, re-kinds and withdraws `exampleSite`'s fragment,
whose v1 is pinned, and fails unless the pinned page's content stays the
pin's `content_html` throughout, the live page moves with the kind, and
deleting the pin file fails the build.

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
