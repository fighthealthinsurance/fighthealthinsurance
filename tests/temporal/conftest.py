"""Shared setup for the Temporal tests.

Database tests run last in this directory.

Most tests here use no database, and the sync activity tests call
close_old_connections() on the main thread. A database test leaves the main
thread's in-memory sqlite connection open (closing it would drop the
database), and pytest-django then refuses that call in every later test.
pytest-django sorts database tests first, so this moves them to the end.

The test server download is retried.

WorkflowEnvironment.start_local / start_time_skipping download the Temporal
CLI or the Java test server into the temp directory the first time they run
in a fresh machine, which on CI is every run. temporal.download has answered
that download with a 429 (Nov 2026), failing every test that happened to be
first to need the binary while the rest of the suite passed. A download
failure is retried with backoff; anything else is raised at once.
"""

import pytest
from temporalio.testing import WorkflowEnvironment

from tests.temporal_download_retry import _retrying_download


@pytest.fixture(autouse=True, scope="session")
def _retry_temporal_server_downloads():
    with pytest.MonkeyPatch.context() as mp:
        for name in ("start_local", "start_time_skipping"):
            # getattr on the class gives the bound classmethod; wrap it and
            # put it back as a staticmethod so callers' calls are unchanged.
            start = getattr(WorkflowEnvironment, name)
            mp.setattr(
                WorkflowEnvironment, name, staticmethod(_retrying_download(start))
            )
        yield


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    items.sort(key=lambda item: item.get_closest_marker("django_db") is not None)
