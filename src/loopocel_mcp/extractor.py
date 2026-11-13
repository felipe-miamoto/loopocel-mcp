"""Domain-agnostic ProcessObject extraction from structured tool arguments and results.

Extracts ProcessObjects strictly from explicit structural fields (typed *_id /
*_ids keys, prefixed identifiers, and explicit resource paths/URIs) and actual
environment state changes, never from free-text similarity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import quote


RESOURCE_PATH_KEYS = ("file_path", "path", "notebook_path", "TargetFile", "Path")
RESOURCE_URI_KEYS = ("url", "uri")
# Tools whose successful call writes the file named in their arguments. Used when the
# file cannot be probed before and after the call (e.g. transcripts ingested later).
FILE_WRITE_TOOLS = {
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "str_replace_editor",
}


@dataclass
class ObjectObservation:
    """Explicit ProcessObject participation and environment-verified state changes."""

    targets: list[str] = field(default_factory=list)
    acted_on: list[str] = field(default_factory=list)
    describes: list[str] = field(default_factory=list)
    initial_objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)


def _kind_from_field(key: str, custom_fields: dict[str, str] | None = None) -> str | None:
    if custom_fields and key in custom_fields:
        return custom_fields[key]
    if key.endswith("_id") and len(key) > 3:
        stem = key[:-3]
        return "".join(part.capitalize() for part in stem.split("_") if part)
    if key.endswith("_ids") and len(key) > 4:
        stem = key[:-4]
        return "".join(part.capitalize() for part in stem.split("_") if part)
    return None


def format_object_id(kind: str, raw_id: str) -> str:
    prefix = kind.lower()
    if ":" in raw_id and raw_id.split(":", 1)[0].isalpha() and not raw_id.startswith(("http:", "https:")):
        return raw_id
    if raw_id.startswith(prefix + ":"):
        return raw_id
    return f"{prefix}:{quote(raw_id, safe='/:._-#')}"


def extract_structured_entities(
    value: Any,
    custom_fields: dict[str, str] | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Extract (object_id, kind, state) triples from explicit structured fields."""
    found: dict[str, tuple[str, str, dict[str, Any]]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str) and node["id"] and isinstance(node.get("kind"), str):
                oid = format_object_id(node["kind"], node["id"])
                state = {k: v for k, v in node.items() if k != "id"}
                found[oid] = (oid, node["kind"], state)

            single_ids = [
                k
                for k, v in node.items()
                if isinstance(v, str) and not k.endswith("_ids") and _kind_from_field(k, custom_fields)
            ]
            sole_id_keys = set(single_ids) if len(single_ids) == 1 else set()
            for key, child in node.items():
                if key in RESOURCE_PATH_KEYS and isinstance(child, str) and child.strip():
                    p_str = child.strip()
                    oid = f"file:{p_str}"
                    found[oid] = (oid, "File", _probe_file_state(p_str))
                elif key in RESOURCE_URI_KEYS and isinstance(child, str) and (
                    child.startswith("http://") or child.startswith("https://")
                ):
                    oid = f"uri:{child}"
                    found[oid] = (oid, "URI", {"kind": "URI", "uri": child})
                else:
                    kind = _kind_from_field(key, custom_fields)
                    if kind is not None:
                        items = child if key.endswith("_ids") and isinstance(child, list) else [child]
                        for item in items:
                            if isinstance(item, str) and item.strip():
                                raw = item.strip()
                                oid = format_object_id(kind, raw)
                                state = {"kind": kind, "business_id": raw}
                                if key in sole_id_keys:
                                    # A record with exactly one *_id field describes that object;
                                    # its other scalar fields are the object's state.
                                    state.update(
                                        {
                                            k: v
                                            for k, v in node.items()
                                            if k != key
                                            and isinstance(v, (str, int, float, bool))
                                            and _kind_from_field(k, custom_fields) is None
                                        }
                                    )
                                prior = found.get(oid)
                                if prior is None or len(state) > len(prior[2]):
                                    found[oid] = (oid, kind, state)
                visit(child)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return [found[k] for k in sorted(found)]


def _probe_file_state(path_str: str) -> dict[str, Any]:
    state: dict[str, Any] = {"kind": "File", "path": path_str}
    try:
        p = Path(path_str)
        if p.is_file():
            raw = p.read_bytes()
            state["exists"] = True
            state["sha256"] = hashlib.sha256(raw).hexdigest()
            state["bytes"] = len(raw)
            return state
    except OSError:
        pass
    state["exists"] = False
    return state


class ProcessObjectExtractor:
    """Tracks ProcessObjects and environment-verified state changes across a session."""

    def __init__(self, custom_fields: dict[str, str] | None = None) -> None:
        self.custom_fields = custom_fields or {}
        self.states: dict[str, dict[str, Any]] = {}
        self.write_counts: dict[str, int] = {}

    def register_initial(self, objects: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        added: dict[str, dict[str, Any]] = {}
        for oid, state in objects.items():
            if oid not in self.states:
                self.states[oid] = dict(state)
                added[oid] = dict(state)
        return added

    def observe_request(
        self,
        tool_name: str,
        arguments: Any,
        *,
        explicit_targets: list[str] | None = None,
    ) -> ObjectObservation:
        obs = ObjectObservation()
        for oid in explicit_targets or ():
            if oid not in self.states:
                self.states[oid] = {"id": oid}
                obs.initial_objects[oid] = self.states[oid]
            if oid not in obs.targets:
                obs.targets.append(oid)

        for oid, _, state in extract_structured_entities(arguments, self.custom_fields):
            if oid not in self.states:
                self.states[oid] = state
                obs.initial_objects[oid] = state
            if oid not in obs.targets:
                obs.targets.append(oid)
        return obs

    def observe_execution_and_result(
        self,
        tool_name: str,
        arguments: Any,
        result_data: Any,
        *,
        outcome: str = "success",
        explicit_acted_on: list[str] | None = None,
        explicit_describes: list[str] | None = None,
        explicit_changes: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> ObjectObservation:
        obs = ObjectObservation()
        writes_file = tool_name in FILE_WRITE_TOOLS and outcome == "success"

        for oid, _, state in extract_structured_entities(arguments, self.custom_fields):
            prior = self.states.get(oid)
            changed_before = len(obs.changes)
            if prior is None:
                if state.get("kind") == "File" and state.get("exists"):
                    self.states[oid] = state
                    obs.changes.append({"id": oid, "state": state, "created": True})
                else:
                    self.states[oid] = state
                    obs.initial_objects[oid] = state
            else:
                if state.get("kind") == "File":
                    fresh = _probe_file_state(state["path"])
                    if fresh != prior and fresh.get("exists"):
                        created = not prior.get("exists", False)
                        self.states[oid] = fresh
                        obs.changes.append({"id": oid, "state": fresh, "created": created})
            if writes_file and state.get("kind") == "File" and len(obs.changes) == changed_before:
                count = self.write_counts.get(oid, 0) + 1
                self.write_counts[oid] = count
                written = {**self.states.get(oid, state), "written_by": tool_name, "writes": count}
                self.states[oid] = written
                obs.changes.append({"id": oid, "state": written, "created": False})
            if oid not in obs.acted_on:
                obs.acted_on.append(oid)
            if outcome == "success" and state.get("kind") in ("File", "URI") and oid not in obs.describes:
                obs.describes.append(oid)

        for oid, _, state in extract_structured_entities(result_data, self.custom_fields):
            if oid not in self.states:
                self.states[oid] = state
                obs.initial_objects[oid] = state
            elif state != self.states[oid] and len(state) > len(self.states[oid]):
                self.states[oid] = state
                obs.changes.append({"id": oid, "state": state, "created": False})
            if oid not in obs.describes:
                obs.describes.append(oid)
            if oid not in obs.acted_on:
                obs.acted_on.append(oid)

        if isinstance(explicit_changes, dict):
            # Map form: {"<kind>:<id>": {"state": {...}, "created": bool}} or {"<kind>:<id>": {...state}}
            explicit_changes = [
                {"id": k, **(v if isinstance(v, dict) and "state" in v else {"state": v})}
                for k, v in explicit_changes.items()
            ]
        for change in explicit_changes or ():
            cid = change["id"]
            cstate = change["state"]
            # An object is created only the first time it is seen; a later "created" flag
            # from the agent would register the same identity twice.
            created = cid not in self.states
            self.states[cid] = cstate
            obs.changes.append({"id": cid, "state": cstate, "created": created})
            if cid not in obs.acted_on:
                obs.acted_on.append(cid)

        for oid in explicit_acted_on or ():
            if oid not in self.states:
                self.states[oid] = {"id": oid}
                obs.initial_objects[oid] = self.states[oid]
            if oid not in obs.acted_on:
                obs.acted_on.append(oid)

        for oid in explicit_describes or ():
            if oid not in self.states:
                self.states[oid] = {"id": oid}
                obs.initial_objects[oid] = self.states[oid]
            if oid not in obs.describes:
                obs.describes.append(oid)

        return obs

