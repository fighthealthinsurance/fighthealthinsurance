from django.http import HttpResponse
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from fighthealthinsurance.models import Denial, InterestedProfessional
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import ColumnDataSource
import pandas as pd
import csv


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
