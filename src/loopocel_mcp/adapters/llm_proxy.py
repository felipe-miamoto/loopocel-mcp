"""Anthropic and OpenAI-compatible HTTP recording proxy for full-fidelity LoopOCEL capture."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from ..capture import Capture, canonical, write_json
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
            if "messages" not in request_body and "input" in request_body:
                # OpenAI Responses API: `instructions` + `input` items.
                messages = self._responses_input_to_messages(request_body.get("input"))
                system_content = request_body.get("instructions")
            else:
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
            parameters = {k: v for k, v in request_body.items() if k not in ("messages", "system", "input", "instructions")}

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
    def _responses_input_to_messages(items: Any) -> list[dict[str, Any]]:
        if isinstance(items, str):
            return [{"role": "user", "content": items}]
        out: list[dict[str, Any]] = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call_output":
                out.append({"role": "tool", "tool_call_id": item.get("call_id"), "content": item.get("output")})
            elif item.get("role") in ("user", "system", "developer", "assistant"):
                role = "system" if item["role"] == "developer" else item["role"]
                out.append({"role": role, "content": item.get("content")})
        return out

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
        if isinstance(response_body.get("output"), list):
            # OpenAI Responses API: `output` holds message and function_call items.
            texts: list[Any] = []
            for idx, item in enumerate(response_body["output"], 1):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "function_call":
                    raw_args = item.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            raw_args = {"raw": raw_args}
                    cid = item.get("call_id") or item.get("id") or f"call_{idx}"
                    calls.append((cid, item.get("name", "tool"), raw_args if isinstance(raw_args, dict) else {}))
                elif item.get("type") == "message":
                    texts.extend(
                        c.get("text", "") for c in item.get("content") or () if isinstance(c, dict)
                    )
            return {"role": "assistant", "content": "".join(str(t) for t in texts)}, calls
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


def reassemble_sse_response(raw_sse: str, *, is_anthropic: bool = False) -> dict[str, Any] | None:
    """Reassemble an Anthropic or OpenAI Server-Sent Events (SSE) stream into a single response object."""
    events: list[dict[str, Any]] = []
    for line in raw_sse.splitlines():
        s = line.strip()
        if not s.startswith("data:"):
            continue
        payload = s[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError:
            continue

    if not events:
        return None

    # OpenAI Responses API streams end with the complete response object.
    for ev in reversed(events):
        if ev.get("type") == "response.completed" and isinstance(ev.get("response"), dict):
            return ev["response"]

    # Detect Anthropic SSE format
    if is_anthropic or any(e.get("type") in ("message_start", "content_block_start", "content_block_delta") for e in events):
        msg_meta: dict[str, Any] = {"role": "assistant", "content": []}
        blocks_by_index: dict[int, dict[str, Any]] = {}
        partial_json_by_index: dict[int, list[str]] = {}
        for ev in events:
            etype = ev.get("type")
            if etype == "message_start" and isinstance(ev.get("message"), dict):
                for k, v in ev["message"].items():
                    if k != "content":
                        msg_meta[k] = deepcopy(v)
            elif etype == "content_block_start":
                idx = int(ev.get("index", len(blocks_by_index)))
                cb = deepcopy(ev.get("content_block") or {"type": "text", "text": ""})
                blocks_by_index[idx] = cb
                if cb.get("type") == "tool_use":
                    partial_json_by_index[idx] = []
            elif etype == "content_block_delta":
                idx = int(ev.get("index", 0))
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                block = blocks_by_index.setdefault(idx, {"type": "text", "text": ""})
                if dtype == "text_delta":
                    block["text"] = str(block.get("text") or "") + str(delta.get("text") or "")
                elif dtype == "input_json_delta":
                    partial_json_by_index.setdefault(idx, []).append(str(delta.get("partial_json") or ""))
            elif etype == "message_delta":
                delta = ev.get("delta") or {}
                for k, v in delta.items():
                    msg_meta[k] = v

        assembled_blocks: list[dict[str, Any]] = []
        for idx in sorted(blocks_by_index):
            block = blocks_by_index[idx]
            if block.get("type") == "tool_use" and idx in partial_json_by_index:
                raw_json = "".join(partial_json_by_index[idx]).strip()
                if raw_json:
                    try:
                        block["input"] = json.loads(raw_json)
                    except json.JSONDecodeError:
                        block["input"] = {"raw": raw_json}
                elif not isinstance(block.get("input"), dict):
                    block["input"] = {}
            assembled_blocks.append(block)
        msg_meta["content"] = assembled_blocks
        return msg_meta

    # OpenAI chat.completion.chunk SSE format
    content_parts: list[str] = []
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    resp_meta: dict[str, Any] = {"object": "chat.completion"}
    for ev in events:
        for k in ("id", "model", "created"):
            if k in ev and k not in resp_meta:
                resp_meta[k] = ev[k]
        choices = ev.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        delta = c0.get("delta") if isinstance(c0.get("delta"), dict) else {}
        if isinstance(delta.get("content"), str):
            content_parts.append(delta["content"])
        for tc in delta.get("tool_calls") or ():
            if not isinstance(tc, dict):
                continue
            t_idx = int(tc.get("index", 0))
            entry = tool_calls_by_index.setdefault(
                t_idx,
                {"id": tc.get("id") or f"call_{t_idx + 1}", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if tc.get("id"):
                entry["id"] = tc["id"]
            fn = tc.get("function")
            if isinstance(fn, dict):
                if fn.get("name"):
                    entry["function"]["name"] += str(fn["name"])
                if fn.get("arguments"):
                    entry["function"]["arguments"] += str(fn["arguments"])

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls_by_index:
        message["tool_calls"] = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
    resp_meta["choices"] = [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls_by_index else "stop"}]
    return resp_meta


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
        self.exchanges = 0
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def _forward(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_req = self.rfile.read(length) if length > 0 else b""
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ("host", "content-length", "accept-encoding", "connection")
                }
                headers["Accept-Encoding"] = "identity"
                upstream_url = parent.upstream_base_url + self.path
                req = Request(upstream_url, data=raw_req if method == "POST" else None, headers=headers, method=method)
                try:
                    resp = build_opener(ProxyHandler({})).open(req, timeout=600)
                    status = resp.status
                except HTTPError as exc:
                    resp = exc
                    status = exc.code
                resp_headers = list(resp.headers.items()) if resp.headers else []
                content_type = dict((k.lower(), v) for k, v in resp_headers).get("content-type", "")
                streaming = "text/event-stream" in content_type.lower()

                self.send_response(status)
                for hk, hv in resp_headers:
                    if hk.lower() not in ("transfer-encoding", "content-encoding", "content-length", "connection"):
                        self.send_header(hk, hv)
                if streaming:
                    # Pass events through as they arrive; the body ends when the connection closes.
                    self.send_header("Connection", "close")
                    self.end_headers()
                    chunks: list[bytes] = []
                    while True:
                        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    raw_resp = b"".join(chunks)
                    self.close_connection = True
                else:
                    raw_resp = resp.read()
                    self.send_header("Content-Length", str(len(raw_resp)))
                    self.end_headers()
                    self.wfile.write(raw_resp)

                if method == "POST" and status == 200:
                    parent.record(self.path, raw_req, raw_resp, streaming)

            def do_POST(self) -> None:  # noqa: N802
                self._forward("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._forward("GET")

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{self.host}:{self.port}"

    def record(self, path: str, raw_req: bytes, raw_resp: bytes, streaming: bool) -> None:
        try:
            req_json = json.loads(raw_req.decode("utf-8"))
            if not isinstance(req_json, dict):
                return
            text_resp = raw_resp.decode("utf-8", errors="replace")
            if streaming or text_resp.lstrip().startswith(("event:", "data:")):
                is_anth = "/v1/messages" in path or "anthropic_version" in req_json
                resp_json = reassemble_sse_response(text_resp, is_anthropic=is_anth)
            else:
                resp_json = json.loads(text_resp)
            if isinstance(resp_json, dict):
                self.recorder.record_exchange(path, req_json, resp_json)
                self.exchanges += 1
        except Exception as exc:  # recording must never break the agent's call
            print(f"loopocel proxy: could not record {path}: {type(exc).__name__}: {exc}", file=sys.stderr)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def finalize(self) -> dict[str, Any] | None:
        """Convert, validate and render the recorded session (if any exchange was recorded)."""
        if not self.exchanges:
            return None
        from ..convert import convert
        from ..figure import render_session_figure
        from ..process_graphs import render_session_process_graphs
        from ..validate import validate_log

        sdir = self.recorder.session_dir
        meta_path = sdir / "session.json"
        if not meta_path.is_file():
            write_json(
                meta_path,
                {
                    "session_id": sdir.name,
                    "task": self.recorder.task_id,
                    "agent": "llm-api",
                    "mode": "llm-proxy",
                    "recorded_via": "llm-proxy",
                    "capture_completeness": "complete",
                    "status": "completed",
                },
            )
        log = convert(sdir)
        validation = validate_log(log, ocel_path=sdir / "loopocel.json")
        write_json(sdir / "validation.json", validation)
        return {
            "exchanges": self.exchanges,
            "validation_passed": validation["passed"],
            "figure": render_session_figure(sdir)["outputs"],
            "process_graphs": render_session_process_graphs(sdir)["outputs"],
        }

