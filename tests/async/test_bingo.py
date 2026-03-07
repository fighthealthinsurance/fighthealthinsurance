"""Test the Insurance Bullshit Bingo functionality"""

from django.test import TestCase, Client
from django.urls import reverse


class BingoTests(TestCase):
    """Test the Insurance Bullshit Bingo feature."""

    def setUp(self):
        self.client = Client()

    def test_bingo_page_contains_bingo_board(self):
        """Test that the bingo page includes bingo board data."""
        response = self.client.get("/bingo")
        self.assertEqual(response.status_code, 200)

        # Check that bingo_board is in the context
        self.assertIn("bingo_board", response.context)

        # Check that the bingo board is a 5x5 grid
        bingo_board = response.context["bingo_board"]
        self.assertEqual(len(bingo_board), 5, "Bingo board should have 5 rows")
        for row in bingo_board:
            self.assertEqual(len(row), 5, "Each row should have 5 cells")

    def test_bingo_board_contains_free_space(self):
        """Test that the center cell is FREE SPACE."""
        response = self.client.get("/bingo")
        bingo_board = response.context["bingo_board"]

        # Center cell (row 2, col 2) should be FREE SPACE
        self.assertEqual(
            bingo_board[2][2], "FREE SPACE", "Center cell should be 'FREE SPACE'"
        )

    def test_bingo_board_has_unique_phrases(self):
        """Test that bingo board phrases are unique (except FREE SPACE)."""
        response = self.client.get("/bingo")
        bingo_board = response.context["bingo_board"]

        # Flatten the board and collect all phrases
        all_phrases = []
        for row in bingo_board:
            all_phrases.extend(row)

        # Remove FREE SPACE for uniqueness check
        phrases_without_free = [p for p in all_phrases if p != "FREE SPACE"]

        # All phrases should be unique
        self.assertEqual(
            len(phrases_without_free),
            len(set(phrases_without_free)),
            "All bingo phrases should be unique",
        )

    def test_bingo_board_changes_on_reload(self):
        """Test that bingo board is randomized on each page load."""
        boards = []
        for _ in range(5):
            response = self.client.get("/bingo")
            board = response.context["bingo_board"]
            # Flatten excluding FREE SPACE
            phrases = []
            for i in range(5):
                for j in range(5):
                    if not (i == 2 and j == 2):
                        phrases.append(board[i][j])
            boards.append(tuple(phrases))

        # With 36 phrases choosing 24, the probability of all 5 boards
        # being identical is astronomically low (~1/1.25 billion per pair)
        unique_boards = set(boards)
        self.assertGreater(
            len(unique_boards),
            1,
            "Expected at least 2 different bingo boards out of 5 loads, "
            "but all were identical -- randomization may be broken",
        )

    def test_bingo_template_renders(self):
        """Test that the bingo board HTML is rendered."""
        response = self.client.get("/bingo")
        content = response.content.decode("utf-8")

        # Check for bingo section elements
        self.assertIn("Insurance Bullshit Bingo", content)
        self.assertIn("bingo-board", content)
        self.assertIn("FREE SPACE", content)
        self.assertIn("Refresh the page for a new board", content)

    def test_bingo_phrases_are_valid(self):
        """Test that all bingo phrases are from the predefined list."""
        from fighthealthinsurance.views import BINGO_PHRASES

        response = self.client.get("/bingo")
        bingo_board = response.context["bingo_board"]

        # Collect all phrases except FREE SPACE
        for row in bingo_board:
            for phrase in row:
                if phrase != "FREE SPACE":
                    self.assertIn(
                        phrase,
                        BINGO_PHRASES,
                        f"Phrase '{phrase}' should be from BINGO_PHRASES list",
                    )

    def test_bingo_phrases_list_is_sufficient(self):
        """Test that there are enough phrases for a 5x5 board."""
        from fighthealthinsurance.views import BINGO_PHRASES

        # We need at least 24 phrases (25 cells - 1 FREE SPACE)
        self.assertGreaterEqual(
            len(BINGO_PHRASES), 24, "BINGO_PHRASES should have at least 24 phrases"
        )

    def test_other_resources_links_to_bingo(self):
        """Test that the other resources page links to the bingo page."""
        response = self.client.get("/other-resources")
        content = response.content.decode("utf-8")

        # Check for link to bingo page
        self.assertIn("Insurance Bullshit Bingo", content)
        self.assertIn('href="/bingo"', content)
