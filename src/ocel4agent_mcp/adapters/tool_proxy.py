"""MCP tool proxy recording ActionRequest -> Execution -> ActionResult chains."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..capture import Capture, canonical
from ..extractor import ProcessObjectExtractor


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
        return raw_result

