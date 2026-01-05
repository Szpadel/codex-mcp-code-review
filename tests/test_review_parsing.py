import unittest

from mcp_code_review.review import parse_review_output


class TestReviewParsing(unittest.TestCase):
    def test_parse_review_output_pass(self):
        text = '{"verdict":"pass","summary":"ok","comment_markdown":""}'
        result = parse_review_output(text)
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.summary, "ok")
        self.assertEqual(result.comment_markdown, "")

    def test_parse_review_output_comment(self):
        text = '{"verdict":"comment","summary":"needs changes","comment_markdown":"- fix"}'
        result = parse_review_output(text)
        self.assertEqual(result.verdict, "comment")
        self.assertEqual(result.summary, "needs changes")
        self.assertEqual(result.comment_markdown, "- fix")

    def test_parse_review_output_fallback(self):
        text = "Looks good overall\n\n- minor nit"
        result = parse_review_output(text)
        self.assertEqual(result.verdict, "comment")
        self.assertEqual(result.summary, "Looks good overall")
        self.assertEqual(result.comment_markdown, text)


if __name__ == "__main__":
    unittest.main()
