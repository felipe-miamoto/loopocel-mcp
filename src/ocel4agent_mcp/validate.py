"""LoopOCEL ontology, official OCEL 2.0 schema, and PM4Py validation."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from .convert import OBJECT_TYPES, RELATIONS


CLASSES = set(OBJECT_TYPES)
SCHEMA_FILE = Path(__file__).resolve().parent / "schema" / "ocel20-schema-json.json"


class VerificationError(ValueError):
    """Captured evidence violates the LoopOCEL ontology or schema contract."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"Timestamp must be a string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"Invalid ISO date-time: {value!r}") from exc
    require(parsed.tzinfo is not None, f"Timestamp lacks timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _builtin_validate_ocel20_schema(ocel: Any) -> list[dict[str, Any]]:
    """Zero-dependency validator for the official OCEL 2.0 JSON draft-07 schema."""
    errors: list[dict[str, Any]] = []

    def err(path: list[Any], message: str) -> None:
        errors.append({"path": path, "message": message})

    if not isinstance(ocel, dict):
        err([], "Root document must be an object")
        return errors

    for req in ("eventTypes", "objectTypes", "events", "objects"):
        if req not in ocel:
            err([], f"{req!r} is a required property")
        elif not isinstance(ocel[req], list):
            err([req], f"{ocel[req]!r} is not of type 'array'")

    for section in ("eventTypes", "objectTypes"):
        items = ocel.get(section)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                err([section, idx], "Item must be an object")
                continue
            for req in ("name", "attributes"):
                if req not in item:
                    err([section, idx], f"{req!r} is a required property")
            if "name" in item and not isinstance(item["name"], str):
                err([section, idx, "name"], "Must be a string")
            attrs = item.get("attributes")
            if attrs is not None:
                if not isinstance(attrs, list):
                    err([section, idx, "attributes"], "Must be an array")
                else:
                    for aidx, attr in enumerate(attrs):
                        if not isinstance(attr, dict):
                            err([section, idx, "attributes", aidx], "Must be an object")
                            continue
                        for req in ("name", "type"):
                            if req not in attr:
                                err([section, idx, "attributes", aidx], f"{req!r} is a required property")
                            elif not isinstance(attr[req], str):
                                err([section, idx, "attributes", aidx, req], "Must be a string")

    events = ocel.get("events")
    if isinstance(events, list):
        for idx, evt in enumerate(events):
            if not isinstance(evt, dict):
                err(["events", idx], "Must be an object")
                continue
            for req in ("id", "type", "time"):
                if req not in evt:
                    err(["events", idx], f"{req!r} is a required property")
                elif not isinstance(evt[req], str):
                    err(["events", idx, req], "Must be a string")
            if isinstance(evt.get("time"), str):
                try:
                    utc(evt["time"])
                except VerificationError as exc:
                    err(["events", idx, "time"], str(exc))
            if "attributes" in evt:
                if not isinstance(evt["attributes"], list):
                    err(["events", idx, "attributes"], "Must be an array")
                else:
                    for aidx, attr in enumerate(evt["attributes"]):
                        if not isinstance(attr, dict):
                            err(["events", idx, "attributes", aidx], "Must be an object")
                            continue
                        for req in ("name", "value"):
                            if req not in attr:
                                err(["events", idx, "attributes", aidx], f"{req!r} is a required property")
                            elif not isinstance(attr[req], str):
                                err(["events", idx, "attributes", aidx, req], "Must be a string")
            if "relationships" in evt:
                if not isinstance(evt["relationships"], list):
                    err(["events", idx, "relationships"], "Must be an array")
                else:
                    for ridx, rel in enumerate(evt["relationships"]):
                        if not isinstance(rel, dict):
                            err(["events", idx, "relationships", ridx], "Must be an object")
                            continue
                        for req in ("objectId", "qualifier"):
                            if req not in rel:
                                err(["events", idx, "relationships", ridx], f"{req!r} is a required property")
                            elif not isinstance(rel[req], str):
                                err(["events", idx, "relationships", ridx, req], "Must be a string")

    objects = ocel.get("objects")
    if isinstance(objects, list):
        for idx, obj in enumerate(objects):
            if not isinstance(obj, dict):
                err(["objects", idx], "Must be an object")
                continue
            for req in ("id", "type"):
                if req not in obj:
                    err(["objects", idx], f"{req!r} is a required property")
                elif not isinstance(obj[req], str):
                    err(["objects", idx, req], "Must be a string")
            if "relationships" in obj:
                if not isinstance(obj["relationships"], list):
                    err(["objects", idx, "relationships"], "Must be an array")
                else:
                    for ridx, rel in enumerate(obj["relationships"]):
                        if not isinstance(rel, dict):
                            err(["objects", idx, "relationships", ridx], "Must be an object")
                            continue
                        for req in ("objectId", "qualifier"):
                            if req not in rel:
                                err(["objects", idx, "relationships", ridx], f"{req!r} is a required property")
                            elif not isinstance(rel[req], str):
                                err(["objects", idx, "relationships", ridx, req], "Must be a string")
            if "attributes" in obj:
                if not isinstance(obj["attributes"], list):
                    err(["objects", idx, "attributes"], "Must be an array")
                else:
                    for aidx, attr in enumerate(obj["attributes"]):
                        if not isinstance(attr, dict):
                            err(["objects", idx, "attributes", aidx], "Must be an object")
                            continue
                        for req in ("name", "value", "time"):
                            if req not in attr:
                                err(["objects", idx, "attributes", aidx], f"{req!r} is a required property")
                            elif not isinstance(attr[req], str):
                                err(["objects", idx, "attributes", aidx, req], "Must be a string")
                        if isinstance(attr.get("time"), str):
                            try:
                                utc(attr["time"])
                            except VerificationError as exc:
                                err(["objects", idx, "attributes", aidx, "time"], str(exc))

    return errors


def validate_schema(ocel: dict[str, Any], schema_path: Path | None = None) -> dict[str, Any]:
    """Validate an OCEL 2.0 dictionary against the official JSON schema."""
    source = schema_path if schema_path and schema_path.is_file() else SCHEMA_FILE
    schema_bytes = source.read_bytes()
    schema_sha = hashlib.sha256(schema_bytes).hexdigest()

    if importlib.util.find_spec("jsonschema") is not None:
        from jsonschema import Draft7Validator, FormatChecker

        validator = Draft7Validator(
            json.loads(schema_bytes),
            format_checker=FormatChecker(),
        )
        raw_errors = sorted(validator.iter_errors(ocel), key=lambda e: str(list(e.path)))
        errors = [{"path": list(err.path), "message": err.message} for err in raw_errors]
    else:
        errors = _builtin_validate_ocel20_schema(ocel)

    return {
        "passed": not errors,
        "schema": str(source),
        "schema_sha256": schema_sha,
        "errors": errors,
    }


def reconstruct(ocel: dict[str, Any]) -> dict[str, Any]:
    """Recover turns, requests, executions, results, and states from OCEL alone."""
    objects = {obj["id"]: obj for obj in ocel["objects"]}
    require(len(objects) == len(ocel["objects"]), "Duplicate object identity")
    require(all(obj["type"] in CLASSES for obj in objects.values()), "Unknown object class")

    payloads: dict[str, dict[str, Any]] = {}
    outgoing: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    incoming: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for oid, obj in objects.items():
        attrs = [a for a in obj.get("attributes", []) if a["name"] == "payload"]
        require(bool(attrs), f"Payload required: {oid}")
        if obj["type"] not in ("Execution", "Instruction"):
            require(len(attrs) == 1, f"Exactly one immutable payload required: {oid}")
        attrs.sort(key=lambda a: utc(a["time"]))
        require(
            len({utc(a["time"]) for a in attrs}) == len(attrs),
            f"Ambiguous simultaneous payload versions: {oid}",
        )
        require(
            all(isinstance(a["value"], str) for a in attrs),
            f"Payload must be a JSON string: {oid}",
        )
        payloads[oid] = json.loads(attrs[-1]["value"])
        require(isinstance(payloads[oid], dict), f"Payload must decode to an object: {oid}")
        if obj["type"] == "Instruction" and len(attrs) > 1:
            final = payloads[oid]
            for attr in attrs[:-1]:
                earlier = json.loads(attr["value"])
                require(
                    earlier.get("messages") == final.get("messages"),
                    f"Instruction messages changed under one identity: {oid}",
                )
                require(
                    all(
                        final.get("request_fields", {}).get(k) == v
                        for k, v in earlier.get("request_fields", {}).items()
                    ),
                    f"Instruction request fields overwritten: {oid}",
                )
        seen = set()
        for rel in obj.get("relationships", []):
            target, qualifier = rel["objectId"], rel["qualifier"]
            require(target in objects, f"Dangling {qualifier}: {oid} -> {target}")
            require(qualifier in RELATIONS, f"Unknown object property: {qualifier}")
            require(
                (obj["type"], objects[target]["type"]) == RELATIONS[qualifier],
                f"Wrong domain/range for {qualifier}: {oid} -> {target}",
            )
            require(
                (target, qualifier) not in seen,
                f"Duplicate relationship: {oid} {qualifier} {target}",
            )
            seen.add((target, qualifier))
            outgoing[oid][qualifier].append(target)
            incoming[target][qualifier].append(oid)

    def exactly_one(values: list[str], description: str) -> str:
        require(len(values) == 1, f"Exactly one {description} required; got {values}")
        return values[0]

    def members(oid: str, qualifier: str) -> list[str]:
        return outgoing[oid].get(qualifier, [])

    for oid, obj in objects.items():
        if obj["type"] == "Prompt":
            exactly_one(incoming[oid]["hasPrompt"], f"Turn owner for Prompt {oid}")
        elif obj["type"] == "Response":
            exactly_one(incoming[oid]["hasResponse"], f"Turn owner for Response {oid}")
        elif obj["type"] == "ActionRequest":
            exactly_one(incoming[oid]["hasActionRequest"], f"Response owner for request {oid}")
        elif obj["type"] == "Execution":
            exactly_one(incoming[oid]["hasExecution"], f"request owner for Execution {oid}")
        elif obj["type"] == "ActionResult":
            exactly_one(members(oid, "resultOf"), f"request correspondence for ActionResult {oid}")
            require(
                len(incoming[oid]["producesResult"]) <= 1,
                f"Multiple execution sources for result {oid}",
            )
            for execution in incoming[oid]["producesResult"]:
                require(
                    incoming[execution]["hasExecution"] == members(oid, "resultOf"),
                    f"Result/Execution request mismatch: {oid}",
                )

    def expand_prompt(pid: str, active: tuple[str, ...] = ()) -> dict[str, Any]:
        require(pid not in active, f"Cyclic content provenance at {pid}")
        p = payloads[pid]
        if p.get("capture_completeness") == "partial" and "layout" not in p:
            return deepcopy(p)
        require(isinstance(p.get("layout"), list), f"Missing ordered Prompt layout: {pid}")
        require(
            isinstance(p.get("parameters"), dict) and "messages" not in p["parameters"],
            f"Prompt parameters must exclude messages: {pid}",
        )
        expected = {
            "Instruction": "hasInstruction",
            "History": "hasHistory",
            "ActionResult": "hasActionResult",
        }
        declared: dict[str, set[str]] = {k: set() for k in expected}
        declared_fields: dict[str, Any] = {}
        messages: list[Any] = []
        for cid in p["layout"]:
            require(
                cid in objects and objects[cid]["type"] in expected,
                f"Invalid Prompt constituent {cid}",
            )
            kind = objects[cid]["type"]
            declared[kind].add(cid)
            if kind == "Instruction":
                request_fields = payloads[cid].get("request_fields", {})
                require(
                    isinstance(request_fields, dict),
                    f"Invalid Instruction request fields: {cid}",
                )
                for name, value in request_fields.items():
                    require(
                        name in p["parameters"] and p["parameters"][name] == value,
                        f"Instruction/Prompt request field mismatch: {pid}.{name}",
                    )
                    require(
                        name not in declared_fields or declared_fields[name] == value,
                        f"Conflicting Instruction request fields: {pid}.{name}",
                    )
                    declared_fields[name] = value
            messages.extend(expand_content(cid, active + (pid,)))
        for kind, qualifier in expected.items():
            require(
                declared[kind] == set(members(pid, qualifier)),
                f"Prompt layout/{qualifier} membership mismatch: {pid}",
            )
        if "response_format" in p["parameters"]:
            require(
                "response_format" in declared_fields,
                f"Unattributed residual response-format instruction: {pid}",
            )
        return {**deepcopy(p["parameters"]), "messages": messages}

    def expand_content(oid: str, active: tuple[str, ...] = ()) -> list[Any]:
        require(oid not in active, f"Cyclic content provenance at {oid}")
        kind, p = objects[oid]["type"], payloads[oid]
        if kind == "Instruction":
            require(isinstance(p.get("messages"), list), f"Invalid Instruction messages: {oid}")
            return deepcopy(p["messages"])
        if kind in ("Response", "ActionResult"):
            require(isinstance(p.get("message"), dict), f"Missing admitted message: {oid}")
            return [deepcopy(p["message"])]
        if kind == "History":
            require(isinstance(p.get("sources"), list), f"Missing ordered History sources: {oid}")
            require(bool(p["sources"]), f"History has no evidenced source: {oid}")
            source_qualifiers = {
                "Prompt": "hasSourcePrompt",
                "Response": "hasSourceResponse",
                "ActionResult": "hasSourceActionResult",
            }
            selected: dict[str, set[str]] = {k: set() for k in source_qualifiers}
            messages: list[Any] = []
            for sid in p["sources"]:
                require(
                    sid in objects and objects[sid]["type"] in source_qualifiers,
                    f"Invalid historical source: {sid}",
                )
                source_type = objects[sid]["type"]
                selected[source_type].add(sid)
                if source_type == "Prompt":
                    messages.extend(expand_prompt(sid, active + (oid,)).get("messages", []))
                else:
                    messages.extend(expand_content(sid, active + (oid,)))
            for skind, qualifier in source_qualifiers.items():
                require(
                    selected[skind] == set(members(oid, qualifier)),
                    f"History source/{qualifier} membership mismatch: {oid}",
                )
            return messages
        raise VerificationError(f"Object cannot contribute Prompt messages: {oid}")

    turn_ids = {oid for oid, obj in objects.items() if obj["type"] == "Turn"}
    indegree = {oid: len(incoming[oid]["hasNextTurn"]) for oid in turn_ids}
    ready = [oid for oid in turn_ids if indegree[oid] == 0]
    visited = 0
    while ready:
        cur = ready.pop()
        visited += 1
        for nxt in members(cur, "hasNextTurn"):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    require(visited == len(turn_ids), "Cycle in hasNextTurn")

    path: list[str] = []
    if turn_ids:
        roots = sorted(
            [oid for oid in turn_ids if not incoming[oid]["hasNextTurn"]],
            key=lambda t: (str(payloads[t].get("task_id", "")), payloads[t].get("index", 0), t),
        )
        seen_turns: set[str] = set()
        for root in roots:
            current: str | None = root
            while current is not None:
                require(current not in seen_turns, "Cycle or convergent branch in hasNextTurn")
                seen_turns.add(current)
                path.append(current)
                successors = members(current, "hasNextTurn")
                require(len(successors) <= 1, "At most one direct continuation per Turn")
                require(len(incoming[current]["hasNextTurn"]) <= 1, "At most one predecessor per Turn")
                current = successors[0] if successors else None
        require(seen_turns == turn_ids, "hasNextTurn does not reach every Turn")

    turns: list[dict[str, Any]] = []
    request_turn: dict[str, str] = {}
    for tid in path:
        pid = exactly_one(members(tid, "hasPrompt"), f"Prompt for Turn {tid}")
        response_ids = members(tid, "hasResponse")
        require(len(response_ids) <= 1, f"Multiple Responses for Turn {tid}")
        rid = response_ids[0] if response_ids else None
        aids = members(rid, "hasActionRequest") if rid else []
        for aid in aids:
            request_turn[aid] = tid
        preds = incoming[tid]["hasNextTurn"]
        turns.append(
            {
                "id": tid,
                "prompt_id": pid,
                "response_id": rid,
                "request": expand_prompt(pid),
                "provider_response": deepcopy(payloads[rid].get("provider")) if rid else None,
                "message": deepcopy(payloads[rid].get("message")) if rid else None,
                "previous_turn": preds[0] if preds else None,
                "action_request_ids": list(aids),
            }
        )

    requests: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    final_objects: dict[str, Any] = {}

    for oid, obj in objects.items():
        p = payloads[oid]
        if obj["type"] == "ActionRequest":
            require(oid in request_turn, f"Unreachable ActionRequest {oid}")
            name = p.get("name") or (p.get("call", {}).get("name") if isinstance(p.get("call"), dict) else None)
            args = p.get("arguments") if isinstance(p.get("arguments"), dict) else (
                p.get("call", {}).get("arguments") if isinstance(p.get("call"), dict) else None
            )
            require(isinstance(name, str) and isinstance(args, dict), f"Invalid ActionRequest payload: {oid}")
            requests.append(
                {
                    "id": oid,
                    "turn_id": request_turn[oid],
                    "name": name,
                    "arguments": deepcopy(args),
                }
            )
        elif obj["type"] == "Execution":
            aid = exactly_one(incoming[oid]["hasExecution"], f"request for Execution {oid}")
            output_ids = members(oid, "producesResult")
            require(len(output_ids) <= 1, "At most one result per Execution")
            for result_id in output_ids:
                require(
                    members(result_id, "resultOf") == [aid],
                    f"Execution/result request mismatch: {oid}",
                )
            executions.append(
                {
                    "id": oid,
                    "request_id": aid,
                    "result_id": output_ids[0] if output_ids else None,
                    "outcome": p.get("outcome"),
                    "acted_on": sorted(members(oid, "actOn")),
                }
            )
        elif obj["type"] == "ActionResult":
            execution_ids = incoming[oid]["producesResult"]
            results.append(
                {
                    "id": oid,
                    "request_id": exactly_one(members(oid, "resultOf"), f"request for {oid}"),
                    "execution_id": execution_ids[0] if execution_ids else None,
                    "message": deepcopy(p.get("message")),
                    "data": deepcopy(p.get("data")),
                }
            )
        elif obj["type"] == "ProcessObject":
            state_attrs = [a for a in obj.get("attributes", []) if a["name"] == "state"]
            state_attrs.sort(key=lambda a: utc(a["time"]))
            require(
                len({utc(a["time"]) for a in state_attrs}) == len(state_attrs),
                f"Ambiguous simultaneous state versions: {oid}",
            )
            final_objects[oid] = (
                json.loads(state_attrs[-1]["value"]) if state_attrs else deepcopy(p)
            )

    events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for event in ocel["events"]:
        require(event["id"] not in event_ids, "Duplicate event identity")
        event_ids.add(event["id"])
        utc(event["time"])
        seen_e2o = set()
        for rel in event.get("relationships", []):
            require(rel["objectId"] in objects, f"Dangling event participant: {event['id']}")
            pair = (rel["objectId"], rel["qualifier"])
            require(pair not in seen_e2o, f"Duplicate E2O link in {event['id']}")
            seen_e2o.add(pair)
        events.append({k: deepcopy(event.get(k, [])) for k in ("id", "type", "time", "relationships")})

    return {
        "turns": turns,
        "requests": requests,
        "executions": executions,
        "results": results,
        "final_objects": final_objects,
        "events": events,
    }


def pm4py_check(path: Path, source: dict[str, Any]) -> dict[str, Any]:
    """Validate native PM4Py OCEL 2.0 JSON ingestion and OCDFG discovery when installed."""
    if importlib.util.find_spec("pm4py") is None:
        return {"available": False, "skipped": True, "reason": "pm4py not installed"}

    import pm4py

    imported = pm4py.read_ocel2_json(str(path))
    model = pm4py.discover_ocdfg(imported)
    source_ids = {obj["id"] for obj in source["objects"]}
    imported_ids = set(imported.objects["ocel:oid"])
    source_events = {e["id"] for e in source["events"]}
    imported_events = set(imported.events["ocel:eid"])
    source_o2o = Counter(
        (o["id"], r["objectId"], r["qualifier"])
        for o in source["objects"]
        for r in o.get("relationships", [])
    )
    imported_o2o = Counter(
        (row["ocel:oid"], row["ocel:oid_2"], row.get("ocel:qualifier"))
        for row in imported.o2o.to_dict("records")
    )
    source_e2o = Counter(
        (e["id"], r["objectId"], r["qualifier"])
        for e in source["events"]
        for r in e.get("relationships", [])
    )
    imported_e2o = Counter(
        (row["ocel:eid"], row["ocel:oid"], row.get("ocel:qualifier"))
        for row in imported.relations.to_dict("records")
    )
    losses = {
        "missing_objects": sorted(source_ids - imported_ids),
        "extra_objects": sorted(imported_ids - source_ids),
        "missing_events": sorted(source_events - imported_events),
        "extra_events": sorted(imported_events - source_events),
        "missing_o2o": [list(edge) + [count] for edge, count in (source_o2o - imported_o2o).items()],
        "extra_o2o": [list(edge) + [count] for edge, count in (imported_o2o - source_o2o).items()],
        "missing_e2o": [list(edge) + [count] for edge, count in (source_e2o - imported_e2o).items()],
        "extra_e2o": [list(edge) + [count] for edge, count in (imported_e2o - source_e2o).items()],
    }
    return {
        "available": True,
        "ingestion_passed": True,
        "ocdfg_discovery_passed": True,
        "version": pm4py.__version__,
        "objects": len(imported.objects),
        "events": len(imported.events),
        "event_object_relations": len(imported.relations),
        "object_object_relations": len(imported.o2o),
        "qualified_graph_preserved": not any(losses.values()),
        "losses": losses,
        "activities": sorted(model.get("activities", [])),
        "object_types": sorted(model.get("object_types", [])),
    }


def validate_log(
    ocel: dict[str, Any],
    *,
    ocel_path: Path | None = None,
    check_pm4py: bool = False,
) -> dict[str, Any]:
    """Run schema, ontology, and optional PM4Py validation on a LoopOCEL log."""
    schema_report = validate_schema(ocel)
    try:
        recovered = reconstruct(ocel)
        ontology_report: dict[str, Any] = {
            "passed": True,
            "counts": {key: len(val) for key, val in recovered.items()},
        }
    except (VerificationError, KeyError, TypeError, ValueError) as exc:
        ontology_report = {
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    report: dict[str, Any] = {
        "passed": bool(schema_report["passed"] and ontology_report["passed"]),
        "schema": schema_report,
        "ontology": ontology_report,
    }
    if check_pm4py and ocel_path is not None and ocel_path.is_file():
        try:
            report["pm4py"] = pm4py_check(ocel_path, ocel)
        except Exception as exc:
            report["pm4py"] = {
                "available": True,
                "ingestion_passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            report["passed"] = False
    return report

