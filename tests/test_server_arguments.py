import unittest
from contextlib import redirect_stderr
from io import StringIO

from mcp_code_review.server import DEFAULT_ENABLED_FEATURES
from mcp_code_review.server import FEATURE_ADDITIONAL_REVIEW_INSTRUCTIONS
from mcp_code_review.server import ServerConfig
from mcp_code_review.server import normalize_arguments
from mcp_code_review.server import parse_args
from mcp_code_review.server import tool_definition


def make_config(*, enabled_features=DEFAULT_ENABLED_FEATURES) -> ServerConfig:
    return ServerConfig(
        codex_bin="codex",
        default_parallelism=1,
        default_concurrency_mode="auto",
        default_timeout_seconds=30,
        default_model=None,
        default_model_provider=None,
        default_profile="review",
        enabled_features=enabled_features,
    )


class TestServerArguments(unittest.TestCase):
    def test_normalize_arguments_preserves_additional_developer_instructions(self):
        result = normalize_arguments(
            {
                "cwd": "/tmp/project",
                "additional_developer_instructions": "Check generated files.",
            },
            make_config(),
        )

        self.assertEqual(
            result["additional_developer_instructions"],
            "Check generated files.",
        )

    def test_normalize_arguments_treats_blank_additional_instructions_as_unset(self):
        result = normalize_arguments(
            {
                "cwd": "/tmp/project",
                "additional_developer_instructions": "   ",
            },
            make_config(),
        )

        self.assertIsNone(result["additional_developer_instructions"])

    def test_normalize_arguments_rejects_additional_instructions_when_feature_disabled(self):
        with self.assertRaisesRegex(
            ValueError,
            (
                "additional_developer_instructions requires "
                "additional_review_instructions to be enabled"
            ),
        ):
            normalize_arguments(
                {
                    "cwd": "/tmp/project",
                    "additional_developer_instructions": "Check generated files.",
                },
                make_config(enabled_features=frozenset()),
            )

    def test_tool_definition_hides_additional_developer_instructions_when_disabled(self):
        properties = tool_definition(make_config(enabled_features=frozenset()))[
            "inputSchema"
        ]["properties"]
        self.assertNotIn("additional_developer_instructions", properties)

    def test_tool_definition_includes_additional_developer_instructions_by_default(self):
        properties = tool_definition(make_config())["inputSchema"]["properties"]
        self.assertIn("additional_developer_instructions", properties)
        self.assertEqual(
            properties["additional_developer_instructions"]["type"],
            "string",
        )

    def test_parse_args_enables_additional_review_instructions_by_default(self):
        config = parse_args([])

        self.assertEqual(
            config.enabled_features,
            frozenset({FEATURE_ADDITIONAL_REVIEW_INSTRUCTIONS}),
        )

    def test_parse_args_accepts_explicit_enable_for_backwards_compatibility(self):
        config = parse_args(["--enable", FEATURE_ADDITIONAL_REVIEW_INSTRUCTIONS])

        self.assertEqual(
            config.enabled_features,
            frozenset({FEATURE_ADDITIONAL_REVIEW_INSTRUCTIONS}),
        )

    def test_parse_args_disables_feature(self):
        config = parse_args(["--disable", FEATURE_ADDITIONAL_REVIEW_INSTRUCTIONS])

        self.assertEqual(config.enabled_features, frozenset())

    def test_parse_args_rejects_conflicting_feature_flags(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as exc_info:
            parse_args(
                [
                    "--enable",
                    FEATURE_ADDITIONAL_REVIEW_INSTRUCTIONS,
                    "--disable",
                    FEATURE_ADDITIONAL_REVIEW_INSTRUCTIONS,
                ]
            )

        self.assertEqual(exc_info.exception.code, 2)

    def test_parse_args_rejects_unknown_feature(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as exc_info:
            parse_args(["--enable", "unknown_feature"])

        self.assertEqual(exc_info.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
