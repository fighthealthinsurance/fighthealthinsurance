"""Optional Microsoft Azure Log Analytics integration.

Ships log records to an Azure Log Analytics workspace via the HTTP Data
Collector API. Disabled unless both ``LOG_ANALYTICS_WORKSPACE_ID`` and
``LOG_ANALYTICS_WORKSPACE_KEY`` are configured (Django settings or env vars).

The optional ``LOG_ANALYTICS_LOG_TYPE`` controls the destination custom log
table name (defaults to ``FightHealthInsurance``). When the integration is
disabled, all helpers and the handler short-circuit cheaply.

Delivery from the stdlib/loguru handler runs on a background daemon thread
with a shared :class:`requests.Session`, so request handlers never block on
log shipment. The in-memory send queue is bounded; records are dropped on
overflow rather than back-pressuring callers.

Reference:
    https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-collector-api
"""

import atexit
import base64
import binascii
import datetime
import hashlib
import hmac
import json
import logging
import os
import queue as _queue_mod
import threading
from typing import Any, Optional, Union

import requests

DEFAULT_LOG_TYPE = "FightHealthInsurance"
DEFAULT_QUEUE_SIZE = 10000


def _get_setting(name: str, default: str = "") -> str:
    """Resolve a Log Analytics setting from Django, falling back to env."""
    try:
        from django.conf import settings

        value = getattr(settings, name, None)
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv(name, default) or default


def is_log_analytics_enabled() -> bool:
    """True only when both workspace ID and shared key are present."""
    return bool(_get_setting("LOG_ANALYTICS_WORKSPACE_ID")) and bool(
        _get_setting("LOG_ANALYTICS_WORKSPACE_KEY")
    )


def _build_signature(
    workspace_id: str,
    workspace_key: str,
    date: str,
    content_length: int,
    method: str = "POST",
    content_type: str = "application/json",
    resource: str = "/api/logs",
) -> str:
    """Build the ``Authorization`` header value for the Data Collector API.

    Raises ``binascii.Error`` if ``workspace_key`` is not valid base64;
    callers convert that into a no-op rather than crashing the app.
    """
    string_to_hash = (
        f"{method}\n{content_length}\n{content_type}\n" f"x-ms-date:{date}\n{resource}"
    )
    # validate=True so silently-truncated keys can't slip through and produce
    # bogus signatures that 403 server-side.
    decoded_key = base64.b64decode(workspace_key, validate=True)
    encoded_hash = base64.b64encode(
        hmac.new(
            decoded_key, string_to_hash.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
    ).decode("utf-8")
    return f"SharedKey {workspace_id}:{encoded_hash}"


# ---------------------------------------------------------------------------
# Shared HTTP session + background shipping worker
# ---------------------------------------------------------------------------

_session: Optional[requests.Session] = None
_session_lock = threading.Lock()

_send_queue: "_queue_mod.Queue[Optional[dict]]" = _queue_mod.Queue(
    maxsize=DEFAULT_QUEUE_SIZE
)
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _get_session() -> requests.Session:
    """Return a process-wide :class:`requests.Session` for connection reuse."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = requests.Session()
    return _session


def _post_log_sync(
    body: Union[list[dict[str, Any]], dict[str, Any]],
    log_type: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """Synchronously POST ``body`` to the configured workspace.

    Returns ``True`` on HTTP 2xx, ``False`` otherwise (including the disabled
    case or a misconfigured base64 key).
    """
    if not is_log_analytics_enabled():
        return False

    workspace_id = _get_setting("LOG_ANALYTICS_WORKSPACE_ID")
    workspace_key = _get_setting("LOG_ANALYTICS_WORKSPACE_KEY")
    log_type_resolved = (
        log_type
        or _get_setting("LOG_ANALYTICS_LOG_TYPE", DEFAULT_LOG_TYPE)
        or DEFAULT_LOG_TYPE
    )

    payload = json.dumps(body, default=str)
    content_length = len(payload.encode("utf-8"))
    rfc1123 = datetime.datetime.now(datetime.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")

    try:
        signature = _build_signature(
            workspace_id, workspace_key, rfc1123, content_length
        )
    except (binascii.Error, ValueError, TypeError):
        # Misconfigured key (e.g. not valid base64): disable rather than crash.
        return False

    uri = (
        f"https://{workspace_id}.ods.opinsights.azure.com"
        f"/api/logs?api-version=2016-04-01"
    )
    headers = {
        "content-type": "application/json",
        "Authorization": signature,
        "Log-Type": log_type_resolved,
        "x-ms-date": rfc1123,
    }
    try:
        resp = _get_session().post(uri, data=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException:
        return False
    return 200 <= resp.status_code < 300


def _worker_loop() -> None:
    """Pull queued shipments and POST them one at a time. ``None`` shuts down."""
    while True:
        item = _send_queue.get()
        try:
            if item is None:  # shutdown sentinel
                return
            _post_log_sync(
                item["body"],
                log_type=item.get("log_type"),
                timeout=item.get("timeout", 10.0),
            )
        except Exception:
            # Worker must never die; logging cannot back-pressure or crash the app.
            pass
        finally:
            _send_queue.task_done()


def _shutdown_worker() -> None:
    """Signal the background worker to drain and exit. Best-effort."""
    try:
        _send_queue.put_nowait(None)
    except _queue_mod.Full:
        pass


def _ensure_worker_started() -> None:
    """Lazily start the background shipping thread on first use."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            t = threading.Thread(
                target=_worker_loop, daemon=True, name="log-analytics-shipper"
            )
            t.start()
            atexit.register(_shutdown_worker)
            _worker = t


def post_log(
    body: Union[list[dict[str, Any]], dict[str, Any]],
    log_type: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """Public synchronous POST helper, primarily for ad-hoc / test use.

    Application logging goes through :class:`LogAnalyticsHandler`, which
    ships from a background thread so the request path never blocks.
    """
    return _post_log_sync(body, log_type=log_type, timeout=timeout)


def build_record(record: logging.LogRecord, formatted_message: str) -> dict[str, Any]:
    """Translate a stdlib ``LogRecord`` to a Log Analytics-friendly dict."""
    entry: dict[str, Any] = {
        "timestamp": datetime.datetime.fromtimestamp(
            record.created, tz=datetime.UTC
        ).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "message": formatted_message,
        "module": record.module,
        "line_number": record.lineno,
    }
    if record.exc_info:
        entry["exception"] = logging.Formatter().formatException(record.exc_info)
    return entry


class LogAnalyticsHandler(logging.Handler):
    """Stdlib logging handler that ships records to Azure Log Analytics.

    ``emit()`` enqueues a serialised record and returns immediately; a single
    background daemon thread drains the queue and performs the HTTPS POST,
    so the request path never blocks on network I/O. If the queue is full
    the record is dropped rather than back-pressuring the caller.

    The handler is safe to install unconditionally: it no-ops when the
    integration is not configured, and any shipment failure is swallowed.
    """

    def __init__(
        self,
        log_type: Optional[str] = None,
        level: int = logging.NOTSET,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(level=level)
        self._log_type = log_type
        self._timeout = timeout

    def emit(self, record: logging.LogRecord) -> None:
        if not is_log_analytics_enabled():
            return
        try:
            entry = build_record(record, self.format(record))
            _ensure_worker_started()
            try:
                _send_queue.put_nowait(
                    {
                        "body": [entry],
                        "log_type": self._log_type,
                        "timeout": self._timeout,
                    }
                )
            except _queue_mod.Full:
                # Drop the record rather than block the caller.
                pass
        except Exception:
            self.handleError(record)
