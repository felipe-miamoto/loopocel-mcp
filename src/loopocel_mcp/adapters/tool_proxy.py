"""MCP tool proxy recording ActionRequest -> Execution -> ActionResult chains."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Callable, TextIO

from ..capture import Capture, canonical, write_json
from ..convert import convert
from ..extractor import ProcessObjectExtractor
from ..figure import render_session_figure
from ..process_graphs import render_session_process_graphs
from ..validate import validate_log


class ToolCallProxy:
    """Wraps downstream tools and logs their LoopOCEL action/execution/result lifecycle."""

    def __init__(
        self,
        session_dir: Path | str,
        *,
        task_id: str = "tool-proxy-task",
        id_fields: dict[str, str] | None = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.cap = Capture(self.session_dir)
        self.extractor = ProcessObjectExtractor(custom_fields=id_fields)
        self.task_id = task_id
        self.sequence = 0
        self.previous_turn: str | None = None
        self.prior_sources: list[str] = []
        self.last_result_id: str | None = None
        self._base_registered = False

    def _ensure_base_instruction(self) -> None:
        if not self._base_registered:
            self.cap.instruction(
                "instruction:base",
                [{"role": "user", "content": self.task_id}],
            )
            self._base_registered = True

    def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
    ) -> Any:
        self._ensure_base_instruction()
        self.sequence += 1
        idx = self.sequence
        tid = f"turn:{idx}"
        pid = f"prompt:{idx}"
        rid = f"response:{idx}"
        aid = f"request:{idx}"
        eid = f"execution:{idx}"
        res_id = f"result:{idx}"

        history_sources = [s for s in self.prior_sources if s != self.last_result_id]
        history = {"id": f"history:{idx}", "sources": history_sources} if history_sources else None
        layout = (
            ["instruction:base"]
            + ([history["id"]] if history else [])
            + ([self.last_result_id] if self.last_result_id else [])
        )

        self.cap.turn_start(
            tid,
            pid,
            idx,
            self.task_id,
            {"mode": "tool_proxy"},
            layout,
            previous_turn=self.previous_turn,
            history=history,
        )
        assistant_msg = {"role": "assistant", "content": canonical({"name": tool_name, "arguments": arguments})}
        self.cap.response(rid, tid, {"mode": "tool_proxy"}, assistant_msg)
        self.prior_sources.append(rid)

        req_obs = self.extractor.observe_request(tool_name, arguments)
        for oid, state in req_obs.initial_objects.items():
            self.cap.business_change(oid, state, created=True, turn_id=tid, emit_event=False)
            self.cap.event("ProcessObject.Registered", subject=oid)

        self.cap.action_request(aid, rid, tid, tool_name, arguments, req_obs.targets)
        self.cap.execution_start(eid, aid, tid)

        try:
            raw_result = handler(arguments)
            outcome = "success"
            result_data = raw_result if isinstance(raw_result, (dict, list)) else {"result": raw_result}
        except Exception as exc:
            outcome = "rejected"
            result_data = {"ok": False, "error": str(exc)}
            raw_result = result_data

        self._finish_call(tid, aid, eid, res_id, tool_name, arguments, result_data, outcome)
        return raw_result

    def record_call_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result_data: Any,
        *,
        outcome: str = "success",
    ) -> dict[str, str]:
        """Record a completed tool call (e.g. intercepted across an MCP stdio stream)."""
        self._ensure_base_instruction()
        self.sequence += 1
        idx = self.sequence
        tid = f"turn:{idx}"
        pid = f"prompt:{idx}"
        rid = f"response:{idx}"
        aid = f"request:{idx}"
        eid = f"execution:{idx}"
        res_id = f"result:{idx}"

        history_sources = [s for s in self.prior_sources if s != self.last_result_id]
        history = {"id": f"history:{idx}", "sources": history_sources} if history_sources else None
        layout = (
            ["instruction:base"]
            + ([history["id"]] if history else [])
            + ([self.last_result_id] if self.last_result_id else [])
        )

        self.cap.turn_start(
            tid,
            pid,
            idx,
            self.task_id,
            {"mode": "tool_proxy"},
            layout,
            previous_turn=self.previous_turn,
            history=history,
        )
        assistant_msg = {"role": "assistant", "content": canonical({"name": tool_name, "arguments": arguments})}
        self.cap.response(rid, tid, {"mode": "tool_proxy"}, assistant_msg)
        self.prior_sources.append(rid)

        req_obs = self.extractor.observe_request(tool_name, arguments)
        for oid, state in req_obs.initial_objects.items():
            self.cap.business_change(oid, state, created=True, turn_id=tid, emit_event=False)
            self.cap.event("ProcessObject.Registered", subject=oid)

        self.cap.action_request(aid, rid, tid, tool_name, arguments, req_obs.targets)
        self.cap.execution_start(eid, aid, tid)

        norm_result = result_data if isinstance(result_data, (dict, list)) else {"result": result_data}
        self._finish_call(tid, aid, eid, res_id, tool_name, arguments, norm_result, outcome)
        return {"turn_id": tid, "request_id": aid, "execution_id": eid, "result_id": res_id}

    def _finish_call(
        self,
        tid: str,
        aid: str,
        eid: str,
        res_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result_data: Any,
        outcome: str,
    ) -> None:
        exec_obs = self.extractor.observe_execution_and_result(
            tool_name,
            arguments,
            result_data,
            outcome=outcome,
        )
        for oid, state in exec_obs.initial_objects.items():
            self.cap.business_change(oid, state, created=True, turn_id=tid, emit_event=False)
            self.cap.event("ProcessObject.Registered", subject=oid)
        for change in exec_obs.changes:
            self.cap.business_change(
                change["id"],
                change["state"],
                created=change["created"],
                execution_id=eid,
                turn_id=tid,
            )

        self.cap.execution_finish(eid, aid, tid, outcome, exec_obs.acted_on)
        feedback_msg = {
            "role": "user",
            "content": canonical({"tool_result": {"request_id": aid, "name": tool_name, "result": result_data}}),
        }
        self.cap.action_result(
            res_id,
            aid,
            eid,
            feedback_msg,
            result_data,
            exec_obs.describes,
            turn_id=tid,
        )
        self.prior_sources.append(res_id)
        self.last_result_id = res_id
        self.previous_turn = tid


class MCPStdioWrapper:
    """Transparent stdio wrapper around any downstream MCP server that records all `tools/call` executions."""

    def __init__(
        self,
        session_dir: Path | str,
        command: list[str],
        *,
        task_id: str = "mcp-wrapped-task",
        id_fields: dict[str, str] | None = None,
        auto_finalize: bool = True,
    ) -> None:
        if not command:
            raise ValueError("Downstream MCP command must not be empty")
        self.session_dir = Path(session_dir)
        self.command = list(command)
        self.proxy = ToolCallProxy(self.session_dir, task_id=task_id, id_fields=id_fields)
        self.auto_finalize = auto_finalize
        self._pending: dict[Any, tuple[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def observe_client_message(self, line: str) -> None:
        """Inspect a client -> server JSON-RPC message for `tools/call`."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            return
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if req_id is not None and isinstance(params, dict):
            tname = str(params.get("name") or "tool")
            targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            with self._lock:
                self._pending[req_id] = (tname, targs)

    def observe_server_message(self, line: str) -> None:
        """Inspect a server -> client JSON-RPC response and record completed `tools/call` turns."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or "id" not in msg:
            return
        req_id = msg.get("id")
        with self._lock:
            pending = self._pending.pop(req_id, None)
        if pending is None:
            return
        tname, targs = pending
        if "error" in msg:
            outcome = "rejected"  # the server refused the call (JSON-RPC error)
            result_payload: Any = {"ok": False, "error": msg["error"]}
        else:
            res = msg.get("result") or {}
            is_err = bool(isinstance(res, dict) and res.get("isError"))
            outcome = "failure" if is_err else "success"  # the tool ran and reported an error
            result_payload = self._extract_mcp_result_payload(res)
        with self._lock:
            self.proxy.record_call_result(tname, targs, result_payload, outcome=outcome)

    @staticmethod
    def _extract_mcp_result_payload(res: Any) -> Any:
        if not isinstance(res, dict):
            return {"result": res}
        if isinstance(res.get("structuredContent"), (dict, list)):
            return res["structuredContent"]
        content = res.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    txt = block["text"]
                    try:
                        return json.loads(txt)
                    except json.JSONDecodeError:
                        return {"text": txt}
        return res

    def finalize(self) -> dict[str, Any] | None:
        """Convert captured JSONL into `loopocel.json` and render coverage figures if any calls were recorded."""
        if self.proxy.sequence == 0:
            return None
        meta_path = self.session_dir / "session.json"
        if not meta_path.is_file():
            write_json(
                meta_path,
                {
                    "session_id": self.session_dir.name,
                    "task": self.proxy.task_id,
                    "agent": "mcp-client",
                    "mode": "mcp-wrap",
                    "recorded_via": "mcp-wrap",
                    "capture_completeness": "partial",
                    "status": "completed",
                },
            )
        log = convert(self.session_dir)
        validation = validate_log(log, ocel_path=self.session_dir / "loopocel.json")
        write_json(self.session_dir / "validation.json", validation)
        fig = render_session_figure(self.session_dir)
        graphs = render_session_process_graphs(self.session_dir)
        return {
            "objects": len(log["objects"]),
            "events": len(log["events"]),
            "figure": fig["outputs"],
            "process_graphs": graphs["outputs"],
        }

    def run(self, *, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
        """Spawn the downstream MCP server and pipe stdio while recording tool calls."""
        proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def forward_client_to_server() -> None:
            try:
                for line in stdin:
                    self.observe_client_message(line)
                    proc.stdin.write(line)
                    proc.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        writer_thread = threading.Thread(target=forward_client_to_server, daemon=True)
        writer_thread.start()

        try:
            for sline in proc.stdout:
                self.observe_server_message(sline)
                stdout.write(sline)
                stdout.flush()
        finally:
            proc.wait()
            if self.auto_finalize:
                try:
                    self.finalize()
                except Exception as exc:
                    print(f"loopocel wrap: could not finalize {self.session_dir}: {exc}", file=sys.stderr)
        return int(proc.returncode or 0)


