"""The data files behind the microsites and the state help pages load
wherever the app runs, collected or not.

The state help loader only asked staticfiles_storage, which reads what
collectstatic copied to STATIC_ROOT. Nothing collects in CI's sync job, so
every state page there answered 404, which the page-outline test found by
rendering /state-help/california/. Both loaders now fall back to the app's
own static directory.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from fighthealthinsurance import microsites, state_help, static_data


def _storage_that_has_nothing(*args, **kwargs):
    raise FileNotFoundError("nothing collected")


class StaticDataWithoutCollectstaticTest(SimpleTestCase):
    def setUp(self):
        state_help._load_state_help_cached.cache_clear()
        microsites._find_microsites_json.cache_clear()
        self.addCleanup(state_help._load_state_help_cached.cache_clear)
        self.addCleanup(microsites._find_microsites_json.cache_clear)

    def test_a_state_page_has_its_data_when_nothing_was_collected(self):
        with patch.object(
            static_data.staticfiles_storage, "open", _storage_that_has_nothing
        ):
            california = state_help.get_state_help("california")
        self.assertIsNotNone(
            california, "state help loaded nothing without collectstatic"
        )

    def test_the_microsites_load_when_nothing_was_collected(self):
        with patch.object(
            static_data.staticfiles_storage, "open", _storage_that_has_nothing
        ):
            contents = microsites._find_microsites_json()
        self.assertTrue(contents and "biologic-denial" in contents)

    def test_a_file_that_is_nowhere_reads_as_none(self):
        with patch.object(
            static_data.staticfiles_storage, "open", _storage_that_has_nothing
        ):
            self.assertIsNone(static_data.read_static_text("no-such-file.json"))
