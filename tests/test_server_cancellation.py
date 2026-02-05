import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from mcp_code_review.server import REQUEST_CANCELLED_ERROR_CODE
from mcp_code_review.server import REQUEST_CANCELLED_ERROR_TEXT
from mcp_code_review.server import ServerConfig
from mcp_code_review.server import extract_cancelled_request_key
from mcp_code_review.server import is_cancelled_notification
from mcp_code_review.server import request_id_key
from mcp_code_review.server import remove_active_tool_call_if_current
from mcp_code_review.server import run_tool_call_task
from mcp_code_review.server import run_tool_call_with_cancellation_response


def make_config() -> ServerConfig:
    return ServerConfig(
        codex_bin="codex",
        default_parallelism=1,
        default_concurrency_mode="auto",
        default_timeout_seconds=1,
        default_model=None,
        default_model_provider=None,
        default_profile=None,
    )


class TestServerCancellationHelpers(unittest.TestCase):
    def test_is_cancelled_notification(self):
        self.assertTrue(is_cancelled_notification({"method": "notifications/cancelled"}))
        self.assertTrue(is_cancelled_notification({"method": "$/cancelRequest"}))
        self.assertFalse(is_cancelled_notification({"method": "tools/call"}))

    def test_extract_cancelled_request_key(self):
        message = {"method": "notifications/cancelled", "params": {"requestId": 123}}
        self.assertEqual(extract_cancelled_request_key(message), request_id_key(123))

        message = {"method": "$/cancelRequest", "params": {"request_id": "abc"}}
        self.assertEqual(extract_cancelled_request_key(message), request_id_key("abc"))

        message = {"method": "$/cancelRequest", "params": {"id": "std-id"}}
        self.assertEqual(extract_cancelled_request_key(message), request_id_key("std-id"))

        self.assertIsNone(extract_cancelled_request_key({"method": "tools/call", "params": {}}))
        self.assertIsNone(
            extract_cancelled_request_key({"method": "notifications/cancelled", "params": {}})
        )

    def test_is_cancelled_notification_rejects_non_string_method(self):
        self.assertFalse(is_cancelled_notification({"method": {"bad": "type"}}))


class TestServerCancellationFlow(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_wrapper_returns_cancellation_response(self):
        async def slow_tool_call(*_args, **_kwargs):
            await asyncio.sleep(10)
            return {"never": "reached"}

        with patch("mcp_code_review.server.handle_tool_call", new=slow_tool_call):
            task = asyncio.create_task(
                run_tool_call_with_cancellation_response("request-1", {}, make_config())
            )
            await asyncio.sleep(0)
            task.cancel()
            response = await task

        self.assertEqual(response["id"], "request-1")
        self.assertEqual(response["error"]["code"], REQUEST_CANCELLED_ERROR_CODE)
        self.assertEqual(response["error"]["message"], REQUEST_CANCELLED_ERROR_TEXT)

    async def test_remove_active_tool_call_if_current_keeps_replacement_task(self):
        active_tool_calls = {}
        old_task = asyncio.create_task(asyncio.sleep(0))
        replacement_task = asyncio.create_task(asyncio.sleep(0))
        key = "request-key"
        active_tool_calls[key] = replacement_task

        remove_active_tool_call_if_current(active_tool_calls, key, old_task)

        self.assertIs(active_tool_calls[key], replacement_task)
        old_task.cancel()
        replacement_task.cancel()
        await asyncio.gather(old_task, replacement_task, return_exceptions=True)

    async def test_remove_active_tool_call_if_current_removes_current_task(self):
        task = asyncio.create_task(asyncio.sleep(0))
        key = "request-key"
        active_tool_calls = {key: task}

        remove_active_tool_call_if_current(active_tool_calls, key, task)

        self.assertNotIn(key, active_tool_calls)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_run_tool_call_task_writes_response_when_cancelled_while_waiting_for_lock(self):
        write_lock = asyncio.Lock()
        await write_lock.acquire()
        captured = []
        write_entered = asyncio.Event()

        async def fake_write_jsonrpc_message(payload, lock):
            write_entered.set()
            async with lock:
                captured.append(payload)

        with (
            patch(
                "mcp_code_review.server.run_tool_call_with_cancellation_response",
                new=AsyncMock(return_value={"id": "request-1", "result": {"ok": True}}),
            ),
            patch("mcp_code_review.server.write_jsonrpc_message", new=fake_write_jsonrpc_message),
        ):
            task = asyncio.create_task(
                run_tool_call_task("request-1", {}, make_config(), write_lock)
            )
            await write_entered.wait()
            task.cancel()
            write_lock.release()
            await asyncio.sleep(0)
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["id"], "request-1")


if __name__ == "__main__":
    unittest.main()
