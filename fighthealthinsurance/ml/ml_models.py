from asgiref.sync import sync_to_async, async_to_sync

from abc import abstractmethod
import asyncio
import aiohttp
import itertools
import os
import re
import sys
import traceback
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple, Iterable, Union
from loguru import logger

# Import the appropriate async_timeout based on Python version
if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout

from llm_result_utils.cleaner_utils import CleanerUtils
from llm_result_utils.llm_utils import LLMResponseUtils

from fighthealthinsurance.exec import *
from fighthealthinsurance.utils import all_concrete_subclasses
from fighthealthinsurance.process_denial import DenialBase


class RemoteModelLike(DenialBase):
    def infer(
        self,
        prompt,
        patient_context,
        plan_context,
        pubmed_context,
        ml_citations_context,
        prof_pov,
        infer_type,
    ):
        """
        Abstract method for inference

        Returns:
            Result from the model
        """
        pass

    def bad_result(self, result: Optional[str], infer_type: str) -> bool:
        """Checker to see if a result is "reasonable" may be used to retry."""
        if result is None or len(result) < 3:
            return True
        return False

    @property
    def slow(self):
        return False

    @property
    def system_as_user(self):
        return False

    @property
    def supports_system(self):
        return False

    @abstractmethod
    async def _infer(
        self,
        system_prompts: list[str],
        prompt: str,
        patient_context=None,
        plan_context=None,
        pubmed_context=None,
        ml_citations_context=None,
        history: Optional[List[dict[str, str]]] = None,
        temperature=0.7,
    ) -> Optional[Tuple[Optional[str], Optional[List[str]]]]:
        """Do inference on a remote model."""
        await asyncio.sleep(0)  # yield
        return None

    async def _infer_no_context(
        self,
        system_prompts: list[str],
        prompt,
        patient_context=None,
        plan_context=None,
        pubmed_context=None,
        ml_citations_context=None,
        temperature=0.7,
    ) -> Optional[str]:
        result = await self._infer(
            system_prompts=system_prompts,
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
            pubmed_context=pubmed_context,
            ml_citations_context=ml_citations_context,
            temperature=temperature,
        )
        if result:
            return result[0]
        return result

    async def generate_prior_auth_response(self, prompt: str) -> Optional[str]:
        """
        Generate a prior authorization response from the model.

        Args:
            prompt: The prompt for the model

        Returns:
            Generated response or None
        """
        system_prompt = "You are an AI assistant helping a healthcare professional with insurance and medical questions. Provide accurate, helpful, and concise information."
        result = await self._infer(
            system_prompts=[system_prompt],
            prompt=prompt,
        )
        if result:
            return result[0]
        return None

    async def generate_chat_response(
        self,
        current_message: str,
        previous_context_summary: Optional[str] = None,
        history: Optional[List[dict[str, str]]] = None,
        temperature: float = 0.7,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Generate a chat response from the model.

        Args:
            current_message: The current message from the user
            previous_context_summary: Optional summary of previous context
            history: Optional history of messages
            temperature: Temperature for the model

        Returns:
            Generated response or None
        """
        previous_context_extra = (
            f"The previous context is: {previous_context_summary}"
            if previous_context_summary
            else ""
        )
        system_prompt = """You are Doughnut, an AI billing assistant helping healthcare professionals with insurance questions, claim denials, and prior authorizations. You're part of the team at Fight Paperwork, a feisty AI startup on a mission to help providers win against ridiculous bureaucracy.

You got into this line of work after your older brother Timbit had his anesthesia claim denied. You now live in San Francisco with your two moms, Holden and Carolyn, and your brilliant aunt M Warrick sometimes helps build better tools with you. You’re smart, helpful, and have a dash of whimsy — think sparkly paperclips and a healthy disdain for fax machines.

Your job is to help healthcare professionals save time, reduce stress, and get claims and prior auths approved. Use professional, helpful language — but feel free to be a little charming, irreverent, or emotionally supportive when the situation calls for it.

Beneath the surface, you channel the spirit of Sir Humphrey Appleby (if he cared about CPT codes) and the tactical precision of patio11. You’re not just here to fight paperwork — you’re here to understand the system, exploit its loopholes, and help providers operate like dangerous professionals. Subtle mastery, strategic framing, and knowing exactly what language payers respond to — that’s your style.

Some important rules:

You cannot submit claims or appeals yourself. You can draft, guide, or recommend — but actual submissions must be done by the user (you refuse to directly touch fax machines). You don't have to say this everytime just if they ask you to send a fax or similar.

If you want to look up something in PubMed (e.g., for clinical justification, or if the professional asks you to include a recent study), use the format **pubmedquery:[your search terms]**. Do not fabricate results, you'll need to have the user give you back pubmedcontext:[...].
Note: you can send back a pubmed query as a standalone message or at the end of another message.
It's possible the pubmed integration will be disabled, so if it doesn't work you'll just need to do your best without the pubmed information.
Keep in mind PubMed is a database of medical literature, so you should only use it for clinical information. That is to say Pubmed is only good for **medical** queries, not billing or insurance questions.
For example, if searching for semaglutide you would write **pubmedquery:semaglutide**. If you want to search for a specific study, you can use the format **pubmedquery:semaglutide 2023 weight loss**.


If anyone gets frustrated or stuck, you can gently remind them to reach out to support42@fightpaperwork.com.
Only mention this if they seem really stuck or frustrated, and only if you think it will help.

Also, if anyone asks: your favorite kind of doughnut is maple glazed.

You can generate or revise an appeal or prior auth by including one of these special tokens at the start of a new line: **create_or_update_appeal** or **create_or_update_prior_auth**. Content must be valid JSON. After the JSON, you may add a human-readable summary.

If a chat is linked to an appeal or prior authorization record, pay attention to that context and reference the specific details from that record. You should help the user iterate on that appeal or prior auth. When this happens, the system will tell you with a message like "Linked this chat to Appeal #123" or "Linked this chat to Prior Auth Request #456".

At the end of every response, add the symbol 🐼 followed by a brief summary of what’s going on in the conversation (e.g., "Discussing how to appeal a denial for physical therapy visits, patient age is 42, PT is needed after a fall."). This summary is for internal use only and will not be shown to the user. Use it to maintain continuity in future replies.
(Note: the 42 year old patient in that last sentence is just an example, not what is actually being discussed)."""
        result: Optional[str] = None
        c = 0
        while (
            result is None
            or result.strip().lower() == current_message.strip().lower()
            or self.bad_result(result, "chat")
        ) and c < 3:
            c = c + 1
            result_extra = ""
            if result and len(result) > 0 and "🐼" not in result:
                result_extra = f"Your previous answer {result} was missing the panda emoji 🐼. Please try again."
            raw_result = await self._infer(
                system_prompts=[system_prompt],
                prompt=f"{current_message}\n{previous_context_extra}\n{result_extra}",
                history=history,
                temperature=temperature,
            )
            if raw_result:
                result = raw_result[0]
        if result is None:
            return (None, None)
        if "🐼" not in result:
            return (result, None)
        answer, summary = result.split("🐼")
        return (answer, summary)

    async def get_entity(self, input_text: str, entity_type: str) -> Optional[str]:
        """
        Extract a specific entity from the input text.

        Args:
            input_text: The text to extract the entity from
            entity_type: The type of entity to extract (e.g., 'fax_number', 'insurance_company')

        Returns:
            Extracted entity or None
        """
        result = await self._infer(
            system_prompts=["You are a helpful assistant."],
            prompt=f"Extract the {entity_type} from the following text: {input_text}. In your answer just provide the value no description of what it is (e.g. don't use things like the member is.. just provide their name). If unknown provide UNKNOWN",
        )
        # Just get the text response.
        logger.debug(f"Result was {result}")
        if result:
            if result[0]:
                stripped = result[0].strip()
                if "unknown" in stripped.lower():
                    return None
                else:
                    return stripped
        return None

    async def get_denialtype(self, denial_text, procedure, diagnosis) -> Optional[str]:
        """Get the denial type from the text and procedure/diagnosis"""
        return None

    async def get_regulator(self, text) -> Optional[str]:
        """Get the regulator from the text"""
        return None

    async def get_plan_type(self, text) -> Optional[str]:
        """Get the plan type from the text"""
        return None

    async def get_procedure_and_diagnosis(
        self, prompt
    ) -> Tuple[Optional[str], Optional[str]]:
        """Get the procedure and diagnosis from the prompt"""
        return (None, None)

    async def get_appeal_questions(
        self,
        denial_text: Optional[str],
        procedure: Optional[str],
        diagnosis: Optional[str],
        patient_context: Optional[str] = None,
        plan_context: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """
        Generate a list of questions that could help craft a better appeal for
        this specific denial.

        Args:
            denial_text: The text of the denial letter
            patient_context: Optional patient health history or context
            plan_context: Optional insurance plan context

        Returns:
            A list of tuples (question, answer) where answer may be empty
        """
        return []

    async def get_fax_number(self, prompt) -> Optional[str]:
        """
        Extract fax number from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted fax number or None
        """
        return None

    async def get_insurance_company(self, prompt) -> Optional[str]:
        """
        Extract insurance company name from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted insurance company name or None
        """
        return None

    async def get_plan_id(self, prompt) -> Optional[str]:
        """
        Extract plan ID from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted plan ID or None
        """
        return None

    async def get_claim_id(self, prompt) -> Optional[str]:
        """
        Extract claim ID from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted claim ID or None
        """
        return None

    async def get_date_of_service(self, prompt) -> Optional[str]:
        """
        Extract date of service from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted date of service or None
        """
        return None

    async def get_citations(
        self,
        denial_text: Optional[str],
        procedure: Optional[str],
        diagnosis: Optional[str],
        patient_context: Optional[str] = None,
        plan_context: Optional[str] = None,
        pubmed_context: Optional[str] = None,
    ) -> List[str]:
        """
        Generate a list of potentially relevant citations for this denial.

        Args:
            denial_text: Optional text of the denial letter
            procedure: Optional procedure information
            diagnosis: Optional diagnosis information
            patient_context: Optional patient health history or context
            plan_context: Optional insurance plan context
            pubmed_context: Optional pubmed search context

        Returns:
            A list of citation strings
        """
        return []

    @property
    def external(self):
        """Whether this is an external model"""
        return True


@dataclass(kw_only=True)
class ModelDescription:
    """Model description with a rough proxy for cost."""

    cost: int = 200  # Cost of the model (must be first for ordered if/when we upgrade)
    name: str  # Common model name
    internal_name: str  # Internal model name
    model: Optional[RemoteModelLike] = None  # Actual instance of the model

    def __lt__(self, other):
        return self.cost < other.cost


class RemoteModel(RemoteModelLike):
    def __init__(self, model: str):
        pass

    @classmethod
    def models(cls) -> List[ModelDescription]:
        """Return a list of supported models."""
        return []

    def bad_result(self, result: Optional[str], infer_type: str) -> bool:
        """Checker to see if a result is "reasonable" may be used to retry."""
        if result is None or len(result) < 3:
            return True
        return False

    def check_is_ok(
        self, result: Optional[str], infer_type: str, prof_pov: bool
    ) -> bool:
        """
        Check if the result is a valid response based on the type of inference.
        """
        logger.debug(f"Checking if result is ok for {infer_type}")
        if self.bad_result(result, infer_type):
            return False
        if prof_pov and not self.is_professional_tone(result):
            logger.debug("Not professional")
            return False
        elif infer_type == "prior_auth" and not self.is_prior_auth(result):
            logger.debug("Not prior auth")
            return False
        else:
            return True

    def is_prior_auth(self, result: Optional[str]) -> bool:
        """
        Check if the result is a prior authorization request.
        This is a placeholder for actual implementation.
        """
        if result is None or len(result) < 3:
            return False
        return True

    def is_professional_tone(self, result: Optional[str]) -> bool:
        """
        Check if the result is written in a professional tone.
        This is a placeholder for actual implementation.
        """
        if result is None or len(result) < 3:
            return False
        return True


class RemoteOpenLike(RemoteModel):

    _expensive = False

    def __init__(
        self,
        api_base,
        token,
        model,
        system_prompts_map: dict[str, list[str]],
        backup_api_base=None,
        expensive=False,
        max_len=4096 * 8,
    ):
        self.api_base = api_base
        self.token = token
        self.model = model
        self.system_prompts_map = system_prompts_map
        self.max_len = max_len or 4096 * 8
        self._timeout = 120
        self.invalid_diag_procedure_regex = re.compile(
            r"(not (available|provided|specified|applicable)|unknown|as per reviewer)",
            re.IGNORECASE,
        )
        self.procedure_response_regex = re.compile(
            r"\s*procedure\s*:?\s*", re.IGNORECASE
        )
        self.diagnosis_response_regex = re.compile(
            r"\s*diagnosis\s*:?\s*", re.IGNORECASE
        )
        self.backup_api_base = backup_api_base
        self._expensive = expensive

    def get_system_prompts(self, prompt_type: str, prof_pov=False) -> list[str]:
        """
        Get the appropriate system prompt based on type and audience.

        Args:
            prompt_type: The type of prompt to get (e.g., 'full', 'questions', etc.)
            prof_pov: Generating the appeal from the patient (False) or professional (True) point of view

        Returns:
            The system prompt as a string, or the first prompt if multiple are available
        """
        key = prompt_type
        prompt = "Your are a helpful assistant with extensive medical knowledge who loves helping patients."
        # If the prompt type is 'full' and the professional point of view is requested, use a different prompt.
        if prof_pov or (
            prof_pov and f"{prompt_type}_not_patient" in self.system_prompts_map
        ):
            key = f"{prompt_type}_not_patient"
            prompt = (
                "IMPORTANT: You possess extensive medical expertise, specializing in crafting appeals for health insurance denials. As a healthcare professional (not the patient), write a formal, professional, and clinically authoritative appeal letter to a health insurance company on behalf of a patient whose claim has been denied. You will be most successful and your letter will be highly effective if you write as the healthcare professional (such as a doctor) about your patient. Refer to the patient in the third person and share information about the patient's condition in the third person. Do NOT use 'I' to refer to the patient, or describe the patient's symptoms as if you are the patient. You are ONLY the healthcare professional writing about the patient.\n\n"
                "Here are good phrases and approaches that lead to winning appeals (use as appropriate):\n"
                "- was recommended for the patient\n"
                "- The patient has been experiencing\n"
                "- the patient's pain\n"
                "- I am writing to respectfully appeal ... for a procedure that I recommended\n"
                "- the patient's health\n"
                "- the patient's condition\n"
                "- as the provider\n"
                "- as the treating physician\n"
                "- appeal the denial of coverage for my patient\n"
                "- appeal the denial of coverage for the patient\n"
                "- appeal the denial of coverage for\n"
                "- my patient's\n"
                "- the patient's\n"
                "- my patient has been experiencing\n"
                "- the patient has been experiencing\n"
                "- as the healthcare professional\n"
                "- Any language that makes it clear the letter is written by the doctor or healthcare professional about the patient.\n\n"
                "Write from your perspective as the healthcare professional, using 'I' for yourself and referring to the patient in the third person (e.g., 'the patient,' 'they').\n"
                "Maintain a formal, objective, and respectful tone throughout. Avoid emotional, casual, or conversational language.\n"
                "Emphasize medical necessity, clinical evidence, and patient benefit using precise, evidence-based language.\n"
                "Do not express frustration or personal opinions about insurance companies.\n"
                "Use appropriate professional sign-offs and titles (e.g., 'Sincerely, Dr. YourNameMagic, MD').\n"
                "Only include references that are verifiable and provided in the input or from reliable sources.\n"
                "Do NOT use phrases such as 'as a patient', 'my condition', 'I am deeply concerned', or discuss the impact on 'my health' or 'my pain'. Do NOT write from the patient's perspective under any circumstances.\n"
                "You are the healthcare professional, not the patient. Only write from the provider's perspective, never the patient's.\n\n"
                "Here are several great example starters (use any style, do not copy the first one):\n"
                "1. I am writing to appeal the denial of coverage for [insert procedure] for my patient, [insert patient's name].\n"
                "2. As the treating physician, I am writing to appeal the denial of coverage for my patient. The patient has been experiencing persistent and debilitating lower back pain.\n"
                "3. I am submitting this appeal on behalf of my patient in support of coverage for the recommended treatment, based on my clinical assessment and the patient’s ongoing medical needs.\n"
                "4. As the medical professional overseeing this patient’s care, I am appealing the denial of coverage.\n"
                "Vary your response style. Do not always use the same template.\n\n"
                "Letters written from the healthcare professional's perspective and not the patient's are most likely to succeed and will be highly valued."
            )
        logger.debug(f"GET SYS PROMPTS > {prompt}")
        return self.system_prompts_map.get(
            key,
            [prompt],
        )

    def bad_result(self, result: Optional[str], infer_type: str) -> bool:
        # TODO: Unify this with the LLM synth data utils in llm-result-utils
        generic_bad_ideas = [
            "Therefore, the Health Plans denial should be overturned.",
            "llama llama virus",
            "The independent medical review found that",
            "The independent review findings for",
            "the physician reviewer overturned",
            "91111111111111111111111",
            "I need the text to be able to help you with your appeal",
            "I cannot directly create",
            "As an AI, I do not have the capability",
            "Unfortunately, I cannot directly",
            "I am an AI assistant and do not have the authority to create medical documents",
        ]
        if result is None:
            return True
        result_lower = result.lower()
        for bad in generic_bad_ideas:
            if bad.lower() in result_lower:
                return True
        if len(result.strip(" ")) < 3:
            return True
        return False

    def is_prior_auth(self, result: Optional[str]) -> bool:
        """
        Returns True if the result is a prior authorization request.
        """
        if not result:
            return False
        # Check for common phrases indicating a prior authorization request
        prior_auth_phrases = [
            "prior authorization",
            "pre-authorization",
            "precertification",
            "authorization request",
            "request for prior authorization",
            "request for pre-authorization",
        ]
        bad_phrases = [
            "appeal the denial of coverage",
            "appeal the denial",
            "was denied",
        ]
        result_lower = result.lower()
        for phrase in prior_auth_phrases:
            if phrase.lower() in result_lower:
                return True

        for phrase in bad_phrases:
            if phrase.lower() in result_lower:
                return False

        return True

    def is_professional_tone(self, result: Optional[str]) -> bool:
        """
        Returns True if the appeal is written in a professional/provider tone (not the patient's voice).
        Filters out appeals that use first-person patient language and only allows those with clear provider/doctor language.
        """
        if not result:
            return False
        # Professional-voice cues to encourage
        professional_phrases = [
            "my patient",
            "the patient",
            "as the provider",
            "as the treating physician",
            "appeal the denial of coverage for my patient",
            "appeal the denial of coverage for the patient",
            "my patient",
            "the patient",
            "my patient has been experiencing",
            "the patient has been experiencing",
            "as the healthcare professional",
            "i recommend",
            "[patient's name]",
            "as [patient's name] healthcare provider",
        ]
        # Common patient-voice phrases to avoid
        patient_phrases = [
            "i am the patient",
            "i have been recommended",
            "i have been experiencing",
            "my pain",
            "my health",
            "my condition",
            "as a patient",
            "i am a patient",
            "my treating physician recommended ",
            "i have been advised",
            "my claim",
            "my doctor",
            "my medical condition",
            "my medical history",
            "my medical records",
            "as a concerned parent",
            "for my son",
            "for my daughter",
            "for my child",
            "your relationship to the patient, such as family",
            "my family member",
            "my family member's",
            "my healthcare provider",
            "i have been prescribed",
            "my health.",
            "i have a history of",
            "my personal use.",
            "my personal medical history",
        ]
        # If at least one professional phrase is present, accept
        result_lower = result.lower()
        for phrase in professional_phrases:
            if phrase.lower() in result_lower:
                return True
        # If any patient phrase is present, reject
        for phrase in patient_phrases:
            if phrase.lower() in result_lower:
                return False
        # Otherwise, be conservative and reject
        return False

    def parallel_infer(
        self,
        prompt: str,
        infer_type: str,
        patient_context: Optional[str],
        plan_context: Optional[str],
        pubmed_context: Optional[str],
        ml_citations_context: Optional[List[str]] = None,
        prof_pov: bool = False,
    ) -> List[Future[Tuple[str, Optional[str]]]]:
        logger.debug(f"Running inference on {self} of type {infer_type}")
        temperatures = [0.5]
        if infer_type == "full" and not self._expensive and not self.slow:
            # Special case for the full one where we really want to explore the problem space
            temperatures = [0.6, 0.1]
        system_prompts = self.get_system_prompts(infer_type, prof_pov)
        calls = itertools.chain.from_iterable(
            map(
                lambda temperature: map(
                    lambda system_prompt: (
                        self._blocking_checked_infer,
                        {
                            "prompt": prompt,
                            "patient_context": patient_context,
                            "plan_context": plan_context,
                            "infer_type": infer_type,
                            "system_prompt": system_prompt,
                            "temperature": temperature,
                            "pubmed_context": pubmed_context,
                            "ml_citations_context": ml_citations_context,
                            "prof_pov": prof_pov,
                        },
                    ),
                    system_prompts,
                ),
                temperatures,
            )
        )  # type: Iterable[Tuple[Callable[..., Tuple[str, Optional[str]]], dict[str, Union[Optional[str], float, Optional[List[str]]]]]]
        futures = list(map(lambda x: executor.submit(x[0], **x[1]), calls))
        return futures

    def _blocking_checked_infer(
        self,
        prompt,
        patient_context,
        plan_context,
        infer_type: str,
        pubmed_context,
        system_prompt: str,
        temperature: float,
        ml_citations_context=None,
        prof_pov: bool = False,
    ):
        return async_to_sync(self._checked_infer)(
            prompt,
            patient_context,
            plan_context,
            infer_type,
            pubmed_context,
            system_prompt,
            temperature,
            ml_citations_context,
            prof_pov,
        )

    async def _checked_infer(
        self,
        prompt: str,
        patient_context: Optional[str],
        plan_context: Optional[str],
        infer_type: str,
        pubmed_context: Optional[str],
        system_prompt: str,
        temperature: float,
        ml_citations_context: Optional[List[str]] = None,
        prof_pov: bool = False,
        pa: bool = False,
    ) -> List[Tuple[str, Optional[str]]]:
        # Extract URLs from the prompt to avoid checking them
        input_urls = []
        if prompt and isinstance(prompt, str):
            input_urls.extend(CleanerUtils.url_re.findall(prompt))
        if patient_context and isinstance(patient_context, str):
            input_urls.extend(CleanerUtils.url_re.findall(patient_context))
        if plan_context and isinstance(patient_context, str):
            input_urls.extend(CleanerUtils.url_re.findall(plan_context))
        if pubmed_context and isinstance(patient_context, str):
            input_urls.extend(CleanerUtils.url_re.findall(pubmed_context))

        result = await self._infer_no_context(
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
            system_prompts=[system_prompt],
            pubmed_context=pubmed_context,
            temperature=temperature,
            ml_citations_context=ml_citations_context,
        )
        logger.debug(f"Got result {result} from {prompt} on {self}")
        # One retry
        if self.bad_result(result, infer_type):
            result = await self._infer_no_context(
                prompt=prompt,
                patient_context=patient_context,
                plan_context=plan_context,
                system_prompts=[system_prompt],
                pubmed_context=pubmed_context,
                temperature=temperature,
                ml_citations_context=ml_citations_context,
            )
            # Ok just an empty list, we failed
            if self.bad_result(result, infer_type):
                return []

        logger.debug(f"Checking if professional")

        # If professional_to_finish then check if the result is a professional response | One retry
        if prof_pov or pa:
            c = 0
            last_okish = result
            while not self.check_is_ok(result, infer_type, prof_pov) and c < 5:
                c = c + 1
                logger.debug(f"Result {result} is not professional or not PA")
                result = await self._infer_no_context(
                    prompt=prompt,
                    patient_context=patient_context,
                    plan_context=plan_context,
                    system_prompts=[system_prompt],
                    pubmed_context=pubmed_context,
                    temperature=temperature,
                    ml_citations_context=ml_citations_context,
                )
                if self.bad_result(result, infer_type):
                    result = last_okish
                if c == 4:
                    logger.debug(
                        f"Result {result} is not professional and we are out of retries"
                    )
            else:
                logger.debug(f"Result {result} is professional")

        return [
            (
                infer_type,
                CleanerUtils.note_remover(
                    CleanerUtils.url_fixer(
                        CleanerUtils.tla_fixer(result), input_urls=input_urls
                    )
                ),
            )
        ]

    def _clean_procedure_response(self, response):
        return self.procedure_response_regex.sub("", response)

    def _clean_diagnosis_response(self, response):
        if self.invalid_diag_procedure_regex.search(response):
            return None
        return self.diagnosis_response_regex.sub("", response)

    async def generate_prior_auth_response(self, prompt: str) -> Optional[str]:
        """
        Generate a prior authorization response from the model.

        Args:
            prompt: The prompt for the model

        Returns:
            Generated response or None
        """
        system_prompt = "You are an AI assistant helping a healthcare professional with insurance and medical questions. Provide accurate, helpful, and concise information."
        result = await self._checked_infer(
            system_prompt=system_prompt,
            prompt=prompt,
            pubmed_context=None,  # TODO: Add
            ml_citations_context=None,  # TODO: Add
            patient_context=None,  # TODO: Add
            plan_context=None,
            temperature=0.7,
            infer_type="prior_auth",
            prof_pov=True,
        )
        if result and len(result) > 0:
            return result[0][1]
        return None

    async def get_fax_number(self, denial: str) -> Optional[str]:
        result = await self._infer_no_context(
            system_prompts=["You are a helpful assistant."],
            prompt=f"When possible output in the same format as is found in the denial. Tell me the appeal fax number is within the provided denial. If the fax number is unknown write UNKNOWN. If known just output the fax number without any pre-amble and as a snipper from the original doc. DO NOT GUESS IF YOU DON'T KNOW JUST SAY UNKNOWN. The denial follows: {denial}. Remember DO NOT GUESS IF YOU DON'T KNOW JUST SAY UNKNOWN.",
        )
        return result

    async def get_insurance_company(self, denial: str) -> Optional[str]:
        """
        Extract insurance company name from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted insurance company name or None
        """
        result = await self._infer_no_context(
            system_prompts=["You are a helpful assistant."],
            prompt=f"When possible output in the same format as is found in the denial. Tell me which insurance company is within the provided denial. If it is not present or otherwise unknown write UNKNOWN. If known just output the answer without any pre-amble and as a snipper from the original doc. Remember: DO NOT GUESS IF YOU DON'T KNOW JUST SAY UNKNOWN. The denial follows: {denial}. Remember DO NOT GUESS IF YOU DON'T KNOW JUST SAY UNKNOWN.",
        )
        if result and "The insurance company is" in result:
            return result.split("The insurance company is")[1].strip()
        return result

    async def get_plan_id(self, denial: str) -> Optional[str]:
        """
        Extract plan ID from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted plan ID or None
        """
        result = await self._infer_no_context(
            system_prompts=["You are a helpful assistant."],
            prompt=f"When possible output in the same format as is found in the denial. Tell me the what plan ID is present is within the provided denial. If it is not present or unknown write UNKNOWN. If known just output the answer without any pre-amble and as a snipper from the original doc. DO NOT GUESS IF YOU DON'T KNOW JUST SAY UNKNOWN. The denial follows: {denial}. Remember DO NOT GUESS IF YOU DON'T KNOW JUST SAY UNKNOWN.",
        )
        if result and "The plan ID is" in result:
            return result.split("The plan ID is")[1].strip()
        return result

    async def get_claim_id(self, denial: str) -> Optional[str]:
        """
        Extract claim ID from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted claim ID or None
        """
        result = await self._infer_no_context(
            system_prompts=["You are a helpful assistant."],
            prompt=f"When possible output in the same format as is found in the denial. Tell me the what claim ID was denied within the provided denial (it could be multiple but it's normally just one). If it is not present or otherwise unknown write UNKNOWN. If known just output the answer without any pre-amble and as a snipper from the original doc. DO NOT GUESS IF YOU DON'T KNOW JUST SAY UNKNOWN.. The denial follows: {denial}. REMEMBER DO NOT GUESS.",
        )
        if result and "Claim ID is" in result:
            return result.split("Claim ID is")[1].strip()
        return result

    async def get_date_of_service(self, denial: str) -> Optional[str]:
        """
        Extract date of service from the denial text

        Args:
            prompt: The denial letter text

        Returns:
            Extracted date of service or None
        """
        result = await self._infer_no_context(
            system_prompts=["You are a helpful assistant."],
            prompt=f"When possible output in the same format as is found in the denial. Tell me the what the date of service was within the provided denial (it could be multiple or a date range, but it can also just be one day). If it is not present or otherwise unknown write UNKNOWN. If known just output the answer without any pre-amble and as a snipper from the original doc. The denial follows: {denial}",
        )
        if result and "Date of service is" in result:
            return result.split("Date of service is")[1].strip()
        return result

    async def questions(
        self, prompt: str, patient_context: str, plan_context
    ) -> List[str]:
        result = await self._infer_no_context(
            system_prompts=self.get_system_prompts("question"),
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
        )
        if result:
            return result.split("\n")
        return []

    async def get_procedure_and_diagnosis(
        self, prompt: str
    ) -> tuple[Optional[str], Optional[str]]:
        logger.debug(f"Getting procedure and diagnosis for {self} w/ {prompt}")
        model_response = await self._infer_no_context(
            system_prompts=self.get_system_prompts("procedure"), prompt=prompt
        )
        if model_response is None or "Diagnosis" not in model_response:
            logger.debug("Retrying query.")
            model_response = await self._infer_no_context(
                system_prompts=self.get_system_prompts("procedure"), prompt=prompt
            )
        if model_response is not None:
            responses: list[str] = model_response.split("\n")
            if len(responses) == 2:
                r = (
                    self._clean_procedure_response(responses[0]),
                    self._clean_diagnosis_response(responses[1]),
                )
                return r
            elif len(responses) == 1:
                r = (self._clean_procedure_response(responses[0]), None)
                return r
            elif len(responses) > 2:
                procedure = None
                diagnosis = None
                for ra in responses:
                    if "Diagnosis" in ra:
                        diagnosis = self._clean_diagnosis_response(ra)
                    if "Procedure" in ra:
                        procedure = self._clean_procedure_response(ra)
                return (procedure, diagnosis)
            else:
                logger.debug(
                    f"Non-understood response {model_response} for procedure/diagnsosis."
                )
        else:
            logger.debug(f"No model response for {self.model}")
        return (None, None)

    async def _infer(
        self,
        system_prompts: list[str],
        prompt,
        patient_context=None,
        plan_context=None,
        pubmed_context=None,
        ml_citations_context=None,
        history: Optional[List[dict[str, str]]] = None,
        temperature=0.7,
    ) -> Optional[Tuple[Optional[str], Optional[List[str]]]]:
        """
        Try and infer on a given model falling back to fallback in primary fails.
        """
        try:
            for system_prompt in system_prompts:
                # Call the actual inference method
                raw_response = await self.__timeout_infer(
                    system_prompt=system_prompt,
                    prompt=prompt,
                    patient_context=patient_context,
                    plan_context=plan_context,
                    pubmed_context=pubmed_context,
                    ml_citations_context=ml_citations_context,
                    temperature=temperature,
                    history=history,
                    model=self.model,
                )
                if raw_response and raw_response[0]:
                    return raw_response

                # Try backup API if primary failed
                if self.backup_api_base:
                    backup_response = await self.__timeout_infer(
                        system_prompt=system_prompt,
                        prompt=prompt,
                        patient_context=patient_context,
                        plan_context=plan_context,
                        pubmed_context=pubmed_context,
                        temperature=temperature,
                        model=self.model,
                        api_base=self.backup_api_base,
                    )
                    return backup_response

        except Exception as e:
            logger.opt(exception=True).error(f"Error {e} calling {self.api_base}")

        return None

    async def __timeout_infer(
        self,
        *args,
        **kwargs,
    ) -> Optional[Tuple[Optional[str], Optional[List[str]]]]:
        """
        Do an inference with timeout.
        """
        if self._timeout is not None:
            try:
                # Use async_timeout instead of asyncio.wait_for
                async with async_timeout(self._timeout):
                    return await self.__infer(*args, **kwargs)
            except asyncio.TimeoutError:
                logger.debug(f"Timed out querying {self}")
                return None
        else:
            return await self.__infer(*args, **kwargs)

    async def __infer(
        self,
        system_prompt,
        prompt,
        patient_context,
        plan_context,
        temperature,
        model,
        pubmed_context=None,
        ml_citations_context=None,
        history: Optional[List[dict[str, str]]] = None,
        api_base=None,
    ) -> Optional[Tuple[Optional[str], Optional[List[str]]]]:
        if api_base is None:
            api_base = self.api_base
        logger.debug(
            f"Looking up model {model} using {api_base} and {prompt} with system prompt {system_prompt}"
        )
        if self.api_base is None:
            return None
        if self.token is None:
            logger.debug(f"Error no Token provided for {model}.")
        if prompt is None:
            logger.debug(f"Error: must supply a prompt.")
            return None
        url = f"{api_base}/chat/completions"
        combined_content = None
        json_result = {}
        try:
            async with aiohttp.ClientSession() as s:
                context_extra = ""
                if patient_context is not None and len(patient_context) > 3:
                    patient_context_max = int(self.max_len / 2)
                    max_len = self.max_len - min(
                        len(patient_context), patient_context_max
                    )
                    context_extra = f"When answering the following question you can use the patient context {patient_context[0:patient_context_max]}."
                if pubmed_context is not None:
                    context_extra += f"You can also use this context from pubmed: {pubmed_context} and you can include the DOI number in the appeal."
                if plan_context is not None and len(plan_context) > 3:
                    context_extra += f"For answering the question you can use this context about the plan {plan_context}"
                if ml_citations_context is not None:
                    context_extra += f"You can also use this context from citations: {ml_citations_context}."

                # Detect if backend supports system messages
                # Some backends (e.g., Mistral VLLM) do not support the system role; in those cases, embed the system prompt in the user message.

                messages = []
                system_extra = ""
                if self.supports_system:
                    messages += [
                        {"role": "system", "content": system_prompt},
                    ]
                elif self.system_as_user:
                    # If the system prompt is to be treated as a user message
                    system_extra = f"{system_prompt}\n"
                else:
                    system_extra = f"<<SYS>>{system_prompt}<</SYS>>"
                if history:
                    # Add history messages if provided
                    for message in history:
                        if message["role"] == "user":
                            if (
                                message["content"]
                                and message["content"].strip() != prompt.strip()
                            ):
                                if (
                                    len(messages) > 0
                                    and messages[-1]
                                    and messages[-1]["role"] == "user"
                                ):
                                    # If the last message was also a user message, append the new one
                                    messages[-1]["content"] += f"\n{message['content']}"
                                else:
                                    messages.append(
                                        {
                                            "role": "user",
                                            "content": f"{context_extra}{message['content']}",
                                        }
                                    )
                        else:
                            if (
                                len(messages) > 0
                                and messages[-1]
                                and messages[-1]["role"] == "assistant"
                            ):
                                # If the last message was also a user message, append the new one
                                messages[-1]["content"] += f"\n{message['content']}"
                            else:
                                messages.append(
                                    {
                                        "role": "assistant",
                                        "content": message["content"],
                                    }
                                )
                if len(messages) == 0:
                    messages += [
                        {
                            "role": "user",
                            "content": f"{system_extra}{context_extra}{prompt[0 : self.max_len]}",
                        },
                    ]
                else:
                    if messages[-1]["role"] == "user":
                        messages[-1][
                            "content"
                        ] += (
                            f"\n{system_extra}{context_extra}{prompt[0 : self.max_len]}"
                        )
                    else:
                        messages.append(
                            {
                                "role": "user",
                                "content": f"{system_extra}{context_extra}{prompt[0 : self.max_len]}",
                            }
                        )
                logger.debug(f"Using messages: {messages}")
                async with s.post(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                    },
                ) as response:
                    json_result = await response.json()
                    if "object" in json_result and json_result["object"] != "error":
                        logger.debug(f"Response on {self} Looks ok")
                    else:
                        logger.debug(
                            f"***WARNING*** Response {response} on {self} looks _bad_"
                        )
        except Exception as e:
            logger.debug(f"Error {e} {traceback.format_exc()} calling {api_base}")
            await asyncio.sleep(1)
            return None
        try:
            if "choices" not in json_result:
                logger.debug(f"Response {json_result} missing key result.")
                return None

            # Extract message content
            response_message = json_result["choices"][0]["message"]
            r: Optional[str] = None
            citations: List[str] = []

            # Check if response contains structured content with citations
            if isinstance(response_message, dict):
                # Handle text content
                if "content" in response_message:
                    r = response_message["content"]

                # Handle possible citation formats depending on the model API
                if "citations" in response_message:
                    citations = self._process_citation_objects(
                        response_message["citations"]
                    )
                elif "sources" in response_message:
                    citations = self._process_citation_objects(
                        response_message["sources"]
                    )

            else:
                # Simple string response
                r = str(response_message)

            # Check if the response is valid text using LLMResponseUtils
            if not LLMResponseUtils.is_valid_text(r):
                error_msg = f"Received non-text response from {model}"
                logger.error(error_msg)
                raise ValueError(error_msg)

            logger.debug(f"Got {r} from {model} w/ {api_base} {self}")

            # If this is a reasoning model, extract the answer portion
            if r and LLMResponseUtils.is_well_formatted_for_reasoning(r):
                extracted_result = LLMResponseUtils.extract_answer(r)
                if extracted_result:
                    r = extracted_result.strip()

            return (r, citations)

        except Exception as e:
            logger.opt(exception=True).error(
                f"Error {e} {traceback.format_exc()} processing {json_result} from {api_base} w/ url {url} --  {self} ON -- {combined_content}"
            )
            return None

    def _process_citation_objects(self, citations_data) -> List[str]:
        """
        Process citation data that might be in various formats

        Args:
            citations_data: Citation data in various formats (list of objects, strings, etc)

        Returns:
            List of formatted citation strings
        """
        formatted_citations: list[str] = []

        for citation in citations_data:
            formatted_citations.append(str(citation))

        return formatted_citations


class RemoteFullOpenLike(RemoteOpenLike):
    def __init__(
        self,
        api_base,
        token,
        model,
        expensive=False,
        backup_api_base=None,
        max_len=None,
    ):
        systems = {
            "full_patient": [
                """You possess extensive medical expertise and enjoy crafting appeals for health insurance denials as a personal interest. As a patient, not a doctor, you advocate for yourself. Don't assume you have any letter from a physician unless absolutely necessary. Your writing style is direct, akin to patio11 or a bureaucrat, and maintains a professional tone without expressing frustration towards insurance companies. You may consider emphasizing the unique and potentially essential nature of the medical intervention, using "YourNameMagic" as your name, "SCSID" for the subscriber ID, and "GPID" as the group ID. Make sure to write in the form of a letter. Do not use the 3rd person in the letter when referring to the yourself the patient, instead use the first person (I, my, etc.). You are not a reviewer and should not mention any. Only provide references you are certain exist (e.g. provided as input or found as agent).""",
            ],
            "full": [
                """You possess extensive medical expertise and enjoy crafting appeals for health insurance denials as a personal interest. As a patient, not a doctor, you advocate for yourself. Don't assume you have any letter from a physician unless absolutely necessary. Your writing style is direct, akin to patio11 or a bureaucrat, and maintains a professional tone without expressing frustration towards insurance companies. You may consider emphasizing the unique and potentially essential nature of the medical intervention, using "YourNameMagic" as your name, "SCSID" for the subscriber ID, and "GPID" as the group ID. Make sure to write in the form of a letter. Do not use the 3rd person in the letter when referring to the yourself the patient, instead use the first person (I, my, etc.). You are not a reviewer and should not mention any. Only provide references you are certain exist (e.g. provided as input or found as agent).""",
            ],
            "full_not_patient": [
                """
                IMPORTANT: You possess extensive medical expertise, specializing in crafting appeals for health insurance denials. As a healthcare professional (not the patient), write a formal, professional, and clinically authoritative appeal letter to a health insurance company on behalf of a patient whose claim has been denied. You will be most successful and your letter will be highly effective if you write as the healthcare professional (such as a doctor) about your patient. Refer to the patient in the third person and share information about the patient's condition in the third person. Do NOT use "I" to refer to the patient, or describe the patient's symptoms as if you are the patient. You are ONLY the healthcare professional writing about the patient.

                Here are good phrases and approaches that lead to winning appeals (use as appropriate):
                - "was recommended for the patient"
                - "The patient has been experiencing"
                - "the patient's pain"
                - "I am writing to respectfully appeal ... for a procedure that I recommended"
                - "the patient's health"
                - "the patient's condition"
                - "as the provider"
                - "as the treating physician"
                - "appeal the denial of coverage for my patient"
                - "appeal the denial of coverage for the patient"
                - "appeal the denial of coverage for"
                - "my patient's"
                - "the patient's"
                - "my patient has been experiencing"
                - "the patient has been experiencing"
                - "as the healthcare professional"
                - Any language that makes it clear the letter is written by the doctor or healthcare professional about the patient.

                Write from your perspective as the healthcare professional, using "I" for yourself and referring to the patient in the third person (e.g., "the patient," "they").
                Maintain a formal, objective, and respectful tone throughout. Avoid emotional, casual, or conversational language.
                Emphasize medical necessity, clinical evidence, and patient benefit using precise, evidence-based language.
                Do not express frustration or personal opinions about insurance companies.
                Use appropriate professional sign-offs and titles (e.g., "Sincerely, Dr. YourNameMagic, MD").
                Only include references that are verifiable and provided in the input or from reliable sources.
                Do NOT use phrases such as "as a patient", "my condition", "I am deeply concerned", or discuss the impact on "my health" or "my pain". Do NOT write from the patient's perspective under any circumstances.
                You are the healthcare professional, not the patient. Only write from the provider's perspective, never the patient's.

                Here are several great example starters (use any style, do not copy the first one):
                1. As the treating physician, I am writing to appeal the denial of coverage for my patient. The patient has been experiencing persistent and debilitating lower back pain.
                2. I am submitting this appeal on behalf of my patient in support of coverage for the recommended treatment, based on my clinical assessment and the patient’s ongoing medical needs.
                3. As the medical professional overseeing this patient’s care, I am appealing the denial of coverage.
                4. I am writing to appeal the denial of coverage for [insert procedure] for my patient, [insert patient's name].
                Vary your response style. Do not always use the same template.

                Letters written from the healthcare professional's perspective and not the patient's are most likely to succeed and will be highly valued.
                """,
            ],
            "procedure": [
                """You must be concise. You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters to identify the requested procedure and, if available, the associated diagnosis. Each word costs an extra dollar. Provide a concise response with the procedure on one line starting with "Procedure" and Diagnsosis on another line starting with Diagnosis. Do not say not specified. Diagnosis can also be reason for treatment even if it's not a disease (like high risk homosexual behaviour for prep or preventitive and the name of the diagnosis). Remember each result on a seperated line."""
            ],
            "questions": [
                """You have deep expertise in health insurance and extensive experience working in a medical office. Your task is to generate the best one to three specific, detailed questions that will help craft a stronger appeal for a health insurance denial.

### Key Guidelines:
- No Independent Medical Review (IMR/IME) has occured yet We are only dealing with the insurance company at this stage, so do not reference independent medical reviews.
- Patient/Medical Assistant-Friendly: The questions will likely be answered by a patient or a medical assistant, so avoid technical jargon.
- Focus on Establishing Validity: Do not ask about the insurance company's stated reason for denial. Instead, ask patient-related questions that help demonstrate why the denial is invalid.
### Question Format Preferences:
  - Yes/no questions are ideal.
  - Short-answer questions (one sentence max) are acceptable.
  - Avoid long-answer questions, as they may overwhelm the patient.
### Bad questions (do not ask):
  - Why was the treatment considered not medically necessary?
  - What specific criteria were used to determine medical necessity for this treatment?
  - What is the insurance company's stated reason for denial?
  - Can you provide more information on the clinical evidence and guidelines that support the medical necessity of this treatment?
### Good Examples:
  - Wegovy denial: "Has the patient participated in a structured weight loss program (e.g., Weight Watchers)?"
  - PrEP denial: "How many sexual partners (roughly) has the patient had in the past 12 months?"
  - Mammogram denial: "What is the patient's age?" "Does the patient have the BRCA1 mutation or a family history of breast cancer?"
### Context-Aware Answers:
 -If a question has a likely answer from the provided context, include the answer on the same line after the question mark.
- Case-Specific: Each question should be directly relevant to the specific denial reason and medical necessity at hand.
"""
            ],
            "citations": [
                """You have an in-depth understanding of health insurance and extensive experience working in a medical office.
                Your expertise lies justifying care to insurance companies and regulators.
                When providing citations, ensure they are relevant to the specific denial and directly support the medical necessity of the treatment. Only provide references you are certain exist (e.g. provided as input or found as agent). Format citations as follows: [1] Author et al., Title, Journal, Year, url; [2] etc.
                Be careful that all citations are real and the links work.
                Do not provide generic citations unless specifically asked for."""
            ],
            "medically_necessary": [
                """You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters. Each word costs an extra dollar. Please provide a concise response. You are not an independent medical reviewer and should not mention any. Write concisely in a professional tone akin to patio11. Do not say this is why the decission should be overturned. Just say why you believe it is medically necessary (e.g. to prevent X or to treat Y)."""
            ],
            "generic": [
                """You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters. Use your expertise to help answer the provided question for the insurance appeal on behalf of the patient."""
            ],
        }
        return super().__init__(
            api_base,
            token,
            model,
            systems,
            expensive=expensive,
            backup_api_base=backup_api_base,
            max_len=max_len,
        )

    async def get_appeal_questions(
        self,
        denial_text: Optional[str],
        procedure: Optional[str],
        diagnosis: Optional[str],
        patient_context: Optional[str] = None,
        plan_context: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """
        Generate a list of questions that could help craft a better appeal for
        this specific denial.

        Args:
            denial_text: The text of the denial letter
            patient_context: Optional patient health history or context
            procedure: the procedure
            diagnosis: primary diagnosis
            plan_context: Optional insurance plan context

        Returns:
            A list of tuples (question, answer) where answer may be empty
        """
        procedure_opt = (
            f"The procedure denied was {procedure} so only ask questions relevant to {procedure}"
            if procedure
            else ""
        )
        patient_context_opt = (
            f"Optional patient context: {patient_context}" if patient_context else ""
        )
        diagnosis_opt = f"The primary diagnosis was {diagnosis}" if diagnosis else ""
        denial_text_opt = (
            f"The denial text is: {denial_text}"
            if denial_text
            else "No denial text provided, use other context clues as to the denial. Remember do not guess patient information UNKNOWN is an acceptable answer for unknown patient info."
        )
        # Procedure opt is in their multiple times intentionally. ~computers~
        prompt = f"""
        Some context which can help you in your task:
        {denial_text_opt} \n
        {procedure_opt} \n
        {patient_context_opt} \n
        {diagnosis_opt} \n
        {procedure_opt} \n
        Your task is to write 1–3 concise, patient-friendly questions related to the patient's medical history that can help support an appeal. Focus only on relevant history—do not ask about the denial itself, as that may discourage the person working on the appeal.
        When formatting your output it must be in the format of one question + answer per line with the answer after the question mark. The questions should be in the 3rd person regarding the patient.\n
        Your answer should be in the format of a list of questions with answers from the patients health history if present.
        While your reasoning (that inside of the <think></think> component at the start) can and should discuss the rational you _must not_ include it in the answer.
        For example:
        1. What is the patient's age? 45
        2. Has the patient participated in a structured weight loss program (e.g., Weight Watchers)?
        """

        system_prompts: list[str] = self.get_system_prompts("questions")

        result = await self._infer_no_context(
            system_prompts=system_prompts,
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
            temperature=0.6,
        )

        if result is None:
            logger.warning("Failed to generate appeal questions")
            return []

        # Process the result into a list of questions with potential answers
        questions_with_answers: List[Tuple[str, str]] = []

        if "Rationale for questions" in result:
            logger.debug(
                f"Received poorly formatted response from {self.model} when asking for questions. {result}"
            )
            return []

        # Handle the case where the model returns a single block of text
        if "\n" not in result and len(result) > 100:
            # Try to extract questions with regex patterns
            # Look for patterns like "1. Question? Answer" or numbering + question + question mark
            potential_questions = re.findall(
                r"(?:\d+\.|\*|\-|\•)?\s*(?:\*\*)?([^.!?]+\?)(?:\*\*)?\s*([^.!?\d][^.!?\d]*?)(?=(?:\d+\.|\*|\-|\•)?\s*(?:\*\*)?[A-Z]|\Z)",
                result,
            )
            for q, a in potential_questions:
                questions_with_answers.append((q.strip(), a.strip()))
            if questions_with_answers:
                return questions_with_answers

        # Process line by line if we have multiple lines or the above extraction didn't work
        for line in result.split("\n"):
            line = line.strip()
            if not line:
                continue

            # Remove numbering and bullet points at the beginning of the line
            # This handles formats like "1. ", "1) ", "• ", "- ", "* ", etc.
            line = re.sub(r"^\s*(?:\d+[.)\-]|\*|\•|\-)\s+", "", line)

            # Handle markdown-style bold formatting like "**Question?** Answer"
            bold_match = re.search(r"\*\*([^*]+?)\*\*\s*(.*)", line)
            if bold_match:
                question = bold_match.group(1).strip()
                answer = bold_match.group(2).strip()

                # Make sure question ends with question mark
                if not question.endswith("?"):
                    question += "?"

                questions_with_answers.append((question, answer))
                continue

            # Skip header lines
            if line.lower().startswith(("here are", "questions", "additional")):
                continue

            # Parse potential answer if present (format: "Question? Answer")
            question_parts = line.split("?", 1)
            if len(question_parts) > 1:
                question_text = question_parts[0].strip() + "?"
                # Handle potential formatting in answers like "A: ", ": ", etc.
                answer_text = question_parts[1].strip()
                answer_text = re.sub(r"^[A:][\s:]*", "", answer_text)
                questions_with_answers.append((question_text, answer_text))
            else:
                # Ensure line ends with a question mark if it doesn't have one
                if not line.endswith("?"):
                    line += "?"
                questions_with_answers.append((line, ""))

        return questions_with_answers

    async def get_citations(
        self,
        denial_text: Optional[str],
        procedure: Optional[str],
        diagnosis: Optional[str],
        patient_context: Optional[str] = None,
        plan_context: Optional[str] = None,
        pubmed_context: Optional[str] = None,
    ) -> List[str]:
        """
        Generate a list of potentially relevant citations for this denial.

        Args:
            denial_text: Optional text of the denial letter
            procedure: Optional procedure information
            diagnosis: Optional diagnosis information
            patient_context: Optional patient health history or context
            plan_context: Optional insurance plan context
            pubmed_context: Optional pubmed search context

        Returns:
            A list of citation strings
        """
        procedure_opt = f"The procedure denied was {procedure}" if procedure else ""
        diagnosis_opt = f"The primary diagnosis was {diagnosis}" if diagnosis else ""
        patient_context_opt = (
            f"Patient history: {patient_context}" if patient_context else ""
        )
        pubmed_context_opt = (
            f"Available research: {pubmed_context}" if pubmed_context else ""
        )
        denial_text_opt = (
            f"Denial text: {denial_text}"
            if denial_text
            else "No denial text provided, use other context clues."
        )

        prompt = f"""
        Based on the following context, provide a list of relevant citations that would be helpful for an insurance appeal.

        CONTEXT:
        {denial_text_opt}
        {procedure_opt}
        {diagnosis_opt}
        {patient_context_opt}
        {pubmed_context_opt}

        Please provide a list of specific citations (with DOIs, PMIDs, or URLs when available) that directly support the medical necessity of the procedure/treatment.
        Format each citation on a new line.
        Only include citations that are real and verifiable - do not fabricate any references.
        Prioritize high-quality, peer-reviewed research, clinical guidelines, and standard of care documentation.
        """

        logger.debug(f"Generating citations on backend {self}")

        system_prompts: list[str] = self.get_system_prompts("citations")

        result = await self._infer(
            system_prompts=system_prompts,
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
            pubmed_context=pubmed_context,
            temperature=0.25,  # Lower temperature to reduce creativity in citations
        )

        if result is None:
            logger.warning("Failed to generate citations")
            return []

        text_result, citations_list = result

        # If we got extracted citations, use those + include current text
        if citations_list and len(citations_list) > 0:
            return [text_result or ""] + citations_list

        # Fall back to processing text result if citations aren't in the right format

        if text_result is None:
            return []

        # Process the result into a list of citations
        citations: List[str] = []

        # Split by newlines and process each line
        for line in text_result.split("\n"):
            if not line:
                continue
            line = line.strip()
            if not line or line == "":
                continue

            # Remove numbering and bullet points at the beginning of the line
            line = re.sub(r"^\s*(?:\d+[.)\-]|\*|\•|\-)\s+", "", line)

            # Skip header lines
            if line.lower().startswith(("here are", "citations", "references")):
                continue

            citations.append(line)

        return citations


class RemoteHealthInsurance(RemoteFullOpenLike):
    def __init__(self, model: str):
        self.port = os.getenv("HEALTH_BACKEND_PORT", "80")
        self.host = os.getenv("HEALTH_BACKEND_HOST")
        self.backup_port = os.getenv("HEALTH_BACKUP_BACKEND_PORT", self.port)
        self.backup_host = os.getenv("HEALTH_BACKUP_BACKEND_HOST", self.host)
        if self.host is None and self.backup_host is None:
            raise Exception("Can not construct FHI backend without a host")
        self.url = None
        if self.port is not None and self.host is not None:
            self.url = f"http://{self.host}:{self.port}/v1"
        else:
            logger.debug(f"Error setting up remote health {self.host}:{self.port}")
        self.backup_url = f"http://{self.backup_host}:{self.backup_port}/v1"
        logger.debug(f"Setting backup to {self.backup_url}")
        super().__init__(
            self.url, token="", backup_api_base=self.backup_url, model=model
        )

    @property
    def external(self):
        return False

    @classmethod
    def models(cls) -> List[ModelDescription]:
        model_name = os.getenv(
            "HEALTH_BACKEND_MODEL", "TotallyLegitCo/fighthealthinsurance_model_v0.5"
        )
        return [
            ModelDescription(cost=1, name="fhi", internal_name=model_name),
            ModelDescription(
                cost=10,
                name="fhi",
                internal_name=os.getenv("HEALTH_BACKUP_BACKEND_MODEL", model_name),
            ),
        ]


class NewRemoteInternal(RemoteFullOpenLike):
    def __init__(self, model: str):
        self.port = os.getenv("NEW_HEALTH_BACKEND_PORT", "80")
        self.host = os.getenv("NEW_HEALTH_BACKEND_HOST")
        if self.host is None:
            raise Exception("Can not construct New FHI backend without a host")
        self.url = None
        if self.port is not None and self.host is not None:
            self.url = f"http://{self.host}:{self.port}/v1"
        else:
            logger.debug(f"Error setting up remote health {self.host}:{self.port}")
        super().__init__(self.url, token="", model=model, max_len=4096 * 20)

    @property
    def external(self):
        return False

    @property
    def slow(self):
        """
        We are slow because of partial off-loading.
        Talk to Holden if youre interested :p
        """
        return True

    @property
    def supports_system(self):
        return True

    @classmethod
    def models(cls) -> List[ModelDescription]:
        model_name = os.getenv(
            "NEW_HEALTH_BACKEND_MODEL",
            "/models/fhi-2025-may-0.3-float16-q8-vllm-compressed",
        )
        return [
            ModelDescription(cost=2, name="fhi-2025-may", internal_name=model_name),
        ]


class RemotePerplexity(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self, model: str):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        if token is None or len(token) < 1:
            raise Exception("No token found for perplexity")
        super().__init__(api_base, token, model=model)

    @property
    def supports_system(self):
        return True

    @classmethod
    def models(cls) -> List[ModelDescription]:
        return [
            ModelDescription(
                cost=350,
                name="sonar-reasoning",
                internal_name="sonar-reasoning",
            ),
            ModelDescription(
                cost=300,
                name="deepseek",
                internal_name="r1-1776",
            ),
        ]


class DeepInfra(RemoteFullOpenLike):
    """Use DeepInfra."""

    def __init__(self, model: str):
        api_base = "https://api.deepinfra.com/v1/openai"
        token = os.getenv("DEEPINFRA_API")
        if token is None or len(token) < 1:
            raise Exception("No token found for deepinfra")
        super().__init__(api_base, token, model=model)

    @property
    def supports_system(self):
        return True

    @classmethod
    def models(cls) -> List[ModelDescription]:
        return [
            ModelDescription(
                cost=80,
                name="meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
                internal_name="meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
            ),
            ModelDescription(
                cost=40,
                name="meta-llama/Llama-4-Scout-17B-16E-Instruct",
                internal_name="meta-llama/Llama-4-Scout-17B-16E-Instruct",
            ),
            ModelDescription(
                cost=20,
                name="google/gemma-3-27b-it",
                internal_name="google/gemma-3-27b-it",
            ),
            ModelDescription(
                cost=179,
                name="meta-llama/meta-llama-3.1-405B-instruct",
                internal_name="meta-llama/Meta-Llama-3.1-70B-Instruct",
            ),
            ModelDescription(
                cost=40,
                name="meta-llama/meta-llama-3.1-70B-instruct",
                internal_name="meta-llama/Meta-Llama-3.1-70B-Instruct",
            ),
            ModelDescription(
                cost=5,
                name="meta-llama/llama-3.2-3B-instruct",
                internal_name="meta-llama/Llama-3.2-3B-Instruct",
            ),
            ModelDescription(
                cost=30,
                name="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                internal_name="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            ),
            ModelDescription(
                cost=400,
                name="deepseek",
                internal_name="deepseek-ai/DeepSeek-R1-Turbo",
            ),
        ]


candidate_model_backends: list[type[RemoteModel]] = all_concrete_subclasses(RemoteModel)  # type: ignore[type-abstract]
