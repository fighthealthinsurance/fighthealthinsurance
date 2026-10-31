"""Staff see how the plan document uploads are going on one page.

The plan documents dashboard (staff_views.PlanDocumentsDashboardView,
behind the staff login) shows how close the share page is to its goal of
employer plans and at what pace (shared_plan_documents.goal_pace), the
shares each week and where they came from, how far the shared files'
reading is, today's budgets, the reminders waiting to go, and the uploads
that weren't shared: the appeal step's and Explain Policy's. The staff hub
and the admin's list of shared documents both link to it.
"""

import datetime
import uuid

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import shared_plan_documents
from fighthealthinsurance.models import (
    Denial,
    PlanDocuments,
    PlanDocumentsReminder,
    PolicyDocument,
    SharedPlanDocumentsDay,
)
from fighthealthinsurance.shared_plan_documents import (
    GOAL_DATE,
    GOAL_PLANS,
    PACE_DAYS,
    Share,
    goal_pace,
    link_fingerprint,
)

User = get_user_model()

LINK = "https://benefits.crumpler-clinic.example.org/2027/plan-documents"


def _employer_share(at: datetime.datetime) -> Share:
    return Share(
        shared_at=at,
        new_plan=True,
        files=1,
        links=0,
        plan_category="employer_erisa",
        plan_year=None,
        premium_answered=False,
        employer=True,
    )


class TheGoalsPaceTest(TestCase):
    """goal_pace alone, with a fixed day."""

    def setUp(self):
        # Ten weeks before the goal's day.
        self.today = GOAL_DATE - datetime.timedelta(days=70)
        self.noon = datetime.datetime.combine(
            self.today, datetime.time(12), tzinfo=timezone.get_current_timezone()
        )

    def recent(self, count, days_ago=1):
        at = self.noon - datetime.timedelta(days=days_ago)
        return [_employer_share(at) for _ in range(count)]

    def test_what_is_left_over_the_weeks_left_is_the_pace_needed(self):
        pace = goal_pace(self.today, 50, self.recent(20))
        self.assertEqual((pace["days_left"], pace["remaining"]), (70, 50))
        self.assertEqual(pace["needed_per_week"], 5.0)
        # 20 in 28 days is 5 a week; ten weeks of that reaches 100.
        self.assertEqual(pace["recent_per_week"], 5.0)
        self.assertEqual(pace["projected"], 100)
        self.assertTrue(pace["on_track"])

    def test_behind_when_the_recent_pace_falls_short(self):
        pace = goal_pace(self.today, 50, self.recent(10))
        self.assertEqual(pace["recent_per_week"], 2.5)
        self.assertEqual(pace["projected"], 75)
        self.assertFalse(pace["on_track"])

    def test_only_the_last_days_count_toward_the_pace(self):
        pace = goal_pace(self.today, 50, self.recent(20, days_ago=PACE_DAYS + 1))
        self.assertEqual((pace["recent"], pace["recent_per_week"]), (0, 0.0))

    def test_a_repeat_or_another_kind_of_plan_is_not_pace(self):
        shares = [
            _employer_share(self.noon)._replace(new_plan=False),
            _employer_share(self.noon)._replace(employer=False),
        ]
        self.assertEqual(goal_pace(self.today, 50, shares)["recent"], 0)

    def test_reached(self):
        pace = goal_pace(self.today, GOAL_PLANS + 3, [])
        self.assertTrue(pace["reached"])
        self.assertEqual((pace["remaining"], pace["needed_per_week"]), (0, 0.0))
        self.assertEqual(pace["percent"], 100)

    def test_past_the_date_nothing_more_is_asked(self):
        pace = goal_pace(GOAL_DATE + datetime.timedelta(days=1), 40, self.recent(20))
        self.assertTrue(pace["past"])
        self.assertEqual((pace["days_left"], pace["needed_per_week"]), (0, 0.0))
        self.assertEqual(pace["projected"], 40)
        self.assertFalse(pace["on_track"])


class _DashboardTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            User.objects.create_user(
                username="rebecca_lee_crumpler", password="pw-for-tests", is_staff=True
            )
        )
        self.today = timezone.localdate()
        monday = self.today - datetime.timedelta(days=self.today.weekday())
        self.this_week = datetime.datetime.combine(
            monday, datetime.time(12), tzinfo=timezone.get_current_timezone()
        )
        self.last_week = self.this_week - datetime.timedelta(days=7)
        self.minutes = 0

    def share(
        self, files=(), links=(), at=None, source=PolicyDocument.SOURCE_SHARED, **facts
    ):
        """One share's rows, made directly: each file by its fingerprint."""
        self.minutes += 1
        when = at or self.this_week + datetime.timedelta(minutes=self.minutes)
        batch = uuid.uuid4()
        status = facts.pop("processing_status", PolicyDocument.PROCESSING_RECEIVED)
        rows = []
        for digest in files:
            rows.append(
                PolicyDocument.objects.create(
                    source=source,
                    share_batch=batch,
                    shared_at=when,
                    fingerprint=digest,
                    processing_status=status,
                    **facts,
                )
            )
        for link in links:
            rows.append(
                PolicyDocument.objects.create(
                    source=PolicyDocument.SOURCE_SHARED_LINK,
                    share_batch=batch,
                    shared_at=when,
                    shared_link=link,
                    shared_link_fingerprint=link_fingerprint(link),
                    processing_status=PolicyDocument.PROCESSING_LINK,
                    **facts,
                )
            )
        return rows

    def page(self):
        response = self.client.get(reverse("plan_documents_dashboard"))
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.content, "html.parser")

    def rows(self, page, table_id):
        table = page.find(id=table_id)
        return [
            [cell.get_text(strip=True) for cell in row.find_all(["th", "td"])]
            for row in table.find("tbody").find_all("tr")
        ]

    def text(self, page, element_id):
        return page.find(id=element_id).get_text(" ", strip=True)


class OnlyStaffSeeItTest(TestCase):
    def test_a_visitor_is_sent_to_log_in(self):
        response = Client().get(reverse("plan_documents_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_a_person_who_is_not_staff_is_sent_to_log_in(self):
        client = Client()
        client.force_login(
            User.objects.create_user(username="patient", password="pw-for-tests")
        )
        response = client.get(reverse("plan_documents_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])


class TheGoalIsShownTest(_DashboardTestCase):
    def test_the_count_the_goal_and_its_pace(self):
        for digest in ("a", "b", "c"):
            self.share(files=[digest], plan_category="employer_erisa")
        self.share(files=["d"], plan_category="medicaid_chip")
        page = self.page()
        line = self.text(page, "goal-line")
        self.assertTrue(
            line.startswith(
                f"3 of {GOAL_PLANS} employer plans by {GOAL_DATE:%B} {GOAL_DATE.day}, {GOAL_DATE.year}"
            ),
            line,
        )
        expected = goal_pace(self.today, 3, shared_plan_documents.plan_shares())
        self.assertEqual(
            self.text(page, "goal-days-left").split()[0], str(expected["days_left"])
        )
        self.assertEqual(
            self.text(page, "goal-remaining").split()[0], str(expected["remaining"])
        )
        self.assertEqual(
            self.text(page, "goal-needed-per-week").split()[0],
            str(expected["needed_per_week"]),
        )
        self.assertEqual(
            self.text(page, "goal-recent-per-week").split()[0],
            str(expected["recent_per_week"]),
        )
        self.assertEqual(
            self.text(page, "goal-projected").split()[0], str(expected["projected"])
        )
        self.assertEqual(
            self.text(page, "goal-pace"),
            "On track" if expected["on_track"] else "Behind",
        )

    def test_the_bar_fills_toward_the_goal(self):
        for n in range(25):
            self.share(files=[f"plan-{n}"], plan_category="employer_non_erisa")
        bar = self.page().find(id="goal").find("i")
        self.assertEqual(bar["style"], "width: 25%")

    def test_with_nothing_shared_the_page_still_renders(self):
        page = self.page()
        self.assertTrue(self.text(page, "goal-line").startswith(f"0 of {GOAL_PLANS}"))
        self.assertEqual(self.rows(page, "weeks"), [["No shares yet."]])


class SharesAreCountedTest(_DashboardTestCase):
    def test_each_week_counts_shares_plans_employer_plans_files_and_links(self):
        self.share(files=["sbc"], at=self.last_week, plan_category="employer_erisa")
        self.share(
            files=["sbc", "spd"], at=self.this_week, plan_category="employer_erisa"
        )
        self.share(
            files=["other"],
            links=[LINK],
            at=self.this_week,
            plan_category="medicaid_chip",
        )
        weeks = self.rows(self.page(), "weeks")
        # This week: two shares, both new plans (the one that repeats "sbc"
        # brings "spd"), of which one is an employer plan; three files, one
        # link.
        self.assertEqual(weeks[0][1:], ["2", "2", "1", "3", "1"])
        self.assertEqual(weeks[1][1:], ["1", "1", "1", "1", "0"])

    def test_shares_are_counted_by_where_they_came_from(self):
        self.share(files=["a", "b"])
        self.share(files=["c"], source=PolicyDocument.SOURCE_SHARED_EXPLAIN)
        self.share(files=["d"], source=PolicyDocument.SOURCE_SHARED_APPEAL)
        self.share(files=["e"], source=PolicyDocument.SOURCE_SHARED_APPEAL)
        self.share(links=[LINK])
        self.assertEqual(
            self.rows(self.page(), "sources"),
            [
                ["Shared for research", "1", "2"],
                ["Shared for research from Explain my policy", "1", "1"],
                ["Shared for research from an appeal", "2", "2"],
                ["Link shared for research", "1", "1"],
            ],
        )

    def test_the_totals(self):
        self.share(files=["a"], plan_category="employer_erisa", premium_amount=250)
        self.share(files=["a"])
        self.share(files=["b"])
        page = self.page()
        self.assertEqual(self.text(page, "shares-total").split()[0], "3")
        self.assertEqual(self.text(page, "plans-total").split()[0], "2")
        self.assertEqual(self.text(page, "employer-plans-total").split()[0], "1")
        self.assertEqual(self.text(page, "premium-answered").split()[0], "1")


class TheReadingIsCountedTest(_DashboardTestCase):
    def test_shared_files_by_where_their_reading_is(self):
        self.share(files=["a", "b"])
        self.share(files=["c"], processing_status=PolicyDocument.PROCESSING_READY)
        self.share(files=["d"], processing_status=PolicyDocument.PROCESSING_UNREADABLE)
        self.share(files=["e"], processing_status=PolicyDocument.PROCESSING_REMOVED)
        self.share(links=[LINK])
        self.assertEqual(
            self.rows(self.page(), "reading-status"),
            [
                ["Received, not read yet", "2"],
                ["Read and cleaned, ready for review", "1"],
                ["No text could be read", "1"],
                ["File and text deleted at the person's request", "1"],
            ],
        )


class TodayIsShownTest(_DashboardTestCase):
    def test_todays_budgets_show_what_was_used_of_each_limit(self):
        day = timezone.now().date()
        SharedPlanDocumentsDay.objects.create(
            day=day, name=SharedPlanDocumentsDay.FILES, count=7
        )
        SharedPlanDocumentsDay.objects.create(
            day=day, name=SharedPlanDocumentsDay.LINKS, count=2
        )
        # Yesterday's count is not today's.
        SharedPlanDocumentsDay.objects.create(
            day=day - datetime.timedelta(days=1),
            name=SharedPlanDocumentsDay.OPT_IN_FILES,
            count=9,
        )
        self.assertEqual(
            self.rows(self.page(), "budgets"),
            [
                [
                    "Files shared on the share page",
                    "7",
                    str(shared_plan_documents._files_per_day()),
                ],
                [
                    "Copies shared by ticking the box on another page",
                    "0",
                    str(shared_plan_documents._opt_in_files_per_day()),
                ],
                ["Links shared", "2", str(shared_plan_documents._links_per_day())],
            ],
        )

    def test_reminders_waiting_are_counted(self):
        PlanDocumentsReminder.objects.create(
            email="rebecca@example.org",
            send_after=timezone.now() + datetime.timedelta(days=30),
        )
        self.assertTrue(
            self.text(self.page(), "reminders-waiting").startswith("1 reminder waiting")
        )


class UploadsThatWerentSharedAreCountedTest(_DashboardTestCase):
    def test_appeal_step_files_with_their_cases_by_week(self):
        first = Denial.objects.create(
            denial_text="Denied.", semi_sekret="a", hashed_email="hash-a"
        )
        second = Denial.objects.create(
            denial_text="Denied.", semi_sekret="b", hashed_email="hash-b"
        )
        PlanDocuments.objects.create(denial=first)
        PlanDocuments.objects.create(denial=first)
        PlanDocuments.objects.create(denial=second)
        page = self.page()
        self.assertEqual(self.text(page, "appeal-files").split()[0], "3")
        self.assertEqual(self.text(page, "appeal-cases").split()[0], "2")
        weeks = self.rows(page, "appeal-weeks")
        self.assertEqual(weeks[0][1], "3")
        self.assertEqual(
            weeks[0][0],
            f"{self.this_week:%b} {self.this_week.day}, {self.this_week.year}",
        )

    def test_explain_policy_files_are_counted_apart_from_shares(self):
        PolicyDocument.objects.create(source=PolicyDocument.SOURCE_EXPLAIN_POLICY)
        self.share(files=["a"])
        page = self.page()
        self.assertEqual(self.text(page, "explain-files").split()[0], "1")
        self.assertEqual(self.rows(page, "explain-weeks")[0][1], "1")

    def test_with_none_the_tables_say_so(self):
        page = self.page()
        self.assertEqual(self.rows(page, "appeal-weeks"), [["No files yet."]])
        self.assertEqual(self.rows(page, "explain-weeks"), [["No files yet."]])


class ItIsLinkedFromWhereStaffLookTest(_DashboardTestCase):
    def test_the_staff_hub_links_to_it(self):
        page = BeautifulSoup(
            self.client.get(reverse("staff_dashboard")).content, "html.parser"
        )
        link = page.find("a", href=reverse("plan_documents_dashboard"))
        self.assertIsNotNone(link)
        self.assertEqual(
            link.find("strong").get_text(strip=True), "Plan Documents Dashboard"
        )
        self.assertIn("100 employer plans", link.get_text(" ", strip=True))

    def test_the_admins_list_of_shared_documents_links_to_it(self):
        admin = User.objects.create_superuser(username="admin", password="pw-for-tests")
        self.client.force_login(admin)
        page = BeautifulSoup(
            self.client.get(
                reverse("admin:fighthealthinsurance_policydocument_changelist")
            ).content,
            "html.parser",
        )
        link = page.find(id="shared-plan-dashboard-link").find("a")
        self.assertEqual(link["href"], reverse("plan_documents_dashboard"))

    def test_the_page_links_back_to_the_hub_the_admin_and_the_share_page(self):
        page = self.page()
        hrefs = {a["href"] for a in page.find_all("a")}
        for name in ("staff_dashboard", "share_plan_documents"):
            self.assertIn(reverse(name), hrefs)
        self.assertIn(
            reverse("admin:fighthealthinsurance_policydocument_changelist"), hrefs
        )
