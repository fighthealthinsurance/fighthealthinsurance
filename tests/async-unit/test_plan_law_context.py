"""The APPLICABLE APPEAL LAW block: which law governs the plan, from what
intake collected, and an invitation to cite it where it helps.

Every statute here is cited by name and section and nothing is quoted; the
allowlist below is the full set, kept in the test on purpose so adding a
citation means touching this file and checking the authority.
"""

import inspect
import re
from types import SimpleNamespace

import pytest

from fighthealthinsurance import generate_appeal
from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.regulatory_citations import (
    PLAN_LAW_HEADER,
    get_plan_law_context,
)

CITATION_ALLOWLIST = {
    "29 U.S.C. § 1133",
    "29 C.F.R. § 2560.503-1",
    "45 C.F.R. § 147.136",
    "29 C.F.R. § 2590.715-2719",
    "42 U.S.C. § 18022",
    "29 U.S.C. § 1003(b)(1)",
    "5 U.S.C. chapter 89",
    "5 C.F.R. § 890.105",
    "42 C.F.R. Part 422, Subpart M",
    "42 C.F.R. Part 405, Subpart I",
    "42 C.F.R. Part 438, Subpart F",
    "42 C.F.R. Part 431, Subpart E",
}
# A section number is digits, dots and dashes, then any number of
# parenthesised subsections; trailing prose punctuation is not part of it.
_CITATION = re.compile(
    r"\d+ (?:U\.S\.C\.|C\.F\.R\.) "
    r"(?:§ \d[\d.-]*(?:\([a-z0-9]+\))*|Part \d+, Subpart [A-Z]|chapter \d+)"
)


def _block(*sources, **kw):
    return get_plan_law_context(list(sources), **kw)


class TestWhichLawGoverns:
    @pytest.mark.parametrize("source", ["Employer -- Private   ", "Union", "Other Group"])
    def test_private_employer_and_union_plans_are_erisa(self, source):
        block = _block(source)
        assert "ERISA governs the appeal" in block
        assert "29 C.F.R. § 2560.503-1" in block
        assert "45 C.F.R. § 147.136" in block  # external review for non-grandfathered

    @pytest.mark.parametrize(
        "source", ["Employer -- State Government ", "Employer -- Other Government"]
    )
    def test_government_employer_plans_are_not_erisa_but_keep_aca_rights(self, source):
        block = _block(source)
        assert "ERISA does not apply (29 U.S.C. § 1003(b)(1))" in block
        assert "45 C.F.R. § 147.136" in block
        assert "ERISA governs" not in block

    def test_federal_employees_get_fehb_not_erisa(self):
        block = _block("Employer -- Federal Government")
        assert "5 C.F.R. § 890.105" in block
        assert "ERISA does not apply" in block
        assert "ERISA governs" not in block

    def test_marketplace_plans_are_aca_not_erisa(self):
        block = _block("State Marketplace / Affordable Care Act")
        assert "ERISA does not apply" in block
        assert "45 C.F.R. § 147.136" in block
        assert "42 U.S.C. § 18022" in block

    @pytest.mark.parametrize(
        "source, marker",
        [
            ("Medicare Advantage", "42 C.F.R. Part 422, Subpart M"),
            ("Medicare Regular", "42 C.F.R. Part 405, Subpart I"),
            ("Medicaid  ", "42 C.F.R. Part 438, Subpart F"),
            ("Veterans Affairs", "VA has its own clinical appeal process"),
        ],
    )
    def test_public_programs_name_their_own_process_and_rule_out_both_laws(self, source, marker):
        block = _block(source)
        assert marker in block
        assert "Neither ERISA nor the ACA appeal rules apply" in block

    def test_medicare_advantage_is_not_read_as_original_medicare(self):
        block = _block("Medicare Advantage")
        assert "Part 405" not in block

    @pytest.mark.parametrize("sources", [(), ("Other",), ("Don't know",), ("",)])
    def test_an_unknown_source_gets_the_hedged_paragraph(self, sources):
        block = _block(*sources)
        assert "We do not know how the patient gets this coverage" in block
        assert "only when the letter itself supports it" in block
        assert "ERISA governs" not in block

    def test_a_tpa_carrier_means_erisa_even_with_no_source(self):
        block = _block(is_tpa=True)
        assert "ERISA governs the appeal" in block
        assert "We do not know" not in block

    def test_the_erisa_regulator_match_means_erisa(self):
        block = _block("Don't know", regulator_alt_name="erisa")
        assert "ERISA governs the appeal" in block

    def test_two_sources_both_appear_with_a_tie_break(self):
        block = _block("Medicare Advantage", "Employer -- Private")
        assert "Medicare Advantage plan" in block
        assert "ERISA governs the appeal" in block
        assert "More than one coverage source was given" in block

    def test_one_source_twice_is_one_paragraph(self):
        block = _block("Employer -- Private", "Union", is_tpa=True)
        assert block.count("ERISA governs the appeal") == 1
        assert "More than one coverage source" not in block


class TestTheBlockItself:
    ALL = [
        _block("Employer -- Private"),
        _block("Employer -- State Government"),
        _block("Employer -- Federal Government"),
        _block("State Marketplace / Affordable Care Act"),
        _block("Medicare Advantage"),
        _block("Medicare Regular"),
        _block("Medicaid"),
        _block("Veterans Affairs"),
        _block(),
        _block("Medicare Advantage", "Employer -- Private"),
    ]

    @pytest.mark.parametrize("block", ALL)
    def test_every_block_invites_and_guards(self, block):
        assert block.startswith(f"{PLAN_LAW_HEADER}: ")
        assert "If citing the applicable law strengthens this appeal" in block
        assert "Never cite a law that does not govern this plan" in block
        assert "do not invent section numbers" in block

    @pytest.mark.parametrize("block", ALL)
    def test_every_citation_is_on_the_allowlist(self, block):
        # The VA paragraph cites nothing on purpose; every other block does.
        found = set(_CITATION.findall(block))
        assert found or "Veterans Affairs" in block, block
        assert found <= CITATION_ALLOWLIST, found - CITATION_ALLOWLIST

    def test_the_allowlist_is_all_used(self):
        used = set()
        for block in self.ALL:
            used |= set(_CITATION.findall(block))
        assert used == CITATION_ALLOWLIST

    @pytest.mark.parametrize("block", ALL)
    def test_house_style(self, block):
        assert "\u2014" not in block


class TestPromptWiring:
    def test_make_open_prompt_appends_the_block_when_given(self):
        block = _block("Employer -- Private")
        prompt = AppealGenerator().make_open_prompt(
            denial_text="Service denied.", plan_law_context=block
        )
        assert prompt is not None
        assert block in prompt

    def test_make_open_prompt_is_unchanged_without_it(self):
        prompt = AppealGenerator().make_open_prompt(denial_text="Service denied.")
        assert prompt is not None
        assert PLAN_LAW_HEADER not in prompt

    def test_the_block_survives_context_shedding(self):
        assert "plan_law_context" not in generate_appeal._PROMPT_TIER1_NULLS

    def test_make_appeals_passes_the_block_through(self):
        src = inspect.getsource(AppealGenerator.make_appeals)
        assert "plan_law_context = self._collect_plan_law_context(denial)" in src
        assert "plan_law_context=plan_law_context," in src


class _Sources:
    def __init__(self, *names, raise_=None):
        self._names = names
        self._raise = raise_

    def all(self):
        if self._raise:
            raise self._raise
        return [SimpleNamespace(name=n) for n in self._names]


class TestCollector:
    def test_reads_plan_sources_from_the_denial(self):
        denial = SimpleNamespace(plan_source=_Sources("State Marketplace / Affordable Care Act"))
        block = AppealGenerator._collect_plan_law_context(denial)
        assert block is not None and "marketplace (Affordable Care Act) plan" in block

    def test_reads_the_tpa_flag_and_the_regulator(self):
        denial = SimpleNamespace(
            plan_source=_Sources(),
            insurance_company_obj=SimpleNamespace(is_tpa=True),
        )
        assert "ERISA governs" in (AppealGenerator._collect_plan_law_context(denial) or "")
        denial = SimpleNamespace(
            plan_source=_Sources(), regulator=SimpleNamespace(alt_name="ERISA")
        )
        assert "ERISA governs" in (AppealGenerator._collect_plan_law_context(denial) or "")

    def test_a_bare_denial_still_gets_the_hedged_block(self):
        block = AppealGenerator._collect_plan_law_context(SimpleNamespace())
        assert block is not None and "We do not know" in block

    def test_a_failing_lookup_returns_none_instead_of_raising(self):
        denial = SimpleNamespace(plan_source=_Sources(raise_=RuntimeError("db down")))
        assert AppealGenerator._collect_plan_law_context(denial) is None

    def test_a_lazy_regulator_that_raises_does_not_lose_the_block(self):
        class Denial:
            plan_source = _Sources("Employer -- Private")

            @property
            def regulator(self):
                raise RuntimeError("SynchronousOnlyOperation")

        block = AppealGenerator._collect_plan_law_context(Denial())
        assert block is not None and "ERISA governs" in block
