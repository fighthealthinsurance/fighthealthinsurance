from urllib.parse import urlencode
import asyncstdlib as a
from asgiref.sync import sync_to_async, async_to_sync
import asyncio
import datetime
import json
from dataclasses import dataclass
from string import Template
import typing
from typing import (
    AsyncIterator,
    Awaitable,
    Any,
    Coroutine,
    Optional,
    Tuple,
    Iterable,
    List,
    Iterator,
)
from loguru import logger
from PyPDF2 import PdfMerger
import ray
import tempfile
import os
import uuid
import re
import time
from stopit.utils import TimeoutException

from django.core.files import File
from django.core.validators import validate_email
from django.forms import Form
from django.template.loader import render_to_string
from django.db.models import QuerySet
from django.urls import reverse


import uszipcode
from fighthealthinsurance.process_denial import ProcessDenialCodes
from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.form_utils import *
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.models import *
from fighthealthinsurance.utils import interleave_iterator_for_keep_alive
from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance import stripe_utils
from fhi_users.models import ProfessionalUser, UserDomain
from fhi_users import emails as fhi_emails
from .pubmed_tools import PubMedTools
from .utils import (
    check_call,
    send_fallback_email,
    fire_and_forget_in_new_threadpool,
    execute_critical_optional_fireandforget,
)


appealGenerator = AppealGenerator()


class RemoveDataHelper:
    @classmethod
    def remove_data_for_email(cls, email: str):
        hashed_email: str = Denial.get_hashed_email(email)
        Denial.objects.filter(hashed_email=hashed_email).delete()
        FollowUpSched.objects.filter(email=email).delete()
        FollowUp.objects.filter(hashed_email=hashed_email).delete()
        FaxesToSend.objects.filter(hashed_email=hashed_email).delete()


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

    def convert_to_serializable(self):
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
                await check_call(base_convert_command)
                return f"{input_path}.pdf"
            # pandoc failures are often character encoding issues
            except Exception as e:
                # try to convert if we've got txt input
                new_input_path = input_path
                if input_path.endswith(".txt"):
                    try:
                        command = [
                            "iconv",
                            "-c",
                            "-t utf8",
                            f"-o{input_path}.u8.txt",
                            input_path,
                        ]
                        await check_call(command)
                        new_input_path = f"{input_path}.u8.txt"
                    except:
                        pass
                if input_path.endswith(".html"):
                    html_command = base_convert_command
                    html_command.extend(["-thtml"])
                    try:
                        await check_call(html_command)
                        return f"{input_path}.pdf"
                    except:
                        pass
                # Try a different engine
                for engine in ["lualatex", "xelatex"]:
                    convert_command = base_convert_command
                    convert_command.extend([f"--pdf-engine={engine}"])
                    try:
                        await check_call(convert_command)
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
                    f"Rendering cover letter from string {cover_template_string} and got {cover_content[0:10]}..."
                )
            else:
                cover_content = render_to_string(
                    cover_template_path,
                    context=cover_context,
                )
                logger.debug(
                    f"Rendering cover letter from path {cover_template_path} and got {cover_content[0:10]}..."
                )
            cover_letter_file = tempfile.NamedTemporaryFile(
                suffix=".html", prefix="info_cover", mode="w+t", delete=False
            )
            logger.debug(f"Writing cover letter to {cover_letter_file.name}")
            cover_letter_file.write(cover_content)
            cover_letter_file.flush()
            files_for_fax.append(cover_letter_file.name)
            logger.debug(f"Added cover letter as {cover_letter_file.name}")

        # Appeal text
        appeal_text_file = tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=False
        )
        appeal_text_file.write(completed_appeal_text)
        appeal_text_file.flush()
        files_for_fax.append(appeal_text_file.name)
        logger.debug(f"Added appeal text as {appeal_text_file.name}")

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
            logger.debug(f"Added health history as {health_history_file.name}")

        # PubMed articles
        if pubmed_ids_parsed is not None and len(pubmed_ids_parsed) > 0:
            logger.debug(f"Processing PubMed articles: {pubmed_ids_parsed}")
            pmt = PubMedTools()
            pubmed_docs: list[PubMedArticleSummarized] = async_to_sync(
                pmt.get_articles
            )(pubmed_ids_parsed)
            logger.debug(f"Retrieved {len(pubmed_docs)} PubMed articles")
            if pubmed_docs:
                pubmed_docs_paths = [
                    x
                    for x in map(async_to_sync(pmt.article_as_pdf), pubmed_docs)
                    if x is not None
                ]
                files_for_fax.extend(pubmed_docs_paths)
                logger.debug(f"Added {len(pubmed_docs_paths)} PubMed PDFs to fax")
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


@dataclass
class FaxHelperResults:
    uuid: str
    hashed_email: str


class SendFaxHelper:
    appeal_assembly_helper = AppealAssemblyHelper()

    @classmethod
    def stage_appeal_as_fax(
        cls,
        appeal: Appeal,
        email: str,
        professional: bool = False,
    ):
        denial = appeal.for_denial
        if denial is None:
            raise Exception("No denial")
        appeal_fax_number = denial.appeal_fax_number
        hashed_email = Denial.get_hashed_email(email)
        appeal_text = appeal.appeal_text
        if not appeal_text:
            raise Exception("No appeal text")
        fts = FaxesToSend.objects.create(
            paid=True,
            pmids=appeal.pubmed_ids_json,
            hashed_email=hashed_email,
            appeal_text=appeal_text,
            email=email,
            denial_id=denial,
            # This should work but idk why it does not
            combined_document_enc=appeal.document_enc,
            destination=appeal_fax_number,
            professional=professional,
        )
        appeal.fax = fts
        appeal.save()
        # We call str on fts.uuid since it's a UUID object but when persisted it's a string
        fax_actor_ref.get.do_send_fax.remote(fts.hashed_email, str(fts.uuid))
        return FaxHelperResults(uuid=fts.uuid, hashed_email=hashed_email)

    @classmethod
    def blocking_dosend_target(cls, email) -> int:
        faxes = FaxesToSend.objects.filter(email=email, sent=False)
        c = 0
        for f in faxes:
            future = fax_actor_ref.get.do_send_fax.remote(f.hashed_email, f.uuid)
            ray.get(future)
            c = c + 1
        return c

    @classmethod
    def blocking_dosend_all(cls, count) -> int:
        faxes = FaxesToSend.objects.filter(sent=False)[0:count]
        c = 0
        for fax in faxes:
            future = fax_actor_ref.get.do_send_fax.remote(fax.hashed_email, fax.uuid)
            ray.get(future)
            c = c + 1
        return c

    @classmethod
    def resend(cls, fax_phone, uuid, hashed_email) -> bool:
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        f.destination = fax_phone
        f.save()
        future = fax_actor_ref.get.do_send_fax.remote(hashed_email, uuid)
        return True

    @classmethod
    def remote_send_fax(cls, hashed_email, uuid) -> bool:
        """Send a fax using ray non-blocking"""
        # Mark fax as to be sent just in case ray doesn't follow through
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        f.should_send = True
        f.paid = True
        f.save()
        future = fax_actor_ref.get.do_send_fax.remote(hashed_email, uuid)
        return True


class ChooseAppealHelper:
    @classmethod
    def choose_appeal(
        cls, denial_id: str, appeal_text: str, email: str, semi_sekret: str
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
        pa = ProposedAppeal(appeal_text=appeal_text, for_denial=denial, chosen=True)
        pa.save()
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


class FollowUpHelper:
    @classmethod
    def fetch_denial(
        cls, uuid: str, follow_up_semi_sekret: str, hashed_email: str, **kwargs
    ):
        denial = Denial.objects.filter(
            uuid=uuid, follow_up_semi_sekret=follow_up_semi_sekret
        ).get()
        if denial is None:
            raise Exception(
                f"Failed to find denial for {uuid} & {follow_up_semi_sekret}"
            )
        return denial

    @classmethod
    def store_follow_up_result(
        cls,
        uuid: str,
        follow_up_semi_sekret: str,
        hashed_email: str,
        user_comments: str,
        appeal_result: str,
        follow_up_again: bool,
        medicare_someone_to_help: bool = False,
        email: Optional[str] = None,
        quote: Optional[str] = None,
        name_for_quote: Optional[str] = None,
        use_quote: bool = False,
        followup_documents=[],
    ):
        denial = cls.fetch_denial(
            uuid=uuid,
            follow_up_semi_sekret=follow_up_semi_sekret,
            hashed_email=hashed_email,
        )
        # Store the follow up response returns nothing but may raise
        denial_id = denial.denial_id
        follow_up = FollowUp.objects.create(
            hashed_email=hashed_email,
            denial_id=denial,
            more_follow_up_requested=follow_up_again,
            follow_up_medicare_someone_to_help=medicare_someone_to_help,
            use_quote=use_quote,
            email=email,
            name_for_quote=name_for_quote,
            quote=quote,
        )
        # If they asked for additional follow up add a new schedule
        if follow_up_again:
            FollowUpSched.objects.create(
                email=denial.raw_email,
                denial_id=denial,
                follow_up_date=denial.date + datetime.timedelta(days=15),
            )
        for document in followup_documents:
            fd = FollowUpDocuments.objects.create(
                follow_up_document_enc=document, denial=denial, follow_up_id=follow_up
            )
            fd.save()
        denial.appeal_result = appeal_result
        denial.save()


class FindNextStepsHelper:
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
    ) -> NextStepInfo:
        hashed_email = Denial.get_hashed_email(email)
        # Update the denial
        denial = Denial.objects.filter(
            denial_id=denial_id,
            # Include the hashed e-mail so folks can't brute force denial_id
            hashed_email=hashed_email,
            semi_sekret=semi_sekret,
        ).get()

        if procedure is not None and len(procedure) < 200:
            denial.procedure = procedure
        if diagnosis is not None and len(diagnosis) < 200:
            denial.diagnosis = diagnosis
        if plan_source is not None:
            denial.plan_source.set(plan_source)
        if patient_health_history:
            denial.health_history = patient_health_history
        # Only set employer name if it's not too long
        if employer_name is not None and len(employer_name) < 300:
            denial.employer_name = employer_name
        else:
            employer_name = None
        if (
            appeal_fax_number is not None
            and len(appeal_fax_number) > 5
            and len(appeal_fax_number) < 30
        ):
            logger.debug(f"Setting appeal fax number to {appeal_fax_number}")
            Denial.objects.filter(denial_id=denial_id).update(
                appeal_fax_number=appeal_fax_number
            )
        else:
            logger.debug(f"Invalid appeal fax number {appeal_fax_number}")

        if include_provided_health_history_in_appeal is not None:
            denial.include_provided_health_history_in_appeal = (
                include_provided_health_history_in_appeal
            )

        outside_help_details = []
        state = your_state or denial.your_state

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
        if denial.regulator == Regulator.objects.filter(alt_name="ERISA").get():
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
        denial.insurance_company = insurance_company
        denial.plan_id = plan_id
        denial.claim_id = claim_id
        if denial_type_text is not None:
            denial.denial_type_text = denial_type_text
        if denial_type:
            denial.denial_type.set(denial_type)

        existing_answers: dict[str, str] = {}
        if denial.qa_context is not None:
            existing_answers = json.loads(denial.qa_context)

        if your_state:
            denial.state = your_state
        if denial_date is not None:
            denial.denial_date = denial_date
            if "denial date" not in existing_answers:
                existing_answers["denial date"] = str(denial_date)
        if date_of_service is not None:
            denial.date_of_service = date_of_service
            if "date of service" not in existing_answers:
                existing_answers["date of service"] = date_of_service
            if "date_of_service" not in existing_answers:
                existing_answers["date_of_service"] = date_of_service
        # This is unique to professional so using this for now to help specialize questions
        prof_pov = denial.professional_to_finish
        if in_network is not None:
            denial.provider_in_network = in_network
            # If they know about in_network they are definitely a professional
            prof_pov = True
            if "in_network" not in existing_answers:
                existing_answers["in_network"] = str(in_network)
        if single_case is not None:
            denial.single_case = single_case

        denial.save()

        # Define the special questions form for the denial
        question_forms = []
        for dt in denial.denial_type.all():
            new_form = dt.get_form()
            if new_form is not None:
                new_form = new_form(
                    initial={"medical_reason": dt.appeal_text}, prof_pov=prof_pov
                )
                question_forms.append(new_form)

        # Generate questions for better appeal creation
        try:
            # If questions don't exist yet, generate them
            if not denial.generated_questions or len(denial.generated_questions) == 0:
                # Call the generate_appeal_questions method to get and store questions
                # Using sync_to_async since we're in a synchronous method
                logger.debug("Generating appeal questions")
                async_to_sync(DenialCreatorHelper.generate_appeal_questions)(
                    denial_id=denial.denial_id
                )
                # Refresh the denial object to get the updated questions
                denial.refresh_from_db()

            # Create a form with the questions as fields if we have generated questions
            if denial.generated_questions:
                from django import forms

                # The generated_questions field now contains tuples of (question, answer)
                generated_questions: list[tuple[str, str]] = denial.generated_questions

                # Create an AppealQuestionsForm to add to our question forms
                class AppealQuestionsForm(forms.Form):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        # Add fields for each question
                        for i, (question, initial_answer) in enumerate(
                            generated_questions, 1
                        ):
                            field_name = f"appeal_generated_question_{i}"
                            self.fields[field_name] = forms.CharField(
                                label=question,
                                help_text=question,
                                required=False,
                                initial=initial_answer,  # Use the answer from the tuple
                            )

                appeal_questions_form = AppealQuestionsForm()
                # Add this form to our question forms list
                question_forms.append(appeal_questions_form)
        except Exception as e:
            logger.opt(exception=True).error(
                f"Failed to process appeal questions for denial {denial_id}: {e}"
            )

        # Combine all forms
        try:
            combined_form = magic_combined_form(question_forms, existing_answers)
            return NextStepInfo(
                outside_help_details=outside_help_details,
                combined_form=combined_form,
                semi_sekret=semi_sekret,
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
        use_external_models=False,
        store_raw_email=False,
        plan_documents=None,
        patient_id=None,
        insurance_company: Optional[str] = None,
        denial: Optional[Denial] = None,
        creating_professional: Optional[ProfessionalUser] = None,
        primary_professional: Optional[ProfessionalUser] = None,
        patient_user: Optional[PatientUser] = None,
        patient_visible: bool = False,
        subscribe: bool = False,  # Note: we don't handle this, but it's in the form so passed through.
    ):
        """
        Create or update an existing denial.
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
        # If we don't have a denial we're making a new one
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
                    patient_visible=patient_visible,
                    professional_to_finish=professional_to_finish,
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
                    patient_visible=patient_visible,
                    professional_to_finish=professional_to_finish,
                )
        else:
            # Directly update denial object fields instead of using denial.update()
            denial.denial_text = denial_text
            denial.hashed_email = hashed_email
            denial.use_external = use_external_models
            denial.raw_email = possible_email
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
            if patient_visible is not None:
                denial.patient_visible = patient_visible

            denial.save()

        if possible_email is not None:
            FollowUpSched.objects.create(
                email=possible_email,
                follow_up_date=denial.date + datetime.timedelta(days=15),
                denial_id=denial,
            )
        your_state = None
        if zip is not None and zip != "":
            try:
                your_state = cls.zip_engine.by_zipcode(zip).state
                denial.your_state = your_state
            except:
                # Default to no state
                your_state = None
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
        return cls._update_denial(
            denial=denial, health_history=health_history, plan_documents=plan_documents
        )

    @classmethod
    async def extract_entity(cls, denial_id: int) -> AsyncIterator[str]:
        """
        Perform entity extraction on a given denial id
        """

        logger.debug(f"Starting entity extraction for denial {denial_id}")
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        if (
            denial.diagnosis
            or denial.extract_procedure_diagnosis_finished
            or denial.procedure
        ):
            logger.debug(
                f"Skipping entity extraction for denial {denial_id} as it is already done."
            )
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
            named_task(cls.extract_set_plan_id(denial_id), "plan id"),
            named_task(cls.extract_set_claim_id(denial_id), "claim id"),
            named_task(cls.extract_set_date_of_service(denial_id), "date of service"),
        ]

        required_awaitables: list[Coroutine[Any, Any, tuple[str, Any]]] = [
            # Denial type depends on denial and diagnosis
            named_task(cls.extract_set_denial_and_diagnosis(denial_id), "diagnosis"),
            named_task(cls.extract_set_denialtype(denial_id), "type of denial"),
        ]

        logger.debug(
            f"Collecting tasks for {denial_id} with {len(optional_awaitables)} optional and {len(required_awaitables)} required tasks."
        )
        try:
            async for item in execute_critical_optional_fireandforget(
                optional=optional_awaitables,
                required=required_awaitables,
                fire_and_forget=[],
                done_record=("Extraction complete", None),
                timeout=90,
            ):
                if item:
                    yield item[0]
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error during extraction for denial {denial_id}: {e}"
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

        Attempts to extract the procedure and diagnosis fields using the appeal generator. Updates the denial with the extracted values and marks extraction as finished, regardless of success. If extraction is successful or existing values are present, triggers background tasks to search for related PubMed articles and build speculative context. Handles timeouts and cancellation during PubMed search gracefully.
        """
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        procedure = None
        diagnosis = None

        try:
            (procedure, diagnosis) = await appealGenerator.get_procedure_and_diagnosis(
                denial_text=denial.denial_text
            )

            # Prepare update fields
            update_fields: dict[str, Any] = {
                "extract_procedure_diagnosis_finished": True
            }

            if procedure is not None and len(procedure) < 300:
                update_fields["procedure"] = procedure
                update_fields["candidate_procedure"] = procedure

            if diagnosis is not None and len(diagnosis) < 300:
                update_fields["diagnosis"] = diagnosis
                update_fields["candidate_diagnosis"] = diagnosis

            # Update all fields in a single atomic database operation
            await Denial.objects.filter(denial_id=denial_id).aupdate(**update_fields)

            # Use fire_and_forget_in_new_threadpool for background PubMed article search
            # now that we have diagnosis and procedure information
            if denial.procedure or denial.diagnosis or procedure or diagnosis:

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

                # Fire and forget the PubMed search task
                logger.debug("Starting pubmed search & building speculative context.")
                await fire_and_forget_in_new_threadpool(find_pubmed_articles())
                # Fire and forget the building the speculative context
                await fire_and_forget_in_new_threadpool(
                    cls.build_speculative_context(denial_id)
                )
                logger.debug("Fire and forgets fired.")

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract procedure and diagnosis for denial {denial_id}: {e}"
            )
            # Even on failure, mark extraction as finished
            await Denial.objects.filter(denial_id=denial_id).aupdate(
                extract_procedure_diagnosis_finished=True
            )

    @classmethod
    async def extract_set_insurance_company(cls, denial_id):
        """Extract insurance company name from denial text"""
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        insurance_company = None
        try:
            insurance_company = await appealGenerator.get_insurance_company(
                denial_text=denial.denial_text
            )

            # Validate insurance company name - simple validation to avoid hallucinations
            if insurance_company is not None:
                # Check if the name appears in the text or is reasonable length
                if (insurance_company in denial.denial_text) or len(
                    insurance_company
                ) < 50:
                    # Use aupdate() directly instead of loading and saving the object
                    await Denial.objects.filter(denial_id=denial_id).aupdate(
                        insurance_company=insurance_company
                    )
                    logger.debug(
                        f"Successfully extracted insurance company: {insurance_company}"
                    )
                    return insurance_company
                else:
                    logger.debug(
                        f"Rejected insurance company extraction: {insurance_company}"
                    )
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

            # Simple validation to avoid hallucinations
            if plan_id is not None and len(plan_id) < 30:
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
    async def extract_set_claim_id(cls, denial_id):
        """Extract claim ID from denial text"""
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        claim_id = None
        try:
            claim_id = await appealGenerator.get_claim_id(
                denial_text=denial.denial_text
            )

            # Simple validation to avoid hallucinations
            if claim_id is not None and len(claim_id) < 30:
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
    async def extract_set_fax_number(cls, denial_id):
        # Try and extract the appeal fax number
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        appeal_fax_number = denial.appeal_fax_number
        if not appeal_fax_number:
            try:
                appeal_fax_number = await appealGenerator.get_fax_number(
                    denial_text=denial.denial_text
                )
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Failed to extract fax number for denial {denial_id}: {e}"
                )

        # More flexible validation against hallucinations
        if appeal_fax_number is not None:
            # Extract just the digits for comparison
            fax_digits = re.sub(r"\D", "", appeal_fax_number)

            if len(fax_digits) < 10 or len(fax_digits) > 15:
                # Invalid length for a phone number
                logger.debug(
                    f"Rejected fax number {appeal_fax_number} - invalid length"
                )
                appeal_fax_number = None
            elif len(appeal_fax_number) > 30:
                # String is too long to be a reasonable fax number
                logger.debug(f"Rejected fax number {appeal_fax_number} - too long")
                appeal_fax_number = None
            else:
                # Check if any subsequence of digits appears in the denial text
                # This is more flexible than exact matching
                denial_text_digits = re.sub(r"\D", "", denial.denial_text)
                if fax_digits[-10:] not in denial_text_digits:
                    # Not found in text - might be hallucinated
                    logger.debug(
                        f"Rejected fax number {appeal_fax_number} - digits not found in text"
                    )
                    appeal_fax_number = None
                else:
                    logger.debug(f"Validated fax number {appeal_fax_number}")

        if appeal_fax_number is not None:
            # Use aupdate instead of fetching and saving
            await Denial.objects.filter(denial_id=denial_id).aupdate(
                appeal_fax_number=appeal_fax_number
            )
            logger.debug(f"Successfully extracted fax number: {appeal_fax_number}")
            return appeal_fax_number
        return None

    @classmethod
    async def extract_set_denialtype(cls, denial_id):
        logger.debug(f"Extracting and setting denial types....")
        # Try and guess at the denial types
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        denial_types = await cls.regex_denial_processor.get_denialtype(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )
        logger.debug(f"Ok lets rock with {denial_types}")
        for dt in denial_types:
            try:
                await DenialTypesRelation.objects.acreate(
                    denial=denial, denial_type=dt, src=await cls.regex_src()
                )
            except:
                logger.opt(exception=True).debug(f"Failed setting denial type")
        logger.debug(f"Done setting denial types")

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


class AppealsBackendHelper:
    regex_denial_processor = ProcessDenialRegex()
    pmt = PubMedTools()

    @classmethod
    async def generate_appeals(cls, parameters) -> AsyncIterator[str]:
        """
        Asynchronously generates and streams appeal texts for a given denial, including both previously saved and newly generated appeals.

        This coroutine retrieves denial and related context, processes templates and forms to construct appeal components, gathers citation contexts, and yields appeal texts with relevant substitutions applied. Previously saved appeals are yielded first, followed by newly generated appeals, each formatted as a JSON string.
        """
        logger.debug(f"Raw parameters received: {parameters}")

        # Extract specific parameters needed early
        denial_id = parameters["denial_id"]
        email = parameters["email"]
        semi_sekret = parameters["semi_sekret"]
        hashed_email = Denial.get_hashed_email(email)
        # Extract the professional_to_finish parameter from the input, default to False
        professional_to_finish = parameters.get("professional_to_finish", False)
        # Medical reason provided?
        medical_reasons = set()
        if (
            "medical_reason" in parameters
            and parameters["medical_reason"]
            and len(parameters["medical_reason"]) > 1
        ):
            medical_reasons.append(parameters["medical_reason"])

        if denial_id is None:
            raise Exception("Missing denial id")
        if semi_sekret is None:
            raise Exception("Missing sekret")

        # Yield status: starting
        yield json.dumps(
            {"type": "status", "message": "Starting appeal generation..."}
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
            # Substitutes for common terms
            subs = {
                "Esteemed Members of the Appeals Committee": insurance_company,
                "[insurance_company]": insurance_company,
                "{insurance_company}": insurance_company,
                "insurance_company": insurance_company,
                "{{insurance_company}}": insurance_company,
                "[Insurance Company Name]": insurance_company,
                "[Insurance Company]": insurance_company,
                "[Insert Date]": denial.date or "{date}",
                "[Health Plan]": insurance_company,
                "[Reference Number from Denial Letter]": claim_id,
                "Dear Insurance Company": f"Dear {insurance_company}",
                "Dear Health Plan": f"Dear {insurance_company}",
                "Dear Sir/Madam": f"Dear {insurance_company}",
                "[Claim ID]": claim_id,
                "{claim_id}": claim_id,
                "[Diagnosis]": diagnosis,
                "[Procedure]": procedure,
                "{diagnosis}": diagnosis,
                "{procedure}": procedure,
            }
            try:
                if (
                    denial.professional_to_finish
                    and denial.primary_professional is not None
                ):
                    subs["[Your Name]"] = denial.primary_professional.get_full_name()
                if denial.patient_user is not None:
                    subs["[Patient Name]"] = denial.patient_user.get_legal_name()
                    subs["[patient name]"] = denial.patient_user.get_legal_name()
                if denial and denial.primary_professional is not None:
                    subs["[Professional Name]"] = (
                        denial.primary_professional.get_full_name()
                    )
                if denial.domain:
                    subs["[Professional Address]"] = denial.domain.get_address()
            except:
                logger.opt(exception=True).error(
                    f"Error fetching info for denial sub {denial.denial_id}"
                )
            for k, v in subs.items():
                if v and v != "" and v != "UNKNOWN":
                    # Handle the {{}}
                    content = content.replace("{{" + k + "}}", "{" + k + "}")
                    if "{" in k:
                        content = content.replace("{" + k + "}", k)
                    content = content.replace(k, str(v))
            appeal["content"] = content
            return appeal

        # If we've had a timeout on the initial call and we're on round 2
        # we should fetch the existing appeals from the previous round if present.
        existing_appeals = ProposedAppeal.objects.filter(for_denial=denial).all()
        # Yield the existing appeals first
        old = 0
        async for appeal in existing_appeals:
            old = old + 1
            if appeal.appeal_text is not None:
                logger.debug(f"Found existing appeal {appeal}, yielding")
                existing_appeal_dict = await sub_in_appeals(
                    {"id": str(appeal.id), "content": appeal.appeal_text}
                )
                yield await format_response(existing_appeal_dict)

        # Yield status after any previously saved appeals have been sent
        yield json.dumps(
            {"type": "status", "message": "Starting appeal generation..."}
        ) + "\n"
        yield json.dumps(
            {"type": "status", "message": "Loaded denial information"}
        ) + "\n"
        yield json.dumps(
            {"type": "status", "message": "Processing denial types and templates..."}
        ) + "\n"

        non_ai_appeals: List[str] = list(
            map(
                lambda t: t.appeal_text,
                await cls.regex_denial_processor.get_appeal_templates(
                    denial.denial_text, denial.diagnosis
                ),
            )
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
            form = await sync_to_async(dt.get_form)()
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

        if denial.medical_reason_manual:
            medical_reasons.add(denial.medical_reason_manual)
        # Add the context to the denial
        if medical_context is not None:
            qa_context = {}
            if denial.qa_context is not None:
                qa_context = json.loads(denial.qa_context)
            qa_context["medical_context"] = " ".join(medical_context)
            denial.qa_context = json.dumps(qa_context)
        if plan_context is not None:
            denial.plan_context = " ".join(set(plan_context))
        # Update the denial object with the received parameter if it differs
        if denial.professional_to_finish != professional_to_finish:
            logger.info(
                f"Updating denial {denial.denial_id} professional_to_finish from {denial.professional_to_finish} to {professional_to_finish}"
            )
            denial.professional_to_finish = professional_to_finish
        denial.gen_attempts = (denial.gen_attempts or 0) + 1
        await denial.asave()

        # Get pubmed and ml citations context
        pubmed_context: Optional[str] = None
        ml_citation_context: Optional[Any] = None

        # Get PubMed context
        logger.debug("Looking up the pubmed context")

        # If we're getting "late" into our number of retries skip additional ctx.
        if denial.gen_attempts < 3:
            # Yield status: gathering context
            yield json.dumps(
                {
                    "type": "status",
                    "message": "Gathering medical research and citations...",
                }
            ) + "\n"

            pubmed_context_awaitable = asyncio.wait_for(
                cls.pmt.find_context_for_denial(denial), timeout=40
            )

            ml_citation_context_awaitable = asyncio.wait_for(
                MLCitationsHelper.generate_citations_for_denial(
                    denial, speculative=False
                ),
                timeout=40,
            )
            # Await both contexts so we can use co-operative multitasking
            try:
                logger.debug("Gathering contexts")
                results = await asyncio.gather(
                    pubmed_context_awaitable,
                    ml_citation_context_awaitable,
                    return_exceptions=True,
                )
                if isinstance(results[0], str):
                    pubmed_context = results[0]
                else:
                    pubmed_context = None
                if isinstance(results[1], str):
                    ml_citation_context = results[1]
                else:
                    ml_citation_context = None
                logger.debug("Success")
            except Exception as e:
                logger.opt(exception=True).error(f"Error gathering contexts: {e}")
                # We still might have saved a context.
                try:
                    # Added in Django 5.1
                    await denial.arefresh_from_db(from_queryset=denial_query)
                except:
                    denial = await denial_query.aget()
                pubmed_context = denial.pubmed_context
                ml_citation_context = denial.ml_citation_context
                logger.debug("Used saved contexts")
        else:
            logger.debug("Too many retries, skipping ML/pubmed ctx")

        async def save_appeal(appeal_text: str) -> dict[str, str]:
            # Save all of the proposed appeals, so we can use RL later.
            t = time.time()
            logger.debug(f"{t}: Saving {appeal_text}")
            await asyncio.sleep(0)
            # YOLO on saving appeals, sqllite gets sad.
            id = "unknown"
            try:
                pa = ProposedAppeal(appeal_text=appeal_text, for_denial=denial)
                await pa.asave()
                id = str(pa.id)
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Failed to save proposed appeal: {e}"
                )
                pass
            passed = time.time() - t
            logger.debug(f"Saved {appeal_text} after {passed} seconds")
            return {"id": id, "content": appeal_text}

        # Yield status: generating appeals
        yield json.dumps(
            {"type": "status", "message": "Generating personalized appeals with AI..."}
        ) + "\n"

        appeals: Iterator[str] = await sync_to_async(appealGenerator.make_appeals)(
            denial,
            AppealTemplateGenerator(prefaces, main, footer),
            medical_reasons=medical_reasons,
            non_ai_appeals=non_ai_appeals,
            pubmed_context=pubmed_context,
            ml_citations_context=ml_citation_context,
        )
        # Only filters out None
        filtered_appeals: Iterator[str] = filter(lambda x: x != None, appeals)

        # We convert to async here.
        saved_appeals: AsyncIterator[dict[str, str]] = a.map(
            save_appeal, filtered_appeals
        )
        # Note: we intentionally call save before substution.
        subbed_appeals: AsyncIterator[dict[str, str]] = a.map(
            sub_in_appeals, saved_appeals
        )
        appeals_json: AsyncIterator[str] = a.map(format_response, subbed_appeals)
        # StreamignHttpResponse needs a synchronous iterator otherwise it blocks.
        interleaved: AsyncIterator[str] = interleave_iterator_for_keep_alive(
            appeals_json
        )

        new = 0
        async for i in interleaved:
            if i and len(i) > 10:
                new = new + 1
            yield i
        logger.debug(f"All appeals sent {new} and {old}")


class StripeWebhookHelper:
    @staticmethod
    def handle_checkout_session_completed(request, session):
        try:
            try:
                metadata = session.metadata
            except:
                metadata = session["metadata"]

            payment_type: Optional[str] = None
            if "payment_type" in metadata:
                payment_type = metadata.get("payment_type")
            else:
                logger.debug(f"No payment type field found in {session}")

            if payment_type == "interested_professional_signup":
                InterestedProfessional.objects.filter(
                    id=metadata.get("interested_professional_id")
                ).update(paid=True)

            elif payment_type == "professional_domain_subscription":
                logger.debug(f"Processing professional domain subscription {session}")
                subscription_id = session.get("subscription")
                customer_id = session.get("customer")
                if subscription_id:
                    UserDomain.objects.filter(id=metadata.get("domain_id")).update(
                        stripe_subscription_id=subscription_id,
                        stripe_customer_id=customer_id,
                        active=True,
                        pending=False,
                    )
                    ProfessionalUser.objects.filter(
                        id=metadata.get("professional_id")
                    ).update(active=True)
                    user = ProfessionalUser.objects.get(
                        id=metadata.get("professional_id")
                    ).user
                    fhi_emails.send_verification_email(request, user, first_only=True)
                else:
                    logger.opt(exception=True).error(
                        "No subscription ID in completed checkout session"
                    )

            elif payment_type == "fax":
                FaxesToSend.objects.filter(uuid=session.metadata.get("uuid")).update(
                    paid=True, should_send=True
                )
            else:
                logger.warning(f"Unknown payment type: {payment_type}")
        except Exception as e:
            logger.opt(exception=True).error("Error processing checkout session")
            raise e

    @staticmethod
    def handle_checkout_session_expired(request, session):
        try:
            # TODO: More complete handling
            logger.debug(f"Checkout session expired: {session}")
            try:
                metadata = session.metadata
            except:
                metadata = session["metadata"]
            payment_type = metadata.get("payment_type")
            item = "Fight Health Insurance / Fight paperwork"
            finish_link = None
            if payment_type == "fax":
                item = "Fight Health Insurance Fax"
            elif payment_type == "professional_domain_subscription":
                item = "Fight Paperwork Professional Domain Subscription"
                # Check if the domain is already active (due to another checkout session)
                domain_id = metadata.get("domain_id")
                if domain_id:
                    try:
                        domain = UserDomain.objects.get(id=domain_id)
                        if domain.active and domain.stripe_subscription_id:
                            logger.info(
                                f"Domain {domain_id} is already active with subscription, ignoring expired checkout"
                            )
                            return
                    except UserDomain.DoesNotExist:
                        logger.info(
                            f"Domain {domain_id} no longer exists, might have been recreated"
                        )

                # Temporary until the FPW UI is ready
                finish_base_link = (
                    "https://www.fightpaperwork.com/stripe/finish-checkout"
                )
                params = urlencode(
                    {
                        "domain_id": domain_id,
                        "professional_id": metadata.get("professional_id"),
                    }
                )
                finish_link = f"{finish_base_link}?{params}"
            email: Optional[str] = None
            try:
                try:
                    email = session.customer_email
                except:
                    email = session["customer_email"]
            except:
                pass
            if email is None:
                try:
                    try:
                        email = session.customer_details.email
                    except:
                        email = session["customer_details"]["email"]
                except:
                    pass
            if email is None:
                logger.debug(
                    "No email found in expired checkout session can't send e-mail"
                )
                return

            session_id = None
            if hasattr(session, "id"):
                session_id = session.id

            # Check if we already have a record for this session or similar data
            # to avoid sending duplicate emails
            existing_session = None
            if session_id:
                existing_session = LostStripeSession.objects.filter(
                    session_id=session_id
                ).first()

            if not existing_session and email and payment_type:
                existing_session = LostStripeSession.objects.filter(
                    email=email, payment_type=payment_type
                ).first()

            if existing_session:
                logger.debug(
                    f"Skipping duplicate lost stripe session notification for {email}"
                )
                return

            # For professional domain subscriptions, check if the user has successfully
            # created a domain in another session
            if payment_type == "professional_domain_subscription" and metadata.get(
                "professional_id"
            ):
                try:
                    professional_id = metadata.get("professional_id")
                    professional = ProfessionalUser.objects.get(id=professional_id)
                    if professional:
                        # Check if the user has active domains
                        active_domains = UserDomain.objects.filter(
                            professionaldomainrelation__professional=professional,
                            professionaldomainrelation__active_domain_relation=True,
                            active=True,
                        ).exists()

                        if active_domains:
                            logger.info(
                                f"User {professional} already has active domains, not creating LostStripeSession"
                            )
                            return
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"Error checking for active domains for user {email}: {e}"
                    )

            lost_session = LostStripeSession.objects.create(
                payment_type=payment_type,
                email=email,
                session_id=session_id,
                metadata=metadata,
            )
            if finish_link is None:
                finish_link_base = reverse("complete_payment")
                finish_link = f"{finish_link_base}?session_id={lost_session.id}"
            if finish_link:
                fhi_emails.send_checkout_session_expired(
                    request,
                    email=email,
                    item=item,
                    link=finish_link,
                )
            else:
                logger.debug(f"Could not create finish link for {payment_type}")
        except Exception as e:
            logger.opt(exception=True).error(
                "Error processing expired checkout session"
            )
            raise

    @staticmethod
    def handle_stripe_webhook(request, event):
        # First check if we've already received this event
        event_id = event.id

        # Check if this event has already been processed
        if StripeWebhookEvents.objects.filter(event_stripe_id=event_id).exists():
            logger.debug(f"Skipping duplicate stripe event {event_id}")
            return

        # Create a record of receiving this event
        webhook_event = StripeWebhookEvents.objects.create(
            event_stripe_id=event_id,
            success=False,  # Will be updated to True if handling succeeds
        )

        try:
            if event.type == "checkout.session.completed":
                StripeWebhookHelper.handle_checkout_session_completed(
                    request, event.data.object
                )
            elif event.type == "checkout.session.expired":
                StripeWebhookHelper.handle_checkout_session_expired(
                    request, event.data.object
                )
            else:
                logger.debug(f"Unhandled stripe event type {event.type}")

            # Update the webhook event record on success
            webhook_event.success = True
            webhook_event.save()
        except Exception as e:
            # Record the error
            webhook_event.error = str(e)[:255]  # Limit to field size
            webhook_event.save()
            logger.opt(exception=True).error(
                f"Error processing stripe webhook {event_id}"
            )
            raise
