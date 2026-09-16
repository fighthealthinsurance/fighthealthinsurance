"""Back links carry an opaque reference, not the case's credentials.

``build_back_url`` used to urlencode (denial_id, email, semi_sekret) into the
query string of every back link from step 3 onward, where the triple lands in
browser history, in access logs, and in anything the person screenshots. The
triple is the whole credential on the case: ``sensitive_post_parameters``
covers POST bodies only, and ``SessionRequiredMixin`` enforces a session only
under DEBUG or TESTING.

The link now carries one random string, resolved server side against the
session that issued it. These tests pin that it is worth nothing on its own,
nothing in another session and nothing once expired, that every consumer
resolves it (including the ``SessionRequiredMixin`` pages, which the three
named GET handlers do not cover), and that the transition window for the old
triple can be closed.

The scheme costs the patient a back link that works on a second device, so
``CrossDeviceResumeTest`` is the acceptance for what they are told instead,
and ``SlidingLifetimeTest`` for not failing at somebody who did nothing wrong.
``ProductionShapedRefusalTest`` asserts the refusals with the session gate
removed the way ``Prod`` removes it, because under tox that gate is on and the
refusal once sat behind it.
"""

import ast
import base64
import json
import os
import pathlib
import re
import subprocess
import time
import types
from datetime import timedelta
from unittest.mock import patch

from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import common_view_logic, models, views

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Reading every tracked blob would put 76MB through this test on every run, so
# anything a purge could not be written in is read only up to here. Anything it
# could be written in is read whole however big it gets, because "we skipped it
# for size" is exactly how a search like this goes quietly blind.
LARGEST_OPAQUE_FILE_WORTH_READING = 512 * 1024
PURGE_COULD_BE_WRITTEN_IN_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".env",
        ".ini",
        ".js",
        ".json",
        ".mk",
        ".py",
        ".service",
        ".sh",
        ".sql",
        ".timer",
        ".toml",
        ".ts",
        ".tf",
        ".txt",
        ".yaml",
        ".yml",
    }
)
PURGE_COULD_BE_WRITTEN_IN_NAMES = ("Makefile", "Dockerfile", "Procfile", "Justfile")


def repo_files() -> list:
    """Every file tracked in this checkout, relative to its root.

    Tracked files plus anything untracked that git is not ignoring.
    ``RetentionClaimTest`` used to search a hand written list of directories
    (k8s, charts, scripts, conf, the Makefile). A reviewer added a
    ``clearsessions`` step under ``.github/workflows/`` and the test stayed
    green, so the list is gone: ask git what the repo holds. The walk is a
    fallback for a checkout with no git binary on PATH, where returning
    nothing would read as a pass.
    """
    try:
        listing = subprocess.run(
            # --others --exclude-standard so a purge that is written but not
            # yet committed still counts: a reviewer dropping one in to check
            # this test bites should see it bite.
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=120,
        ).stdout
        tracked = [pathlib.Path(name) for name in listing.split("\0") if name]
    except (OSError, subprocess.SubprocessError):
        tracked = []
    if tracked:
        return tracked

    skip = {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "fhi.egg-info",
    }
    walked = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [name for name in dirnames if name not in skip]
        here = pathlib.Path(dirpath)
        walked.extend((here / name).relative_to(REPO_ROOT) for name in filenames)
    return walked


def could_hold_a_purge(relative_path) -> bool:
    """Whether a scheduled job could plausibly be written in this file."""
    name = relative_path.name
    return relative_path.suffix in PURGE_COULD_BE_WRITTEN_IN_SUFFIXES or any(
        name.startswith(prefix) for prefix in PURGE_COULD_BE_WRITTEN_IN_NAMES
    )


def read_repo_text(relative_path) -> str:
    """A tracked file as text, or an empty string if it is not readable text."""
    path = REPO_ROOT / relative_path
    try:
        if (
            not could_hold_a_purge(relative_path)
            and path.stat().st_size > LARGEST_OPAQUE_FILE_WORTH_READING
        ):
            return ""
        return path.read_text(errors="ignore")
    except OSError:
        return ""


EMAIL = "back-link-walker@example.com"
SEMI_SEKRET = "a-permanent-case-secret"

# Every path the appeal flow can link to. Used to pick the flow links out of
# a page without depending on a button's class or label.
FLOW_URL_NAMES = (
    "scan",
    "process",
    "hh",
    "dvc",
    "eev",
    "categorize_review",
    "find_next_steps",
    "generate_appeal",
    "escalation_packet",
)


def hrefs(response) -> list:
    """Every href on the page."""
    return re.findall(r'href="([^"]*)"', response.content.decode())


def flow_hrefs(response) -> list:
    """The hrefs that point at a page in the appeal flow."""
    paths = {reverse(name) for name in FLOW_URL_NAMES}
    return [h for h in hrefs(response) if h.split("?")[0] in paths]


class BackLinkReferenceTestBase(TestCase):
    def setUp(self):
        self.client = Client()
        self.denial = models.Denial.objects.create(
            denial_text="They said no.",
            hashed_email=models.Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
            insurance_company="Aetna",
            your_state="CA",
        )

    def issue_token(self, client=None, denial=None, email=EMAIL) -> str:
        """Mint a reference into a client's session the way the views do."""
        client = client or self.client
        denial = denial or self.denial
        session = client.session
        token = views.issue_denial_ref_token(
            types.SimpleNamespace(session=session),
            denial.denial_id,
            email,
            denial.semi_sekret,
        )
        session.save()
        assert token is not None
        return token

    def ref_url(self, url_name: str, token: str) -> str:
        return f"{reverse(url_name)}?{views.DENIAL_REF_QUERY_PARAM}={token}"

    def stored_expiry(self, token: str, client=None) -> float:
        """When a reference stops resolving, read out of the reference."""
        client = client or self.client
        expiry = views.denial_ref_expiry(client.session, token)
        assert expiry is not None, "this session cannot read that reference"
        return expiry

    def issue_token_at(self, minted_at: float, client=None, denial=None) -> str:
        """Mint a reference as if the clock read ``minted_at``."""
        with patch("time.time", return_value=minted_at):
            return self.issue_token(client=client, denial=denial)

    def expired_token(self, client=None, denial=None) -> str:
        """A reference minted just past its window."""
        return self.issue_token_at(
            time.time() - views.DENIAL_REF_IDLE_TTL_SECONDS - 1,
            client=client,
            denial=denial,
        )

    def legacy_url(self, url_name: str) -> str:
        return (
            f"{reverse(url_name)}?denial_id={self.denial.denial_id}"
            f"&email={EMAIL}&semi_sekret={self.denial.semi_sekret}"
        )

    def assertUrlCarriesNoCredential(self, url: str):
        """The address bar holds nothing about the person or the case."""
        query = url.split("?", 1)[1] if "?" in url else ""
        self.assertNotIn("@", url, msg=f"email address in URL: {url}")
        self.assertNotIn(EMAIL, url, msg=f"email address in URL: {url}")
        self.assertNotIn(self.denial.semi_sekret, url, msg=f"case secret in URL: {url}")
        self.assertNotIn(
            models.Denial.get_hashed_email(EMAIL),
            url,
            msg=f"hashed email in URL: {url}",
        )
        self.assertNotIn("semi_sekret", query, msg=f"secret named in URL: {url}")
        self.assertNotIn("email", query, msg=f"email named in URL: {url}")
        self.assertNotIn("denial_id", query, msg=f"case id named in URL: {url}")
        if query:
            self.assertEqual(
                [pair.split("=")[0] for pair in query.split("&")],
                [views.DENIAL_REF_QUERY_PARAM],
                msg=f"unexpected query parameters in URL: {url}",
            )

    def assertLandsOnUploadPageWithHelp(self, response):
        """Refused, and the upload page is told to explain why.

        Everything that fails to resolve a back link goes to one place, and
        that place knows a back link was followed. A bare redirect to the
        upload page is what made this look like "your appeal is gone".
        """
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f"{reverse('scan')}?{views.RESUME_HELP_QUERY_PARAM}=1",
            msg="did not send the person somewhere that explains itself",
        )

    def assertReferenceFormPopulated(self, response):
        """The destination arrived with the case reference filled in.

        Every page in the flow posts the triple onward from hidden fields, so
        a page that renders an empty denial_id is a page the next step cannot
        be reached from.
        """
        body = response.content.decode()
        self.assertIn(
            f'name="denial_id" value="{self.denial.denial_id}"',
            body.replace("\n", " "),
            msg="destination did not render a populated denial reference",
        )


class BackLinkWalkTest(BackLinkReferenceTestBase):
    """Walk the flow backwards, following each link the server actually built."""

    # (page url name, template, url name its back link must point at)
    CHAIN = (
        ("escalation_packet", "escalation_packet.html", "generate_appeal"),
        ("generate_appeal", "appeals.html", "find_next_steps"),
        ("find_next_steps", "outside_help.html", "categorize_review"),
        ("categorize_review", "categorize.html", "dvc"),
        ("dvc", "plan_documents.html", "hh"),
    )

    def follow_back(self, url, expect_template, expect_back_name):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, msg=f"{url} did not render")
        self.assertTemplateUsed(response, expect_template)
        self.assertReferenceFormPopulated(response)
        for href in flow_hrefs(response):
            self.assertUrlCarriesNoCredential(href)
        back_path = reverse(expect_back_name)
        # Distinct hrefs: the questions page offers "Ask me some questions
        # anyway" next to its Back button, and both carry the same reference.
        matching = sorted(
            {h for h in flow_hrefs(response) if h.split("?")[0] == back_path}
        )
        self.assertEqual(
            len(matching),
            1,
            msg=f"{url} did not render exactly one back link to {back_path}",
        )
        return matching[0]

    def test_walking_back_through_the_flow_never_shows_a_credential(self):
        url = self.ref_url("escalation_packet", self.issue_token())
        self.assertUrlCarriesNoCredential(url)
        for page_name, template, back_name in self.CHAIN:
            self.assertEqual(url.split("?")[0], reverse(page_name))
            url = self.follow_back(url, template, back_name)

        # The last hop is the health history page; its back link is the
        # upload page, which needs no reference at all.
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "health_history.html")
        self.assertReferenceFormPopulated(response)
        for href in flow_hrefs(response):
            self.assertUrlCarriesNoCredential(href)

    def test_extraction_page_is_reached_and_linked_by_reference(self):
        """The pages behind SessionRequiredMixin are consumers too."""
        url = self.ref_url("eev", self.issue_token())
        back = self.follow_back(url, "entity_extract.html", "dvc")
        response = self.client.get(back)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "plan_documents.html")
        self.assertReferenceFormPopulated(response)

    def test_extraction_socket_still_gets_a_credential_it_can_resolve(self):
        """PR1's gate resolves the triple the page puts on the wire.

        The socket authorizes with the same triple
        ``common_view_logic.get_denial_for_action`` checks, and it reads it
        out of the page's form context, not out of the URL. Moving the link
        to an opaque reference must not starve it.
        """
        response = self.client.get(self.ref_url("eev", self.issue_token()))
        self.assertEqual(response.status_code, 200)
        match = re.search(
            r'<script id="fhi-form-context" type="application/json">(.*?)</script>',
            response.content.decode(),
            re.S,
        )
        self.assertIsNotNone(match, msg="page sent the socket no form context")
        context = json.loads(match.group(1).replace("\\u0022", '"'))
        resolved = common_view_logic.get_denial_for_action(
            context["denial_id"], context["email"], context["semi_sekret"]
        )
        self.assertIsNotNone(resolved, msg="socket credential no longer resolves")
        self.assertEqual(resolved.denial_id, self.denial.denial_id)


class TokenOpacityTest(BackLinkReferenceTestBase):
    def test_nothing_about_the_case_can_be_recovered_from_the_token(self):
        """Signing is not hiding, so the reference is not an encoded payload.

        A ``django.core.signing.dumps`` of the triple would pass a plain
        substring check and still hand over the email and the permanent
        secret to anyone who base64-decodes it. Decode it every way it could
        be encoded and look again.
        """
        token = self.issue_token()
        # The denial_id is deliberately not in this list: it is a short
        # integer, so looking for it inside a random string is a coin flip.
        # The walk pins the query string down to one ref parameter instead.
        secrets_that_must_not_appear = (
            EMAIL,
            EMAIL.split("@")[0],
            "@example.com",
            self.denial.semi_sekret,
            models.Denial.get_hashed_email(EMAIL),
        )

        candidates = [token]
        # A signed payload is "<payload>:<sig>:<sig>"; try each colon-separated
        # part as well as the whole string.
        candidates.extend(token.split(":"))
        decoded = []
        for candidate in candidates:
            decoded.append(candidate)
            for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                padded = candidate + "=" * (-len(candidate) % 4)
                try:
                    raw = decoder(padded.encode())
                except Exception:
                    continue
                decoded.append(raw.decode("utf-8", errors="replace"))
                # signing.dumps zlib-compresses when that is shorter.
                try:
                    import zlib

                    decoded.append(
                        zlib.decompress(raw).decode("utf-8", errors="replace")
                    )
                except Exception:
                    pass

        for blob in decoded:
            for secret in secrets_that_must_not_appear:
                self.assertNotIn(
                    secret,
                    blob,
                    msg=f"{secret!r} recoverable from the reference: {blob!r}",
                )

    def test_two_cases_in_one_session_get_different_references(self):
        other = models.Denial.objects.create(
            denial_text="A second denial.",
            hashed_email=models.Denial.get_hashed_email(EMAIL),
            semi_sekret="a-different-secret",
        )
        self.assertNotEqual(self.issue_token(), self.issue_token(denial=other))

    def test_the_same_case_can_be_referenced_more_than_once(self):
        """Each render mints afresh; every reference to the case resolves."""
        first, second = self.issue_token(), self.issue_token()
        self.assertNotEqual(first, second)
        request = types.SimpleNamespace(session=self.client.session)
        for token in (first, second):
            self.assertEqual(
                views.resolve_denial_ref_token(request, token)["denial_id"],
                str(self.denial.denial_id),
            )

    def test_nothing_about_a_reference_is_stored_in_the_session(self):
        token = self.issue_token()
        session = self.client.session
        self.assertNotIn("denial_back_refs", session.keys())
        for value in session.values():
            self.assertNotIn(token, str(value))
        self.assertNotIn(self.denial.semi_sekret, str(dict(session)))


class TokenScopeAndExpiryTest(BackLinkReferenceTestBase):
    def test_a_reference_from_another_session_is_refused(self):
        token = self.issue_token()
        other_browser = Client()
        response = other_browser.get(self.ref_url("generate_appeal", token))
        self.assertLandsOnUploadPageWithHelp(response)

    def test_a_tampered_reference_lands_on_the_upload_page(self):
        token = self.issue_token()
        response = self.client.get(self.ref_url("generate_appeal", token + "x"))
        self.assertLandsOnUploadPageWithHelp(response)

    def test_the_reference_is_stored_with_the_documented_lifetime(self):
        issued_at = time.time()
        token = self.issue_token()
        self.assertAlmostEqual(
            self.stored_expiry(token) - issued_at,
            views.DENIAL_REF_IDLE_TTL_SECONDS,
            delta=30,
        )
        self.assertEqual(views.DENIAL_REF_IDLE_TTL_SECONDS, 12 * 60 * 60)

    def test_an_expired_reference_lands_on_the_upload_page(self):
        token = self.expired_token()

        response = self.client.get(self.ref_url("generate_appeal", token))
        self.assertLandsOnUploadPageWithHelp(response)

    def test_an_expired_reference_is_refused_by_the_session_mixin_pages_too(self):
        token = self.expired_token()

        response = self.client.get(self.ref_url("eev", token))
        self.assertLandsOnUploadPageWithHelp(response)


class SlidingLifetimeTest(BackLinkReferenceTestBase):
    """A person working an appeal never hits a cliff.

    Every page render mints fresh references for the links it shows, each
    good for the full window from that moment, so following a link inside
    its window always leaves the person holding links good for another full
    window. There is nothing stored to push out.
    """

    def _tokens_on(self, response) -> list:
        return [
            parse_qs(urlparse(href).query)[views.DENIAL_REF_QUERY_PARAM][0]
            for href in flow_hrefs(response)
            if views.DENIAL_REF_QUERY_PARAM in parse_qs(urlparse(href).query)
        ]

    def test_following_a_back_link_leaves_fresh_links_on_the_page(self):
        nearly_up = time.time() - views.DENIAL_REF_IDLE_TTL_SECONDS + 60
        token = self.issue_token_at(nearly_up)

        response = self.client.get(self.ref_url("generate_appeal", token))

        self.assertEqual(response.status_code, 200)
        fresh = self._tokens_on(response)
        self.assertTrue(fresh, "the page rendered no reference links")
        for new_token in fresh:
            self.assertNotEqual(new_token, token)
            self.assertAlmostEqual(
                self.stored_expiry(new_token) - time.time(),
                views.DENIAL_REF_IDLE_TTL_SECONDS,
                delta=30,
            )

    def test_the_mixin_pages_mint_fresh_links_too(self):
        for url_name in ("hh", "dvc", "eev"):
            with self.subTest(url_name=url_name):
                nearly_up = time.time() - views.DENIAL_REF_IDLE_TTL_SECONDS + 60
                token = self.issue_token_at(nearly_up)
                response = self.client.get(self.ref_url(url_name, token))
                self.assertEqual(response.status_code, 200)
                for new_token in self._tokens_on(response):
                    self.assertGreater(
                        self.stored_expiry(new_token), self.stored_expiry(token)
                    )

    def test_a_day_of_work_never_hits_the_cliff(self):
        """Hop every eleven hours for three days; each hop follows a link the
        previous page minted."""
        step = views.DENIAL_REF_IDLE_TTL_SECONDS - 60 * 60
        now = time.time()
        token = self.issue_token_at(now)
        for hop in range(1, 7):
            with patch("time.time", return_value=now + hop * step):
                response = self.client.get(self.ref_url("generate_appeal", token))
                self.assertEqual(response.status_code, 200, f"hop {hop} was refused")
                fresh = self._tokens_on(response)
                self.assertTrue(fresh, f"hop {hop} rendered no reference links")
                token = fresh[0]

    def test_a_reference_nobody_touches_still_goes_stale(self):
        token = self.issue_token()
        later = time.time() + views.DENIAL_REF_IDLE_TTL_SECONDS + 1
        with patch("time.time", return_value=later):
            response = self.client.get(self.ref_url("generate_appeal", token))
        self.assertLandsOnUploadPageWithHelp(response)


class NothingToLoseInASaveRaceTest(BackLinkReferenceTestBase):
    """Two requests minting at once cannot lose each other's references.

    The old scheme kept every reference in one session dictionary that each
    request saved whole, so the second save dropped the first's entry. A
    reference is now self-contained, and the session holds only a secret
    written when the case was started and the case's email, written with
    the same value every time.
    """

    def test_a_reference_survives_a_stale_session_snapshot_saved_after_it(self):
        # The upload request bound the session to the case: secret and email
        # written, ahead of any page that renders a link.
        bound = self.client.session
        views._denial_ref_fernet(bound, create=True)
        views.remember_denial_ref_email(bound, self.denial.denial_id, EMAIL)
        bound.save()
        # Request 2 loads the session now, before request 1 has minted.
        stale = self.client.session
        # Request 1 renders a link.
        token = self.issue_token()
        # Request 2 saves last, carrying only what it read.
        stale["unrelated"] = "write from a concurrent request"
        stale.save()

        response = self.client.get(self.ref_url("generate_appeal", token))

        self.assertEqual(response.status_code, 200)

    def test_two_references_minted_from_one_snapshot_both_resolve(self):
        request = types.SimpleNamespace(session=self.client.session)
        first = views.issue_denial_ref_token(
            request, self.denial.denial_id, EMAIL, self.denial.semi_sekret
        )
        other = models.Denial.objects.create(
            denial_text="A second denial.",
            hashed_email=models.Denial.get_hashed_email(EMAIL),
            semi_sekret="a-different-secret",
        )
        second = views.issue_denial_ref_token(
            request, other.denial_id, EMAIL, other.semi_sekret
        )
        request.session.save()

        for token, denial in ((first, self.denial), (second, other)):
            self.assertEqual(
                views.resolve_denial_ref_token(request, token)["denial_id"],
                str(denial.denial_id),
            )

    def test_a_session_holding_junk_under_the_keys_does_not_500(self):
        token = self.issue_token()
        session = self.client.session
        session[views._DENIAL_REF_KEY_SESSION_KEY] = ["not", "a", "string"]
        session[views._DENIAL_REF_EMAILS_SESSION_KEY] = "not a dict"
        session.save()

        response = self.client.get(self.ref_url("generate_appeal", token))

        self.assertLandsOnUploadPageWithHelp(response)
        request = types.SimpleNamespace(session=self.client.session)
        reissued = views.issue_denial_ref_token(
            request, self.denial.denial_id, EMAIL, self.denial.semi_sekret
        )
        self.assertIsNotNone(reissued, msg="issuing died on a junk session value")


class LegacyQueryTripleTest(BackLinkReferenceTestBase):
    """The old links keep working for one release, and can then be shut off."""

    def test_the_old_triple_still_resolves_during_the_transition(self):
        response = self.client.get(self.legacy_url("generate_appeal"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "appeals.html")

    def test_the_old_triple_resolves_on_the_session_mixin_pages_too(self):
        response = self.client.get(self.legacy_url("dvc"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "plan_documents.html")
        self.assertReferenceFormPopulated(response)

    def test_arriving_on_an_old_link_hands_back_only_opaque_links(self):
        """The legacy path cannot be used to lift the secret into a new link."""
        response = self.client.get(self.legacy_url("generate_appeal"))
        self.assertEqual(response.status_code, 200)
        links = flow_hrefs(response)
        self.assertTrue(links, msg="no flow links on the page to check")
        for href in links:
            self.assertUrlCarriesNoCredential(href)

    @override_settings(LEGACY_DENIAL_REF_QUERY=False)
    def test_closing_the_window_refuses_the_old_triple(self):
        response = self.client.get(self.legacy_url("generate_appeal"))
        self.assertLandsOnUploadPageWithHelp(response)

    @override_settings(LEGACY_DENIAL_REF_QUERY=False)
    def test_closing_the_window_refuses_the_old_triple_on_mixin_pages(self):
        """The mixin's dispatch reads the query string as well as the resolver."""
        response = self.client.get(self.legacy_url("dvc"))
        self.assertLandsOnUploadPageWithHelp(response)

    def test_a_bare_case_id_in_the_query_still_seeds_the_session(self):
        """SessionRequiredMixin.dispatch reads the query string on its own.

        Nothing in the site builds such a link, but the mixin has always
        accepted one, so it keeps working for the same window as the rest of
        the old shape.
        """
        response = self.client.get(
            f"{reverse('dvc')}?denial_id={self.denial.denial_id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "plan_documents.html")
        self.assertEqual(
            str(self.client.session.get("denial_id")),
            str(self.denial.denial_id),
            msg="the bare case id did not seed the session",
        )

    @override_settings(LEGACY_DENIAL_REF_QUERY=False)
    def test_closing_the_window_refuses_a_bare_case_id_in_the_query(self):
        """The case id is an identifier for a medical case; it goes too.

        Scope, stated so this is not read as more than it is: the seeding it
        refuses lives INSIDE ``SessionRequiredMixin``'s session gate, which
        is off in production. So this pins a session-enforcing configuration
        (DEBUG on, or TESTING set, which is what tox does) and nothing about
        the deployed site, where that branch has never run at all.
        ``ProductionShapedRefusalTest`` has the production half.
        """
        self.assertTrue(
            views.session_gate_enforced(),
            msg="this test only means anything where the session gate is on",
        )
        response = self.client.get(
            f"{reverse('dvc')}?denial_id={self.denial.denial_id}"
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(response.url, (reverse("scan"), reverse("process")))

    @override_settings(LEGACY_DENIAL_REF_QUERY=False)
    def test_closing_the_window_leaves_the_new_reference_working(self):
        response = self.client.get(self.ref_url("generate_appeal", self.issue_token()))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "appeals.html")

    @override_settings(LEGACY_DENIAL_REF_QUERY=False)
    def test_closing_the_window_leaves_posted_hidden_fields_working(self):
        """The gate is about the address bar, not about form bodies."""
        response = self.client.post(
            reverse("escalation_packet"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": self.denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "escalation_packet.html")


@override_settings(DEBUG=False)
class ProductionShapedRefusalTest(BackLinkReferenceTestBase):
    """The refusal is real where it matters, not only where the tests run.

    ``SessionRequiredMixin``'s session gate is off in production on purpose,
    and on under tox (``TESTING=True``), so a refusal that sits behind it goes
    green here over a production that serves the patient a blank form instead.
    This class removes the gate the way ``Prod`` does (``DEBUG = False``,
    ``pre_setup`` deleting ``TESTING``) and asserts every refusal again. The
    first two tests prove the removal took; without them the rest would pass
    for the wrong reason.
    """

    def setUp(self):
        super().setUp()
        # patch.dict with no changes snapshots os.environ and restores it on
        # stop, so removing TESTING here cannot leak into any other test.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TESTING", None)

    # Every page a back link can land on: four plain View handlers, then the
    # three served through SessionRequiredMixin.
    PLAIN_PAGES = (
        "categorize_review",
        "find_next_steps",
        "generate_appeal",
        "escalation_packet",
    )
    MIXIN_PAGES = ("eev", "dvc", "hh")
    ALL_PAGES = PLAIN_PAGES + MIXIN_PAGES

    def test_the_session_gate_this_used_to_hide_behind_is_off(self):
        """Without this, everything below passes for the wrong reason."""
        self.assertFalse(views.session_gate_enforced())
        self.assertNotIn("TESTING", os.environ)

    def test_a_page_with_no_reference_at_all_still_renders(self):
        """Second proof the gate is off, from the outside.

        With the gate on, a mixin page reached with nothing in the session
        redirects to ``process``. Production lets it render. That is what
        makes the redirects below attributable to the back link failing and
        to nothing else.
        """
        response = self.client.get(reverse("dvc"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "plan_documents.html")

    def test_a_reference_from_another_device_is_refused_on_every_page(self):
        """Phone to laptop, or a link the person texted themselves."""
        token = self.issue_token()
        other_device = Client()
        for url_name in self.ALL_PAGES:
            with self.subTest(page=url_name):
                response = other_device.get(self.ref_url(url_name, token))
                self.assertLandsOnUploadPageWithHelp(response)

    def test_an_expired_reference_is_refused_on_every_page(self):
        for url_name in self.ALL_PAGES:
            with self.subTest(page=url_name):
                token = self.expired_token()
                response = self.client.get(self.ref_url(url_name, token))
                self.assertLandsOnUploadPageWithHelp(response)

    def test_a_tampered_reference_is_refused_on_every_page(self):
        token = self.issue_token()
        for url_name in self.ALL_PAGES:
            with self.subTest(page=url_name):
                response = self.client.get(self.ref_url(url_name, token + "x"))
                self.assertLandsOnUploadPageWithHelp(response)

    def test_an_empty_reference_is_refused_on_every_page(self):
        """``?ref=`` is a back link that arrived mangled, not one nobody sent.

        Whether a link was followed was read off the truthiness of the value,
        so an empty one counted as "nothing followed", walked past the refusal
        and left the mixin pages serving the blank production 200 this branch
        exists to stop.
        """
        for url_name in self.ALL_PAGES:
            with self.subTest(page=url_name):
                response = self.client.get(
                    f"{reverse(url_name)}?{views.DENIAL_REF_QUERY_PARAM}="
                )
                self.assertLandsOnUploadPageWithHelp(response)

    @override_settings(LEGACY_DENIAL_REF_QUERY=False)
    def test_a_closed_window_old_link_is_refused_on_every_page(self):
        for url_name in self.ALL_PAGES:
            with self.subTest(page=url_name):
                response = self.client.get(self.legacy_url(url_name))
                self.assertLandsOnUploadPageWithHelp(response)

    def test_the_refused_patient_is_told_what_happened(self):
        """The redirect is only half of it; the landing page is the other."""
        token = self.issue_token()
        other_device = Client()
        response = other_device.get(self.ref_url("eev", token))
        self.assertLandsOnUploadPageWithHelp(response)

        landed = other_device.get(response.url)
        self.assertEqual(landed.status_code, 200)
        self.assertTemplateUsed(landed, "scrub.html")
        body = landed.content.decode()
        self.assertIn('id="resume-help"', body)
        self.assertIn("did not open your appeal", body)
        self.assertIn("same browser you started in", body)

    def test_nobody_ever_sees_the_blank_form_this_branch_exists_to_kill(self):
        """The old production failure, asserted as absent.

        A 200 carrying the step's form with an empty denial_id is the exact
        shape of "retype everything and lose it": there is nothing to submit
        and nothing said.
        """
        token = self.issue_token()
        other_device = Client()
        for url_name in self.MIXIN_PAGES:
            with self.subTest(page=url_name):
                response = other_device.get(self.ref_url(url_name, token))
                self.assertNotEqual(
                    response.status_code,
                    200,
                    msg=f"{url_name} served a page to a reference it could not open",
                )

    def test_a_reference_that_does_resolve_still_works(self):
        """The refusal must not have been bought by refusing everybody."""
        token = self.issue_token()
        for url_name in self.MIXIN_PAGES:
            with self.subTest(page=url_name):
                response = self.client.get(self.ref_url(url_name, token))
                self.assertEqual(response.status_code, 200)
                self.assertReferenceFormPopulated(response)

    def test_a_bare_case_id_keeps_the_behaviour_it_has_always_had(self):
        """Production never honoured a bare case id, and still does not.

        ``SessionRequiredMixin.dispatch`` seeds the session from a bare
        ``denial_id`` only inside the gate, so in production that branch has
        never run: the page renders with nothing filled in, the same before
        this branch as after. It carries no email and no secret, so it was
        never a back link, and it must not be turned into one -- telling
        somebody their link failed when they followed no link is its own
        small cruelty.
        """
        response = self.client.get(
            f"{reverse('dvc')}?denial_id={self.denial.denial_id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "plan_documents.html")
        self.assertNotIn('id="resume-help"', response.content.decode())


class CrossDeviceResumeTest(BackLinkReferenceTestBase):
    """The cost of a session-scoped reference, and what the person is told.

    Losing a back link that works on a second device is the point of the
    scheme and a real behaviour change that lands on somebody mid-appeal, so
    the failure has to say what happened and offer a way back in rather than
    dropping them on a blank upload page.
    """

    def upload_page_after(self, response, client):
        """Follow the refusal and return the page the person actually sees."""
        self.assertEqual(response.status_code, 302)
        landed = client.get(response.url)
        self.assertEqual(landed.status_code, 200)
        self.assertTemplateUsed(landed, "scrub.html")
        return landed

    def assertExplainsAndOffersAWayBack(self, page):
        body = page.content.decode()
        self.assertIn(
            'id="resume-help"',
            body,
            msg="the upload page said nothing about the link that failed",
        )
        self.assertIn("did not open your appeal", body)
        self.assertIn("same browser you started in", body)
        self.assertIn("mailto:support42@fighthealthinsurance.com", body)
        self.assertIn(reverse("contact"), body)

    def test_a_back_link_opened_on_another_device_explains_itself(self):
        """Phone to laptop, or a link someone texts themselves."""
        token = self.issue_token()
        other_device = Client()

        response = other_device.get(self.ref_url("generate_appeal", token))
        self.assertLandsOnUploadPageWithHelp(response)
        self.assertExplainsAndOffersAWayBack(
            self.upload_page_after(response, other_device)
        )

    def test_the_second_device_is_told_the_same_thing_on_the_mixin_pages(self):
        """These used to render a blank step with no word about why.

        ``SessionRequiredMixin`` fed the form from the query string and
        rendered whatever came back, so an unresolvable reference produced a
        page with an empty denial_id: nothing to submit, nothing explained.
        """
        token = self.issue_token()
        other_device = Client()

        for url_name in ("eev", "dvc", "hh"):
            with self.subTest(page=url_name):
                response = other_device.get(self.ref_url(url_name, token))
                self.assertLandsOnUploadPageWithHelp(response)
                self.assertExplainsAndOffersAWayBack(
                    self.upload_page_after(response, other_device)
                )

    def test_an_expired_link_in_the_same_browser_explains_itself_too(self):
        """Same person, same browser, back the next day.

        And the copy has to hold for them. "Go back to the browser you started
        in and your appeal will be where you left it" is true for a second
        device and false here: this person is already in that browser, and
        reopening the link does nothing.
        """
        token = self.expired_token()

        response = self.client.get(self.ref_url("categorize_review", token))
        self.assertLandsOnUploadPageWithHelp(response)
        page = self.upload_page_after(response, self.client)
        self.assertExplainsAndOffersAWayBack(page)
        self.assertIn(
            "opening it again will not bring it back",
            page.content.decode(),
            msg=(
                "the page promises the original browser restores the appeal, "
                "which is not true for a link that has gone stale in it"
            ),
        )

    def test_the_explanation_carries_nothing_about_the_case(self):
        """The page that explains the failure must not itself leak.

        It is reached by a URL anyone could construct, in a browser that has
        no claim on the case, so it gets the case's own marker and nothing
        else.
        """
        token = self.issue_token()
        other_device = Client()

        response = other_device.get(self.ref_url("escalation_packet", token))
        # The marker is the only thing the redirect carries. It is not the
        # opaque reference, so assertUrlCarriesNoCredential's one-parameter
        # rule does not apply; check the credentials directly instead.
        self.assertNotIn("@", response.url)
        self.assertNotIn(EMAIL, response.url)
        self.assertNotIn(self.denial.semi_sekret, response.url)
        self.assertNotIn("denial_id", response.url)
        page = self.upload_page_after(response, other_device)
        body = page.content.decode()
        for secret in (
            EMAIL,
            self.denial.semi_sekret,
            models.Denial.get_hashed_email(EMAIL),
        ):
            self.assertNotIn(secret, body, msg=f"{secret!r} on the upload page")
        self.assertIsNone(
            other_device.session.get(views._DENIAL_REF_KEY_SESSION_KEY),
            msg="the second device was handed a way to read references",
        )

    def test_the_lifetime_the_page_quotes_comes_from_the_code(self):
        """The page must not drift away from the reference it describes."""
        token = self.issue_token()
        other_device = Client()
        response = other_device.get(self.ref_url("find_next_steps", token))
        body = self.upload_page_after(response, other_device).content.decode()
        self.assertIn(
            f"about {views.DENIAL_REF_IDLE_TTL_SECONDS // 3600} hours "
            "after you last used it",
            body,
            msg="the page quotes a lifetime that is not the one the code keeps",
        )

    def test_someone_who_followed_no_link_is_not_told_their_link_failed(self):
        """A plain visit to a flow page is not a failed back link."""
        response = self.client.get(reverse("generate_appeal"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("scan"),
            msg="an unaddressed request was treated as a broken back link",
        )
        landed = self.client.get(response.url)
        self.assertEqual(landed.status_code, 200)
        self.assertNotIn('id="resume-help"', landed.content.decode())

    def test_the_upload_page_is_quiet_for_everybody_else(self):
        response = self.client.get(reverse("scan"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="resume-help"', response.content.decode())

    def test_a_bare_case_id_is_not_treated_as_a_failed_back_link(self):
        """The mixin's own session seed keeps the behaviour it has always had.

        ``tests/sync/test_insecure_routing.py`` pins that a bare denial_id in
        the query string seeds the session and renders the page. It carries
        no email and no secret, so it was never a back link, and turning it
        into one would put an explanation in front of people who did not
        follow anything.
        """
        response = self.client.get(
            f"{reverse('dvc')}?denial_id={self.denial.denial_id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "plan_documents.html")


class RetentionClaimTest(TestCase):
    """The retention sentence an owner signs off on, pinned to the repo.

    An earlier draft told the owner the stored copy of the triple lived twelve
    hours. It does not. ``DENIAL_REF_IDLE_TTL_SECONDS`` decides only whether a
    reference still resolves; the plaintext email and the permanent
    ``semi_sekret`` sit in ``django_session``, whose row lifetime comes from
    ``SESSION_COOKIE_AGE``, whose storage comes from ``SESSION_ENGINE`` and
    whose deletion comes from a purge nobody has written.

    That sentence is prose, in ``docs/back-link-references.md`` and in
    ``views.issue_denial_ref_token``, and prose rots quietly. So fail here and
    name the files to fix.

    What this class does and does not establish, because a test believed to
    cover more than it does is worse than no test:

    - The settings assertions read the ``Prod`` configuration class as well as
      the running one. The active settings object under tox is whatever
      ``TestSync`` resolved to, and a reviewer once set a six hour cookie age
      on ``class Prod(Base)`` with every assertion here still passing.
      django-configurations copies Django's global defaults into every
      configuration class body, so "is it set" cannot be asked of the class;
      it is asked of the settings source instead.
    - One test drives the store rather than reading a setting, because the
      bullet about Django not deleting expired rows is a claim about
      behaviour.
    - The purge search finds a ``clearsessions`` invocation or a direct delete
      of ``Session`` rows. It cannot rule out a purge spelled some third way,
      or one living outside this repo. Tripwire, not proof.
    """

    # Django's own defaults, from django/conf/global_settings.py. Spelled out
    # rather than imported because the assertion is about the numbers the
    # retention paragraph quotes, not about whatever Django says today: if a
    # default moves, the paragraph needs rewriting and this should say so.
    DJANGO_DEFAULT_SESSION_COOKIE_AGE = 60 * 60 * 24 * 7 * 2
    DJANGO_DEFAULT_SESSION_ENGINE = "django.contrib.sessions.backends.db"

    DOC = "docs/back-link-references.md"
    SETTINGS_SOURCE = pathlib.Path("fighthealthinsurance/settings.py")

    # A purge, in the shapes it would actually be written in. The negative
    # lookbehind keeps the other models in this repo whose names end in
    # "Session" out of it.
    PURGE_PATTERNS = (
        r"clearsessions",
        r"(?<![\w.])Session\.objects",
        r"from django\.contrib\.sessions\.models import Session",
        r"""(?i:delete\s+from\s+["'`]?django_session)""",
    )

    @staticmethod
    def prod_configuration():
        """The configuration class production runs (``DJANGO_CONFIGURATION=Prod``)."""
        from fighthealthinsurance import settings as settings_module

        return settings_module.Prod

    def test_the_reference_lifetime_is_not_the_retention_period(self):
        """The two numbers are different, which is the whole point."""
        self.assertLess(
            views.DENIAL_REF_IDLE_TTL_SECONDS,
            self.prod_configuration().SESSION_COOKIE_AGE,
            msg=(
                "in production the stored triple no longer outlives the "
                "reference that points at it; if that stops being true, "
                f"rewrite {self.DOC}"
            ),
        )

    def test_the_settings_module_leaves_both_session_settings_alone(self):
        """ "Nothing sets them" is the claim; this is the only place to ask it.

        ``Prod.SESSION_COOKIE_AGE`` answers "what value applies", never "did we
        set it": django-configurations puts Django's global defaults in every
        configuration class body, so ``vars()`` cannot tell the two apart.
        """
        source = read_repo_text(self.SETTINGS_SOURCE)
        self.assertNotEqual(source, "", msg=f"could not read {self.SETTINGS_SOURCE}")
        for setting in ("SESSION_COOKIE_AGE", "SESSION_ENGINE"):
            with self.subTest(setting=setting):
                assignment = re.search(rf"^\s*{setting}\s*=", source, re.MULTILINE)
                self.assertIsNone(
                    assignment,
                    msg=(
                        f"{self.SETTINGS_SOURCE} now sets {setting}, so the "
                        f"retention paragraph in {self.DOC} and the privacy "
                        "note in views.issue_denial_ref_token are out of date"
                    ),
                )

    def test_production_runs_the_two_week_default_cookie_age(self):
        stale = (
            "is no longer Django's two week default, so the retention "
            f"paragraph in {self.DOC} and the privacy note in "
            "views.issue_denial_ref_token are out of date"
        )
        self.assertEqual(
            self.prod_configuration().SESSION_COOKIE_AGE,
            self.DJANGO_DEFAULT_SESSION_COOKIE_AGE,
            msg=f"Prod.SESSION_COOKIE_AGE {stale}",
        )
        self.assertEqual(
            settings.SESSION_COOKIE_AGE,
            self.DJANGO_DEFAULT_SESSION_COOKIE_AGE,
            msg=f"the running configuration's SESSION_COOKIE_AGE {stale}",
        )

    def test_production_keeps_the_database_session_backend(self):
        """The bullet the whole ``django_session`` paragraph rests on.

        A cache or signed-cookie engine would mean there is no plaintext row
        in ``django_session`` to purge at all.
        """
        stale = (
            "is not Django's database backend, so the paragraph about a "
            f"plaintext row in django_session in {self.DOC} and the privacy "
            "note in views.issue_denial_ref_token describe the wrong store"
        )
        self.assertEqual(
            self.prod_configuration().SESSION_ENGINE,
            self.DJANGO_DEFAULT_SESSION_ENGINE,
            msg=f"Prod.SESSION_ENGINE {stale}",
        )
        self.assertEqual(
            settings.SESSION_ENGINE,
            self.DJANGO_DEFAULT_SESSION_ENGINE,
            msg=f"the running configuration's SESSION_ENGINE {stale}",
        )

    def test_an_expired_session_row_stops_resolving_and_stays_in_the_table(self):
        """The "stops honouring, does not delete" bullet, exercised.

        Everything else here reads a setting. This drives the store: a real
        row, the expiry Django stamps on it, and what survives the clock.
        """
        store = SessionStore()
        store[views._DENIAL_REF_EMAILS_SESSION_KEY] = {"1": "someone@example.com"}
        store.save()
        key = store.session_key

        row = Session.objects.get(session_key=key)
        self.assertAlmostEqual(
            (row.expire_date - timezone.now()).total_seconds(),
            settings.SESSION_COOKIE_AGE,
            delta=300,
            msg=(
                "a session row is not stamped with SESSION_COOKIE_AGE, so the "
                f"retention paragraph in {self.DOC} quotes the wrong lifetime"
            ),
        )

        Session.objects.filter(session_key=key).update(
            expire_date=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(
            SessionStore(session_key=key).load(),
            {},
            msg="an expired session row still resolves",
        )
        self.assertTrue(
            Session.objects.filter(session_key=key).exists(),
            msg=(
                "Django deleted the expired row by itself, so the retention "
                f"paragraph in {self.DOC} and the privacy note in "
                "views.issue_denial_ref_token overstate what is retained"
            ),
        )

    def test_nothing_in_the_repo_deletes_expired_session_rows(self):
        """No ``clearsessions`` and no delete against ``Session``.

        Markdown is excluded because saying that nothing runs a purge is
        exactly what the documentation does, and this file is excluded because
        the test above deletes nothing but does name the model. Any third file
        that matches has to be looked at by a person.
        """
        searched = repo_files()
        # A listing that came back short would make the search below pass for
        # the wrong reason. ci.yml is a committed file in a directory an
        # earlier hand written listing missed entirely.
        self.assertIn(
            pathlib.Path(".github/workflows/ci.yml"),
            searched,
            msg=(
                f"the repo listing came back with {len(searched)} files and "
                "none of them is .github/workflows/ci.yml, so the listing is "
                "not reaching one of the places a scheduled purge would live; "
                "fix the listing before believing the result below"
            ),
        )
        this_file = pathlib.Path(__file__).resolve().relative_to(REPO_ROOT)
        purge = re.compile("|".join(self.PURGE_PATTERNS))
        runs_it = sorted(
            str(path)
            for path in searched
            if path.suffix != ".md"
            and path != this_file
            and purge.search(read_repo_text(path))
        )
        self.assertEqual(
            runs_it,
            [],
            msg=(
                "something may purge expired sessions now, so the retention "
                f"paragraph in {self.DOC} and the privacy note in "
                "views.issue_denial_ref_token understate what is cleaned up: "
                + ", ".join(runs_it)
            ),
        )


class BackLinkCallSiteTest(TestCase):
    """Every caller passes the request, checked by walking the call sites.

    The reference lives in the session, so ``build_back_url`` grew a leading
    ``request`` parameter and every caller here moved with it. A branch
    written against the old signature merges clean: git sees a call added in
    one place and a signature changed in another and has no reason to object.
    The break then surfaces as a TypeError while a patient is loading the
    step. Walking the call sites turns that into a failing build.
    """

    SOURCE = pathlib.Path("fighthealthinsurance/views.py")

    def call_sites(self):
        """(path, call node) for every ``build_back_url(...)`` in the repo."""
        for relative_path in repo_files():
            if relative_path.suffix != ".py":
                continue
            source = read_repo_text(relative_path)
            if "build_back_url" not in source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = getattr(func, "id", None) or getattr(func, "attr", None)
                if called == "build_back_url":
                    yield relative_path, node

    def test_the_definition_still_takes_the_request_first(self):
        tree = ast.parse(read_repo_text(self.SOURCE))
        defs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "build_back_url"
        ]
        self.assertEqual(
            len(defs),
            1,
            msg=f"expected one build_back_url in {self.SOURCE}, found {len(defs)}",
        )
        first = defs[0].args.args[0].arg if defs[0].args.args else None
        self.assertEqual(
            first,
            "request",
            msg=(
                "build_back_url no longer takes the request first, so the "
                "call site check below is pinned to a signature that is gone; "
                f"it takes {first!r}"
            ),
        )

    def test_no_call_site_was_written_against_the_old_signature(self):
        sites = list(self.call_sites())
        self.assertGreater(
            len(sites),
            1,
            msg=(
                f"found {len(sites)} build_back_url call sites, so this walk "
                "is not reaching views.py and a green result means nothing"
            ),
        )
        stale = []
        for relative_path, node in sites:
            if any(keyword.arg == "request" for keyword in node.keywords):
                continue
            first = node.args[0] if node.args else None
            # ``request`` in a view function, ``self.request`` in a CBV.
            if isinstance(first, ast.Name) and first.id == "request":
                continue
            if isinstance(first, ast.Attribute) and first.attr == "request":
                continue
            stale.append(f"{relative_path}:{node.lineno}")
        self.assertEqual(
            stale,
            [],
            msg=(
                "these build_back_url calls do not pass the request, so the "
                "back link cannot reach the session holding the reference and "
                "the call raises at render time: " + ", ".join(stale)
            ),
        )


class StartingOverAfterARefusalTest(TestCase):
    """The refusal page says "you can also start a new one below", so the
    upload form under it must create a new case. The form's session dedupe
    would otherwise reuse the case this browser last worked on and write
    the new letter over it."""

    EMAIL = "starting-over@example.com"

    def _upload(self, client, letter):
        return client.post(
            reverse("process"),
            {
                "email": self.EMAIL,
                "denial_text": letter,
                "pii": "on",
                "tos": "on",
                "privacy": "on",
            },
            follow=True,
        )

    def test_the_new_appeal_offered_after_a_refusal_is_a_new_case(self):
        client = Client()
        first = self._upload(client, "The first denial letter, about an MRI.")
        self.assertEqual(first.status_code, 200)
        original = models.Denial.objects.get(
            hashed_email=models.Denial.get_hashed_email(self.EMAIL)
        )

        refused = client.get(
            f"{reverse('hh')}?{views.DENIAL_REF_QUERY_PARAM}=not-a-reference",
            follow=True,
        )
        self.assertEqual(refused.status_code, 200)

        second = self._upload(client, "A different denial letter, about a CT scan.")
        self.assertEqual(second.status_code, 200)

        rows = models.Denial.objects.filter(
            hashed_email=models.Denial.get_hashed_email(self.EMAIL)
        )
        self.assertEqual(rows.count(), 2, "starting over must not reuse the case")
        original.refresh_from_db()
        self.assertEqual(original.denial_text, "The first denial letter, about an MRI.")

    def test_without_a_refusal_the_dedupe_still_reuses_the_case(self):
        """The dedupe is deliberate for a reload or a double submit; only the
        refusal page turns it off."""
        client = Client()
        self._upload(client, "The first denial letter, about an MRI.")
        self._upload(client, "The first denial letter, about an MRI.")

        self.assertEqual(
            models.Denial.objects.filter(
                hashed_email=models.Denial.get_hashed_email(self.EMAIL)
            ).count(),
            1,
        )
