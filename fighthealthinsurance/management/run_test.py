import subprocess
import os
import webbrowser
from django.core.management.base import BaseCommand
from django.test.utils import get_runner
from django.conf import settings

class Command(BaseCommand):
    help = 'Run tests with coverage and open the HTML report in the browser'

    def add_arguments(self, parser):
        # Add a custom argument for specifying a specific test file or directory
        parser.add_argument(
            '--test-file', 
            type=str, 
            help='Specify the test file or directory to run (e.g., tests/test_module.py)'
        )

    def handle(self, *args, **kwargs):
        test_file = kwargs.get('test_file', None)

        # Use Django's test runner
        TestRunner = get_runner(settings)
        test_runner = TestRunner()

        # Step 1: Run the coverage command
        self.stdout.write(self.style.SUCCESS('Running tests with coverage.... '))

        if test_file:
            # Step 2: Run tests with a specific test file or directory
            result = subprocess.run(
                ['coverage', 'run', 'manage.py', 'test', test_file],
                capture_output=True, text=True
            )
        else:
            # Step 2: Run all tests if no specific file is provided
            result = subprocess.run(
                ['coverage', 'run', 'manage.py', 'test'],
                capture_output=True, text=True
            )

        if result.returncode == 0:
            self.stdout.write(self.style.SUCCESS('Tests ran successfully. Generating coverage report...'))
            
            # Step 3: Generate the HTML coverage report
            subprocess.run(['coverage', 'html'])

            # Step 4: Open the HTML report in the default web browser
            self.stdout.write(self.style.SUCCESS('Opening the HTML coverage report...'))

            report_path = os.path.join(os.getcwd(), 'htmlcov', 'index.html')
            webbrowser.open(f'file://{report_path}')

        else:
            self.stdout.write(self.style.ERROR(f'Tests failed: {result.stderr}'))
