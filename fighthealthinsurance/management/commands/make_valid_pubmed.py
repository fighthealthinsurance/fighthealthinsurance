from typing import Any

from django.core.management.base import BaseCommand
from django.db import models

from fighthealthinsurance.models import PubMedArticleSummarized


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
