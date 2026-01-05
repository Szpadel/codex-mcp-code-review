import argparse
import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional

from mcp_code_review.app_server import resolve_cwd, run_reviews
from mcp_code_review.review import ReviewResult


TOOL_NAME = "review_uncommitted_changes"
PROTOCOL_VERSION_FALLBACK = "2024-11-05"


@dataclass(frozen=True)
class ServerConfig:
    codex_bin: str
    default_parallelism: int
    default_concurrency_mode: str
    default_timeout_seconds: int
    default_model: Optional[str]
    default_model_provider: Optional[str]


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
                "parallelism": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Number of parallel review runs.",
                },
                "concurrency_mode": {
                    "type": "string",
                    "enum": ["auto", "threads", "processes"],
                    "description": "How to run parallel reviews.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Timeout per review run in seconds.",
                },
                "model": {"type": "string", "description": "Override model id."},
                "model_provider": {
                    "type": "string",
                    "description": "Override model provider.",
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
    parallelism = args.get("parallelism", config.default_parallelism)
    concurrency_mode = args.get("concurrency_mode", config.default_concurrency_mode)
    timeout_seconds = args.get("timeout_seconds", config.default_timeout_seconds)
    model = args.get("model", config.default_model)
    model_provider = args.get("model_provider", config.default_model_provider)

    parallelism = ensure_int(parallelism, "parallelism")
    timeout_seconds = ensure_int(timeout_seconds, "timeout_seconds")
    if parallelism < 1:
        raise ValueError("parallelism must be >= 1")
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

    try:
        results = await run_reviews(
            config.codex_bin,
            args["cwd"],
            args["parallelism"],
            args["timeout_seconds"],
            args["concurrency_mode"],
            model=args["model"],
            model_provider=args["model_provider"],
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

    structured = format_results(results)
    return jsonrpc_response(
        request_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": f"completed {len(results)} review(s)",
                }
            ],
            "structuredContent": structured,
        },
    )


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
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--concurrency-mode", default="auto", choices=["auto", "threads", "processes"])
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--model", default=None)
    parser.add_argument("--model-provider", default=None)
    args = parser.parse_args(argv)
    return ServerConfig(
        codex_bin=args.codex_bin,
        default_parallelism=args.parallelism,
        default_concurrency_mode=args.concurrency_mode,
        default_timeout_seconds=args.timeout_seconds,
        default_model=args.model,
        default_model_provider=args.model_provider,
    )


async def run_server(config: ServerConfig) -> None:
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

        response = await handle_request(message, config)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response))
        sys.stdout.write("\n")
        sys.stdout.flush()


def main() -> None:
    config = parse_args()
    asyncio.run(run_server(config))


if __name__ == "__main__":
    main()
