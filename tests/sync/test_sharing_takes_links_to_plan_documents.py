"""People can share links to their plan's documents, and nothing fetches them.

The share page takes up to five links to public pages, one a line, as well
as or instead of files: a share needs at least one file or one link. Each
link is checked as text only: https or http, a website's name, no user name
or password, no IP address or localhost, at most 2,000 characters. Blank
lines and repeats are dropped. Each link is one PolicyDocument row with no
file, marked a link, in the same batch and with the same plan facts as the
share's files. The link is stored encrypted, with a keyed hash beside it so
repeats can be found. No reading or sweep picks one up, and nothing on the server
fetches it: staff open it by hand from the admin. Links count against no
file budget. Remove-my-data deletes a link as it deletes a file.
"""

import datetime
import hashlib
import time
from decimal import Decimal
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from asgiref.sync import async_to_sync
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import TextField
from django.db.models.functions import Cast
from django.forms.models import model_to_dict
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance import consent, shared_plan_documents, temporal_client
from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.activities import plan_documents_sweep as sweep_activities
from fighthealthinsurance.activities import shared_plan_documents as activities
from fighthealthinsurance.chat_forms import (
    LINK_NO_SITE_MESSAGE,
    LINK_NOT_A_LINK_MESSAGE,
    LINK_NOT_PUBLIC_MESSAGE,
    LINK_SCHEME_MESSAGE,
    LINK_SIGN_IN_MESSAGE,
    LINK_TOO_LONG_MESSAGE,
    MAX_SHARED_LINK_CHARS,
    SharePlanDocumentsForm,
    clean_shared_link,
)
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import Denial, PolicyDocument, SharedPlanDocumentsDay
from fighthealthinsurance.shared_plan_documents import (
    PLAN_FACT_FIELDS,
    TRY_AGAIN,
    link_fingerprint,
    render_token,
    save_shared_documents,
)

User = get_user_model()

LINK = "https://benefits.crumpler-clinic.example.org/2026/plan-documents"
LINK_B = "http://www.crumplermutual.example.com/plans/gold-ppo"
EMAIL = "rebecca.crumpler@example.com"
OTHER_EMAIL = "mary.mahoney@example.com"
TXT = b"Summary of Benefits, Crumpler Mutual Gold PPO. Overall deductible: $1,500"
DASHES = ("\u2014", "\u2013")
LINKS = PolicyDocument.SOURCE_SHARED_LINK


def txt(name="sbc.txt", content=TXT):
    return SimpleUploadedFile(name, content, content_type="text/plain")


def stored(pk, field="shared_link"):
    """What the database holds in ``field``, before anything decrypts it."""
    return (
        PolicyDocument.objects.filter(pk=pk)
        .annotate(raw=Cast(field, output_field=TextField()))
        .values_list("raw", flat=True)
        .get()
    )


@contextmanager
def no_network(calls):
    """Fail, and note it in ``calls``, if anything opens a connection, looks
    up a name, or sends a request with any HTTP client we have."""

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("something tried to reach the network")

    async def arefuse(*args, **kwargs):
        refuse(*args, **kwargs)

    with ExitStack() as stack:
        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.create_connection",
            "socket.getaddrinfo",
            "urllib.request.urlopen",
            "requests.sessions.Session.request",
            "httpx.Client.send",
        ):
            stack.enter_context(patch(target, refuse))
        for target in ("httpx.AsyncClient.send", "aiohttp.ClientSession._request"):
            stack.enter_context(patch(target, arefuse))
        yield calls


class _LinkTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("share_plan_documents")

    def tearDown(self):
        # Rows roll back with the test; their stored files would not.
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def share(self, files=(), links="", rendered=None, **fields):
        data = {
            "agree": "on",
            "plan_links": links,
            "rendered": (
                render_token(at=time.time() - 60) if rendered is None else rendered
            ),
        }
        if files:
            data["plan_documents"] = list(files)
        data.update(fields)
        return self.client.post(self.url, data)

    def link_rows(self):
        # The stored link is ciphertext, so the database can't order by it.
        rows = PolicyDocument.objects.filter(source=LINKS)
        return sorted(rows, key=lambda row: row.shared_link)


# ---- Each link is checked as text --------------------------------------------


class EachLinkIsCheckedAsTextTest(TestCase):
    REFUSED = {
        "javascript:alert(document.cookie)": LINK_SCHEME_MESSAGE,
        "JavaScript:alert(1)": LINK_SCHEME_MESSAGE,
        "data:text/html,<script>alert(1)</script>": LINK_SCHEME_MESSAGE,
        "file:///etc/passwd": LINK_SCHEME_MESSAGE,
        "ftp://files.crumplermutual.example.com/sbc.pdf": LINK_SCHEME_MESSAGE,
        "www.crumplermutual.example.com/sbc.pdf": LINK_SCHEME_MESSAGE,
        "//crumplermutual.example.com/sbc.pdf": LINK_SCHEME_MESSAGE,
        "https://rebecca:hunter2@crumplermutual.example.com/sbc": LINK_SIGN_IN_MESSAGE,
        "https://rebecca@crumplermutual.example.com/sbc": LINK_SIGN_IN_MESSAGE,
        "https://:hunter2@crumplermutual.example.com/sbc": LINK_SIGN_IN_MESSAGE,
        "https://": LINK_NO_SITE_MESSAGE,
        "https:///plans/sbc.pdf": LINK_NO_SITE_MESSAGE,
        "http://192.168.1.10/sbc.pdf": LINK_NOT_PUBLIC_MESSAGE,
        "http://169.254.169.254/latest/meta-data": LINK_NOT_PUBLIC_MESSAGE,
        "http://[::1]/sbc.pdf": LINK_NOT_PUBLIC_MESSAGE,
        "http://2130706433/sbc.pdf": LINK_NOT_PUBLIC_MESSAGE,
        "http://0x7f.1/sbc.pdf": LINK_NOT_PUBLIC_MESSAGE,
        "http://localhost:8000/admin": LINK_NOT_PUBLIC_MESSAGE,
        "http://LOCALHOST./admin": LINK_NOT_PUBLIC_MESSAGE,
        "http://plans.localhost/sbc.pdf": LINK_NOT_PUBLIC_MESSAGE,
        "https://crumpler mutual.example.com/sbc": LINK_NOT_A_LINK_MESSAGE,
        "https://crumplermutual.example.com/"
        + "a" * MAX_SHARED_LINK_CHARS: LINK_TOO_LONG_MESSAGE,
    }
    TAKEN = (
        "https://www.crumplermutual.example.com/plans/gold-ppo/sbc-2026.pdf",
        "http://benefits.crumpler-clinic.example.org/2026",
        "HTTPS://WWW.CRUMPLERMUTUAL.EXAMPLE.COM/SBC",
        "https://crumplermutual.example.com/plans?year=2026&tier=gold#costs",
        "https://crumplermutual.example.com:8443/plans",
        "https://posts.example.com/@crumpler/our-plan",
    )

    def test_each_kind_of_bad_link_is_refused_with_its_reason(self):
        for link, message in self.REFUSED.items():
            with self.subTest(link=link[:60]):
                with self.assertRaises(ValidationError) as raised:
                    clean_shared_link(link)
                self.assertEqual(raised.exception.messages, [message])

    def test_public_web_links_are_taken_as_given(self):
        for link in self.TAKEN:
            with self.subTest(link=link):
                self.assertEqual(clean_shared_link(f"  {link} "), link)

    def test_a_link_of_exactly_the_longest_length_is_taken(self):
        prefix = "https://crumplermutual.example.com/"
        link = prefix + "a" * (MAX_SHARED_LINK_CHARS - len(prefix))
        self.assertEqual(len(link), MAX_SHARED_LINK_CHARS)
        self.assertEqual(clean_shared_link(link), link)


class TheLinksBoxIsReadLineByLineTest(TestCase):
    def form(self, links, **data):
        return SharePlanDocumentsForm({"agree": "on", "plan_links": links, **data}, {})

    def test_lines_are_trimmed_and_blank_lines_and_repeats_dropped(self):
        form = self.form(f"\n  {LINK}  \n\n{LINK}\r\n{LINK_B}\n   \n")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["plan_links"], [LINK, LINK_B])

    def test_five_links_are_taken(self):
        links = [f"https://plans.example.com/{n}" for n in range(5)]
        form = self.form("\n".join(links))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["plan_links"], links)

    def test_a_sixth_link_is_refused(self):
        links = [f"https://plans.example.com/{n}" for n in range(6)]
        form = self.form("\n".join(links))
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["plan_links"], [SharePlanDocumentsForm.too_many_links_message()]
        )

    def test_repeats_do_not_count_toward_the_five(self):
        links = [f"https://plans.example.com/{n}" for n in range(5)]
        form = self.form("\n".join(links + links[:2]))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(form.cleaned_data["plan_links"]), 5)

    def test_each_bad_link_is_named_with_its_reason(self):
        long_link = "javascript:" + "a" * 100
        form = self.form(f"{LINK}\nfile:///etc/passwd\n{long_link}")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["plan_links"],
            [
                f"file:///etc/passwd: {LINK_SCHEME_MESSAGE}",
                f"{long_link[:57]}...: {LINK_SCHEME_MESSAGE}",
            ],
        )

    def test_the_cap_is_five(self):
        self.assertEqual(SharePlanDocumentsForm.MAX_LINKS, 5)


# ---- Sharing links, files, or both -------------------------------------------


class ALinkOnlyShareTest(_LinkTestCase):
    def test_links_alone_store_one_row_each_marked_a_link(self):
        response = self.share(links=f"{LINK}\n{LINK_B}")
        self.assertRedirects(response, self.url + "?shared=links")
        rows = self.link_rows()
        self.assertEqual([row.shared_link for row in rows], sorted([LINK, LINK_B]))
        self.assertEqual(PolicyDocument.objects.count(), 2)
        for row in rows:
            with self.subTest(link=row.shared_link):
                self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_LINK)
                self.assertFalse(row.document_enc)
                self.assertEqual((row.filename, row.fingerprint), ("", ""))
                self.assertEqual(row.shared_consent_version, consent.SHARING_VERSION)
                self.assertIsNotNone(row.shared_at)
        self.assertEqual(len({row.share_batch for row in rows}), 1)

    def test_a_link_only_share_starts_no_reading(self):
        with (
            patch.object(temporal_client, "dispatch_shared_plan_documents") as dispatch,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.share(links=LINK)
        self.assertEqual(len(self.link_rows()), 1)
        dispatch.assert_not_called()

    def test_links_use_no_file_budget(self):
        self.share(links=f"{LINK}\n{LINK_B}")
        self.assertEqual(shared_plan_documents.shared_today(), 0)
        self.assertFalse(
            SharedPlanDocumentsDay.objects.filter(
                name__in=(SharedPlanDocumentsDay.FILES, SharedPlanDocumentsDay.OPT_IN_FILES)
            ).exists()
        )
        used = self.client.session["shared_plan_documents_today"]
        self.assertEqual((used["files"], used["bytes"]), (0, 0))

    @override_settings(SHARED_PLAN_DOCUMENTS_DAILY_FILES=1)
    def test_links_are_taken_when_todays_file_budget_is_full(self):
        self.share([txt()])
        refused = self.share([txt("spd.txt")])
        self.assertEqual(refused.status_code, 200)
        self.assertEqual(PolicyDocument.objects.count(), 1)
        response = self.share(links=LINK)
        self.assertRedirects(response, self.url + "?shared=links")
        self.assertEqual(len(self.link_rows()), 1)


class ALinkIsStoredEncryptedTest(_LinkTestCase):
    """The link is encrypted with the uploaded files' key, and read back as
    the link. Each save encrypts differently, so a keyed hash of the link,
    the same however the page's address was typed, finds repeats."""

    def test_the_database_holds_the_link_encrypted(self):
        self.share(links=LINK)
        (row,) = self.link_rows()
        raw = stored(row.pk)
        self.assertTrue(raw)
        self.assertNotIn("crumpler-clinic", raw)
        self.assertEqual(Cryptographer.decrypted(raw.encode()).decode(), LINK)
        self.assertEqual(row.shared_link, LINK)

    def test_each_save_encrypts_differently_and_the_hash_finds_the_repeat(self):
        self.share(links=LINK)
        self.share(links=LINK)
        first, second = PolicyDocument.objects.filter(source=LINKS)
        self.assertNotEqual(stored(first.pk), stored(second.pk))
        self.assertEqual(first.shared_link_fingerprint, link_fingerprint(LINK))
        self.assertEqual(second.shared_link_fingerprint, link_fingerprint(LINK))

    def test_the_hash_is_keyed_and_is_never_the_link(self):
        digest = link_fingerprint(LINK)
        self.assertEqual(len(digest), 64)
        self.assertNotIn("crumpler", digest)
        self.assertNotEqual(digest, hashlib.sha256(LINK.encode()).hexdigest())
        with override_settings(SECRET_KEY="another-key-for-these-tests"):
            self.assertNotEqual(link_fingerprint(LINK), digest)

    def test_the_hash_reads_one_page_typed_differently_as_the_same(self):
        for link in (
            LINK + "/",
            "HTTPS://Benefits.Crumpler-Clinic.example.org/2026/plan-documents",
            LINK + "#page=2",
            f"  {LINK}  ",
        ):
            with self.subTest(link=link):
                self.assertEqual(link_fingerprint(link), link_fingerprint(LINK))
        for link in (
            LINK.replace("2026", "2027"),
            LINK + "?plan=gold",
            LINK.replace("plan-documents", "Plan-Documents"),
            LINK_B,
        ):
            with self.subTest(link=link):
                self.assertNotEqual(link_fingerprint(link), link_fingerprint(LINK))

    def test_a_value_that_will_not_decrypt_reads_as_empty(self):
        self.share(links=LINK)
        (row,) = self.link_rows()
        table = PolicyDocument._meta.db_table
        pk = PolicyDocument._meta.pk.get_db_prep_value(row.pk, connection)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET shared_link = %s WHERE id = %s",
                ["not-a-token", pk],
            )
        row.refresh_from_db()
        self.assertEqual(row.shared_link, "")


class AShareWithFilesAndLinksTest(_LinkTestCase):
    FACTS = {
        "plan_category": "employer_erisa",
        "insurer_name": "Crumpler Health Mutual",
        "employer_name": "Lee Crumpler Clinic",
        "plan_year": "2026",
        "state": "MA",
        "premium_amount": "250",
        "premium_period": "monthly",
        "who_is_covered": "family",
        "email": EMAIL,
    }

    def test_links_carry_the_same_batch_facts_and_agreement_as_the_files(self):
        response = self.share([txt()], links=LINK, **self.FACTS)
        self.assertRedirects(response, self.url + "?shared=both")
        file_row = PolicyDocument.objects.get(source=PolicyDocument.SOURCE_SHARED)
        (link_row,) = self.link_rows()
        self.assertEqual(link_row.shared_link, LINK)
        shared_with = PLAN_FACT_FIELDS + (
            "share_batch",
            "shared_consent_version",
            "shared_at",
            "hashed_email",
            "document_type",
        )
        for name in shared_with:
            with self.subTest(field=name):
                self.assertEqual(getattr(link_row, name), getattr(file_row, name))
        self.assertEqual(link_row.hashed_email, Denial.get_hashed_email(EMAIL))
        self.assertEqual(link_row.premium_amount, Decimal("250"))

    def test_only_the_files_are_read_and_counted(self):
        with (
            patch.object(temporal_client, "dispatch_shared_plan_documents") as dispatch,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.share([txt()], links=f"{LINK}\n{LINK_B}")
        file_row = PolicyDocument.objects.get(source=PolicyDocument.SOURCE_SHARED)
        dispatch.assert_called_once_with(str(file_row.share_batch))
        self.assertEqual(file_row.processing_status, PolicyDocument.PROCESSING_RECEIVED)
        self.assertEqual(shared_plan_documents.shared_today(), 1)
        self.assertEqual(
            self.client.session["shared_plan_documents_today"]["files"], 1
        )

    def test_files_alone_still_share_as_before(self):
        response = self.share([txt()])
        self.assertRedirects(response, self.url + "?shared=1")
        row = PolicyDocument.objects.get()
        self.assertEqual(row.source, PolicyDocument.SOURCE_SHARED)
        self.assertEqual(row.shared_link, "")
        self.assertTrue(row.document_enc)

    def test_a_bad_link_stops_the_whole_share(self):
        response = self.share([txt()], links=f"{LINK}\njavascript:alert(1)")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PolicyDocument.objects.exists())
        self.assertContains(response, LINK_SCHEME_MESSAGE)


class TheThankYouNoteIsShortTest(_LinkTestCase):
    """One short paragraph: what was received, that we publish what we learn,
    and a link to share more. It never says an AI model reads links."""

    def thanks(self, shared):
        response = self.client.get(self.url, {"shared": shared})
        soup = BeautifulSoup(response.content, "html.parser")
        note = soup.find(id="share-thanks")
        return note.get_text(" ", strip=True) if note else None

    def test_after_files_it_names_documents(self):
        self.assertEqual(
            self.thanks("1"),
            "Thank you. We have your documents. We'll publish what we learn on "
            "this site. Have more? Share more documents",
        )

    def test_after_links_it_names_links_and_no_ai_model(self):
        note = self.thanks("links")
        self.assertIn("Thank you. We have your links.", note)
        self.assertNotIn("AI models", note)

    def test_after_both_it_names_each(self):
        self.assertIn(
            "Thank you. We have your documents and links.", self.thanks("both")
        )

    def test_the_note_is_one_paragraph(self):
        soup = BeautifulSoup(
            self.client.get(self.url, {"shared": "both"}).content, "html.parser"
        )
        self.assertEqual(len(soup.find(id="share-thanks").find_all("p")), 1)

    def test_an_unknown_value_shows_the_form_not_a_note(self):
        self.assertIsNone(self.thanks("yes"))

    def test_a_share_lands_on_the_note_for_what_it_held(self):
        cases = (
            ({"files": [txt()]}, "We have your documents."),
            ({"links": LINK}, "We have your links."),
            ({"files": [txt("spd.txt")], "links": LINK_B}, "We have your documents and links."),
        )
        for share, words in cases:
            with self.subTest(words=words):
                response = self.share(**share)
                page = self.client.get(response["Location"])
                self.assertContains(page, words)

    def test_what_happens_says_links_are_opened_by_hand_and_not_read_by_ai(self):
        page = BeautifulSoup(self.client.get(self.url).content, "html.parser")
        text = page.find(id="what-happens").get_text(" ", strip=True)
        self.assertIn("We store what you share encrypted, files and links alike.", text)
        self.assertIn(
            "Our team opens links by hand. We never fetch them automatically.", text
        )
        for sentence in text.split(". "):
            if "link" in sentence:
                with self.subTest(sentence=sentence):
                    self.assertNotIn("AI", sentence)

    def test_a_failed_save_keeps_no_link_rows(self):
        with patch(
            "fighthealthinsurance.shared_plan_documents.fingerprint",
            side_effect=OSError,
        ):
            response = self.share([txt()], links=LINK)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PolicyDocument.objects.exists())


class NothingToShareTest(_LinkTestCase):
    def test_no_file_and_no_link_is_refused_with_a_clear_reason(self):
        response = self.share(links="  \n \n")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PolicyDocument.objects.exists())
        soup = BeautifulSoup(response.content, "html.parser")
        note = soup.find(id="id_plan_documents_error").get_text(strip=True)
        self.assertEqual(note, SharePlanDocumentsForm.NOTHING_TO_SHARE)
        files = soup.find("input", attrs={"name": "plan_documents"})
        self.assertIn("id_plan_documents_error", files["aria-describedby"].split())
        self.assertIn(
            SharePlanDocumentsForm.NOTHING_TO_SHARE,
            soup.find(id="share-errors").get_text(" ", strip=True),
        )

    def test_a_bad_link_alone_says_what_is_wrong_with_the_link(self):
        response = self.share(links="javascript:alert(1)")
        self.assertFalse(PolicyDocument.objects.exists())
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertEqual(
            soup.find(id="id_plan_links_error").get_text(strip=True),
            f"javascript:alert(1): {LINK_SCHEME_MESSAGE}",
        )
        self.assertEqual(soup.find(id="id_plan_documents_error").get_text(), "")
        self.assertNotIn(
            SharePlanDocumentsForm.NOTHING_TO_SHARE,
            soup.find(id="share-errors").get_text(" ", strip=True),
        )

    def test_asking_again_keeps_the_links_typed(self):
        response = self.share(links=f"{LINK}\n{LINK_B}", rendered="")
        self.assertContains(response, TRY_AGAIN)
        self.assertFalse(PolicyDocument.objects.exists())
        box = BeautifulSoup(response.content, "html.parser").find(id="id_plan_links")
        self.assertEqual(box.get_text().strip(), f"{LINK}\n{LINK_B}")

    def test_the_links_are_kept_out_of_error_reports(self):
        response = self.share(links=LINK)
        self.assertIn("plan_links", response.wsgi_request.sensitive_post_parameters)


# ---- The page ------------------------------------------------------------------


class ThePageOffersALinksBoxTest(_LinkTestCase):
    def setUp(self):
        super().setUp()
        self.soup = BeautifulSoup(self.client.get(self.url).content, "html.parser")
        self.box = self.soup.find("textarea", attrs={"name": "plan_links"})

    def test_the_box_sits_under_the_file_upload_and_before_the_plan_questions(self):
        files = self.soup.find("input", attrs={"name": "plan_documents"})
        self.assertIs(files.find_next("textarea"), self.box)
        self.assertIsNotNone(self.box.find_next("select", attrs={"name": "plan_category"}))

    def test_the_box_folds_under_one_line_that_says_links_are_optional(self):
        fold = self.box.find_parent("details")
        self.assertEqual(fold["id"], "share-links")
        self.assertFalse(fold.has_attr("open"))
        self.assertEqual(
            fold.find("summary").get_text(" ", strip=True),
            "No files? Share links to your plan's documents instead (optional)",
        )

    def test_links_kept_after_a_refusal_open_the_fold(self):
        response = self.share(links=LINK, rendered="not-a-token")
        soup = BeautifulSoup(response.content, "html.parser")
        fold = soup.find("details", id="share-links")
        self.assertTrue(fold.has_attr("open"))
        self.assertEqual(fold.find("textarea").get_text(strip=True), LINK)

    def test_a_bad_link_opens_the_fold_to_show_its_error(self):
        response = self.share([txt()], links="javascript:alert(1)")
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertTrue(soup.find("details", id="share-links").has_attr("open"))

    def test_the_box_is_labelled_optional_and_says_which_links(self):
        label = self.soup.find("label", attrs={"for": self.box["id"]})
        self.assertEqual(label.get_text(strip=True), "Links to your plan's documents")
        self.assertFalse(self.box.has_attr("required"))
        hint = self.soup.find(id=f"{self.box['id']}_helptext")
        self.assertIn(hint["id"], self.box["aria-describedby"].split())
        self.assertEqual(
            hint.get_text(strip=True),
            "One link per line, up to 5. Use public pages only, like an "
            "employer's benefits site or the plan's page on an insurer's site, "
            "not links that need you to sign in.",
        )

    def test_the_script_says_there_is_nothing_to_share_in_the_servers_words(self):
        form = self.soup.find("form", id="share-plan-documents-form")
        self.assertEqual(form["data-nothing"], SharePlanDocumentsForm.NOTHING_TO_SHARE)

    def test_the_new_words_use_no_long_dashes(self):
        form = SharePlanDocumentsForm()
        words = [
            str(form.fields["plan_links"].label),
            str(form.fields["plan_links"].help_text),
            SharePlanDocumentsForm.NOTHING_TO_SHARE,
            SharePlanDocumentsForm.too_many_links_message(),
            LINK_TOO_LONG_MESSAGE,
            LINK_SCHEME_MESSAGE,
            LINK_SIGN_IN_MESSAGE,
            LINK_NO_SITE_MESSAGE,
            LINK_NOT_PUBLIC_MESSAGE,
            LINK_NOT_A_LINK_MESSAGE,
            consent.SHARING_TERMS,
            dict(PolicyDocument.PROCESSING_STATUS_CHOICES)[
                PolicyDocument.PROCESSING_LINK
            ],
        ]
        for text in words:
            for dash in DASHES:
                with self.subTest(text=text[:40], dash=dash):
                    self.assertNotIn(dash, text)


# ---- Remove-my-data --------------------------------------------------------------


class RemovingMyDataDeletesLinksTest(TestCase):
    def share(self, details, email=EMAIL):
        return save_shared_documents(
            [],
            source=PolicyDocument.SOURCE_SHARED,
            details=details,
            hashed_email=Denial.get_hashed_email(email),
            links=[LINK],
        )[0]

    def remove(self, email=EMAIL):
        with self.captureOnCommitCallbacks(execute=True):
            RemoveDataHelper.remove_data_for_email(email)

    def test_a_link_with_plan_facts_is_deleted_and_the_facts_kept(self):
        row = self.share({"insurer_name": "Crumpler Health Mutual", "plan_year": 2026})
        self.remove()
        row.refresh_from_db()
        self.assertEqual(row.shared_link, "")
        self.assertEqual((stored(row.pk), row.shared_link_fingerprint), ("", ""))
        self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_REMOVED)
        self.assertIsNone(row.hashed_email)
        self.assertEqual((row.insurer_name, row.plan_year), ("Crumpler Health Mutual", 2026))
        for name, value in model_to_dict(row).items():
            with self.subTest(field=name):
                self.assertNotIn("crumpler-clinic", str(value))

    def test_a_link_with_no_plan_facts_is_deleted_whole(self):
        row = self.share({})
        self.remove()
        self.assertFalse(PolicyDocument.objects.filter(pk=row.pk).exists())

    def test_other_peoples_links_are_untouched(self):
        mine = self.share({"insurer_name": "Crumpler Health Mutual"})
        theirs = self.share({"insurer_name": "Crumpler Health Mutual"}, OTHER_EMAIL)
        self.remove()
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.shared_link, "")
        self.assertEqual(theirs.shared_link, LINK)
        self.assertEqual(theirs.processing_status, PolicyDocument.PROCESSING_LINK)


# ---- Nothing reads or fetches a link -----------------------------------------------


class NothingReadsOrFetchesALinkTest(TransactionTestCase):
    """The reading's and the sweep's own activities, run for real against
    the database, with every way out to the network refusing."""

    def setUp(self):
        self.calls: list = []
        self.enterContext(no_network(self.calls))
        self.addCleanup(lambda: self.assertEqual(self.calls, []))

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def share(self, files=(), links=(LINK, LINK_B)):
        with patch.object(temporal_client, "dispatch_shared_plan_documents"):
            return save_shared_documents(
                list(files),
                source=PolicyDocument.SOURCE_SHARED,
                details={"insurer_name": "Crumpler Health Mutual"},
                hashed_email=None,
                links=list(links),
            )

    def listed(self, row):
        return async_to_sync(activities.list_shared_plan_documents)(
            str(row.share_batch)
        )

    def waiting(self):
        return async_to_sync(sweep_activities.find_waiting_share_batches)(15, 50)

    def age(self, rows, **changes):
        PolicyDocument.objects.filter(pk__in=[row.pk for row in rows]).update(
            shared_at=timezone.now() - datetime.timedelta(hours=2), **changes
        )

    def assertUntouched(self, row, link):
        row.refresh_from_db()
        self.assertEqual(row.shared_link, link)
        self.assertIsNone(row.processed_at)
        self.assertEqual(row.cleaned_text, "")

    def test_a_share_of_links_has_nothing_to_read(self):
        rows = self.share()
        self.assertEqual(self.listed(rows[0]), [])

    def test_a_shares_reading_lists_its_files_and_not_its_links(self):
        file_row, *link_rows = self.share([txt()])
        self.assertEqual(self.listed(file_row), [str(file_row.pk)])

    def test_reading_a_link_row_by_id_reads_and_writes_nothing(self):
        (row, _) = self.share()
        for activity_fn in (
            activities.process_shared_plan_document,
            activities.mark_shared_plan_document_unreadable,
        ):
            with self.subTest(activity=activity_fn.__name__):
                self.assertEqual(async_to_sync(activity_fn)(str(row.pk)), processing.GONE)
                self.assertUntouched(row, row.shared_link)
                self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_LINK)

    def test_the_sweep_never_finds_a_share_of_links(self):
        self.age(self.share())
        self.assertEqual(self.waiting(), [])

    def test_a_link_row_set_to_waiting_is_still_never_read(self):
        rows = self.share()
        self.age(rows, processing_status=PolicyDocument.PROCESSING_RECEIVED)
        self.assertEqual(self.waiting(), [])
        self.assertEqual(self.listed(rows[0]), [])
        self.assertEqual(
            async_to_sync(activities.process_shared_plan_document)(str(rows[0].pk)),
            processing.GONE,
        )
        self.assertUntouched(rows[0], rows[0].shared_link)

    def test_a_mixed_share_has_its_file_read_and_its_links_left_alone(self):
        file_row, *link_rows = self.share([txt()])
        self.age([file_row, *link_rows])
        self.assertEqual(self.waiting(), [str(file_row.share_batch)])
        for document_id in self.listed(file_row):
            async_to_sync(activities.process_shared_plan_document)(document_id)
        file_row.refresh_from_db()
        self.assertEqual(file_row.processing_status, PolicyDocument.PROCESSING_READY)
        for row in link_rows:
            self.assertUntouched(row, row.shared_link)
            self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_LINK)
        self.assertEqual(self.waiting(), [])


# ---- What staff see ----------------------------------------------------------------


class StaffSeeSharedLinksTest(TestCase):
    def setUp(self):
        self.calls: list = []
        self.enterContext(no_network(self.calls))
        admin = User.objects.create_superuser(
            username="susan_la_flesche_picotte", password="pw-for-tests"
        )
        self.client = Client()
        self.client.force_login(admin)
        with patch.object(temporal_client, "dispatch_shared_plan_documents"):
            self.file_row, self.link_row = save_shared_documents(
                [txt()],
                source=PolicyDocument.SOURCE_SHARED,
                details={},
                hashed_email=None,
                links=[LINK],
            )

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def page(self, name, *args):
        response = self.client.get(
            reverse(f"admin:fighthealthinsurance_policydocument_{name}", args=args)
        )
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.content, "html.parser")

    def opening_links(self, soup):
        return soup.find_all("a", href=LINK)

    def test_the_change_page_shows_the_link_as_text_and_a_safe_link(self):
        soup = self.page("change", self.link_row.pk)
        (opening,) = self.opening_links(soup)
        self.assertEqual(opening["target"], "_blank")
        self.assertEqual(opening["rel"], ["noopener", "noreferrer"])
        self.assertIn(LINK, opening.parent.get_text(" ", strip=True))
        self.assertIsNone(soup.find(attrs={"name": "shared_link"}))
        self.assertEqual(self.calls, [])

    def test_the_list_shows_links_next_to_shared_files(self):
        soup = self.page("changelist")
        (opening,) = self.opening_links(soup)
        self.assertEqual(opening["rel"], ["noopener", "noreferrer"])
        text_view = reverse("staff_policy_document_text", kwargs={"pk": self.file_row.pk})
        self.assertIsNotNone(soup.find("a", href=text_view))
        counts = soup.find(id="shared-document-counts").get_text(" ", strip=True)
        self.assertIn("Shared for research: 1 file, 1 different document.", counts)
        self.assertIn("1 link to plan documents.", counts)
        self.assertEqual(self.calls, [])

    def test_a_stored_link_that_is_not_a_web_link_is_shown_only_as_text(self):
        PolicyDocument.objects.filter(pk=self.link_row.pk).update(
            shared_link="javascript:alert(document.cookie)"
        )
        soup = self.page("change", self.link_row.pk)
        self.assertEqual(soup.find_all("a", href=lambda h: h and "javascript" in h), [])
        self.assertIn("javascript:alert(document.cookie)", soup.get_text())

    def test_read_again_never_sets_a_link_to_waiting(self):
        PolicyDocument.objects.filter(pk=self.link_row.pk).update(
            processing_status=PolicyDocument.PROCESSING_UNREADABLE
        )
        self.client.post(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"action": "read_again", "_selected_action": [str(self.link_row.pk)]},
        )
        self.link_row.refresh_from_db()
        self.assertEqual(
            self.link_row.processing_status, PolicyDocument.PROCESSING_UNREADABLE
        )
