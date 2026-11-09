# ocel4agent-mcp

MCP server, capture adapters, converter, validator, and coverage figure renderer for constructing **LoopOCEL** (OCEL 2.0) logs from live or recorded agent runs.

## Overview

`ocel4agent-mcp` records agent interactions into the nine-class, sixteen-relation LoopOCEL profile of OCEL 2.0 JSON (`Turn`, `Prompt`, `Response`, `Instruction`, `History`, `ActionRequest`, `Execution`, `ActionResult`, `ProcessObject`) and renders an ontology coverage and instance count figure (`SVG` and `PNG`).

It supports four capture modes for any agent:

1. **Direct MCP recording (`mode="mcp"`)**: Any MCP-connected agent calls `session_start`, `record_turn`, `record_object`, and `session_end` while executing a task.
2. **Hooks + Transcript (`mode="transcript"` / `claude-code-hooks`)**: Hooks (`PreToolUse`, `PostToolUse`, `Stop`, etc.) and transcript files (Claude Code JSONL, Antigravity `transcript.jsonl`, or chat JSON) are ingested into `capture.jsonl`.
3. **LLM HTTP Recording Proxy (`mode="llm-proxy"`)**: Local HTTP proxy (`ANTHROPIC_BASE_URL` or OpenAI-compatible base URL) capturing full-fidelity `Prompt`, `Instruction`, `History`, `Response`, `ActionRequest`, `Execution`, and `ActionResult` records.
4. **Downstream Tool Proxy (`mode="tool-proxy"`)**: Wraps tool execution boundaries into `ActionRequest -> Execution -> ActionResult` chains.

## MCP Tools

| Tool | Description |
|---|---|
| `session_start` | Open a recording session, optionally registering initial `ProcessObject` instances and base `Instruction`. |
| `record_turn` | Record a model interaction `Turn` (`Prompt`, `Instruction`/`History`, `Response`, and `ActionRequest -> Execution -> ActionResult -> ProcessObject` chains). |
| `record_object` | Register or update a business `ProcessObject` (`Registered`, `Created`, or `StateChanged`). |
| `ingest_transcript` | Ingest an agent transcript file into the session. |
| `session_end` | Finalize the session, convert `capture.jsonl` to `loopocel.json`, validate against the OCEL 2.0 schema and LoopOCEL constraints, and render `figure.svg` and `figure.png`. |
| `get_log` | Return `loopocel.json` (`ocel-json`) or `capture.jsonl` (`capture-jsonl`). |
| `get_figure` | Render and return the LoopOCEL coverage figure (`png` or `svg`, including an MCP image content block), with optional multi-session side-by-side comparison. |
| `get_summary` | Return instance counts per class, O2O relation, and event type, plus coverage and validation status. |
| `list_sessions` | List recorded and active sessions. |

## CLI Usage

```sh
# Run MCP server over stdio
PYTHONPATH=src python3 -m ocel4agent_mcp serve --transport stdio

# Run MCP server over HTTP
PYTHONPATH=src python3 -m ocel4agent_mcp serve --transport http --port 8765

# Convert and validate a session directory
PYTHONPATH=src python3 -m ocel4agent_mcp convert sessions/<session_id>
PYTHONPATH=src python3 -m ocel4agent_mcp validate sessions/<session_id>
PYTHONPATH=src python3 -m ocel4agent_mcp figure sessions/<session_id>

# Run test suite
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
