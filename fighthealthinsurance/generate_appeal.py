import asyncio
import itertools
import random
import re
import time
import traceback
from concurrent.futures import Future
from typing import Any, Coroutine, Iterator, List, Optional, Tuple
from loguru import logger

from fighthealthinsurance.denial_base import DenialBase
from fighthealthinsurance.exec import *
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike, RemoteModelLike
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.process_denial import *
from fighthealthinsurance.utils import as_available_nested, best_within_timelimit
from typing_extensions import reveal_type
from .pubmed_tools import PubMedTools

import json


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


class AppealGenerator(object):
    def __init__(self):
        self.regex_denial_processor = ProcessDenialRegex()

    async def _extract_entity_with_regexes_and_model(
        self,
        denial_text: str,
        patterns: List[str],
        flags: int = re.IGNORECASE,
        use_external: bool = False,
        model_method_name: Optional[str] = None,
        prompt_template: Optional[str] = None,
        find_in_denial=True,
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
        # First try regex patterns
        for pattern in patterns:
            match = re.search(pattern, denial_text, flags)
            if match:
                return match.group(1).strip()

        denial_lowered = denial_text.lower()
        # If regex fails, try ML models
        if model_method_name:
            models_to_try = ml_router.entity_extract_backends(use_external)
            for model in models_to_try:
                if hasattr(model, model_method_name):
                    c = 0
                    method = getattr(model, model_method_name)
                    # Gentle retry
                    while c < 3:
                        await asyncio.sleep(1)
                        c = c + 1
                        extracted: Optional[str] = await method(
                            denial_text
                        )  # type:ignore
                        if extracted is not None:
                            extracted_lowered = extracted.lower()
                            if (
                                "unknown" not in extracted_lowered
                                and extracted_lowered != "false"
                                # Since this occurs often in our training set it can be bad
                                and "independent medical review"
                                not in extracted_lowered
                            ):
                                if (
                                    not find_in_denial
                                    or extracted_lowered in denial_lowered
                                ):
                                    return extracted
        return None

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
        return await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=fax_patterns,
            flags=re.IGNORECASE | re.DOTALL,
            use_external=use_external,
            model_method_name="get_fax_number",
            find_in_denial=False,  # Since we might have -s or other formatting
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

        # Try to find well-known insurance companies
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
            # These are often mentioned even when not the insurer
            # "Medicare",
            # "Medicaid",
        ]

        for company in known_companies:
            if company in denial_text:
                # Find the full company name (looking for patterns like "Aetna Health Insurance")
                pattern = rf"({company}\s+[A-Za-z\s&]+(?:Insurance|Health|Healthcare|Medical|Plan|Benefits))"
                match = re.search(pattern, denial_text, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                return company

        # If regex fails, use ML models
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

        return await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=plan_patterns,
            use_external=use_external,
            model_method_name="get_plan_id",
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

        return await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=claim_patterns,
            use_external=use_external,
            model_method_name="get_claim_id",
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

        result = await self._extract_entity_with_regexes_and_model(
            denial_text=denial_text,
            patterns=date_patterns,
            use_external=use_external,
            model_method_name="get_date_of_service",
        )
        return result

    async def get_procedure_and_diagnosis(
        self, denial_text=None, use_external=False
    ) -> Tuple[Optional[str], Optional[str]]:
        prompt: Optional[str] = self.make_open_procedure_prompt(denial_text)
        models_to_try: list[DenialBase] = [
            self.regex_denial_processor,
        ]
        ml_entity_models = ml_router.entity_extract_backends(use_external)
        models_to_try.extend(ml_entity_models)
        procedure = None
        diagnosis = None
        # How many models have we tried?
        c = 0
        logger.debug(f"Trying to get procedure and diagnosis using {models_to_try}")
        for model in models_to_try:
            c = c + 1
            logger.debug(f"Hiiii Exploring model {model}")
            procedure_diagnosis = await model.get_procedure_and_diagnosis(denial_text)
            if procedure_diagnosis is not None:
                if len(procedure_diagnosis) > 1:
                    procedure = procedure or procedure_diagnosis[0]
                    # If it's too long then we're probably not valid
                    if procedure is not None and len(procedure) > 200:
                        procedure = None
                    diagnosis = diagnosis or procedure_diagnosis[1]
                    if diagnosis is not None and len(diagnosis) > 200:
                        diagnosis = None
                else:
                    logger.debug(
                        f"Unexpected procedure diagnosis len on {procedure_diagnosis}"
                    )
                if procedure is not None and diagnosis is not None:
                    logger.debug(f"Return with procedure {procedure} and {diagnosis}")
                    return (procedure, diagnosis)
                elif c > 2 and (procedure is not None or diagnosis is not None):
                    logger.debug(
                        f"Return *fast* with procedure {procedure} and {diagnosis}"
                    )
                    return (procedure, diagnosis)
                else:
                    logger.debug(f"So far infered {procedure} and {diagnosis}")
        logger.debug(
            f"Fell through :/ could not fully populate but got {procedure}, {diagnosis}"
        )
        return (procedure, diagnosis)

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

        Returns:
            A formatted prompt string for appeal generation, or None if denial_text is not provided.
        """
        if denial_text is None:
            return None
        base = ""
        if is_trans:
            base = "While answering the question keep in mind the patient is trans."
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
            base = f"{base}. You should try and incorporate the following context into your appeal: {qa_context}."
        if patient is not None:
            base = f"{base}. Please include and fill in the patients info {patient}."
        if professional is not None:
            base = f"{base}. Please include and fill in the professionals info {professional}."
        if plan_id is not None and plan_id != "" and plan_id != "UNKNOWN":
            base = f"{base}. Please include and fill in any references to the plan id as {plan_id}."
        if (
            insurance_company is not None
            and insurance_company != ""
            and insurance_company != "UNKNOWN"
        ):
            base = f"{base}. Please include and fill in any references to the insurance company to be {insurance_company}."
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

    def make_appeals(
        self,
        denial,
        template_generator,
        medical_reasons=None,
        non_ai_appeals=None,
        pubmed_context=None,
        ml_citations_context=None,
    ) -> Iterator[str]:
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

        Returns:
            An iterator yielding generated appeal texts as strings.
        """
        logger.debug("Starting to make appeals...")
        if medical_reasons is None:
            medical_reasons = []
        if non_ai_appeals is None:
            non_ai_appeals = []

        open_prompt = self.make_open_prompt(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
            patient=denial.patient_user,
            professional=denial.primary_professional,
            qa_context=denial.qa_context,
            professional_to_finish=denial.professional_to_finish,
            plan_id=denial.plan_id,
            claim_id=denial.claim_id,
            insurance_company=denial.insurance_company,
        )
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
            logger.debug(f"Looking up on {model_name}")
            if model_name not in ml_router.models_by_name:
                logger.debug(f"No backend for {model_name}")
                return []
            model_backends = ml_router.models_by_name[model_name]
            if prompt is None:
                logger.debug(f"No prompt for {model_name} skipping")
                return []
            for model in model_backends:
                try:
                    logger.debug(f"Getting result on {model} backend for {model_name}")
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
                    logger.debug(f"Got back {result} for {model_name} on {model}")
                    return result
                except Exception as e:
                    logger.debug(f"Backend {model} failed {e}")
            logger.debug(f"All backends for {model_name} failed")
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
            logger.debug(
                f"Infered {results} for {model}-{infer_type} using {prompt} w/ {patient_context}"
            )
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
        plan_context = denial.plan_context
        backup_calls: List[Any] = []
        calls = [
            {
                "model_name": "fhi",
                "prompt": open_prompt,
                "patient_context": medical_context,
                "plan_context": plan_context,
                "infer_type": "full",
                "pubmed_context": pubmed_context,
                "ml_citations_context": ml_citations_context,
                "prof_pov": prof_pov,
            },
        ]

        if denial.use_external:
            calls.extend(
                [
                    {
                        "model_name": "perplexity",
                        "prompt": open_prompt,
                        "patient_context": medical_context,
                        "infer_type": "full",
                        "plan_context": plan_context,
                        "pubmed_context": pubmed_context,
                        "ml_citations_context": ml_citations_context,
                        "prof_pov": prof_pov,
                    }
                ]
            )
            calls.extend(
                [
                    {
                        "model_name": "meta-llama/llama-3.2-405b-instruct",
                        "prompt": open_prompt,
                        "patient_context": medical_context,
                        "infer_type": "full",
                        "plan_context": plan_context,
                        "pubmed_context": pubmed_context,
                        "ml_citations_context": ml_citations_context,
                        "prof_pov": prof_pov,
                    }
                ]
            )
            backup_calls.extend(
                [
                    {
                        "model_name": "meta-llama/llama-3.1-405b-instruct",
                        "prompt": open_prompt,
                        "patient_context": medical_context,
                        "infer_type": "full",
                        "plan_context": plan_context,
                        "pubmed_context": pubmed_context,
                        "ml_citations_context": ml_citations_context,
                        "prof_pov": prof_pov,
                    }
                ]
            )

        # If we need to know the medical reason ask our friendly LLMs
        static_appeal = template_generator.generate_static()
        initial_appeals = non_ai_appeals
        if static_appeal is None:
            calls.extend(
                [
                    {
                        "model_name": "fhi",
                        "prompt": open_medically_necessary_prompt,
                        "patient_context": medical_context,
                        "infer_type": "medically_necessary",
                        "plan_context": plan_context,
                        "pubmed_context": pubmed_context,
                        "ml_citations_context": ml_citations_context,
                        "prof_pov": prof_pov,
                    },
                ]
            )
            if denial.use_external:
                backup_calls.extend(
                    [
                        {
                            "model_name": "perplexity",
                            "prompt": open_medically_necessary_prompt,
                            "patient_context": medical_context,
                            "infer_type": "medically_necessary",
                            "plan_context": plan_context,
                            "pubmed_context": pubmed_context,
                            "ml_citations_context": ml_citations_context,
                            "prof_pov": prof_pov,
                        },
                    ]
                )
        else:
            # Otherwise just put in as is.
            initial_appeals.append(static_appeal)
        for reason in medical_reasons:
            logger.debug(f"Using reason {reason}")
            appeal = template_generator.generate(reason)
            initial_appeals.append(appeal)

        logger.debug(f"Initial appeal {initial_appeals}")
        # Executor map wants a list for each parameter.

        def make_async_model_calls(calls) -> List[Future[Iterator[str]]]:
            logger.debug(f"Calling models: {calls}")
            model_futures = itertools.chain.from_iterable(
                map(lambda x: get_model_result(**x), calls)
            )

            def generated_to_appeals_text(k_text_future):
                model_results = k_text_future.result()
                if model_results is None:
                    return []
                for k, text in model_results:
                    if text is None:
                        pass
                    # It's either full or a reason to plug into a template
                    if k == "full":
                        yield text
                    else:
                        yield template_generator.generate(text)

            # Python lack reasonable future chaining (ugh)
            generated_text_futures = list(
                map(
                    lambda f: executor.submit(generated_to_appeals_text, f),
                    model_futures,
                )
            )
            return generated_text_futures

        generated_text_futures: List[Future[Iterator[str]]] = make_async_model_calls(
            calls
        )

        # Since we publish the results as they become available
        # we want to add some randomization to the initial appeals so they are
        # not always appearing in the first position.
        def random_delay(appeal) -> Iterator[str]:
            time.sleep(random.randint(0, 20))
            return iter([appeal])

        delayed_initial_appeals: List[Future[Iterator[str]]] = list(
            map(lambda appeal: executor.submit(random_delay, appeal), initial_appeals)
        )
        core_futures: List[Future[Iterator[str]]] = (
            delayed_initial_appeals + generated_text_futures
        )
        appeals: Iterator[str] = as_available_nested(core_futures)
        logger.debug(f"Appeals itr starting with {appeals}")
        # Check and make sure we have some AI powered results
        try:
            logger.debug(f"Getting first {appeals}")
            appeals = itertools.chain([appeals.__next__()], appeals)
            logger.debug(f"First pulled off {appeals}")
        except StopIteration:
            logger.warning(f"Adding backup calls {backup_calls}")
            appeals = as_available_nested(make_async_model_calls(backup_calls))
        logger.debug(f"Sending back {appeals}")
        return appeals
