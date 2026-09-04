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
from django.core.files import File
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, close_old_connections, transaction


from django.db.models import F, Q, QuerySet
from django.db.models.functions import Length
from django.forms import Form
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import escape as html_escape

import asyncstdlib as a
import ray
import uszipcode
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async

from fighthealthinsurance import generation_lease
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
    CONTEXT_LEVEL_SPECULATIVE,
    CONTEXT_LEVEL_SYNTHESIZED,
    CONTEXT_LEVEL_TIER1_SHED,
    CONTEXT_LEVEL_TIER2_SHED,
    SPECULATIVE_CONTEXT_LEVELS,
    summarize_denial_context_tokens,
)
from fighthealthinsurance.denial_context import load_qa, merge_plan_context, merge_qa
from fighthealthinsurance.denials.algorithmic_review_detector import (
    detect_algorithmic_review_terms,
    render_template_blocks,
)
from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.medical_code_extractor import (
    extract_icd10_codes,
    extract_procedure_codes,
)
from fighthealthinsurance.ml.bad_output_utils import strip_boilerplate_service
from fighthealthinsurance.reliability_events import capture_reliability_event
from fighthealthinsurance.form_utils import *
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.ml.ml_appeal_context_helper import MLAppealContextHelper
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
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


@dataclass
class NextStepInfo:
    outside_help_details: list[Tuple[str, str]]
    combined_form: Form
    semi_sekret: str
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


def mark_proposal_chosen(
    denial: Denial,
    appeal_text: str,
    editted: bool = False,
    proposed_appeal_id: Optional[int] = None,
) -> ProposedAppeal:
    """Create a chosen=True ProposedAppeal, copying model_name from the original
    generated row when we can identify which draft was picked.

    Lookup precedence:
      1. proposed_appeal_id (preferred) - the id returned by save_appeal in
         the streaming JSON frame. Survives sub_in_appeals rewriting the
         displayed text (e.g. {claim_id} -> "ABC123") since it does not
         depend on string equality.
      2. exact appeal_text match against a chosen=False row for the same
         denial. Useful as a fallback when the frontend did not echo the id
         (older clients, share-appeal flow).
      3. sole-draft inference - when every draft generated for the denial
         came from one model, the pick necessarily did too (even after
         edits or sub_in_appeals rewrites). Skipped for editted=True calls:
         the share-appeal flow submits arbitrary text that may never have
         been a draft.
      4. model_name=None - the user edited the draft heavily and multiple
         models were in play, or the proposal predates the model_name field.
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
        original = ProposedAppeal.objects.filter(
            id=proposed_appeal_id,
            for_denial=denial,
            chosen=False,
            speculative=False,
        ).first()
    if original is None:
        original = (
            ProposedAppeal.objects.filter(
                for_denial=denial,
                appeal_text=appeal_text,
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
    elif not editted:
        inferred = ProposedAppeal.sole_draft_attribution(denial.denial_id)
        if inferred is not None:
            model_name, synthesized, context_level = inferred
    pa = ProposedAppeal(
        appeal_text=appeal_text,
        for_denial=denial,
        chosen=True,
        editted=editted,
        model_name=model_name,
        synthesized=synthesized,
        context_level=context_level,
    )
    pa.save()
    return pa


class ChooseAppealHelper:
    @classmethod
    def choose_appeal(
        cls,
        denial_id: str,
        appeal_text: str,
        email: str,
        semi_sekret: str,
        proposed_appeal_id: Optional[int] = None,
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
        mark_proposal_chosen(denial, appeal_text, proposed_appeal_id=proposed_appeal_id)
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
        """Build question forms from denial types and generated questions (shared logic)."""
        from django import forms

        question_forms = []
        prof_pov = denial.professional_to_finish

        # Add forms for each denial type
        for dt in denial.denial_type.all():
            new_form = dt.get_form()
            if new_form is not None:
                new_form = new_form(
                    initial={"medical_reason": dt.appeal_text}, prof_pov=prof_pov
                )
                question_forms.append(new_form)

        # Add generated questions form if available
        if denial.generated_questions:
            generated_questions: list[tuple[str, str]] = denial.generated_questions

            class AppealQuestionsForm(forms.Form):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    for i, (question, initial_answer) in enumerate(
                        generated_questions, 1
                    ):
                        field_name = f"appeal_generated_question_{i}"
                        self.fields[field_name] = forms.CharField(
                            label=question,
                            help_text=question,
                            required=False,
                            initial=initial_answer,
                        )

            question_forms.append(AppealQuestionsForm())

        return question_forms

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

        # Snapshot dx/px before the user's confirmed values overwrite them, so
        # the round-2 speculative dispatch below can tell "user corrected the
        # extraction" from "user accepted it as-is".
        prior_procedure = denial.procedure
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
            denial.state = your_state
            changed_fields.add("state")
        if denial_date is not None:
            denial.denial_date = denial_date
            changed_fields.add("denial_date")
            if "denial date" not in existing_answers:
                existing_answers["denial date"] = str(denial_date)
        if date_of_service is not None:
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
                denial, prior_procedure, prior_diagnosis
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"speculative appeals[dx_px_confirmed]: dispatch failed for "
                f"denial {denial_id}"
            )

        # Generate questions for better appeal creation if they don't exist yet
        try:
            if not denial.generated_questions or len(denial.generated_questions) == 0:
                logger.debug("Generating appeal questions")
                async_to_sync(DenialCreatorHelper.generate_appeal_questions)(
                    denial_id=denial.denial_id
                )
                denial.refresh_from_db()
        except Exception as e:
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
                pharmacy_coupon_suggestion=pharmacy_coupon_suggestion,
                financial_assistance=financial_assistance,
            )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Unexpected error building query {denial_id}: {e}"
            )
            combined_form = magic_combined_form(question_forms, {})
            return NextStepInfo(
                outside_help_details=outside_help_details,
                combined_form=combined_form,
                semi_sekret=semi_sekret,
                pharmacy_coupon_suggestion=pharmacy_coupon_suggestion,
                financial_assistance=financial_assistance,
            )

    @classmethod
    def _maybe_dispatch_confirmed_speculative(
        cls,
        denial: "Denial",
        prior_procedure: Optional[str],
        prior_diagnosis: Optional[str],
    ) -> None:
        """Kick off the round-2 (confirmed-context) speculative precompute.

        Called after ``find_next_steps`` saves the user's confirmed
        procedure/diagnosis. Fires when a dx or px is present AND either the
        user actually changed a value (their correction supersedes any earlier
        reserve, including a previous confirmed-context one) or no
        confirmed-context reserve exists yet. Re-POSTs of the categorize-review
        form with unchanged values therefore no-op here, and the helper's own
        guards (skip when live appeals exist, replace only after new drafts
        persist) bound the rest.
        """
        confirmed_procedure = (denial.procedure or "").strip()
        confirmed_diagnosis = (denial.diagnosis or "").strip()
        if not confirmed_procedure and not confirmed_diagnosis:
            logger.debug(
                f"speculative appeals[dx_px_confirmed]: denial "
                f"{denial.denial_id} confirmed without procedure or diagnosis; "
                f"nothing to refresh with"
            )
            return
        values_changed = (prior_procedure or "").strip() != confirmed_procedure or (
            prior_diagnosis or ""
        ).strip() != confirmed_diagnosis
        if not values_changed:
            from fighthealthinsurance.context_utils import (
                CONTEXT_LEVEL_SPECULATIVE_CONFIRMED,
            )

            # Values unchanged: only fire if this is the FIRST confirmation
            # (no confirmed-context rows anywhere -- held-back or promoted).
            # The create-time reserve almost always predates extraction, so
            # "accepted as-is" still deserves one refresh with dx/px in the
            # prompt; a second identical POST does not.
            if ProposedAppeal.objects.filter(
                for_denial=denial,
                context_level=CONTEXT_LEVEL_SPECULATIVE_CONFIRMED,
            ).exists():
                logger.debug(
                    f"speculative appeals[dx_px_confirmed]: denial "
                    f"{denial.denial_id} unchanged dx/px and confirmed-context "
                    f"reserve already exists; skipping"
                )
                return

        from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
            dispatch_speculative_appeals,
        )

        # force carries "the values changed" into the helper: a stale
        # confirmed-context reserve from an earlier confirmation must not
        # veto the refresh there.
        dispatch_speculative_appeals(
            denial.denial_id,
            force=values_changed,
            trigger="dx_px_confirmed",
            confirmed_context=True,
        )

    @classmethod
    def find_next_steps_for_denial(cls, denial: "Denial", email: str) -> "NextStepInfo":
        """
        Simplified version of find_next_steps for GET requests (back navigation).
        Returns the outside_help info without modifying the denial.
        """
        # Use shared helpers for outside help details and question forms
        outside_help_details = cls._get_outside_help_details(denial)
        question_forms = cls._build_question_forms(denial)
        combined_form = magic_combined_form(question_forms, {})
        return NextStepInfo(
            outside_help_details=outside_help_details,
            combined_form=combined_form,
            semi_sekret=denial.semi_sekret,
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
    async def generate_appeal_questions(cls, denial_id: int) -> List[Tuple[str, str]]:
        """
        Generate a list of questions that could help craft a better appeal for
        this specific denial. The questions will be stored in the denial object's
        generated_questions field as tuples of (question, answer).
        Also generates citations in a non-blocking manner.
        This is NOT SPECULATIVE.
        Args:
            denial_id: The ID of the denial to generate questions for

        Returns:
            A list of (question, answer) tuples to help with appeal creation
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        if not denial:
            logger.warning(f"Could not find denial with ID {denial_id}")
            return []

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
                timeout=20,
            )

            # Store the generated questions in the denial object
            await Denial.objects.filter(denial_id=denial_id).aupdate(
                generated_questions=questions
            )

            logger.debug(f"Generated {len(questions)} questions for denial {denial_id}")
            return questions
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to generate questions for denial {denial_id}: {e}"
            )
            return []

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

        Best-effort: a failure here must not break denial creation/update, so
        the caller wraps this. The in-memory instance is cleared too, since it
        flows on through the rest of the request.
        """
        deleted, _ = ProposedAppeal.objects.filter(
            for_denial=denial, speculative=True
        ).delete()
        Denial.objects.filter(denial_id=denial.denial_id).update(
            denial_text_summary=None, candidate_denial_text_summary=None
        )
        denial.denial_text_summary = None
        denial.candidate_denial_text_summary = None
        logger.info(
            f"Denial {denial.denial_id} text replaced; invalidated "
            f"{deleted} held-back speculative appeal(s) and both cached "
            f"denial-text summaries"
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
            # Directly update denial object fields instead of using denial.update()
            denial.denial_text = denial_text
            denial.hashed_email = hashed_email
            denial.use_external = use_external_models
            denial.raw_email = possible_email
            # Guarded like every other optional field here: the denial form
            # has no health_history field, so this path is ALWAYS called with
            # health_history=None -- unguarded, a user who went back to edit
            # their denial letter lost their previously-entered history.
            if health_history is not None:
                denial.health_history = health_history

            # Only update these fields if they're provided
            if creating_professional is not None:
                denial.creating_professional = creating_professional
            if primary_professional is not None:
                denial.primary_professional = primary_professional
            if patient_user is not None:
                denial.patient_user = patient_user
            if insurance_company is not None:
                denial.insurance_company = insurance_company
            if insurance_company_obj is not None:
                denial.insurance_company_obj = insurance_company_obj
            if insurance_plan_obj is not None:
                denial.insurance_plan_obj = insurance_plan_obj
            if patient_visible is not None:
                denial.patient_visible = patient_visible
            if microsite_slug is not None:
                denial.microsite_slug = microsite_slug
            if referral_source is not None:
                denial.referral_source = referral_source
            if referral_source_details is not None:
                denial.referral_source_details = referral_source_details

            # Update tracking info if provided
            if tracking_info:
                tracking_info.update_model_fields(denial)

            denial.save()

        if possible_email is not None:
            schedule_follow_ups(possible_email, denial)
        your_state = None
        if zip is not None and zip != "":
            try:
                your_state = cls.zip_engine.by_zipcode(zip).state
                denial.your_state = your_state
            except Exception as e:
                # Default to no state - zip lookup can fail for invalid/unknown zips
                logger.debug(f"Zip code lookup failed for {zip}: {e}")
                your_state = None
            # ZIP3 is HIPAA Safe Harbor de-identified, so it's safe to keep on
            # the row; UCREnrichmentHelper.resolve_geographic_area uses it.
            # Persist alongside `your_state` so neither field is silently
            # dropped on update paths that don't otherwise call save().
            denial.service_zip = zip[:3]
            denial.save(update_fields=["service_zip", "your_state"])
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
                denial.save()

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

        return cls._update_denial(
            denial=denial, health_history=health_history, plan_documents=plan_documents
        )

    @classmethod
    async def extract_entity(cls, denial_id: int) -> AsyncIterator[str]:
        """
        Perform entity extraction on a given denial id
        """

        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        if (
            denial.diagnosis
            or denial.extract_procedure_diagnosis_finished
            or denial.procedure
        ):
            logger.debug(f"extract_entity({denial_id}): skipping, already done")
            # Regulator matching is cheap (a handful of regexes), idempotent,
            # and independent of the procedure/diagnosis extraction this gate
            # protects — run it even when the denial was manually populated or
            # extraction already finished, so those denials still get
            # regulator contact info.
            try:
                await cls.extract_set_regulator(denial_id)
            except Exception:
                logger.opt(exception=True).warning(
                    f"extract_set_regulator failed for denial {denial_id}"
                )
            return
        # Bound persistent extraction failures: extract_entity runs once per
        # WebSocket connection (websockets.StreamingEntityBackend.receive)
        # with no upstream rate-limit, so without a cap a denial whose LLM
        # extraction reliably fails would re-run extraction (and re-fire
        # the PubMed/citation cache warmers) on every reconnect. The
        # counter is bumped in extract_set_denial_and_diagnosis's except
        # path via F() so the increment is race-safe.
        if (denial.extract_attempts or 0) >= 3:
            logger.warning(
                f"extract_entity({denial_id}): skipping LLM extraction, "
                f"extract_attempts={denial.extract_attempts} exhausted"
            )
            # The counter measures procedure/diagnosis LLM failures only;
            # regulator matching is a handful of regexes with no LLM in the
            # loop, so run it anyway -- same reasoning as the already-done
            # early exit above. Also yield a completion frame so the client's
            # status list doesn't present an instant, empty close as success
            # with no output at all.
            try:
                await cls.extract_set_regulator(denial_id)
                yield "regulator"
            except Exception:
                logger.opt(exception=True).warning(
                    f"extract_set_regulator failed for denial {denial_id}"
                )
            yield "Extraction complete"
            return

        # Define a wrapper function that returns both the name and result
        async def named_task(awaitable: Awaitable[Any], name: str) -> tuple[str, Any]:
            try:
                result = await awaitable
                return name, result
            except Exception as e:
                logger.opt(exception=True).warning(f"Failed in task {name}: {e}")
                return name, None

        # Best effort extractions
        optional_awaitables: list[Coroutine[Any, Any, tuple[str, Any]]] = [
            named_task(cls.extract_set_fax_number(denial_id), "fax"),
            named_task(
                cls.extract_set_insurance_company(denial_id), "insurance company"
            ),
            named_task(
                cls.match_insurance_plan_from_regex(denial_id), "insurance plan"
            ),
            named_task(cls.extract_set_plan_id(denial_id), "plan id"),
            named_task(cls.extract_set_claim_id(denial_id), "claim id"),
            named_task(cls.extract_set_date_of_service(denial_id), "date of service"),
            named_task(cls.extract_set_regulator(denial_id), "regulator"),
            named_task(
                MLPlanDocHelper.generate_plan_documents_summary(denial_id),
                "plan document summary",
            ),
        ]

        required_awaitables: list[Coroutine[Any, Any, tuple[str, Any]]] = [
            # Denial type depends on denial and diagnosis
            named_task(cls.extract_set_denial_and_diagnosis(denial_id), "diagnosis"),
            named_task(cls.extract_set_denialtype(denial_id), "type of denial"),
        ]

        logger.debug(
            f"extract_entity({denial_id}): {len(optional_awaitables)} optional + "
            f"{len(required_awaitables)} required tasks"
        )
        try:
            async for item in execute_critical_optional_fireandforget(
                optional=optional_awaitables,
                required=required_awaitables,
                fire_and_forget=[cls._maybe_dispatch_ucr(denial_id)],
                done_record=("Extraction complete", None),
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
                if item:
                    yield item[0]
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error during extraction for denial {denial_id}: {e}"
            )

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
    async def extract_set_denial_and_diagnosis(cls, denial_id: int):
        """
        Asynchronously extracts procedure and diagnosis from a denial's text and updates the denial record.

        Attempts to extract the procedure and diagnosis fields using the appeal generator. Updates the denial with the extracted values and marks extraction as finished, regardless of success. If extraction is successful or existing values are present, triggers background tasks to search for related PubMed articles, prefetch ClinicalTrials.gov matches, and build speculative context. All background searches are fire-and-forget with their own timeouts and never block the caller.
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
            for field, value in user_facing.items():
                updated = await (
                    Denial.objects.filter(denial_id=denial_id)
                    .filter(Q(**{f"{field}__isnull": True}) | Q(**{field: ""}))
                    .aupdate(**{field: value})
                )
                if not updated:
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

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract procedure and diagnosis for denial {denial_id}: {e}"
            )
            # Leave extract_procedure_diagnosis_finished as False so a
            # subsequent extract_entity call can re-attempt extraction on
            # transient failures. Bump extract_attempts atomically (F()
            # makes concurrent-reconnect increments race-safe) so
            # extract_entity's gate stops retrying after 3 failures.
            try:
                await Denial.objects.filter(denial_id=denial_id).aupdate(
                    extract_attempts=F("extract_attempts") + 1
                )
            except Exception as inner:
                logger.opt(exception=True).debug(
                    f"Failed to bump extract_attempts for denial {denial_id}: {inner}"
                )

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
    async def extract_set_insurance_company(cls, denial_id):
        """Extract insurance company name from denial text and match to structured models.

        Once a company is matched, propagates the company's known appeal-routing
        info (fax number) onto the denial when the denial doesn't already have
        one - this means downstream code (PDF cover sheet, fax send) can use
        Anthem/UHC/etc.'s published appeals fax even if the denial letter
        itself didn't include it.
        """
        from fighthealthinsurance.models import InsuranceCompany, InsurancePlan

        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        insurance_company = None
        try:
            insurance_company = await appealGenerator.get_insurance_company(
                denial_text=denial.denial_text
            )

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

            update_fields: dict[str, Any] = {}
            if resolved_name:
                update_fields["insurance_company"] = resolved_name
            if matched_company:
                update_fields["insurance_company_obj"] = matched_company
                logger.debug(f"Matched to structured company: {matched_company.name}")
            if matched_plan:
                update_fields["insurance_plan_obj"] = matched_plan
                logger.debug(f"Matched to structured plan: {matched_plan}")

            # Propagate the known appeal fax number from the matched plan/company
            # onto the denial only if the denial doesn't already have one. We
            # do NOT overwrite a fax number that came directly from the denial
            # letter or plan documents. Use a single conditional ``aupdate``
            # so the read+write is atomic - extract_set_fax_number runs
            # concurrently and could otherwise write between our read and
            # write.
            propagated_fax = None
            if matched_plan and matched_plan.appeal_fax_number:
                propagated_fax = matched_plan.appeal_fax_number
            elif matched_company and matched_company.appeal_fax_number:
                propagated_fax = matched_company.appeal_fax_number

            if update_fields:
                await Denial.objects.filter(denial_id=denial_id).aupdate(
                    **update_fields
                )
                logger.debug(
                    f"Successfully extracted insurance company: {resolved_name}"
                )

            if propagated_fax:
                rows_updated = (
                    await Denial.objects.filter(denial_id=denial_id)
                    .filter(Q(appeal_fax_number__isnull=True) | Q(appeal_fax_number=""))
                    .aupdate(appeal_fax_number=propagated_fax)
                )
                if rows_updated:
                    logger.debug(
                        f"Propagated appeal_fax_number {propagated_fax} from carrier"
                    )

            return resolved_name
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract insurance company for denial {denial_id}: {e}"
            )
        return None

    @classmethod
    async def extract_set_plan_id(cls, denial_id):
        """Extract plan ID from denial text"""
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        plan_id = None
        try:
            # Extract plan ID - could be in various formats (alphanumeric)
            plan_id = await appealGenerator.get_plan_id(denial_text=denial.denial_text)

            # Validate that the extracted value looks like a real identifier
            from fighthealthinsurance.generate_appeal import is_plausible_identifier

            if plan_id is not None and is_plausible_identifier(plan_id):
                # Use aupdate to directly update the field at the database level
                await Denial.objects.filter(denial_id=denial_id).aupdate(
                    plan_id=plan_id
                )
                logger.debug(f"Successfully extracted plan ID: {plan_id}")
                return plan_id
            else:
                logger.debug(f"Rejected plan ID extraction: {plan_id}")
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract plan ID for denial {denial_id}: {e}"
            )
        return None

    @classmethod
    async def match_insurance_plan_from_regex(cls, denial_id):
        """
        Match denial to a specific insurance plan using regex patterns.
        This helps identify state-specific plans like "Anthem Medicaid California" vs "Anthem Medicaid New York".
        """
        from fighthealthinsurance.models import InsurancePlan

        # select_related caches both FKs so the insurance_*_obj reads below
        # stay async-safe (a lazy read would raise SynchronousOnlyOperation,
        # silently eaten by the except blocks).
        denial = await Denial.objects.select_related(
            "insurance_plan_obj", "insurance_company_obj"
        ).aget(denial_id=denial_id)

        try:
            # Only proceed if we don't already have a plan matched
            if denial.insurance_plan_obj:
                logger.debug(f"Denial {denial_id} already has matched plan, skipping")
                return denial.insurance_plan_obj

            denial_text = denial.denial_text

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

                            # Update both plan and company if not already set
                            update_fields: dict[str, Any] = {"insurance_plan_obj": plan}
                            if not denial.insurance_company_obj:
                                update_fields["insurance_company_obj"] = (
                                    plan.insurance_company
                                )

                            await Denial.objects.filter(denial_id=denial_id).aupdate(
                                **update_fields
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
    async def extract_set_claim_id(cls, denial_id):
        """Extract claim ID from denial text"""
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        claim_id = None
        try:
            claim_id = await appealGenerator.get_claim_id(
                denial_text=denial.denial_text
            )

            # Validate that the extracted value looks like a real identifier
            from fighthealthinsurance.generate_appeal import is_plausible_identifier

            if claim_id is not None and is_plausible_identifier(claim_id):
                # Use aupdate to directly update the field at the database level
                await Denial.objects.filter(denial_id=denial_id).aupdate(
                    claim_id=claim_id
                )
                logger.debug(f"Successfully extracted claim ID: {claim_id}")
                return claim_id
            else:
                logger.debug(f"Rejected claim ID extraction: {claim_id}")
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract claim ID for denial {denial_id}: {e}"
            )
        return None

    @classmethod
    async def extract_set_date_of_service(cls, denial_id):
        """Extract date of service from denial text"""
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        date_of_service = None
        try:
            date_of_service = await appealGenerator.get_date_of_service(
                denial_text=denial.denial_text
            )

            # Validate date of service
            if date_of_service is not None:
                # Use aupdate to directly update at the database level
                await Denial.objects.filter(denial_id=denial_id).aupdate(
                    date_of_service=date_of_service
                )
                logger.debug(
                    f"Successfully extracted date of service: {date_of_service}"
                )
                return date_of_service
            else:
                logger.debug(f"No date of service found")
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract date of service for denial {denial_id}: {e}"
            )
        return None

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

        # First try to extract from denial text
        try:
            appeal_fax_number = await appealGenerator.get_fax_number(
                denial_text=denial_text
            )
        except Exception as e:
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

        # Final fallback: if we still don't have a fax number but we matched a
        # carrier (insurance_company_obj or insurance_plan_obj), use that
        # carrier's published appeal fax. This is a last resort and isn't
        # validated against source text - it's the carrier's own data.
        # Re-read the denial under a fresh query in case a concurrent task
        # (extract_set_insurance_company) has just propagated a fax onto it -
        # we never want to overwrite an already-stored value here. We avoid
        # ``select_related`` because the joined regex columns trigger
        # RegexField.from_db_value on NULL values and raise ValidationError.
        if appeal_fax_number is None:
            current = (
                await Denial.objects.filter(denial_id=denial_id)
                .values(
                    "appeal_fax_number",
                    "insurance_company_obj_id",
                    "insurance_plan_obj_id",
                )
                .afirst()
            )
            if current is not None:
                if current["appeal_fax_number"]:
                    return current["appeal_fax_number"]
                if current["insurance_plan_obj_id"]:
                    plan_fax = (
                        await InsurancePlan.objects.filter(
                            id=current["insurance_plan_obj_id"]
                        )
                        .values_list("appeal_fax_number", flat=True)
                        .afirst()
                    )
                    if plan_fax:
                        appeal_fax_number = plan_fax
                        logger.debug(
                            f"Using plan-published appeal fax {appeal_fax_number}"
                        )
                if appeal_fax_number is None and current["insurance_company_obj_id"]:
                    company_fax = (
                        await InsuranceCompany.objects.filter(
                            id=current["insurance_company_obj_id"]
                        )
                        .values_list("appeal_fax_number", flat=True)
                        .afirst()
                    )
                    if company_fax:
                        appeal_fax_number = company_fax
                        logger.debug(
                            f"Using carrier-published appeal fax {appeal_fax_number}"
                        )

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
        return None

    @classmethod
    async def extract_set_regulator(cls, denial_id):
        """Match the denial text against known regulators and store the match.

        Populates ``Denial.regulator`` so downstream flows (outside help,
        the escalation packet's ERISA detection) can surface the right
        regulator along with its complaint phone number.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        if denial.regulator_id is not None:
            return
        regulators = await cls.regex_denial_processor.get_regulator(
            denial.denial_text or ""
        )
        if regulators:
            # First match wins; the seeded regexes are mutually specific.
            await Denial.objects.filter(
                denial_id=denial_id, regulator__isnull=True
            ).aupdate(regulator=regulators[0])

    @classmethod
    async def extract_set_denialtype(cls, denial_id):
        # Try and guess at the denial types
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        denial_types = await cls.regex_denial_processor.get_denialtype(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )
        logger.debug(
            f"extract_set_denialtype({denial_id}): processing {len(denial_types)} types"
        )
        for dt in denial_types:
            try:
                await DenialTypesRelation.objects.acreate(
                    denial=denial, denial_type=dt, src=await cls.regex_src()
                )
            except Exception as e:
                # Can fail if relation already exists (duplicate)
                logger.opt(exception=True).debug(f"Failed setting denial type: {e}")

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
        )

    @classmethod
    def _update_denial(
        cls,
        denial,
        health_history=None,
        plan_documents=None,
        include_provided_health_history_in_appeal=None,
        health_history_anonymized=None,
    ):
        if plan_documents is not None:
            for plan_document in plan_documents:
                pd = PlanDocuments.objects.create(
                    plan_document_enc=plan_document, denial=denial
                )
                pd.save()

        if health_history is not None:
            denial.health_history = health_history
        if include_provided_health_history_in_appeal is not None:
            denial.include_provided_health_history_in_appeal = (
                include_provided_health_history_in_appeal
            )
        if health_history_anonymized is not None:
            denial.health_history_anonymized = health_history_anonymized
        denial.save()
        # Durable intake journey (dark until TEMPORAL_INTAKE_JOURNEY_ENABLED):
        # the journey observes the funnel from the first substantive step; it
        # must never be able to break the user-facing flow, so the dispatcher
        # swallows every failure. Opt-in for the nudge = store_raw_email,
        # observable here as a retained raw_email.
        from fighthealthinsurance.temporal_client import dispatch_intake_started

        dispatch_intake_started(
            denial.hashed_email,
            str(denial.uuid),
            bool((denial.raw_email or "").strip()),
        )
        # Return the current the state
        return cls.format_denial_response_info(denial)

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
        # in; the interactive path takes one by stealing below. Draft inserts
        # are FENCED on it only for background runs -- a human is the
        # preemptor, and two tabs of the same human are out of scope
        # (approved design) -- while interactive runs use it to keep the
        # lease alive across a long stream and to release it at the end.
        lease_epoch: Optional[int] = parameters.get("_lease_epoch")
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

        # Yield status: starting
        yield json.dumps(
            {
                "type": "status",
                "phase": "init",
                "message": "Starting appeal generation...",
                "generation_id": generation_id,
            }
        ) + "\n"

        # Get the current info (e.g. denial).
        await asyncio.sleep(0)
        denial_query = Denial.objects.filter(
            denial_id=denial_id, semi_sekret=semi_sekret, hashed_email=hashed_email
        ).select_related(
            "patient_user",
            "patient_user__user",
            "domain",
            "primary_professional",
            "primary_professional__user",
        )
        denial = await denial_query.aget()
        if not background:
            # A live human outranks any background generator: STEAL the
            # denial's generation lease so a journey attempt in flight sees
            # the epoch move and stops quietly, and one arriving inside the
            # TTL backs off. One UPDATE; expiry is the release. Never let a
            # lease hiccup break the interactive flow (external review).
            try:
                stolen = await generation_lease.aacquire(
                    denial,
                    holder=generation_lease.new_holder("interactive"),
                    steal=True,
                )
                lease_epoch = stolen.epoch
                lease_ref["denial"] = denial
                lease_ref["epoch"] = stolen.epoch
            except Exception:
                # Never let a lease hiccup stop a human: run un-leased.
                logger.opt(exception=True).warning(
                    f"generation lease steal failed for denial {denial_id}"
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
            content = appeal["content"]
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
                if (
                    denial.professional_to_finish
                    and denial.primary_professional is not None
                ):
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
                    f"Error fetching patient name for denial sub "
                    f"{denial.denial_id}: {e}"
                )
            try:
                if denial and denial.primary_professional is not None:
                    subs["[Professional Name]"] = (
                        denial.primary_professional.get_full_name()
                    )
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
                    f"Error fetching domain address for denial sub "
                    f"{denial.denial_id}: {e}"
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
            appeal["content"] = content
            return appeal

        # If we've had a timeout on the initial call and we're on round 2
        # we should fetch the existing appeals from the previous round if present.
        # Exclude speculative rows: those are the background precompute held in
        # reserve and are served ONLY as a fallback below, not as normal
        # existing appeals.
        existing_appeals = ProposedAppeal.objects.filter(
            for_denial=denial, speculative=False
        ).all()
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
        old = 0
        new = 0
        async for appeal in existing_appeals:
            # Enforce the deliverability rules on previously-saved appeals too:
            # the DB may hold short or wordless drafts saved before those
            # checks existed (or by paths that skipped the filter), and we must
            # not re-deliver them.
            if is_real_appeal(appeal.appeal_text):
                key = _served_key(appeal.appeal_text)
                if key in served_keys:
                    # Legacy duplicate rows (NULL fingerprints, equivalent
                    # normalized text) are one draft to the user: stream
                    # the first, skip its twins, and don't count them in
                    # `old` (review).
                    logger.debug(f"Skipping duplicate existing appeal {appeal}")
                    continue
                old = old + 1
                logger.debug(f"Found existing appeal {appeal}, yielding")
                served_keys.add(key)
                existing_appeal_dict = await sub_in_appeals(
                    {"id": str(appeal.id), "content": appeal.appeal_text}
                )
                yield await format_response(existing_appeal_dict)
            elif appeal.appeal_text is not None and str(appeal.appeal_text).strip():
                warn_unusable_appeal(
                    appeal.appeal_text,
                    f"saved appeal id={appeal.id} for denial {denial_id}",
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
                ProposedAppeal.objects.filter(for_denial=denial, speculative=True)
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
                    ProposedAppeal.objects.filter(
                        for_denial=denial, speculative=True, chosen=False
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
                        pk=row.pk, speculative=True, chosen=False
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
                    row_dict = await sub_in_appeals(
                        {"id": str(row.id), "content": row.appeal_text}
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
        if medical_context:
            merge_qa(
                denial,
                {"medical_context": " ".join(sorted(medical_context))},
                source="appeal_gen_form",
            )
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
            nonlocal first_model
            appeal_text = item.text
            model_name = item.model_name
            if first_model is None and model_name:
                # First deliverable draft's model — recorded for the done frame
                # and zero-appeal diagnostics so we can see which backend won.
                first_model = str(model_name)
            t = time.time()
            logger.debug(f"Saving appeal ({len(appeal_text)} chars)")
            await asyncio.sleep(0)
            id = "unknown"
            save_failed = False
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
                    # the draft insert commit together, so a run that was
                    # stolen from cannot persist a draft after the steal
                    # however far along its model call was -- and an
                    # IntegrityError here cannot poison the caller's
                    # transaction state before the recovery query below runs
                    # (external reviews). In autocommit it is just a short
                    # transaction around the insert.
                    with transaction.atomic():
                        if background and lease_epoch is not None:
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
                        await ProposedAppeal.objects.filter(
                            pk=existing.pk, speculative=True
                        ).aupdate(speculative=False)
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
                id = str(pa.id)
                if not background and lease_epoch is not None:
                    # Keep a long interactive stream inside its lease: each
                    # persisted draft pushes the expiry out, so a journey
                    # cannot join a human's run after the TTL (review).
                    await generation_lease.aextend(denial, lease_epoch)
            except generation_lease.LeaseSuperseded as e:
                # Superseded by an interactive steal: the draft is NOT
                # persisted and goes out flagged as unsaved (the existing
                # contract for a row without a durable id). The journey's
                # per-frame epoch check ends the run right after.
                save_failed = True
                logger.info(f"Draft not persisted, generation lease superseded: {e}")
            except Exception as e:
                # Still stream the draft -- the user gets their appeal even
                # when the save fails -- but tell the client the row has no
                # durable id so choose/edit affordances can be suppressed.
                save_failed = True
                logger.opt(exception=True).warning(
                    f"Failed to save proposed appeal: {e}"
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
            return result

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
            # Also a checkpoint: draining this iterator blocks on the next model
            # future, so a run that streams one draft and then stalls for
            # minutes still gets the reserve at the deadline.
            async for _spec in serve_reserve_if_stalled():
                yield _spec
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
                ProposedAppeal.objects.filter(for_denial=denial, speculative=False)
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
        served_keys.update({_served_key(s) for s in saved_appeal_texts if s})
        # Synthesis requires >=2 drafts to be meaningful: with a single
        # input, models often regurgitate it verbatim. The client dedupes
        # by content, so a verbatim copy gets silently dropped, which then
        # trips the "partial delivery" error path in appeal_fetcher.ts.
        if len(saved_appeal_texts) >= 2:
            yield json.dumps(
                {
                    "type": "status",
                    "phase": "synthesizing",
                    "message": "Synthesizing best appeal from all drafts...",
                }
            ) + "\n"
            try:
                synthesis_task = asyncio.ensure_future(
                    appealGenerator.synthesize_appeals(
                        appeal_texts=saved_appeal_texts,
                        denial_text=(
                            str(denial.denial_text) if denial.denial_text else None
                        ),
                        procedure=(str(denial.procedure) if denial.procedure else None),
                        diagnosis=(str(denial.diagnosis) if denial.diagnosis else None),
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
                            subbed = await sub_in_appeals(saved)
                            subbed["synthesized"] = "true"
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
                ProposedAppeal.objects.filter(for_denial=denial, chosen=False)
            ).order_by("id"):
                text = row.appeal_text
                if not is_real_appeal(text):
                    continue
                normalized = str(text).strip()
                if _served_key(text) in served_keys:
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
                        pk=row.pk, speculative=True, chosen=False
                    ).aupdate(speculative=False):
                        continue
                    row.speculative = False
                row_dict = await sub_in_appeals({"id": str(row.id), "content": text})
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

        # Interactive generation finished: NOW complete the intake journey
        # (fire-and-forget; dark until enabled; skipped for the journey's own
        # background child). Signalling at the END, not the start, means the
        # child generation the workflow launches sees the delivered drafts
        # and no-ops -- a backstop, never a concurrent second generator
        # (external review: the start-time signal raced this very run).
        if not background:
            from fighthealthinsurance.temporal_client import (
                asignal_intake_fire_and_forget,
            )

            await asignal_intake_fire_and_forget(str(denial.uuid), "form_completed")

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

        yield json.dumps(
            {
                "type": "status",
                "phase": "generating",
                "message": (
                    f"Generating {len(needing_generation)} regulator letter(s)..."
                ),
                "total": len(needing_generation),
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
            text = await generate_regulator_letter(
                denial, recipient, use_external=use_external
            )
            return recipient, text

        tasks = [asyncio.create_task(_draft(r)) for r in needing_generation]
        try:
            for fut in asyncio.as_completed(tasks):
                recipient, letter_text = await fut
                if not letter_text:
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
        finally:
            # If the consumer disconnected mid-stream, cancel any unfinished
            # drafting tasks so we don't keep paying for ML calls nobody is
            # listening to.
            for t in tasks:
                if not t.done():
                    t.cancel()

        yield json.dumps(
            {
                "type": "status",
                "phase": "done",
                "message": "All regulator letters generated.",
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
