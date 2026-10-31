"""The appeal's plan documents step keeps one copy of each file.

Each file stored on an appeal carries a keyed fingerprint of its contents
(PlanDocuments.fingerprint), and the step skips a file the appeal already
holds, or one picked twice in the same upload, so a second click, a replayed
post or going back and picking the same file again adds nothing. Two saves
of the same file at once meet the table's constraint: the second is skipped
and the file it wrote is deleted. The fingerprint is keyed under a key of its
own, so it matches neither a plain hash of the file nor the fingerprint of
the shared copy the opt-in box makes. Rows stored before the fingerprint, and
rows the last release saves, have none and are never matched; the appeal's
reader reads identical files once whatever their rows say.
"""

import asyncio
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pymupdf
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from fighthealthinsurance import shared_plan_documents
from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper
from fighthealthinsurance.models import Denial, PlanDocuments, PolicyDocument
from fighthealthinsurance.views import plan_document_count

EMAIL = "rebecca.crumpler@test-fhi.com"
SEMI_SEKRET = "sekret-for-one-copy-of-each-file"
PDF = b"%PDF-1.4 Summary of Benefits for the Crumpler Cooperative" + b" x" * 100
TXT = b"Summary Plan Description, Crumpler Cooperative, plan year 2026."


def pdf(name="SBC.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def txt(name="SPD.txt", content=TXT):
    return SimpleUploadedFile(name, content, content_type="text/plain")


def _delete_stored_files():
    # Rows roll back with a test; their stored files would not.
    for doc in PlanDocuments.objects.all():
        if doc.plan_document_enc:
            doc.plan_document_enc.delete(save=False)
    for doc in PolicyDocument.objects.all():
        if doc.document_enc:
            doc.document_enc.delete(save=False)


class _OneCopyTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.denial = self.appeal(EMAIL, SEMI_SEKRET)

    def tearDown(self):
        _delete_stored_files()

    def appeal(self, email, semi_sekret):
        return Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(email),
            semi_sekret=semi_sekret,
        )

    def post(self, files, denial=None, email=EMAIL, semi_sekret=SEMI_SEKRET, **extra):
        denial = denial or self.denial
        data = {
            "denial_id": denial.denial_id,
            "email": email,
            "semi_sekret": semi_sekret,
            "plan_documents": files,
        }
        data.update(extra)
        response = self.client.post(reverse("dvc"), data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "entity_extract.html")
        return response

    def stored(self, denial=None):
        return PlanDocuments.objects.filter(denial=denial or self.denial)


class EachFileIsKeptOnceTest(_OneCopyTestCase):
    def test_a_file_picked_twice_in_one_upload_is_kept_once(self):
        self.post([pdf(), pdf()])
        self.assertEqual(self.stored().count(), 1)

    def test_the_same_file_in_a_later_upload_adds_nothing(self):
        self.post([pdf(), txt()])
        self.post([pdf()])
        self.assertEqual(self.stored().count(), 2)

    def test_a_replayed_upload_adds_nothing(self):
        self.post([pdf(), txt()])
        self.post([pdf(), txt()])
        self.assertEqual(self.stored().count(), 2)

    def test_a_new_file_in_a_later_upload_is_added(self):
        self.post([pdf()])
        self.post([pdf(), txt()])
        self.assertEqual(self.stored().count(), 2)

    def test_the_same_contents_under_another_name_is_the_same_file(self):
        self.post([pdf("SBC.pdf")])
        self.post([pdf("SBC (1).pdf")])
        self.assertEqual(self.stored().count(), 1)

    def test_another_appeal_keeps_its_own_copy(self):
        other = self.appeal("sarah.fraser@test-fhi.com", "another-sekret")
        self.post([pdf()])
        self.post(
            [pdf()],
            denial=other,
            email="sarah.fraser@test-fhi.com",
            semi_sekret="another-sekret",
        )
        self.assertEqual(self.stored().count(), 1)
        self.assertEqual(self.stored(other).count(), 1)

    def test_every_stored_file_has_a_fingerprint(self):
        self.post([pdf(), txt()])
        self.assertNotIn("", set(self.stored().values_list("fingerprint", flat=True)))

    def test_the_count_on_the_page_counts_each_file_once(self):
        self.post([pdf(), txt()])
        self.post([pdf(), txt()])
        self.assertEqual(plan_document_count(self.denial.denial_id), 2)


class TheFingerprintIsKeyedTest(_OneCopyTestCase):
    def test_it_is_not_a_plain_hash_of_the_file(self):
        self.post([pdf()])
        self.assertNotEqual(
            self.stored().get().fingerprint, hashlib.sha256(PDF).hexdigest()
        )

    def test_it_cannot_be_joined_to_the_shared_copy(self):
        self.post([pdf()], share_for_research="on")
        copy = PolicyDocument.objects.get(source=PolicyDocument.SOURCE_SHARED_APPEAL)
        self.assertTrue(copy.fingerprint)
        self.assertNotEqual(self.stored().get().fingerprint, copy.fingerprint)

    def test_a_repeat_with_the_box_ticked_is_still_shared(self):
        # The appeal keeps one copy; sharing is its own choice, and the
        # shared side counts repeats by its own fingerprint.
        self.post([pdf()])
        self.post([pdf()], share_for_research="on")
        self.assertEqual(self.stored().count(), 1)
        self.assertEqual(
            PolicyDocument.objects.filter(
                source=PolicyDocument.SOURCE_SHARED_APPEAL
            ).count(),
            1,
        )


    def test_a_replayed_upload_with_the_box_ticked_is_shared_once(self):
        """A replay adds nothing to the appeal, so it must not store a second
        research copy or spend the share budgets again."""
        self.post([pdf()], share_for_research="on")
        self.post([pdf()], share_for_research="on")
        self.assertEqual(
            PolicyDocument.objects.filter(
                source=PolicyDocument.SOURCE_SHARED_APPEAL
            ).count(),
            1,
        )
        self.assertEqual(
            shared_plan_documents.shared_today(PolicyDocument.SOURCE_SHARED_APPEAL),
            1,
        )

    def test_a_shared_copy_that_could_not_be_read_is_shared_again(self):
        """Unreadable can mean the reads ran out of retries, so the same file
        again is worth a second try."""
        self.post([pdf()], share_for_research="on")
        PolicyDocument.objects.filter(
            source=PolicyDocument.SOURCE_SHARED_APPEAL
        ).update(processing_status=PolicyDocument.PROCESSING_UNREADABLE)
        self.post([pdf()], share_for_research="on")
        self.assertEqual(
            PolicyDocument.objects.filter(
                source=PolicyDocument.SOURCE_SHARED_APPEAL
            ).count(),
            2,
        )

    def test_a_copy_that_fails_the_closer_look_does_not_stand_in_for_one_that_passes(
        self,
    ):
        from django.core.exceptions import ValidationError

        with patch(
            "fighthealthinsurance.chat_forms.validate_shared_document",
            side_effect=[ValidationError("not this one"), None],
        ):
            self.post(
                [pdf(name="first.pdf"), pdf(name="second.pdf")],
                share_for_research="on",
            )
        self.assertEqual(
            PolicyDocument.objects.filter(
                source=PolicyDocument.SOURCE_SHARED_APPEAL
            ).count(),
            1,
        )


    def test_the_copy_that_passes_the_closer_look_is_the_one_shared(self):
        from django.core.exceptions import ValidationError

        second = pdf(name="second.pdf", content=PDF + b" second")
        expected = shared_plan_documents.fingerprint(second)
        with patch(
            "fighthealthinsurance.chat_forms.validate_shared_document",
            side_effect=[ValidationError("not this one"), None],
        ):
            self.post(
                [pdf(name="first.pdf", content=PDF + b" first"), second],
                share_for_research="on",
            )
        shared = PolicyDocument.objects.get(
            source=PolicyDocument.SOURCE_SHARED_APPEAL
        )
        self.assertEqual(shared.fingerprint, expected)


class TwoSavesAtOnceTest(_OneCopyTestCase):
    """The second of two saves that both missed the other's row."""

    def setUp(self):
        super().setUp()
        dirs = [tempfile.TemporaryDirectory() for _ in range(2)]
        for d in dirs:
            self.addCleanup(d.cleanup)
        self.dirs = [Path(d.name) for d in dirs]
        storage = PlanDocuments._meta.get_field("plan_document_enc").storage
        patcher = patch.object(
            storage, "backends", [FileSystemStorage(location=d) for d in self.dirs]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored_files(self):
        return sorted(
            str(path.relative_to(d))
            for d in self.dirs
            for path in d.rglob("*")
            if path.is_file()
        )

    def test_the_second_is_skipped_and_its_file_deleted(self):
        self.post([pdf()])
        first = self.stored_files()
        self.assertTrue(first)
        # As if the other save committed after this one looked.
        with patch.object(
            DenialCreatorHelper,
            "_held_plan_document_fingerprints",
            return_value=set(),
        ):
            self.post([pdf()])
        self.assertEqual(self.stored().count(), 1)
        self.assertEqual(self.stored_files(), first)

    def test_the_rest_of_the_upload_is_still_kept(self):
        self.post([pdf()])
        with patch.object(
            DenialCreatorHelper,
            "_held_plan_document_fingerprints",
            return_value=set(),
        ):
            self.post([pdf(), txt()])
        self.assertEqual(self.stored().count(), 2)


class TheLastReleaseCanStillSaveTest(_OneCopyTestCase):
    """The release before the fingerprint saves rows without it."""

    def save_the_old_way(self):
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {quote(PlanDocuments._meta.db_table)} "
                f"({quote('plan_document')}, {quote('plan_document_enc')}, "
                f"{quote('denial_id')}) VALUES (%s, %s, %s)",
                [None, None, self.denial.denial_id],
            )

    def test_its_rows_take_a_blank_fingerprint(self):
        self.save_the_old_way()
        self.assertEqual(self.stored().get().fingerprint, "")

    def test_any_number_of_its_rows_fit_on_one_appeal(self):
        self.save_the_old_way()
        self.save_the_old_way()
        self.assertEqual(self.stored().count(), 2)

    def test_a_row_without_a_fingerprint_never_blocks_a_new_file(self):
        self.save_the_old_way()
        self.post([pdf()])
        self.assertEqual(self.stored().count(), 2)


class ThePageSaysARepeatIsNotAddedTest(_OneCopyTestCase):
    def test_the_note_beside_the_count(self):
        self.post([pdf()])
        response = self.client.post(
            reverse("hh"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": "",
            },
        )
        self.assertContains(response, "1 plan document already added")
        self.assertContains(response, "A file that's already here isn't added again.")


def _pdf_bytes(text):
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


class TheReaderReadsIdenticalFilesOnceTest(TransactionTestCase):
    """Rows from before the fingerprint can hold the same file twice.

    A TransactionTestCase: the reader's async queries run on another thread,
    which only sees committed rows (as in test_ml_plan_doc_helper.py)."""

    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email="hash:one-copy-reader",
            denial_text="Your claim has been denied.",
        )

    def tearDown(self):
        _delete_stored_files()

    def store(self, content, name="plan.pdf"):
        row = PlanDocuments(denial=self.denial)
        row.plan_document_enc.save(name, ContentFile(content), save=True)

    def read(self):
        return asyncio.run(MLPlanDocHelper.load_plan_documents(self.denial.denial_id))

    def test_the_same_file_twice_is_read_once(self):
        sbc = _pdf_bytes("Summary of Benefits: prior authorization required.")
        self.store(sbc)
        self.store(sbc, name="plan-again.pdf")
        documents = self.read()
        self.assertEqual([doc.name for doc in documents], ["Plan document 1"])

    def test_different_files_are_each_read(self):
        self.store(_pdf_bytes("Summary of Benefits: prior authorization required."))
        self.store(_pdf_bytes("Evidence of Coverage: appeals within 180 days."))
        self.assertEqual(
            [doc.name for doc in self.read()], ["Plan document 1", "Plan document 2"]
        )
