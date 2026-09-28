import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import blyg_stamp as bs  # noqa: E402


def write_post(content_dir: Path, name: str, front_matter: str, body: str) -> Path:
    path = content_dir / name
    path.write_text(f"+++\n{front_matter}+++\n{body}", encoding="utf-8")
    return path


class TmpRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.content_dir = Path(self.tmp.name) / "content" / "blyg"
        self.content_dir.mkdir(parents=True)
        self.ledger_path = Path(self.tmp.name) / "data" / "blyg" / "ledger.json"

    def tearDown(self):
        self.tmp.cleanup()


class IdFormatTests(unittest.TestCase):
    def test_generated_id_is_26_crockford_chars(self):
        for _ in range(200):
            blyg_id = bs.generate_blyg_id()
            self.assertEqual(len(blyg_id), 26)
            self.assertTrue(bs.is_valid_blyg_id(blyg_id), blyg_id)
            for c in blyg_id:
                self.assertIn(c, bs.CROCKFORD_ALPHABET)

    def test_ids_do_not_collide_in_practice(self):
        ids = {bs.generate_blyg_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)

    def test_rejects_wrong_length(self):
        self.assertFalse(bs.is_valid_blyg_id("abc"))
        self.assertFalse(bs.is_valid_blyg_id("a" * 25))
        self.assertFalse(bs.is_valid_blyg_id("a" * 27))

    def test_rejects_excluded_letters(self):
        # Crockford excludes i, l, o, u.
        for bad in "ilou":
            candidate = bad * 26
            self.assertFalse(bs.is_valid_blyg_id(candidate))

    def test_first_char_never_uses_top_two_bits(self):
        # 26 chars * 5 bits = 130 bits of capacity for 128 bits of entropy,
        # so the first char can only ever be one of the first 8 alphabet
        # symbols (0-7), never 8-31.
        first_chars = {bs.generate_blyg_id()[0] for _ in range(500)}
        allowed = set(bs.CROCKFORD_ALPHABET[:8])
        self.assertTrue(first_chars.issubset(allowed), first_chars - allowed)


class FrontMatterSplitTests(unittest.TestCase):
    def test_byte_exact_with_leading_and_trailing_blank_lines(self):
        text = (
            '+++\ntitle = "Raw Test"\ndate = "2020-01-01T00:00:00Z"\n'
            'draft = false\n+++\n'
            '\n\nLine one.\n\nLine two with trailing spaces.   \n\n\n'
        )
        fm, body = bs.split_front_matter(text)
        self.assertEqual(body, "\n\nLine one.\n\nLine two with trailing spaces.   \n\n\n")

    def test_byte_exact_no_leading_blank_no_trailing_newline(self):
        text = (
            '+++\ntitle = "Raw Test 2"\ndraft = false\n+++\n'
            'No leading blank line, and no trailing newline at all.'
        )
        fm, body = bs.split_front_matter(text)
        self.assertEqual(body, "No leading blank line, and no trailing newline at all.")

    def test_missing_delimiters_raise(self):
        with self.assertRaises(bs.BlygStampError):
            bs.split_front_matter("no front matter here")


class NewItemTests(TmpRepoTestCase):
    def test_new_item_gets_id_and_v1(self):
        write_post(
            self.content_dir, "first.md",
            'title = "First"\ndate = "2024-03-01T12:00:00Z"\ndraft = false\n',
            "Hello, blyg.\n",
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.kind, "new")
        self.assertTrue(bs.is_valid_blyg_id(plan.blyg_id))

        stamped = (self.content_dir / "first.md").read_text(encoding="utf-8")
        self.assertIn(f'blyg_id = "{plan.blyg_id}"', stamped)

        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        entry = ledger[plan.blyg_id]
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["created"], "2024-03-01T12:00:00Z")
        self.assertEqual(entry["changelog"], [{"version": 1, "at": "2024-03-01T12:00:00Z", "note": None, "kind": "thread"}])
        self.assertEqual(entry["kind"], "thread")
        self.assertFalse(entry["withdrawn"])

    def test_naive_date_backfills_as_utc(self):
        write_post(
            self.content_dir, "naive.md",
            'title = "Naive Date"\ndate = "2013-11-12T05:04:39"\ndraft = false\n',
            "Old post, no offset.\n",
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        entry = ledger[plans[0].blyg_id]
        self.assertEqual(entry["created"], "2013-11-12T05:04:39Z")

    def test_never_re_serializes_front_matter(self):
        original_fm = 'title = "Preserve Me"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\ntags = ["a", "b",]\n'
        write_post(self.content_dir, "preserve.md", original_fm, "Body text.\n")
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        stamped = (self.content_dir / "preserve.md").read_text(encoding="utf-8")
        # Every original front-matter line survives untouched, in order,
        # with exactly one new blyg_id line added before the closing +++.
        expected_lines = ["+++"] + original_fm.rstrip("\n").split("\n") + [
            f'blyg_id = "{plans[0].blyg_id}"', "+++", "Body text.", ""
        ]
        self.assertEqual(stamped.split("\n"), expected_lines)


class DraftTests(TmpRepoTestCase):
    def test_draft_is_skipped_entirely(self):
        write_post(
            self.content_dir, "draft.md",
            'title = "Draft"\ndate = "2024-01-01T00:00:00Z"\ndraft = true\n',
            "Not published yet.\n",
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans[0].kind, "draft-skip")
        self.assertIsNone(plans[0].blyg_id)

        stamped = (self.content_dir / "draft.md").read_text(encoding="utf-8")
        self.assertNotIn("blyg_id", stamped)
        self.assertEqual(json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.exists() else {}, {})

    def test_index_md_is_never_treated_as_an_item(self):
        (self.content_dir / "_index.md").write_text("+++\ntitle = \"blyg\"\n+++\n", encoding="utf-8")
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans, [])


class IdempotencyAndBumpTests(TmpRepoTestCase):
    def _stamp_once(self, name="post.md", body="Original body.\n"):
        write_post(
            self.content_dir, name,
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            body,
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        return plans[0].blyg_id

    def test_second_run_with_no_change_is_a_noop(self):
        self._stamp_once()
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans[0].kind, "noop")
        ledger_before = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        plans_again = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        ledger_after = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(plans_again[0].kind, "noop")
        self.assertEqual(ledger_before, ledger_after)

    def test_repeated_stamp_is_fully_idempotent(self):
        blyg_id = self._stamp_once()
        for _ in range(5):
            bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger[blyg_id]["version"], 1)
        self.assertEqual(len(ledger[blyg_id]["changelog"]), 1)

    def test_body_change_bumps_version(self):
        blyg_id = self._stamp_once(body="Original body.\n")
        path = self.content_dir / "post.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("Original body.", "Edited body."), encoding="utf-8")

        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans[0].kind, "bump")

        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        entry = ledger[blyg_id]
        self.assertEqual(entry["version"], 2)
        self.assertEqual(len(entry["changelog"]), 2)
        self.assertEqual(entry["changelog"][1]["version"], 2)
        self.assertIsNone(entry["changelog"][1]["note"])
        self.assertEqual(entry["last_hash"], bs.content_hash("Edited body.\n"))

    def test_front_matter_only_change_does_not_bump(self):
        # content_hash covers content_md (body) only, per §5.1 -- a
        # metadata-only edit (e.g. a tag) must not look like a new version.
        blyg_id = self._stamp_once()
        path = self.content_dir / "post.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace('title = "Post"', 'title = "Post (renamed)"')
        path.write_text(text, encoding="utf-8")

        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans[0].kind, "noop")

    def test_missing_ledger_entry_for_known_id_is_a_hard_error(self):
        blyg_id = self._stamp_once()
        self.ledger_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(bs.BlygStampError):
            bs.run_stamp(self.content_dir, self.ledger_path, write=True)


class DryRunTests(TmpRepoTestCase):
    def test_dry_run_writes_nothing(self):
        write_post(
            self.content_dir, "post.md",
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            "Body.\n",
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=False)
        self.assertEqual(plans[0].kind, "new")
        self.assertFalse(self.ledger_path.exists())
        stamped = (self.content_dir / "post.md").read_text(encoding="utf-8")
        self.assertNotIn("blyg_id", stamped)

    def test_dry_run_never_generates_an_id(self):
        # An id is 128 random bits from a cryptographically strong source
        # (§5.1) -- a dry run must not generate one just to display it,
        # since a subsequent real run would never produce that same id
        # and showing one implies a stability that doesn't exist.
        write_post(
            self.content_dir, "post.md",
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            "Body.\n",
        )
        plan = bs.run_stamp(self.content_dir, self.ledger_path, write=False)[0]
        self.assertIsNone(plan.blyg_id)
        self.assertIn("(pending)", bs.format_plan(plan))

    def test_dry_run_and_real_run_messages_differ(self):
        write_post(
            self.content_dir, "post.md",
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            "Body.\n",
        )
        dry_plan = bs.run_stamp(self.content_dir, self.ledger_path, write=False)[0]
        real_plan = bs.run_stamp(self.content_dir, self.ledger_path, write=True)[0]
        self.assertNotEqual(bs.format_plan(dry_plan), bs.format_plan(real_plan))
        self.assertTrue(bs.is_valid_blyg_id(real_plan.blyg_id))
        self.assertIsNone(dry_plan.blyg_id)

    def test_dry_run_bump_and_withdraw_messages_say_would(self):
        write_post(
            self.content_dir, "post.md",
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            "Body.\n",
        )
        bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        path = self.content_dir / "post.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("Body.", "Edited body."),
            encoding="utf-8",
        )
        plan = bs.run_stamp(self.content_dir, self.ledger_path, write=False)[0]
        self.assertEqual(plan.kind, "bump")
        self.assertIn("would bump", plan.detail)
        self.assertIsNone(plan.ledger_entry)

    def test_check_mode_reports_pending_changes(self):
        write_post(
            self.content_dir, "post.md",
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            "Body.\n",
        )
        rc = bs.main([
            "--content-dir", str(self.content_dir),
            "--ledger-path", str(self.ledger_path),
            "--check",
        ])
        self.assertEqual(rc, 1)
        self.assertFalse(self.ledger_path.exists())


class WithdrawalTests(TmpRepoTestCase):
    def _stamp_once(self):
        write_post(
            self.content_dir, "post.md",
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            "Body.\n",
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        return plans[0].blyg_id

    def _mark_withdrawn(self):
        path = self.content_dir / "post.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("draft = false\n", "draft = false\nblyg_withdrawn = true\n")
        path.write_text(text, encoding="utf-8")

    def test_withdrawal_is_exactly_one_endcap_bump(self):
        blyg_id = self._stamp_once()
        self._mark_withdrawn()

        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans[0].kind, "withdraw")

        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        entry = ledger[blyg_id]
        self.assertEqual(entry["version"], 2)
        self.assertTrue(entry["withdrawn"])
        self.assertEqual(entry["last_hash"], bs.EMPTY_CONTENT_HASH)
        self.assertEqual(len(entry["changelog"]), 2)
        self.assertEqual(entry["changelog"][1]["note"], "withdrawn")

    def test_withdrawal_does_not_repeat_on_subsequent_runs(self):
        blyg_id = self._stamp_once()
        self._mark_withdrawn()
        bs.run_stamp(self.content_dir, self.ledger_path, write=True)

        # Even if the underlying body keeps changing while withdrawn, no
        # further version bump should happen -- there's nothing to serve.
        path = self.content_dir / "post.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("Body.", "Body, edited while withdrawn."),
            encoding="utf-8",
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans[0].kind, "noop")

        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger[blyg_id]["version"], 2)

    def test_return_after_withdrawal_bumps_again(self):
        blyg_id = self._stamp_once()
        self._mark_withdrawn()
        bs.run_stamp(self.content_dir, self.ledger_path, write=True)

        path = self.content_dir / "post.md"
        text = path.read_text(encoding="utf-8").replace("blyg_withdrawn = true\n", "")
        path.write_text(text, encoding="utf-8")

        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        self.assertEqual(plans[0].kind, "return")

        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        entry = ledger[blyg_id]
        self.assertEqual(entry["version"], 3)
        self.assertFalse(entry["withdrawn"])
        self.assertEqual(entry["last_hash"], bs.content_hash("Body.\n"))
        self.assertEqual(entry["changelog"][-1]["note"], "returned")


class PinTests(TmpRepoTestCase):
    def setUp(self):
        super().setUp()
        self.public_dir = Path(self.tmp.name) / "public"
        self.static_dir = Path(self.tmp.name) / "static"

    def _stamp_once(self):
        write_post(
            self.content_dir, "post.md",
            'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n',
            "Body.\n",
        )
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=True)
        return plans[0].blyg_id

    def _write_built_item(self, blyg_id: str, version: int, **extra):
        items_dir = self.public_dir / "blyg" / "items"
        items_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "blyg": "0.2",
            "id": blyg_id,
            "kind": "thread",
            "origin": "https://example.org/blyg/",
            "created": "2024-01-01T00:00:00Z",
            "updated": "2024-01-01T00:00:00Z",
            "version": version,
            "content_md": "Body.\n",
            "content_html": "<p>Body.</p>\n",
            "content_hash": bs.content_hash("Body.\n"),
            "media": [],
            "transclusions": [],
            "changelog": [{"version": version, "at": "2024-01-01T00:00:00Z", "note": None}],
        }
        doc.update(extra)
        (items_dir / f"{blyg_id}.json").write_text(json.dumps(doc), encoding="utf-8")

    def test_pin_refuses_on_version_mismatch(self):
        blyg_id = self._stamp_once()
        self._write_built_item(blyg_id, version=99)

        parser = bs.build_parser()
        args = parser.parse_args([
            "--ledger-path", str(self.ledger_path),
            "--public-dir", str(self.public_dir),
            "--static-dir", str(self.static_dir),
            "pin", blyg_id,
        ])
        with self.assertRaises(bs.BlygStampError):
            bs.cmd_pin(args)

    def test_pin_copies_built_item_to_static(self):
        blyg_id = self._stamp_once()
        self._write_built_item(blyg_id, version=1)

        parser = bs.build_parser()
        args = parser.parse_args([
            "--ledger-path", str(self.ledger_path),
            "--public-dir", str(self.public_dir),
            "--static-dir", str(self.static_dir),
            "pin", blyg_id,
        ])
        rc = bs.cmd_pin(args)
        self.assertEqual(rc, 0)

        pinned = self.static_dir / "blyg" / "items" / blyg_id / "v1.json"
        self.assertTrue(pinned.exists())

        # §8's own shape: flat version/at/note/pinned, no changelog,
        # created, updated, or media key at all -- a pin is not a copy
        # of the live item document.
        pin_doc = json.loads(pinned.read_text(encoding="utf-8"))
        self.assertEqual(pin_doc["version"], 1)
        self.assertEqual(pin_doc["at"], "2024-01-01T00:00:00Z")
        self.assertIsNone(pin_doc["note"])
        self.assertTrue(pin_doc["pinned"])
        self.assertEqual(pin_doc["content_md"], "Body.\n")
        for absent in ("changelog", "created", "updated", "media"):
            self.assertNotIn(absent, pin_doc)

        ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertTrue(ledger[blyg_id]["changelog"][0]["pinned"])

    def test_pin_refuses_a_withdrawn_item(self):
        blyg_id = self._stamp_once()
        path = self.content_dir / "post.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("draft = false\n", "draft = false\nblyg_withdrawn = true\n"),
            encoding="utf-8",
        )
        bs.run_stamp(self.content_dir, self.ledger_path, write=True)  # -> v2 endcap
        self._write_built_item(blyg_id, version=2, kind="withdrawn",
                                content_md="", content_html="")

        parser = bs.build_parser()
        args = parser.parse_args([
            "--ledger-path", str(self.ledger_path),
            "--public-dir", str(self.public_dir),
            "--static-dir", str(self.static_dir),
            "pin", blyg_id,
        ])
        with self.assertRaises(bs.BlygStampError):
            bs.cmd_pin(args)


class EndcapShapeTests(unittest.TestCase):
    def test_empty_content_hash_is_well_known(self):
        # sha256("") -- the endcap's content_md is always "".
        self.assertEqual(
            bs.EMPTY_CONTENT_HASH,
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )



FM = 'title = "Post"\ndate = "2024-01-01T00:00:00Z"\ndraft = false\n'


class StampHelpers(TmpRepoTestCase):
    def stamp(self, **kw):
        return bs.run_stamp(self.content_dir, self.ledger_path, write=True, **kw)

    def ledger(self):
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def edit(self, name, old, new):
        path = self.content_dir / name
        path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")

    def first(self, name="post.md", fm=FM, body="Body.\n"):
        write_post(self.content_dir, name, fm, body)
        return self.stamp()[0].blyg_id


class PublishedItemsStayBuildableTests(StampHelpers):
    """§4: items/{id}.json MUST return 200 forever once published."""

    def test_draft_after_publish_is_a_hard_error(self):
        self.first()
        self.edit("post.md", "draft = false", "draft = true")
        with self.assertRaisesRegex(bs.BlygStampError, "MUST stay 200"):
            self.stamp()

    def test_future_date_after_publish_is_a_hard_error(self):
        self.first()
        self.edit("post.md", "2024-01-01", "2999-01-01")
        with self.assertRaisesRegex(bs.BlygStampError, "future date"):
            self.stamp()

    def test_expiry_date_is_a_hard_error(self):
        self.first()
        self.edit("post.md", "draft = false", 'draft = false\nexpiryDate = "2030-01-01"')
        with self.assertRaisesRegex(bs.BlygStampError, "expiryDate"):
            self.stamp()

    def test_future_dated_new_item_waits_unstamped(self):
        write_post(self.content_dir, "later.md",
                   'title = "Later"\ndate = "2999-01-01T00:00:00Z"\n', "Soon.\n")
        plans = self.stamp()
        self.assertEqual(plans[0].kind, "future-skip")
        self.assertEqual(self.ledger(), {})
        self.assertNotIn("blyg_id", (self.content_dir / "later.md").read_text(encoding="utf-8"))

    def test_publish_date_wins_over_date(self):
        write_post(self.content_dir, "p.md",
                   'title = "P"\ndate = "2020-01-01T00:00:00Z"\npublishDate = "2999-01-01T00:00:00Z"\n',
                   "x\n")
        self.assertEqual(self.stamp()[0].kind, "future-skip")

    def test_missing_file_for_ledger_entry_is_a_hard_error(self):
        blyg_id = self.first()
        (self.content_dir / "post.md").unlink()
        with self.assertRaisesRegex(bs.BlygStampError, blyg_id):
            self.stamp()

    def test_missing_file_is_caught_by_check_too(self):
        self.first()
        (self.content_dir / "post.md").unlink()
        rc = bs.main(["--content-dir", str(self.content_dir),
                      "--ledger-path", str(self.ledger_path), "--check"])
        self.assertEqual(rc, 2)

    def test_duplicate_blyg_id_is_a_hard_error(self):
        self.first()
        text = (self.content_dir / "post.md").read_text(encoding="utf-8")
        (self.content_dir / "copy.md").write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(bs.BlygStampError, "both carry"):
            self.stamp()

    def test_renamed_file_keeps_its_version(self):
        blyg_id = self.first()
        (self.content_dir / "post.md").rename(self.content_dir / "moved.md")
        plans = self.stamp()
        self.assertEqual(plans[0].kind, "moved")
        entry = self.ledger()[blyg_id]
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["path"], "content/blyg/moved.md")


class TransclusionDirectiveTests(StampHelpers):
    """§10.1/§10.2: directives MUST resolve; `![[id@vN]]` MUST be rejected."""

    ID = "7c9wk2mhq0v3xj8tn5rzfd41bg"

    def test_directive_line_is_refused(self):
        write_post(self.content_dir, "t.md", FM, f"Intro.\n\n  ![[{self.ID}]]  \n")
        with self.assertRaisesRegex(bs.BlygStampError, "MUST resolve"):
            self.stamp()

    def test_versioned_directive_is_refused_as_reserved(self):
        write_post(self.content_dir, "t.md", FM, f"![[{self.ID}@v2]]\n")
        with self.assertRaisesRegex(bs.BlygStampError, "reserved"):
            self.stamp()

    def test_inert_forms_are_fine(self):
        body = (f"Inline ![[{self.ID}]] is text.\n\n"
                f"```\n![[{self.ID}]]\n```\n\n"
                f"~~~~md\n![[{self.ID}]]\n```\n![[{self.ID}]]\n~~~~\n\n"
                f"![[notanid]]\n![[{self.ID.upper()}]]\n")
        write_post(self.content_dir, "t.md", FM, body)
        self.assertEqual(self.stamp()[0].kind, "new")

    def test_directive_after_a_closed_fence_is_live(self):
        write_post(self.content_dir, "t.md", FM, f"```\ncode\n```\n![[{self.ID}]]\n")
        with self.assertRaises(bs.BlygStampError):
            self.stamp()

    def test_non_ascii_whitespace_around_a_directive_is_refused(self):
        # DIRECTIVE_RE's \s matches U+00A0, which neither Markdown nor
        # item.html strips: refused here rather than failing the build.
        write_post(self.content_dir, "t.md", FM, f"![[{self.ID}]]\u00a0\n")
        with self.assertRaisesRegex(bs.BlygStampError, "U\\+00A0"):
            self.stamp()
        self.assertFalse(self.ledger_path.exists())

    def test_backtick_in_a_backtick_info_string_opens_no_fence(self):
        # "```a`b" is a paragraph with inline code to CommonMark, so the
        # directive after it is live and MUST resolve (issue #7).
        write_post(self.content_dir, "t.md", FM, f"```a`b\n\n![[{self.ID}]]\n")
        with self.assertRaisesRegex(bs.BlygStampError, "MUST resolve"):
            self.stamp()

    def test_backtick_in_a_tilde_info_string_still_opens_a_fence(self):
        write_post(self.content_dir, "t.md", FM, f"~~~a`b\n![[{self.ID}]]\n~~~\n")
        self.assertEqual(self.stamp()[0].kind, "new")

    def test_withdrawn_items_are_not_scanned(self):
        blyg_id = self.first()
        self.edit("post.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.edit("post.md", "Body.", f"![[{self.ID}]]")
        self.assertEqual(self.stamp()[0].kind, "withdraw")
        self.assertTrue(self.ledger()[blyg_id]["withdrawn"])


class TransclusionResolutionTests(StampHelpers):
    """§10.2 resolution at publish time, and §10.4: no cascade."""

    FRAG = FM + 'blyg_kind = "fragment"\n'

    def setUp(self):
        super().setUp()
        # "z" sorts after the threads below, so every test also proves that
        # fragments are planned first regardless of file order.
        self.frag = self.first("z-frag.md", fm=self.FRAG, body="Quoted.\n")

    def thread(self, name="a-thread.md", *targets, extra=""):
        body = "Intro.\n\n" + "".join(f"![[{t}]]\n" for t in targets) + extra
        write_post(self.content_dir, name, FM, body)

    def test_directive_bakes_the_fragments_latest_version(self):
        self.thread("a-thread.md", self.frag)
        plans = self.stamp()
        self.assertEqual([p.path.name for p in plans], ["a-thread.md", "z-frag.md"])
        entry = self.ledger()[plans[0].blyg_id]
        self.assertEqual(entry["transclusions"], [{
            "id": self.frag, "version": 1, "line": 3,
            "content_hash": bs.content_hash("Quoted.\n"), "content_md": "Quoted.\n"}])

    def test_same_run_edit_bakes_the_then_latest_version(self):
        self.edit("z-frag.md", "Quoted.", "Quoted, v2.")
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        [t] = self.ledger()[thread_id]["transclusions"]
        self.assertEqual((t["version"], t["content_md"]), (2, "Quoted, v2.\n"))

    def test_directive_order_and_repeats_are_kept(self):
        other = self.first("y-frag.md", fm=self.FRAG, body="Other.\n")
        self.thread("a-thread.md", self.frag, other, self.frag)
        thread_id = self.stamp()[0].blyg_id
        got = [(t["id"], t["line"]) for t in self.ledger()[thread_id]["transclusions"]]
        self.assertEqual(got, [(self.frag, 3), (other, 4), (self.frag, 5)])

    def test_editing_the_fragment_leaves_the_thread_alone(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        before = self.ledger()[thread_id]
        self.edit("z-frag.md", "Quoted.", "Rewritten.")
        plans = {p.path.name: p.kind for p in self.stamp()}
        self.assertEqual(plans, {"a-thread.md": "noop", "z-frag.md": "bump"})
        self.assertEqual(self.ledger()[thread_id], before)

    def test_withdrawing_the_fragment_leaves_the_thread_alone(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        before = self.ledger()[thread_id]
        self.edit("z-frag.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.stamp()
        self.assertEqual(self.ledger()[thread_id], before)

    def test_republishing_re_resolves(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        self.edit("z-frag.md", "Quoted.", "Rewritten.")
        self.stamp()
        self.edit("a-thread.md", "Intro.", "Intro, edited.")
        self.stamp()
        [t] = self.ledger()[thread_id]["transclusions"]
        self.assertEqual((t["version"], t["content_md"]), (2, "Rewritten.\n"))

    def test_republishing_against_a_withdrawn_fragment_is_refused(self):
        self.thread("a-thread.md", self.frag)
        self.stamp()
        self.edit("z-frag.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.stamp()
        self.edit("a-thread.md", "Intro.", "Intro, edited.")
        with self.assertRaisesRegex(bs.BlygStampError, "is withdrawn"):
            self.stamp()

    def test_unknown_target_is_refused(self):
        self.thread("a-thread.md", "7c9wk2mhq0v3xj8tn5rzfd41bg")
        with self.assertRaisesRegex(bs.BlygStampError, "unknown id or draft"):
            self.stamp()

    def test_thread_target_is_refused(self):
        other = self.first("b-thread.md")
        self.thread("a-thread.md", other)
        with self.assertRaisesRegex(bs.BlygStampError, "is a thread"):
            self.stamp()

    def test_a_thread_can_not_transclude_itself(self):
        thread_id = self.first("a-thread.md")
        self.edit("a-thread.md", "Body.", f"![[{thread_id}]]")
        with self.assertRaisesRegex(bs.BlygStampError, "is a thread"):
            self.stamp()

    def test_directive_in_a_fragment_is_refused(self):
        write_post(self.content_dir, "b-frag.md", self.FRAG, f"![[{self.frag}]]\n")
        with self.assertRaisesRegex(bs.BlygStampError, "only threads transclude"):
            self.stamp()

    def test_fenced_directive_stays_inert(self):
        self.thread("a-thread.md", extra=f"```\n![[{self.frag}]]\n```\n")
        thread_id = self.stamp()[0].blyg_id
        self.assertNotIn("transclusions", self.ledger()[thread_id])

    def test_withdrawing_the_thread_empties_its_transclusions(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        self.edit("a-thread.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.stamp()
        self.assertNotIn("transclusions", self.ledger()[thread_id])

    def test_kind_change_to_fragment_drops_transclusions(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        self.edit("a-thread.md", "draft = false", 'draft = false\nblyg_kind = "fragment"')
        self.edit("a-thread.md", f"![[{self.frag}]]\n", "")
        self.stamp()
        self.assertNotIn("transclusions", self.ledger()[thread_id])

    def test_check_resolves_without_writing(self):
        self.thread("a-thread.md", self.frag)
        before = self.ledger()
        plans = bs.run_stamp(self.content_dir, self.ledger_path, write=False)
        self.assertEqual(plans[0].kind, "new")
        self.assertEqual(self.ledger(), before)
        self.thread("a-thread.md", "7c9wk2mhq0v3xj8tn5rzfd41bg")
        with self.assertRaises(bs.BlygStampError):
            bs.run_stamp(self.content_dir, self.ledger_path, write=False)

    def test_hand_edited_snapshot_is_refused(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        ledger = self.ledger()
        ledger[thread_id]["transclusions"][0]["content_md"] = "Forged.\n"
        bs.save_ledger(self.ledger_path, ledger)
        with self.assertRaisesRegex(bs.BlygStampError, "content_hash"):
            self.stamp()

    def test_snapshot_not_matching_the_body_is_refused(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        ledger = self.ledger()
        ledger[thread_id]["transclusions"][0]["line"] = 1
        bs.save_ledger(self.ledger_path, ledger)
        with self.assertRaisesRegex(bs.BlygStampError, "edited by hand"):
            self.stamp()

    def test_amend_revert_restores_the_shipped_snapshots(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        shipped = self.ledger()
        self.edit("a-thread.md", "Intro.", "Oops.")
        self.stamp(published=shipped)
        # The source moves on and is withdrawn; undoing the thread edit
        # still goes back to exactly what shipped, snapshot included.
        self.edit("z-frag.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.edit("a-thread.md", "Oops.", "Intro.")
        plans = {p.path.name: p.kind for p in self.stamp(published=shipped)}
        self.assertEqual(plans["a-thread.md"], "revert")
        self.assertEqual(self.ledger()[thread_id], shipped[thread_id])

    def test_amend_re_resolves_an_unshipped_version(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        shipped = self.ledger()
        self.edit("a-thread.md", "Intro.", "Edit one.")
        self.stamp(published=shipped)
        self.edit("z-frag.md", "Quoted.", "Rewritten.")
        self.edit("a-thread.md", "Edit one.", "Edit two.")
        self.stamp(published=shipped)
        entry = self.ledger()[thread_id]
        self.assertEqual(entry["version"], 2)
        self.assertEqual(entry["transclusions"][0]["content_md"], "Rewritten.\n")

    # --amend on a *source*: a version the thread baked but that never
    # shipped can be undone or rewritten, and the thread (unchanged
    # itself) has to follow, or its provenance names a version that no
    # longer exists or holds other content (§10.3).

    def _thread_bakes_unshipped_v2(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        shipped = self.ledger()
        self.edit("z-frag.md", "Quoted.", "Draft.")
        self.edit("a-thread.md", "Intro.", "Edited.")
        self.stamp(published=shipped)
        [t] = self.ledger()[thread_id]["transclusions"]
        self.assertEqual((t["version"], t["content_md"]), (2, "Draft.\n"))
        return thread_id, shipped

    def test_amend_revert_of_a_source_re_resolves_the_thread(self):
        thread_id, shipped = self._thread_bakes_unshipped_v2()
        self.edit("z-frag.md", "Draft.", "Quoted.")
        plans = {p.path.name: p for p in self.stamp(published=shipped)}
        self.assertEqual(plans["z-frag.md"].kind, "revert")
        self.assertEqual(plans["a-thread.md"].kind, "amend")
        self.assertIn("was undone", plans["a-thread.md"].detail)
        entry = self.ledger()[thread_id]
        self.assertEqual(entry["version"], 2)
        [t] = entry["transclusions"]
        self.assertEqual((t["version"], t["content_md"]), (1, "Quoted.\n"))

    def test_amend_rewrite_of_a_source_re_resolves_the_thread(self):
        thread_id, shipped = self._thread_bakes_unshipped_v2()
        self.edit("z-frag.md", "Draft.", "Final.")
        plans = {p.path.name: p for p in self.stamp(published=shipped)}
        self.assertEqual(plans["z-frag.md"].kind, "amend")
        self.assertEqual(plans["a-thread.md"].kind, "amend")
        self.assertIn("rewritten in place", plans["a-thread.md"].detail)
        [t] = self.ledger()[thread_id]["transclusions"]
        self.assertEqual((t["version"], t["content_md"]), (2, "Final.\n"))
        # Settled: another run changes nothing.
        kinds = {p.kind for p in self.stamp(published=shipped)}
        self.assertEqual(kinds, {"noop"})

    def test_amend_re_resolves_a_never_shipped_thread_too(self):
        shipped = self.ledger()  # the fragment's v1 only
        self.edit("z-frag.md", "Quoted.", "Draft.")
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp(published=shipped)[0].blyg_id
        self.edit("z-frag.md", "Draft.", "Quoted.")
        plans = {p.path.name: p.kind for p in self.stamp(published=shipped)}
        self.assertEqual(plans["a-thread.md"], "amend")
        entry = self.ledger()[thread_id]
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["transclusions"][0]["version"], 1)

    def test_amend_withdrawing_a_baked_unshipped_source_is_refused(self):
        # The unshipped v2 the thread baked is rewritten into an endcap, so
        # re-resolving the thread hits a withdrawn source (§10.2).
        _, shipped = self._thread_bakes_unshipped_v2()
        self.edit("z-frag.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        with self.assertRaisesRegex(bs.BlygStampError, "is withdrawn"):
            self.stamp(published=shipped)

    def test_withdrawing_a_source_whose_baked_version_shipped_is_fine(self):
        thread_id, _ = self._thread_bakes_unshipped_v2()
        shipped = self.ledger()  # now v2 ships, snapshot and all
        self.edit("z-frag.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        plans = {p.path.name: p.kind for p in self.stamp(published=shipped)}
        self.assertEqual(plans, {"z-frag.md": "withdraw", "a-thread.md": "noop"})
        self.assertEqual(self.ledger()[thread_id], shipped[thread_id])

    def test_amend_elsewhere_leaves_a_thread_baking_shipped_versions_alone(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        shipped = self.ledger()
        self.edit("z-frag.md", "Quoted.", "Draft.")
        self.stamp(published=shipped)
        self.edit("z-frag.md", "Draft.", "Final.")
        plans = {p.path.name: p.kind for p in self.stamp(published=shipped)}
        self.assertEqual(plans["a-thread.md"], "noop")
        self.assertEqual(self.ledger()[thread_id], shipped[thread_id])

    def test_stale_snapshot_without_amend_is_refused(self):
        self.thread("a-thread.md", self.frag)
        thread_id = self.stamp()[0].blyg_id
        ledger = self.ledger()
        ledger[thread_id]["transclusions"][0]["version"] = 2
        bs.save_ledger(self.ledger_path, ledger)
        with self.assertRaisesRegex(bs.BlygStampError, "was undone"):
            self.stamp()

    def test_dry_run_reports_the_re_resolution_without_writing(self):
        thread_id, shipped = self._thread_bakes_unshipped_v2()
        self.edit("z-frag.md", "Draft.", "Quoted.")
        before = self.ledger()
        plans = {p.path.name: p for p in bs.run_stamp(
            self.content_dir, self.ledger_path, write=False, published=shipped)}
        self.assertEqual(plans["a-thread.md"].kind, "amend")
        self.assertEqual(self.ledger(), before)

class KindTests(StampHelpers):
    def test_kind_is_recorded(self):
        blyg_id = self.first(fm=FM + 'blyg_kind = "fragment"\n')
        entry = self.ledger()[blyg_id]
        self.assertEqual(entry["kind"], "fragment")
        self.assertEqual(entry["changelog"][0]["kind"], "fragment")

    def test_kind_change_is_a_new_version(self):
        blyg_id = self.first()
        self.edit("post.md", "draft = false", 'draft = false\nblyg_kind = "fragment"')
        plans = self.stamp()
        self.assertEqual(plans[0].kind, "bump")
        self.assertIn("thread -> fragment", plans[0].detail)
        entry = self.ledger()[blyg_id]
        self.assertEqual((entry["version"], entry["kind"]), (2, "fragment"))
        self.assertEqual([c["kind"] for c in entry["changelog"]], ["thread", "fragment"])

    def test_invalid_kind_is_refused(self):
        write_post(self.content_dir, "p.md", FM + 'blyg_kind = "withdrawn"\n', "x\n")
        with self.assertRaises(bs.BlygStampError):
            self.stamp()

    def test_endcap_changelog_kind_is_withdrawn(self):
        blyg_id = self.first()
        self.edit("post.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.stamp()
        self.assertEqual(self.ledger()[blyg_id]["changelog"][-1]["kind"], "withdrawn")

    def test_kind_change_while_withdrawn_waits_for_return(self):
        blyg_id = self.first()
        self.edit("post.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.stamp()
        self.edit("post.md", "draft = false", 'draft = false\nblyg_kind = "fragment"')
        self.assertEqual(self.stamp()[0].kind, "noop")
        self.edit("post.md", "blyg_withdrawn = true\n", "")
        self.assertEqual(self.stamp()[0].kind, "return")
        entry = self.ledger()[blyg_id]
        self.assertEqual((entry["version"], entry["kind"]), (3, "fragment"))

    def test_ledger_without_kind_is_refused(self):
        blyg_id = self.first()
        ledger = self.ledger()
        del ledger[blyg_id]["kind"]
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        with self.assertRaisesRegex(bs.BlygStampError, "predates kind"):
            self.stamp()


class NoteTests(StampHelpers):
    def test_note_lands_on_the_new_version_only(self):
        blyg_id = self.first()
        self.edit("post.md", "Body.", "Better body.")
        self.stamp(note="sharpened the claim")
        notes = [c["note"] for c in self.ledger()[blyg_id]["changelog"]]
        self.assertEqual(notes, [None, "sharpened the claim"])

    def test_note_overrides_the_withdrawal_default(self):
        blyg_id = self.first()
        self.edit("post.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.stamp(note="no longer true")
        self.assertEqual(self.ledger()[blyg_id]["changelog"][-1]["note"], "no longer true")


class AmendTests(StampHelpers):
    """--amend: versions the last deploy never carried are rewritten, not
    bumped again (§5.2: draft saves are invisible to the protocol)."""

    def test_edit_of_a_shipped_version_still_bumps(self):
        blyg_id = self.first()
        shipped = self.ledger()
        self.edit("post.md", "Body.", "Edit.")
        plans = self.stamp(published=shipped)
        self.assertEqual(plans[0].kind, "bump")
        self.assertEqual(self.ledger()[blyg_id]["version"], 2)

    def test_second_unshipped_edit_amends(self):
        blyg_id = self.first()
        shipped = self.ledger()
        self.edit("post.md", "Body.", "Edit one.")
        self.stamp(published=shipped, note="typo")
        self.edit("post.md", "Edit one.", "Edit two.")
        plans = self.stamp(published=shipped)
        self.assertEqual(plans[0].kind, "amend")
        entry = self.ledger()[blyg_id]
        self.assertEqual(entry["version"], 2)
        self.assertEqual(len(entry["changelog"]), 2)
        self.assertEqual(entry["changelog"][1]["note"], "typo")
        self.assertEqual(entry["last_hash"], bs.content_hash("Edit two.\n"))

    def test_stacked_unshipped_versions_collapse(self):
        blyg_id = self.first()
        shipped = self.ledger()
        for n in range(3):  # three plain bumps, as if run without --amend
            self.edit("post.md", "Body" if n == 0 else f"B{n - 1}", f"B{n}")
            self.stamp()
        self.assertEqual(self.ledger()[blyg_id]["version"], 4)
        self.edit("post.md", "B2", "Final")
        self.stamp(published=shipped)
        entry = self.ledger()[blyg_id]
        self.assertEqual(entry["version"], 2)
        self.assertEqual([c["version"] for c in entry["changelog"]], [1, 2])

    def test_undoing_unshipped_edits_reverts_to_shipped(self):
        blyg_id = self.first()
        shipped = self.ledger()
        self.edit("post.md", "Body.", "Oops.")
        self.stamp(published=shipped)
        self.edit("post.md", "Oops.", "Body.")
        plans = self.stamp(published=shipped)
        self.assertEqual(plans[0].kind, "revert")
        self.assertEqual(self.ledger()[blyg_id], shipped[blyg_id])

    def test_never_shipped_item_stays_v1_at_its_date(self):
        blyg_id = self.first()
        self.edit("post.md", "Body.", "Edit.")
        plans = self.stamp(published={})
        self.assertEqual(plans[0].kind, "amend")
        entry = self.ledger()[blyg_id]
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["changelog"][0]["at"], "2024-01-01T00:00:00Z")

    def test_withdrawing_a_never_shipped_item_is_refused(self):
        self.first()
        self.edit("post.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        with self.assertRaisesRegex(bs.BlygStampError, "never shipped"):
            self.stamp(published={})

    def test_a_pinned_unshipped_version_is_never_rewritten(self):
        blyg_id = self.first()
        shipped = self.ledger()
        self.edit("post.md", "Body.", "Edit one.")
        self.stamp(published=shipped)
        ledger = self.ledger()
        ledger[blyg_id]["changelog"][-1]["pinned"] = True
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        self.edit("post.md", "Edit one.", "Edit two.")
        self.assertEqual(self.stamp(published=shipped)[0].kind, "bump")
        self.assertEqual(self.ledger()[blyg_id]["version"], 3)


OPEN = '{{< blyg-gen model="m" >}}'
CLOSE = "{{< /blyg-gen >}}"


class GenMarkupTests(unittest.TestCase):
    """§5.7: blyg-gen markers are authoring state, stripped from content_md."""

    def parse(self, body):
        return bs.parse_gen(Path("p.md"), body)

    def test_block_marker_lines_go_entirely(self):
        body = f"Before.\n\n{OPEN}\nGenerated.\n{CLOSE}\n\nAfter.\n"
        self.assertEqual(bs.strip_gen_markers(body), "Before.\n\nGenerated.\n\nAfter.\n")
        [span] = self.parse(body)
        self.assertEqual((span.line, span.end, span.block, span.params), (3, 5, True, {"model": "m"}))

    def test_inline_markers_go_on_their_own(self):
        body = f"Some {OPEN}generated{CLOSE} words, {OPEN}twice{CLOSE}.\n"
        self.assertEqual(bs.strip_gen_markers(body), "Some generated words, twice.\n")
        self.assertEqual([s.block for s in self.parse(body)], [False, False])

    def test_escaped_shortcode_is_inert_text(self):
        body = "Write {{</* blyg-gen */>}} to mark a span.\n"
        self.assertEqual(bs.strip_gen_markers(body), body)
        self.assertEqual(self.parse(body), [])

    def refused(self, body, why):
        with self.assertRaisesRegex(bs.BlygStampError, why):
            self.parse(body)

    def test_block_needs_blank_lines_around_it(self):
        self.refused(f"Before.\n{OPEN}\nGenerated.\n{CLOSE}\n", "blank line before")
        self.refused(f"{OPEN}\nGenerated.\n{CLOSE}\nAfter.\n", "blank line after")

    def test_unbalanced_nested_and_empty_spans(self):
        self.refused(f"{OPEN}\nGenerated.\n", "never closes")
        self.refused(f"Text.\n\n{CLOSE}\n", "closes nothing")
        self.refused(f"{OPEN}\n\n{OPEN}\nx\n{CLOSE}\n\n{CLOSE}\n", "don't nest")
        self.refused(f"{OPEN}\n\n{CLOSE}\n", "empty")
        self.refused(f"A {OPEN} {CLOSE} b.\n", "empty")

    def test_inline_span_stays_on_one_line(self):
        self.refused(f"A {OPEN}b\nc{CLOSE} d.\n", "same line")

    def test_only_sources_model_and_at(self):
        self.refused('{{< blyg-gen prompt="write me a poem" >}}\nx\n' + CLOSE + "\n",
                     "MUST NOT carry instruction text")
        self.refused('A {{< blyg-gen at="yesterday" >}}b' + CLOSE + "\n", "ISO 8601")
        self.refused('A {{< blyg-gen sources="nope" >}}b' + CLOSE + "\n", "isn't an item id")

    def test_other_shortcode_forms_are_refused(self):
        self.refused("A {{% blyg-gen %}}b{{% /blyg-gen %}}\n", "malformed")
        self.refused("A {{< blyg-gen model=m >}}b" + CLOSE + "\n", "malformed")

    def test_no_transclusion_inside_a_generated_block(self):
        self.refused(f"{OPEN}\n![[{'0' * 26}]]\n{CLOSE}\n", "quotation, never generation")


class GenerationTests(StampHelpers):
    """§5.7 provenance, recorded at publish time."""

    FRAG = FM + 'blyg_kind = "fragment"\n'

    def setUp(self):
        super().setUp()
        self.source = self.first("a-source.md", fm=self.FRAG, body="Source text.\n")

    def add(self, name, fm=FM, body="Body.\n"):
        """Write and stamp one file; its id (StampHelpers.first returns the
        first plan's, and a-source.md sorts first)."""
        write_post(self.content_dir, name, fm, body)
        return next(p.blyg_id for p in self.stamp() if p.path.name == name)

    def gen_body(self, sources, text="Generated."):
        return f'Intro.\n\n{{{{< blyg-gen sources="{sources}" model="m" >}}}}\n{text}\n{CLOSE}\n'

    def test_content_hash_covers_content_md_without_markers(self):
        blyg_id = self.add("b.md", body=self.gen_body(self.source))
        entry = self.ledger()[blyg_id]
        self.assertEqual(entry["last_hash"], bs.content_hash("Intro.\n\nGenerated.\n"))
        self.assertEqual(entry["generated"], [{
            "sources": [{"id": self.source, "version": 1}], "model": "m",
            "authored": {"sources": self.source, "model": "m"}}])

    def test_sources_pin_the_version_published_before_this_run(self):
        # The source moves to v2 in the same run: the span drew on v1.
        self.edit("a-source.md", "Source text.", "Source, edited.")
        blyg_id = self.add("b.md", body=self.gen_body(f"{self.source}, {self.source}@v1"))
        [g] = self.ledger()[blyg_id]["generated"]
        self.assertEqual(g["sources"], [{"id": self.source, "version": 1}] * 2)

    def test_unresolvable_sources_are_refused(self):
        for sources, why in ((f"{'0' * 26}", "isn't a published item"),
                             (f"{self.source}@v2", "never published v2")):
            write_post(self.content_dir, "b.md", FM, self.gen_body(sources))
            with self.assertRaisesRegex(bs.BlygStampError, why):
                self.stamp()

    def test_a_withdrawn_source_needs_an_explicit_version(self):
        self.edit("a-source.md", 'blyg_kind = "fragment"', 'blyg_kind = "fragment"\nblyg_withdrawn = true')
        self.stamp()
        write_post(self.content_dir, "b.md", FM, self.gen_body(self.source))
        with self.assertRaisesRegex(bs.BlygStampError, "withdrawn now"):
            self.stamp()
        write_post(self.content_dir, "b.md", FM, self.gen_body(f"{self.source}@v1"))
        self.stamp()
        with self.assertRaisesRegex(bs.BlygStampError, "withdrawal endcap"):
            write_post(self.content_dir, "c.md", FM, self.gen_body(f"{self.source}@v2"))
            self.stamp()

    def test_a_parameter_change_is_a_new_version(self):
        blyg_id = self.add("b.md", body=self.gen_body(self.source))
        self.edit("b.md", 'model="m"', 'model="m2"')
        self.assertEqual(self.stamp()[1].kind, "bump")
        entry = self.ledger()[blyg_id]
        self.assertEqual((entry["version"], entry["generated"][0]["model"]), (2, "m2"))
        self.assertEqual(entry["last_hash"], bs.content_hash("Intro.\n\nGenerated.\n"))

    def test_no_cascade_from_a_later_source_edit(self):
        blyg_id = self.add("b.md", body=self.gen_body(self.source))
        self.edit("a-source.md", "Source text.", "Source, edited.")
        plans = {p.path.name: p.kind for p in self.stamp()}
        self.assertEqual(plans["b.md"], "noop")
        self.assertEqual(self.ledger()[blyg_id]["generated"][0]["sources"][0]["version"], 1)

    def test_republishing_re_resolves_a_bare_source(self):
        blyg_id = self.add("b.md", body=self.gen_body(self.source))
        self.edit("a-source.md", "Source text.", "Source, edited.")
        self.stamp()
        self.edit("b.md", "Generated.", "Generated again.")
        self.stamp()
        self.assertEqual(self.ledger()[blyg_id]["generated"][0]["sources"][0]["version"], 2)

    def test_endcap_carries_no_generated_and_return_resolves_afresh(self):
        blyg_id = self.add("b.md", body=self.gen_body(self.source))
        self.edit("b.md", "draft = false", "draft = false\nblyg_withdrawn = true")
        self.stamp()
        self.assertNotIn("generated", self.ledger()[blyg_id])
        self.edit("b.md", "blyg_withdrawn = true", "")
        self.stamp()
        self.assertEqual(len(self.ledger()[blyg_id]["generated"]), 1)

    def test_amend_revert_restores_the_shipped_provenance(self):
        blyg_id = self.add("b.md", body=self.gen_body(self.source))
        shipped = self.ledger()
        self.edit("b.md", 'model="m"', 'model="m2"')
        self.stamp(published=shipped)
        self.edit("b.md", 'model="m2"', 'model="m"')
        self.assertEqual(self.stamp(published=shipped)[1].kind, "revert")
        self.assertEqual(self.ledger()[blyg_id], shipped[blyg_id])

    def test_transclusion_snapshot_keeps_the_markers_and_hashes_without(self):
        frag_body = f"Quoted {OPEN}generated{CLOSE} bit.\n"
        frag = self.add("c-frag.md", fm=self.FRAG, body=frag_body)
        thread = self.add("d-thread.md", body=f"Intro.\n\n![[{frag}]]\n")
        [snap] = self.ledger()[thread]["transclusions"]
        self.assertEqual(snap["content_md"], frag_body)
        self.assertEqual(snap["content_hash"], self.ledger()[frag]["last_hash"])
        self.assertNotIn("generated", self.ledger()[thread])

    def test_pin_document_carries_generated(self):
        built = {"blyg": "0.2", "id": "x", "kind": "fragment", "origin": "o",
                 "content_md": "", "content_html": "", "content_hash": "h",
                 "generated": [{"sources": [], "model": "m"}]}
        doc = bs.build_pin_document(built, {"version": 1, "at": "t", "note": None})
        self.assertEqual(doc["generated"], built["generated"])


class FragmentCapTests(StampHelpers):
    def test_long_fragment_warns_but_publishes(self):
        write_post(self.content_dir, "f.md", FM + 'blyg_kind = "fragment"\n', "x" * 2001)
        plan = self.stamp()[0]
        self.assertEqual(plan.kind, "new")
        self.assertIn("SHOULD cap", plan.warning)

    def test_long_thread_does_not_warn(self):
        write_post(self.content_dir, "t.md", FM, "x" * 5000)
        self.assertIsNone(self.stamp()[0].warning)


class MediaTests(TmpRepoTestCase):
    """§5.4: a media URL MUST always serve the same bytes once published."""

    def setUp(self):
        super().setUp()
        self.media_dir = Path(self.tmp.name) / "static" / "blyg" / "media"
        self.media_dir.mkdir(parents=True)
        self.media_ledger = Path(self.tmp.name) / "data" / "blyg" / "media.json"
        (self.media_dir / "a.png").write_bytes(b"one")
        bs.run_media_stamp(self.media_dir, self.media_ledger, write=True)

    def test_new_file_is_recorded(self):
        recorded = json.loads(self.media_ledger.read_text(encoding="utf-8"))
        self.assertEqual(recorded, {"a.png": "sha256:" + __import__("hashlib").sha256(b"one").hexdigest()})
        self.assertEqual(bs.run_media_stamp(self.media_dir, self.media_ledger, write=True), [])

    def test_changed_bytes_are_refused(self):
        (self.media_dir / "a.png").write_bytes(b"two")
        with self.assertRaisesRegex(bs.BlygStampError, "same bytes"):
            bs.run_media_stamp(self.media_dir, self.media_ledger, write=True)

    def test_deletion_is_refused(self):
        (self.media_dir / "a.png").unlink()
        with self.assertRaisesRegex(bs.BlygStampError, "gone"):
            bs.run_media_stamp(self.media_dir, self.media_ledger, write=True)

    def test_unshipped_file_may_still_change_with_amend(self):
        (self.media_dir / "a.png").write_bytes(b"two")
        changes = bs.run_media_stamp(self.media_dir, self.media_ledger, write=True, published={})
        self.assertEqual(len(changes), 1)


class PinDryRunTests(StampHelpers):
    def test_dry_run_before_or_after_the_subcommand_writes_nothing(self):
        blyg_id = self.first()
        public = Path(self.tmp.name) / "public"
        static = Path(self.tmp.name) / "static"
        items = public / "blyg" / "items"
        items.mkdir(parents=True)
        (items / f"{blyg_id}.json").write_text(json.dumps({
            "blyg": "0.2", "id": blyg_id, "kind": "thread", "version": 1,
            "origin": "https://example.org/blyg/", "content_md": "Body.\n",
            "content_html": "<p>Body.</p>", "content_hash": bs.content_hash("Body.\n"),
            "transclusions": [],
        }), encoding="utf-8")
        common = ["--ledger-path", str(self.ledger_path), "--public-dir", str(public),
                  "--static-dir", str(static)]
        for argv in (common + ["--dry-run", "pin", blyg_id],
                     common + ["pin", blyg_id, "--dry-run"]):
            self.assertEqual(bs.main(argv), 0)
            self.assertFalse((static / "blyg").exists(), argv)
            self.assertNotIn("pinned", self.ledger()[blyg_id]["changelog"][0])


if __name__ == "__main__":
    unittest.main()
