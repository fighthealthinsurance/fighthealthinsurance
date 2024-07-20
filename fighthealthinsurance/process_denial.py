import itertools
import concurrent
from concurrent.futures import Future
import csv
import os
import re
from abc import ABC, abstractmethod
from functools import cache, lru_cache
from typing import Tuple, List, Optional
import time

from typing_extensions import reveal_type

import icd10
import requests
from fighthealthinsurance.models import (
    AppealTemplates,
    DenialTypes,
    Diagnosis,
    PlanType,
    Procedures,
    Regulator,
)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)


# Process all of our "expert system" rules.


class DenialBase(ABC):
    @abstractmethod
    def get_denialtype(self, text):
        pass

    @abstractmethod
    def get_regulator(self, text):
        pass

    @abstractmethod
    def get_plan_type(self, text):
        pass


class ProcessDenialCodes(DenialBase):
    """Process the denial type based on the procedure codes."""

    def __init__(self):
        self.preventive_denial = DenialTypes.objects.filter(
            name="Preventive Care"
        ).get()
        # These will match many things which are not ICD10 codes or CPT codes but
        # then the lookup will hopefully fail.
        self.icd10_re = re.compile(
            "[\\(\\s:\\.\,]+([A-TV-Z][0-9][0-9AB]\\.?[0-9A-TV-Z]{0,4})[\\s:\\.\\)\,]",
            re.M | re.UNICODE,
        )
        self.cpt_code_re = re.compile(
            "[\\(\\s:\,]+(\\d{4,4}[A-Z0-9])[\\s:\\.\\)\,]", re.M | re.UNICODE
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
        except:
            self.preventive_diagnosis = {}

    def get_denialtype(self, text):
        """Get the denial type. For now short circuit logic."""
        icd_codes = self.icd10_re.finditer(text)
        for i in icd_codes:
            diag = i.group(1)
            tag = icd10.find(diag)
            if tag is not None:
                if re.search("preventive", tag.block_description, re.IGNORECASE):
                    return [self.preventive_denial]
                if diag in self.preventive_diagnosis:
                    return [self.preventive_denial]
        cpt_codes = self.cpt_code_re.finditer(text)
        for i in cpt_codes:
            code = i.group(1)
            if code in self.preventive_codes:
                return [self.preventive_denial]
        return []

    def get_regulator(self, text):
        return []

    def get_plan_type(self, text):
        return []


class RemoteModel(object):
    """
    For models which produce a "full" appeal tag them with "full"
    For medical reason (e.g. ones where we hybrid with our "expert system"
    ) tag them with "medically_necessary".
    """

    def infer(self, prompt, t):
        pass


class PalmAPI(RemoteModel):
    def bad_result(self, result) -> bool:
        return result is not None

    @cache
    def infer(self, prompt: str, t: str) -> List[Tuple[str, str]]:
        result = self._infer(prompt)
        if self.bad_result(result):
            result = self._infer(prompt)
        if result is not None:
            return [(t, result)]
        else:
            return []

    def get_procedure_and_diagnosis(self, prompt):
        return (None, None)

    def _infer(self, prompt) -> Optional[str]:
        API_KEY = os.getenv("PALM_KEY")
        if API_KEY is None:
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={API_KEY}"
        try:
            import requests

            s = requests.Session()
            result = s.post(url, json={"contents": [{"parts": [{"text": prompt}]}]})
            json_result = result.json()
            candidates = json_result["candidates"]
            return candidates[0]["content"]["parts"][0]["text"]
        except Exception as e:
            return None


class RemoteOpenLike(RemoteModel):
    def __init__(self, api_base, token, model, system_messages, backup_model=None):
        self.api_base = api_base
        self.token = token
        self.model = model
        self.system_messages = system_messages
        self.max_len = 4096 * 2
        self.procedure_response_regex = re.compile(
            r"\s*procedure\s*:?\s*", re.IGNORECASE
        )
        self.diagnosis_response_regex = re.compile(
            r"\s*diagnosis\s*:?\s*", re.IGNORECASE
        )
        self.backup_model = backup_model

    def bad_result(self, result: Optional[str]) -> bool:
        bad = "Therefore, the Health Plans denial should be overturned."
        if result is None:
            return True
        if bad in result:
            return True
        if len(result.strip(" ")) < 5:
            return True
        return False

    def parallel_infer(self, prompt: str, t: str):
        print(f"Running inference on {t}")
        temps = [0.7]
        if t == "full":
            # Special case for the full one where we really want to explore the problem space
            temps = [0.7, 0.2]
        calls = itertools.chain.from_iterable(
            map(
                lambda temp: map(
                    lambda sm: [self._checked_infer, prompt, t, sm, temp],
                    self.system_messages[t],
                ),
                temps,
            )
        )
        futures = list(map(lambda x: executor.submit(x[0], *x[1:]), calls))
        print(f"Returning {futures}")
        return futures

    @cache
    def _checked_infer(self, prompt: str, t, sm: str, temp):
        result = self._infer(prompt, sm, temp)
        # One retry
        if self.bad_result(result):
            result = self._infer(prompt, sm, temp)
        if self.bad_result(result):
            return []
        return [(t, result)]

    def _clean_procedure_response(self, response):
        return self.procedure_response_regex.sub("", response)

    def _clean_diagnosis_response(self, response):
        if (
            "Not available in" in response
            or "Not provided" in response
            or "Not specified" in response
            or "Not Applicable" in response
        ):
            return None
        return self.diagnosis_response_regex.sub("", response)

    @cache
    def get_procedure_and_diagnosis(
        self, prompt: str
    ) -> tuple[Optional[str], Optional[str]]:
        print(f"Getting procedure and diagnosis for {self} w/ {prompt}")
        if self.system_messages["procedure"] is None:
            print(f"No procedure message for {self.model} skipping")
            return (None, None)
        model_response = self._infer(self.system_messages["procedure"], prompt)
        if model_response is None or "Diagnosis" not in model_response:
            print("Retrying query.")
            model_response = self._infer(self.system_messages["procedure"], prompt)
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

    def _infer(self, system_prompt, prompt, temperature=0.7) -> Optional[str]:
        # Retry backup model if necessary
        try:
            r = self.__infer(system_prompt, prompt, temperature, self.model)
        except Exception as e:
            if self.backup_model is None:
                raise e
            else:
                return self.__infer(
                    system_prompt, prompt, temperature, self.backup_model
                )
        if r is None and self.backup_model is not None:
            return self.__infer(system_prompt, prompt, temperature, self.backup_model)
        else:
            return r

    def __infer(self, system_prompt, prompt, temperature, model) -> Optional[str]:
        print(f"Looking up model {model} using {self.api_base} and {prompt}")
        if self.token is None:
            print(f"Error no Token provided for {self.model}.")
        if prompt is None:
            print(f"Error: must supply a prompt.")
            return None
        url = f"{self.api_base}/chat/completions"
        try:
            import requests

            s = requests.Session()
            # Combine the message, Mistral's VLLM container does not like the system role anymore?
            # despite it still being fine-tuned with the system role.
            combined_content = (
                f"<<SYS>>{system_prompt}<</SYS>>{prompt[0 : self.max_len]}"
            )
            result = s.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": combined_content,
                        },
                    ],
                    "temperature": temperature,
                },
            )
            print(f"Got {result} on {self.api_base} {self}")
            json_result = result.json()
            if "object" in json_result and json_result["object"] != "error":
                print(f"Looks ok")
        except Exception as e:
            print(f"Error {e} calling {self.api_base}")
            return None
        try:
            r = json_result["choices"][0]["message"]["content"]
            print(f"Got {r} from {self.model} w/ {self.api_base} {self}")
            return r
        except Exception as e:
            print(
                f"Error {e} processing {json_result} from {self.api_base} w/ url {url} --  {self}"
            )
            return None


class RemoteFullOpenLike(RemoteOpenLike):
    def __init__(self, api_base, token, model, backup_model=None):
        systems = {
            "full": [
                """You possess extensive medical expertise and enjoy crafting appeals for health insurance denials as a personal interest. As a patient, not a doctor, you advocate for yourself. Your writing style is direct, akin to patio11 or a bureaucrat, and maintains a professional tone without expressing frustration towards insurance companies. You may consider emphasizing the unique and potentially essential nature of the medical intervention, using "YourNameMagic" as your name, "SCSID" for the subscriber ID, and "GPID" as the group ID. Make sure to write in the form of a letter. Ideally use the communication the style of patio11 / a being a Dangerous Professional means communicating in what might be a slightly adversarial context in a way which suggests that a bureaucracy take one's concerns seriously and escalate them to someone empowered to resolve them swiftly. You can be verbose. Start your response with Dear [Insurance Company];""",
                """You possess extensive medical expertise and enjoy crafting appeals for health insurance denials as a personal interest. As a patient, not a doctor, you advocate for yourself. Your writing style is direct, akin to patio11 or a bureaucrat, and maintains a professional tone without expressing frustration towards insurance companies. You may consider emphasizing the unique and potentially essential nature of the medical intervention, using "YourNameMagic" as your name, "SCSID" for the subscriber ID, and "GPID" as the group ID. Make sure to write in the form of a letter. Do not use the 3rd person when refering to the patient, instead use the first persion (I, my, etc.). You are not a review and should not mention any.""",
            ],
            "procedure": [
                """You must be concise. You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters to identify the requested procedure and, if available, the associated diagnosis. Each word costs an extra dollar. Provide a concise response with the procedure on one line starting with "Procedure" and Diagnsosis on another line starting with Diagnosis. Do not say not specified. Diagnosis can also be reason for treatment even if it's not a disease (like high risk homosexual behaviour for prep or preventitive and the name of the diagnosis). Remember each result on a seperated line."""
            ],
            "medically_necessary": [
                """You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters. Each word costs an extra dollar. Please provide a concise response. Do not use the 3rd person when refering to the patient (e.g. don't say "the patient", "patient's", "his", "hers"), instead use the first persion (I, my, mine,etc.) when talking about the patient. You are not a review and should not mention any. Write concisely in a professional tone akin to patio11. Do not say this is why the decission should be overturned. Just say why you believe it is medically necessary (e.g. to prevent X or to treat Y)."""
            ],
        }
        return super().__init__(api_base, token, model, systems, backup_model)

    def model_type(self) -> str:
        return "full"


class RemoteHealthInsurance(RemoteFullOpenLike):
    def __init__(self):
        self.port = os.getenv("HEALTH_BACKEND_PORT", "80")
        self.host = os.getenv("HEALTH_BACKEND_HOST")
        self.url = None
        if self.port is not None and self.host is not None:
            self.url = f"http://{self.host}:{self.port}/v1"
        else:
            print(f"Error setting up remote health {self.host}:{self.port}")
        self.model = os.getenv(
            "HEALTH_BACKEND_MODEL", "TotallyLegitCo/fighthealthinsurance_model_v0.5"
        )
        self.backup_model = os.getenv(
            "HEALTH_BACKUP_BACKEND_MODEL",
            "TotallyLegitCo/fighthealthinsurance_model_v0.3",
        )
        super().__init__(
            self.url, token="", model=self.model, backup_model=self.backup_model
        )


class RemoteTogetherAI(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.together.xyz"
        token = os.getenv("TOGETHER_KEY")
        model = "mistralai/Mixtral-8x7B-Instruct-v0.1"
        super().__init__(api_base, token, model)


class RemotePerplexityInstruct(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        model = "mistral-7b-instruct"
        super().__init__(api_base, token, model)


class RemotePerplexityMedReason(RemoteOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        model = "mistral-7b-instruct"
        medical_reason_message = "You are a doctor answering a friends question as to why a procedure is medically necessary."
        super().__init__(api_base, token, model, medical_reason_message)


class RemoteOpen(RemoteFullOpenLike):
    """Use RemoteOpen for denial magic calls a service"""

    def __init__(self):
        api_base = os.getenv("OPENAI_API_BASE")
        token = os.getenv("OPENAI_API_KEY")
        model = "HuggingFaceH4/zephyr-7b-beta"
        super().__init__(api_base, token, model)


class RemoteOpenInst(RemoteFullOpenLike):
    """Use RemoteOpen for denial magic calls a service"""

    def __init__(self):
        api_base = os.getenv("OPENAI_API_BASE")
        token = os.getenv("OPENAI_API_KEY")
        model = "mistralai/Mistral-7B-Instruct-v0.1"
        super().__init__(api_base, token, model)


class ProcessDenialRegex(DenialBase):
    """Process the denial type based on the regexes stored in the database."""

    def __init__(self):
        self.planTypes = PlanType.objects.all()
        self.regulators = Regulator.objects.all()
        self.denialTypes = DenialTypes.objects.all()
        self.diagnosis = Diagnosis.objects.all()
        self.procedures = Procedures.objects.all()
        self.templates = AppealTemplates.objects.all()

    def get_procedure(self, text):
        print(f"Getting procedure types for {text}")
        procedure = None
        for d in self.procedures:
            print(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    print("positive regex match")
                    return s.groups("procedure")[0]
        return None

    def get_diagnosis(self, text):
        print(f"Getting diagnosis types for {text}")
        procedure = None
        for d in self.diagnosis:
            print(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    print("positive regex match")
                    return s.groups("diagnosis")[0]
        return None

    def get_procedure_and_diagnosis(self, text):
        print("Getting procedure and diagnosis")
        return (self.get_procedure(text), self.get_diagnosis(text))

    def get_denialtype(self, text):
        print(f"Getting denial types for {text}")
        denials = []
        for d in self.denialTypes:
            print(f"Exploring {d} w/ {d.regex} & {d.negative_regex}")
            if d.regex.pattern != "" and d.regex.search(text) is not None:
                print("positive regex match")
                if (
                    d.negative_regex.pattern == ""
                    or d.negative_regex.search(text) is None
                ):
                    print("no negative regex match!")
                    denials.append(d)
        print(f"Collected: {denials}")
        return denials

    def get_regulator(self, text):
        regulators = []
        for r in self.regulators:
            if (
                r.regex.search(text) is not None
                and r.negative_regex.search(text) is None
            ):
                regulators.append(r)
        return regulators

    def get_plan_type(self, text):
        plans = []
        for p in self.planTypes:
            if p.regex.pattern != "" and p.regex.search(text) is not None:
                print(f"positive regex match for plan {p}")
                if p.negative_regex != "" or p.negative_regex.search(text) is None:
                    plans.append(p)
            else:
                print(f"no match {p}")
        return plans

    def get_appeal_templates(self, text):
        templates = []
        for t in self.templates:
            if t.regex.pattern != "" and t.regex.search(text) is not None:
                templates.append(t)
                print("yay match")
            else:
                print(f"no match on {t.regex.pattern}")
        return templates


class AppealTemplateGenerator(object):
    def __init__(self, prefaces, main, footer):
        self.prefaces = prefaces
        self.main = main
        self.footer = footer
        self.combined = str("\n".join(prefaces + main + footer))
        print(f"Template {self.combined} on {self} ")

    def generate_static(self):
        print(f"Generating static on {self}")
        if "{medical_reason}" not in self.combined and self.combined != "":
            return self.combined
        else:
            return None

    def generate(self, medical_reason):
        print(f"Generating non-static on {self}")
        result = self.combined.replace("{medical_reason}", medical_reason)
        if result != "":
            return result
        else:
            return None


class AppealGenerator(object):
    def __init__(self):
        self.regex_denial_processor = ProcessDenialRegex()
        self.perplexity = RemotePerplexityInstruct()
        self.perplexity_med = RemotePerplexityMedReason()
        self.anyscale = RemoteOpen()
        self.anyscale2 = RemoteOpenInst()
        self.remotehealth = RemoteHealthInsurance()
        self.together = RemoteTogetherAI()
        self.palm = PalmAPI()

    def get_procedure_and_diagnosis(self, denial_text=None):
        prompt = self.make_open_procedure_prompt(denial_text)
        models_to_try = [
            self.regex_denial_processor,
            #            self.perplexity,
            #            self.together,
            #            self.anyscale,
            self.remotehealth,
            #            self.palm,
        ]
        procedure = None
        diagnosis = None
        for model in models_to_try:
            print(f"Exploring model {model}")
            procedure_diagnosis = model.get_procedure_and_diagnosis(denial_text)
            if procedure_diagnosis is not None:
                if len(procedure_diagnosis) > 1:
                    procedure = procedure or procedure_diagnosis[0]
                    diagnosis = diagnosis or procedure_diagnosis[1]
                else:
                    print(
                        f"Unexpected procedure diagnosis len on {procedure_diagnosis}"
                    )
                if procedure is not None and diagnosis is not None:
                    print(f"Return with procedure {procedure} and {diagnosis}")
                    return (procedure, diagnosis)
                else:
                    print(f"So far infered {procedure} and {diagnosis}")
        print(f"Fell through :/ could not fully populate.")
        return (procedure, diagnosis)

    def make_open_procedure_prompt(self, denial_text=None):
        if denial_text is not None:
            return f"What was the procedure/treatment and what is the diagnosis from the following denial (remember to provide two strings seperated by MAGIC as your response): {denial_text}"
        else:
            return None

    def make_open_prompt(
        self, denial_text=None, procedure=None, diagnosis=None
    ) -> Optional[str]:
        if denial_text is None:
            return None
        start = "Write a health insurance appeal for the following denial:"
        if (
            procedure is not None
            and procedure != ""
            and diagnosis is not None
            and diagnosis != ""
        ):
            start = f"Write a health insurance appeal for procedure {procedure} with diagnosis {diagnosis} given the following denial:"
        elif procedure is not None and procedure != "":
            start = f"Write a health insurance appeal for procedure {procedure} given the following denial:"
        return f"{start}\n{denial_text}"

    def make_open_med_prompt(self, procedure=None, diagnosis=None) -> Optional[str]:
        if procedure is not None and len(procedure) > 3:
            if diagnosis is not None and len(diagnosis) > 3:
                return f"Why is {procedure} medically necessary for {diagnosis}?"
            else:
                return f"Why is {procedure} is medically necessary?"
        else:
            return None

    def make_appeals(self, denial, template_generator, medical_reasons=[]):
        open_prompt = self.make_open_prompt(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )
        open_medically_necessary_prompt = self.make_open_med_prompt(
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )

        # TODO: use the streaming and cancellable APIs (maybe some fancy JS on the client side?)
        generated_futures = []

        # For any model that we have a prompt for try to call it
        def get_model_result(
            model: RemoteModel, prompt: str, t: str
        ) -> List[Future[str, Optional[str]]]:
            print(f"Looking up on {model}")
            if prompt is None:
                print(f"No prompt for {model} skipping")
                return []
            # If the model has parallelism use it
            results = None
            try:
                if isinstance(model, RemoteOpenLike):
                    reveal_type(model)
                    results = model.parallel_infer(prompt, t)
                else:
                    results = [executor.submit(model.infer, prompt, t)]
            except:
                results = [executor.submit(model.infer, prompt, t)]
            print(f"Infered {results} for {model}-{t} using {prompt}")
            print("Yay!")
            return results

        calls = [
            [self.remotehealth, open_prompt, "full"],
            #            [self.together, open_prompt],
            #            [self.palm, open_prompt],
        ]

        backup_calls = [
            #            [self.perplexity, open_prompt],
            #            [self.anyscale, open_prompt],
            #            [self.anyscale2, open_prompt],
            #            [self.runpod, open_prompt],
        ]
        # If we need to know the medical reason ask our friendly LLMs
        static_appeal = template_generator.generate_static()
        initial_appeals = []
        if static_appeal is None:
            calls.extend(
                [
                    [
                        self.remotehealth,
                        open_medically_necessary_prompt,
                        "medically_necessary",
                    ],
                ]
            )
        else:
            # Otherwise just put in as is.
            initial_appeals.append(static_appeal)
        for reason in medical_reasons:
            print(f"Using reason {reason}")
            appeal = template_generator.generate(reason)
            initial_appeals.append(appeal)

        print(f"Initial appeal {initial_appeals}")
        # Executor map wants a list for each parameter.

        def make_calls_async(calls):
            print(f"Calling models: {calls}")
            generated_futures = itertools.chain.from_iterable(
                map(lambda x: get_model_result(*x), calls)
            )

            def generated_to_appeals_text(k_text):
                model_results = k_text.result()
                for k, text in model_results:
                    if text is None:
                        pass
                    # It's either full or a reason to plug into a template
                    if k == "full":
                        yield text
                    else:
                        yield template_generator.generate(text)

            generated_text = map(
                generated_to_appeals_text,
                concurrent.futures.as_completed(generated_futures),
            )
            return generated_text

        generated_text = make_calls_async(calls)
        appeals = itertools.chain([initial_appeals], generated_text)
        # Check and make sure we have a result
        try:
            appeals = itertools.chain([appeals.__next__()], appeals)
        except StopIteration:
            appeals = make_calls_async(backup_calls)
        print(f"Sending back {appeals}")
        return itertools.chain.from_iterable(appeals)
