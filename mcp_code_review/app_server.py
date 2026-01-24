import asyncio
import contextlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mcp_code_review.review import ReviewResult, default_review_instructions, parse_review_output


class AppServerError(RuntimeError):
    pass


class ConcurrencyNotSupported(AppServerError):
    pass


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
        self._reader_task = asyncio.create_task(self._reader_loop())

    @classmethod
    async def start(
        cls,
        codex_bin: str,
        env: Optional[Dict[str, str]] = None,
        profile: Optional[str] = None,
    ) -> "AppServerClient":
        cmd = [codex_bin]
        if profile:
            # App-server only reads config overrides, so set the active profile via -c.
            cmd.extend(["-c", f"profile={profile}"])
        cmd.append("app-server")
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
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)
        return await future

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    def add_tracker(self, tracker: ReviewTracker) -> None:
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
        if not self._trackers:
            return
        for tracker in list(self._trackers):
            tracker.handle_notification(message)
            if tracker.future.done():
                self._trackers.remove(tracker)


async def run_single_review(
    client: AppServerClient,
    cwd: str,
    timeout_seconds: int,
    model: Optional[str] = None,
    model_provider: Optional[str] = None,
    instructions: Optional[str] = None,
) -> ReviewResult:
    instructions = instructions or default_review_instructions()
    thread_params: Dict[str, Any] = {
        "cwd": cwd,
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "baseInstructions": instructions,
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
    instructions: Optional[str] = None,
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
                    instructions=instructions,
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
    instructions: Optional[str] = None,
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
            instructions=instructions,
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
    instructions: Optional[str] = None,
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
                instructions=instructions,
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
    instructions: Optional[str] = None,
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
            instructions=instructions,
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
            instructions=instructions,
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
            instructions=instructions,
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
            instructions=instructions,
            profile=profile,
        )


def resolve_cwd(path: Optional[str]) -> str:
    if path:
        return os.path.abspath(path)
    return os.getcwd()


__all__ = [
    "AppServerError",
    "ConcurrencyNotSupported",
    "is_concurrency_error",
    "matches_thread_turn",
    "timeout_error",
    "run_reviews",
    "resolve_cwd",
    "ReviewResult",
]
