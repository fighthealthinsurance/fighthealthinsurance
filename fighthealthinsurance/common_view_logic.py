import asyncio
import datetime
import json
import os
import re
import tempfile
import time
import typing
import uuid
from dataclasses import dataclass
from string import Template
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Coroutine,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    AsyncGenerator,
    cast,
)

if TYPE_CHECKING:
    from fighthealthinsurance.financial_assistance_directory import (
        FinancialAssistanceResults,
    )
    from fighthealthinsurance.pharmacy_coupon_detector import (
        PharmacyCouponSuggestion,
    )
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone
from django.core.files import File
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, close_old_connections, transaction


from django.db.models import F, Q, QuerySet
from django.db.models.functions import Length
from django.forms import Form
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html, escape as html_escape

import asyncstdlib as a
import ray
import uszipcode
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async

from fighthealthinsurance import generation_lease
from fighthealthinsurance.denial_history_consent import (
    DERIVED_FROM_HEALTH_HISTORY,
    history_may_be_used,
)
from fighthealthinsurance.appeal_fingerprints import fingerprint_text
from loguru import logger
from PyPDF2 import PdfMerger
from stopit.utils import TimeoutException

from fhi_users import emails as fhi_emails
from fhi_users.audit import TrackingInfo
from fhi_users.models import ProfessionalUser, UserDomain
from fighthealthinsurance import stripe_utils
from fighthealthinsurance.context_barrier import warm_then_fetch
from fighthealthinsurance.exec import bridge_executor
from fighthealthinsurance.context_utils import (
    attach_supplemental_to_citations,
    CONTEXT_LEVEL_TEMPLATE,
    CONTEXT_LEVEL_SPECULATIVE,
    CONTEXT_LEVEL_SYNTHESIZED,
    CONTEXT_LEVEL_TIER1_SHED,
    CONTEXT_LEVEL_TIER2_SHED,
    SPECULATIVE_CONTEXT_LEVELS,
    summarize_denial_context_tokens,
)
from fighthealthinsurance.denial_context import (
    generated_question_fields,
    load_qa,
    merge_plan_context,
    merge_qa,
    stored_answer_for_question,
)
from fighthealthinsurance.denials.algorithmic_review_detector import (
    detect_algorithmic_review_terms,
    render_template_blocks,
)
from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.medical_code_extractor import (
    extract_icd10_codes,
    extract_procedure_codes,
)
from fighthealthinsurance.ml import denial_triage, letter_quality
from fighthealthinsurance.ml.bad_output_utils import strip_boilerplate_service
from fighthealthinsurance.reliability_events import capture_reliability_event
from fighthealthinsurance.form_utils import *
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.generate_appeal import backend_label
from fighthealthinsurance.ml.model_attempt_log import (
    OUTCOME_REJECTED_AT_PEEK,
    ModelAttemptRecord,
    ModelAttemptRecorder,
)
from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    canonical_model_name,
)
from fighthealthinsurance.ml.ml_appeal_context_helper import MLAppealContextHelper
from fighthealthinsurance.ml.ml_appeal_questions_helper import (
    MLAppealQuestionsHelper,
    claim_generated_questions,
    questions_fingerprint,
)
from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.ml.imr_decision_retriever import IMRDecisionRetriever
from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper
from fighthealthinsurance.models import *
from fighthealthinsurance.process_denial import ProcessDenialCodes
from fighthealthinsurance.rag_client import get_rag_context_for_denial
from fighthealthinsurance.utils import (
    extract_file_text,
    interleave_iterator_for_keep_alive,
    is_real_appeal,
    keepalive_frames,
    MIN_APPEAL_CHARS,
    sync_iterator_to_async,
    warn_unusable_appeal,
)
from .clinicaltrials_tools import ClinicalTrialsTools
from .pubmed_tools import PubMedTools
from .nice_tools import NICETools
from .email_utils import is_sendable_email
from .utils import (
    _try_pandoc_engines,
    check_call,
    execute_critical_optional_fireandforget,
    fire_and_forget_in_new_threadpool,
    send_fallback_email,
)

appealGenerator = AppealGenerator()


states_with_caps = {
    "AR",
    "CA",
    "CT",
    "DE",
    "DC",
    "GA",
    "IL",
    "IA",
    "KS",
    "KY",
    "ME",
    "MD",
    "MA",
    "MI",
    "MS",
    "MO",
    "MT",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "MP",
    "OK",
    "OR",
    "PA",
    "RI",
    "TN",
    "TX",
    "VT",
    "VI",
    "WV",
}


# What the questions page should say about the questions it is rendering.
# A Django Form is truthy with no fields in it (BaseForm defines neither
# ``__bool__`` nor ``__len__``), so the template cannot work this out from
# the form and has to be told.
QUESTIONS_OUTCOME_PRESENT = "questions"
QUESTIONS_OUTCOME_NONE = "no_questions"
QUESTIONS_OUTCOME_UNFINISHED = "generation_unfinished"


@dataclass
class NextStepInfo:
    outside_help_details: list[Tuple[str, str]]
    combined_form: Form
    semi_sekret: str
    # One of the QUESTIONS_OUTCOME_* values above.
    questions_outcome: str = QUESTIONS_OUTCOME_PRESENT
    # PharmacyCouponSuggestion when the denial concerns a recognizable
    # prescription drug or contains generic prescription cues; None
    # otherwise. Surfaced in outside_help.html so users see GoodRx /
    # Cost Plus / Crush Cost / Amazon Pharmacy options as a cash-pay
    # bridge while they fight the denial. Server-rendered only - not
    # part of the REST serialization.
    pharmacy_coupon_suggestion: Optional["PharmacyCouponSuggestion"] = None
    # FinancialAssistanceResults aggregate (copay foundations, manufacturer
    # programs, safety-net clinics, state Medicaid) when the denial has at
    # least one specific match; None otherwise. Same gating as the pharmacy
    # field: server-rendered into outside_help.html, not REST-serialized.
    financial_assistance: Optional["FinancialAssistanceResults"] = None

    def convert_to_serializable(self) -> "NextStepInfoSerializable":
        return NextStepInfoSerializable(
            outside_help_details=self.outside_help_details,
            combined_form=list(
                map(
                    lambda xy: self._field_to_dict(*xy),
                    self.combined_form.fields.items(),
                )
            ),
            semi_sekret=self.semi_sekret,
            questions_outcome=self.questions_outcome,
        )

    def _field_to_dict(self, field_name: str, field: Any) -> dict[str, Any]:
        label = field.label
        visible = not field.hidden_widget
        required = field.required
        help_text = field.help_text
        initial = field.initial
        field_type = field.__class__.__name__
        r = {
            "name": field_name,
            "field_type": field_type,
            "label": label,
            "visible": visible,
            "required": required,
            "help_text": help_text,
            "initial": initial,
            "type": field_type,
        }
        if hasattr(field, "choices"):
            r["choices"] = field.choices
        return r


class AppealAssemblyHelper:
    async def _convert_input(self, input_path: str) -> Optional[str]:
        if input_path.endswith(".pdf"):
            return input_path
        else:
            await asyncio.sleep(0)
            base_convert_command = [
                "pandoc",
                "--wrap=auto",
                input_path,
                f"-o{input_path}.pdf",
            ]
            try:
                await _try_pandoc_engines(base_convert_command)
                return f"{input_path}.pdf"
            # pandoc failures are often character encoding issues
            except Exception as e:
                # try to convert if we've got txt input
                new_input_path = input_path
                if input_path.endswith(".txt") and not input_path.endswith(
                    ".magic.u8.txt"
                ):
                    try:
                        command = [
                            "iconv",
                            "-c",
                            "-t utf8",
                            f"-o{input_path}.magic.u8.txt",
                            input_path,
                        ]
                        await check_call(command)
                        new_input_path = f"{input_path}.magic.u8.txt"
                        return await self._convert_input(new_input_path)
                    except:
                        pass
                if input_path.endswith(".html") or input_path.endswith(".htm"):
                    html_command = base_convert_command + ["-thtml"]
                    try:
                        await _try_pandoc_engines(html_command)
                        return f"{input_path}.pdf"
                    except:
                        pass
                return None

    async def assemble_single_output(
        self, user_header: str, extra: str, input_paths: list[str], target: str
    ) -> str:
        """Assembles all the inputs into one output. Will need to be chunked."""
        merger = PdfMerger()
        converted_paths = await asyncio.gather(
            *(self._convert_input(path) for path in input_paths)
        )

        for pdf_path in filter(None, converted_paths):
            merger.append(pdf_path)

        merger.write(target)
        merger.close()
        return target

    def create_or_update_appeal(
        self,
        fax_phone: str,
        completed_appeal_text: str,
        company_name: str,
        email: str,
        include_provided_health_history: bool,
        name: str,
        include_cover: bool = True,
        insurance_company: Optional[str] = None,
        denial: Optional[Denial] = None,
        denial_id: Optional[str] = None,
        semi_sekret: Optional[str] = None,
        appeal: Optional[Appeal] = None,
        creating_professional: Optional[ProfessionalUser] = None,
        primary_professional: Optional[ProfessionalUser] = None,
        patient_user: Optional[PatientUser] = None,
        domain: Optional[UserDomain] = None,
        patient_address: Optional[str] = None,
        patient_fax: Optional[str] = None,
        cover_template_path: str = "faxes/cover.html",
        cover_template_string: Optional[str] = None,
        company_phone_number: str = "202-938-3266",
        company_fax_number: str = "415-840-7591",
        pubmed_ids_parsed: Optional[List[str]] = None,
        pending: Optional[bool] = None,
        # If the user is going to pay for the faxing (optional)
        fax_pwyw: Optional[int] = None,
        fax_amount: Optional[int] = None,
        fax_amount_custom: Optional[int] = None,
    ) -> Appeal:
        if denial is None:
            if denial_id is not None:
                denial = (
                    Denial.objects.filter(denial_id=denial_id)
                    .filter(
                        hashed_email=Denial.get_hashed_email(email),
                        semi_sekret=semi_sekret,
                    )
                    .get()
                )
        if denial is None:
            raise Exception("No denial ID or denial provided.")
        # Build our cover page
        professional_name: Optional[str] = None
        if primary_professional:
            professional_name = f"{primary_professional.user.first_name} {primary_professional.user.last_name}"
        # Get the reply fax number
        professional_fax_number: Optional[str] = None
        if (
            primary_professional
            and primary_professional.fax_number is not None
            and len(primary_professional.fax_number) > 5
        ):
            professional_fax_number = primary_professional.fax_number
        elif domain and domain.office_fax:
            professional_fax_number = domain.office_fax
        hashed_email = Denial.get_hashed_email(email)
        # Get the current info
        if insurance_company:
            denial.insurance_company = insurance_company
        else:
            insurance_company = denial.insurance_company
        claim_id = denial.claim_id
        health_history: Optional[str] = None
        if (
            include_provided_health_history
            or denial.include_provided_health_history_in_appeal
        ):
            health_history = denial.health_history
        # Usage based billing goes here
        if appeal and hasattr(appeal, "domain") and appeal.domain:
            stripe_customer_id = appeal.domain.stripe_customer_id
            if stripe_customer_id:
                stripe_utils.increment_meter(
                    user_id=stripe_customer_id,
                    meter_name="Incremental Appeal",
                    quantity=1,
                    identifier=appeal.uuid,
                )
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", prefix="alltogether", mode="w+b", delete=False
        ) as t:
            self._assemble_appeal_pdf(
                insurance_company=insurance_company,
                patient_name=name,
                claim_id=claim_id,
                fax_phone=fax_phone,
                completed_appeal_text=completed_appeal_text,
                health_history=health_history,
                pubmed_ids_parsed=pubmed_ids_parsed,
                company_name=company_name,
                cover_template_path=cover_template_path,
                cover_template_string=cover_template_string,
                company_phone_number=company_phone_number,
                company_fax_number=company_fax_number,
                professional_fax_number=professional_fax_number,
                professional_name=professional_name,
                target=t.name,
                include_cover=include_cover,
            )
            t.flush()
            t.seek(0)
            doc_fname = os.path.basename(t.name)

            if appeal is None:
                appeal = Appeal.objects.create(
                    for_denial=denial,
                    appeal_text=completed_appeal_text,
                    hashed_email=hashed_email,
                    document_enc=File(t, name=doc_fname),
                    primary_professional=primary_professional,
                    creating_professional=creating_professional,
                    patient_user=patient_user,
                    domain=domain,
                    pubmed_ids_json=pubmed_ids_parsed,
                )
            else:
                # Instead of using update(), set values individually preserving existing ones if not provided
                if denial:
                    appeal.for_denial = denial
                if completed_appeal_text:
                    appeal.appeal_text = completed_appeal_text
                if hashed_email:
                    appeal.hashed_email = hashed_email
                appeal.document_enc = File(t, name=doc_fname)
                if primary_professional:
                    appeal.primary_professional = primary_professional
                if creating_professional:
                    appeal.creating_professional = creating_professional
                if patient_user:
                    appeal.patient_user = patient_user
                if domain:
                    appeal.domain = domain
                if pubmed_ids_parsed:
                    appeal.pubmed_ids_json = pubmed_ids_parsed
            if pending is not None:
                appeal.pending = pending
            appeal.save()
            return appeal

    # TODO: Asyncify
    def _assemble_appeal_pdf(
        self,
        insurance_company: Optional[str],
        fax_phone: str,
        completed_appeal_text: str,
        company_name: str,
        patient_name: str,
        claim_id: Optional[str],
        include_cover: bool = True,
        health_history: Optional[str] = None,
        patient_address: Optional[str] = None,
        patient_fax: Optional[str] = None,
        cover_template_path: str = "faxes/cover.html",
        cover_template_string: Optional[str] = None,
        company_phone_number: str = "202-938-3266",
        company_fax_number: str = "415-840-7591",
        professional_fax_number: Optional[str] = None,
        professional_name: Optional[str] = None,
        pubmed_ids_parsed: Optional[List[str]] = None,
        target: str = "",
    ):
        if len(target) < 2:
            return
        files_for_fax: list[str] = []
        if include_cover:
            # Build our cover page
            onbehalf_of_name = f"{professional_name} and {patient_name}"
            cover_context = {
                "receiver_name": insurance_company or "",
                "receiver_fax_number": fax_phone,
                "company_name": company_name,
                "company_fax_number": company_fax_number,
                "company_phone_number": company_phone_number,
                "fax_sent_datetime": str(datetime.datetime.now()),
                "provider_fax_number": (professional_fax_number or professional_name),
                "provider_name": professional_name,
                "professional_fax_number": professional_fax_number,
                "patient_name": patient_name,
                "onbehalf_of_name": onbehalf_of_name,
                "claim_id": claim_id,
            }
            cover_content: str = ""
            # Render the cover content
            if cover_template_string and len(cover_template_string) > 1:
                cover_content = Template(cover_template_string).substitute(
                    cover_context
                )
                logger.debug(
                    f"Rendered cover letter from string ({len(cover_content)} chars)"
                )
            else:
                cover_content = render_to_string(
                    cover_template_path,
                    context=cover_context,
                )
                logger.debug(
                    f"Rendered cover letter from {cover_template_path} ({len(cover_content)} chars)"
                )
            cover_letter_file = tempfile.NamedTemporaryFile(
                suffix=".html", prefix="info_cover", mode="w+t", delete=False
            )
            cover_letter_file.write(cover_content)
            cover_letter_file.flush()
            files_for_fax.append(cover_letter_file.name)
            logger.debug(f"Added cover letter {cover_letter_file.name}")

        # Appeal text
        appeal_text_file = tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=False
        )
        appeal_text_file.write(completed_appeal_text)
        appeal_text_file.flush()
        files_for_fax.append(appeal_text_file.name)
        logger.debug(f"Added appeal text {appeal_text_file.name}")

        # Health history
        # Make the file scope up here so it lasts until after we've got the single output
        health_history_file = None
        if health_history and len(health_history) > 2:
            health_history_file = tempfile.NamedTemporaryFile(
                suffix=".txt", prefix="healthhist", mode="w+t", delete=False
            )
            health_history_file.write("Health History:\n")
            health_history_file.write(health_history)
            files_for_fax.append(health_history_file.name)
            health_history_file.flush()
            logger.debug(f"Added health history {health_history_file.name}")

        # PubMed articles
        if pubmed_ids_parsed is not None and len(pubmed_ids_parsed) > 0:
            pmt = PubMedTools()
            pubmed_docs: list[PubMedArticleSummarized] = async_to_sync(
                pmt.get_articles
            )(pubmed_ids_parsed)
            pdf_count = 0
            if pubmed_docs:
                pubmed_docs_paths = [
                    x
                    for x in map(async_to_sync(pmt.article_as_pdf), pubmed_docs)
                    if x is not None
                ]
                files_for_fax.extend(pubmed_docs_paths)
                pdf_count = len(pubmed_docs_paths)
            logger.debug(
                f"PubMed: requested {len(pubmed_ids_parsed)} articles "
                f"({pubmed_ids_parsed}), retrieved {len(pubmed_docs)}, "
                f"added {pdf_count} PDFs"
            )
        # TODO: Add more generic DOI handler.

        # Combine and return path
        target = async_to_sync(self.assemble_single_output)(
            input_paths=files_for_fax,
            extra="",
            user_header=str(uuid.uuid4()),
            target=target,
        )
        logger.debug(f"Final target is {target}")
        return target


async def _record_synthesis_attempt(
    *,
    denial_id: int,
    generation_id: Optional[str],
    model: Any,
    text: str,
    duration_seconds: float,
    started_wall: Any,
    detail: str = "",
) -> None:
    """Persist which backend wrote a synthesized letter, as a ModelCallAttempt
    row at stage "synthesis". The ProposedAppeal row keeps the reserved
    "synthesized" model name -- that is the dashboard's bucket -- so this row
    is the only record of the model that actually produced the text the user
    may go on to send. Best-effort, like every attempt write."""
    try:
        recorder = ModelAttemptRecorder(
            denial_id=denial_id, generation_id=generation_id, run_kind="live"
        )
        recorder.record(
            ModelAttemptRecord(
                model_name=canonical_model_name(model),
                outcome="ok",
                stage="synthesis",
                context_level=CONTEXT_LEVEL_SYNTHESIZED,
                infer_type="synthesis",
                backend=backend_label(model),
                response_text=text,
                response_chars=len(text),
                duration_ms=int(duration_seconds * 1000),
                started_at=started_wall,
                error_detail=detail,
            )
        )
        await recorder.aflush()
    except Exception:
        logger.opt(exception=True).debug("Could not record the synthesis attempt")


def substitute_appeal_fields(denial: Denial, content: str) -> str:
    """Substitute the denial's own values (insurance company, claim id,
    diagnosis, procedure, the patient's and professional's names, dates ...)
    for the placeholders a draft carries.

    Every frame the browser receives goes through this while the stored draft
    keeps its placeholders, so text that comes back from the browser is
    compared with a draft only after the draft has been substituted the same
    way (see mark_proposal_chosen)."""
    insurance_company = "{insurance_company}"
    if (
        denial.insurance_company is not None
        and denial.insurance_company != ""
        and denial.insurance_company != "UNKNOWN"
    ):
        insurance_company = denial.insurance_company
    claim_id = "{claim_id}"
    if (
        denial.claim_id is not None
        and denial.claim_id != ""
        and denial.claim_id != "UNKNOWN"
        and denial.claim_id != insurance_company
    ):
        claim_id = denial.claim_id
    diagnosis = "{diagnosis}"
    if (
        denial.diagnosis is not None
        and denial.diagnosis != ""
        and denial.diagnosis != "UNKNOWN"
    ):
        diagnosis = denial.diagnosis
    procedure = "{procedure}"
    if (
        denial.procedure is not None
        and denial.procedure != ""
        and denial.procedure != "UNKNOWN"
    ):
        procedure = denial.procedure
    # Substitutes for common terms - using {{PLACEHOLDER}} format
    # matching data pipeline conventions
    subs = {
        # Insurance company substitutions
        "Esteemed Members of the Appeals Committee": insurance_company,
        "{{insurance_company}}": insurance_company,
        "[insurance_company]": insurance_company,
        "{insurance_company}": insurance_company,
        "insurance_company": insurance_company,
        "[Insurance Company Name]": insurance_company,
        "[Insurance Company]": insurance_company,
        "[Health Plan]": insurance_company,
        "Dear Insurance Company": f"Dear {insurance_company}",
        "Dear Health Plan": f"Dear {insurance_company}",
        "Dear Sir/Madam": f"Dear {insurance_company}",
        # Date
        "[Insert Date]": denial.date or "{{date}}",
        # Claim/Case ID
        "{{CASEID}}": claim_id,
        "[Reference Number from Denial Letter]": claim_id,
        "[Claim ID]": claim_id,
        "{claim_id}": claim_id,
        # Subscriber/Group IDs - leave {{SCSID}} and {{GPID}} intact
        # for frontend (appeal.ts) to fill from localStorage
        # using the actual subscriber_id and group_id values
        # Diagnosis & Procedure
        "[Diagnosis]": diagnosis,
        "[Procedure]": procedure,
        "{diagnosis}": diagnosis,
        "{procedure}": procedure,
        # Legacy $-prefixed keys (used in fixture templates)
        "$insurance_company": insurance_company,
        "$DATE": denial.date or "{{date}}",
        "$diagnosis": diagnosis,
        "$procedure": procedure,
        "$claim_id": claim_id,
        "$CASEID": claim_id,
    }
    # Each lookup individually guarded: one failing relation (e.g. a
    # deleted professional profile) must not abort the LATER
    # substitutions too, leaving [Patient Name]-style placeholders in
    # the letter the user downloads.
    try:
        if denial.professional_to_finish and denial.primary_professional is not None:
            prof_name = denial.primary_professional.get_full_name()
            subs["{{Your Name}}"] = prof_name
            subs["[Your Name]"] = prof_name
            subs["YourNameMagic"] = prof_name
            subs["$your_name_here"] = prof_name
    except Exception as e:
        logger.opt(exception=True).error(
            f"Error fetching professional name for denial sub "
            f"{denial.denial_id}: {e}"
        )
    try:
        if denial.patient_user is not None:
            patient_name = denial.patient_user.get_legal_name()
            subs["{{FIRST_NAME}} {{LAST_NAME}}"] = patient_name
            subs["[Patient Name]"] = patient_name
            subs["[patient name]"] = patient_name
    except Exception as e:
        logger.opt(exception=True).error(
            f"Error fetching patient name for denial sub {denial.denial_id}: {e}"
        )
    try:
        if denial and denial.primary_professional is not None:
            subs["[Professional Name]"] = denial.primary_professional.get_full_name()
    except Exception as e:
        logger.opt(exception=True).error(
            f"Error fetching professional display name for denial sub "
            f"{denial.denial_id}: {e}"
        )
    try:
        if denial.domain:
            subs["[Professional Address]"] = denial.domain.get_address()
    except Exception as e:
        logger.opt(exception=True).error(
            f"Error fetching domain address for denial sub {denial.denial_id}: {e}"
        )
    for k, v in subs.items():
        if v and v != "" and v != "UNKNOWN":
            content = content.replace(k, str(v))
    # Second pass: regex-based fuzzy matching for model-generated
    # placeholder variants like [Claim # Placeholder]
    patient_name_value = subs.get("[Patient Name]", "{{Your Name}}")
    prof_name_value = subs.get("{{Your Name}}", "")
    professional_name_value = subs.get("[Professional Name]", "")
    domain_address_value = subs.get("[Professional Address]", "")
    fuzzy_subs = [
        # Claim/Reference number variants
        (r"\[Claim\s*#?\s*(?:Number\s*)?(?:Placeholder)?\]", claim_id),
        (r"\[Reference\s*#?\s*(?:Number\s*)?(?:Placeholder)?\]", claim_id),
        (r"\[Case\s*#?\s*(?:Number\s*)?(?:Placeholder)?\]", claim_id),
        (r"\[CLAIM_NUMBER\]", claim_id),
        # Diagnosis variants
        (r"\[Diagnosis\s*(?:Code\s*)?(?:Placeholder)?\]", diagnosis),
        # Procedure variants
        (r"\[Procedure\s*(?:Code\s*)?(?:Placeholder)?\]", procedure),
        # Insurance company variants
        (
            r"\[Insurance\s+Company\s*(?:Name\s*)?(?:Placeholder)?\]",
            insurance_company,
        ),
        (
            r"\[Health\s+Plan\s*(?:Name\s*)?(?:Placeholder)?\]",
            insurance_company,
        ),
        # Date variants
        (
            r"\[(?:Insert\s+)?(?:Current\s+)?Date\s*(?:Placeholder)?\]",
            denial.date or "{{date}}",
        ),
        # Patient name variants
        (
            r"\[Patient(?:'?s?)?\s+Name\s*(?:Placeholder)?\]",
            patient_name_value,
        ),
        # Provider/professional name variants
        (
            r"\[(?:Provider|Professional|Doctor|Physician)(?:'?s?)?\s+Name\s*(?:Placeholder)?\]",
            prof_name_value or professional_name_value,
        ),
        # Address variants
        (
            r"\[(?:Provider|Professional|Practice)?\s*Address\s*(?:Placeholder)?\]",
            domain_address_value,
        ),
    ]
    for pattern, value in fuzzy_subs:
        if not value or value == "" or value == "UNKNOWN":
            continue
        str_value = str(value)
        escaped = str_value.replace("\\", r"\\")
        content = re.sub(pattern, escaped, content, flags=re.IGNORECASE)
    return content


def mark_proposal_chosen(
    denial: Denial,
    appeal_text: str,
    editted: Optional[bool] = False,
    proposed_appeal_id: Optional[int] = None,
    draft_unsaved: bool = False,
    arbitrary_text: bool = False,
    presented_ids: Optional[List[int]] = None,
) -> ProposedAppeal:
    """Create a chosen=True ProposedAppeal, copying model_name from the original
    generated row when we can identify which draft was picked.

    Lookup precedence:
      1. proposed_appeal_id (preferred) - the id returned by save_appeal in
         the streaming JSON frame. Survives sub_in_appeals rewriting the
         displayed text (e.g. {claim_id} -> "ABC123") since it does not
         depend on string equality. The id may name an earlier chosen=True
         copy: the appeals page replays a user's pick under the copy's id,
         and the copy carries the attribution copied from its draft.
      2. text match against a chosen=False row for the same denial, on the
         whitespace-normalized fingerprint (browsers submit textarea content
         with CRLF line endings while drafts are stored with LF, so a
         byte-for-byte comparison failed for every multi-line letter), with
         an exact match kept for legacy rows whose fingerprint is NULL.
         Useful as a fallback when the frontend did not echo the id (older
         clients, share-appeal flow).
      3. sole-draft inference - when every draft generated for the denial
         came from one model, the pick necessarily did too (even after
         edits or sub_in_appeals rewrites). Skipped for arbitrary_text=True
         calls (the share-appeal flow submits text that may never have been
         a draft) and for draft_unsaved=True calls (the browser says
         the picked draft was streamed but never stored -- save_appeal's
         save_failed frames -- so the stored drafts say nothing about which
         model produced it).
      4. model_name=None - the user edited the draft heavily and multiple
         models were in play, or the proposal predates the model_name field.

    ``presented_ids`` are the drafts that were on screen when the pick was
    made (the browser reports them), kept on the chosen row for the usage
    dashboard's denominator; they are filtered to this denial's own rows so
    a stray id cannot credit another denial's draft with a presentation.

    ``editted`` records only whether the user changed the draft before
    picking it (the browser reports it from the textarea). It used to double
    as the share flow's marker AND the inference gate, which left the column
    constantly False for the main flow: verbatim-vs-edited per model was
    unmeasurable, and the admin filter on it showed a flow with no edits.
    ``None`` means the caller cannot say (the professional flow has no
    textarea flag): the pick is then recorded as edited when its text is not
    the matched draft's own.
    """
    # speculative=False throughout: a held-back precompute row was never shown
    # to the user, so it can't be the pick. Served speculative rows are flipped
    # to speculative=False when promoted (keeping context_level="speculative"),
    # so they still match here and correctly carry that level onto the chosen
    # row -- which is exactly what analytics wants (users picking a speculative
    # fallback). The guard only excludes held-back rows the user never saw,
    # whose coincidentally-identical text would otherwise mislabel the pick.
    original: Optional[ProposedAppeal] = None
    if proposed_appeal_id is not None:
        # No chosen=False here: a re-submit after a page revisit echoes the
        # id of the user's earlier chosen copy (the replay serves it newest
        # first and dedupes the draft under it). Refusing the copy sent every
        # such pick to sole-draft inference, which gives up the moment two
        # models were in play -- a correctly attributed pick degraded to
        # "(unattributed)" by ordinary back-navigation. A copy that carries
        # no model is no evidence, though, and falls through like a miss.
        original = ProposedAppeal.objects.filter(
            id=proposed_appeal_id,
            for_denial=denial,
            speculative=False,
        ).first()
        if (
            original is not None
            and original.chosen
            and (
                not (original.model_name or "").strip()
                # The backfill's placeholder for a pick it could not attribute
                # is not evidence either: copying it would file a pick made
                # today as a pre-tracking one.
                or original.model_name == LEGACY_UNATTRIBUTED_LABEL
            )
        ):
            original = None
    if original is None:
        original = (
            ProposedAppeal.objects.filter(
                ProposedAppeal.text_match_q(appeal_text),
                for_denial=denial,
                chosen=False,
                speculative=False,
            )
            .order_by("-id")
            .first()
        )
    model_name: Optional[str] = None
    synthesized = False
    context_level: Optional[str] = None
    if original is not None:
        model_name = original.model_name
        synthesized = original.synthesized
        # Carry the draft's shed level onto the chosen row -- otherwise the
        # dashboard/RL export (which read only chosen rows) would be blind to
        # which context level users actually pick.
        context_level = original.context_level
    elif not arbitrary_text and not draft_unsaved:
        inferred = ProposedAppeal.sole_draft_attribution(denial.denial_id)
        if inferred is not None:
            model_name, synthesized, context_level = inferred
    if editted is None:
        # Edited when the text is not the matched draft's own; with no draft
        # matched (an inferred or unattributed pick) it is not any draft's.
        # The browser never saw the stored text -- every frame has the
        # denial's values substituted for the draft's placeholders -- so the
        # draft is compared after the same substitution. A matched chosen
        # copy (a re-pick) keeps its own answer when the text is unchanged.
        if original is None:
            editted = True
        else:
            unchanged = ProposedAppeal.fingerprint(appeal_text) in {
                ProposedAppeal.fingerprint(original.appeal_text),
                ProposedAppeal.fingerprint(
                    substitute_appeal_fields(denial, original.appeal_text or "")
                ),
            }
            if not unchanged:
                editted = True
            else:
                editted = bool(original.editted) if original.chosen else False
    elif not editted and original is not None and original.chosen and original.editted:
        # A re-pick of an edited copy the page replayed: the textarea was left
        # alone this time, but the letter is still the user's edit of the
        # model's draft, not the draft.
        editted = True
    shown: Optional[List[int]] = None
    if presented_ids is not None:
        # In the order the browser reported (the page ranks its cards, so
        # the order says which sat on top), deduped, and only rows this
        # denial could have served: its own, and not a held-back precompute
        # row (speculative=True until served), which no page ever showed and
        # the fallback path excludes too. An empty report is kept as one:
        # "nothing stored was on screen" is not "nobody said" (NULL), for
        # which the dashboard falls back to every draft stored before the
        # pick.
        own = (
            set(
                ProposedAppeal.objects.filter(
                    for_denial=denial, id__in=presented_ids, speculative=False
                ).values_list("id", flat=True)
            )
            if presented_ids
            else set()
        )
        shown = list(dict.fromkeys(i for i in presented_ids if i in own))
    pa = ProposedAppeal(
        appeal_text=appeal_text,
        for_denial=denial,
        chosen=True,
        editted=editted,
        model_name=model_name,
        synthesized=synthesized,
        context_level=context_level,
        presented_ids=shown,
    )
    pa.save()
    return pa


def record_professional_pick(
    denial: Denial,
    appeal_text: Optional[str],
    proposed_appeal_id: Optional[int] = None,
) -> Optional[ProposedAppeal]:
    """Record the text a professional assembled as their pick, for the same
    model-usage reporting the consumer flow feeds through ChooseAppealHelper.

    One pick per professional denial: assemble_appeal is also how a
    professional regenerates the document after fixing a typo, so unchanged
    text is not a new pick, and a changed one replaces the pick an earlier
    assembly recorded rather than adding another -- a denial the professional
    iterated on would otherwise outweigh one they got right first time. Only
    the flow's own rows are replaced: picks that carry an on-screen report
    came from the appeals page. Nothing is recorded when no draft was ever
    stored for the denial: no model was on offer. Best-effort -- reporting
    must never cost the professional their appeal document, so a failure
    here is logged and swallowed.
    """
    try:
        if not appeal_text or not appeal_text.strip():
            return None
        if not ProposedAppeal.objects.filter(
            for_denial=denial, chosen=False, speculative=False
        ).exists():
            return None
        # Unchanged text is not a new pick, compared the way picks are
        # matched to drafts (a CRLF resubmit is the same letter). Chosen rows
        # carry no stored fingerprint, so it is computed here.
        target = ProposedAppeal.fingerprint(appeal_text)
        for existing in ProposedAppeal.objects.filter(
            for_denial=denial, chosen=True
        ).values_list("appeal_text", flat=True):
            if existing == appeal_text or (
                target is not None and ProposedAppeal.fingerprint(existing) == target
            ):
                return None
        with transaction.atomic():
            # completed_appeal_text is post-editing text and this flow has no
            # textarea flag, so whether the pick was edited is read off the
            # text.
            pa = mark_proposal_chosen(
                denial,
                appeal_text,
                proposed_appeal_id=proposed_appeal_id,
                editted=None,
            )
            if denial.creating_professional_id is not None:
                # A new row rather than an update, so the drafts this pick
                # could have been made from are the ones stored before it.
                ProposedAppeal.objects.filter(
                    for_denial=denial, chosen=True, presented_ids__isnull=True
                ).exclude(id=pa.id).delete()
        return pa
    except Exception:
        logger.opt(exception=True).warning(
            f"Could not record the professional's pick for denial "
            f"{denial.denial_id}; the appeal document itself is unaffected"
        )
        return None


class ChooseAppealHelper:
    @classmethod
    def choose_appeal(
        cls,
        denial_id: str,
        appeal_text: str,
        email: str,
        semi_sekret: str,
        proposed_appeal_id: Optional[int] = None,
        draft_unsaved: bool = False,
        editted: bool = False,
        presented_ids: Optional[List[int]] = None,
    ) -> Tuple[
        Optional[str], Optional[str], Optional[QuerySet[PubMedArticleSummarized]]
    ]:
        hashed_email = Denial.get_hashed_email(email)
        # Get the current info
        denial: Denial = Denial.objects.filter(
            denial_id=denial_id, hashed_email=hashed_email, semi_sekret=semi_sekret
        ).get()
        denial.appeal_text = appeal_text
        denial.save()
        mark_proposal_chosen(
            denial,
            appeal_text,
            editted=editted,
            proposed_appeal_id=proposed_appeal_id,
            draft_unsaved=draft_unsaved,
            presented_ids=presented_ids,
        )
        articles = None
        article_ids = None

        # Try to load article IDs from PubMedQueryData
        pmqd = PubMedQueryData.objects.filter(denial_id=denial_id).first()
        if pmqd and pmqd.articles:
            try:
                article_ids = json.loads(pmqd.articles)
            except json.JSONDecodeError as e:
                logger.debug(
                    f"Failed to parse PubMedQueryData articles JSON for denial {denial_id}: {e}"
                )

        # Fallback to denial.pubmed_ids_json if no article_ids yet
        if not article_ids:
            try:
                article_ids = denial.pubmed_ids_json
            except Exception as e:
                logger.debug(
                    f"Error loading articles from denial.pubmed_ids_json for denial {denial_id}: {e}"
                )

        # Query for articles if we have IDs
        if article_ids:
            try:
                articles = PubMedArticleSummarized.objects.filter(
                    pmid__in=article_ids
                ).distinct()
            except Exception as e:
                logger.debug(f"Error finding articles {article_ids}: {e}")

        logger.debug(f"Loaded articles {articles}...")
        return (denial.appeal_fax_number, denial.insurance_company, articles)


@dataclass
class NextStepInfoSerializable:
    outside_help_details: list[Tuple[str, str]]
    combined_form: list[Any]
    semi_sekret: str
    # One of the QUESTIONS_OUTCOME_* values: an empty combined_form is
    # "no_questions" after a finished run and "generation_unfinished" after
    # one that did not finish, and a REST client cannot tell those apart
    # from the form alone.
    questions_outcome: str


def schedule_follow_ups(
    email: str,
    denial: "Denial",
    from_date: Optional[datetime.date] = None,
) -> None:
    """Schedule 1-day, 7-day, 30-day, and 90-day follow-up emails for a denial.

    Args:
        email: Recipient email address.
        denial: The denial to schedule follow-ups for.
        from_date: Base date for computing follow-up dates. Defaults to
            denial.date. Pass datetime.date.today() when re-scheduling
            (e.g. when a user requests additional follow-up).

    Skips follow-ups whose date would already be in the past (e.g. when
    backfilling old denials) and uses update_or_create to prevent duplicates
    atomically.
    """
    if not is_sendable_email(email):
        return
    if from_date is None:
        from_date = denial.date
    follow_up_types = FollowUpType.objects.filter(
        name__in=["followup_1day", "followup_7day", "followup_30day", "followup_90day"]
    )
    today = datetime.date.today()
    for fut in follow_up_types:
        follow_up_date = from_date + fut.duration
        # Skip if the follow-up date is already in the past
        if follow_up_date < today:
            continue
        # Atomic upsert — avoids race condition with exists()+create()
        FollowUpSched.objects.update_or_create(
            denial_id=denial,
            follow_up_type=fut,
            defaults={
                "email": email,
                "follow_up_date": follow_up_date,
            },
        )


class FollowUpHelper:
    @classmethod
    def fetch_denial(
        cls, uuid: str, follow_up_semi_sekret: str, hashed_email: str, **kwargs
    ):
        # Return None (per the callers' `if denial is None` guards) instead of
        # letting DoesNotExist escape: follow-up links live in emails for
        # months, and a denial deleted meanwhile -- or a link mangled by an
        # email client -- used to 500 on page LOAD (get_initial runs on GET).
        try:
            return Denial.objects.filter(
                uuid=uuid, follow_up_semi_sekret=follow_up_semi_sekret
            ).get()
        except Denial.DoesNotExist:
            logger.info(f"fetch_denial: no denial for follow-up link uuid={uuid}")
            return None

    @classmethod
    def store_follow_up_result(
        cls,
        uuid: str,
        follow_up_semi_sekret: str,
        hashed_email: str,
        user_comments: str = "",
        appeal_result: str = "",
        follow_up_again: bool = False,
        medicare_someone_to_help: bool = False,
        email: Optional[str] = None,
        quote: Optional[str] = None,
        name_for_quote: Optional[str] = None,
        use_quote: bool = False,
        followup_documents=None,
    ):
        if followup_documents is None:
            followup_documents = []
        denial = cls.fetch_denial(
            uuid=uuid,
            follow_up_semi_sekret=follow_up_semi_sekret,
            hashed_email=hashed_email,
        )
        if denial is None:
            # Preserve the pre-None-contract behavior for this write path: a
            # follow-up result cannot be stored against a missing denial.
            raise Denial.DoesNotExist(
                f"No denial for follow-up {uuid}/{follow_up_semi_sekret}"
            )
        follow_up = FollowUp.objects.create(
            hashed_email=hashed_email,
            denial_id=denial,
            more_follow_up_requested=follow_up_again,
            follow_up_medicare_someone_to_help=medicare_someone_to_help,
            use_quote=use_quote,
            email=email,
            name_for_quote=name_for_quote,
            quote=quote,
            user_comments=user_comments,
            appeal_result=appeal_result,
        )
        # If they asked for additional follow up, schedule from today
        # so they get a fresh round of check-ins rather than re-using
        # the original denial date (which may already be weeks/months ago).
        if follow_up_again and denial.raw_email:
            schedule_follow_ups(
                denial.raw_email, denial, from_date=datetime.date.today()
            )
        for document in followup_documents:
            if not document:
                continue
            FollowUpDocuments.objects.create(
                follow_up_document_enc=document, denial=denial, follow_up_id=follow_up
            )
        denial.appeal_result = appeal_result or None
        denial.save()
        cls._notify_support_of_feedback(follow_up, denial)

    @staticmethod
    def _notify_support_of_feedback(follow_up: "FollowUp", denial: "Denial") -> None:
        admin_path = reverse(
            "admin:fighthealthinsurance_followup_change",
            args=[follow_up.followup_result_id],
        )
        admin_url = f"https://{settings.FIGHT_HEALTH_INSURANCE_DOMAIN}{admin_path}"
        body = (
            f"New feedback received via the follow-up webform.\n\n"
            f"Denial ID: {denial.denial_id}\n"
            f"Appeal result: {denial.appeal_result or 'N/A'}\n"
            f"Admin: {admin_url}\n"
        )
        try:
            send_mail(
                f"New webform feedback - denial {denial.denial_id}",
                body,
                settings.DEFAULT_FROM_EMAIL,
                ["support42@fighthealthinsurance.com"],
            )
        except Exception:
            logger.opt(exception=True).error(
                f"Error sending feedback notification email "
                f"(denial_id={denial.denial_id}, "
                f"followup_result_id={follow_up.followup_result_id})"
            )


class FindNextStepsHelper:
    @classmethod
    def _build_pharmacy_coupon_suggestion(
        cls, denial: "Denial"
    ) -> Optional["PharmacyCouponSuggestion"]:
        """
        Compute a PharmacyCouponSuggestion for the denial, or None.

        Surfaced on the consumer flow's "next steps" page so users with a
        denied medication can see GoodRx / Cost Plus / Crush Cost / Amazon
        Pharmacy options as a short-term cash-pay bridge while they fight
        the denial. Lazy-imports to avoid circular-import pain at module
        load.

        Best-effort: any failure returns None rather than blocking the
        flow - the rest of the page is still useful without coupons.
        """
        from fighthealthinsurance.pharmacy_coupon_detector import (
            PharmacyCouponSuggestion as _PharmacyCouponSuggestion,
            suggest_for_denial,
        )

        try:
            # Funnel through a typed local so mypy `warn_return_any`
            # doesn't lose the return type across the lazy import +
            # django-stubs plugin combination CI uses.
            suggestion: Optional[_PharmacyCouponSuggestion] = suggest_for_denial(
                denial_text=denial.denial_text,
                procedure=denial.procedure,
                diagnosis=denial.diagnosis,
            )
            return suggestion
        except Exception:
            logger.opt(exception=True).debug(
                "Pharmacy coupon suggestion failed for next-steps; returning None"
            )
            return None

    @classmethod
    def _build_financial_assistance(
        cls, denial: "Denial"
    ) -> Optional["FinancialAssistanceResults"]:
        """
        Look up a FinancialAssistanceResults aggregate for the denial.

        Surfaced on the consumer "next steps" page alongside the pharmacy
        coupon section so users denied a medication see condition-specific
        copay foundations (ADAP for HIV, LLS for blood cancers, etc.),
        manufacturer copay cards, the general directory, 340B safety-net
        clinics, and their state Medicaid pathway.

        Returns None unless the search produced at least one entry tied to
        the patient's specific drug, diagnosis, or state - i.e. there's
        something targeted enough to render a dedicated section. The
        general copay directories alone are not specific enough to gate on
        (they're always returned by `search()`).

        Best-effort: any failure returns None rather than blocking the
        flow - the rest of the page is still useful without the directory.
        """
        from fighthealthinsurance.financial_assistance_directory import (
            FinancialAssistanceResults as _FinancialAssistanceResults,
            search,
        )

        try:
            results: _FinancialAssistanceResults = search(
                drug=denial.procedure,
                diagnosis=denial.diagnosis,
                denial_text=denial.denial_text,
                state_abbreviation=denial.your_state,
            )
        except Exception:
            logger.opt(exception=True).debug(
                "Financial assistance lookup failed for next-steps; returning None"
            )
            return None
        if not results.has_specific_matches():
            return None
        return results

    @classmethod
    def _get_outside_help_details(
        cls, denial: "Denial", state: Optional[str] = None
    ) -> list:
        """Get outside help details based on state and regulator (shared logic)."""
        outside_help_details = []
        state = state or denial.your_state

        if state in states_with_caps:
            outside_help_details.append(
                (
                    (
                        "<a href='https://www.cms.gov/CCIIO/Resources/Consumer-Assistance-Grants/"
                        + state
                        + "'>"
                        + f"Your state {state} participates in a "
                        + f"Consumer Assistance Program (CAP), and you may be able to get help "
                        + f"through them.</a>"
                    ),
                    "Visit CMS.gov for more info<a href='https://www.cms.gov/CCIIO/Resources/Consumer-Assistance-Grants/'> here</a>",
                )
            )
        erisa_regulator = Regulator.objects.filter(alt_name="ERISA").first()
        if erisa_regulator and denial.regulator == erisa_regulator:
            outside_help_details.append(
                (
                    (
                        "Your plan looks to be an ERISA plan which means your employer <i>may</i>"
                        + " have more input into plan decisions. If your are on good terms with HR "
                        + " it could be worth it to ask them for advice."
                    ),
                    "Talk to your employer's HR if you are on good terms with them.",
                )
            )
        # These rows are rendered with ``{% autoescape off %}`` in
        # outside_help.html, so escape the DB-sourced values and only
        # linkify http(s) URLs. Reuse sanitize_http_url so the scheme check
        # is case-insensitive (a valid ``HTTPS://`` URL must not be dropped)
        # and consistent with the escalation-packet path.
        from fighthealthinsurance.escalation_addresses import sanitize_http_url

        regulator = denial.regulator
        website = sanitize_http_url(regulator.website) if regulator else ""
        if regulator is not None and (regulator.phone or website):
            how_to_parts = []
            if regulator.phone:
                how_to_parts.append(f"Call {html_escape(regulator.phone)}")
            if website:
                how_to_parts.append(
                    f"<a href='{html_escape(website)}' target='_blank' rel='noopener'>"
                    "file a complaint online</a>"
                )
            outside_help_details.append(
                (
                    (
                        f"Your denial letter mentions <strong>{html_escape(regulator.name)}</strong>, "
                        "a regulator that oversees this kind of plan. They take consumer "
                        "complaints about denials and can require the plan to respond."
                    ),
                    " or ".join(how_to_parts) + ".",
                )
            )
        return outside_help_details

    @classmethod
    def _build_question_forms(
        cls, denial: "Denial", existing_answers: Optional[dict] = None
    ) -> list:
        """Build question forms from denial types and generated questions (shared logic).

        ``existing_answers`` is the decoded ``qa_context``. Generated
        questions have to resolve their stored answers here rather than in
        ``magic_combined_form``: those answers are filed by
        ``qa_key_for_question``, not by field name, so the lookup by field
        name that the merge does never finds them.
        """
        from django import forms

        answers: dict[str, str] = existing_answers or {}
        question_forms = []
        prof_pov = denial.professional_to_finish

        # Deliberately no ``initial=`` from the denial type: ``appeal_text``
        # is canned appeal boilerplate, not an answer, and an answer box is
        # the person's. Some of those paragraphs are also longer than the
        # field they would land in, so the page would fail its own
        # validation.
        for dt in denial.denial_type.all():
            new_form = dt.get_form()
            if new_form is not None:
                new_form = new_form(prof_pov=prof_pov)
                question_forms.append(new_form)

        # Add generated questions form if available, and only if it was
        # generated for the inputs the row holds now (or predates the stamp):
        # after a failed regeneration the old set is still on the row, and
        # rendering it would show questions about a since-corrected service.
        if denial.generated_questions and denial.generated_questions_for in (
            None,
            questions_fingerprint(denial.procedure, denial.diagnosis),
        ):
            question_fields = generated_question_fields(denial.generated_questions)

            class AppealQuestionsForm(forms.Form):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    for field_name, (
                        question,
                        suggested_answer,
                    ) in question_fields.items():
                        stored = stored_answer_for_question(question, answers)
                        # The model's suggestion goes beside the box, not in
                        # it. The letter is written in this person's voice and
                        # sent over their name, and the box is posted on every
                        # Next whether or not it was touched, so a prefilled
                        # suggestion nobody read became their own account of
                        # their own medical history. Offered as a hint it
                        # still helps with questions like "reason for elevated
                        # risk requiring this screening", which few people can
                        # answer cold. Product owner's call of 2026-09-14.
                        hint = (suggested_answer or "").strip()
                        self.fields[field_name] = forms.CharField(
                            label=question,
                            required=False,
                            initial=stored if stored is not None else "",
                            # Escaped: the form's table template marks
                            # help_text safe, and this text is model output.
                            help_text=(
                                format_html("One way to answer: {}", hint)
                                if hint
                                else ""
                            ),
                        )

            question_forms.append(AppealQuestionsForm())

        return question_forms

    @staticmethod
    def _questions_outcome(combined_form: Form, generation_finished: bool) -> str:
        """Which of the three real states the questions page is in."""
        if combined_form.fields:
            return QUESTIONS_OUTCOME_PRESENT
        if not generation_finished:
            return QUESTIONS_OUTCOME_UNFINISHED
        return QUESTIONS_OUTCOME_NONE

    @classmethod
    def find_next_steps(
        cls,
        denial_id: str,
        email: str,
        procedure: str,
        diagnosis: str,
        insurance_company,
        plan_id,
        claim_id,
        denial_type,
        include_provided_health_history_in_appeal: Optional[bool] = None,
        denial_date: Optional[datetime.date] = None,
        semi_sekret: str = "",
        your_state: Optional[str] = None,
        captcha=None,
        denial_type_text: Optional[str] = None,
        plan_source=None,
        employer_name: Optional[str] = None,
        appeal_fax_number: Optional[str] = None,
        patient_health_history: Optional[str] = None,
        date_of_service: Optional[str] = None,
        in_network: Optional[bool] = None,
        single_case: Optional[bool] = None,
        prof_pov: Optional[bool] = False,
        insurance_company_obj: Optional["InsuranceCompany"] = None,
        insurance_plan_obj: Optional["InsurancePlan"] = None,
    ) -> NextStepInfo:
        hashed_email = Denial.get_hashed_email(email)
        # Update the denial
        denial = Denial.objects.filter(
            denial_id=denial_id,
            # Include the hashed e-mail so folks can't brute force denial_id
            hashed_email=hashed_email,
            semi_sekret=semi_sekret,
        ).get()

        # Snapshot for the round-2 dispatch below, which fires on a
        # correction but not on an unchanged re-POST.
        prior_procedure = denial.procedure
        # The boundary for the reserve retirement below, taken deliberately
        # right after the read of the prior values above rather than at the
        # top of the request. A reserve is written by a background run from a
        # snapshot it took when it started, so a row created before this
        # instant was built from inputs no newer than the ones just read, and
        # is stale once this request changes them. Rows created after it may
        # belong to a later correction and are kept. Moving this earlier
        # would keep stale rows written during the read.
        request_started = timezone.now()
        prior_diagnosis = denial.diagnosis

        # Track exactly which fields THIS request assigns so the save below
        # can write only those columns. The old full-row ``denial.save()``
        # wrote every column from the snapshot loaded above -- while the
        # entity-extraction tasks (fax number, insurer, plan/claim id, date of
        # service, regulator) persist concurrently via ``aupdate`` -- so any
        # extraction landing between the load and the save was silently
        # reverted. It even reverted this function's OWN fax-number write,
        # which used a parallel ``.update()`` the stale instance never saw.
        changed_fields: set[str] = set()

        # A blank submit must not clobber data we already have: these fields
        # are required=False on the form, so an empty string arrives whenever
        # the user leaves the box alone (e.g. because extraction was still
        # populating it) -- overwriting with "" turns a temporarily-empty form
        # into permanent data loss.
        if procedure and len(procedure) < 200:
            denial.procedure = procedure
            changed_fields.add("procedure")
        if diagnosis and len(diagnosis) < 200:
            denial.diagnosis = diagnosis
            changed_fields.add("diagnosis")
        # Truthiness, not ``is not None``: an empty ModelMultipleChoiceField
        # cleans to an empty queryset, and .set([]) would CLEAR the stored
        # plan source (breaking Medicare detection downstream). Matches the
        # ``if denial_type:`` guard below.
        if plan_source:
            denial.plan_source.set(plan_source)
        if patient_health_history:
            denial.health_history = patient_health_history
            changed_fields.add("health_history")
        # Only set employer name if it's not too long
        if employer_name is not None and len(employer_name) < 300:
            denial.employer_name = employer_name
            changed_fields.add("employer_name")
        else:
            employer_name = None
        if (
            appeal_fax_number is not None
            and len(appeal_fax_number) > 5
            and len(appeal_fax_number) < 30
        ):
            logger.debug(f"Setting appeal fax number to {appeal_fax_number}")
            denial.appeal_fax_number = appeal_fax_number
            changed_fields.add("appeal_fax_number")
        else:
            logger.debug(f"Invalid appeal fax number {appeal_fax_number}")

        if include_provided_health_history_in_appeal is not None:
            denial.include_provided_health_history_in_appeal = (
                include_provided_health_history_in_appeal
            )
            changed_fields.add("include_provided_health_history_in_appeal")

        # Get outside help details using shared helper
        outside_help_details = cls._get_outside_help_details(denial, your_state)

        if insurance_company:
            denial.insurance_company = insurance_company
            changed_fields.add("insurance_company")
        if insurance_company_obj is not None:
            denial.insurance_company_obj = insurance_company_obj
            changed_fields.add("insurance_company_obj")
        if insurance_plan_obj is not None:
            denial.insurance_plan_obj = insurance_plan_obj
            changed_fields.add("insurance_plan_obj")
        if plan_id:
            denial.plan_id = plan_id
            changed_fields.add("plan_id")
        if claim_id:
            denial.claim_id = claim_id
            changed_fields.add("claim_id")
        if denial_type_text is not None:
            denial.denial_type_text = denial_type_text
            changed_fields.add("denial_type_text")
        if denial_type:
            denial.denial_type.set(denial_type)

        # load_qa, not a bare json.loads: qa_context is a plain TextField and
        # historical rows hold free text -- a bare parse 500s the review POST
        # on exactly those denials (every other reader is already defensive).
        existing_answers: dict[str, str] = load_qa(denial)

        if your_state:
            # your_state is the column of record; state is a mirror some
            # readers are still on, and doubles as the marker the intake path
            # checks before inferring a state from the zip again.
            denial.your_state = your_state
            denial.state = your_state
            changed_fields.add("your_state")
            changed_fields.add("state")
        if denial_date is not None:
            denial.denial_date = denial_date
            if "denial date" not in existing_answers:
                existing_answers["denial date"] = str(denial_date)
            # The date goes on the row now, by its own statement, and is NOT
            # part of the final save below. The triage resolves its window
            # against the date it reads from the row and writes conditionally
            # on that date; while the corrected date lived only in memory
            # here, a triage in flight could read the old one, pass its
            # predicate, and land a deadline anchored to it after the refresh
            # below had found nothing to reconcile. And two overlapping
            # submissions could have the first one's final save restore its
            # older date under a deadline computed from the second's (review).
            # Written once, here, the date and the deadline only ever change
            # together with the predicate that keeps them consistent.
            Denial.objects.filter(denial_id=denial.denial_id).update(
                denial_date=denial_date
            )
            # Triage usually runs before the user confirms the denial date, so
            # a window like "180 days from notice" was stored unresolved; and
            # a corrected date moves the deadline with it. Only an anchored
            # window follows the date (an absolute date the model chose does
            # not), and only a triage of the CURRENT letter counts.
            # The triage may have landed after this instance was loaded.
            denial.refresh_from_db(
                fields=[
                    "appeal_deadline_label",
                    "appeal_deadline",
                    "triage_text_hash",
                    "triage_source",
                    "denial_text",
                ]
            )
            if denial_triage.is_anchored_window(
                denial.appeal_deadline_label
            ) and denial_triage.is_current(denial):
                resolved = denial_triage.resolve_window(
                    denial.appeal_deadline_label, denial_date
                )
                if resolved != denial.appeal_deadline:
                    # Conditional, like the triage's own write: only while
                    # the row still carries this date, this letter's triage
                    # AND the window we resolved (a second triage of the
                    # same letter can land a different window in between).
                    # A newer submission or a newer triage wins by making
                    # the predicate fail (review).
                    Denial.objects.filter(
                        denial_id=denial.denial_id,
                        denial_date=denial_date,
                        triage_text_hash=denial.triage_text_hash,
                        appeal_deadline_label=denial.appeal_deadline_label,
                    ).update(appeal_deadline=resolved)
                    denial.appeal_deadline = resolved
        # Truthy, not "is not None": the form posts every field on every
        # submit, so an untouched box arrives as "" rather than absent.
        if date_of_service:
            denial.date_of_service = date_of_service
            changed_fields.add("date_of_service")
            if "date of service" not in existing_answers:
                existing_answers["date of service"] = date_of_service
            if "date_of_service" not in existing_answers:
                existing_answers["date_of_service"] = date_of_service
        # This is unique to professional so using this for now to help specialize questions
        prof_pov = denial.professional_to_finish
        if in_network is not None:
            denial.provider_in_network = in_network
            changed_fields.add("provider_in_network")
            # If they know about in_network they are definitely a professional
            prof_pov = True
            if "in_network" not in existing_answers:
                existing_answers["in_network"] = str(in_network)
        if single_case is not None:
            denial.single_case = single_case
            changed_fields.add("single_case")

        # Always include last_interaction: with update_fields, Django only
        # writes auto_now columns that are LISTED, so omitting it would
        # silently freeze the denial's activity timestamp at creation time.
        # And save even when nothing else changed -- reaching this step IS an
        # interaction, and the full-row save this replaced always touched it.
        denial.save(update_fields=sorted(changed_fields | {"last_interaction"}))

        # Round-2 speculative precompute: the user has now CONFIRMED (and
        # possibly corrected) procedure/diagnosis, which the create-time
        # reserve was generated without -- extraction hadn't run yet, let
        # alone been reviewed. Refreshing the held-back reserve here means
        # that if the live generation later underdelivers, the fallback
        # drafts argue about the right service instead of the bare letter.
        # Fire-and-forget with its own guards; never blocks or breaks the
        # questions page.
        try:
            cls._maybe_dispatch_confirmed_speculative(
                denial, prior_procedure, prior_diagnosis, since=request_started
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"speculative appeals[dx_px_confirmed]: dispatch failed for "
                f"denial {denial_id}"
            )

        # Generate questions for better appeal creation if they don't exist yet.
        generation_finished = True
        try:
            if not stored_questions_are_current(denial):
                # Nothing finished for these inputs yet: none stored, a set
                # stored for a procedure or diagnosis since corrected, or an
                # empty set from before the stamp, which always regenerated.
                # A nonempty set from before the stamp is of unknown origin
                # and is kept as it always was.
                logger.debug("Generating appeal questions")
                generated = async_to_sync(
                    DenialCreatorHelper.generate_appeal_questions
                )(denial_id=denial.denial_id)
                generation_finished = generated is not None
                denial.refresh_from_db()
        except Exception as e:
            generation_finished = False
            logger.opt(exception=True).error(
                f"Failed to process appeal questions for denial {denial_id}: {e}"
            )

        # Build question forms using shared helper
        question_forms = cls._build_question_forms(denial, existing_answers)

        # Combine all forms
        pharmacy_coupon_suggestion = cls._build_pharmacy_coupon_suggestion(denial)
        financial_assistance = cls._build_financial_assistance(denial)
        try:
            combined_form = magic_combined_form(question_forms, existing_answers)
            return NextStepInfo(
                outside_help_details=outside_help_details,
                combined_form=combined_form,
                semi_sekret=semi_sekret,
                questions_outcome=cls._questions_outcome(
                    combined_form, generation_finished
                ),
                pharmacy_coupon_suggestion=pharmacy_coupon_suggestion,
                financial_assistance=financial_assistance,
            )
        except Exception as e:
            # Anything landing here rebuilds the page without the answers
            # the person gave, so it is logged at error, not swallowed.
            logger.opt(exception=True).error(
                f"Unexpected error building query {denial_id}: {e}"
            )
            combined_form = magic_combined_form(question_forms, {})
            return NextStepInfo(
                outside_help_details=outside_help_details,
                combined_form=combined_form,
                semi_sekret=semi_sekret,
                questions_outcome=cls._questions_outcome(
                    combined_form, generation_finished
                ),
                pharmacy_coupon_suggestion=pharmacy_coupon_suggestion,
                financial_assistance=financial_assistance,
            )

    @classmethod
    def _maybe_dispatch_confirmed_speculative(
        cls,
        denial: "Denial",
        prior_procedure: Optional[str],
        prior_diagnosis: Optional[str],
        since: Optional[datetime.datetime] = None,
    ) -> None:
        """Kick off the round-2 (confirmed-context) speculative precompute.

        Called after ``find_next_steps`` saves the user's confirmed
        procedure, diagnosis and state. Fires when there is something to
        generate from AND either the user actually changed one of those values
        (their correction supersedes any earlier reserve, including a previous
        confirmed-context one) or no confirmed-context reserve exists yet.
        Re-POSTs of the categorize-review form with unchanged values therefore
        no-op here, and the helper's own guards (skip when live appeals exist,
        replace only after new drafts persist) bound the rest.

        The state is not compared before and after like dx/px: the zip step
        writes it in its own request, so by the time this runs both readings
        already say the new state. Reserve rows carry the state they were
        written for instead, and a state with no confirmed reserve written
        for it gets one.
        """
        confirmed_procedure = (denial.procedure or "").strip()
        confirmed_diagnosis = (denial.diagnosis or "").strip()
        confirmed_state = (denial.your_state or "").strip()
        if not confirmed_procedure and not confirmed_diagnosis and not confirmed_state:
            logger.debug(
                f"speculative appeals[dx_px_confirmed]: denial "
                f"{denial.denial_id} confirmed without procedure, diagnosis or "
                f"state; nothing to refresh with"
            )
            return
        from fighthealthinsurance.context_utils import (
            CONTEXT_LEVEL_SPECULATIVE_CONFIRMED,
        )

        reserve_for_this_state = ProposedAppeal.objects.filter(
            for_denial=denial,
            context_level=CONTEXT_LEVEL_SPECULATIVE_CONFIRMED,
            built_for_state=confirmed_state,
        ).exists()
        dx_px_changed = (prior_procedure or "").strip() != confirmed_procedure or (
            prior_diagnosis or ""
        ).strip() != confirmed_diagnosis
        values_changed = dx_px_changed or not reserve_for_this_state
        if not values_changed:
            logger.debug(
                f"speculative appeals[dx_px_confirmed]: denial "
                f"{denial.denial_id} unchanged dx/px and a confirmed-context "
                f"reserve for {confirmed_state!r} already exists; skipping"
            )
            return

        from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
            dispatch_speculative_appeals,
        )

        if dx_px_changed:
            # A confirmed reserve about the old procedure or diagnosis is
            # worse than none while the replacement is written: a stalled run
            # would serve it, and a replacement that produces nothing would
            # leave it. Retire it now rather than when the replacement lands.
            # Only rows older than this request: a reserve written since is
            # a later correction's and is kept.
            retired, _ = ProposedAppeal.objects.filter(
                for_denial=denial,
                speculative=True,
                chosen=False,
                context_level=CONTEXT_LEVEL_SPECULATIVE_CONFIRMED,
                created_at__lt=since or timezone.now(),
            ).delete()
            if retired:
                logger.info(
                    f"speculative appeals[dx_px_confirmed]: retired {retired} "
                    f"confirmed reserve row(s) for denial {denial.denial_id} "
                    f"written for the values before this correction"
                )
        # force: a confirmed-context reserve written for other values must
        # not veto this one in the helper.
        dispatch_speculative_appeals(
            denial.denial_id,
            force=True,
            trigger="dx_px_confirmed",
            confirmed_context=True,
        )

    @classmethod
    def find_next_steps_for_denial(
        cls,
        denial: "Denial",
        email: str,
        existing_answers: Optional[dict[str, str]] = None,
    ) -> "NextStepInfo":
        """
        Simplified version of find_next_steps for GET requests (back navigation).
        Returns the outside_help info without modifying the denial.

        ``existing_answers`` defaults to the answers stored on the row.
        """
        if existing_answers is None:
            existing_answers = load_qa(denial)
        # Use shared helpers for outside help details and question forms
        outside_help_details = cls._get_outside_help_details(denial)
        question_forms = cls._build_question_forms(denial, existing_answers)
        combined_form = magic_combined_form(question_forms, existing_answers)
        return NextStepInfo(
            outside_help_details=outside_help_details,
            combined_form=combined_form,
            semi_sekret=denial.semi_sekret,
            # Back navigation generates nothing. Whether a run finished for
            # the inputs the row holds now is on the row, by the same rule
            # that starts one: a set stamped for corrected-away inputs, left
            # behind when its replacement never finished, is not finished.
            questions_outcome=cls._questions_outcome(
                combined_form,
                generation_finished=stored_questions_are_current(denial),
            ),
            pharmacy_coupon_suggestion=cls._build_pharmacy_coupon_suggestion(denial),
            financial_assistance=cls._build_financial_assistance(denial),
        )


@dataclass
class DenialResponseInfo:
    selected_denial_type: list[DenialTypes]
    all_denial_types: list[DenialTypes]
    denial_id: int
    uuid: str
    your_state: Optional[str]
    procedure: Optional[str]
    diagnosis: Optional[str]
    employer_name: Optional[str]
    semi_sekret: str
    appeal_fax_number: Optional[str]
    appeal_id: Optional[int]
    claim_id: Optional[str]
    date_of_service: Optional[str]
    insurance_company: Optional[str]
    plan_id: Optional[str]


class PatientNotificationHelper:
    @classmethod
    def send_signup_invitation(
        cls, email: str, professional_name: Optional[str], practice_number: str
    ):
        subject = "Welcome to Fight Paperwork"
        if professional_name:
            subject += " from {professional_name}"
        return send_fallback_email(
            subject=subject,
            template_name="new_patient",
            context={"practice_number": practice_number},
            to_email=email,
        )

    @classmethod
    def notify_of_draft_appeal(
        cls, email: str, professional_name: Optional[str], practice_number: str
    ):
        subject = "Draft Appeal on Fight Paperwork"
        if professional_name:
            subject += " from {professional_name}"
        return send_fallback_email(
            subject=subject,
            template_name="draft_appeal",
            context={"practice_number": practice_number},
            to_email=email,
        )


class ProfessionalNotificationHelper:
    @classmethod
    def send_signup_invitation(
        cls, email: str, professional_name: str, practice_number: str
    ):
        return send_fallback_email(
            subject="You are invited to join your coworker on Fight Paperwork",
            template_name="invite_professional",
            context={
                "professional_name": professional_name,
                "practice_number": practice_number,
            },
            to_email=email,
        )


# The vocabulary the extraction socket speaks. Every frame is a JSON object
# carrying a ``task`` and an ``outcome``.
EXTRACTION_OUTCOME_FOUND = "found"
EXTRACTION_OUTCOME_NOTHING_FOUND = "nothing_found"
EXTRACTION_OUTCOME_FAILED = "failed"
EXTRACTION_OUTCOME_CACHED = "cached"
EXTRACTION_OUTCOME_TIMED_OUT = "timed_out"
# The model found something and we wrote none of it down: every column it
# answered already held a value. Separate from ``found`` because the page's
# words for ``found`` say we filled something in.
EXTRACTION_OUTCOME_KEPT_EXISTING = "kept_existing"

EXTRACTION_OUTCOMES = frozenset(
    {
        EXTRACTION_OUTCOME_FOUND,
        EXTRACTION_OUTCOME_NOTHING_FOUND,
        EXTRACTION_OUTCOME_FAILED,
        EXTRACTION_OUTCOME_CACHED,
        EXTRACTION_OUTCOME_TIMED_OUT,
        EXTRACTION_OUTCOME_KEPT_EXISTING,
    }
)

# Exactly one run-level outcome is sent per run. ``already_have_details`` and
# ``read_and_found_nothing`` are separate because the early-exit gate is an OR
# that includes ``extract_procedure_diagnosis_finished``, and that flag is set
# whenever the model call returned without raising, a return of (None, None)
# included. Collapsing them congratulates someone on details they do not have.
EXTRACTION_RUN_FINISHED = "run_finished"
EXTRACTION_RUN_ALREADY_HAVE_DETAILS = "run_already_have_details"
EXTRACTION_RUN_KEPT_YOUR_DETAILS = "run_kept_your_details"
EXTRACTION_RUN_READ_AND_FOUND_NOTHING = "run_read_and_found_nothing"
EXTRACTION_RUN_OUT_OF_ATTEMPTS = "run_out_of_attempts"
EXTRACTION_RUN_FAILED = "run_failed"

EXTRACTION_RUN_OUTCOMES = frozenset(
    {
        EXTRACTION_RUN_FINISHED,
        EXTRACTION_RUN_ALREADY_HAVE_DETAILS,
        EXTRACTION_RUN_KEPT_YOUR_DETAILS,
        EXTRACTION_RUN_READ_AND_FOUND_NOTHING,
        EXTRACTION_RUN_OUT_OF_ATTEMPTS,
        EXTRACTION_RUN_FAILED,
    }
)

# Wire names for the steps. Keys, not copy: a step is only ever named on the
# page through ``EXTRACTION_TASK_LABELS`` below, and one with no entry there is
# sent but never rendered.
EXTRACTION_TASK_FAX = "fax_number"
EXTRACTION_TASK_INSURANCE_COMPANY = "insurance_company"
EXTRACTION_TASK_INSURANCE_PLAN = "insurance_plan"
EXTRACTION_TASK_PLAN_ID = "plan_id"
EXTRACTION_TASK_CLAIM_ID = "claim_id"
EXTRACTION_TASK_DATE_OF_SERVICE = "date_of_service"
EXTRACTION_TASK_REGULATOR = "regulator"
EXTRACTION_TASK_TRIAGE = "triage"
EXTRACTION_TASK_PLAN_DOCUMENTS = "plan_documents"
EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS = "procedure_and_diagnosis"
EXTRACTION_TASK_DENIAL_TYPE = "denial_type"

EXTRACTION_TASK_RUN = "run"

# The words for the steps a patient should see.
EXTRACTION_TASK_LABELS: dict[str, str] = {
    EXTRACTION_TASK_FAX: "Fax number to send the appeal to",
    EXTRACTION_TASK_INSURANCE_COMPANY: "Insurance company",
    EXTRACTION_TASK_PLAN_ID: "Plan ID",
    EXTRACTION_TASK_CLAIM_ID: "Claim ID",
    EXTRACTION_TASK_DATE_OF_SERVICE: "Date of service",
    EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS: "Procedure and diagnosis",
    EXTRACTION_TASK_DENIAL_TYPE: "Reason they gave for the denial",
}

# One sentence per run-level outcome. None of them says the extraction
# completed unless something was actually read.
EXTRACTION_RUN_LABELS: dict[str, str] = {
    EXTRACTION_RUN_FINISHED: (
        "We read your letter and filled in what we found. " "Check it on the next page."
    ),
    EXTRACTION_RUN_ALREADY_HAVE_DETAILS: (
        "The details for this case are already here, so we did not read the "
        "letter again."
    ),
    EXTRACTION_RUN_KEPT_YOUR_DETAILS: (
        "We read your letter and did not change the details already on your "
        "case. Check them on the next page."
    ),
    EXTRACTION_RUN_READ_AND_FOUND_NOTHING: (
        "We read your letter and did not find the procedure or the diagnosis "
        "in it. You can have us try again, or type them in yourself."
    ),
    EXTRACTION_RUN_OUT_OF_ATTEMPTS: (
        "We have used up the tries we give one letter and still could not "
        "find the procedure or the diagnosis. Typing them in yourself is the "
        "way forward from here."
    ),
    EXTRACTION_RUN_FAILED: (
        "We could not finish reading your letter. You can have us try again, "
        "or type the details in yourself."
    ),
}


# Outer deadline on one non-speculative question-generation run. It must
# stay above the model phase's own ceiling: firing first cancels the helper
# and throws away every model result that had already arrived. The loading
# page, not this timer, is what gives the person a way out sooner, and its
# Continue button at 20 seconds can start a second overlapping run --
# see claim_generated_questions.
QUESTION_GENERATION_DEADLINE_SECONDS = 130


def stored_questions_are_current(denial: "Denial") -> bool:
    """Whether the set on the row is a finished set for the row's inputs now.

    Stamped for the current procedure and diagnosis: finished, empty
    included, since a run that found nothing to ask stores []. Unstamped,
    from before the stamp existed: finished only when nonempty, since the
    code before the stamp wrote [] for a run that never finished. Anything
    else, a set stamped for inputs since corrected above all, is not a
    finished set for this row. The one rule for starting a run, reporting
    one that did not finish, and rendering Back.
    """
    if denial.generated_questions is None:
        return False
    stamp = denial.generated_questions_for
    if stamp is None:
        return bool(denial.generated_questions)
    return bool(stamp == questions_fingerprint(denial.procedure, denial.diagnosis))


def record_derived_medical_context(
    denial, medical_context: set[str], withdraw: bool = True
) -> bool:
    """Replace, never add to, the sentence the answers derive.

    ``medical_context`` is computed from the current answers on every
    generation. Merging it additively kept the previous sentence when the
    answers no longer produced one: untick "urgent", generate, and the
    prompt still said the claim was urgent. Empty now withdraws it. Returns
    whether qa_context changed.
    """
    before = denial.qa_context
    # A form whose boxes are all unticked derives "", which is not a
    # sentence; {""} must read as nothing derived.
    sentences = {text.strip() for text in medical_context if text and text.strip()}
    if sentences:
        merge_qa(
            denial,
            {"medical_context": " ".join(sorted(sentences))},
            source="appeal_gen_form",
        )
    elif withdraw:
        merge_qa(denial, {}, source="appeal_gen_form", withdraw=["medical_context"])
    return bool(denial.qa_context != before)


def reserve_state(denial) -> str:
    """The state a held-back reserve must have been written for to be served."""
    return (denial.your_state or "").strip()


def state_on_the_row_now():
    """The row's state as a subquery, for every statement that compares a
    reserve's stamp with the case's state.

    Read in the same statement as the write or the filter, so a correction
    landing between a separate read and this statement cannot slip a
    wrong-state row through. Matches the stamp's spelling, which
    reserve_state trims: empty for no state, no surrounding whitespace.
    """
    from django.db.models import OuterRef, Subquery, Value
    from django.db.models.functions import Coalesce, Trim

    return Subquery(
        Denial.objects.filter(denial_id=OuterRef("for_denial"))
        .annotate(now=Trim(Coalesce("your_state", Value(""))))
        .values("now")[:1]
    )


def served_reserve_for_another_state() -> Q:
    """Promoted reserve rows that argue under another state's law.

    A reserve keeps its stamp when it is promoted, so a case that has since
    named another state does not get it replayed or fed to synthesis unless
    the person chose it. A row from before the stamp existed is unknown, and
    unknown is treated as another state.

    The stamp is compared with the state on the row as the query runs, not
    with the copy the request loaded when it began: a correction landing
    mid-run moves the row, and a reserve stamped for the old state must not
    pass on a snapshot taken before it.
    """
    return Q(context_level__in=SPECULATIVE_CONTEXT_LEVELS, chosen=False) & (
        ~Q(built_for_state=state_on_the_row_now()) | Q(built_for_state__isnull=True)
    )


class DenialCreatorHelper:
    regex_denial_processor = ProcessDenialRegex()
    zip_engine = uszipcode.search.SearchEngine()
    # Lazy load to avoid bootstrap problem w/new project
    _codes_denial_processor = None
    _regex_src = None
    _codes_src = None
    _all_denial_types = None

    @classmethod
    def codes_denial_processor(cls):
        if cls._codes_denial_processor is None:
            cls._codes_denial_processor = ProcessDenialCodes()
        return cls._codes_denial_processor

    @classmethod
    async def regex_src(cls):
        if cls._regex_src is None:
            cls._regex_src = await DataSource.objects.aget(name="regex")
        return cls._regex_src

    @classmethod
    def codes_src(cls):
        if cls._codes_src is None:
            cls._codes_src = DataSource.objects.get(name="codes")
        return cls._codes_src

    @classmethod
    def all_denial_types(cls):
        if cls._all_denial_types is None:
            cls._all_denial_types = DenialTypes.objects.all()
        return cls._all_denial_types

    @classmethod
    async def generate_appeal_questions(
        cls, denial_id: int
    ) -> Optional[List[Tuple[str, str]]]:
        """
        Generate a list of questions that could help craft a better appeal for
        this specific denial. The questions will be stored in the denial object's
        generated_questions field as tuples of (question, answer).
        Also generates citations in a non-blocking manner.
        This is NOT SPECULATIVE.
        Args:
            denial_id: The ID of the denial to generate questions for

        Returns:
            A list of (question, answer) tuples to help with appeal creation,
            or None when generation did not finish. None and [] are different
            answers: [] means we asked and there was nothing to ask about,
            None means we never found out, and the page tells the person
            which of those happened instead of offering both the same copy.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        if not denial:
            logger.warning(f"Could not find denial with ID {denial_id}")
            return None

        try:
            # Use fire_and_forget_in_new_threadpool for citation generation to run in background
            # This is non-speculative because at this point the things we use to generate citations are "fixed"
            citation_task = MLCitationsHelper.generate_citations_for_denial(
                denial, speculative=False
            )
            await fire_and_forget_in_new_threadpool(citation_task)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to start async generate citations for denial {denial_id}: {e}"
            )

        try:
            # Generate appeal questions using the helper class
            questions = await asyncio.wait_for(
                MLAppealQuestionsHelper.generate_questions_for_denial(
                    denial, speculative=False
                ),
                timeout=QUESTION_GENERATION_DEADLINE_SECONDS,
            )

            if questions is None:
                return await cls._questions_already_on_the_row(denial_id)
            # Never a bare write: another run for this denial may have
            # rendered its own set to the person already. The helper claims
            # for itself too; a second claim for the same inputs is a read.
            questions = await claim_generated_questions(
                denial_id,
                questions,
                generated_for=questions_fingerprint(denial.procedure, denial.diagnosis),
                # Conservative on purpose: this claim does not know whether
                # the run that produced these used the history, and a
                # refusal can land between the helper's own claim and this
                # one. A case with a history is treated as though it did, so
                # a no here means nothing new is written; a set already
                # standing is still handed back.
                used_history=bool(denial.health_history),
            )
            if questions is None:
                return await cls._questions_already_on_the_row(denial_id)
            logger.debug(f"Generated {len(questions)} questions for denial {denial_id}")
            return questions
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to generate questions for denial {denial_id}: {e}"
            )
            return await cls._questions_already_on_the_row(denial_id)

    @classmethod
    async def _questions_already_on_the_row(
        cls, denial_id: int
    ) -> Optional[List[Tuple[str, str]]]:
        """Questions this denial already has, after a run came back with none.

        Two things can still be on the row: questions a previous run stored,
        and the speculative candidate set. Candidates are promoted only
        under the helper's own rule -- the procedure and diagnosis they were
        generated for still match the row -- so a candidate written for a
        different service is never shown.

        Returns None when there is nothing, i.e. generation really did not
        finish.
        """
        try:
            denial = await Denial.objects.filter(denial_id=denial_id).aget()
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Could not re-read denial {denial_id} after failed question "
                f"generation: {e}"
            )
            return None
        # The inverse of the rule that starts a run, so a set another run
        # finished for these inputs, empty included, counts as finished.
        if stored_questions_are_current(denial):
            return cast(List[Tuple[str, str]], denial.generated_questions)
        if (
            denial.candidate_generated_questions
            and denial.candidate_procedure == denial.procedure
            and denial.candidate_diagnosis == denial.diagnosis
        ):
            questions = await claim_generated_questions(
                denial_id,
                cast(List[Tuple[str, str]], denial.candidate_generated_questions),
                generated_for=questions_fingerprint(
                    denial.candidate_procedure, denial.candidate_diagnosis
                ),
                # These came off the speculative pass, which may well have
                # read the history, and this instance can be holding a copy
                # a refusal has since cleared from the row.
                used_history=bool(denial.health_history),
            )
            if questions is None:
                return None
            logger.info(
                f"Question generation for denial {denial_id} did not finish; "
                f"promoted {len(questions)} candidate question(s) instead"
            )
            return questions
        return None

    @staticmethod
    def _invalidate_denial_text_artifacts(denial: Denial) -> None:
        """Drop everything derived from a denial letter that has been replaced.

        Called when an update changes ``denial_text``. Two classes of artifact
        are purely derived from the letter and become wrong -- not merely stale
        -- once it changes:

        * the HELD-BACK speculative reserve (``speculative=True``), which would
          otherwise be served later as a fallback appeal written about the old
          denial. Promoted rows (``speculative=False``) are deliberately kept:
          those were already delivered to the user and may have been chosen, so
          deleting them would destroy user-visible history. (Ordinary drafts
          going stale on a text change is pre-existing behavior, unchanged.)
        * both cached summaries, which are substituted into the prompt in place
          of the raw text for oversized denials -- a summary of the old letter
          would silently misdescribe the claim.
        * the two candidate mirrors of the extracted procedure and diagnosis,
          plus ``extract_procedure_diagnosis_finished`` (a statement about a
          letter that no longer exists) and ``extract_attempts`` (a per-letter
          failure budget, not a per-case one).

        The live ``procedure`` and ``diagnosis`` columns are deliberately NOT
        cleared: those may be what the person typed, and a new letter is not a
        reason to throw their answers away.

        Best-effort: a failure here must not break denial creation/update, so
        the caller wraps this. The in-memory instance is cleared too, since it
        flows on through the rest of the request.
        """
        deleted, _ = ProposedAppeal.objects.filter(
            for_denial=denial, speculative=True
        ).delete()
        # The triage was computed from the OLD letter; every column of it goes
        # back to null so nothing downstream can read letter A's deadline
        # against letter B.
        cleared = denial_triage.cleared_values()
        extraction_cleared: dict[str, Any] = {
            "candidate_procedure": None,
            "candidate_diagnosis": None,
            "extract_procedure_diagnosis_finished": False,
            "extract_attempts": 0,
        }
        Denial.objects.filter(denial_id=denial.denial_id).update(
            denial_text_summary=None,
            candidate_denial_text_summary=None,
            **cleared,
            **extraction_cleared,
        )
        denial.denial_text_summary = None
        denial.candidate_denial_text_summary = None
        for column, value in cleared.items():
            setattr(denial, column, value)
        for column, value in extraction_cleared.items():
            setattr(denial, column, value)
        logger.info(
            f"Denial {denial.denial_id} text replaced; invalidated "
            f"{deleted} held-back speculative appeal(s), both cached "
            f"denial-text summaries, the triage columns and the candidate "
            f"procedure/diagnosis mirrors"
        )

    @classmethod
    def create_or_update_denial(
        cls,
        email,
        denial_text,
        zip,
        health_history=None,
        pii=False,
        tos=False,
        privacy=False,
        use_external_models=True,
        store_raw_email=False,
        plan_documents=None,
        patient_id=None,
        insurance_company: Optional[str] = None,
        insurance_company_obj: Optional["InsuranceCompany"] = None,
        insurance_plan_obj: Optional["InsurancePlan"] = None,
        denial: Optional[Denial] = None,
        creating_professional: Optional[ProfessionalUser] = None,
        primary_professional: Optional[ProfessionalUser] = None,
        patient_user: Optional[PatientUser] = None,
        patient_visible: bool = False,
        subscribe: bool = False,  # Note: we don't handle this, but it's in the form so passed through.
        microsite_slug: Optional[str] = None,
        referral_source: Optional[str] = None,
        referral_source_details: Optional[str] = None,
        tracking_info: Optional[TrackingInfo] = None,
    ):
        """
        Create or update an existing denial.

        Args:
            email: The email address associated with the denial.
            denial_text: The text of the denial.
            zip: The ZIP code associated with the denial.
            health_history: Optional health history information.
            pii: Whether personally identifiable information is included.
            tos: Whether terms of service have been accepted.
            privacy: Whether privacy policy has been accepted.
            use_external_models: Whether to use external models.
            store_raw_email: Whether to store the raw email address.
            plan_documents: Optional plan documents.
            patient_id: Optional patient ID.
            insurance_company: Optional insurance company name.
            insurance_company_obj: Optional InsuranceCompany model instance.
            insurance_plan_obj: Optional InsurancePlan model instance.
            denial: Optional existing Denial object to update.
            creating_professional: Optional ProfessionalUser creating the denial.
            primary_professional: Optional ProfessionalUser as primary.
            patient_user: Optional PatientUser associated with the denial.
            patient_visible: Whether the denial is visible to the patient.
            subscribe: Whether the user has subscribed (not handled in this function).
            microsite_slug: Optional slug identifier for the microsite from which the denial was created.
                           Should be a valid microsite slug or None.
            referral_source: Optional referral source (e.g., "Search Engine", "Friend or Family").
            referral_source_details: Optional free-text details about the referral source.
            tracking_info: Optional TrackingInfo with user_agent, ASN, and IP (for professionals).

        Returns:
            The created or updated Denial object.
        """
        hashed_email = Denial.get_hashed_email(email)
        # If they ask us to store their raw e-mail we do
        possible_email = None
        validate_email(email)
        if store_raw_email:
            possible_email = email
        if not isinstance(primary_professional, ProfessionalUser):
            primary_professional = None
        if not isinstance(creating_professional, ProfessionalUser):
            creating_professional = None
        # For the pro flow we default to pro to finish
        professional_to_finish = creating_professional is not None
        # Build tracking kwargs
        tracking_kwargs = tracking_info.to_model_kwargs() if tracking_info else {}

        # If we don't have a denial we're making a new one
        is_new_denial = denial is None
        denial_text_changed = False
        if denial is None:
            try:
                denial = Denial.objects.create(
                    denial_text=denial_text,
                    hashed_email=hashed_email,
                    use_external=use_external_models,
                    raw_email=possible_email,
                    health_history=health_history,
                    creating_professional=creating_professional,
                    primary_professional=primary_professional,
                    patient_user=patient_user,
                    insurance_company=insurance_company,
                    insurance_company_obj=insurance_company_obj,
                    insurance_plan_obj=insurance_plan_obj,
                    patient_visible=patient_visible,
                    professional_to_finish=professional_to_finish,
                    microsite_slug=microsite_slug,
                    referral_source=referral_source,
                    referral_source_details=referral_source_details,
                    **tracking_kwargs,
                )
            except Exception as e:
                # This is a temporary hack to drop non-ASCII characters
                denial_text = (
                    denial_text.encode("ascii", errors="ignore")
                    .decode(errors="ignore")
                    .replace("\x00", "")
                )
                denial = Denial.objects.create(
                    denial_text=denial_text,
                    hashed_email=hashed_email,
                    use_external=use_external_models,
                    raw_email=possible_email,
                    health_history=health_history,
                    creating_professional=creating_professional,
                    primary_professional=primary_professional,
                    patient_user=patient_user,
                    insurance_company=insurance_company,
                    insurance_company_obj=insurance_company_obj,
                    insurance_plan_obj=insurance_plan_obj,
                    patient_visible=patient_visible,
                    professional_to_finish=professional_to_finish,
                    microsite_slug=microsite_slug,
                    referral_source=referral_source,
                    referral_source_details=referral_source_details,
                    **tracking_kwargs,
                )
        else:
            # Captured before the overwrite: everything derived from the denial
            # letter (the speculative reserve + the cached summaries) is stale
            # if the letter itself changed, and must be invalidated below.
            denial_text_changed = denial.denial_text != denial_text
            # Scoped, so this save cannot revert a concurrent writer's column.
            resubmit_fields: set[str] = set()
            # Directly update denial object fields instead of using denial.update()
            denial.denial_text = denial_text
            denial.hashed_email = hashed_email
            denial.use_external = use_external_models
            resubmit_fields.update({"denial_text", "hashed_email", "use_external"})
            # Nudge opt-in = a retained raw_email; remember the old value so a
            # change can be pushed to an already-running intake journey below.
            contact_opt_in_before = bool((denial.raw_email or "").strip())
            denial.raw_email = possible_email
            resubmit_fields.add("raw_email")
            # Guarded like every other optional field here: the denial form
            # has no health_history field, so this path is ALWAYS called with
            # health_history=None -- unguarded, a user who went back to edit
            # their denial letter lost their previously-entered history.
            if health_history is not None:
                denial.health_history = health_history
                # Redundant with the _update_denial tail call on the happy
                # path, but not a no-op: that save shares one atomic() with
                # intake_outbox.record_intent and no request transaction wraps
                # either save (ATOMIC_REQUESTS is False on every database), so
                # a record_intent failure rolls that write back and leaves
                # this one committed.
                resubmit_fields.add("health_history")

            # Only update these fields if they're provided
            if creating_professional is not None:
                denial.creating_professional = creating_professional
                resubmit_fields.add("creating_professional")
            if primary_professional is not None:
                denial.primary_professional = primary_professional
                resubmit_fields.add("primary_professional")
            if patient_user is not None:
                denial.patient_user = patient_user
                resubmit_fields.add("patient_user")
            if insurance_company is not None:
                denial.insurance_company = insurance_company
                resubmit_fields.add("insurance_company")
            if insurance_company_obj is not None:
                denial.insurance_company_obj = insurance_company_obj
                resubmit_fields.add("insurance_company_obj")
            if insurance_plan_obj is not None:
                denial.insurance_plan_obj = insurance_plan_obj
                resubmit_fields.add("insurance_plan_obj")
            if patient_visible is not None:
                denial.patient_visible = patient_visible
                resubmit_fields.add("patient_visible")
            if microsite_slug is not None:
                denial.microsite_slug = microsite_slug
                resubmit_fields.add("microsite_slug")
            if referral_source is not None:
                denial.referral_source = referral_source
                resubmit_fields.add("referral_source")
            if referral_source_details is not None:
                denial.referral_source_details = referral_source_details
                resubmit_fields.add("referral_source_details")

            # Update tracking info if provided
            if tracking_info:
                tracking_info.update_model_fields(denial)
                resubmit_fields.update({"user_agent", "asn", "asn_name", "ip_address"})

            denial.save(update_fields=sorted(resubmit_fields | {"last_interaction"}))
            if contact_opt_in_before != bool((possible_email or "").strip()):
                # Best-effort, no outbox row: a lost signal fails SAFE because
                # the nudge activity independently gates on the RETAINED
                # raw_email before sending, so the journey's copy of the
                # flag can only ever make it skip, never send to someone who
                # opted out (external review).
                from fighthealthinsurance import intake_outbox

                intake_outbox.signal_contact_opt_in(denial)

        if possible_email is not None:
            schedule_follow_ups(possible_email, denial)
        if zip is not None and zip != "":
            # A value in denial.state came off the review form, but is NOT
            # proof the person typed it: that form prefills its state box from
            # this same zip lookup (views.py, PostInferedForm initial), so
            # clicking through without touching it posts the guess back. So a
            # stored state outranks the zip only while the zip is unchanged.
            # Only ZIP3 is retained, so "unchanged" can only mean the first
            # three digits; a correction inside the last two is invisible
            # here and leaves a stored state standing.
            previous_zip3 = (denial.service_zip or "").strip()
            zip_changed = bool(previous_zip3) and previous_zip3 != zip[:3]
            confirmed_state = (denial.state or "").strip()
            # What this request decided from. The write below is conditional
            # on the row still holding these, so a review correction landing
            # meanwhile is kept and this request's decision is dropped.
            decided_from = {"your_state": denial.your_state, "state": denial.state}
            # Every column named here is written from this request's copy of
            # the row, so a column is named only when this request changed
            # it: a review correction landing meanwhile keeps its value.
            changed_state_fields = ["service_zip"]
            if confirmed_state and not zip_changed:
                # Owner decision 2026-09-13: new cases only, no backfill
                # migration, so this pass is the only thing that ever brings
                # a pre-existing mismatched row back into step.
                if (denial.your_state or "").strip() != confirmed_state:
                    denial.your_state = confirmed_state
                    changed_state_fields.append("your_state")
            else:
                inferred_state = None
                try:
                    inferred_state = cls.zip_engine.by_zipcode(zip).state
                except Exception as e:
                    logger.debug(f"Zip code lookup failed for {zip}: {e}")
                if inferred_state:
                    denial.your_state = inferred_state
                    changed_state_fields.append("your_state")
                    if confirmed_state:
                        # Reached from the zip they just replaced, so it no
                        # longer confirms anything; left on the row, the next
                        # submission would copy it back over the inference.
                        denial.state = None
                        changed_state_fields.append("state")
                elif zip_changed:
                    # No replacement to offer. Recording the new ZIP3 anyway
                    # would make the next submit of this same zip read as
                    # unchanged and skip the lookup, so the correction could
                    # never be retried. Leave the row exactly as it was.
                    changed_state_fields = []
            if changed_state_fields:
                # Only the first three digits are kept, the Safe Harbor cut
                # (without the population check Safe Harbor also asks for).
                # UCREnrichmentHelper.resolve_geographic_area reads it.
                denial.service_zip = zip[:3]
                Denial.objects.filter(denial_id=denial.denial_id).update(
                    service_zip=denial.service_zip
                )
                state_columns = {
                    column: getattr(denial, column)
                    for column in changed_state_fields
                    if column != "service_zip"
                }
                if state_columns:
                    moved = not Denial.objects.filter(
                        denial_id=denial.denial_id, **decided_from
                    ).update(**state_columns)
                    if moved:
                        logger.info(
                            f"intake: the state on denial {denial.denial_id} moved "
                            f"while this request ran; its own decision is dropped"
                        )
                        denial.refresh_from_db(fields=["your_state", "state"])
        # Optionally:
        # Fire off some async requests to the model to extract info.
        # denial_id = denial.denial_id
        # For now we fire this off "later" on a dedicated page with javascript magic.
        r = re.compile(r"Group Name:\s*(.*?)(,|)\s*(INC|CO|LTD|LLC)\s+", re.IGNORECASE)
        g = r.search(denial_text)
        # TODO: Update based on plan document upload if present.
        employer_name = None
        if g is not None:
            employer_name = g.group(1)
            if len(employer_name) < 300:
                denial.employer_name = employer_name
                # Scoped like the resubmission save above: the entity extract
                # fills procedure and diagnosis on this same row, and a bare
                # save() would put this stale instance's copies back over it.
                # last_interaction is auto_now, so it only moves when listed.
                denial.save(update_fields=["employer_name", "last_interaction"])

        denial_id = denial.denial_id
        semi_sekret = denial.semi_sekret

        # The instant a new denial's text arrives, kick off a non-blocking,
        # no-deadline, internal-model-only precompute of bare candidate appeals
        # (+ denial summary) from the raw text. Held in reserve and served only
        # if the live generation later underdelivers or gathered no extra data.
        # Fires on CREATE, and again if an update REPLACES the denial letter --
        # in which case the artifacts derived from the old letter are dropped
        # first, or we would later substitute a summary of the old letter into
        # the prompt, or serve a reserve appeal written about a different
        # denial. A plain update (no text change) doesn't re-fire; the helper is
        # idempotent regardless. Never blocks or breaks denial creation.
        if is_new_denial or denial_text_changed:
            # Guarded separately from the dispatch below: if invalidation fails
            # partway (say the delete lands but the summary update doesn't), we
            # still want a fresh precompute kicked off rather than leaving the
            # denial with no reserve at all.
            if denial_text_changed:
                try:
                    cls._invalidate_denial_text_artifacts(denial)
                except Exception:
                    logger.opt(exception=True).warning(
                        "Failed to invalidate denial-text-derived artifacts for "
                        f"denial {denial_id}"
                    )
            try:
                from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
                    dispatch_speculative_appeals,
                )

                # force on a replaced letter: the idempotency guard also matches
                # PROMOTED reserve rows, which invalidation deliberately keeps,
                # so without this a denial that ever served one reserve appeal
                # could never rebuild a reserve for its new text.
                dispatch_speculative_appeals(
                    denial_id,
                    force=denial_text_changed,
                    trigger=(
                        "denial_text_replaced"
                        if denial_text_changed
                        else "denial_created"
                    ),
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "Failed to dispatch speculative appeals precompute for "
                    f"denial {denial_id}"
                )

        if health_history is None and plan_documents is None:
            # Nothing for the optional-step save to write. Its full-row save
            # would put this request's copy of every column back, including
            # a state the review page corrected while this request ran. The
            # one thing it does on every call besides the save, recording
            # the intake-started intent, still happens here.
            from django.db import transaction as _transaction

            from fighthealthinsurance import intake_outbox

            with _transaction.atomic():
                intent = intake_outbox.record_intent(
                    denial, intake_outbox.INTAKE_STARTED
                )
            if intent is not None:
                intake_outbox.deliver(intent)
            return cls.format_denial_response_info(denial)
        return cls._update_denial(
            denial=denial, health_history=health_history, plan_documents=plan_documents
        )

    @staticmethod
    def _extraction_record(task: str, outcome: str) -> dict:
        """One step's frame: what ran, how it came out, and the words for it.

        ``label`` is only present for the steps a patient should see. A step
        with no label is still sent, so the stream stays auditable, and the
        page renders nothing for it.
        """
        record: dict = {"type": "task", "task": task, "outcome": outcome}
        label = EXTRACTION_TASK_LABELS.get(task)
        if label is not None:
            record["label"] = label
        return record

    @staticmethod
    def _extraction_run_record(outcome: str) -> dict:
        """The one run-level frame. Exactly one of these is sent per run."""
        return {
            "type": "run",
            "task": EXTRACTION_TASK_RUN,
            "outcome": outcome,
            "label": EXTRACTION_RUN_LABELS[outcome],
        }

    @staticmethod
    def _outcome_for_result(result: Any) -> str:
        """Read a step's return value as an outcome.

        An extractor that knows its own outcome returns one of the outcome
        strings and is believed. The rest return the value they extracted, or
        ``None``, which is a truthful found/nothing-found signal only for the
        extractors that let their exceptions out.
        """
        if isinstance(result, str) and result in EXTRACTION_OUTCOMES:
            return result
        if result:
            return EXTRACTION_OUTCOME_FOUND
        return EXTRACTION_OUTCOME_NOTHING_FOUND

    @classmethod
    async def _run_extraction_step(cls, awaitable: Awaitable[Any], task: str) -> dict:
        """Await one step and turn it into exactly one frame."""
        try:
            result = await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.opt(exception=True).warning(f"Failed in task {task}: {e}")
            return cls._extraction_record(task, EXTRACTION_OUTCOME_FAILED)
        return cls._extraction_record(task, cls._outcome_for_result(result))

    @staticmethod
    def _names_disagree(named: Optional[str], theirs: Optional[str]) -> bool:
        """Does the insurer the person wrote disagree with a carrier's name?

        An empty box, or one naming the same carrier, is agreement.
        """
        named = (named or "").strip().lower()
        theirs = (theirs or "").strip().lower()
        if not named or not theirs:
            return False
        return not (named in theirs or theirs in named)

    @classmethod
    async def _row_names_another_carrier(cls, denial_id: int, matched) -> bool:
        """Does the insurer the person wrote in the box disagree with ``matched``?

        The free-text name is theirs to correct and is never overwritten, so
        a structured match that contradicts it must not be stored beside it.
        ``matched`` is a carrier, or a carrier id when only that is to hand.
        """
        named = (
            await Denial.objects.filter(denial_id=denial_id)
            .values_list("insurance_company", flat=True)
            .afirst()
        ) or ""
        if not named.strip():
            return False
        theirs = (
            matched.name
            if hasattr(matched, "name")
            else await InsuranceCompany.objects.filter(id=matched)
            .values_list("name", flat=True)
            .afirst()
        )
        return cls._names_disagree(named, theirs)

    @classmethod
    async def _store_plan_if_it_is_the_rows_carriers(cls, denial_id: int, plan) -> bool:
        """Fill the empty plan column, only with a plan of the row's carrier.

        A row with no structured carrier yet takes any plan; a row that holds
        one takes only that carrier's plans.
        """
        row = dict(
            await Denial.objects.filter(denial_id=denial_id)
            .values("insurance_company_obj_id", "insurance_company")
            .afirst()
            or {}
        )
        carrier_id = row.get("insurance_company_obj_id")
        if carrier_id is not None and carrier_id != plan.insurance_company_id:
            logger.debug(
                f"Plan {plan.id} belongs to another carrier than the row holds; "
                f"not storing it"
            )
            return False
        if carrier_id is None and await cls._row_names_another_carrier(
            denial_id, plan.insurance_company_id
        ):
            # No structured carrier yet, but the person named one in the box
            # and it is not this plan's.
            return False
        return await cls._write_plan_if_the_carrier_still_holds(
            denial_id, plan, carrier_id, row.get("insurance_company")
        )

    @staticmethod
    async def _write_plan_if_the_carrier_still_holds(
        denial_id: int, plan, carrier_id, typed_insurer
    ) -> bool:
        """Fill the empty plan column only if the row still holds the carrier
        columns the plan was checked against; a correction landing between
        the check and the write is kept."""
        return bool(
            await Denial.objects.filter(
                denial_id=denial_id,
                insurance_plan_obj__isnull=True,
                insurance_company_obj_id=carrier_id,
                insurance_company=typed_insurer,
            ).aupdate(insurance_plan_obj=plan)
        )

    @classmethod
    async def _fax_for_the_carrier_on_the_row(cls, denial_id: int) -> Optional[str]:
        """The fax the row's carrier justifies; see _fax_and_its_justification."""
        fax, _ = await cls._fax_and_its_justification(denial_id)
        return fax

    @staticmethod
    async def _write_fax_if_the_carrier_still_holds(
        denial_id: int, fax: str, justification: dict
    ) -> bool:
        """Write ``fax`` only if the row still holds what justified it.

        The read that chose the number and the write that stores it are
        separate statements, so a carrier correction landing between them
        would otherwise get the old carrier's number written under it. The
        write carries the carrier columns as they were read.
        """
        return bool(
            await Denial.objects.filter(denial_id=denial_id, **justification)
            .filter(Q(appeal_fax_number__isnull=True) | Q(appeal_fax_number=""))
            .aupdate(appeal_fax_number=fax)
        )

    @staticmethod
    async def _fax_and_its_justification(
        denial_id: int,
    ) -> tuple[Optional[str], dict]:
        """The appeal fax of whichever carrier the row actually holds.

        Read after the carrier columns are written rather than before, so a
        name the person corrected decides the fax as well. Returns None when
        the row's own carrier has no fax on file, which leaves the column
        empty for the person to fill rather than filling it with somebody
        else's number.

        Values rather than model instances: these rows carry RegexFields that
        compile on load and raise on a stored empty pattern, and none of that
        is needed to read a phone number.
        """
        row = (
            await Denial.objects.filter(denial_id=denial_id)
            .values(
                "insurance_company_obj__appeal_fax_number",
                "insurance_plan_obj__appeal_fax_number",
                "insurance_plan_obj__insurance_company_id",
                "insurance_plan_obj_id",
                "insurance_company_obj__id",
                "insurance_company_obj__name",
                "insurance_plan_obj__insurance_company__name",
                "insurance_company",
            )
            .afirst()
        )
        if not row:
            return None, {}
        company_id = row["insurance_company_obj__id"]
        justification = {
            "insurance_company_obj_id": company_id,
            "insurance_plan_obj_id": row["insurance_plan_obj_id"],
            "insurance_company": row["insurance_company"],
        }
        if DenialCreatorHelper._names_disagree(
            row["insurance_company"], row["insurance_company_obj__name"]
        ) or (
            company_id is None
            and DenialCreatorHelper._names_disagree(
                row["insurance_company"],
                row["insurance_plan_obj__insurance_company__name"],
            )
        ):
            # The box names one insurer and the structured column another
            # (a text correction made after the match). Neither number is
            # trusted; empty asks the person for one.
            return None, justification
        plan_fax = row["insurance_plan_obj__appeal_fax_number"]
        plan_company_id = row["insurance_plan_obj__insurance_company_id"]
        # A plan belonging to a different carrier than the row names is the
        # same mismatch one level down, so it has to agree too.
        if plan_fax and (company_id is None or plan_company_id == company_id):
            return str(plan_fax), justification
        company_fax = row["insurance_company_obj__appeal_fax_number"]
        return (str(company_fax) if company_fax else None), justification

    @staticmethod
    async def _fill_if_empty(
        denial_id: int, field: str, value: Any, *, blank_is_empty: bool = True
    ) -> bool:
        """Write ``value`` into ``field`` only where the row still holds nothing.

        Every column these extractors write is editable on the review page, and
        the retry lifts the gate that normally stops a second read, so an
        unconditional write replaces a correction the person made between the
        two runs. Read and write are one statement because the extractors run
        concurrently with each other and with the review POST.
        """
        empty = Q(**{f"{field}__isnull": True})
        if blank_is_empty:
            empty = empty | Q(**{field: ""})
        return bool(
            await Denial.objects.filter(denial_id=denial_id)
            .filter(empty)
            .aupdate(**{field: value})
        )

    @classmethod
    async def clear_extraction_for_retry(cls, denial_id: int) -> None:
        """Spend one of the letter's attempts and make it readable again.

        Returning to the extraction URL re-runs nothing: the already-done gate
        in ``extract_entity`` is an OR over ``diagnosis``,
        ``extract_procedure_diagnosis_finished`` and ``procedure``, so a run
        that read the letter and found nothing can never be asked to look
        again. This clears the finished flag and the two candidate mirrors,
        which are ours, and leaves ``procedure`` and ``diagnosis`` alone,
        which may be what the person typed.

        ``extract_attempts`` is bumped rather than reset, in the same UPDATE so
        the increment is race-safe. It has to be bumped here: the counter is
        otherwise only moved by ``extract_set_denial_and_diagnosis``'s except
        path, so on a letter the model reads cleanly it never moves, and the
        button would be an unbounded invitation to re-run eleven steps and the
        PubMed/ClinicalTrials/speculative-context fan-out behind them. The
        caller checks the cap BEFORE calling this.
        """
        await Denial.objects.filter(denial_id=denial_id).aupdate(
            extract_procedure_diagnosis_finished=False,
            candidate_procedure=None,
            candidate_diagnosis=None,
            extract_attempts=F("extract_attempts") + 1,
        )
        logger.debug(
            f"extract_entity({denial_id}): cleared for an authorized retry, "
            "one attempt spent"
        )

    @classmethod
    async def extract_entity(
        cls, denial_id: int, retry: bool = False
    ) -> AsyncIterator[dict]:
        """
        Perform entity extraction on a given denial id.

        Yields one JSON-serialisable record per finished step and exactly one
        run-level record per run. The consumer is a ``json.dumps`` and a send;
        it makes no decisions about what any of this means.

        ``retry=True`` clears what the gate below reads before the gate reads
        it, so the letter is actually read again, and spends one of the
        letter's attempts so the button cannot be pressed forever.
        """

        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        # Read the budget before anything clears it: a retry already over the
        # cap must reach the out-of-attempts branch below.
        attempts = denial.extract_attempts or 0
        out_of_attempts = attempts >= 3
        if retry and not out_of_attempts:
            await cls.clear_extraction_for_retry(denial_id)

        if not retry and (
            denial.diagnosis
            or denial.extract_procedure_diagnosis_finished
            or denial.procedure
        ):
            logger.debug(f"extract_entity({denial_id}): skipping, already done")
            # Regulator matching is cheap (a handful of regexes), idempotent,
            # and independent of the procedure/diagnosis extraction this gate
            # protects: run it even when the denial was manually populated or
            # extraction already finished, so those denials still get
            # regulator contact info.
            yield await cls._run_extraction_step(
                cls.extract_set_regulator(denial_id), EXTRACTION_TASK_REGULATOR
            )
            # Triage is idempotent on the text hash, so retrying it here is
            # free when it already ran, and it is the only retry a timed-out
            # first attempt gets (a reconnect lands on this branch).
            yield await cls._run_extraction_step(
                cls.extract_set_triage(denial_id), EXTRACTION_TASK_TRIAGE
            )
            # Two different rows reach this branch and must not be told the
            # same thing. A row that only has extract_procedure_diagnosis_
            # finished set got that flag from a model call that returned
            # (None, None), which is the opposite news.
            if denial.procedure or denial.diagnosis:
                yield cls._extraction_run_record(EXTRACTION_RUN_ALREADY_HAVE_DETAILS)
            else:
                yield cls._extraction_run_record(EXTRACTION_RUN_READ_AND_FOUND_NOTHING)
            return
        # Bound persistent extraction failures: extract_entity runs once per
        # WebSocket connection (websockets.StreamingEntityBackend.receive)
        # with no upstream rate-limit, so without a cap a denial whose LLM
        # extraction reliably fails would re-run extraction (and re-fire
        # the PubMed/citation cache warmers) on every reconnect. The counter
        # is bumped by extract_set_denial_and_diagnosis's except path and by
        # an authorized retry, both via F() so the increments are race-safe.
        if out_of_attempts:
            logger.warning(
                f"extract_entity({denial_id}): skipping LLM extraction, "
                f"extract_attempts={attempts} exhausted"
            )
            # The counter measures the procedure/diagnosis extraction only;
            # regulator matching is a handful of regexes with no LLM in the
            # loop, so run it anyway, as in the already-done exit above.
            yield await cls._run_extraction_step(
                cls.extract_set_regulator(denial_id), EXTRACTION_TASK_REGULATOR
            )
            # Same for triage: no LLM of ours in the loop, idempotent on the
            # text hash, and this branch is the only retry it would get.
            yield await cls._run_extraction_step(
                cls.extract_set_triage(denial_id), EXTRACTION_TASK_TRIAGE
            )
            yield cls._extraction_run_record(EXTRACTION_RUN_OUT_OF_ATTEMPTS)
            return

        # Best effort extractions
        optional_awaitables: list[Coroutine[Any, Any, dict]] = [
            cls._run_extraction_step(
                cls.extract_set_fax_number(denial_id), EXTRACTION_TASK_FAX
            ),
            cls._run_extraction_step(
                cls.extract_set_insurance_company(denial_id),
                EXTRACTION_TASK_INSURANCE_COMPANY,
            ),
            cls._run_extraction_step(
                cls.match_insurance_plan_from_regex(denial_id),
                EXTRACTION_TASK_INSURANCE_PLAN,
            ),
            cls._run_extraction_step(
                cls.extract_set_plan_id(denial_id), EXTRACTION_TASK_PLAN_ID
            ),
            cls._run_extraction_step(
                cls.extract_set_claim_id(denial_id), EXTRACTION_TASK_CLAIM_ID
            ),
            cls._run_extraction_step(
                cls.extract_set_date_of_service(denial_id),
                EXTRACTION_TASK_DATE_OF_SERVICE,
            ),
            cls._run_extraction_step(
                cls.extract_set_regulator(denial_id), EXTRACTION_TASK_REGULATOR
            ),
            cls._run_extraction_step(
                cls.extract_set_triage(denial_id), EXTRACTION_TASK_TRIAGE
            ),
            cls._run_extraction_step(
                MLPlanDocHelper.generate_plan_documents_summary(denial_id),
                EXTRACTION_TASK_PLAN_DOCUMENTS,
            ),
        ]

        required_awaitables: list[Coroutine[Any, Any, dict]] = [
            # Denial type depends on denial and diagnosis
            cls._run_extraction_step(
                cls.extract_set_denial_and_diagnosis(denial_id, attempt_spent=retry),
                EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS,
            ),
            cls._run_extraction_step(
                cls.extract_set_denialtype(denial_id), EXTRACTION_TASK_DENIAL_TYPE
            ),
        ]

        expected_tasks = [
            EXTRACTION_TASK_FAX,
            EXTRACTION_TASK_INSURANCE_COMPANY,
            EXTRACTION_TASK_INSURANCE_PLAN,
            EXTRACTION_TASK_PLAN_ID,
            EXTRACTION_TASK_CLAIM_ID,
            EXTRACTION_TASK_DATE_OF_SERVICE,
            EXTRACTION_TASK_REGULATOR,
            EXTRACTION_TASK_TRIAGE,
            EXTRACTION_TASK_PLAN_DOCUMENTS,
            EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS,
            EXTRACTION_TASK_DENIAL_TYPE,
        ]

        logger.debug(
            f"extract_entity({denial_id}): {len(optional_awaitables)} optional + "
            f"{len(required_awaitables)} required tasks"
        )
        reported: dict[str, str] = {}
        try:
            async for item in execute_critical_optional_fireandforget(
                optional=optional_awaitables,
                required=required_awaitables,
                fire_and_forget=[cls._maybe_dispatch_ucr(denial_id)],
                # The run-level frame is decided below, from what the steps
                # reported, not from the loop reaching its end.
                done_record=None,
                timeout=90,
                # The optional tasks (fax number, insurer, plan/claim id, date
                # of service) are LLM roundtrips just like the required ones;
                # the default 2s grace after the required set finishes
                # cancelled them mid-call almost every run, which is why the
                # review page kept coming up blank on exactly the fields the
                # spinner said were being extracted. The user is still on the
                # extraction page with a progress list -- give the extras a
                # real window (the overall 90s cap above still bounds it).
                max_extra_time_for_optional=45,
            ):
                if not item:
                    continue
                reported[item["task"]] = item["outcome"]
                yield item
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error during extraction for denial {denial_id}: {e}"
            )
            yield cls._extraction_run_record(EXTRACTION_RUN_FAILED)
            return

        # A step that never reported was cancelled by the timeout or the
        # optional-task grace window.
        for task in expected_tasks:
            if task not in reported:
                yield cls._extraction_record(task, EXTRACTION_OUTCOME_TIMED_OUT)
                reported[task] = EXTRACTION_OUTCOME_TIMED_OUT

        main_outcome = reported.get(EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS)
        if main_outcome == EXTRACTION_OUTCOME_FOUND:
            yield cls._extraction_run_record(EXTRACTION_RUN_FINISHED)
        elif main_outcome == EXTRACTION_OUTCOME_KEPT_EXISTING:
            yield cls._extraction_run_record(EXTRACTION_RUN_KEPT_YOUR_DETAILS)
        elif main_outcome == EXTRACTION_OUTCOME_NOTHING_FOUND:
            yield cls._extraction_run_record(EXTRACTION_RUN_READ_AND_FOUND_NOTHING)
        else:
            # Failed, timed out, or never reported: we did not finish reading
            # the letter, which is not the same as finding nothing in it.
            yield cls._extraction_run_record(EXTRACTION_RUN_FAILED)

    @classmethod
    async def _maybe_dispatch_ucr(cls, denial_id: int) -> None:
        """Fire-and-forget UCR enrichment when the denial looks like an OON
        under-reimbursement.

        Heuristic gate first (regex on denial_text) so we don't waste rate
        lookups on denials with no UCR-relevant context. The dispatch itself
        prefers the Ray actor and falls back to a sync inline enrich if Ray
        isn't available — see ucr_helper.dispatch_ucr_refresh.
        """
        try:
            from fighthealthinsurance.ucr_helper import (
                dispatch_ucr_refresh,
                is_under_reimbursement_claim,
            )

            denial = await Denial.objects.filter(denial_id=denial_id).aget()
            if not is_under_reimbursement_claim(denial.denial_text):
                return
            await database_sync_to_async(dispatch_ucr_refresh)(denial.pk)
        except Exception:
            logger.opt(exception=True).warning(
                "UCR fire-and-forget dispatch failed for denial {}", denial_id
            )

    @classmethod
    async def build_speculative_context(cls, denial_id: int) -> None:
        """
        Build context based on the idea we extracted the correct info
        Intended for fire and forget usage.
        The results are stored on the denial object.
        """
        logger.debug("Building speculative context.")
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        citations_awaitable = MLCitationsHelper.generate_citations_for_denial(
            denial, speculative=True
        )
        questions_awaitable = MLAppealQuestionsHelper.generate_questions_for_denial(
            denial=denial, speculative=True
        )
        await asyncio.gather(citations_awaitable, questions_awaitable)
        return None

    @classmethod
    async def extract_set_denial_and_diagnosis(
        cls, denial_id: int, attempt_spent: bool = False
    ) -> str:
        """
        Asynchronously extracts procedure and diagnosis from a denial's text and updates the denial record.

        Attempts to extract the procedure and diagnosis fields using the appeal generator. Updates the denial with the extracted values and marks extraction as finished, regardless of success. If extraction is successful or existing values are present, triggers background tasks to search for related PubMed articles, prefetch ClinicalTrials.gov matches, and build speculative context. All background searches are fire-and-forget with their own timeouts and never block the caller.

        Returns an extraction outcome rather than ``None``: this method
        swallows every exception out of ``get_procedure_and_diagnosis``, which
        it has to because a model outage must not break denial creation, so
        the returned outcome is the only way a model failure reaches the page.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        procedure = None
        diagnosis = None

        try:
            procedure, diagnosis = await appealGenerator.get_procedure_and_diagnosis(
                denial_text=denial.denial_text
            )

            # Prepare update fields
            update_fields: dict[str, Any] = {
                "extract_procedure_diagnosis_finished": True
            }

            if procedure is not None:
                procedure = strip_boilerplate_service(procedure)
                if procedure is not None and len(procedure) < 300:
                    update_fields["procedure"] = procedure
                    update_fields["candidate_procedure"] = procedure

            if diagnosis is not None:
                diagnosis = strip_boilerplate_service(diagnosis)
                if diagnosis is not None and len(diagnosis) < 300:
                    update_fields["diagnosis"] = diagnosis
                    update_fields["candidate_diagnosis"] = diagnosis

            # The candidate_* mirrors and the finished flag are ours to write
            # unconditionally, but procedure/diagnosis themselves may have
            # been typed by the USER while this LLM call ran: the extraction
            # page says "you can skip ahead and enter this manually", and
            # find_next_steps saves those confirmed values. Last-writer-wins
            # here used to replace the user's confirmed service with the
            # model's guess and generate the whole appeal about the wrong
            # thing -- so the live fields are only filled where still empty.
            user_facing = {}
            for field in ("procedure", "diagnosis"):
                if field in update_fields:
                    user_facing[field] = update_fields.pop(field)
            await Denial.objects.filter(denial_id=denial_id).aupdate(**update_fields)
            filled_in = False
            kept_existing = False
            for field, value in user_facing.items():
                updated = await (
                    Denial.objects.filter(denial_id=denial_id)
                    .filter(Q(**{f"{field}__isnull": True}) | Q(**{field: ""}))
                    .aupdate(**{field: value})
                )
                if updated:
                    filled_in = True
                else:
                    kept_existing = True
                    logger.debug(
                        f"extract_set_denial_and_diagnosis({denial_id}): "
                        f"{field} already set (user or earlier run); keeping it"
                    )

            # Refresh in-memory denial so enrichment sees updated values.
            await denial.arefresh_from_db()

            # Use fire_and_forget_in_new_threadpool for background PubMed article search
            # now that we have diagnosis and procedure information.
            if denial.procedure or denial.diagnosis:

                async def find_pubmed_articles():
                    """
                    Asynchronously searches for PubMed articles related to a denial's diagnosis and procedure.

                    Attempts to find relevant articles using PubMedTools with a 120-second timeout. Logs a warning if the search times out, is cancelled, or encounters an error.
                    """
                    try:
                        pubmed_tool = PubMedTools()
                        # Find related articles based on diagnosis and procedure
                        # Adding proper timeout handling with asyncio.wait_for
                        await asyncio.wait_for(
                            pubmed_tool.find_pubmed_articles_for_denial(
                                denial, timeout=110.0
                            ),
                            timeout=120.0,  # Enforce same timeout at asyncio level
                        )

                    except asyncio.TimeoutError:
                        logger.warning(
                            f"PubMed article search timed out for denial {denial_id} after 120s"
                        )
                    except asyncio.exceptions.CancelledError:
                        logger.opt(exception=True).debug(
                            f"Cancelled PubMed article search for denial {denial_id}"
                        )
                    except Exception as e:
                        logger.opt(exception=True).warning(
                            f"Failed to find PubMed articles for denial {denial_id}: {e}"
                        )

                async def find_clinical_trials():
                    """
                    Prefetch ClinicalTrials.gov matches for this denial into the
                    DB cache, so the chat assistant (and any future appeal-side
                    consumer) gets an instant hit instead of a live API roundtrip.

                    Intentionally fire-and-forget: trial data is supplementary
                    evidence, and the appeal flow must never stall on it. Any
                    timeout, cancellation, or unexpected error is swallowed here
                    so it can't propagate out of the daemon thread.
                    """
                    try:
                        ct_tools = ClinicalTrialsTools()
                        # find_trials_for_denial enforces its own end-to-end
                        # budget; wait_for is a belt-and-suspenders cap in case
                        # something deeper hangs past the internal timeout.
                        await asyncio.wait_for(
                            ct_tools.find_trials_for_denial(denial, timeout=40.0),
                            timeout=50.0,
                        )
                    except asyncio.TimeoutError:
                        logger.debug(
                            f"ClinicalTrials search timed out for denial {denial_id}"
                        )
                    except asyncio.exceptions.CancelledError:
                        logger.debug(
                            f"Cancelled ClinicalTrials search for denial {denial_id}"
                        )
                    except Exception as e:
                        logger.opt(exception=True).debug(
                            f"ClinicalTrials prefetch failed for denial {denial_id}: "
                            f"{type(e).__name__}"
                        )

                # Fire and forget the PubMed search task
                await fire_and_forget_in_new_threadpool(find_pubmed_articles())
                # Fire and forget the ClinicalTrials.gov prefetch. Supplementary
                # evidence for "experimental/investigational" denials; non-blocking.
                await fire_and_forget_in_new_threadpool(find_clinical_trials())
                # Fire and forget the building the speculative context
                await fire_and_forget_in_new_threadpool(
                    cls.build_speculative_context(denial_id)
                )
                logger.debug(
                    f"Fired pubmed + clinical-trials search & speculative context "
                    f"for denial {denial_id}"
                )

            # Two questions, both needed. Did the MODEL produce anything? That
            # is the candidate mirrors, not what the row holds, which may be a
            # procedure the person typed while this call was in flight. And did
            # any of it get written down? "found" is the outcome the page turns
            # into "we filled in what we found", so it needs both.
            if update_fields.get("candidate_procedure") or update_fields.get(
                "candidate_diagnosis"
            ):
                if filled_in:
                    return EXTRACTION_OUTCOME_FOUND
                if kept_existing:
                    return EXTRACTION_OUTCOME_KEPT_EXISTING
            return EXTRACTION_OUTCOME_NOTHING_FOUND

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract procedure and diagnosis for denial {denial_id}: {e}"
            )
            # Leave extract_procedure_diagnosis_finished as False so a
            # subsequent extract_entity call can re-attempt extraction on
            # transient failures. Bump extract_attempts atomically (F()
            # makes concurrent-reconnect increments race-safe) so
            # extract_entity's gate stops retrying after 3 failures. An
            # authorized retry spent its attempt up front, in
            # clear_extraction_for_retry; a failed retry is one attempt,
            # not two.
            if not attempt_spent:
                try:
                    await Denial.objects.filter(denial_id=denial_id).aupdate(
                        extract_attempts=F("extract_attempts") + 1
                    )
                except Exception as inner:
                    logger.opt(exception=True).debug(
                        f"Failed to bump extract_attempts for denial {denial_id}: "
                        f"{inner}"
                    )
            return EXTRACTION_OUTCOME_FAILED

    @classmethod
    async def _match_insurance_company(
        cls, extracted_name: Optional[str], denial_text: str
    ) -> Optional["InsuranceCompany"]:
        """Find the best InsuranceCompany match for a denial.

        Tries in order:
        1. Exact (case-insensitive) match on the LLM-extracted name.
        2. Specificity-scored substring/alt_name match against the extracted name.
        3. Regex match against the full denial text using each company's
           ``regex`` pattern (with ``negative_regex`` exclusion).

        Step 3 is a fallback for when steps 1-2 don't produce a name match
        (LLM extraction missing, or none of the carriers' names/alt_names
        appeared in the extracted text). Running it lazily avoids paying the
        regex-search cost on every extraction.
        """
        from fighthealthinsurance.models import InsuranceCompany

        # No useful input: nothing to match against.
        if not extracted_name and not denial_text:
            return None

        # 1. Exact match on the LLM-extracted name
        if extracted_name:
            matched = await InsuranceCompany.objects.filter(
                name__iexact=extracted_name
            ).afirst()
            if matched:
                return matched

        # 2. Specificity-scored substring/alt_name match against extracted name.
        # Cache companies during the iteration so step 3 doesn't have to re-query.
        # Only fetch the columns we actually use to keep the working set small
        # even as the routing TextFields grow.
        matches: list[tuple[InsuranceCompany, float]] = []
        all_companies: list[InsuranceCompany] = []
        text_lower = extracted_name.lower() if extracted_name else ""

        # Restrict to the matching-relevant + propagation-relevant columns to
        # keep working-set size bounded as the routing TextFields grow. The
        # caller (extract_set_insurance_company) reads ``appeal_fax_number``
        # off the returned record for propagation; everything else (e.g.
        # appeal_address) is fetched separately on demand.
        company_qs = InsuranceCompany.objects.only(
            "id",
            "name",
            "alt_names",
            "regex",
            "negative_regex",
            "appeal_fax_number",
        )
        async for company in company_qs:
            all_companies.append(company)
            if not text_lower:
                continue
            company_lower = company.name.lower()

            if company_lower == text_lower:
                matches.append((company, 100.0))
            elif company_lower in text_lower:
                score = len(company_lower) / len(text_lower) * 90
                matches.append((company, score))
            elif text_lower in company_lower:
                score = len(text_lower) / len(company_lower) * 80
                matches.append((company, score))

            if company.alt_names:
                for alt in company.alt_names.split("\n"):
                    alt = alt.strip().lower()
                    if not alt:
                        continue
                    if alt == text_lower:
                        matches.append((company, 95.0))
                    elif alt in text_lower:
                        score = len(alt) / len(text_lower) * 85
                        matches.append((company, score))
                    elif text_lower in alt:
                        score = len(text_lower) / len(alt) * 75
                        matches.append((company, score))

        # 3. Regex fallback - only run when name/alt_name matching produced
        # no candidates. Score 60.0 keeps these below any name-based match.
        if not matches and denial_text:
            for company in all_companies:
                if not company.regex or not company.regex.pattern:
                    continue
                try:
                    if company.regex.search(denial_text):
                        if (
                            company.negative_regex
                            and company.negative_regex.pattern
                            and company.negative_regex.search(denial_text)
                        ):
                            continue
                        matches.append((company, 60.0))
                except Exception as e:
                    logger.opt(exception=True).debug(
                        f"Error applying regex for company {company.id}: {e}"
                    )

        if not matches:
            return None
        matches.sort(key=lambda x: x[1], reverse=True)
        best_company, best_score = matches[0]
        logger.debug(
            f"Matched '{extracted_name}' to '{best_company.name}' with score {best_score}"
        )
        return best_company

    @classmethod
    async def _match_insurance_plan(
        cls,
        company: "InsuranceCompany",
        denial_text: str,
        state: Optional[str],
    ) -> Optional["InsurancePlan"]:
        """Find the best InsurancePlan for a matched company.

        Prefers plans whose ``regex`` matches the denial text (most specific),
        then falls back to a state-only match if the denial has a state.
        """
        from fighthealthinsurance.models import InsurancePlan

        # select_related so callers can format ``str(plan)`` without
        # triggering an async-context sync DB hit through the related descriptor.
        plans = InsurancePlan.objects.filter(insurance_company=company).select_related(
            "insurance_company"
        )
        if denial_text:
            async for plan in plans:
                if not plan.regex or not plan.regex.pattern:
                    continue
                try:
                    if plan.regex.search(denial_text):
                        if (
                            plan.negative_regex
                            and plan.negative_regex.pattern
                            and plan.negative_regex.search(denial_text)
                        ):
                            continue
                        return plan
                except Exception as e:
                    logger.opt(exception=True).debug(
                        f"Error applying regex for plan {plan.id}: {e}"
                    )
        if state:
            return await plans.filter(state__iexact=state).afirst()
        return None

    @classmethod
    async def extract_set_insurance_company(cls, denial_id) -> str:
        """Extract insurance company name from denial text and match to structured models.

        Once a company is matched, propagates the company's known appeal-routing
        info (fax number) onto the denial when the denial doesn't already have
        one - this means downstream code (PDF cover sheet, fax send) can use
        Anthem/UHC/etc.'s published appeals fax even if the denial letter
        itself didn't include it.

        Returns an extraction outcome, for the reason given on
        ``extract_set_plan_id``.
        """
        from fighthealthinsurance.models import InsuranceCompany, InsurancePlan

        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        insurance_company = None
        reader_failed = False
        try:
            try:
                insurance_company = await appealGenerator.get_insurance_company(
                    denial_text=denial.denial_text
                )
            except ExtractionUnavailable:
                # The carrier regexes below need no model. Failed is the
                # answer only if they find nothing either.
                reader_failed = True

            # Reject obviously hallucinated names early - but still allow the
            # regex-based fallback below to run, since a missing/invalid LLM
            # extraction shouldn't block a known-carrier match.
            extracted_name: Optional[str] = None
            if insurance_company is not None:
                if (insurance_company in (denial.denial_text or "")) or len(
                    insurance_company
                ) < 50:
                    extracted_name = insurance_company
                else:
                    logger.debug(
                        f"Rejected insurance company extraction: {insurance_company}"
                    )

            matched_company: Optional[InsuranceCompany] = None
            matched_plan: Optional[InsurancePlan] = None

            try:
                matched_company = await cls._match_insurance_company(
                    extracted_name=extracted_name,
                    denial_text=denial.denial_text or "",
                )
                if matched_company:
                    matched_plan = await cls._match_insurance_plan(
                        company=matched_company,
                        denial_text=denial.denial_text or "",
                        state=denial.state,
                    )
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"Error matching structured insurance models: {e}"
                )

            # When we have a structured match, use its canonical name so
            # Denial.insurance_company stays in sync with insurance_company_obj.
            # Downstream prompt/cover-sheet code reads the text field, so any
            # divergence (e.g. LLM extracted "Anthem" but matched a regional
            # brand "Empire BlueCross BlueShield") would address the appeal
            # to the wrong carrier name. Fall back to the LLM extraction only
            # when no structured match was found.
            resolved_name: Optional[str] = extracted_name
            if matched_company:
                resolved_name = matched_company.name

            found_something = False
            wrote_something = False
            if resolved_name:
                found_something = True
                wrote_something |= await cls._fill_if_empty(
                    denial_id, "insurance_company", resolved_name
                )
            if matched_company:
                found_something = True
                if await cls._row_names_another_carrier(denial_id, matched_company):
                    # The person wrote a different insurer in the box. Their
                    # words outrank a match in the letter; the structured
                    # columns stay empty rather than hold the wrong carrier.
                    logger.debug(
                        f"Row names a different insurer than the matched "
                        f"{matched_company.name}; not storing the match"
                    )
                    matched_company = None
                    matched_plan = None
            if matched_company:
                logger.debug(f"Matched to structured company: {matched_company.name}")
                wrote_something |= await cls._fill_if_empty(
                    denial_id,
                    "insurance_company_obj",
                    matched_company,
                    blank_is_empty=False,
                )
            if matched_plan:
                found_something = True
                logger.debug(f"Matched to structured plan: {matched_plan}")
                wrote_something |= await cls._store_plan_if_it_is_the_rows_carriers(
                    denial_id, matched_plan
                )

            # The rule every carrier writer follows: a plan is stored only for
            # the carrier the row holds, and the fax is the published fax of
            # whatever carrier the row holds after the columns are written,
            # or empty. The writers are this function, extract_set_fax_number
            # and match_insurance_plan_from_regex. An empty fax number asks the
            # person for one. A wrong one sends the appeal, with everything in
            # it, to a company that has nothing to do with the claim.
            propagated_fax, justified_by = await cls._fax_and_its_justification(
                denial_id
            )
            if propagated_fax:
                rows_updated = await cls._write_fax_if_the_carrier_still_holds(
                    denial_id, propagated_fax, justified_by
                )
                if rows_updated:
                    logger.debug(
                        f"Propagated appeal_fax_number {propagated_fax} from the "
                        f"carrier on the row"
                    )

            if not found_something:
                if reader_failed:
                    return EXTRACTION_OUTCOME_FAILED
                return EXTRACTION_OUTCOME_NOTHING_FOUND
            if wrote_something:
                return EXTRACTION_OUTCOME_FOUND
            return EXTRACTION_OUTCOME_KEPT_EXISTING
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract insurance company for denial {denial_id}: {e}"
            )
            return EXTRACTION_OUTCOME_FAILED

    @classmethod
    async def extract_set_plan_id(cls, denial_id) -> str:
        """Extract plan ID from denial text.

        Returns an extraction outcome: this method swallows the model's
        exceptions, so a returned value cannot tell a read that failed from a
        letter with no plan ID in it, and the page says one or the other.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        try:
            plan_id = await appealGenerator.get_plan_id(denial_text=denial.denial_text)

            from fighthealthinsurance.generate_appeal import is_plausible_identifier

            if plan_id is not None and is_plausible_identifier(plan_id):
                if await cls._fill_if_empty(denial_id, "plan_id", plan_id):
                    return EXTRACTION_OUTCOME_FOUND
                return EXTRACTION_OUTCOME_KEPT_EXISTING
            logger.debug(f"Rejected plan ID extraction: {plan_id}")
            return EXTRACTION_OUTCOME_NOTHING_FOUND
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract plan ID for denial {denial_id}: {e}"
            )
            return EXTRACTION_OUTCOME_FAILED

    @classmethod
    async def match_insurance_plan_from_regex(cls, denial_id):
        """
        Match denial to a specific insurance plan using regex patterns.
        This helps identify state-specific plans like "Anthem Medicaid California" vs "Anthem Medicaid New York".
        """
        from fighthealthinsurance.models import InsurancePlan

        # Columns, not a joined instance: a select_related over the plan FK
        # runs RegexField.from_db_value on the NULL regex columns of a row
        # with no plan yet, which is every row this is meant to fill, and
        # that raises before the try below.
        row = (
            await Denial.objects.filter(denial_id=denial_id)
            .values("denial_text", "insurance_plan_obj_id")
            .afirst()
        )
        if row is None:
            return None

        try:
            # Only proceed if we don't already have a plan matched
            if row["insurance_plan_obj_id"]:
                logger.debug(f"Denial {denial_id} already has matched plan, skipping")
                return await InsurancePlan.objects.aget(id=row["insurance_plan_obj_id"])

            denial_text = row["denial_text"]

            # Try to match plans using regex patterns
            async for plan in InsurancePlan.objects.select_related(
                "insurance_company"
            ).all():
                if plan.regex and plan.regex.pattern:
                    try:
                        if plan.regex.search(denial_text):
                            # Check negative regex to avoid false positives
                            if plan.negative_regex and plan.negative_regex.pattern:
                                if plan.negative_regex.search(denial_text):
                                    continue

                            # We found a match!
                            logger.debug(f"Matched denial {denial_id} to plan: {plan}")

                            # Conditional rather than a read-then-write against
                            # the instance loaded above: the retry runs this
                            # concurrently with extract_set_insurance_company
                            # and after the person may have picked a plan on
                            # the review page.
                            if not await cls._store_plan_if_it_is_the_rows_carriers(
                                denial_id, plan
                            ):
                                # Another carrier's plan, or one is stored.
                                continue
                            await cls._fill_if_empty(
                                denial_id,
                                "insurance_company_obj",
                                plan.insurance_company,
                                blank_is_empty=False,
                            )
                            return plan
                    except Exception as e:
                        logger.opt(exception=True).debug(
                            f"Error matching plan {plan.id}: {e}"
                        )

            logger.debug(f"No matching insurance plan found for denial {denial_id}")

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to match insurance plan for denial {denial_id}: {e}"
            )

        return None

    @classmethod
    async def extract_set_claim_id(cls, denial_id) -> str:
        """Extract claim ID from denial text.

        Returns an extraction outcome, for the reason given on
        ``extract_set_plan_id``.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        try:
            claim_id = await appealGenerator.get_claim_id(
                denial_text=denial.denial_text
            )

            from fighthealthinsurance.generate_appeal import is_plausible_identifier

            if claim_id is not None and is_plausible_identifier(claim_id):
                if await cls._fill_if_empty(denial_id, "claim_id", claim_id):
                    return EXTRACTION_OUTCOME_FOUND
                return EXTRACTION_OUTCOME_KEPT_EXISTING
            logger.debug(f"Rejected claim ID extraction: {claim_id}")
            return EXTRACTION_OUTCOME_NOTHING_FOUND
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract claim ID for denial {denial_id}: {e}"
            )
            return EXTRACTION_OUTCOME_FAILED

    @classmethod
    async def extract_set_date_of_service(cls, denial_id) -> str:
        """Extract date of service from denial text.

        Returns an extraction outcome, for the reason given on
        ``extract_set_plan_id``.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        try:
            date_of_service = await appealGenerator.get_date_of_service(
                denial_text=denial.denial_text
            )

            if date_of_service is not None:
                if await cls._fill_if_empty(
                    denial_id, "date_of_service", date_of_service
                ):
                    return EXTRACTION_OUTCOME_FOUND
                return EXTRACTION_OUTCOME_KEPT_EXISTING
            logger.debug("No date of service found")
            return EXTRACTION_OUTCOME_NOTHING_FOUND
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract date of service for denial {denial_id}: {e}"
            )
            return EXTRACTION_OUTCOME_FAILED

    @classmethod
    async def get_plan_documents_text(cls, denial_id: int) -> str:
        """
        Extract text from all plan documents associated with a denial.

        Args:
            denial_id: The denial ID to get plan documents for

        Returns:
            Combined text from all plan documents (PDF and text files)
        """
        combined_text = ""
        try:
            plan_docs = PlanDocuments.objects.filter(denial_id=denial_id)
            async for doc in plan_docs:
                try:
                    # Try encrypted field first, fall back to unencrypted
                    file_field = doc.plan_document_enc or doc.plan_document
                    if not file_field:
                        continue

                    path = file_field.path
                    text = extract_file_text(path)
                    if text:
                        combined_text += text + "\n"
                except Exception as e:
                    logger.debug(f"Error processing plan document: {e}")
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error getting plan documents for denial {denial_id}: {e}"
            )
        return combined_text

    @classmethod
    async def extract_set_fax_number(cls, denial_id):
        """
        Extract fax number from denial text and plan documents.

        First tries the denial letter text, then searches plan documents if
        no fax number was found. Validates extracted fax numbers against
        source text to avoid hallucinations.

        If the denial already has an ``appeal_fax_number`` (e.g. user-entered
        or propagated from a matched carrier), it is left untouched - we only
        run hallucination-validation and the carrier fallback against
        newly-extracted values, never against a value already saved on the
        denial.
        """
        from fighthealthinsurance.models import InsuranceCompany, InsurancePlan

        denial = await Denial.objects.filter(denial_id=denial_id).aget()

        # If the denial already has a fax we trust it (user input,
        # propagation from a matched carrier, or a previously validated
        # extraction) and exit early.
        if denial.appeal_fax_number:
            return denial.appeal_fax_number

        # Text sources for validation
        denial_text = denial.denial_text or ""
        plan_docs_text = ""
        all_source_text = denial_text
        appeal_fax_number: Optional[str] = None
        # A reader that could not read is a failure, not a letter with no fax
        # in it; it only shows if nothing else supplies a number below.
        reader_failed = False

        # First try to extract from denial text
        try:
            appeal_fax_number = await appealGenerator.get_fax_number(
                denial_text=denial_text
            )
        except Exception as e:
            reader_failed = isinstance(e, ExtractionUnavailable)
            logger.opt(exception=True).warning(
                f"Failed to extract fax number from denial text for {denial_id}: {e}"
            )

        # If still not found, try plan documents
        if not appeal_fax_number:
            try:
                plan_docs_text = await cls.get_plan_documents_text(denial_id)
                if plan_docs_text:
                    all_source_text = denial_text + "\n" + plan_docs_text
                    appeal_fax_number = await appealGenerator.get_fax_number(
                        denial_text=plan_docs_text
                    )
                    if appeal_fax_number:
                        logger.debug(
                            f"Found fax number in plan documents for denial {denial_id}"
                        )
            except Exception as e:
                reader_failed = reader_failed or isinstance(e, ExtractionUnavailable)
                logger.opt(exception=True).warning(
                    f"Failed to extract fax number from plan docs for {denial_id}: {e}"
                )

        # Validate the extracted fax number against hallucinations
        if appeal_fax_number is not None:
            fax_digits = re.sub(r"\D", "", appeal_fax_number)

            if len(fax_digits) < 10 or len(fax_digits) > 15:
                logger.debug(
                    f"Rejected fax number {appeal_fax_number} - invalid length"
                )
                appeal_fax_number = None
            elif len(appeal_fax_number) > 30:
                logger.debug(f"Rejected fax number {appeal_fax_number} - too long")
                appeal_fax_number = None
            else:
                # Validate against all source text (denial + plan docs)
                all_source_digits = re.sub(r"\D", "", all_source_text)
                if fax_digits[-10:] not in all_source_digits:
                    logger.debug(
                        f"Rejected fax number {appeal_fax_number} - digits not found in source text"
                    )
                    appeal_fax_number = None
                else:
                    logger.debug(f"Validated fax number {appeal_fax_number}")

        # Final fallback: the published fax of the carrier the row holds. Same
        # rule as the carrier extractor (_fax_for_the_carrier_on_the_row), so
        # a plan belonging to another carrier can never supply it here either.
        if appeal_fax_number is None:
            already = (
                await Denial.objects.filter(denial_id=denial_id)
                .values_list("appeal_fax_number", flat=True)
                .afirst()
            )
            if already:
                return already
            fallback_fax, justified_by = await cls._fax_and_its_justification(denial_id)
            if fallback_fax and await cls._write_fax_if_the_carrier_still_holds(
                denial_id, fallback_fax, justified_by
            ):
                return fallback_fax
            # Nothing, or the carrier moved under the read: the letter's own
            # verdict stands.
            appeal_fax_number = None
        if appeal_fax_number is not None:
            # Conditional update: only write if no fax has been set since
            # we started (extract_set_insurance_company runs concurrently
            # and may have propagated a fax onto the denial in the meantime).
            rows_updated = (
                await Denial.objects.filter(denial_id=denial_id)
                .filter(Q(appeal_fax_number__isnull=True) | Q(appeal_fax_number=""))
                .aupdate(appeal_fax_number=appeal_fax_number)
            )
            if rows_updated:
                logger.debug(f"Successfully extracted fax number: {appeal_fax_number}")
                return appeal_fax_number
            # Another task wrote a fax in the meantime; return whatever's
            # currently stored so the caller sees a consistent value.
            return (
                await Denial.objects.filter(denial_id=denial_id)
                .values_list("appeal_fax_number", flat=True)
                .afirst()
            )
        return EXTRACTION_OUTCOME_FAILED if reader_failed else None

    @classmethod
    async def extract_set_triage(cls, denial_id) -> str:
        """Triage the denial with TypeSafe (ml/denial_triage.py) and store it.

        Optional and fire-and-forget like the other extractors: a missing
        key, a declined external-model consent, a timeout or a malformed
        answer all leave the denial untriaged, never un-created. Idempotent
        on the text hash, so a retry after the letter was triaged is free.

        Returns an extraction outcome. Every path that leaves the row
        untriaged says ``nothing_found``; the already-current path says
        ``cached``, being the one path where the work is genuinely done.
        """
        if not denial_triage.enabled():
            return EXTRACTION_OUTCOME_NOTHING_FOUND
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        if not denial.use_external:
            return EXTRACTION_OUTCOME_NOTHING_FOUND
        if denial_triage.is_current(denial):
            return EXTRACTION_OUTCOME_CACHED
        text = denial.denial_text
        result = await denial_triage.triage(text, denial.denial_date)
        if result is None:
            return EXTRACTION_OUTCOME_NOTHING_FOUND
        values = denial_triage.row_values(result, timezone.now(), text)
        # An anchored window is always resolved against the date on the row
        # at WRITE time, and the write is conditional on that date (and on
        # the letter and the consent) still being what we resolved against;
        # a date corrected mid-call fails the predicate and we go round once
        # more with the fresh date.
        for _attempt in range(2):
            latest_date = (
                await Denial.objects.filter(denial_id=denial_id)
                .values_list("denial_date", flat=True)
                .afirst()
            )
            if denial_triage.is_anchored_window(values["appeal_deadline_label"]):
                values["appeal_deadline"] = denial_triage.resolve_window(
                    values["appeal_deadline_label"], latest_date
                )
            updated = await Denial.objects.filter(
                denial_id=denial_id,
                denial_text=text,
                use_external=True,
                denial_date=latest_date,
            ).aupdate(**values)
            if updated:
                return EXTRACTION_OUTCOME_FOUND
        logger.info(
            f"denial triage for {denial_id} discarded: letter, consent or date "
            "changed while it was in flight"
        )
        return EXTRACTION_OUTCOME_NOTHING_FOUND

    @classmethod
    async def extract_set_regulator(cls, denial_id) -> str:
        """Match the denial text against known regulators and store the match.

        Populates ``Denial.regulator`` so downstream flows (outside help,
        the escalation packet's ERISA detection) can surface the right
        regulator along with its complaint phone number.

        Returns an extraction outcome: the row already had a regulator, we
        matched one, or the letter matched nothing.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        if denial.regulator_id is not None:
            return EXTRACTION_OUTCOME_CACHED
        regulators = await cls.regex_denial_processor.get_regulator(
            denial.denial_text or ""
        )
        if regulators:
            # First match wins; the seeded regexes are mutually specific.
            await Denial.objects.filter(
                denial_id=denial_id, regulator__isnull=True
            ).aupdate(regulator=regulators[0])
            return EXTRACTION_OUTCOME_FOUND
        return EXTRACTION_OUTCOME_NOTHING_FOUND

    @classmethod
    async def extract_set_denialtype(cls, denial_id) -> str:
        """Match the denial text against the known denial types and store them.

        Returns an extraction outcome: the page's words for this step are
        "Reason they gave for the denial", and a bare return reads as
        nothing-found even on the runs that stored types.

        ``get_or_create``, not ``create``: DenialTypesRelation carries no
        unique constraint, so a second read adds a second copy of every type.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        denial_types = await cls.regex_denial_processor.get_denialtype(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )
        logger.debug(
            f"extract_set_denialtype({denial_id}): processing {len(denial_types)} types"
        )
        src = await cls.regex_src()
        created = 0
        already_stored = 0
        failed = 0
        for dt in denial_types:
            try:
                _, was_created = await DenialTypesRelation.objects.aget_or_create(
                    denial=denial, denial_type=dt, src=src
                )
                if was_created:
                    created += 1
                else:
                    already_stored += 1
            except Exception as e:
                failed += 1
                logger.opt(exception=True).debug(f"Failed setting denial type: {e}")
        if created:
            return EXTRACTION_OUTCOME_FOUND
        if failed:
            return EXTRACTION_OUTCOME_FAILED
        if already_stored:
            return EXTRACTION_OUTCOME_CACHED
        return EXTRACTION_OUTCOME_NOTHING_FOUND

    @classmethod
    def update_denial(
        cls,
        email,
        denial_id,
        semi_sekret,
        health_history=None,
        plan_documents=None,
        include_provided_health_history_in_appeal=None,
        health_history_anonymized=None,
        health_history_consent=None,
        health_history_seen=None,
    ):
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.filter(
            hashed_email=hashed_email, denial_id=denial_id, semi_sekret=semi_sekret
        ).get()
        return cls._update_denial(
            denial,
            health_history=health_history,
            plan_documents=plan_documents,
            include_provided_health_history_in_appeal=include_provided_health_history_in_appeal,
            health_history_anonymized=health_history_anonymized,
            health_history_consent=health_history_consent,
            health_history_seen=health_history_seen,
        )

    @classmethod
    def _update_denial(
        cls,
        denial,
        health_history=None,
        plan_documents=None,
        include_provided_health_history_in_appeal=None,
        health_history_anonymized=None,
        health_history_consent=None,
        health_history_seen=None,
    ):
        from django.db import transaction as _transaction

        from fighthealthinsurance import intake_outbox

        # ONE transaction for the whole intake mutation: the plan documents,
        # the denial update, and the intake_started intent (dark until
        # TEMPORAL_INTAKE_JOURNEY_ENABLED) commit together or not at all. A
        # failed intent insert fails the mutation -- the correct contract; the
        # alternative is a silent gap in the durable journey (external
        # review). Delivery happens AFTER the commit, behind deliver()'s
        # exception boundary, so the journey can never break the user-facing
        # flow. Opt-in for the nudge = store_raw_email, observable as a
        # retained raw_email.
        # Scoped, so this save cannot revert a concurrent writer's column.
        changed_fields: set[str] = set()

        with _transaction.atomic():
            if plan_documents is not None:
                # Additive and not idempotent: a second click or a replayed
                # POST adds another row. PlanDocuments carries no filename or
                # content hash to dedupe on, so plan_documents.html only shows
                # a count to discourage it. 2026-09-14: left as is, a dedupe
                # key is a migration this branch is not making.
                for plan_document in plan_documents:
                    PlanDocuments.objects.create(
                        plan_document_enc=plan_document, denial=denial
                    )
            # The contract for all three optional columns below: None means
            # the caller said nothing and the stored value stands. For
            # health_history an empty string is a decision, not silence -- it
            # is how the page deletes what the person wrote, and no other page
            # can -- so the blank is written rather than refused.
            if health_history is not None and not cls._history_submit_is_stale(
                denial, health_history, health_history_seen, locked=True
            ):
                denial.health_history = health_history
                changed_fields.add("health_history")
            if include_provided_health_history_in_appeal is not None:
                denial.include_provided_health_history_in_appeal = (
                    include_provided_health_history_in_appeal
                )
                changed_fields.add("include_provided_health_history_in_appeal")
            if health_history_anonymized is not None:
                denial.health_history_anonymized = health_history_anonymized
                changed_fields.add("health_history_anonymized")
            if health_history_consent is not None:
                denial.health_history_consent = health_history_consent
                changed_fields.add("health_history_consent")
                if health_history_consent is False:
                    # Questions and citations produced while the history was
                    # allowed were chosen out of it, and both are reused
                    # ahead of the consent check on the next run. Without
                    # this, a refusal took the history out of the prompt and
                    # left material derived from it in, citations included,
                    # which go to an external provider when use_external is
                    # set. Dropping the caches makes the next run recompute
                    # them from the denial alone. Nothing anybody has been
                    # shown is removed: these are stored model inputs, not
                    # letters.
                    #
                    # Every refusal clears, not only the first, whether or
                    # not a history is stored now, and whether or not this
                    # copy of the row shows anything in the columns.
                    # Somebody can clear the box in one visit and untick it
                    # in the next, and a run still in flight can write a
                    # cache back between this instance being loaded and
                    # this save, which a "only if it holds something" test
                    # would then leave in place. The cost of clearing a
                    # cache that owes nothing to a history is that the next
                    # run recomputes it.
                    for cache_field in DERIVED_FROM_HEALTH_HISTORY:
                        setattr(denial, cache_field, None)
                        changed_fields.add(cache_field)
            denial.save(update_fields=sorted(changed_fields | {"last_interaction"}))
            intent = intake_outbox.record_intent(denial, intake_outbox.INTAKE_STARTED)
        if intent is not None:
            intake_outbox.deliver(intent)
        # Return the current the state
        return cls.format_denial_response_info(denial)

    @classmethod
    def _history_submit_is_stale(
        cls,
        denial,
        submitted: Optional[str],
        seen_digest: Optional[str],
        locked: bool = False,
    ) -> bool:
        """Is this an untouched page posting over an edit made after it loaded?

        The step is reachable from several places and a person can have it
        open in one tab while editing in another, or press Back onto a copy
        rendered before a removal. The box is posted on every Next whether or
        not it was touched, so an untouched stale page would write its own
        stale text back and quietly undo the newer edit, or restore history
        that had just been deleted.

        Only that case is refused. A box whose content differs from what the
        page was rendered with is treated as typing, and the last to type
        wins. That is a comparison of content, not of intent: it cannot tell
        someone who retyped the original wording from someone who never
        touched the box, and it cannot see what the browser put there.

        ``locked`` reads the stored history inside the caller's transaction
        with the row locked, so the comparison and the write that follows
        cannot straddle another request's save. The instance the caller
        loaded is not consulted for it.
        """
        if not seen_digest:
            # No digest means a caller that does not render the box at all
            # (the REST API, the professional flow). Nothing to be stale
            # against, so this rule has no opinion.
            return False
        from fighthealthinsurance.denial_context import health_history_digest

        if health_history_digest(submitted, denial.denial_id) != seen_digest:
            return False
        if locked:
            stored = (
                Denial.objects.select_for_update()
                .filter(pk=denial.pk)
                .values_list("health_history", flat=True)
                .first()
            )
        else:
            stored = denial.health_history
        stale: bool = health_history_digest(stored, denial.denial_id) != seen_digest
        if stale:
            logger.info(
                f"health history: refusing a stale unedited submit for denial "
                f"{denial.denial_id}; the stored history changed after this "
                f"page was rendered"
            )
        return stale

    @classmethod
    def format_denial_response_info(cls, denial):
        appeal_id = None
        if Appeal.objects.filter(for_denial=denial).exists():
            appeal_obj = Appeal.objects.filter(for_denial=denial).first()
            if appeal_obj is None:
                raise Exception(f"Could not find appeal for denial {denial.denial_id}")
            else:
                appeal_id = appeal_obj.id
        else:
            logger.debug(
                f"Could not find appeal for {denial} -- expected for consumer version"
            )
        r = DenialResponseInfo(
            selected_denial_type=denial.denial_type.all(),
            all_denial_types=cls.all_denial_types(),
            uuid=denial.uuid,
            denial_id=denial.denial_id,
            your_state=denial.your_state,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
            employer_name=denial.employer_name,
            semi_sekret=denial.semi_sekret,
            appeal_fax_number=denial.appeal_fax_number,
            appeal_id=appeal_id,
            claim_id=denial.claim_id,
            date_of_service=denial.date_of_service,
            insurance_company=denial.insurance_company,
            plan_id=denial.plan_id,
        )
        return r


async def _refresh_sync_denial_context(
    denial: Any,
    getter_factory: typing.Callable[[], typing.Callable[[Any], Optional[str]]],
    label: str,
) -> Optional[str]:
    """Re-run a synchronous denial-context getter off the event loop.

    Used by the appeal-generation gather block's fallback and retry paths to
    re-fetch cheap ORM-only context (PA rules, USPSTF preventive recs) that
    isn't persisted on the denial. Empty strings collapse to ``None`` so the
    downstream "has citations?" checks in ``make_open_prompt`` treat absent
    and empty results the same way.

    ``getter_factory`` is invoked lazily inside the try block so import-time
    failures (e.g., ImportError, circular-import edge cases) get the same
    degrade-to-None treatment as DB errors from the getter itself — matching
    the original pre-refactor behavior where each call site wrapped its
    ``from ... import ...`` in the same try/except.
    """
    try:
        getter = getter_factory()
        return await database_sync_to_async(getter)(denial) or None
    except Exception as e:
        logger.opt(exception=True).debug(f"{label} context refresh failed: {e}")
        return None


def _pa_context_getter_factory() -> typing.Callable[[Any], Optional[str]]:
    """Lazy importer for the PA-context getter.

    The import lives inside the factory (not at module top) so a failure
    here is caught by ``_refresh_sync_denial_context``'s try/except rather
    than aborting the appeal pipeline.
    """
    from fighthealthinsurance.pa_requirements import get_pa_context_for_denial

    return get_pa_context_for_denial


def _uspstf_context_getter_factory() -> typing.Callable[[Any], Optional[str]]:
    """Lazy importer for the USPSTF-context getter (see PA factory above)."""
    from fighthealthinsurance.uspstf_api import get_uspstf_context_for_denial

    return get_uspstf_context_for_denial


def deliverable_candidates(qs: QuerySet) -> QuerySet:
    """Narrow a ProposedAppeal queryset to rows that could be deliverable.

    The DB-side half of the deliverability rule, applied BEFORE anything
    counts, gates on, or decides what to do with these rows -- so a reserve
    holding nothing but junk reads as the empty reserve it effectively is,
    rather than as rows we are about to serve.

    Only the cheap half pushes into SQL: a raw character count is an upper
    bound on ``meaningful_appeal_length`` (whitespace and control characters
    only ever subtract), so anything shorter than ``MIN_APPEAL_CHARS`` raw can
    never pass and is not worth fetching. The word rule can't be expressed in
    SQL, so ``is_real_appeal`` stays the authority and still runs on every row
    AFTER this, immediately before it is served.
    """
    # Annotated rather than returned directly: django-stubs types the alias()
    # chain as Any, which trips --warn-return-any.
    narrowed: QuerySet = (
        qs.exclude(appeal_text__isnull=True)
        .alias(_appeal_len=Length("appeal_text"))
        .filter(_appeal_len__gte=MIN_APPEAL_CHARS)
    )
    return narrowed


def scoring_redactions(denial: Denial) -> list[tuple[str, str]]:
    """The identifiers held in this denial's profile fields, as (value,
    category) pairs for letter_quality.Redactor: the denial's email, claim
    id, plan id, fax and employer; the patient's and each professional's
    names, user names and emails; the professional's NPI and fax; each
    user's contact phone and address lines; the practice address. Nothing
    that lives only in the letter text itself.

    Sync on purpose (it walks FK relations; call it through
    database_sync_to_async). ALL or nothing: a relation that cannot be read
    raises, and the caller turns scoring off for the run. A partial list
    that quietly dropped the patient's name would be worse than no scoring.
    """
    out: list[tuple[str, str]] = []

    def add(value: Any, category: str) -> None:
        text = str(value).strip() if value is not None else ""
        if text and text.upper() != "UNKNOWN":
            out.append((text, category))

    def add_contact(user: Any) -> None:
        # The user's contact record: phone and address lines. Absent is
        # fine; unreadable is not.
        from django.core.exceptions import ObjectDoesNotExist

        try:
            contact = user.usercontactinfo
        except ObjectDoesNotExist:
            return
        add(contact.phone_number, "PHONE")
        add(contact.address1, "ADDRESS")
        add(contact.address2, "ADDRESS")

    def add_username(user: Any) -> None:
        # An identifier, not a name: matched whole-word in any case, where a
        # name only matches its capitalised spellings (review). A
        # domain-scoped login is stored as raw🐼domain_id
        # (fhi_users.auth.auth_utils.combine_domain_and_username); the raw
        # login is the spelling a person would write, so both go in (review).
        # A login spelled "unknown" falls to add()'s sentinel filter on
        # purpose: it identifies nobody, and redacting that word would blank
        # ordinary prose in most letters (review, accepted).
        username = str(getattr(user, "username", "") or "")
        add(username, "USERNAME")
        if "🐼" in username:
            add(username.split("🐼", 1)[0], "USERNAME")

    add(denial.raw_email, "EMAIL")
    add(denial.claim_id, "CLAIM_ID")
    add(denial.plan_id, "PLAN_ID")
    add(denial.appeal_fax_number, "PHONE")
    add(denial.employer_name, "EMPLOYER")
    patient = denial.patient_user
    if patient is not None:
        add(patient.get_legal_name(), "PATIENT#patient")
        add(patient.get_display_name(), "PATIENT#patient")
        add(patient.user.first_name, "PATIENT#patient")
        add(patient.user.last_name, "PATIENT#patient")
        add(patient.user.email, "EMAIL")
        add_username(patient.user)
        add_contact(patient.user)
    for field in ("primary_professional", "creating_professional"):
        professional = getattr(denial, field)
        if professional is None:
            continue
        person = f"PROFESSIONAL#{professional.pk}"  # one token per person
        add(professional.get_full_name(), person)
        add(professional.display_name, person)
        add(professional.user.first_name, person)
        add(professional.user.last_name, person)
        add(professional.user.email, "EMAIL")
        add_username(professional.user)
        add(professional.npi_number, "NPI")
        add(professional.fax_number, "PHONE")
        add_contact(professional.user)
    if denial.domain is not None:
        add(denial.domain.get_address(), "ADDRESS")
    return out


class AppealsBackendHelper:
    regex_denial_processor = ProcessDenialRegex()
    pmt = PubMedTools()
    nice = NICETools()
    clinical_trials = ClinicalTrialsTools()

    # How many delivered appeals count as "enough" for this denial. Below it we
    # top up from the reduced-context rows (the speculative reserve + the shed
    # tiers); at or above it we leave them held back, because padding a
    # sufficient result with weaker drafts adds no value.
    ENOUGH_APPEALS = 3

    # How many previously-saved drafts to replay before this run's own drafts.
    #
    # Rows accumulate per denial across retries, reconnects and re-runs, and the
    # replay had no cap at all, so a denial that had been through generation a
    # few times opened with a wall of stored letters and put the fresh one at
    # the bottom. Eighteen old drafts ahead of one new one is not a listing, it
    # is a haystack.
    #
    # Newest first, because the most recent drafts were generated with the most
    # context. This is deliberately independent of TypeSafe scoring: with the
    # ranking flags off there are no scores to sort by, and the page still must
    # not open with a haystack. Ranking sits on top of this rather than
    # replacing it.
    #
    # The cap limits what is SHOWN at the start of a run. It does not fence the
    # held-back rows off from the rest of the run, and that is deliberate:
    # synthesis below still draws on every stored draft for the denial (it is
    # choosing inputs, not showing them), and if the model regenerates text
    # identical to a held-back row, the uniqueness handler streams that stored
    # row -- a draft the user has not seen this session, which is the right
    # outcome even though the done frame counts it as new (review).
    MAX_REPLAYED_APPEALS = 3

    # Deadlines, measured from the start of the generation flow, after which a
    # run starts serving the speculative reserve instead of holding it to the
    # very end. Research + make_appeals routinely run for minutes, and a reserve
    # that only lands after all of it leaves the user staring at a spinner while
    # a usable appeal sits in the DB. Past the applicable mark, every checkpoint
    # in the flow (context gathering, the generating-phase heartbeats, the
    # streaming loop) flushes what the reserve has, up to ENOUGH_APPEALS.
    #
    # Two marks, because "nothing at all" and "fewer than we aim for" are
    # different problems. Someone with an empty screen is rescued first; someone
    # who already has an appeal in hand can afford to wait longer for the
    # full-context drafts, which are better than anything the reserve holds.
    #
    # Live drafts are unaffected either way: they are full-context rows and are
    # always streamed as they arrive, so this only ever adds to what the user
    # gets -- it never trades a good appeal for a speculative one.
    #
    # No appeal delivered at all yet:
    SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS = 45.0
    # At least one delivered, but still short of ENOUGH_APPEALS:
    SPECULATIVE_FALLBACK_UNDER_TARGET_SECONDS = 90.0

    @classmethod
    def generate_appeals_for_denial(
        cls, denial, background: bool = True, lease_epoch: Optional[int] = None
    ):
        """Internal entry point: the caller already holds a loaded, authorized
        ``Denial``. Builds the parameters itself (including the private
        identity key), so internal dispatchers never construct the public
        parameter dict by hand -- and the public path never learns to accept
        a caller-supplied hash. ``background=True`` also keeps these runs
        from consuming the user's interactive ``gen_attempts`` budget."""
        return cls.generate_appeals(
            {
                "denial_id": denial.denial_id,
                "email": None,
                "semi_sekret": denial.semi_sekret,
                "_internal_hashed_email": denial.hashed_email,
                "_background": background,
                # The journey's generation-lease epoch: save_appeal fences
                # every draft insert on it (generation_lease.assert_holds).
                "_lease_epoch": lease_epoch,
            }
        )

    @classmethod
    async def generate_appeals(cls, parameters) -> AsyncIterator[str]:
        """Public generator: streams ``_generate_appeals_body`` and, for the
        interactive path, RELEASES the generation lease the body stole when
        the stream finishes or the client hangs up (``aclose``). Wrapping
        rather than a try/finally around the ~1900-line body keeps that
        body's diff untouched (review)."""
        lease_ref: dict[str, Any] = {}
        # Declared AsyncIterator, an async generator at runtime (aclose exists).
        agen = cast(
            AsyncGenerator[str, None], cls._generate_appeals_body(parameters, lease_ref)
        )
        try:
            async for chunk in agen:
                yield chunk
        finally:
            await agen.aclose()
            extender = lease_ref.get("extender")
            if extender is not None:
                extender.cancel()
                try:
                    await extender
                except (asyncio.CancelledError, Exception):
                    pass
            if lease_ref.get("denial") is not None and lease_ref.get("epoch"):
                try:
                    await generation_lease.arelease(
                        lease_ref["denial"], lease_ref["epoch"]
                    )
                except Exception:
                    logger.opt(exception=True).warning(
                        "generation lease release failed; expiry will free it"
                    )

    @classmethod
    async def _generate_appeals_body(
        cls, parameters, lease_ref: dict[str, Any]
    ) -> AsyncIterator[str]:
        """
        Asynchronously generates and streams appeal texts for a given denial, including both previously saved and newly generated appeals.

        This coroutine retrieves denial and related context, processes templates and forms to construct appeal components, gathers citation contexts, and yields appeal texts with relevant substitutions applied. Previously saved appeals are yielded first, followed by newly generated appeals, each formatted as a JSON string.
        """
        logger.debug(f"Raw parameters received: {parameters}")

        # Extract specific parameters needed early
        denial_id = parameters["denial_id"]
        email = parameters["email"]
        semi_sekret = parameters["semi_sekret"]
        # Public contract: the hash is DERIVED from the caller's raw email,
        # never accepted from outside. Internal callers that already hold an
        # authorized Denial go through generate_appeals_for_denial below,
        # which sets the private key -- so a future endpoint passing
        # user-controlled values through this dict cannot substitute a hash
        # for knowing the email (PR #963 review).
        hashed_email = parameters.get(
            "_internal_hashed_email"
        ) or Denial.get_hashed_email(email)
        background = bool(parameters.get("_background"))
        # Generation-lease epoch this run writes under. Journeys pass theirs
        # in; the interactive path takes one by stealing below. EVERY draft
        # insert is fenced on it (save_appeal -> assert_holds), interactive
        # included: two concurrent interactive runs -- two tabs, a reconnect
        # racing the socket it replaced, retries through a proxy -- would
        # otherwise both persist and blow past the draft target at double
        # model spend (external review's barrier test: six drafts). The
        # newer steal wins; the superseded run stops generating quietly.
        # Interactive runs also use the epoch to keep the lease alive across
        # a long stream and to release it at the end.
        lease_epoch: Optional[int] = parameters.get("_lease_epoch")
        # Set when this run's epoch is superseded mid-stream: the streaming
        # loop stops pulling from the model and skips synthesis.
        superseded = False
        # Set when an interactive run could not acquire a lease at all (the
        # steal raised, twice). Such a run keeps STREAMING -- a database
        # hiccup must not cost a human their letters -- but it may not
        # PERSIST: a durable draft row without a live epoch could be written
        # alongside another owner's, which is the concurrency the lease
        # exists to prevent (external review).
        lease_unavailable = False
        # Extract the professional_to_finish parameter from the input, default to False
        professional_to_finish = parameters.get("professional_to_finish", False)
        # Set by the JS client when this socket replaces one that dropped (see
        # ws.onopen in appeal_fetcher.ts). Such a user has already waited out a
        # broken connection on top of whatever generation has cost so far, and
        # a fresh flow restarts flow_started at zero -- so the reserve's
        # deadlines would make them wait the full 45s/90s all over again. On a
        # reconnect the reserve goes out as soon as we know they are short of
        # ENOUGH_APPEALS. Absent (REST, older clients) means False, i.e. the
        # deadline behaviour is unchanged.
        is_reconnect = bool(parameters.get("reconnect", False))
        # Medical reason provided?
        medical_reasons = set()
        if (
            "medical_reason" in parameters
            and parameters["medical_reason"]
            and len(parameters["medical_reason"]) > 1
        ):
            medical_reasons.add(parameters["medical_reason"])

        if denial_id is None:
            raise Exception("Missing denial id")
        if semi_sekret is None:
            raise Exception("Missing sekret")

        # Short correlation id emitted in the init frame (so the client
        # captures it before any inactivity timeout can fire) and again in the
        # done frame. Lets a client-side ReportClientError be tied back to the
        # server-side generation trace. See APPEAL_GEN_DIAG logging below.
        generation_id = uuid.uuid4().hex[:12]

        # Start of the user's wait. The speculative-reserve deadline is measured
        # from here (not from the generating phase) because what the user
        # experiences is one uninterrupted wait: research/enrichment happens
        # before generation and can eat the whole budget on its own.
        flow_started = time.monotonic()

        # Instrumentation captured during the generating phase and surfaced in
        # the done frame + zero-appeal diagnostics.
        make_appeals_seconds: float = -1.0
        first_model: Optional[str] = None
        make_appeals_diag: dict[str, Any] = {}

        # Get the current info (e.g. denial). NOTHING is yielded before the
        # authenticated lookup and the durable form_completed intent below:
        # the init frame used to go out first, and a disconnect while the
        # generator sat suspended at that yield lost the completion entirely
        # (external review's one-yield-and-close test).
        await asyncio.sleep(0)
        denial_query = Denial.objects.filter(
            denial_id=denial_id, semi_sekret=semi_sekret, hashed_email=hashed_email
        ).select_related(
            "patient_user",
            "patient_user__user",
            "domain",
            "primary_professional",
            "primary_professional__user",
            "creating_professional",
            "creating_professional__user",
        )
        denial = await denial_query.aget()
        if not background:
            # Form completed: the durable intent is recorded the moment the
            # authenticated lookup succeeds -- before any yield, enrichment,
            # research, RAG, or reserve logic can crash and make a completed
            # user look abandoned (external review). One short transaction
            # (an INSERT-or-get); delivery is best-effort behind adeliver's
            # exception boundary and the relay repairs anything that fails.
            from fighthealthinsurance import intake_outbox

            form_intent = await intake_outbox.arecord_intent(
                denial, intake_outbox.FORM_COMPLETED
            )
        else:
            form_intent = None

        # Yield status: starting -- the first byte to the client, and it
        # follows the durable intent above by design.
        yield json.dumps(
            {
                "type": "status",
                "phase": "init",
                "message": "Starting appeal generation...",
                "generation_id": generation_id,
            }
        ) + "\n"

        if form_intent is not None:
            # Best-effort inline delivery (short timeout; the relay covers
            # the rest within a minute). After the first byte so a Temporal
            # stall never delays the user's first response.
            await intake_outbox.adeliver(form_intent, inline=True)
        if not background:
            # A live human outranks any background generator: STEAL the
            # denial's generation lease so a journey attempt in flight sees
            # the epoch move and stops quietly, and one arriving inside the
            # TTL backs off. One UPDATE; expiry is the release. Never let a
            # lease hiccup break the interactive flow (external review).
            # One shared renewal policy for the interactive flow and the
            # appeal journey (generation_lease.keep_renewed), so the two
            # cannot drift apart, and one shared clock so a renewal
            # confirmed by EITHER path counts for both -- save_appeal below
            # renews this same lease after each draft, and crediting only
            # the background loop meant a run whose background calls kept
            # raising declared the lease lost after a TTL while per-save
            # renewals were succeeding throughout (external review).
            lease_clock = generation_lease.RenewalClock()

            def _lease_lost(reason: str) -> None:
                nonlocal superseded
                superseded = True
                logger.info(
                    f"[gen_id={generation_id}] interactive lease no longer "
                    f"held for denial {denial_id}: {reason}"
                )

            async def _keep_interactive_lease(epoch: int) -> None:
                await generation_lease.keep_renewed(
                    denial, epoch, lease_clock, on_lost=_lease_lost
                )

            try:
                stolen = await generation_lease.aacquire(
                    denial,
                    holder=generation_lease.new_holder("interactive"),
                    steal=True,
                )
                lease_epoch = stolen.epoch
                lease_ref["denial"] = denial
                lease_ref["epoch"] = stolen.epoch

                lease_ref["extender"] = asyncio.create_task(
                    _keep_interactive_lease(stolen.epoch)
                )
            except Exception:
                # One quick retry -- a transient connection blip should not
                # cost this run its ability to persist.
                await asyncio.sleep(0.5)
                try:
                    stolen = await generation_lease.aacquire(
                        denial,
                        holder=generation_lease.new_holder("interactive"),
                        steal=True,
                    )
                    lease_epoch = stolen.epoch
                    lease_ref["denial"] = denial
                    lease_ref["epoch"] = stolen.epoch
                    lease_ref["extender"] = asyncio.create_task(
                        _keep_interactive_lease(stolen.epoch)
                    )
                except Exception:
                    # Still no lease: STREAM, but never persist. Drafts go
                    # out flagged unsaved rather than becoming durable rows
                    # this run cannot prove it owns (external review).
                    lease_unavailable = True
                    logger.opt(exception=True).error(
                        f"generation lease unavailable for denial {denial_id}; "
                        "streaming without persisting any draft"
                    )

        # Initial keepalive newline so clients know we're alive.
        yield "\n"

        # Helper format methods
        async def format_response(response: dict[str, str]) -> str:
            """
            Serializes a response dictionary to a JSON string with a trailing newline.

            Args:
                response: A dictionary containing string keys and values to serialize.

            Returns:
                A JSON-formatted string representation of the response, ending with a newline.
            """
            return json.dumps(response) + "\n"

        async def sub_in_appeals(appeal: dict[str, str]) -> dict[str, str]:
            """
            Performs dynamic substitution of denial and appeal-related fields into an appeal template.

            Replaces placeholders in the appeal's content with actual values from the associated denial, such as insurance company, claim ID, diagnosis, procedure, patient and professional names, and other context-specific information. Returns the appeal dictionary with the substituted content.
            """
            await asyncio.sleep(0)
            appeal["content"] = substitute_appeal_fields(denial, appeal["content"])
            return appeal

        # If we've had a timeout on the initial call and we're on round 2
        # we should fetch the existing appeals from the previous round if present.
        # Exclude speculative rows: those are the background precompute held in
        # reserve and are served ONLY as a fallback below, not as normal
        # existing appeals.
        # Newest first so the cap below keeps the most recent drafts rather than
        # whatever the database happened to return. created_at is null on legacy
        # rows, and Postgres sorts NULLs first on DESC, which would have handed
        # those rows the whole budget; nulls_last puts them where they belong.
        existing_appeals = (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False)
            .exclude(served_reserve_for_another_state())
            .order_by(F("created_at").desc(nulls_last=True), "-id")
            .all()
        )
        # Everything already delivered to this client, by normalized raw text.
        # Grown by every path that ships an appeal (existing rows, streamed
        # drafts, the early reserve flush, synthesis, the end-of-flow
        # reconciliation) so no path can send the same text twice.
        served_keys: set[str] = set()

        def _served_key(text: Any) -> str:
            # Normalized content fingerprint -- the SAME normalization the
            # database constraint enforces (fingerprint_text is the pure
            # function ProposedAppeal.fingerprint mirrors). An exact-string
            # set let a capitalization or whitespace variant through the
            # dedupe and then collide on insert, streaming two drafts against
            # one stored row (external review).
            return fingerprint_text(text) or str(text).strip()

        # Yield the existing appeals first
        # Draft scoring for ORDERING (ml/letter_quality.py). One task per saved
        # draft, never on the letter's own path: the letter streams the moment
        # it is saved, and its score follows as a {"type": "score"} frame,
        # drained between letters and once more (bounded) before the done
        # frame. Whatever the client sees, the score lands on the row for the
        # staff dashboard. Gated on the user's external-model consent, because
        # TypeSafe is one more external processor of the denial text.
        scoring_active = letter_quality.enabled() and bool(
            getattr(denial, "use_external", False)
        )
        score_tasks: list["asyncio.Task[Optional[str]]"] = []
        # Collected once, before any draft exists: the prompt asks the model
        # to write the patient's and professional's details INTO the letter,
        # so a draft is redacted against everything we hold before it leaves.
        scoring_identifiers: list[tuple[str, str]] = []
        if scoring_active:
            try:
                scoring_identifiers = await database_sync_to_async(scoring_redactions)(
                    denial
                )
            except Exception:
                # No identifier list means no scoring at all: the generic
                # patterns alone are not a promise we can keep.
                logger.opt(exception=True).warning(
                    f"[gen_id={generation_id}] could not collect redactions for "
                    f"denial {denial_id}; draft scoring off for this run"
                )
                scoring_active = False

        async def _note_scoring_failure(summary: str) -> None:
            # Cross-pod record for the status page: why the last call failed.
            await ExternalServiceHealth.anote_failure(letter_quality.SERVICE, summary)

        async def _score_draft(proposed_id: str, draft_text: str) -> Optional[str]:
            score = await letter_quality.score_letter(
                denial.denial_text,
                draft_text,
                identifiers=scoring_identifiers,
                on_failure=_note_scoring_failure,
            )
            if score is None:
                return None
            # Same record, the other way: TypeSafe answered. Best effort.
            await ExternalServiceHealth.anote_success(letter_quality.SERVICE)
            # Only the row whose text is still the text that was scored: an
            # admin can edit a draft during the few seconds of scoring, and
            # a score for text nobody sees any more must not land on the
            # edited row or reach the page (review). Legacy rows have no
            # fingerprint, so the guard is the text itself.
            updated = 1
            try:
                updated = await ProposedAppeal.objects.filter(
                    pk=proposed_id, appeal_text=draft_text
                ).aupdate(
                    quality_score=score.quality,
                    grounding_score=score.grounding,
                    quality_scorer=score.scorer,
                    quality_scored_at=timezone.now(),
                )
            except Exception:
                # The frame still goes out: ordering this run matters more
                # than the analytics row, and the failure is logged.
                logger.opt(exception=True).warning(
                    f"[gen_id={generation_id}] could not record a draft score "
                    f"for denial {denial_id}"
                )
            if not updated:
                logger.info(
                    f"[gen_id={generation_id}] draft {proposed_id} changed while "
                    "it was being scored; score dropped"
                )
                return None
            return json.dumps(letter_quality.score_frame(proposed_id, score)) + "\n"

        def _start_scoring(proposed_id: str, draft_text: str) -> None:
            existing = letter_quality.in_flight_task(proposed_id)
            if existing is not None:
                # A sibling stream on this worker (a reconnect) already
                # asked: share its answer instead of paying for it twice.
                if existing not in score_tasks:
                    score_tasks.append(existing)
                return
            task = asyncio.create_task(_score_draft(proposed_id, draft_text))
            letter_quality.keep_alive(task, proposed_id)
            score_tasks.append(task)

        def _finished_score_frames() -> list[str]:
            frames: list[str] = []
            for task in list(score_tasks):
                if not task.done():
                    continue
                score_tasks.remove(task)
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.warning(
                        f"[gen_id={generation_id}] draft scoring task failed: "
                        f"{type(exc).__name__}"
                    )
                    continue
                frame = task.result()
                if frame:
                    frames.append(frame)
            return frames

        # A drain waits, with its own budget, only on tasks no earlier drain
        # has waited on: a task stalled at the first drain is not waited on
        # again before done, while a draft saved later (synthesis, the
        # reconciliation) still gets a full DRAIN_SECONDS of its own.
        waited: set["asyncio.Task[Optional[str]]"] = set()

        async def _drain_score_frames(timeout: float) -> list[str]:
            fresh = [task for task in score_tasks if task not in waited]
            if fresh and timeout > 0:
                waited.update(fresh)
                await asyncio.wait(fresh, timeout=timeout)
            return _finished_score_frames()

        old = 0
        new = 0
        # Stored drafts the cap keeps off the screen, by normalized text. They
        # are NOT served: a synthesis result or a live draft that lands on one
        # of them is new to this user and must be delivered (the uniqueness
        # handler streams the stored row). They ARE kept out of the
        # end-of-flow reconciliation, or the cap would be undone at the end
        # (review).
        held_back_keys: set[str] = set()
        async for appeal in existing_appeals:
            # Enforce the deliverability rules on previously-saved appeals too:
            # the DB may hold short or wordless drafts saved before those
            # checks existed (or by paths that skipped the filter), and we must
            # not re-deliver them.
            if is_real_appeal(appeal.appeal_text):
                key = _served_key(appeal.appeal_text)
                if key in served_keys or key in held_back_keys:
                    # Legacy duplicate rows (NULL fingerprints, equivalent
                    # normalized text) are one draft to the user: stream
                    # the first, skip its twins, and don't count them in
                    # `old` (review).
                    logger.debug(f"Skipping duplicate existing appeal {appeal}")
                    continue
                if old >= cls.MAX_REPLAYED_APPEALS:
                    # Past the cap: remember it, don't show it. The cap counts
                    # DELIVERED rows, not rows examined: duplicates and
                    # unusable drafts are skipped above and must not spend the
                    # budget, or a denial whose recent rows happen to be twins
                    # would replay nothing at all.
                    held_back_keys.add(key)
                    continue
                old = old + 1
                logger.debug(f"Found existing appeal {appeal}, yielding")
                served_keys.add(key)
                if scoring_active and letter_quality.needs_scoring(appeal):
                    # An unscored (or older-rubric) draft must not sort
                    # below every fresh one just for being older.
                    _start_scoring(str(appeal.id), appeal.appeal_text)
                existing_appeal_dict = await sub_in_appeals(
                    letter_quality.with_score_fields(
                        {"id": str(appeal.id), "content": appeal.appeal_text}, appeal
                    )
                )
                yield await format_response(existing_appeal_dict)
            elif appeal.appeal_text is not None and str(appeal.appeal_text).strip():
                warn_unusable_appeal(
                    appeal.appeal_text,
                    f"saved appeal id={appeal.id} for denial {denial_id}",
                )

        if held_back_keys:
            logger.info(
                f"[gen_id={generation_id}] replay cap for denial {denial_id}: "
                f"served {old} stored drafts (newest first), held back "
                f"{len(held_back_keys)}"
            )

        # --- Early speculative fallback ---
        # What the precompute had ready before this run started. Logged here so
        # a trace shows, from the first frames, whether there was ever a safety
        # net -- and so the "we served the reserve" logs below can say whether
        # those rows were waiting all along or landed while we generated (the
        # precompute is a detached actor and finishes on its own schedule).
        #
        # Counts only rows that could actually be served: a reserve of runts or
        # identifier-echo junk is no safety net at all, and reporting it as one
        # would send an incident review looking for a fallback that could never
        # have fired. The reserve caps at MAX_SPECULATIVE_APPEALS rows, so
        # applying the full is_real_appeal rule here costs a handful of short
        # reads rather than a COUNT(*).
        # Best-effort: a failed count must not take the generation down with it.
        reserve_at_start = -1
        try:
            reserve_at_start = 0
            async for _row in deliverable_candidates(
                ProposedAppeal.objects.filter(
                    for_denial=denial,
                    speculative=True,
                    built_for_state=state_on_the_row_now(),
                )
            ).only("appeal_text"):
                if is_real_appeal(_row.appeal_text):
                    reserve_at_start += 1
        except Exception:
            reserve_at_start = -1
            logger.opt(exception=True).warning(
                f"[gen_id={generation_id}] could not count the speculative "
                f"reserve for denial {denial_id}"
            )
        logger.info(
            f"[gen_id={generation_id}] starting appeal generation for denial "
            f"{denial_id}: speculative reserve available at start="
            f"{reserve_at_start} (existing appeals={old}, deadlines: "
            + (
                "waived, reconnect"
                if is_reconnect
                else (
                    f"{cls.SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS:.0f}s with "
                    f"nothing delivered / "
                    f"{cls.SPECULATIVE_FALLBACK_UNDER_TARGET_SECONDS:.0f}s "
                    f"under {cls.ENOUGH_APPEALS}"
                )
            )
            + ")"
        )

        # Rows served early by serve_reserve_if_stalled, tracked by normalized
        # text: they are promoted to speculative=False, so without this they
        # would look like ordinary live drafts to the synthesis input query.
        early_reserve_texts: set[str] = set()
        reserve_served = 0
        reserve_notice_sent = False

        async def serve_reserve_if_stalled() -> AsyncIterator[str]:
            """Flush the speculative reserve once the run is over its deadline.

            Which deadline applies depends on what the user is looking at right
            now: with nothing delivered they are rescued at
            SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS; with at least one appeal in
            hand but fewer than ENOUGH_APPEALS they can wait until
            SPECULATIVE_FALLBACK_UNDER_TARGET_SECONDS for the full-context
            drafts, which beat anything the reserve holds. At or above
            ENOUGH_APPEALS this is a no-op, so a run that is delivering keeps
            its reserve held back exactly as before.

            On a WS reconnect neither deadline applies: the wait this run
            measures started when the replacement socket opened, so honouring
            it would make someone who already lost a connection start their
            45s/90s over. There, being under ENOUGH_APPEALS is the whole
            condition and the reserve goes out at the first checkpoint.

            Past the applicable mark it serves the held-back precompute (oldest
            first), promoting each row to speculative=False so it persists as a
            real appeal and a later call serves it as existing -- the same
            promotion the end-of-flow reconciliation does, just early enough to
            matter to someone watching a spinner.

            Callers invoke it at the checkpoints where the flow is already
            yielding (context gathering, generating-phase heartbeats, the
            streaming loop), so it costs one indexed query per checkpoint and
            only once a deadline is behind us.

            Best-effort like the reconciliation: never raises, because a DB
            hiccup here must not kill a stream that is otherwise fine.
            """
            nonlocal new, reserve_served, reserve_notice_sent
            delivered = new + old
            if delivered >= cls.ENOUGH_APPEALS:
                return
            if is_reconnect:
                # No deadline on a reconnect: this run's clock started when the
                # replacement socket opened, but the user's wait didn't. Being
                # under ENOUGH_APPEALS is the whole condition.
                rule = "reconnect"
            else:
                if delivered == 0:
                    deadline = cls.SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS
                    rule = f"no-appeal@{deadline:.0f}s"
                else:
                    deadline = cls.SPECULATIVE_FALLBACK_UNDER_TARGET_SECONDS
                    rule = f"under-target@{deadline:.0f}s"
                if (time.monotonic() - flow_started) < deadline:
                    return
            try:
                # chosen=False: never hand the user their own pick back as a
                # new appeal (see the same filter in the reconciliation).
                # deliverable_candidates keeps junk out of the loop entirely, so
                # it can't reach the ENOUGH_APPEALS check below and can't be the
                # row we stop on; is_real_appeal then re-checks each survivor,
                # since the word rule doesn't fit in SQL.
                async for row in deliverable_candidates(
                    # Compared with the state on the row as the query runs,
                    # like the promotion below: a correction landing mid-run
                    # must not hide a reserve stamped for the corrected state
                    # behind the copy this run loaded when it began.
                    ProposedAppeal.objects.filter(
                        for_denial=denial,
                        speculative=True,
                        chosen=False,
                        built_for_state=state_on_the_row_now(),
                    )
                ).order_by("id"):
                    if (new + old) >= cls.ENOUGH_APPEALS:
                        break
                    if not is_real_appeal(row.appeal_text):
                        continue
                    normalized = str(row.appeal_text).strip()
                    if _served_key(row.appeal_text) in served_keys:
                        continue
                    # Claim the row atomically. served_keys is per-run, so it
                    # cannot dedupe against a concurrent flow -- and a reconnect
                    # makes exactly that overlap likely, since the dropped
                    # socket's generator can still be draining server-side while
                    # the replacement flushes the reserve. Both would otherwise
                    # select and serve the same held-back draft; the client
                    # dedupes by content, so the done frame would promise more
                    # appeals than are on screen. The loser of the race skips.
                    if not await ProposedAppeal.objects.filter(
                        pk=row.pk,
                        speculative=True,
                        chosen=False,
                        built_for_state=state_on_the_row_now(),
                    ).aupdate(speculative=False):
                        continue
                    row.speculative = False
                    if not reserve_notice_sent:
                        reserve_notice_sent = True
                        yield json.dumps(
                            {
                                "type": "status",
                                "phase": "generating",
                                "message": (
                                    "Sending a draft appeal now while the "
                                    "full version keeps generating..."
                                ),
                            }
                        ) + "\n"
                    if scoring_active and letter_quality.needs_scoring(row):
                        _start_scoring(str(row.id), row.appeal_text)
                    row_dict = await sub_in_appeals(
                        letter_quality.with_score_fields(
                            {"id": str(row.id), "content": row.appeal_text}, row
                        )
                    )
                    yield await format_response(row_dict)
                    served_keys.add(_served_key(normalized))
                    early_reserve_texts.add(normalized)
                    new += 1
                    reserve_served += 1
                    logger.info(
                        f"[gen_id={generation_id}] picked speculative reserve "
                        f"appeal {row.id} for denial {denial_id} after "
                        f"{time.monotonic() - flow_started:.1f}s "
                        f"(rule={rule}, new={new}, old={old}, "
                        f"{'available at start' if reserve_at_start > 0 else 'arrived mid-generation'})"
                    )
            except Exception:
                logger.opt(exception=True).warning(
                    f"[gen_id={generation_id}] early speculative fallback failed "
                    f"for denial {denial_id}; the end-of-flow reconciliation "
                    f"remains as the backstop"
                )

        # First checkpoint, placed before research/generation rather than after
        # them: on a reconnect this fires immediately (no deadline to wait out),
        # so a user whose socket dropped gets the reserve in their first frames
        # instead of sitting through the whole flow again. On an ordinary run
        # nothing is past its deadline this early, so it is a no-op.
        async for _spec in serve_reserve_if_stalled():
            yield _spec

        # Yield status after any previously saved appeals have been sent
        yield json.dumps(
            {
                "type": "status",
                "phase": "init",
                "message": "Starting appeal generation...",
            }
        ) + "\n"
        yield json.dumps(
            {
                "type": "status",
                "phase": "init",
                "message": "Loaded denial information",
            }
        ) + "\n"
        yield json.dumps(
            {
                "type": "status",
                "phase": "init",
                "message": "Processing denial types and templates...",
            }
        ) + "\n"

        non_ai_appeals: List[str] = list(
            map(
                lambda t: t.appeal_text,
                await cls.regex_denial_processor.get_appeal_templates(
                    denial.denial_text, denial.diagnosis
                ),
            )
        )

        algorithmic_detection = detect_algorithmic_review_terms(
            denial.denial_text or ""
        )
        if algorithmic_detection.matched and algorithmic_detection.confidence in {
            "medium",
            "high",
        }:
            non_ai_appeals.extend(
                render_template_blocks(algorithmic_detection.suggested_template_blocks)
            )
            logger.info(
                f"Algorithmic-review detection matched for denial {denial.denial_id}: "
                f"{algorithmic_detection.debug_reason}"
            )

        # Specialized denial-type templates (e.g., MentalHealthParityAppeal)
        # surface a fully-formed letter as a static appeal AND seed a
        # citation hint for the highest-quality internal model.
        specialized_templates = detect_specialized_templates(
            denial.denial_text,
            denial.procedure,
            denial.diagnosis,
        )
        if specialized_templates:
            logger.info(
                "Specialized denial-type templates matched for denial "
                f"{denial.denial_id}: "
                f"{[t.name for t in specialized_templates]}"
            )
            for t in specialized_templates:
                try:
                    non_ai_appeals.append(t.static_appeal())
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"Failed to render specialized template {t.name}: {e}"
                    )

        insurance_company = denial.insurance_company or "insurance company;"
        claim_id = denial.claim_id or "YOURCLAIMIDGOESHERE"
        prefaces = []
        main = []
        footer = []
        medical_context = set()
        plan_context = set()
        # Extract any medical context AND
        # Apply all of our 'expert system'
        # (aka six regexes in a trench coat hiding behind a database).
        async for dt in denial.denial_type.all():
            form = await database_sync_to_async(dt.get_form)()
            if form is not None:
                parsed = form(parameters)
                if parsed.is_valid():
                    # Check and see if the form has a context method
                    op = getattr(parsed, "medical_context", None)
                    if op is not None and callable(op):
                        try:
                            mc = parsed.medical_context()
                            if mc is not None:
                                medical_context.add(mc)
                        except Exception as e:
                            logger.debug(
                                f"Error {e} processing form {form} for medical context"
                            )
                    # Check for plan context
                    op = getattr(parsed, "plan_context", None)
                    if op is not None and callable(op):
                        try:
                            pc = parsed.plan_context(denial)
                            if pc is not None:
                                plan_context.add(pc)
                        except Exception as e:
                            logger.debug(
                                f"Error {e} processing form {form} for plan context"
                            )
                    # See if we have a provided medical reason
                    if (
                        "medical_reason" in parsed.cleaned_data
                        and parsed.cleaned_data["medical_reason"] != ""
                    ):
                        medical_reasons.add(parsed.cleaned_data["medical_reason"])
                        logger.debug(f"Med reason {medical_reasons}")
                    # Questionable dynamic template
                    new_prefaces = parsed.preface()
                    for p in new_prefaces:
                        if p not in prefaces:
                            prefaces.append(p)
                    new_main = parsed.main()
                    for m in new_main:
                        if m not in main:
                            main.append(m)
                    new_footer = parsed.footer()
                    for f in new_footer:
                        if f not in footer:
                            footer.append(f)
                else:
                    if dt.appeal_text is not None:
                        main.append(dt.appeal_text)

        # Add the context to the denial — merge, never overwrite. Previously
        # this rebuilt qa_context with json.dumps and gated plan_context on
        # `is None`, dropping any plan info on subsequent calls.
        # medical_context / plan_context are sets; sort before joining so the
        # persisted strings are deterministic and don't churn between runs.
        # Save only the fields we touch here. A full-row asave() on this
        # denial — loaded well before the fire-and-forget PubMed/citation
        # warmers run — would write back the stale in-memory cache columns
        # (pubmed_context, candidate_ml_citation_context, ...) and clobber
        # whatever those background tasks persisted, defeating the warm-cache
        # barrier downstream.
        # Background journey runs do not consume the user's interactive
        # attempt budget: gen_attempts drives the skip-research-after-3
        # behavior users see, and silent background retries were eating it
        # (PR #963 review).
        dirty_fields = set() if background else {"gen_attempts"}
        # Only a questionnaire submission can withdraw the derived sentence.
        # Back and background generation carry no answers, so their empty
        # forms say nothing about what the person decided.
        if record_derived_medical_context(
            denial, medical_context, withdraw=bool(parameters.get("questionnaire"))
        ):
            dirty_fields.add("qa_context")
        if plan_context:
            merge_plan_context(denial, sorted(plan_context))
            dirty_fields.add("plan_context")
        # Update the denial object with the received parameter if it differs
        if denial.professional_to_finish != professional_to_finish:
            logger.info(
                f"Updating denial {denial.denial_id} professional_to_finish from {denial.professional_to_finish} to {professional_to_finish}"
            )
            denial.professional_to_finish = professional_to_finish
            dirty_fields.add("professional_to_finish")
        if not background:
            denial.gen_attempts = (denial.gen_attempts or 0) + 1
        if dirty_fields:
            await denial.asave(update_fields=sorted(dirty_fields))

        # Get pubmed, ml citations, and RAG context
        pubmed_context: Optional[str] = None
        ml_citation_context: Optional[Any] = None
        rag_context: Optional[str] = None
        nice_context: Optional[str] = None
        imr_context: Optional[str] = None
        pa_context: Optional[str] = None
        uspstf_context: Optional[str] = None
        clinical_trials_context: Optional[str] = None

        # Get PubMed context
        logger.debug("Looking up the pubmed context")

        # If we're getting "late" into our number of retries skip additional ctx.
        # (gen_attempts can be None on a background run, which never bumps it.)
        if (denial.gen_attempts or 0) < 3:
            # Yield status: gathering context
            yield json.dumps(
                {
                    "type": "status",
                    "phase": "research",
                    "message": "Gathering medical research and citations...",
                }
            ) + "\n"

            # Queue for per-substep completion status messages
            status_queue: asyncio.Queue[str] = asyncio.Queue()

            async def tracked_awaitable(
                awaitable: Any,
                substep: str,
                done_msg: str,
            ) -> Any:
                try:
                    result = await awaitable
                    await status_queue.put(
                        json.dumps(
                            {
                                "type": "status",
                                "phase": "research",
                                "substep": substep,
                                "state": "done",
                                "message": done_msg,
                            }
                        )
                    )
                    return result
                except Exception as e:
                    logger.warning(f"Research substep '{substep}' failed: {e}")
                    await status_queue.put(
                        json.dumps(
                            {
                                "type": "status",
                                "phase": "research",
                                "substep": substep,
                                "state": "error",
                                "message": f"{done_msg} (failed: {e})",
                            }
                        )
                    )
                    return None

            # Brief bounded wait for any in-flight fire-and-forget PubMed /
            # citation tasks (launched during entity extraction) before
            # falling through to the inline 40s fetch.  Keeps first-attempt
            # appeals from being under-contextualized when the background
            # cache warmer is almost done, while still capping the wait so
            # we never block forever.  Tunable via settings.
            from django.conf import settings as django_settings

            # Tolerate bad config: a present-but-unparseable value (None, "",
            # or a typo'd string) would otherwise raise here and abort appeal
            # generation before the inline fallback ever runs. Degrade to the
            # default instead — the whole barrier is best-effort.
            raw_barrier_timeout = getattr(
                django_settings, "FHI_CONTEXT_BARRIER_TIMEOUT_S", 10
            )
            try:
                barrier_timeout = float(raw_barrier_timeout)
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid FHI_CONTEXT_BARRIER_TIMEOUT_S="
                    f"{raw_barrier_timeout!r}; defaulting to 10s"
                )
                barrier_timeout = 10.0

            # Readiness columns are the ones the *background* task writes.
            # The speculative citation task stores to
            # candidate_ml_citation_context (not ml_citation_context), so
            # the citation barrier watches both; the refresh then hands the
            # inline generate_citations call a warm in-memory denial, and
            # that helper applies its own candidate->main freshness/promotion.
            def warmed_context(
                readiness_fields, refresh_fields, substep, done_msg, inline_coro
            ):
                return warm_then_fetch(
                    denial,
                    readiness_fields=readiness_fields,
                    refresh_fields=refresh_fields,
                    barrier_timeout=barrier_timeout,
                    fetch=lambda: tracked_awaitable(
                        asyncio.wait_for(inline_coro(), timeout=40),
                        substep=substep,
                        done_msg=done_msg,
                    ),
                )

            pubmed_context_awaitable = warmed_context(
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                substep="pubmed",
                done_msg="PubMed search complete",
                inline_coro=lambda: cls.pmt.find_context_for_denial(denial),
            )

            ml_citation_context_awaitable = warmed_context(
                readiness_fields=[
                    "ml_citation_context",
                    "candidate_ml_citation_context",
                ],
                refresh_fields=[
                    "ml_citation_context",
                    "candidate_ml_citation_context",
                    "candidate_procedure",
                    "candidate_diagnosis",
                ],
                substep="citations",
                done_msg="Citations generated",
                inline_coro=lambda: MLCitationsHelper.generate_citations_for_denial(
                    denial, speculative=False
                ),
            )

            # Extract procedure (CPT + HCPCS) and ICD-10 codes from the
            # denial text for RAG search. HCPCS Level II codes (DME,
            # drugs, prosthetics) are now included alongside CPT codes
            # so DME-coded denials get properly enriched context.
            rag_procedure_codes = None
            rag_diagnosis_codes = None
            denial_text_for_rag = denial.denial_text or ""
            if denial_text_for_rag:
                procedure_matches = sorted(extract_procedure_codes(denial_text_for_rag))
                icd_matches = sorted(extract_icd10_codes(denial_text_for_rag))
                if procedure_matches:
                    rag_procedure_codes = procedure_matches
                if icd_matches:
                    rag_diagnosis_codes = icd_matches

            # Get RAG context from magic-rag-service
            rag_context_awaitable = tracked_awaitable(
                asyncio.wait_for(
                    get_rag_context_for_denial(
                        denial_text=denial_text_for_rag,
                        state=denial.state,
                        procedure_codes=rag_procedure_codes,
                        diagnosis_codes=rag_diagnosis_codes,
                    ),
                    timeout=30,
                ),
                substep="guidelines",
                done_msg="Guidelines lookup complete",
            )

            # Get prior IMR / external-appeal decisions similar to this denial
            imr_context_awaitable = tracked_awaitable(
                asyncio.wait_for(
                    IMRDecisionRetriever.get_context_for_denial(denial),
                    timeout=10,
                ),
                substep="imr_decisions",
                done_msg="Prior IMR decisions lookup complete",
            )

            # Look up the payer's published prior-auth requirements for any
            # CPT/HCPCS in the denial. Cheap synchronous ORM call, wrapped so
            # it joins the parallel gather below.
            from fighthealthinsurance.pa_requirements import (
                get_pa_context_for_denial,
            )

            pa_context_awaitable = tracked_awaitable(
                asyncio.wait_for(
                    database_sync_to_async(get_pa_context_for_denial)(denial),
                    timeout=10,
                ),
                substep="pa_requirements",
                done_msg="Payer PA requirements lookup complete",
            )

            # Look up USPSTF preventive-services recommendations for any
            # preventive-care codes (e.g., screening colonoscopy, mammogram,
            # vaccines) referenced in the denial. A/B graded services trigger
            # ACA cost-sharing protections, which is the appeal angle. Cheap
            # synchronous ORM call against the cached recommendation table.
            from fighthealthinsurance.uspstf_api import (
                get_uspstf_context_for_denial,
            )

            uspstf_context_awaitable = tracked_awaitable(
                asyncio.wait_for(
                    database_sync_to_async(get_uspstf_context_for_denial)(denial),
                    timeout=10,
                ),
                substep="uspstf",
                done_msg="USPSTF preventive-services lookup complete",
            )

            # Look up cached ClinicalTrials.gov matches for this denial.
            # DB-only read — the live registry call already happened in the
            # ``find_clinical_trials`` prefetch fired from
            # ``extract_set_denial_and_diagnosis``. A short timeout is enough
            # because the worst case here is a couple of indexed ORM queries.
            clinical_trials_context_awaitable = tracked_awaitable(
                asyncio.wait_for(
                    cls.clinical_trials.get_context_for_denial(denial),
                    timeout=10,
                ),
                substep="clinical_trials",
                done_msg="ClinicalTrials.gov lookup complete",
            )

            # Skip the NICE task entirely when no key is configured: avoids a
            # misleading "NICE guidance lookup complete" status and the wait_for
            # overhead in environments without syndication access.
            gather_awaitables = [
                pubmed_context_awaitable,
                ml_citation_context_awaitable,
                rag_context_awaitable,
                imr_context_awaitable,
                pa_context_awaitable,
                uspstf_context_awaitable,
                clinical_trials_context_awaitable,
            ]
            if cls.nice.api_key:
                gather_awaitables.append(
                    tracked_awaitable(
                        asyncio.wait_for(
                            cls.nice.find_context_for_denial(denial),
                            timeout=30,
                        ),
                        substep="nice",
                        done_msg="NICE guidance lookup complete",
                    )
                )

            # return_exceptions=True is belt-and-suspenders: tracked_awaitable
            # already catches exceptions, but this prevents gather from raising
            # if any edge case slips through.
            try:
                logger.debug("Gathering contexts")
                results = await asyncio.gather(
                    *gather_awaitables,
                    return_exceptions=True,
                )

                # Drain substep status messages
                try:
                    while True:
                        yield status_queue.get_nowait() + "\n"
                except asyncio.QueueEmpty:
                    pass

                if isinstance(results[0], str):
                    pubmed_context = results[0]
                else:
                    pubmed_context = None
                if isinstance(results[1], list):
                    ml_citation_context = results[1]
                elif isinstance(results[1], str):
                    ml_citation_context = results[1]
                else:
                    ml_citation_context = None
                if isinstance(results[2], str):
                    rag_context = results[2]
                    logger.info("RAG context retrieved successfully")
                else:
                    rag_context = None
                    if results[2] is not None:
                        logger.debug(f"RAG context not available: {results[2]}")
                if isinstance(results[3], str) and results[3]:
                    imr_context = results[3]
                    logger.info("IMR decisions context retrieved")
                # Cache RAG and IMR on the denial so a gen_attempts>=3 retry
                # or an exception-fallback can recover them without rerunning
                # the external services. Mirrors the persistence behavior
                # that pubmed_context / ml_citation_context / nice_context
                # already enjoy via their helpers.
                persist_updates: dict[str, Any] = {}
                if rag_context:
                    persist_updates["rag_context"] = rag_context
                if imr_context:
                    persist_updates["imr_context"] = imr_context
                if persist_updates:
                    try:
                        await Denial.objects.filter(denial_id=denial_id).aupdate(
                            **persist_updates
                        )
                    except Exception as e:
                        logger.opt(exception=True).debug(
                            f"Failed to persist RAG/IMR context for "
                            f"denial {denial_id}: {e}"
                        )
                if isinstance(results[4], str) and results[4]:
                    pa_context = results[4]
                    logger.info("Payer PA requirements context retrieved")
                if isinstance(results[5], str) and results[5]:
                    uspstf_context = results[5]
                    logger.info("USPSTF preventive-services context retrieved")
                if isinstance(results[6], str) and results[6]:
                    clinical_trials_context = results[6]
                    logger.info("ClinicalTrials.gov context retrieved")
                if len(results) > 7 and isinstance(results[7], str):
                    nice_context = results[7]
                else:
                    # No fresh NICE result (skipped task or non-string error). Fall
                    # back to whatever is already persisted on the denial so cached
                    # NICE guidance survives a regen even when the API key is unset.
                    nice_context = denial.nice_context
                logger.debug("Success")
            except Exception as e:
                logger.opt(exception=True).error(f"Error gathering contexts: {e}")
                # Drain any status messages before falling back
                try:
                    while True:
                        yield status_queue.get_nowait() + "\n"
                except asyncio.QueueEmpty:
                    pass
                # We still might have saved a context.
                try:
                    # Added in Django 5.1
                    await denial.arefresh_from_db(from_queryset=denial_query)
                except AttributeError:
                    # arefresh_from_db with from_queryset not available in older Django
                    denial = await denial_query.aget()
                pubmed_context = denial.pubmed_context
                ml_citation_context = denial.ml_citation_context
                nice_context = denial.nice_context
                # RAG and IMR are now persisted (migration 0181) so recover
                # them on fallback the same way pubmed_context does. Missing
                # values stay None.
                rag_context = denial.rag_context or rag_context
                imr_context = denial.imr_context or imr_context
                # PA and USPSTF contexts aren't persisted; re-run the cheap
                # ORM queries so retries don't silently lose payer rules or
                # preventive-services recommendations. The factories
                # lazy-import their getter inside ``_refresh_sync_denial_context``
                # so an import-time failure degrades to None instead of
                # aborting the fallback path.
                pa_context = await _refresh_sync_denial_context(
                    denial, _pa_context_getter_factory, "PA"
                )
                uspstf_context = await _refresh_sync_denial_context(
                    denial, _uspstf_context_getter_factory, "USPSTF"
                )
                # ClinicalTrials context isn't persisted either; re-render from
                # the prefetched cache so the fallback path doesn't silently
                # lose trial evidence. The reader is already async (DB-only — no
                # live registry call), so it can't go through
                # _refresh_sync_denial_context (which wraps a sync getter in
                # database_sync_to_async); use an inline guard with the same
                # degrade-to-None behavior. wait_for bounds the DB read with
                # the same 10s budget the gather path uses so a stalled query
                # can't hang this resilience path (TimeoutError is an
                # Exception subclass, so the except below catches it too).
                try:
                    clinical_trials_context = await asyncio.wait_for(
                        cls.clinical_trials.get_context_for_denial(denial),
                        timeout=10,
                    )
                except Exception as inner:
                    logger.opt(exception=True).debug(
                        f"ClinicalTrials context refresh during fallback failed: {inner}"
                    )
                    clinical_trials_context = None
                logger.debug("Used saved contexts")
        else:
            logger.debug("Too many retries, skipping ML/pubmed/RAG ctx")
            # Re-read the row before reusing persisted contexts: earlier
            # attempts (or still-in-flight fire-and-forget tasks) may have
            # written pubmed/citation/RAG/IMR context after this request loaded
            # ``denial`` (~L2488). Without the refresh we'd log "using previous
            # results" while reading a stale snapshot and dropping the very
            # context this path exists to reuse. Mirrors the fallback branch.
            try:
                # Added in Django 5.1
                await denial.arefresh_from_db(from_queryset=denial_query)
            except AttributeError:
                # arefresh_from_db(from_queryset=...) not available pre-5.1
                denial = await denial_query.aget()
            nice_context = denial.nice_context
            pubmed_context = denial.pubmed_context
            ml_citation_context = denial.ml_citation_context
            rag_context = denial.rag_context
            imr_context = denial.imr_context
            # PA and USPSTF lookups are cheap ORM queries against cached
            # tables, so re-run them on retries instead of dropping them.
            # The factories lazy-import inside the helper's try/except so an
            # import-time failure degrades to None instead of aborting the
            # retry path.
            pa_context = await _refresh_sync_denial_context(
                denial, _pa_context_getter_factory, "PA"
            )
            uspstf_context = await _refresh_sync_denial_context(
                denial, _uspstf_context_getter_factory, "USPSTF"
            )
            # ClinicalTrials lookup is also DB-only against the prefetched
            # cache, so re-render it on retries. The reader is async, so it
            # uses an inline guard rather than _refresh_sync_denial_context
            # (which wraps a sync getter in database_sync_to_async). wait_for bounds
            # the DB read with the same 10s budget as the gather path.
            try:
                clinical_trials_context = await asyncio.wait_for(
                    cls.clinical_trials.get_context_for_denial(denial),
                    timeout=10,
                )
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"ClinicalTrials context refresh on retry failed: {e}"
                )
                clinical_trials_context = None
            yield json.dumps(
                {
                    "type": "status",
                    "phase": "research",
                    "message": "Research skipped — using previous results",
                    "substep": "all",
                    "state": "skipped",
                }
            ) + "\n"

        # Research/enrichment is the first stretch that can blow the reserve
        # deadline on its own (each source is individually bounded, but they
        # add up), so check here -- before the generating phase even starts --
        # rather than making the user wait out generation too.
        async for _spec in serve_reserve_if_stalled():
            yield _spec

        # Get microsite context if available. Optional means OPTIONAL: this
        # sits on the appeal generation critical path, so it gets a hard time
        # bound and any failure degrades to "no microsite context" instead of
        # killing the whole stream before generation starts.
        microsite_context: Optional[str] = None
        if denial.microsite_slug:
            try:
                from fighthealthinsurance.microsites import get_microsite

                microsite = get_microsite(denial.microsite_slug)
                if microsite:
                    # Note: pubmed_tools not available in this context, so only extralinks will be fetched
                    microsite_context = await asyncio.wait_for(
                        microsite.get_combined_context(
                            pubmed_tools=None,
                            max_extralink_docs=3,
                            max_extralink_chars=1500,
                        ),
                        timeout=15,
                    )
            except Exception as e:
                logger.warning(
                    f"Skipping microsite context for {denial.microsite_slug}: {e}"
                )
                microsite_context = None

        # Merge supplemental contexts into the citation pipeline. Both
        # microsite and IMR context follow the same pattern: append to
        # ml_citation_context if present, else pubmed_context, else use
        # standalone. attach_supplemental_to_citations also dedupes against
        # the existing block so a retry doesn't re-append the same content.
        # Guarded: losing supplemental context must never cost the appeal.
        try:
            ml_citation_context, pubmed_context = attach_supplemental_to_citations(
                ml_citation_context, pubmed_context, microsite_context
            )
            ml_citation_context, pubmed_context = attach_supplemental_to_citations(
                ml_citation_context, pubmed_context, imr_context
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to merge supplemental context into citations; "
                "continuing with the unmerged contexts"
            )

        async def save_appeal(item: GeneratedAppeal) -> dict[str, Any]:
            # Save all of the proposed appeals, so we can use RL later.
            nonlocal first_model, superseded
            appeal_text = item.text
            model_name = item.model_name
            if (
                first_model is None
                and model_name
                and item.context_level != CONTEXT_LEVEL_TEMPLATE
            ):
                # First deliverable draft's model — recorded for the done frame
                # and zero-appeal diagnostics so we can see which backend won.
                # Template rows carry a pseudo-model name and no backend won
                # anything for them, so they never claim this.
                first_model = str(model_name)
            t = time.time()
            logger.debug(f"Saving appeal ({len(appeal_text)} chars)")
            await asyncio.sleep(0)
            id = "unknown"
            save_failed = False
            stored_row: Optional[ProposedAppeal] = None
            if lease_unavailable or lease_epoch is None:
                # No epoch, no durable row -- whatever the reason: the
                # interactive steal raised twice (lease_unavailable), or a
                # caller drove generation without one. Persisting would
                # create a draft nobody can prove ownership of, beside
                # whatever the real lease holder is writing, which is
                # exactly the concurrency the lease prevents (external
                # review). The letter still streams, flagged unsaved, and
                # generation continues: losing every remaining draft would
                # be a worse outcome than losing durability for this run.
                logger.warning(
                    f"[gen_id={generation_id}] not persisting a draft for "
                    f"denial {denial_id}: no generation lease held"
                )
                if appeal_text:
                    served_keys.add(_served_key(appeal_text))
                unsaved: dict[str, Any] = {
                    "id": "unknown",
                    "content": appeal_text,
                    "save_failed": True,
                }
                if item.synthesized:
                    unsaved["synthesized"] = "true"
                return unsaved
            try:
                fingerprint = ProposedAppeal.fingerprint(appeal_text)
                pa = ProposedAppeal(
                    appeal_text=appeal_text,
                    for_denial=denial,
                    model_name=model_name,
                    synthesized=item.synthesized,
                    context_level=item.context_level,
                    text_fingerprint=fingerprint,
                )

                def _insert_fenced() -> None:
                    # One transaction, which atomic() nests as a SAVEPOINT
                    # inside any enclosing one: the lease ownership check and
                    # the draft insert commit together, so ANY run that was
                    # stolen from -- background or interactive -- cannot
                    # persist a draft after the steal, however far along its
                    # model call was; and an IntegrityError here cannot
                    # poison the caller's transaction state before the
                    # recovery query below runs (external reviews). Every
                    # generated-draft write requires a live, matching epoch:
                    # a run that cannot prove ownership never reaches here
                    # (see the no-epoch guard above). In autocommit this is
                    # just a short transaction around the insert.
                    with transaction.atomic():
                        if lease_epoch is not None:
                            generation_lease.assert_holds(denial, lease_epoch)
                        pa.save()

                try:
                    await database_sync_to_async(_insert_fenced)()
                except generation_lease.LeaseSuperseded:
                    raise
                except IntegrityError:
                    # Another writer (a racing journey activity, a retry, or
                    # a concurrent interactive run) already stored this exact
                    # draft: the unique (denial, fingerprint) constraint is
                    # the idempotency boundary. Reuse the durable row.
                    existing = await ProposedAppeal.objects.filter(
                        for_denial=denial, text_fingerprint=fingerprint
                    ).afirst()
                    if existing is None:
                        raise
                    if existing.speculative:
                        # The twin is a HELD-BACK reserve row. Reusing it
                        # un-promoted would stream a draft whose row stays
                        # invisible to every downstream reader (page
                        # reloads, choose/edit, journey counting): the
                        # appeal the user just watched would disappear
                        # (external review). Claim it atomically, same
                        # pattern as the reserve flush; if the flush claimed
                        # it first the update is a no-op and the row is
                        # already deliverable.
                        # Its text is the live run's own, generated from the
                        # inputs this run started with, so it is stamped for
                        # the state this run started under. If the state has
                        # moved meanwhile the replay filter hides it next time,
                        # as it should: the text argues under the old law.
                        await ProposedAppeal.objects.filter(
                            pk=existing.pk, speculative=True
                        ).aupdate(
                            speculative=False, built_for_state=reserve_state(denial)
                        )
                        existing.speculative = False
                    if existing.appeal_text != appeal_text:
                        # A normalized variant collided: stream the DURABLE
                        # text under the stored row's id. Sending the variant
                        # would show the client two drafts for one row, and a
                        # reload would 'lose' one (external review).
                        appeal_text = existing.appeal_text or appeal_text
                    pa = existing
                except Exception:
                    # Most save failures here are a stale/idle-killed
                    # connection on this consumer's thread; refresh
                    # connections and retry once before giving up.
                    await database_sync_to_async(close_old_connections)()
                    await database_sync_to_async(_insert_fenced)()
                stored_row = pa
                id = str(pa.id)
                if scoring_active and letter_quality.needs_scoring(pa):
                    _start_scoring(id, appeal_text)
            except generation_lease.LeaseSuperseded as e:
                # Superseded by a newer steal: the draft is NOT persisted and
                # goes out flagged as unsaved (the existing contract for a
                # row without a durable id). The journey's per-frame epoch
                # check ends its run right after; the interactive streaming
                # loop reads the flag below and stops pulling from the model.
                save_failed = True
                superseded = True
                logger.info(f"Draft not persisted, generation lease superseded: {e}")
            except Exception as e:
                # Still stream the draft -- the user gets their appeal even
                # when the save fails -- but tell the client the row has no
                # durable id so choose/edit affordances can be suppressed.
                save_failed = True
                logger.opt(exception=True).warning(
                    f"Failed to save proposed appeal: {e}"
                )
            if not save_failed and not background and lease_epoch is not None:
                # Keep a long interactive stream inside its lease: each
                # persisted draft pushes the expiry out, so a journey cannot
                # join a human's run after the TTL (review). Its own try,
                # AFTER the save handling: the row is already durable here,
                # so a transient renewal error must not report a stored
                # draft as failed -- the client would suppress choose/edit
                # for a perfectly valid id. A renewal that returns False
                # means the lease moved on: this draft is still legitimately
                # stored (it passed the fence when it was inserted), but the
                # run is superseded and stops after this frame.
                try:
                    _renew_started = time.monotonic()
                    # Bounded like the background loop: a hung renewal here
                    # would park the interactive flow AFTER the draft was
                    # already durable (external review). The timeout is
                    # caught by the except below and treated as transient.
                    if not await asyncio.wait_for(
                        generation_lease.aextend(denial, lease_epoch),
                        timeout=generation_lease.EXTEND_INTERVAL_SECONDS,
                    ):
                        superseded = True
                        logger.info(
                            f"[gen_id={generation_id}] lease no longer held after "
                            f"saving a draft for denial {denial_id}"
                        )
                    else:
                        # A renewal confirmed HERE is still a renewal: tell the
                        # shared clock, or the background loop's expiry check
                        # counts from a staleness this path already disproved.
                        lease_clock.confirm(_renew_started)
                except Exception:
                    logger.opt(exception=True).warning(
                        f"[gen_id={generation_id}] lease renewal failed after a "
                        f"successful save for denial {denial_id}"
                    )
            passed = time.time() - t
            logger.debug(f"Saved appeal ({len(appeal_text)} chars) in {passed:.1f}s")
            # Mark it served as soon as it is on its way out, so the early
            # reserve flush running between streamed drafts can't ship a
            # speculative row whose text matches one already sent.
            if appeal_text:
                served_keys.add(_served_key(appeal_text))
            result: dict[str, Any] = {"id": id, "content": appeal_text}
            if save_failed:
                result["save_failed"] = True
            # The badge says what the ROW says. A synthesis result can land on
            # a stored draft (the fingerprint constraint hands back the twin),
            # and a pick of that frame is recorded under the twin's model, so
            # the frame must not call it a synthesis: decided here, from the
            # row, rather than by the synthesis branch that asked for it.
            row_synthesized = (
                stored_row.synthesized if stored_row is not None else item.synthesized
            )
            if row_synthesized:
                result["synthesized"] = "true"
            return result

        # (The form_completed intake event was recorded right after the
        # authenticated denial lookup at the top of this generator.)

        # Yield status: generating appeals
        yield json.dumps(
            {
                "type": "status",
                "phase": "generating",
                "message": "Generating personalized appeals with AI...",
            }
        ) + "\n"

        # Feed the prompt the *merged* plan_context (built by
        # merge_plan_context above) plus the plan-documents summary. The old
        # code interpolated the raw `plan_context` set, which both dropped
        # the merged fragments and leaked a Python set repr ("{'a', 'b'}")
        # into the model prompt.
        plan_parts: list[str] = []
        if denial.plan_context:
            plan_parts.append(str(denial.plan_context))
        if denial.plan_documents_summary:
            plan_parts.append(denial.plan_documents_summary)
        model_plan_context: Optional[str] = "\n\n".join(plan_parts) or None

        # Cadence + ceiling for the generating-phase heartbeat. make_appeals
        # blocks (commonly 10s-2min, worst case several minutes across the
        # primary -> backup -> shed-tier cascade, each ML call bounded by the
        # 300s per-inference timeout) with nothing else reaching the wire.
        # Without heartbeats the browser's 90s inactivity watchdog
        # (WS_INACTIVITY_TIMEOUT_MS in appeal_fetcher.ts) tears the socket
        # down mid-generation -> escalate to REST -> same silent stall ->
        # 0 appeals. 15s stays well under that 90s budget (and keeps iOS from
        # dropping the REST-fallback stream).
        #
        # GENERATING_PHASE_BUDGET is the ceiling for the WHOLE generating phase
        # -- the (rare, cached) denial-text summarization PLUS make_appeals --
        # SHARED between them so their sum can't exceed the client's 420s hard
        # cap (WS_HARD_TIMEOUT_MS): summarization is capped at
        # SUMMARIZE_OVERALL_TIMEOUT, then make_appeals gets whatever remains of
        # the 360s budget. (Reasoning about make_appeals in isolation would
        # miss the summarize phase running before it.)
        MAKE_APPEALS_KEEPALIVE_INTERVAL = 15
        GENERATING_PHASE_BUDGET = 360
        SUMMARIZE_OVERALL_TIMEOUT = 90

        def _generating_heartbeat(elapsed: float) -> str:
            return (
                json.dumps(
                    {
                        "type": "status",
                        "phase": "generating",
                        "message": (
                            "Generating personalized appeals with AI... "
                            f"({int(elapsed)}s elapsed)"
                        ),
                    }
                )
                + "\n"
            )

        # For a very large denial letter, condense denial_text once (cached)
        # so it doesn't overflow the model's context window -- an otherwise
        # silent failure that yields 0 appeals. Returns None instantly for
        # normal-sized denials (the common case), so full context is preferred.
        # Heartbeat-wrapped so the rare, slow summarization call can't open a
        # silent window either; capped at SUMMARIZE_OVERALL_TIMEOUT so it can't
        # eat the whole shared budget.
        generating_phase_started = time.monotonic()
        summarize_task: "asyncio.Future[Optional[str]]" = asyncio.ensure_future(
            MLAppealContextHelper.maybe_summarize_denial_text(denial)
        )
        async for _hb in keepalive_frames(
            summarize_task,
            interval=MAKE_APPEALS_KEEPALIVE_INTERVAL,
            overall_timeout=SUMMARIZE_OVERALL_TIMEOUT,
            label=f"summarize[gen_id={generation_id}]",
            make_heartbeat=lambda elapsed: json.dumps(
                {
                    "type": "status",
                    "phase": "generating",
                    "message": (
                        "Condensing a long denial letter... "
                        f"({int(elapsed)}s elapsed)"
                    ),
                }
            )
            + "\n",
        ):
            yield _hb
            # Piggyback on the heartbeat cadence: every beat past the deadline
            # is a chance to hand over the reserve instead of another "still
            # working" frame.
            async for _spec in serve_reserve_if_stalled():
                yield _spec
        denial_text_override: Optional[str] = None
        if summarize_task.done():
            try:
                denial_text_override = summarize_task.result()
            except Exception:
                logger.opt(exception=True).warning(
                    f"[gen_id={generation_id}] denial_text summarization "
                    f"failed for denial {denial_id}; using full text"
                )
        else:
            # Summary took too long. It's a native coroutine, so cancel it
            # cleanly and proceed with the full denial text (make_appeals'
            # shed ladder remains the backstop for context overflow).
            summarize_task.cancel()
            logger.warning(
                f"[gen_id={generation_id}] denial_text summarization exceeded "
                f"{SUMMARIZE_OVERALL_TIMEOUT}s for denial {denial_id}; "
                f"proceeding with full text"
            )

        # make_appeals gets whatever remains of the shared generating-phase
        # budget after summarization, with a floor so a slow summarize can't
        # starve generation entirely. This keeps summarize + generate under the
        # client's 420s hard cap.
        gen_started = time.monotonic()
        make_appeals_overall_timeout = max(
            60.0,
            GENERATING_PHASE_BUDGET - (gen_started - generating_phase_started),
        )
        gen_task: "asyncio.Future[Iterator[GeneratedAppeal]]" = asyncio.ensure_future(
            # thread_sensitive=False, for the same reason the speculative
            # precompute passes it (ml_speculative_appeals_helper): the default
            # (True) runs this on asgiref's ONE process-wide thread-sensitive
            # executor thread, and make_appeals holds it for the whole
            # generating budget (up to GENERATING_PHASE_BUDGET). Everything
            # else that reaches the ORM through an async path -- including the
            # `serve_reserve_if_stalled()` query on the heartbeat below, and
            # every OTHER concurrent stream on this pod -- then queues behind
            # it. Observed: the reserve checkpoint on the 45s beat blocked for
            # 255s until make_appeals returned, so the socket went silent well
            # past the client's 90s inactivity watchdog
            # (WS_INACTIVITY_TIMEOUT_MS) and the run ended with 0 appeals
            # delivered. DatabaseSyncToAsync still wraps each call in
            # close_old_connections() either way, so connection handling is
            # unchanged. executor=bridge_executor keeps this minutes-long
            # block off the loop's small shared default executor (see
            # exec.py).
            database_sync_to_async(
                appealGenerator.make_appeals,
                thread_sensitive=False,
                executor=bridge_executor,
            )(
                denial,
                AppealTemplateGenerator(prefaces, main, footer),
                medical_reasons=medical_reasons,
                non_ai_appeals=non_ai_appeals,
                pubmed_context=pubmed_context,
                ml_citations_context=ml_citation_context,
                plan_context=model_plan_context,
                rag_context=rag_context,
                nice_context=nice_context,
                specialized_templates=specialized_templates,
                pa_context=pa_context,
                uspstf_context=uspstf_context,
                clinical_trials_context=clinical_trials_context,
                generation_id=generation_id,
                diagnostics_sink=make_appeals_diag,
                denial_text_override=denial_text_override,
                # Cooperative cutoff for the submitted model calls: when the
                # keepalive loop below abandons this task at the overall
                # timeout, the executor threads notice the same deadline and
                # drain within one call boundary instead of running for
                # minutes after the client is gone.
                deadline=gen_started + make_appeals_overall_timeout,
            )
        )
        # Heartbeat while make_appeals blocks so no >90s silent window exists
        # on either transport (the WS consumer and the REST fallback both
        # forward every yielded frame verbatim).
        async for _hb in keepalive_frames(
            gen_task,
            interval=MAKE_APPEALS_KEEPALIVE_INTERVAL,
            overall_timeout=make_appeals_overall_timeout,
            make_heartbeat=_generating_heartbeat,
            label=f"generating[gen_id={generation_id}]",
        ):
            yield _hb
            # The longest blocking stretch of the whole flow, and the one the
            # reserve was built for: check on every beat so a stalled backend
            # costs the user at most one heartbeat interval past the deadline.
            async for _spec in serve_reserve_if_stalled():
                yield _spec

        make_appeals_seconds = time.monotonic() - gen_started
        appeals: Iterator[GeneratedAppeal]
        gen_error: Optional[str] = None
        if gen_task.done():
            try:
                appeals = await gen_task
            except Exception as e:
                # Do NOT let this propagate. make_appeals blowing up is exactly
                # the case the held-back reserve exists for, and re-raising here
                # skips the synthesis and end-of-flow reconciliation below --
                # so the user got nothing even though we had drafts ready for
                # them. Falling through with an empty iterator reaches the
                # reconciliation, which serves the reserve, and still ends the
                # stream with a proper done frame instead of a dirty one. The
                # failure is not hidden: it is logged here and the zero-appeal
                # diagnostic (keyed on the pre-reserve count) still fires.
                gen_error = f"{type(e).__name__}: {e}"
                logger.opt(exception=True).error(
                    f"[gen_id={generation_id}] make_appeals raised for denial "
                    f"{denial_id} after {make_appeals_seconds:.1f}s; falling "
                    f"through to the reserve. {gen_error}"
                )
                capture_reliability_event(
                    "make_appeals_raised",
                    denial_id=denial_id,
                    generation_id=generation_id,
                    seconds=round(make_appeals_seconds, 1),
                    error=gen_error,
                )
                appeals = iter([])
            else:
                logger.info(
                    f"[gen_id={generation_id}] make_appeals returned in "
                    f"{make_appeals_seconds:.1f}s for denial {denial_id} "
                    f"(winning_stage={make_appeals_diag.get('winning_stage')}, "
                    f"shed_tier={make_appeals_diag.get('shed_tier')})"
                )
        else:
            # Exceeded the overall budget while still running. The threadpool
            # thread cannot be cancelled and keeps running in the background
            # (database_sync_to_async still closes its DB connections on the
            # awaiting side); abandon its result and fall through with zero
            # appeals. Retrieve any eventual exception so asyncio does not log
            # "Task exception was never retrieved".
            def _swallow_abandoned(t: "asyncio.Future[Any]") -> None:
                if not t.cancelled():
                    t.exception()

            gen_task.add_done_callback(_swallow_abandoned)
            logger.error(
                f"[gen_id={generation_id}] make_appeals exceeded "
                f"{make_appeals_overall_timeout:.0f}s for denial {denial_id}; "
                f"abandoning (background thread continues). "
                f"{summarize_denial_context_tokens(denial)}"
            )
            capture_reliability_event(
                "make_appeals_abandoned",
                denial_id=denial_id,
                generation_id=generation_id,
                budget_seconds=round(make_appeals_overall_timeout, 0),
            )
            appeals = iter([])
        # Drop None / empty / whitespace / runt / wordless outputs. Track the
        # rejects so the zero-appeal diagnostic can distinguish "models silent"
        # from "models producing only undeliverable strings".
        runts = 0
        dupes = 0

        def keep(item: Optional[GeneratedAppeal]) -> bool:
            nonlocal runts, dupes
            if item is None:
                return False
            text = item.text
            if is_real_appeal(text):
                # A live model can land on text we already sent -- most likely
                # the reserve draft the early fallback just served, since the
                # precompute runs the same internal models on the same denial.
                # Drop it rather than stream a known duplicate: the client
                # dedupes by content, so shipping it would make the done frame's
                # count exceed what the user can see, which is precisely what
                # trips the "partial delivery" error path in appeal_fetcher.ts.
                # (Same reasoning as the verbatim-copy guard on synthesis.)
                # This runs on the sync_iterator_to_async worker thread while the
                # event loop may be adding to served_keys; a set membership test
                # is a single atomic lookup, and the only cost of racing one is
                # an occasional duplicate slipping through -- exactly today's
                # behavior.
                if _served_key(text) in served_keys:
                    dupes += 1
                    logger.info(
                        f"[gen_id={generation_id}] dropping a live draft "
                        f"(model={item.model_name!r}) for denial {denial_id} "
                        f"that duplicates an appeal already sent"
                    )
                    return False
                return True
            if isinstance(text, str) and text.strip():
                runts += 1
                warn_unusable_appeal(
                    text,
                    f"model={item.model_name!r} for denial {denial_id}",
                )
            return False

        filtered_appeals: Iterator[GeneratedAppeal] = filter(keep, appeals)

        # Convert the blocking sync iterator to async so next() calls
        # run in a thread executor and don't block the event loop.
        # Without this, as_available_nested()'s concurrent.futures.as_completed()
        # blocks the event loop, preventing keep-alive newlines from being sent.
        async_appeals: AsyncIterator[GeneratedAppeal] = sync_iterator_to_async(
            filtered_appeals
        )

        # We convert to async here.
        saved_appeals: AsyncIterator[dict[str, str]] = a.map(save_appeal, async_appeals)
        # Note: we intentionally call save before substution.
        subbed_appeals: AsyncIterator[dict[str, str]] = a.map(
            sub_in_appeals, saved_appeals
        )
        appeals_json: AsyncIterator[str] = a.map(format_response, subbed_appeals)
        # StreamignHttpResponse needs a synchronous iterator otherwise it blocks.
        interleaved: AsyncIterator[str] = interleave_iterator_for_keep_alive(
            appeals_json
        )

        # NB: `new` is NOT reset here -- the early speculative fallback may
        # already have delivered rows, and they count toward both the done
        # frame's totals and the ENOUGH_APPEALS gate.
        async for i in interleaved:
            # Interleave keep-alives are bare newlines; real appeals exceed
            # MIN_APPEAL_CHARS (same threshold as the generation-side filter).
            if i and len(i) > MIN_APPEAL_CHARS:
                new = new + 1
                logger.debug(f"Sending appeal count: {new+old}...")
            else:
                logger.debug("Sending keep alive....")
            yield i
            for score_json in _finished_score_frames():
                yield score_json
            if superseded:
                # A newer run owns this denial now (a second tab, a reconnect
                # replacing this socket, a retry through a proxy): stop
                # pulling from the model instead of generating drafts that
                # can no longer be persisted. The producer thread already in
                # flight finishes on its own (see the gen_task note above);
                # nothing it yields after this is consumed. (Review.)
                logger.info(
                    f"[gen_id={generation_id}] generation lease superseded; "
                    f"stopping the stream for denial {denial_id}"
                )
                break
            # Also a checkpoint: draining this iterator blocks on the next model
            # future, so a run that streams one draft and then stalls for
            # minutes still gets the reserve at the deadline.
            async for _spec in serve_reserve_if_stalled():
                yield _spec
        # Scores still in flight get a bounded wait so the client can rank
        # before the done frame; a superseded run only collects what finished.
        for score_json in await _drain_score_frames(
            0.0 if superseded else letter_quality.DRAIN_SECONDS
        ):
            yield score_json
        logger.debug(
            f"Normal appeals sent {new} and {old} "
            f"(runt_count={runts}, dupe_count={dupes})"
        )
        yield json.dumps(
            {
                "type": "status",
                "phase": "generating",
                "message": "Regular appeals finished. Checking once more...",
            }
        ) + "\n"

        # --- Final synthesis step ---
        # Query saved appeals from DB rather than collecting in-flight,
        # so we don't interfere with the streaming pipeline.
        # We emit keepalives every 20s (SYNTHESIS keepalive loop below) and
        # cap synthesis at 120s so the client's 90s inactivity watchdog
        # (WS_INACTIVITY_TIMEOUT_MS in appeal_fetcher.ts) never fires between
        # frames.
        # Exclude speculative rows: synthesis should combine the live drafts,
        # not the held-back precompute (which could also spuriously push a
        # 1-real-draft denial to the >=2 synthesis gate). Rows the early
        # fallback served are excluded by text: promotion already flipped them
        # to speculative=False, so the filter alone would no longer catch them
        # -- and reaching the synthesis gate on the strength of the reserve is
        # exactly what that filter is there to prevent.
        saved_appeal_texts: list[str] = [
            str(pa.appeal_text)
            async for pa in deliverable_candidates(
                ProposedAppeal.objects.filter(
                    for_denial=denial, speculative=False
                ).exclude(served_reserve_for_another_state())
            )
            if is_real_appeal(pa.appeal_text)
            and str(pa.appeal_text).strip() not in early_reserve_texts
        ]
        # Everything streamed so far this run persists as speculative=False
        # (existing rows + drafts saved during streaming), so this doubles as
        # the set already sent. served_keys is tracked from the top of the flow
        # (and grown by synthesis / the end-of-flow reconciliation below), so
        # this only fills in anything a row landed for that no yield path saw.
        # Caveat: save_appeal deliberately swallows a failed asave and still
        # streams the draft; such a draft has no row here, but the streaming
        # path already recorded its text, so the reconciliation still won't
        # re-serve it.
        # ...except the rows the replay cap held back. Those were never sent,
        # and a synthesis result that lands on one of them is new to this user:
        # marking it served here would make the guard below discard the only
        # copy the user would ever see (review).
        served_keys.update(
            {
                k
                for k in (_served_key(s) for s in saved_appeal_texts if s)
                if k not in held_back_keys
            }
        )
        # Synthesis requires >=2 drafts to be meaningful: with a single
        # input, models often regurgitate it verbatim. The client dedupes
        # by content, so a verbatim copy gets silently dropped, which then
        # trips the "partial delivery" error path in appeal_fetcher.ts.
        if len(saved_appeal_texts) >= 2 and not superseded:
            yield json.dumps(
                {
                    "type": "status",
                    "phase": "synthesizing",
                    "message": "Synthesizing best appeal from all drafts...",
                }
            ) + "\n"
            try:
                # Which backend's synthesis wins, for the attempt log: the
                # stored row keeps the reserved "synthesized" name, so this is
                # the only record of who actually wrote the letter.
                synthesis_provenance: dict[str, Any] = {}
                synthesis_started = time.monotonic()
                synthesis_started_wall = timezone.now()
                synthesis_task = asyncio.ensure_future(
                    appealGenerator.synthesize_appeals(
                        appeal_texts=saved_appeal_texts,
                        denial_text=(
                            str(denial.denial_text) if denial.denial_text else None
                        ),
                        procedure=(str(denial.procedure) if denial.procedure else None),
                        diagnosis=(str(denial.diagnosis) if denial.diagnosis else None),
                        provenance=synthesis_provenance,
                    )
                )
                # Emit keepalives while synthesis is running, up to 120s
                # (best_within_timelimit uses 60s internally + fallback)
                SYNTHESIS_TIMEOUT = 120
                KEEPALIVE_INTERVAL = 20
                elapsed = 0.0
                while not synthesis_task.done() and elapsed < SYNTHESIS_TIMEOUT:
                    t0 = time.monotonic()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(synthesis_task),
                            timeout=KEEPALIVE_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        elapsed += time.monotonic() - t0
                        yield "\n"
                        continue
                    elapsed += time.monotonic() - t0
                    break

                if not synthesis_task.done():
                    synthesis_task.cancel()
                    logger.warning(f"Synthesis timed out after {SYNTHESIS_TIMEOUT}s")
                else:
                    synthesized = synthesis_task.result()
                    # The model's time, before the save and lease renewal.
                    synthesis_seconds = time.monotonic() - synthesis_started
                    if synthesized and not is_real_appeal(synthesized):
                        # Non-empty but not deliverable (too short, or not
                        # made of words): filter it out so
                        # synthesis can't bypass the rules the streaming path
                        # enforces.
                        warn_unusable_appeal(
                            synthesized,
                            f"synthesis output for denial {denial_id}",
                        )
                    elif synthesized:
                        # Belt-and-suspenders: even with >=2 drafts a model
                        # can still pick one verbatim. Skip the yield in
                        # that case rather than ship a known duplicate.
                        normalized = synthesized.strip()
                        if _served_key(synthesized) in served_keys:
                            logger.info(
                                "Synthesis returned a verbatim copy of an input draft; skipping yield"
                            )
                        else:
                            saved = await save_appeal(
                                GeneratedAppeal(
                                    text=synthesized,
                                    model_name="synthesized",
                                    synthesized=True,
                                    context_level=CONTEXT_LEVEL_SYNTHESIZED,
                                )
                            )
                            winner = synthesis_provenance.get("model")
                            if winner is not None:
                                # The call still succeeded when its text landed
                                # on a stored draft, but what was served is that
                                # draft under its own model: say so on the row.
                                served_as_draft = saved.get("synthesized") != "true"
                                await _record_synthesis_attempt(
                                    denial_id=denial.denial_id,
                                    generation_id=generation_id,
                                    model=winner,
                                    text=synthesized,
                                    duration_seconds=synthesis_seconds,
                                    started_wall=synthesis_started_wall,
                                    detail=(
                                        f"reproduced stored draft {saved.get('id')}; "
                                        "served as that draft"
                                        if served_as_draft
                                        else ""
                                    ),
                                )
                            subbed = await sub_in_appeals(saved)
                            if subbed.get("synthesized") != "true":
                                # The synthesis reproduced a stored draft the
                                # replay cap held back: what is served is that
                                # draft, under its own model (save_appeal
                                # badges from the row), so the frame is not
                                # badged as a synthesis that a pick would then
                                # be credited to the draft's model for.
                                logger.info(
                                    f"[gen_id={generation_id}] synthesis for "
                                    f"denial {denial_id} reproduced a stored "
                                    f"draft; serving it as that draft"
                                )
                            yield await format_response(subbed)
                            served_keys.add(_served_key(normalized))
                            new += 1
                            logger.info(
                                f"Synthesized appeal generated from {len(saved_appeal_texts)} drafts"
                            )
                    else:
                        logger.debug("Synthesis returned no result, skipping")
            except Exception:
                logger.opt(exception=True).warning("Final appeal synthesis failed")

        # --- End-of-flow reconciliation ---
        # One final DB read (both WS and REST run through this generator, so
        # both get it) to catch any real appeals that landed but weren't
        # streamed. The background speculative precompute writes asynchronously
        # and can land rows mid-flight; a slow/late model draft can too. We
        # serve anything we haven't already sent, with one gate:
        #   - "mini"/restricted rows -- the reduced-context precompute
        #     (speculative) and the shed tiers (tier1/tier2) -- are served ONLY
        #     while under threshold (< ENOUGH_APPEALS delivered). Once the user
        #     has enough real drafts, padding with weaker reduced-context ones
        #     adds no value, so we stop.
        #   - full drafts that landed late are always served (they're real
        #     appeals we generated, just not streamed in time).
        # Served speculative rows are flipped to non-speculative so they persist
        # as real appeals and later calls serve them as existing. Runs before
        # the zero/underdelivery logging and the done frame so the counts stay
        # truthful. Dedup is by normalized raw text via served_keys.
        ENOUGH_APPEALS = cls.ENOUGH_APPEALS
        MINI_LEVELS = {
            *SPECULATIVE_CONTEXT_LEVELS,
            CONTEXT_LEVEL_TIER1_SHED,
            CONTEXT_LEVEL_TIER2_SHED,
        }
        reconciled = 0
        # Subset of `reconciled` that actually came from the speculative
        # precompute. The rest are late live drafts, which the reserve counters
        # must not claim.
        reconciled_from_reserve = 0
        # Snapshot the LIVE delivery count before the reserve tops it up. The
        # zero-appeal diagnostics below key off this, not the final total:
        # otherwise a run where generation produced nothing but the reserve
        # covered it reports new=3 and neither branch fires, so a total backend
        # failure becomes invisible to alerting -- exactly the incident this
        # instrumentation exists to catch. reserve_served is subtracted for the
        # same reason: rows the early fallback shipped mid-flight are already in
        # `new`, and counting them as live delivery would hide the same failure.
        live_new = new - reserve_served
        # Best-effort, like the synthesis block above: a failure here (e.g. a
        # promotion asave hitting DB lock contention -- save_appeal wraps its own
        # asave for exactly this reason) must NOT propagate, or the done frame
        # never emits and an already-delivered stream ends dirty. order_by("id")
        # promotes the oldest reserve rows first (deterministic FIFO).
        try:
            # chosen=False: never hand the user their own pick back as a "new
            # appeal". In the ordinary case served_keys already covers this --
            # saved_appeal_texts above is not chosen-filtered, so a chosen row's
            # text is in the set and the dedup below skips it. This closes the
            # gap where that is NOT true: mark_proposal_chosen running in
            # another request mid-stream lands a chosen row (for an editted one,
            # text the user wrote, which was never a draft) in between that
            # query and this one, leaving it absent from served_keys.
            async for row in deliverable_candidates(
                ProposedAppeal.objects.filter(for_denial=denial, chosen=False).filter(
                    # A held-back reserve written for another state argues
                    # under that state's law: only a live row or a reserve
                    # written for the state on the row now is served...
                    Q(speculative=False)
                    | Q(built_for_state=state_on_the_row_now())
                )
                # ...and a reserve already promoted keeps its stamp.
                .exclude(served_reserve_for_another_state())
            ).order_by("id"):
                text = row.appeal_text
                if not is_real_appeal(text):
                    continue
                normalized = str(text).strip()
                key = _served_key(text)
                # Held-back rows stay held back: the reconciliation exists to
                # land drafts that no yield path saw, and these were skipped
                # on purpose, not missed.
                if key in served_keys or key in held_back_keys:
                    continue
                is_mini = bool(row.speculative) or row.context_level in MINI_LEVELS
                # Re-evaluate the threshold each iteration: serving increments new.
                if is_mini and (new + old) >= ENOUGH_APPEALS:
                    continue
                # Attribution for the reserve counters below, taken BEFORE the
                # promotion clears the flag. context_level is part of the test
                # so a reserve row promoted by an earlier run still counts as
                # precompute output; a late FULL-context draft (also served
                # here) is live generation and must not be credited to the
                # reserve, or the diagnostics would report a fallback that
                # never happened.
                from_precompute = (
                    bool(row.speculative)
                    or row.context_level in SPECULATIVE_CONTEXT_LEVELS
                )
                if row.speculative:
                    # Promote to a real appeal so it persists and later calls
                    # serve it as existing. Claimed atomically for the same
                    # reason as the early flush above: a concurrent run must not
                    # serve the same held-back draft, and the loser skips it.
                    if not await ProposedAppeal.objects.filter(
                        pk=row.pk,
                        speculative=True,
                        chosen=False,
                        built_for_state=state_on_the_row_now(),
                    ).aupdate(speculative=False):
                        continue
                    row.speculative = False
                if scoring_active and letter_quality.needs_scoring(row):
                    _start_scoring(str(row.id), text)
                row_dict = await sub_in_appeals(
                    letter_quality.with_score_fields(
                        {"id": str(row.id), "content": text}, row
                    )
                )
                yield await format_response(row_dict)
                served_keys.add(_served_key(normalized))
                new += 1
                reconciled += 1
                if from_precompute:
                    reconciled_from_reserve += 1
        except Exception:
            logger.opt(exception=True).warning(
                f"[gen_id={generation_id}] end-of-flow reconciliation failed for "
                f"denial {denial_id}; already-delivered appeals are unaffected"
            )
        if reconciled:
            logger.info(
                f"[gen_id={generation_id}] end-of-flow reconciliation served "
                f"{reconciled} late/held-back appeal(s) for denial {denial_id} "
                f"(new={new}, old={old})"
            )

        # Every model call is drained by now (the streaming loop above pulls the
        # generators to exhaustion), so this picks up the attempts that were
        # still lazily chained when make_appeals returned and flushed its own
        # batch. Best-effort and last: a DB hiccup here must not cost the user
        # the done frame, and make_appeals' synchronous flush already saved the
        # records that exist when a client hangs up before we get here.
        attempt_recorder = make_appeals_diag.get("attempt_recorder")
        if attempt_recorder is not None:
            written = await attempt_recorder.aflush()
            if written:
                logger.debug(
                    f"[gen_id={generation_id}] persisted {written} late model "
                    f"attempt record(s) for denial {denial_id}"
                )
            # Runts the ladder's peek rejected never reach keep(), the only
            # place `runts` was counted, so a run where EVERY model answered
            # with a runt logged runt_count=0 -- which the comment below
            # defines as "models were silent". Add the peek rejections so the
            # diag line says what the attempt rows say.
            runts += sum(
                1
                for _, outcome in attempt_recorder.outcome_pairs()
                if outcome == OUTCOME_REJECTED_AT_PEEK
            )
        # runt_count=0 means models were silent; >0 means models produced only
        # undeliverable outputs (too short, or not made of words) —
        # different root causes for incident review.
        shed_tier = make_appeals_diag.get("shed_tier")
        winning_stage = make_appeals_diag.get("winning_stage")
        # Recomputed from the recorder now that every generator has been
        # drained: the string make_appeals built was a snapshot from the peek
        # phase, so on a successful run it under-reported by design. The
        # zero-appeal case was already complete, and stays so.
        if attempt_recorder is not None:
            make_appeals_diag["models_tried"] = attempt_recorder.models_tried_summary()
        models_tried = make_appeals_diag.get("models_tried") or "none"
        # Keyed on live_new (pre-reserve), so a reserve that rescued the user
        # still reports the backend failure. reserve_note says whether the user
        # was actually left empty-handed or the fallback covered it -- counting
        # both routes the reserve can take: the mid-flight deadline flush and
        # the end-of-flow reconciliation.
        from_reserve = reconciled_from_reserve + reserve_served
        # Unconditional counterpart to the start-of-generation line above: every
        # run that ends up leaning on the precompute says so exactly once,
        # including runs the zero/underdelivery branches below never fire for
        # (e.g. the live models returned one draft and the reserve filled the
        # rest). reserve_at_start distinguishes a reserve that was waiting from
        # one that landed mid-flight, which is the difference between "the
        # precompute got ahead of the user" and "it barely kept up".
        if from_reserve:
            logger.info(
                f"[gen_id={generation_id}] picked {from_reserve} appeal(s) from "
                f"the speculative reserve for denial {denial_id} "
                f"(early={reserve_served}, end_of_flow={reconciled_from_reserve}, "
                f"available_at_start={reserve_at_start}, "
                f"live_generated={live_new}, existing={old})"
            )
        elif reserve_at_start > 0:
            logger.info(
                f"[gen_id={generation_id}] did NOT need the speculative reserve "
                f"for denial {denial_id} ({reserve_at_start} row(s) still held "
                f"back, live_generated={live_new}, existing={old})"
            )
        reserve_note = (
            f" served_from_reserve={from_reserve} "
            f"(early={reserve_served}, end_of_flow={reconciled_from_reserve}) "
            f"(user was NOT left empty-handed)"
            if from_reserve
            else ""
        )
        if gen_error:
            reserve_note += f" gen_error={gen_error}"
        if live_new + old == 0:
            logger.error(
                f"APPEAL_GEN_DIAG [gen_id={generation_id}] Zero appeals "
                f"generated for denial {denial_id}, "
                f"gen_attempts={denial.gen_attempts}, runt_count={runts}, "
                f"dupe_count={dupes}, "
                f"make_appeals_s={make_appeals_seconds:.1f}, "
                f"first_model={first_model}, winning_stage={winning_stage}, "
                f"shed_tier={shed_tier}, models_tried=[{models_tried}], "
                f"{summarize_denial_context_tokens(denial)}{reserve_note}"
            )
        elif live_new == 0 and old > 0:
            logger.warning(
                f"APPEAL_GEN_DIAG [gen_id={generation_id}] No new appeals "
                f"generated for denial {denial_id} "
                f"(but {old} existing appeals found), "
                f"gen_attempts={denial.gen_attempts}, runt_count={runts}, "
                f"dupe_count={dupes}, "
                f"make_appeals_s={make_appeals_seconds:.1f}, "
                f"first_model={first_model}, winning_stage={winning_stage}, "
                f"models_tried=[{models_tried}]{reserve_note}"
            )

        # (The form_completed intake event is recorded and delivered at the
        # START of generation, above; the generation lease is what keeps the
        # journey's child from racing this run, not signal timing.)

        # Synthesis and the reconciliation above save drafts after the first
        # drain, so drain once more, bounded, right before done: a score that
        # arrives after this frame is telemetry only.
        for score_json in await _drain_score_frames(
            0.0 if superseded else letter_quality.DRAIN_SECONDS
        ):
            yield score_json

        # Explicit end-of-stream so the client knows exactly what was sent.
        # Carries the correlation id + generating-phase instrumentation so a
        # client "0 appeals" report can be joined to this server trace.
        yield json.dumps(
            {
                "type": "status",
                "phase": "done",
                "message": f"Complete: {new} new and {old} existing appeals generated",
                "new_appeals": new,
                "existing_appeals": old,
                "total_appeals": new + old,
                "generation_id": generation_id,
                "make_appeals_seconds": round(make_appeals_seconds, 1),
                "first_model": first_model or "none",
                "shed_tier": shed_tier,
                "models_tried": models_tried,
                # How many of new_appeals came from the speculative reserve
                # rather than this run's live generation (early flush +
                # end-of-flow reconciliation), so a client report can be read
                # without guessing which path filled the stream.
                "speculative_appeals": from_reserve,
            }
        ) + "\n"


def get_denial_for_action(
    denial_id: Any, email: str, semi_sekret: str
) -> Optional[Denial]:
    """Look up the denial keyed by id + hashed email + semi_sekret.

    Returns None if any field is missing/invalid or the denial doesn't
    exist. The (denial_id, hashed_email, semi_sekret) triple is the
    canonical "is this the right user touching this denial" check used
    throughout the appeal flow.
    """
    if denial_id is None or not email or not semi_sekret:
        return None
    try:
        denial_id_int = int(denial_id)
    except (TypeError, ValueError):
        return None
    return Denial.objects.filter(
        denial_id=denial_id_int,
        hashed_email=Denial.get_hashed_email(email),
        semi_sekret=semi_sekret,
    ).first()


class EscalationPacketHelper:
    """Streaming generator for the regulator/executive escalation packet.

    Produces one cover letter per recipient (state DOI, plan medical
    director, DOL EBSA for ERISA plans) and persists each draft as a
    `RegulatorEscalation` row keyed to the originating denial.
    """

    @classmethod
    async def generate_escalation_letters(cls, parameters: dict) -> AsyncIterator[str]:
        """Async generator yielding JSON payloads, mirroring AppealsBackendHelper."""
        from fighthealthinsurance.escalation_addresses import get_recipients_for_denial
        from fighthealthinsurance.generate_regulator_letter import (
            generate_regulator_letter,
        )

        denial_id_raw = parameters.get("denial_id")
        email = parameters.get("email") or ""
        semi_sekret = parameters.get("semi_sekret") or ""

        if not denial_id_raw or not email or not semi_sekret:
            yield json.dumps(
                {"type": "error", "message": "Missing denial id, email, or semi_sekret"}
            ) + "\n"
            return
        try:
            denial_id = int(denial_id_raw)
        except (TypeError, ValueError):
            yield json.dumps({"type": "error", "message": "Invalid denial id"}) + "\n"
            return

        hashed_email = Denial.get_hashed_email(email)

        yield json.dumps(
            {
                "type": "status",
                "phase": "init",
                "message": "Starting escalation letter generation...",
            }
        ) + "\n"

        # We deliberately don't ``select_related("regulator",
        # "insurance_company_obj")`` here: those tables hold ``RegexField``
        # columns whose ``from_db_value`` raises ``ValidationError`` on NULL,
        # so a LEFT JOIN against an un-matched denial blows up the whole
        # stream. ``prefetch_related("plan_source")`` is safe (separate
        # query, no JOIN) and avoids an extra round-trip when
        # ``get_recipients_for_denial`` checks ERISA likelihood.
        try:
            denial = await Denial.objects.prefetch_related("plan_source").aget(
                denial_id=denial_id,
                semi_sekret=semi_sekret,
                hashed_email=hashed_email,
            )
        except Denial.DoesNotExist:
            yield json.dumps({"type": "error", "message": "Denial not found"}) + "\n"
            return

        recipients = await database_sync_to_async(get_recipients_for_denial)(denial)
        if not recipients:
            yield json.dumps(
                {"type": "error", "message": "No regulator recipients available"}
            ) + "\n"
            return

        # Reuse any letters we've already drafted for this denial so that
        # navigating back to the page (e.g. via the "Back to all regulator
        # letters" button on the review screen, or a browser refresh) doesn't
        # burn fresh ML calls and accumulate duplicate draft rows.
        recipient_types = [r.recipient_type for r in recipients]
        existing_by_type: dict[str, RegulatorEscalation] = {}
        async for esc in RegulatorEscalation.objects.filter(
            for_denial=denial,
            hashed_email=hashed_email,
            recipient_type__in=recipient_types,
        ).order_by("-created"):
            # Keep only the most recent draft per recipient type. Stop
            # as soon as every relevant type has been covered so we don't
            # scan unbounded history.
            existing_by_type.setdefault(esc.recipient_type, esc)
            if len(existing_by_type) == len(recipient_types):
                break

        needing_generation = [
            r for r in recipients if r.recipient_type not in existing_by_type
        ]

        # ``total`` is every recipient the packet must contain, not just the
        # ones still needing an ML call, so the page can tell a full packet
        # from a partial one.
        yield json.dumps(
            {
                "type": "status",
                "phase": "generating",
                "message": (
                    f"Generating {len(needing_generation)} regulator letter(s)..."
                ),
                "total": len(recipients),
                "cached": len(existing_by_type),
                "generating": len(needing_generation),
            }
        ) + "\n"

        # Stream existing drafts first so the user sees them immediately
        # without waiting on the ML calls for the still-missing recipients.
        for recipient in recipients:
            existing = existing_by_type.get(recipient.recipient_type)
            if existing is None:
                continue
            yield json.dumps(
                {
                    "type": "letter",
                    "escalation_id": str(existing.uuid),
                    "recipient_type": existing.recipient_type,
                    "recipient_name": existing.recipient_name,
                    "recipient_address": existing.recipient_address,
                    "recipient_phone": existing.recipient_phone,
                    "recipient_url": existing.recipient_url,
                    "rationale": recipient.rationale,
                    "content": existing.letter_text,
                }
            ) + "\n"

        use_external = bool(getattr(denial, "use_external", False))

        async def _draft(recipient):
            # One recipient's failure, never the stream's: raising out of
            # here skips the done frame, and the page is left with no
            # complete flag and no count for the run.
            try:
                text = await generate_regulator_letter(
                    denial, recipient, use_external=use_external
                )
            except Exception:
                logger.opt(exception=True).warning(
                    f"escalation letter for {recipient.recipient_type} raised"
                )
                text = None
            return recipient, text

        tasks = [asyncio.create_task(_draft(r)) for r in needing_generation]
        generated = 0
        failed_names: list[str] = []
        try:
            for fut in asyncio.as_completed(tasks):
                recipient, letter_text = await fut
                if not letter_text:
                    failed_names.append(recipient.name)
                    yield json.dumps(
                        {
                            "type": "status",
                            "phase": "generating",
                            "substep": recipient.recipient_type,
                            "state": "error",
                            "message": (
                                f"Could not generate a letter for "
                                f"{recipient.name}; skipping."
                            ),
                        }
                    ) + "\n"
                    continue

                try:
                    escalation = await RegulatorEscalation.objects.acreate(
                        for_denial=denial,
                        hashed_email=hashed_email,
                        recipient_type=recipient.recipient_type,
                        recipient_name=recipient.name,
                        recipient_address=recipient.address,
                        recipient_phone=recipient.phone,
                        recipient_url=recipient.url,
                        letter_text=letter_text,
                    )
                except Exception:
                    logger.opt(exception=True).warning(
                        f"could not save the {recipient.recipient_type} letter"
                    )
                    failed_names.append(recipient.name)
                    yield json.dumps(
                        {
                            "type": "status",
                            "phase": "generating",
                            "substep": recipient.recipient_type,
                            "state": "error",
                            "message": (
                                f"Could not save the letter for "
                                f"{recipient.name}; skipping."
                            ),
                        }
                    ) + "\n"
                    continue

                yield json.dumps(
                    {
                        "type": "letter",
                        "escalation_id": str(escalation.uuid),
                        "recipient_type": recipient.recipient_type,
                        "recipient_name": recipient.name,
                        "recipient_address": recipient.address,
                        "recipient_phone": recipient.phone,
                        "recipient_url": recipient.url,
                        "rationale": recipient.rationale,
                        "content": letter_text,
                    }
                ) + "\n"
                generated += 1
        finally:
            # If the consumer disconnected mid-stream, cancel any unfinished
            # drafting tasks so we don't keep paying for ML calls nobody is
            # listening to.
            for t in tasks:
                if not t.done():
                    t.cancel()

        delivered = generated + len(existing_by_type)
        complete = delivered == len(recipients) and not failed_names
        yield json.dumps(
            {
                "type": "status",
                "phase": "done",
                "total": len(recipients),
                "generated": generated,
                "cached": len(existing_by_type),
                "failed": len(failed_names),
                "failed_names": failed_names,
                "complete": complete,
                "message": (
                    f"All {len(recipients)} regulator letter(s) are ready."
                    if complete
                    else (
                        f"{delivered} of {len(recipients)} regulator letters are "
                        f"ready. We could not write the rest."
                    )
                ),
            }
        ) + "\n"

    @classmethod
    def save_chosen_letter(
        cls,
        escalation_uuid: str,
        denial_id: int,
        email: str,
        semi_sekret: str,
        letter_text: str,
    ) -> Optional["RegulatorEscalation"]:
        """Persist the user's edited regulator letter as the chosen draft."""
        denial = get_denial_for_action(denial_id, email, semi_sekret)
        if denial is None:
            return None
        try:
            escalation = RegulatorEscalation.objects.get(
                uuid=escalation_uuid, for_denial=denial
            )
        except RegulatorEscalation.DoesNotExist:
            return None
        was_edited = letter_text.strip() != (escalation.letter_text or "").strip()
        escalation.letter_text = letter_text
        escalation.chosen = True
        escalation.edited = escalation.edited or was_edited
        escalation.save()
        return escalation
