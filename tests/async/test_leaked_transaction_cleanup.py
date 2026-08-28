"""Regression test for the between-test cleanup in ``tests/conftest.py``.

A plain ``async def`` test runs every native async ORM call and every
``database_sync_to_async`` body on asgiref's process-wide
``SyncToAsync.single_thread_executor``. If a call there leaves its connection
mid-transaction -- an ``atomic`` whose COMMIT failed, or one whose awaiting task
was abandoned when the event loop closed -- nothing clears it: Django refuses to
close an in-memory sqlite connection, because closing would destroy the
database. The open transaction holds a sqlite table lock, so the next
``transaction=True`` test dies in its teardown flush ("Database ... couldn't be
flushed") and the rows that flush failed to delete leak into every later test.

``_rollback_leaked_transactions`` is what breaks that chain, so it has its own
test: the fixture that calls it only shows up as a mystery flake far away.
"""

import pytest
from asgiref.sync import sync_to_async

from fighthealthinsurance.models import ChooserTask
from tests.conftest import _rollback_leaked_transactions


def _abandon_an_open_transaction() -> int:
    """Leave this thread's connection mid-transaction; return the written pk."""
    from django.db import transaction

    transaction.atomic().__enter__()  # deliberately never exited
    task = ChooserTask.objects.create(
        task_type="appeal", status="READY", source="synthetic"
    )
    return task.pk


def _in_transaction() -> bool:
    """Whether the calling thread's connection is mid-transaction."""
    from django.db import connection

    return bool(connection.connection and connection.connection.in_transaction)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_abandoned_transaction_is_rolled_back_on_the_executor_thread():
    """The cleanup ends an abandoned transaction and undoes its write."""
    # thread_sensitive is spelled out because the test depends on it: it is what
    # puts every call below on the one thread-sensitive executor, so they all
    # see the same connection.
    pk = await sync_to_async(_abandon_an_open_transaction, thread_sensitive=True)()
    assert await sync_to_async(_in_transaction, thread_sensitive=True)() is True

    await sync_to_async(_rollback_leaked_transactions, thread_sensitive=True)()

    assert await sync_to_async(_in_transaction, thread_sensitive=True)() is False
    # The abandoned write went with it, so the table lock is gone and the next
    # test's teardown flush can run.
    assert not await ChooserTask.objects.filter(pk=pk).aexists()
