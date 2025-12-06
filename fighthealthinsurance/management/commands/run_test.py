import subprocess
import os
import webbrowser
from django.core.management.base import BaseCommand
from django.test.utils import get_runner
from django.conf import settings


class Command(BaseCommand):
    help = "Run tests with coverage and open the HTML report in the browser"

    def add_arguments(self, parser):
        parser.add_argument(
            "--test-file",
            type=str,
            help="Specify the test file or directory to run (e.g., tests/test_module.py)",
        )

    def handle(self, *args, **kwargs):
        test_file = kwargs.get("test_file", None)

        TestRunner = get_runner(settings)
        test_runner = TestRunner()

        self.stdout.write(self.style.SUCCESS("Running tests with coverage.... "))

        if test_file:
            self.stdout.write(self.style.SUCCESS("Test file: " + test_file))
            result = subprocess.run(
                ["coverage", "run", "-m", "pytest", test_file],
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(
                ["coverage", "run", "-m", "pytest"], capture_output=True, text=True
            )

        if result.returncode == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "Tests ran successfully. Generating coverage report..."
                )
            )

            subprocess.run(["coverage", "html"])

            self.stdout.write(self.style.SUCCESS("Opening the HTML coverage report..."))

            report_path = os.path.join(os.getcwd(), "htmlcov", "index.html")
            webbrowser.open(f"file://{report_path}")

        else:
            self.stdout.write(self.style.ERROR(f"Tests failed: {result.stderr}"))
