# Codex MCP Code Review

Run Codex app-server reviews of uncommitted changes via an MCP tool.

## Requirements

- Codex CLI installed and authenticated.
- uv installed.

## Run locally with uv

From the repo root:

```bash
uv run -m mcp_code_review.server
```

Common flags:

```bash
uv run -m mcp_code_review.server --parallelism 4 --concurrency-mode auto --timeout-seconds 2700
```

## Configure in Codex (MCP)

Codex loads MCP servers from `~/.codex/config.toml` and supports configuring them via the `codex mcp` CLI.

### Option A: CLI (recommended)

This registers a stdio MCP server that Codex launches when a session starts.

```bash
codex mcp add codex-code-review -- \
  uv run -m mcp_code_review.server --parallelism 4 --concurrency-mode auto --timeout-seconds 2700
```

The `codex mcp add` workflow is the supported way to add MCP servers from the CLI.

### Option B: config.toml

Add an MCP server entry in `~/.codex/config.toml`:

```toml
[mcp_servers.codex-code-review]
command = "uv"
args = [
  "run",
  "-m",
  "mcp_code_review.server",
  "--parallelism",
  "4",
  "--concurrency-mode",
  "auto",
  "--timeout-seconds",
  "2700"
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
  python -m mcp_code_review.server --parallelism 4 --concurrency-mode auto --timeout-seconds 2700
```

### Codex (config.toml)

```toml
[mcp_servers.codex-code-review-uvx]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/Szpadel/codex-mcp-code-review",
  "python",
  "-m",
  "mcp_code_review.server",
  "--parallelism",
  "4",
  "--concurrency-mode",
  "auto",
  "--timeout-seconds",
  "2700"
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
        "4",
        "--concurrency-mode",
        "auto",
        "--timeout-seconds",
        "2700"
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
  --parallelism 4 --concurrency-mode auto --timeout-seconds 2700
```

### config.toml

```toml
[mcp_servers.codex-code-review-dev]
command = "uv"
args = [
  "run",
  "--project",
  "/absolute/path/to/codex-mcp-code-review",
  "-m",
  "mcp_code_review.server",
  "--parallelism",
  "4",
  "--concurrency-mode",
  "auto",
  "--timeout-seconds",
  "2700"
]
```

## Tool behavior

- Tool name: `review_uncommitted_changes`.
- Uses the native app-server review target `uncommittedChanges` (includes untracked files).
- Default runs: 4.
- Sandbox: read-only; approval policy: never.
