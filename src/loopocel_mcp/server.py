"""LoopOCEL MCP server: session control, live agent-loop recording, log export, and figure rendering."""

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import traceback
from typing import Any, TextIO

from .adapters.claude_code import claude_code_settings_snippet, ingest_transcript
from .capture import Capture, SessionStore, canonical, content_hash, now, write_json
from .convert import convert, summarize_log
from .extractor import ProcessObjectExtractor
from .figure import render_session_figure
from .process_graphs import render_session_process_graphs
from .validate import validate_log


MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "loopocel-mcp", "version": "0.1.0"}


class _LiveSessionState:
    """Tracks in-memory turn, history, and object state for live MCP recording."""

    def __init__(self, session_dir: Path, meta: dict[str, Any]) -> None:
        self.session_dir = session_dir
        self.meta = meta
        self.cap = Capture(session_dir)
        self.extractor = ProcessObjectExtractor(custom_fields=meta.get("id_fields"))
        self.turn_index = 0
        self.request_index = 0
        self.previous_turn: str | None = None
        self.active_instructions: list[str] = []
        self.prior_sources: list[str] = []
        self.latest_results: list[str] = []
        self._rehydrate()

    def _rehydrate(self) -> None:
        cap_path = self.session_dir / "capture.jsonl"
        if not cap_path.is_file():
            return
        for raw_line in cap_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec.get("kind")
            payload = rec.get("data") or rec.get("payload") or {}
            if kind == "instruction":
                iid = payload.get("id")
                if iid and iid not in self.active_instructions:
                    self.active_instructions.append(iid)
            elif kind == "turn_start":
                idx = int(payload.get("index", 0))
                if idx > self.turn_index:
                    self.turn_index = idx
                self.previous_turn = payload.get("id")
                self.latest_results = []
            elif kind == "response":
                rid = payload.get("id")
                if rid and rid not in self.prior_sources:
                    self.prior_sources.append(rid)
            elif kind == "action_request":
                aid = str(payload.get("id", ""))
                if aid.startswith("request:"):
                    try:
                        r_idx = int(aid.split(":", 1)[1])
                        if r_idx > self.request_index:
                            self.request_index = r_idx
                    except ValueError:
                        self.request_index += 1
                else:
                    self.request_index += 1
            elif kind == "action_result":
                res_id = payload.get("id")
                if res_id:
                    if res_id not in self.prior_sources:
                        self.prior_sources.append(res_id)
                    if res_id not in self.latest_results:
                        self.latest_results.append(res_id)
            elif kind == "business_initial":
                for oid, st in (payload.get("objects") or {}).items():
                    if isinstance(st, dict):
                        self.extractor.states[oid] = dict(st)
            elif kind == "business_change":
                oid = payload.get("id")
                st = payload.get("state")
                if oid and isinstance(st, dict):
                    self.extractor.states[oid] = dict(st)


class LoopOCELServer:
    """Core MCP service managing LoopOCEL sessions, live capture, conversion, and figures."""

    def __init__(self, sessions_root: Path | str | None = None) -> None:
        default_root = Path.cwd() / "sessions"
        self.store = SessionStore(sessions_root or default_root)
        self._live: dict[str, _LiveSessionState] = {}

    def _get_live(self, session_id: str) -> _LiveSessionState:
        if session_id not in self._live:
            meta = self.store.get_meta(session_id)
            self._live[session_id] = _LiveSessionState(self.store.session_dir(session_id), meta)
        return self._live[session_id]

    def session_start(
        self,
        task: str = "agent-task",
        agent: str = "agent",
        profile: str = "general",
        mode: str = "mcp",
        session_id: str | None = None,
        initial_objects: dict[str, dict[str, Any]] | None = None,
        instruction: str | list[dict[str, Any]] | None = None,
        id_fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        meta = self.store.create(
            task=task,
            agent=agent,
            profile=profile,
            mode=mode,
            session_id=session_id,
            id_fields=id_fields,
        )
        sid = meta["session_id"]
        state = _LiveSessionState(self.store.session_dir(sid), meta)
        self._live[sid] = state

        if initial_objects:
            added = state.extractor.register_initial(initial_objects)
            if added:
                state.cap.business_initial(added, emit_registered=True)

        if instruction is not None:
            msgs = (
                [{"role": "user", "content": instruction}]
                if isinstance(instruction, str)
                else list(instruction)
            )
            iid = "instruction:base"
            state.cap.instruction(iid, msgs)
            state.active_instructions.append(iid)

        sdir = self.store.session_dir(sid)
        return {
            "session_id": sid,
            "directory": str(sdir.resolve()),
            "meta": meta,
            "claude_code_hooks": claude_code_settings_snippet(sdir),
        }

    def record_object(
        self,
        session_id: str,
        object_id: str,
        state: dict[str, Any],
        created: bool = False,
        registered: bool = False,
    ) -> dict[str, Any]:
        live = self._get_live(session_id)
        was_known = object_id in live.extractor.states
        live.extractor.states[object_id] = dict(state)
        if registered and not was_known:
            live.cap.business_change(
                object_id,
                state,
                created=True,
                turn_id=live.previous_turn,
                emit_event=False,
            )
            live.cap.event("ProcessObject.Registered", subject=object_id)
        else:
            live.cap.business_change(
                object_id,
                state,
                created=created or not was_known,
                turn_id=live.previous_turn,
                emit_event=True,
            )
        return {"session_id": session_id, "object_id": object_id, "recorded": True}

    def record_turn(
        self,
        session_id: str,
        response_message: str | dict[str, Any],
        actions: list[dict[str, Any]] | None = None,
        instruction: str | list[dict[str, Any]] | None = None,
        parameters: dict[str, Any] | None = None,
        provider: dict[str, Any] | None = None,
        request_fields: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> dict[str, Any]:
        live = self._get_live(session_id)
        live.turn_index += 1
        idx = live.turn_index
        tid = f"turn:{idx}"
        pid = f"prompt:{idx}"
        rid = f"response:{idx}"

        if instruction is not None:
            msgs = (
                [{"role": "user", "content": instruction}]
                if isinstance(instruction, str)
                else list(instruction)
            )
            iid = "instruction:base" if not live.active_instructions else f"instruction:{content_hash(msgs)}"
            if iid not in live.active_instructions:
                live.cap.instruction(iid, msgs, request_fields=request_fields)
                live.active_instructions.append(iid)
        elif not live.active_instructions:
            iid = "instruction:base"
            live.cap.instruction(
                iid,
                [{"role": "user", "content": live.meta.get("task", "agent-task")}],
                request_fields=request_fields,
            )
            live.active_instructions.append(iid)

        history_sources = [s for s in live.prior_sources if s not in live.latest_results]
        history = (
            {"id": f"history:{idx}", "sources": list(history_sources)}
            if history_sources
            else None
        )
        layout = (
            list(live.active_instructions)
            + ([history["id"]] if history else [])
            + list(live.latest_results)
        )
        prompt_params = dict(parameters or {"model": live.meta.get("agent", "agent")})
        if request_fields:
            for k, v in request_fields.items():
                prompt_params.setdefault(k, v)

        live.cap.turn_start(
            tid,
            pid,
            idx,
            live.meta.get("task", session_id),
            prompt_params,
            layout,
            previous_turn=live.previous_turn,
            history=history,
        )

        if failed and not response_message:
            live.cap.event("Turn.Failed", subject=tid, prompt=pid)
            live.previous_turn = tid
            return {"turn_id": tid, "prompt_id": pid, "response_id": None, "actions_recorded": 0}

        msg_dict = (
            {"role": "assistant", "content": response_message}
            if isinstance(response_message, str)
            else dict(response_message)
        )
        prov_dict = dict(provider or {"turn": idx, "status": "failed" if failed else "completed"})
        live.cap.response(rid, tid, prov_dict, msg_dict, finish_turn=not failed)
        if failed:
            live.cap.event("Turn.Failed", subject=tid, response=rid)

        live.prior_sources.append(rid)
        live.latest_results = []

        recorded_actions: list[dict[str, str]] = []
        for act in actions or ():
            live.request_index += 1
            r_idx = live.request_index
            aid = act.get("request_id") or f"request:{r_idx}"
            eid = act.get("execution_id") or f"execution:{r_idx}"
            res_id = act.get("result_id") or f"result:{r_idx}"
            tname = str(act.get("name", "tool"))
            targs = act.get("arguments") if isinstance(act.get("arguments"), dict) else {"value": act.get("arguments")}
            rejected = bool(act.get("rejected", False))
            outcome = str(act.get("outcome", "rejected" if rejected else "success"))
            raw_result = act.get("result", {"ok": outcome == "success"})
            res_data = raw_result if isinstance(raw_result, (dict, list)) else {"result": raw_result}

            req_obs = live.extractor.observe_request(
                tname,
                targs,
                explicit_targets=act.get("targets"),
            )
            for oid, ostate in req_obs.initial_objects.items():
                live.cap.business_change(oid, ostate, created=True, turn_id=tid, emit_event=False)
                live.cap.event("ProcessObject.Registered", subject=oid)

            live.cap.action_request(
                aid,
                rid,
                tid,
                tname,
                targs,
                req_obs.targets,
                dispatched=not rejected,
                rejected=rejected,
            )
            if rejected:
                recorded_actions.append({"request_id": aid, "status": "rejected"})
                continue

            live.cap.execution_start(eid, aid, tid)
            exec_obs = live.extractor.observe_execution_and_result(
                tname,
                targs,
                res_data,
                outcome=outcome,
                explicit_acted_on=act.get("acted_on"),
                explicit_describes=act.get("describes"),
                explicit_changes=act.get("changes"),
            )
            for oid, ostate in exec_obs.initial_objects.items():
                live.cap.business_change(oid, ostate, created=True, turn_id=tid, emit_event=False)
                live.cap.event("ProcessObject.Registered", subject=oid)
            for chg in exec_obs.changes:
                live.cap.business_change(
                    chg["id"],
                    chg["state"],
                    created=chg["created"],
                    execution_id=eid,
                    turn_id=tid,
                )

            live.cap.execution_finish(eid, aid, tid, outcome, exec_obs.acted_on)
            feedback_msg = {
                "role": "user",
                "content": canonical({"tool_result": {"request_id": aid, "name": tname, "result": res_data}}),
            }
            live.cap.action_result(
                res_id,
                aid,
                eid,
                feedback_msg,
                res_data,
                exec_obs.describes,
                turn_id=tid,
            )
            live.prior_sources.append(res_id)
            live.latest_results.append(res_id)
            recorded_actions.append({"request_id": aid, "execution_id": eid, "result_id": res_id})

        live.previous_turn = tid
        return {
            "turn_id": tid,
            "prompt_id": pid,
            "response_id": rid,
            "actions_recorded": len(recorded_actions),
            "actions": recorded_actions,
        }

    def ingest_session_transcript(
        self,
        session_id: str,
        transcript_path: str,
    ) -> dict[str, Any]:
        sdir = self.store.session_dir(session_id)
        meta = self.store.get_meta(session_id)
        stats = ingest_transcript(
            sdir,
            Path(transcript_path),
            task_id=meta.get("task", session_id),
            id_fields=meta.get("id_fields"),
        )
        self.store.update_meta(session_id, capture_completeness="partial", mode="transcript")
        return {"session_id": session_id, **stats}

    def session_end(self, session_id: str, transcript_path: str | None = None) -> dict[str, Any]:
        sdir = self.store.session_dir(session_id)
        if transcript_path:
            self.ingest_session_transcript(session_id, transcript_path)

        log = convert(sdir)
        ocel_path = sdir / "loopocel.json"
        validation = validate_log(log, ocel_path=ocel_path)
        write_json(sdir / "validation.json", validation)

        meta = self.store.update_meta(
            session_id,
            status="completed",
            finished_at=now(),
            validation_passed=validation["passed"],
        )
        summary = summarize_log(log, meta=meta)
        write_json(sdir / "summary.json", summary)
        fig_info = render_session_figure(sdir, formats=("svg", "png"))
        graphs = render_session_process_graphs(sdir, formats=("svg", "png"))

        return {
            "session_id": session_id,
            "status": "completed",
            "ocel_path": str(ocel_path.resolve()),
            "figure_outputs": fig_info["outputs"],
            "process_graph_outputs": graphs["outputs"],
            "validation": validation,
            "summary": summary,
        }

    def get_log(self, session_id: str, format: str = "ocel-json") -> dict[str, Any]:  # noqa: A002
        sdir = self.store.session_dir(session_id)
        if format == "capture-jsonl":
            cap_path = sdir / "capture.jsonl"
            if not cap_path.is_file():
                raise FileNotFoundError(f"No capture.jsonl in session {session_id}")
            lines = [json.loads(line) for line in cap_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            return {"session_id": session_id, "format": "capture-jsonl", "path": str(cap_path.resolve()), "records": lines}

        ocel_path = sdir / "loopocel.json"
        if not ocel_path.is_file():
            log = convert(sdir)
        else:
            log = json.loads(ocel_path.read_text(encoding="utf-8"))
        return {"session_id": session_id, "format": "ocel-json", "path": str(ocel_path.resolve()), "log": log}

    def get_summary(self, session_id: str) -> dict[str, Any]:
        sdir = self.store.session_dir(session_id)
        ocel_path = sdir / "loopocel.json"
        if not ocel_path.is_file():
            log = convert(sdir)
        else:
            log = json.loads(ocel_path.read_text(encoding="utf-8"))
        meta = self.store.get_meta(session_id)
        summary = summarize_log(log, meta=meta)
        validation = validate_log(log, ocel_path=ocel_path)
        return {"session_id": session_id, "summary": summary, "validation": validation}

    def get_figure(
        self,
        session_id: str,
        format: str = "png",  # noqa: A002
        compare_with: list[str] | None = None,
    ) -> dict[str, Any]:
        sdir = self.store.session_dir(session_id)
        compare_dirs = [self.store.session_dir(cid) for cid in (compare_with or ())]
        fmt = format.lower()
        if fmt not in ("svg", "png"):
            raise ValueError(f"Unsupported figure format: {format}")
        info = render_session_figure(sdir, compare_dirs=compare_dirs, formats=("svg", "png"))
        target_path = Path(info["outputs"].get(fmt) or info["outputs"]["svg"])
        raw_bytes = target_path.read_bytes()
        mime = "image/png" if target_path.suffix == ".png" else "image/svg+xml"
        return {
            "session_id": session_id,
            "format": target_path.suffix.lstrip("."),
            "mime_type": mime,
            "path": str(target_path.resolve()),
            "base64": base64.b64encode(raw_bytes).decode("ascii"),
            "cohorts": info["cohorts"],
        }

    def latest_session_id(self) -> str:
        sessions = [s for s in self.store.list_sessions() if s.get("has_capture") or s.get("has_ocel")]
        if not sessions:
            raise FileNotFoundError("No recorded sessions found")
        latest = max(sessions, key=lambda s: str(s.get("finished_at") or s.get("started_at") or ""))
        return str(latest["session_id"])

    def get_process_graphs(
        self,
        session_id: str | None = None,
        format: str = "png",  # noqa: A002
        include_sessions: list[str] | None = None,
        view: str = "both",
    ) -> dict[str, Any]:
        """Render the agent-loop OC-DFG and the semantic process graph for a session."""
        fmt = format.lower()
        if fmt not in ("svg", "png"):
            raise ValueError(f"Unsupported figure format: {format}")
        if view not in ("both", "ocdfg", "semantic"):
            raise ValueError(f"Unsupported view: {view}")
        sid = session_id or self.latest_session_id()
        sdir = self.store.session_dir(sid)
        include_dirs = [self.store.session_dir(x) for x in (include_sessions or ()) if x != sid]
        res = render_session_process_graphs(sdir, include_dirs=include_dirs, formats=("svg", "png"))
        sem = res["semantic_process"]
        graphs = []
        for name in ("ocdfg", "semantic"):
            if view not in ("both", name):
                continue
            outputs = res["outputs"][name]
            path = Path(outputs.get(fmt) or outputs["svg"])
            graphs.append(
                {
                    "view": name,
                    "title": "Agent-loop OC-DFG" if name == "ocdfg" else "Semantic process graph",
                    "format": path.suffix.lstrip("."),
                    "mime_type": "image/png" if path.suffix == ".png" else "image/svg+xml",
                    "path": str(path),
                    "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )
        return {
            "session_id": sid,
            "sessions": res["sessions"],
            "graphs": graphs,
            "ocdfg": {
                "activities": res["ocdfg"]["activities"],
                "edges": len(res["ocdfg"]["edges"]),
            },
            "semantic_process": {
                "activities": {
                    k: {kk: vv for kk, vv in v.items() if kk in ("count", "kind", "touched_kinds", "changed_kinds")}
                    for k, v in sem["activities"].items()
                },
                "variants": sem["variants"],
                "objects": sem["objects"],
            },
        }

    def list_sessions(self) -> dict[str, Any]:
        return {"sessions": self.store.list_sessions()}

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "session_start",
                "description": "Open a LoopOCEL recording session for an agent task. Optionally register initial ProcessObjects and base Instruction.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Description or identifier of the task."},
                        "agent": {"type": "string", "description": "Agent name (e.g. claude-code, antigravity, agent)."},
                        "profile": {"type": "string", "description": "Optional domain label."},
                        "mode": {"type": "string", "description": "Capture mode (mcp, claude-code-hooks, llm-proxy, transcript)."},
                        "session_id": {"type": "string", "description": "Optional explicit session ID."},
                        "initial_objects": {"type": "object", "description": "Initial ProcessObject states keyed by object ID."},
                        "instruction": {"type": ["string", "array"], "description": "Initial instruction text or message array."},
                        "id_fields": {"type": "object", "description": "Optional mapping of argument field names to ProcessObject kinds."},
                    },
                },
            },
            {
                "name": "record_turn",
                "description": "Record one model interaction Turn (Prompt, Instruction/History, Response, and optional ActionRequest -> Execution -> ActionResult -> ProcessObject chains).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "response_message": {"type": ["string", "object"], "description": "Assistant response text or message object."},
                        "actions": {
                            "type": "array",
                            "description": "List of tool/action dictionaries with name, arguments, result, outcome, targets, acted_on, describes, changes.",
                            "items": {"type": "object"},
                        },
                        "instruction": {"type": ["string", "array"], "description": "New user instruction introduced on this turn, if any."},
                        "parameters": {"type": "object", "description": "Model parameters for this Prompt."},
                        "provider": {"type": "object", "description": "Provider metadata for the Response."},
                        "failed": {"type": "boolean", "description": "Whether the model interaction failed."},
                    },
                    "required": ["session_id", "response_message"],
                },
            },
            {
                "name": "record_object",
                "description": "Register or update a business ProcessObject and emit ProcessObject.Registered, Created, or StateChanged.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "object_id": {"type": "string"},
                        "state": {"type": "object"},
                        "created": {"type": "boolean"},
                        "registered": {"type": "boolean"},
                    },
                    "required": ["session_id", "object_id", "state"],
                },
            },
            {
                "name": "ingest_transcript",
                "description": "Ingest an agent transcript file (Claude Code JSONL, Antigravity transcript.jsonl, or chat JSON) into the session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "transcript_path": {"type": "string"},
                    },
                    "required": ["session_id", "transcript_path"],
                },
            },
            {
                "name": "session_end",
                "description": "Close a recording session, convert capture.jsonl to loopocel.json, validate against the LoopOCEL ontology and OCEL 2.0 schema, and render coverage figures.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "transcript_path": {"type": "string", "description": "Optional transcript path to ingest before converting."},
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "get_log",
                "description": "Return the recorded log in LoopOCEL OCEL 2.0 JSON format ('ocel-json') or raw capture evidence ('capture-jsonl').",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "format": {"type": "string", "enum": ["ocel-json", "capture-jsonl"], "default": "ocel-json"},
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "get_figure",
                "description": "Render and return the LoopOCEL coverage and instance count figure (SVG or PNG) for a session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "format": {"type": "string", "enum": ["png", "svg"], "default": "png"},
                        "compare_with": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional session IDs to compare side by side in the figure.",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "get_process_graphs",
                "description": "Discover and return two process graphs for a recorded session: the architectural agent-loop OC-DFG (event types, directly-follows per object type) and the semantic domain process graph (tool actions on business objects, failures, rework). Defaults to the most recent session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "Session to analyze (default: most recent)."},
                        "format": {"type": "string", "enum": ["png", "svg"], "default": "png"},
                        "include_sessions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional further session IDs to aggregate into the same graphs.",
                        },
                        "view": {"type": "string", "enum": ["both", "ocdfg", "semantic"], "default": "both"},
                    },
                },
            },
            {
                "name": "get_summary",
                "description": "Return instance counts per class, O2O relation, and event type, plus ontology coverage and validation status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "list_sessions",
                "description": "List all recorded and active LoopOCEL sessions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        if name == "session_start":
            return self.session_start(**args)
        if name == "record_turn":
            return self.record_turn(**args)
        if name == "record_object":
            return self.record_object(**args)
        if name == "ingest_transcript":
            return self.ingest_session_transcript(**args)
        if name == "session_end":
            return self.session_end(**args)
        if name == "get_log":
            return self.get_log(**args)
        if name == "get_figure":
            return self.get_figure(**args)
        if name == "get_process_graphs":
            return self.get_process_graphs(**args)
        if name == "get_summary":
            return self.get_summary(**args)
        if name == "list_sessions":
            return self.list_sessions()
        raise ValueError(f"Unknown tool: {name}")

    def list_resources(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        for s in self.store.list_sessions():
            sid = s["session_id"]
            resources.extend(
                [
                    {
                        "uri": f"loopocel://sessions/{sid}/log",
                        "name": f"LoopOCEL Log ({sid})",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"loopocel://sessions/{sid}/figure",
                        "name": f"LoopOCEL Figure ({sid})",
                        "mimeType": "image/svg+xml",
                    },
                    {
                        "uri": f"loopocel://sessions/{sid}/summary",
                        "name": f"LoopOCEL Summary ({sid})",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"loopocel://sessions/{sid}/ocdfg",
                        "name": f"Agent-loop OC-DFG ({sid})",
                        "mimeType": "image/svg+xml",
                    },
                    {
                        "uri": f"loopocel://sessions/{sid}/semantic",
                        "name": f"Semantic Process Graph ({sid})",
                        "mimeType": "image/svg+xml",
                    },
                ]
            )
        return resources

    def read_resource(self, uri: str) -> dict[str, Any]:
        prefix = "loopocel://sessions/"
        if not uri.startswith(prefix):
            raise ValueError(f"Unsupported resource URI: {uri}")
        rest = uri.removeprefix(prefix)
        parts = rest.split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid resource URI: {uri}")
        sid, kind = parts
        if kind == "log":
            data = self.get_log(sid, format="ocel-json")["log"]
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, indent=2)}]}
        if kind == "summary":
            data = self.get_summary(sid)
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, indent=2)}]}
        if kind == "figure":
            fig = self.get_figure(sid, format="svg")
            svg_text = Path(fig["path"]).read_text(encoding="utf-8")
            return {"contents": [{"uri": uri, "mimeType": "image/svg+xml", "text": svg_text}]}
        if kind in ("ocdfg", "semantic"):
            res = self.get_process_graphs(sid, format="svg", view=kind)
            svg_text = Path(res["graphs"][0]["path"]).read_text(encoding="utf-8")
            return {"contents": [{"uri": uri, "mimeType": "image/svg+xml", "text": svg_text}]}
        raise ValueError(f"Unknown resource kind: {kind}")

    def list_prompts(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "record-task",
                "description": "Instruct the agent to execute a task while recording every reasoning turn, tool execution, and domain object state transition into a validated LoopOCEL (OCEL 2.0) log.",
                "arguments": [
                    {
                        "name": "task",
                        "description": "The user task or instruction to execute and record.",
                        "required": True,
                    },
                    {
                        "name": "session_id",
                        "description": "Optional explicit session ID (auto-generated if omitted).",
                        "required": False,
                    },
                    {
                        "name": "profile",
                        "description": "Optional domain profile label (e.g. travel, coding, general).",
                        "required": False,
                    },
                ],
            },
            {
                "name": "process-graphs",
                "description": "One-line entry point: optionally run and record a task, then show the agent-loop OC-DFG and the semantic domain process graph of the run.",
                "arguments": [
                    {
                        "name": "task",
                        "description": "Task to execute and record first. Omit to analyze an existing session.",
                        "required": False,
                    },
                    {
                        "name": "session_id",
                        "description": "Session to analyze (default: the one just recorded, else the most recent).",
                        "required": False,
                    },
                ],
            },
            {
                "name": "ingest-and-analyze",
                "description": "Ingest an existing agent transcript file into loopocel-mcp, validate OCEL 2.0 compliance, and render the LoopOCEL ontology coverage figure.",
                "arguments": [
                    {
                        "name": "transcript_path",
                        "description": "Path to the transcript file (Claude Code JSONL, Antigravity transcript.jsonl, or chat JSON).",
                        "required": True,
                    },
                    {
                        "name": "session_id",
                        "description": "Optional session ID.",
                        "required": False,
                    },
                    {
                        "name": "task",
                        "description": "Optional task description.",
                        "required": False,
                    },
                ],
            },
        ]

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        if name == "record-task":
            task_text = str(args.get("task") or "Execute the requested task.")
            session_id = args.get("session_id")
            profile = str(args.get("profile") or "general")
            sid_hint = f'session_id="{session_id}"' if session_id else "an explicit or auto-generated session_id"
            prompt_body = (
                f"Execute the following task while recording a complete LoopOCEL (OCEL 2.0) process log via `loopocel-mcp`:\n\n"
                f"Task: {task_text}\n\n"
                f"Follow this instrumentation protocol strictly:\n"
                f"1. **Start Session**: Before taking action, call `session_start` with `task`={json.dumps(task_text)}, `profile`={json.dumps(profile)}, {sid_hint}, `instruction`={json.dumps(task_text)}, and `initial_objects` containing the initial domain `ProcessObject` instances (using `<kind>:<id>` keys such as `trip:01` or `file:app.py`).\n"
                f"2. **Record Every Turn**: After each reasoning and tool-use cycle, call `record_turn` with:\n"
                f"   - `session_id`: the active session ID\n"
                f"   - `response_message`: your reasoning summary for this turn\n"
                f"   - `actions`: list of executed tools, each with `name`, `arguments`, `outcome` (`\"success\"` or `\"rejected\"`), `result`, `targets` (list of targeted `ProcessObject` IDs), `acted_on`, `describes`, and `changes` (`{{\"<kind>:<id>\": {{\"state\": {{...}}, \"created\": true/false}}}}`) for any domain entity created or mutated.\n"
                f"3. **Finalize & Render**: When the task is complete, call `session_end(session_id=...)` to build and validate `loopocel.json` and render the coverage figure and both process graphs, then call `get_process_graphs(session_id=...)` to show the agent-loop OC-DFG and the semantic process graph (and `get_figure(session_id=...)` if the ontology coverage diagram is wanted)."
            )
            return {
                "description": f"Record LoopOCEL execution log for task: {task_text}",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": prompt_body},
                    }
                ],
            }
        if name == "process-graphs":
            task_text = str(args.get("task") or "").strip()
            session_id = args.get("session_id")
            show = (
                f"call `get_process_graphs(session_id={json.dumps(session_id)})`"
                if session_id
                else "call `get_process_graphs` with that session_id"
            )
            explain = (
                "Show both images and explain them briefly: from the OC-DFG, how the agent loop ran "
                "(turns, tool calls, object updates, cycles); from the semantic graph, which domain steps "
                "were taken in which order, which failed and were reworked, and the final state of the business objects."
            )
            if task_text:
                record = self.get_prompt("record-task", {"task": task_text, "session_id": session_id})
                body = (
                    record["messages"][0]["content"]["text"].rsplit("3. **Finalize", 1)[0]
                    + f"3. **Finalize**: call `session_end(session_id=...)`.\n"
                    f"4. **Process graphs**: {show}. {explain}"
                )
            else:
                target = (
                    f"session `{session_id}`" if session_id else "the most recent recorded session (omit session_id)"
                )
                show = f"Call `get_process_graphs` for {target}."
                body = f"{show} {explain}"
            return {
                "description": "Agent-loop OC-DFG and semantic process graph" + (f" for: {task_text}" if task_text else ""),
                "messages": [{"role": "user", "content": {"type": "text", "text": body}}],
            }
        if name == "ingest-and-analyze":
            tpath = str(args.get("transcript_path") or "")
            session_id = args.get("session_id") or "ingested_session"
            task_text = str(args.get("task") or f"Ingested transcript {Path(tpath).name if tpath else ''}").strip()
            prompt_body = (
                f"Ingest the transcript at `{tpath}` into `loopocel-mcp` and generate its validated LoopOCEL log and coverage figure:\n"
                f"1. Call `session_start(session_id={json.dumps(session_id)}, task={json.dumps(task_text)}, mode=\"transcript\")`.\n"
                f"2. Call `session_end(session_id={json.dumps(session_id)}, transcript_path={json.dumps(tpath)})`.\n"
                f"3. Call `get_summary(session_id={json.dumps(session_id)})` and `get_figure(session_id={json.dumps(session_id)})` and summarize the ontology coverage and validation status.\n"
                f"4. Call `get_process_graphs(session_id={json.dumps(session_id)})` and explain the agent-loop OC-DFG and the semantic process graph."
            )
            return {
                "description": f"Ingest and analyze transcript: {tpath}",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": prompt_body},
                    }
                ],
            }
        raise ValueError(f"Unknown prompt: {name}")


def handle_jsonrpc(server: LoopOCELServer, request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one MCP JSON-RPC 2.0 request or notification."""
    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return _error(request_id, -32600, "Invalid Request")
    method = request["method"]
    is_notification = request_id is None
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return None if is_notification else _error(request_id, -32602, "Invalid params")

    try:
        if method == "initialize":
            requested = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
            selected = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
            result: dict[str, Any] = {
                "protocolVersion": selected,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
                "instructions": "Record agent loops into LoopOCEL (OCEL 2.0) logs, render ontology coverage figures, and show the agent-loop OC-DFG and semantic process graph (get_process_graphs, or the process-graphs prompt).",
            }
        elif method in ("notifications/initialized", "notifications/cancelled"):
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": server.list_tools()}
        elif method == "tools/call":
            tname = params.get("name")
            targs = params.get("arguments") or {}
            if not isinstance(tname, str) or not isinstance(targs, dict):
                return None if is_notification else _error(request_id, -32602, "Invalid params")
            structured = server.call_tool(tname, targs)
            content: list[dict[str, Any]] = []
            if tname == "get_process_graphs":
                meta = {k: v for k, v in structured.items() if k != "graphs"}
                meta["graphs"] = [{k: v for k, v in g.items() if k != "base64"} for g in structured["graphs"]]
                content.append({"type": "text", "text": json.dumps(meta, indent=2)})
                for g in structured["graphs"]:
                    content.append({"type": "image", "data": g["base64"], "mimeType": g["mime_type"]})
                structured = meta
            elif tname == "get_figure" and "base64" in structured:
                summary_meta = {k: v for k, v in structured.items() if k != "base64"}
                content.append({"type": "text", "text": json.dumps(summary_meta, indent=2)})
                content.append(
                    {
                        "type": "image",
                        "data": structured["base64"],
                        "mimeType": structured["mime_type"],
                    }
                )
            else:
                content.append({"type": "text", "text": json.dumps(structured, indent=2)})
            result = {"content": content, "structuredContent": structured, "isError": False}
        elif method == "prompts/list":
            result = {"prompts": server.list_prompts()}
        elif method == "prompts/get":
            pname = params.get("name")
            pargs = params.get("arguments") or {}
            if not isinstance(pname, str) or not isinstance(pargs, dict):
                return None if is_notification else _error(request_id, -32602, "Invalid params")
            result = server.get_prompt(pname, pargs)
        elif method == "resources/list":
            result = {"resources": server.list_resources()}
        elif method == "resources/templates/list":
            result = {
                "resourceTemplates": [
                    {
                        "uriTemplate": "loopocel://sessions/{session_id}/log",
                        "name": "LoopOCEL Log",
                        "mimeType": "application/json",
                    },
                    {
                        "uriTemplate": "loopocel://sessions/{session_id}/figure",
                        "name": "LoopOCEL Coverage Figure",
                        "mimeType": "image/svg+xml",
                    },
                    {
                        "uriTemplate": "loopocel://sessions/{session_id}/summary",
                        "name": "LoopOCEL Summary",
                        "mimeType": "application/json",
                    },
                    {
                        "uriTemplate": "loopocel://sessions/{session_id}/ocdfg",
                        "name": "Agent-loop OC-DFG",
                        "mimeType": "image/svg+xml",
                    },
                    {
                        "uriTemplate": "loopocel://sessions/{session_id}/semantic",
                        "name": "Semantic Process Graph",
                        "mimeType": "image/svg+xml",
                    },
                ]
            }
        elif method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return None if is_notification else _error(request_id, -32602, "Invalid params")
            result = server.read_resource(uri)
        else:
            return None if is_notification else _error(request_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            },
        }

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def run_stdio(server: LoopOCELServer, *, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    """Serve MCP JSON-RPC messages over newline-delimited stdio."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                resp = _error(None, -32600, "Invalid Request")
            else:
                resp = handle_jsonrpc(server, req)
        except json.JSONDecodeError:
            resp = _error(None, -32700, "Parse error")
        except Exception:
            traceback.print_exc(file=sys.stderr)
            resp = _error(None, -32603, "Internal error")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()


def run_http(server: LoopOCELServer, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve streamable HTTP MCP JSON-RPC requests on /mcp."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(raw.decode("utf-8"))
                resp = handle_jsonrpc(server, req) if isinstance(req, dict) else _error(None, -32600, "Invalid Request")
            except json.JSONDecodeError:
                resp = _error(None, -32700, "Parse error")
            if resp is None:
                self.send_response(202)
                self.end_headers()
                return
            payload = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def create_fastmcp_server(sessions_root: Path | str | None = None) -> Any:
    """Build an official FastMCP server instance when the mcp SDK is installed."""
    if importlib.util.find_spec("mcp") is None:
        raise RuntimeError("Official 'mcp' package is not installed; use run_stdio or run_http directly")
    from mcp.server.fastmcp import FastMCP, Image

    core = LoopOCELServer(sessions_root=sessions_root)
    mcp = FastMCP("loopocel-mcp")

    @mcp.tool()
    def session_start(
        task: str = "agent-task",
        agent: str = "agent",
        profile: str = "general",
        mode: str = "mcp",
        session_id: str | None = None,
        initial_objects: dict[str, dict[str, Any]] | None = None,
        instruction: str | list[dict[str, Any]] | None = None,
        id_fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return core.session_start(
            task=task,
            agent=agent,
            profile=profile,
            mode=mode,
            session_id=session_id,
            initial_objects=initial_objects,
            instruction=instruction,
            id_fields=id_fields,
        )

    @mcp.tool()
    def record_turn(
        session_id: str,
        response_message: str | dict[str, Any],
        actions: list[dict[str, Any]] | None = None,
        instruction: str | list[dict[str, Any]] | None = None,
        parameters: dict[str, Any] | None = None,
        provider: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> dict[str, Any]:
        return core.record_turn(
            session_id=session_id,
            response_message=response_message,
            actions=actions,
            instruction=instruction,
            parameters=parameters,
            provider=provider,
            failed=failed,
        )

    @mcp.tool()
    def record_object(
        session_id: str,
        object_id: str,
        state: dict[str, Any],
        created: bool = False,
        registered: bool = False,
    ) -> dict[str, Any]:
        return core.record_object(
            session_id=session_id,
            object_id=object_id,
            state=state,
            created=created,
            registered=registered,
        )

    @mcp.tool()
    def ingest_transcript(session_id: str, transcript_path: str) -> dict[str, Any]:
        return core.ingest_session_transcript(session_id=session_id, transcript_path=transcript_path)

    @mcp.tool()
    def session_end(session_id: str, transcript_path: str | None = None) -> dict[str, Any]:
        return core.session_end(session_id=session_id, transcript_path=transcript_path)

    @mcp.tool()
    def get_log(session_id: str, format: str = "ocel-json") -> dict[str, Any]:  # noqa: A002
        return core.get_log(session_id=session_id, format=format)

    @mcp.tool(structured_output=False)
    def get_figure(
        session_id: str,
        format: str = "png",  # noqa: A002
        compare_with: list[str] | None = None,
    ) -> list[Any]:
        info = core.get_figure(session_id=session_id, format=format, compare_with=compare_with)
        meta = {k: v for k, v in info.items() if k != "base64"}
        img_bytes = Path(info["path"]).read_bytes()
        return [json.dumps(meta, indent=2), Image(data=img_bytes, format=info["format"])]

    @mcp.tool(structured_output=False)
    def get_process_graphs(
        session_id: str | None = None,
        format: str = "png",  # noqa: A002
        include_sessions: list[str] | None = None,
        view: str = "both",
    ) -> list[Any]:
        """Agent-loop OC-DFG and semantic domain process graph for a session (default: most recent)."""
        info = core.get_process_graphs(
            session_id=session_id, format=format, include_sessions=include_sessions, view=view
        )
        meta = {k: v for k, v in info.items() if k != "graphs"}
        meta["graphs"] = [{k: v for k, v in g.items() if k != "base64"} for g in info["graphs"]]
        out: list[Any] = [json.dumps(meta, indent=2)]
        for g in info["graphs"]:
            out.append(Image(data=Path(g["path"]).read_bytes(), format=g["format"]))
        return out

    @mcp.tool()
    def get_summary(session_id: str) -> dict[str, Any]:
        return core.get_summary(session_id=session_id)

    @mcp.tool()
    def list_sessions() -> dict[str, Any]:
        return core.list_sessions()

    @mcp.prompt(name="record-task")
    def prompt_record_task(task: str, session_id: str | None = None, profile: str = "general") -> str:
        res = core.get_prompt("record-task", {"task": task, "session_id": session_id, "profile": profile})
        return res["messages"][0]["content"]["text"]

    @mcp.prompt(name="process-graphs")
    def prompt_process_graphs(task: str | None = None, session_id: str | None = None) -> str:
        res = core.get_prompt("process-graphs", {"task": task, "session_id": session_id})
        return res["messages"][0]["content"]["text"]

    @mcp.prompt(name="ingest-and-analyze")
    def prompt_ingest_and_analyze(
        transcript_path: str,
        session_id: str = "ingested_session",
        task: str | None = None,
    ) -> str:
        res = core.get_prompt(
            "ingest-and-analyze",
            {"transcript_path": transcript_path, "session_id": session_id, "task": task},
        )
        return res["messages"][0]["content"]["text"]

    return mcp


