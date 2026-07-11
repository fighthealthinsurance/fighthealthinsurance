"""Tests for the canonical ML model reporting identity helpers."""

from django.test import SimpleTestCase

from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    SYNTHESIZED_MODEL_NAME,
    canonical_model_name,
    is_object_repr,
    legacy_unresolved_label,
    normalize_model_label,
)


class FakeBackend:
    """Duck-typed stand-in for RemoteModelLike: optional router-stamped
    ``name`` plus the configured provider ``model`` string."""

    name = None

    def __init__(self, model=None, name=None):
        if model is not None:
            self.model = model
        if name is not None:
            self.name = name


class BareBackend:
    """No identity attributes at all - exercises the class-name fallback."""


class CanonicalModelNameTest(SimpleTestCase):
    def test_two_instances_of_same_configured_model_share_identity(self):
        # Regression: previously str(model) produced per-instance
        # "<... object at 0x...>" strings, splitting one configured model
        # into many reporting rows.
        a = FakeBackend(model="google/gemma-4-26B-A4B-it")
        b = FakeBackend(model="google/gemma-4-26B-A4B-it")
        self.assertEqual(canonical_model_name(a), canonical_model_name(b))
        self.assertEqual(canonical_model_name(a), "google/gemma-4-26B-A4B-it")

    def test_same_class_different_configured_models_stay_separate(self):
        a = FakeBackend(model="deepseek-ai/DeepSeek-V4-Pro")
        b = FakeBackend(model="google/gemma-4-26B-A4B-it")
        self.assertNotEqual(canonical_model_name(a), canonical_model_name(b))

    def test_router_stamped_name_preferred_over_provider_model(self):
        # The registry key is what the appeal pipeline records on
        # ProposedAppeal rows, so it wins for cross-source consistency.
        m = FakeBackend(model="/models/fhi-2025-may", name="fhi-2025-may")
        self.assertEqual(canonical_model_name(m), "fhi-2025-may")

    def test_unstamped_none_name_falls_through_to_model(self):
        # RemoteModelLike declares ``name = None`` at class level; an
        # unstamped instance must not surface None (the old
        # getattr(model, "name", str(model)) bug) nor a repr.
        m = FakeBackend(model="sonar")
        self.assertIsNone(m.name)
        self.assertEqual(canonical_model_name(m), "sonar")

    def test_class_name_fallback_is_stable_and_addressless(self):
        a, b = BareBackend(), BareBackend()
        self.assertEqual(canonical_model_name(a), "BareBackend")
        self.assertEqual(canonical_model_name(a), canonical_model_name(b))

    def test_never_contains_memory_address(self):
        for candidate in (FakeBackend(model="x"), BareBackend(), FakeBackend()):
            name = canonical_model_name(candidate)
            self.assertIsNotNone(name)
            self.assertNotIn("object at 0x", name)

    def test_none_model_returns_none(self):
        self.assertIsNone(canonical_model_name(None))


class NormalizeModelLabelTest(SimpleTestCase):
    REPR = "<fighthealthinsurance.ml.ml_models.DeepInfra object at 0x7f81456da840>"

    def test_object_repr_normalizes_to_class_level_legacy_label(self):
        self.assertEqual(
            normalize_model_label(self.REPR), legacy_unresolved_label("DeepInfra")
        )

    def test_two_addresses_of_same_class_collapse(self):
        other = "<fighthealthinsurance.ml.ml_models.DeepInfra object at 0x7deadbeef>"
        self.assertEqual(normalize_model_label(self.REPR), normalize_model_label(other))

    def test_normalized_label_never_keeps_address(self):
        self.assertNotIn("0x", str(normalize_model_label(self.REPR)))
        self.assertNotIn("object at", str(normalize_model_label(self.REPR)))

    def test_regular_names_pass_through(self):
        self.assertEqual(normalize_model_label("fhi-legacy"), "fhi-legacy")
        self.assertEqual(normalize_model_label("  padded  "), "padded")

    def test_synthesized_and_legacy_labels_pass_through(self):
        self.assertEqual(
            normalize_model_label(SYNTHESIZED_MODEL_NAME), SYNTHESIZED_MODEL_NAME
        )
        self.assertEqual(
            normalize_model_label(LEGACY_UNATTRIBUTED_LABEL),
            LEGACY_UNATTRIBUTED_LABEL,
        )

    def test_none_and_empty_normalize_to_none(self):
        self.assertIsNone(normalize_model_label(None))
        self.assertIsNone(normalize_model_label(""))
        self.assertIsNone(normalize_model_label("   "))

    def test_is_object_repr(self):
        self.assertTrue(is_object_repr(self.REPR))
        self.assertFalse(is_object_repr("fhi-legacy"))
        self.assertFalse(is_object_repr(None))
        self.assertFalse(is_object_repr("<html>"))
