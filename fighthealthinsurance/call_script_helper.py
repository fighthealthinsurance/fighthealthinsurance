"""Phone call script generator.

Implements issue #568 "What to Say on the Phone". Generates an LLM-backed
script the patient (or advocate) can read aloud when calling the insurer.

Caching strategy: a non-PHI ``GenericCallScript`` row is cached per
``(insurer, denial_reason, goal, prompt_version)``. The patient-specific
``CallScript`` row substitutes placeholders (claim id, member id, etc.) at
render time from the parent ``Denial``.
"""

from dataclasses import dataclass
from typing import Optional

from asgiref.sync import sync_to_async
from django.db import IntegrityError
from django.template.loader import render_to_string
from loguru import logger

from fighthealthinsurance.call_script_prompts import (
    PROMPT_VERSION,
    build_generation_prompt,
    get_system_prompts,
)
from fighthealthinsurance.ml.ml_inference import infer_with_fallback
from fighthealthinsurance.models import (
    CallScript,
    CallScriptGoal,
    Denial,
    GenericCallScript,
)


@dataclass
class CallScriptResult:
    call_script: CallScript
    script_text: str
    script_html: str


class CallScriptHelper:
    """Generates and persists phone call scripts for denials."""

    # Aligned with infer_with_fallback() defaults used elsewhere; a script is
    # short so the timeout doesn't need to be as generous as appeal generation.
    DEFAULT_TIMEOUT = 30.0
    DEFAULT_TEMPERATURE = 0.4

    @staticmethod
    def _resolve_insurer(denial: Denial, override: Optional[str]) -> str:
        if override:
            return override.strip()
        # Prefer the structured FK when available; fall back to the free-text
        # CharField that older denials populated before insurance_company_obj
        # was introduced.
        obj = getattr(denial, "insurance_company_obj", None)
        if obj is not None and getattr(obj, "name", None):
            return str(obj.name)
        return (denial.insurance_company or "the insurance company").strip()

    @staticmethod
    def _resolve_denial_reason(denial: Denial, override: Optional[str]) -> str:
        if override:
            return override.strip()
        # denial_type_text is a short normalized label set when the denial is
        # categorized. We deliberately do NOT fall back to denial_text: raw
        # denial letters routinely contain patient names, claim/member IDs,
        # and clinical specifics, and this value is persisted in the
        # non-PHI GenericCallScript cache and sent to the LLM.
        if denial.denial_type_text:
            return denial.denial_type_text.strip()
        return "unspecified"

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.lower().split())

    @classmethod
    async def _generate_script_text(
        cls,
        insurer: str,
        denial_reason: str,
        goal: str,
        cacheable: bool,
    ) -> tuple[Optional[str], Optional[GenericCallScript]]:
        """Return (script_text, generic_script_row).

        When ``cacheable`` is True, look up / write the non-PHI
        GenericCallScript row. When False, bypass the cache entirely -- the
        inputs are user-supplied free text that may contain PHI (e.g. a
        denial_reason override the patient typed verbatim from their letter),
        and the cache is global across all users.
        """

        if cacheable:
            cache_insurer = cls._normalize(insurer)
            cache_reason = cls._normalize(denial_reason)
            try:
                cached = await GenericCallScript.objects.filter(
                    insurer_name=cache_insurer,
                    denial_reason=cache_reason,
                    goal=goal,
                    prompt_version=PROMPT_VERSION,
                ).afirst()
                if cached:
                    return cached.script_text, cached
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Error fetching cached generic call script: {e}"
                )

        text = await infer_with_fallback(
            system_prompts=get_system_prompts(goal),
            prompt=build_generation_prompt(
                insurer=insurer, denial_reason=denial_reason
            ),
            temperature=cls.DEFAULT_TEMPERATURE,
            timeout=cls.DEFAULT_TIMEOUT,
            min_length=120,
            label="call_script",
        )
        if not text:
            logger.warning(f"All models failed to generate call script for goal={goal}")
            return None, None

        if not cacheable:
            return text, None

        try:
            obj, _ = await GenericCallScript.objects.aget_or_create(
                insurer_name=cls._normalize(insurer),
                denial_reason=cls._normalize(denial_reason),
                goal=goal,
                prompt_version=PROMPT_VERSION,
                defaults={"script_text": text},
            )
            return obj.script_text, obj
        except IntegrityError:
            # Another worker raced us and inserted first; re-read and return.
            existing = await GenericCallScript.objects.filter(
                insurer_name=cls._normalize(insurer),
                denial_reason=cls._normalize(denial_reason),
                goal=goal,
                prompt_version=PROMPT_VERSION,
            ).afirst()
            return (existing.script_text if existing else text), existing

    @staticmethod
    def _substitute_placeholders(script_text: str, denial: Denial) -> str:
        """Fill in caller-facing placeholders from the denial when known.

        Placeholders without a value stay in [BRACKETS] so the caller knows to
        speak them aloud from their own records.
        """
        substitutions = {
            "[CLAIM ID]": denial.claim_id or "[CLAIM ID]",
            "[DATE OF SERVICE]": (
                denial.date_of_service
                or denial.date_of_service_text
                or "[DATE OF SERVICE]"
            ),
        }
        result = script_text
        for placeholder, value in substitutions.items():
            if value and value != placeholder:
                result = result.replace(placeholder, value)
        return result

    @classmethod
    def render_printable_html(
        cls,
        script_text: str,
        denial: Optional[Denial],
        goal: str,
        insurer_name: str,
    ) -> str:
        """Render a print-friendly HTML version of the script."""
        return render_to_string(
            "call_script_printable.html",
            {
                "script_text": script_text,
                "denial": denial,
                "goal": goal,
                "goal_display": dict(CallScriptGoal.choices).get(goal, goal),
                "insurer_name": insurer_name,
            },
        )

    @classmethod
    async def generate_call_script(
        cls,
        denial: Denial,
        goal: str,
        insurer_override: Optional[str] = None,
        denial_reason_override: Optional[str] = None,
    ) -> Optional[CallScriptResult]:
        """Generate (or cache-hit) and persist a CallScript for ``denial``."""

        if goal not in CallScriptGoal.values:
            raise ValueError(f"Invalid call script goal: {goal!r}")

        insurer = cls._resolve_insurer(denial, insurer_override)
        denial_reason = cls._resolve_denial_reason(denial, denial_reason_override)

        # Caller-supplied overrides are free text and may contain PHI the
        # user copy-pasted from their denial letter ("John Doe claim 123
        # denied because..."), so we never let them touch the global
        # GenericCallScript cache. Default-path values are derived only from
        # categorized, non-PHI fields and are safe to cache.
        cacheable = insurer_override is None and denial_reason_override is None

        raw_script, generic = await cls._generate_script_text(
            insurer=insurer,
            denial_reason=denial_reason,
            goal=goal,
            cacheable=cacheable,
        )
        if raw_script is None:
            return None

        script_text = cls._substitute_placeholders(raw_script, denial)

        call_script = await CallScript.objects.acreate(
            for_denial=denial,
            goal=goal,
            insurer_name=insurer,
            denial_reason=denial_reason,
            script_text=script_text,
            generic_script=generic,
        )

        script_html = await sync_to_async(cls.render_printable_html)(
            script_text=script_text,
            denial=denial,
            goal=goal,
            insurer_name=insurer,
        )

        return CallScriptResult(
            call_script=call_script,
            script_text=script_text,
            script_html=script_html,
        )
