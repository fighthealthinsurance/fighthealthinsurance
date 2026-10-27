"""Database tests run last in this directory.

Most tests here use no database, and the sync activity tests call
close_old_connections() on the main thread. A database test leaves the main
thread's in-memory sqlite connection open (closing it would drop the
database), and pytest-django then refuses that call in every later test.
pytest-django sorts database tests first, so this moves them to the end.
"""

import pytest


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    items.sort(key=lambda item: item.get_closest_marker("django_db") is not None)
