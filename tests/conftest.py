"""
Pytest configuration for the test suite.

Sets up environment variables needed for tests to match tox configuration.
"""

import os

# Set TESTING=True to match tox.ini configuration
# This is needed for SessionRequiredMixin and other test-aware code
os.environ["TESTING"] = "True"
