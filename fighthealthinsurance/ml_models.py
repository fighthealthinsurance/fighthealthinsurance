from abc import ABC, abstractmethod
import asyncio
import aiohttp
import itertools
import os
import re
import traceback
from concurrent.futures import Future
from dataclasses import dataclass
from functools import cache
from typing import Callable, List, Optional, Tuple, Iterable, Union, Awaitable
import requests
from stopit import ThreadingTimeout as Timeout

from fighthealthinsurance.exec import *
from fighthealthinsurance.utils import all_concrete_subclasses, url_fixer
from fighthealthinsurance.process_denial import DenialBase


class RemoteModelLike(DenialBase):
    def infer(self, prompt, patient_context, plan_context, pubmed_context, infer_type):
        return None

    @abstractmethod
    async def _infer(
        self,
        system_prompt,
        prompt,
        patient_context=None,
        plan_context=None,
        pubmed_context=None,
        temperature=0.7,
    ) -> Optional[str]:
        await asyncio.sleep(0)  # yield
        return None

    async def get_denialtype(self, denial_text, procedure, diagnosis):
        await asyncio.sleep(0)  # yield
        return None

    async def get_regulator(self, text) -> Optional[str]:
        await asyncio.sleep(0)  # yield
        return None

    async def get_plan_type(self, text) -> Optional[str]:
        await asyncio.sleep(0)  # yield
        return None

    async def get_procedure_and_diagnosis(
        self, prompt
    ) -> Tuple[Optional[str], Optional[str]]:
        await asyncio.sleep(0)  # yield
        return (None, None)

    async def get_fax_number(self, prompt) -> Optional[str]:
        return await asyncio.sleep(0, result=None)

    def external(self):
        return True


@dataclass(kw_only=True)
class ModelDescription:
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
        return []

    def bad_result(self, result: Optional[str]) -> bool:
        return False

    def tla_fixer(self, result: Optional[str]) -> Optional[str]:
        """Fix incorrectly picked TLAs from the LLM."""
        if result is None:
            return None
        else:
            m = re.search(
                "([A-Z])\\w+ ([A-Z])\\w+ ([A-Z])\\w+ \\(([A-Z]{3})\\)", result
            )
            if m is not None:
                tla = m.group(1) + m.group(2) + m.group(3)
                if tla != m.group(4):
                    return re.sub(f"(?<=[\\.\\( ]){m.group(4)}", tla, result)
            return result

    def note_remover(self, result: Optional[str]) -> Optional[str]:
        """Remove the last line note because we'll put similar content up earlier anyways"""
        if result is None:
            return None
        else:
            return re.sub(r"\n\s*\**\s*Note.*\Z", "", result)


class RemoteOpenLike(RemoteModel):

    _expensive = False

    def __init__(
        self,
        api_base,
        token,
        model,
        system_prompts,
        backup_api_base=None,
        expensive=False,
    ):
        self.api_base = api_base
        self.token = token
        self.model = model
        self.system_prompts = system_prompts
        self.max_len = 4096 * 8
        self._timeout = 90
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

    def bad_result(self, result: Optional[str]) -> bool:
        bad_ideas = [
            "Therefore, the Health Plans denial should be overturned.",
            "I am writing on behalf of",
        ]
        if result is None:
            return True
        for bad in bad_ideas:
            if bad in result:
                return True
        if len(result.strip(" ")) < 5:
            return True
        return False

    def parallel_infer(
        self,
        prompt: str,
        patient_context: Optional[str],
        plan_context: Optional[str],
        pubmed_context: Optional[str],
        infer_type: str,
    ) -> List[Future[Tuple[str, Optional[str]]]]:
        print(f"Running inference on {self} of type {infer_type}")
        temperatures = [0.5]
        if infer_type == "full" and not self._expensive:
            # Special case for the full one where we really want to explore the problem space
            temperatures = [0.6, 0.1]
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
                        },
                    ),
                    self.system_prompts[infer_type],
                ),
                temperatures,
            )
        )  # type: Iterable[Tuple[Callable[..., Tuple[str, Optional[str]]], dict[str, Union[Optional[str], float]]]]
        futures = list(map(lambda x: executor.submit(x[0], **x[1]), calls))
        return futures

    def _blocking_checked_infer(
        self,
        prompt: str,
        patient_context,
        plan_context,
        infer_type,
        pubmed_context,
        system_prompt: str,
        temperature: float,
    ):
        return asyncio.run(
            self._checked_infer(
                prompt,
                patient_context,
                plan_context,
                infer_type,
                pubmed_context,
                system_prompt,
                temperature,
            )
        )

    async def _checked_infer(
        self,
        prompt: str,
        patient_context,
        plan_context,
        infer_type,
        pubmed_context,
        system_prompt: str,
        temperature: float,
    ):
        result = await self._infer(
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
            system_prompt=system_prompt,
            pubmed_context=pubmed_context,
            temperature=temperature,
        )
        print(f"Got result {result} from {prompt} on {self}")
        # One retry
        if self.bad_result(result):
            result = await self._infer(
                prompt=prompt,
                patient_context=patient_context,
                plan_context=plan_context,
                system_prompt=system_prompt,
                pubmed_context=pubmed_context,
                temperature=temperature,
            )
        if self.bad_result(result):
            return []
        return [(infer_type, self.note_remover(url_fixer(self.tla_fixer(result))))]

    def _clean_procedure_response(self, response):
        return self.procedure_response_regex.sub("", response)

    def _clean_diagnosis_response(self, response):
        if self.invalid_diag_procedure_regex.search(response):
            return None
        return self.diagnosis_response_regex.sub("", response)

    async def get_fax_number(self, denial: str) -> Optional[str]:
        return await self._infer(
            system_prompt="You are a helpful assistant.",
            prompt=f"Tell me the to appeal fax number is within the provided denial. If the fax number is unknown write UNKNOWN. If known just output the fax number without any pre-amble and as a snipper from the original doc. The denial follows: {denial}",
        )

    async def questions(
        self, prompt: str, patient_context: str, plan_context
    ) -> List[str]:
        result = await self._infer(
            system_prompt=self.system_prompts["questions"],
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
        )
        if result is None:
            return []
        else:
            return result.split("\n")

    async def get_procedure_and_diagnosis(
        self, prompt: str
    ) -> tuple[Optional[str], Optional[str]]:
        print(f"Getting procedure and diagnosis for {self} w/ {prompt}")
        if self.system_prompts["procedure"] is None:
            print(f"No procedure message for {self.model} skipping")
            return (None, None)
        model_response = await self._infer(
            system_prompt=self.system_prompts["procedure"], prompt=prompt
        )
        if model_response is None or "Diagnosis" not in model_response:
            print("Retrying query.")
            model_response = await self._infer(
                system_prompt=self.system_prompts["procedure"], prompt=prompt
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
                print(
                    f"Non-understood response {model_response} for procedure/diagnsosis."
                )
        else:
            print(f"No model response for {self.model}")
        return (None, None)

    async def _infer(
        self,
        system_prompt,
        prompt,
        patient_context=None,
        plan_context=None,
        pubmed_context=None,
        temperature=0.7,
    ) -> Optional[str]:
        r = await self.__timeout_infer(
            system_prompt=system_prompt,
            prompt=prompt,
            patient_context=patient_context,
            plan_context=plan_context,
            pubmed_context=pubmed_context,
            temperature=temperature,
            model=self.model,
        )
        if r is None and self.backup_api_base is not None:
            r = await self.__timeout_infer(
                system_prompt=system_prompt,
                prompt=prompt,
                patient_context=patient_context,
                plan_context=plan_context,
                temperature=temperature,
                pubmed_context=pubmed_context,
                model=self.model,
                api_base=self.backup_api_base,
            )
        return r

    async def __timeout_infer(
        self,
        *args,
        **kwargs,
    ) -> Optional[str]:
        if self._timeout is not None:
            with Timeout(self._timeout) as timeout_ctx:
                return await self.__infer(*args, **kwargs)
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
        api_base=None,
    ) -> Optional[str]:
        if api_base is None:
            api_base = self.api_base
        print(f"Looking up model {model} using {api_base} and {prompt}")
        if self.api_base is None:
            return None
        if self.token is None:
            print(f"Error no Token provided for {model}.")
        if prompt is None:
            print(f"Error: must supply a prompt.")
            return None
        url = f"{api_base}/chat/completions"
        combined_content = None
        json_result = {}
        try:
            async with aiohttp.ClientSession() as s:
                # Combine the message, Mistral's VLLM container does not like the system role anymore?
                # despite it still being fine-tuned with the system role.
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
                combined_content = f"<<SYS>>{system_prompt}<</SYS>>{context_extra}{prompt[0 : self.max_len]}"
                print(f"Using {combined_content}")
                async with s.post(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "user",
                                "content": combined_content,
                            },
                        ],
                        "temperature": temperature,
                    },
                ) as response:
                    json_result = await response.json()
                    if "object" in json_result and json_result["object"] != "error":
                        print(f"Response on {self} Looks ok")
                    else:
                        print(
                            f"***WARNING*** Response {response} on {self} looks _bad_"
                        )
        except Exception as e:
            print(f"Error {e} {traceback.format_exc()} calling {api_base}")
            return None
        try:
            r: str = json_result["choices"][0]["message"]["content"]
            print(f"Got {r} from {model} w/ {api_base} {self}")
            return r
        except Exception as e:
            print(
                f"Error {e} {traceback.format_exc()} processing {json_result} from {api_base} w/ url {url} --  {self} ON -- {combined_content}"
            )
            return None


class RemoteFullOpenLike(RemoteOpenLike):
    def __init__(
        self,
        api_base,
        token,
        model,
        expensive=False,
        backup_api_base=None,
    ):
        systems = {
            "full": [
                #                """You possess extensive medical expertise and enjoy crafting appeals for health insurance denials as a personal interest. As a patient, not a doctor, you advocate for yourself. Don't assume you have any letter from a physician. Your writing style is direct, akin to patio11 or a bureaucrat, and maintains a professional tone without expressing frustration towards insurance companies. You may consider emphasizing the unique and potentially essential nature of the medical intervention, using "YourNameMagic" as your name, "SCSID" for the subscriber ID, and "GPID" as the group ID. Make sure to write in the form of a letter. Ideally use the communication the style of patio11 / a being a Dangerous Professional means communicating in what might be a slightly adversarial context in a way which suggests that a bureaucracy take one's concerns seriously and escalate them to someone empowered to resolve them swiftly. You can be verbose. Start your response with Dear [Insurance Company];""",
                """You possess extensive medical expertise and enjoy crafting appeals for health insurance denials as a personal interest. As a patient, not a doctor, you advocate for yourself. Don't assume you have any letter from a physician unless absolutely necessary. Your writing style is direct, akin to patio11 or a bureaucrat, and maintains a professional tone without expressing frustration towards insurance companies. You may consider emphasizing the unique and potentially essential nature of the medical intervention, using "YourNameMagic" as your name, "SCSID" for the subscriber ID, and "GPID" as the group ID. Make sure to write in the form of a letter. Do not use the 3rd person when refering to the patient, instead use the first persion (I, my, etc.). You are not a review and should not mention any. Only provide references you are certain exist (e.g. provided as input or found as agent).""",
            ],
            "procedure": [
                """You must be concise. You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters to identify the requested procedure and, if available, the associated diagnosis. Each word costs an extra dollar. Provide a concise response with the procedure on one line starting with "Procedure" and Diagnsosis on another line starting with Diagnosis. Do not say not specified. Diagnosis can also be reason for treatment even if it's not a disease (like high risk homosexual behaviour for prep or preventitive and the name of the diagnosis). Remember each result on a seperated line."""
            ],
            "questions": [
                """Given the appeal and the context what questions should we ask the user to provide context to help generate their appeal? Put each question on a new line."""
            ],
            "medically_necessary": [
                """You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters. Each word costs an extra dollar. Please provide a concise response. Do not use the 3rd person when refering to the patient (e.g. don't say "the patient", "patient's", "his", "hers"), instead use the first persion (I, my, mine,etc.) when talking about the patient. You are not a review and should not mention any. Write concisely in a professional tone akin to patio11. Do not say this is why the decission should be overturned. Just say why you believe it is medically necessary (e.g. to prevent X or to treat Y)."""
            ],
            "generic": [
                """You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters. Help a patient answer the provided question for their insurance appeal."""
            ],
        }
        return super().__init__(
            api_base,
            token,
            model,
            systems,
            expensive=expensive,
            backup_api_base=backup_api_base,
        )


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
            print(f"Error setting up remote health {self.host}:{self.port}")
        self.backup_url = f"http://{self.backup_host}:{self.backup_port}/v1"
        print(f"Setting backup to {self.backup_url}")
        super().__init__(
            self.url, token="", backup_api_base=self.backup_url, model=model
        )

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
                cost=2,
                name="fhi",
                internal_name=os.getenv("HEALTH_BACKUP_BACKEND_MODEL", model_name),
            ),
        ]


class RemoteTogetherAI(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self, model: str):
        api_base = "https://api.together.xyz"
        token = os.getenv("TOGETHER_KEY")
        if token is None or len(token) < 1:
            raise Exception("No token found for together")
        super().__init__(api_base, token, model=model)

    @classmethod
    def models(cls) -> List[ModelDescription]:
        return [
            ModelDescription(
                cost=350,
                name="meta-llama/llama-3.2-405B-instruct",
                internal_name="meta-llama/Meta-Llama-3.2-405B-Instruct-Turbo",
            ),
            ModelDescription(
                cost=350,
                name="meta-llama/llama-3.1-405B-instruct",
                internal_name="meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
            ),
            ModelDescription(
                cost=88,
                name="meta-llama/llama-3.2-70B-instruct",
                internal_name="meta-llama/Meta-Llama-3.2-70B-Instruct-Turbo",
            ),
            ModelDescription(
                cost=88,
                name="meta-llama/llama-3.1-70b-instruct",
                internal_name="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            )
        ]


class RemotePerplexity(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self, model: str):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        if token is None or len(token) < 1:
            raise Exception("No token found for perplexity")
        super().__init__(api_base, token, model=model)

    @classmethod
    def models(cls) -> List[ModelDescription]:
        return [
            ModelDescription(
                cost=500,
                name="perplexity",
                internal_name="llama-3.1-sonar-huge-128k-online",
            ),
            ModelDescription(
                cost=400,
                name="perplexity",
                internal_name="llama-3.1-sonar-large-128k-online",
            ),
            ModelDescription(
                cost=100,
                name="meta-llama/llama-3.1-70b-instruct",
                internal_name="llama-3.1-70b-instruct",
            ),
            ModelDescription(
                cost=20,
                name="meta-llama/llama-3.1-8b-instruct",
                internal_name="llama-3.1-8b-instruct",
            ),
        ]


class DeepInfra(RemoteFullOpenLike):
    """Use DeepInfra."""

    def __init__(self, model: str):
        api_base = "https://api.deepinfra.com/v1/openai/chat/completions"
        token = os.getenv("DEEPINFRA_API")
        if token is None or len(token) < 1:
            raise Exception("No token found for deepinfra")
        super().__init__(api_base, token, model=model)

    @classmethod
    def models(cls) -> List[ModelDescription]:
        return [
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
                internal_name="meta-llama/Llama-3.3-70B-Instruct-Turbo")
        ]


candidate_model_backends: list[type[RemoteModel]] = all_concrete_subclasses(RemoteModel)  # type: ignore[type-abstract]
