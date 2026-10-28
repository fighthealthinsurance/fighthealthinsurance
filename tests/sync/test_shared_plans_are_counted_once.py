"""Shared plan documents are counted as plans, once each.

Each share (a batch of files and links sent together) is one plan, unless
every file and link in it came in an earlier share: files are matched by
their keyed fingerprint, links by the keyed hash of the link. A link-only
share counts unless every link repeats an earlier one. Rows a removal
request cleared count for nothing. The staff counts on the PolicyDocument
admin list count every kind of plan this way, and call out the employer
plans the share page's goal counts: an employer plan of either kind, or a
plan whose kind wasn't given with an employer named.
"""

import datetime
import uuid
from decimal import Decimal

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.shared_plan_documents import (
    distinct_plan_count,
    employer_plan_count,
    link_fingerprint,
    plan_shares,
    share_counts,
)

User = get_user_model()

LINK = "https://benefits.crumpler-clinic.example.org/2027/plan-documents"
LINK_B = "https://www.crumplermutual.example.com/plans/gold-ppo"
START = datetime.datetime(2026, 10, 20, 15, 0, tzinfo=datetime.timezone.utc)


class _PlansTestCase(TestCase):
    def setUp(self):
        self.minutes = 0

    def share(self, files=(), links=(), at=None, source=PolicyDocument.SOURCE_SHARED, **facts):
        """One share's rows, made directly: each file by its fingerprint."""
        self.minutes += 1
        when = at or START + datetime.timedelta(minutes=self.minutes)
        batch = uuid.uuid4()
        rows = []
        for digest in files:
            rows.append(
                PolicyDocument.objects.create(
                    source=source,
                    share_batch=batch,
                    shared_at=when,
                    fingerprint=digest,
                    processing_status=PolicyDocument.PROCESSING_RECEIVED,
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

    def remove(self, rows):
        # What remove-my-data leaves on a row it keeps for its plan facts.
        PolicyDocument.objects.filter(pk__in=[row.pk for row in rows]).update(
            fingerprint="",
            shared_link="",
            shared_link_fingerprint="",
            processing_status=PolicyDocument.PROCESSING_REMOVED,
        )

    def plans(self):
        return distinct_plan_count()


class EachShareIsOnePlanTest(_PlansTestCase):
    def test_a_share_of_several_files_is_one_plan(self):
        self.share(files=["sbc", "spd", "rates"])
        self.share(files=["other-sbc"])
        self.assertEqual(self.plans(), 2)

    def test_a_share_whose_files_all_came_before_does_not_count_again(self):
        self.share(files=["sbc", "spd"])
        self.share(files=["spd", "sbc"])
        self.share(files=["sbc"])
        self.assertEqual(self.plans(), 1)

    def test_one_new_file_makes_a_share_a_new_plan(self):
        self.share(files=["sbc", "spd"])
        self.share(files=["sbc", "next-years-sbc"])
        self.assertEqual(self.plans(), 2)

    def test_a_copy_shared_from_another_page_counts_the_same_way(self):
        self.share(files=["sbc"], source=PolicyDocument.SOURCE_SHARED_EXPLAIN)
        self.share(files=["sbc"])
        self.share(files=["eoc"], source=PolicyDocument.SOURCE_SHARED_APPEAL)
        self.assertEqual(self.plans(), 2)

    def test_an_upload_that_was_not_shared_is_not_a_plan(self):
        PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY, fingerprint="sbc"
        )
        self.assertEqual(self.plans(), 0)

    def test_the_earlier_share_is_the_one_that_counts(self):
        later = START + datetime.timedelta(days=2)
        self.share(files=["sbc"], at=later, plan_year=2027)
        self.share(files=["sbc"], at=START, plan_year=2026)
        first, second = plan_shares()
        self.assertEqual((first.plan_year, first.new_plan), (2026, True))
        self.assertEqual((second.plan_year, second.new_plan), (2027, False))


class ALinkOnlyShareTest(_PlansTestCase):
    def test_it_counts_unless_every_link_came_before(self):
        self.share(links=[LINK])
        self.assertEqual(self.plans(), 1)
        # The same page typed differently is a repeat.
        self.share(
            links=["HTTPS://Benefits.Crumpler-Clinic.example.org/2027/plan-documents/"]
        )
        self.assertEqual(self.plans(), 1)
        self.share(links=[LINK, LINK_B])
        self.assertEqual(self.plans(), 2)

    def test_links_and_files_are_matched_apart(self):
        self.share(files=["sbc"])
        self.share(links=[LINK])
        self.assertEqual(self.plans(), 2)

    def test_a_mixed_share_counts_if_anything_in_it_is_new(self):
        self.share(files=["sbc"])
        self.share(files=["sbc"], links=[LINK])
        self.share(files=["sbc"], links=[LINK])
        self.assertEqual(self.plans(), 2)


class RemovedRowsCountForNothingTest(_PlansTestCase):
    def test_a_removed_share_is_not_a_plan(self):
        rows = self.share(files=["sbc"], links=[LINK], insurer_name="Crumpler Mutual")
        self.remove(rows)
        self.assertEqual(self.plans(), 0)

    def test_a_share_after_a_removed_one_counts(self):
        self.remove(self.share(files=["sbc"]))
        self.share(files=["sbc"])
        self.assertEqual(self.plans(), 1)

    def test_a_share_with_only_part_removed_is_still_a_plan(self):
        rows = self.share(files=["sbc", "spd"])
        self.remove(rows[:1])
        self.assertEqual(self.plans(), 1)


class TheGoalCountsEmployerPlansTest(_PlansTestCase):
    def test_employer_plans_of_either_kind_and_unsure_ones_with_an_employer(self):
        self.share(files=["a"], plan_category="employer_erisa")
        self.share(files=["b"], plan_category="employer_non_erisa")
        self.share(files=["c"], plan_category="unknown", employer_name="Crumpler Clinic")
        self.share(files=["d"], plan_category="unknown")
        self.share(files=["e"], plan_category="aca_marketplace", employer_name="Crumpler Clinic")
        self.share(files=["f"], plan_category="medicare_advantage")
        self.assertEqual((distinct_plan_count(), employer_plan_count()), (6, 3))

    def test_a_repeat_of_an_employer_plan_is_not_counted_again(self):
        self.share(files=["a"], plan_category="employer_erisa")
        self.share(files=["a"], plan_category="employer_erisa")
        self.share(links=[LINK], plan_category="employer_erisa")
        self.share(links=[LINK], plan_category="employer_erisa")
        self.assertEqual(employer_plan_count(), 2)


class StaffSeeThePlansTest(_PlansTestCase):
    def setUp(self):
        super().setUp()
        admin = User.objects.create_superuser(
            username="rebecca_lee_crumpler", password="pw-for-tests"
        )
        self.client = Client()
        self.client.force_login(admin)
        self.today = timezone.localdate()
        monday = self.today - datetime.timedelta(days=self.today.weekday())
        self.this_week = datetime.datetime.combine(
            monday, datetime.time(12), tzinfo=timezone.get_current_timezone()
        )
        self.last_week = self.this_week - datetime.timedelta(days=7)

    def counts_on_the_page(self):
        response = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_changelist")
        )
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.content, "html.parser").find(
            id="shared-plan-counts"
        )

    def rows(self, section, table_id):
        table = section.find(id=table_id)
        return [
            [cell.get_text(strip=True) for cell in row.find_all(["th", "td"])]
            for row in table.find("tbody").find_all("tr")
        ]

    def test_shares_and_new_plans_each_week(self):
        self.share(files=["sbc"], at=self.last_week)
        self.share(files=["sbc"], at=self.this_week)
        self.share(links=[LINK], at=self.this_week)
        weeks = self.rows(self.counts_on_the_page(), "shared-plan-weeks")
        self.assertEqual([row[1:] for row in weeks], [["2", "1"], ["1", "1"]])
        self.assertEqual(
            weeks[0][0], f"{self.this_week:%b} {self.this_week.day}, {self.this_week.year}"
        )

    def test_plans_by_kind_year_and_premium_answer(self):
        year = self.today.year
        self.share(
            files=["a"],
            plan_category="employer_erisa",
            plan_year=year + 1,
            premium_amount=Decimal("250"),
        )
        self.share(files=["b"], plan_category="employer_erisa", plan_year=year)
        self.share(files=["c"], plan_category="aca_marketplace")
        # A repeat adds a share, not a plan.
        self.share(files=["a"], plan_category="employer_erisa", plan_year=year + 1)
        section = self.counts_on_the_page()
        self.assertIn(
            "3 different plans from 4 shares.",
            section.find(id="shared-plan-total").get_text(" ", strip=True),
        )
        self.assertEqual(
            self.rows(section, "shared-plan-kinds"),
            [
                ["Employer Plan (ERISA)", "2"],
                ["ACA Marketplace (Healthcare.gov / State Exchange)", "1"],
            ],
        )
        self.assertEqual(
            self.rows(section, "shared-plan-years"),
            [[str(year + 1), "1"], [str(year), "1"], ["Other years", "0"], ["Not given", "1"]],
        )
        self.assertIn(
            "1 of 3 plans came with an answer to the premium question.",
            section.find(id="shared-plan-premiums").get_text(" ", strip=True),
        )

    def test_employer_plans_are_called_out(self):
        self.share(files=["a"], plan_category="employer_erisa")
        self.share(files=["b"], plan_category="unknown", employer_name="Crumpler Clinic")
        self.share(files=["c"], plan_category="medicaid_chip")
        text = self.counts_on_the_page().find(id="shared-plan-employer").get_text(
            " ", strip=True
        )
        self.assertTrue(text.startswith("2 employer plans toward the goal of 100 by January 15, 2027"))

    def test_the_counts_match_the_rule(self):
        self.share(files=["a"])
        self.share(files=["a"])
        self.remove(self.share(links=[LINK]))
        counts = share_counts(self.today)
        self.assertEqual((counts["shares"], counts["plans"]), (2, distinct_plan_count()))
        self.assertEqual(counts["employer_plans"], employer_plan_count())

    def test_with_nothing_shared_the_tables_say_so(self):
        section = self.counts_on_the_page()
        self.assertIn("0 different plans from 0 shares.", section.get_text(" ", strip=True))
        self.assertEqual(self.rows(section, "shared-plan-weeks"), [["No shares yet."]])
