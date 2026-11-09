---
name: ocel4agent-logger
description: Record autonomous AI agent executions into validated LoopOCEL (OCEL 2.0 JSON) logs and render ontology coverage diagrams using ocel4agent-mcp. Use when asked to log an agent task as OCEL 2.0 / LoopOCEL, ingest an agent transcript, wrap a downstream MCP server, or visualize LoopOCEL ontology coverage and instance cardinalities.
---

# OCEL4Agent-MCP Instrumentation Skill

This skill teaches an agent how to record its own execution—or convert an existing transcript/session—into a validated **LoopOCEL (OCEL 2.0 JSON)** object-centric event log and ontology coverage diagram using **`ocel4agent-mcp`**.

---

## 1. Choose the Right Capture Mode

`ocel4agent-mcp` supports four capture modes depending on your setup:

1. **Direct MCP Tool Recording (`mode="mcp"`)**:
   - Use when `ocel4agent-mcp` is connected as an MCP server and you want to actively record each turn and domain `ProcessObject` state change as you execute a task.
2. **Post-Hoc Transcript Ingestion (`mode="transcript"`)**:
   - Use when an agent has already completed a run (Claude Code JSONL, Antigravity `transcript.jsonl`, or OpenAI/Anthropic chat JSON) and you want to convert its transcript into `loopocel.json` + `figure.svg`/`figure.png`.
3. **Downstream MCP Server Wrapper (`ocel4agent-mcp wrap`)**:
   - Use when you want zero-token passive recording of all `tools/call` executions sent to another MCP server.
4. **Streaming HTTP LLM Proxy (`ocel4agent-mcp proxy`)**:
   - Use when you want passive full-fidelity capture of raw LLM requests/responses (including SSE `text/event-stream` chunks) via `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`.

---

## 2. Mode 1: Direct MCP Tool Recording Protocol (`mode="mcp"`)

When executing a user task while recording via `ocel4agent-mcp` tools, follow this exact 3-step lifecycle:

### Step 1: Open the Session (`session_start`)
Before executing the first domain action, call `session_start`:

```json
{
  "session_id": "my_task_01",
  "task": "Concise description of the user task",
  "agent": "agent-name",
  "profile": "domain-name",
  "mode": "mcp",
  "instruction": "Full user prompt or instruction text",
  "initial_objects": {
    "trip:01": {
      "origin": "Munich",
      "destination": "Vienna",
      "status": "requested"
    }
  }
}
```
- **Object Naming Convention**: Always prefix `ProcessObject` IDs with `<kind>:<identifier>` (e.g., `trip:01`, `flight:OS112`, `hotel:motel-one`, `file:src/main.py`, `issue:42`).

### Step 2: Record Every Turn (`record_turn`)
After each reasoning step and tool execution cycle, call `record_turn`:

```json
{
  "session_id": "my_task_01",
  "response_message": "Searched flights and verified budget constraints.",
  "parameters": {"model": "agent-model"},
  "actions": [
    {
      "name": "search_flights",
      "arguments": {"origin": "MUC", "destination": "VIE"},
      "outcome": "success",
      "result": {"candidates": [{"flight_id": "OS112", "price_eur": 210}]},
      "targets": ["trip:01"],
      "acted_on": ["trip:01", "flight:OS112"],
      "describes": ["trip:01", "flight:OS112"],
      "changes": {
        "flight:OS112": {
          "state": {"carrier": "Austrian", "price_eur": 210, "status": "candidate"},
          "created": true
        }
      }
    }
  ]
}
```

#### Rules for High-Fidelity `actions` Entries:
- `outcome`: Set to `"success"` when the tool call succeeds or `"rejected"` when a tool call or constraint check fails (this enables rework loop discovery!).
- `targets`: List of `ProcessObject` IDs referenced in the tool's input arguments (`ActionRequest.targetsObject`).
- `acted_on`: List of `ProcessObject` IDs touched during execution (`Execution.actOn`).
- `describes`: List of `ProcessObject` IDs described in the tool's output (`ActionResult.describesObject`).
- `changes`: Map of `ProcessObject` IDs whose state was created (`"created": true` on first discovery) or mutated (`"created": false` on state update, emitting `ProcessObject.StateChanged`).

### Step 3: Finalize, Validate, and Render (`session_end` & `get_figure`)
Once the task finishes:
1. Call `session_end({"session_id": "my_task_01"})`:
   - Converts `capture.jsonl` $\to$ `loopocel.json`
   - Runs two-stage verification (OCEL 2.0 JSON Schema draft-07 + LoopOCEL relational cardinality check)
   - Renders `figure.svg` and `figure.png`
2. Call `get_figure({"session_id": "my_task_01", "format": "png"})` (or `"compare_with": ["other_session"]` for side-by-side cohort comparison).

---

## 3. Mode 2: Post-Hoc Transcript Ingestion (`mode="transcript"`)

To convert an existing agent transcript file without re-running the task:

1. Via MCP tools:
   - Call `session_start({"session_id": "run_01", "task": "Task name", "mode": "transcript"})`
   - Call `session_end({"session_id": "run_01", "transcript_path": "/path/to/transcript.jsonl"})`
2. Or via CLI:
   ```sh
   ocel4agent-mcp ingest sessions/run_01 /path/to/transcript.jsonl --task "Task name"
   ```

---

## 4. Mode 3 & 4: Passive Wrapping & Streaming Proxy

- **Wrap any stdio MCP server**:
  ```sh
  ocel4agent-mcp wrap --session-dir sessions/wrapped_run --task "Filesystem task" -- npx -y @modelcontextprotocol/server-filesystem /workspace
  ```
- **Run local HTTP/SSE LLM proxy**:
  ```sh
  ocel4agent-mcp proxy --session-dir sessions/proxy_run --upstream https://api.anthropic.com --port 18080
  ```
  Then point `ANTHROPIC_BASE_URL=http://127.0.0.1:18080` (or `OPENAI_BASE_URL`) before running your agent.

