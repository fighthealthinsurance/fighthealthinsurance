import tempfile
from metapub import PubMedFetcher, FindIt
import PyPDF2
from stopit import ThreadingTimeout as Timeout
import random
import concurrent
import csv
import itertools
import os
import time
import traceback
from concurrent.futures import Future
from functools import cache, lru_cache
from typing import Any, List, Optional, Tuple, Iterator
import json

import metapub

import icd10
import requests
from typing_extensions import reveal_type

from fighthealthinsurance.exec import *
from fighthealthinsurance.model_router import model_router
from fighthealthinsurance.ml_models import RemoteModelLike, RemoteFullOpenLike
from fighthealthinsurance.models import (
    AppealTemplates,
    DenialTypes,
    Diagnosis,
    PlanType,
    Procedures,
    Regulator,
    PubMedQueryData,
    PubMedArticleSummarized,
)
from fighthealthinsurance.process_denial import *
from fighthealthinsurance.utils import as_available_nested

pubmed_fetcher = PubMedFetcher()


class AppealTemplateGenerator(object):
    def __init__(self, prefaces, main, footer):
        self.prefaces = prefaces
        self.main = main
        self.footer = footer
        self.combined = str("\n".join(prefaces + main + footer))

    def generate_static(self):
        if "{medical_reason}" not in self.combined and self.combined != "":
            return self.combined
        else:
            return None

    def generate(self, medical_reason):
        result = self.combined.replace("{medical_reason}", medical_reason)
        if result != "":
            return result
        else:
            return None


class AppealGenerator(object):
    def __init__(self):
        self.regex_denial_processor = ProcessDenialRegex()

    def get_procedure_and_diagnosis(
        self, denial_text=None, use_external=False
    ) -> Tuple[Optional[str], Optional[str]]:
        prompt = self.make_open_procedure_prompt(denial_text)
        models_to_try = [
            self.regex_denial_processor,
        ]
        models_to_try.extend(model_router.entity_extract_backends(use_external))
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

    def find_more_context(self, denial) -> str:
        """
        Kind of hacky RAG routine that uses PubMed.
        """
        # PubMed
        pmids = None
        pmid_text: list[str] = []
        article_futures: list[Future[PubMedArticleSummarized]] = []
        with Timeout(10.0) as timeout_ctx:
            query = f"{denial.procedure} {denial.diagnosis}"
            pmids = pubmed_fetcher.pmids_for_query(query)
            articles_json = json.dumps(pmids)
            PubMedQueryData.objects.create(query=query, articles=articles_json).save()
            for article_id in pmids[0:2]:
                article_futures.append(
                    pubmed_executor.submit(self.do_article_summary, article_id, query)
                )

        def article_to_summary(article) -> str:
            return f"PubMed DOI {article.doi} title {article.title} summary {article.basic_summary}"

        articles: list[PubMedArticleSummarized] = []
        # Get the articles that we've summarized
        for f in article_futures:
            try:
                articles.append(f.result(timeout=10))
            except Exception as e:
                print(f"Skipping appending article from {f} due to {e} of {type(e)}")
                pass

        if len(articles) > 0:
            return "\n".join(map(article_to_summary, articles))
        else:
            return ""

    def do_article_summary(self, article_id, query) -> PubMedArticleSummarized:
        possible_articles = PubMedArticleSummarized.objects.filter(
            pmid=article_id,
            query=query,
            basic_summary__isnull=False,
        )[:1]
        article = None
        if len(possible_articles) > 0:
            article = possible_articles[0]

        if article is None:
            fetched = pubmed_fetcher.article_by_pmid(article_id)
            src = FindIt(article_id)
            url = src.url
            article_text = ""
            if url is not None:
                response = requests.get(url)
                if (
                    ".pdf" in url
                    or response.headers.get("Content-Type") == "application/pdf"
                ):
                    with tempfile.NamedTemporaryFile(
                        suffix=".pdf", delete=False
                    ) as my_data:
                        my_data.write(response.content)

                        open_pdf_file = open(my_data.name, "rb")
                        read_pdf = PyPDF2.PdfReader(open_pdf_file)
                        if read_pdf.is_encrypted:
                            read_pdf.decrypt("")
                            for page in read_pdf.pages:
                                article_text += page.extract_text()
                        else:
                            for page in read_pdf.pages:
                                article_text += page.extract_text()
                else:
                    article_text += response.text
            else:
                article_text = fetched.content.text

            article = PubMedArticleSummarized.objects.create(
                pmid=article_id,
                doi=fetched.doi,
                title=fetched.title,
                abstract=fetched.abstract,
                text=article_text,
                query=query,
                basic_summary=model_router.summarize(
                    query=query, abstract=fetched.abstract, text=article_text
                ),
            )
        return article

    def make_appeals(
        self, denial, template_generator, medical_reasons=[], non_ai_appeals=[]
    ) -> Iterator[str]:

        open_prompt = self.make_open_prompt(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )
        open_medically_necessary_prompt = self.make_open_med_prompt(
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )

        pubmed_context = None
        try:
            pubmed_context = self.find_more_context(denial)
        except Exception as e:
            print(f"Error {e} looking up context for {denial}.")
            pass

        # TODO: use the streaming and cancellable APIs (maybe some fancy JS on the client side?)

        # For any model that we have a prompt for try to call it and return futures
        def get_model_result(
            model_name: str,
            prompt: str,
            patient_context: Optional[str],
            plan_context: Optional[str],
            infer_type: str,
            pubmed_context: Optional[str] = None,
        ) -> List[Future[Tuple[str, Optional[str]]]]:
            print(f"Looking up on {model_name}")
            if model_name not in model_router.models_by_name:
                print(f"No backend for {model_name}")
                return []
            model_backends = model_router.models_by_name[model_name]
            if prompt is None:
                print(f"No prompt for {model_name} skipping")
                return []
            for model in model_backends:
                try:
                    return _get_model_result(
                        model=model,
                        prompt=prompt,
                        patient_context=patient_context,
                        plan_context=plan_context,
                        infer_type=infer_type,
                        pubmed_context=pubmed_context,
                    )
                except Exception as e:
                    print(f"Backend {model} failed {e}")
            print(f"All backends for {model_name} failed")
            return []

        def _get_model_result(
            model: RemoteModelLike,
            prompt: str,
            patient_context: Optional[str],
            plan_context: Optional[str],
            infer_type: str,
            pubmed_context: Optional[str],
        ) -> List[Future[Tuple[str, Optional[str]]]]:
            # If the model has parallelism use it
            results = None
            try:
                if isinstance(model, RemoteFullOpenLike):
                    print(f"Using {model}'s parallel inference")
                    reveal_type(model)
                    results = model.parallel_infer(
                        prompt=prompt,
                        patient_context=patient_context,
                        plan_context=plan_context,
                        pubmed_context=pubmed_context,
                        infer_type=infer_type,
                    )
                else:
                    print(f"Using system level parallel inference for {model}")
                    results = [
                        executor.submit(
                            model.infer,
                            prompt=prompt,
                            patient_context=patient_context,
                            plan_context=plan_context,
                            infer_type=infer_type,
                            pubmed_context=pubmed_context,
                        )
                    ]
            except Exception as e:
                print(
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
                    )
                ]
            print(
                f"Infered {results} for {model}-{infer_type} using {prompt} w/ {patient_context}"
            )
            return results

        medical_context = ""
        if denial.qa_context is not None:
            medical_context += denial.qa_context
        if denial.health_history is not None:
            medical_context += denial.health_history
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
                        },
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

        def make_async_model_calls(calls) -> List[Future[Iterator[str]]]:
            print(f"Calling models: {calls}")
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
            time.sleep(random.randint(0, 15))
            return iter([appeal])

        delayed_initial_appeals: List[Future[Iterator[str]]] = list(
            map(lambda appeal: executor.submit(random_delay, appeal), initial_appeals)
        )
        appeals: Iterator[str] = as_available_nested(generated_text_futures)
        # Check and make sure we have some AI powered results
        try:
            appeals = itertools.chain([appeals.__next__()], appeals)
        except StopIteration:
            appeals = as_available_nested(make_async_model_calls(backup_calls))
        appeals = itertools.chain(appeals, as_available_nested(delayed_initial_appeals))
        print(f"Sending back {appeals}")
        return appeals
