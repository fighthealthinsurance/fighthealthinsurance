import csv
import re

import icd10
from loguru import logger

from fighthealthinsurance.denial_base import DenialBase
from fighthealthinsurance.medical_code_extractor import (
    extract_cpt_codes,
    extract_hcpcs_codes,
    extract_icd10_codes,
    is_dme_code,
)
from fighthealthinsurance.models import (
    AppealTemplates,
    DenialTypes,
    Diagnosis,
    PlanType,
    Procedures,
    Regulator,
)


# Process all of our "expert system" rules.
class ProcessDenialCodes(DenialBase):
    """Process the denial type based on the procedure codes."""

    def __init__(self):
        self.preventive_denial = DenialTypes.objects.filter(
            name="Preventive Care"
        ).get()
        self.preventive_regex = re.compile(
            r"(exposure to human immunodeficiency virus|preventive|high risk homosexual)",
            re.M | re.UNICODE | re.IGNORECASE,
        )
        try:
            with open("./data/preventitivecodes.csv") as f:
                rows = csv.reader(f)
                self.preventive_codes = {k: v for k, v in rows}
        except Exception:
            self.preventive_codes = {}
        try:
            with open("./data/preventive_diagnosis.csv") as f:
                rows = csv.reader(f)
                self.preventive_diagnosis = {k: v for k, v in rows}
        except Exception:
            self.preventive_diagnosis = {}

    async def get_procedure_and_diagnosis(self, denial_text):
        return (None, None)

    async def get_denialtype(self, denial_text, procedure, diagnosis):
        """Get the denial type. For now short circuit logic."""
        for diag in extract_icd10_codes(denial_text):
            tag = icd10.find(diag)
            if tag is not None:
                if re.search("preventive", tag.block_description, re.IGNORECASE):
                    return [self.preventive_denial]
                if diag in self.preventive_diagnosis:
                    return [self.preventive_denial]
        cpt_codes = extract_cpt_codes(denial_text)
        for code in cpt_codes:
            if code in self.preventive_codes:
                return [self.preventive_denial]
        # HCPCS codes (e.g. DME-only denials) may also be present in the
        # preventive_codes CSV - check them too rather than silently
        # ignoring HCPCS-coded preventive items.
        for code in extract_hcpcs_codes(denial_text):
            if code in self.preventive_codes:
                return [self.preventive_denial]
        return []

    async def get_dme_codes(self, denial_text):
        """Return the set of HCPCS Level II codes from *denial_text*
        that identify durable medical equipment (E/K) or orthotic /
        prosthetic items (L)."""
        return {c for c in extract_hcpcs_codes(denial_text) if is_dme_code(c)}

    async def get_regulator(self, text):
        return []

    async def get_plan_type(self, text):
        return []


class ProcessDenialRegex(DenialBase):
    """Process the denial type based on the regexes stored in the database."""

    def __init__(self):
        self.planTypes = PlanType.objects.all()
        self.regulators = Regulator.objects.all()
        self.denialTypes = DenialTypes.objects.all()
        self.diagnosis = Diagnosis.objects.all()
        self.procedures = Procedures.objects.all()
        self.templates = AppealTemplates.objects.all()

    async def get_procedure(self, text):
        logger.debug(f"Getting procedure types for {text}")
        procedure = None
        async for d in self.procedures:
            logger.debug(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    logger.debug("positive regex match")
                    return s.groups("procedure")[0]
        return None

    async def get_diagnosis(self, text):
        logger.debug(f"Getting diagnosis types ({len(text)} chars)")
        procedure = None
        async for d in self.diagnosis:
            logger.debug(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    logger.debug("positive regex match")
                    return s.groups("diagnosis")[0]
        return None

    async def get_procedure_and_diagnosis(self, text):
        logger.debug("Getting procedure and diagnosis")
        return (await self.get_procedure(text), await self.get_diagnosis(text))

    async def get_denialtype(self, denial_text, procedure, diagnosis):
        logger.debug(f"Getting denial types ({len(denial_text)} chars)")
        denials = []
        async for d in self.denialTypes:
            if (
                (
                    d.regex is not None
                    and d.regex.pattern != ""
                    and d.regex.search(denial_text) is not None
                )
                or (
                    procedure is not None
                    and (
                        d.procedure_regex is not None
                        and d.procedure_regex.search(procedure) is not None
                    )
                )
                or (
                    diagnosis is not None
                    and (
                        d.diagnosis_regex is not None
                        and d.diagnosis_regex.search(diagnosis) is not None
                    )
                )
            ):
                if (
                    d.negative_regex.pattern == ""
                    or d.negative_regex.search(denial_text) is None
                ):
                    logger.debug(f"Found denial type match: {d.name}")
                    denials.append(d)
        logger.debug(f"Collected {len(denials)} denial types")
        return denials

    async def get_denial_types(self, denial_text):
        """
        Get the denial types based on text only (no procedure/diagnosis matching).

        Args:
            denial_text: The text of the denial letter

        Returns:
            List of DenialTypes objects that match the text
        """
        return await self.get_denialtype(denial_text, procedure=None, diagnosis=None)

    async def get_regulator(self, text):
        regulators = []
        async for r in self.regulators:
            if (
                r.regex.search(text) is not None
                and r.negative_regex.search(text) is None
            ):
                regulators.append(r)
        return regulators

    async def get_plan_type(self, text):
        plans = []
        async for p in self.planTypes:
            if p.regex.pattern != "" and p.regex.search(text) is not None:
                logger.debug(f"positive regex match for plan {p}")
                if (
                    p.negative_regex.pattern == ""
                    or p.negative_regex.search(text) is None
                ):
                    plans.append(p)
            else:
                logger.debug(f"no match {p}")
        return plans

    async def get_appeal_templates(self, text, diagnosis):
        templates = []
        async for t in self.templates:
            if t.regex.pattern != "" and t.regex.search(text) is not None:
                # Check if this requires a specific diagnosis
                if t.diagnosis_regex.pattern != "":
                    if (
                        t.diagnosis_regex.search(diagnosis) is not None
                        or diagnosis == ""
                    ):
                        templates.append(t)
                else:
                    templates.append(t)
                logger.debug("yay match")
            else:
                logger.debug(f"no match on {t.regex.pattern}")
        return templates
