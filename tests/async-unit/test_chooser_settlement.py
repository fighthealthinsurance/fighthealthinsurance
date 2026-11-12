"""The Arena's settling rules (chooser_settlement.verdict): when a case's
votes show a clear winner, or show it is clearly undecided, so it stops
being served."""

from django.test import SimpleTestCase, override_settings

from fighthealthinsurance.chooser_settlement import (
    CLEAR_WINNER,
    NO_USABLE_DRAFT,
    UNDECIDED,
    Review,
    head_to_head,
    sign_test_p,
    verdict,
)

A, B, C, D = 1, 2, 3, 4
DRAFTS = [A, B, C, D]


def pick(*winners, shown=DRAFTS):
    """A review won by ``winners`` (a tie when more than one)."""
    return Review.from_vote(
        list(shown), winners[0], list(winners) if len(winners) > 1 else [], False
    )


def all_bad(shown=DRAFTS):
    return Review.from_vote(list(shown), None, [], True)


def picks(*counts):
    """Reviews giving A, B, C, D the given numbers of outright wins."""
    reviews = []
    for draft, count in zip(DRAFTS, counts):
        reviews += [pick(draft) for _ in range(count)]
    return reviews


SETTINGS = dict(
    CHOOSER_SETTLE_MIN_REVIEWS=5,
    CHOOSER_UNDECIDED_REVIEWS=10,
    CHOOSER_UNDECIDED_MAX_REVIEWS=20,
    CHOOSER_UNDECIDED_SKIP_SHARE=0.5,
)


@override_settings(**SETTINGS)
class SignTestTest(SimpleTestCase):
    def test_five_nil_is_significant(self):
        self.assertAlmostEqual(sign_test_p(5, 0), 1 / 32)

    def test_four_one_is_not(self):
        self.assertAlmostEqual(sign_test_p(4, 1), 6 / 32)

    def test_no_reviews_is_no_evidence(self):
        self.assertEqual(sign_test_p(0, 0), 1.0)


@override_settings(**SETTINGS)
class HeadToHeadTest(SimpleTestCase):
    def test_a_tie_between_the_two_is_no_evidence(self):
        self.assertEqual(head_to_head([pick(A, B)], A, B), (0, 0))

    def test_a_vote_for_a_third_draft_is_no_evidence(self):
        self.assertEqual(head_to_head([pick(C)], A, B), (0, 0))

    def test_a_review_that_did_not_show_both_is_left_out(self):
        self.assertEqual(head_to_head([pick(A, shown=[A, C])], A, B), (0, 0))

    def test_a_tie_with_a_third_still_beats_a_loser(self):
        self.assertEqual(head_to_head([pick(A, C)], A, B), (1, 0))


@override_settings(**SETTINGS)
class ClearWinnerTest(SimpleTestCase):
    def test_a_unanimous_five_is_a_clear_winner(self):
        found = verdict(picks(5), 0, DRAFTS)
        self.assertEqual(found.reason, CLEAR_WINNER)

    def test_four_unanimous_is_too_few_reviews(self):
        self.assertIsNone(verdict(picks(4), 0, DRAFTS))

    def test_a_close_runner_up_keeps_the_case_open(self):
        self.assertIsNone(verdict(picks(4, 2), 0, DRAFTS))

    def test_seven_to_one_is_a_clear_winner(self):
        self.assertEqual(verdict(picks(7, 1), 0, DRAFTS).reason, CLEAR_WINNER)

    def test_agreeing_nothing_is_usable_is_a_clear_answer(self):
        found = verdict([all_bad() for _ in range(5)], 0, DRAFTS)
        self.assertEqual(found.reason, CLEAR_WINNER)
        self.assertIn("no usable draft", found.detail)

    def test_all_bad_reviews_count_against_the_leader(self):
        reviews = picks(5) + [all_bad() for _ in range(4)]
        self.assertIsNone(verdict(reviews, 0, DRAFTS))

    def test_a_clear_winner_wins_over_heavy_skipping(self):
        self.assertEqual(verdict(picks(5), 6, DRAFTS).reason, CLEAR_WINNER)

    def test_a_draft_never_compared_blocks_a_clear_winner(self):
        # D was never shown, so nothing says A beats it.
        reviews = [pick(A, shown=[A, B, C]) for _ in range(6)]
        self.assertIsNone(verdict(reviews, 0, DRAFTS))


@override_settings(**SETTINGS)
class UndecidedTest(SimpleTestCase):
    def test_mostly_skipped_is_undecided(self):
        found = verdict(picks(2), 3, DRAFTS)
        self.assertEqual(found.reason, UNDECIDED)

    def test_skips_below_the_share_keep_the_case_open(self):
        self.assertIsNone(verdict(picks(2, 1), 2, DRAFTS))

    def test_skips_need_enough_judges(self):
        self.assertIsNone(verdict([], 4, DRAFTS))

    def test_an_even_spread_is_undecided(self):
        found = verdict(picks(3, 3, 2, 2), 0, DRAFTS)
        self.assertEqual(found.reason, UNDECIDED)

    def test_an_even_spread_needs_enough_reviews(self):
        self.assertIsNone(verdict(picks(3, 3, 2, 1), 0, DRAFTS))

    def test_a_trending_leader_keeps_the_case_open(self):
        # 6-3 against the runner-up: not clear yet, not even either.
        self.assertIsNone(verdict(picks(6, 3, 1), 0, DRAFTS))

    def test_the_top_two_always_tied_is_undecided(self):
        reviews = [pick(A, B) for _ in range(10)]
        self.assertEqual(verdict(reviews, 0, DRAFTS).reason, UNDECIDED)

    def test_no_clear_winner_by_the_cap_is_undecided(self):
        found = verdict(picks(11, 7, 1, 1), 0, DRAFTS)
        self.assertEqual(found.reason, UNDECIDED)

    @override_settings(CHOOSER_UNDECIDED_MAX_REVIEWS=40)
    def test_the_cap_is_a_setting(self):
        self.assertIsNone(verdict(picks(11, 7, 1, 1), 0, DRAFTS))


@override_settings(**SETTINGS)
class ReviewFromVoteTest(SimpleTestCase):
    def test_an_all_bad_vote_credits_no_usable_draft(self):
        self.assertEqual(all_bad().shares, {NO_USABLE_DRAFT: 1.0})

    def test_a_tie_splits_the_point(self):
        self.assertEqual(pick(A, B).shares, {A: 0.5, B: 0.5})

    def test_malformed_presented_ids_are_ignored(self):
        review = Review.from_vote(["junk", A, None], A, [], False)
        self.assertEqual(review.presented, frozenset({A, NO_USABLE_DRAFT}))
