---
name: loopocel-logger
description: Record autonomous AI agent executions live into validated LoopOCEL (OCEL 2.0 JSON) logs and show the agent-loop OC-DFG and semantic process graph using loopocel-mcp. Use when asked to install, set up, or connect loopocel-mcp to an agent (Claude Desktop, Claude Code, Codex, Cursor, Antigravity, Gemini CLI); whenever the loopocel MCP server is connected and the user gives a multi-step task (planning, searching, booking, coding, data work), when asked to log an agent task as OCEL 2.0 / LoopOCEL, ingest an agent transcript, wrap a downstream MCP server, or show process graphs or ontology coverage of a run.
---

# LoopOCEL-MCP Instrumentation Skill

This skill teaches an agent how to record its own execution—or convert an existing transcript/session—into a validated **LoopOCEL (OCEL 2.0 JSON)** object-centric event log and ontology coverage diagram using **`loopocel-mcp`**.

---

## Setup (first time only)

The server must be registered with the agent before a task starts. Its tools (`session_start`, `record_turn`, `session_end`, `get_process_graphs` ...) then appear in the agent's own tool list. If they are not there, set the server up with the steps below, or ask the user to, instead of driving the code another way.

### When the user asks you to set it up

If you can run shell commands and edit files (Claude Code, the Code tab of Claude Desktop, Codex, Cursor, Antigravity), do the setup yourself:

1. Clone the repository where the user asks (default `~/loopocel-mcp`); if a clone already exists there, reuse it. Install it as in Step A.
2. Register the server for the client the user names, as in Step B. When editing an existing JSON or TOML config, read it first, keep every existing key, add only the `loopocel` entry, and save a backup copy next to it (`<file>.bak`) before writing.
3. Run the smoke test in Step C and confirm it lists 10 tools.
4. Tell the user exactly what to restart, and that after the restart the client should show `loopocel` with 10 tools. A running client does not pick up a new server until it restarts.

If you cannot run commands (for example a plain chat), give the user these steps with the paths filled in instead.

### Step A: Install

```sh
git clone https://github.com/felipe-miamoto/loopocel-mcp.git
cd loopocel-mcp
uv venv --python 3.12 && uv pip install -e .     # or: python3.12 -m venv .venv && .venv/bin/pip install -e .
```

This puts a `loopocel-mcp` command in `.venv/bin/`. Note two absolute paths; every client below needs them, because desktop apps start servers from `/` and without your shell `PATH`:

- `<REPO>`: the absolute path of the clone (`pwd` inside it)
- `<REPO>/.venv/bin/loopocel-mcp`: the server command

Sessions are stored in `<REPO>/sessions` (any absolute folder works).

### Step B: Register the server in your agent

**Claude Desktop**: Settings → Developer → Edit Config opens `claude_desktop_config.json`. Add an `mcpServers` entry next to the existing keys:

```json
"mcpServers": {
  "loopocel": {
    "command": "<REPO>/.venv/bin/loopocel-mcp",
    "args": ["--sessions-root", "<REPO>/sessions", "serve"]
  }
}
```

**Claude Code** (run in the project where you will work; `--scope user` makes it available everywhere):

```sh
claude mcp add loopocel -- <REPO>/.venv/bin/loopocel-mcp --sessions-root <REPO>/sessions serve
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.loopocel]
command = "<REPO>/.venv/bin/loopocel-mcp"
args = ["--sessions-root", "<REPO>/sessions", "serve"]
```

**Cursor** (`~/.cursor/mcp.json`), **Gemini CLI** (`~/.gemini/settings.json`), **Antigravity** (MCP server settings, or a workspace plugin at `.agents/plugins/loopocel-mcp/mcp_config.json`): the same `mcpServers` → `loopocel` entry as for Claude Desktop.

### Step C: Restart and check

1. Restart the client completely (Claude Desktop: Cmd+Q and reopen; Claude Code: start a new session; Antigravity: refresh the MCP server, since it caches tool lists).
2. Check that the client shows `loopocel` as connected with 10 tools, including `get_process_graphs`, and the prompts `record-task` and `process-graphs`. In Claude Code: `claude mcp list`.
3. Turn on the client's web search or other tools the task needs. loopocel only records; the agent still needs real tools to do the work, otherwise it has nothing real to record.
4. Optional smoke test from a terminal:
   ```sh
   echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | <REPO>/.venv/bin/loopocel-mcp --sessions-root /tmp/loopocel-check serve
   ```

### Step D: Use it

Give the agent a normal multi-step task, for example *"Using the loopocel MCP server, record this task: Plan a round-trip from Frankfurt to Copenhagen …"*, then ask for the process graphs (`get_process_graphs`, or the `process-graphs` prompt). After `session_end`, `provenance.recorded_via` should read `mcp-stdio` and `provenance.live` should be `true`.

**Troubleshooting**
- *Tools do not appear*: the path in `command` must be absolute and executable; check the client's MCP log (Claude Desktop: Settings → Developer → Open Logs).
- *`recorded_via` is `cli` or `python-api`*: the agent did not use the MCP tools; the server is not registered in that client or the client was not restarted.
- *Errors from a loopocel tool*: report them; do not edit files in `sessions/` by hand.

---

## 0. Recording Rules (read first)

- **Do the task for real** with your normal tools (web search, files, shell, APIs). The loopocel tools only log what you do; they never perform the task.
- **Record live.** Call `session_start` before your first real action, then `record_turn` right after each reasoning-and-tool cycle, while the task runs. Do not collect turns and record them at the end.
- **Record what really happened.** An action's `name` is the tool you actually called (`search_web`, `read_file`, `run_command` ...), its `arguments` are what you passed, and its `result` comes from the real tool output (shortened is fine). Never invent tools, results, prices, IDs, or outcomes. Record failed steps with `"outcome": "failure"`.
- **Steps you do yourself without a tool** (comparing options, checking constraints, deciding, committing a plan) are actions with `"internal": true`, a descriptive name such as `verify_constraints`, and your own conclusion as `result`, computed from data already recorded. Never present them as tool calls, and never invent external effects such as bookings, payments, or reference numbers.
- **Only use IDs and values that appear in a tool output.** If a search returns price ranges or no exact item, record the range or a descriptive ID (`flight:lufthansa_fra_cph`) instead of inventing flight numbers or prices.
- **Never edit `capture.jsonl`** or other session files. If a loopocel tool returns an error, report it to the user instead of working around it.
- **Never write a script that imports `loopocel_mcp`** or replays prepared turns. If the loopocel MCP tools are not in your tool list, call each tool live from the shell after each step:
  ```sh
  python -m loopocel_mcp --sessions-root <sessions> call record_turn --args-json '<json>'
  ```
- `session_end` reports `provenance`: runs recorded through a Python import, or whose turns all arrive within one second, are flagged as not live, and the graphs carry a red warning.
- Finish with `session_end`, which writes only the validated log. Then ask the user whether they want to see the two process graphs; call `get_process_graphs` only if they say yes, then show both.

---

## 1. Choose the Right Capture Mode

`loopocel-mcp` supports four capture modes depending on your setup:

1. **Direct MCP Tool Recording (`mode="mcp"`)**:
   - Use when `loopocel-mcp` is connected as an MCP server and you want to actively record each turn and domain `ProcessObject` state change as you execute a task.
2. **Post-Hoc Transcript Ingestion (`mode="transcript"`)**:
   - Use when an agent has already completed a run (Claude Code JSONL, Antigravity `transcript.jsonl`, or OpenAI/Anthropic chat JSON) and you want to convert its transcript into `loopocel.json` + `figure.svg`/`figure.png`.
3. **Downstream MCP Server Wrapper (`loopocel-mcp wrap`)**:
   - Use when you want zero-token passive recording of all `tools/call` executions sent to another MCP server.
4. **Streaming HTTP LLM Proxy (`loopocel-mcp proxy`)**:
   - Use when you want passive full-fidelity capture of raw LLM requests/responses (including SSE `text/event-stream` chunks) via `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`.

---

## 2. Mode 1: Direct MCP Tool Recording Protocol (`mode="mcp"`)

When executing a user task while recording via `loopocel-mcp` tools, follow this exact 3-step lifecycle:

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

### Step 3: Finalize, Validate, and Render (`session_end`, `get_process_graphs`, `get_figure`)
Once the task finishes:
1. Call `session_end({"session_id": "my_task_01"})`:
   - Converts `capture.jsonl` $\to$ `loopocel.json`
   - Runs two-stage verification (OCEL 2.0 JSON Schema draft-07 + LoopOCEL relational cardinality check)
   - Renders no figures: the log is the only default output
2. Ask the user whether they want to see the two process graphs. If yes, call `get_process_graphs({"session_id": "my_task_01"})`; it renders `ocdfg_process_graph.svg`/`.png` and `semantic_process_graph.svg`/`.png` right away and returns both (omit `session_id` for the most recent session; add `"include_sessions": [...]` to aggregate runs):
   - **Agent-loop OC-DFG**: LoopOCEL event types with directly-follows edges per object type (how the agent loop ran).
   - **Semantic process graph**: the domain steps (tool actions named by tool and by the business object kinds they touch), failed actions and their rework, plus the final state of each business object kind. Use meaningful tool names and `<kind>:<id>` object IDs, since they become the node labels.
3. If the user asks for it, call `get_figure({"session_id": "my_task_01", "format": "png"})` (or `"compare_with": ["other_session"]`) for the ontology coverage figure.

Shortcut: the `process-graphs` MCP prompt does all of this from one line. With a `task` argument it records the task and then shows both graphs; without one it shows the graphs of the most recent session.

---

## 3. Mode 2: Post-Hoc Transcript Ingestion (`mode="transcript"`)

To convert an existing agent transcript file without re-running the task:

1. Via MCP tools:
   - Call `session_start({"session_id": "run_01", "task": "Task name", "mode": "transcript"})`
   - Call `session_end({"session_id": "run_01", "transcript_path": "/path/to/transcript.jsonl"})`
2. Or via CLI:
   ```sh
   loopocel-mcp ingest sessions/run_01 /path/to/transcript.jsonl --task "Task name"
   ```

---

## 4. Mode 3 & 4: Passive Wrapping & Streaming Proxy

- **Wrap any stdio MCP server**:
  ```sh
  loopocel-mcp wrap --session-dir sessions/wrapped_run --task "Filesystem task" -- npx -y @modelcontextprotocol/server-filesystem /workspace
  ```
- **Run local HTTP/SSE LLM proxy**:
  ```sh
  loopocel-mcp proxy --session-dir sessions/proxy_run --upstream https://api.anthropic.com --port 18080
  ```
  Then point `ANTHROPIC_BASE_URL=http://127.0.0.1:18080` (or `OPENAI_BASE_URL`) before running your agent.

