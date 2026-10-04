"""Opening a page in the appeal flow the way the site's own back links do.

A GET reaches a case only through a back link's reference (``?ref=``), which
resolves only in the session that minted it. A test that wants a mid-flow
page therefore mints one into the session its client or browser holds.
"""

import types

from django.urls import reverse

from fighthealthinsurance import views


def issue_ref(session, denial, email: str) -> str:
    """Mint a reference to ``denial`` into ``session`` and save it, as the views do."""
    token = views.issue_denial_ref_token(
        types.SimpleNamespace(session=session),
        denial.denial_id,
        email,
        denial.semi_sekret,
    )
    session.save()
    assert token is not None, "no reference minted: the email or case secret is empty"
    return token


def ref_url(url_name: str, token: str) -> str:
    """The path of ``url_name`` carrying ``token`` as its reference."""
    return f"{reverse(url_name)}?{views.DENIAL_REF_QUERY_PARAM}={token}"


def back_link(client, url_name: str, denial, email: str) -> str:
    """The path of ``url_name`` with a reference the test ``client`` can open."""
    return ref_url(url_name, issue_ref(client.session, denial, email))
