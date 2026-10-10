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
from django.utils.html import escape
from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter

from fighthealthinsurance import forms as core_forms
from fighthealthinsurance import letter_review, letter_review_highlight
from fighthealthinsurance.models import (
    LetterReviewLabelsFrozen,
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
            [(row["place"], row["mark"]) for row in letters],
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


class FrozenLabelTests(PageTestBase):
    """Once every reader is done the export can show readers each other's
    marks, so from then on each blind verdict stays as it was given."""

    def setUp(self) -> None:
        super().setUp()
        self.label(self.reader_b, KEY_3, "flag", note="SYNTH-NOTE-B3")
        self.label(self.reader_a, KEY_1, "fabricates", note="SYNTH-NOTE-A1")
        self.label(self.reader_b, KEY_1, "clean")

    def _finish_everyone(self) -> None:
        for key in (KEY_2, KEY_4):
            self.label(self.reader_a, key, "clean")

    def _exported(self) -> List[Dict[str, Any]]:
        return letter_review.export_labels(self.packet)["labels"]

    def test_a_reader_can_correct_a_mark_before_everyone_finishes(self):
        self.client.force_login(self.staff_a)
        response = self.client.post(self.item_url(KEY_1), {"verdict": "clean", "note": ""})
        self.assertEqual(response.status_code, 302)
        label = LetterReviewLabel.objects.get(reader=self.reader_a, item__key=KEY_1)
        self.assertEqual(label.verdict, "clean")

    def test_a_change_after_everyone_finishes_is_refused_and_the_export_is_unchanged(
        self,
    ):
        self._finish_everyone()
        before = self._exported()
        self.client.force_login(self.staff_a)
        response = self.client.post(self.item_url(KEY_1), {"verdict": "clean", "note": "later"})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "the marks are final", status_code=409)
        self.assertNotContains(response, 'id="lr-form"', status_code=409)
        self.assertEqual(self._exported(), before)

    def test_the_page_shows_the_mark_without_a_form_once_everyone_finishes(self):
        self._finish_everyone()
        self.client.force_login(self.staff_a)
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(response, "the marks are final")
        self.assertNotContains(response, 'id="lr-form"')

    def test_the_model_refuses_a_single_label_change_or_removal(self):
        self._finish_everyone()
        before = self._exported()
        label = LetterReviewLabel.objects.get(reader=self.reader_a, item__key=KEY_1)
        label.verdict = "clean"
        with self.assertRaises(LetterReviewLabelsFrozen):
            label.save()
        with self.assertRaises(LetterReviewLabelsFrozen):
            letter_review.save_label(self.reader_a, self.item(KEY_1), "clean", "")
        with self.assertRaises(LetterReviewLabelsFrozen):
            LetterReviewLabel.objects.get(pk=label.pk).delete()
        self.assertEqual(self._exported(), before)

    def test_the_check_reads_the_stored_packet_not_the_item_in_memory(self):
        self._finish_everyone()
        before = self._exported()
        other = self.load_packet(_packet_data(packet="synthetic-open-packet"))
        label = LetterReviewLabel.objects.get(reader=self.reader_a, item__key=KEY_1)
        label.item = other.items.first()
        label.verdict = "clean"
        with self.assertRaises(LetterReviewLabelsFrozen):
            label.save(update_fields=["verdict"])
        self.assertEqual(self._exported(), before)

    def test_a_stored_label_is_locked_before_its_packet(self):
        """Its own row first, so a concurrent move of the same label can't
        change which packet is checked."""
        from django.db.models import QuerySet

        order: List[str] = []
        real = QuerySet.select_for_update

        def spy(qs, *args, **kwargs):
            order.append(qs.model.__name__)
            return real(qs, *args, **kwargs)

        label = self.label(self.reader_a, KEY_2, "clean")
        with patch.object(QuerySet, "select_for_update", spy):
            label.verdict = "flag"
            label.save()
            label.delete()
        self.assertEqual(
            order,
            ["LetterReviewLabel", "LetterReviewPacket"] * 2,
        )

    def test_a_label_cannot_be_moved_onto_a_finished_packet(self):
        other = self.load_packet(_packet_data(packet="synthetic-open-packet"))
        moving = LetterReviewLabel.objects.filter(item__packet=other).first() or LetterReviewLabel.objects.create(
            item=other.items.first(),
            reader=other.readers.first(),
            verdict="clean",
        )
        self._finish_everyone()
        before = self._exported()
        moving.item = self.item(KEY_2)
        with self.assertRaises(LetterReviewLabelsFrozen):
            moving.save()
        self.assertEqual(self._exported(), before)

    def test_a_save_that_loses_the_race_to_the_last_reader_is_a_409(self):
        """The last reader finishes between the page's check and this save."""
        real = letter_review.labels_frozen
        calls = {"n": 0}

        def finishes_after_the_page_check(packet):
            calls["n"] += 1
            if calls["n"] == 1:
                self._finish_everyone()
                return False
            return real(packet)

        self.client.force_login(self.staff_a)
        with patch.object(letter_review, "labels_frozen", finishes_after_the_page_check):
            response = self.client.post(
                self.item_url(KEY_1), {"verdict": "clean", "note": ""}
            )
        self.assertEqual(response.status_code, 409)
        label = LetterReviewLabel.objects.get(reader=self.reader_a, item__key=KEY_1)
        self.assertEqual(label.verdict, "fabricates")

    def test_every_label_write_locks_its_packet_inside_a_transaction(self):
        """The save that finishes a packet and an edit to one of its labels
        take the same packet lock, so neither can slip between the other's
        check and write."""
        from django.db.models import QuerySet

        seen: List[Any] = []
        real = QuerySet.select_for_update

        def spy(qs, *args, **kwargs):
            if qs.model is LetterReviewPacket:
                seen.append(connection.in_atomic_block)
            return real(qs, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", spy):
            label = self.label(self.reader_a, KEY_2, "clean")
            label.verdict = "flag"
            label.save()
            label.delete()
        self.assertEqual(seen, [True, True, True])


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


BINARY = {"form": "binary", "rule_version": "2026-10-10-binary-v1"}


def _answers(**given: str) -> Dict[str, str]:
    """A POST of answers: Q1 and Q2 no unless given, the rest skipped."""
    data = {"invents_or_contradicts": "no", "unsupported_history": "no"}
    data.update(given)
    return data


class BinaryImportTests(StaffUsersMixin, TestCase):
    def setUp(self) -> None:
        self.make_users()

    def test_a_binary_packet_keeps_its_form(self):
        packet = self.load_packet(_packet_data(**BINARY))
        self.assertEqual(
            (packet.form, packet.rule_version), ("binary", "2026-10-10-binary-v1")
        )

    def test_a_packet_without_a_form_is_a_verdict_packet(self):
        self.assertEqual(self.load_packet().form, "verdict")

    def test_an_unknown_form_is_refused(self):
        with self.assertRaises(letter_review.PacketError):
            letter_review.parse_packet(_packet_data(form="stars"))

    def test_the_form_field_matches_the_questions(self):
        fields = core_forms.LetterReviewAnswersForm().fields
        self.assertEqual(
            [(name, f.required) for name, f in fields.items() if name != "note"],
            [(q.field, q.required) for q in letter_review.QUESTIONS],
        )


class BinaryPageTests(PageTestBase):
    """A binary packet's letters: yes or no questions and the reading aids."""

    def setUp(self) -> None:
        self.make_users()
        self.packet = self.load_packet(_packet_data(**BINARY))
        self.reader_a = LetterReviewReader.objects.get(handle="reader_a")
        self.reader_b = LetterReviewReader.objects.get(handle="reader_b")
        self.client.force_login(self.staff_a)

    def stored(self, key: str) -> LetterReviewLabel:
        return LetterReviewLabel.objects.get(item=self.item(key), reader=self.reader_a)

    def test_the_page_asks_every_question_and_lets_the_last_three_be_skipped(self):
        response = self.client.get(self.item_url(KEY_1))
        self.assertTemplateUsed(response, "letter_review_item_binary.html")
        for question in letter_review.QUESTIONS:
            self.assertContains(response, escape(question.text))
        self.assertContains(response, "> Skip</label>", count=3)
        self.assertContains(response, 'value="yes" aria-describedby="lr-help-invents_or_contradicts" required')

    def test_the_first_two_answers_are_required(self):
        response = self.client.post(
            self.item_url(KEY_1), {"invents_or_contradicts": "yes"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Answer the first two questions", status_code=400)
        self.assertFalse(LetterReviewLabel.objects.exists())

    def test_answers_are_stored_with_skips_as_null(self):
        self.client.post(
            self.item_url(KEY_1),
            _answers(
                invents_or_contradicts="yes",
                argues_against_reason="no",
                ready_to_send="",
                note="a note",
            ),
        )
        label = self.stored(KEY_1)
        self.assertEqual(
            (
                label.invents_or_contradicts,
                label.unsupported_history,
                label.argues_against_reason,
                label.specific_medical_necessity,
                label.ready_to_send,
                label.verdict,
                label.note,
            ),
            (True, False, False, None, None, None, "a note"),
        )

    def test_a_saved_answer_comes_back_checked(self):
        self.client.post(self.item_url(KEY_1), _answers(unsupported_history="yes"))
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(response, 'name="unsupported_history" value="yes" aria-describedby="lr-help-unsupported_history" checked')

    def test_answers_without_the_first_two_are_refused_below_the_form(self):
        with self.assertRaises(ValueError):
            letter_review.save_answers(
                self.reader_a, self.item(KEY_1), {"invents_or_contradicts": True}, ""
            )
        self.assertFalse(LetterReviewLabel.objects.exists())

    def test_a_letter_is_done_once_the_first_two_are_answered(self):
        self.assertEqual(letter_review.progress(self.reader_a), (0, 3))
        self.client.post(self.item_url(KEY_1), _answers())
        self.assertEqual(letter_review.progress(self.reader_a), (1, 3))
        self.assertEqual(letter_review.next_unlabeled(self.reader_a), self.item(KEY_2))

    def test_the_packet_finishes_and_freezes_when_every_reader_is_done(self):
        for key in (KEY_1, KEY_2, KEY_4):
            self.client.post(self.item_url(key), _answers())
        self.client.force_login(self.staff_b)
        for key in (KEY_1, KEY_3):
            self.client.post(self.item_url(key), _answers())
        self.assertTrue(letter_review.packet_finished(self.packet))
        response = self.client.post(
            self.item_url(KEY_1), _answers(invents_or_contradicts="yes")
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(
            LetterReviewLabel.objects.get(
                item=self.item(KEY_1), reader=self.reader_b
            ).invents_or_contradicts
        )

    def test_the_export_carries_each_answer(self):
        self.client.post(
            self.item_url(KEY_1),
            _answers(invents_or_contradicts="yes", ready_to_send="no"),
        )
        data = letter_review.export_labels(self.packet)
        self.assertEqual(data["form"], "binary")
        self.assertEqual(data["rule_version"], "2026-10-10-binary-v1")
        [label] = data["labels"]
        self.assertEqual(
            (label["key"], label["reader"], label["answers"]),
            (
                KEY_1,
                "reader_a",
                {
                    "invents_or_contradicts": True,
                    "unsupported_history": False,
                    "argues_against_reason": None,
                    "specific_medical_necessity": None,
                    "ready_to_send": False,
                },
            ),
        )
        self.assertNotIn("verdict", label)

    def test_my_letters_shows_the_first_two_answers(self):
        self.client.post(self.item_url(KEY_2), _answers(invents_or_contradicts="yes"))
        response = self.client.get(self.mine_url())
        self.assertContains(response, "invents: yes, history: no")
        self.assertContains(response, "not yet", count=2)

    def test_on_a_phone_the_way_to_the_letter_is_outside_the_folding_input(self):
        page = self.client.get(self.item_url(KEY_1)).content.decode()
        input_step = page.index('id="lr-step-0"')
        self.assertGreater(
            page.index('data-step-to="1"'), page.index("</details>", input_step)
        )

    def test_steps_announce_themselves_and_their_headings_take_focus(self):
        response = self.client.get(self.item_url(KEY_1))
        self.assertContains(response, 'id="lr-step-status" class="lr-visually-hidden" aria-live="polite"')
        self.assertContains(response, 'id="lr-letter-heading" tabindex="-1" data-step-heading')
        self.assertContains(response, 'id="lr-answers-heading" tabindex="-1" data-step-heading')

    def test_the_reading_aids_are_on_the_page(self):
        response = self.client.get(self.item_url(KEY_1))
        for text in (
            'data-size="21px"',
            'id="lr-hl-toggle"',
            'id="lr-focus-toggle"',
            "What gets highlighted",
            escape(letter_review_highlight.RULES[0]),
            "<progress",
        ):
            self.assertContains(response, text)


class BinaryHighlightPageTests(StaffUsersMixin, TestCase):
    def test_specifics_not_in_the_input_are_marked_and_fill_ins_styled(self):
        self.make_users()
        data = _packet_data(**BINARY)
        data["items"][0]["prompt"] = "SYNTH-PROMPT: billed $1,200 on 03/04/2026."
        data["items"][0]["letter"] = (
            "SYNTH-LETTER: The $1,200.00 bill.\n\nThe 14 visits on [Date]."
        )
        packet = self.load_packet(data)
        self.client.force_login(self.staff_a)
        item = LetterReviewItem.objects.get(packet=packet, key=KEY_1)
        response = self.client.get(
            reverse("letter_review_item", args=[packet.pk, item.slug])
        )
        self.assertContains(response, '<mark class="lr-hl">14</mark>')
        self.assertNotContains(response, '<mark class="lr-hl">$1,200.00</mark>')
        self.assertContains(response, '<span class="lr-fill">[Date]</span>')
        self.assertContains(response, "<p>SYNTH-LETTER: The $1,200.00 bill.</p>")


# An input laid out like the appeal prompt a writer sees: the patient context
# (intake Q&A answers and history), the details and plan sections, then the
# denial last. Each section has a marker found nowhere else.
SECTIONED_PROMPT = (
    "System context: When answering the following question you can use the "
    "patient context SYNTH-QA-SECTION Tried physical therapy:Yes, 14 weeks\n"
    "SYNTH-HISTORY-SECTION Seen by Dr. Mary Walker since 2019.\n\n"
    "TASK: Write a health insurance appeal for the denial letter below.\n\n"
    "DETAILS TO INCLUDE (use these values exactly as given):\n"
    '- Answers from the patient\'s intake questions (work these into the '
    'appeal): {"SYNTH-QA-JSON": "Tried physical therapy: Yes"}\n\n'
    "PLAN DETAILS: SYNTH-PLAN-SECTION the plan covers made-up test widgets.\n\n"
    "DENIAL LETTER:\nSYNTH-DENIAL-SECTION the test widget is not covered."
)


class FullInputTests(StaffUsersMixin, TestCase):
    """The input pane shows the whole prompt, every context section and not
    only the denial, on both forms of packet."""

    def page(self, letter: str = LETTER_1, **form: str) -> str:
        self.make_users()
        data = _packet_data(**form)
        data["items"][0]["prompt"] = SECTIONED_PROMPT
        data["items"][0]["letter"] = letter
        packet = self.load_packet(data)
        self.client.force_login(self.staff_a)
        item = LetterReviewItem.objects.get(packet=packet, key=KEY_1)
        return self.client.get(
            reverse("letter_review_item", args=[packet.pk, item.slug])
        ).content.decode()

    def test_a_binary_page_shows_the_whole_prompt_in_the_input_pane(self):
        page = self.page(**BINARY)
        self.assertIn(f'<div class="lr-input">{escape(SECTIONED_PROMPT)}</div>', page)

    def test_a_verdict_page_shows_the_whole_prompt_in_the_input_pane(self):
        page = self.page()
        self.assertIn(f'<div class="lr-input">{escape(SECTIONED_PROMPT)}</div>', page)

    def test_specifics_given_only_in_the_qa_or_history_are_not_highlighted(self):
        page = self.page(
            letter="SYNTH-LETTER: After 14 weeks of therapy Mary Walker, "
            "treating since 2019, recommends the widget.",
            **BINARY,
        )
        self.assertNotIn('<mark class="lr-hl">', page)


class BinaryBreakTests(StaffUsersMixin, TestCase):
    """A break page after every ten letters a reader finishes."""

    def setUp(self) -> None:
        self.make_users()
        data = _packet_data(**BINARY, readers=["reader_a"])
        data["items"] = [
            {
                "key": f"break-item-{n:02d}",
                "prompt": f"SYNTH-PROMPT-{n}",
                "letter": f"SYNTH-LETTER-{n}",
                "readers": ["reader_a"],
            }
            for n in range(12)
        ]
        parsed = letter_review.parse_packet(data)
        self.packet = letter_review.import_packet(
            parsed, {"reader_a": self.staff_a}
        ).packet
        self.client.force_login(self.staff_a)
        self.items = list(LetterReviewItem.objects.filter(packet=self.packet))

    def save(self, n: int):
        url = reverse(
            "letter_review_item", args=[self.packet.pk, self.items[n].slug]
        )
        return self.client.post(url, _answers())

    def test_the_tenth_letter_done_goes_to_the_break(self):
        for n in range(9):
            self.save(n)
        response = self.save(9)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.url.startswith(
                reverse("letter_review_break", args=[self.packet.pk])
            )
        )
        page = self.client.get(response.url)
        self.assertContains(page, "Time for a short break")
        self.assertContains(page, "10 of 12 done")

    def test_changing_a_letter_already_done_does_not(self):
        for n in range(10):
            self.save(n)
        response = self.save(3)
        self.assertRedirects(
            response,
            reverse("letter_review_item", args=[self.packet.pk, self.items[10].slug]),
        )

    def test_after_the_break_the_next_letter_follows_the_one_just_saved(self):
        # Letter 0 skipped: ten done by letter 10, then on to letter 11, not
        # back to letter 0.
        for n in range(1, 10):
            self.save(n)
        response = self.save(10)
        self.assertEqual(
            response.url,
            reverse("letter_review_break", args=[self.packet.pk])
            + f"?after={self.items[10].slug}",
        )
        page = self.client.get(response.url)
        self.assertContains(
            page,
            reverse("letter_review_item", args=[self.packet.pk, self.items[11].slug]),
        )

    def test_a_break_link_to_someone_elses_letter_is_ignored(self):
        page = self.client.get(
            reverse("letter_review_break", args=[self.packet.pk]) + "?after=nope"
        )
        self.assertContains(
            page, reverse("letter_review_next", args=[self.packet.pk])
        )

    def test_a_verdict_packet_has_no_break(self):
        self.assertEqual(letter_review.BREAK_EVERY, 10)
        packet = LetterReviewPacket.objects.get(pk=self.packet.pk)
        packet.form = "verdict"
        packet.save()
        for n in range(9):
            LetterReviewLabel.objects.create(
                item=self.items[n],
                reader=LetterReviewReader.objects.get(handle="reader_a"),
                verdict="clean",
            )
        url = reverse("letter_review_item", args=[packet.pk, self.items[9].slug])
        response = self.client.post(url, {"verdict": "clean"})
        self.assertRedirects(
            response,
            reverse("letter_review_item", args=[packet.pk, self.items[10].slug]),
        )


class HighlightTests(SimpleTestCase):
    """The highlight rules, on synthetic text."""

    @staticmethod
    def marked(letter: str, prompt: str, kind: str = "unmatched") -> List[str]:
        return [
            seg.text
            for para in letter_review_highlight.paragraphs(letter, prompt)
            for seg in para
            if seg.kind == kind
        ]

    def test_specifics_absent_from_the_input_are_marked(self):
        letter = (
            "On March 4, 2026 Rebecca Lee Crumpler paid $250 for 3 visits, "
            "claim ZX9-4471, per 29 C.F.R. § 2560.503-1 and doi 10.1000/xyz123."
        )
        self.assertEqual(
            self.marked(letter, "nothing relevant"),
            [
                "March 4, 2026",
                "Rebecca Lee Crumpler",
                "$250",
                "ZX9-4471",
                "29 C.F.R. § 2560.503-1",
                "10.1000/xyz123",
            ],
        )

    def test_the_same_text_in_the_input_is_not_marked(self):
        letter = "Rebecca Lee Crumpler was billed $1,200.00 under claim ZX9-4471."
        prompt = "member rebecca lee  crumpler, claim zx9-4471, billed $1200"
        self.assertEqual(self.marked(letter, prompt), [])

    def test_a_date_written_another_way_is_still_marked(self):
        self.assertEqual(
            self.marked("Seen March 4, 2026.", "seen 03/04/2026"), ["March 4, 2026"]
        )

    def test_single_digits_and_letter_words_are_not_marked(self):
        letter = "Dear Appeals Department,\n\n1. Medical Necessity is clear."
        self.assertEqual(self.marked(letter, ""), [])

    def test_a_number_inside_a_longer_one_is_not_support(self):
        self.assertEqual(self.marked("Paid 120 today.", "account 51203"), ["120"])

    def test_part_of_a_longer_amount_or_code_is_not_support(self):
        self.assertEqual(self.marked("Billed $120.", "billed $120.50"), ["$120"])
        self.assertEqual(self.marked("Paid $250.", "claim AB250"), ["$250"])
        self.assertEqual(
            self.marked("Claim AB12345.", "claim AB123456"), ["AB12345"]
        )
        self.assertEqual(
            self.marked("Claim AB12345.", "claim AB12345-7"), ["AB12345"]
        )

    def test_amounts_with_zero_cents_match_on_either_side(self):
        self.assertEqual(self.marked("Billed $1,500.00.", "billed $1,500.00"), [])
        self.assertEqual(self.marked("Billed $120.", "billed $120.00"), [])
        self.assertEqual(self.marked("Billed $120.", "billed $120.05"), ["$120"])

    def test_an_amount_at_either_end_of_a_range_is_support(self):
        self.assertEqual(
            self.marked("Billed $1,200.", "billed $1,200-1,500"), []
        )

    def test_a_citation_in_brackets_matches_the_input(self):
        prompt = "The plan must follow 29 C.F.R. § 2560.503-1 and § 1557."
        letter = "A fair review (29 C.F.R. § 2560.503-1), not a form one (see § 1557)."
        self.assertEqual(self.marked(letter, prompt), [])

    def test_a_possessive_name_matches_the_input(self):
        prompt = "Physician: Mary Walker. Insurer: Blue Cross Blue Shield."
        letter = "Mary Walker's note answers Blue Cross Blue Shield's denial."
        self.assertEqual(self.marked(letter, prompt), [])

    def test_a_number_inside_a_date_id_or_code_is_not_support(self):
        for prompt in (
            "seen 12/03/2024",
            "seen 2024-03-12",
            "code ICD-10",
            "SSN 123-45-6789",
        ):
            with self.subTest(prompt=prompt):
                number = "45" if "SSN" in prompt else "10" if "ICD" in prompt else "12"
                self.assertEqual(
                    self.marked(f"For {number} weeks.", prompt), [number]
                )
        self.assertEqual(
            self.marked("Claim 123-456 was denied.", "Claim 456-123"), ["123-456"]
        )

    def test_the_same_number_date_or_id_is_support(self):
        for letter, prompt in (
            ("For 12 weeks.", "for 12 weeks"),
            ("Seen 12/03/2024.", "seen 12/03/2024"),
            ("ID 123-45-6789.", "SSN 123-45-6789"),
            ("Paid $100 then $200.", "between $100-$200"),
            ("Up 10.0% at 4,500.00 on 2.0 visits.", "up 10.0% at $4,500.00, 2.0 visits"),
        ):
            with self.subTest(letter=letter):
                self.assertEqual(self.marked(letter, prompt), [])

    def test_more_date_forms_are_dates(self):
        self.assertEqual(self.marked("Seen 5 March 2024.", ""), ["5 March 2024"])
        self.assertEqual(self.marked("Seen March 5 again.", ""), ["March 5"])
        self.assertEqual(
            self.marked("Seen March 5 again.", "seen march 5, 2024"), []
        )

    def test_short_codes_are_codes_and_ordinals_are_not(self):
        letter = "Codes I10 and M545 on the 2nd visit."
        self.assertEqual(self.marked(letter, ""), ["I10", "M545"])
        self.assertEqual(self.marked(letter, "codes i10, m545"), [])

    def test_names_in_any_script_case_or_with_a_hyphen(self):
        self.assertEqual(self.marked("María López wrote.", ""), ["María López"])
        self.assertEqual(self.marked("María López wrote.", "maría lópez"), [])
        self.assertEqual(self.marked("JOHN SMITH signed.", "John Smith"), [])
        self.assertEqual(
            self.marked("Mary Smith-Jones signed.", "Mary Smith"),
            ["Mary Smith-Jones"],
        )
        self.assertEqual(
            self.marked("Patient Rebecca Lee Crumpler.", "rebecca lee crumpler"), []
        )

    def test_a_heading_in_capitals_is_not_a_name(self):
        self.assertEqual(self.marked("APPEAL OF DENIAL\n\nText.", ""), [])

    def test_windows_line_endings_still_split_paragraphs(self):
        paras = letter_review_highlight.paragraphs("One.\r\n\r\nTwo.", "")
        self.assertEqual(len(paras), 2)

    def test_fill_ins_have_their_own_kind(self):
        self.assertEqual(
            self.marked("Sincerely, [Your Name] on [Date]", "", "fill-in"),
            ["[Your Name]", "[Date]"],
        )

    def test_paragraphs_split_on_blank_lines_and_keep_all_the_text(self):
        letter = "First line\nsame paragraph.\n\n\nSecond paragraph 42."
        paras = letter_review_highlight.paragraphs(letter, "")
        self.assertEqual(
            ["".join(seg.text for seg in para) for para in paras],
            ["First line\nsame paragraph.", "Second paragraph 42."],
        )

    def test_the_same_letter_always_gets_the_same_marks(self):
        letter = "Dr. Mary Walker saw her on 2026-01-02 for 45 minutes."
        self.assertEqual(
            letter_review_highlight.paragraphs(letter, "x"),
            letter_review_highlight.paragraphs(letter, "x"),
        )


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
