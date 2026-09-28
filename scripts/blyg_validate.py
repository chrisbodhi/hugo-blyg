#!/usr/bin/env python3
"""Validate a built blyg surface (public/blyg/) against the ledger and
blygger-spec docs/protocol-v0.2.md (pinned at c5884b9).

This is the build-side half of docs/conformance.md: everything
scripts/blyg_stamp.py can't see because Hugo produces it -- the rendered
content_html above all -- plus a cross-check that every surface agrees
with data/blyg/ledger.json. CI runs it after `hugo --minify`, before
anything ships.

Usage:
    blyg_validate.py [--public-dir public] [--ledger-path data/blyg/ledger.json]

Exits 1 and lists every problem found; 0 when the surface conforms.
"""

from __future__ import annotations

import argparse
import datetime
import email.utils
import html.parser
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import blyg_stamp as bs  # noqa: E402

BLYG_NS = "https://blygger.org/ns/0.1"
ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
GUID_RE = re.compile(r"^blyg:(" + bs.BLYG_ID_RE_SRC + r"):v([1-9][0-9]*)$")
PIN_RE = re.compile(r"^v([1-9][0-9]*)\.json$")
FEED_WINDOW = 50

# Attributes whose value is one URL, and the subset that embeds media.
URL_ATTRS = {"href", "src", "poster", "cite", "action", "formaction", "data",
             "background", "longdesc"}
EMBED_TAGS = {"img", "source", "video", "audio", "track", "embed", "object", "input"}
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]*)\1\s*\)")
SAFE_SCHEMES = ("http:", "https:", "mailto:", "tel:", "data:")
# Every <outline> attribute OPML 2.0 defines (common, subscription-list,
# and link/include types); anything else would be an extension (§11).
OPML_OUTLINE_ATTRS = {"text", "type", "isComment", "isBreakpoint", "created",
                      "category", "description", "htmlUrl", "language", "title",
                      "version", "xmlUrl", "url"}


class Problems(list):
    def add(self, where: str, msg: str) -> None:
        self.append(f"{where}: {msg}")


def is_iso_z(value) -> bool:
    return isinstance(value, str) and bool(ISO_Z_RE.match(value))


class _HTMLCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, str]] = []  # (tag, attr, url)
        # (data-blyg-id, data-blyg-version) of every §10.2 wrapper, in order
        self.transclusions: list[tuple[str | None, str | None]] = []
        # blyg-tk-gen wrappers (§5.7) outside any transclusion: this
        # version's own generated spans, not ones quoted with a fragment
        self.generated = 0
        self._blockquotes: list[bool] = []  # open <blockquote>s: a transclusion?

    def handle_endtag(self, tag):
        if tag == "blockquote" and self._blockquotes:
            self._blockquotes.pop()

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "blockquote":
            a = dict(attrs)
            quoted = "blyg-transclusion" in classes
            self._blockquotes.append(quoted)
            if quoted:
                self.transclusions.append((a.get("data-blyg-id"), a.get("data-blyg-version")))
        if tag in ("span", "div") and "blyg-tk-gen" in classes and not any(self._blockquotes):
            self.generated += 1
        for name, value in attrs:
            if value is None:
                continue
            if name in URL_ATTRS:
                self.refs.append((tag, name, value.strip()))
            elif name == "srcset":
                for candidate in value.split(","):
                    parts = candidate.split()
                    if parts:
                        self.refs.append((tag, name, parts[0]))
            elif name == "style":
                for m in CSS_URL_RE.finditer(value):
                    self.refs.append((tag, "style url()", m.group(2).strip()))


def _collect(content_html: str) -> _HTMLCollector:
    collector = _HTMLCollector()
    collector.feed(content_html)
    collector.close()
    return collector


def check_content_html(where: str, content_html: str, origin: str,
                       public_blyg: Path, problems: Problems) -> None:
    """§7: <description> HTML MUST be self-contained -- absolute media
    URLs, no dependence on the origin's stylesheets or scripts -- and
    §5.4: media MUST be immutable, which only files under the origin's
    media/ directory are held to (scripts/blyg_stamp.py)."""
    collector = _collect(content_html)
    origin_host = urlsplit(origin).netloc
    media_prefix = origin + "media/"

    for tag, attr, url in collector.refs:
        if url == "" or url.startswith("#"):
            continue  # in-document anchors (footnotes) travel with the HTML
        lowered = url.lower()
        if lowered.startswith("//"):
            url = "https:" + url
            lowered = url.lower()
        if not lowered.startswith(SAFE_SCHEMES):
            problems.add(where, f"<{tag} {attr}> has non-absolute URL {url!r} -- "
                                f"content_html MUST be self-contained (§7)")
            continue
        parts = urlsplit(url)
        on_origin = parts.netloc == origin_host
        if tag in ("script", "link") and on_origin:
            problems.add(where, f"<{tag}> loads {url} from the origin -- content_html "
                                f"MUST NOT depend on the origin's scripts or stylesheets (§7)")
        embedded = (tag in EMBED_TAGS and attr in ("src", "srcset", "poster", "data")) \
            or attr == "style url()"
        if embedded and on_origin:
            if not url.startswith(media_prefix):
                problems.add(where, f"<{tag} {attr}> embeds {url}, outside {media_prefix} -- "
                                    f"only blyg media/ files are held immutable (§5.4); "
                                    f"move it to static/blyg/media/")
            elif not (public_blyg / url[len(origin):].split("?")[0]).is_file():
                problems.add(where, f"<{tag} {attr}> embeds {url}, which isn't in the build")


def check_manifest(public_blyg: Path, problems: Problems) -> dict | None:
    path = public_blyg / "blyg.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.add("blyg.json", f"unreadable: {exc}")
        return None
    expect = {"blyg": "0.2", "feed": "feed.xml", "items": "items/index.json"}
    for key, value in expect.items():
        if manifest.get(key) != value:
            problems.add("blyg.json", f"{key} is {manifest.get(key)!r}, expected {value!r}")
    if manifest.get("level") != 1:
        problems.add("blyg.json", f"level is {manifest.get('level')!r}; only 1 is built")
    for key in ("generator", "site", "title"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            problems.add("blyg.json", f"missing {key}")
    if not str(manifest.get("site", "")).endswith("/"):
        problems.add("blyg.json", "site must be the origin base URL, ending in /")
    if not is_iso_z(manifest.get("updated")):
        problems.add("blyg.json", f"updated {manifest.get('updated')!r} is not ISO 8601 UTC")
    if "author" in manifest:
        author = manifest["author"]
        if not isinstance(author, dict):
            problems.add("blyg.json", "author must be an object (§6.1)")
        elif "name" in author and not (isinstance(author["name"], str) and author["name"]):
            problems.add("blyg.json", f"author.name is {author['name']!r}; omit author "
                                      f"rather than assert an empty one (§6.1)")
    return manifest


def check_blogroll(public_blyg: Path, manifest: dict, problems: Problems) -> None:
    """§11: blogroll.opml is standard OPML 2.0 with no extensions, one
    `type="rss"` outline per shown subscription, and exists exactly when
    the manifest's `blogroll` key names it -- a blyg with nothing to show
    serves no file and omits the key (§6.1)."""
    where = "blogroll.opml"
    path = public_blyg / where
    key = manifest.get("blogroll")
    if key is None:
        if path.is_file():
            problems.add(where, "served, but blyg.json has no blogroll key (§6.1) -- "
                                "a leftover from an earlier build? (hugo --cleanDestinationDir)")
        return
    if key != where:
        problems.add("blyg.json", f"blogroll is {key!r}; the filename is protocol-fixed "
                                  f"as {where!r} (§11)")
    if not path.is_file():
        problems.add("blyg.json", f"blogroll key present but {where} isn't served (§6.1)")
        return
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        problems.add(where, f"unparseable: {exc}")
        return
    if root.tag != "opml" or root.get("version") != "2.0":
        problems.add(where, "not an OPML 2.0 document (§11)")
    if root.find("head") is None or root.find("body") is None:
        problems.add(where, "OPML needs <head> and <body>")
        return
    outlines = list(root.find("body").iter("outline"))
    if not outlines:
        problems.add(where, "no entries -- a blyg with nothing to show serves no "
                            "blogroll.opml and omits the manifest key (§11)")
    for n, o in enumerate(outlines, start=1):
        w = f"{where} outline {n}"
        extra = sorted(set(o.attrib) - OPML_OUTLINE_ATTRS)
        if extra:
            problems.add(w, f"carries {extra}, outside OPML 2.0 -- the blogroll has "
                            f"no blyg-specific attributes (§11)")
        if o.get("type") != "rss" or not o.get("text"):
            problems.add(w, 'every entry is type="rss" with a text display title (§11)')
        for attr in ("xmlUrl", "htmlUrl"):
            parts = urlsplit(o.get(attr) or "")
            if parts.scheme not in ("http", "https") or not parts.netloc:
                problems.add(w, f"{attr} {o.get(attr)!r} isn't an absolute http(s) URL (§11)")


def wire_generated(recorded: list[dict]) -> list[dict]:
    """The item document's `generated` from the ledger's: the §5.7 members
    only (the authored parameters are ledger-private)."""
    return [{k: g[k] for k in ("sources", "model", "at") if k in g} for g in recorded]


def check_generated(where: str, generated, ledger: dict, problems: Problems) -> None:
    """§5.7: a non-empty array, one entry per generated span, each naming
    exact published versions of this origin's items as `sources`, with
    optional model and at -- and nothing else, since the array MUST NOT
    carry instruction text or other authoring state."""
    if not isinstance(generated, list) or not generated:
        problems.add(where, "generated is omitted entirely when a version involved no "
                            "generation, never empty (§5.7 rule 4)")
        return
    for n, g in enumerate(generated, start=1):
        w = f"{where} generated[{n}]"
        if not isinstance(g, dict):
            problems.add(w, "must be an object")
            continue
        extra = sorted(set(g) - {"sources", "model", "at"})
        if extra:
            problems.add(w, f"carries {extra} -- only sources, model and at: no "
                            f"instruction text or other authoring state (§5.7 rule 2)")
        if "model" in g and not (isinstance(g["model"], str) and g["model"]):
            problems.add(w, "model must be a non-empty string")
        if "at" in g and not is_iso_z(g["at"]):
            problems.add(w, f"at {g['at']!r} isn't ISO 8601 UTC (§4)")
        sources = g.get("sources")
        if not isinstance(sources, list):
            problems.add(w, "sources must be an array (possibly empty) (§5.7 rule 1)")
            continue
        for src in sources:
            sid = src.get("id") if isinstance(src, dict) else None
            version = src.get("version") if isinstance(src, dict) else None
            if not isinstance(src, dict) or set(src) != {"id", "version"}:
                problems.add(w, f"source {src!r} must be exactly {{id, version}}")
                continue
            changelog = (ledger.get(sid) or {}).get("changelog", [])
            if not isinstance(version, int) or not 1 <= version <= len(changelog):
                problems.add(w, f"source {sid} v{version} isn't a version this origin "
                                f"published (§5.7 rule 1)")
            elif changelog[version - 1].get("kind") == "withdrawn":
                problems.add(w, f"source {sid} v{version} is a withdrawal endcap, with no "
                                f"content to draw on")


def check_item(blyg_id: str, entry: dict, ledger: dict, origin: str, public_blyg: Path,
               problems: Problems) -> dict | None:
    where = f"items/{blyg_id}.json"
    path = public_blyg / "items" / f"{blyg_id}.json"
    if not path.is_file():
        problems.add(where, "missing -- a published item MUST return 200 forever (§4)")
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        problems.add(where, f"not JSON: {exc}")
        return None

    if doc.get("blyg") != "0.2":
        problems.add(where, f"blyg is {doc.get('blyg')!r}, expected '0.2'")
    if doc.get("id") != blyg_id or not bs.is_valid_blyg_id(str(doc.get("id"))):
        problems.add(where, f"id {doc.get('id')!r} doesn't match file name / isn't a blyg id")
    if doc.get("origin") != origin:
        problems.add(where, f"origin {doc.get('origin')!r} != manifest site {origin!r}")
    if "forked_from" in doc:
        problems.add(where, "carries 'forked_from', reserved for L2 (§5.6)")

    kind = doc.get("kind")
    withdrawn = bool(entry.get("withdrawn"))
    expected_kind = "withdrawn" if withdrawn else entry.get("kind")
    if kind != expected_kind:
        problems.add(where, f"kind {kind!r}, ledger says {expected_kind!r}")
    if kind not in ("fragment", "thread", "withdrawn"):
        problems.add(where, f"kind {kind!r} is not a 0.2 kind (§5.3)")

    version = doc.get("version")
    if version != entry.get("version"):
        problems.add(where, f"version {version!r}, ledger says {entry.get('version')!r}")
    changelog = doc.get("changelog")
    if not isinstance(changelog, list) or not changelog:
        problems.add(where, "changelog missing or empty")
    else:
        if [c.get("version") for c in changelog] != list(range(1, len(changelog) + 1)):
            problems.add(where, "changelog versions aren't exactly 1..n (§5.2)")
        if changelog[-1].get("version") != version:
            problems.add(where, "latest changelog version != version")
        if doc.get("updated") != changelog[-1].get("at"):
            problems.add(where, "updated MUST equal the latest changelog entry's at (§5.2)")
        for c in changelog:
            if not is_iso_z(c.get("at")):
                problems.add(where, f"changelog v{c.get('version')} at {c.get('at')!r} isn't ISO 8601 UTC")
            extra = set(c) - {"version", "at", "note", "pinned"}
            if extra:
                problems.add(where, f"changelog v{c.get('version')} leaks ledger-private keys {sorted(extra)}")
            if "pinned" in c and c["pinned"] is not True:
                problems.add(where, f"changelog v{c.get('version')} pinned must be true or absent")
    for key in ("created", "updated"):
        if not is_iso_z(doc.get(key)):
            problems.add(where, f"{key} {doc.get(key)!r} isn't ISO 8601 UTC (§4)")

    content_md = doc.get("content_md", None)
    content_html = doc.get("content_html", None)
    if not isinstance(content_md, str) or not isinstance(content_html, str):
        problems.add(where, "content_md/content_html missing")
        return doc
    if doc.get("content_hash") != bs.content_hash(content_md):
        problems.add(where, "content_hash != sha256(content_md) (§5.1)")
    if doc.get("content_hash") != entry.get("last_hash"):
        problems.add(where, "content_hash != ledger last_hash (unstamped edit?)")

    is_thread = entry.get("kind") == "thread"
    baked = [] if withdrawn else [{"id": t.get("id"), "version": t.get("version")}
                                  for t in entry.get("transclusions", [])]
    if is_thread:
        if doc.get("transclusions") != baked:
            problems.add(where, f"transclusions {doc.get('transclusions')!r} != the "
                                f"ledger's baked {baked!r} -- threads MUST carry "
                                f"{{id, version}} in directive order, [] for an endcap (§10.3)")
    elif "transclusions" in doc:
        problems.add(where, "fragments MUST omit transclusions (§10.3)")

    media = doc.get("media")
    if not isinstance(media, list):
        problems.add(where, "media must be an array")
        media = []
    recorded = [] if withdrawn else wire_generated(entry.get("generated", []))
    if "generated" in doc:
        check_generated(where, doc["generated"], ledger, problems)
    if doc.get("generated", []) != recorded:
        problems.add(where, f"generated {doc.get('generated')!r} != the ledger's "
                            f"{recorded!r} (§5.7)")
    if withdrawn:
        if content_md or content_html or media:
            problems.add(where, "withdrawal endcap MUST have empty content_md/content_html and media [] (§9)")
        if "generated" in doc:
            problems.add(where, "a withdrawal endcap never carries generated (§5.7 rule 4, §9)")
        return doc
    if bs.GEN_ANY_RE.search(content_md):
        problems.add(where, "content_md carries blyg-gen markers -- the published "
                            "markdown carries no authoring markup (§5.7)")

    directives = [(lineno, line, bs.DIRECTIVE_RE.match(line).group(1))
                  for lineno, line, _ in bs.find_directives(content_md)]
    if not is_thread:
        for lineno, line, _ in directives:
            problems.add(where, f"content_md line {lineno} {line!r} is a transclusion "
                                f"directive in a fragment; only threads transclude (§10)")
    elif [d[2] for d in directives] != [t["id"] for t in baked]:
        problems.add(where, f"content_md's directives {[d[2] for d in directives]} don't match "
                            f"transclusions {[t['id'] for t in baked]} -- every directive "
                            f"MUST resolve, in directive order (§10.2, §10.3)")
    check_content_html(where, content_html, origin, public_blyg, problems)
    collected = _collect(content_html)
    if collected.generated != len(doc.get("generated", [])):
        problems.add(where, f"content_html wraps {collected.generated} generated span(s) as "
                            f"blyg-tk-gen, but generated lists {len(doc.get('generated', []))} "
                            f"-- one entry per span (§5.7)")
    wrappers = collected.transclusions
    want = [(t["id"], str(t["version"])) for t in baked]
    if is_thread and wrappers != want:
        problems.add(where, f"content_html bakes {wrappers} but transclusions says {want} -- "
                            f"each directive is baked as <blockquote class=\"blyg-transclusion\" "
                            f"data-blyg-id data-blyg-version> (§10.2)")
    elif not is_thread and wrappers:
        problems.add(where, "a fragment's content_html carries blyg-transclusion blockquotes (§10)")
    for m in media:
        url = m.get("url") if isinstance(m, dict) else None
        if not url or not m.get("mime"):
            problems.add(where, f"media entry {m!r} needs url and mime (§5.4)")
            continue
        rel = url[len(origin):] if url.startswith(origin) else url
        if "://" in rel or not (public_blyg / rel).is_file():
            problems.add(where, f"media {url} isn't served from the origin's media/")
    return doc


def wrapper(blyg_id: str, version: int, fragment_html: str) -> str:
    return (f'<blockquote class="blyg-transclusion" data-blyg-id="{blyg_id}" '
            f'data-blyg-version="{version}">{fragment_html}</blockquote>')


def check_snapshots(public_blyg: Path, ledger: dict, docs: dict, problems: Problems) -> None:
    """§10.3: provenance names "the exact versions baked", so each one must
    be a version its source actually published as a fragment -- the
    ledger's changelog records every version's kind. (An --amend that
    undid or rewrote an unshipped source version is what could break
    that; blyg_stamp.py re-resolves such threads, and this is the check
    behind it.)

    §10.2: a baked snapshot is the fragment's rendered HTML at the baked
    version. The template renders it from the ledger's stored content_md
    rather than copying bytes, so wherever that version's content_html is
    still on the wire -- the fragment's live document, or a pin -- the
    baked copy must match it exactly. (A later fragment version leaves
    nothing to compare against; that's §10.4 working, not a problem.)"""
    for thread_id, doc in sorted(docs.items()):
        if doc.get("kind") != "thread":
            continue
        for t in doc.get("transclusions") or []:
            fid, version = t.get("id"), t.get("version")
            published = {c.get("version"): c.get("kind")
                         for c in (ledger.get(fid) or {}).get("changelog", [])}
            if version not in published:
                problems.add(f"items/{thread_id}.json",
                             f"transcludes {fid} v{version}, a version {fid} has never "
                             f"published (§10.3)")
                continue
            if published[version] != "fragment":
                problems.add(f"items/{thread_id}.json",
                             f"transcludes {fid} v{version}, which was published as "
                             f"{published[version]!r}, not a fragment (§10.2)")
                continue
            source = docs.get(fid)
            reference = None
            if source and source.get("kind") == "fragment" and source.get("version") == version:
                reference = source.get("content_html")
            else:
                pin = public_blyg / "items" / str(fid) / f"v{version}.json"
                if pin.is_file():
                    try:
                        reference = json.loads(pin.read_text(encoding="utf-8")).get("content_html")
                    except ValueError:
                        pass  # check_pins reports it
            if reference is not None and wrapper(fid, version, reference) not in doc.get("content_html", ""):
                problems.add(f"items/{thread_id}.json",
                             f"the baked snapshot of {fid} v{version} isn't byte-identical to "
                             f"that version's own content_html (§10.2)")


def check_index(public_blyg: Path, ledger: dict, docs: dict, problems: Problems) -> None:
    where = "items/index.json"
    try:
        index = json.loads((public_blyg / "items" / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.add(where, f"unreadable: {exc}")
        return
    rows = index.get("items")
    if not isinstance(rows, list):
        problems.add(where, "items must be an array")
        return
    ids = [r.get("id") for r in rows]
    if len(ids) != len(set(ids)):
        problems.add(where, "duplicate ids")
    missing = set(ledger) - set(ids)
    extra = set(ids) - set(ledger)
    for i in sorted(missing):
        problems.add(where, f"{i} is missing -- the index lists every item ever published (§6.2)")
    for i in sorted(extra):
        problems.add(where, f"{i} isn't in the ledger")
    updated = [r.get("updated") for r in rows]
    if updated != sorted(updated, reverse=True):
        problems.add(where, "not ordered by updated descending (§6.2)")
    if rows and index.get("updated") != max(updated):
        problems.add(where, "updated != newest item's updated")
    for r in rows:
        doc = docs.get(r.get("id"))
        if doc is None:
            continue
        for key in ("kind", "created", "updated", "version"):
            if r.get(key) != doc.get(key):
                problems.add(where, f"{r.get('id')} {key} {r.get(key)!r} != item document's {doc.get(key)!r}")


def check_pins(public_blyg: Path, ledger: dict, origin: str, problems: Problems) -> None:
    items_dir = public_blyg / "items"
    served = set()
    if items_dir.is_dir():
        for d in items_dir.iterdir():
            if not d.is_dir():
                continue
            for f in d.iterdir():
                m = PIN_RE.match(f.name)
                where = f"items/{d.name}/{f.name}"
                if not m:
                    problems.add(where, "unexpected file")
                    continue
                n = int(m.group(1))
                served.add((d.name, n))
                entry = ledger.get(d.name)
                if entry is None:
                    problems.add(where, "pin for an id not in the ledger")
                    continue
                if n > len(entry["changelog"]) or not entry["changelog"][n - 1].get("pinned"):
                    problems.add(where, "served, but the ledger doesn't record the pin")
                try:
                    doc = json.loads(f.read_text(encoding="utf-8"))
                except ValueError as exc:
                    problems.add(where, f"not JSON: {exc}")
                    continue
                if doc.get("id") != d.name or doc.get("version") != n or doc.get("pinned") is not True:
                    problems.add(where, "id/version/pinned don't match the path (§8)")
                if doc.get("kind") not in ("fragment", "thread"):
                    problems.add(where, "withdrawal endcaps MUST NOT be pinned (§8 rule 2)")
                if doc.get("content_hash") != bs.content_hash(doc.get("content_md", "")):
                    problems.add(where, "content_hash != sha256(content_md)")
                if doc.get("origin") != origin:
                    problems.add(where, "origin != manifest site")
                if "media" in doc:
                    problems.add(where, "pinned documents carry no media array (§8 rule 4)")
                if "generated" in doc:
                    # §5.7 rule 5: a pinned version carries its own provenance.
                    check_generated(where, doc["generated"], ledger, problems)
    for blyg_id, entry in ledger.items():
        for c in entry["changelog"]:
            if c.get("pinned") and (blyg_id, c["version"]) not in served:
                problems.add(f"items/{blyg_id}/v{c['version']}.json",
                             "ledger records a pin but the file isn't served -- MUST return 200 forever (§8)")


def permalink(origin: str, blyg_id: str, kind: str) -> str:
    """An item's live HTML page (§8.4): f/{id}/ for a fragment, t/{id}/
    for a thread, keyed on the authored kind (which withdrawal keeps)."""
    return f"{origin}{'f' if kind == 'fragment' else 't'}/{blyg_id}/"


# The version line's data-* attributes: the channel the version stepper
# (assets/blyg/version-nav.js) reads, which names versions.
VERSION_DATA = ("data-item", "data-live", "data-pins")


class _PageCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rels: dict[str, list[str]] = {}  # <link rel> -> hrefs
        self.hrefs: list[str] = []            # every href on the page
        self.anchors: list[str] = []          # the plain <a href> links among them
        self.version_data: list[dict] = []    # each element's VERSION_DATA attributes
        self.refresh: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link":
            for rel in (a.get("rel") or "").split():
                self.rels.setdefault(rel, []).append(a.get("href") or "")
        if a.get("href"):
            self.hrefs.append(a["href"])
            if tag == "a":
                self.anchors.append(a["href"])
        if any(k in a for k in VERSION_DATA):
            self.version_data.append({k: a.get(k) for k in VERSION_DATA})
        if tag == "meta" and (a.get("http-equiv") or "").lower() == "refresh":
            self.refresh = (a.get("content") or "").partition("url=")[2] or None


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "source", "track", "wbr"}


class _Markup(html.parser.HTMLParser):
    """HTML as a list of tokens that an HTML minifier doesn't change:
    tags with their (sorted) attributes, and text with whitespace runs
    collapsed. Quoting, entity spelling, attribute order, self-closing
    slashes and inter-tag whitespace all drop out."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple] = []

    def handle_starttag(self, tag, attrs):
        self.tokens.append(("<", tag, tuple(sorted((k, v or "") for k, v in attrs))))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag not in VOID_TAGS:
            self.tokens.append(("</", tag))

    def handle_data(self, data):
        text = " ".join(data.split())
        if text:
            self.tokens.append(("text", text))


def markup(fragment: str) -> list[tuple]:
    parser = _Markup()
    parser.feed(fragment)
    parser.close()
    return parser.tokens


def carries(page_html: str, content_html: str) -> bool:
    """Whether a page carries `content_html` as-is: the same markup, in one
    unbroken run. Not a byte comparison: a site that builds with
    `hugo --minify` minifies these pages like its others -- quoting,
    whitespace -- which changes bytes but not one element, attribute or
    word of the content. (The byte-exact citation is the JSON twin.)"""
    want, have = markup(content_html), markup(page_html)
    if not want:
        return True
    return any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))


def _page(path: Path) -> tuple[str, _PageCollector]:
    text = path.read_text(encoding="utf-8")
    collector = _PageCollector()
    collector.feed(text)
    collector.close()
    return text, collector


def pages_built(public_blyg: Path) -> bool:
    """Whether this build wrote the HTML pages ([params.blyg] pages). Told
    by what only the module writes -- the item-page directories, or feed
    entries linking item pages -- never by blyg/index.html, which a site
    with pages off may well serve itself."""
    if any((public_blyg / p).is_dir() for p in ("f", "t")):
        return True
    try:
        root = ET.parse(public_blyg / "feed.xml").getroot()
    except (OSError, ET.ParseError):
        return False  # check_feed reports it
    return any(item.find("link") is not None for item in root.iter("item"))


def check_pages(public_blyg: Path, ledger: dict, docs: dict, origin: str,
                problems: Problems) -> None:
    """§8.4 and §4's human-readable HTML, when the build wrote it: every
    item has its live permalink page; a pinned page exists exactly for
    each pin (404 otherwise), carrying the pin's content_html unaltered
    and linking its JSON twin; every pin is a plain link from its item's
    live page, so each stays reachable with JavaScript off; and no page
    anywhere links a version the origin doesn't promise forever -- the
    live one and pins (§8.4) -- or names one in the version line's data-*
    attributes, the channel the version stepper reads.

    The pages render inside the site's own baseof.html, so rel="canonical"
    is the site's head's to emit: it's checked where present (a wrong one
    is worse than none), but a missing one isn't a problem."""
    if not pages_built(public_blyg):
        return
    if not (public_blyg / "index.html").is_file():
        problems.add("index.html", "item pages are built, but the feed page isn't -- "
                                   "add \"html\" to the section's outputs in "
                                   "content/blyg/_index.md")
    pinned = {(i, c["version"]) for i, e in ledger.items()
              for c in e["changelog"] if c.get("pinned")}
    pages: list[Path] = [public_blyg / "index.html"]

    for blyg_id, entry in sorted(ledger.items()):
        live = permalink(origin, blyg_id, entry["kind"])
        where = live[len(origin):] + "index.html"
        path = public_blyg / where
        if not path.is_file():
            problems.add(where, f"missing -- {blyg_id}'s live page")
            continue
        text, page = _page(path)
        if page.rels.get("canonical", [live]) != [live]:
            problems.add(where, f"rel=canonical is {page.rels.get('canonical')}, not {live}")
        if page.rels.get("blyg") != [origin]:
            problems.add(where, f"<link rel=\"blyg\"> MUST name the origin {origin} (§12.1)")
        doc = docs.get(blyg_id) or {}
        if doc.get("kind") not in (None, "withdrawn") and not carries(text, doc.get("content_html", "")):
            problems.add(where, "doesn't carry the item's live content_html")

        for n in sorted(v for i, v in pinned if i == blyg_id):
            pin_json = public_blyg / "items" / blyg_id / f"v{n}.json"
            try:
                pin = json.loads(pin_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # check_pins reports it
            pin_page = permalink(origin, blyg_id, pin.get("kind")) + f"v{n}/"
            pw = pin_page[len(origin):] + "index.html"
            if not (public_blyg / pw).is_file():
                problems.add(pw, f"missing -- {blyg_id} v{n} is pinned, and a pinned page "
                                 f"MUST return 200 forever once served (§8.4 rule 1)")
                continue
            if pin_page not in page.anchors:
                problems.add(where, f"doesn't link {pin_page} -- every pinned version stays a "
                                    f"plain link away, JavaScript or not")
            ptext, ppage = _page(public_blyg / pw)
            if not carries(ptext, pin.get("content_html", "")):
                problems.add(pw, "MUST carry the pinned version's publish-time content_html "
                                 "verbatim (§8.4 rule 2)")
            if ppage.rels.get("canonical", [live]) != [live]:
                problems.add(pw, f"rel=canonical SHOULD point at the live page {live} (§8.4 rule 3)")
            if f"{origin}items/{blyg_id}/v{n}.json" not in ppage.hrefs:
                problems.add(pw, f"SHOULD link its v{n}.json twin (§8.4 rule 3)")

    for kind_dir in ("f", "t"):
        d = public_blyg / kind_dir
        if not d.is_dir():
            continue
        for item_dir in sorted(d.iterdir()):
            blyg_id = item_dir.name
            entry = ledger.get(blyg_id)
            if entry is None:
                problems.add(f"{kind_dir}/{blyg_id}/", "served, but not in the ledger")
                continue
            live = permalink(origin, blyg_id, entry["kind"])
            kind = "fragment" if kind_dir == "f" else "thread"
            for child in sorted(item_dir.iterdir()):
                where = f"{kind_dir}/{blyg_id}/{child.name}"
                m = re.fullmatch(r"v([1-9][0-9]*)", child.name)
                if child.name == "index.html":
                    pages.append(child)
                    if kind == entry["kind"]:
                        continue  # the live page, checked above
                    _, page = _page(child)
                    had = any(c.get("kind") == kind for c in entry["changelog"])
                    if not had or page.refresh != live:
                        problems.add(where, f"only the live page {live}, or a redirect to it "
                                            f"from a kind the item once had, belongs here")
                elif m and child.is_dir():
                    pages.append(child / "index.html")
                    if (blyg_id, int(m.group(1))) not in pinned:
                        problems.add(where + "/", f"served, but v{m.group(1)} isn't pinned -- a "
                                                  f"pinned page MUST 404 unless it is (§8.4 rule 1)")
                else:
                    problems.add(where, "unexpected")

    version_link = re.compile(re.escape(origin) + r"(?:[ft]/|items/)(" + bs.BLYG_ID_RE_SRC
                              + r")/v([1-9][0-9]*)(?:/|\.json)")
    for path in pages:
        if not path.is_file():
            continue
        where = str(path.relative_to(public_blyg))
        _, page = _page(path)
        for href in page.hrefs:
            m = version_link.match(href)
            if m and (m.group(1), int(m.group(2))) not in pinned:
                problems.add(where, f"links {href}, an unpinned version -- a version display MUST "
                                    f"NOT offer access to unpinned history (§8.4)")
        for data in page.version_data:
            check_version_data(where, data, ledger, pinned, problems)


def check_version_data(where: str, data: dict, ledger: dict, pinned: set,
                       problems: Problems) -> None:
    """The version line's data-* attributes (VERSION_DATA) are only a
    DOM-to-script channel for the version stepper, but a script turns
    whatever they name into a fetch and a page. So, like a link, they may
    name only versions the origin promises forever: the item's live one
    (data-live) and its pins (data-pins) -- §8.4."""
    blyg_id = data.get("data-item")
    entry = ledger.get(blyg_id)
    if entry is None:
        problems.add(where, f"a version line's data-item is {blyg_id!r}, not an item in the ledger")
        return
    named = [("data-live", data.get("data-live"))]
    named += [("data-pins", v) for v in (data.get("data-pins") or "").split(",") if v]
    for attr, value in named:
        if value is None or not re.fullmatch(r"[1-9][0-9]*", value.strip()):
            problems.add(where, f"{blyg_id}'s {attr} is {value!r}, not a version number")
            continue
        n = int(value)
        if attr == "data-live" and n != entry["version"]:
            problems.add(where, f"{blyg_id}'s data-live is v{n}, but its live version is "
                                f"v{entry['version']}")
        if n != entry["version"] and (blyg_id, n) not in pinned:
            problems.add(where, f"{blyg_id}'s {attr} names v{n}, an unpinned version -- a "
                                f"version display MUST NOT offer access to unpinned history, "
                                f"to a script any more than in a link (§8.4)")


def check_feed(public_blyg: Path, ledger: dict, docs: dict, manifest: dict,
               origin: str, problems: Problems) -> None:
    where = "feed.xml"
    try:
        root = ET.parse(public_blyg / "feed.xml").getroot()
    except (OSError, ET.ParseError) as exc:
        problems.add(where, f"unparseable: {exc}")
        return
    if root.tag != "rss" or root.get("version") != "2.0":
        problems.add(where, "not an RSS 2.0 document (§7)")
    channel = root.find("channel")
    if channel is None:
        problems.add(where, "no <channel>")
        return
    q = lambda name: f"{{{BLYG_NS}}}{name}"  # noqa: E731
    if (channel.findtext(q("manifest")) or "").strip() != origin + "blyg.json":
        problems.add(where, "<blyg:manifest> MUST point at the manifest (§7)")
    for name in ("title", "link", "description"):
        if channel.find(name) is None:
            problems.add(where, f"channel lacks <{name}> (RSS 2.0)")

    with_pages = pages_built(public_blyg)
    entries = channel.findall("item")
    if len(entries) > FEED_WINDOW:
        problems.add(where, f"{len(entries)} entries; window is {FEED_WINDOW} (§7)")
    last_pub = None
    per_id: dict[str, list[int]] = {}
    for n, item in enumerate(entries, start=1):
        w = f"{where} item {n}"
        guid = item.find("guid")
        m = GUID_RE.match(guid.text or "") if guid is not None else None
        if not m or guid.get("isPermaLink") != "false":
            problems.add(w, "guid MUST be blyg:{id}:v{n} with isPermaLink=\"false\" (§7)")
            continue
        blyg_id, version = m.group(1), int(m.group(2))
        per_id.setdefault(blyg_id, []).append(version)
        if item.findtext(q("id")) != blyg_id or item.findtext(q("version")) != str(version):
            problems.add(w, "blyg:id / blyg:version disagree with the guid")
        entry, doc = ledger.get(blyg_id), docs.get(blyg_id)
        if entry is None or doc is None:
            problems.add(w, f"{blyg_id} isn't a built ledger item")
            continue
        if version > entry["version"]:
            problems.add(w, f"v{version} is newer than the item's v{entry['version']}")
            continue
        ledger_kind = entry["changelog"][version - 1].get("kind", entry["kind"])
        if item.findtext(q("kind")) != ledger_kind:
            problems.add(w, f"blyg:kind {item.findtext(q('kind'))!r}, but v{version} was {ledger_kind!r}")
        if item.findtext(q("created")) != doc.get("created"):
            problems.add(w, "blyg:created != item document's created")
        if item.findtext(q("item")) != f"{origin}items/{blyg_id}.json":
            problems.add(w, "blyg:item doesn't point at the item document")
        link = item.findtext("link")
        if with_pages and link != permalink(origin, blyg_id, entry["kind"]):
            problems.add(w, f"<link> {link!r} isn't the item's live page")
        elif not with_pages and link is not None:
            problems.add(w, f"<link> {link!r}, but no item pages were built")
        description = item.findtext("description") or ""
        # --minify trims the text node's edge whitespace; the HTML is the same.
        if description.strip() != doc.get("content_html", "").strip():
            problems.add(w, "description MUST carry the item's latest content_html (§7)")
        note = entry["changelog"][version - 1].get("note")
        title = item.findtext("title")
        if entry.get("withdrawn"):
            if title != "withdrawn" or description:
                problems.add(w, "a withdrawn item's entry has title `withdrawn` and empty description (§7)")
        elif note and title != note:
            problems.add(w, f"entry drops its changelog note {note!r} (§7)")
        if title is None and not description:
            problems.add(w, "RSS 2.0 items need a title or a description")
        try:
            pub = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            problems.add(w, "pubDate isn't RFC 822 (§7)")
            continue
        expected = datetime.datetime.fromisoformat(
            entry["changelog"][version - 1]["at"].replace("Z", "+00:00"))
        if pub != expected:
            problems.add(w, "pubDate != that version's changelog at")
        if last_pub is not None and pub > last_pub:
            problems.add(w, "entries aren't newest first (§7)")
        last_pub = pub

    for blyg_id, entry in ledger.items():
        if entry.get("withdrawn"):
            got = per_id.get(blyg_id, [])
            if len(got) > 1 or (got and got != [entry["version"]]):
                problems.add(where, f"withdrawn {blyg_id} MUST contribute exactly its withdrawal entry (§7)")


def validate(public_dir: Path, ledger_path: Path) -> Problems:
    problems = Problems()
    public_blyg = public_dir / "blyg"
    ledger = bs.load_ledger(ledger_path)
    manifest = check_manifest(public_blyg, problems)
    if manifest is None:
        return problems
    origin = manifest.get("site", "")
    check_blogroll(public_blyg, manifest, problems)

    docs = {}
    for blyg_id, entry in sorted(ledger.items()):
        doc = check_item(blyg_id, entry, ledger, origin, public_blyg, problems)
        if doc is not None:
            docs[blyg_id] = doc
    items_dir = public_blyg / "items"
    if items_dir.is_dir():
        for f in items_dir.glob("*.json"):
            if f.name != "index.json" and f.stem not in ledger:
                problems.add(f"items/{f.name}", "served, but not in the ledger")
    if docs and manifest.get("updated") != max(d.get("updated", "") for d in docs.values()):
        problems.add("blyg.json", "updated != newest item's updated")

    check_snapshots(public_blyg, ledger, docs, problems)
    check_index(public_blyg, ledger, docs, problems)
    check_pins(public_blyg, ledger, origin, problems)
    check_pages(public_blyg, ledger, docs, origin, problems)
    check_feed(public_blyg, ledger, docs, manifest, origin, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--public-dir", default=str(bs.DEFAULT_PUBLIC_DIR))
    parser.add_argument("--ledger-path", default=str(bs.DEFAULT_LEDGER_PATH))
    args = parser.parse_args(argv)

    problems = validate(Path(args.public_dir), Path(args.ledger_path))
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} blyg conformance problem(s).", file=sys.stderr)
        return 1
    print("blyg surface conforms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
