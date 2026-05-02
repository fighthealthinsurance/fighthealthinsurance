"""Tests for the import_ecri_guidelines management command."""

import io
import json
import os
import tempfile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from fighthealthinsurance.models import ECRIGuideline


class ImportECRIGuidelinesCommandTest(TestCase):
    def _write_records(self, records):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_imports_new_records(self):
        path = self._write_records(
            [
                {
                    "guideline_id": "gid-1",
                    "title": "Test Guideline",
                    "developer_organization": "Test Org",
                    "publication_date": "2024-01-15",
                    "procedure_keywords": ["PCI", "Stent"],
                    "diagnosis_keywords": ["Coronary Artery Disease"],
                    "topics": ["Cardiology"],
                    "url": "https://guidelines.ecri.org/example-1",
                }
            ]
        )
        out = io.StringIO()
        call_command("import_ecri_guidelines", source=path, stdout=out)

        guideline = ECRIGuideline.objects.get(guideline_id="gid-1")
        self.assertEqual(guideline.title, "Test Guideline")
        self.assertEqual(guideline.developer_organization, "Test Org")
        self.assertEqual(guideline.publication_date.year, 2024)
        # Keyword lists should be lower-cased and de-duplicated.
        self.assertEqual(guideline.procedure_keywords, ["pci", "stent"])
        self.assertEqual(guideline.diagnosis_keywords, ["coronary artery disease"])
        self.assertEqual(guideline.topics, ["cardiology"])
        self.assertTrue(guideline.is_active)

    def test_updates_existing_record(self):
        ECRIGuideline.objects.create(
            guideline_id="gid-2",
            title="Old Title",
            developer_organization="Old Org",
            procedure_keywords=["old"],
            diagnosis_keywords=["old"],
        )
        path = self._write_records(
            [
                {
                    "guideline_id": "gid-2",
                    "title": "New Title",
                    "developer_organization": "New Org",
                    "procedure_keywords": ["new procedure"],
                    "diagnosis_keywords": ["new diagnosis"],
                }
            ]
        )
        call_command("import_ecri_guidelines", source=path, stdout=io.StringIO())

        guideline = ECRIGuideline.objects.get(guideline_id="gid-2")
        self.assertEqual(guideline.title, "New Title")
        self.assertEqual(guideline.developer_organization, "New Org")
        self.assertEqual(guideline.procedure_keywords, ["new procedure"])
        # Single record in DB — update_or_create did not duplicate.
        self.assertEqual(ECRIGuideline.objects.filter(guideline_id="gid-2").count(), 1)

    def test_deactivate_missing_flag(self):
        ECRIGuideline.objects.create(guideline_id="gid-keep", title="Keep")
        ECRIGuideline.objects.create(guideline_id="gid-drop", title="Drop")
        path = self._write_records(
            [{"guideline_id": "gid-keep", "title": "Keep Updated"}]
        )
        call_command(
            "import_ecri_guidelines",
            source=path,
            deactivate_missing=True,
            stdout=io.StringIO(),
        )

        keep = ECRIGuideline.objects.get(guideline_id="gid-keep")
        drop = ECRIGuideline.objects.get(guideline_id="gid-drop")
        self.assertTrue(keep.is_active)
        self.assertFalse(drop.is_active)

    def test_deactivate_missing_with_empty_input_deactivates_all(self):
        """Empty input + --deactivate-missing must still mark everything inactive."""
        ECRIGuideline.objects.create(guideline_id="gid-stale-1", title="Stale 1")
        ECRIGuideline.objects.create(guideline_id="gid-stale-2", title="Stale 2")
        path = self._write_records([])
        call_command(
            "import_ecri_guidelines",
            source=path,
            deactivate_missing=True,
            stdout=io.StringIO(),
        )
        self.assertEqual(
            ECRIGuideline.objects.filter(is_active=True).count(),
            0,
        )

    def test_dry_run_does_not_write(self):
        path = self._write_records([{"guideline_id": "gid-dry", "title": "Dry Run"}])
        call_command(
            "import_ecri_guidelines",
            source=path,
            dry_run=True,
            stdout=io.StringIO(),
        )
        self.assertFalse(ECRIGuideline.objects.filter(guideline_id="gid-dry").exists())

    def test_skips_records_missing_required_fields(self):
        path = self._write_records(
            [
                {"title": "Missing ID"},
                {"guideline_id": "gid-ok", "title": ""},
                {"guideline_id": "gid-ok2", "title": "Ok"},
            ]
        )
        out = io.StringIO()
        err = io.StringIO()
        call_command("import_ecri_guidelines", source=path, stdout=out, stderr=err)
        self.assertTrue(ECRIGuideline.objects.filter(guideline_id="gid-ok2").exists())
        self.assertFalse(ECRIGuideline.objects.filter(guideline_id="gid-ok").exists())

    def test_invalid_keyword_field_raises(self):
        path = self._write_records(
            [
                {
                    "guideline_id": "gid-bad",
                    "title": "Bad",
                    "procedure_keywords": "not a list",
                }
            ]
        )
        with self.assertRaises(CommandError):
            call_command("import_ecri_guidelines", source=path, stdout=io.StringIO())

    def test_missing_source_file_raises(self):
        with self.assertRaises(CommandError):
            call_command(
                "import_ecri_guidelines",
                source="/nonexistent/path/ecri.json",
                stdout=io.StringIO(),
            )

    def test_string_is_active_falsy_values_are_false(self):
        """Strings like "false", "0", "no" must coerce to False, not True."""
        path = self._write_records(
            [
                {"guideline_id": "gid-false", "title": "F", "is_active": "false"},
                {"guideline_id": "gid-zero", "title": "Z", "is_active": "0"},
                {"guideline_id": "gid-no", "title": "N", "is_active": "no"},
                {"guideline_id": "gid-true", "title": "T", "is_active": "true"},
            ]
        )
        call_command("import_ecri_guidelines", source=path, stdout=io.StringIO())
        self.assertFalse(ECRIGuideline.objects.get(guideline_id="gid-false").is_active)
        self.assertFalse(ECRIGuideline.objects.get(guideline_id="gid-zero").is_active)
        self.assertFalse(ECRIGuideline.objects.get(guideline_id="gid-no").is_active)
        self.assertTrue(ECRIGuideline.objects.get(guideline_id="gid-true").is_active)

    def test_malformed_json_raises_command_error(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        self.addCleanup(os.remove, path)
        with self.assertRaises(CommandError) as ctx:
            call_command("import_ecri_guidelines", source=path, stdout=io.StringIO())
        # Error message should reference the source path, not just the raw decoder error.
        self.assertIn(path, str(ctx.exception))

    def test_seed_fixture_imports_cleanly(self):
        """Smoke-test the bundled seed fixture so it stays valid."""
        seed_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "fighthealthinsurance",
            "fixtures",
            "ecri_guidelines_seed.json",
        )
        seed_path = os.path.abspath(seed_path)
        self.assertTrue(os.path.exists(seed_path), seed_path)
        call_command("import_ecri_guidelines", source=seed_path, stdout=io.StringIO())
        # Fixture should produce at least a few guidelines.
        self.assertGreaterEqual(ECRIGuideline.objects.count(), 3)
