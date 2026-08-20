"""MetricsAccessMiddleware must gate /metrics on the ASGI path too.

Prod serves through uvicorn, so the middleware's async branch is the one that
actually runs there; a sync-only gate would be adapted per-request and, worse,
a broken async branch would silently pass every request through.
"""

import pytest
from django.http import HttpResponse
from django.test import AsyncClient, RequestFactory, override_settings

from fighthealthinsurance.middleware import MetricsAccessMiddleware

IN_CLUSTER_ADDR = "10.42.0.7"


async def _ok(_request):
    return HttpResponse("metrics")


@pytest.mark.asyncio
async def test_async_middleware_serves_in_cluster_scrape():
    middleware = MetricsAccessMiddleware(_ok)
    request = RequestFactory().get("/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR)
    response = await middleware(request)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_async_middleware_404s_forwarded_request():
    middleware = MetricsAccessMiddleware(_ok)
    request = RequestFactory().get(
        "/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR, HTTP_X_FORWARDED_FOR="203.0.113.5"
    )
    response = await middleware(request)
    assert response.status_code == 404


# The ASGI-stack tests below go through the real handler and MIDDLEWARE list
# rather than the middleware class alone. AsyncClient pins the scope's peer
# address to 127.0.0.1 (extra kwargs become headers, not scope keys), so the
# peer-address branch is exercised by narrowing the allowed CIDRs instead.


@pytest.mark.asyncio
async def test_asgi_stack_serves_an_in_cluster_scrape():
    response = await AsyncClient().get("/metrics")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_asgi_stack_returns_prometheus_content():
    response = await AsyncClient().get("/metrics")
    assert b"# HELP" in response.content


@pytest.mark.asyncio
async def test_asgi_stack_404s_a_forwarded_request():
    response = await AsyncClient().get(
        "/metrics", headers={"x-forwarded-for": "203.0.113.5"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_asgi_stack_404s_a_peer_outside_the_allowed_cidrs():
    with override_settings(METRICS_ALLOWED_CIDRS=[f"{IN_CLUSTER_ADDR}/32"]):
        response = await AsyncClient().get("/metrics")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_async_middleware_404s_public_peer():
    middleware = MetricsAccessMiddleware(_ok)
    request = RequestFactory().get("/metrics", REMOTE_ADDR="203.0.113.5")
    response = await middleware(request)
    assert response.status_code == 404
