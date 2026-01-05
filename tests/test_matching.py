import unittest

from mcp_code_review.app_server import is_concurrency_error, matches_thread_turn


class TestMatching(unittest.TestCase):
    def test_matches_thread_turn_defaults(self):
        self.assertTrue(matches_thread_turn(None, "thread", "turn"))

    def test_matches_thread_turn_exact(self):
        params = {"threadId": "thread", "turnId": "turn"}
        self.assertTrue(matches_thread_turn(params, "thread", "turn"))
        self.assertFalse(matches_thread_turn(params, "other", "turn"))
        self.assertFalse(matches_thread_turn(params, "thread", "other"))

    def test_matches_thread_turn_missing_fields(self):
        params = {"threadId": "thread"}
        self.assertTrue(matches_thread_turn(params, "thread", "turn"))
        params = {"turnId": "turn"}
        self.assertTrue(matches_thread_turn(params, "thread", "turn"))

    def test_is_concurrency_error(self):
        self.assertTrue(is_concurrency_error("turn already in progress"))
        self.assertTrue(is_concurrency_error("Only one active turn"))
        self.assertTrue(is_concurrency_error("busy"))
        self.assertFalse(is_concurrency_error("authentication failed"))


if __name__ == "__main__":
    unittest.main()
