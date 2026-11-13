# loopocel-mcp

Plug-and-play Model Context Protocol (MCP) server, capture adapters, converter, validator, and ontology figure renderer for constructing **LoopOCEL** (OCEL 2.0) logs from live or recorded AI agent runs.

## Overview

`loopocel-mcp` records agent interactions into the nine-class, sixteen-relation LoopOCEL profile of OCEL 2.0 JSON (`Turn`, `Prompt`, `Response`, `Instruction`, `History`, `ActionRequest`, `Execution`, `ActionResult`, `ProcessObject`) and renders three figures (`SVG` and high-resolution `PNG`):

- **Agent-loop OC-DFG**: the architectural view, an object-centric directly-follows graph over LoopOCEL event types (one colored edge per object type).
- **Semantic process graph**: the domain view, the tool actions the agent took on business objects, in order, with failed actions, rework loops, and the final state of each business object kind.
- **Ontology coverage figure**: instance counts per LoopOCEL class, relation, and event type.

It supports four capture modes for any agent:

1. **Direct MCP recording (`mode="mcp"`)**: Any MCP-connected agent calls `session_start`, `record_turn`, `record_object`, and `session_end` while executing a task (guided by the built-in `record-task` MCP Prompt or the bundled [`skills/loopocel-logger/SKILL.md`](skills/loopocel-logger/SKILL.md) skill).
2. **Downstream MCP Server Wrapper (`loopocel-mcp wrap`)**: Transparent stdio middleware that wraps *any* existing MCP server and automatically logs every `tools/call` (`ActionRequest -> Execution -> ActionResult -> ProcessObject`) without spending LLM tokens. Each start of the wrapped server is recorded as its own session (a new timestamped folder under `--sessions-root`), so it can stay configured in `mcp.json` for every agent session.
3. **Hooks + Transcript (`mode="transcript"` / `claude-code-hooks`)**: Hooks (`PreToolUse`, `PostToolUse`, `Stop`, etc.) and transcript files (Claude Code JSONL, Antigravity `transcript.jsonl`, or OpenAI/Anthropic chat JSON) are ingested into `capture.jsonl`. With hooks, `--session-dir` is a root: every Claude Code session gets its own folder, `Stop` keeps the log converted and validated, and `SessionEnd` also renders the figures.
4. **LLM HTTP/SSE Recording Proxy (`mode="llm-proxy"`)**: Local HTTP reverse proxy (`ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`) for agentic workflows built directly on the model APIs: Anthropic Messages, OpenAI Chat Completions, and the OpenAI Responses API (used by the OpenAI Agents SDK), with JSON or streamed (SSE) responses passed through as they arrive. Stopping the proxy (Ctrl+C) converts, validates, and renders the session.

---

## One Prompt: Both Process Graphs

With the server connected, use the `process-graphs` MCP prompt (in Claude Code: `/mcp__loopocel__process-graphs`):

- with a `task` argument, the agent executes and records the task, then shows both graphs;
- without arguments, it shows the graphs of the most recent recorded session.

A plain request works too, for example *"Use loopocel to record this task, then show me the process graphs"*: the agent calls `get_process_graphs`, which returns both images.

### Live recording only

The server tells every connected agent (through its MCP `instructions`, tool descriptions, and prompts) to record any multi-step task while doing it with its normal tools: real tool names, real arguments, results taken from real tool output, one `record_turn` per cycle. A plain task such as *"Plan a round-trip from Frankfurt to Copenhagen under €800 with luggage and free cancellation"* is enough once the server is connected.

`session_end` returns a `provenance` block (`recorded_via`, turn timing, `live`). Sessions written by a script that imports `loopocel_mcp`, or whose turns all arrive within one second, are flagged as not live, and both process graphs show the warning in red.

After updating the server, restart or reload the MCP server in your agent (Antigravity caches tool schemas) so it picks up `get_process_graphs` and the new instructions.

---

## Quick Start: Plug In Your Agent

The same steps, with troubleshooting, are in [`skills/loopocel-logger/SKILL.md`](skills/loopocel-logger/SKILL.md#setup-first-time-only), so agents that load the skill can walk you through them.

### Set Up by Prompt

In an agent that can run shell commands (Claude Code, the Code tab of Claude Desktop, Codex, Cursor, Antigravity), paste:

> Set up the loopocel MCP server for Claude Desktop: clone https://github.com/felipe-miamoto/loopocel-mcp into ~/loopocel-mcp and follow the "Setup (first time only)" section of skills/loopocel-logger/SKILL.md. Keep my existing Claude Desktop settings, run the smoke test, and tell me when to restart.

Replace "Claude Desktop" with the client you use. The manual steps follow.

### 1. Install

```sh
git clone https://github.com/felipe-miamoto/loopocel-mcp.git
cd loopocel-mcp
uv venv --python 3.12 && uv pip install -e .     # or: python3.12 -m venv .venv && .venv/bin/pip install -e .
```

This creates the server command `<REPO>/.venv/bin/loopocel-mcp`, where `<REPO>` is the absolute path of the clone (`pwd`). Use absolute paths in every client configuration: desktop apps start MCP servers from `/` and without your shell `PATH`. Sessions are stored under `--sessions-root`, here `<REPO>/sessions`.

### 2. Register the Server

**Claude Desktop** (Settings → Developer → Edit Config), **Cursor** (`~/.cursor/mcp.json`), **Gemini CLI** (`~/.gemini/settings.json`), **Antigravity** (MCP server settings or `.agents/plugins/loopocel-mcp/mcp_config.json`):

```json
{
  "mcpServers": {
    "loopocel": {
      "command": "<REPO>/.venv/bin/loopocel-mcp",
      "args": ["--sessions-root", "<REPO>/sessions", "serve"]
    }
  }
}
```

If the file already has other settings, add only the `"mcpServers"` entry next to them.

**Claude Code** (add `--scope user` to make it available in every project):

```sh
claude mcp add loopocel -- <REPO>/.venv/bin/loopocel-mcp --sessions-root <REPO>/sessions serve
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.loopocel]
command = "<REPO>/.venv/bin/loopocel-mcp"
args = ["--sessions-root", "<REPO>/sessions", "serve"]
```

### 3. Restart and Verify

1. Restart the client completely (Claude Desktop: Cmd+Q and reopen; Claude Code: new session; Antigravity: refresh the MCP server, which caches tool lists).
2. Check that `loopocel` is connected with 10 tools, including `get_process_graphs` (Claude Code: `claude mcp list`).
3. Turn on web search or whatever tools the task needs: loopocel only records, the agent still does the work with its own tools.
4. After a recorded task, `session_end` should report `provenance.recorded_via: "mcp-stdio"` and `provenance.live: true`.

Smoke test without a client:

```sh
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | <REPO>/.venv/bin/loopocel-mcp --sessions-root /tmp/loopocel-check serve
```

### 4. Install the Agent Skill (`SKILL.md`)

To teach any skill-compatible agent (Claude Code, Antigravity, Cursor, Codex) how to structure `ProcessObject` IDs, state changes, and turns automatically, copy [`skills/loopocel-logger/SKILL.md`](skills/loopocel-logger/SKILL.md) into your workspace's `.agents/skills/loopocel-logger/SKILL.md` (or `.claude/skills/`).

### 5. Wrap Any Existing MCP Server Passively (`loopocel-mcp wrap`)

Wrap any third-party MCP server in your `mcp.json` to automatically record all tool executions into a validated `loopocel.json` and render the coverage figure and both process graphs when the session closes:

```sh
loopocel-mcp --sessions-root <REPO>/sessions wrap --task "Workspace edits" -- npx -y @modelcontextprotocol/server-filesystem /path/to/workspace
```

---

## MCP Capabilities

### Tools

| Tool | Description |
|---|---|
| `session_start` | Open a recording session, optionally registering initial `ProcessObject` instances and base `Instruction`. |
| `record_turn` | Record a model interaction `Turn` (`Prompt`, `Instruction`/`History`, `Response`, and `ActionRequest -> Execution -> ActionResult -> ProcessObject` chains). |
| `record_object` | Register or update a business `ProcessObject` (`Registered`, `Created`, or `StateChanged`). |
| `ingest_transcript` | Ingest an agent transcript file into the session. |
| `session_end` | Finalize the session, convert `capture.jsonl` to `loopocel.json`, validate against the OCEL 2.0 schema and LoopOCEL constraints, and render the coverage figure and both process graphs. |
| `get_log` | Return `loopocel.json` (`ocel-json`) or `capture.jsonl` (`capture-jsonl`). |
| `get_figure` | Render and return the LoopOCEL coverage figure (`png` or `svg`, including an MCP image content block), with optional multi-session side-by-side comparison. |
| `get_process_graphs` | Return the agent-loop OC-DFG and the semantic process graph as images plus their discovered structure (defaults to the most recent session; `include_sessions` aggregates runs; `view` selects `ocdfg`, `semantic`, or `both`). |
| `get_summary` | Return instance counts per class, O2O relation, and event type, plus coverage and validation status. |
| `list_sessions` | List recorded and active sessions. |

### Prompts

| Prompt | Description |
|---|---|
| `record-task` | Injects step-by-step instructions guiding the connected agent to record every turn, tool execution, and `ProcessObject` mutation for a given `task`. |
| `process-graphs` | One-line entry point: optionally record a `task`, then show the agent-loop OC-DFG and the semantic process graph. |
| `ingest-and-analyze` | Guides the agent to ingest an existing transcript file, validate OCEL 2.0 compliance, and render the coverage figure. |

### Resources

- `loopocel://sessions/{session_id}/log` (`application/json`)
- `loopocel://sessions/{session_id}/figure` (`image/svg+xml`)
- `loopocel://sessions/{session_id}/summary` (`application/json`)
- `loopocel://sessions/{session_id}/ocdfg` (`image/svg+xml`)
- `loopocel://sessions/{session_id}/semantic` (`image/svg+xml`)

---

## CLI Usage

```sh
# Run MCP server over stdio (default)
PYTHONPATH=src python3 -m loopocel_mcp serve --transport stdio

# Run MCP server over HTTP
PYTHONPATH=src python3 -m loopocel_mcp serve --transport http --port 8765

# Wrap a downstream stdio MCP server
PYTHONPATH=src python3 -m loopocel_mcp --sessions-root sessions wrap --task "<task>" -- <downstream-mcp-cmd>

# Run local HTTP/SSE LLM recording proxy
PYTHONPATH=src python3 -m loopocel_mcp --sessions-root sessions proxy --task "<task>" --upstream https://api.anthropic.com --port 18080
# then: ANTHROPIC_BASE_URL=http://127.0.0.1:18080  (or OPENAI_BASE_URL=http://127.0.0.1:18080/v1 with --upstream https://api.openai.com)

# Ingest an existing transcript file
PYTHONPATH=src python3 -m loopocel_mcp ingest sessions/ingested_01 /path/to/transcript.jsonl

# Convert, validate, and render a session directory
PYTHONPATH=src python3 -m loopocel_mcp convert sessions/<session_id>
PYTHONPATH=src python3 -m loopocel_mcp validate sessions/<session_id> --pm4py
PYTHONPATH=src python3 -m loopocel_mcp figure sessions/<session_id>
PYTHONPATH=src python3 -m loopocel_mcp graphs sessions/<session_id> [--include sessions/<other_id> ...]

# Run test suite
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
