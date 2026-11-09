"""Append-only capture recorder and session storage for LoopOCEL."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid


BASELINE_EVENT_TYPES = (
    "Turn.Started",
    "Turn.Finished",
    "Turn.Failed",
    "Prompt.Submitted",
    "Response.Received",
    "ActionRequest.Dispatched",
    "ActionRequest.Rejected",
    "Execution.Started",
    "Execution.Finished",
    "ActionResult.Received",
    "Instruction.Registered",
    "History.Assembled",
    "ProcessObject.Created",
    "ProcessObject.StateChanged",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:16]


class Capture:
    """Append-only JSONL capture stream compatible with the freight converter."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "capture.jsonl"
        self.events: list[dict[str, Any]] = []
        self.sequence = self._load_existing_sequence()

    def _load_existing_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        seq = 0
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("kind") == "event":
                    eid = str(record.get("data", {}).get("id", ""))
                    if eid.startswith("event:"):
                        suffix = eid.removeprefix("event:")
                        if suffix.isdigit():
                            seq = max(seq, int(suffix))
        return seq

    def raw(self, kind: str, data: dict[str, Any], *, timestamp: str | None = None) -> str:
        stamp = timestamp or now()
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(canonical({"kind": kind, "time": stamp, "data": data}) + "\n")
        return stamp

    def event(
        self,
        kind: str,
        *,
        timestamp: str | None = None,
        event_id: str | None = None,
        attributes: list[dict[str, str]] | None = None,
        **participants: str | list[str] | None,
    ) -> dict[str, Any]:
        self.sequence += 1
        eid = event_id or f"event:{self.sequence}"
        relations = [
            {"objectId": oid, "qualifier": role}
            for role, ids in participants.items()
            for oid in (ids if isinstance(ids, list) else [ids])
            if oid is not None
        ]
        data: dict[str, Any] = {"id": eid, "type": kind, "relationships": relations}
        if attributes:
            data["attributes"] = attributes
        stamp = self.raw("event", data, timestamp=timestamp)
        entry = {**data, "time": stamp}
        self.events.append(entry)
        return entry

    def business_initial(
        self,
        objects: dict[str, Any],
        *,
        emit_registered: bool = False,
        timestamp: str | None = None,
    ) -> str:
        stamp = self.raw("business_initial", {"objects": objects}, timestamp=timestamp)
        if emit_registered:
            for oid in objects:
                self.event("ProcessObject.Registered", timestamp=stamp, subject=oid)
        return stamp

    def instruction(
        self,
        instruction_id: str,
        messages: list[dict[str, Any]],
        *,
        request_fields: dict[str, Any] | None = None,
        emit_event: bool = True,
        timestamp: str | None = None,
    ) -> str:
        data: dict[str, Any] = {"id": instruction_id, "messages": messages}
        if request_fields is not None:
            data["request_fields"] = request_fields
        stamp = self.raw("instruction", data, timestamp=timestamp)
        if emit_event:
            self.event("Instruction.Registered", timestamp=stamp, subject=instruction_id)
        return stamp

    def turn_start(
        self,
        turn_id: str,
        prompt_id: str,
        index: int,
        task_id: str,
        parameters: dict[str, Any],
        layout: list[str],
        *,
        previous_turn: str | None = None,
        history: dict[str, Any] | None = None,
        request: dict[str, Any] | None = None,
        emit_events: bool = True,
        timestamp: str | None = None,
    ) -> str:
        data: dict[str, Any] = {
            "id": turn_id,
            "prompt_id": prompt_id,
            "index": index,
            "task_id": task_id,
            "previous_turn": previous_turn,
            "parameters": parameters,
            "layout": layout,
            "history": history,
        }
        if request is not None:
            data["request"] = request
        stamp = self.raw("turn_start", data, timestamp=timestamp)
        if emit_events:
            if history is not None:
                self.event("History.Assembled", timestamp=stamp, subject=history["id"], prompt=prompt_id)
            self.event("Turn.Started", timestamp=stamp, subject=turn_id, prompt=prompt_id)
            self.event("Prompt.Submitted", timestamp=stamp, subject=prompt_id, turn=turn_id)
        return stamp

    def response(
        self,
        response_id: str,
        turn_id: str,
        provider: Any,
        message: dict[str, Any],
        *,
        emit_events: bool = True,
        finish_turn: bool = True,
        timestamp: str | None = None,
    ) -> str:
        stamp = self.raw(
            "response",
            {"id": response_id, "turn_id": turn_id, "provider": provider, "message": message},
            timestamp=timestamp,
        )
        if emit_events:
            self.event("Response.Received", timestamp=stamp, subject=response_id, turn=turn_id)
            if finish_turn:
                self.event("Turn.Finished", timestamp=stamp, subject=turn_id, response=response_id)
        return stamp

    def action_request(
        self,
        request_id: str,
        response_id: str,
        turn_id: str,
        name: str,
        arguments: dict[str, Any],
        targets: list[str] | None = None,
        *,
        dispatched: bool = True,
        rejected: bool = False,
        emit_event: bool = True,
        timestamp: str | None = None,
    ) -> str:
        stamp = self.raw(
            "action_request",
            {
                "id": request_id,
                "response_id": response_id,
                "turn_id": turn_id,
                "name": name,
                "arguments": arguments,
                "targets": list(targets or []),
            },
            timestamp=timestamp,
        )
        if emit_event:
            if rejected:
                self.event("ActionRequest.Rejected", timestamp=stamp, subject=request_id, turn=turn_id)
            elif dispatched:
                self.event("ActionRequest.Dispatched", timestamp=stamp, subject=request_id, turn=turn_id)
        return stamp

    def execution_start(
        self,
        execution_id: str,
        request_id: str,
        turn_id: str,
        *,
        emit_event: bool = True,
        timestamp: str | None = None,
    ) -> str:
        stamp = self.raw(
            "execution_start",
            {"id": execution_id, "request_id": request_id, "turn_id": turn_id},
            timestamp=timestamp,
        )
        if emit_event:
            self.event("Execution.Started", timestamp=stamp, subject=execution_id, request=request_id)
        return stamp

    def execution_finish(
        self,
        execution_id: str,
        request_id: str,
        turn_id: str,
        outcome: str,
        acted_on: list[str] | None = None,
        *,
        emit_event: bool = True,
        timestamp: str | None = None,
    ) -> str:
        acted = list(acted_on or [])
        stamp = self.raw(
            "execution_finish",
            {
                "id": execution_id,
                "request_id": request_id,
                "turn_id": turn_id,
                "outcome": outcome,
                "acted_on": acted,
            },
            timestamp=timestamp,
        )
        if emit_event:
            self.event(
                "Execution.Finished",
                timestamp=stamp,
                subject=execution_id,
                request=request_id,
                process_object=acted,
            )
        return stamp

    def action_result(
        self,
        result_id: str,
        request_id: str,
        execution_id: str | None,
        message: dict[str, Any],
        data: Any,
        describes: list[str] | None = None,
        *,
        turn_id: str | None = None,
        emit_event: bool = True,
        timestamp: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "id": result_id,
            "request_id": request_id,
            "execution_id": execution_id,
            "message": message,
            "data": data,
            "describes": list(describes or []),
        }
        if turn_id is not None:
            payload["turn_id"] = turn_id
        stamp = self.raw("action_result", payload, timestamp=timestamp)
        if emit_event:
            self.event(
                "ActionResult.Received",
                timestamp=stamp,
                subject=result_id,
                execution=execution_id,
                request=request_id,
            )
        return stamp

    def business_change(
        self,
        object_id: str,
        state: dict[str, Any],
        *,
        created: bool,
        execution_id: str | None = None,
        turn_id: str | None = None,
        emit_event: bool = True,
        timestamp: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "id": object_id,
            "state": state,
            "created": created,
            "execution_id": execution_id,
            "turn_id": turn_id,
        }
        stamp = self.raw("business_change", payload, timestamp=timestamp)
        if emit_event:
            self.event(
                "ProcessObject.Created" if created else "ProcessObject.StateChanged",
                timestamp=stamp,
                subject=object_id,
                execution=execution_id,
            )
        return stamp


class SessionStore:
    """Manages recording sessions under a root directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            raise ValueError(f"Invalid session_id: {session_id!r}")
        return self.root / session_id

    def create(
        self,
        *,
        task: str = "agent-task",
        agent: str = "agent",
        profile: str = "general",
        mode: str = "mcp",
        session_id: str | None = None,
        id_fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        sid = session_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        directory = self.session_dir(sid)
        directory.mkdir(parents=True, exist_ok=True)
        completeness = "complete" if mode in ("llm-proxy", "mcp", "harness") else "partial"
        meta: dict[str, Any] = {
            "session_id": sid,
            "task": task,
            "agent": agent,
            "profile": profile,
            "mode": mode,
            "capture_completeness": completeness,
            "status": "recording",
            "started_at": now(),
            "finished_at": None,
            "directory": str(directory.resolve()),
        }
        if id_fields:
            meta["id_fields"] = id_fields
        write_json(directory / "session.json", meta)
        (directory / "capture.jsonl").touch(exist_ok=True)
        return meta

    def get_meta(self, session_id: str) -> dict[str, Any]:
        directory = self.session_dir(session_id)
        meta_path = directory / "session.json"
        if not meta_path.is_file():
            if (directory / "capture.jsonl").is_file() or (directory / "loopocel.json").is_file():
                return {
                    "session_id": session_id,
                    "task": session_id,
                    "agent": "external",
                    "profile": "generic",
                    "mode": "external",
                    "capture_completeness": "complete",
                    "status": "completed" if (directory / "loopocel.json").is_file() else "recording",
                    "started_at": None,
                    "finished_at": None,
                    "directory": str(directory.resolve()),
                }
            raise FileNotFoundError(f"Unknown session: {session_id}")
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def update_meta(self, session_id: str, **updates: Any) -> dict[str, Any]:
        meta = self.get_meta(session_id)
        meta.update(updates)
        write_json(self.session_dir(session_id) / "session.json", meta)
        return meta

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return sessions
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            if any((child / name).is_file() for name in ("session.json", "capture.jsonl", "loopocel.json")):
                try:
                    meta = self.get_meta(child.name)
                except Exception:
                    continue
                meta["has_capture"] = (child / "capture.jsonl").is_file()
                meta["has_ocel"] = (child / "loopocel.json").is_file()
                meta["has_figure_svg"] = (child / "figure.svg").is_file()
                meta["has_figure_png"] = (child / "figure.png").is_file()
                sessions.append(meta)
        return sessions

