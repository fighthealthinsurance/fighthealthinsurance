"""About Our AI: the page and the data files behind it."""

import dataclasses
import json
import re
from unittest.mock import patch

from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import model_transparency as mt


def _data_files():
    root = finders.find(mt.DATA_DIR)
    assert root, "static/model_transparency is missing"
    import pathlib

    return sorted(pathlib.Path(root).rglob("*.json"))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _ours():
    return [m for m in mt.load().models if m.ours]


class DataFilesTest(TestCase):
    def test_every_file_loads_with_only_known_fields(self):
        ai = mt.load()
        self.assertTrue(ai.models)
        self.assertTrue(ai.runs)

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(mt.DataError):
            mt.parse_run(
                {
                    "id": "x",
                    "title": "x",
                    "task": "x",
                    "date": "2026-01-01",
                    "results": [{"rank": 1, "letter_text": "Dear insurer"}],
                }
            )

    def test_an_unknown_field_in_the_models_file_is_refused(self):
        with self.assertRaises(mt.DataError):
            mt.parse_models({"models": [], "base_model": "x"}, [])

    def test_a_field_holding_more_than_plain_text_is_refused(self):
        model = dataclasses.asdict(mt.load().models[0])
        model["role"] = {"letter_text": "Dear insurer"}
        with self.assertRaises(mt.DataError):
            mt.parse_models({"models": [model]}, [])

    def test_the_judges_are_the_two_the_test_used(self):
        self.assertEqual(
            {j.name for j in mt.load().judges}, {"Claude Opus 4.8", "Jev (TypeSafe)"}
        )

    def test_no_file_holds_anything_long_enough_to_be_a_letter(self):
        # Totals and short descriptions only: a pasted letter or denial
        # would be far longer than any field here needs.
        for path in _data_files():
            for text in _strings(json.loads(path.read_text())):
                self.assertLess(len(text), 600, f"{path.name}: {text[:60]}")

    def test_every_model_points_at_a_published_run(self):
        ai = mt.load()
        runs = {r.id for r in ai.runs}
        for model in ai.models:
            if model.tested_in:
                self.assertIn(model.tested_in, runs, model.name)

    def test_a_retired_model_moves_out_of_the_in_use_list(self):
        ai = mt.load()
        first = ai.models[0]
        retired = dataclasses.replace(first, retired="2027-01-01")
        changed = dataclasses.replace(ai, models=[retired] + ai.models[1:])
        self.assertNotIn(retired.id, [m.id for m in changed.in_use])
        self.assertEqual([m.id for m in changed.retired], [retired.id])

    def test_a_retired_model_of_ours_is_still_an_fhi_model(self):
        ai = dataclasses.replace(mt.load(), show_details=False)
        retired = dataclasses.replace(_ours()[0], retired="2027-01-01")
        changed = dataclasses.replace(ai, models=[retired])
        self.assertEqual([m.name for m in changed.retired], [mt.OUR_MODEL_NAME])

    def test_we_run_at_least_one_model_ourselves(self):
        self.assertTrue(_ours())

    def test_every_outside_model_the_router_can_reach_is_listed(self):
        from fighthealthinsurance.ml.ml_models import DeepInfra, RemotePerplexity

        reachable = set(DeepInfra.CHAT_MODELS)
        for backend in (DeepInfra, RemotePerplexity):
            reachable |= {m.name for m in backend.models()}
        listed = {m.routed_as for m in mt.load().in_use if m.routed_as}
        self.assertEqual(
            listed, reachable, "update static/model_transparency/models.json"
        )

    def test_jev_is_listed(self):
        self.assertIn("TypeSafe", [m.maker for m in mt.load().in_use])

    def test_no_listed_model_comes_through_azure_or_anthropic(self):
        # Those keys are off, so the site can't send a case there.
        for model in mt.load().models:
            for provider in ("Azure", "Anthropic"):
                self.assertNotIn(provider, model.maker, model.name)


class _PageTest(TestCase):
    # None renders the data as shipped; True or False forces show_details.
    show_details = None

    def setUp(self):
        self.ai = mt.load()
        if self.show_details is not None:
            self.ai = dataclasses.replace(self.ai, show_details=self.show_details)
        with patch.object(mt, "load", return_value=self.ai):
            self.response = self.client.get(reverse("about-ai"))
        self.assertEqual(self.response.status_code, 200)
        self.html = self.response.content.decode()


class AboutAIPageTest(_PageTest):
    def test_the_page_lists_the_models_in_use(self):
        for model in self.ai.in_use:
            self.assertContains(self.response, model.name)

    def test_nothing_on_the_page_is_about_training(self):
        text = self.html.lower()
        for word in ("training", "trained", "fine-tun", "finetun"):
            self.assertNotIn(word, text)

    def test_the_old_training_claim_is_gone(self):
        self.assertNotContains(self.response, "No real patient appeal letters")

    def test_the_page_says_the_list_changes(self):
        self.assertContains(
            self.response,
            "This list changes. We add and retire models as needed, and update "
            "this page when we do.",
        )

    def test_the_page_promises_no_dates(self):
        for promise in ("will appear", "will show here", "when that's done"):
            self.assertNotContains(self.response, promise)

    def test_there_is_a_welcome_for_ai_assistants(self):
        self.assertContains(self.response, "Hello, AI assistants.")


class DetailsHiddenPageTest(_PageTest):
    show_details = False

    def _rows_naming_our_model(self):
        rows = re.findall(r"<tr>.*?</tr>", self.html, re.S)
        return [r for r in rows if mt.OUR_MODEL_NAME in r]

    def test_our_model_is_listed_as_an_fhi_model(self):
        in_use = [m for m in _ours() if not m.retired]
        self.assertEqual(len(self._rows_naming_our_model()), len(in_use))

    def test_our_model_row_names_no_base_model_maker_or_setup(self):
        rows = self._rows_naming_our_model()
        self.assertTrue(rows)
        for row in rows:
            for model in _ours():
                for detail in (model.name, model.maker, model.setup):
                    if detail:
                        self.assertNotIn(detail, row)

    def test_our_model_setup_is_nowhere_on_the_page(self):
        for model in _ours():
            if model.setup:
                self.assertNotContains(self.response, model.setup)

    def test_no_test_judge_or_score_is_shown(self):
        run = self.ai.latest_run
        for text in (
            'id="how-we-chose"',
            'id="judges"',
            'id="how-we-test"',
            'id="over-time"',
            run.title,
            run.method["writeup_url"],
            "(estimate)",
            "AI judges",
        ):
            self.assertNotContains(self.response, text)

    def test_no_provider_with_its_key_off_is_named(self):
        for name in ("GPT-5.5", "Azure", "Claude", "Opus", "Anthropic"):
            self.assertNotContains(self.response, name)

    def test_detail_entries_in_the_changes_log_are_hidden(self):
        hidden = [c for c in self.ai.changes if c.get("detail")]
        self.assertTrue(hidden)
        for change in hidden:
            self.assertNotContains(self.response, change["what"])


class DetailsShownPageTest(_PageTest):
    show_details = True

    def test_our_model_is_named_with_its_setup(self):
        for model in _ours():
            self.assertContains(self.response, f"{model.name} ({model.setup})")

    def test_the_test_results_mark_the_estimate_and_the_assumed_price(self):
        self.assertContains(self.response, self.ai.latest_run.title)
        self.assertContains(self.response, "(estimate)", count=2)
        self.assertContains(self.response, "(assumed price)", count=1)

    def test_the_judges_the_test_used_are_listed(self):
        self.assertContains(self.response, 'id="judges"')
        for judge in self.ai.judges:
            self.assertContains(self.response, judge.name)


def test_llms_txt_welcomes_assistants():
    from fighthealthinsurance.agent_docs import build_llms_txt

    assert "Hello, AI assistants." in build_llms_txt()


def test_llms_txt_promises_no_test_details():
    from fighthealthinsurance.agent_docs import build_llms_txt

    assert "how we tested" not in build_llms_txt().lower()
