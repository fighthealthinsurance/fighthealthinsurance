"""Calendar utilities for generating .ics files for follow-up reminders.

This module provides functionality to generate iCalendar (.ics) files that users
can import into their calendar applications (Gmail, Outlook, Apple Calendar, etc.)
to receive reminders about insurance appeal follow-ups at 2, 30, and 90 day intervals.
"""

from datetime import datetime, timedelta
from typing import Any, Optional, cast

from icalendar import Alarm, Calendar, Event, vText
from django.utils import timezone


def generate_followup_ics(denial: Any, email: str) -> bytes:
    """Generate an iCalendar (.ics) file with follow-up reminders for a denial.

    Creates a calendar file with three all-day events at 2, 30, and 90 days after
    the denial date. Each event includes details about the denial and instructions
    for following up with the insurance company.

    Args:
        denial: Denial model instance containing denial information
        email: Email address of the user (for calendar organizer field)

    Returns:
        bytes: iCalendar file content that can be saved as .ics or attached to email

    Example:
        >>> denial = Denial.objects.get(uuid=some_uuid)
        >>> ics_content = generate_followup_ics(denial, "user@example.com")
        >>> with open("followup.ics", "wb") as f:
        ...     f.write(ics_content)
    """
    # Create calendar
    cal = Calendar()
    cal.add("prodid", "-//Fight Health Insurance//Calendar Reminder//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Health Insurance Appeal Follow-ups")
    cal.add(
        "x-wr-caldesc",
        "Reminders to follow up on your health insurance appeal",
    )

    # Get denial date - use denial_date if available, otherwise created_at
    if hasattr(denial, "denial_date") and denial.denial_date:
        base_date = denial.denial_date
    elif hasattr(denial, "created_at") and denial.created_at:
        # Convert datetime to date if needed
        base_date = (
            denial.created_at.date()
            if isinstance(denial.created_at, datetime)
            else denial.created_at
        )
    else:
        # Fallback to today
        base_date = timezone.now().date()

    # Insurance company name
    insurance_company = getattr(denial, "insurance_company", "your insurance company")

    # Procedure/claim information
    procedure = getattr(denial, "procedure", "your claim")

    # Create three follow-up events
    follow_up_days = [
        (2, "2 Day Follow-up", "Initial follow-up to check appeal status"),
        (30, "30 Day Follow-up", "Monthly check on appeal progress"),
        (90, "90 Day Follow-up", "Quarterly follow-up on appeal"),
    ]

    for days, summary_suffix, description_prefix in follow_up_days:
        event = Event()

        # Event summary (title)
        event.add(
            "summary",
            f"Follow up on {insurance_company} appeal - {summary_suffix}",
        )

        # Event description with detailed instructions
        description = f"""{description_prefix}

Insurance Company: {insurance_company}
Procedure/Claim: {procedure}

Action Items:
1. Call {insurance_company} to check on appeal status
2. Ask for a reference number and the name of the person you spoke with
3. Document the conversation in your Fight Health Insurance dashboard
4. If appeal was denied, consider next steps (external review, etc.)

Need help? Visit https://www.fighthealthinsurance.com or check your dashboard.

This reminder was created by Fight Health Insurance (https://www.fighthealthinsurance.com)
"""
        event.add("description", description)

        # Event date (all-day event)
        event_date = base_date + timedelta(days=days)
        event.add("dtstart", event_date)
        event.add("dtend", event_date + timedelta(days=1))

        # Make it an all-day event
        event["dtstart"].params["VALUE"] = "DATE"
        event["dtend"].params["VALUE"] = "DATE"

        # Add organizer (using the user's email)
        event.add("organizer", f"mailto:{email}")

        # Add reminder alarm (1 day before the event)
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"Reminder: {summary_suffix}")
        alarm.add("trigger", timedelta(days=-1))
        event.add_component(alarm)

        # Add unique ID
        event.add(
            "uid",
            f"fhi-followup-{denial.uuid}-{days}day@fighthealthinsurance.com",
        )

        # Add timestamp
        event.add("dtstamp", timezone.now())

        # Add status
        event.add("status", "CONFIRMED")

        # Add category
        event.add("categories", ["Health Insurance", "Follow-up"])

        # Add location (online/phone)
        event.add("location", "Phone call to insurance company")

        # Add to calendar
        cal.add_component(event)

    # Return as bytes
    return cast(bytes, cal.to_ical())
