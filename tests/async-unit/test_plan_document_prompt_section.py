"""The matched public plan documents in the appeal prompt.

``plan_document_matcher.format_plan_document_context`` renders a guarded
section; this pins where the two prompt builders put it (right after the
payer-policy block, whose last sentence tells the model to defer to the
patient's own plan documents and must not be read as pointing at these
automatically matched excerpts), that nothing renders without it, that the
tier-2 context shed cuts it instead of dropping it and keeps its closing
marker, and that ``make_appeals`` reads it off the denial only while
``PLAN_DOCUMENT_MATCHING_ENABLED`` is on.

The helpers mirror ``test_generate_appeal.py`` (``_mock_denial``, the
MagicMock backend reached through make_appeals' sync ``infer`` seam, the
``calls``/``open_prompt_kwargs`` shapes); this directory's name keeps that
module from being imported by its dotted name.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
    _PLAN_DOCUMENT_CONTEXT_TIER2_CAP,
    _PROMPT_TIER1_NULLS,
    _PROMPT_TIER2_TRUNCATIONS,
    _apply_tier2_truncations,
    _shed_context,
)
from fighthealthinsurance.plan_document_matcher import (
    PLAN_DOCUMENT_END,
    PLAN_DOCUMENT_GUARD,
    truncate_plan_document_context,
)

DENIAL_TEXT = "Your MRI was denied as not medically necessary."
PAYER_POLICY = "Other insurers cover this service under documented criteria."
# The sentence the payer block ends with in both builders.
PAYER_POLICY_TAIL = (
    "Always defer to the patient's own plan documents for what is actually covered."
)
PASSAGE = (
    "[Example Plan EOC (2025), page 7] Imaging is covered when medically necessary."
)
# What format_plan_document_context hands the builders, in miniature.
SECTION = (
    f"{PLAN_DOCUMENT_GUARD}\n"
    "Matched document(s):\n"
    "- Example Plan EOC (2025) -- Evidence of Coverage, CA, "
    "https://example.test/eoc.pdf\n"
    "Relevant passages (page-labelled):\n"
    f"{PASSAGE}\n"
    f"{PLAN_DOCUMENT_END}\n"
)
# Past the tier-2 cap, so a truncation has something to cut; closed by the
# marker like every rendered section.
LONG_SECTION = (
    f"{PLAN_DOCUMENT_GUARD}\nRelevant passages (page-labelled):\n"
    + f"{PASSAGE} " * (_PLAN_DOCUMENT_CONTEXT_TIER2_CAP // len(PASSAGE) + 10)
    + f"\n{PLAN_DOCUMENT_END}\n"
)

BUILDERS = ["make_open_prompt", "make_sectioned_open_prompt"]

_REAL_APPEAL = "Dear insurer, " + "this claim is medically necessary. " * 20


def _builder_kwargs(**overrides):
    kwargs = dict(
        denial_text=DENIAL_TEXT,
        procedure="MRI",
        diagnosis="back pain",
        insurance_company="Acme Health",
        payer_policy_context=PAYER_POLICY,
    )
    kwargs.update(overrides)
    return kwargs


def _build(builder, **overrides):
    prompt = getattr(AppealGenerator(), builder)(**_builder_kwargs(**overrides))
    assert prompt is not None
    return prompt


# --- The builders -----------------------------------------------------------


@pytest.mark.parametrize("builder", BUILDERS)
def test_prompt_builders_render_plan_document_section_after_payer_policy(builder):
    prompt = _build(builder, plan_document_context=SECTION)
    assert SECTION in prompt
    # After the whole payer block, including its closing "defer to the
    # patient's own plan documents", and before the denial letter.
    assert prompt.index(PAYER_POLICY) < prompt.index(PAYER_POLICY_TAIL)
    assert prompt.index(PAYER_POLICY_TAIL) < prompt.index(SECTION)
    assert prompt.index(SECTION) < prompt.index(DENIAL_TEXT)


@pytest.mark.parametrize("builder", BUILDERS)
def test_prompt_builders_render_the_section_once_without_adding_a_label(builder):
    # The renderer frames the section itself (guard, documents, passages,
    # closing marker); the builders add nothing around it.
    prompt = _build(builder, plan_document_context=SECTION)
    assert prompt.count(PLAN_DOCUMENT_GUARD) == 1
    assert prompt.count(PLAN_DOCUMENT_END) == 1


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("absent", [None, ""])
def test_section_absent_when_context_is_none_or_empty(builder, absent):
    with_kwarg = _build(builder, plan_document_context=absent)
    without_kwarg = _build(builder)
    assert PLAN_DOCUMENT_GUARD not in with_kwarg
    assert "PLAN DOCUMENT" not in with_kwarg
    assert with_kwarg == without_kwarg


# --- Context shedding -------------------------------------------------------


def _make_call(prompt):
    """A ``calls``-shape dict, as make_appeals builds them."""
    return {
        "model_name": "fhi-internal",
        "prompt": prompt,
        "patient_context": "patient medical history",
        "plan_context": "plan documents summary",
        "infer_type": "full",
        "pubmed_context": "pubmed citations",
        "ml_citations_context": ["citation-1", "citation-2"],
        "prof_pov": False,
    }


def _prompt_kwargs(**overrides):
    """An ``open_prompt_kwargs`` shape, enrichment non-empty so tier 1 has
    something to null (and so rebuilds)."""
    base = {
        "denial_text": DENIAL_TEXT,
        "procedure": "MRI",
        "diagnosis": "back pain",
        "patient": None,
        "professional": None,
        "qa_context": None,
        "professional_to_finish": False,
        "plan_id": None,
        "claim_id": None,
        "insurance_company": "Acme Health",
        "is_tpa": False,
        "ml_context": "ml ctx",
        "pubmed_context": "pubmed ctx",
        "plan_context": "patient plan body",
        "rag_context": "rag ctx",
        "nice_context": "nice ctx",
        "ucr_context": "ucr ctx",
        "payer_policy_context": PAYER_POLICY,
        "pa_context": "pa ctx",
        "uspstf_context": "uspstf ctx",
        "clinical_trials_context": "clinical trials ctx",
        "medication_context": "med ctx",
        "regulatory_citation_context": "regulatory ctx",
        "plan_law_context": None,
        "plan_document_context": SECTION,
    }
    base.update(overrides)
    return base


def _rebuild_spy(builder):
    """``(seen_kwargs, rebuild)``: the real builder, remembering its kwargs."""
    seen: dict = {}

    def rebuild(**kwargs):
        seen.update(kwargs)
        return builder(**kwargs)

    return seen, rebuild


def _section_in(prompt):
    """The plan-document section as it stands in ``prompt``: from the guard
    through the closing marker's line."""
    start = prompt.index(PLAN_DOCUMENT_GUARD)
    end = prompt.index(PLAN_DOCUMENT_END, start) + len(PLAN_DOCUMENT_END) + 1
    return prompt[start:end]


def test_plan_document_context_is_a_tier_two_truncation_after_plan_context():
    assert _PROMPT_TIER2_TRUNCATIONS[0][0] == "plan_context"
    assert (
        "plan_document_context",
        _PLAN_DOCUMENT_CONTEXT_TIER2_CAP,
    ) in _PROMPT_TIER2_TRUNCATIONS
    assert "plan_document_context" not in _PROMPT_TIER1_NULLS


def test_tier2_shed_truncates_plan_document_context():
    cap = _PLAN_DOCUMENT_CONTEXT_TIER2_CAP
    assert len(LONG_SECTION) > cap
    gen = AppealGenerator()
    kwargs = _prompt_kwargs(plan_document_context=LONG_SECTION)
    original = gen.make_open_prompt(**kwargs)
    seen, rebuild = _rebuild_spy(gen.make_open_prompt)

    new_calls, changed = _shed_context(
        [_make_call(prompt=original)],
        tier=2,
        open_prompt_kwargs=kwargs,
        rebuild_prompt=rebuild,
        original_open_prompt=original,
    )

    assert "prompt.plan_document_context(truncated)" in changed
    shed_section = seen["plan_document_context"]
    assert len(shed_section) <= cap
    assert shed_section == truncate_plan_document_context(LONG_SECTION, cap)
    rebuilt = new_calls[0]["prompt"]
    assert LONG_SECTION not in rebuilt
    assert shed_section in rebuilt
    # In the prompt itself: at most the cap, and still closed by the marker,
    # so what the prompt adds after it stays outside the quoted block.
    in_prompt = _section_in(rebuilt)
    assert len(in_prompt) <= cap
    assert in_prompt.endswith("\n" + PLAN_DOCUMENT_END + "\n")
    assert rebuilt.count(PLAN_DOCUMENT_END) == 1
    # Cut, not dropped: the guard at the head of the section is still there.
    assert PLAN_DOCUMENT_GUARD in rebuilt
    assert in_prompt.startswith(PLAN_DOCUMENT_GUARD)


def test_apply_tier2_truncations_cuts_the_section_and_keeps_its_marker():
    cap = _PLAN_DOCUMENT_CONTEXT_TIER2_CAP
    target = {"plan_document_context": LONG_SECTION, "plan_context": "short"}
    changed: set = set()

    _apply_tier2_truncations(
        target, _PROMPT_TIER2_TRUNCATIONS, changed, label="prompt."
    )

    assert changed == {"prompt.plan_document_context(truncated)"}
    cut = target["plan_document_context"]
    assert len(cut) <= cap
    assert cut.startswith(PLAN_DOCUMENT_GUARD)
    assert cut.endswith("\n" + PLAN_DOCUMENT_END + "\n")
    assert cut.count(PLAN_DOCUMENT_END) == 1
    # The other key was within its cap and is untouched.
    assert target["plan_context"] == "short"


def test_apply_tier2_truncations_leaves_a_section_within_the_cap_alone():
    target = {"plan_document_context": SECTION}
    changed: set = set()

    _apply_tier2_truncations(
        target, _PROMPT_TIER2_TRUNCATIONS, changed, label="prompt."
    )

    assert changed == set()
    assert target["plan_document_context"] is SECTION


def test_tier2_shed_leaves_a_section_within_the_cap_alone():
    assert len(SECTION) <= _PLAN_DOCUMENT_CONTEXT_TIER2_CAP
    gen = AppealGenerator()
    kwargs = _prompt_kwargs(plan_document_context=SECTION)
    seen, rebuild = _rebuild_spy(gen.make_open_prompt)

    new_calls, changed = _shed_context(
        [_make_call(prompt="ORIGINAL")],
        tier=2,
        open_prompt_kwargs=kwargs,
        rebuild_prompt=rebuild,
        original_open_prompt="ORIGINAL",
    )

    assert seen["plan_document_context"] == SECTION
    assert not any("plan_document_context" in c for c in changed)
    assert SECTION in new_calls[0]["prompt"]


def test_tier1_shed_leaves_plan_document_context_alone():
    gen = AppealGenerator()
    kwargs = _prompt_kwargs(plan_document_context=LONG_SECTION)
    seen, rebuild = _rebuild_spy(gen.make_open_prompt)

    new_calls, changed = _shed_context(
        [_make_call(prompt="ORIGINAL")],
        tier=1,
        open_prompt_kwargs=kwargs,
        rebuild_prompt=rebuild,
        original_open_prompt="ORIGINAL",
    )

    # Tier 1 rebuilt (the enrichment came out) but kept the whole section.
    assert "prompt.pubmed_context" in changed
    assert seen["plan_document_context"] == LONG_SECTION
    assert not any("plan_document_context" in c for c in changed)
    assert LONG_SECTION in new_calls[0]["prompt"]


# --- make_appeals -----------------------------------------------------------


def _mock_denial():
    denial = MagicMock()
    denial.denial_id = 42
    denial.use_external = False
    for attr in (
        "qa_context",
        "health_history",
        "plan_context",
        "plan_documents_summary",
        "claim_id",
    ):
        setattr(denial, attr, None)
    denial.professional_to_finish = False
    denial.diagnosis = "dx"
    denial.procedure = "px"
    denial.denial_text = "denial"
    denial.insurance_company = "ins"
    # No structured carrier: the payer-policy auto-build is skipped by the
    # explicit payer_policy_context every test passes.
    denial.insurance_company_obj = None
    return denial


def _fake_backend():
    """A backend reached through make_appeals' sync ``infer`` seam, with the
    in-memory signals the router and the shed ladder read."""
    backend = MagicMock()
    backend.external = False
    backend.is_available.return_value = True
    backend._spend_allows.return_value = True
    backend.get_max_context.return_value = 128000
    backend.infer.return_value = [("full", _REAL_APPEAL)]
    return backend


def _full_prompts_sent(denial, **make_appeals_kwargs):
    """Drive make_appeals against one stubbed internal model; the prompts
    its full-letter calls received."""
    backend = _fake_backend()
    gen = AppealGenerator()
    tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=lambda use_external=False, fail_open=True: ["fhi-a"],
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new={"fhi-a": [backend]},
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        list(
            gen.make_appeals(
                denial,
                tmpl,
                medical_reasons=[],
                non_ai_appeals=[],
                payer_policy_context=PAYER_POLICY,
                **make_appeals_kwargs,
            )
        )
    prompts = [
        c.kwargs["prompt"]
        for c in backend.infer.call_args_list
        if c.kwargs.get("infer_type") == "full"
    ]
    assert prompts, "no full-letter call reached the stubbed backend"
    return prompts


READER = (
    "fighthealthinsurance.plan_document_matcher.get_plan_document_context_for_denial"
)


def test_make_appeals_auto_builds_plan_document_context_when_flag_on():
    denial = _mock_denial()
    with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True), patch(
        READER, return_value=SECTION
    ) as reader:
        prompts = _full_prompts_sent(denial)

    reader.assert_called_once_with(denial)
    assert all(SECTION in p for p in prompts)
    assert all(p.index(PAYER_POLICY_TAIL) < p.index(SECTION) for p in prompts)


def test_make_appeals_skips_the_plan_document_lookup_when_flag_off():
    with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=False), patch(
        READER, return_value=SECTION
    ) as reader:
        prompts = _full_prompts_sent(_mock_denial())

    reader.assert_not_called()
    assert not any(PLAN_DOCUMENT_GUARD in p for p in prompts)


def test_make_appeals_does_not_look_up_a_context_the_caller_passed():
    # "" means the caller decided: no section, and no read of the rows.
    with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True), patch(
        READER, return_value=SECTION
    ) as reader:
        prompts = _full_prompts_sent(_mock_denial(), plan_document_context="")

    reader.assert_not_called()
    assert not any(PLAN_DOCUMENT_GUARD in p for p in prompts)


def test_a_failing_plan_document_lookup_costs_the_section_not_the_appeal():
    with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True), patch(
        READER, side_effect=RuntimeError("database went away")
    ) as reader:
        prompts = _full_prompts_sent(_mock_denial())

    reader.assert_called_once()
    assert all(PAYER_POLICY in p for p in prompts)
    assert not any(PLAN_DOCUMENT_GUARD in p for p in prompts)
