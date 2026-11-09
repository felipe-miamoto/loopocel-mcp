# Execution Runbook: 15 Live-Web Travel Planning Runs via `ocel4agent-mcp`

This directory contains only:
- [`tasks.json`](tasks.json) — The 15 academic & conference travel tasks across 3 complexity tiers (`tier1_direct_01`..`05`, `tier2_constrained_01`..`05`, `tier3_multimodal_01`..`05`).
- [`analyze_logs.py`](analyze_logs.py) — Post-run process mining and cohort analysis script that reads `sessions/<task_id>/loopocel.json` and generates Table 1, the LoopOCEL coverage figures, and the discovered Object-Centric Process Graph (OC-DFG).

All 15 experiments are recorded **directly through the `ocel4agent-mcp` MCP server**—no domain-specific wrapper scripts are used.

---

## How to Record Each Run Using `ocel4agent-mcp`

The `ocel4agent-mcp` server is registered as a workspace plugin in [`.agents/plugins/ocel4agent-mcp/mcp_config.json`](../../../.agents/plugins/ocel4agent-mcp/mcp_config.json) storing sessions in `sessions`.

You can invoke the MCP tools (`session_start`, `record_object`, `record_turn`, `session_end`, `get_log`, `get_figure`, `get_summary`, `list_sessions`) either:
1. **Directly as MCP tools** if loaded in your active tool declarations, or
2. **Via JSON-RPC `tools/call` against `ocel4agent-mcp`**:
   ```bash
   PYTHONPATH=src python3 -m ocel4agent_mcp --sessions-root sessions call <tool_name> --args-json '<json>'
   ```

### MCP Tool Sequence Per Task (`<task_id>` from `tasks.json`)

1. **`session_start`**:
   ```json
   {
     "session_id": "tier1_direct_01",
     "task": "Frankfurt to Copenhagen Direct Round-Trip",
     "agent": "live-web-agent",
     "profile": "general",
     "mode": "mcp",
     "instruction": "Plan trip tier1_direct_01 (FRA -> CPH, 2026-10-14 to 2026-10-18, 4 nights, max 1100 EUR, arrive by 18:00).",
     "initial_objects": {
       "trip:tier1_direct_01": {
         "origin": "FRA",
         "destination": "CPH",
         "nights": 4,
         "max_total_eur": 1100.0,
         "status": "requested"
       }
     }
   }
   ```

2. **Query the Live Web (`search_web`) & Call `record_turn`**:
   - Perform live `search_web` queries for flights, rail connections, and hotels.
   - Record each turn via `record_turn`:
     ```json
     {
       "session_id": "tier1_direct_01",
       "response_message": "Searched direct flights FRA-CPH and hotels near Kongens Lyngby/Copenhagen.",
       "actions": [
         {
           "name": "search_web",
           "arguments": {
             "query": "direct flights Frankfurt FRA to Copenhagen CPH Lufthansa SAS price schedule",
             "trip_id": "tier1_direct_01"
           },
           "outcome": "success",
           "result": {
             "flight_ids": ["LH824", "SK676"],
             "summary": "LH824 dep 08:40 arr 10:05 (245 EUR RT); SK676 dep 13:05 arr 14:30 (215 EUR RT)"
           },
           "targets": ["trip:tier1_direct_01", "flight:LH824", "flight:SK676"],
           "acted_on": ["trip:tier1_direct_01", "flight:LH824", "flight:SK676"],
           "describes": ["trip:tier1_direct_01", "flight:LH824", "flight:SK676"]
         },
         {
           "name": "search_web",
           "arguments": {
             "query": "hotels near Kongens Lyngby or central Copenhagen nightly rate EUR",
             "trip_id": "tier1_direct_01"
           },
           "outcome": "success",
           "result": {
             "hotel_ids": ["scandic-eremitagen-lyngby", "wake-up-copenhagen"],
             "summary": "Scandic Eremitagen Lyngby 145 EUR/night; Wakeup Copenhagen 105 EUR/night"
           },
           "targets": ["hotel:scandic-eremitagen-lyngby", "hotel:wake-up-copenhagen"],
           "acted_on": ["hotel:scandic-eremitagen-lyngby", "hotel:wake-up-copenhagen"],
           "describes": ["hotel:scandic-eremitagen-lyngby", "hotel:wake-up-copenhagen"]
         }
       ]
     }
     ```
   - In **Tier 2** and **Tier 3**, when a candidate flight/train/hotel combination violates a constraint (e.g., hotel rate > nightly cap, transfer buffer < 90 min, or total > budget cap), record that verification step with `"outcome": "failure"` (or `"rejected": true` if rejected pre-execution) and then record the subsequent rework turn that selects the compliant option and mutates `"trip:<task_id>"` and the selected `"flight:*"` / `"train:*"` / `"hotel:*"` via `"changes"`.
   - On the final turn of each task, include `"changes"` on `"trip:<task_id>"` and the selected booking objects (`"flight:*"`, `"train:*"`, `"hotel:*"`) so `ProcessObject.StateChanged` is emitted.

3. **`session_end`**:
   ```json
   {
     "session_id": "tier1_direct_01"
   }
   ```
   This converts `capture.jsonl` → `loopocel.json`, validates the OCEL 2.0 schema and LoopOCEL ontology reconstruction (`validation.passed == true`), and renders `figure.svg` and `figure.png`.

---

## Step 4: Run Process Mining Cohort Analysis

Once all 15 sessions (`tier1_direct_01`..`05`, `tier2_constrained_01`..`05`, `tier3_multimodal_01`..`05`) are completed in `sessions/`, run:

```bash
PYTHONPATH=src python3 experiments/travel_case_study/analyze_logs.py
```
