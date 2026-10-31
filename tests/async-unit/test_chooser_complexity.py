"""The chooser's complexity score (chooser_complexity): what a case's
judging load is made of, how it bands, and the warm-up level a judge's
votes earn. No backends; the scoring command's tests use the database."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from fighthealthinsurance import chooser_complexity as cc
from fighthealthinsurance.models import ChooserCandidate, ChooserTask

_EVIDENCE = {
    "evidence": [
        {"pmid": "1", "title": "Imaging for low back pain", "summary": "It helped."},
        {"pmid": "2", "title": "A second study"},
        {"pmid": "3", "title": ""},  # no title: never shown, never counted
    ]
}


def _draft(words: int) -> str:
    return " ".join(["necessary"] * words)


def _title(index: int) -> str:
    """An article title in words no draft uses, so it is never read as a
    citation of itself."""
    return f"Outcomes of treatment arm {index} in a lumbar imaging cohort"


class TestBands:
    def test_the_lightest_case_is_a_one(self):
        assert cc.score_task(None, []) == cc.COMPLEXITY_MIN
        assert cc.score_task({}, [""]) == cc.COMPLEXITY_MIN

    def test_each_ceiling_is_the_last_minute_of_its_band(self):
        assert cc.band(0) == 1
        assert cc.band(8) == 1
        assert cc.band(8.01) == 2
        assert cc.band(14) == 2
        assert cc.band(21) == 3
        assert cc.band(30) == 4
        assert cc.band(30.5) == 5
        assert cc.band(10_000) == cc.COMPLEXITY_MAX

    def test_more_words_never_lower_the_score(self):
        sizes = (50, 200, 500, 900, 1300, 2000)
        scores = [cc.score_task({}, [_draft(n)] * 4) for n in sizes]
        assert scores == sorted(scores)
        assert scores[0] == 1
        assert scores[-1] == cc.COMPLEXITY_MAX

    def test_a_typical_appeal_round_lands_mid_scale(self):
        # Four 600-word drafts and a 600-word synthesized one, five articles
        # with summaries, a fact list and an interview, two citations a
        # draft: what a seeded appeal task usually is.
        context = {
            "denial_text_preview": _draft(30),
            "facts": [_draft(12)] * 5,
            "qa_pairs": [{"question": _draft(10), "answer": _draft(25)}] * 4,
            "evidence": [
                {"pmid": str(i), "title": _title(i), "summary": _draft(60)}
                for i in range(5)
            ],
        }
        cited = " (PMID 1234567) (Smith et al., 2020)"
        drafts = [_draft(600) + cited for _ in range(4)] + [_draft(600)]
        assert cc.breakdown(context, drafts).citations == 8
        assert cc.score_task(context, drafts) == 3

    def test_the_lightest_case_with_evidence_is_still_a_one(self):
        # Four 300-word drafts, one article with a summary, a citation each.
        context = {
            "denial_text_preview": _draft(20),
            "facts": [_draft(10)] * 4,
            "evidence": [{"pmid": "1", "title": _title(1), "summary": _draft(60)}],
        }
        drafts = [_draft(300) + " (PMID 1234567)" for _ in range(4)]
        assert cc.score_task(context, drafts) == 1


class TestBreakdown:
    def test_the_case_text_counts_as_reading(self):
        context = {
            "procedure": "MRI",
            "diagnosis": "low back pain",
            "insurance_company": "Fictional Mutual",
            "denial_text_preview": "not medically necessary",
            "facts": ["in their 40s", "eight months of pain"],
            "qa_pairs": [{"question": "How long?", "answer": "Eight months now."}],
            "evidence": [
                {"pmid": "1", "title": "Imaging study", "summary": "It helped a lot."}
            ],
        }
        detail = cc.breakdown(context, ["Dear reviewer"])
        words_in_case = 1 + 3 + 2 + 3 + (3 + 4) + (2 + 3) + (2 + 4)
        assert detail.words == 2 + words_in_case

    def test_a_chat_case_reads_its_history_and_question(self):
        context = {
            "prompt": "What can I do next?",
            "history": [
                {"role": "user", "content": "My prior auth was denied"},
                {"role": "assistant", "content": "I can help with that."},
            ],
        }
        assert cc.breakdown(context, []).words == 5 + 5 + 5

    def test_only_titled_evidence_is_an_article(self):
        assert cc.breakdown(_EVIDENCE, []).articles == 2
        assert cc.breakdown({"evidence": None}, []).articles == 0
        assert cc.breakdown(None, []).articles == 0

    def test_empty_drafts_are_not_drafts(self):
        assert cc.breakdown({}, ["a letter", "", None]).drafts == 1

    def test_the_evidence_is_read_and_citations_are_checked(self):
        plain = cc.breakdown({}, [_draft(100)])
        with_evidence = cc.breakdown(_EVIDENCE, [_draft(100)])
        with_citations = cc.breakdown({}, [_draft(100) + " (PMID 12345678) et al."])
        # The list's titles and summaries are words to read, nothing more.
        assert with_evidence.minutes == pytest.approx(plain.minutes + 10 / 230)
        # A citation is its words read, plus the check against the list.
        assert with_citations.citations == 2
        assert with_citations.minutes == pytest.approx(104 / 230 + 2 * 0.2)


class TestCitations:
    @pytest.mark.parametrize(
        "text",
        [
            "PMID 12345678",
            "pmid: 1234",
            "https://pubmed.ncbi.nlm.nih.gov/999/",
            "doi: 10.1000/xyz",
            "Smith et al. found",
            "as shown [2]",
            "(Smith, 2021)",
            "(Smith 2021)",
            "(Jones and Lee 2019)",
            "(Jones & O'Brien, 2019)",
            "(Smith et al., 2020)",
        ],
    )
    def test_each_kind_of_citation_counts_once(self, text):
        assert cc.count_citations([f"The letter says {text} and goes on."]) == 1

    def test_an_article_cited_by_its_title_counts(self):
        drafts = [
            "As Imaging For Low Back Pain found, imaging for low back pain helps."
        ]
        assert cc.count_citations(drafts, _EVIDENCE) == 2
        assert cc.count_citations(drafts) == 0

    def test_a_short_title_is_too_common_a_phrase_to_count(self):
        context = {"evidence": [{"pmid": "1", "title": "Imaging"}]}
        assert cc.count_citations(["Imaging helps. Imaging is safe."], context) == 0

    def test_a_bare_year_is_not_a_citation(self):
        assert cc.count_citations(["In 2021 the plan paid for this (see page 2)."]) == 0

    def test_citations_are_capped(self):
        assert cc.count_citations(["[1] " * 50, "[2] " * 50]) == 20


class TestWarmupLevel:
    def test_one_vote_a_level_by_default(self):
        assert [cc.warmup_level(r) for r in range(0, 6)] == [1, 2, 3, 4, 5, 5]

    def test_the_level_tops_out_at_the_maximum(self):
        assert cc.warmup_level(1_000) == cc.COMPLEXITY_MAX

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=2)
    def test_two_votes_a_level(self):
        assert [cc.warmup_level(r) for r in range(0, 9)] == [1, 1, 2, 2, 3, 3, 4, 4, 5]

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=0)
    def test_zero_turns_the_warm_up_off(self):
        assert cc.warmup_level(0) == cc.COMPLEXITY_MAX

    def test_negative_rounds_read_as_none(self):
        assert cc.warmup_level(-3) == cc.COMPLEXITY_MIN


def _task(status="READY", task_type="appeal", complexity=None, words=20):
    task = ChooserTask.objects.create(
        task_type=task_type, status=status, source="synthetic", complexity=complexity
    )
    for index in range(2):
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=index,
            kind="appeal_letter" if task_type == "appeal" else "chat_response",
            model_name=f"model-{index}",
            content=_draft(words),
        )
    return task


def _score(*args):
    out = StringIO()
    call_command("score_chooser_complexity", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestScoreCommand:
    def test_unscored_ready_tasks_are_scored(self):
        task = _task()
        out = _score()
        task.refresh_from_db()
        assert task.complexity == 1
        assert "Scored 1 task(s); 1 with a new score" in out
        assert "complexity 1: 1" in out

    def test_a_scored_task_is_left_alone_unless_rescoring(self):
        task = _task(complexity=4)
        _score()
        task.refresh_from_db()
        assert task.complexity == 4
        _score("--rescore")
        task.refresh_from_db()
        assert task.complexity == 1

    def test_a_dry_run_stores_nothing(self):
        task = _task()
        out = _score("--dry-run")
        task.refresh_from_db()
        assert task.complexity is None
        assert (
            "Would score 1 task(s); 1 with a new score (dry run, nothing stored)" in out
        )

    def test_only_ready_tasks_are_scored(self):
        queued = _task(status="QUEUED")
        disabled = _task(status="DISABLED")
        _score()
        queued.refresh_from_db()
        disabled.refresh_from_db()
        assert queued.complexity is None
        assert disabled.complexity is None

    def test_a_task_type_can_be_chosen(self):
        appeal = _task()
        chat = _task(task_type="chat")
        _score("--task-type", "chat")
        appeal.refresh_from_db()
        chat.refresh_from_db()
        assert appeal.complexity is None
        assert chat.complexity == 1

    def test_verbose_output_shows_the_breakdown(self):
        task = _task()
        out = _score("-v", "2")
        assert f"task {task.id} (appeal): 1 (40 words in 2 drafts and the case" in out
