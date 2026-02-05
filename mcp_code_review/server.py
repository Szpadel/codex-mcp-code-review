import argparse
import asyncio
from collections import Counter
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from mcp_code_review.app_server import resolve_cwd, run_reviews
from mcp_code_review.review import ReviewResult, extract_severity_tags
from mcp_code_review.spawn_guard import find_guarded_app_server_ancestor_cmdline


TOOL_NAME = "review_uncommitted_changes"
PROTOCOL_VERSION_FALLBACK = "2024-11-05"
RECURSIVE_REVIEW_ERROR_TEXT = (
    "recursive review call forbidden: this server is running inside a spawned guarded app-server"
)
REQUEST_CANCELLED_ERROR_CODE = -32800
REQUEST_CANCELLED_ERROR_TEXT = "request cancelled"
INTERNAL_ERROR_CODE = -32000


class RecursiveReviewCallError(RuntimeError):
    pass


def ensure_not_recursive_review_call() -> None:
    guarded_cmdline = find_guarded_app_server_ancestor_cmdline()
    if guarded_cmdline is not None:
        raise RecursiveReviewCallError(RECURSIVE_REVIEW_ERROR_TEXT)


@dataclass(frozen=True)
class ServerConfig:
    codex_bin: str
    default_parallelism: int
    default_concurrency_mode: str
    default_timeout_seconds: int
    default_model: Optional[str]
    default_model_provider: Optional[str]
    default_profile: Optional[str]


def tool_definition() -> Dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "title": "Review uncommitted changes",
        "description": (
            "Run Codex review on uncommitted changes using the app-server review API. "
            "Returns structured results for each run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Repository root to review."},
                "runs": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Number of review runs (recommended: 4).",
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "verdict": {"type": "string"},
                            "summary": {"type": "string"},
                            "comment_markdown": {"type": "string"},
                        },
                        "required": ["index", "verdict", "summary", "comment_markdown"],
                    },
                }
            },
            "required": ["results"],
        },
    }


def jsonrpc_response(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def ensure_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def normalize_arguments(
    args: Optional[Dict[str, Any]],
    config: ServerConfig,
) -> Dict[str, Any]:
    args = args or {}
    cwd = args.get("cwd")
    parallelism = args.get("runs", args.get("parallelism", config.default_parallelism))
    concurrency_mode = args.get("concurrency_mode", config.default_concurrency_mode)
    timeout_seconds = args.get("timeout_seconds", config.default_timeout_seconds)
    model = args.get("model", config.default_model)
    model_provider = args.get("model_provider", config.default_model_provider)
    profile = config.default_profile

    parallelism = ensure_int(parallelism, "runs")
    timeout_seconds = ensure_int(timeout_seconds, "timeout_seconds")
    if parallelism < 1:
        raise ValueError("runs must be >= 1")
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be >= 1")
    if concurrency_mode not in {"auto", "threads", "processes"}:
        raise ValueError("concurrency_mode must be auto, threads, or processes")

    return {
        "cwd": resolve_cwd(cwd),
        "parallelism": parallelism,
        "concurrency_mode": concurrency_mode,
        "timeout_seconds": timeout_seconds,
        "model": model,
        "model_provider": model_provider,
        "profile": profile,
    }


def format_results(results: list[ReviewResult]) -> Dict[str, Any]:
    payload = []
    for idx, result in enumerate(results, start=1):
        payload.append(
            {
                "index": idx,
                "verdict": result.verdict,
                "summary": result.summary,
                "comment_markdown": result.comment_markdown,
            }
        )
    return {"results": payload}


def count_tagged_issue_runs(results: list[ReviewResult]) -> int:
    return sum(1 for result in results if extract_severity_tags(result.comment_markdown))


def format_severity_summary(results: list[ReviewResult]) -> str:
    counts: Counter[str] = Counter()
    for result in results:
        for tag in extract_severity_tags(result.comment_markdown):
            counts[tag] += 1
    if not counts:
        return ""

    def severity_key(tag: str) -> tuple[int, str]:
        number = tag[1:] if tag.startswith("P") else ""
        return (int(number) if number.isdigit() else sys.maxsize, tag)

    ordered = sorted(counts, key=severity_key)
    summary = ", ".join(f"{tag}={counts[tag]}" for tag in ordered)
    return f" ({summary})"


def format_elapsed_time(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(round(elapsed_seconds)))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_completion_summary(
    review_count: int,
    issue_runs: int,
    severity_summary: str,
    elapsed_seconds: float,
) -> str:
    elapsed_text = format_elapsed_time(elapsed_seconds)
    return (
        f"completed {review_count} review(s); {issue_runs} run(s) reported tagged issues"
        f"{severity_summary}; took {elapsed_text}"
    )


def request_id_key(request_id: Any) -> str:
    try:
        return json.dumps(request_id, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(request_id)


def is_cancelled_notification(message: Dict[str, Any]) -> bool:
    method = message.get("method")
    if not isinstance(method, str):
        return False
    return method in {"notifications/cancelled", "$/cancelRequest"}


def extract_cancelled_request_key(message: Dict[str, Any]) -> Optional[str]:
    if not is_cancelled_notification(message):
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    request_id = params.get("requestId")
    if request_id is None:
        request_id = params.get("request_id")
    if request_id is None:
        request_id = params.get("id")
    if request_id is None:
        return None
    return request_id_key(request_id)


def cancellation_response(request_id: Any) -> Dict[str, Any]:
    return jsonrpc_error(request_id, REQUEST_CANCELLED_ERROR_CODE, REQUEST_CANCELLED_ERROR_TEXT)


def internal_error_response(request_id: Any, message: str) -> Dict[str, Any]:
    return jsonrpc_error(request_id, INTERNAL_ERROR_CODE, message)


def remove_active_tool_call_if_current(
    active_tool_calls: dict[str, asyncio.Task],
    task_key: str,
    finished_task: asyncio.Task,
) -> None:
    current_task = active_tool_calls.get(task_key)
    if current_task is finished_task:
        active_tool_calls.pop(task_key, None)


async def handle_tool_call(
    request_id: Any,
    params: Dict[str, Any],
    config: ServerConfig,
) -> Dict[str, Any]:
    name = params.get("name")
    if name != TOOL_NAME:
        return jsonrpc_error(request_id, -32601, f"unknown tool: {name}")

    if shutil.which(config.codex_bin) is None:
        return jsonrpc_error(request_id, -32602, f"{config.codex_bin} not found on PATH")

    try:
        args = normalize_arguments(params.get("arguments"), config)
    except ValueError as exc:
        return jsonrpc_error(request_id, -32602, str(exc))

    start_time = time.monotonic()
    try:
        results = await run_reviews(
            config.codex_bin,
            args["cwd"],
            args["parallelism"],
            args["timeout_seconds"],
            args["concurrency_mode"],
            model=args["model"],
            model_provider=args["model_provider"],
            profile=args["profile"],
        )
    except Exception as exc:
        return jsonrpc_response(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": f"review failed: {exc}",
                    }
                ],
                "isError": True,
            },
        )
    elapsed_seconds = time.monotonic() - start_time

    structured = format_results(results)
    issues = count_tagged_issue_runs(results)
    severity_summary = format_severity_summary(results)
    summary_text = format_completion_summary(
        len(results),
        issues,
        severity_summary,
        elapsed_seconds,
    )
    return jsonrpc_response(
        request_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": summary_text,
                }
            ],
            "structuredContent": structured,
        },
    )


async def run_tool_call_with_cancellation_response(
    request_id: Any,
    params: Dict[str, Any],
    config: ServerConfig,
) -> Dict[str, Any]:
    try:
        return await handle_tool_call(request_id, params, config)
    except asyncio.CancelledError:
        return cancellation_response(request_id)
    except Exception as exc:  # pragma: no cover - defensive fallback path
        return internal_error_response(request_id, f"review failed: {exc}")


async def write_jsonrpc_message(payload: Dict[str, Any], lock: asyncio.Lock) -> None:
    async with lock:
        sys.stdout.write(json.dumps(payload))
        sys.stdout.write("\n")
        sys.stdout.flush()


async def run_tool_call_task(
    request_id: Any,
    params: Dict[str, Any],
    config: ServerConfig,
    write_lock: asyncio.Lock,
) -> None:
    response = await run_tool_call_with_cancellation_response(request_id, params, config)
    # Once we computed a terminal response, ensure it is emitted even if the task is cancelled
    # while waiting for the write lock.
    await asyncio.shield(write_jsonrpc_message(response, write_lock))


async def handle_request(
    message: Dict[str, Any],
    config: ServerConfig,
) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        params = message.get("params") or {}
        protocol_version = params.get("protocolVersion", PROTOCOL_VERSION_FALLBACK)
        result = {
            "capabilities": {"tools": {"listChanged": False}},
            "protocolVersion": protocol_version,
            "serverInfo": {
                "name": "codex-mcp-code-review",
                "title": "Codex MCP Code Review",
                "version": "0.1.0",
            },
        }
        return jsonrpc_response(request_id, result)

    if method == "tools/list":
        return jsonrpc_response(request_id, {"tools": [tool_definition()]})

    if method == "tools/call":
        params = message.get("params") or {}
        return await handle_tool_call(request_id, params, config)

    if method == "notifications/initialized":
        return None

    if request_id is not None:
        return jsonrpc_error(request_id, -32601, f"unknown method: {method}")
    return None


def parse_args(argv: Optional[list[str]] = None) -> ServerConfig:
    parser = argparse.ArgumentParser(description="Codex MCP code review server")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--concurrency-mode", default="auto", choices=["auto", "threads", "processes"])
    parser.add_argument("--timeout-seconds", type=int, default=2700)
    parser.add_argument("--model", default=None)
    parser.add_argument("--model-provider", default=None)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)
    return ServerConfig(
        codex_bin=args.codex_bin,
        default_parallelism=args.parallelism,
        default_concurrency_mode=args.concurrency_mode,
        default_timeout_seconds=args.timeout_seconds,
        default_model=args.model,
        default_model_provider=args.model_provider,
        default_profile=args.profile,
    )


async def run_server(config: ServerConfig) -> None:
    write_lock = asyncio.Lock()
    active_tool_calls: dict[str, asyncio.Task] = {}
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        cancelled_key = extract_cancelled_request_key(message)
        if cancelled_key is not None:
            task = active_tool_calls.get(cancelled_key)
            if task is not None:
                task.cancel()
            continue

        if message.get("method") == "tools/call":
            request_id = message.get("id")
            task_key = request_id_key(request_id)
            params = message.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            previous_task = active_tool_calls.pop(task_key, None)
            if previous_task is not None:
                previous_task.cancel()
            task = asyncio.create_task(run_tool_call_task(request_id, params, config, write_lock))
            active_tool_calls[task_key] = task
            task.add_done_callback(
                lambda done_task, key=task_key: remove_active_tool_call_if_current(
                    active_tool_calls, key, done_task
                )
            )
            continue

        response = await handle_request(message, config)
        if response is None:
            continue
        await write_jsonrpc_message(response, write_lock)

    if active_tool_calls:
        remaining_tasks = list(active_tool_calls.values())
        for task in remaining_tasks:
            task.cancel()
        await asyncio.gather(*remaining_tasks, return_exceptions=True)


def main() -> None:
    try:
        ensure_not_recursive_review_call()
        config = parse_args()
        asyncio.run(run_server(config))
    except RecursiveReviewCallError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
