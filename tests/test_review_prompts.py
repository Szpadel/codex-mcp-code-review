import unittest

from mcp_code_review.review_prompts import (
    append_additional_review_instructions,
    build_uncommitted_changes_review_prompt,
)


class TestReviewPrompts(unittest.TestCase):
    def test_build_uncommitted_changes_review_prompt(self):
        self.assertEqual(
            build_uncommitted_changes_review_prompt(),
            "Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings.",
        )

    def test_append_additional_review_instructions_trims_extra_text(self):
        prompt = append_additional_review_instructions(
            build_uncommitted_changes_review_prompt(),
            "  Check generated files carefully.  ",
        )
        self.assertEqual(
            prompt,
            "Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings.\n\nAdditional review instructions:\nCheck generated files carefully.",
        )


if __name__ == "__main__":
    unittest.main()
