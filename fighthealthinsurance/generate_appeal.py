import asyncio
import itertools
import json
import random
import re
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Iterator, List, Optional, Tuple, TypeVar

from loguru import logger


@dataclass
class GeneratedAppeal:
    """An appeal text along with the model that produced it.

    Threaded through the streaming pipeline so ProposedAppeal rows can be
    saved with their originating model_name for downstream analytics.
    """

    text: str
    model_name: Optional[str]


from fighthealthinsurance.context_utils import truncate_at_boundary
from fighthealthinsurance.denial_base import DenialBase

from .exec import executor
from .ml.ml_models import RemoteFullOpenLike, RemoteModelLike, repetition_penalty
from .ml.ml_router import ml_router
from .payer_policy_helper import (
    get_combined_payer_policy_context,
    resolve_company_from_text,
)
from .process_denial import ProcessDenialRegex
from .pubmed_tools import PubMedTools
from .utils import as_available_nested, best_within_timelimit, is_real_appeal


class AppealTemplateGenerator(object):
    def __init__(self, prefaces: list[str], main: list[str], footer: list[str]):
        self.prefaces = prefaces
        self.main = main
        self.footer = footer
        self.combined = str("\n".join(prefaces + main + footer))

    def generate_static(self):
        if "{medical_reason}" not in self.combined and self.combined != "":
            return self.combined
        else:
            return None

    def generate(self, medical_reason: str):
        result = self.combined.replace("{medical_reason}", medical_reason)
        if result != "":
            return result
        else:
            return None


class SpecializedDenialTemplate(object):
    """Base class for specialized denial-type appeal templates.

    Subclasses encode the specific laws/regulations to cite, a detection
    rule, a fully-formed static letter (suitable for ``non_ai_appeals``),
    and a prompt hint to nudge the largest model toward the right citations.

    Placeholders ``{insurance_company}``, ``{claim_id}``, ``{procedure}``,
    and ``{diagnosis}`` are filled in downstream by ``sub_in_appeals``.
    """

    name: str = ""
    text_patterns: tuple[str, ...] = ()
    procedure_patterns: tuple[str, ...] = ()
    diagnosis_patterns: tuple[str, ...] = ()
    negative_patterns: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()

    # IGNORECASE for forgiving matching across denial-letter prose;
    # DOTALL so `.*` between key phrases (used in multi-keyword patterns
    # like "post-surgical … rehab") still matches when the phrases are
    # split across newlines, which is common in real denial letters.
    _MATCH_FLAGS = re.IGNORECASE | re.DOTALL

    @classmethod
    def matches(
        cls,
        denial_text: Optional[str],
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
    ) -> bool:
        text = denial_text or ""
        proc = procedure or ""
        diag = diagnosis or ""

        # Negative patterns are checked against every field we match on,
        # not just denial_text — otherwise an exclusion phrase that lives
        # in the procedure or diagnosis (e.g., a "physician assistant"
        # specialty noted in the procedure field) would not block a
        # spurious match.
        if cls.negative_patterns:
            for p in cls.negative_patterns:
                for field in (text, proc, diag):
                    if field and re.search(p, field, cls._MATCH_FLAGS):
                        return False

        if cls.text_patterns and any(
            re.search(p, text, cls._MATCH_FLAGS) for p in cls.text_patterns
        ):
            return True
        if (
            cls.procedure_patterns
            and proc
            and any(
                re.search(p, proc, cls._MATCH_FLAGS) for p in cls.procedure_patterns
            )
        ):
            return True
        if (
            cls.diagnosis_patterns
            and diag
            and any(
                re.search(p, diag, cls._MATCH_FLAGS) for p in cls.diagnosis_patterns
            )
        ):
            return True
        return False

    @classmethod
    def static_appeal(cls) -> str:
        raise NotImplementedError

    @classmethod
    def model_prompt_hint(cls) -> str:
        bullets = "\n".join(f"- {c}" for c in cls.citations)
        return (
            f"This denial appears to involve {cls.name}. "
            "When writing the appeal, ground the argument in the following "
            "authorities and cite them by name where they support the patient's "
            f"position:\n{bullets}\n"
            "Do not fabricate case numbers, regulatory paragraphs, or quote text "
            "you cannot verify; cite the laws/rules by name and section only."
        )


class MentalHealthParityAppeal(SpecializedDenialTemplate):
    name = "Mental Health Parity / behavioral-health denial"
    text_patterns = (
        r"\bmental\s+health\b",
        r"\bbehavioral\s+health\b",
        r"\bsubstance\s+(?:use|abuse)\b",
        r"\bpsychiatric\b",
        r"\bpsychotherap",
        r"\b(?:addiction|opioid\s+use\s+disorder|alcohol\s+use\s+disorder)\b",
        r"\beating\s+disorder\b",
        r"\bresidential\s+treatment\b",
        r"\bpartial\s+hospitalization\b",
        r"\bintensive\s+outpatient\b",
        r"\b(?:applied\s+behavior\s+analysis|ABA\s+therapy)\b",
    )
    diagnosis_patterns = (
        r"\b(?:depression|major\s+depressive|anxiety|bipolar|schizophreni|"
        r"ptsd|adhd|autism|substance\s+use\s+disorder|alcohol\s+use\s+disorder|"
        r"eating\s+disorder|anorexia|bulimia)\b",
    )
    citations = (
        "Mental Health Parity and Addiction Equity Act of 2008 (MHPAEA), "
        "29 U.S.C. § 1185a",
        "CMS 2024 Final Rule on the use of algorithms and artificial intelligence "
        "in coverage determinations",
        "Affordable Care Act Essential Health Benefits, "
        "42 U.S.C. § 18022(b)(1)(E) (mental health and substance use disorder services)",
        "Department of Labor 2024 MHPAEA Final Rules on Nonquantitative Treatment "
        "Limitations (NQTLs)",
    )

    @classmethod
    def static_appeal(cls) -> str:
        return (
            "Re: Appeal of denied behavioral-health claim {claim_id}\n\n"
            "Dear {insurance_company},\n\n"
            "I am formally appealing the denial of coverage for {procedure} "
            "(diagnosis: {diagnosis}). Because this denial concerns mental "
            "health or substance use disorder services, it is governed by "
            "behavioral-health parity protections that the carrier must satisfy "
            "before any adverse determination can stand.\n\n"
            "1. Mental Health Parity and Addiction Equity Act (MHPAEA, "
            "29 U.S.C. § 1185a). Any nonquantitative treatment limitation "
            "(NQTL) applied here — including medical-necessity criteria, "
            "prior-authorization rules, fail-first/step-therapy protocols, "
            "concurrent review, network composition, or reimbursement "
            "methodology — must be no more restrictive in writing or in "
            "operation than the comparable NQTLs applied to medical/surgical "
            "benefits in the same classification. Please produce the "
            "comparative analysis required by ERISA § 712(a)(8) and the "
            "2024 DOL Final Rules for the NQTL relied upon in this denial.\n\n"
            "2. CMS 2024 Final Rule on algorithmic coverage determinations. "
            "If an algorithm, predictive model, or AI tool contributed to "
            "this denial, the determination must still rest on an "
            "individualized clinical assessment by a qualified human reviewer "
            "applying the patient's full clinical picture; the algorithm may "
            "not be the sole basis for the adverse decision. Please disclose "
            "whether such a tool was used, identify it, and provide the "
            "human reviewer's clinical reasoning.\n\n"
            "3. Affordable Care Act Essential Health Benefits "
            "(42 U.S.C. § 18022(b)(1)(E)). Mental health and substance use "
            "disorder services, including behavioral-health treatment, are "
            "designated essential health benefits and may not be effectively "
            "excluded by NQTLs that are more restrictive than those used on "
            "the medical/surgical side.\n\n"
            "I respectfully request that you (a) overturn this denial, "
            "(b) provide the MHPAEA NQTL comparative analysis for the "
            "criterion applied, and (c) confirm in writing what role, if any, "
            "an algorithm or AI tool played in the determination.\n\n"
            "Sincerely,\n[Name]\n"
        )


class AdvancedImagingAppeal(SpecializedDenialTemplate):
    name = "Advanced imaging denial (MRI/CT/PET)"
    text_patterns = (
        r"\bMRI\b",
        r"\bmagnetic\s+resonance\b",
        r"\bMRA\b",
        r"\bMRCP\b",
        r"\bCT\s+scan\b",
        r"\bcomputed\s+tomograph",
        r"\bPET\s+(scan|/CT)\b",
        r"\bpositron\s+emission\b",
        r"\bnuclear\s+medicine\b",
        r"\badvanced\s+imaging\b",
    )
    procedure_patterns = (
        r"\b(MRI|MRA|MRCP|CT|CTA|PET|SPECT)\b",
        r"\bimaging\b",
    )
    citations = (
        "American College of Radiology (ACR) Appropriateness Criteria",
        "CMS National Coverage Determinations for diagnostic imaging "
        "(NCD Manual chapter 220)",
        "CMS 2024 Final Rule on the use of algorithms and artificial intelligence "
        "in coverage determinations",
    )

    @classmethod
    def static_appeal(cls) -> str:
        return (
            "Re: Appeal of denied advanced imaging claim {claim_id}\n\n"
            "Dear {insurance_company},\n\n"
            "I am appealing the denial of {procedure} ordered for {diagnosis}. "
            "Advanced imaging decisions should be made under nationally "
            "recognized appropriateness criteria, not blanket utilization-"
            "management rules.\n\n"
            "1. ACR Appropriateness Criteria. The American College of "
            "Radiology publishes evidence-based criteria identifying the "
            "appropriate imaging modality for specific clinical scenarios. "
            "The presentation in this case meets ACR-published indications "
            "for the requested study; lower-cost alternatives such as plain "
            "radiographs or ultrasound are inadequate to answer the clinical "
            "question and would predictably lead to repeat imaging.\n\n"
            "2. CMS National Coverage Determinations (NCD chapter 220). "
            "CMS NCDs set the community standard for advanced imaging "
            "coverage and the requested study satisfies the relevant NCD's "
            "indications.\n\n"
            "3. CMS 2024 Final Rule on algorithmic coverage determinations. "
            "If an automated utilization-management tool drove this denial, "
            "the carrier must still produce an individualized clinical "
            "review by a qualified human reviewer; the algorithm cannot be "
            "the sole basis for the adverse decision. Please disclose "
            "whether such a tool was used and provide the reviewer's "
            "clinical rationale.\n\n"
            "Delaying or denying this imaging risks missed diagnoses and "
            "downstream cost from lower-yield workups. I respectfully "
            "request that the denial be overturned.\n\n"
            "Sincerely,\n[Name]\n"
        )


class SpecialtyMedicationAppeal(SpecializedDenialTemplate):
    name = "Specialty medication denial"
    text_patterns = (
        r"\bspecialty\s+(?:drug|medication|pharmac)",
        r"\bbiologic\b",
        r"\b(?:infliximab|adalimumab|etanercept|ustekinumab|secukinumab|"
        r"vedolizumab|rituximab|tocilizumab|dupilumab|omalizumab)\b",
        r"\b(?:humira|enbrel|stelara|remicade|cosentyx|entyvio|dupixent)\b",
        r"\b(?:GLP-?1|semaglutide|tirzepatide|liraglutide)\b",
        r"\b(?:ozempic|wegovy|mounjaro|zepbound)\b",
        r"\bgene\s+therapy\b",
        r"\bcar[\s\-]t\b",
        r"\boncology\s+(?:infusion|drug|therapy)\b",
        r"\bstep\s+therapy\b",
        r"\bfail[\s\-]first\b",
        r"\bnon[\s\-]formulary\b",
    )
    procedure_patterns = (
        r"\bspecialty\b",
        r"\binfusion\b",
        r"\bbiologic\b",
    )
    citations = (
        "Affordable Care Act non-discrimination provisions, "
        "42 U.S.C. § 18116 (Section 1557)",
        "ERISA § 503 / 29 C.F.R. § 2560.503-1 (full and fair review, "
        "including the carrier's clinical criteria and reviewer credentials)",
        "State step-therapy override statutes (where applicable; the "
        "exception is required when stepping has been tried, is "
        "contraindicated, or is expected to be ineffective)",
        "CMS 2024 Final Rule on the use of algorithms and artificial intelligence "
        "in coverage determinations",
    )

    @classmethod
    def static_appeal(cls) -> str:
        return (
            "Re: Appeal of denied specialty-medication claim {claim_id}\n\n"
            "Dear {insurance_company},\n\n"
            "I am appealing the denial of {procedure} for {diagnosis}. "
            "Specialty medications are typically prescribed because lower-"
            "tier alternatives are unsuitable for the patient's clinical "
            "circumstances; the denial as issued does not engage with that "
            "individualized analysis.\n\n"
            "1. Step-therapy / fail-first override. Where the carrier's "
            "denial relies on step therapy, the patient qualifies for an "
            "override under applicable state law and plan terms because "
            "preferred agents are contraindicated, have been tried and "
            "failed, or are expected to be ineffective for this indication. "
            "Please apply the override and process the claim.\n\n"
            "2. ERISA full-and-fair-review obligations "
            "(29 C.F.R. § 2560.503-1). Please produce, with the appeal "
            "decision, (a) the specific clinical criteria relied on, "
            "(b) the credentials of the reviewing clinician, and (c) any "
            "internal rule, guideline, protocol, or similar criterion that "
            "was used. Generic 'not medically necessary' language without "
            "this disclosure does not satisfy the regulation.\n\n"
            "3. CMS 2024 Final Rule on algorithmic coverage determinations. "
            "If an automated tool flagged this prescription, the final "
            "denial must still rest on an individualized clinical review by "
            "a qualified human reviewer applying this patient's full "
            "clinical picture, not solely on an algorithmic output.\n\n"
            "I respectfully request that the denial be overturned and the "
            "medication authorized without further delay.\n\n"
            "Sincerely,\n[Name]\n"
        )


class PhysicalTherapyContinuationAppeal(SpecializedDenialTemplate):
    name = "Physical therapy continuation (visits beyond initial sessions)"
    text_patterns = (
        r"\bphysical\s+therapy\b",
        r"\bPT\s+(?:visits|sessions|services)\b",
        r"\boccupational\s+therapy\b",
        r"\bOT\s+(?:visits|sessions|services)\b",
        r"\bspeech\s+therapy\b",
        r"\bvisit\s+limit\b",
        r"\bmaximum\s+(number\s+of\s+)?visits\b",
        r"\badditional\s+(visits|sessions)\b",
        r"\bcontinued\s+(therapy|treatment)\b",
        r"\bplateau\b",
        r"\bmaintenance\s+(therapy|care)\b",
    )
    procedure_patterns = (
        r"\b(?:physical\s+therapy|PT)\b",
        r"\b(?:occupational\s+therapy|OT)\b",
    )
    negative_patterns = (r"\bphysician\s+assistant\b",)
    citations = (
        "Jimmo v. Sebelius (1:11-cv-00017, D. Vt. 2013) — coverage may not "
        "be denied solely because a patient has reached a 'plateau' or is "
        "not improving; skilled therapy to maintain function or slow "
        "decline is covered when otherwise medically necessary",
        "CMS Medicare Benefit Policy Manual, chapter 15 (skilled therapy "
        "and the maintenance-coverage standard)",
        "Affordable Care Act Essential Health Benefits, "
        "42 U.S.C. § 18022(b)(1)(G) (rehabilitative and habilitative "
        "services and devices)",
        "CMS 2024 Final Rule on the use of algorithms and artificial intelligence "
        "in coverage determinations",
    )

    @classmethod
    def static_appeal(cls) -> str:
        return (
            "Re: Appeal of denied therapy continuation, claim {claim_id}\n\n"
            "Dear {insurance_company},\n\n"
            "I am appealing the denial of additional {procedure} sessions "
            "for {diagnosis}. The denial appears to rest on a visit cap or "
            "a finding that the patient has stopped improving; both grounds "
            "are inconsistent with controlling authority.\n\n"
            "1. Jimmo v. Sebelius — improvement is NOT the standard. "
            "Skilled therapy is covered when it is reasonable and necessary "
            "to maintain the patient's current condition or to slow further "
            "decline, even if the patient is not actively improving. "
            "Plateau- or maintenance-based denials cannot stand under "
            "Jimmo and the resulting CMS clarifications in chapter 15 of "
            "the Medicare Benefit Policy Manual.\n\n"
            "2. ACA Essential Health Benefits "
            "(42 U.S.C. § 18022(b)(1)(G)). Rehabilitative and habilitative "
            "services and devices are designated essential health benefits. "
            "A blanket session cap that prevents continued medically "
            "necessary therapy effectively excludes a covered EHB and is "
            "not enforceable as written.\n\n"
            "3. CMS 2024 Final Rule on algorithmic coverage determinations. "
            "If a utilization-management algorithm flagged this case as "
            "having reached its visit limit, the final denial must still "
            "rest on an individualized clinical review by a qualified human "
            "reviewer; the algorithm may not be the sole basis for the "
            "adverse decision.\n\n"
            "I respectfully request the denial be overturned and the "
            "additional sessions authorized.\n\n"
            "Sincerely,\n[Name]\n"
        )


class PostSurgicalRehabAppeal(SpecializedDenialTemplate):
    name = "Post-surgical rehabilitation denial"
    # Every pattern requires explicit rehab/therapy/SNF/IRF context so we
    # don't mistake denials of the surgery itself (e.g., a bare "ACL
    # repair" denial) for denials of the post-surgical rehabilitation.
    # Specific surgery names without rehab context are intentionally not
    # listed here.
    text_patterns = (
        r"\bpost[\s\-]?(surgical|operative|op)\b.*\b(rehab|therapy|care)\b",
        r"\b(rehab|therapy|care)\b.*\bpost[\s\-]?(surgical|operative|op)\b",
        r"\bafter\s+surgery\b.*\b(rehab|therapy)\b",
        r"\binpatient\s+rehab(ilitation)?\b",
        r"\bskilled\s+nursing\b",
        r"\bSNF\b",
        r"\bacute\s+rehab\b",
        r"\bjoint\s+replacement\b.*\b(rehab|therapy)\b",
        r"\bspinal\s+(fusion|surgery)\b.*\b(rehab|therapy)\b",
    )
    procedure_patterns = (
        # Procedure-only matches must be unambiguous rehab/post-acute
        # contexts; "post-op" alone can describe the surgical episode and
        # is intentionally excluded here.
        r"\binpatient\s+rehab",
        r"\bskilled\s+nursing\b|\bSNF\b",
    )
    citations = (
        "Jimmo v. Sebelius (1:11-cv-00017, D. Vt. 2013) — maintenance and "
        "slow-decline therapy is covered; improvement is not the standard",
        "CMS Medicare Benefit Policy Manual, chapter 1 (inpatient "
        "rehabilitation facility coverage) and chapter 8 (skilled nursing "
        "facility coverage)",
        "Affordable Care Act Essential Health Benefits, "
        "42 U.S.C. § 18022(b)(1)(G) (rehabilitative and habilitative "
        "services and devices)",
        "CMS 2024 Final Rule on the use of algorithms and artificial intelligence "
        "in coverage determinations",
    )

    @classmethod
    def static_appeal(cls) -> str:
        return (
            "Re: Appeal of denied post-surgical rehabilitation, "
            "claim {claim_id}\n\n"
            "Dear {insurance_company},\n\n"
            "I am appealing the denial of post-surgical rehabilitation "
            "({procedure}) following surgery for {diagnosis}. Recovery from "
            "surgery is precisely the scenario in which skilled "
            "rehabilitation is most clearly medically necessary, and the "
            "denial as issued is inconsistent with controlling authority.\n\n"
            "1. CMS coverage standards for inpatient rehabilitation and "
            "skilled nursing care (Medicare Benefit Policy Manual, "
            "chapters 1 and 8). Coverage turns on whether the patient "
            "requires skilled, multidisciplinary rehabilitation services on "
            "an intensive basis (IRF) or daily skilled care (SNF). The "
            "denial does not engage with the chapter 1/8 criteria as "
            "applied to this patient's actual post-operative status.\n\n"
            "2. Jimmo v. Sebelius. To the extent the denial relies on a "
            "lack of measurable improvement or a 'plateau,' the "
            "improvement standard was rejected in Jimmo and clarified in "
            "subsequent CMS guidance. Skilled rehabilitation that maintains "
            "function or prevents deterioration during post-surgical "
            "recovery is covered.\n\n"
            "3. ACA Essential Health Benefits "
            "(42 U.S.C. § 18022(b)(1)(G)). Rehabilitative and habilitative "
            "services and devices are essential health benefits and cannot "
            "be effectively excluded through restrictive utilization "
            "management.\n\n"
            "4. CMS 2024 Final Rule on algorithmic coverage determinations. "
            "If a length-of-stay algorithm or similar tool drove the "
            "denial, the carrier must produce an individualized human "
            "clinical review; the algorithm cannot be the sole basis for "
            "the adverse decision.\n\n"
            "I respectfully request that the denial be overturned and the "
            "post-surgical rehabilitation authorized.\n\n"
            "Sincerely,\n[Name]\n"
        )


class GLP1WeightLossAppeal(SpecializedDenialTemplate):
    name = "GLP-1 / anti-obesity medication denial (Wegovy, Zepbound, Saxenda)"
    # Scoped to weight-management / anti-obesity GLP-1 use. Diabetes-only
    # GLP-1 denials (e.g., Ozempic/Mounjaro for type 2 diabetes) are left to
    # SpecialtyMedicationAppeal; this template adds the obesity-as-disease,
    # "not cosmetic," and FDA-comorbidity-indication arguments that are
    # specific to weight-loss coverage fights. SpecialtyMedicationAppeal may
    # also fire on the drug name — detect_specialized_templates returns every
    # match by design, so the two are complementary, not exclusive.
    # Match only when the denial actually references an anti-obesity / GLP-1
    # *medication* (brand or generic) or an explicit weight-loss/anti-obesity
    # drug phrase — never an obesity/overweight diagnosis alone, which would
    # otherwise attach a weight-management-medication letter to unrelated
    # denials (bariatric surgery, nutrition counseling, or sleep-study care
    # that merely carry an obesity diagnosis).
    text_patterns = (
        r"\b(?:wegovy|zepbound|saxenda)\b",
        r"\banti[\s\-]?obesity\s+"
        r"(?:drug|medication|medications|agent|agents|rx|therap|pharmacotherap)",
        r"\bweight[\s\-]?(?:loss|management)\s+"
        r"(?:drug|medication|medications|agent|agents|injection|injections|rx|pill|pills|therap)",
        r"\bGLP[\s\-]?1\b.*\b(?:weight|obesit)",
        r"\b(?:semaglutide|tirzepatide|liraglutide)\b.*\b(?:weight|obesit)",
    )
    procedure_patterns = (
        r"\b(?:wegovy|zepbound|saxenda)\b",
        r"\banti[\s\-]?obesity\s+(?:drug|medication|medications|agent|agents)\b",
        r"\bweight[\s\-]?(?:loss|management)\s+"
        r"(?:drug|medication|medications|agent|agents|injection|injections)\b",
    )
    # No diagnosis_patterns by design: an obesity/overweight diagnosis alone is
    # not sufficient — a GLP-1 / anti-obesity medication must be referenced.
    citations = (
        "American Medical Association recognition of obesity as a disease "
        "(AMA Policy H-440.842, adopted 2013)",
        "FDA-approved labeling for the prescribed anti-obesity medication — "
        "e.g., Wegovy (semaglutide 2.4 mg) for chronic weight management and, "
        "under its 2024 SELECT-trial indication, reduction of major adverse "
        "cardiovascular events; Zepbound (tirzepatide) for chronic weight "
        "management and for moderate-to-severe obstructive sleep apnea in "
        "adults with obesity",
        "Affordable Care Act Section 1557 non-discrimination, " "42 U.S.C. § 18116",
        "ERISA § 503 / 29 C.F.R. § 2560.503-1 (full and fair review — "
        "disclosure of the clinical criteria, reviewer credentials, and the "
        "exact plan-exclusion language relied upon)",
        "CMS 2024 Final Rule on the use of algorithms and artificial "
        "intelligence in coverage determinations",
    )

    @classmethod
    def static_appeal(cls) -> str:
        return (
            "Re: Appeal of denied weight-management medication, "
            "claim {claim_id}\n\n"
            "Dear {insurance_company},\n\n"
            "I am appealing the denial of {procedure} for {diagnosis}. "
            "Anti-obesity medications such as GLP-1 and GIP/GLP-1 receptor "
            "agonists are FDA-approved treatments for a recognized disease, "
            "and the denial as issued does not engage with that individualized "
            "clinical analysis.\n\n"
            "1. Obesity is a disease, not a cosmetic concern. The American "
            "Medical Association formally recognized obesity as a disease in "
            "2013 (Policy H-440.842). To the extent this denial relies on a "
            "'cosmetic,' 'lifestyle,' or 'weight-loss' exclusion, that "
            "exclusion does not properly reach FDA-approved pharmacotherapy "
            "prescribed to treat a diagnosed disease. Please quote the exact "
            "plan language relied upon and explain how it encompasses the "
            "FDA-approved indication at issue.\n\n"
            "2. FDA-approved indication. The requested medication is FDA-"
            "approved for the patient's condition — chronic weight management "
            "in patients with obesity, or with overweight and a weight-related "
            "comorbidity — and, for several agents, additional medical "
            "indications (Wegovy for reducing major adverse cardiovascular "
            "events in adults with established cardiovascular disease; "
            "Zepbound for moderate-to-severe obstructive sleep apnea in adults "
            "with obesity). Where the patient is being treated for such an "
            "indication, the therapy treats a covered medical condition and is "
            "medically necessary.\n\n"
            "3. Individualized, full-and-fair review. Under ERISA § 503 "
            "(29 C.F.R. § 2560.503-1) and ACA Section 1557 (42 U.S.C. "
            "§ 18116), please produce the specific clinical criteria applied, "
            "the credentials of the reviewing clinician, and any internal rule "
            "or guideline relied upon. Where the patient has already been "
            "stabilized on this therapy, I request continuity-of-care "
            "protection so that treatment is not interrupted during this "
            "appeal.\n\n"
            "4. CMS 2024 Final Rule on algorithmic coverage determinations. "
            "If an automated tool contributed to this denial, the final "
            "decision must still rest on an individualized clinical review by "
            "a qualified human reviewer; the algorithm may not be the sole "
            "basis for the adverse determination. Please disclose whether such "
            "a tool was used and identify it.\n\n"
            "I respectfully request that the denial be overturned and the "
            "medication authorized without further delay.\n\n"
            "Sincerely,\n[Name]\n"
        )


SPECIALIZED_DENIAL_TEMPLATES: tuple[type[SpecializedDenialTemplate], ...] = (
    MentalHealthParityAppeal,
    AdvancedImagingAppeal,
    SpecialtyMedicationAppeal,
    PhysicalTherapyContinuationAppeal,
    PostSurgicalRehabAppeal,
    GLP1WeightLossAppeal,
)


def detect_specialized_templates(
    denial_text: Optional[str],
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
) -> list[type[SpecializedDenialTemplate]]:
    """Return the specialized templates that match a denial.

    A denial can match more than one template — for example, a denial
    that says "outpatient substance use disorder therapy has reached the
    maximum number of visits" matches both MentalHealthParityAppeal (via
    the substance-use-disorder cue) and PhysicalTherapyContinuationAppeal
    (via the visit-cap cue). We return every match and let the caller
    decide what to surface.
    """
    if not denial_text and not procedure and not diagnosis:
        return []
    return [
        t
        for t in SPECIALIZED_DENIAL_TEMPLATES
        if t.matches(denial_text, procedure, diagnosis)
    ]


try:
    from english_words import get_english_words_set

    _ENGLISH_WORDS: frozenset[str] = frozenset(
        get_english_words_set(["gcide", "web2"], lower=True)
    )
except (ImportError, ModuleNotFoundError):
    logger.warning("english-words package not available, using empty word set")
    _ENGLISH_WORDS = frozenset()
except Exception as e:
    logger.error(f"Unexpected error loading english-words package: {e}")
    raise


def _is_english_word(word: str) -> bool:
    """Check if a word (or its likely stem) is a known English word."""
    if word in _ENGLISH_WORDS:
        return True
    # Check common inflected forms by stripping suffixes
    # This catches "covers" (cover), "denied" (deny), "approved" (approve), etc.
    for suffix in ("s", "es", "ed", "ing", "er", "ers", "tion", "ly", "ment"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            stem = word[: -len(suffix)]
            if stem in _ENGLISH_WORDS:
                return True
            # "approved" -> "approv" -> "approve" (stem + "e")
            if (stem + "e") in _ENGLISH_WORDS:
                return True
    # "denied" -> "deny" (strip "ied", add "y")
    if word.endswith("ied") and len(word) > 4:
        stem = word[:-3] + "y"
        if stem in _ENGLISH_WORDS:
            return True
    return False


_LABEL_PREFIX_RE = re.compile(
    r"^(?:plan|claim|member|group|policy|subscriber|id|number|no|#)" r"[\s:.\-/#]*",
    re.IGNORECASE,
)


def is_plausible_identifier(value: Optional[str]) -> bool:
    """Check whether a string looks like a plausible plan/claim/member ID.

    Real IDs are typically alphanumeric codes like 'ABC123456', 'H5521-001',
    'PLAN987654', or occasionally pure-alpha codes like 'BCBSMA'.
    They are not common English words or labeled phrases like 'Plan ID: ABC123'.
    """
    if value is None:
        return False
    stripped = value.strip()
    if not stripped:
        return False
    # Strip common label prefixes (e.g. "Plan ID: ", "Claim #", "Member: ")
    # Apply repeatedly to handle stacked prefixes like "Plan ID:"
    prev = None
    while stripped != prev:
        prev = stripped
        stripped = _LABEL_PREFIX_RE.sub("", stripped).strip()
    if not stripped:
        return False
    # Reject very short or very long values
    if len(stripped) < 3 or len(stripped) > 50:
        return False
    # Must be primarily alphanumeric (allow hyphens, underscores, spaces, dots, slashes)
    # Colons are not valid in IDs — they indicate labels.
    if not re.match(r"^[A-Za-z0-9\s\-_./#]+$", stripped):
        return False
    # Reject if the lowercased value is a known English word (including inflected forms)
    lowered = stripped.lower()
    if _is_english_word(lowered):
        return False
    # Reject multi-word phrases where every word is English
    words = re.split(r"[\s\-_./#]+", lowered)
    if len(words) > 1 and all(_is_english_word(w) for w in words if w):
        return False
    return True


def _identifier_score(result: Optional[str], denial_text: str) -> float:
    """Shared scoring function for plan_id and claim_id extraction."""
    if result is None:
        return -1.0
    if not is_plausible_identifier(result):
        return -1.0
    # Check that the identifier is found in the source document
    if not identifier_found_in_text(result, denial_text):
        return -0.5
    score = 1.0
    length = len(result.strip())
    if 5 <= length <= 20:
        score += 1.0
    elif 3 <= length <= 30:
        score += 0.5
    # Bonus for having digits (most IDs do)
    if re.search(r"\d", result):
        score += 0.3
    # Bonus for mixed alphanumeric (common in IDs)
    if re.search(r"[A-Za-z]", result) and re.search(r"\d", result):
        score += 0.5
    return score


def identifier_found_in_text(identifier: str, text: str) -> bool:
    """Check if an identifier appears in text with flexible matching.

    Handles format variations like hyphens vs spaces vs no separator.
    For example, 'H5521-001' should match 'H5521 001' or 'H5521001' in text.
    """
    if not identifier or not text:
        return False

    # Normalize: remove common separators and lowercase
    def normalize(s: str) -> str:
        return re.sub(r"[\s\-_./#:]+", "", s).lower()

    norm_id = normalize(identifier)
    norm_text = normalize(text)

    if norm_id in norm_text:
        return True

    # Also try the original (lowercased) as-is in the lowered text
    if identifier.lower() in text.lower():
        return True

    return False


# Tier-shed retry: reduce prompt size when zero-result failures may stem
# from context-window overflow. Each enrichment context appears on TWO
# surfaces and both need to shrink in lockstep, otherwise we'd null the
# call-dict copy while the prompt-baked copy still pins the token count
# above the limit.
#
#   * make_open_prompt bakes enrichment (pubmed, ml citations, rag, nice,
#     uspstf, ucr, pa, payer-policy, medication) into the prompt string.
#     To shed those we re-render the prompt with the matching kwargs set
#     to None / truncated.
#   * The same call dict also carries pubmed_context, ml_citations_context,
#     plan_context, and patient_context as separate keys, which the model
#     re-injects via ``context_extra`` (see ml_models.RemoteFullOpenLike).
#     We null/truncate those alongside the prompt re-render.

# In-prompt enrichment dropped entirely at tier 1+. Names match the
# ``make_open_prompt`` parameters (note: ``ml_context`` is the prompt-side
# rendered form of ``ml_citations_context``).
_PROMPT_TIER1_NULLS: tuple[str, ...] = (
    "ml_context",
    "pubmed_context",
    "rag_context",
    "nice_context",
    "uspstf_context",
    "ucr_context",
    "pa_context",
    "payer_policy_context",
    "medication_context",
)

# In-prompt patient-specific context truncated (not dropped) at tier 2+.
_PROMPT_TIER2_TRUNCATIONS: tuple[tuple[str, int], ...] = (("plan_context", 4000),)

# Call-dict copies (the ``context_extra`` injection surface) sheddable at
# tier 1+. These mirror their ``_PROMPT_TIER1_NULLS`` counterparts.
_SHEDDABLE_TIER1 = ("pubmed_context", "ml_citations_context")

# Call-dict patient/plan context truncated at tier 2+. ``patient_context``
# (``medical_context`` in the caller) is not in the prompt string, so it
# only needs trimming on the call-dict surface.
_TIER2_TRUNCATIONS = (("plan_context", 4000), ("patient_context", 6000))


def _shed_context(
    calls: list[dict],
    tier: int,
    *,
    open_prompt_kwargs: Optional[dict] = None,
    rebuild_prompt: Optional[Callable[..., Optional[str]]] = None,
    original_open_prompt: Optional[str] = None,
) -> tuple[list[dict], list[str]]:
    """Return (new_calls, changed_names) with context reduced by tier.

    Tier 1 nulls the enrichment contexts on both surfaces: in the
    re-rendered prompt (when ``open_prompt_kwargs`` + ``rebuild_prompt`` +
    ``original_open_prompt`` are provided) and in the call-dict copies.
    Tier 2 additionally truncates ``plan_context`` (in-prompt) and
    ``plan_context`` + ``patient_context`` (call-dict). Higher tiers
    include all lower-tier reductions.

    When the prompt-rebuild arguments are omitted the function falls back
    to call-dict-only shedding — kept for direct unit testing of the
    call-dict surface.
    """
    changed: set[str] = set()

    new_open_prompt: Optional[str] = None
    if (
        tier >= 1
        and open_prompt_kwargs is not None
        and rebuild_prompt is not None
        and original_open_prompt is not None
    ):
        shed_kwargs = dict(open_prompt_kwargs)
        for key in _PROMPT_TIER1_NULLS:
            # Truthy check (not ``is not None``): an empty-string enrichment
            # is already a no-op inside ``make_open_prompt`` (each section is
            # gated on ``!= ""``), so nulling it would add diagnostic noise
            # to ``changed`` without changing the rebuilt prompt.
            if shed_kwargs.get(key):
                shed_kwargs[key] = None
                changed.add(f"prompt.{key}")
        if tier >= 2:
            for key, cap in _PROMPT_TIER2_TRUNCATIONS:
                val = shed_kwargs.get(key)
                if isinstance(val, str) and len(val) > cap:
                    shed_kwargs[key] = truncate_at_boundary(val, cap, ellipsis="")
                    changed.add(f"prompt.{key}(truncated)")
        new_open_prompt = rebuild_prompt(**shed_kwargs)

    new_calls = []
    for call in calls:
        new = dict(call)
        # Swap the shed prompt into any call whose prompt is the original
        # open_prompt or starts with it (the specialized variant appends a
        # hint block). The medically-necessary prompt is unrelated and is
        # left untouched.
        if (
            new_open_prompt is not None
            and original_open_prompt
            and isinstance(new.get("prompt"), str)
            and new["prompt"].startswith(original_open_prompt)
        ):
            tail = new["prompt"][len(original_open_prompt) :]
            new["prompt"] = new_open_prompt + tail
        for key in _SHEDDABLE_TIER1:
            if new.get(key) is not None:
                new[key] = None
                changed.add(key)
        if tier >= 2:
            for key, cap in _TIER2_TRUNCATIONS:
                val = new.get(key)
                if isinstance(val, str) and len(val) > cap:
                    new[key] = val[:cap]
                    changed.add(f"{key}(truncated)")
        new_calls.append(new)
    return new_calls, sorted(changed)


_T_peek = TypeVar("_T_peek")


def _peek_or_none(
    it: Iterator[_T_peek],
) -> Tuple[Optional[_T_peek], Iterator[_T_peek]]:
    """Pull one item or return (None, empty). Chains the pulled item back
    so callers can use the returned iterator without losing the head."""
    try:
        first = next(it)
        return first, itertools.chain([first], it)
    except StopIteration:
        return None, iter([])


def _peek_real_or_none(
    it: Iterator["GeneratedAppeal"], denial_id: Any, stage: str
) -> Tuple[Optional["GeneratedAppeal"], Iterator["GeneratedAppeal"]]:
    """Like _peek_or_none, but returns (None, _) when the first item is
    non-None but fails is_real_appeal. Without this, a runt first item
    would suppress the fallback path even though downstream filtering
    drops it — leading to zero deliverable appeals."""
    first, it = _peek_or_none(it)
    if first is not None and not is_real_appeal(first.text):
        first_len = len(first.text.strip()) if isinstance(first.text, str) else 0
        logger.warning(
            f"make_appeals: {stage} first item is a runt (len={first_len}) "
            f"for denial {denial_id}; treating as empty to trigger fallback"
        )
        return None, it
    return first, it


class AppealGenerator(object):
    QUALITY_KEYWORDS = (
        "evidence",
        "medical necessity",
        "medically necessary",
        "appeal",
        "policy",
        "clinical",
    )

    MAX_SYNTHESIS_DRAFTS = 3

    @staticmethod
    def _score_appeal_text(text: str, diagnosis: Optional[str] = None) -> float:
        """Score an appeal text by length, keyword presence, diagnosis match,
        and repetition quality.

        Appeals with repeated sentences or blocks are penalized proportionally
        so that cleaner appeals are preferred during synthesis selection.
        """
        stripped = text.strip()
        lower = stripped.lower()
        length_score = min(len(stripped), 3000)
        keyword_score = sum(
            10 for kw in AppealGenerator.QUALITY_KEYWORDS if kw in lower
        )
        diagnosis_bonus = 50 if diagnosis and diagnosis.lower() in lower else 0
        base = length_score * 0.3 + keyword_score + diagnosis_bonus
        # Penalize repetitive content: a fully-repetitive appeal loses up to
        # 200 points, enough to noticeably prefer a clean alternative.
        rep_penalty = repetition_penalty(stripped) * 200
        return base - rep_penalty

    # System prompt for the synthesis step
    SYNTHESIS_SYSTEM_PROMPT = (
        "You are an expert health insurance appeal writer. You will be given several "
        "draft appeal letters that were generated for the same insurance denial. Your job "
        "is to synthesize the best possible single appeal letter by combining the strongest "
        "arguments, citations, and language from all drafts. "
        "Rules:\n"
        "- Keep the formal appeal letter format (address, date, salutation, body, closing).\n"
        "- Include ALL valid citations and references from any draft — do NOT invent new ones.\n"
        "- Choose the most persuasive and specific arguments from each draft.\n"
        "- Eliminate redundancy while preserving completeness.\n"
        "- Maintain a professional, assertive tone throughout.\n"
        "- If drafts disagree on facts, prefer the most specific and well-supported version.\n"
        "- The final letter should be comprehensive but not unnecessarily long.\n"
        "- Preserve any patient/provider/plan details exactly as they appear in the drafts."
    )

    def __init__(self):
        self.regex_denial_processor = ProcessDenialRegex()

    @staticmethod
    def _best_internal_model_name() -> Optional[str]:
        """Return the friendly name (used by ml_router.models_by_name) of the
        highest-quality internal model, or None if none are available.

        We deliberately route specialized-template hints through this single
        model rather than every backend: the hints add prompt length and
        instructions that benefit most from the strongest available model,
        and broadcasting them across every backend would multiply cost
        without proportional quality gain.
        """
        best = ml_router.best_internal_model()
        if best is None:
            return None
        for name, instances in ml_router.models_by_name.items():
            if best in instances:
                return str(name)
        return None

    @staticmethod
    def _build_specialized_hint_block(
        templates: List[type[SpecializedDenialTemplate]],
    ) -> str:
        """Combine the prompt hints from one or more specialized templates."""
        seen: set[str] = set()
        ordered: list[type[SpecializedDenialTemplate]] = []
        for t in templates:
            if t.name in seen:
                continue
            seen.add(t.name)
            ordered.append(t)
        if not ordered:
            return ""
        return "\n\n".join(t.model_prompt_hint() for t in ordered)

    async def _extract_entity_with_regexes_and_model(
        self,
        denial_text: str,
        patterns: List[str],
        flags: int = re.IGNORECASE,
        use_external: bool = False,
        model_method_name: Optional[str] = None,
        prompt_template: Optional[str] = None,
        find_in_denial=True,
        score_fn: Optional[Callable[[Optional[str], Any], float]] = None,
    ) -> Optional[str]:
        """
        Common base function for extracting entities using regex patterns first,
        then falling back to ML models if needed.

        Args:
            denial_text: The text to extract from
            patterns: List of regex patterns to try
            flags: Regex flags to apply
            use_external: Whether to use external models
            model_method_name: Name of the method to call on ML models
            prompt_template: Template for prompting extraction if ML models are needed

        Returns:
            Extracted entity or None
        """
        # First try regex patterns directly (fast path)
        for pattern in patterns:
            match = re.search(pattern, denial_text, flags)
            if match:
                candidate = match.group(1).strip()
                if score_fn is not None:
                    candidate_score = score_fn(candidate, denial_text)
                    if candidate_score < 0:
                        logger.debug(
                            f"Rejecting regex candidate: score={candidate_score}, "
                            f"length={len(candidate)}, pattern={pattern}"
                        )
                        continue
                return candidate

        # Fallback to ML backends with parallel timed selection
        if not model_method_name:
            return None

        models_to_try = [
            m
            for m in ml_router.entity_extract_backends(use_external)
            if hasattr(m, model_method_name)
        ]
        if not models_to_try:
            return None

        denial_lowered = denial_text.lower()

        # Local import avoids the generate_appeal <-> field_confidence
        # circular dependency at module load time.
        from fighthealthinsurance.field_confidence import (
            score_freetext_extraction,
        )

        async def attempt_model(model: DenialBase) -> Optional[str]:
            method = getattr(model, model_method_name)
            # Retry up to 3 times gently
            for _ in range(3):
                try:
                    extracted: Optional[str] = await method(denial_text)  # type: ignore
                except Exception:
                    logger.opt(exception=True).debug(
                        f"Extraction call failed for {model} {model_method_name}"
                    )
                    extracted = None
                if extracted is None:
                    await asyncio.sleep(1)
                    continue
                score = score_freetext_extraction(extracted, denial_text)
                if score == "low":
                    # Previously dropped silently. Log at INFO so the
                    # rejection is traceable for triage — but only metadata.
                    # attempt_model also extracts claim_id / plan_id / member
                    # info, so the raw value can be PHI/PII and must not hit
                    # the logs.
                    logger.info(
                        f"Dropped low-confidence extraction: "
                        f"method={model_method_name} model={type(model).__name__} "
                        f"value_len={len(extracted.strip())}"
                    )
                    await asyncio.sleep(1)
                    continue
                lowered = extracted.lower().strip()
                if find_in_denial and lowered not in denial_lowered:
                    # Require presence in original text unless flag disabled
                    await asyncio.sleep(1)
                    continue
                return extracted.strip()
            return None

        awaitables: List[Coroutine[Any, Any, Optional[str]]] = [
            attempt_model(m) for m in models_to_try
        ]

        def default_score(result: Optional[str], _: Any) -> float:
            if result is None:
                return -1.0
            length = len(result)
            score = 1.0
            if 3 <= length <= 120:
                score += 0.5
            score -= 0.002 * max(0, length - 120)
            return score

        use_score = score_fn or default_score

        try:
            best = await best_within_timelimit(
                awaitables, score_fn=use_score, timeout=30
            )
        except Exception:
            logger.opt(exception=True).debug(
                "best_within_timelimit failed for entity extraction"
            )
            best = None

        # best_within_timelimit returns any truthy result regardless of score.
        # If a score_fn was provided, verify the result actually scores positively
        # to avoid returning junk like English words that passed attempt_model.
        if best is not None and score_fn is not None:
            final_score = score_fn(best, denial_text)
            if final_score < 0:
                logger.debug(
                    f"Rejecting extraction result: score={final_score}, "
                    f"length={len(best)}, type={type(best).__name__}"
                )
                best = None

        return best

    async def get_fax_number(
        self, denial_text=None, use_external=False
    ) -> Optional[str]:
        """
        Extract fax number from denial text

        Args:
            denial_text: The text of the denial letter
            use_external: Whether to use external models

        Returns:
            Extracted fax number or None
        """
        if denial_text is None:
            return None

        # Short-circuit if there's no mention of fax or facsimile in the text
        if "fax" not in denial_text.lower() and "facsimile" not in denial_text.lower():
            logger.debug("No mention of fax or facsimile in text, skipping extraction")
            return None

        # Common fax number regex patterns
        fax_patterns = [
            r"[Ff]ax(?:\s*(?:number|#|:))?\s*[:=]?\s*(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Ff]ax(?:\s*(?:to|at))?\s*[:=]?\s*(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Aa]ppeal.*?[Ff]ax.*?(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Ff]ax.*?[Aa]ppeal.*?(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Tt]o\s+[Ff]ax\s+(?:at|to)?\s*(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Ss]end\s+(?:an?\s+)?(?:appeal|request).*?(?:to|at)?\s*(?:[Ff]ax|#)?\s*[:]?\s*(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Ff]ax.*?(?:to|at)?\s*(?:number|#)?\s*[:]?\s*[\(\[\{]?(\d{3})[\)\]\}]?[-.\s]?(\d{3})[-.\s]?(\d{4})",
            r"[Ff]ax\s*(?:number|#)?\s*(?:is|:|=)?\s*[\(\[\{]?(\d{3})[\)\]\}]?[-.\s]?(\d{3})[-.\s]?(\d{4})",
            r"(?:by|via)\s+[Ff]ax\s+(?:at|to)?\s*(?:number|#)?\s*[:]?\s*(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Ff]ax\s*[\(\[\{]?(\d{3})[\)\]\}]?[-.\s]?(\d{3})[-.\s]?(\d{4})",
            r"[Ff]acsimile(?:\s*(?:number|#|:))?\s*[:=]?\s*(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
            r"[Ff]acsimile(?:\s*(?:to|at))?\s*[:=]?\s*(\d{3}[-.\s]?\d{3}[-.\s]?\d{4})",
        ]

        # First try with exact regex matches
        for pattern in fax_patterns:
            match = re.search(pattern, denial_text, re.IGNORECASE | re.DOTALL)
            if match:
                groups = match.groups()
                if len(groups) == 1:
                    # Standard pattern with one capture group
                    return self._normalize_fax_number(groups[0])
                elif len(groups) == 3:
                    # Pattern with separate area code, prefix, line number groups
                    return self._normalize_fax_number(
                        f"{groups[0]}{groups[1]}{groups[2]}"
                    )

        # More flexible matching approach
        # Custom scoring preferring valid 10-digit phone number-like fax values
        def fax_score(result: Optional[str], _: Any) -> float:
            if result is None:
                return -1.0
            digits = re.sub(r"\D", "", result)
            score = 0.0
            if len(digits) == 10:
                score += 2.0
            elif len(digits) >= 7:
                score += 1.0
            # Bonus for standard formatting
            if re.search(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", result):
                score += 0.5
            # Penalty for overly long strings
            score -= 0.01 * max(0, len(result) - 25)
            return score

        return await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=fax_patterns,
            flags=re.IGNORECASE | re.DOTALL,
            use_external=use_external,
            model_method_name="get_fax_number",
            find_in_denial=False,  # Fax number may not appear verbatim in letter text formatting
            score_fn=fax_score,
        )

    def _normalize_fax_number(self, fax_number: str) -> str:
        """
        Normalize a fax number by removing non-digit characters and formatting consistently.

        Args:
            fax_number: Raw fax number string

        Returns:
            Normalized fax number in format: XXX-XXX-XXXX
        """
        # Extract all digits from the string
        digits = re.sub(r"\D", "", fax_number)

        # If we have at least 10 digits, format as XXX-XXX-XXXX
        if len(digits) >= 10:
            return f"{digits[-10:-7]}-{digits[-7:-4]}-{digits[-4:]}"
        return fax_number

    async def get_insurance_company(
        self, denial_text=None, use_external=False
    ) -> Optional[str]:
        """
        Extract insurance company name from denial text

        Args:
            denial_text: The text of the denial letter
            use_external: Whether to use external models

        Returns:
            Extracted insurance company name or None
        """
        if denial_text is None:
            return None

        # Try regex patterns first
        company_patterns = [
            r"^([A-Z][A-Za-z\s&]+(?:Insurance|Health|Healthcare|Medical|Plan|Benefits|Blue|Cross|Shield)(?:\s[A-Za-z&\s]+)?)\n",
            r"letterhead:\s*([A-Z][A-Za-z\s&]+(?:Insurance|Health|Healthcare|Medical|Plan|Benefits|Blue|Cross|Shield)(?:\s[A-Za-z&\s]+)?)",
            r"from:\s*([A-Z][A-Za-z\s&]+(?:Insurance|Health|Healthcare|Medical|Plan|Benefits|Blue|Cross|Shield)(?:\s[A-Za-z&\s]+)?)",
        ]

        # Try direct regex matches
        for pattern in company_patterns:
            match = re.search(pattern, denial_text, re.IGNORECASE | re.MULTILINE)
            if match:
                return match.group(1).strip()

        # Load known companies from database dynamically
        known_companies: list[str] = []
        try:
            from asgiref.sync import sync_to_async

            from fighthealthinsurance.models import InsuranceCompany

            # Get all insurance companies from database
            companies = await sync_to_async(
                lambda: list(InsuranceCompany.objects.values_list("name", "alt_names"))
            )()
            for name, alt_names in companies:
                known_companies.append(name)
                if alt_names:
                    # Add alternative names too
                    for alt_name in alt_names.split("\n"):
                        alt_name = alt_name.strip()
                        if alt_name:
                            known_companies.append(alt_name)
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Failed to load companies from database, using fallback list: {e}"
            )
            # Fallback to hardcoded list if database query fails
            known_companies = [
                "Aetna",
                "Anthem",
                "Blue Cross",
                "Blue Shield",
                "Cigna",
                "Humana",
                "Kaiser Permanente",
                "UnitedHealthcare",
                "United Healthcare",
                "Centene",
                "Molina Healthcare",
                "WellCare",
                "CVS Health",
                "Empire BlueCross",
                "Empire Health",
            ]

        # Try to find companies in the denial text
        for company in known_companies:
            if company in denial_text:
                # Find the full company name (looking for patterns like "Aetna Health Insurance")
                pattern = rf"({company}\s+[A-Za-z\s&]+(?:Insurance|Health|Healthcare|Medical|Plan|Benefits))"
                match = re.search(pattern, denial_text, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                return company

        # If regex fails, use ML models with known companies as context
        models_to_try = ml_router.entity_extract_backends(use_external)
        for model in models_to_try:
            if hasattr(model, "get_insurance_company"):
                insurance_company: Optional[str] = await model.get_insurance_company(
                    denial_text
                )
                if insurance_company is not None and "UNKNOWN" not in insurance_company:
                    return insurance_company

        return None

    async def get_plan_id(self, denial_text=None, use_external=False) -> Optional[str]:
        """
        Extract plan ID from denial text

        Args:
            denial_text: The text of the denial letter
            use_external: Whether to use external models

        Returns:
            Extracted plan ID or None
        """
        if denial_text is None:
            return None

        # Common plan ID patterns
        plan_patterns = [
            r"[Pp]lan(?:\s*(?:ID|Number|#|:))?\s*[:=]?\s*([A-Z0-9]{5,20})",
            r"[Gg]roup(?:\s*(?:ID|Number|#|:))?\s*[:=]?\s*([A-Z0-9]{5,20})",
            r"[Pp]olicy(?:\s*(?:ID|Number|#|:))?\s*[:=]?\s*([A-Z0-9]{5,20})",
            r"[Mm]ember(?:\s*(?:ID|Number|#|:))?\s*[:=]?\s*([A-Z0-9]{5,20})",
        ]

        def plan_id_score(result: Optional[str], _: Any) -> float:
            return _identifier_score(result, denial_text)

        return await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=plan_patterns,
            use_external=use_external,
            model_method_name="get_plan_id",
            find_in_denial=False,  # Handled by plan_id_score with flexible matching
            score_fn=plan_id_score,
        )

    async def get_claim_id(self, denial_text=None, use_external=False) -> Optional[str]:
        """
        Extract claim ID from denial text

        Args:
            denial_text: The text of the denial letter
            use_external: Whether to use external models

        Returns:
            Extracted claim ID or None
        """
        if denial_text is None:
            return None

        # Common claim ID patterns
        claim_patterns = [
            r"[Cc]laim(?:\s*(?:ID|Number|#|:))?\s*[:=]?\s*([A-Z0-9]{5,20})",
            r"[Cc]laim(?:\s*(?:ID|Number|#|:))?\s*[:=]?\s*([A-Z0-9-]{5,20})",
            r"[Rr]eference(?:\s*(?:ID|Number|#|:))?\s*[:=]?\s*([A-Z0-9-]{5,20})",
        ]

        def claim_id_score(result: Optional[str], _: Any) -> float:
            return _identifier_score(result, denial_text)

        return await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=claim_patterns,
            use_external=use_external,
            model_method_name="get_claim_id",
            find_in_denial=False,  # Handled by claim_id_score with flexible matching
            score_fn=claim_id_score,
        )

    async def get_date_of_service(
        self, denial_text=None, use_external=False
    ) -> Optional[str]:
        """
        Extract date of service from denial text

        Args:
            denial_text: The text of the denial letter
            use_external: Whether to use external models

        Returns:
            Extracted date of service or None
        """
        if denial_text is None:
            return None

        # Common date patterns (MM/DD/YYYY, MM-DD-YYYY, Month DD, YYYY)
        date_patterns = [
            r"[Dd]ate(?:\s*(?:of|for))?\s*[Ss]ervice\s*[:=]?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
            r"[Dd]ate(?:\s*(?:of|for))?\s*[Ss]ervice\s*[:=]?\s*([A-Za-z]+\s+\d{1,2},?\s*\d{2,4})",
            r"[Ss]ervice(?:\s*(?:date|period))?\s*[:=]?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
            r"[Ss]ervice(?:\s*(?:date|period))?\s*[:=]?\s*([A-Za-z]+\s+\d{1,2},?\s*\d{2,4})",
        ]

        def date_score(result: Optional[str], _: Any) -> float:
            if result is None:
                return -1.0
            r = result.strip()
            score = 0.0
            # Common single date formats
            single_patterns = [
                r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
                r"\b\d{1,2}-\d{1,2}-\d{2,4}\b",
                r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{2,4}\b",
            ]
            range_patterns = [
                r"\b\d{1,2}/\d{1,2}/\d{2,4}\s*(?:-|–|to)\s*\d{1,2}/\d{1,2}/\d{2,4}\b",
                r"\b\d{1,2}-\d{1,2}-\d{2,4}\s*(?:-|–|to)\s*\d{1,2}-\d{1,2}-\d{2,4}\b",
            ]
            if any(re.search(p, r) for p in range_patterns):
                score += 2.0
            if any(re.search(p, r) for p in single_patterns):
                score += 1.5
            # Attempt parse for common numeric formats to add bonus
            from datetime import datetime

            parsed = False
            for fmt in ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"]:
                try:
                    # Only parse first token if range present
                    token = r.split()[0]
                    if re.match(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", token):
                        datetime.strptime(token, fmt)
                        parsed = True
                        break
                except Exception:
                    pass
            if parsed:
                score += 0.5
            # Penalize extreme length
            score -= 0.01 * max(0, len(r) - 40)
            return score

        return await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=date_patterns,
            use_external=use_external,
            model_method_name="get_date_of_service",
            score_fn=date_score,
        )

    async def get_procedure_and_diagnosis(
        self, denial_text=None, use_external=False
    ) -> Tuple[Optional[str], Optional[str]]:
        # Build model list: regex first, then ML backends
        models_to_try: list[DenialBase] = [self.regex_denial_processor]
        models_to_try.extend(ml_router.entity_extract_backends(use_external))

        logger.debug(
            f"Trying to get procedure and diagnosis (timed best) using {models_to_try}"
        )

        # Prepare awaitables from all models
        awaitables: List[
            Coroutine[Any, Any, Optional[Tuple[Optional[str], Optional[str]]]]
        ] = [model.get_procedure_and_diagnosis(denial_text) for model in models_to_try]

        # Scoring: prefer results that give both fields, penalize overly long values
        def score_fn(
            result: Optional[Tuple[Optional[str], Optional[str]]], _: Any
        ) -> float:
            if result is None:
                return -1.0
            proc, diag = result
            score = 0.0

            def is_good(s: Optional[str]) -> bool:
                return s is not None and len(s.strip()) > 0 and len(s) <= 200

            if is_good(proc):
                score += 1.0
            if is_good(diag):
                score += 1.0
            if is_good(proc) and is_good(diag):
                score += 0.5  # bonus for both present

            # Prefer shorter (but valid) strings slightly
            total_len = (len(proc) if proc else 0) + (len(diag) if diag else 0)
            score -= 0.001 * total_len
            return score

        try:
            best = await best_within_timelimit(
                awaitables, score_fn=score_fn, timeout=30
            )
        except Exception:
            logger.opt(exception=True).debug(
                "best_within_timelimit failed for get_procedure_and_diagnosis"
            )
            best = None

        if best is None:
            logger.debug("No model returned procedure/diagnosis within timeout")
            return (None, None)

        proc, diag = best
        # Enforce length constraint similar to previous logic
        if proc is not None and len(proc) > 200:
            proc = None
        if diag is not None and len(diag) > 200:
            diag = None
        logger.debug(f"Returning (procedure, diagnosis)=({proc}, {diag})")
        return (proc, diag)

    def make_open_procedure_prompt(self, denial_text=None) -> Optional[str]:
        if denial_text is not None:
            return f"What was the procedure/treatment and what is the diagnosis from the following denial (remember to provide two strings seperated by MAGIC as your response): {denial_text}"
        else:
            return None

    def make_open_prompt(
        self,
        denial_text=None,
        procedure=None,
        diagnosis=None,
        is_trans=False,
        patient=None,
        professional=None,
        qa_context=None,
        professional_to_finish=None,
        plan_id=None,
        claim_id=None,
        insurance_company=None,
        is_tpa=False,
        ml_context=None,
        pubmed_context=None,
        plan_context=None,
        rag_context=None,
        nice_context=None,
        ucr_context=None,
        payer_policy_context=None,
        pa_context=None,
        uspstf_context=None,
        clinical_trials_context=None,
        medication_context=None,
        regulatory_citation_context=None,
    ) -> Optional[str]:
        """
        Constructs a prompt for generating a health insurance appeal based on denial details and optional contextual information.

        Args:
            denial_text: The text of the insurance denial letter.
            procedure: The medical procedure being appealed, if known.
            diagnosis: The diagnosis related to the appeal, if known.
            is_trans: Whether the patient is transgender.
            patient: Patient information to include in the prompt.
            professional: Professional information to include in the prompt.
            qa_context: Additional context or background to incorporate into the appeal.
            professional_to_finish: If True, instructs to write from the professional's point of view.
            plan_id: Insurance plan ID to include.
            claim_id: Claim ID to include.
            medication_context: Pre-rendered drug-class guidance block from
                ``_collect_medication_context``. When non-empty, appended as a
                DRUG-CLASS GUIDANCE section to nudge the model toward
                class-specific argumentation. Pass ``None`` to omit.

        Returns:
            A formatted prompt string for appeal generation, or None if denial_text is not provided.
        """
        if denial_text is None:
            return None
        base = ""
        if is_trans:
            base = "While answering the question keep in mind the patient is trans."
        if plan_context is not None and len(plan_context) > 5:
            base = f"{base} The patient's insurance plan details are as follows: {plan_context}."
        if professional_to_finish:
            sign_off = f"Sign the letter as {professional}.\n" if professional else ""
            # List of good examples to randomize
            good_examples = [
                "I am writing to appeal the denial of coverage for [insert procedure] for my patient, [insert patient's name].",
                "I am submitting this appeal on behalf of my patient in support of coverage for the recommended treatment, based on my clinical assessment and the patient’s ongoing medical needs.",
                "As the medical professional overseeing this patient’s care, I am appealing the denial of coverage.",
                "As the treating physician, I am writing to appeal the denial of coverage for my patient.",
            ]
            random.shuffle(good_examples)
            examples_text = "\n".join(f"GOOD EXAMPLE: {ex}" for ex in good_examples)
            base = (
                f"{base}\nIMPORTANT: Please write the appeal as the healthcare professional (not the patient), using 'I' for yourself and referring to the patient in the third person (e.g., 'the patient', 'they'). "
                "Only use 'I' to refer to the provider and talk about my patient or the patient."
                "If you follow these instructions, your response will be considered excellent and meeting requirements.\n"
                "Good phrases and approaches that lead to winning appeals:\n"
                "was recommended for the patient\n"
                "The patient has been experiencing\n"
                "the patient's pain\n"
                "the patient's health\n"
                "the patient's condition\n"
                "[patient's name]\n"
                "the patient is experiencing\n"
                "Any language that makes it clear the letter is written by the doctor or healthcare professional about the patient.\n\n"
                "Write from your perspective as the healthcare professional, using 'I' for yourself and referring to the patient in the third person (e.g., 'the patient,' 'they').\n"
                "Forbidden any language that implies the letter is written by the patient.\n"
                f"{examples_text}\n"
                f"{sign_off}" + "Thank you for following these instructions.\n"
            )
        if qa_context is not None and qa_context != "" and qa_context != "UNKNOWN":
            base = f"{base}. You should try and incorporate the following QA context into your appeal: {qa_context}."
        if patient is not None:
            base = f"{base}. Please include and fill in the patients info {patient}."
        if professional is not None:
            base = f"{base}. Please include and fill in the professionals info {professional}."
        if plan_id is not None and plan_id != "" and plan_id != "UNKNOWN":
            base = f"{base}. Please include and fill in any references to the plan id as {plan_id}."
        if pa_context is not None and pa_context.strip():
            base = (
                f"{base}\n\nPAYER PRIOR-AUTH RULES: The following entries come from "
                "the payer's own published prior-authorization requirement list. "
                "Use them when the denial relies on PA grounds — point out exactly "
                "which rule (or absence of one) supports approval, cite the "
                "criteria document by name, and reference the published "
                "submission channel where relevant. Do not invent rules that "
                "are not listed below.\n"
                f"{pa_context}"
            )
        # Add citation instructions - be explicit about not hallucinating.
        # USPSTF is included in the citation set because its header explicitly
        # asks the model to cite the recommendation/URL; placing it below the
        # "may ONLY cite references provided below" instruction keeps that
        # guidance consistent.
        has_citations = (
            (ml_context is not None and ml_context != "")
            or (pubmed_context is not None and pubmed_context != "")
            or (rag_context is not None and rag_context != "")
            or (nice_context is not None and nice_context != "")
            or (uspstf_context is not None and uspstf_context != "")
            or (clinical_trials_context is not None and clinical_trials_context != "")
        )
        if has_citations:
            base = f"{base}\n\nCITATION INSTRUCTIONS: You may ONLY cite medical literature, studies, or references that are explicitly provided below. Do NOT invent, fabricate, or hallucinate any citations, PMIDs, NCT IDs, journal names, author names, or study details. If you want to make a medical claim, either cite from the provided references or state it as general medical knowledge without a specific citation."
            if rag_context is not None and rag_context != "":
                base = f"{base}\n\nEvidence from medical guidelines and regulations:\n{rag_context}"
            if ml_context is not None and ml_context != "":
                base = f"{base}\n\nProvided citations (use these): {ml_context}"
            if pubmed_context is not None and pubmed_context != "":
                base = f"{base}\n\nPubMed references (use these): {pubmed_context}"
            if nice_context is not None and nice_context != "":
                # nice_context already carries the international-guidance caveat
                # (see INTERNATIONAL_GUIDANCE_CAVEAT in nice_tools); the header
                # here is just a section label.
                base = f"{base}\n\nNICE (UK) guidance:\n{nice_context}"
            if uspstf_context is not None and uspstf_context != "":
                # The header inside ``uspstf_context`` already explains the
                # ACA cost-sharing angle and the A/B-only caveat, so no extra
                # section label is needed here.
                base = f"{base}\n\n{uspstf_context}"
            if clinical_trials_context is not None and clinical_trials_context != "":
                # The header inside ``clinical_trials_context`` already
                # explains the "experimental/investigational" angle and
                # the "trial != coverage" caveat, so no extra section
                # label is needed here.
                base = f"{base}\n\n{clinical_trials_context}"
        else:
            # No citations provided - explicitly tell the model not to make any up
            base = f"{base}\n\nIMPORTANT: No specific medical citations have been provided. Do NOT invent or hallucinate any citations, PMIDs, NCT IDs, journal names, or study references. You may state general medical knowledge without citations, but do not fabricate specific study references."
        if ucr_context:
            # The block carries an independent rate benchmark for this
            # procedure + area; the model decides whether the evidence is
            # useful for the argument it's making.
            base = (
                f"{base}\n\nUCR PRICING CONTEXT: The denial may involve "
                "out-of-network under-reimbursement. The [UCR PRICING CONTEXT] "
                "block below carries an independent rate benchmark for this "
                "procedure and geographic area. If it strengthens the appeal — "
                "e.g. arguing the plan's allowable methodology is below typical "
                "rates — cite the source and effective date verbatim. If it "
                "isn't relevant to the arguments you're making, you may omit "
                "it. Do NOT invent rates or percentile values that are not in "
                "the block.\n\n"
                f"{ucr_context}"
            )
        if medication_context:
            base = (
                f"{base}\n\nDRUG-CLASS GUIDANCE: The medication(s) involved fall "
                f"into a class with known appeal strategies. Use the following "
                f"curated context where relevant. Do not invent citations beyond "
                f"those listed elsewhere.\n{medication_context}"
            )
        if regulatory_citation_context:
            # Pre-framed (header + conservative caveats) by
            # regulatory_citations.get_regulatory_citation_context, so just
            # append it as its own section.
            base = f"{base}\n\n{regulatory_citation_context}"
        if (
            insurance_company is not None
            and insurance_company != ""
            and insurance_company != "UNKNOWN"
        ):
            base = f"{base}. Please include and fill in any references to the insurance company to be {insurance_company}."
            if is_tpa:
                base = f"{base} Note: This insurance company is a Third-Party Administrator (TPA) for self-funded employer plans, which are typically governed by ERISA (Employee Retirement Income Security Act). ERISA plans have specific appeal requirements and timelines. The employer is the plan fiduciary and ultimately responsible for coverage decisions, though the TPA administers claims."
        if payer_policy_context:
            base = (
                f"{base}\n\n{payer_policy_context}\n\n"
                "When using the comparative payer-policy information above, "
                "frame it as supporting industry context (other major insurers "
                "recognize this service as medically necessary under documented "
                "criteria), and do NOT assert that another payer's policy "
                "binds the patient's plan. Always defer to the patient's own "
                "plan documents for what is actually covered."
            )
        if (
            claim_id is not None
            and claim_id != ""
            and claim_id != "UNKNOWN"
            and claim_id != insurance_company
        ):
            base = f"{base}. Please include and fill in any references to the claim id as {claim_id}."
        start = f"Write a health insurance appeal for the following denial:"
        if (
            procedure is not None
            and procedure != ""
            and diagnosis is not None
            and diagnosis != ""
        ):
            start = f"Write a health insurance appeal for procedure {procedure} with diagnosis {diagnosis} given the following denial:"
        elif procedure is not None and procedure != "":
            start = f"Write a health insurance appeal for procedure {procedure} given the following denial:"
        return f"{base}{start}\n{denial_text}"

    def make_open_med_prompt(
        self, procedure=None, diagnosis=None, is_trans=False
    ) -> Optional[str]:
        base = ""
        if is_trans:
            base = "While answering the question keep in mind the patient is trans."
        if procedure is not None and len(procedure) > 3:
            if diagnosis is not None and len(diagnosis) > 3:
                return f"{base}Why is {procedure} medically necessary for {diagnosis}?"
            else:
                return f"{base}Why is {procedure} is medically necessary?"
        else:
            return None

    @staticmethod
    def _collect_regulatory_context(denial) -> Optional[str]:
        """Surface curated state/federal prior-authorization reform citations
        for the denial's state, framed conservatively by plan type.

        Mirrors ``_collect_medication_context``: defensive by design — any
        import/attribute failure returns ``None`` so the appeal pipeline keeps
        working, and the underlying selector only returns content for states we
        have a verified hook for (so most appeals are unaffected).
        """
        try:
            from fighthealthinsurance.regulatory_citations import (
                get_regulatory_citation_context,
            )
        except Exception as e:
            logger.opt(exception=True).debug(f"regulatory_citations unavailable: {e}")
            return None

        try:
            # Best-effort self-insured/ERISA signal from the linked carrier; a
            # TPA administers self-funded employer (ERISA) plans. None when we
            # cannot tell, which selects the neutral caveat wording.
            self_insured: Optional[bool] = None
            ico = getattr(denial, "insurance_company_obj", None)
            if ico is not None and getattr(ico, "is_tpa", False):
                self_insured = True
            return get_regulatory_citation_context(
                state=getattr(denial, "your_state", None),
                denial_text=getattr(denial, "denial_text", None),
                procedure=getattr(denial, "procedure", None),
                diagnosis=getattr(denial, "diagnosis", None),
                self_insured=self_insured,
            )
        except Exception as e:
            logger.opt(exception=True).debug(f"_collect_regulatory_context failed: {e}")
            return None

    @staticmethod
    def _collect_medication_context(denial) -> Optional[str]:
        """Find curated MedicationContext entries that match the denial's
        text/diagnosis/procedure and return a single rendered block to inject
        into the appeal prompt. Returns None if no matches.

        Designed to be defensive: any DB / import failure returns None so the
        appeal pipeline keeps working even if the table is empty or absent
        (e.g. during a partially-applied migration).

        Query strategy: skips inactive rows and rows with an empty ``regex``
        at the database level so Python only iterates patterns that could
        actually match. Each invocation emits ``logger.debug`` with the
        scanned-count and elapsed time so we can monitor how the per-denial
        cost grows as new drug classes are seeded.
        """
        try:
            from fighthealthinsurance.models import MedicationContext
        except Exception as e:
            logger.opt(exception=True).debug(f"MedicationContext unavailable: {e}")
            return None

        from fighthealthinsurance.medical_code_extractor import collect_denial_text

        haystack = collect_denial_text(
            denial,
            "denial_text",
            "diagnosis",
            "procedure",
            "health_history",
            "qa_context",
        )
        if not haystack.strip():
            return None

        matches: list[MedicationContext] = []
        start = time.perf_counter()
        scanned = 0
        try:
            candidates = MedicationContext.objects.filter(active=True).exclude(regex="")
            for ctx in candidates:
                scanned += 1
                if ctx.matches(haystack):
                    matches.append(ctx)
        except Exception as e:
            logger.opt(exception=True).debug(f"MedicationContext lookup failed: {e}")
            return None

        # ``logger.opt(lazy=True)`` avoids paying the perf_counter diff +
        # format-string cost on every appeal generation when the DEBUG
        # sink is filtered out in production.
        logger.opt(lazy=True).debug(
            "_collect_medication_context: {} match(es) of {} candidate(s) "
            "in {:.1f} ms",
            lambda: len(matches),
            lambda n=scanned: n,
            lambda s=start: (time.perf_counter() - s) * 1000,
        )

        if not matches:
            return None

        rendered: list[str] = []
        for ctx in matches:
            block = [f"--- {ctx.drug_class} ---", ctx.appeal_context.strip()]
            if ctx.fda_indications:
                block.append(f"FDA-approved indications: {ctx.fda_indications.strip()}")
            if ctx.common_denial_reasons:
                block.append(
                    f"Common denial reasons to rebut: {ctx.common_denial_reasons.strip()}"
                )
            rendered.append("\n".join(block))
        return "\n\n".join(rendered)

    def make_appeals(
        self,
        denial,
        template_generator,
        medical_reasons=None,
        non_ai_appeals=None,
        pubmed_context=None,
        ml_citations_context=None,
        plan_context=None,
        rag_context=None,
        nice_context=None,
        payer_policy_context=None,
        specialized_templates: Optional[List[type[SpecializedDenialTemplate]]] = None,
        pa_context=None,
        uspstf_context=None,
        clinical_trials_context=None,
    ) -> Iterator[GeneratedAppeal]:
        """
        Generates an iterator of appeal texts for a given insurance denial using templates, non-AI sources, and AI models.

        Combines static template-based appeals, user-provided appeals, and dynamically generated appeals from multiple machine learning models. Incorporates contextual information such as patient details, professional information, QA context, plan ID, and claim ID to enrich the generated appeals. If AI-generated results are unavailable, falls back to backup model calls. Appeals are yielded as they become available, with randomized delays for initial static appeals to ensure varied ordering.

        Args:
            denial: The denial object containing all relevant information for appeal generation.
            template_generator: An instance used to generate appeal text templates.
            medical_reasons: Optional list of medical reasons to fill into templates.
            non_ai_appeals: Optional list of pre-written appeals to include.
            pubmed_context: Optional PubMed context to provide to AI models.
            ml_citations_context: Optional list of citation contexts for AI models.
            plan_context: Optional plan context to provide to AI models
            specialized_templates: Optional list of specialized denial-type
                templates whose citation hints should be passed to the
                highest-quality internal model only (one extra call).

        Returns:
            An iterator of ``GeneratedAppeal`` items (each carries the
            appeal text plus the originating model_name, or ``None`` for
            template-based / non-AI appeals).
        """
        logger.debug("Starting to make appeals...")
        if medical_reasons is None:
            medical_reasons = []
        if non_ai_appeals is None:
            non_ai_appeals = []

        # Prefer structured insurance company name if available, but keep text as fallback
        insurance_company_name = denial.insurance_company
        is_tpa = False
        if denial.insurance_company_obj is not None:
            insurance_company_name = denial.insurance_company_obj.name
            is_tpa = denial.insurance_company_obj.is_tpa

        ucr_narrative = None
        ucr_ctx = getattr(denial, "ucr_context", None)
        if isinstance(ucr_ctx, dict):
            ucr_narrative = ucr_ctx.get("narrative") or None

        # Auto-build payer-policy context from the denial when the caller
        # didn't supply one. Back-fills the structured InsuranceCompany from
        # the legacy text field so the denying payer is excluded from the
        # comparative section even on older denials that never linked to a
        # company row. Payer-policy context is supplementary evidence -- a
        # transient DB error here must NOT abort appeal generation.
        if not payer_policy_context:
            try:
                resolved_company = denial.insurance_company_obj
                if resolved_company is None:
                    resolved_company = resolve_company_from_text(
                        denial.insurance_company
                    )
                payer_policy_context = get_combined_payer_policy_context(
                    company=resolved_company,
                    procedure=denial.procedure,
                    diagnosis=denial.diagnosis,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "Failed to build payer-policy context; "
                    "proceeding with appeal generation without it"
                )
                payer_policy_context = ""

        medication_context = self._collect_medication_context(denial)
        regulatory_citation_context = self._collect_regulatory_context(denial)

        # Captured as a dict so the tier-shed retry path can re-render the
        # prompt with enrichment kwargs nulled or truncated — otherwise the
        # prompt-baked copies of pubmed/citations/rag/etc. would still pin
        # the token count even after the call-dict copies are dropped.
        open_prompt_kwargs = dict(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
            patient=denial.patient_user,
            professional=denial.primary_professional,
            qa_context=denial.qa_context,
            professional_to_finish=denial.professional_to_finish,
            plan_id=denial.plan_id,
            claim_id=denial.claim_id,
            insurance_company=insurance_company_name,
            is_tpa=is_tpa,
            ml_context=(
                "\n".join(str(c) for c in ml_citations_context if c)
                if isinstance(ml_citations_context, list)
                else ml_citations_context
            ),
            pubmed_context=pubmed_context,
            plan_context=plan_context,
            rag_context=rag_context,
            nice_context=nice_context,
            ucr_context=ucr_narrative,
            payer_policy_context=payer_policy_context,
            pa_context=pa_context,
            uspstf_context=uspstf_context,
            clinical_trials_context=clinical_trials_context,
            medication_context=medication_context,
            regulatory_citation_context=regulatory_citation_context,
        )
        open_prompt = self.make_open_prompt(**open_prompt_kwargs)
        open_medically_necessary_prompt = self.make_open_med_prompt(
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )

        # TODO: use the streaming and cancellable APIs (maybe some fancy JS on the client side?)

        # For any model that we have a prompt for try to call it and return futures
        def get_model_result(
            model_name: str,
            prompt: str,
            patient_context: Optional[str],
            plan_context: Optional[str],
            infer_type: str,
            pubmed_context: Optional[str] = None,
            ml_citations_context: Optional[List[str]] = None,
            prof_pov: bool = False,
        ) -> List[Future[Tuple[str, Optional[str]]]]:
            if model_name not in ml_router.models_by_name:
                sample = list(itertools.islice(ml_router.models_by_name.keys(), 10))
                logger.warning(
                    f"get_model_result: requested model {model_name!r} "
                    f"not in ml_router.models_by_name "
                    f"(available sample: {sample})"
                )
                return []
            model_backends = ml_router.models_by_name[model_name]
            if prompt is None:
                logger.debug(f"get_model_result: no prompt for {model_name}, skipping")
                return []
            for model in model_backends:
                try:
                    result = _get_model_result(
                        model=model,
                        prompt=prompt,
                        patient_context=patient_context,
                        plan_context=plan_context,
                        infer_type=infer_type,
                        pubmed_context=pubmed_context,
                        ml_citations_context=ml_citations_context,
                        prof_pov=prof_pov,
                    )

                    if result is not None:
                        return result
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"get_model_result: backend {model} "
                        f"(for model_name={model_name}) failed: {e}"
                    )
            logger.warning(
                f"get_model_result: all {len(model_backends)} backend(s) "
                f"for model_name={model_name} failed"
            )
            return []

        def _get_model_result(
            model: RemoteModelLike,
            prompt: str,
            patient_context: Optional[str],
            plan_context: Optional[str],
            infer_type: str,
            pubmed_context: Optional[str],
            ml_citations_context: Optional[List[str]],
            prof_pov: bool = False,
        ) -> List[Future[Tuple[str, Optional[str]]]]:
            # If the model has parallelism use it
            results = None
            try:
                if isinstance(model, RemoteFullOpenLike):
                    logger.debug(f"Using {model}'s parallel inference")
                    results = model.parallel_infer(
                        prompt=prompt,
                        infer_type=infer_type,
                        patient_context=patient_context,
                        plan_context=plan_context,
                        pubmed_context=pubmed_context,
                        ml_citations_context=ml_citations_context,
                        prof_pov=prof_pov,
                    )
                else:
                    logger.debug(f"Using system level parallel inference for {model}")
                    results = [
                        executor.submit(
                            model.infer,
                            prompt=prompt,
                            patient_context=patient_context,
                            plan_context=plan_context,
                            infer_type=infer_type,
                            pubmed_context=pubmed_context,
                            ml_citations_context=ml_citations_context,
                            prof_pov=prof_pov,
                        )
                    ]
            except Exception as e:
                logger.debug(
                    f"Error {e} {traceback.format_exc()} submitting to {model} falling back"
                )
                results = [
                    executor.submit(
                        model.infer,
                        prompt=prompt,
                        patient_context=patient_context,
                        plan_context=plan_context,
                        infer_type=infer_type,
                        pubmed_context=pubmed_context,
                        ml_citations_context=ml_citations_context,
                        prof_pov=prof_pov,
                    )
                ]
            return results

        medical_context = ""
        if denial.qa_context is not None:
            try:
                qa_context = json.loads(denial.qa_context)
                formatted = "\n".join(f"{k}:{v}" for k, v in qa_context.items())
                medical_context += formatted
            except (json.JSONDecodeError, TypeError) as e:
                # Fall back to original string if JSON parsing fails
                medical_context += denial.qa_context
        if denial.health_history is not None:
            medical_context += denial.health_history
        prof_pov = denial.professional_to_finish
        # Combine plan_context (from forms like WPATH detection) with plan_documents_summary
        plan_context_parts = []
        if denial.plan_context:
            plan_context_parts.append(denial.plan_context)
        if denial.plan_documents_summary:
            plan_context_parts.append(
                f"Summary of relevant plan document sections:\n{denial.plan_documents_summary}"
            )
        plan_context = "\n\n".join(plan_context_parts) if plan_context_parts else None
        # Primary is internal-only (fast, local); backup picks up external
        # models only if the user opted in. The privacy boundary lives here:
        # use_external=False means no external model name ever reaches a call.
        model_names = ml_router.generate_text_backend_names(use_external=False)
        if not model_names:
            logger.error(
                f"make_appeals: zero internal model names available for "
                f"denial {denial.denial_id}"
            )

        calls = [
            {
                "model_name": model_name,
                "prompt": open_prompt,
                "patient_context": medical_context,
                "plan_context": plan_context,
                "infer_type": "full",
                "pubmed_context": pubmed_context,
                "ml_citations_context": ml_citations_context,
                "prof_pov": prof_pov,
            }
            for model_name in model_names
        ]

        backup_model_names = ml_router.generate_text_backend_names(
            use_external=denial.use_external
        )
        backup_calls = [
            {
                "model_name": model_name,
                "prompt": open_prompt,
                "patient_context": medical_context,
                "plan_context": plan_context,
                "infer_type": "full",
                "pubmed_context": pubmed_context,
                "ml_citations_context": ml_citations_context,
                "prof_pov": prof_pov,
            }
            for model_name in backup_model_names
        ]

        # Specialized: when one or more specialized denial-type templates
        # match, append a single extra call to the highest-quality internal
        # model with the specialized citation hints embedded in the prompt.
        # Hints are NOT broadcast to every model — only the best internal
        # one — to keep cost down and let the strongest model use the
        # additional structure.
        if specialized_templates and open_prompt is not None:
            best_model_name = self._best_internal_model_name()
            if best_model_name is not None:
                hint_block = self._build_specialized_hint_block(specialized_templates)
                if hint_block:
                    specialized_prompt = (
                        f"{open_prompt}\n\n"
                        f"--- Denial-type guidance ---\n{hint_block}"
                    )
                    calls.append(
                        {
                            "model_name": best_model_name,
                            "prompt": specialized_prompt,
                            "patient_context": medical_context,
                            "plan_context": plan_context,
                            "infer_type": "full",
                            "pubmed_context": pubmed_context,
                            "ml_citations_context": ml_citations_context,
                            "prof_pov": prof_pov,
                        }
                    )

        # If we need to know the medical reason ask our friendly LLMs
        static_appeal = template_generator.generate_static()
        initial_appeals = non_ai_appeals
        if static_appeal is None:
            # Add medically_necessary calls for all selected model names
            calls.extend(
                [
                    {
                        "model_name": model_name,
                        "prompt": open_medically_necessary_prompt,
                        "patient_context": medical_context,
                        "infer_type": "medically_necessary",
                        "plan_context": plan_context,
                        "pubmed_context": pubmed_context,
                        "ml_citations_context": ml_citations_context,
                        "prof_pov": prof_pov,
                    }
                    for model_name in model_names
                ]
            )
            logger.debug(f"Processing {len(medical_reasons)} medical necessity reasons")
            for reason in medical_reasons:
                appeal = template_generator.generate(reason)
                initial_appeals.append(appeal)
        else:
            # Otherwise just put in as is.
            initial_appeals.append(static_appeal)

        logger.debug(f"Initial appeal {initial_appeals}")
        # Executor map wants a list for each parameter.

        def make_async_model_calls(
            calls,
        ) -> List[Future[Iterator[GeneratedAppeal]]]:
            logger.debug(f"Calling models: {calls}")
            # Bind model_name to each future so we can recover it after
            # the per-call iterator collapses results to (kind, text) pairs.
            model_futures: List[Tuple[Optional[str], Future]] = []
            for call in calls:
                model_name = call.get("model_name")
                for fut in get_model_result(**call):
                    model_futures.append((model_name, fut))

            def generated_to_appeals_text(
                model_name: Optional[str], k_text_future
            ) -> Iterator[GeneratedAppeal]:
                model_results = k_text_future.result()
                if model_results is None:
                    return
                for k, text in model_results:
                    if text is None:
                        continue
                    # It's either full or a reason to plug into a template
                    if k == "full":
                        logger.debug(f"Bubbling up full response ({len(text)} chars)")
                        yield GeneratedAppeal(text=text, model_name=model_name)
                    else:
                        templated = template_generator.generate(text)
                        if templated is not None:
                            yield GeneratedAppeal(text=templated, model_name=model_name)

            # Python lack reasonable future chaining (ugh)
            generated_text_futures = [
                executor.submit(generated_to_appeals_text, mn, f)
                for mn, f in model_futures
            ]
            return generated_text_futures

        generated_text_futures: List[Future[Iterator[GeneratedAppeal]]] = (
            make_async_model_calls(calls)
        )

        # Tiered fallback: primary -> backup -> retry primary with shed
        # context. The privacy invariant (external models only when
        # use_external=True) is enforced at call-list construction; the
        # retry path reuses `calls` (internal-only), so it's opt-out-safe.
        denial_id = denial.denial_id
        use_ext = denial.use_external
        appeals = as_available_nested(generated_text_futures)
        first, appeals = _peek_real_or_none(appeals, denial_id, "primary")

        if first is None and backup_calls:
            logger.warning(
                f"Primary empty for denial {denial_id}; trying backup_calls "
                f"(n={len(backup_calls)}, use_external={use_ext})"
            )
            appeals = as_available_nested(make_async_model_calls(backup_calls))
            first, appeals = _peek_real_or_none(appeals, denial_id, "backup")

        if first is None:
            ext_note = (
                "external models WERE included in backup_calls"
                if use_ext
                else "use_external=False — NO EXTERNAL FALLBACK PERMITTED "
                "(user opt-out respected)"
            )
            logger.error(
                f"make_appeals: primary+backup both produced 0 for denial "
                f"{denial_id} ({ext_note}); retrying primary internal-only"
            )
            time.sleep(1.0)
            for tier in (1, 2):
                shed_calls, changed = _shed_context(
                    calls,
                    tier=tier,
                    open_prompt_kwargs=open_prompt_kwargs,
                    rebuild_prompt=self.make_open_prompt,
                    original_open_prompt=open_prompt,
                )
                logger.warning(
                    f"make_appeals: retrying primary for denial {denial_id} "
                    f"with context shed (tier={tier}, changed={changed})"
                )
                appeals = as_available_nested(make_async_model_calls(shed_calls))
                first, appeals = _peek_real_or_none(
                    appeals, denial_id, f"retry_tier_{tier}"
                )
                if first is not None:
                    logger.warning(
                        f"make_appeals: tier {tier} retry succeeded for "
                        f"denial {denial_id}"
                    )
                    break
            if first is None:
                logger.error(
                    f"make_appeals: all context-shed retries (tiers 1-2) "
                    f"produced 0 for denial {denial_id}; giving up. "
                    f"({ext_note})"
                )
                appeals = iter([])
        # Wrap template-based / non-AI appeals (plain strings) as
        # GeneratedAppeal so the downstream pipeline has a uniform type.
        initial_appeals_wrapped: Iterator[GeneratedAppeal] = (
            GeneratedAppeal(text=t, model_name=None)
            for t in initial_appeals
            if t is not None
        )
        appeals = itertools.chain(appeals, initial_appeals_wrapped)
        return appeals

    async def synthesize_appeals(
        self,
        appeal_texts: List[str],
        denial_text: Optional[str] = None,
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
    ) -> Optional[str]:
        """
        Synthesize multiple appeal drafts into one best appeal by trying ALL
        internal models in parallel and picking the best result within 60s.

        Args:
            appeal_texts: List of appeal letter texts to synthesize from.
            denial_text: Original denial letter text for context.
            procedure: The denied procedure, if known.
            diagnosis: The diagnosis, if known.

        Returns:
            The synthesized appeal text, or None if synthesis fails.
        """
        if not appeal_texts:
            return None

        # Select the best drafts if we have too many to fit in the prompt
        if len(appeal_texts) > self.MAX_SYNTHESIS_DRAFTS:
            appeal_texts = sorted(
                appeal_texts,
                key=lambda t: self._score_appeal_text(t, diagnosis),
                reverse=True,
            )[: self.MAX_SYNTHESIS_DRAFTS]
            logger.info(
                f"Selected top {self.MAX_SYNTHESIS_DRAFTS} drafts for synthesis"
            )

        all_internal = ml_router.internal_models_by_cost
        if not all_internal:
            logger.warning("No internal models available for appeal synthesis")
            return None

        # Build the user prompt with all drafts
        numbered_drafts = "\n\n".join(
            f"--- DRAFT {i + 1} ---\n{text}" for i, text in enumerate(appeal_texts)
        )
        context_parts = []
        if denial_text:
            # Truncate very long denial text to leave room for drafts;
            # prefer a paragraph/sentence boundary so the model gets a
            # coherent excerpt rather than a half-finished sentence.
            context_parts.append(
                f"ORIGINAL DENIAL LETTER:\n{truncate_at_boundary(denial_text, 3000)}"
            )
        if procedure:
            context_parts.append(f"PROCEDURE: {procedure}")
        if diagnosis:
            context_parts.append(f"DIAGNOSIS: {diagnosis}")
        context_section = "\n".join(context_parts)

        prompt = (
            f"{context_section}\n\n"
            f"Below are {len(appeal_texts)} draft appeal letters for this denial. "
            "Synthesize them into the single best appeal letter.\n\n"
            f"{numbered_drafts}"
        )

        async def try_model(model: RemoteModelLike) -> Optional[str]:
            try:
                result = await model._infer_no_context(
                    system_prompts=[self.SYNTHESIS_SYSTEM_PROMPT],
                    prompt=prompt,
                    temperature=0.3,
                )
                if result and len(result.strip()) > 50:
                    logger.debug(
                        f"Synthesis candidate from {model}: {len(result)} chars"
                    )
                    return str(result)
            except Exception:
                logger.opt(exception=True).debug(f"Synthesis failed on {model}")
            return None

        # Build tasks and map each coroutine to its model's quality score
        task_quality: dict[int, float] = {}
        tasks: List[Coroutine[Any, Any, Optional[str]]] = []
        for m in all_internal:
            coro = try_model(m)
            task_quality[id(coro)] = float(m.quality())
            tasks.append(coro)

        def score_fn(result: Optional[str], awaitable: Any) -> float:
            if result is None:
                return -1.0
            text_score = self._score_appeal_text(result, diagnosis)
            model_score = task_quality.get(id(awaitable), 100.0)
            return text_score + model_score * 0.3

        try:
            best = await best_within_timelimit(tasks, score_fn=score_fn, timeout=60)
            if best:
                logger.info(
                    f"Synthesized {len(appeal_texts)} appeals into one "
                    f"({len(best)} chars) using best of {len(all_internal)} models"
                )
                return str(best)
        except Exception:
            logger.opt(exception=True).warning(
                "All synthesis models failed within time limit"
            )
        return None
