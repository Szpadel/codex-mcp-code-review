import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from mcp_code_review.app_server import (
    AppServerError,
    BANNED_APP_SERVER_FEATURES,
    MCP_DISABLED_ERROR_TEXT,
    MCP_SERVER_LIST_ATTEMPTS,
    build_app_server_command,
    is_mcp_tool_call_notification,
    list_mcp_server_names_with_retry,
    parse_mcp_server_names,
    run_reviews,
    run_single_review,
)
from mcp_code_review.spawn_guard import SPAWN_GUARD_CONFIG_ASSIGNMENT


class TestAppServerSpawnConfig(unittest.TestCase):
    def test_parse_mcp_server_names_deduplicates(self):
        payload = """
        [
          {"name": "review"},
          {"name": "serena"},
          {"name": "review"},
          {"name": 123},
          {}
        ]
        """
        self.assertEqual(parse_mcp_server_names(payload), ["review", "serena"])

    def test_parse_mcp_server_names_rejects_invalid_json(self):
        with self.assertRaises(AppServerError):
            parse_mcp_server_names("{not json")

    def test_build_app_server_command_disables_mcp_servers_and_sets_guard(self):
        command = build_app_server_command(
            codex_bin="codex",
            profile="review",
            mcp_server_names=["review", "serena"],
        )

        self.assertEqual(command[0], "codex")
        self.assertEqual(command[-1], "app-server")
        self.assertIn("profile=review", command)
        self.assertEqual(
            BANNED_APP_SERVER_FEATURES,
            ("apps", "collab", "multi_agent", "memory_tool", "spawn_csv"),
        )
        for feature in BANNED_APP_SERVER_FEATURES:
            self.assertIn(f"features.{feature}=false", command)
        self.assertIn(SPAWN_GUARD_CONFIG_ASSIGNMENT, command)
        self.assertIn("mcp_servers.review.enabled=false", command)
        self.assertIn("mcp_servers.serena.enabled=false", command)

    def test_is_mcp_tool_call_notification_variants(self):
        self.assertTrue(
            is_mcp_tool_call_notification({"method": "item/mcpToolCall/progress", "params": {}})
        )
        self.assertTrue(
            is_mcp_tool_call_notification(
                {"method": "event/msg", "params": {"msg": {"type": "mcp_tool_call_begin"}}}
            )
        )
        self.assertTrue(
            is_mcp_tool_call_notification(
                {"method": "item/completed", "params": {"item": {"type": "mcpToolCall"}}}
            )
        )
        self.assertFalse(
            is_mcp_tool_call_notification(
                {"method": "item/completed", "params": {"item": {"type": "exitedReviewMode"}}}
            )
        )


class TestMcpServerNameRetry(unittest.IsolatedAsyncioTestCase):
    async def test_list_mcp_server_names_retries_once(self):
        list_once = AsyncMock(side_effect=[AppServerError("transient"), ["review"]])
        with patch("mcp_code_review.app_server._list_mcp_server_names_once", list_once):
            names = await list_mcp_server_names_with_retry("codex", profile="review")

        self.assertEqual(names, ["review"])
        self.assertEqual(list_once.await_count, 2)

    async def test_list_mcp_server_names_fails_after_retry(self):
        list_once = AsyncMock(
            side_effect=[AppServerError("first"), AppServerError("second")]
        )
        with patch("mcp_code_review.app_server._list_mcp_server_names_once", list_once):
            with self.assertRaises(AppServerError):
                await list_mcp_server_names_with_retry("codex", profile=None)

        self.assertEqual(list_once.await_count, MCP_SERVER_LIST_ATTEMPTS)


class TestRunReviewsFallback(unittest.IsolatedAsyncioTestCase):
    async def test_auto_falls_back_to_process_mode_on_thread_failure(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", False),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(side_effect=AppServerError("codex app-server closed stdout")),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(return_value=["process-result"]),
            ) as run_processes,
        ):
            result = await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertEqual(result, ["process-result"])
        self.assertEqual(run_threads.await_count, 1)
        self.assertEqual(run_processes.await_count, 1)

    async def test_auto_remembers_thread_failure_and_starts_next_review_in_process_mode(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", False),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(side_effect=AppServerError("thread failure")),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(side_effect=[["first"], ["second"]]),
            ) as run_processes,
        ):
            first = await run_reviews("codex", "/tmp/repo", 2, 30, "auto")
            second = await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertEqual(first, ["first"])
        self.assertEqual(second, ["second"])
        self.assertEqual(run_threads.await_count, 1)
        self.assertEqual(run_processes.await_count, 2)

    async def test_threads_mode_falls_back_to_process_mode_on_failure(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", False),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(side_effect=AppServerError("thread failure")),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(return_value=["process-result"]),
            ) as run_processes,
        ):
            result = await run_reviews("codex", "/tmp/repo", 2, 30, "threads")

        self.assertEqual(result, ["process-result"])
        self.assertEqual(run_threads.await_count, 1)
        self.assertEqual(run_processes.await_count, 1)

    async def test_threads_mode_uses_process_mode_when_memory_prefers_processes(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", True),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(return_value=["thread-result"]),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(return_value=["process-result"]),
            ) as run_processes,
        ):
            result = await run_reviews("codex", "/tmp/repo", 2, 30, "threads")

        self.assertEqual(result, ["process-result"])
        self.assertEqual(run_threads.await_count, 0)
        self.assertEqual(run_processes.await_count, 1)

    async def test_auto_does_not_remember_process_mode_when_fallback_fails(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", False),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(side_effect=[AppServerError("thread failure"), ["thread-result"]]),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(side_effect=[AppServerError("process failure")]),
            ) as run_processes,
        ):
            with self.assertRaises(AppServerError):
                await run_reviews("codex", "/tmp/repo", 2, 30, "auto")
            second = await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertEqual(second, ["thread-result"])
        self.assertEqual(run_threads.await_count, 2)
        self.assertEqual(run_processes.await_count, 1)

    async def test_auto_does_not_fallback_on_mcp_policy_violation(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", False),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(side_effect=AppServerError(MCP_DISABLED_ERROR_TEXT)),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(return_value=["process-result"]),
            ) as run_processes,
        ):
            with self.assertRaises(AppServerError) as ctx:
                await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertEqual(str(ctx.exception), MCP_DISABLED_ERROR_TEXT)
        self.assertEqual(run_threads.await_count, 1)
        self.assertEqual(run_processes.await_count, 0)

    async def test_auto_does_not_fallback_on_wrapped_mcp_policy_violation(self):
        wrapped_error = (
            "{'code': -32000, 'message': "
            f"'{MCP_DISABLED_ERROR_TEXT}'"
            "}"
        )
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", False),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(side_effect=AppServerError(wrapped_error)),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(return_value=["process-result"]),
            ) as run_processes,
        ):
            with self.assertRaises(AppServerError) as ctx:
                await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertIn(MCP_DISABLED_ERROR_TEXT, str(ctx.exception))
        self.assertEqual(run_threads.await_count, 1)
        self.assertEqual(run_processes.await_count, 0)

    async def test_auto_does_not_fallback_on_timeout_error(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", False),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(side_effect=AppServerError("codex review timed out after 30s")),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(return_value=["process-result"]),
            ) as run_processes,
        ):
            with self.assertRaises(AppServerError) as ctx:
                await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertIn("timed out", str(ctx.exception).lower())
        self.assertEqual(run_threads.await_count, 1)
        self.assertEqual(run_processes.await_count, 0)

    async def test_auto_recovers_to_threads_when_preferred_process_mode_fails(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", True),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(return_value=["thread-result"]),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(side_effect=AppServerError("process failure")),
            ) as run_processes,
        ):
            result = await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertEqual(result, ["thread-result"])
        self.assertEqual(run_processes.await_count, 1)
        self.assertEqual(run_threads.await_count, 1)

    async def test_auto_preferred_process_mode_does_not_retry_threads_on_mcp_policy_violation(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", True),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(return_value=["thread-result"]),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(side_effect=AppServerError(MCP_DISABLED_ERROR_TEXT)),
            ) as run_processes,
        ):
            with self.assertRaises(AppServerError) as ctx:
                await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertEqual(str(ctx.exception), MCP_DISABLED_ERROR_TEXT)
        self.assertEqual(run_processes.await_count, 1)
        self.assertEqual(run_threads.await_count, 0)

    async def test_auto_preferred_process_mode_clears_memory_on_hard_process_error(self):
        with (
            patch("mcp_code_review.app_server._PREFER_PROCESS_MODE", True),
            patch(
                "mcp_code_review.app_server.run_reviews_threads",
                new=AsyncMock(return_value=["thread-result"]),
            ) as run_threads,
            patch(
                "mcp_code_review.app_server.run_reviews_processes",
                new=AsyncMock(side_effect=[AppServerError("codex review timed out after 30s")]),
            ) as run_processes,
        ):
            with self.assertRaises(AppServerError) as ctx:
                await run_reviews("codex", "/tmp/repo", 2, 30, "auto")
            second = await run_reviews("codex", "/tmp/repo", 2, 30, "auto")

        self.assertIn("timed out", str(ctx.exception).lower())
        self.assertEqual(second, ["thread-result"])
        self.assertEqual(run_processes.await_count, 1)
        self.assertEqual(run_threads.await_count, 1)


class _RecordingClient:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "review/start":
            return {"turn": {"id": "turn-1"}, "reviewThreadId": "thread-1"}
        raise AssertionError(f"unexpected method: {method}")

    def add_tracker(self, tracker):
        tracker.future.set_result(
            '{"verdict":"pass","summary":"ok","comment_markdown":""}'
        )


class TestRunSingleReview(unittest.IsolatedAsyncioTestCase):
    async def test_thread_start_payload_omits_base_instructions(self):
        client = _RecordingClient()

        result = await run_single_review(
            client,
            cwd="/tmp/project",
            timeout_seconds=5,
            model="gpt-5",
            model_provider="openai",
        )

        self.assertEqual(result.verdict, "pass")
        self.assertEqual(client.requests[0][0], "thread/start")
        thread_start_payload = client.requests[0][1]
        self.assertEqual(thread_start_payload["cwd"], "/tmp/project")
        self.assertEqual(thread_start_payload["approvalPolicy"], "never")
        self.assertEqual(thread_start_payload["sandbox"], "read-only")
        self.assertEqual(thread_start_payload["model"], "gpt-5")
        self.assertEqual(thread_start_payload["modelProvider"], "openai")
        self.assertNotIn("baseInstructions", thread_start_payload)

    async def test_review_start_uses_uncommitted_changes_without_extra_instructions(self):
        client = _RecordingClient()

        await run_single_review(
            client,
            cwd="/tmp/project",
            timeout_seconds=5,
        )

        self.assertEqual(client.requests[1][0], "review/start")
        review_start_payload = client.requests[1][1]
        self.assertEqual(review_start_payload["delivery"], "inline")
        self.assertEqual(review_start_payload["target"], {"type": "uncommittedChanges"})

    async def test_review_start_uses_custom_prompt_with_extra_instructions(self):
        client = _RecordingClient()

        await run_single_review(
            client,
            cwd="/tmp/project",
            timeout_seconds=5,
            additional_developer_instructions="  Check generated files carefully.  ",
        )

        self.assertEqual(client.requests[1][0], "review/start")
        review_start_payload = client.requests[1][1]
        self.assertEqual(review_start_payload["delivery"], "inline")
        self.assertEqual(review_start_payload["target"]["type"], "custom")
        self.assertIn(
            "Review the current code changes (staged, unstaged, and untracked files)",
            review_start_payload["target"]["instructions"],
        )
        self.assertIn(
            "Additional review instructions:\nCheck generated files carefully.",
            review_start_payload["target"]["instructions"],
        )


if __name__ == "__main__":
    unittest.main()
