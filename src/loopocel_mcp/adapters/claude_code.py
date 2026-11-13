"""Hook and transcript adapters for Claude Code, Codex, Antigravity and OpenAI/Anthropic chat JSON."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from ..capture import Capture, canonical, now, write_json
from ..extractor import ProcessObjectExtractor


def _ensure_iso_tz(ts: Any) -> str:
    if not isinstance(ts, str) or not ts.strip():
        return now()
    s = ts.strip()
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat(timespec="microseconds")
    except ValueError:
        return now()


def _instruction_id(content: Any) -> str:
    digest = hashlib.sha256(canonical(content).encode("utf-8")).hexdigest()[:12]
    return f"instruction:{digest}"


HOOK_AGENTS = ("claude-code", "codex", "antigravity")


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        val = payload.get(key)
        if val not in (None, ""):
            return val
    return None


def hook_agent(payload: dict[str, Any]) -> str:
    """Which agent sent a hook payload: Antigravity uses camelCase, Codex writes rollout transcripts."""
    if "conversationId" in payload or "transcriptPath" in payload:
        return "antigravity"
    tpath = str(payload.get("transcript_path") or "")
    if "/.codex/" in tpath or Path(tpath).name.startswith("rollout-"):
        return "codex"
    return "claude-code"


def hook_event_name(payload: dict[str, Any], event: str | None = None) -> str:
    """Event name from the CLI flag, the payload, or (Antigravity Stop) the payload shape."""
    name = event or _first(payload, "hook_event_name", "hookEventName", "event")
    if name:
        return str(name)
    if "fullyIdle" in payload or "terminationReason" in payload:
        return "Stop"
    if "toolCall" in payload:
        return "PostToolUse" if "error" in payload else "PreToolUse"
    return ""


def hook_session_dir(root: Path | str, payload: dict[str, Any]) -> Path:
    """Folder for one agent session: <root>/<session_id> when the hook payload names one."""
    root = Path(root)
    sid = _first(payload, "session_id", "conversationId", "conversation_id")
    if isinstance(sid, str) and sid.strip():
        return root / re.sub(r"[^A-Za-z0-9._-]+", "-", sid.strip())
    return root


def finalize_hook_session(session_dir: Path, transcript_path: Path, agent: str = "claude-code") -> dict[str, Any]:
    """Ingest the transcript with the recorded hooks, convert and validate."""
    from ..convert import convert
    from ..validate import validate_log

    meta_path = session_dir / "session.json"
    if not meta_path.is_file():
        write_json(
            meta_path,
            {
                "session_id": session_dir.name,
                "task": session_dir.name,
                "agent": agent,
                "mode": f"{agent}-hooks",
                "recorded_via": f"{agent}-hooks",
                "capture_completeness": "partial",
                "status": "recording",
            },
        )
    stats = ingest_transcript(session_dir, transcript_path)
    log = convert(session_dir)
    validation = validate_log(log, ocel_path=session_dir / "loopocel.json")
    write_json(session_dir / "validation.json", validation)
    return {**stats, "validation_passed": validation["passed"]}


def record_hook_event(session_dir: Path | str, payload: dict[str, Any], event: str | None = None) -> dict[str, Any]:
    """Append a raw hook event from Claude Code, Codex or Antigravity.

    `session_dir` is a root: each agent session (hook `session_id`) gets its own folder.
    `Stop` and `SessionEnd` re-ingest the transcript, convert and validate it.
    """
    session_dir = hook_session_dir(session_dir, payload)
    session_dir.mkdir(parents=True, exist_ok=True)
    hooks_file = session_dir / "hooks.jsonl"
    stamp = now()
    entry = {"time": stamp, "payload": payload}
    with hooks_file.open("a", encoding="utf-8") as stream:
        stream.write(canonical(entry) + "\n")

    event_name = hook_event_name(payload, event)
    agent = hook_agent(payload)
    transcript_path = _first(payload, "transcript_path", "transcriptPath")
    if event_name in ("Stop", "SessionEnd") and isinstance(transcript_path, str) and Path(transcript_path).is_file():
        try:
            finalize_hook_session(session_dir, Path(transcript_path), agent=agent)
        except Exception as exc:  # a hook must never break the agent
            print(f"loopocel hook: could not process {session_dir}: {exc}", file=sys.stderr)
    return {
        "recorded": True,
        "agent": agent,
        "hook_event_name": event_name,
        "time": stamp,
        "session_dir": str(session_dir),
    }


def _load_hooks(session_dir: Path) -> list[dict[str, Any]]:
    hooks_file = session_dir / "hooks.jsonl"
    if not hooks_file.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with hooks_file.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def ingest_transcript(
    session_dir: Path | str,
    transcript_path: Path | str,
    *,
    task_id: str | None = None,
    id_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Parse an agent transcript (Claude Code, Codex or Antigravity JSONL, or chat JSON) into capture.jsonl."""
    session_dir = Path(session_dir)
    transcript_path = Path(transcript_path)
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    effective_task = task_id or meta.get("task") or session_dir.name
    effective_fields = id_fields or meta.get("id_fields")

    raw_text = transcript_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError(f"Empty transcript: {transcript_path}")

    if raw_text.startswith("{") and ("\n" not in raw_text or raw_text.endswith("}")):
        try:
            doc = json.loads(raw_text)
            if isinstance(doc, dict) and "messages" in doc:
                return _ingest_normalized_turns(
                    session_dir,
                    _normalize_chat_messages(doc["messages"]),
                    task_id=effective_task,
                    id_fields=effective_fields,
                    partial=True,
                )
        except json.JSONDecodeError:
            pass

    lines = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"No records in transcript: {transcript_path}")

    first = lines[0]
    if first.get("type") in CODEX_LINE_TYPES and "payload" in first:
        turns = _normalize_codex_rollout(lines)
    elif "step_index" in first and "type" in first:
        turns = _normalize_antigravity_jsonl(lines)
    else:
        turns = _normalize_claude_code_jsonl(lines, _load_hooks(session_dir))

    return _ingest_normalized_turns(
        session_dir,
        turns,
        task_id=effective_task,
        id_fields=effective_fields,
        partial=True,
    )


def _normalize_claude_code_jsonl(
    lines: list[dict[str, Any]],
    hooks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize Claude Code transcript JSONL + optional hook records into ordered turn dicts."""
    pre_hooks_by_id: dict[str, dict[str, Any]] = {}
    post_hooks_by_id: dict[str, dict[str, Any]] = {}
    rejected_ids: set[str] = set()
    compaction_times: list[str] = []

    for h in hooks:
        hp = h.get("payload", {})
        hname = hp.get("hook_event_name") or hp.get("event")
        tuid = hp.get("tool_use_id") or hp.get("id")
        if hname == "PreToolUse" and isinstance(tuid, str):
            pre_hooks_by_id[tuid] = h
        elif hname == "PostToolUse" and isinstance(tuid, str):
            post_hooks_by_id[tuid] = h
        elif hname in ("PermissionRequest", "PreToolUse") and hp.get("decision") in ("deny", "block", "rejected"):
            if isinstance(tuid, str):
                rejected_ids.add(tuid)
        elif hname == "PreCompact":
            compaction_times.append(h.get("time", now()))

    pending_user_messages: list[tuple[str, list[dict[str, Any]]]] = []
    pending_tool_results: dict[str, dict[str, Any]] = {}
    seen_tool_use_ids: set[Any] = set()
    compacted_flag = False
    turns: list[dict[str, Any]] = []

    for row in lines:
        stamp = _ensure_iso_tz(row.get("timestamp") or row.get("created_at") or row.get("time"))
        msg = row.get("message") if isinstance(row.get("message"), dict) else row
        role = msg.get("role") or row.get("type")
        content = msg.get("content")

        if row.get("type") == "summary" or row.get("isCompactSummary"):
            compacted_flag = True
            continue

        if role == "user":
            if isinstance(content, list):
                user_text_blocks: list[dict[str, Any]] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tuid = block.get("tool_use_id") or block.get("id")
                        if isinstance(tuid, str):
                            pending_tool_results[tuid] = {
                                "time": stamp,
                                "content": block.get("content"),
                                "is_error": bool(block.get("is_error", False)),
                                "raw": block,
                            }
                    else:
                        user_text_blocks.append(block if isinstance(block, dict) else {"type": "text", "text": str(block)})
                if user_text_blocks:
                    pending_user_messages.append((stamp, [{"role": "user", "content": user_text_blocks}]))
            elif isinstance(content, str) and content.strip():
                pending_user_messages.append((stamp, [{"role": "user", "content": content}]))

        elif role == "assistant":
            tool_uses: list[dict[str, Any]] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("id") in seen_tool_use_ids:
                            continue
                        seen_tool_use_ids.add(block.get("id"))
                        tool_uses.append(
                            {
                                "id": block.get("id") or f"call_{len(tool_uses) + 1}",
                                "name": block.get("name", "tool"),
                                "arguments": block.get("input") if isinstance(block.get("input"), dict) else {},
                            }
                        )
            elif isinstance(msg.get("tool_calls"), list):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else tc
                    raw_args = fn.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            raw_args = {"raw": raw_args}
                    tool_uses.append(
                        {
                            "id": tc.get("id") or f"call_{len(tool_uses) + 1}",
                            "name": fn.get("name", "tool"),
                            "arguments": raw_args if isinstance(raw_args, dict) else {},
                        }
                    )

            # Claude Code writes one row per content block; rows sharing a message id
            # belong to the same model response and form one turn.
            message_id = msg.get("id")
            if (
                message_id
                and turns
                and turns[-1].get("message_id") == message_id
                and not pending_user_messages
            ):
                last = turns[-1]
                last_content = last["message"]["content"]
                merged = (last_content if isinstance(last_content, list) else [last_content]) + (
                    content if isinstance(content, list) else [content]
                )
                last["message"]["content"] = [b for b in merged if b not in (None, "")]
                last["tool_uses"].extend(tool_uses)
                continue

            turns.append(
                {
                    "time": stamp,
                    "message_id": message_id,
                    "user_instructions": list(pending_user_messages),
                    "compacted": compacted_flag or bool(compaction_times),
                    "model": msg.get("model", "assistant"),
                    "message": {"role": "assistant", "content": content},
                    "provider": row,
                    "tool_uses": tool_uses,
                    "pre_hooks": pre_hooks_by_id,
                    "post_hooks": post_hooks_by_id,
                    "rejected_ids": rejected_ids,
                }
            )
            pending_user_messages.clear()
            compacted_flag = False

    for t in turns:
        for tu in t["tool_uses"]:
            tuid = tu["id"]
            if tuid in pending_tool_results:
                tu["result"] = pending_tool_results[tuid]
            elif tuid in post_hooks_by_id:
                hp = post_hooks_by_id[tuid]["payload"]
                tu["result"] = {
                    "time": post_hooks_by_id[tuid]["time"],
                    "content": hp.get("tool_response", {}),
                    "is_error": bool(hp.get("is_error", False)),
                    "raw": hp,
                }

    return turns


def _normalize_antigravity_jsonl(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize Antigravity transcript.jsonl records into ordered turn dicts."""
    pending_user: list[tuple[str, list[dict[str, Any]]]] = []
    turns: list[dict[str, Any]] = []

    awaiting_output: list[dict[str, Any]] = []
    for row in lines:
        stype = row.get("type")
        stamp = _ensure_iso_tz(row.get("created_at"))
        if stype != "PLANNER_RESPONSE" and stype != "USER_INPUT" and awaiting_output:
            # Antigravity logs each tool's output as the next non-planner row.
            tu = awaiting_output.pop(0)
            out = row.get("content", "")
            exit_code = re.search(r"exited with code (-?\d+)", out) if isinstance(out, str) else None
            tu["result"] = {
                "time": stamp,
                "content": out,
                "is_error": row.get("status") == "ERROR" or bool(exit_code and exit_code.group(1) != "0"),
                "raw": out,
            }
            continue
        if stype == "USER_INPUT":
            text = row.get("content", "")
            pending_user.append((stamp, [{"role": "user", "content": text}]))
        elif stype == "PLANNER_RESPONSE":
            tcalls = row.get("tool_calls") or []
            tool_uses: list[dict[str, Any]] = []
            for idx, tc in enumerate(tcalls, 1):
                if not isinstance(tc, dict):
                    continue
                tname = tc.get("name") or tc.get("tool_name") or "tool"
                targs = tc.get("arguments") or tc.get("args") or {}
                if isinstance(targs, str):
                    try:
                        targs = json.loads(targs)
                    except json.JSONDecodeError:
                        targs = {"raw": targs}
                if isinstance(targs, dict):
                    # Antigravity stores argument values as JSON-encoded strings ('"ls -la"').
                    targs = {k: _decode_json_value(v) for k, v in targs.items()}
                tid = tc.get("id") or f"step_{row.get('step_index', 0)}_call_{idx}"
                tresult = tc.get("result") or tc.get("output")
                tu: dict[str, Any] = {
                    "id": tid,
                    "name": tname,
                    "arguments": targs if isinstance(targs, dict) else {},
                }
                if tresult is not None:
                    tu["result"] = {
                        "time": stamp,
                        "content": tresult,
                        "is_error": row.get("status") == "ERROR",
                        "raw": tresult,
                    }
                tool_uses.append(tu)
                if "result" not in tu:
                    awaiting_output.append(tu)

            turns.append(
                {
                    "time": stamp,
                    "user_instructions": list(pending_user),
                    "compacted": False,
                    "model": "antigravity",
                    "message": {"role": "assistant", "content": row.get("content", "")},
                    "provider": {"step_index": row.get("step_index"), "status": row.get("status")},
                    "tool_uses": tool_uses,
                    "pre_hooks": {},
                    "post_hooks": {},
                    "rejected_ids": set(),
                }
            )
            pending_user.clear()

    return turns


CODEX_LINE_TYPES = {"session_meta", "turn_context", "response_item", "event_msg", "world_state"}
_CODEX_CALLS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}
_CODEX_OUTPUTS = {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}


def _codex_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and "text" in b)
    return "" if content is None else json.dumps(content)


def _normalize_codex_rollout(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a Codex rollout (~/.codex/sessions/.../rollout-*.jsonl) into ordered turn dicts.

    One turn is one model response: the assistant message and tool calls that follow a user
    message or a tool output. Tool outputs are matched to their calls by `call_id`.
    """
    pending_user: list[tuple[str, list[dict[str, Any]]]] = []
    turns: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    model = "codex"
    current: dict[str, Any] | None = None

    for row in lines:
        rtype = row.get("type")
        item = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        stamp = _ensure_iso_tz(row.get("timestamp"))
        if rtype == "turn_context" and item.get("model"):
            model = str(item["model"])
            continue
        if rtype != "response_item":
            continue
        itype = item.get("type")
        if itype == "message" and item.get("role") == "user":
            pending_user.append((stamp, [{"role": "user", "content": _codex_text(item.get("content"))}]))
            current = None
        elif itype in _CODEX_OUTPUTS:
            out = item.get("output")
            text = _codex_text(out.get("content") if isinstance(out, dict) and "content" in out else out)
            exit_code = re.search(r"(?:exit code|exited with code)[:\s]+(-?\d+)", text, re.I)
            tu = calls.get(str(item.get("call_id")))
            if tu is not None:
                tu["result"] = {
                    "time": stamp,
                    "content": text,
                    "is_error": bool(exit_code and exit_code.group(1) != "0"),
                    "raw": out,
                }
            current = None
        elif (itype == "message" and item.get("role") == "assistant") or itype in _CODEX_CALLS:
            if current is None:
                current = {
                    "time": stamp,
                    "user_instructions": list(pending_user),
                    "compacted": False,
                    "model": model,
                    "message": {"role": "assistant", "content": ""},
                    "provider": {"response_id": item.get("id")},
                    "tool_uses": [],
                    "pre_hooks": {},
                    "post_hooks": {},
                    "rejected_ids": set(),
                }
                turns.append(current)
                pending_user.clear()
            if itype == "message":
                text = _codex_text(item.get("content"))
                prev = current["message"]["content"]
                current["message"]["content"] = f"{prev}\n{text}" if prev else text
                continue
            raw_args = item.get("arguments", item.get("input", item.get("action", {})))
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {"input": raw_args}
            cid = str(item.get("call_id") or item.get("id") or f"codex_call_{len(calls) + 1}")
            tu = {
                "id": cid,
                "name": item.get("name") or itype.removesuffix("_call"),
                "arguments": raw_args if isinstance(raw_args, dict) else {"input": raw_args},
            }
            calls[cid] = tu
            current["tool_uses"].append(tu)

    return turns


def _normalize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize standard OpenAI/tau-bench style message lists into ordered turn dicts."""
    pending_user: list[tuple[str, list[dict[str, Any]]]] = []
    results_by_call_id: dict[str, dict[str, Any]] = {}
    turns: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        stamp = _ensure_iso_tz(msg.get("timestamp") or msg.get("time"))
        if role in ("system", "user"):
            pending_user.append((stamp, [{"role": role, "content": msg.get("content", "")}]))
        elif role == "tool":
            cid = msg.get("tool_call_id") or msg.get("id")
            if isinstance(cid, str):
                results_by_call_id[cid] = {
                    "time": stamp,
                    "content": msg.get("content"),
                    "is_error": False,
                    "raw": msg,
                }
        elif role == "assistant":
            tool_uses: list[dict[str, Any]] = []
            for cidx, tc in enumerate(msg.get("tool_calls") or (), 1):
                fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else tc
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {"raw": raw_args}
                tool_uses.append(
                    {
                        "id": tc.get("id") or f"call_{idx}_{cidx}",
                        "name": fn.get("name", "tool"),
                        "arguments": raw_args if isinstance(raw_args, dict) else {},
                    }
                )
            turns.append(
                {
                    "time": stamp,
                    "user_instructions": list(pending_user),
                    "compacted": False,
                    "model": msg.get("model", "assistant"),
                    "message": msg,
                    "provider": msg.get("raw_data") or {"message_index": idx},
                    "tool_uses": tool_uses,
                    "pre_hooks": {},
                    "post_hooks": {},
                    "rejected_ids": set(),
                }
            )
            pending_user.clear()

    for t in turns:
        for tu in t["tool_uses"]:
            if tu["id"] in results_by_call_id:
                tu["result"] = results_by_call_id[tu["id"]]

    return turns


def _decode_json_value(val: Any) -> Any:
    """Decode a JSON-encoded scalar or container; leave other values unchanged."""
    if isinstance(val, str):
        text = val.strip()
        if text[:1] in ('"', "{", "[") or text in ("true", "false", "null") or text.lstrip("-").isdigit():
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return val
    return val


def _decode_possible_json(val: Any) -> Any:
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return {"text": val}
        return {"text": val}
    return {"value": val}


def _ingest_normalized_turns(
    session_dir: Path,
    turns: list[dict[str, Any]],
    *,
    task_id: str,
    id_fields: dict[str, str] | None = None,
    partial: bool = True,
) -> dict[str, Any]:
    capture_file = session_dir / "capture.jsonl"
    if capture_file.is_file():
        capture_file.unlink()

    cap = Capture(session_dir)
    extractor = ProcessObjectExtractor(custom_fields=id_fields)

    # Transcripts often stamp several records with the same (second-resolution) time.
    # Payload versions of one object must be strictly ordered, so keep time monotonic.
    last_time: list[datetime] = []

    def mono(ts: str) -> str:
        t = datetime.fromisoformat(_ensure_iso_tz(ts))
        if last_time and t <= last_time[0]:
            t = last_time[0] + timedelta(microseconds=1)
        last_time[:] = [t]
        return t.isoformat()

    active_instructions: list[str] = []
    prior_sources: list[str] = []
    latest_turn_results: list[str] = []
    previous_turn_id: str | None = None
    req_counter = 0

    for t_idx, turn in enumerate(turns, 1):
        tid = f"turn:{t_idx}"
        pid = f"prompt:{t_idx}"
        rid = f"response:{t_idx}"

        for inst_time, inst_msgs in turn["user_instructions"]:
            iid = _instruction_id(inst_msgs)
            if iid not in active_instructions:
                cap.instruction(iid, inst_msgs, timestamp=mono(inst_time))
                active_instructions.append(iid)

        stamp = mono(turn["time"])
        if not active_instructions:
            default_iid = "instruction:base"
            cap.instruction(
                default_iid,
                [{"role": "user", "content": task_id}],
                timestamp=stamp,
            )
            active_instructions.append(default_iid)

        history_sources = [s for s in prior_sources if s not in latest_turn_results]
        history_obj = (
            {"id": f"history:{t_idx}", "sources": list(history_sources)}
            if history_sources
            else None
        )
        layout = list(active_instructions) + ([history_obj["id"]] if history_obj else []) + list(latest_turn_results)

        turn_data: dict[str, Any] = {
            "id": tid,
            "prompt_id": pid,
            "index": t_idx,
            "task_id": task_id,
            "previous_turn": previous_turn_id,
            "parameters": {"model": turn.get("model", "assistant")},
            "layout": layout,
            "history": history_obj,
        }
        if partial:
            turn_data["capture_completeness"] = "partial"
            turn_data["membership_basis"] = "transcript_alternation"
            turn_data["capture_scope"] = "newly_visible_input"
            turn_data["admission_basis"] = "transcript_alternation"

        cap.raw("turn_start", turn_data, timestamp=stamp)
        if history_obj is not None:
            cap.event("History.Assembled", timestamp=stamp, subject=history_obj["id"], prompt=pid)
        cap.event("Turn.Started", timestamp=stamp, subject=tid, prompt=pid)
        cap.event("Prompt.Submitted", timestamp=stamp, subject=pid, turn=tid)

        cap.response(
            rid,
            tid,
            turn.get("provider", {}),
            turn["message"],
            timestamp=stamp,
        )
        prior_sources.append(rid)
        latest_turn_results = []

        for tu in turn["tool_uses"]:
            req_counter += 1
            aid = f"request:{req_counter}"
            eid = f"execution:{req_counter}"
            res_id = f"result:{req_counter}"
            tname = tu["name"]
            targs = tu["arguments"]
            tuid = tu["id"]

            req_obs = extractor.observe_request(tname, targs)
            for oid, state in req_obs.initial_objects.items():
                cap.business_change(oid, state, created=True, turn_id=tid, emit_event=False, timestamp=stamp)
                cap.event("ProcessObject.Registered", timestamp=stamp, subject=oid)

            is_rejected = tuid in turn.get("rejected_ids", set())
            cap.action_request(
                aid,
                rid,
                tid,
                tname,
                targs,
                req_obs.targets,
                dispatched=not is_rejected,
                rejected=is_rejected,
                timestamp=stamp,
            )
            if is_rejected:
                continue

            pre_time = mono(
                turn["pre_hooks"][tuid]["time"]
                if tuid in turn.get("pre_hooks", {})
                else stamp
            )
            cap.execution_start(eid, aid, tid, timestamp=pre_time)

            res_info = tu.get("result")
            res_time = mono(res_info["time"] if res_info else pre_time)
            res_data = _decode_possible_json(res_info["content"]) if res_info else {"ok": True}
            if res_info and res_info.get("is_error"):
                text = str(res_info.get("content", "")).lower()
                denied = any(
                    marker in text
                    for marker in (
                        "doesn't want to proceed",
                        "permission for this command was denied",
                        "has been denied",
                        "was denied by",
                        "user rejected",
                        "user denied",
                    )
                )
                outcome = "rejected" if denied else "failure"
            else:
                outcome = "success"

            exec_obs = extractor.observe_execution_and_result(
                tname,
                targs,
                res_data,
                outcome=outcome,
            )
            for oid, state in exec_obs.initial_objects.items():
                cap.business_change(oid, state, created=True, turn_id=tid, emit_event=False, timestamp=res_time)
                cap.event("ProcessObject.Registered", timestamp=res_time, subject=oid)
            for change in exec_obs.changes:
                cap.business_change(
                    change["id"],
                    change["state"],
                    created=change["created"],
                    execution_id=eid,
                    turn_id=tid,
                    timestamp=res_time,
                )

            cap.execution_finish(eid, aid, tid, outcome, exec_obs.acted_on, timestamp=res_time)
            feedback_msg = {
                "role": "user",
                "content": canonical({"tool_result": {"request_id": aid, "name": tname, "result": res_data}}),
            }
            cap.action_result(
                res_id,
                aid,
                eid,
                feedback_msg,
                res_data,
                exec_obs.describes,
                turn_id=tid,
                timestamp=res_time,
            )
            prior_sources.append(res_id)
            latest_turn_results.append(res_id)

        previous_turn_id = tid

    return {"turns_ingested": len(turns), "requests_ingested": req_counter}


def claude_code_settings_snippet(session_dir: Path | str, command: str = "loopocel-mcp") -> dict[str, Any]:
    """Return a Claude Code .claude/settings.json hooks snippet for live session capture."""
    target = str(Path(session_dir).resolve())
    hook_cmd = f'{command} hook --session-dir "{target}"'
    hook_entry = [{"matcher": "*", "hooks": [{"type": "command", "command": hook_cmd}]}]
    plain_entry = [{"hooks": [{"type": "command", "command": hook_cmd}]}]
    return {
        "hooks": {
            "SessionStart": plain_entry,
            "UserPromptSubmit": plain_entry,
            "PreToolUse": hook_entry,
            "PostToolUse": hook_entry,
            "PermissionRequest": hook_entry,
            "PreCompact": plain_entry,
            "Stop": plain_entry,
            "SessionEnd": plain_entry,
        }
    }


def codex_hooks_snippet(session_dir: Path | str, command: str = "loopocel-mcp") -> dict[str, Any]:
    """Return a Codex ~/.codex/hooks.json snippet for live session capture."""
    target = str(Path(session_dir).resolve())
    hook_cmd = f'{command} hook --session-dir "{target}"'
    tool_entry = [{"matcher": ".*", "hooks": [{"type": "command", "command": hook_cmd}]}]
    plain_entry = [{"hooks": [{"type": "command", "command": hook_cmd}]}]
    return {
        "hooks": {
            "SessionStart": plain_entry,
            "UserPromptSubmit": plain_entry,
            "PreToolUse": tool_entry,
            "PostToolUse": tool_entry,
            "Stop": plain_entry,
        }
    }


def antigravity_hooks_snippet(session_dir: Path | str, command: str = "loopocel-mcp") -> dict[str, Any]:
    """Return an Antigravity ~/.gemini/config/hooks.json snippet for live session capture.

    Antigravity payloads carry no event name, so each command passes it with --event.
    """
    target = str(Path(session_dir).resolve())
    hook_cmd = f'{command} hook --session-dir "{target}"'
    return {
        "loopocel": {
            "PostToolUse": [
                {"matcher": ".*", "hooks": [{"type": "command", "command": f"{hook_cmd} --event PostToolUse"}]}
            ],
            "Stop": [{"type": "command", "command": f"{hook_cmd} --event Stop"}],
        }
    }


def hooks_snippet(agent: str, session_dir: Path | str, command: str = "loopocel-mcp") -> dict[str, Any]:
    """Hook configuration for one of HOOK_AGENTS."""
    if agent == "codex":
        return codex_hooks_snippet(session_dir, command)
    if agent == "antigravity":
        return antigravity_hooks_snippet(session_dir, command)
    return claude_code_settings_snippet(session_dir, command)


def main_hook_cli(session_dir: Path, event: str | None = None) -> int:
    """Entry point invoked by Claude Code, Codex or Antigravity hooks (reads JSON from stdin)."""
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict) and payload:
        res = record_hook_event(session_dir, payload, event=event)
        if res["agent"] == "antigravity":
            # Antigravity requires a JSON object on stdout; any Stop decision but "continue" lets the agent stop.
            print(json.dumps({"decision": "stop"} if res["hook_event_name"] == "Stop" else {}))
    return 0

