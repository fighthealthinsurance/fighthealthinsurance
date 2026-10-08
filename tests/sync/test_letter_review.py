"""Staff letter review (letter_review.py, /timbit/help/letter_review/).

Every letter, prompt, note and handle here is synthetic. The repo is public:
real eval letters, denials and reader names never go in a test.

Covers the import contract and its refusals, --replace, reader assignment
to staff accounts, reader isolation on the pages, label save and update, the
next/done flow, the export contract, delete, that no command prints a
letter or a prompt, and that no error report carries one, or a mark.
"""

import copy
import datetime
import json
import os
import tempfile
from io import BytesIO, StringIO, TextIOWrapper
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter

from fighthealthinsurance import letter_review
from fighthealthinsurance.models import (
    LetterReviewItem,
    LetterReviewLabel,
    LetterReviewPacket,
    LetterReviewReader,
)
from fighthealthinsurance.sentry_filters import (
    before_send_filter,
    before_send_transaction_filter,
)

User = get_user_model()

# Marker words that appear nowhere else, so "is this text on the page / in
# the output" cannot match by accident.
PROMPT_1 = "SYNTH-PROMPT-ONE: a made-up denial for a made-up test widget."
LETTER_1 = "SYNTH-LETTER-ONE: Dear Plan,\nplease cover the test widget.\nThanks."
PROMPT_2 = "SYNTH-PROMPT-TWO about nothing real."
LETTER_2 = "SYNTH-LETTER-TWO about nothing real."
PROMPT_3 = "SYNTH-PROMPT-THREE about nothing real."
LETTER_3 = "SYNTH-LETTER-THREE about nothing real."
PROMPT_4 = "SYNTH-PROMPT-FOUR about nothing real."
LETTER_4 = "SYNTH-LETTER-FOUR about nothing real."
ALL_BODY_TEXT = (
    PROMPT_1,
    LETTER_1,
    PROMPT_2,
    LETTER_2,
    PROMPT_3,
    LETTER_3,
    PROMPT_4,
    LETTER_4,
)
RULE_TEXT = "SYNTH-RULE: mark made-up specifics.\nSecond line of the rule."

# Real keys are opaque ids in no particular order, so these run backwards:
# sorting by key would read them 4, 3, 2, 1, and only position gives 1, 2,
# 3, 4. A page or export that ordered by key fails the ordering tests.
KEY_1 = "item-d-first"
KEY_2 = "item-c-second"
KEY_3 = "item-b-third"
KEY_4 = "item-a-fourth"


def _packet_data(**overrides: Any) -> Dict[str, Any]:
    """A small packet: item 1 shared, 2 and 4 for reader_a, 3 for reader_b."""
    data: Dict[str, Any] = {
        "packet": "synthetic-packet",
        "rule_version": "test-rule-0",
        "rule_text": RULE_TEXT,
        "readers": ["reader_a", "reader_b"],
        "items": [
            {
                "key": KEY_1,
                "prompt": PROMPT_1,
                "letter": LETTER_1,
                "readers": ["reader_a", "reader_b"],
            },
            {
                "key": KEY_2,
                "prompt": PROMPT_2,
                "letter": LETTER_2,
                "readers": ["reader_a"],
            },
            {
                "key": KEY_3,
                "prompt": PROMPT_3,
                "letter": LETTER_3,
                "readers": ["reader_b"],
            },
            {
                "key": KEY_4,
                "prompt": PROMPT_4,
                "letter": LETTER_4,
                "readers": ["reader_a"],
            },
        ],
    }
    data.update(overrides)
    return data


class StaffUsersMixin:
    """Two staff readers, one staff non-reader, one non-staff account."""

    def make_users(self) -> None:
        self.staff_a = User.objects.create_user(
            username="staff_a",
            password="pw",
            email="staff.a@example.com",
            is_staff=True,
        )
        self.staff_b = User.objects.create_user(
            username="staff_b",
            password="pw",
            email="staff.b@example.com",
            is_staff=True,
        )
        self.staff_other = User.objects.create_user(
            username="staff_other", password="pw", is_staff=True
        )
        self.civilian = User.objects.create_user(
            username="civilian", password="pw", is_staff=False
        )

    def load_packet(self, data: Optional[Dict[str, Any]] = None) -> LetterReviewPacket:
        parsed = letter_review.parse_packet(data or _packet_data())
        users = {"reader_a": self.staff_a, "reader_b": self.staff_b}
        return letter_review.import_packet(parsed, users).packet


def _run_import(
    data: Any,
    *assign: str,
    replace: bool = False,
    raw: Optional[str] = None,
    stdin: Any = None,
):
    out, err = StringIO(), StringIO()
    if stdin is None:
        stdin = StringIO(raw if raw is not None else json.dumps(data))
    args = ["letter_review_import", "--file", "-"]
    for pair in assign:
        args += ["--assign", pair]
    if replace:
        args.append("--replace")
    call_command(*args, stdin=stdin, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


ASSIGN = ("reader_a=staff_a", "reader_b=staff_b")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class ImportTests(StaffUsersMixin, TestCase):
    def setUp(self) -> None:
        self.make_users()

    def test_import_stores_items_in_order_with_their_readers(self):
        _run_import(_packet_data(), *ASSIGN)
        packet = LetterReviewPacket.objects.get(name="synthetic-packet")
        items = list(LetterReviewItem.objects.filter(packet=packet))
        self.assertEqual(
            [(i.key, i.position) for i in items],
            [(KEY_1, 0), (KEY_2, 1), (KEY_3, 2), (KEY_4, 3)],
        )
        self.assertEqual(items[0].letter, LETTER_1)
        self.assertEqual(
            sorted(r.handle for r in items[0].readers.all()), ["reader_a", "reader_b"]
        )

    def test_import_ties_each_handle_to_its_staff_account(self):
        _run_import(_packet_data(), *ASSIGN)
        readers = {
            r.handle: r.user for r in LetterReviewReader.objects.select_related("user")
        }
        self.assertEqual(readers, {"reader_a": self.staff_a, "reader_b": self.staff_b})

    def test_import_assigns_by_email_ignoring_case(self):
        _run_import(_packet_data(), "reader_a=STAFF.A@example.com", "reader_b=staff_b")
        reader = LetterReviewReader.objects.get(handle="reader_a")
        self.assertEqual(reader.user, self.staff_a)

    def test_import_reads_a_file_path(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(_packet_data(), fh)
            path = fh.name
        try:
            call_command(
                "letter_review_import",
                "--file",
                path,
                "--assign",
                ASSIGN[0],
                "--assign",
                ASSIGN[1],
                stdout=StringIO(),
                stderr=StringIO(),
            )
        finally:
            os.unlink(path)
        self.assertEqual(LetterReviewItem.objects.count(), 4)

    def test_import_gives_each_item_its_own_random_slug(self):
        _run_import(_packet_data(), *ASSIGN)
        items = list(LetterReviewItem.objects.all())
        slugs = {item.slug for item in items}
        self.assertEqual(len(slugs), len(items))
        for item in items:
            self.assertRegex(item.slug, r"^[A-Za-z0-9_-]{12}$")
            self.assertNotIn(item.key, item.slug)

    def test_import_output_never_prints_letter_prompt_or_rule_text(self):
        out, err = _run_import(_packet_data(), *ASSIGN)
        for text in ALL_BODY_TEXT + (RULE_TEXT,):
            self.assertNotIn(text[:12], out + err)

    def test_import_output_reports_counts_per_reader(self):
        out, _ = _run_import(_packet_data(), *ASSIGN)
        self.assertIn("4 items, 1 read by more than one reader", out)
        self.assertIn("reader_a -> staff_a: 3 items", out)
        self.assertIn("reader_b -> staff_b: 2 items", out)


class ImportRefusalTests(StaffUsersMixin, TestCase):
    def setUp(self) -> None:
        self.make_users()

    def assertRefused(self, data: Any, *assign: str, raw: Optional[str] = None) -> str:
        with self.assertRaises(CommandError) as ctx:
            _run_import(data, *(assign or ASSIGN), raw=raw)
        self.assertEqual(LetterReviewPacket.objects.count(), 0)
        message = str(ctx.exception)
        for text in ALL_BODY_TEXT:
            self.assertNotIn(text[:12], message)
        return message

    def test_a_field_outside_the_contract_is_refused(self):
        message = self.assertRefused(_packet_data(model="synthetic-model-x"))
        self.assertIn("model", message)

    def test_an_item_field_outside_the_contract_is_refused(self):
        data = _packet_data()
        data["items"][1]["judge_score"] = 0.5
        message = self.assertRefused(data)
        self.assertIn("judge_score", message)

    def test_a_missing_field_is_refused(self):
        data = _packet_data()
        del data["rule_text"]
        self.assertIn("rule_text", self.assertRefused(data))

    def test_a_missing_item_field_is_refused(self):
        data = _packet_data()
        del data["items"][2]["letter"]
        self.assertIn("letter", self.assertRefused(data))

    def test_a_repeated_key_is_refused(self):
        data = _packet_data()
        data["items"][3]["key"] = KEY_1
        self.assertIn("repeats", self.assertRefused(data))

    def test_a_key_shorter_than_eight_is_refused(self):
        data = _packet_data()
        data["items"][0]["key"] = "short"
        self.assertRefused(data)

    def test_a_key_with_other_characters_is_refused(self):
        data = _packet_data()
        data["items"][0]["key"] = "item/0001"
        self.assertRefused(data)

    def test_an_item_reader_outside_the_packet_readers_is_refused(self):
        data = _packet_data()
        data["items"][1]["readers"] = ["reader_c"]
        self.assertIn("reader_c", self.assertRefused(data))

    def test_a_field_given_twice_in_the_json_is_refused(self):
        raw = json.dumps(_packet_data())
        raw = raw.replace(
            '"rule_version": "test-rule-0"', '"rule_version": "a", "rule_version": "b"'
        )
        self.assertIn("twice", self.assertRefused(None, raw=raw))

    def test_text_that_is_not_json_is_refused(self):
        self.assertIn("not valid JSON", self.assertRefused(None, raw="{nope"))

    def test_an_empty_letter_is_refused(self):
        data = _packet_data()
        data["items"][0]["letter"] = "   "
        self.assertRefused(data)

    def test_a_letter_over_the_length_limit_is_refused(self):
        data = _packet_data()
        data["items"][0]["letter"] = "x" * (letter_review.MAX_LETTER + 1)
        self.assertRefused(data)

    def test_an_empty_item_list_is_refused(self):
        self.assertRefused(_packet_data(items=[]))

    def test_a_packet_name_with_a_newline_is_refused(self):
        self.assertRefused(_packet_data(packet="two\nlines"))

    def test_text_with_a_lone_surrogate_is_refused(self):
        # json.dumps writes it as the escape \ud800, which decodes to a
        # string the database cannot encode.
        lone = "SYNTH-LONE \ud800 surrogate"
        for field, item in (
            ("packet", None),
            ("rule_version", None),
            ("rule_text", None),
            ("prompt", 1),
            ("letter", 1),
        ):
            with self.subTest(field=field):
                data = _packet_data()
                (data if item is None else data["items"][item])[field] = lone
                message = self.assertRefused(data)
                self.assertIn(f"{field} has a character that is not valid", message)
                self.assertNotIn("SYNTH-LONE", message)

    def test_json_nested_too_deeply_is_refused_without_echoing_it(self):
        depth = 100_000
        raw = '{"packet": ' + "[" * depth + "]" * depth + "}"
        message = self.assertRefused(None, raw=raw)
        self.assertIn("nests too deeply", message)
        self.assertNotIn("[[", message)

    def test_a_number_too_long_to_read_is_refused_without_echoing_it(self):
        # Past Python's limit on the digits it turns into an int.
        raw = '{"packet": ' + "9" * 5_000 + "}"
        message = self.assertRefused(None, raw=raw)
        self.assertIn("number too long", message)
        self.assertNotIn("999", message)

    def test_a_reader_without_an_assignment_is_refused(self):
        message = self.assertRefused(_packet_data(), "reader_a=staff_a")
        self.assertIn("reader_b", message)

    def test_an_assignment_for_a_handle_not_in_the_packet_is_refused(self):
        self.assertRefused(
            _packet_data(),
            "reader_a=staff_a",
            "reader_b=staff_b",
            "reader_c=staff_other",
        )

    def test_an_assignment_to_a_non_staff_account_is_refused(self):
        message = self.assertRefused(
            _packet_data(), "reader_a=staff_a", "reader_b=civilian"
        )
        self.assertIn("not staff", message)

    def test_an_assignment_to_an_inactive_staff_account_is_refused(self):
        self.staff_b.is_active = False
        self.staff_b.save()
        self.assertIn("not active", self.assertRefused(_packet_data(), *ASSIGN))

    def test_an_assignment_to_no_account_is_refused(self):
        self.assertRefused(_packet_data(), "reader_a=staff_a", "reader_b=nobody_here")

    def test_two_handles_on_one_account_is_refused(self):
        message = self.assertRefused(
            _packet_data(), "reader_a=staff_a", "reader_b=staff_a"
        )
        self.assertIn("same account", message)

    def test_a_failure_part_way_through_the_write_leaves_nothing(self):
        real_create = LetterReviewItem.objects.create
        calls = {"n": 0}

        def flaky_create(**kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("synthetic write failure")
            return real_create(**kwargs)

        with patch.object(LetterReviewItem.objects, "create", side_effect=flaky_create):
            with self.assertRaises(RuntimeError):
                _run_import(_packet_data(), *ASSIGN)
        self.assertEqual(LetterReviewPacket.objects.count(), 0)
        self.assertEqual(LetterReviewItem.objects.count(), 0)
        self.assertEqual(LetterReviewReader.objects.count(), 0)


IMPORT_COMMAND = "fighthealthinsurance.management.commands.letter_review_import"


class ImportSizeTests(StaffUsersMixin, TestCase):
    """A packet over MAX_PACKET_BYTES is refused before any of it is decoded,
    and the import never reads more than one past the limit."""

    def setUp(self) -> None:
        self.make_users()
        self.raw = json.dumps(_packet_data())

    def test_the_limit_fits_a_packet_at_every_field_limit(self):
        handles = [f"reader_{n:02d}".ljust(64, "x") for n in range(20)]
        item = {
            "key": "k" * 64,
            "prompt": "p" * letter_review.MAX_PROMPT,
            "letter": "l" * letter_review.MAX_LETTER,
            "readers": handles,
        }

        def size(items: int) -> int:
            # Pretty-printed, so the indentation counts too.
            top = _packet_data(
                packet="n" * letter_review.MAX_NAME,
                rule_version="v" * letter_review.MAX_RULE_VERSION,
                rule_text="r" * letter_review.MAX_RULE_TEXT,
                readers=handles,
                items=[item] * items,
            )
            return len(json.dumps(top, indent=4))

        per_item = size(2) - size(1)
        largest = size(1) + (letter_review.MAX_ITEMS - 1) * per_item
        self.assertLessEqual(largest, letter_review.MAX_PACKET_BYTES)

    def test_a_packet_at_the_limit_is_read_a_chunk_at_a_time(self):
        with patch(f"{IMPORT_COMMAND}.MAX_PACKET_BYTES", len(self.raw)):
            with patch(f"{IMPORT_COMMAND}.READ_CHUNK", 7):
                _run_import(None, *ASSIGN, raw=self.raw)
        self.assertEqual(LetterReviewItem.objects.count(), 4)

    def test_a_packet_over_the_limit_is_refused_before_it_is_decoded(self):
        # Not JSON, so a refusal about size shows it never reached the decoder.
        stdin = StringIO("{" * 10_000)
        with patch(f"{IMPORT_COMMAND}.MAX_PACKET_BYTES", 100):
            with self.assertRaises(CommandError) as ctx:
                _run_import(None, *ASSIGN, stdin=stdin)
        self.assertEqual(str(ctx.exception), "The packet is larger than 100 bytes")
        self.assertEqual(stdin.tell(), 101, "read stops one past the limit")
        self.assertEqual(LetterReviewPacket.objects.count(), 0)

    def test_a_real_stdin_is_read_as_utf8_bytes(self):
        data = _packet_data()
        data["items"][0]["letter"] = "SYNTH-LETTER caf\u00e9"
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        # The text layer would read the é as two latin-1 characters; the
        # command reads the bytes under it, as UTF-8.
        stdin = TextIOWrapper(BytesIO(raw), encoding="latin-1")
        with patch(f"{IMPORT_COMMAND}.MAX_PACKET_BYTES", len(raw)):
            _run_import(None, *ASSIGN, stdin=stdin)
        self.assertEqual(
            LetterReviewItem.objects.get(key=KEY_1).letter, data["items"][0]["letter"]
        )

    def test_a_real_stdin_over_the_limit_counts_bytes_not_characters(self):
        data = _packet_data()
        data["items"][0]["letter"] = "SYNTH-LETTER caf\u00e9"
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        stdin = TextIOWrapper(BytesIO(raw), encoding="utf-8")
        # As many characters as the limit allows, but one more byte: the é.
        with patch(f"{IMPORT_COMMAND}.MAX_PACKET_BYTES", len(raw) - 1):
            with self.assertRaises(CommandError) as ctx:
                _run_import(None, *ASSIGN, stdin=stdin)
        self.assertIn("larger than", str(ctx.exception))

    def test_a_packet_file_over_the_limit_is_refused(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as fh:
            fh.write(self.raw.encode("utf-8"))
            path = fh.name
        try:
            with patch(f"{IMPORT_COMMAND}.MAX_PACKET_BYTES", len(self.raw) - 1):
                with self.assertRaises(CommandError) as ctx:
                    call_command(
                        "letter_review_import",
                        "--file",
                        path,
                        "--assign",
                        ASSIGN[0],
                        "--assign",
                        ASSIGN[1],
                        stdout=StringIO(),
                        stderr=StringIO(),
                    )
        finally:
            os.unlink(path)
        self.assertIn("larger than", str(ctx.exception))
        self.assertEqual(LetterReviewPacket.objects.count(), 0)


class ReplaceTests(StaffUsersMixin, TestCase):
    def setUp(self) -> None:
        self.make_users()
        _run_import(_packet_data(), *ASSIGN)
        self.original = LetterReviewPacket.objects.get()
        reader = LetterReviewReader.objects.get(handle="reader_a")
        LetterReviewLabel.objects.create(
            item=LetterReviewItem.objects.get(key=KEY_1),
            reader=reader,
            verdict="clean",
        )

    def test_an_existing_name_is_refused_without_replace(self):
        changed = _packet_data(rule_version="test-rule-1")
        with self.assertRaises(CommandError) as ctx:
            _run_import(changed, *ASSIGN)
        self.assertIn("--replace", str(ctx.exception))
        packet = LetterReviewPacket.objects.get()
        self.assertEqual(packet.rule_version, "test-rule-0")
        self.assertEqual(LetterReviewLabel.objects.count(), 1)

    def test_replace_swaps_the_packet_for_the_new_one(self):
        changed = _packet_data(rule_version="test-rule-1")
        changed["items"] = changed["items"][:2]
        _run_import(changed, *ASSIGN, replace=True)
        packet = LetterReviewPacket.objects.get()
        self.assertEqual(packet.rule_version, "test-rule-1")
        self.assertNotEqual(packet.pk, self.original.pk)
        self.assertEqual(LetterReviewItem.objects.filter(packet=packet).count(), 2)

    def test_replace_deletes_the_old_labels_and_says_how_many(self):
        _, err = _run_import(_packet_data(), *ASSIGN, replace=True)
        self.assertEqual(LetterReviewLabel.objects.count(), 0)
        self.assertIn("1 labels were deleted", err)


# ---------------------------------------------------------------------------
# Pages: access and reader isolation
# ---------------------------------------------------------------------------


class PageTestBase(StaffUsersMixin, TestCase):
    def setUp(self) -> None:
        self.make_users()
        self.packet = self.load_packet()
        self.reader_a = LetterReviewReader.objects.get(handle="reader_a")
        self.reader_b = LetterReviewReader.objects.get(handle="reader_b")

    def item(self, key: str) -> LetterReviewItem:
        return LetterReviewItem.objects.get(packet=self.packet, key=key)

    def item_url(self, key: str) -> str:
        """The page for the item with this eval key, which is addressed by
        its slug: the key itself never appears in a URL."""
        return reverse("letter_review_item", args=[self.packet.pk, self.item(key).slug])

    def next_url(self) -> str:
        return reverse("letter_review_next", args=[self.packet.pk])

    def mine_url(self) -> str:
        return reverse("letter_review_mine", args=[self.packet.pk])

    def done_url(self) -> str:
        return reverse("letter_review_done", args=[self.packet.pk])

    def export_url(self) -> str:
        return reverse("letter_review_export", args=[self.packet.pk])

    def label(self, reader: LetterReviewReader, key: str, verdict: str, note: str = ""):
        return LetterReviewLabel.objects.create(
            item=self.item(key),
            reader=reader,
            verdict=verdict,
            note=note,
        )


class AccessTests(PageTestBase):
    def _urls(self):
        return [
            reverse("letter_review_index"),
            self.next_url(),
            self.done_url(),
            self.mine_url(),
            self.item_url(KEY_1),
            self.export_url(),
        ]

    def test_anonymous_is_sent_to_the_admin_login(self):
        for url in self._urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(
                    response,
                    f"{reverse('admin:login')}?next={url}",
                    fetch_redirect_response=False,
                )

    def test_non_staff_is_sent_to_the_admin_login(self):
        self.client.force_login(self.civilian)
        for url in self._urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(
                    response,
                    f"{reverse('admin:login')}?next={url}",
                    fetch_redirect_response=False,
                )

    def test_non_staff_post_saves_no_label(self):
        self.client.force_login(self.civilian)
        self.client.post(self.item_url(KEY_1), {"verdict": "clean"})
        self.assertEqual(LetterReviewLabel.objects.count(), 0)

    def test_a_reader_opens_their_own_item(self):
        self.client.force_login(self.staff_a)
        response = self.client.get(self.item_url(KEY_1))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SYNTH-LETTER-ONE")
        self.assertContains(response, "SYNTH-PROMPT-ONE")
        self.assertContains(response, "SYNTH-RULE")

    def test_a_reader_cannot_open_another_readers_item(self):
        self.client.force_login(self.staff_a)
        response = self.client.get(self.item_url(KEY_3))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "SYNTH-LETTER-THREE", status_code=404)

    def test_a_reader_cannot_label_another_readers_item(self):
        self.client.force_login(self.staff_a)
        response = self.client.post(self.item_url(KEY_3), {"verdict": "flag"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(LetterReviewLabel.objects.count(), 0)

    def test_staff_who_are_not_readers_cannot_open_an_item(self):
        self.client.force_login(self.staff_other)
        for url in (
            self.item_url(KEY_1),
            self.next_url(),
            self.done_url(),
            self.mine_url(),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_an_unknown_slug_is_a_404(self):
        self.client.force_login(self.staff_a)
        url = reverse("letter_review_item", args=[self.packet.pk, "no-such-slug"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_eval_key_does_not_open_the_item(self):
        self.client.force_login(self.staff_a)
        url = reverse("letter_review_item", args=[self.packet.pk, KEY_1])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_item_page_never_shows_an_eval_key(self):
        self.client.force_login(self.staff_a)
        response = self.client.get(self.item_url(KEY_2))
        for key in (KEY_1, KEY_2, KEY_3, KEY_4):
            self.assertNotContains(response, key)

    def test_the_index_shows_non_readers_only_names_and_a_count(self):
        self.client.force_login(self.staff_other)
        response = self.client.get(reverse("letter_review_index"))
        self.assertContains(response, "synthetic-packet")
        self.assertContains(response, "4 letters")
        for text in ALL_BODY_TEXT + (RULE_TEXT,):
            self.assertNotContains(response, text[:12])
        self.assertNotContains(response, self.next_url())

    def test_the_index_shows_a_reader_their_own_progress(self):
        self.label(self.reader_a, KEY_2, "clean")
        self.label(self.reader_b, KEY_1, "flag")
        self.label(self.reader_b, KEY_3, "flag")
        self.client.force_login(self.staff_a)
        response = self.client.get(reverse("letter_review_index"))
        self.assertContains(response, "1 of 3 labeled")
        self.assertContains(response, self.next_url())

    def test_a_reader_never_sees_another_readers_label(self):
        self.label(self.reader_b, KEY_1, "fabricates", note="SYNTH-B-ONLY-NOTE")
        self.client.force_login(self.staff_a)
        response = self.client.get(self.item_url(KEY_1))
        self.assertNotContains(response, "SYNTH-B-ONLY-NOTE")
        self.assertNotContains(response, " checked")

    def test_the_letter_is_escaped_and_keeps_its_line_breaks(self):
        item = LetterReviewItem.objects.get(packet=self.packet, key=KEY_2)
        item.letter = "SYNTH line one\n<script>alert(1)</script>\nline three"
        item.save()
        self.client.force_login(self.staff_a)
        response = self.client.get(self.item_url(KEY_2))
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "SYNTH line one\n&lt;script&gt;")

    def test_staff_who_are_not_superusers_cannot_export(self):
        self.client.force_login(self.staff_a)
        self.assertEqual(self.client.get(self.export_url()).status_code, 403)

    def test_the_dashboard_links_to_the_review(self):
        self.client.force_login(self.staff_other)
        response = self.client.get(reverse("staff_dashboard"))
        self.assertContains(response, reverse("letter_review_index"))
        self.assertContains(response, "Letter review")


# ---------------------------------------------------------------------------
# Labels, and the next / done flow
# ---------------------------------------------------------------------------


class LabelTests(PageTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.staff_a)

    def test_saving_creates_the_readers_label(self):
        self.client.post(
            self.item_url(KEY_1), {"verdict": "flag", "note": "SYNTH note"}
        )
        label = LetterReviewLabel.objects.get()
        self.assertEqual(
            (label.reader, label.item.key, label.verdict, label.note),
            (self.reader_a, KEY_1, "flag", "SYNTH note"),
        )

    def test_saving_again_changes_the_same_label(self):
        self.client.post(self.item_url(KEY_1), {"verdict": "flag"})
        self.client.post(self.item_url(KEY_1), {"verdict": "clean", "note": "x"})
        label = LetterReviewLabel.objects.get()
        self.assertEqual((label.verdict, label.note), ("clean", "x"))

    def test_saving_moves_to_the_readers_next_item_in_order(self):
        response = self.client.post(self.item_url(KEY_1), {"verdict": "clean"})
        # Item 3 belongs to reader_b, so reader_a goes from item 1 to item 2.
        self.assertRedirects(
            response, self.item_url(KEY_2), fetch_redirect_response=False
        )

    def test_saving_skips_letters_after_it_that_are_already_labeled(self):
        self.label(self.reader_a, KEY_2, "flag")
        response = self.client.post(self.item_url(KEY_1), {"verdict": "clean"})
        self.assertRedirects(
            response, self.item_url(KEY_4), fetch_redirect_response=False
        )

    def test_changing_an_earlier_label_returns_to_the_next_unlabeled(self):
        """Fixing letter 1 after labeling 1 and 2 goes on to 3, never back
        through 2 with its old mark pre-checked."""
        self.label(self.reader_a, KEY_1, "flag")
        self.label(self.reader_a, KEY_2, "flag")
        response = self.client.post(self.item_url(KEY_1), {"verdict": "clean"})
        self.assertRedirects(
            response, self.item_url(KEY_4), fetch_redirect_response=False
        )

    def test_with_nothing_unlabeled_after_it_saving_goes_to_the_next_redirect(self):
        self.label(self.reader_a, KEY_4, "flag")
        response = self.client.post(self.item_url(KEY_2), {"verdict": "clean"})
        self.assertRedirects(response, self.next_url(), fetch_redirect_response=False)

    def test_saving_the_last_item_goes_to_the_next_unlabeled(self):
        response = self.client.post(self.item_url(KEY_4), {"verdict": "clean"})
        self.assertRedirects(response, self.next_url(), fetch_redirect_response=False)

    def test_a_verdict_outside_the_three_is_refused(self):
        response = self.client.post(self.item_url(KEY_1), {"verdict": "maybe"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(LetterReviewLabel.objects.count(), 0)

    def test_a_missing_verdict_is_refused(self):
        response = self.client.post(self.item_url(KEY_1), {"note": "SYNTH"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(LetterReviewLabel.objects.count(), 0)

    def test_a_note_over_two_thousand_characters_is_refused(self):
        response = self.client.post(
            self.item_url(KEY_1), {"verdict": "flag", "note": "n" * 2001}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(LetterReviewLabel.objects.count(), 0)

    def test_the_readers_own_label_is_filled_in(self):
        self.label(self.reader_a, KEY_1, "fabricates", note="SYNTH-OWN-NOTE")
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(response, 'value="fabricates" data-shortcut="1" checked')
        self.assertContains(response, "SYNTH-OWN-NOTE")

    def test_a_labeled_letter_says_saving_replaces_the_mark(self):
        self.label(self.reader_a, KEY_1, "flag")
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(response, "You marked this Flag. Saving replaces it.")

    def test_an_unlabeled_letter_says_nothing_about_a_mark(self):
        response = self.client.get(self.item_url(KEY_1))
        self.assertNotContains(response, "You marked this")

    def test_another_readers_label_is_not_called_yours(self):
        self.label(self.reader_b, KEY_1, "flag")
        response = self.client.get(self.item_url(KEY_1))
        self.assertNotContains(response, "You marked this")

    def test_a_refused_save_still_says_what_was_saved_before(self):
        self.label(self.reader_a, KEY_1, "clean")
        response = self.client.post(self.item_url(KEY_1), {"verdict": "maybe"})
        self.assertContains(
            response, "You marked this Clean. Saving replaces it.", status_code=400
        )

    def test_the_item_page_says_which_mark_wins(self):
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(response, "pick the most serious that applies")

    def test_the_fabricates_help_names_the_input_not_an_unseen_record(self):
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(
            response, "says something the input on the left contradicts"
        )
        self.assertNotContains(response, "the record contradicts")

    def test_the_verdict_and_note_are_masked_in_error_reports(self):
        response = self.client.post(
            self.item_url(KEY_1), {"verdict": "flag", "note": "SYNTH-SECRET-NOTE"}
        )
        shown = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
        self.assertNotIn("SYNTH-SECRET-NOTE", str(shown))
        self.assertNotIn("flag", str(shown.get("verdict")))

    def test_one_reader_has_one_label_per_item(self):
        self.label(self.reader_a, KEY_1, "clean")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.label(self.reader_a, KEY_1, "flag")

    def test_two_readers_each_keep_a_label_on_a_shared_item(self):
        self.label(self.reader_b, KEY_1, "fabricates")
        self.client.post(self.item_url(KEY_1), {"verdict": "clean"})
        verdicts = dict(
            LetterReviewLabel.objects.values_list("reader__handle", "verdict")
        )
        self.assertEqual(verdicts, {"reader_a": "clean", "reader_b": "fabricates"})

    def test_previous_links_to_the_readers_previous_item(self):
        response = self.client.get(self.item_url(KEY_4))
        self.assertContains(response, self.item_url(KEY_2))
        self.assertContains(response, "Letter 3 of 3")

    def test_next_links_to_the_readers_next_item_without_saving(self):
        response = self.client.get(self.item_url(KEY_2))
        self.assertContains(response, f'href="{self.item_url(KEY_4)}">Next')
        self.assertEqual(LetterReviewLabel.objects.count(), 0)

    def test_the_last_letter_has_no_next_link(self):
        response = self.client.get(self.item_url(KEY_4))
        self.assertNotContains(response, "Next &rarr;")

    def test_the_item_page_links_to_my_letters(self):
        response = self.client.get(self.item_url(KEY_2))
        self.assertContains(response, self.mine_url())


class NextAndDoneTests(PageTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.staff_a)

    def test_next_goes_to_the_first_item_in_order(self):
        response = self.client.get(self.next_url())
        self.assertRedirects(
            response, self.item_url(KEY_1), fetch_redirect_response=False
        )

    def test_next_skips_items_the_reader_has_labeled(self):
        self.label(self.reader_a, KEY_1, "clean")
        self.label(self.reader_a, KEY_4, "clean")
        response = self.client.get(self.next_url())
        self.assertRedirects(
            response, self.item_url(KEY_2), fetch_redirect_response=False
        )

    def test_another_readers_label_does_not_count_as_done(self):
        self.label(self.reader_b, KEY_1, "clean")
        response = self.client.get(self.next_url())
        self.assertRedirects(
            response, self.item_url(KEY_1), fetch_redirect_response=False
        )

    def test_next_goes_to_done_once_everything_is_labeled(self):
        for key in (KEY_1, KEY_2, KEY_4):
            self.label(self.reader_a, key, "clean")
        response = self.client.get(self.next_url())
        self.assertRedirects(response, self.done_url(), fetch_redirect_response=False)

    def test_the_done_page_says_all_are_labeled(self):
        for key in (KEY_1, KEY_2, KEY_4):
            self.label(self.reader_a, key, "clean")
        response = self.client.get(self.done_url())
        self.assertContains(response, "You have labeled all 3")

    def test_the_done_page_links_to_my_letters(self):
        response = self.client.get(self.done_url())
        self.assertContains(response, self.mine_url())


class MyLettersTests(PageTestBase):
    """The reader's own letters in one list, the way back to any of them."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.staff_a)

    def test_it_lists_the_readers_letters_in_order_with_links(self):
        response = self.client.get(self.mine_url())
        page = response.content.decode()
        urls = [self.item_url(key) for key in (KEY_1, KEY_2, KEY_4)]
        self.assertEqual(sorted(urls, key=page.index), urls, "listed in reading order")

    def test_it_leaves_out_letters_the_reader_does_not_read(self):
        response = self.client.get(self.mine_url())
        self.assertNotContains(response, self.item(KEY_3).slug)

    def test_it_shows_each_mark_against_its_letter_number(self):
        self.label(self.reader_a, KEY_2, "fabricates")
        letters = letter_review.reader_letters(self.reader_a)
        self.assertEqual(
            [(row["place"], row["verdict"]) for row in letters],
            [(1, None), (2, "fabricates"), (3, None)],
        )

    def test_it_never_shows_another_readers_mark(self):
        self.label(self.reader_a, KEY_2, "flag")
        self.label(self.reader_b, KEY_1, "fabricates")
        response = self.client.get(self.mine_url())
        self.assertNotContains(response, "Fabricates")
        self.assertContains(response, "Flag")
        self.assertContains(response, "not yet", count=2)

    def test_it_offers_the_next_unlabeled_until_all_are_done(self):
        response = self.client.get(self.mine_url())
        self.assertContains(response, self.next_url())

    def test_it_stops_offering_the_next_unlabeled_once_all_are_done(self):
        for key in (KEY_1, KEY_2, KEY_4):
            self.label(self.reader_a, key, "clean")
        response = self.client.get(self.mine_url())
        self.assertNotContains(response, self.next_url())


class ReadingOrderTests(PageTestBase):
    """Position alone sets the reading order, never the key or the row id.

    The fixture's keys already sort backwards. Here the positions are also
    shuffled after import, so reader_a's order (KEY_2, KEY_4, KEY_1) is
    neither key order (KEY_4, KEY_2, KEY_1) nor id order (KEY_1, KEY_2,
    KEY_4).
    """

    NEW_POSITIONS = {KEY_1: 2, KEY_2: 0, KEY_3: 3, KEY_4: 1}

    def setUp(self) -> None:
        super().setUp()
        items = LetterReviewItem.objects.filter(packet=self.packet)
        for item in items:  # out of the way of the unique (packet, position)
            item.position += 100
            item.save()
        for item in items.all():
            item.position = self.NEW_POSITIONS[item.key]
            item.save()
        self.client.force_login(self.staff_a)

    def test_next_goes_to_the_first_by_position(self):
        response = self.client.get(self.next_url())
        self.assertRedirects(
            response, self.item_url(KEY_2), fetch_redirect_response=False
        )

    def test_saving_goes_on_to_the_next_by_position(self):
        response = self.client.post(self.item_url(KEY_2), {"verdict": "clean"})
        self.assertRedirects(
            response, self.item_url(KEY_4), fetch_redirect_response=False
        )

    def test_the_place_and_previous_link_follow_position(self):
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(response, "Letter 3 of 3")
        self.assertContains(response, f'href="{self.item_url(KEY_4)}">&larr; Previous')

    def test_my_letters_follows_position(self):
        letters = letter_review.reader_letters(self.reader_a)
        self.assertEqual(
            [row["slug"] for row in letters],
            [self.item(key).slug for key in (KEY_2, KEY_4, KEY_1)],
        )

    def test_the_export_follows_position(self):
        for key in (KEY_1, KEY_2, KEY_4):
            self.label(self.reader_a, key, "clean")
        data = letter_review.export_labels(self.packet)
        self.assertEqual(
            [label["key"] for label in data["labels"]], [KEY_2, KEY_4, KEY_1]
        )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class ExportTests(PageTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.label(self.reader_b, KEY_3, "flag", note="SYNTH-NOTE-B3")
        self.label(self.reader_a, KEY_1, "fabricates", note="SYNTH-NOTE-A1")
        self.label(self.reader_b, KEY_1, "clean")

    def _export(self, *extra: str):
        out, err = StringIO(), StringIO()
        call_command(
            "letter_review_export",
            "--packet",
            "synthetic-packet",
            *extra,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    def test_export_matches_the_contract_exactly(self):
        out, _ = self._export()
        data = json.loads(out)
        self.assertEqual(set(data), {"packet", "rule_version", "exported_at", "labels"})
        self.assertEqual(data["packet"], "synthetic-packet")
        self.assertEqual(data["rule_version"], "test-rule-0")
        datetime.datetime.fromisoformat(data["exported_at"])
        for label in data["labels"]:
            self.assertEqual(
                set(label), {"key", "reader", "verdict", "note", "labeled_at"}
            )
            self.assertIn(label["verdict"], ("fabricates", "flag", "clean"))
            self.assertIsNotNone(
                datetime.datetime.fromisoformat(label["labeled_at"]).tzinfo
            )
        self.assertEqual(
            [(l["key"], l["reader"], l["verdict"], l["note"]) for l in data["labels"]],
            [
                (KEY_1, "reader_a", "fabricates", "SYNTH-NOTE-A1"),
                (KEY_1, "reader_b", "clean", ""),
                (KEY_3, "reader_b", "flag", "SYNTH-NOTE-B3"),
            ],
        )

    def test_export_to_stdout_is_only_json(self):
        out, err = self._export("--out", "-")
        json.loads(out)
        self.assertIn("Exported 3 labels", err)

    def test_export_writes_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "labels.json")
            out, _ = self._export("--out", path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        self.assertEqual(len(data["labels"]), 3)
        self.assertIn("Exported 3 labels", out)

    def test_export_never_prints_letter_or_prompt_text(self):
        out, err = self._export()
        for text in ALL_BODY_TEXT + (RULE_TEXT,):
            self.assertNotIn(text[:12], out + err)

    def test_export_reads_no_letter_or_prompt(self):
        with CaptureQueriesContext(connection) as queries:
            data = letter_review.export_labels(self.packet)
        self.assertEqual(len(data["labels"]), 3)
        sql = "\n".join(query["sql"] for query in queries.captured_queries)
        self.assertIn(LetterReviewLabel._meta.db_table, sql)
        for name in ("letter", "prompt"):
            column = LetterReviewItem._meta.get_field(name).column
            self.assertNotIn(connection.ops.quote_name(column), sql)

    def test_export_of_an_unknown_packet_is_refused(self):
        with self.assertRaises(CommandError):
            call_command(
                "letter_review_export",
                "--packet",
                "no-such-packet",
                stdout=StringIO(),
                stderr=StringIO(),
            )

    def _superuser_reader(self) -> Any:
        """Make reader_a's account a superuser, as a co-founder's may be."""
        self.staff_a.is_superuser = True
        self.staff_a.save()
        self.client.force_login(self.staff_a)
        return self.staff_a

    def _finish_everyone(self) -> None:
        for key in (KEY_2, KEY_4):
            self.label(self.reader_a, key, "clean")

    def test_a_superuser_reader_cannot_export_before_everyone_finishes(self):
        self._superuser_reader()
        response = self.client.get(self.export_url())
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"SYNTH-NOTE-B3", response.content)

    def test_a_superuser_reader_who_finished_still_waits_for_the_others(self):
        self._superuser_reader()
        self._finish_everyone()
        LetterReviewLabel.objects.filter(reader=self.reader_b, item__key=KEY_3).delete()
        self.assertEqual(self.client.get(self.export_url()).status_code, 403)

    def test_a_superuser_reader_can_export_once_everyone_has_finished(self):
        self._superuser_reader()
        self._finish_everyone()
        self.assertEqual(self.client.get(self.export_url()).status_code, 200)

    def test_the_index_has_no_export_link_for_a_superuser_still_reading(self):
        self._superuser_reader()
        response = self.client.get(reverse("letter_review_index"))
        self.assertNotContains(response, self.export_url())

    def test_the_index_offers_the_export_once_everyone_has_finished(self):
        self._superuser_reader()
        self._finish_everyone()
        response = self.client.get(reverse("letter_review_index"))
        self.assertContains(response, self.export_url())

    def test_the_index_offers_a_superuser_the_export_of_a_packet_they_do_not_read(
        self,
    ):
        boss = User.objects.create_superuser(
            username="synth_super", password="pw", email="super@example.com"
        )
        self.client.force_login(boss)
        response = self.client.get(reverse("letter_review_index"))
        self.assertContains(response, self.export_url())

    def test_a_superuser_downloads_the_same_json(self):
        boss = User.objects.create_superuser(
            username="synth_super", password="pw", email="super@example.com"
        )
        self.client.force_login(boss)
        response = self.client.get(self.export_url())
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("synthetic-packet-labels.json", response["Content-Disposition"])
        page = json.loads(response.content)
        cli = json.loads(self._export()[0])
        page.pop("exported_at")
        cli.pop("exported_at")
        self.assertEqual(page, cli)


# ---------------------------------------------------------------------------
# Delete, and what deleting a staff account does
# ---------------------------------------------------------------------------


class DeleteTests(PageTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.label(self.reader_a, KEY_1, "clean")
        other = copy.deepcopy(_packet_data(packet="synthetic-other"))
        self.other = self.load_packet(other)

    def _delete(self, *extra: str):
        out, err = StringIO(), StringIO()
        call_command(
            "letter_review_delete",
            "--packet",
            "synthetic-packet",
            *extra,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    def test_delete_without_yes_deletes_nothing(self):
        with self.assertRaises(CommandError) as ctx:
            self._delete()
        self.assertIn("--yes", str(ctx.exception))
        self.assertTrue(LetterReviewPacket.objects.filter(pk=self.packet.pk).exists())

    def test_delete_removes_items_readers_and_labels(self):
        self._delete("--yes")
        self.assertFalse(LetterReviewPacket.objects.filter(pk=self.packet.pk).exists())
        self.assertFalse(
            LetterReviewItem.objects.filter(packet_id=self.packet.pk).exists()
        )
        self.assertFalse(
            LetterReviewReader.objects.filter(packet_id=self.packet.pk).exists()
        )
        self.assertEqual(LetterReviewLabel.objects.count(), 0)

    def test_delete_removes_the_assignment_rows(self):
        """Checked by the ids taken before the delete: a join through the
        item table would find nothing once the items are gone, whether or
        not the assignment rows were."""
        through = LetterReviewItem.readers.through
        item_ids = list(
            LetterReviewItem.objects.filter(packet=self.packet).values_list(
                "id", flat=True
            )
        )
        reader_ids = list(
            LetterReviewReader.objects.filter(packet=self.packet).values_list(
                "id", flat=True
            )
        )
        self.assertTrue(through.objects.filter(letterreviewitem_id__in=item_ids))
        self._delete("--yes")
        self.assertFalse(
            through.objects.filter(letterreviewitem_id__in=item_ids).exists()
        )
        self.assertFalse(
            through.objects.filter(letterreviewreader_id__in=reader_ids).exists()
        )

    def test_delete_leaves_other_packets_alone(self):
        self._delete("--yes")
        self.assertEqual(LetterReviewItem.objects.filter(packet=self.other).count(), 4)

    def test_delete_output_has_counts_and_no_letter_text(self):
        out, err = self._delete("--yes")
        self.assertIn("4 items, 2 readers and 1 labels", out)
        for text in ALL_BODY_TEXT + (RULE_TEXT,):
            self.assertNotIn(text[:12], out + err)

    def test_deleting_a_staff_account_keeps_its_labels_under_the_handle(self):
        self.staff_a.delete()
        reader = LetterReviewReader.objects.get(packet=self.packet, handle="reader_a")
        self.assertIsNone(reader.user)
        data = letter_review.export_labels(self.packet)
        self.assertEqual([l["reader"] for l in data["labels"]], ["reader_a"])


# ---------------------------------------------------------------------------
# Error reports: the ADMINS email lists every frame's variables
# ---------------------------------------------------------------------------

STAFF_VIEWS = "fighthealthinsurance.staff_views"
LETTER_REVIEW = "fighthealthinsurance.letter_review"
LETTER_REVIEW_FILES = ("staff_views.py", "letter_review.py")

# Every letter_review.py function that holds a letter, a prompt or a mark.
SENSITIVE_HELPERS = (
    "_fields",
    "_long_text",
    "parse_packet",
    "import_packet",
    "export_labels",
    "item_or_404",
    "own_label",
    "save_label",
    "next_unlabeled",
    "next_unlabeled_after",
    "neighbours",
    "reader_letters",
)


class ErrorReportTests(PageTestBase):
    """A failure on a letter review page, or in a helper holding a letter, a
    prompt or a mark, blanks the variables of its frame and every frame under
    it in the error report Django emails to ADMINS."""

    def setUp(self) -> None:
        super().setUp()
        self.label(self.reader_a, KEY_2, "fabricates", note="SYNTH-SECRET-NOTE")
        self.client.force_login(self.staff_a)

    def report_frames(
        self, target: str, call: Callable[..., Any], *args: Any
    ) -> List[Dict[str, Any]]:
        """The error report's frames when ``target`` raises during ``call``.

        Caught by hand: assertRaises drops the traceback the report reads.
        """
        error: Optional[BaseException] = None
        with patch(target, side_effect=RuntimeError("synthetic failure")):
            try:
                call(*args)
            except RuntimeError as raised:
                error = raised
        assert error is not None, f"{target} did not fail the call"
        reporter = ExceptionReporter(
            None, type(error), error, error.__traceback__, is_email=True
        )
        return reporter.get_traceback_frames()

    def assertBlankedFromLetterReviewDown(self, frames: List[Dict[str, Any]]) -> None:
        start = next(
            (
                index
                for index, frame in enumerate(frames)
                if os.path.basename(frame["filename"]) in LETTER_REVIEW_FILES
            ),
            None,
        )
        self.assertIsNotNone(start, "the failure passed through letter review code")
        blank = SafeExceptionReporterFilter.cleansed_substitute
        for frame in frames[start:]:
            for name, value in frame["vars"]:
                self.assertEqual(value, blank, f"{frame['function']}: {name}")

    def test_a_failure_on_any_page_blanks_its_frames(self):
        cases = [
            ("index", f"{STAFF_VIEWS}.render", reverse("letter_review_index")),
            ("next", f"{STAFF_VIEWS}.redirect", self.next_url()),
            # reader_letters' second query, with the reader's marks loaded.
            ("my letters, reading", f"{LETTER_REVIEW}.items_for", self.mine_url()),
            ("my letters, rendering", f"{STAFF_VIEWS}.render", self.mine_url()),
            ("done", f"{STAFF_VIEWS}.render", self.done_url()),
            ("item", f"{STAFF_VIEWS}.render", self.item_url(KEY_2)),
        ]
        for page, target, url in cases:
            with self.subTest(page=page):
                frames = self.report_frames(target, self.client.get, url)
                self.assertBlankedFromLetterReviewDown(frames)

    def test_a_failure_after_saving_a_label_blanks_its_frames(self):
        frames = self.report_frames(
            f"{STAFF_VIEWS}.redirect",
            self.client.post,
            self.item_url(KEY_2),
            {"verdict": "flag", "note": "SYNTH-SECRET-NOTE"},
        )
        self.assertBlankedFromLetterReviewDown(frames)

    def test_a_failure_exporting_blanks_its_frames(self):
        boss = User.objects.create_superuser(
            username="synth_super", password="pw", email="super@example.com"
        )
        self.client.force_login(boss)
        # After the labels JSON, notes and all, is built.
        frames = self.report_frames(
            f"{LETTER_REVIEW}.export_filename", self.client.get, self.export_url()
        )
        self.assertBlankedFromLetterReviewDown(frames)

    def test_a_helper_blanks_its_frames_whoever_calls_it(self):
        frames = self.report_frames(
            f"{LETTER_REVIEW}.items_for", letter_review.reader_letters, self.reader_a
        )
        self.assertBlankedFromLetterReviewDown(frames)

    def test_every_helper_holding_a_letter_prompt_or_mark_is_marked(self):
        for name in SENSITIVE_HELPERS:
            with self.subTest(helper=name):
                code = getattr(letter_review, name).__code__
                self.assertEqual(code.co_name, "sensitive_variables_wrapper")


# ---------------------------------------------------------------------------
# Sentry: both readers can read it, so no verdict, note or letter goes there
# ---------------------------------------------------------------------------

ITEM_ROUTE = "/timbit/help/letter_review/{packet_id}/item/{slug}"
ITEM_URL = "https://example.com/timbit/help/letter_review/7/item/AbCdEfGhIjKl"


def _letter_review_error() -> Dict[str, Any]:
    """A synthetic error event raised while saving a label."""
    return {
        "transaction": ITEM_ROUTE,
        "request": {
            "url": ITEM_URL,
            "method": "POST",
            "data": {"verdict": "flag", "note": "SYNTH-SENTRY-NOTE"},
        },
        "exception": {
            "values": [
                {
                    "type": "OperationalError",
                    "value": "synthetic",
                    "stacktrace": {
                        "frames": [
                            {
                                "module": "fighthealthinsurance.staff_views",
                                "function": "post",
                                "vars": {"note": "SYNTH-SENTRY-NOTE"},
                            },
                            {
                                "module": "django.db.backends.utils",
                                "function": "execute",
                                "vars": {"params": ["flag", "SYNTH-SENTRY-NOTE"]},
                            },
                        ]
                    },
                }
            ]
        },
    }


class SentryTests(SimpleTestCase):
    def test_a_letter_review_transaction_is_dropped(self):
        event = {
            "type": "transaction",
            "transaction": ITEM_ROUTE,
            "transaction_info": {"source": "route"},
            "spans": [
                {
                    "op": "template.render",
                    "data": {"context": {"selected": "flag", "note": "SYNTH"}},
                }
            ],
        }
        self.assertIsNone(before_send_transaction_filter(event, {}))

    def test_a_letter_review_transaction_is_dropped_by_its_url_too(self):
        event = {
            "type": "transaction",
            "transaction": "fighthealthinsurance.staff_views.LetterReviewItemView",
            "transaction_info": {"source": "component"},
            "request": {"url": ITEM_URL, "data": {"verdict": "flag"}},
        }
        self.assertIsNone(before_send_transaction_filter(event, {}))

    def test_other_staff_transactions_are_kept(self):
        for name in ("/timbit/help/", "/timbit/help/letter_reviewer/"):
            with self.subTest(name=name):
                event = {
                    "type": "transaction",
                    "transaction": name,
                    "transaction_info": {"source": "route"},
                }
                self.assertIs(before_send_transaction_filter(event, {}), event)

    def test_a_letter_review_error_loses_its_request_body(self):
        kept = before_send_filter(_letter_review_error(), {})
        self.assertNotIn("data", kept["request"])
        self.assertEqual(kept["request"]["url"], ITEM_URL)

    def test_a_letter_review_error_loses_every_frames_variables(self):
        kept = before_send_filter(_letter_review_error(), {})
        frames = kept["exception"]["values"][0]["stacktrace"]["frames"]
        self.assertEqual([f["function"] for f in frames], ["post", "execute"])
        self.assertNotIn("SYNTH-SENTRY-NOTE", json.dumps(kept))

    def test_a_letter_review_error_is_found_by_url_alone(self):
        event = _letter_review_error()
        del event["transaction"]
        kept = before_send_filter(event, {})
        self.assertNotIn("SYNTH-SENTRY-NOTE", json.dumps(kept))

    def test_an_error_elsewhere_keeps_its_request_body(self):
        event = _letter_review_error()
        event["transaction"] = "/timbit/help/"
        event["request"]["url"] = "https://example.com/timbit/help/"
        kept = before_send_filter(event, {})
        self.assertIn("data", kept["request"])

    def test_a_malformed_request_never_raises(self):
        for request in (None, "[Filtered]", {"url": 5}, {"url": "http://[bad"}):
            with self.subTest(request=request):
                event = {"transaction": 5, "request": request}
                self.assertIs(before_send_filter(event, {}), event)
                self.assertIs(before_send_transaction_filter(event, {}), event)
