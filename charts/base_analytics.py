from django.db.models import Q
from django.views import View
from fighthealthinsurance.models import Denial
import re


class BaseAnalyticsView(View):
    """
    Base class for views that need to filter out test/internal emails from analytics.

    This class provides common methods to exclude both hashed and unhashed test emails.
    It centralizes the list of excluded emails in one place to ensure consistency
    across all analytics views and reports.

    Usage:
    1. For function-based views, use the static methods:
       - BaseAnalyticsView.get_excluded_hashed_emails()
       - BaseAnalyticsView.get_email_exclude_q()
       - BaseAnalyticsView.get_raw_email_exclude_q()

    2. For class-based views, inherit from BaseAnalyticsView:
       class MyView(BaseAnalyticsView):
           def get(self, request):
               # Use self.get_email_exclude_q() to exclude test emails
               queryset = Model.objects.exclude(self.get_email_exclude_q())
    """

    # Add emails that should be excluded from analytics here
    EMAIL_EXCLUDES = [
        "farts@farts.com",
        "holden@pigscanfly.ca",
        "holden.karau@gmail.com",
        "warrick@example.com",
        "warrick@gmail.com",
        "warrick@fighthealthinsurance.com",
    ]

    # Optional regex pattern for matching emails in cases where exact match isn't possible
    EMAIL_REGEX_PATTERN = r"(farts@|holden@|warrick@)"

    @classmethod
    def get_excluded_hashed_emails(cls):
        """
        Returns a list of hashed email values to exclude from queries.

        This method converts all emails in EMAIL_EXCLUDES to their hashed form
        using the same hashing algorithm as the Denial model.

        Returns:
            list: List of SHA-512 hashed email strings
        """
        return [Denial.get_hashed_email(email) for email in cls.EMAIL_EXCLUDES]

    @classmethod
    def get_email_exclude_q(cls):
        """
        Returns a Q object for excluding hashed emails.

        For use with models that have a hashed_email field.

        Returns:
            Q: A Django Q object for use in .exclude() queries
        """
        return Q(hashed_email__in=cls.get_excluded_hashed_emails())

    @classmethod
    def get_raw_email_exclude_q(cls):
        """
        Returns a Q object for excluding raw (non-hashed) emails.

        For use with models that store the actual email addresses.

        Returns:
            Q: A Django Q object for use in .exclude() queries
        """
        exclude_q = Q()
        for email in cls.EMAIL_EXCLUDES:
            exclude_q |= Q(email=email)
        return exclude_q

    @classmethod
    def get_regex_email_exclude_q(cls, field_name="email"):
        """
        Returns a Q object for excluding emails based on regex pattern.

        Args:
            field_name: The name of the field containing email addresses

        Returns:
            Q: A Django Q object for use in .exclude() queries
        """
        return Q(**{f"{field_name}__regex": cls.EMAIL_REGEX_PATTERN})
