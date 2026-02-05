import os
from typing import Iterable, Iterator, Optional


SPAWN_GUARD_CONFIG_KEY = "codex_mcp_code_review_spawn_guard"
SPAWN_GUARD_CONFIG_VALUE = "true"
SPAWN_GUARD_CONFIG_ASSIGNMENT = f"{SPAWN_GUARD_CONFIG_KEY}={SPAWN_GUARD_CONFIG_VALUE}"


def is_guarded_app_server_cmdline(cmdline: str) -> bool:
    if not cmdline:
        return False
    lowered = cmdline.lower()
    return "app-server" in lowered and SPAWN_GUARD_CONFIG_ASSIGNMENT in lowered


def find_guarded_app_server_in_cmdlines(cmdlines: Iterable[str]) -> Optional[str]:
    for cmdline in cmdlines:
        if is_guarded_app_server_cmdline(cmdline):
            return cmdline
    return None


def read_proc_cmdline(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if not data:
        return None
    return data.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()


def read_proc_parent_pid(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith("PPid:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1])
    except OSError:
        return None
    return None


def iter_ancestor_cmdlines(max_depth: int = 16) -> Iterator[str]:
    pid = os.getpid()
    for _ in range(max_depth):
        cmdline = read_proc_cmdline(pid)
        if cmdline:
            yield cmdline

        parent_pid = read_proc_parent_pid(pid)
        if parent_pid is None or parent_pid <= 0 or parent_pid == pid:
            break
        pid = parent_pid


def find_guarded_app_server_ancestor_cmdline(max_depth: int = 16) -> Optional[str]:
    return find_guarded_app_server_in_cmdlines(iter_ancestor_cmdlines(max_depth=max_depth))


__all__ = [
    "SPAWN_GUARD_CONFIG_ASSIGNMENT",
    "SPAWN_GUARD_CONFIG_KEY",
    "SPAWN_GUARD_CONFIG_VALUE",
    "is_guarded_app_server_cmdline",
    "find_guarded_app_server_in_cmdlines",
    "find_guarded_app_server_ancestor_cmdline",
]
