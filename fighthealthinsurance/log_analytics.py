"""Optional Microsoft Azure Log Analytics integration.

Ships log records to an Azure Log Analytics workspace via the HTTP Data
Collector API. Disabled unless both ``LOG_ANALYTICS_WORKSPACE_ID`` and
``LOG_ANALYTICS_WORKSPACE_KEY`` are configured (Django settings or env vars).

The optional ``LOG_ANALYTICS_LOG_TYPE`` controls the destination custom log
table name (defaults to ``FightHealthInsurance``). When the integration is
disabled, all helpers and the handler short-circuit cheaply.

Reference:
    https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-collector-api
"""

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Optional, Union

import requests

DEFAULT_LOG_TYPE = "FightHealthInsurance"


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
    """Build the ``Authorization`` header value for the Data Collector API."""
    string_to_hash = (
        f"{method}\n{content_length}\n{content_type}\n" f"x-ms-date:{date}\n{resource}"
    )
    decoded_key = base64.b64decode(workspace_key)
    encoded_hash = base64.b64encode(
        hmac.new(
            decoded_key, string_to_hash.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
    ).decode("utf-8")
    return f"SharedKey {workspace_id}:{encoded_hash}"


def post_log(
    body: Union[list[dict[str, Any]], dict[str, Any]],
    log_type: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """POST one or more records to the workspace.

    Returns ``True`` when accepted (HTTP 2xx), ``False`` otherwise -- including
    the no-op case where Log Analytics is not configured.
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
    except (ValueError, TypeError):
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
        resp = requests.post(uri, data=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException:
        return False
    return 200 <= resp.status_code < 300


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

    The handler is safe to install unconditionally: it no-ops when the
    integration is not configured, and any shipment failure is swallowed so
    logging cannot break the calling code.
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
            post_log([entry], log_type=self._log_type, timeout=self._timeout)
        except Exception:
            self.handleError(record)
