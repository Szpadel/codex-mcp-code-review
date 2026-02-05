# Codex MCP Code Review

Run Codex app-server reviews of uncommitted changes via an MCP tool. Compared to the built-in `/review`, this keeps review context clean while fixes happen in the main session that retains implementation knowledge, reducing regressions and enabling longer autonomous runs with better code quality.

## Requirements

- Codex CLI installed and authenticated.
- uv installed.

## Configure Codex (MCP)

Codex loads MCP servers from `~/.codex/config.toml` and supports configuring them via the `codex mcp` CLI.

### Review profile (recommended)

Profile example (gpt-5.3-codex, xhigh reasoning effort):

```toml
# ~/.codex/config.toml
[profiles.review]
model = "gpt-5.3-codex"
model_reasoning_effort = "xhigh"
```

AGENTS.md example instruction:

```text
for verification call `review_uncommitted_changes: runs=1` until no issues
```

### Tool timeout (required)

Set `tool_timeout_sec` for the MCP server in `~/.codex/config.toml` to a value larger than the review timeout (`--timeout-seconds` passed to the server command). Example: if the review timeout is 2700 seconds, set `tool_timeout_sec = 3000`.

### Option A: CLI (recommended)

This registers a stdio MCP server that Codex launches when a session starts.

```bash
codex mcp add codex-code-review -- \
  uv run -m mcp_code_review.server --parallelism 1 --concurrency-mode auto --timeout-seconds 2700 \
  --profile review
```

The `codex mcp add` workflow is the supported way to add MCP servers from the CLI. After adding the server, set `tool_timeout_sec` in `~/.codex/config.toml` (see Option B) so it is higher than `--timeout-seconds`.

### Option B: config.toml

Add an MCP server entry in `~/.codex/config.toml`:

```toml
[mcp_servers.codex-code-review]
command = "uv"
tool_timeout_sec = 3000
args = [
  "run",
  "-m",
  "mcp_code_review.server",
  "--parallelism",
  "1",
  "--concurrency-mode",
  "auto",
  "--timeout-seconds",
  "2700",
  "--profile",
  "review"
]
```

Codex reads MCP server entries from the `mcp_servers` table in `~/.codex/config.toml`.

## Configure with uvx (run directly from Git)

If you prefer not to clone locally, you can run the server directly from the Git repository using `uvx`.

Repository: `https://github.com/Szpadel/codex-mcp-code-review`

### Codex (CLI)

```bash
codex mcp add codex-code-review-uvx -- \
  uvx --from git+https://github.com/Szpadel/codex-mcp-code-review \
  python -m mcp_code_review.server --parallelism 1 --concurrency-mode auto --timeout-seconds 2700 \
  --profile review
```

### Codex (config.toml)

```toml
[mcp_servers.codex-code-review-uvx]
command = "uvx"
tool_timeout_sec = 3000
args = [
  "--from",
  "git+https://github.com/Szpadel/codex-mcp-code-review",
  "python",
  "-m",
  "mcp_code_review.server",
  "--parallelism",
  "1",
  "--concurrency-mode",
  "auto",
  "--timeout-seconds",
  "2700",
  "--profile",
  "review"
]
```

### Claude Code (.mcp.json)

```json
{
  "mcpServers": {
    "codex-code-review-uvx": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Szpadel/codex-mcp-code-review",
        "python",
        "-m",
        "mcp_code_review.server",
        "--parallelism",
        "1",
        "--concurrency-mode",
        "auto",
        "--timeout-seconds",
        "2700",
        "--profile",
        "review"
      ],
      "env": {}
    }
  }
}
```

## Developer mode (run from source path)

If you want Codex to run the server directly from this source checkout, point `uv` at the project path.

### CLI

```bash
codex mcp add codex-code-review-dev -- \
  uv run --project /absolute/path/to/codex-mcp-code-review -m mcp_code_review.server \
  --parallelism 1 --concurrency-mode auto --timeout-seconds 2700 --profile review
```

### config.toml

```toml
[mcp_servers.codex-code-review-dev]
command = "uv"
tool_timeout_sec = 3000
args = [
  "run",
  "--project",
  "/absolute/path/to/codex-mcp-code-review",
  "-m",
  "mcp_code_review.server",
  "--parallelism",
  "1",
  "--concurrency-mode",
  "auto",
  "--timeout-seconds",
  "2700",
  "--profile",
  "review"
]
```

## Tool behavior

- Tool name: `review_uncommitted_changes`.
- Uses the native app-server review target `uncommittedChanges` (includes untracked files).
- Default runs: 4 (override by setting `--parallelism` on the MCP server config).
- Sandbox: read-only; approval policy: never.
