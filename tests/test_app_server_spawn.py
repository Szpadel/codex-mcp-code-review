import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from mcp_code_review.app_server import (
    AppServerError,
    DISABLE_APPS_CONFIG_ASSIGNMENT,
    DISABLE_COLLAB_CONFIG_ASSIGNMENT,
    DISABLE_MULTI_AGENT_CONFIG_ASSIGNMENT,
    DISABLE_MEMORY_TOOL_CONFIG_ASSIGNMENT,
    MCP_SERVER_LIST_ATTEMPTS,
    build_app_server_command,
    is_mcp_tool_call_notification,
    list_mcp_server_names_with_retry,
    parse_mcp_server_names,
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
        self.assertIn(DISABLE_APPS_CONFIG_ASSIGNMENT, command)
        self.assertIn(DISABLE_COLLAB_CONFIG_ASSIGNMENT, command)
        self.assertIn(DISABLE_MULTI_AGENT_CONFIG_ASSIGNMENT, command)
        self.assertIn(DISABLE_MEMORY_TOOL_CONFIG_ASSIGNMENT, command)
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


if __name__ == "__main__":
    unittest.main()
