import asyncio
import datetime
import json
from typing import Any, Dict, Set

from django.http import HttpResponse
from django.views import View, generic
from django.db import transaction
from loguru import logger
from django.shortcuts import redirect, render, get_object_or_404
from django.http import StreamingHttpResponse


from fighthealthinsurance import common_view_logic
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.forms import FollowUpTestForm, PubMedPreloadForm
from fighthealthinsurance.models import (
    Denial,
    FollowUpSched,
    ProfessionalUser,
    UserDomain,
    ProfessionalDomainRelation,
    PubMedMiniArticle,
)
from fighthealthinsurance.followup_emails import (
    ThankyouEmailSender,
    FollowUpEmailSender,
)
from fighthealthinsurance.pubmed_tools import PubMedTools


class ScheduleFollowUps(View):
    """A view to go through and schedule any missing follow ups."""

    def get(self, request):
        denials = (
            Denial.objects.filter(raw_email__isnull=False)
            .filter(followupsched__isnull=True)
            .iterator()
        )
        c = 0
        for denial in denials:
            # Shouldn't happen but makes the type checker happy.
            if denial.raw_email is None:
                continue
            FollowUpSched.objects.create(
                email=denial.raw_email,
                follow_up_date=denial.date + datetime.timedelta(days=15),
                denial_id=denial,
            )
            c = c + 1
        return HttpResponse(str(c))


class FollowUpEmailSenderView(generic.FormView):
    """A view to test the follow up sender."""

    template_name = "followup_test.html"
    form_class = FollowUpTestForm

    def form_valid(self, form):
        s = FollowUpEmailSender()
        field = form.cleaned_data.get("email")
        try:
            count = int(field)
            sent = s.send_all(count=field)
        except ValueError:
            sent = s.dosend(email=field)
        return HttpResponse(str(sent))


class ThankyouSenderView(generic.FormView):
    """A view to test the thankyou sender."""

    template_name = "followup_test.html"
    form_class = core_forms.FollowUpTestForm

    def form_valid(self, form):
        s = ThankyouEmailSender()
        field = form.cleaned_data.get("email")
        try:
            count = int(field)
            sent = s.send_all(count=field)
        except ValueError:
            sent = s.dosend(email=field)
        return HttpResponse(str(sent))


class ActivateProUserView(generic.FormView):
    template_name = "pro_domain_task.html"
    form_class = core_forms.ActivateProForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Activate Pro User"
        context["heading"] = "Activate Pro User Domain"
        context["description"] = "Enter the phone number of the domain to activate."
        context["button_text"] = "Activate"
        return context

    def form_valid(self, form):
        phonenumber = form.cleaned_data.get("phonenumber")
        domain = UserDomain.objects.get(visible_phone_number=phonenumber)
        domain.active = True
        domain.save()
        # Correctly update all professionals associated with the domain.
        ProfessionalUser.objects.filter(domains__in=[domain]).update(active=True)
        for p in ProfessionalUser.objects.filter(domains__in=[domain]):
            user = p.user
            user.is_active = True
            user.save()
        for r in ProfessionalDomainRelation.objects.filter(domain=domain):
            r.active_domain_relation = True
            r.pending_domain_relation = False
            r.save()
        return HttpResponse("Pro user activated")


class EnableBetaForDomainView(generic.FormView):
    """A view to enable beta features for a user domain by phone number."""

    template_name = "pro_domain_task.html"
    form_class = core_forms.ActivateProForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Enable Beta Features"
        context["heading"] = "Enable Beta Features for Domain"
        context["description"] = (
            "Enter the phone number of the domain to enable beta features."
        )
        context["button_text"] = "Enable Beta"
        return context

    def form_valid(self, form):
        try:
            phonenumber = form.cleaned_data.get("phonenumber")
            domain = UserDomain.objects.get(visible_phone_number=phonenumber)
            with transaction.atomic():
                domain.beta = True
                domain.save()
            return HttpResponse(
                f"Beta features enabled for domain {domain.name} ({phonenumber})"
            )
        except UserDomain.DoesNotExist:
            return HttpResponse(
                f"No domain found with phone number {phonenumber}", status=404
            )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error enabling beta for domain with phone {phonenumber}: {str(e)}"
            )
            return HttpResponse(f"Error enabling beta: {str(e)}", status=500)


class FollowUpFaxSenderView(generic.FormView):
    """A view to test the follow up sender."""

    template_name = "followup_test.html"
    form_class = core_forms.FollowUpTestForm

    def form_valid(self, form):
        field = form.cleaned_data.get("email")
        helper = common_view_logic.SendFaxHelper

        if field.isdigit():
            sent = helper.blocking_dosend_all(count=field)
        else:
            sent = helper.blocking_dosend_target(email=field)

        return HttpResponse(str(sent))


class PubMedPreloadView(generic.FormView):
    """
    A view for preloading PubMed searches for medications, conditions, and their combinations.

    This view allows staff members to enter lists of medications and conditions,
    and performs PubMed searches for each item individually and for each medication+condition pair.
    Results are streamed back to the client as they become available.
    """

    template_name = "pubmed_preload.html"
    form_class = PubMedPreloadForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "PubMed Pre-Population Tool"
        context["heading"] = "PubMed Pre-Population Tool"
        context["description"] = (
            "This tool searches PubMed for medications, conditions, and their combinations "
            "and caches the results for faster appeal generation."
        )
        return context

    def get(self, request, *args, **kwargs):
        """
        Handle GET requests, which will be used by EventSource.

        If medications or conditions parameters are present in the query string,
        treat it as a form submission for SSE streaming.
        """
        if "medications" in request.GET or "conditions" in request.GET:
            # Create a form instance with GET data
            form = self.form_class(request.GET)
            if form.is_valid():
                logger.debug("Form is valid, processing PubMed search...")
                return self.form_valid(form)
            else:
                logger.debug("Form is invalid")
                return self.form_invalid(form)
        else:
            # No medications or conditions in query string, show the form
            logger.debug("No medications or conditions in query string, showing form.")

        # Standard GET request - show the form
        return super().get(request, *args, **kwargs)

    def form_valid(self, form):
        logger.debug("Form is valid, processing PubMed search...")
        medications = form.cleaned_data.get("medications", [])
        conditions = form.cleaned_data.get("conditions", [])

        if not medications and not conditions:
            return render(
                self.request,
                self.template_name,
                {
                    "form": form,
                    "error": "Please enter at least one medication or condition.",
                    **self.get_context_data(),
                },
            )

        # Generate search combinations
        search_terms = []

        # Add individual medications
        for med in medications:
            search_terms.append({"term": med, "type": "medication"})

        # Add individual conditions
        for cond in conditions:
            search_terms.append({"term": cond, "type": "condition"})

        # Add medication + condition pairs
        for med in medications:
            for cond in conditions:
                search_terms.append(
                    {"term": f"{med} {cond}", "type": "medication+condition"}
                )

        # Return a streaming response that sends results as they come in
        return StreamingHttpResponse(
            self._stream_pubmed_search_results(search_terms),
            content_type="text/event-stream",
        )

    async def _perform_pubmed_search(
        self, term: str, search_type: str
    ) -> Dict[str, Any]:
        """
        Perform a PubMed search for a given term and type.

        Args:
            term: The search term to query
            search_type: The type of search (medication, condition, or combination)

        Returns:
            A dictionary with search results and metadata
        """
        pubmed_tools = PubMedTools()
        start_time = datetime.datetime.now()

        # Collect unique PMIDs across all year filters
        all_pmids: Set[str] = set()
        recent_pmids = None
        all_time_pmids = None

        try:
            # Search for recent articles (2024+)
            recent_pmids = await pubmed_tools.find_pubmed_article_ids_for_query(
                term, since="2024", timeout=30.0
            )
            if recent_pmids:
                all_pmids.update(recent_pmids)

            # Search for all-time articles
            all_time_pmids = await pubmed_tools.find_pubmed_article_ids_for_query(
                term, timeout=30.0
            )
            if all_time_pmids:
                all_pmids.update(all_time_pmids)

            # Convert to list and limit to first 10 for display
            pmids_list = list(all_pmids)[:10]

            # Get article details for the first few PMIDs
            articles = []
            for pmid in pmids_list[:10]:  # Limit to first 10 for efficiency
                mini_article = await PubMedMiniArticle.objects.filter(
                    pmid=pmid
                ).afirst()
                if mini_article:
                    articles.append(
                        {
                            "pmid": mini_article.pmid,
                            "title": mini_article.title,
                            "url": mini_article.article_url
                            or f"https://pubmed.ncbi.nlm.nih.gov/{mini_article.pmid}/",
                        }
                    )

            end_time = datetime.datetime.now()
            duration = (end_time - start_time).total_seconds()

            return {
                "term": term,
                "type": search_type,
                "status": "success",
                "recent_count": len(recent_pmids) if recent_pmids else 0,
                "all_time_count": len(all_time_pmids) if all_time_pmids else 0,
                "unique_count": len(all_pmids),
                "pmids": pmids_list,
                "articles": articles,
                "duration": duration,
                "timestamp": end_time.isoformat(),
            }

        except Exception as e:
            logger.opt(exception=True).error(
                f"Error performing PubMed search for term '{term}': {str(e)}"
            )
            end_time = datetime.datetime.now()
            duration = (end_time - start_time).total_seconds()

            return {
                "term": term,
                "type": search_type,
                "status": "error",
                "error": str(e),
                "duration": duration,
                "timestamp": end_time.isoformat(),
            }

    def _stream_pubmed_search_results(self, search_terms):
        """
        Query pubmed for the search terms

        Args:
            search_terms: List of dictionaries with term and type keys

        Yields:
            Async Generator of server-sent event formatted data for each completed search
        """
        logger.debug("Generating PubMed search results...")

        # Create async coroutines
        async def get_results():
            logger.debug("Starting PubMed search...")
            # Start all searches concurrently
            tasks = [
                self._perform_pubmed_search(term["term"], term["type"])
                for term in search_terms
            ]
            logger.debug(f"Total searches to perform: {len(tasks)}")

            # Initial response
            yield f"data: {json.dumps({'status': 'started', 'total': len(tasks)})}\n\n"

            # Process results as they come in
            completed = 0
            for task in asyncio.as_completed(tasks):
                result = await task
                completed += 1

                # Send event with result data
                yield f"data: {json.dumps({'result': result, 'completed': completed, 'total': len(tasks)})}\n\n"

            # Final message
            yield f"data: {json.dumps({'status': 'completed', 'total': len(tasks)})}\n\n"

        return get_results()
