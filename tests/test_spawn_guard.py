import unittest
from unittest.mock import patch

from mcp_code_review.server import RecursiveReviewCallError
from mcp_code_review.server import ensure_not_recursive_review_call
from mcp_code_review.spawn_guard import SPAWN_GUARD_CONFIG_ASSIGNMENT
from mcp_code_review.spawn_guard import find_guarded_app_server_in_cmdlines
from mcp_code_review.spawn_guard import is_guarded_app_server_cmdline


class TestSpawnGuard(unittest.TestCase):
    def test_is_guarded_app_server_cmdline_detects_marker(self):
        cmdline = f"codex -c {SPAWN_GUARD_CONFIG_ASSIGNMENT} app-server"
        self.assertTrue(is_guarded_app_server_cmdline(cmdline))

    def test_is_guarded_app_server_cmdline_requires_both_parts(self):
        self.assertFalse(is_guarded_app_server_cmdline("codex app-server"))
        self.assertFalse(is_guarded_app_server_cmdline(SPAWN_GUARD_CONFIG_ASSIGNMENT))

    def test_find_guarded_app_server_in_cmdlines(self):
        cmdlines = [
            "uv run -m mcp_code_review.server",
            "codex -c profile=review app-server",
            f"codex -c {SPAWN_GUARD_CONFIG_ASSIGNMENT} app-server",
        ]
        guarded = find_guarded_app_server_in_cmdlines(cmdlines)
        self.assertEqual(guarded, cmdlines[2])

    def test_ensure_not_recursive_review_call_allows_non_guarded(self):
        with patch(
            "mcp_code_review.server.find_guarded_app_server_ancestor_cmdline",
            return_value=None,
        ):
            ensure_not_recursive_review_call()

    def test_ensure_not_recursive_review_call_blocks_guarded_context(self):
        with patch(
            "mcp_code_review.server.find_guarded_app_server_ancestor_cmdline",
            return_value=f"codex -c {SPAWN_GUARD_CONFIG_ASSIGNMENT} app-server",
        ):
            with self.assertRaises(RecursiveReviewCallError):
                ensure_not_recursive_review_call()


if __name__ == "__main__":
    unittest.main()
