import itertools
import concurrent
from concurrent.futures import Future
import csv
import os
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
from fighthealthinsurance.process_denial import *
from fighthealthinsurance.ml_models import *

executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)


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
        self.anyscale = RemoteOpen()
        self.anyscale2 = RemoteOpenInst()
        self.remotehealth = RemoteHealthInsurance()
        self.together = RemoteTogetherAI()
        self.palm = PalmAPI()
        self.octoai = OctoAI()

    def get_procedure_and_diagnosis(self, denial_text=None):
        prompt = self.make_open_procedure_prompt(denial_text)
        models_to_try = [
            self.regex_denial_processor,
            self.remotehealth,
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
        self,
        denial_text=None,
        procedure=None,
        diagnosis=None,
        is_trans=False,
    ) -> Optional[str]:
        if denial_text is None:
            return None
        base = ""
        if is_trans:
            base = "While answering the question keep in mind the patient is trans."
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

    def make_appeals(self, denial, template_generator, medical_reasons=[], medical_context=""):
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
                model: RemoteModel, prompt: str, patient_context: Optional[str], t: str
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
                    results = model.parallel_infer(prompt, t, patient_context)
                else:
                    results = [executor.submit(model.infer, prompt, t)]
            except:
                results = [executor.submit(model.infer, prompt, t)]
            print(f"Infered {results} for {model}-{t} using {prompt}")
            print("Yay!")
            return results

        calls = [
            [self.remotehealth, open_prompt, medical_context, "full"],
        ]

        if denial.use_external:
            calls.extend([[self.octoai, open_prompt, medical_context, "full"]])

        # TODO: Add another external backup for external models.
        backup_calls = []
        # If we need to know the medical reason ask our friendly LLMs
        static_appeal = template_generator.generate_static()
        initial_appeals = []
        if static_appeal is None:
            calls.extend(
                [
                    [
                        self.remotehealth,
                        open_medically_necessary_prompt,
                        medical_context,
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
                print(f"Got result {model_results}")
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
