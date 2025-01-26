# As part of the migration to postgres we need to remove nulls from strings.
import os

from typing import Any

from fighthealthinsurance.models import PubMedArticleSummarized
from django.core.management.base import BaseCommand, CommandParser
from django.db import models


class Command(BaseCommand):
    help = "Make the pubmed strings valid since someone put binary data in some of them"

    def handle(self, *args: str, **options: Any) -> None:
        for article in PubMedArticleSummarized.objects.all():
            update = False
            for field in article._meta.get_fields():
                if isinstance(field, models.CharField) or isinstance(
                    field, models.TextField
                ):
                    #                    print(f"Cleaning field {field}")
                    original_value = getattr(article, field.name)
                    if isinstance(original_value, str):
                        cleaned_value = (
                            original_value.encode("utf-8", "ignore")
                            .decode("utf-8")
                            .replace("\x00", "")
                        )
                        setattr(article, field.name, cleaned_value)
                        if cleaned_value != original_value:
                            update = True
                            print(f"Need to update {article}")
            if update:
                print(f"Updating {article}")
                update = False
                article.save()
