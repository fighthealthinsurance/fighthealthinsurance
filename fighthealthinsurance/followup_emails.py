import asyncio
import datetime
import random
from collections import defaultdict
from typing import Any, Optional

from asgiref.sync import sync_to_async
from django.db.utils import NotSupportedError, ProgrammingError
from django.urls import reverse
from django.utils import timezone

from loguru import logger

from fighthealthinsurance.models import FollowUpSched, InterestedProfessional
from fighthealthinsurance.utils import mask_email_for_logging, send_fallback_email


class AsyncEmailSenderMixin:
    """Mixin providing async wrappers for email sender classes.

    Requires the subclass to implement find_candidates() and dosend().
    """

    async def afind_candidates(self) -> list[Any]:
        return await sync_to_async(self.find_candidates)()  # type: ignore[attr-defined, no-any-return]

    async def adosend(self, **kwargs: Any) -> bool:
        return await sync_to_async(self.dosend)(**kwargs)  # type: ignore[attr-defined, no-any-return]

    async def asend_all(
        self, count: Optional[int] = None, candidates: Optional[list] = None
    ) -> int:
        """Async send_all with per-email delay for rate limiting.

        Args:
            count: Maximum number of emails to send.
            candidates: Pre-fetched candidate list. If None, queries DB.
        """
        if candidates is None:
            candidates = await self.afind_candidates()
        if count is not None:
            candidates = candidates[:count]
        sent = 0
        for candidate in candidates:
            result = await self._asend_one(candidate)
            if result:
                sent += 1
                await asyncio.sleep(random.uniform(1.0, 3.0))
        return sent

    async def _asend_one(self, candidate: Any) -> bool:
        """Send to a single candidate. Override in subclass for correct kwarg."""
        raise NotImplementedError


class ThankyouEmailSender(AsyncEmailSenderMixin):
    def _find_candidates(self):
        return InterestedProfessional.objects.filter(thankyou_email_sent=False)

    def find_candidates(
        self,
    ) -> list[InterestedProfessional]:
        candidates = self._find_candidates()
        # Grab the top 100 candidates.
        return list(candidates[0:100])

    def send_all(self, count: Optional[int] = None) -> int:
        candidates = self._find_candidates()
        selected_candidates = candidates
        if count is not None:
            selected_candidates = candidates[:count]
        return len(
            list(map(lambda f: self.dosend(interested_pro=f), selected_candidates))
        )

    def dosend(
        self,
        email: Optional[str] = None,
        interested_pro: Optional[InterestedProfessional] = None,
    ) -> bool:
        if email is not None:
            interested_pro = InterestedProfessional.objects.filter(email=email)[0]
        if interested_pro is None:
            return False
        email = interested_pro.email
        context = {
            "name": interested_pro.name,
        }

        try:
            send_fallback_email(
                template_name="professional_thankyou",
                subject="Thank you for signing up for Fight Health Insurance Pro Beta!",
                context=context,
                to_email=email,
            )
            interested_pro.thankyou_email_sent = True
            interested_pro.save()
            return True
        except Exception as e:
            logger.warning(
                f"Failed to send thank you email to {mask_email_for_logging(email)}: {e}"
            )
            return False

    async def _asend_one(self, candidate: Any) -> bool:
        return await self.adosend(interested_pro=candidate)


class FollowUpEmailSender(AsyncEmailSenderMixin):
    def _find_due(self):
        """Base query: all unsent follow-ups that are due and not too old."""
        six_months_ago = datetime.date.today() - datetime.timedelta(days=183)
        return (
            FollowUpSched.objects.filter(follow_up_sent=False)
            .filter(follow_up_date__lte=datetime.date.today())
            .filter(initial__gte=six_months_ago)
            .select_related("follow_up_type")
            .order_by("follow_up_date")
        )

    def _find_candidates(self):
        base_qs = self._find_due()
        try:
            candidates = base_qs.order_by(
                "email", "follow_up_type", "follow_up_date"
            ).distinct("email", "follow_up_type")
            candidates.exists()
            return candidates
        except (NotSupportedError, ProgrammingError):
            return base_qs

    def find_candidates(self):
        return list(self._find_candidates())

    def find_all_due(self) -> list[FollowUpSched]:
        """Return ALL due follow-ups without DISTINCT ON dedup.

        send_all/asend_all need the complete set so that suppressed
        rows across multiple denials for the same email/type are all
        marked as sent.
        """
        return list(self._find_due())

    def _group_candidates_by_email(
        self, candidates: list[FollowUpSched]
    ) -> list[tuple[FollowUpSched, list[FollowUpSched]]]:
        """Group candidates by email, picking the best one to send per email.

        Returns a list of (best_candidate, other_candidates) tuples.
        The "best" candidate is the one with the longest follow_up_type.duration
        (e.g., 90-day > 30-day > 7-day), since it represents the most
        significant check-in milestone.
        """
        groups: dict[str, list[FollowUpSched]] = defaultdict(list)
        for candidate in candidates:
            groups[candidate.email].append(candidate)

        result = []
        for group in groups.values():
            group.sort(
                key=lambda s: (
                    s.follow_up_type.duration
                    if s.follow_up_type and s.follow_up_type.duration
                    else datetime.timedelta(0)
                ),
                reverse=True,
            )
            best = group[0]
            others = group[1:]
            result.append((best, others))
        return result

    def _is_stale(self, follow_up_sched: FollowUpSched) -> bool:
        """Check if a follow-up is stale (a later type for same denial already sent)."""
        if follow_up_sched.follow_up_type and follow_up_sched.follow_up_type.duration:
            return FollowUpSched.objects.filter(
                denial_id=follow_up_sched.denial_id,
                follow_up_sent=True,
                follow_up_type__duration__gt=follow_up_sched.follow_up_type.duration,
            ).exists()
        return False

    def _mark_as_sent_without_sending(
        self, follow_up_scheds: list[FollowUpSched]
    ) -> None:
        """Mark follow-up schedules as sent without actually sending email."""
        if not follow_up_scheds:
            return
        now = timezone.now()
        pks = [s.pk for s in follow_up_scheds]
        FollowUpSched.objects.filter(pk__in=pks).update(
            follow_up_sent=True,
            follow_up_sent_date=now,
        )

    def _send_grouped(self, all_candidates: list[FollowUpSched]) -> bool:
        """Send one email for a group of candidates sharing the same address.

        Iterates candidates in priority order (longest duration first).
        Skips stale candidates, sends the first non-stale one, and marks
        the rest as sent without emailing. Returns True if an email was sent.
        """
        to_suppress: list[FollowUpSched] = []
        email_sent = False
        for candidate in all_candidates:
            if email_sent:
                to_suppress.append(candidate)
                continue
            if self._is_stale(candidate):
                to_suppress.append(candidate)
                continue
            result = self.dosend(follow_up_sched=candidate, _skip_stale_check=True)
            if result:
                email_sent = True
            else:
                break
        if to_suppress:
            if email_sent:
                logger.info(
                    f"Suppressed {len(to_suppress)} follow-up(s) for "
                    f"{mask_email_for_logging(all_candidates[0].email)}"
                )
            self._mark_as_sent_without_sending(to_suppress)
        return email_sent

    def group_due_followups(
        self, candidates: Optional[list[FollowUpSched]] = None
    ) -> list[tuple[FollowUpSched, list[FollowUpSched]]]:
        """Group due follow-ups by email, sorted by priority (longest duration first).

        Public API for both send_all and the management command.
        """
        if candidates is None:
            candidates = self.find_all_due()
        return self._group_candidates_by_email(candidates)

    def preview_grouped_send(
        self, all_candidates: list[FollowUpSched]
    ) -> tuple[Optional[FollowUpSched], int]:
        """Preview which candidate would be sent for a group.

        Returns (candidate_to_send, suppressed_count).
        candidate_to_send is None if all candidates are stale.
        """
        to_send = next(
            (c for c in all_candidates if not self._is_stale(c)),
            None,
        )
        suppressed = len(all_candidates) - (1 if to_send else 0)
        return to_send, suppressed

    def send_all(
        self,
        count: Optional[int] = None,
        candidates: Optional[list[FollowUpSched]] = None,
    ) -> int:
        if candidates is None:
            candidates = self.find_all_due()
        grouped = self._group_candidates_by_email(candidates)
        if count is not None:
            grouped = grouped[:count]
        sent = 0
        for best, others in grouped:
            if self._send_grouped([best] + others):
                sent += 1
        return sent

    def dosend(
        self,
        follow_up_sched: Optional[FollowUpSched] = None,
        email: Optional[str] = None,
        _skip_stale_check: bool = False,
    ) -> bool:
        if follow_up_sched is None and email is not None:
            follow_up_sched = FollowUpSched.objects.filter(email=email).filter(
                follow_up_sent=False
            )[0]
        elif follow_up_sched is None and email is None:
            raise Exception("One of email and follow_up_sched must be set.")
        assert follow_up_sched is not None

        if not _skip_stale_check and self._is_stale(follow_up_sched):
            follow_up_sched.follow_up_sent = True
            follow_up_sched.follow_up_sent_date = timezone.now()
            follow_up_sched.save()
            return True

        # Use the email from follow_up_sched to ensure consistency
        email = follow_up_sched.email
        denial = follow_up_sched.denial_id
        selected_appeal = denial.chose_appeal()
        context = {
            "selected_appeal": selected_appeal,
            "followup_link": "https://www.fighthealthinsurance.com"
            + reverse(
                "followup",
                kwargs={
                    "uuid": denial.uuid,
                    "hashed_email": denial.hashed_email,
                    "follow_up_semi_sekret": denial.follow_up_semi_sekret,
                },
            ),
        }

        # Use type-specific template and subject when available,
        # fall back to generic for legacy records without a type.
        if (
            follow_up_sched.follow_up_type
            and follow_up_sched.follow_up_type.template_name
        ):
            template_name = follow_up_sched.follow_up_type.template_name
            subject = follow_up_sched.follow_up_type.subject
        else:
            template_name = "followup"
            subject = "Following up from Fight Health Insurance"

        try:
            send_fallback_email(
                template_name=template_name,
                subject=subject,
                context=context,
                to_email=email,
            )
            follow_up_sched.follow_up_sent = True
            follow_up_sched.follow_up_sent_date = timezone.now()
            follow_up_sched.save()
            return True
        except Exception as e:
            logger.warning(
                f"Failed to send follow-up email to {mask_email_for_logging(email)}: {e}"
            )
            return False

    async def asend_all(
        self,
        count: Optional[int] = None,
        candidates: Optional[list[FollowUpSched]] = None,
    ) -> int:
        """Async send_all with per-email grouping and rate limiting.

        Groups candidates by email address so each recipient gets at most
        one follow-up email per batch. Iterates candidates in priority order,
        skips stale ones, sends the first valid one, and marks the rest as sent.
        """
        if candidates is None:
            candidates = await sync_to_async(self.find_all_due)()
        grouped = await sync_to_async(self._group_candidates_by_email)(candidates)
        if count is not None:
            grouped = grouped[:count]
        sent = 0
        for best, others in grouped:
            result = await sync_to_async(self._send_grouped)([best] + others)
            if result:
                sent += 1
                await asyncio.sleep(random.uniform(1.0, 3.0))
        return sent

    async def _asend_one(self, candidate: Any) -> bool:
        return await self.adosend(follow_up_sched=candidate)
