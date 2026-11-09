"""Map captured agent-loop evidence to the LoopOCEL OCEL 2.0 profile.

Events are copied from explicit harness or adapter occurrences. Object
relationships are asserted from captured identities, never from timestamps or
shared contents.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
from typing import Any


OBJECT_TYPES = (
    "Turn", "ProcessObject", "Prompt", "Response", "Instruction", "History",
    "ActionRequest", "Execution", "ActionResult",
)

RELATIONS = {
    "hasPrompt": ("Turn", "Prompt"),
    "hasResponse": ("Turn", "Response"),
    "hasNextTurn": ("Turn", "Turn"),
    "hasInstruction": ("Prompt", "Instruction"),
    "hasHistory": ("Prompt", "History"),
    "hasActionResult": ("Prompt", "ActionResult"),
    "hasActionRequest": ("Response", "ActionRequest"),
    "hasExecution": ("ActionRequest", "Execution"),
    "targetsObject": ("ActionRequest", "ProcessObject"),
    "actOn": ("Execution", "ProcessObject"),
    "producesResult": ("Execution", "ActionResult"),
    "resultOf": ("ActionResult", "ActionRequest"),
    "describesObject": ("ActionResult", "ProcessObject"),
    "hasSourcePrompt": ("History", "Prompt"),
    "hasSourceResponse": ("History", "Response"),
    "hasSourceActionResult": ("History", "ActionResult"),
}

BASELINE_EVENTS_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "Turn": ("Turn.Started", "Turn.Finished", "Turn.Failed"),
    "Prompt": ("Prompt.Submitted",),
    "Response": ("Response.Received",),
    "ActionRequest": ("ActionRequest.Dispatched", "ActionRequest.Rejected"),
    "Execution": ("Execution.Started", "Execution.Finished"),
    "ActionResult": ("ActionResult.Received",),
    "Instruction": ("Instruction.Registered",),
    "History": ("History.Assembled",),
    "ProcessObject": ("ProcessObject.Created", "ProcessObject.StateChanged"),
}

BASELINE_EVENT_SET = {
    etype for family_events in BASELINE_EVENTS_BY_FAMILY.values() for etype in family_events
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected a nonempty identifier, got {value!r}")
    return value


def _time(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Capture time must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Capture time needs an explicit timezone: {value}")
    return value


def convert(directory: Path | str, *, write_output: bool = True) -> dict[str, Any]:
    """Read capture.jsonl, validate the mapping, and write loopocel.json."""
    directory = Path(directory)
    objects: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    edges: list[tuple[str, str, str]] = []
    deferred_layouts: list[tuple[str, list[str]]] = []
    deferred_histories: list[tuple[str, list[str]]] = []
    completed_executions: set[str] = set()

    def add_object(oid: str, typ: str, payload: object, time: str, *, reuse: bool = False) -> dict[str, Any]:
        oid = _identifier(oid)
        encoded = _json(payload)
        if oid in objects:
            existing = objects[oid]
            if (
                reuse
                and existing["type"] == typ
                and existing["attributes"][0]["value"] == encoded
            ):
                return existing
            raise ValueError(f"Duplicate or conflicting object identity: {oid}")
        obj: dict[str, Any] = {
            "id": oid,
            "type": typ,
            "attributes": [{"name": "payload", "time": time, "value": encoded}],
            "relationships": [],
        }
        objects[oid] = obj
        return obj

    def edge(source: str, qualifier: str, target: str) -> None:
        edges.append((_identifier(source), qualifier, _identifier(target)))

    with (directory / "capture.jsonl").open(encoding="utf-8") as stream:
        for lineno, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                kind, time, data = record["kind"], _time(record["time"]), record["data"]
                if kind == "business_initial":
                    for oid, state in data["objects"].items():
                        obj = add_object(oid, "ProcessObject", state, time)
                        obj["attributes"].append(
                            {"name": "state", "time": time, "value": _json(state)}
                        )
                elif kind == "instruction":
                    payload: dict[str, Any] = {"messages": data["messages"]}
                    if "request_fields" in data:
                        if not isinstance(data["request_fields"], dict):
                            raise ValueError("Instruction request_fields must be an object")
                        payload["request_fields"] = data["request_fields"]
                    add_object(data["id"], "Instruction", payload, time, reuse=True)
                elif kind == "turn_start":
                    turn, prompt = data["id"], data["prompt_id"]
                    turn_payload: dict[str, Any] = {
                        "index": data["index"],
                        "task_id": data["task_id"],
                    }
                    if "admission_basis" in data:
                        turn_payload["admission_basis"] = data["admission_basis"]
                    prompt_payload: dict[str, Any] = {
                        "parameters": data["parameters"],
                        "layout": data["layout"],
                    }
                    for extra_key in ("capture_completeness", "membership_basis", "capture_scope"):
                        if extra_key in data:
                            prompt_payload[extra_key] = data[extra_key]
                    add_object(turn, "Turn", turn_payload, time)
                    add_object(prompt, "Prompt", prompt_payload, time)
                    edge(turn, "hasPrompt", prompt)
                    if data.get("previous_turn") is not None:
                        edge(data["previous_turn"], "hasNextTurn", turn)
                    history = data.get("history")
                    if history is not None:
                        add_object(
                            history["id"],
                            "History",
                            {"sources": history["sources"]},
                            time,
                            reuse=True,
                        )
                        deferred_histories.append((history["id"], history["sources"]))
                    deferred_layouts.append((prompt, data["layout"]))
                elif kind == "response":
                    add_object(
                        data["id"],
                        "Response",
                        {"provider": data["provider"], "message": data["message"]},
                        time,
                    )
                    edge(data["turn_id"], "hasResponse", data["id"])
                elif kind == "action_request":
                    add_object(
                        data["id"],
                        "ActionRequest",
                        {"name": data["name"], "arguments": data["arguments"]},
                        time,
                    )
                    edge(data["response_id"], "hasActionRequest", data["id"])
                    for target in data["targets"]:
                        edge(data["id"], "targetsObject", target)
                elif kind == "execution_start":
                    add_object(data["id"], "Execution", {"outcome": "pending"}, time)
                    edge(data["request_id"], "hasExecution", data["id"])
                elif kind == "execution_finish":
                    oid = data["id"]
                    if oid in completed_executions:
                        raise ValueError(f"Execution finished twice: {oid}")
                    obj = objects[oid]
                    if obj["type"] != "Execution":
                        raise ValueError(f"Not an Execution: {oid}")
                    if (data["request_id"], "hasExecution", oid) not in edges:
                        raise ValueError(f"Execution request identity changed: {oid}")
                    obj["attributes"].append(
                        {
                            "name": "payload",
                            "time": time,
                            "value": _json({"outcome": data["outcome"]}),
                        }
                    )
                    completed_executions.add(oid)
                    for target in data["acted_on"]:
                        edge(oid, "actOn", target)
                elif kind == "action_result":
                    add_object(
                        data["id"],
                        "ActionResult",
                        {"message": data["message"], "data": data["data"]},
                        time,
                    )
                    edge(data["id"], "resultOf", data["request_id"])
                    if data.get("execution_id") is not None:
                        edge(data["execution_id"], "producesResult", data["id"])
                    for target in data["describes"]:
                        edge(data["id"], "describesObject", target)
                elif kind == "business_change":
                    oid, state = data["id"], data["state"]
                    if data["created"]:
                        obj = add_object(oid, "ProcessObject", state, time, reuse=True)
                    else:
                        if oid not in objects:
                            obj = add_object(oid, "ProcessObject", state, time)
                        else:
                            obj = objects[oid]
                            if obj["type"] != "ProcessObject":
                                raise ValueError(
                                    f"Business change targets non-business object: {oid}"
                                )
                    obj["attributes"].append(
                        {"name": "state", "time": time, "value": _json(state)}
                    )
                elif kind == "event":
                    eid = _identifier(data["id"])
                    if eid in event_ids:
                        raise ValueError(f"Duplicate event identity: {eid}")
                    event_ids.add(eid)
                    events.append(
                        {
                            "id": eid,
                            "type": _identifier(data["type"]),
                            "time": time,
                            "attributes": list(data.get("attributes", [])),
                            "relationships": data["relationships"],
                        }
                    )
                else:
                    raise ValueError(f"Unknown capture kind: {kind}")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"capture.jsonl:{lineno}: {exc}") from exc

    for prompt, layout in deferred_layouts:
        for oid in layout:
            typ = objects[oid]["type"]
            qualifier = {
                "Instruction": "hasInstruction",
                "History": "hasHistory",
                "ActionResult": "hasActionResult",
            }.get(typ)
            if qualifier is None:
                raise ValueError(f"Invalid direct Prompt member: {oid} ({typ})")
            edge(prompt, qualifier, oid)
    for history, sources in deferred_histories:
        if not sources:
            raise ValueError(f"History has no evidenced source: {history}")
        for oid in sources:
            typ = objects[oid]["type"]
            qualifier = {
                "Prompt": "hasSourcePrompt",
                "Response": "hasSourceResponse",
                "ActionResult": "hasSourceActionResult",
            }.get(typ)
            if qualifier is None:
                raise ValueError(f"Invalid History source: {oid} ({typ})")
            edge(history, qualifier, oid)

    instruction_payloads = {
        oid: json.loads(obj["attributes"][0]["value"])
        for oid, obj in objects.items()
        if obj["type"] == "Instruction"
    }
    instruction_uses: dict[str, list[str]] = defaultdict(list)
    required_fields = {
        oid: dict(payload.get("request_fields", {}))
        for oid, payload in instruction_payloads.items()
    }
    prompt_payloads = {
        oid: json.loads(obj["attributes"][0]["value"])
        for oid, obj in objects.items()
        if obj["type"] == "Prompt"
    }
    for prompt, layout in deferred_layouts:
        instructions = list(
            dict.fromkeys(
                oid for oid in layout if objects[oid]["type"] == "Instruction"
            )
        )
        for oid in instructions:
            instruction_uses[oid].append(prompt)
        parameters = prompt_payloads[prompt]["parameters"]
        if "response_format" not in parameters:
            continue
        owners = [
            oid for oid in instructions if "response_format" in required_fields[oid]
        ]
        if not owners:
            if len(instructions) != 1:
                raise ValueError(
                    f"Ambiguous Instruction provenance for {prompt}.response_format"
                )
            owners = instructions
        for oid in owners:
            fields = required_fields[oid]
            if (
                "response_format" in fields
                and fields["response_format"] != parameters["response_format"]
            ):
                raise ValueError(
                    f"Instruction content changed across reuse: {oid}.response_format"
                )
            fields["response_format"] = parameters["response_format"]
    for oid, fields in required_fields.items():
        uses = instruction_uses[oid]
        for prompt in uses:
            parameters = prompt_payloads[prompt]["parameters"]
            for name, value in fields.items():
                if name not in parameters or parameters[name] != value:
                    raise ValueError(
                        f"Instruction field mismatch: {oid}.{name} in {prompt}"
                    )
        payload = instruction_payloads[oid]
        if fields and fields != payload.get("request_fields", {}):
            payload["request_fields"] = fields
            first_use = min(
                (objects[p]["attributes"][0]["time"] for p in uses),
                key=lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
            )
            objects[oid]["attributes"].append(
                {"name": "payload", "time": first_use, "value": _json(payload)}
            )

    outgoing: dict[tuple[str, str], list[str]] = defaultdict(list)
    incoming: dict[tuple[str, str], list[str]] = defaultdict(list)
    for source, qualifier, target in dict.fromkeys(edges):
        if source not in objects or target not in objects:
            raise ValueError(
                f"Unresolved O2O reference: {(source, qualifier, target)}"
            )
        actual = (objects[source]["type"], objects[target]["type"])
        if RELATIONS.get(qualifier) != actual:
            raise ValueError(
                f"Invalid O2O domain/range: {(source, qualifier, target)}"
            )
        objects[source]["relationships"].append(
            {"objectId": target, "qualifier": qualifier}
        )
        outgoing[source, qualifier].append(target)
        incoming[target, qualifier].append(source)

    def count(
        mapping: dict[tuple[str, str], list[str]],
        oid: str,
        prop: str,
        low: int,
        high: int,
    ) -> None:
        n = len(mapping[oid, prop])
        if not low <= n <= high:
            raise ValueError(f"Invalid {prop} cardinality for {oid}: {n}")

    for oid, obj in objects.items():
        typ = obj["type"]
        if typ == "Turn":
            count(outgoing, oid, "hasPrompt", 1, 1)
            count(outgoing, oid, "hasResponse", 0, 1)
        elif typ in ("Prompt", "Response"):
            count(incoming, oid, "has" + typ, 1, 1)
        elif typ == "ActionRequest":
            count(incoming, oid, "hasActionRequest", 1, 1)
        elif typ == "Execution":
            count(incoming, oid, "hasExecution", 1, 1)
        elif typ == "ActionResult":
            count(outgoing, oid, "resultOf", 1, 1)
            count(incoming, oid, "producesResult", 0, 1)
            for execution in incoming[oid, "producesResult"]:
                if incoming[execution, "hasExecution"] != outgoing[oid, "resultOf"]:
                    raise ValueError(f"Result/Execution request mismatch: {oid}")

    turns = {oid for oid, obj in objects.items() if obj["type"] == "Turn"}
    indegree = {oid: len(incoming[oid, "hasNextTurn"]) for oid in turns}
    ready = [oid for oid in turns if indegree[oid] == 0]
    visited = 0
    while ready:
        oid = ready.pop()
        visited += 1
        for target in outgoing[oid, "hasNextTurn"]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(turns):
        raise ValueError("hasNextTurn contains a cycle")

    event_attr_names: dict[str, list[str]] = defaultdict(list)
    for event in events:
        seen = set()
        for attr in event.get("attributes", []):
            aname = _identifier(attr["name"])
            if aname not in event_attr_names[event["type"]]:
                event_attr_names[event["type"]].append(aname)
        for relationship in event["relationships"]:
            target = _identifier(relationship["objectId"])
            qualifier = _identifier(relationship["qualifier"])
            if target not in objects:
                raise ValueError(
                    f"Unresolved E2O reference: {event['id']} -> {target}"
                )
            if (target, qualifier) in seen:
                raise ValueError(f"Duplicate E2O link in {event['id']}")
            seen.add((target, qualifier))

    log: dict[str, Any] = {
        "objectTypes": [
            {
                "name": typ,
                "attributes": [
                    {"name": name, "type": "string"}
                    for name in (
                        ["payload", "state"] if typ == "ProcessObject" else ["payload"]
                    )
                ],
            }
            for typ in OBJECT_TYPES
        ],
        "eventTypes": [
            {
                "name": typ,
                "attributes": [
                    {"name": aname, "type": "string"}
                    for aname in event_attr_names.get(typ, [])
                ],
            }
            for typ in dict.fromkeys(event["type"] for event in events)
        ],
        "objects": list(objects.values()),
        "events": events,
    }
    if write_output:
        (directory / "loopocel.json").write_text(
            json.dumps(log, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return log


def summarize_log(log: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute instance counts and ontology coverage statistics for a LoopOCEL log."""
    obj_counts = Counter(obj["type"] for obj in log.get("objects", []))
    o2o_counts = Counter(
        rel["qualifier"]
        for obj in log.get("objects", [])
        for rel in obj.get("relationships", [])
    )
    event_counts = Counter(evt["type"] for evt in log.get("events", []))
    e2o_counts = Counter(
        rel["qualifier"]
        for evt in log.get("events", [])
        for rel in evt.get("relationships", [])
    )

    partial_prompts = False
    for obj in log.get("objects", []):
        if obj["type"] == "Prompt":
            for attr in obj.get("attributes", []):
                if attr["name"] == "payload":
                    try:
                        val = json.loads(attr["value"])
                        if val.get("capture_completeness") == "partial":
                            partial_prompts = True
                    except Exception:
                        pass

    classes_by_name = {cls: obj_counts.get(cls, 0) for cls in OBJECT_TYPES}
    relations_by_name = {rel: o2o_counts.get(rel, 0) for rel in RELATIONS}
    baseline_events_by_name = {
        etype: event_counts.get(etype, 0) for etype in BASELINE_EVENT_SET
    }
    extension_events = {
        etype: cnt
        for etype, cnt in sorted(event_counts.items())
        if etype not in BASELINE_EVENT_SET
    }

    covered_classes = sum(1 for v in classes_by_name.values() if v > 0)
    covered_relations = sum(1 for v in relations_by_name.values() if v > 0)
    covered_baseline_events = sum(1 for v in baseline_events_by_name.values() if v > 0)

    completeness = (meta or {}).get(
        "capture_completeness",
        "partial" if partial_prompts else "complete",
    )

    return {
        "total_objects": len(log.get("objects", [])),
        "total_events": len(log.get("events", [])),
        "total_o2o": sum(o2o_counts.values()),
        "total_e2o": sum(e2o_counts.values()),
        "classes": classes_by_name,
        "relations": relations_by_name,
        "events": dict(sorted(event_counts.items())),
        "baseline_events": {
            etype: event_counts.get(etype, 0)
            for family in OBJECT_TYPES
            for etype in BASELINE_EVENTS_BY_FAMILY.get(family, ())
        },
        "extension_events": extension_events,
        "e2o_roles": dict(sorted(e2o_counts.items())),
        "coverage": {
            "classes_covered": covered_classes,
            "classes_total": len(OBJECT_TYPES),
            "relations_covered": covered_relations,
            "relations_total": len(RELATIONS),
            "baseline_events_covered": covered_baseline_events,
            "baseline_events_total": len(BASELINE_EVENT_SET),
            "extension_event_types": len(extension_events),
        },
        "capture_completeness": completeness,
        "partial_prompts": partial_prompts,
        "session": meta or {},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = convert(args.directory)
    print(json.dumps({"objects": len(result["objects"]), "events": len(result["events"])}))

