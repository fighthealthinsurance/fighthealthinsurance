import re
import csv
import icd10

from fighthealthinsurance.models import (
    AppealTemplates,
    DenialTypes,
    Diagnosis,
    PlanType,
    Procedures,
    Regulator,
)
from fighthealthinsurance.denial_base import DenialBase


# Process all of our "expert system" rules.
class ProcessDenialCodes(DenialBase):
    """Process the denial type based on the procedure codes."""

    def __init__(self):
        self.preventive_denial = DenialTypes.objects.filter(
            name="Preventive Care"
        ).get()
        # These will match many things which are not ICD10 codes or CPT codes but
        # then the lookup will hopefully fail.
        self.icd10_re = re.compile(
            "[\\(\\s:\\.,]+([A-TV-Z][0-9][0-9AB]\\.?[0-9A-TV-Z]{0,4})[\\s:\\.\\),]",
            re.M | re.UNICODE,
        )
        self.cpt_code_re = re.compile(
            "[\\(\\s:,]+(\\d{4,4}[A-Z0-9])[\\s:\\.\\),]", re.M | re.UNICODE
        )
        self.preventive_regex = re.compile(
            "(exposure to human immunodeficiency virus|preventive|high risk homosexual)",
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
        icd_codes = self.icd10_re.finditer(denial_text)
        for i in icd_codes:
            diag = i.group(1)
            tag = icd10.find(diag)
            if tag is not None:
                if re.search("preventive", tag.block_description, re.IGNORECASE):
                    return [self.preventive_denial]
                if diag in self.preventive_diagnosis:
                    return [self.preventive_denial]
        cpt_codes = self.cpt_code_re.finditer(denial_text)
        for i in cpt_codes:
            code = i.group(1)
            if code in self.preventive_codes:
                return [self.preventive_denial]
        return []

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
        print(f"Getting procedure types for {text}")
        procedure = None
        async for d in self.procedures:
            print(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    print("positive regex match")
                    return s.groups("procedure")[0]
        return None

    async def get_diagnosis(self, text):
        print(f"Getting diagnosis types for {text}")
        procedure = None
        async for d in self.diagnosis:
            print(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    print("positive regex match")
                    return s.groups("diagnosis")[0]
        return None

    async def get_procedure_and_diagnosis(self, text):
        print("Getting procedure and diagnosis")
        return (await self.get_procedure(text), await self.get_diagnosis(text))

    async def get_denialtype(self, denial_text, procedure, diagnosis):
        print(f"Getting denial types for {denial_text}")
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
                    print("no negative regex match!")
                    denials.append(d)
        print(f"Collected: {denials}")
        return denials

    async def get_regulator(self, text):
        regulators = []
        for r in self.regulators:
            if (
                r.regex.search(text) is not None
                and r.negative_regex.search(text) is None
            ):
                regulators.append(r)
        return regulators

    async def get_plan_type(self, text):
        plans = []
        for p in self.planTypes:
            if p.regex.pattern != "" and p.regex.search(text) is not None:
                print(f"positive regex match for plan {p}")
                if p.negative_regex != "" or p.negative_regex.search(text) is None:
                    plans.append(p)
            else:
                print(f"no match {p}")
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
                print("yay match")
            else:
                print(f"no match on {t.regex.pattern}")
        return templates
