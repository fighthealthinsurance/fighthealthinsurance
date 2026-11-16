"""Prod's object-store client carries its own request limits.

Requests to the object store are bounded by the HTTP client's timeouts
(Prod.minio_http_client), not by interrupting the calling thread, so a store
that takes the connection and goes quiet costs seconds rather than minio-py's
default of minutes per try.
"""

import socket
import threading
import time

import pytest

from fighthealthinsurance import settings as fhi_settings


def _prod(**overrides):
    cfg = fhi_settings.Prod()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_http_client_uses_the_configured_timeouts():
    client = _prod(
        MINIO_CONNECT_TIMEOUT_S=1.5, MINIO_READ_TIMEOUT_S=7.0
    ).minio_http_client()
    timeout = client.connection_pool_kw["timeout"]
    assert (timeout.connect_timeout, timeout.read_timeout) == (1.5, 7.0)


def test_http_client_retries_a_bounded_number_of_times():
    client = _prod(MINIO_RETRIES=1).minio_http_client()
    assert client.connection_pool_kw["retries"].total == 1


@pytest.fixture
def silent_server():
    """A local port that accepts connections and never sends a byte."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held = []
    stop = threading.Event()

    def accept():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                held.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.getsockname()[1]}"
    stop.set()
    thread.join(timeout=2)
    for conn in held:
        conn.close()
    server.close()


def test_store_that_never_answers_is_given_up_on_quickly(silent_server):
    cfg = _prod(
        MINIO_STORAGE_ENDPOINT=silent_server,
        MINIO_STORAGE_ACCESS_KEY="test-access",
        MINIO_STORAGE_SECRET_KEY="test-secret",
        MINIO_STORAGE_MEDIA_BUCKET_NAME="test-bucket",
        MINIO_STORAGE_REGION="us-east-1",
        MINIO_CONNECT_TIMEOUT_S=0.3,
        MINIO_READ_TIMEOUT_S=0.3,
        MINIO_RETRIES=0,
    )
    start = time.monotonic()
    storage = cfg.EXTERNAL_STORAGE_B
    elapsed = time.monotonic() - start
    assert (storage, elapsed < 10) == (None, True)
