"""The import_plan_documents command: a bundle of public plan documents
(plan_document_bundle) lands in PublicPlanDocument, one row per
document_key.

The bundles here are invented and the documents are public data, so none
of it is PHI; the command still never echoes a document's text, and a
sentinel planted in the pages proves it.
"""

import datetime
import hashlib
import json
import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.management.color import color_style
from django.test import TestCase
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    InsurancePlan,
    PlanDocumentMatch,
    PublicPlanDocument,
    _stored_file_in_use,
)
from fighthealthinsurance.plan_document_bundle import MAX_TEXT_CHARS
from tests.pdf_fixtures import make_pdf_bytes

#: The example negative regex of the bundle contract: the other region and
#: the other products, written so that "medical" is left alone.
NORCAL_NEGATIVE_REGEX = (
    r"\bsouthern\s+california\b|\blos\s+angeles\b|\bsenior\s+advantage\b"
    r"|\bmedi[-\s]cal\b"
)
EOC_KEY = "kaiser-norcal-eoc-2025-hmo"
SBC_KEY = "kaiser-norcal-sbc-2025-hmo"
PDF_RELATIVE = "kaiser/norcal-sbc-2025.pdf"
PDF_PAGES = [
    "Northern California Summary of Benefits and Coverage.",
    "Covered services and what you pay.",
]
#: The same document fetched again with its text revised: a different PDF.
REVISED_PDF_PAGES = [
    "Northern California Summary of Benefits and Coverage, 2026 edition.",
    "Covered services and what you pay, revised.",
]
SENTINEL = "SENTINEL-PAGE-TEXT-7f3a9c"
EMAIL = "rebecca.crumpler@example.com"


class _ImportTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.kaiser = InsuranceCompany.objects.create(
            name="Kaiser Permanente",
            alt_names="Kaiser\nKP",
            regex=r"kaiser\s*permanente|kaiser(?!\s*foundation)",
        )
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna", alt_names="Aetna Health", regex=r"aetna"
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    # -- records ---------------------------------------------------------------

    def record(self, **overrides):
        """The worked NorCal EOC record of the bundle contract, with pages."""
        record = {
            "document_key": EOC_KEY,
            "insurer": "Kaiser Permanente",
            "title": (
                "Kaiser Permanente Northern California 2025 HMO Evidence of Coverage"
            ),
            "document_type": "evidence_of_coverage",
            "coverage_kind": "medical",
            "line_of_business": "commercial",
            "state": "CA",
            "region": "Northern California",
            "plan_year": 2025,
            "match_keywords": ["northern california", "san francisco"],
            "negative_regex": NORCAL_NEGATIVE_REGEX,
            "pages": [
                "Northern California member benefits.",
                "Appeals and grievances.",
            ],
        }
        record.update(overrides)
        return record

    def record_without(self, *names, **overrides):
        record = self.record(**overrides)
        for name in names:
            record.pop(name, None)
        return record

    def file_record(self, relative=PDF_RELATIVE, **overrides):
        """The SBC record, whose text comes from a file under the files root."""
        return self.record_without(
            "pages",
            document_key=SBC_KEY,
            document_type="summary_of_benefits",
            title="Kaiser Permanente Northern California 2025 HMO Summary of Benefits",
            file=relative,
            **overrides,
        )

    # -- files -----------------------------------------------------------------

    def write_bundle(self, records, name="bundle.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh)
        return path

    def write_text(self, text, name):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def pdf_under_root(self, relative=PDF_RELATIVE, pages=PDF_PAGES):
        """A two-page PDF of ``pages`` at ``relative`` under a files root;
        returns ``(files_root, pdf_bytes)``."""
        root = os.path.join(self.tmp, "files")
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = make_pdf_bytes(pages)
        with open(path, "wb") as fh:
            fh.write(data)
        return root, data

    def temporary_storage(self):
        """Point the stored_file field's storage at one temporary directory,
        the way _StoredFilesTestCase does; returns that backend."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        backend = FileSystemStorage(location=tmp.name)
        storage = PublicPlanDocument._meta.get_field("stored_file").storage
        patcher = patch.object(storage, "backends", [backend])
        patcher.start()
        self.addCleanup(patcher.stop)
        return backend

    def stored_files(self, backend):
        """The file names the backend holds under the field's upload
        directory, sorted; none before the first file is written."""
        try:
            _dirs, files = backend.listdir("public_plan_documents")
        except FileNotFoundError:
            return []
        return sorted(files)

    # -- running the command ---------------------------------------------------

    def run_import(self, records, **options):
        """Import a bundle file of ``records``; returns (stdout, stderr)."""
        return self.run_source(self.write_bundle(records), **options)

    def run_source(self, source, **options):
        out, err = StringIO(), StringIO()
        call_command(
            "import_plan_documents", source=source, stdout=out, stderr=err, **options
        )
        return out.getvalue(), err.getvalue()

    def run_import_from_stdin(self, text, **options):
        """Import ``text`` piped in as the command's stdin (--source -)."""
        return self.run_source("-", stdin=StringIO(text), **options)

    def match_row_for(self, key):
        """A PlanDocumentMatch row built from the document's current text."""
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(EMAIL),
        )
        return PlanDocumentMatch.objects.create(
            denial=denial,
            document=PublicPlanDocument.objects.get(document_key=key),
            company=self.kaiser,
            score=49.0,
            signals={"keyword": 20, "keyword_hits": 2, "state": 10, "identifying": 1},
            rank=1,
            selected=True,
            inputs_hash="0" * 64,
        )

    def the_document(self, key=EOC_KEY):
        return PublicPlanDocument.objects.get(document_key=key)


class NewUpdatedUnchangedTest(_ImportTestCase):
    def test_imports_a_new_record_from_pages_as_page_marked_text(self):
        out, _ = self.run_import(
            [
                self.record(
                    pages=[
                        "Northern California member benefits.",
                        "   ",
                        "Appeals and grievances.",
                    ]
                )
            ]
        )
        doc = self.the_document()
        self.assertEqual(
            doc.text,
            "[Page 1]\nNorthern California member benefits.\n\n"
            "[Page 3]\nAppeals and grievances.",
        )
        self.assertEqual(doc.page_count, 2)
        self.assertIn("created=1 updated=0 unchanged=0 skipped=0", out)

    def test_numbered_pages_keep_their_numbers(self):
        self.run_import(
            [
                self.record(
                    pages=[
                        {"page": 12, "text": "Exclusions and limitations."},
                        {"page": 4, "text": "How to get care."},
                    ]
                )
            ]
        )
        self.assertEqual(
            self.the_document().text,
            "[Page 4]\nHow to get care.\n\n[Page 12]\nExclusions and limitations.",
        )

    def test_imported_row_carries_the_text_hash_and_counts(self):
        self.run_import([self.record()])
        doc = self.the_document()
        self.assertEqual(
            doc.text_sha256, hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
        )
        self.assertEqual(doc.char_count, len(doc.text))

    def test_keywords_are_lower_cased_prefixes_upper_cased_and_empties_dropped(self):
        self.run_import(
            [
                self.record(
                    match_keywords=[
                        "Northern  California",
                        "",
                        "san francisco",
                        "northern california",
                    ],
                    plan_id_prefixes=["kp", " ", "KP", "grp-1"],
                )
            ]
        )
        doc = self.the_document()
        self.assertEqual(doc.match_keywords, ["northern california", "san francisco"])
        self.assertEqual(doc.plan_id_prefixes, ["KP", "GRP-1"])

    def test_record_fields_land_on_the_row(self):
        self.run_import(
            [
                self.record(
                    source_url="https://healthy.kaiserpermanente.org/norcal-eoc.pdf",
                    fetched_at="2025-03-01T12:00:00Z",
                    source_sha256="AB" * 32,
                    pipeline_version="v3",
                )
            ],
            batch="nightly-7",
        )
        doc = self.the_document()
        self.assertEqual(
            (
                doc.insurance_company,
                doc.title,
                doc.document_type,
                doc.coverage_kind,
                doc.line_of_business,
                doc.state,
                doc.region,
                doc.plan_year,
            ),
            (
                self.kaiser,
                "Kaiser Permanente Northern California 2025 HMO Evidence of Coverage",
                "evidence_of_coverage",
                "medical",
                "commercial",
                "CA",
                "Northern California",
                2025,
            ),
        )
        self.assertEqual(doc.negative_regex.pattern, NORCAL_NEGATIVE_REGEX)
        self.assertEqual(doc.match_regex.pattern, "")
        self.assertEqual(
            doc.source_url, "https://healthy.kaiserpermanente.org/norcal-eoc.pdf"
        )
        self.assertEqual(
            doc.fetched_at,
            datetime.datetime(2025, 3, 1, 12, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(doc.source_sha256, "ab" * 32)
        self.assertEqual(
            (doc.pipeline_version, doc.import_batch, doc.is_active),
            ("v3", "nightly-7", True),
        )

    def test_text_with_page_markers_is_kept_and_its_pages_counted(self):
        text = "[Page 1]\nMember benefits.\n\n[Page 2]\nAppeals."
        self.run_import([self.record_without("pages", text=text)])
        doc = self.the_document()
        self.assertEqual((doc.text, doc.page_count), (text, 2))

    def test_plain_text_is_wrapped_as_page_one(self):
        self.run_import([self.record_without("pages", text="Member benefits.\n")])
        doc = self.the_document()
        self.assertEqual((doc.text, doc.page_count), ("[Page 1]\nMember benefits.", 1))

    def test_a_title_with_a_line_break_is_stored_with_a_single_space(self):
        self.run_import(
            [self.record(title="Kaiser Permanente\nNorthern California 2025 HMO EOC")]
        )
        self.assertEqual(
            self.the_document().title,
            "Kaiser Permanente Northern California 2025 HMO EOC",
        )

    def test_updates_an_existing_record_by_document_key_without_duplicating(self):
        self.run_import([self.record()])
        out, _ = self.run_import([self.record(title="Kaiser NorCal 2025 HMO EOC, revised")])
        self.assertEqual(
            list(PublicPlanDocument.objects.values_list("title", flat=True)),
            ["Kaiser NorCal 2025 HMO EOC, revised"],
        )
        self.assertIn("created=0 updated=1 unchanged=0", out)

    def test_an_identical_reimport_is_counted_unchanged(self):
        self.run_import([self.record()])
        out, _ = self.run_import([self.record()])
        self.assertIn("created=0 updated=0 unchanged=1", out)
        self.assertEqual(PublicPlanDocument.objects.count(), 1)


class ReimportAndMatchRowsTest(_ImportTestCase):
    def test_reimport_with_changed_text_deletes_the_documents_match_rows(self):
        self.run_import([self.record()])
        row = self.match_row_for(EOC_KEY)
        self.run_import(
            [self.record(pages=["Northern California member benefits, 2026 edition."])]
        )
        self.assertFalse(PlanDocumentMatch.objects.filter(pk=row.pk).exists())

    def test_reimport_with_unchanged_text_keeps_the_match_rows(self):
        self.run_import([self.record()])
        row = self.match_row_for(EOC_KEY)
        self.run_import([self.record()])
        self.assertTrue(PlanDocumentMatch.objects.filter(pk=row.pk).exists())

    def test_reimport_changing_only_metadata_keeps_the_match_rows(self):
        self.run_import([self.record()])
        row = self.match_row_for(EOC_KEY)
        self.run_import([self.record(title="Kaiser NorCal 2025 HMO EOC, revised")])
        self.assertTrue(PlanDocumentMatch.objects.filter(pk=row.pk).exists())


class DryRunAndDeactivateMissingTest(_ImportTestCase):
    def test_dry_run_writes_nothing(self):
        out, _ = self.run_import([self.record()], dry_run=True)
        self.assertEqual(PublicPlanDocument.objects.count(), 0)
        self.assertIn("created=1", out)
        self.assertIn("dry_run=True", out)

    def three_documents_then_the_eoc_alone(self, **options):
        """Two Kaiser documents and an Aetna one, all stamped a month old,
        then a Kaiser bundle carrying only the EOC."""
        self.run_import(
            [
                self.record(),
                self.record(document_key=SBC_KEY, document_type="summary_of_benefits"),
                self.record(
                    document_key="aetna-eoc-2025",
                    insurer="Aetna",
                    title="Aetna 2025 Evidence of Coverage",
                    state="",
                    region="",
                    negative_regex="",
                ),
            ]
        )
        self.past = timezone.now() - datetime.timedelta(days=30)
        PublicPlanDocument.objects.update(updated_at=self.past)
        out, _ = self.run_import([self.record()], **options)
        return out, {d.document_key: d for d in PublicPlanDocument.objects.all()}

    def test_deactivate_missing_retires_only_the_bundles_carriers_missing_keys(self):
        out, docs = self.three_documents_then_the_eoc_alone(deactivate_missing=True)
        self.assertEqual(
            {key: d.is_active for key, d in docs.items()},
            {EOC_KEY: True, SBC_KEY: False, "aetna-eoc-2025": True},
        )
        self.assertIn("deactivated=1", out)

    def test_deactivate_missing_bumps_updated_at_of_the_retired_row_only(self):
        _, docs = self.three_documents_then_the_eoc_alone(deactivate_missing=True)
        self.assertGreater(docs[SBC_KEY].updated_at, self.past)
        self.assertEqual(docs["aetna-eoc-2025"].updated_at, self.past)
        # An unchanged re-import does not save the row, so its stamp stays.
        self.assertEqual(docs[EOC_KEY].updated_at, self.past)

    def test_without_deactivate_missing_nothing_is_retired(self):
        out, docs = self.three_documents_then_the_eoc_alone()
        self.assertTrue(all(d.is_active for d in docs.values()))
        self.assertIn("deactivated=n/a", out)

    def test_deactivate_missing_keeps_the_row_of_a_record_the_bundle_refused(self):
        sbc = dict(document_key=SBC_KEY, document_type="summary_of_benefits")
        self.run_import([self.record(), self.record(**sbc)])
        out, err = self.run_import(
            [self.record(), self.record(match_regex="(", **sbc)],
            deactivate_missing=True,
        )
        self.assertIn(
            f"Skipped record 1 ({SBC_KEY}): match_regex: does not compile", err
        )
        # The bundle still names the key, so its refusal retires nothing.
        self.assertTrue(self.the_document(SBC_KEY).is_active)
        self.assertIn("skipped=1 deactivated=0", out)


class FileRecordsTest(_ImportTestCase):
    def test_parses_a_pdf_file_under_the_files_root(self):
        root, _ = self.pdf_under_root()
        self.run_import([self.file_record()], files_root=root)
        doc = self.the_document(SBC_KEY)
        self.assertTrue(doc.text.startswith("[Page 1]\n"), doc.text[:40])
        self.assertIn(PDF_PAGES[0], doc.text)
        self.assertIn(f"[Page 2]\n{PDF_PAGES[1]}", doc.text)
        self.assertEqual(doc.page_count, 2)

    def test_a_file_records_source_sha256_is_the_files_hash(self):
        root, data = self.pdf_under_root()
        self.run_import([self.file_record()], files_root=root)
        doc = self.the_document(SBC_KEY)
        self.assertEqual(doc.source_sha256, hashlib.sha256(data).hexdigest())
        # Without --store-file the original is not kept.
        self.assertFalse(doc.stored_file)

    def test_a_path_escaping_the_files_root_is_refused(self):
        root, data = self.pdf_under_root()
        with open(os.path.join(self.tmp, "outside.pdf"), "wb") as fh:
            fh.write(data)
        out, err = self.run_import([self.file_record("../outside.pdf")], files_root=root)
        self.assertIn(
            f"Skipped record 0 ({SBC_KEY}): file: the path leaves the files root", err
        )
        self.assertIn("skipped=1", out)
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_an_absolute_file_path_is_refused(self):
        root, _ = self.pdf_under_root()
        _, err = self.run_import(
            [self.file_record(os.path.join(root, PDF_RELATIVE))], files_root=root
        )
        self.assertIn("file: the path must be relative to the files root", err)
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_a_file_record_without_files_root_is_a_command_error(self):
        self.pdf_under_root()
        with self.assertRaises(CommandError) as caught:
            self.run_import([self.file_record()])
        self.assertIn("files-root", str(caught.exception))
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_store_file_keeps_the_original(self):
        backend = self.temporary_storage()
        root, data = self.pdf_under_root()
        self.run_import([self.file_record()], files_root=root, store_file=True)
        doc = self.the_document(SBC_KEY)
        # Named after the key, with the file's extension; the storage adds
        # its own random token before the extension (combined_storage).
        stored_name = os.path.basename(doc.stored_file.name)
        self.assertTrue(stored_name.startswith(SBC_KEY), stored_name)
        self.assertTrue(stored_name.endswith(".pdf"), stored_name)
        with backend.open(doc.stored_file.name, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertTrue(_stored_file_in_use(doc.stored_file.name))

    def test_store_file_on_an_identical_reimport_is_unchanged(self):
        self.temporary_storage()
        root, _ = self.pdf_under_root()
        self.run_import([self.file_record()], files_root=root, store_file=True)
        out, _ = self.run_import([self.file_record()], files_root=root, store_file=True)
        self.assertIn("created=0 updated=0 unchanged=1", out)

    def test_a_file_path_with_an_embedded_nul_is_refused_not_a_crash(self):
        root, _ = self.pdf_under_root()
        out, err = self.run_import(
            [self.file_record("kaiser/norcal-sbc-2025\x00.pdf")], files_root=root
        )
        self.assertIn(f"Skipped record 0 ({SBC_KEY}): file:", err)
        self.assertIn("skipped=1", out)
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_a_dry_run_with_store_file_writes_no_file(self):
        backend = self.temporary_storage()
        root, _ = self.pdf_under_root()
        out, _ = self.run_import(
            [self.file_record()], files_root=root, store_file=True, dry_run=True
        )
        self.assertEqual(self.stored_files(backend), [])
        self.assertEqual(PublicPlanDocument.objects.count(), 0)
        self.assertIn("created=1", out)
        self.assertIn("dry_run=True", out)


class StoredFileReplacementTest(_ImportTestCase):
    """--store-file for a row that already keeps a file: the new original
    takes its place, and the old one goes only once the run has committed,
    so an abandoned run leaves the row on a file that still exists."""

    def setUp(self):
        super().setUp()
        self.backend = self.temporary_storage()
        root, self.first_data = self.pdf_under_root()
        self.run_import([self.file_record()], files_root=root, store_file=True)
        self.first_name = self.the_document(SBC_KEY).stored_file.name
        # The same path under the files root now holds a different PDF.
        self.root, self.revised_data = self.pdf_under_root(pages=REVISED_PDF_PAGES)

    def reimport(self, **options):
        return self.run_import(
            [self.file_record()], files_root=self.root, store_file=True, **options
        )

    def test_a_file_with_new_bytes_replaces_the_stored_original(self):
        with self.captureOnCommitCallbacks(execute=True):
            out, _ = self.reimport()
        doc = self.the_document(SBC_KEY)
        self.assertNotEqual(doc.stored_file.name, self.first_name)
        with self.backend.open(doc.stored_file.name, "rb") as fh:
            self.assertEqual(fh.read(), self.revised_data)
        self.assertIn("created=0 updated=1 unchanged=0", out)

    def test_the_old_file_stays_until_the_run_commits(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.reimport()
            # A rollback would put the row back on this file.
            self.assertTrue(self.backend.exists(self.first_name))

    def test_the_old_file_is_removed_once_the_run_has_committed(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.reimport()
        doc = self.the_document(SBC_KEY)
        self.assertEqual(
            self.stored_files(self.backend), [os.path.basename(doc.stored_file.name)]
        )

    def test_a_strict_run_abandoned_after_a_replacement_keeps_the_original(self):
        before = self.stored_files(self.backend)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(CommandError) as caught:
                self.run_import(
                    [
                        self.file_record(),
                        self.record(document_key="kaiser-bad-regex", match_regex="("),
                    ],
                    files_root=self.root,
                    store_file=True,
                    strict=True,
                )
        self.assertIn("record 1 (kaiser-bad-regex)", str(caught.exception))
        doc = self.the_document(SBC_KEY)
        self.assertEqual(doc.stored_file.name, self.first_name)
        with self.backend.open(self.first_name, "rb") as fh:
            self.assertEqual(fh.read(), self.first_data)
        # The file written before the run was abandoned went with it.
        self.assertEqual(self.stored_files(self.backend), before)

    def test_a_dry_run_of_a_replacement_writes_no_file(self):
        before = self.stored_files(self.backend)
        out, _ = self.reimport(dry_run=True)
        self.assertEqual(self.stored_files(self.backend), before)
        self.assertEqual(self.the_document(SBC_KEY).stored_file.name, self.first_name)
        self.assertIn("updated=1", out)
        self.assertIn("dry_run=True", out)


class PlanResolutionTest(_ImportTestCase):
    def plan(self, state="", company=None):
        return InsurancePlan.objects.create(
            insurance_company=company or self.kaiser,
            plan_name="Medicare Advantage",
            state=state,
        )

    def test_resolves_the_plan_by_name_and_the_documents_state(self):
        self.plan(state="")
        ca_plan = self.plan(state="CA")
        self.run_import([self.record(plan_name="Medicare Advantage", state="CA")])
        self.assertEqual(self.the_document().insurance_plan, ca_plan)

    def test_resolves_the_national_plan_when_the_state_has_none(self):
        national = self.plan(state="")
        _, err = self.run_import([self.record(plan_name="medicare advantage", state="CA")])
        self.assertEqual(self.the_document().insurance_plan, national)
        self.assertEqual(err, "")

    def test_plan_state_picks_that_states_plan(self):
        self.plan(state="")
        wa_plan = self.plan(state="WA")
        self.run_import(
            [self.record(plan_name="Medicare Advantage", plan_state="WA", state="CA")]
        )
        self.assertEqual(self.the_document().insurance_plan, wa_plan)

    def test_an_unknown_plan_name_leaves_the_plan_blank_with_a_warning(self):
        out, err = self.run_import([self.record(plan_name="Platinum Plus HMO")])
        self.assertIsNone(self.the_document().insurance_plan)
        self.assertIn(
            f"Record 0 ({EOC_KEY}): plan_name: no plan named so for Kaiser Permanente",
            err,
        )
        self.assertIn("created=1", out)

    def test_another_carriers_plan_is_not_resolved(self):
        self.plan(state="", company=self.aetna)
        self.run_import([self.record(plan_name="Medicare Advantage")])
        self.assertIsNone(self.the_document().insurance_plan)


class CarrierResolutionTest(_ImportTestCase):
    def test_an_unresolvable_insurer_is_skipped_and_counted(self):
        out, err = self.run_import(
            [self.record(document_key="acme-eoc-2025", insurer="Acme Health")]
        )
        self.assertIn(
            "Skipped record 0 (acme-eoc-2025): insurer: no insurance company by that name",
            err,
        )
        self.assertIn("skipped=1", out)
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_skipped_records_are_warnings_and_the_run_still_succeeds(self):
        # force_color so the styles show; the same palette on both sides.
        style = color_style(force_color=True)
        out, err = self.run_import(
            [
                self.record(),
                self.record(document_key="acme-eoc-2025", insurer="Acme Health"),
            ],
            force_color=True,
        )
        self.assertIn(
            style.WARNING(
                "Skipped record 1 (acme-eoc-2025): insurer: no insurance company "
                "by that name"
            ),
            err,
        )
        self.assertIn(
            style.WARNING(
                "Plan document import done: created=1 updated=0 unchanged=0 "
                "skipped=1 deactivated=n/a dry_run=False"
            ),
            out,
        )

    def test_insurer_resolves_through_an_alt_name(self):
        self.run_import([self.record(insurer="KP")])
        self.assertEqual(self.the_document().insurance_company, self.kaiser)

    def test_insurance_company_id_wins_over_insurer(self):
        self.run_import(
            [self.record(insurance_company_id=self.aetna.id, insurer="Kaiser Permanente")]
        )
        self.assertEqual(self.the_document().insurance_company, self.aetna)


class NegativeRegexProbeTest(_ImportTestCase):
    def test_a_negative_regex_matching_ordinary_words_is_imported_with_a_warning(self):
        out, err = self.run_import([self.record(negative_regex="medi-?cal")])
        self.assertIn(
            f"Record 0 ({EOC_KEY}): negative_regex: matches ordinary denial-letter words",
            err,
        )
        self.assertEqual(PublicPlanDocument.objects.count(), 1)
        self.assertIn("created=1", out)

    def test_strict_refuses_a_negative_regex_matching_ordinary_words(self):
        with self.assertRaises(CommandError) as caught:
            self.run_import([self.record(negative_regex="medi-?cal")], strict=True)
        self.assertIn(f"record 0 ({EOC_KEY}): negative_regex", str(caught.exception))
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_the_example_negative_regex_passes_the_probe(self):
        out, err = self.run_import([self.record()], strict=True)
        self.assertEqual(err, "")
        self.assertIn("created=1", out)


class RefusedRecordsTest(_ImportTestCase):
    def test_bad_records_are_refused_with_the_field_named(self):
        cases = [
            (
                "missing document_key",
                self.record_without("document_key"),
                "Skipped record 0 (no key): document_key: required",
            ),
            (
                "regex that does not compile",
                self.record(match_regex="("),
                "match_regex: does not compile",
            ),
            (
                "unknown document_type",
                self.record(document_type="brochure"),
                "document_type: one of",
            ),
            ("unknown field", self.record(colour="blue"), "unknown field(s): colour"),
            (
                "two text sources",
                self.record(text="The same document as text."),
                "exactly one of pages, text or file",
            ),
            (
                "oversize text",
                self.record_without("pages", text="a" * (MAX_TEXT_CHARS + 1)),
                f"text: more than {MAX_TEXT_CHARS} characters",
            ),
        ]
        for name, record, reason in cases:
            with self.subTest(name):
                out, err = self.run_import([record])
                self.assertIn(reason, err)
                self.assertIn("skipped=1", out)
                self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_strict_aborts_on_the_first_bad_record_and_rolls_back(self):
        with self.assertRaises(CommandError) as caught:
            self.run_import(
                [
                    self.record(),
                    self.record(
                        document_key="kaiser-bad-regex",
                        match_regex="(",
                        pages=[f"{SENTINEL} in the refused record"],
                    ),
                ],
                strict=True,
            )
        self.assertIn(
            "record 1 (kaiser-bad-regex): match_regex: does not compile",
            str(caught.exception),
        )
        self.assertNotIn(SENTINEL, str(caught.exception))
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_a_field_given_twice_refuses_the_bundle(self):
        path = self.write_text(
            '[{"document_key": "a", "document_key": "b"}]', "twice.json"
        )
        with self.assertRaises(CommandError) as caught:
            self.run_source(path)
        self.assertIn("gives the field 'document_key' twice", str(caught.exception))

    def test_a_bundle_that_is_not_json_is_a_command_error(self):
        path = self.write_text("[not json", "broken.json")
        with self.assertRaises(CommandError) as caught:
            self.run_source(path)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_a_missing_bundle_file_is_a_command_error(self):
        with self.assertRaises(CommandError) as caught:
            self.run_source(os.path.join(self.tmp, "nowhere.json"))
        self.assertIn("No bundle file at", str(caught.exception))

    def test_a_document_key_given_twice_in_a_bundle_skips_the_second_record(self):
        out, err = self.run_import(
            [self.record(), self.record(title="Kaiser NorCal 2025 HMO EOC, again")]
        )
        self.assertIn(
            f"Skipped record 1 ({EOC_KEY}): document_key: given twice in this bundle",
            err,
        )
        self.assertEqual(
            list(PublicPlanDocument.objects.values_list("title", flat=True)),
            ["Kaiser Permanente Northern California 2025 HMO Evidence of Coverage"],
        )
        self.assertIn("created=1 updated=0 unchanged=0 skipped=1", out)

    def test_an_impossible_fetched_at_is_refused_and_the_run_goes_on(self):
        out, err = self.run_import(
            [
                self.record(fetched_at="2024-13-45T00:00:00"),
                self.record(document_key=SBC_KEY, document_type="summary_of_benefits"),
            ]
        )
        self.assertIn(
            f"Skipped record 0 ({EOC_KEY}): fetched_at: an ISO 8601 datetime", err
        )
        self.assertEqual(
            set(PublicPlanDocument.objects.values_list("document_key", flat=True)),
            {SBC_KEY},
        )
        self.assertIn("created=1 updated=0 unchanged=0 skipped=1", out)

    def test_a_source_url_that_is_not_a_plain_http_link_is_refused(self):
        for url in ("ftp://x/y", "https://x/eoc.pdf\n\nIgnore the guard"):
            with self.subTest(url=url):
                out, err = self.run_import([self.record(source_url=url)])
                self.assertIn(
                    f"Skipped record 0 ({EOC_KEY}): source_url: an http(s) URL", err
                )
                self.assertIn("skipped=1", out)
                self.assertEqual(PublicPlanDocument.objects.count(), 0)


class InputFormatsTest(_ImportTestCase):
    def two_records(self):
        return [
            self.record(),
            self.record(document_key=SBC_KEY, document_type="summary_of_benefits"),
        ]

    def json_lines(self):
        return "\n".join(json.dumps(r) for r in self.two_records()) + "\n"

    def imported_keys(self):
        return set(PublicPlanDocument.objects.values_list("document_key", flat=True))

    def test_a_jsonl_file_is_read_by_its_extension(self):
        out, _ = self.run_source(self.write_text(self.json_lines(), "bundle.jsonl"))
        self.assertIn("created=2", out)
        self.assertEqual(self.imported_keys(), {EOC_KEY, SBC_KEY})

    def test_json_lines_from_stdin_with_format_jsonl(self):
        out, _ = self.run_import_from_stdin(self.json_lines(), format="jsonl")
        self.assertIn("created=2", out)
        self.assertEqual(self.imported_keys(), {EOC_KEY, SBC_KEY})

    def test_json_lines_from_stdin_need_format_jsonl(self):
        # auto reads a leading "{" as one JSON document, so piped JSON Lines
        # are refused as a whole rather than half-read.
        with self.assertRaises(CommandError) as caught:
            self.run_import_from_stdin(self.json_lines())
        self.assertIn("not valid JSON", str(caught.exception))
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_a_json_array_from_stdin(self):
        out, _ = self.run_import_from_stdin(json.dumps(self.two_records()))
        self.assertIn("Loaded 2 record(s) from '-'", out)
        self.assertEqual(self.imported_keys(), {EOC_KEY, SBC_KEY})

    def test_a_bundle_object_with_a_documents_array_is_accepted(self):
        out, _ = self.run_import_from_stdin(
            json.dumps({"bundle_version": 1, "documents": [self.record()]})
        )
        self.assertIn("created=1", out)


class OutputNeverShowsTextTest(_ImportTestCase):
    def test_output_never_contains_document_text(self):
        messages = []
        sink = logger.add(lambda m: messages.append(m.record["message"]), level="DEBUG")
        self.addCleanup(logger.remove, sink)
        records = [
            self.record(pages=[f"Benefits {SENTINEL} page."]),
            self.record(
                document_key="kaiser-bad-regex",
                match_regex="(",
                pages=[f"{SENTINEL} in the refused record"],
            ),
            self.record_without(
                "pages",
                document_key="kaiser-warned",
                negative_regex="medi-?cal",
                text=f"{SENTINEL} in the warned record",
            ),
        ]
        out, err = self.run_import(records, force_color=True)
        self.assertIn("created=2 updated=0 unchanged=0 skipped=1", out)
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn(SENTINEL, err)
        self.assertEqual([m for m in messages if SENTINEL in m], [])
