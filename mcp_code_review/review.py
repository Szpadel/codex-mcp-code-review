from dataclasses import dataclass
import json
import re
from typing import Optional


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    summary: str
    comment_markdown: str


SEVERITY_TAG_PATTERN = re.compile(r"\[P(\d+)\]", re.IGNORECASE)


def extract_severity_tags(text: str) -> list[str]:
    if not text:
        return []
    severities: set[int] = set()
    for match in SEVERITY_TAG_PATTERN.finditer(text):
        try:
            severities.add(int(match.group(1)))
        except ValueError:
            continue
    return [f"P{severity}" for severity in sorted(severities)]


def extract_json_block(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    end = text.rfind("}")
    if end == -1 or end < start:
        return None
    return text[start : end + 1]


def parse_review_output(text: str) -> ReviewResult:
    trimmed = text.strip()
    if not trimmed:
        return ReviewResult(verdict="pass", summary="no issues found", comment_markdown="")

    json_text = extract_json_block(trimmed)
    if json_text is not None:
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            verdict = parsed.get("verdict")
            summary = parsed.get("summary")
            comment_markdown = parsed.get("comment_markdown", "")
            if isinstance(verdict, str) and isinstance(summary, str):
                verdict_norm = verdict.strip().lower()
                if verdict_norm in {"pass", "comment"}:
                    return ReviewResult(
                        verdict=verdict_norm,
                        summary=summary,
                        comment_markdown=str(comment_markdown),
                    )

    summary = "Codex review"
    for line in trimmed.splitlines():
        if line.strip():
            summary = line.strip()
            break
    return ReviewResult(verdict="comment", summary=summary, comment_markdown=trimmed)


def default_review_instructions() -> str:
    return (
        "You are a senior code reviewer. Review only the uncommitted changes and return JSON only.\n\n"
        "Return JSON with fields verdict (pass or comment), summary, and comment_markdown. "
        "If verdict is pass, comment_markdown can be an empty string."
    )
