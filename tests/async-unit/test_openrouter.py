"""OpenRouter: the hosted backend (OPENROUTER_API_KEY), its web search model
(context only), and the web search citations built from it
(ml/web_search_context.py), with their relevance guardrail."""

import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from fighthealthinsurance.ml import research_judging, spend, web_search_context
from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.ml.ml_models import (
    OpenRouter,
    OpenRouterWebSearch,
    RateLimitedRemoteOpenLike,
    WebCitation,
    url_citations_from_annotations,
)
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.models import Denial

OPENROUTER_ENV = {"OPENROUTER_API_KEY": "sk-or-test"}

SUPPORTS = research_judging.Judgment(on_topic=0.9, supports=0.9, undermines=0.1)
WEAKLY_SUPPORTS = research_judging.Judgment(on_topic=0.9, supports=0.5, undermines=0.1)
OFF_TOPIC = research_judging.Judgment(on_topic=0.1, supports=0.9, undermines=0.0)
AGAINST = research_judging.Judgment(on_topic=0.9, supports=0.1, undermines=0.9)
# On topic only probably not: kept, with a note, unless enough is plainly on
# topic (research_judging.focus).
PROBABLY_OFF_TOPIC = research_judging.Judgment(
    on_topic=0.45, supports=0.9, undermines=0.0
)


class _Response:
    """An aiohttp response stand-in with a canned JSON body."""

    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, *args, **kwargs):
        return self._json

    async def text(self):
        return ""

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(), history=(), status=self.status
            )


class _Session:
    """An aiohttp.ClientSession stand-in that records the POST."""

    def __init__(self, response, capture):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self._capture.update(url=url, headers=headers, json=json)
        return self._response


def _completion(content="Found some sources.", annotations=None):
    message = {"role": "assistant", "content": content}
    if annotations is not None:
        message["annotations"] = annotations
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


def _url_citation(url, title="A page", content="An excerpt."):
    return {
        "type": "url_citation",
        "url_citation": {
            "url": url,
            "title": title,
            "content": content,
            "start_index": 0,
            "end_index": 5,
        },
    }


async def _post_once(model, response_json):
    """Run one _infer against a canned response; return (result, capture)."""
    capture: dict = {}
    session = _Session(_Response(response_json), capture)
    with patch.object(aiohttp, "ClientSession", return_value=session):
        result = await model._infer(system_prompts=["sys"], prompt="hello")
    return result, capture


class TestOpenRouterRegistration(unittest.TestCase):
    def test_no_models_without_a_key(self):
        self.assertEqual(OpenRouter.models(), [])
        self.assertEqual(OpenRouterWebSearch.models(), [])

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_models_are_prefixed_with_openrouter(self):
        names = [m.name for m in OpenRouter.models()]
        self.assertEqual(
            names,
            [f"openrouter/{model}" for model, _, _ in OpenRouter.DEFAULT_MODELS],
        )

    @patch.dict(
        os.environ,
        {
            **OPENROUTER_ENV,
            "OPENROUTER_MODELS": "qwen/qwen3.8-flash, qwen/qwen3.8-flash",
        },
    )
    def test_models_override_replaces_defaults_once_each(self):
        self.assertEqual(
            [m.internal_name for m in OpenRouter.models()], ["qwen/qwen3.8-flash"]
        )

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_router_puts_drafting_models_in_the_external_pool(self):
        router = MLRouter()
        external = {str(getattr(m, "name", m)) for m in router.external_models_by_cost}
        self.assertIn("openrouter/anthropic/claude-sonnet-5.5", external)

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_router_keeps_web_search_out_of_the_general_pools(self):
        router = MLRouter()
        pooled = [m for m in router.all_models_by_cost]
        self.assertFalse(any(isinstance(m, OpenRouterWebSearch) for m in pooled))

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_router_offers_the_web_search_backend(self):
        self.assertIsInstance(MLRouter().web_search_backend(), OpenRouterWebSearch)

    @patch.dict(
        os.environ, {**OPENROUTER_ENV, "OPENROUTER_WEB_SEARCH_ENABLED": "false"}
    )
    def test_web_search_can_be_turned_off(self):
        self.assertIsNone(MLRouter().web_search_backend())

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_web_search_backend_is_none_while_openrouter_is_paused(self):
        router = MLRouter()
        with patch.object(spend, "allows", return_value=False):
            self.assertIsNone(router.web_search_backend())

    def test_external_and_web_search_context_only(self):
        self.assertTrue(object.__new__(OpenRouter).external)
        self.assertFalse(object.__new__(OpenRouter).context_only)
        self.assertTrue(object.__new__(OpenRouterWebSearch).context_only)

    def test_ranks_below_internal_models(self):
        with patch.dict(os.environ, OPENROUTER_ENV):
            model = OpenRouter(model="anthropic/claude-sonnet-5.5")
        self.assertLess(model.quality(), 101)


class TestOpenRouterRequests(unittest.TestCase):
    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_request_denies_data_collection_and_requires_zdr(self):
        model = OpenRouter(model="google/gemini-3.8-flash")
        _, capture = asyncio.run(_post_once(model, _completion()))
        self.assertEqual(
            capture["json"]["provider"], {"data_collection": "deny", "zdr": True}
        )

    @patch.dict(os.environ, {**OPENROUTER_ENV, "OPENROUTER_REQUIRE_ZDR": "off"})
    def test_zdr_can_be_turned_off_but_data_collection_stays_denied(self):
        model = OpenRouter(model="google/gemini-3.8-flash")
        _, capture = asyncio.run(_post_once(model, _completion()))
        self.assertEqual(capture["json"]["provider"], {"data_collection": "deny"})

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_request_sends_key_and_attribution_headers(self):
        model = OpenRouter(model="google/gemini-3.8-flash")
        _, capture = asyncio.run(_post_once(model, _completion()))
        self.assertEqual(
            capture["headers"],
            {
                "HTTP-Referer": "https://www.fighthealthinsurance.com",
                "X-OpenRouter-Title": "Fight Health Insurance",
                "Authorization": "Bearer sk-or-test",
            },
        )

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_request_goes_to_openrouter(self):
        model = OpenRouter(model="google/gemini-3.8-flash")
        _, capture = asyncio.run(_post_once(model, _completion()))
        self.assertEqual(
            capture["url"], "https://openrouter.ai/api/v1/chat/completions"
        )

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_web_search_request_carries_the_server_tool(self):
        model = OpenRouterWebSearch(model=OpenRouterWebSearch.DEFAULT_MODEL)
        _, capture = asyncio.run(_post_once(model, _completion()))
        self.assertEqual(
            capture["json"]["tools"],
            [{"type": "openrouter:web_search", "parameters": {"max_results": 5}}],
        )

    @patch.dict(
        os.environ,
        {
            **OPENROUTER_ENV,
            "OPENROUTER_WEB_SEARCH_ENGINE": "exa",
            "OPENROUTER_WEB_SEARCH_MAX_RESULTS": "99",
        },
    )
    def test_web_search_engine_and_capped_result_count(self):
        self.assertEqual(
            OpenRouterWebSearch.web_search_tool()["parameters"],
            {"max_results": 25, "engine": "exa"},
        )

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_web_search_returns_the_annotated_pages_not_the_prose(self):
        model = OpenRouterWebSearch(model=OpenRouterWebSearch.DEFAULT_MODEL)
        response = _completion(
            content="MRI is covered when red flags are present.",
            annotations=[_url_citation("https://example.org/mri", "MRI policy")],
        )

        async def run():
            session = _Session(_Response(response), {})
            with patch.object(aiohttp, "ClientSession", return_value=session):
                return await model.web_search("MRI for back pain?")

        found = asyncio.run(run())
        self.assertEqual(
            [(c.page_title, c.url, c.excerpt) for c in found],
            [("MRI policy", "https://example.org/mri", "An excerpt.")],
        )

    @patch.dict(os.environ, OPENROUTER_ENV)
    def test_web_search_failure_returns_nothing(self):
        model = OpenRouterWebSearch(model=OpenRouterWebSearch.DEFAULT_MODEL)
        with patch.object(
            RateLimitedRemoteOpenLike,
            "_infer",
            new_callable=AsyncMock,
            return_value=None,
        ):
            self.assertEqual(asyncio.run(model.web_search("q")), [])


class TestUrlCitationsFromAnnotations(unittest.TestCase):
    def test_a_long_title_is_cut_to_what_the_judge_reads(self):
        citation = WebCitation("t" * 500, "https://a.org/x")
        self.assertEqual(
            (citation.page_title, str(citation)),
            ("t" * 200, "t" * 200 + ". https://a.org/x"),
        )

    def test_reads_url_citations_with_their_parts(self):
        found = url_citations_from_annotations(
            [_url_citation("https://a.org/x", " Title  one ", "Some  text")]
        )
        self.assertEqual(
            [(str(c), c.page_title, c.url, c.excerpt) for c in found],
            [
                (
                    "Title one. https://a.org/x",
                    "Title one",
                    "https://a.org/x",
                    "Some text",
                )
            ],
        )

    def test_one_per_url(self):
        found = url_citations_from_annotations(
            [_url_citation("https://a.org/x"), _url_citation("https://a.org/x")]
        )
        self.assertEqual(len(found), 1)

    def test_skips_other_annotations_and_non_http_urls(self):
        found = url_citations_from_annotations(
            [
                {"type": "file_citation", "file_citation": {"url": "https://a.org"}},
                _url_citation("javascript:alert(1)"),
                {"type": "url_citation", "url_citation": "https://a.org"},
                "https://a.org",
            ]
        )
        self.assertEqual(found, [])

    def test_not_a_list_is_nothing(self):
        self.assertEqual(url_citations_from_annotations({"type": "url_citation"}), [])

    def test_excerpt_is_capped(self):
        found = url_citations_from_annotations(
            [_url_citation("https://a.org/x", content="x" * 10_000)]
        )
        self.assertLess(len(found[0].excerpt), 10_000)


def _denial(**overrides):
    denial = MagicMock(spec=Denial)
    denial.denial_id = 4242
    denial.procedure = "Lumbar MRI"
    denial.diagnosis = "Low back pain"
    denial.use_external = True
    for name, value in overrides.items():
        setattr(denial, name, value)
    return denial


def _results(*urls):
    return [WebCitation(f"Page {u}", u, f"About {u}") for u in urls]


class TestWebSearchCitations:
    """The guardrail: only results judged on topic and not against the
    treatment reach the citations."""

    @pytest.fixture(autouse=True)
    def _wiring(self):
        self.backend = MagicMock()
        self.backend.web_search = AsyncMock(return_value=[])
        self.judgments: dict = {}

        self.judge_kwargs: list = []

        async def judge(result, procedure, diagnosis, **kwargs):
            self.judge_kwargs.append(kwargs)
            return self.judgments.get(result.url)

        with (
            patch.object(
                web_search_context.ml_router,
                "web_search_backend",
                return_value=self.backend,
            ),
            patch.object(
                web_search_context.ml_router, "best_internal_model", return_value=None
            ),
            # Jev is on, so there is a judge to search for.
            patch.object(
                web_search_context.research_judging, "enabled", return_value=True
            ),
            patch.object(
                web_search_context,
                "_consent_allows",
                new_callable=AsyncMock,
                return_value=True,
            ) as consent,
            patch.object(web_search_context, "judge_result", side_effect=judge),
        ):
            self.consent = consent
            yield

    @pytest.mark.asyncio
    async def test_keeps_relevant_results_strongest_support_first(self):
        self.backend.web_search.return_value = _results(
            "https://a.org/weak", "https://a.org/strong"
        )
        self.judgments = {
            "https://a.org/weak": WEAKLY_SUPPORTS,
            "https://a.org/strong": SUPPORTS,
        }
        cited = await web_search_context.web_search_citations(_denial(), 10)
        assert [c.split(" ")[1] for c in cited] == [
            "https://a.org/strong.",
            "https://a.org/weak.",
        ]

    @pytest.mark.asyncio
    async def test_drops_off_topic_and_against_results(self):
        self.backend.web_search.return_value = _results(
            "https://a.org/off", "https://a.org/against", "https://a.org/good"
        )
        self.judgments = {
            "https://a.org/off": OFF_TOPIC,
            "https://a.org/against": AGAINST,
            "https://a.org/good": SUPPORTS,
        }
        cited = await web_search_context.web_search_citations(_denial(), 10)
        assert len(cited) == 1 and "https://a.org/good" in cited[0]

    @pytest.mark.asyncio
    async def test_does_not_search_with_no_judge_at_all(self):
        self.backend.web_search.return_value = _results("https://a.org/x")
        with patch.object(
            web_search_context.research_judging, "enabled", return_value=False
        ):
            await web_search_context.web_search_citations(_denial(), 10)
        self.backend.web_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drops_results_nobody_could_judge(self):
        self.backend.web_search.return_value = _results("https://a.org/x")
        assert await web_search_context.web_search_citations(_denial(), 10) == []

    @pytest.mark.asyncio
    async def test_kept_citation_carries_url_and_labelled_excerpt(self):
        self.backend.web_search.return_value = _results("https://a.org/good")
        self.judgments = {"https://a.org/good": SUPPORTS}
        cited = await web_search_context.web_search_citations(_denial(), 10)
        assert cited == [
            "Page https://a.org/good. https://a.org/good "
            "(web page excerpt: About https://a.org/good)"
        ]

    @pytest.mark.asyncio
    async def test_probably_off_topic_result_is_kept_with_a_note_when_alone(self):
        self.backend.web_search.return_value = _results(
            "https://a.org/maybe", "https://a.org/good"
        )
        self.judgments = {
            "https://a.org/maybe": PROBABLY_OFF_TOPIC,
            "https://a.org/good": SUPPORTS,
        }
        cited = await web_search_context.web_search_citations(_denial(), 10)
        noted = [c for c in cited if "probably off topic" in c]
        assert len(cited) == 2 and len(noted) == 1 and "a.org/maybe" in noted[0]

    @pytest.mark.asyncio
    async def test_probably_off_topic_result_is_left_out_beside_two_on_topic(self):
        self.backend.web_search.return_value = _results(
            "https://a.org/maybe", "https://a.org/good", "https://a.org/also"
        )
        self.judgments = {
            "https://a.org/maybe": PROBABLY_OFF_TOPIC,
            "https://a.org/good": SUPPORTS,
            "https://a.org/also": WEAKLY_SUPPORTS,
        }
        cited = await web_search_context.web_search_citations(_denial(), 10)
        assert not any("a.org/maybe" in c for c in cited) and len(cited) == 2

    @pytest.mark.asyncio
    async def test_keeps_at_most_max_kept(self):
        urls = [f"https://a.org/{i}" for i in range(5)]
        self.backend.web_search.return_value = _results(*urls)
        self.judgments = {u: SUPPORTS for u in urls}
        cited = await web_search_context.web_search_citations(_denial(), 10)
        assert len(cited) == web_search_context.MAX_KEPT

    @pytest.mark.asyncio
    async def test_searches_without_external_models_consent(self):
        # Only the procedure and diagnosis go out, as with a PubMed search.
        self.consent.return_value = False
        self.backend.web_search.return_value = _results("https://a.org/good")
        self.judgments = {"https://a.org/good": SUPPORTS}
        cited = await web_search_context.web_search_citations(
            _denial(use_external=False), 10
        )
        assert len(cited) == 1 and "https://a.org/good" in cited[0]

    @pytest.mark.asyncio
    async def test_jev_may_not_judge_without_consent(self):
        self.consent.return_value = False
        self.backend.web_search.return_value = _results("https://a.org/x")
        await web_search_context.web_search_citations(_denial(), 10)
        assert [k["may_ask_jev"] for k in self.judge_kwargs] == [False]

    @pytest.mark.asyncio
    async def test_jev_may_judge_with_consent(self):
        self.backend.web_search.return_value = _results("https://a.org/x")
        await web_search_context.web_search_citations(_denial(), 10)
        assert [k["may_ask_jev"] for k in self.judge_kwargs] == [True]

    @pytest.mark.asyncio
    async def test_no_search_without_a_procedure(self):
        assert (
            await web_search_context.web_search_citations(_denial(procedure=""), 10)
            == []
        )
        self.backend.web_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_sends_only_procedure_and_diagnosis(self):
        denial = _denial(denial_text="SECRET LETTER", health_history="SECRET HISTORY")
        await web_search_context.web_search_citations(denial, 10)
        question = self.backend.web_search.call_args.args[0]
        assert "Lumbar MRI" in question and "Low back pain" in question
        assert "SECRET" not in question

    @pytest.mark.asyncio
    async def test_no_search_without_a_backend(self):
        with patch.object(
            web_search_context.ml_router, "web_search_backend", return_value=None
        ):
            assert await web_search_context.web_search_citations(_denial(), 10) == []

    @pytest.mark.asyncio
    async def test_a_slow_search_gives_nothing_within_the_budget(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(30)
            return _results("https://a.org/x")

        self.backend.web_search.side_effect = slow
        started = time.monotonic()
        assert await web_search_context.web_search_citations(_denial(), 0.3) == []
        assert time.monotonic() - started < 2

    @pytest.mark.asyncio
    async def test_slow_judging_leaves_those_results_out_within_the_budget(self):
        self.backend.web_search.return_value = _results(
            "https://a.org/fast", "https://a.org/slow"
        )

        async def judge(result, *args, **kwargs):
            if result.url.endswith("slow"):
                await asyncio.sleep(30)
            return SUPPORTS

        started = time.monotonic()
        with patch.object(web_search_context, "judge_result", side_effect=judge):
            cited = await web_search_context.web_search_citations(_denial(), 3)
        assert len(cited) == 1 and "https://a.org/fast" in cited[0]
        assert time.monotonic() - started < 4

    @pytest.mark.asyncio
    async def test_an_error_gives_nothing(self):
        self.backend.web_search.side_effect = RuntimeError("boom")
        assert await web_search_context.web_search_citations(_denial(), 10) == []


class TestJudgeResult:
    @pytest.mark.asyncio
    async def test_uses_our_model_when_jev_is_off(self):
        model = object()
        with (
            patch.object(research_judging, "enabled", return_value=False),
            patch.object(
                research_judging,
                "judge_article_with",
                new_callable=AsyncMock,
                return_value=SUPPORTS,
            ) as own,
        ):
            judgment = await web_search_context.judge_result(
                WebCitation("T", "https://a.org", "excerpt"),
                "MRI",
                "pain",
                judge_model=model,
            )
        assert judgment is SUPPORTS
        assert own.call_args.args[:5] == (model, "T", "excerpt", "MRI", "pain")

    @pytest.mark.asyncio
    async def test_prefers_jev_when_it_answers(self):
        with (
            patch.object(research_judging, "enabled", return_value=True),
            patch.object(research_judging, "skip_reason", return_value=None),
            patch.object(
                research_judging,
                "judge_article",
                new_callable=AsyncMock,
                return_value=AGAINST,
            ),
            patch.object(
                research_judging, "judge_article_with", new_callable=AsyncMock
            ) as own,
        ):
            judgment = await web_search_context.judge_result(
                WebCitation("T", "https://a.org", "excerpt"),
                "MRI",
                "pain",
                judge_model=object(),
                may_ask_jev=True,
            )
        assert judgment is AGAINST
        own.assert_not_called()

    @pytest.mark.asyncio
    async def test_asks_our_model_not_jev_without_consent(self):
        with (
            patch.object(research_judging, "enabled", return_value=True),
            patch.object(research_judging, "skip_reason", return_value=None),
            patch.object(
                research_judging, "judge_article", new_callable=AsyncMock
            ) as jev,
            patch.object(
                research_judging,
                "judge_article_with",
                new_callable=AsyncMock,
                return_value=SUPPORTS,
            ),
        ):
            judgment = await web_search_context.judge_result(
                WebCitation("T", "https://a.org", "excerpt"),
                "MRI",
                "pain",
                judge_model=object(),
                may_ask_jev=False,
            )
        assert judgment is SUPPORTS
        jev.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_judge_is_no_judgment(self):
        with patch.object(research_judging, "enabled", return_value=False):
            judgment = await web_search_context.judge_result(
                WebCitation("T", "https://a.org", "excerpt"), "MRI", "pain"
            )
        assert judgment is None


class TestConsent:
    @pytest.mark.asyncio
    async def test_no_consent_in_memory_is_no(self):
        assert (
            await web_search_context._consent_allows(_denial(use_external=False))
            is False
        )


class TestIsWebCitation:
    @pytest.mark.parametrize(
        "judgment", [None, SUPPORTS, PROBABLY_OFF_TOPIC], ids=["none", "on", "off"]
    )
    @pytest.mark.parametrize("excerpt", ["About it", ""], ids=["excerpt", "bare"])
    def test_every_cited_result_is_recognised(self, judgment, excerpt):
        page = WebCitation("Page", "https://a.org/x", excerpt)
        assert web_search_context.is_web_citation(
            web_search_context.cited(page, judgment)
        )

    @pytest.mark.parametrize(
        "citation",
        ["Smith J. Lumbar MRI. JAMA 2020. PMID 123", "CMS NCD 220.2", "", None],
    )
    def test_other_citations_are_not(self, citation):
        assert not web_search_context.is_web_citation(citation)


class TestCitationStepAppendsWebResults:
    @pytest.mark.asyncio
    async def test_web_citations_follow_ml_and_cms_citations(self):
        denial = MagicMock(spec=Denial)
        denial.pk = None
        denial.denial_id = 7
        denial.procedure = "MRI"
        denial.diagnosis = "Headache"
        denial.denial_text = "Denied"
        denial.health_history = ""
        denial.health_history_consent = None
        denial.plan_context = ""
        denial.ml_citation_context = None
        denial.microsite_slug = None
        denial.use_external = False
        with (
            patch.object(
                MLCitationsHelper,
                "generate_cms_coverage_citations",
                new_callable=AsyncMock,
                return_value=["CMS NCD 1"],
            ),
            patch.object(
                MLCitationsHelper,
                "generate_generic_citations",
                new_callable=AsyncMock,
                return_value=["ML 1"],
            ),
            patch.object(
                MLCitationsHelper,
                "generate_specific_citations",
                new_callable=AsyncMock,
                return_value=["ML 1"],
            ),
            patch(
                "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
                new_callable=AsyncMock,
                return_value=["ML 1"],
            ),
            patch(
                "fighthealthinsurance.ml.ml_citations_helper.web_search_citations",
                new_callable=AsyncMock,
                return_value=["Web 1", "CMS NCD 1"],
            ) as web,
        ):
            result = await MLCitationsHelper._generate_citations_for_denial(
                denial=denial, timeout=30
            )
        assert result == ["ML 1", "CMS NCD 1", "Web 1"]
        assert web.call_args.kwargs["budget_seconds"] < 30


class TestJudgeAllCancellation:
    @pytest.mark.asyncio
    async def test_cancelling_the_caller_cancels_the_judging(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def judge(*args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with (
            patch.object(
                web_search_context.ml_router, "best_internal_model", return_value=None
            ),
            patch.object(web_search_context, "judge_result", side_effect=judge),
        ):
            caller = asyncio.ensure_future(
                web_search_context._judge_all(
                    _results("https://a.org/x"), "MRI", "pain", 30, False
                )
            )
            await asyncio.wait_for(started.wait(), 5)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await asyncio.wait_for(cancelled.wait(), 1)


class TestJudgingTimeSplit:
    @pytest.mark.asyncio
    async def test_jev_gets_half_when_our_model_can_take_over(self):
        with (
            patch.object(research_judging, "enabled", return_value=True),
            patch.object(research_judging, "skip_reason", return_value=None),
            patch.object(
                research_judging,
                "judge_article",
                new_callable=AsyncMock,
                return_value=None,
            ) as jev,
            patch.object(
                research_judging,
                "judge_article_with",
                new_callable=AsyncMock,
                return_value=SUPPORTS,
            ) as own,
        ):
            await web_search_context.judge_result(
                WebCitation("T", "https://a.org", "excerpt"),
                "MRI",
                "pain",
                judge_model=object(),
                may_ask_jev=True,
                timeout_seconds=10,
            )
        assert jev.call_args.kwargs["timeout_seconds"] == 5
        assert 0 < own.call_args.kwargs["timeout_seconds"] <= 10

    @pytest.mark.asyncio
    async def test_jev_gets_all_of_it_with_no_fallback(self):
        with (
            patch.object(research_judging, "enabled", return_value=True),
            patch.object(research_judging, "skip_reason", return_value=None),
            patch.object(
                research_judging,
                "judge_article",
                new_callable=AsyncMock,
                return_value=SUPPORTS,
            ) as jev,
        ):
            await web_search_context.judge_result(
                WebCitation("T", "https://a.org", "excerpt"),
                "MRI",
                "pain",
                may_ask_jev=True,
                timeout_seconds=10,
            )
        assert jev.call_args.kwargs["timeout_seconds"] == 10

    @pytest.mark.asyncio
    async def test_our_model_still_judges_after_a_slow_jev_comes_back_empty(self):
        async def slow_empty(*args, **kwargs):
            await asyncio.sleep(kwargs["timeout_seconds"])
            return None

        with (
            patch.object(
                web_search_context.ml_router,
                "best_internal_model",
                return_value=object(),
            ),
            patch.object(research_judging, "enabled", return_value=True),
            patch.object(research_judging, "skip_reason", return_value=None),
            patch.object(research_judging, "judge_article", side_effect=slow_empty),
            patch.object(
                research_judging,
                "judge_article_with",
                new_callable=AsyncMock,
                return_value=SUPPORTS,
            ),
        ):
            judgments = await web_search_context._judge_all(
                _results("https://a.org/x"), "MRI", "pain", 1.0, True
            )
        assert judgments == {"https://a.org/x": SUPPORTS}


def _rows(value=None, error=None):
    """Denial.objects.filter(...).values(...).afirst() answering ``value``,
    or raising ``error``."""
    query = MagicMock()
    query.values.return_value.afirst = AsyncMock(return_value=value, side_effect=error)
    return query


class TestConsentIsReadAgain:
    @pytest.mark.asyncio
    async def test_consent_withdrawn_since_is_no(self):
        with patch.object(
            Denial.objects, "filter", return_value=_rows({"use_external": False})
        ):
            assert await web_search_context._consent_allows(_denial()) is False

    @pytest.mark.asyncio
    async def test_missing_row_is_no(self):
        with patch.object(Denial.objects, "filter", return_value=_rows(None)):
            assert await web_search_context._consent_allows(_denial()) is False

    @pytest.mark.asyncio
    async def test_unreadable_row_is_no(self):
        with patch.object(
            Denial.objects, "filter", return_value=_rows(error=RuntimeError("db"))
        ):
            assert await web_search_context._consent_allows(_denial()) is False

    @pytest.mark.asyncio
    async def test_consent_still_given_is_yes(self):
        with patch.object(
            Denial.objects, "filter", return_value=_rows({"use_external": True})
        ):
            assert await web_search_context._consent_allows(_denial()) is True


class TestCitationStepCancelsItsLookups:
    @pytest.mark.asyncio
    async def test_cancelling_the_run_cancels_the_web_search(self):
        denial = MagicMock(spec=Denial)
        denial.pk = None
        denial.denial_id = 8
        denial.procedure = "MRI"
        denial.diagnosis = "Headache"
        denial.denial_text = "Denied"
        denial.health_history = ""
        denial.health_history_consent = None
        denial.plan_context = ""
        denial.ml_citation_context = None
        denial.microsite_slug = None
        denial.use_external = False
        ml_started = asyncio.Event()
        web_cancelled = asyncio.Event()

        async def slow_ml(*args, **kwargs):
            ml_started.set()
            await asyncio.sleep(30)

        async def slow_web(*args, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                web_cancelled.set()
                raise

        with (
            patch.object(
                MLCitationsHelper,
                "generate_cms_coverage_citations",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                MLCitationsHelper,
                "generate_generic_citations",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                MLCitationsHelper,
                "generate_specific_citations",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
                side_effect=slow_ml,
            ),
            patch(
                "fighthealthinsurance.ml.ml_citations_helper.web_search_citations",
                side_effect=slow_web,
            ),
        ):
            run = asyncio.ensure_future(
                MLCitationsHelper._generate_citations_for_denial(
                    denial=denial, timeout=30
                )
            )
            await asyncio.wait_for(ml_started.wait(), 5)
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
            await asyncio.wait_for(web_cancelled.wait(), 1)
