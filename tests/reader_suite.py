#!/usr/bin/env python3
"""An adapter that runs blygger-spec's reader suite (conformance/reader/,
`content_md` -> `content_html` fixtures written from the 0.4 text) against
hugo-blyg: the stamp script and a real Hugo build, end to end.

    git clone https://github.com/blygger/blygger-spec.git /tmp/blygger-spec
    cd /tmp/blygger-spec/conformance/reader
    node run.mjs --batch --name hugo-blyg --out /tmp/hugo-blyg-reader \\
        --only "$(python3 /path/to/hugo-blyg/tests/reader_suite.py --local-cases)" \\
        --cmd "python3 /path/to/hugo-blyg/tests/reader_suite.py --batch"

Needs hugo and go on PATH, like the other end-to-end tests. Not run in CI:
it needs the spec repository, whose fixtures move with the living text.

Each case gets its own scratch copy of exampleSite/'s config, with no
content. The items the case says the publisher holds are published first
-- under the fixture's own ids, in an order that puts every item after the
ones it quotes or links -- then the case itself, and the answer is its
built items/{id}.json. A stamp error or a build failure is a publish
error. This module publishes a single origin, so a case that needs an
imported item from another origin is out of scope (--local-cases lists the
rest). The module mounts its origin at /blyg/, and the fixtures put
theirs at the domain root, so `https://home.example/blyg/` is reported as
`https://home.example/`: a mount path is presentation (§4).

Generation cases mark their span in the reference client's TK notation,
`[TK]instruction[=]output[/TK]`; it maps onto this module's own blyg-gen
shortcode -- inline when the output is one line, a block otherwise -- with
every id the instruction names as a source.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import blyg_stamp as bs  # noqa: E402

ID_RE = re.compile(bs.BLYG_ID_RE_SRC)
TK_RE = re.compile(r"\[TK\](.*?)\[=\](.*?)\[/TK\]", re.S)
BASE = "https://home.example/"
OUR_ORIGIN = BASE + "blyg/"


class Unconverted(Exception):
    """A TK scope that was never generated: authoring state, never published."""


def tk_to_shortcode(md: str) -> str:
    """The TK notation onto blyg-gen: inline when the scope sits inside a
    line, a block when it is a paragraph of its own or spans lines. A TK
    sequence inside code is text, as any authoring grammar is there."""
    code = [(m.start(), m.end()) for m in bs.CODE_SPAN_RE.finditer(md)]
    live = {n for n, _ in bs.live_lines(md)}
    starts = [0]
    for line in md.split("\n"):
        starts.append(starts[-1] + len(line) + 1)

    def line_of(pos: int) -> int:
        return next(n for n in range(1, len(starts)) if pos < starts[n])

    def one(m: re.Match) -> str:
        if any(a <= m.start() < b for a, b in code) or line_of(m.start()) not in live:
            return m.group(0)
        instruction, output = m.group(1), m.group(2)
        sources = ", ".join(dict.fromkeys(
            re.findall(r"\[\[(" + bs.BLYG_ID_RE_SRC + r")", instruction)))
        params = f' sources="{sources}"' if sources else ""
        before = md[:m.start()].rsplit("\n", 1)[-1].strip()
        after = md[m.end():].split("\n", 1)[0].strip()
        if "\n" not in output and (before or after):
            return f"{{{{< blyg-gen{params} >}}}}{output}{{{{< /blyg-gen >}}}}"
        return f"\n\n{{{{< blyg-gen{params} >}}}}\n{output.strip(chr(10))}\n{{{{< /blyg-gen >}}}}\n\n"
    out = TK_RE.sub(one, md)
    spans = [(x.start(), x.end()) for x in bs.CODE_SPAN_RE.finditer(out)]
    for m in re.finditer(r"\[TK\]", out):
        if not any(a <= m.start() < b for a, b in spans):
            raise Unconverted("a TK scope with no generated output reaches content_md")
    # A block span sits between blank lines; don't double them up.
    return re.sub(r"\n{3,}", "\n\n", out).strip("\n") + ("\n" if md.endswith("\n") else "")


def local_case(case: dict) -> bool:
    return all(item.get("local") for item in case.get("items", []))


def order_items(items: list[dict]) -> list[dict]:
    """Items after the ones their content_md names."""
    by_id = {i["id"]: i for i in items}
    done: list[dict] = []
    seen: set[str] = set()

    def visit(item, path=()):
        if item["id"] in seen or item["id"] in path:
            return
        for ref in ID_RE.findall(item.get("content_md", "")):
            if ref in by_id and ref != item["id"]:
                visit(by_id[ref], path + (item["id"],))
        seen.add(item["id"])
        done.append(item)
    for item in items:
        visit(item)
    return done


class Site:
    def __init__(self, root: Path):
        self.root = root
        self.content = root / "content" / "blyg"
        self.ledger = root / "data" / "blyg" / "ledger.json"
        self.n = 0
        shutil.copytree(REPO / "exampleSite", root, ignore=shutil.ignore_patterns(
            "public", "resources", "*.md", "ledger.json", "media.json", "items", "media",
            "blogroll.json"))
        shutil.copy(REPO / "exampleSite" / "content" / "blyg" / "_index.md", self.content)
        (root / "hugo.toml").write_text(
            (root / "hugo.toml").read_text().replace('baseURL = "https://example.org/"',
                                                     f'baseURL = "{BASE}"'))
        (root / "go.mod").write_text(
            (root / "go.mod").read_text().replace("=> ../", f"=> {REPO}/"))

    def path(self, blyg_id: str) -> Path:
        return self.content / f"{blyg_id}.md"

    def write(self, blyg_id: str, kind: str, body: str, *, stamped: bool,
              withdrawn: bool = False) -> None:
        self.n += 1
        fm = f'+++\ndate = "2026-01-01T00:{self.n // 60:02d}:{self.n % 60:02d}Z"\n' \
             f'blyg_kind = "{kind}"\n'
        if withdrawn:
            fm += "blyg_withdrawn = true\n"
        if stamped:
            fm += f'blyg_id = "{blyg_id}"\n'
        self.path(blyg_id).write_text(fm + "+++\n" + body, encoding="utf-8")

    def stamp(self, new_id: str | None = None) -> None:
        real = bs.generate_blyg_id
        if new_id is not None:
            bs.generate_blyg_id = lambda: new_id
        try:
            bs.run_stamp(self.content, self.ledger, write=True)
        finally:
            bs.generate_blyg_id = real

    def publish(self, item: dict) -> None:
        """An item the publisher holds, at its fixture version and state."""
        if item.get("state") == "draft":
            return  # never published: no id, nothing to resolve
        version = int(item.get("version") or 1)
        for v in range(1, version):
            self.write(item["id"], item["kind"], f"Version {v}.\n", stamped=v > 1)
            self.stamp(item["id"] if v == 1 else None)
        self.write(item["id"], item["kind"], item["content_md"], stamped=version > 1)
        self.stamp(item["id"] if version == 1 else None)
        if item.get("state") == "withdrawn":
            self.write(item["id"], item["kind"], item["content_md"], stamped=True,
                       withdrawn=True)
            self.stamp()

    def build(self) -> str | None:
        p = subprocess.run(["hugo", "--logLevel", "error"], cwd=self.root,
                           capture_output=True, text=True)
        if p.returncode == 0:
            return None
        return (p.stderr or p.stdout).strip() or f"hugo exited {p.returncode}"


# Every item any case holds, by id: a fixture's item that quotes another
# may leave the quoted one to another fixture (the loader checks that one
# id means one item throughout), and this module bakes quotes itself.
KNOWN: dict[str, dict] = {}


def with_quoted(items: list[dict]) -> list[dict]:
    have = {i["id"] for i in items}
    todo, out = list(items), list(items)
    while todo:
        for ref in ID_RE.findall(todo.pop().get("content_md", "")):
            if ref not in have and ref in KNOWN:
                have.add(ref)
                out.append(KNOWN[ref])
                todo.append(KNOWN[ref])
    return out


def render(case: dict) -> dict:
    if not local_case(case):
        return {"error": "out of scope: hugo-blyg publishes one origin and imports nothing"}
    with tempfile.TemporaryDirectory() as tmp:
        site = Site(Path(tmp) / "site")
        me = case["self"]["id"]
        prior = case["self"].get("prior_content_md")
        try:
            if prior is not None:
                # The closure case: an item quotes an earlier version of
                # the publishing item, so that version ships first.
                site.write(me, case["kind"], prior, stamped=False)
                site.stamp(me)
            for item in order_items(with_quoted([i for i in case["items"] if i["id"] != me])):
                site.publish(item)
            md = tk_to_shortcode(case["content_md"]) if case.get("notation") else case["content_md"]
            site.write(me, case["kind"], md, stamped=prior is not None)
            site.stamp(None if prior is not None else me)
        except bs.BlygStampError as exc:
            return {"error": f"stamp: {exc}"}
        except Unconverted as exc:
            return {"error": str(exc)}
        err = site.build()
        if err:
            return {"error": f"build: {err.splitlines()[-1]}"}
        doc = json.loads((site.root / "public" / "blyg" / "items" / f"{me}.json")
                         .read_text(encoding="utf-8"))
    out = {"content_html": doc["content_html"].replace(OUR_ORIGIN, BASE),
           "links": doc.get("links", []), "generated": doc.get("generated", [])}
    if "transclusions" in doc:
        out["transclusions"] = doc["transclusions"]
    return out


def main(argv: list[str]) -> int:
    if "--local-cases" in argv:
        fixtures = Path(os.environ.get("READER_FIXTURES", "fixtures"))
        ids = [json.loads(f.read_text())["id"] for f in sorted(fixtures.glob("[0-9]*.json"))
               if local_case(json.loads(f.read_text()))]
        print(",".join(ids))
        return 0
    data = json.load(sys.stdin)
    if "--batch" in argv:
        for c in data["cases"]:
            for item in c.get("items", []):
                if item.get("local"):
                    KNOWN.setdefault(item["id"], item)
        results = [dict(render(c), id=c["id"]) for c in data["cases"]]
        version = re.search(r'"hugo-blyg/([^"]+)"', (REPO / "layouts" / "blyg" /
                                                     "section.blygmanifest.json").read_text())
        json.dump({"renderer": {"name": "hugo-blyg", "version": version.group(1) if version else ""},
                   "results": results}, sys.stdout)
    else:
        json.dump(render(data), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
