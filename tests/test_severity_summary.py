import unittest

from mcp_code_review.review import ReviewResult
from mcp_code_review.server import count_tagged_issue_runs, format_severity_summary


class TestSeveritySummary(unittest.TestCase):
    def test_format_severity_summary_aggregates_counts(self):
        results = [
            ReviewResult(
                verdict="comment",
                summary="first",
                comment_markdown="- [P1] one\n- [P2] two\n- [P2] dup",
            ),
            ReviewResult(
                verdict="comment",
                summary="second",
                comment_markdown="notes [p1] again",
            ),
            ReviewResult(
                verdict="pass",
                summary="clean",
                comment_markdown="- [P0] ignored",
            ),
        ]
        self.assertEqual(format_severity_summary(results), " (P0=1, P1=2, P2=1)")

    def test_format_severity_summary_empty(self):
        results = [ReviewResult(verdict="comment", summary="x", comment_markdown="no tags")]
        self.assertEqual(format_severity_summary(results), "")

    def test_count_tagged_issue_runs_by_tags(self):
        results = [
            ReviewResult(verdict="comment", summary="none", comment_markdown="no tags"),
            ReviewResult(verdict="pass", summary="tagged", comment_markdown="- [P1] found"),
            ReviewResult(verdict="comment", summary="multi", comment_markdown="- [P2]\n- [P3]"),
            ReviewResult(verdict="comment", summary="dup", comment_markdown="- [P2]\n- [P2]"),
        ]
        self.assertEqual(count_tagged_issue_runs(results), 3)


if __name__ == "__main__":
    unittest.main()
