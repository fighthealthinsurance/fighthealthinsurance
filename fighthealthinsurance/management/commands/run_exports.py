"""Management command to run all JSONL exports to files, avoiding HTTP timeout issues."""

import os
from collections.abc import Callable, Generator

from django.core.management.base import BaseCommand

from charts.views import (
    generate_chat_lines,
    generate_chooser_ranked_lines,
    generate_de_identified_lines,
    generate_denial_appeal_lines,
    generate_denial_questions_lines,
    generate_pubmed_article_lines,
    generate_questions_by_procedure_lines,
)

EXPORTS: dict[str, tuple[Callable[[], Generator[str, None, None]], str]] = {
    "de_identified": (generate_de_identified_lines, "de_identified.jsonl"),
    "chooser_ranked": (generate_chooser_ranked_lines, "chooser_ranked.jsonl"),
    "denial_appeal": (generate_denial_appeal_lines, "denial_appeal.jsonl"),
    "chat": (generate_chat_lines, "chat.jsonl"),
    "questions_by_procedure": (
        generate_questions_by_procedure_lines,
        "questions_by_procedure.jsonl",
    ),
    "denial_questions": (generate_denial_questions_lines, "denial_questions.jsonl"),
    "pubmed_articles": (generate_pubmed_article_lines, "pubmed_articles.jsonl"),
}


class Command(BaseCommand):
    help = "Run JSONL exports and write to files (avoids HTTP timeout issues)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default="exports/",
            help="Output directory for export files (default: exports/)",
        )
        parser.add_argument(
            "--exports",
            nargs="*",
            choices=list(EXPORTS.keys()),
            help=f"Specific exports to run (default: all). Choices: {', '.join(EXPORTS.keys())}",
        )

    def handle(self, *args, **options):
        output_dir = options["output_dir"]
        selected = options["exports"] or list(EXPORTS.keys())

        os.makedirs(output_dir, exist_ok=True)

        for name in selected:
            generator_func, filename = EXPORTS[name]
            filepath = os.path.join(output_dir, filename)
            self.stdout.write(f"Exporting {name} -> {filepath} ...")

            count = 0
            with open(filepath, "w") as f:
                for line in generator_func():
                    f.write(line)
                    count += 1

            self.stdout.write(self.style.SUCCESS(f"  {name}: {count} records written"))

        self.stdout.write(self.style.SUCCESS("All exports complete."))
