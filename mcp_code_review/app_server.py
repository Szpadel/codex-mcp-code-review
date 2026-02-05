import asyncio
import contextlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mcp_code_review.review import ReviewResult, parse_review_output
from mcp_code_review.spawn_guard import SPAWN_GUARD_CONFIG_ASSIGNMENT


class AppServerError(RuntimeError):
    pass


class ConcurrencyNotSupported(AppServerError):
    pass


MCP_SERVER_LIST_ATTEMPTS = 2  # Initial attempt plus one retry.
DISABLE_APPS_CONFIG_ASSIGNMENT = "features.apps=false"
DISABLE_COLLAB_CONFIG_ASSIGNMENT = "features.collab=false"
MCP_DISABLED_ERROR_TEXT = "spawned codex attempted MCP tool call; MCP servers must stay disabled"


def is_concurrency_error(message: str) -> bool:
    if not message:
        return False
    msg = message.lower()
    patterns = [
        "already in progress",
        "already running",
        "only one",
        "single active",
        "busy",
        "concurrent",
        "another turn",
        "one active",
    ]
    return any(pattern in msg for pattern in patterns)


def matches_thread_turn(params: Optional[Dict[str, Any]], thread_id: str, turn_id: str) -> bool:
    if not params:
        return True
    thread_value = params.get("threadId")
    turn_value = params.get("turnId")
    thread_matches = thread_value is None or thread_value == thread_id
    turn_matches = turn_value is None or turn_value == turn_id
    return thread_matches and turn_matches


def timeout_error(timeout_seconds: int) -> AppServerError:
    return AppServerError(f"codex review timed out after {timeout_seconds}s")


def parse_mcp_server_names(raw_json: str) -> List[str]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise AppServerError("failed to parse `codex mcp list --json` output") from exc
    if not isinstance(payload, list):
        raise AppServerError("`codex mcp list --json` returned unexpected payload type")

    names: List[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


async def _list_mcp_server_names_once(
    codex_bin: str,
    profile: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> List[str]:
    command = [codex_bin]
    if profile:
        command.extend(["-c", f"profile={profile}"])
    command.extend(["mcp", "list", "--json"])
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise AppServerError("failed to launch `codex mcp list --json`") from exc

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="ignore").strip()
        details = f": {stderr_text}" if stderr_text else ""
        raise AppServerError(f"`codex mcp list --json` exited with {process.returncode}{details}")

    return parse_mcp_server_names(stdout.decode("utf-8", errors="ignore"))


async def list_mcp_server_names_with_retry(
    codex_bin: str,
    profile: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> List[str]:
    last_error: Optional[AppServerError] = None
    for _ in range(MCP_SERVER_LIST_ATTEMPTS):
        try:
            return await _list_mcp_server_names_once(codex_bin, profile=profile, env=env)
        except AppServerError as exc:
            last_error = exc
    raise AppServerError(
        "failed to enumerate MCP servers after retry; refusing to start spawned codex app-server"
    ) from last_error


def build_app_server_command(
    codex_bin: str,
    profile: Optional[str],
    mcp_server_names: List[str],
) -> List[str]:
    command = [codex_bin]
    if profile:
        command.extend(["-c", f"profile={profile}"])
    command.extend(["-c", DISABLE_APPS_CONFIG_ASSIGNMENT])
    command.extend(["-c", DISABLE_COLLAB_CONFIG_ASSIGNMENT])
    command.extend(["-c", SPAWN_GUARD_CONFIG_ASSIGNMENT])
    for name in mcp_server_names:
        command.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    command.append("app-server")
    return command


def is_mcp_tool_call_notification(message: Dict[str, Any]) -> bool:
    method = message.get("method")
    if not isinstance(method, str):
        return False

    lowered_method = method.lower()
    if lowered_method in {"item/mcptoolcall/progress", "mcp_tool_call_begin", "mcp_tool_call_end"}:
        return True

    params = message.get("params")
    if not isinstance(params, dict):
        return False

    # app-server emits multiple event shapes across versions. Match all known variants.
    if method == "item/completed":
        item = params.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if isinstance(item_type, str):
                normalized = item_type.lower()
                if "mcptoolcall" in normalized or "mcp_tool_call" in normalized:
                    return True

    for key in ("msg", "event", "item"):
        nested = params.get(key)
        if not isinstance(nested, dict):
            continue
        nested_type = nested.get("type")
        if not isinstance(nested_type, str):
            continue
        normalized = nested_type.lower()
        if "mcptoolcall" in normalized or "mcp_tool_call" in normalized:
            return True

    return False


@dataclass
class ReviewTracker:
    thread_id: str
    turn_id: str
    future: asyncio.Future
    review_text: Optional[str] = None

    def handle_notification(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not matches_thread_turn(params, self.thread_id, self.turn_id):
            return

        if method == "item/completed":
            item = (params or {}).get("item", {})
            if item.get("type") == "exitedReviewMode":
                review = item.get("review")
                if isinstance(review, str):
                    self.review_text = review
        elif method == "turn/completed":
            turn = (params or {}).get("turn", {})
            status = turn.get("status")
            if status == "failed":
                error_msg = (
                    (turn.get("error") or {}).get("message")
                    if isinstance(turn.get("error"), dict)
                    else None
                )
                message_text = error_msg or "codex turn failed"
                if not self.future.done():
                    self.future.set_exception(AppServerError(message_text))
            else:
                if self.review_text is None:
                    if not self.future.done():
                        self.future.set_exception(
                            AppServerError("codex review missing review text")
                        )
                elif not self.future.done():
                    self.future.set_result(self.review_text)


class AppServerClient:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._pending: Dict[str, asyncio.Future] = {}
        self._trackers: List[ReviewTracker] = []
        self._buffer = bytearray()
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._mcp_violation_error: Optional[AppServerError] = None
        self._reader_task = asyncio.create_task(self._reader_loop())

    @classmethod
    async def start(
        cls,
        codex_bin: str,
        env: Optional[Dict[str, str]] = None,
        profile: Optional[str] = None,
    ) -> "AppServerClient":
        mcp_server_names = await list_mcp_server_names_with_retry(codex_bin, profile=profile, env=env)
        cmd = build_app_server_command(codex_bin, profile, mcp_server_names)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            env=env,
        )
        if process.stdin is None or process.stdout is None:
            process.terminate()
            raise AppServerError("failed to open codex app-server pipes")
        return cls(process)

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-mcp-code-review",
                    "title": "Codex MCP Code Review",
                    "version": "0.1.0",
                }
            },
        )
        await self.notify("initialized")

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._mcp_violation_error is not None:
            raise self._mcp_violation_error
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)
        return await future

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self._mcp_violation_error is not None:
            raise self._mcp_violation_error
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    def add_tracker(self, tracker: ReviewTracker) -> None:
        if self._mcp_violation_error is not None:
            if not tracker.future.done():
                tracker.future.set_exception(self._mcp_violation_error)
            return
        self._trackers.append(tracker)

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self._stdin is None:
            raise AppServerError("codex app-server stdin closed")
        line = json.dumps(payload)
        async with self._write_lock:
            self._stdin.write(line.encode("utf-8"))
            self._stdin.write(b"\n")
            await self._stdin.drain()

    async def _reader_loop(self) -> None:
        assert self._stdout is not None
        try:
            while True:
                chunk = await self._stdout.read(65536)
                if not chunk:
                    break
                self._buffer.extend(chunk)
                while True:
                    newline_index = self._buffer.find(b"\n")
                    if newline_index == -1:
                        break
                    line = bytes(self._buffer[: newline_index + 1])
                    del self._buffer[: newline_index + 1]
                    decoded = line.decode("utf-8", errors="ignore").strip()
                    if not decoded:
                        continue
                    try:
                        message = json.loads(decoded)
                    except json.JSONDecodeError:
                        continue

                    if "method" in message and "id" in message:
                        await self._handle_server_request(message)
                        continue
                    if "id" in message:
                        await self._handle_response(message)
                        continue
                    if "method" in message:
                        self._dispatch_notification(message)
        finally:
            error = AppServerError("codex app-server closed stdout")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            for tracker in self._trackers:
                if not tracker.future.done():
                    tracker.future.set_exception(error)
            self._trackers.clear()

    async def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "item/commandExecution/requestApproval":
            decision = "accept"
        elif method == "item/fileChange/requestApproval":
            decision = "decline"
        else:
            response = {
                "id": request_id,
                "error": {"message": "unsupported request"},
            }
            await self._send(response)
            return

        response = {"id": request_id, "result": {"decision": decision}}
        await self._send(response)

    async def _handle_response(self, message: Dict[str, Any]) -> None:
        request_id = str(message.get("id"))
        future = self._pending.pop(request_id, None)
        if future is None:
            return
        if "error" in message:
            future.set_exception(AppServerError(str(message["error"])))
        else:
            future.set_result(message.get("result"))

    def _dispatch_notification(self, message: Dict[str, Any]) -> None:
        if is_mcp_tool_call_notification(message):
            self._register_mcp_violation()
            return
        if not self._trackers:
            return
        for tracker in list(self._trackers):
            tracker.handle_notification(message)
            if tracker.future.done():
                self._trackers.remove(tracker)

    def _register_mcp_violation(self) -> None:
        if self._mcp_violation_error is not None:
            return
        error = AppServerError(MCP_DISABLED_ERROR_TEXT)
        self._mcp_violation_error = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for tracker in self._trackers:
            if not tracker.future.done():
                tracker.future.set_exception(error)
        self._trackers.clear()


async def run_single_review(
    client: AppServerClient,
    cwd: str,
    timeout_seconds: int,
    model: Optional[str] = None,
    model_provider: Optional[str] = None,
) -> ReviewResult:
    thread_params: Dict[str, Any] = {
        "cwd": cwd,
        "approvalPolicy": "never",
        "sandbox": "read-only",
    }
    if model:
        thread_params["model"] = model
    if model_provider:
        thread_params["modelProvider"] = model_provider

    thread_response = await client.request("thread/start", thread_params)
    thread = (thread_response or {}).get("thread", {})
    thread_id = thread.get("id")
    if not thread_id:
        raise AppServerError("thread/start missing thread id")

    review_response = await client.request(
        "review/start",
        {
            "threadId": thread_id,
            "delivery": "inline",
            "target": {"type": "uncommittedChanges"},
        },
    )
    turn_id = (review_response or {}).get("turn", {}).get("id")
    if not turn_id:
        raise AppServerError("review/start missing turn id")
    review_thread_id = (review_response or {}).get("reviewThreadId", thread_id)

    tracker = ReviewTracker(
        thread_id=review_thread_id,
        turn_id=turn_id,
        future=asyncio.get_running_loop().create_future(),
    )
    client.add_tracker(tracker)
    try:
        review_text = await asyncio.wait_for(tracker.future, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise timeout_error(timeout_seconds) from exc
    return parse_review_output(review_text)


async def run_reviews_threads(
    codex_bin: str,
    cwd: str,
    parallelism: int,
    timeout_seconds: int,
    model: Optional[str] = None,
    model_provider: Optional[str] = None,
    profile: Optional[str] = None,
) -> List[ReviewResult]:
    client = await AppServerClient.start(codex_bin, profile=profile)
    try:
        await client.initialize()
        tasks = [
            asyncio.create_task(
                run_single_review(
                    client,
                    cwd,
                    timeout_seconds,
                    model=model,
                    model_provider=model_provider,
                )
            )
            for _ in range(parallelism)
        ]
        results: List[ReviewResult] = []
        concurrency_error: Optional[Exception] = None
        for task in tasks:
            try:
                results.append(await task)
            except AppServerError as exc:
                if is_concurrency_error(str(exc)):
                    concurrency_error = exc
                    break
                raise
        if concurrency_error is not None:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise ConcurrencyNotSupported("app-server concurrency not supported") from concurrency_error
        return results
    finally:
        await client.close()


async def run_review_process(
    codex_bin: str,
    cwd: str,
    timeout_seconds: int,
    model: Optional[str] = None,
    model_provider: Optional[str] = None,
    profile: Optional[str] = None,
) -> ReviewResult:
    client = await AppServerClient.start(codex_bin, profile=profile)
    try:
        await client.initialize()
        return await run_single_review(
            client,
            cwd,
            timeout_seconds,
            model=model,
            model_provider=model_provider,
        )
    finally:
        await client.close()


async def run_reviews_processes(
    codex_bin: str,
    cwd: str,
    parallelism: int,
    timeout_seconds: int,
    model: Optional[str] = None,
    model_provider: Optional[str] = None,
    profile: Optional[str] = None,
) -> List[ReviewResult]:
    tasks = [
        asyncio.create_task(
            run_review_process(
                codex_bin,
                cwd,
                timeout_seconds,
                model=model,
                model_provider=model_provider,
                profile=profile,
            )
        )
        for _ in range(parallelism)
    ]
    return await asyncio.gather(*tasks)


async def run_reviews(
    codex_bin: str,
    cwd: str,
    parallelism: int,
    timeout_seconds: int,
    concurrency_mode: str,
    model: Optional[str] = None,
    model_provider: Optional[str] = None,
    profile: Optional[str] = None,
) -> List[ReviewResult]:
    if parallelism < 1:
        raise ValueError("parallelism must be >= 1")
    concurrency_mode = concurrency_mode.lower()
    if concurrency_mode == "threads":
        return await run_reviews_threads(
            codex_bin,
            cwd,
            parallelism,
            timeout_seconds,
            model=model,
            model_provider=model_provider,
            profile=profile,
        )
    if concurrency_mode == "processes":
        return await run_reviews_processes(
            codex_bin,
            cwd,
            parallelism,
            timeout_seconds,
            model=model,
            model_provider=model_provider,
            profile=profile,
        )
    if concurrency_mode != "auto":
        raise ValueError(f"unsupported concurrency mode: {concurrency_mode}")

    try:
        return await run_reviews_threads(
            codex_bin,
            cwd,
            parallelism,
            timeout_seconds,
            model=model,
            model_provider=model_provider,
            profile=profile,
        )
    except ConcurrencyNotSupported:
        return await run_reviews_processes(
            codex_bin,
            cwd,
            parallelism,
            timeout_seconds,
            model=model,
            model_provider=model_provider,
            profile=profile,
        )


def resolve_cwd(path: Optional[str]) -> str:
    if path:
        return os.path.abspath(path)
    return os.getcwd()


__all__ = [
    "AppServerError",
    "DISABLE_APPS_CONFIG_ASSIGNMENT",
    "DISABLE_COLLAB_CONFIG_ASSIGNMENT",
    "ConcurrencyNotSupported",
    "MCP_DISABLED_ERROR_TEXT",
    "MCP_SERVER_LIST_ATTEMPTS",
    "build_app_server_command",
    "is_concurrency_error",
    "is_mcp_tool_call_notification",
    "list_mcp_server_names_with_retry",
    "matches_thread_turn",
    "parse_mcp_server_names",
    "timeout_error",
    "run_reviews",
    "resolve_cwd",
    "ReviewResult",
]
