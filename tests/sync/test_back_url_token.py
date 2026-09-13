"""Back links carry an opaque reference, not the case's credentials.

``build_back_url`` used to urlencode (denial_id, email, semi_sekret) into the
query string of every back link from step 3 onward. From there the triple
lands in browser history on a shared device, in every reverse proxy access
log, and in anything the person screenshots or pastes when asking a friend
for help -- and the triple is the whole credential on the case, because
``sensitive_post_parameters`` covers POST bodies only and
``SessionRequiredMixin`` enforces a session only under DEBUG or TESTING.

The link now carries one random string. The case reference it stands for is
kept server side against the session, so the string is worth nothing on its
own, worth nothing in someone else's session, and worth nothing once it has
expired. These tests pin all three, pin that every consumer resolves it
(including the pages reached through ``SessionRequiredMixin``, which the
three named GET handlers do not cover), and pin that the transition window
for the old triple can actually be closed.

Tying the reference to the session costs something, and it costs the patient,
so the cost is spelled out here rather than left between the lines. The old
triple worked in any browser: someone could start an appeal on their phone
and open the same link on a laptop, or text it to themselves. A reference
that resolves only in its own session ends that, and ``CrossDeviceResumeTest``
is the acceptance for what happens instead. Every way a back link can fail to
open a case -- a second device, a link someone sent themselves, a reference
that has expired, an old link after the transition window closes -- lands on
the upload page with that page told to say what happened, what it means for
their appeal, and how to get back in. Somebody who followed no link at all is
told nothing, because they have nothing to explain.

Two of these classes exist because a test can pass and still be telling you
about the wrong site:

``ProductionShapedRefusalTest`` strips the session gate the way ``Prod`` does
(DEBUG off, TESTING deleted from the environment) before asserting any of it.
Under tox that gate is on, and while the refusal sat behind it these same
assertions went green over a production that served the patient a blank form
instead.

``SlidingLifetimeTest`` covers the other half of "the failure has to be
honest", which is not failing at people who did nothing wrong. The twelve
hours run from the last use, so a person working an appeal all day keeps
their reference; only one nobody has touched for twelve hours goes stale.
"""

import base64
import json
import os
import pathlib
import re
import time
import types
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import common_view_logic, models, views


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
        """The idle expiry the session is holding for one reference."""
        client = client or self.client
        return client.session[views._DENIAL_REF_SESSION_KEY][token]["exp"]

    def set_expiry(self, token: str, exp: float, client=None):
        """Move a reference's expiry, the way the clock would."""
        client = client or self.client
        session = client.session
        refs = session[views._DENIAL_REF_SESSION_KEY]
        refs[token]["exp"] = exp
        session[views._DENIAL_REF_SESSION_KEY] = refs
        session.save()

    def expire(self, token: str, client=None):
        """Age a reference past its idle window."""
        self.set_expiry(token, time.time() - 1, client=client)

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
        self.assertNotIn(
            self.denial.semi_sekret, url, msg=f"case secret in URL: {url}"
        )
        self.assertNotIn(
            models.Denial.get_hashed_email(EMAIL),
            url,
            msg=f"hashed email in URL: {url}",
        )
        self.assertNotIn("semi_sekret", query, msg=f"secret named in URL: {url}")
        self.assertNotIn("email", query, msg=f"email named in URL: {url}")
        self.assertNotIn("denial_id", query, msg=f"case id named in URL: {url}")
        if query:
            # One parameter, and it is the opaque reference. Anything else in
            # the address bar is something this change exists to remove.
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
        matching = [h for h in flow_hrefs(response) if h.split("?")[0] == back_path]
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
        # The acceptance criterion: no email address, no hashed email and no
        # semi_sekret. The denial_id is deliberately not in this list -- it is
        # a short integer, so looking for it inside a random string is a coin
        # flip, not a test. That the id is not in the address bar is asserted
        # by the walk, which pins the query string down to one ref parameter.
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

    def test_the_same_case_reuses_one_reference(self):
        """A walk back and forth must not fill the session with references."""
        self.assertEqual(self.issue_token(), self.issue_token())


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
        token = self.issue_token()
        self.expire(token)

        response = self.client.get(self.ref_url("generate_appeal", token))
        self.assertLandsOnUploadPageWithHelp(response)

    def test_an_expired_reference_is_refused_by_the_session_mixin_pages_too(self):
        token = self.issue_token()
        self.expire(token)

        response = self.client.get(self.ref_url("eev", token))
        self.assertLandsOnUploadPageWithHelp(response)

    def test_an_expired_reference_is_dropped_from_the_session(self):
        token = self.issue_token()
        self.expire(token)

        self.client.get(self.ref_url("generate_appeal", token))
        self.assertNotIn(token, self.client.session[views._DENIAL_REF_SESSION_KEY])


class SlidingLifetimeTest(BackLinkReferenceTestBase):
    """The twelve hours run from the last use, not from the first issue.

    An expiry stamped once at first issue is a cliff in the middle of an
    active appeal: somebody who uploads a denial in the morning, works at it
    through the day and comes back to it at ten at night is thrown out to the
    upload page while still using the site. These pin the idle window
    instead, and pin that it is still a window: a reference nobody touches
    for the full lifetime does go stale.
    """

    def assertExpiryPushedOut(self, token, before, client=None):
        after = self.stored_expiry(token, client=client)
        self.assertGreater(
            after,
            before,
            msg="using the reference did not push its expiry out",
        )
        self.assertAlmostEqual(
            after - time.time(),
            views.DENIAL_REF_IDLE_TTL_SECONDS,
            delta=30,
            msg="the reference did not get a fresh full idle window",
        )

    def test_following_a_back_link_pushes_the_expiry_out(self):
        token = self.issue_token()
        # Eleven and a half hours in: the old code would have let this die
        # half an hour later however hard the person was working.
        nearly_up = time.time() + 30 * 60
        self.set_expiry(token, nearly_up)

        response = self.client.get(self.ref_url("generate_appeal", token))
        self.assertEqual(response.status_code, 200)
        self.assertExpiryPushedOut(token, nearly_up)

    def test_the_mixin_pages_push_the_expiry_out_too(self):
        """Health history, plan documents and extraction, same rule."""
        for url_name in ("eev", "dvc", "hh"):
            with self.subTest(page=url_name):
                token = self.issue_token()
                nearly_up = time.time() + 30 * 60
                self.set_expiry(token, nearly_up)

                response = self.client.get(self.ref_url(url_name, token))
                self.assertEqual(response.status_code, 200)
                self.assertExpiryPushedOut(token, nearly_up)

    def test_rendering_a_link_to_the_case_counts_as_using_it(self):
        """The reference stays alive while the flow keeps linking to the case.

        Forward navigation POSTs, so a long sitting may never follow a back
        link at all. The page still renders one, and that is the person
        still working, so it keeps the reference alive.
        """
        token = self.issue_token()
        nearly_up = time.time() + 30 * 60
        self.set_expiry(token, nearly_up)

        response = self.client.post(
            reverse("escalation_packet"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": self.denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertExpiryPushedOut(token, nearly_up)

    def test_a_day_of_work_never_hits_the_cliff(self):
        """The whole point, walked out: use it, wait, use it, wait, use it.

        Each wait is longer than half the idle window, so an expiry stamped
        at first issue would have run out partway through.
        """
        token = self.issue_token()
        elapsed = 0.0
        for _ in range(4):
            step = views.DENIAL_REF_IDLE_TTL_SECONDS * 0.75
            elapsed += step
            # Wind the stored expiry back by the time that has "passed"
            # rather than sleeping for nine hours.
            self.set_expiry(token, self.stored_expiry(token) - step)
            response = self.client.get(self.ref_url("categorize_review", token))
            self.assertEqual(
                response.status_code,
                200,
                msg=f"thrown out after {elapsed / 3600:.0f} hours of use",
            )

    def test_a_reference_nobody_touches_still_goes_stale(self):
        """Sliding is not immortal, or the window would mean nothing."""
        token = self.issue_token()
        self.set_expiry(token, time.time() - 1)

        response = self.client.get(self.ref_url("generate_appeal", token))
        self.assertLandsOnUploadPageWithHelp(response)
        self.assertNotIn(token, self.client.session[views._DENIAL_REF_SESSION_KEY])


class ReferenceStoreTest(BackLinkReferenceTestBase):
    """The session store holds what it says it holds."""

    def issue_for_new_case(self, session, index: int) -> str:
        denial = models.Denial.objects.create(
            denial_text=f"Denial {index}.",
            hashed_email=models.Denial.get_hashed_email(EMAIL),
            semi_sekret=f"secret-{index}",
        )
        token = views.issue_denial_ref_token(
            types.SimpleNamespace(session=session),
            denial.denial_id,
            EMAIL,
            denial.semi_sekret,
        )
        assert token is not None
        return token

    def test_the_store_never_goes_over_its_stated_cap(self):
        """The cap is the cap, not the cap plus the one being added.

        Every entry is a plaintext email and a permanent case secret, so
        "briefly one over" is a real extra copy, and a constant that does not
        mean what it says is worse than a different number.
        """
        session = self.client.session
        for index in range(views.DENIAL_REF_MAX_PER_SESSION + 5):
            self.issue_for_new_case(session, index)
            self.assertLessEqual(
                len(session[views._DENIAL_REF_SESSION_KEY]),
                views.DENIAL_REF_MAX_PER_SESSION,
                msg=f"store held more than {views.DENIAL_REF_MAX_PER_SESSION}",
            )

    def test_a_session_holding_junk_under_the_key_does_not_500(self):
        """Nothing writes that key but this module, so this is belt and braces.

        The resolver already refused a non-dict; issuing walked straight into
        it. Both sides guard now, because a page render is not the place to
        find out.
        """
        session = self.client.session
        session[views._DENIAL_REF_SESSION_KEY] = ["not", "a", "dict"]
        session.save()

        response = self.client.get(
            f"{reverse('generate_appeal')}?{views.DENIAL_REF_QUERY_PARAM}=whatever"
        )
        self.assertLandsOnUploadPageWithHelp(response)

        token = views.issue_denial_ref_token(
            types.SimpleNamespace(session=self.client.session),
            self.denial.denial_id,
            EMAIL,
            self.denial.semi_sekret,
        )
        self.assertIsNotNone(token, msg="issuing died on a junk session value")


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

    ``SessionRequiredMixin`` has always had a session gate that is switched
    off in production on purpose, and for a while the refusal of an
    unresolvable back link sat behind it. Under tox that gate is on
    (``TESTING=True``), so a test could assert a redirect from health
    history, plan documents or extraction and pass, while production served
    those same requests a 200 with an empty form and not a word about why:
    the patient on the second device retyped their procedure and diagnosis,
    submitted, and only then got bounced to the upload page with what they
    had typed gone.

    So this class removes the gate the way ``Prod`` does -- ``DEBUG = False``
    plus ``pre_setup`` deleting ``TESTING`` from the environment -- and makes
    the same assertions again. The first test proves the removal took, and
    the rest would go green for the wrong reason without it.
    """

    def setUp(self):
        super().setUp()
        # patch.dict with no changes snapshots os.environ and restores it on
        # stop, so removing TESTING here cannot leak into any other test.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TESTING", None)

    # Every page a back link can land on: the four plain View handlers, then
    # the three served through SessionRequiredMixin, which are the ones that
    # used to fail silently.
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
                token = self.issue_token()
                self.expire(token)
                response = self.client.get(self.ref_url(url_name, token))
                self.assertLandsOnUploadPageWithHelp(response)

    def test_a_tampered_reference_is_refused_on_every_page(self):
        token = self.issue_token()
        for url_name in self.ALL_PAGES:
            with self.subTest(page=url_name):
                response = self.client.get(self.ref_url(url_name, token + "x"))
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

    The old query triple worked in any browser, so a patient could start on
    their phone and finish on a laptop, or text themselves the link. A
    reference that resolves only in the session that issued it takes that
    away. That is the point of it (a link in someone's history stops being a
    key to their medical case) but it is a real behaviour change and it lands
    on the patient, so the failure has to say what happened and offer a way
    back in rather than dropping them on a blank upload page.
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
        # Says WHY, in the terms the person can act on: the browser they
        # started in, and the fact that the link goes stale.
        self.assertIn("same browser you started in", body)
        # Offers a way back in rather than only an apology.
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
        """Same person, same browser, back the next day."""
        token = self.issue_token()
        session = self.client.session
        refs = session[views._DENIAL_REF_SESSION_KEY]
        refs[token]["exp"] = time.time() - 1
        session[views._DENIAL_REF_SESSION_KEY] = refs
        session.save()

        response = self.client.get(self.ref_url("categorize_review", token))
        self.assertLandsOnUploadPageWithHelp(response)
        self.assertExplainsAndOffersAWayBack(
            self.upload_page_after(response, self.client)
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
            other_device.session.get(views._DENIAL_REF_SESSION_KEY),
            msg="the second device was handed a reference it should not have",
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

    An earlier draft of this change told the owner the stored copy of the
    triple lived twelve hours. It does not. Twelve hours
    (``DENIAL_REF_IDLE_TTL_SECONDS``) decides only whether a reference still
    resolves; the plaintext email and the permanent ``semi_sekret`` sit in
    ``django_session``, whose row lifetime comes from ``SESSION_COOKIE_AGE``
    and whose deletion comes from ``manage.py clearsessions``. This repo sets
    neither, so the honest sentence is "until the session row is purged, and
    nothing purges it".

    That sentence is prose, in ``docs/back-link-references.md`` and in
    ``views.issue_denial_ref_token``, and prose rots quietly. If somebody
    later sets a cookie age or wires up a purge, the sentence becomes wrong
    while still reading fine, and the next owner signs off on a stale claim.
    So fail here and name the files to fix.
    """

    # Django's own default, from django/conf/global_settings.py. Spelled out
    # rather than imported so that this test keeps meaning something if the
    # default ever moves.
    DJANGO_DEFAULT_SESSION_COOKIE_AGE = 60 * 60 * 24 * 7 * 2

    DOC = "docs/back-link-references.md"

    def test_the_reference_lifetime_is_not_the_retention_period(self):
        """The two numbers are different, which is the whole point."""
        self.assertLess(
            views.DENIAL_REF_IDLE_TTL_SECONDS,
            settings.SESSION_COOKIE_AGE,
            msg=(
                "the stored triple outlives the reference that points at it; "
                f"if that stops being true, rewrite {self.DOC}"
            ),
        )

    def test_the_repo_still_sets_no_session_cookie_age(self):
        self.assertEqual(
            settings.SESSION_COOKIE_AGE,
            self.DJANGO_DEFAULT_SESSION_COOKIE_AGE,
            msg=(
                "SESSION_COOKIE_AGE is no longer Django's two week default, so "
                f"the retention paragraph in {self.DOC} and the privacy note in "
                "views.issue_denial_ref_token are out of date"
            ),
        )

    def test_nothing_in_the_repo_purges_expired_sessions(self):
        """No ``clearsessions`` anywhere, so an abandoned row stays.

        Searched over the places a scheduled purge could live: the k8s
        manifests, the helm charts, the scripts directory and the Makefile.
        Documentation is excluded, because saying that nothing runs it is
        exactly what the documentation does.
        """
        root = pathlib.Path(__file__).resolve().parents[2]
        searched = [
            path
            for directory in ("k8s", "charts", "scripts", "conf")
            for path in (root / directory).rglob("*")
            if path.is_file() and path.suffix != ".md"
        ]
        searched.append(root / "Makefile")
        runs_it = []
        for path in searched:
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if "clearsessions" in text:
                runs_it.append(str(path.relative_to(root)))
        self.assertEqual(
            runs_it,
            [],
            msg=(
                "something purges expired sessions now, so the retention "
                f"paragraph in {self.DOC} and the privacy note in "
                "views.issue_denial_ref_token understate what is cleaned up: "
                + ", ".join(runs_it)
            ),
        )
