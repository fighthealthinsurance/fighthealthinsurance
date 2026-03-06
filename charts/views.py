import json
import os
from functools import wraps

from django.http import HttpResponse, HttpResponseForbidden, StreamingHttpResponse
from django.shortcuts import render
from django.views import View
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from datetime import timedelta

from fighthealthinsurance.models import (
    Appeal,
    Denial,
    DenialQA,
    GenericQuestionGeneration,
    InterestedProfessional,
    OngoingChat,
    ProposedAppeal,
    PubMedArticleSummarized,
    ChooserTask,
    ChooserCandidate,
    ChooserVote,
)
from fhi_users.models import ProfessionalDomainRelation
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import ColumnDataSource
import pandas as pd
import csv


def export_enabled_required(view_func):
    """Decorator that gates export views behind the EXPORT_ENABLED=1 env var."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if os.environ.get("EXPORT_ENABLED") != "1":
            return HttpResponseForbidden("Exports are not enabled.")
        return view_func(request, *args, **kwargs)

    return wrapper


class BaseEmailsWithRawEmailCSV(View):
    """Base class for views that export email addresses from denials where raw_email is not null."""

    def get_queryset(self):
        """To be implemented by subclasses to filter the queryset."""
        raise NotImplementedError("Subclasses must implement get_queryset method")

    def get_filename(self):
        """Return the filename for the CSV download."""
        raise NotImplementedError("Subclasses must implement get_filename method")

    def get(self, request):
        """Handle the request and return a CSV response."""
        denials_qs = self.get_queryset()

        # Exclude test emails
        hashed_farts = Denial.get_hashed_email("farts@farts.com")
        hashed_pcf = Denial.get_hashed_email("holden@pigscanfly.ca")
        denials_qs = denials_qs.exclude(
            Q(hashed_email=hashed_farts) | Q(hashed_email=hashed_pcf)
        )

        # Exclude professional denials
        denials_qs = denials_qs.filter(
            Q(creating_professional__isnull=True)
            & Q(primary_professional__isnull=True)
            & Q(domain__isnull=True)
        )

        # Get distinct raw_email values (exclude those that don't have a valid email)
        denials_qs = (
            denials_qs.filter(raw_email__contains="@")
            .values("raw_email", "date")
            .distinct("raw_email")
        )

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="{self.get_filename()}"'
        )

        writer = csv.writer(response)
        writer.writerow(["Email", "Date"])

        for denial in denials_qs:
            writer.writerow(
                [
                    denial["raw_email"],
                    denial["date"].strftime("%Y-%m-%d"),
                ]
            )

        return response


class OlderThanTwoWeeksEmailsCSV(BaseEmailsWithRawEmailCSV):
    """Export unique emails from denials that are older than two weeks."""

    def get_queryset(self):
        two_weeks_ago = timezone.now().date() - timedelta(days=14)
        return (
            Denial.objects.filter(raw_email__isnull=False, date__lt=two_weeks_ago)
            .order_by("raw_email", "date")
            .distinct("raw_email")
        )

    def get_filename(self):
        return "emails_older_than_two_weeks.csv"


class LastTwoWeeksEmailsCSV(BaseEmailsWithRawEmailCSV):
    """Export unique emails from denials from the last two weeks."""

    def get_queryset(self):
        two_weeks_ago = timezone.now().date() - timedelta(days=14)
        return (
            Denial.objects.filter(raw_email__isnull=False, date__gte=two_weeks_ago)
            .order_by("raw_email", "date")
            .distinct("raw_email")
        )

    def get_filename(self):
        return "emails_last_two_weeks.csv"


class AllDenialEmailSansProCSV(BaseEmailsWithRawEmailCSV):
    """Export all unique emails from denials excluding those created by professionals."""

    def get_queryset(self):
        return (
            Denial.objects.filter(raw_email__isnull=False)
            .order_by("raw_email", "date")
            .distinct("raw_email")
        )

    def get_filename(self):
        return "all_denial_emails_sans_pro.csv"


class MailingListSubscriberCSV(View):
    """Export all mailing list subscriber emails."""

    def get(self, request):
        """Handle the request and return a CSV response."""
        from fighthealthinsurance.models import MailingListSubscriber

        subscribers_qs = (
            MailingListSubscriber.objects.all()
            .order_by("email", "signup_date")
            .distinct("email")
        )

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="mailing_list_subscribers.csv"'
        )

        writer = csv.writer(response)
        writer.writerow(["Email", "Name", "Phone", "Signup Date", "Comments"])

        for subscriber in subscribers_qs:
            writer.writerow(
                [
                    subscriber.email,
                    subscriber.name,
                    subscriber.phone,
                    subscriber.signup_date.strftime("%Y-%m-%d"),
                    subscriber.comments,
                ]
            )

        return response


@staff_member_required
def incomplete_signups_csv(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="incomplete_signups.csv"'

    writer = csv.writer(response)
    writer.writerow(
        ["Provider Name", "Business Name", "Visible Phone", "Internal Phone", "Email"]
    )

    # Query ProfessionalDomainRelation for links between inactive professionals and inactive domains
    inactive_relations = ProfessionalDomainRelation.objects.filter(
        professional__active=False, domain__active=False
    ).select_related(
        "professional",
        "professional__user",
        "domain",
    )

    for relation in inactive_relations:
        prof = relation.professional
        domain = relation.domain
        email = prof.user.email if prof.user else "N/A"

        provider_name = prof.display_name
        if not provider_name:
            provider_name = prof.user.get_full_name() if prof.user else "N/A"

        business_name = domain.business_name if domain.business_name else ""
        # visible_phone_number is not nullable, so direct access is fine.
        visible_phone = domain.visible_phone_number
        internal_phone = (
            domain.internal_phone_number if domain.internal_phone_number else ""
        )

        writer.writerow(
            [provider_name, business_name, visible_phone, internal_phone, email]
        )

    return response


def generate_de_identified_lines(limit=None):
    """Yield JSONL lines of de-identified denial data."""
    hashed_farts = Denial.get_hashed_email("farts@farts.com")
    hashed_pcf = Denial.get_hashed_email("holden@pigscanfly.ca")
    hashed_gmail = Denial.get_hashed_email("holden.karau@gmail.com")
    exclude_emails = [hashed_farts, hashed_pcf, hashed_gmail]
    safe_denials = Denial.objects.exclude(
        Q(hashed_email__in=exclude_emails)
        | Q(manual_deidentified_denial="")
        | Q(manual_deidentified_denial__isnull=True)
    ).values(
        "denial_id",
        "manual_deidentified_denial",
        "manual_deidentified_ocr_cleaned_denial",
        "manual_deidentified_appeal",
        "manual_searchterm",
        "verified_procedure",
        "verified_diagnosis",
        "ml_citation_context",
        "generated_questions",
        "procedure",
        "diagnosis",
        "insurance_company",
        "appeal_fax_number",
    )
    if limit:
        safe_denials = safe_denials[0 : int(limit)]

    for record in safe_denials.iterator():
        yield json.dumps(record, default=str) + "\n"


@staff_member_required
@export_enabled_required
def de_identified_export(request):
    limit = request.GET.get("limit")
    response = StreamingHttpResponse(
        streaming_content=generate_de_identified_lines(limit=limit),
        content_type="application/x-ndjson",
    )
    response["Content-Disposition"] = 'attachment; filename="de_identified.jsonl"'
    return response


def generate_chooser_ranked_lines():
    """Yield JSONL lines of highest-ranked candidate for each chooser task."""
    tasks_with_votes = (
        ChooserTask.objects.filter(votes__isnull=False)
        .distinct()
        .prefetch_related("candidates")
    )

    # Batch all vote counts in a single query to avoid N+1
    all_vote_counts = (
        ChooserVote.objects.filter(task__in=tasks_with_votes)
        .values("task_id", "chosen_candidate_id")
        .annotate(vote_count=Count("id"))
    )
    # Build {task_id: [(chosen_candidate_id, vote_count), ...]} sorted by vote_count desc
    votes_by_task: dict[int, list[tuple[int, int]]] = {}
    for row in all_vote_counts:
        votes_by_task.setdefault(row["task_id"], []).append(
            (row["chosen_candidate_id"], row["vote_count"])
        )
    for task_id in votes_by_task:
        votes_by_task[task_id].sort(key=lambda x: x[1], reverse=True)

    for task in tasks_with_votes:
        task_votes = votes_by_task.get(task.id)
        if not task_votes:
            continue

        candidates_by_id = {c.id: c for c in task.candidates.all()}

        top_candidate_id, top_vote_count = task_votes[0]
        chosen = candidates_by_id.get(top_candidate_id)
        if chosen is None:
            continue

        total_votes = sum(vc for _, vc in task_votes)

        all_candidates = [
            {
                "id": c.id,
                "candidate_index": c.candidate_index,
                "kind": c.kind,
                "model_name": c.model_name,
                "content": c.content,
                "metadata": c.metadata,
            }
            for c in candidates_by_id.values()
            if c.is_active
        ]
        all_candidates.sort(key=lambda c: c["candidate_index"])

        record = {
            "task_id": task.id,
            "task_type": task.task_type,
            "context": task.context_json,
            "source": task.source,
            "chosen_candidate": {
                "id": chosen.id,
                "candidate_index": chosen.candidate_index,
                "kind": chosen.kind,
                "model_name": chosen.model_name,
                "content": chosen.content,
            },
            "vote_count": top_vote_count,
            "total_votes": total_votes,
            "all_candidates": all_candidates,
        }
        yield json.dumps(record, default=str) + "\n"


@staff_member_required
@export_enabled_required
def chooser_ranked_export(request):
    """Export the highest-ranked candidate for each chooser task as JSONL."""
    response = StreamingHttpResponse(
        streaming_content=generate_chooser_ranked_lines(),
        content_type="application/x-ndjson",
    )
    response["Content-Disposition"] = 'attachment; filename="chooser_ranked.jsonl"'
    return response


def generate_denial_appeal_lines():
    """Yield JSONL lines of de-identified denial + chosen appeal pairs (non-pro only)."""
    hashed_farts = Denial.get_hashed_email("farts@farts.com")
    hashed_pcf = Denial.get_hashed_email("holden@pigscanfly.ca")
    hashed_gmail = Denial.get_hashed_email("holden.karau@gmail.com")
    exclude_emails = [hashed_farts, hashed_pcf, hashed_gmail]

    has_chosen_proposed = Exists(
        ProposedAppeal.objects.filter(for_denial=OuterRef("pk"), chosen=True)
    )
    has_appeal = Exists(Appeal.objects.filter(for_denial=OuterRef("pk")))
    denials = (
        Denial.objects.exclude(hashed_email__in=exclude_emails)
        .filter(
            creating_professional__isnull=True,
            primary_professional__isnull=True,
            domain__isnull=True,
        )
        .filter(Q(flag_for_exclude=False) | Q(flag_for_exclude__isnull=True))
        .filter(has_chosen_proposed | has_appeal)
        .prefetch_related("proposedappeal_set", "appeal_set", "denialqa_set")
    )

    for denial in denials:
        denial_text = (
            denial.manual_deidentified_denial
            if denial.manual_deidentified_denial
            else denial.denial_text
        )
        procedure = (
            denial.verified_procedure if denial.verified_procedure else denial.procedure
        )
        diagnosis = (
            denial.verified_diagnosis if denial.verified_diagnosis else denial.diagnosis
        )

        appeal_text = None
        if denial.manual_deidentified_appeal:
            appeal_text = denial.manual_deidentified_appeal
        else:
            chosen_proposed = denial.proposedappeal_set.filter(chosen=True).first()
            if chosen_proposed:
                appeal_text = chosen_proposed.appeal_text
            else:
                appeal = denial.appeal_set.first()
                if appeal:
                    appeal_text = appeal.appeal_text

        if not appeal_text:
            continue

        # Build Q&A from prefetched DenialQA records
        qa_pairs = []
        for qa in denial.denialqa_set.all():
            answer = qa.text_answer if qa.text_answer else qa.bool_answer
            if answer is not None:
                qa_pairs.append({"question": qa.question, "answer": answer})

        record = {
            "denial_id": str(denial.denial_id),
            "denial_text": denial_text,
            "procedure": procedure,
            "diagnosis": diagnosis,
            "insurance_company": denial.insurance_company,
            "appeal_text": appeal_text,
            "references": denial.references,
            "generated_questions": denial.generated_questions,
            "qa_pairs": qa_pairs,
            "ml_citation_context": denial.ml_citation_context,
            "pubmed_context": denial.pubmed_context,
        }
        yield json.dumps(record, default=str) + "\n"


@staff_member_required
@export_enabled_required
def denial_appeal_export(request):
    """Export de-identified denial + chosen appeal pairs as JSONL (non-pro users only)."""
    response = StreamingHttpResponse(
        streaming_content=generate_denial_appeal_lines(),
        content_type="application/x-ndjson",
    )
    response["Content-Disposition"] = 'attachment; filename="denial_appeal.jsonl"'
    return response


def generate_chat_lines():
    """Yield JSONL lines of de-identified chat histories (non-pro only)."""
    hashed_farts = Denial.get_hashed_email("farts@farts.com")
    hashed_pcf = Denial.get_hashed_email("holden@pigscanfly.ca")
    hashed_gmail = Denial.get_hashed_email("holden.karau@gmail.com")
    exclude_emails = [hashed_farts, hashed_pcf, hashed_gmail]

    chats = (
        OngoingChat.objects.filter(
            professional_user__isnull=True,
            domain__isnull=True,
        )
        .exclude(chat_history__isnull=True)
        .exclude(chat_history=[])
        .exclude(hashed_email__in=exclude_emails)
        .prefetch_related("appeals")
    )

    for chat in chats:
        appeal_texts = [a.appeal_text for a in chat.appeals.all() if a.appeal_text]
        record = {
            "chat_id": str(chat.id),
            "chat_history": chat.chat_history,
            "denied_item": chat.denied_item,
            "denied_reason": chat.denied_reason,
            "appeal_texts": appeal_texts,
        }
        yield json.dumps(record, default=str) + "\n"


@staff_member_required
@export_enabled_required
def chat_export(request):
    """Export de-identified chat histories as JSONL (non-pro users only)."""
    response = StreamingHttpResponse(
        streaming_content=generate_chat_lines(),
        content_type="application/x-ndjson",
    )
    response["Content-Disposition"] = 'attachment; filename="chat.jsonl"'
    return response


@staff_member_required
def pro_signups_csv(request):
    interested_professionals_qs = (
        InterestedProfessional.objects.exclude(
            Q(email="farts@farts.com") | Q(email="holden@pigscanfly.ca")
        )
        .values(
            "email",
            "name",
            "address",
            "signup_date",
            "clicked_for_paid",
            "phone_number",
        )
        .order_by("signup_date")
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="professional_signups.csv"'

    writer = csv.writer(response)
    writer.writerow(
        ["Email", "Name", "Address", "Signup Date", "Clicked For Paid", "Phone Number"]
    )

    for pro in interested_professionals_qs:
        writer.writerow(
            [
                pro["email"],
                pro["name"],
                pro["address"],
                pro["signup_date"].strftime("%Y-%m-%d"),
                "Yes" if pro["clicked_for_paid"] else "No",
                pro["phone_number"],
            ]
        )

    return response


@staff_member_required
def pro_signups_csv_single_lines(request):
    interested_professionals_qs = (
        InterestedProfessional.objects.exclude(
            Q(email="farts@farts.com") | Q(email="holden@pigscanfly.ca")
        )
        .values(
            "email",
            "name",
            "address",
            "signup_date",
            "clicked_for_paid",
            "phone_number",
        )
        .order_by("email", "signup_date")
        .distinct("email")
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        'attachment; filename="professional_signups_single_line.csv"'
    )

    writer = csv.writer(response)
    writer.writerow(
        ["Email", "Name", "Address", "Signup Date", "Clicked For Paid", "Phone Number"]
    )

    for pro in interested_professionals_qs:
        # Replace newlines with spaces in text fields
        writer.writerow(
            [
                pro["email"].replace("\n", ""),
                (
                    pro["name"].replace("\n", " ").replace("\r", " ")
                    if pro["name"]
                    else ""
                ),
                (
                    pro["address"].replace("\n", " ").replace("\r", " ")
                    if pro["address"]
                    else ""
                ),
                pro["signup_date"].strftime("%Y-%m-%d"),
                "Yes" if pro["clicked_for_paid"] else "No",
                (
                    pro["phone_number"].replace("\n", " ").replace("\r", " ")
                    if pro["phone_number"]
                    else ""
                ),
            ]
        )

    return response


@staff_member_required
def signups_by_day(request):
    # Query to count unique email signups per day, separated by paid status
    try:
        signups_per_day = (
            InterestedProfessional.objects.exclude(
                Q(email="farts@farts.com") | Q(email="holden@pigscanfly.ca")
            )
            .distinct("email")
            .order_by("signup_date")
            .values("signup_date", "clicked_for_paid")
            .annotate(count=Count("email"))
        )
        # Convert query results to a DataFrame
        df = pd.DataFrame(list(signups_per_day))
    except:
        signups_per_day = (
            InterestedProfessional.objects.exclude(
                Q(email="farts@farts.com") | Q(email="holden@pigscanfly.ca")
            )
            .order_by("signup_date")
            .values("signup_date", "clicked_for_paid")
            .annotate(count=Count("email", distinct=True))
        )
        # Convert query results to a DataFrame
        df = pd.DataFrame(list(signups_per_day))

    print(df)
    if df.empty or "signup_date" not in df.columns:
        return HttpResponse("No signup data available.", content_type="text/plain")

    # Ensure signup_date is a datetime column
    df["signup_date"] = pd.to_datetime(df["signup_date"])

    # Pivot data: rows = signup_date, columns = paid (True/False), values = count
    df_pivot = df.pivot_table(
        index="signup_date", columns="clicked_for_paid", values="count", aggfunc="sum"
    ).fillna(0)

    # Rename columns dynamically
    df_pivot = df_pivot.rename(columns={True: "Paid", False: "Unpaid"})

    # Ensure both 'Paid' and 'Unpaid' columns exist
    for col in ["Paid", "Unpaid"]:
        if col not in df_pivot.columns:
            df_pivot[col] = 0  # Add missing column with zeros

    # Compute stacking (daily, not cumulative over time)
    df_pivot["unpaid_top"] = df_pivot["Unpaid"]
    df_pivot["paid_top"] = (
        df_pivot["Unpaid"] + df_pivot["Paid"]
    )  # Stack Paid on top of Unpaid

    # Prepare Bokeh data source
    source = ColumnDataSource(df_pivot)

    # Create a Bokeh plot
    p = figure(title="Daily Signups", x_axis_type="datetime")

    # Stacked area plot (not cumulative over time)
    p.varea(
        x="signup_date",
        y1=0,
        y2="unpaid_top",
        source=source,
        color="red",
        legend_label="Unpaid",
    )
    p.varea(
        x="signup_date",
        y1="unpaid_top",
        y2="paid_top",
        source=source,
        color="green",
        legend_label="Paid",
    )

    # Labels & legend
    p.legend.title = "Signup Type"
    p.xaxis.axis_label = "Date"
    p.yaxis.axis_label = "Number of Signups"

    # Generate Bokeh components
    script, div = components(p)

    df_html = df_pivot.to_html()
    totals_html = df_pivot.sum().to_frame().to_html()

    return render(
        request,
        "bokeh.html",
        {"script": script, "div": div, "df_html": df_html, "totals_html": totals_html},
    )


@staff_member_required
def sf_signups(request):
    pro_signups = (
        InterestedProfessional.objects.exclude(
            Q(email="farts@farts.com") | Q(email="holden@pigscanfly.ca")
        )
        .filter(
            Q(address__icontains="San Francisco")
            | Q(address__icontains="Daly City")
            | Q(address__icontains="Oakland")
            | Q(address__icontains="Berkeley")
            | Q(address__icontains="Millbrae")
            | Q(address__icontains="Burlingame")
            | Q(address__icontains="San Mateo")
            | Q(address__icontains="Belmont")
            | Q(address__icontains="Redwood City")
            | Q(address__icontains="North Fair Oaks")
            | Q(address__icontains="Atherton")
            | Q(address__icontains="Menlo Park")
            | Q(address__icontains="Palo Alto")
            | Q(address__icontains="Stanford")
            | Q(address__icontains="Woodside")
            | Q(address__icontains="941")
            | Q(address__icontains="SF, CA")
        )
        .order_by("signup_date")
    )
    return render(request, "basic_table_only.html", {"table": pro_signups})


@staff_member_required
def users_by_day(request):
    # Query to count users by day
    # Our "test" users are farts@farts.com & holden@pigscanfly.ca
    hashed_farts = Denial.get_hashed_email("farts@farts.com")
    hashed_pcf = Denial.get_hashed_email("holden@pigscanfly.ca")
    users_per_day = (
        Denial.objects.exclude(
            Q(hashed_email=hashed_farts) | Q(hashed_email=hashed_pcf)
        )
        .order_by("date")
        .values("date")
        .annotate(count=Count("hashed_email", distinct=True))
    )
    # Convert query results to a DataFrame
    df = pd.DataFrame(list(users_per_day))

    print(df)
    if df.empty:
        return HttpResponse("No user data available.", content_type="text/plain")

    # Ensure date is a datetime column
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)

    # Prepare Bokeh data source
    source = ColumnDataSource(df)

    # Create a Bokeh plot
    p = figure(title="Daily Users", x_axis_type="datetime")

    # Stacked area plot (not cumulative over time)
    p.varea(
        x="date", y1=0, y2="count", source=source, color="red", legend_label="Users"
    )

    # Labels & legend
    p.legend.title = "Users"
    p.xaxis.axis_label = "Date"
    p.yaxis.axis_label = "Number of users"

    # Generate Bokeh components
    script, div = components(p)

    df_html = df.to_html()
    totals_html = df.sum().to_frame().to_html()

    return render(
        request,
        "bokeh.html",
        {"script": script, "div": div, "df_html": df_html, "totals_html": totals_html},
    )


def generate_questions_by_procedure_lines():
    """Yield JSONL lines of generated questions by procedure + diagnosis."""
    queryset = GenericQuestionGeneration.objects.all().order_by(
        "procedure", "diagnosis"
    )

    for row in queryset.iterator(chunk_size=200):
        record = {
            "procedure": row.procedure,
            "diagnosis": row.diagnosis,
            "generated_questions": row.generated_questions,
        }
        yield json.dumps(record, default=str) + "\n"


@staff_member_required
@export_enabled_required
def questions_by_procedure_export(request):
    """Export generated questions keyed by procedure + diagnosis as JSONL."""
    response = StreamingHttpResponse(
        streaming_content=generate_questions_by_procedure_lines(),
        content_type="application/x-ndjson",
    )
    response["Content-Disposition"] = (
        'attachment; filename="questions_by_procedure.jsonl"'
    )
    return response


def generate_denial_questions_lines():
    """Yield JSONL lines of denial questions with answer status (non-pro only)."""
    hashed_farts = Denial.get_hashed_email("farts@farts.com")
    hashed_pcf = Denial.get_hashed_email("holden@pigscanfly.ca")
    hashed_gmail = Denial.get_hashed_email("holden.karau@gmail.com")
    exclude_emails = [hashed_farts, hashed_pcf, hashed_gmail]

    denials = (
        Denial.objects.exclude(hashed_email__in=exclude_emails)
        .filter(
            creating_professional__isnull=True,
            primary_professional__isnull=True,
            domain__isnull=True,
            generated_questions__isnull=False,
        )
        .filter(Q(flag_for_exclude=False) | Q(flag_for_exclude__isnull=True))
        .exclude(generated_questions=[])
    )

    # Batch-fetch all answered questions to avoid N+1
    denial_ids = list(denials.values_list("denial_id", flat=True))
    answered_by_denial = {}
    for qa in DenialQA.objects.filter(denial_id__in=denial_ids).values_list(
        "denial_id", "question"
    ):
        answered_by_denial.setdefault(qa[0], set()).add(qa[1])

    for denial in denials.iterator(chunk_size=100):
        if not denial.generated_questions:
            continue

        answered_questions = answered_by_denial.get(denial.denial_id, set())

        questions = []
        for question_text, default_answer in denial.generated_questions:
            questions.append(
                {
                    "question": question_text,
                    "default_answer": default_answer,
                    "was_answered": question_text in answered_questions,
                }
            )

        # Sort: answered first, then unanswered
        questions.sort(key=lambda q: (not q["was_answered"], q["question"]))

        procedure = (
            denial.verified_procedure if denial.verified_procedure else denial.procedure
        )
        diagnosis = (
            denial.verified_diagnosis if denial.verified_diagnosis else denial.diagnosis
        )

        record = {
            "denial_id": str(denial.denial_id),
            "procedure": procedure,
            "diagnosis": diagnosis,
            "questions": questions,
        }
        yield json.dumps(record, default=str) + "\n"


@staff_member_required
@export_enabled_required
def denial_questions_export(request):
    """Export denial questions with answer status as JSONL (non-pro only)."""
    response = StreamingHttpResponse(
        streaming_content=generate_denial_questions_lines(),
        content_type="application/x-ndjson",
    )
    response["Content-Disposition"] = 'attachment; filename="denial_questions.jsonl"'
    return response


def generate_pubmed_article_lines():
    """Yield JSONL lines of summarized PubMed articles."""
    queryset = (
        PubMedArticleSummarized.objects.exclude(basic_summary__isnull=True)
        .exclude(basic_summary="")
        .values(
            "pmid",
            "doi",
            "query",
            "title",
            "abstract",
            "basic_summary",
            "says_effective",
            "publication_date",
            "article_url",
        )
    )

    for record in queryset.iterator(chunk_size=200):
        yield json.dumps(record, default=str) + "\n"


@staff_member_required
@export_enabled_required
def pubmed_article_export(request):
    """Export summarized PubMed articles as JSONL."""
    response = StreamingHttpResponse(
        streaming_content=generate_pubmed_article_lines(),
        content_type="application/x-ndjson",
    )
    response["Content-Disposition"] = 'attachment; filename="pubmed_articles.jsonl"'
    return response


@staff_member_required
def procedures_denied_chart(request):
    """
    Create a chart showing the count of each procedure that has been denied,
    grouped together in a case-insensitive and space-insensitive way.
    """
    # Exclude test emails
    hashed_farts = Denial.get_hashed_email("farts@farts.com")
    hashed_pcf = Denial.get_hashed_email("holden@pigscanfly.ca")
    hashed_gmail = Denial.get_hashed_email("holden.karau@gmail.com")
    exclude_emails = [hashed_farts, hashed_pcf, hashed_gmail]

    # Get all denials with procedures that aren't empty or None
    denials_with_procedures = (
        Denial.objects.exclude(Q(hashed_email__in=exclude_emails))
        .exclude(Q(procedure__isnull=True) | Q(procedure=""))
        .values_list("procedure", flat=True)
    )

    # Process procedures to normalize them (case insensitive, remove spaces)
    procedure_counts = {}
    for procedure in denials_with_procedures:
        if procedure:
            # Normalize the procedure name: lowercase and remove extra spaces
            normalized_proc = " ".join(procedure.lower().split())
            procedure_counts[normalized_proc] = (
                procedure_counts.get(normalized_proc, 0) + 1
            )

    # Convert to DataFrame for visualization
    df = pd.DataFrame(
        {
            "procedure": list(procedure_counts.keys()),
            "count": list(procedure_counts.values()),
        }
    )

    if df.empty:
        return HttpResponse("No procedure data available.", content_type="text/plain")

    # Sort by count in descending order
    df = df.sort_values("count", ascending=False)

    # Limit to top 15 procedures for the chart and aggregate the rest
    top_n = 15
    if len(df) > top_n:
        top_df = df.iloc[:top_n].copy()
        other_count = df.iloc[top_n:]["count"].sum()
        top_df = pd.concat(
            [
                top_df,
                pd.DataFrame(
                    {"procedure": ["Other Procedures"], "count": [other_count]}
                ),
            ]
        )
        chart_df = top_df
    else:
        chart_df = df

    # Create a Bokeh figure
    p = figure(
        title="Most Commonly Denied Procedures",
        x_range=chart_df["procedure"].tolist(),
        height=500,
        width=800,
        toolbar_location="right",
        tools="hover,pan,box_zoom,reset,save",
        tooltips=[("Procedure", "@procedure"), ("Count", "@count")],
    )

    # Create bar plot
    source = ColumnDataSource(chart_df)
    p.vbar(
        x="procedure",
        top="count",
        width=0.8,
        source=source,
        color="firebrick",
        line_color="white",
    )

    # Customize the chart appearance
    p.xaxis.major_label_orientation = 3.14 / 4  # Rotate x-axis labels 45 degrees
    p.xgrid.grid_line_color = None
    p.y_range.start = 0
    p.xaxis.axis_label = "Procedures"
    p.yaxis.axis_label = "Number of Denials"

    # Generate Bokeh components
    script, div = components(p)

    # Generate HTML table for full data (not just what's in the chart)
    df_html = df.to_html(classes=["table", "table-striped", "table-hover"], index=False)

    # Calculate total number of procedures
    total_procedures = df["count"].sum()
    total_unique_procedures = len(df)
    totals_html = f"<p>Total denials with procedures: {total_procedures}</p><p>Total unique procedures: {total_unique_procedures}</p>"

    return render(
        request,
        "bokeh.html",
        {
            "script": script,
            "div": div,
            "df_html": df_html,
            "totals_html": totals_html,
            "title": "Denied Procedures Analysis",
        },
    )
