"""Anthropic and OpenAI-compatible HTTP recording proxy for full-fidelity LoopOCEL capture."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from ..capture import Capture, canonical
from ..extractor import ProcessObjectExtractor


class LLMRecordingRecorder:
    """Stateful recorder translating raw LLM HTTP requests and responses into LoopOCEL capture records."""

    def __init__(
        self,
        session_dir: Path | str,
        *,
        task_id: str = "proxy-task",
        id_fields: dict[str, str] | None = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.cap = Capture(self.session_dir)
        self.extractor = ProcessObjectExtractor(custom_fields=id_fields)
        self.task_id = task_id
        self.turn_index = 0
        self.req_index = 0
        self.previous_turn: str | None = None
        self.instructions_seen: set[str] = set()
        self.prior_sources: list[str] = []
        self.call_id_to_request: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        self.emitted_results: set[str] = set()
        self._lock = threading.Lock()

    def record_exchange(self, path: str, request_body: dict[str, Any], response_body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.turn_index += 1
            idx = self.turn_index
            tid = f"turn:{idx}"
            pid = f"prompt:{idx}"
            rid = f"response:{idx}"

            is_anthropic = "/v1/messages" in path or "anthropic_version" in request_body or isinstance(request_body.get("system"), (str, list))
            messages = list(request_body.get("messages") or [])
            system_content = request_body.get("system")

            base_msgs: list[dict[str, Any]] = []
            if system_content is not None:
                base_msgs.append({"role": "system", "content": system_content})
            for m in messages:
                if m.get("role") == "system":
                    base_msgs.append(m)
                elif m.get("role") == "user" and not base_msgs and not self._has_tool_result(m):
                    base_msgs.append(m)
                    break
            if not base_msgs and messages:
                base_msgs.append(messages[0])

            inst_hash = hashlib.sha256(canonical(base_msgs).encode("utf-8")).hexdigest()[:12]
            iid = f"instruction:{inst_hash}"
            req_fields: dict[str, Any] = {}
            if "response_format" in request_body:
                req_fields["response_format"] = request_body["response_format"]
            if iid not in self.instructions_seen:
                self.cap.instruction(
                    iid,
                    base_msgs,
                    request_fields=req_fields if req_fields else None,
                )
                self.instructions_seen.add(iid)

            # Extract any new tool results supplied in the incoming messages
            admitted_results: list[str] = []
            for m in messages:
                for call_id, content, is_err in self._extract_tool_results(m):
                    if call_id in self.call_id_to_request and call_id not in self.emitted_results:
                        aid, eid, tname, targs = self.call_id_to_request[call_id]
                        res_num = aid.removeprefix("request:")
                        res_id = f"result:{res_num}"
                        outcome = "rejected" if is_err else "success"
                        parsed_data = self._parse_json_or_wrap(content)
                        obs = self.extractor.observe_execution_and_result(
                            tname,
                            targs,
                            parsed_data,
                            outcome=outcome,
                        )
                        for oid, state in obs.initial_objects.items():
                            self.cap.business_change(oid, state, created=True, turn_id=self.previous_turn, emit_event=False)
                            self.cap.event("ProcessObject.Registered", subject=oid)
                        for change in obs.changes:
                            self.cap.business_change(
                                change["id"],
                                change["state"],
                                created=change["created"],
                                execution_id=eid,
                                turn_id=self.previous_turn,
                            )
                        self.cap.execution_finish(eid, aid, self.previous_turn or tid, outcome, obs.acted_on)
                        feedback_msg = {"role": "user", "content": canonical({"tool_result": {"request_id": aid, "name": tname, "result": parsed_data}})}
                        self.cap.action_result(
                            res_id,
                            aid,
                            eid,
                            feedback_msg,
                            parsed_data,
                            obs.describes,
                            turn_id=self.previous_turn or tid,
                        )
                        self.emitted_results.add(call_id)
                        admitted_results.append(res_id)
                        self.prior_sources.append(res_id)

            history_sources = [s for s in self.prior_sources if s not in admitted_results]
            history = {"id": f"history:{idx}", "sources": history_sources} if history_sources else None
            layout = [iid] + ([history["id"]] if history else []) + admitted_results
            parameters = {k: v for k, v in request_body.items() if k not in ("messages", "system")}

            self.cap.turn_start(
                tid,
                pid,
                idx,
                self.task_id,
                parameters,
                layout,
                previous_turn=self.previous_turn,
                history=history,
                request=request_body,
            )

            assistant_msg, tool_calls = self._extract_response_and_calls(response_body, is_anthropic=is_anthropic)
            self.cap.response(rid, tid, response_body, assistant_msg)
            self.prior_sources.append(rid)

            for call_id, tname, targs in tool_calls:
                self.req_index += 1
                aid = f"request:{self.req_index}"
                eid = f"execution:{self.req_index}"
                req_obs = self.extractor.observe_request(tname, targs)
                for oid, state in req_obs.initial_objects.items():
                    self.cap.business_change(oid, state, created=True, turn_id=tid, emit_event=False)
                    self.cap.event("ProcessObject.Registered", subject=oid)
                self.cap.action_request(aid, rid, tid, tname, targs, req_obs.targets)
                self.cap.execution_start(eid, aid, tid)
                self.call_id_to_request[call_id] = (aid, eid, tname, targs)

            self.previous_turn = tid
            return {"turn_id": tid, "prompt_id": pid, "response_id": rid, "tool_calls": len(tool_calls)}

    @staticmethod
    def _has_tool_result(message: dict[str, Any]) -> bool:
        if message.get("role") == "tool":
            return True
        content = message.get("content")
        if isinstance(content, list):
            return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        return False

    @staticmethod
    def _extract_tool_results(message: dict[str, Any]) -> list[tuple[str, Any, bool]]:
        out: list[tuple[str, Any, bool]] = []
        if message.get("role") == "tool":
            cid = message.get("tool_call_id") or message.get("id")
            if isinstance(cid, str):
                out.append((cid, message.get("content"), False))
        elif isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    cid = block.get("tool_use_id") or block.get("id")
                    if isinstance(cid, str):
                        out.append((cid, block.get("content"), bool(block.get("is_error", False))))
        return out

    @staticmethod
    def _parse_json_or_wrap(val: Any) -> Any:
        if isinstance(val, (dict, list)):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return {"text": val}
        return {"value": val}

    @staticmethod
    def _extract_response_and_calls(
        response_body: dict[str, Any],
        *,
        is_anthropic: bool,
    ) -> tuple[dict[str, Any], list[tuple[str, str, dict[str, Any]]]]:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        if is_anthropic or "content" in response_body and "choices" not in response_body:
            content = response_body.get("content", [])
            msg = {"role": response_body.get("role", "assistant"), "content": deepcopy(content)}
            if isinstance(content, list):
                for idx, block in enumerate(content, 1):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        cid = block.get("id") or f"toolu_{idx}"
                        tname = block.get("name", "tool")
                        targs = block.get("input") if isinstance(block.get("input"), dict) else {}
                        calls.append((cid, tname, targs))
            return msg, calls

        choices = response_body.get("choices") or [{}]
        msg = deepcopy(choices[0].get("message") or {"role": "assistant", "content": ""})
        for idx, tc in enumerate(msg.get("tool_calls") or (), 1):
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else tc
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {"raw": raw_args}
            cid = tc.get("id") or f"call_{idx}"
            calls.append((cid, fn.get("name", "tool"), raw_args if isinstance(raw_args, dict) else {}))
        return msg, calls


class LLMProxyServer:
    """Local HTTP proxy forwarding requests to an upstream LLM API while recording LoopOCEL evidence."""

    def __init__(
        self,
        session_dir: Path | str,
        upstream_base_url: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        task_id: str = "proxy-task",
    ) -> None:
        self.recorder = LLMRecordingRecorder(session_dir, task_id=task_id)
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw_req = self.rfile.read(length) if length > 0 else b"{}"
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ("host", "content-length", "accept-encoding")
                }
                headers["Accept-Encoding"] = "identity"
                upstream_url = parent.upstream_base_url + self.path
                req = Request(upstream_url, data=raw_req, headers=headers, method="POST")
                try:
                    with build_opener(ProxyHandler({})).open(req, timeout=120) as resp:
                        status = resp.status
                        resp_headers = list(resp.headers.items())
                        raw_resp = resp.read()
                except HTTPError as exc:
                    status = exc.code
                    resp_headers = list(exc.headers.items()) if exc.headers else []
                    raw_resp = exc.read()

                try:
                    req_json = json.loads(raw_req.decode("utf-8"))
                    resp_json = json.loads(raw_resp.decode("utf-8"))
                    if isinstance(req_json, dict) and isinstance(resp_json, dict) and status == 200:
                        parent.recorder.record_exchange(self.path, req_json, resp_json)
                except Exception:
                    pass

                self.send_response(status)
                for hk, hv in resp_headers:
                    if hk.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                        self.send_header(hk, hv)
                self.send_header("Content-Length", str(len(raw_resp)))
                self.end_headers()
                self.wfile.write(raw_resp)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

