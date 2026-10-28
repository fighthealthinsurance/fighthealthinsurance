"""The share page opens on why now, the goal, the documents that help most,
and the numbers behind the ask.

In open enrollment season an eyebrow says next year's plan documents are
out. The goal line says 100 employer plans by January 15, and shows a
running count of distinct employer plans only from 20, counted the same for
every visitor and at most ten minutes old; after January 15 it goes. Any
kind of plan is welcome, and the page says so under the goal. Three cards
in a row name the documents for a plan through work in the order we want
them, and a fold says what to send for other kinds of plans. Under "Why we're asking", two tiles: the share of
people covered only through a job, from Denial Watch's bundle, and that
Denial Watch has no denial rates for those plans. A card says what happens
to what people share, in the agreement's terms and no more.
"""

import datetime
import json
import uuid
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.shared_plan_documents import (
    GOAL_DATE,
    PLAN_COUNT_SECONDS,
    employer_plan_count,
)

ROOT = Path(__file__).resolve().parents[2] / "fighthealthinsurance"
IN_SEASON = datetime.date(2026, 11, 2)
OUT_OF_SEASON = datetime.date(2026, 9, 1)
EYEBROW = "Open enrollment: next year's plan documents are out now"
LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "share-page-goal",
    }
}
DASHES = ("\u2014", "\u2013")


def seed_plans(count, start=0, **facts):
    """``count`` shares of one different file each, employer plans unless
    ``facts`` say otherwise."""
    facts = {"plan_category": "employer_erisa", **facts}
    for n in range(start, start + count):
        PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_SHARED,
            share_batch=uuid.uuid4(),
            fingerprint=f"plan-{n}",
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            **facts,
        )


class _PageTestCase(TestCase):
    def page(self, day=IN_SEASON, client=None, path=None):
        with mock.patch("django.utils.timezone.localdate", return_value=day):
            response = (client or self.client).get(path or reverse("share_plan_documents"))
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.content, "html.parser")


class TheTitleSaysWhyNowTest(_PageTestCase):
    def test_in_season_the_eyebrow_says_next_years_documents_are_out(self):
        eyebrow = self.page(IN_SEASON).find(class_="fhi-page-eyebrow")
        self.assertEqual(eyebrow.get_text(strip=True), EYEBROW)

    def test_out_of_season_there_is_no_eyebrow(self):
        self.assertIsNone(self.page(OUT_OF_SEASON).find(class_="fhi-page-eyebrow"))

    def test_the_title_and_lede(self):
        soup = self.page()
        self.assertEqual(soup.find("h1").get_text(strip=True), "Share your plan documents")
        self.assertEqual(
            soup.find(class_="fhi-page-lede").get_text(strip=True),
            "What is health insurance through work going to cost next year? Help "
            "us find out by sharing the documents your plan gives everyone.",
        )


class TheGoalTest(_PageTestCase):
    def goal(self, soup):
        return soup.find(id="share-goal")

    def test_the_goal_shows_without_a_count_below_twenty_plans(self):
        seed_plans(19)
        goal = self.goal(self.page())
        self.assertIn(
            "Our goal: 100 employer plans by January 15", goal.get_text(" ", strip=True)
        )
        self.assertIsNone(goal.find(id="share-goal-count"))
        self.assertIsNone(goal.find(class_="share-goal-meter"))

    def test_from_twenty_plans_the_count_and_its_bar_show(self):
        seed_plans(20)
        goal = self.goal(self.page())
        self.assertEqual(goal.find(id="share-goal-count").get_text(strip=True), "20 so far")
        bar = goal.find(class_="share-goal-meter")
        self.assertEqual(bar["aria-hidden"], "true")
        self.assertEqual(bar.find("i")["style"], "width: 20%")

    def test_the_bar_stops_at_full(self):
        seed_plans(130)
        goal = self.goal(self.page())
        self.assertEqual(goal.find(id="share-goal-count").get_text(strip=True), "130 so far")
        self.assertEqual(goal.find(class_="share-goal-meter").find("i")["style"], "width: 100%")

    def test_the_count_is_plans_not_uploads(self):
        seed_plans(20)
        # A repeat of a file already counted adds no plan.
        PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_SHARED,
            share_batch=uuid.uuid4(),
            fingerprint="plan-0",
            plan_category="employer_erisa",
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
        )
        goal = self.goal(self.page())
        self.assertEqual(goal.find(id="share-goal-count").get_text(strip=True), "20 so far")

    def test_the_count_is_employer_plans_only(self):
        seed_plans(8)
        seed_plans(6, start=8, plan_category="employer_non_erisa")
        seed_plans(6, start=14, plan_category="unknown", employer_name="Crumpler Clinic")
        # Welcome, but not what the goal counts.
        seed_plans(5, start=20, plan_category="aca_marketplace")
        seed_plans(3, start=25, plan_category="medicare_advantage")
        seed_plans(2, start=28, plan_category="unknown")
        goal = self.goal(self.page())
        self.assertEqual(goal.find(id="share-goal-count").get_text(strip=True), "20 so far")

    def test_other_plans_alone_never_show_a_count(self):
        seed_plans(30, plan_category="medicaid_chip")
        self.assertIsNone(self.goal(self.page()).find(id="share-goal-count"))

    def test_under_the_goal_the_page_says_any_plan_is_welcome(self):
        note = self.goal(self.page()).find(id="any-plan")
        self.assertEqual(
            note.get_text(" ", strip=True),
            "Any kind of plan is welcome. What to send",
        )
        self.assertEqual(note.find("a")["href"], "#other-plans")

    def test_the_thank_you_page_links_to_no_card_it_does_not_show(self):
        soup = self.page(path=reverse("share_plan_documents") + "?shared=1")
        self.assertIsNone(soup.find(id="other-plans"))
        self.assertIsNone(soup.find("a", href="#other-plans"))
        self.assertIn("Any kind of plan is welcome.", soup.find(id="any-plan").get_text())

    def test_after_the_goal_the_page_still_says_any_plan_is_welcome(self):
        soup = self.page(GOAL_DATE + datetime.timedelta(days=1))
        self.assertIn("Any kind of plan is welcome.", soup.find(id="any-plan").get_text())

    def test_the_goal_shows_through_its_day_and_then_goes(self):
        self.assertIsNotNone(self.goal(self.page(GOAL_DATE)))
        self.assertIsNone(self.goal(self.page(GOAL_DATE + datetime.timedelta(days=1))))

    def test_the_goal_shows_on_the_thank_you_page_too(self):
        path = reverse("share_plan_documents") + "?shared=1"
        self.assertIsNotNone(self.goal(self.page(path=path)))

    def test_a_count_that_fails_leaves_the_goal_without_one(self):
        seed_plans(20)
        with mock.patch(
            "fighthealthinsurance.shared_plan_documents.plan_shares",
            side_effect=RuntimeError("database went away"),
        ):
            goal = self.goal(self.page())
        self.assertIn("Our goal", goal.get_text())
        self.assertIsNone(goal.find(id="share-goal-count"))


@override_settings(CACHES=LOCMEM_CACHE)
class TheCountCarriesNoVisitorAndStaysFreshTest(_PageTestCase):
    """The page itself is never stored whole: it carries a CSRF token and a
    signed render time. Only the count is kept, for every visitor alike."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def count_on(self, soup):
        return soup.find(id="share-goal-count").get_text(strip=True)

    def test_every_visitor_sees_the_same_count(self):
        seed_plans(20)
        sharer = Client(HTTP_CF_CONNECTING_IP="203.0.113.7")
        session = sharer.session
        session["shared_plan_documents_today"] = {"day": "2026-11-02", "files": 3, "bytes": 10}
        session.save()
        first = self.count_on(self.page(client=sharer))
        second = self.count_on(self.page(client=Client()))
        self.assertEqual((first, second), ("20 so far", "20 so far"))

    def test_the_count_is_at_most_ten_minutes_old(self):
        self.assertLessEqual(PLAN_COUNT_SECONDS, 10 * 60)
        seed_plans(20)
        self.assertEqual(self.count_on(self.page()), "20 so far")
        seed_plans(1, start=20)
        self.assertEqual(self.count_on(self.page()), "20 so far")
        later = datetime.datetime.now().timestamp() + PLAN_COUNT_SECONDS + 1
        with mock.patch("django.core.cache.backends.locmem.time.time", return_value=later):
            self.assertEqual(employer_plan_count(), 21)

    def test_only_a_number_is_kept(self):
        seed_plans(20)
        employer_plan_count()
        self.assertEqual(cache.get("shared-plan-documents-employer-plans"), 20)

    def test_the_page_is_never_stored_whole(self):
        with mock.patch("django.utils.timezone.localdate", return_value=IN_SEASON):
            response = self.client.get(reverse("share_plan_documents"))
        self.assertNotIn("public", response.get("Cache-Control", ""))
        self.assertContains(response, "csrfmiddlewaretoken")


class TheDocumentCardsTest(_PageTestCase):
    def test_three_cards_in_the_order_we_want_them_each_with_a_hidden_icon(self):
        cards = self.page().find(id="most-useful").find_all("li", recursive=False)
        self.assertEqual(
            [card.find(class_="fhi-card-title").get_text(strip=True) for card in cards],
            [
                "Rate sheet or enrollment guide",
                "Summary of Benefits and Coverage",
                "Summary Plan Description or Evidence of Coverage",
            ],
        )
        for card in cards:
            with self.subTest(card=card.get_text(strip=True)[:30]):
                icon = card.find("svg")
                self.assertEqual(icon["aria-hidden"], "true")
                self.assertEqual(icon["focusable"], "false")
        cards_list = self.page().find(id="most-useful")
        self.assertEqual(cards_list.name, "ol")
        # The page kit's card grid: a row where three fit, one column on a phone.
        self.assertIn("fhi-cards", cards_list["class"])


class OtherKindsOfPlansTest(_PageTestCase):
    def test_a_fold_says_what_to_send_for_other_plans(self):
        card = self.page().find(id="other-plans")
        self.assertEqual(card.name, "details")
        self.assertEqual(
            card.find("summary").get_text(strip=True),
            "Other kinds of plans: what to send",
        )
        self.assertEqual(
            [li.get_text(" ", strip=True) for li in card.find_all("li")],
            [
                "Marketplace or individual plans: the Summary of Benefits and "
                "Coverage, and what you pay each month.",
                "Medicare Advantage: the Evidence of Coverage and the Annual "
                "Notice of Change.",
                "Medicaid or CHIP: the member handbook.",
            ],
        )

    def test_it_names_documents_the_how_to_section_already_names(self):
        soup = self.page()
        how_to = soup.find(id="get-your-plan-documents").get_text(" ", strip=True)
        for name in (
            "Summary of Benefits and Coverage",
            "Evidence of Coverage",
            "Annual Notice of Change",
            "member handbook",
        ):
            with self.subTest(name=name):
                self.assertIn(name, how_to)

    def test_it_sits_under_the_cards_before_the_agreement(self):
        fold = self.page().find(id="other-plans")
        self.assertIsNotNone(fold.find_previous(id="most-useful"))
        self.assertIsNotNone(fold.find_next("input", attrs={"name": "agree"}))


class WhyWereAskingTest(_PageTestCase):
    def tiles(self):
        return self.page().find(id="why-numbers")

    def test_the_job_share_tile_reads_denial_watchs_bundle(self):
        bundle = json.loads((ROOT / "static" / "denial_watch.json").read_text())
        mix = bundle["coverage_mix"]
        job = next(row for row in mix["rows"] if row["k"] == "job")
        tile = self.tiles().find(id="why-job-share")
        self.assertEqual(tile.find("b").get_text(strip=True), f"{job['pct']:.1f}%")
        self.assertIn("of people have coverage only through a job", tile.get_text(" ", strip=True))
        self.assertIn("2026", mix["name"])
        self.assertIn("Census Bureau survey, 2026", tile.get_text(" ", strip=True))

    def test_the_second_tile_says_denial_watch_has_no_rates_for_job_plans(self):
        tile = self.tiles().find(id="why-no-rates")
        self.assertEqual(
            tile.get_text(" ", strip=True),
            "None of Denial Watch's denial rates cover plans through work Plan "
            "documents won't show denial rates, but they show the rules behind denials.",
        )

    def test_the_tiles_come_first_under_the_heading(self):
        section = self.page().find(id="about-sharing")
        self.assertEqual(section.find("h2").find_next_sibling()["id"], "why-numbers")

    def test_without_the_bundle_the_tiles_step_aside(self):
        with mock.patch(
            "fighthealthinsurance.views.denial_watch_coverage_mix",
            side_effect=FileNotFoundError("denial_watch.json"),
        ):
            soup = self.page()
        self.assertIsNone(soup.find(id="why-numbers"))
        self.assertIsNotNone(soup.find(id="what-we-have"))


class NoLongDashesTest(_PageTestCase):
    def test_it_has_no_long_dashes(self):
        main = self.page().find("main").get_text()
        for dash in DASHES:
            with self.subTest(dash=dash):
                self.assertNotIn(dash, main)
