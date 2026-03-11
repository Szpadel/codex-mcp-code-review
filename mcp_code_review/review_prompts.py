# Drift note:
# This mirrors only the native uncommitted-changes review target prompt from:
# `.agent/codex/codex-rs/core/src/review_prompts.rs` at commit
# `6a673e7339161cf5aa6711324bfe873846234b6b`.
#
# Local alteration:
# - append `Additional review instructions` when this MCP server is called with
#   per-run `additional_developer_instructions`
UNCOMMITTED_CHANGES_REVIEW_PROMPT = (
    "Review the current code changes (staged, unstaged, and untracked files) "
    "and provide prioritized findings."
)


def build_uncommitted_changes_review_prompt() -> str:
    return UNCOMMITTED_CHANGES_REVIEW_PROMPT


def append_additional_review_instructions(
    prompt: str,
    additional_instructions: str,
) -> str:
    return f"{prompt}\n\nAdditional review instructions:\n{additional_instructions.strip()}"
