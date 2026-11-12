# loopocel-mcp

Plug-and-play Model Context Protocol (MCP) server, capture adapters, converter, validator, and ontology figure renderer for constructing **LoopOCEL** (OCEL 2.0) logs from live or recorded AI agent runs.

## Overview

`loopocel-mcp` records agent interactions into the nine-class, sixteen-relation LoopOCEL profile of OCEL 2.0 JSON (`Turn`, `Prompt`, `Response`, `Instruction`, `History`, `ActionRequest`, `Execution`, `ActionResult`, `ProcessObject`) and renders three figures (`SVG` and high-resolution `PNG`):

- **Agent-loop OC-DFG**: the architectural view, an object-centric directly-follows graph over LoopOCEL event types (one colored edge per object type).
- **Semantic process graph**: the domain view, the tool actions the agent took on business objects, in order, with failed actions, rework loops, and the final state of each business object kind.
- **Ontology coverage figure**: instance counts per LoopOCEL class, relation, and event type.

It supports four capture modes for any agent:

1. **Direct MCP recording (`mode="mcp"`)**: Any MCP-connected agent calls `session_start`, `record_turn`, `record_object`, and `session_end` while executing a task (guided by the built-in `record-task` MCP Prompt or the bundled [`skills/loopocel-logger/SKILL.md`](skills/loopocel-logger/SKILL.md) skill).
2. **Downstream MCP Server Wrapper (`loopocel-mcp wrap`)**: Transparent stdio middleware that wraps *any* existing MCP server and automatically logs every `tools/call` (`ActionRequest -> Execution -> ActionResult -> ProcessObject`) without spending LLM tokens.
3. **Hooks + Transcript (`mode="transcript"` / `claude-code-hooks`)**: Hooks (`PreToolUse`, `PostToolUse`, `Stop`, etc.) and transcript files (Claude Code JSONL, Antigravity `transcript.jsonl`, or OpenAI/Anthropic chat JSON) are ingested into `capture.jsonl`.
4. **LLM HTTP/SSE Recording Proxy (`mode="llm-proxy"`)**: Local HTTP reverse proxy (`ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`) supporting both JSON and `text/event-stream` (SSE) streaming responses.

---

## One Prompt: Both Process Graphs

With the server connected, use the `process-graphs` MCP prompt (in Claude Code: `/mcp__loopocel__process-graphs`):

- with a `task` argument, the agent executes and records the task, then shows both graphs;
- without arguments, it shows the graphs of the most recent recorded session.

A plain request works too, for example *"Use loopocel to record this task, then show me the process graphs"*: the agent calls `get_process_graphs`, which returns both images.

---

## Quick Start: Plug In Your Agent

### 1. Claude Desktop / Cursor / Windsurf (`mcp.json`)

Add `loopocel-mcp` to your MCP client configuration:

```json
{
  "mcpServers": {
    "loopocel": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/felipe-miamoto/loopocel-mcp",
        "loopocel-mcp",
        "--sessions-root",
        "./sessions",
        "serve"
      ]
    }
  }
}
```

Or from a local clone:

```json
{
  "mcpServers": {
    "loopocel": {
      "command": "python3",
      "args": ["-m", "loopocel_mcp", "--sessions-root", "./sessions", "serve"],
      "env": {
        "PYTHONPATH": "/path/to/loopocel-mcp/src"
      }
    }
  }
}
```

### 2. Claude Code CLI

```sh
claude mcp add loopocel -- uvx --from git+https://github.com/felipe-miamoto/loopocel-mcp loopocel-mcp
```

### 3. Install the Agent Skill (`SKILL.md`)

To teach any skill-compatible agent (Claude Code, Antigravity, Cursor, Codex) how to structure `ProcessObject` IDs, state changes, and turns automatically, copy [`skills/loopocel-logger/SKILL.md`](skills/loopocel-logger/SKILL.md) into your workspace's `.agents/skills/loopocel-logger/SKILL.md` (or `.claude/skills/`).

### 4. Wrap Any Existing MCP Server Passively (`loopocel-mcp wrap`)

Wrap any third-party MCP server in your `mcp.json` to automatically record all tool executions into a validated `loopocel.json` and render the coverage figure and both process graphs when the session closes:

```sh
loopocel-mcp wrap --session-dir ./sessions/fs_run --task "Workspace edits" -- npx -y @modelcontextprotocol/server-filesystem /path/to/workspace
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
PYTHONPATH=src python3 -m loopocel_mcp wrap --session-dir sessions/wrapped -- <downstream-mcp-cmd>

# Run local HTTP/SSE LLM recording proxy
PYTHONPATH=src python3 -m loopocel_mcp proxy --session-dir sessions/proxy_01 --upstream https://api.anthropic.com --port 18080

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
