"""Process graphs discovered from LoopOCEL logs.

Two complementary views of the same recorded run(s):

* Agent-loop OC-DFG: the architectural view. Nodes are LoopOCEL event types,
  edges are directly-follows relations computed per object type (one colored
  edge per object type, as in an object-centric directly-follows graph).
* Semantic process graph: the domain view. Nodes are the tool actions the
  agent performed, named by tool and by the kinds of business ProcessObjects
  they touched; failed or rejected actions get their own node, so retries
  show up as rework loops. A table summarizes the business objects.

Both graphs are laid out with a small layered (Sugiyama-style) layout and drawn
once into a scene that is emitted as SVG and rendered to PNG with Matplotlib,
so text is always set in a real anti-aliased font.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import importlib.util
import json
import logging
import math
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .raster import svg_to_png

FONT_STACK = "Helvetica Neue, Helvetica, Arial, DejaVu Sans, sans-serif"
MPL_FONTS = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]

INK = "#1F2328"
MUTED = "#57606A"
FAINT = "#8C959F"
GRID = "#D0D7DE"

OBJECT_TYPE_COLORS: dict[str, str] = {
    "Turn": "#1F5F9E",
    "Prompt": "#3B8BD0",
    "Response": "#11807F",
    "Instruction": "#5B6B7A",
    "History": "#2E7D4F",
    "ActionRequest": "#7A4D9B",
    "Execution": "#B0428A",
    "ActionResult": "#C2651A",
    "ProcessObject": "#B3261E",
}
CANONICAL_EVENT_ORDER = (
    "Instruction.Registered",
    "History.Assembled",
    "Turn.Started",
    "Prompt.Submitted",
    "Response.Received",
    "Turn.Finished",
    "Turn.Failed",
    "ActionRequest.Dispatched",
    "ActionRequest.Rejected",
    "Execution.Started",
    "ProcessObject.Registered",
    "ProcessObject.Created",
    "ProcessObject.StateChanged",
    "Execution.Finished",
    "ActionResult.Received",
)
SUCCESS_OUTCOMES = {"success", "succeeded", "ok", "completed", "pending"}
STATE_FIELDS = ("status", "booking_state", "state", "phase")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _payloads(obj: dict[str, Any], name: str = "payload") -> list[Any]:
    out: list[Any] = []
    for attr in obj.get("attributes", []):
        if attr.get("name") != name:
            continue
        try:
            out.append(json.loads(attr["value"]))
        except (TypeError, ValueError, KeyError):
            out.append(attr.get("value"))
    return out


def _last_dict(values: list[Any]) -> dict[str, Any]:
    for v in reversed(values):
        if isinstance(v, dict):
            return v
    return {}


def _merged_state(obj: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for v in _payloads(obj, "payload") + _payloads(obj, "state"):
        if isinstance(v, dict):
            merged.update(v)
    return merged


def _kind(object_id: str) -> str:
    return object_id.split(":", 1)[0] if ":" in object_id else "object"


def _sorted_events(log: dict[str, Any]) -> list[dict[str, Any]]:
    indexed = list(enumerate(log.get("events", [])))
    indexed.sort(key=lambda p: (str(p[1].get("time", "")), p[0]))
    return [e for _, e in indexed]


def discover_ocdfg(logs: list[dict[str, Any]]) -> dict[str, Any]:
    """Object-centric directly-follows graph over LoopOCEL event types."""
    activity_counts: Counter[str] = Counter()
    edges: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    objects_per_type: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    position = 0

    for log in logs:
        otype_of = {o["id"]: o["type"] for o in log.get("objects", [])}
        objects_per_type.update(otype_of.values())
        timelines: dict[str, list[str]] = defaultdict(list)
        for ev in _sorted_events(log):
            etype = ev["type"]
            activity_counts[etype] += 1
            first_seen.setdefault(etype, position)
            position += 1
            for oid in dict.fromkeys(r["objectId"] for r in ev.get("relationships", [])):
                timelines[oid].append(etype)
        for oid, seq in timelines.items():
            otype = otype_of.get(oid, "Unknown")
            for a, b in zip(seq, seq[1:]):
                edges[(a, b)][otype] += 1

    order = {e: i for i, e in enumerate(CANONICAL_EVENT_ORDER)}
    activities = sorted(
        activity_counts,
        key=lambda a: (order.get(a, len(order)), first_seen.get(a, 0)),
    )
    return {
        "n_logs": len(logs),
        "activities": {a: activity_counts[a] for a in activities},
        "objects_per_type": dict(objects_per_type),
        "edges": [
            {"source": a, "target": b, "object_type": ot, "count": c}
            for (a, b), per_type in edges.items()
            for ot, c in sorted(per_type.items())
        ],
    }


def _action_steps(log: dict[str, Any]) -> list[dict[str, Any]]:
    objs = {o["id"]: o for o in log.get("objects", [])}
    o2o: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for oid, o in objs.items():
        for rel in o.get("relationships", []):
            o2o[oid][rel.get("qualifier", "")].append(rel.get("objectId", ""))

    dispatch_order: dict[str, int] = {}
    rejected: set[str] = set()
    changed_by_exec: dict[str, list[str]] = defaultdict(list)
    for idx, ev in enumerate(_sorted_events(log)):
        rels = {r.get("qualifier"): r.get("objectId") for r in ev.get("relationships", [])}
        if ev["type"] in ("ActionRequest.Dispatched", "ActionRequest.Rejected"):
            dispatch_order.setdefault(rels.get("subject", ""), idx)
            if ev["type"] == "ActionRequest.Rejected":
                rejected.add(rels.get("subject", ""))
        elif ev["type"] in ("ProcessObject.StateChanged", "ProcessObject.Created") and rels.get("execution"):
            changed_by_exec[rels["execution"]].append(rels.get("subject", ""))

    steps: list[dict[str, Any]] = []
    for rid in sorted(
        (oid for oid, o in objs.items() if o.get("type") == "ActionRequest"),
        key=lambda r: dispatch_order.get(r, 10**9),
    ):
        request = _last_dict(_payloads(objs[rid]))
        name = str(request.get("name") or "action")
        exec_ids = o2o[rid].get("hasExecution", [])
        eid = exec_ids[0] if exec_ids else ""
        if rid in rejected or request.get("rejected"):
            outcome = "rejected"
        elif eid in objs:
            outcome = str(_last_dict(_payloads(objs[eid])).get("outcome", "success")).lower()
        else:
            outcome = "success"
        touched = list(
            dict.fromkeys(
                o2o[rid].get("targetsObject", []) + (o2o[eid].get("actOn", []) if eid else [])
            )
        )
        touched = [t for t in touched if objs.get(t, {}).get("type") == "ProcessObject"]
        changed = [c for c in dict.fromkeys(changed_by_exec.get(eid, [])) if c]
        steps.append(
            {
                "request_id": rid,
                "internal": bool(request.get("internal")),
                "tool": name,
                "outcome": outcome,
                "failed": outcome not in SUCCESS_OUTCOMES,
                "touched": touched,
                "changed": changed,
            }
        )
    return steps


def _object_kind_stats(logs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for log in logs:
        changed: set[str] = set()
        for ev in log.get("events", []):
            if ev["type"] in ("ProcessObject.StateChanged", "ProcessObject.Created"):
                for rel in ev.get("relationships", []):
                    if rel.get("qualifier") == "subject":
                        changed.add(rel.get("objectId", ""))
        for o in log.get("objects", []):
            if o.get("type") != "ProcessObject":
                continue
            kind = _kind(o["id"])
            entry = stats.setdefault(kind, {"objects": 0, "changed": 0, "final_states": Counter()})
            entry["objects"] += 1
            entry["changed"] += int(o["id"] in changed)
            state = _merged_state(o)
            final = next((str(state[f]) for f in STATE_FIELDS if state.get(f) not in (None, "")), None)
            if final:
                entry["final_states"][final] += 1
    return {
        k: {**v, "final_states": dict(v["final_states"].most_common())}
        for k, v in sorted(stats.items(), key=lambda kv: (-kv[1]["objects"], kv[0]))
    }


def _activity_label(step: dict[str, Any]) -> str:
    if step["outcome"] == "rejected":
        return f"{step['tool']} [rejected]"
    if step["failed"]:
        return f"{step['tool']} [failed]"
    return step["tool"]


def discover_semantic_process(logs: list[dict[str, Any]]) -> dict[str, Any]:
    """Domain-level process: tool actions on business objects, with failures and rework."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: Counter[tuple[str, str]] = Counter()
    traces: list[list[str]] = []
    start, end = "Task start", "Task end"

    for log in logs:
        steps = _action_steps(log)
        trace = [start]
        for st in steps:
            key = _activity_label(st)
            node = nodes.setdefault(
                key,
                {
                    "tool": st["tool"],
                    "count": 0,
                    "failed": st["failed"],
                    "internal": st["internal"],
                    "outcome": st["outcome"],
                    "touched_kinds": Counter(),
                    "changed_kinds": Counter(),
                },
            )
            node["count"] += 1
            node["touched_kinds"].update(list(dict.fromkeys(_kind(t) for t in st["touched"])))
            node["changed_kinds"].update(_kind(c) for c in st["changed"])
            trace.append(key)
        trace.append(end)
        traces.append(trace)
        for a, b in zip(trace, trace[1:]):
            edges[(a, b)] += 1

    n = len(logs)
    return {
        "n_runs": n,
        "start": start,
        "end": end,
        "activities": {
            start: {"count": n, "kind": "start"},
            **{
                k: {
                    "count": v["count"],
                    "tool": v["tool"],
                    "kind": "failed" if v["failed"] else ("mutating" if v["changed_kinds"] else "action"),
                    "internal": v["internal"],
                    "outcome": v["outcome"],
                    "touched_kinds": dict(v["touched_kinds"].most_common()),
                    "changed_kinds": dict(v["changed_kinds"].most_common()),
                }
                for k, v in nodes.items()
            },
            end: {"count": n, "kind": "end"},
        },
        "edges": [{"source": a, "target": b, "count": c} for (a, b), c in edges.items()],
        "variants": dict(Counter(" -> ".join(t[1:-1]) for t in traces).most_common()),
        "objects": _object_kind_stats(logs),
    }


# --------------------------------------------------------------------------- #
# Scene: drawing primitives emitted to SVG and Matplotlib
# --------------------------------------------------------------------------- #

_NARROW = set("ijlt.,:;'|!()[] ")
_WIDE = set("mwMW@%")


def text_width(s: str, size: float, bold: bool = False) -> float:
    total = 0.0
    for ch in s:
        if ch in _NARROW:
            total += 0.28
        elif ch in _WIDE:
            total += 0.86
        elif ch.isupper() or ch.isdigit():
            total += 0.64 if ch.isupper() else 0.56
        else:
            total += 0.52
    return total * size * (1.07 if bold else 1.0)


def _tint(hex_color: str, amount: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: round(c + (255 - c) * amount)  # noqa: E731
    return f"#{mix(r):02X}{mix(g):02X}{mix(b):02X}"


@dataclass
class Scene:
    width: float
    height: float
    items: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def rect(self, x, y, w, h, *, fill="none", stroke="none", sw=1.0, r=0.0, dash=None) -> None:
        self.items.append(("rect", dict(x=x, y=y, w=w, h=h, fill=fill, stroke=stroke, sw=sw, r=r, dash=dash)))

    def text(self, x, y, s, *, size=12.0, color=INK, bold=False, anchor="middle", italic=False) -> None:
        self.items.append(
            ("text", dict(x=x, y=y, s=s, size=size, color=color, bold=bold, anchor=anchor, italic=italic))
        )

    def path(self, cmds, *, stroke=INK, sw=1.5, dash=None, opacity=1.0) -> None:
        self.items.append(("path", dict(cmds=cmds, stroke=stroke, sw=sw, dash=dash, opacity=opacity)))

    def polygon(self, pts, *, fill=INK, opacity=1.0) -> None:
        self.items.append(("polygon", dict(pts=pts, fill=fill, opacity=opacity)))

    def label(self, x, y, s, *, size=11.0, color=INK, bold=True) -> None:
        w = text_width(s, size, bold) + 8
        self.rect(x - w / 2, y - size * 0.82, w, size * 1.3, fill="#FFFFFF", r=3)
        self.text(x, y + size * 0.36 - 0.5, s, size=size, color=color, bold=bold)

    # ---- SVG ---------------------------------------------------------------
    def to_svg(self) -> str:
        W, H = math.ceil(self.width), math.ceil(self.height)
        out = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'font-family="{FONT_STACK}">',
            f'<rect x="0" y="0" width="{W}" height="{H}" fill="#FFFFFF"/>',
        ]
        for kind, a in self.items:
            if kind == "rect":
                dash = f' stroke-dasharray="{a["dash"][0]},{a["dash"][1]}"' if a["dash"] else ""
                out.append(
                    f'<rect x="{a["x"]:.1f}" y="{a["y"]:.1f}" width="{a["w"]:.1f}" height="{a["h"]:.1f}" '
                    f'rx="{a["r"]:.1f}" fill="{a["fill"]}" stroke="{a["stroke"]}" stroke-width="{a["sw"]}"{dash}/>'
                )
            elif kind == "text":
                anchor = {"middle": "middle", "left": "start", "right": "end"}[a["anchor"]]
                weight = ' font-weight="700"' if a["bold"] else ""
                style = ' font-style="italic"' if a["italic"] else ""
                out.append(
                    f'<text x="{a["x"]:.1f}" y="{a["y"]:.1f}" text-anchor="{anchor}" font-size="{a["size"]}"'
                    f'{weight}{style} fill="{a["color"]}">{escape(a["s"])}</text>'
                )
            elif kind == "path":
                d = " ".join(c[0] + " " + " ".join(f"{v:.1f}" for v in c[1:]) for c in a["cmds"])
                dash = f' stroke-dasharray="{a["dash"][0]},{a["dash"][1]}"' if a["dash"] else ""
                op = f' stroke-opacity="{a["opacity"]}"' if a["opacity"] < 1 else ""
                out.append(
                    f'<path d="{d}" fill="none" stroke="{a["stroke"]}" stroke-width="{a["sw"]}" '
                    f'stroke-linecap="round" stroke-linejoin="round"{dash}{op}/>'
                )
            elif kind == "polygon":
                pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in a["pts"])
                op = f' fill-opacity="{a["opacity"]}"' if a["opacity"] < 1 else ""
                out.append(f'<polygon points="{pts}" fill="{a["fill"]}"{op}/>')
        out.append("</svg>")
        return "\n".join(out) + "\n"

    # ---- Matplotlib --------------------------------------------------------
    def to_png(self, png_path: Path, *, scale: float = 2.0) -> bool:
        if importlib.util.find_spec("matplotlib") is None:
            return False
        import matplotlib

        matplotlib.use("Agg")
        logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch, PathPatch, Polygon
        from matplotlib.path import Path as MplPath

        px = 0.72  # one canvas unit is 1/100 inch = 0.72 pt
        with plt.rc_context(
            {"font.family": "sans-serif", "font.sans-serif": MPL_FONTS, "text.antialiased": True}
        ):
            fig = plt.figure(figsize=(self.width / 100, self.height / 100), dpi=100)
            ax = fig.add_axes((0, 0, 1, 1))
            ax.set_xlim(0, self.width)
            ax.set_ylim(self.height, 0)
            ax.set_axis_off()
            z = 1
            for kind, a in self.items:
                z += 1
                if kind == "rect":
                    ls = (0, (a["dash"][0] / a["sw"], a["dash"][1] / a["sw"])) if a["dash"] else "solid"
                    ax.add_patch(
                        FancyBboxPatch(
                            (a["x"], a["y"]),
                            a["w"],
                            a["h"],
                            boxstyle=f"round,pad=0,rounding_size={a['r']}" if a["r"] else "square,pad=0",
                            facecolor=a["fill"] if a["fill"] != "none" else "none",
                            edgecolor=a["stroke"] if a["stroke"] != "none" else "none",
                            linewidth=a["sw"] * px if a["stroke"] != "none" else 0,
                            linestyle=ls,
                            zorder=z,
                        )
                    )
                elif kind == "text":
                    ax.text(
                        a["x"],
                        a["y"],
                        a["s"],
                        fontsize=a["size"] * px,
                        color=a["color"],
                        fontweight="bold" if a["bold"] else "normal",
                        fontstyle="italic" if a["italic"] else "normal",
                        ha={"middle": "center", "left": "left", "right": "right"}[a["anchor"]],
                        va="baseline",
                        zorder=z,
                    )
                elif kind == "path":
                    verts, codes = [], []
                    for c in a["cmds"]:
                        if c[0] == "M":
                            verts.append(c[1:3])
                            codes.append(MplPath.MOVETO)
                        elif c[0] == "L":
                            verts.append(c[1:3])
                            codes.append(MplPath.LINETO)
                        elif c[0] == "Q":
                            verts += [c[1:3], c[3:5]]
                            codes += [MplPath.CURVE3] * 2
                        elif c[0] == "C":
                            verts += [c[1:3], c[3:5], c[5:7]]
                            codes += [MplPath.CURVE4] * 3
                    ls = (0, (a["dash"][0] / a["sw"], a["dash"][1] / a["sw"])) if a["dash"] else "solid"
                    ax.add_patch(
                        PathPatch(
                            MplPath(verts, codes),
                            facecolor="none",
                            edgecolor=a["stroke"],
                            linewidth=a["sw"] * px,
                            linestyle=ls,
                            alpha=a["opacity"],
                            capstyle="round",
                            joinstyle="round",
                            zorder=z,
                        )
                    )
                elif kind == "polygon":
                    ax.add_patch(
                        Polygon(a["pts"], closed=True, facecolor=a["fill"], edgecolor="none", alpha=a["opacity"], zorder=z)
                    )
            png_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(png_path, dpi=100 * scale, facecolor="white")
            plt.close(fig)
        return png_path.is_file()


def _arrowhead(scene: Scene, tip: tuple[float, float], frm: tuple[float, float], color: str, size: float) -> None:
    dx, dy = tip[0] - frm[0], tip[1] - frm[1]
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n
    length, half = size * 2.3, size
    bx, by = tip[0] - ux * length, tip[1] - uy * length
    scene.polygon([tip, (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half)], fill=color)


def _bezier_point(p0, p1, p2, p3, t):
    mt = 1 - t
    return (
        mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
        mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1],
    )


def _rounded_orthogonal(points: list[tuple[float, float]], radius: float = 10.0) -> list[tuple]:
    cmds: list[tuple] = [("M", *points[0])]
    for i in range(1, len(points) - 1):
        (x0, y0), (x1, y1), (x2, y2) = points[i - 1], points[i], points[i + 1]
        d0 = math.hypot(x1 - x0, y1 - y0) or 1
        d1 = math.hypot(x2 - x1, y2 - y1) or 1
        r = min(radius, d0 / 2, d1 / 2)
        a = (x1 - (x1 - x0) / d0 * r, y1 - (y1 - y0) / d0 * r)
        b = (x1 + (x2 - x1) / d1 * r, y1 + (y2 - y1) / d1 * r)
        cmds += [("L", *a), ("Q", x1, y1, *b)]
    cmds.append(("L", *points[-1]))
    return cmds


# --------------------------------------------------------------------------- #
# Layered layout
# --------------------------------------------------------------------------- #


@dataclass
class _Node:
    key: str
    w: float
    h: float
    layer: int = 0
    x: float = 0.0
    y: float = 0.0
    dummy: bool = False


def _layout(
    node_sizes: dict[str, tuple[float, float]],
    edges: list[tuple[str, str]],
    *,
    col_gap: float = 96.0,
    row_gap: float = 34.0,
) -> tuple[dict[str, _Node], dict[tuple[str, str], list[str]], set[tuple[str, str]]]:
    """Left-to-right layered layout. Returns nodes, dummy chains of forward edges, back edges."""
    keys = list(node_sizes)
    rank = {k: i for i, k in enumerate(keys)}
    succ: dict[str, list[str]] = defaultdict(list)
    for a, b in dict.fromkeys(edges):
        if a != b:
            succ[a].append(b)
    for a in succ:
        succ[a].sort(key=rank.get)

    # Break cycles with a DFS in the given node order.
    back: set[tuple[str, str]] = set()
    state: dict[str, int] = {}

    def dfs(u: str) -> None:
        state[u] = 1
        for v in succ[u]:
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2

    for k in keys:
        if k not in state:
            dfs(k)

    forward = [(a, b) for a, b in dict.fromkeys(edges) if a != b and (a, b) not in back]
    preds: dict[str, list[str]] = defaultdict(list)
    for a, b in forward:
        preds[b].append(a)
    layer: dict[str, int] = {}

    def depth(u: str) -> int:
        if u not in layer:
            layer[u] = 0
            layer[u] = max((depth(p) + 1 for p in preds[u]), default=0)
        return layer[u]

    for k in keys:
        depth(k)

    # Pull sources right, next to their earliest successor, to avoid long edges.
    fsucc: dict[str, list[str]] = defaultdict(list)
    for a, b in forward:
        fsucc[a].append(b)
    for k in sorted(keys, key=lambda k: -layer[k]):
        if not preds[k] and fsucc[k]:
            layer[k] = max(0, min(layer[b] for b in fsucc[k]) - 1)
    # Isolated nodes go to the last layer that has room, after connected ones.
    isolated = [k for k in keys if not preds[k] and not fsucc[k] and not any(a == k or b == k for a, b in edges)]
    if isolated and len(isolated) < len(keys):
        for k in isolated:
            layer[k] = 0

    nodes = {k: _Node(k, *node_sizes[k], layer=layer[k]) for k in keys}
    chains: dict[tuple[str, str], list[str]] = {}
    adjacency: list[tuple[str, str]] = []
    for a, b in forward:
        chain = [a]
        for lyr in range(layer[a] + 1, layer[b]):
            d = f"__dummy__{a}->{b}#{lyr}"
            nodes[d] = _Node(d, 0.0, 14.0, layer=lyr, dummy=True)
            chain.append(d)
        chain.append(b)
        chains[(a, b)] = chain
        adjacency += list(zip(chain, chain[1:]))

    n_layers = max(n.layer for n in nodes.values()) + 1
    layers: list[list[str]] = [[] for _ in range(n_layers)]
    for k in sorted(nodes, key=lambda k: (k in isolated, rank.get(k, len(rank)), k)):
        layers[nodes[k].layer].append(k)

    up: dict[str, list[str]] = defaultdict(list)
    down: dict[str, list[str]] = defaultdict(list)
    for a, b in adjacency:
        down[a].append(b)
        up[b].append(a)

    # Crossing reduction: barycenter sweeps.
    for _ in range(6):
        for direction in (1, -1):
            rng = range(1, n_layers) if direction == 1 else range(n_layers - 2, -1, -1)
            for li in rng:
                ref = {k: i for i, k in enumerate(layers[li - direction])}
                nbrs = up if direction == 1 else down
                cur = {k: i for i, k in enumerate(layers[li])}

                def bary(k: str) -> float:
                    vals = [ref[n] for n in nbrs[k] if n in ref]
                    return sum(vals) / len(vals) if vals else cur[k]

                layers[li].sort(key=lambda k: (bary(k), cur[k]))

    # Coordinates: columns left to right, rows stacked then pulled toward neighbors.
    x = 0.0
    for li, lyr in enumerate(layers):
        col_w = max((nodes[k].w for k in lyr), default=0.0)
        for k in lyr:
            nodes[k].x = x + (col_w - nodes[k].w) / 2
        x += col_w + col_gap

    def sep(a: str, b: str) -> float:
        gap = 10.0 if nodes[a].dummy or nodes[b].dummy else row_gap
        return nodes[a].h / 2 + nodes[b].h / 2 + gap

    def place(lyr: list[str], desired: list[float]) -> None:
        pos = list(desired)
        for i in range(1, len(pos)):
            pos[i] = max(pos[i], pos[i - 1] + sep(lyr[i - 1], lyr[i]))
        shift = sum(d - p for d, p in zip(desired, pos)) / max(1, len(pos))
        for k, p in zip(lyr, pos):
            nodes[k].y = p + shift

    for lyr in layers:
        place(lyr, [0.0] * len(lyr))
    for _ in range(8):
        for direction in (1, -1):
            order = layers if direction == 1 else layers[::-1]
            for lyr in order:
                nb = up if direction == 1 else down
                desired = []
                for k in lyr:
                    ys = [nodes[n].y for n in nb[k] + (down if direction == 1 else up)[k]]
                    desired.append(sum(ys) / len(ys) if ys else nodes[k].y)
                place(lyr, desired)
    return nodes, chains, back


# --------------------------------------------------------------------------- #
# Generic graph drawing
# --------------------------------------------------------------------------- #


@dataclass
class _Edge:
    source: str
    target: str
    count: int
    color: str
    label: str
    width: float
    dashed: bool = False


def _draw_graph(
    *,
    title: str,
    subtitle: str,
    node_lines: dict[str, list[tuple[str, float, str, bool]]],
    node_style: dict[str, tuple[str, str, bool]],
    edges: list[_Edge],
    footer: list[list[tuple[str, str]]] | None = None,
    legend: list[tuple[str, str, bool]] | None = None,
    table: tuple[list[str], list[list[str]]] | None = None,
    node_h: float = 54.0,
) -> Scene:
    """Lay out and draw a directed graph into a Scene.

    node_lines: per node, text lines as (text, size, color, bold).
    node_style: per node, (stroke, fill, pill_shape).
    """
    pad_x = 16.0
    sizes: dict[str, tuple[float, float]] = {}
    for k, lines in node_lines.items():
        w = max(text_width(t, s, b) for t, s, _, b in lines) + 2 * pad_x
        sizes[k] = (max(w, 92.0), node_h)

    self_loops = Counter(e.source for e in edges if e.source == e.target)
    row_gap = 34.0 + (20.0 * max(self_loops.values()) if self_loops else 0.0)
    nodes, chains, back = _layout(sizes, [(e.source, e.target) for e in edges], row_gap=row_gap)

    real = [n for n in nodes.values() if not n.dummy]
    min_y = min(n.y - n.h / 2 for n in nodes.values())
    max_y = max(n.y + n.h / 2 for n in nodes.values())
    max_x = max(n.x + n.w for n in nodes.values())

    loop_room = 26.0 + 14.0 * max(self_loops.values(), default=0) if self_loops else 0.0
    back_edges = [e for e in edges if (e.source, e.target) in back]
    lane_gap = 22.0
    margin = 36.0
    header = 74.0
    ox = margin
    oy = margin + header + loop_room - min_y
    for n in nodes.values():
        n.x += ox
        n.y += oy
    graph_bottom = max_y + oy
    lanes_bottom = graph_bottom + (len({(e.source, e.target) for e in back_edges}) * lane_gap + 18 if back_edges else 0)

    table_h = 0.0
    if table:
        table_h = 40.0 + 22.0 * (len(table[1]) + 1)
    legend_h = 34.0 if legend else 0.0
    footer_h = 20.0 * len(footer or [])
    width = max(max_x + ox + margin, 760.0)
    height = lanes_bottom + 26 + legend_h + table_h + footer_h + margin
    scene = Scene(width, height)

    scene.text(margin, margin + 18, title, size=19, bold=True, anchor="left")
    scene.text(margin, margin + 42, subtitle, size=12.5, color=MUTED, anchor="left")
    scene.path([("M", margin, margin + 58), ("L", width - margin, margin + 58)], stroke=GRID, sw=1)

    # Ports: spread edge endpoints over each node side, ordered by the other end.
    by_pair: dict[tuple[str, str], list[_Edge]] = defaultdict(list)
    for e in edges:
        by_pair[(e.source, e.target)].append(e)
    out_ports: dict[str, list[tuple[float, _Edge]]] = defaultdict(list)
    in_ports: dict[str, list[tuple[float, _Edge]]] = defaultdict(list)
    for (a, b), group in by_pair.items():
        if a == b or (a, b) in back:
            continue
        chain = chains[(a, b)]
        for i, e in enumerate(group):
            out_ports[a].append((nodes[chain[1]].y + i * 0.01, e))
            in_ports[b].append((nodes[chain[-2]].y + i * 0.01, e))

    def port_map(ports: dict[str, list[tuple[float, _Edge]]]) -> dict[int, float]:
        res: dict[int, float] = {}
        for k, lst in ports.items():
            lst.sort(key=lambda p: p[0])
            span = min(nodes[k].h * 0.62, 12.0 * (len(lst) - 1))
            for i, (_, e) in enumerate(lst):
                off = 0.0 if len(lst) == 1 else -span / 2 + span * i / (len(lst) - 1)
                res[id(e)] = nodes[k].y + off
        return res

    out_y, in_y = port_map(out_ports), port_map(in_ports)
    labels: list[tuple[float, float, str, str]] = []

    # Forward edges.
    for (a, b), group in by_pair.items():
        if a == b or (a, b) in back:
            continue
        chain = chains[(a, b)]
        for gi, e in enumerate(group):
            pts = [(nodes[a].x + nodes[a].w, out_y[id(e)])]
            pts += [(nodes[d].x, nodes[d].y + (gi - (len(group) - 1) / 2) * 6) for d in chain[1:-1]]
            pts.append((nodes[b].x - 2, in_y[id(e)]))
            cmds: list[tuple] = [("M", *pts[0])]
            segs = []
            for p, q in zip(pts, pts[1:]):
                dx = max(30.0, (q[0] - p[0]) * 0.5)
                c1, c2 = (p[0] + dx, p[1]), (q[0] - dx, q[1])
                cmds.append(("C", *c1, *c2, *q))
                segs.append((p, c1, c2, q))
            scene.path(cmds, stroke=e.color, sw=e.width, dash=(6, 4) if e.dashed else None, opacity=0.92)
            p0, c1, c2, p3 = segs[-1]
            _arrowhead(scene, p3, _bezier_point(p0, c1, c2, p3, 0.9), e.color, 3.2 + e.width * 0.45)
            first = segs[0]
            t = 0.5 if len(group) == 1 else 0.3 + 0.4 * gi / (len(group) - 1)
            lx, ly = _bezier_point(*first, t)
            labels.append((lx, ly, e.label, e.color))

    # Self loops above the node.
    loops_seen: Counter[str] = Counter()
    for e in edges:
        if e.source != e.target:
            continue
        n = nodes[e.source]
        k = loops_seen[e.source]
        loops_seen[e.source] += 1
        top = n.y - n.h / 2
        lift = 22.0 + 14.0 * k
        x0, x1 = n.x + n.w * 0.62 + 6 * k, n.x + n.w * 0.38 - 6 * k
        cmds = [("M", x0, top), ("C", x0 + 10, top - lift, x1 - 10, top - lift, x1, top - 2)]
        scene.path(cmds, stroke=e.color, sw=e.width, dash=(6, 4) if e.dashed else None, opacity=0.92)
        _arrowhead(scene, (x1, top - 1), (x1 - 4, top - 10), e.color, 3.2 + e.width * 0.45)
        labels.append((n.x + n.w / 2, top - lift * 0.78, e.label, e.color))

    # Back edges routed in lanes below the graph.
    lane_of: dict[tuple[str, str], int] = {}
    for e in sorted(back_edges, key=lambda e: abs(nodes[e.source].x - nodes[e.target].x)):
        pair = (e.source, e.target)
        lane = lane_of.setdefault(pair, len(lane_of))
        same = [x for x in by_pair[pair] if x in back_edges]
        gi = same.index(e)
        a, b = nodes[e.source], nodes[e.target]
        lane_y = graph_bottom + 18 + lane * lane_gap + gi * 5
        sx = a.x + a.w + 14 + 4 * lane
        tx = b.x - 16 - 4 * lane
        sy = a.y + a.h * 0.22 + gi * 4
        ty = b.y + b.h * 0.22 + gi * 4
        pts = [(a.x + a.w, sy), (sx, sy), (sx, lane_y), (tx, lane_y), (tx, ty), (b.x - 2, ty)]
        scene.path(_rounded_orthogonal(pts, 9), stroke=e.color, sw=e.width, dash=(6, 4), opacity=0.92)
        _arrowhead(scene, (b.x - 1, ty), (tx, ty), e.color, 3.2 + e.width * 0.45)
        labels.append(((sx + tx) / 2, lane_y, e.label, e.color))

    # Nodes.
    for n in real:
        stroke, fill, pill = node_style[n.key]
        top = n.y - n.h / 2
        scene.rect(n.x, top, n.w, n.h, fill=fill, stroke=stroke, sw=1.8, r=n.h / 2 if pill else 9)
        lines = node_lines[n.key]
        total = sum(s * 1.28 for _, s, _, _ in lines)
        cy = n.y - total / 2
        for t, s, c, b in lines:
            cy += s * 1.28
            scene.text(n.x + n.w / 2, cy - s * 0.3, t, size=s, color=c, bold=b)

    for lx, ly, s, c in labels:
        if s:
            scene.label(lx, ly, s, size=11, color=c)

    # Legend, table, footer.
    y = lanes_bottom + 30
    if legend:
        x = margin
        for name, color, dashed in legend:
            scene.path([("M", x, y - 4), ("L", x + 22, y - 4)], stroke=color, sw=2.6, dash=(5, 3) if dashed else None)
            scene.text(x + 28, y, name, size=11.5, color=INK, anchor="left")
            x += 28 + text_width(name, 11.5) + 22
            if x > width - 160:
                x = margin
                y += 20
        y += legend_h
    if table:
        headers, rows = table
        col_w = [
            max(text_width(r[i], 12) for r in rows + [headers]) + 30 for i in range(len(headers))
        ]
        scene.text(margin, y, "Business objects (ProcessObject)", size=13, bold=True, anchor="left")
        y += 10
        x0 = margin
        total_w = sum(col_w)
        scene.rect(x0, y, total_w, 22, fill="#F6F8FA", stroke="none")
        x = x0
        for h, cw in zip(headers, col_w):
            scene.text(x + 10, y + 15.5, h, size=11.5, color=MUTED, bold=True, anchor="left")
            x += cw
        for ri, row in enumerate(rows):
            ry = y + 22 * (ri + 1)
            scene.path([("M", x0, ry), ("L", x0 + total_w, ry)], stroke=GRID, sw=0.8)
            x = x0
            for ci, (cell, cw) in enumerate(zip(row, col_w)):
                scene.text(x + 10, ry + 15.5, cell, size=12, color=INK, bold=ci == 0, anchor="left")
                x += cw
        y += 22 * (len(rows) + 1) + 26
    for parts in footer or []:
        x = margin
        for s, c in parts:
            scene.text(x, y, s, size=11.5, color=c, anchor="left")
            x += text_width(s, 11.5)
        y += 20
    return scene


def _edge_width(count: int, max_count: int) -> float:
    return 1.4 + 2.6 * math.sqrt(count / max(1, max_count))


# --------------------------------------------------------------------------- #
# The two views
# --------------------------------------------------------------------------- #


def ocdfg_scene(
    ocdfg: dict[str, Any], *, title: str, subtitle: str, notes: list[str] | None = None
) -> Scene:
    acts = ocdfg["activities"]
    max_c = max((e["count"] for e in ocdfg["edges"]), default=1)
    node_lines, node_style = {}, {}
    for a, c in acts.items():
        cls, _, verb = a.partition(".")
        color = OBJECT_TYPE_COLORS.get(cls, "#5B6B7A")
        node_lines[a] = [(cls, 11.5, color, True), (verb or a, 14, INK, True), (f"{c:,} event{'s' if c != 1 else ''}", 11, MUTED, False)]
        node_style[a] = (color, _tint(color, 0.9), False)
    type_order = list(OBJECT_TYPE_COLORS)
    edges = [
        _Edge(
            e["source"],
            e["target"],
            e["count"],
            OBJECT_TYPE_COLORS.get(e["object_type"], "#5B6B7A"),
            f"{e['count']:,}",
            _edge_width(e["count"], max_c),
        )
        for e in sorted(
            ocdfg["edges"],
            key=lambda e: type_order.index(e["object_type"]) if e["object_type"] in type_order else 99,
        )
    ]
    present = [t for t in type_order if any(e["object_type"] == t for e in ocdfg["edges"])]
    legend = [(t, OBJECT_TYPE_COLORS[t], False) for t in present]
    footer = [
        [("Edges: directly-follows relations per object type (color = object type, label = count). ", MUTED)],
        [("Dashed edges close a cycle in the agent loop.", MUTED)],
    ] + [[(f"Warning: {n}", "#B3261E")] for n in notes or ()]
    return _draw_graph(
        title=title,
        subtitle=subtitle,
        node_lines=node_lines,
        node_style=node_style,
        edges=edges,
        legend=legend,
        footer=footer,
        node_h=62,
    )


def _kinds_text(kinds: dict[str, int], limit: int = 3) -> str:
    names = list(kinds)
    text = ", ".join(names[:limit])
    return text + (f" +{len(names) - limit}" if len(names) > limit else "")


def semantic_scene(
    sem: dict[str, Any], *, title: str, subtitle: str, notes: list[str] | None = None
) -> Scene:
    acts = sem["activities"]
    max_c = max((e["count"] for e in sem["edges"]), default=1)
    styles = {
        "start": ("#24292F", "#24292F", True),
        "end": ("#24292F", "#24292F", True),
        "action": ("#1F5F9E", "#EEF4FB", False),
        "mutating": ("#B85D19", "#FFF5EB", False),
        "failed": ("#B3261E", "#FDEFEE", False),
    }
    node_lines, node_style = {}, {}
    for k, a in acts.items():
        kind = a["kind"]
        stroke, fill, pill = styles[kind]
        node_style[k] = (stroke, fill, pill)
        if kind in ("start", "end"):
            node_lines[k] = [(k, 13.5, "#FFFFFF", True), (f"{a['count']} run{'s' if a['count'] != 1 else ''}", 11, "#D0D7DE", False)]
            continue
        lines = [(a["tool"], 14, INK, True)]
        if a.get("internal"):
            lines.append(("agent step, no tool", 11, FAINT, False))
        if kind == "failed":
            lines.append((a.get("outcome", "failed"), 11.5, stroke, True))
        on = _kinds_text(a["touched_kinds"])
        if on:
            lines.append((f"on {on}", 11.5, MUTED, False))
        if a["changed_kinds"]:
            lines.append(
                ("changes " + ", ".join(f"{k2} ×{v}" for k2, v in list(a["changed_kinds"].items())[:3]), 11.5, "#B85D19", False)
            )
        lines.append((f"{a['count']}×", 12, stroke, True))
        node_lines[k] = lines
    n_lines = max(len(v) for v in node_lines.values())
    edges = []
    for e in sem["edges"]:
        tgt_failed = acts[e["target"]]["kind"] == "failed"
        src_failed = acts[e["source"]]["kind"] == "failed"
        color = "#B3261E" if tgt_failed else ("#2E7D4F" if src_failed else "#57606A")
        edges.append(_Edge(e["source"], e["target"], e["count"], color, f"{e['count']}", _edge_width(e["count"], max_c), dashed=src_failed))
    legend = [
        ("read / query action", "#1F5F9E", False),
        ("action that changes business objects", "#B85D19", False),
        ("failed or rejected action", "#B3261E", False),
        ("recovery after a failure", "#2E7D4F", True),
    ]
    if any(a.get("internal") for a in acts.values()):
        footer_internal = "Steps marked 'agent step, no tool' were done by the agent itself (checking, deciding), not by a tool."
    else:
        footer_internal = ""
    rows = [
        [
            kind,
            str(s["objects"]),
            str(s["changed"]),
            ", ".join(f"{st} ×{c}" for st, c in list(s["final_states"].items())[:4]) or "—",
        ]
        for kind, s in sem["objects"].items()
    ]
    table = (["kind", "objects", "changed", "final state"], rows) if rows else None
    variants = sem.get("variants", {})
    footer = [[(f"{len(variants)} distinct action sequence{'s' if len(variants) != 1 else ''} across {sem['n_runs']} run{'s' if sem['n_runs'] != 1 else ''}. Edge label = number of transitions.", MUTED)]]
    if footer_internal:
        footer.append([(footer_internal, MUTED)])
    footer += [[(f"Warning: {n}", "#B3261E")] for n in notes or ()]
    return _draw_graph(
        title=title,
        subtitle=subtitle,
        node_lines=node_lines,
        node_style=node_style,
        edges=edges,
        legend=legend,
        table=table,
        footer=footer,
        node_h=24 + 17 * n_lines,
    )


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def _write(scene: Scene, out_dir: Path, stem: str, formats: tuple[str, ...]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{stem}.svg"
    svg_path.write_text(scene.to_svg(), encoding="utf-8")
    outputs = {"svg": str(svg_path.resolve())}
    if "png" in formats:
        png_path = out_dir / f"{stem}.png"
        if scene.to_png(png_path) or svg_to_png(svg_path, png_path):
            outputs["png"] = str(png_path.resolve())
    return outputs


def render_process_graphs(
    logs: list[dict[str, Any]],
    out_dir: Path | str,
    *,
    label: str = "session",
    task: str | None = None,
    notes: list[str] | None = None,
    formats: tuple[str, ...] = ("svg", "png"),
) -> dict[str, Any]:
    """Discover and render both process graphs for one or more LoopOCEL logs.

    `notes` are printed in red under both graphs (e.g. provenance warnings).
    """
    out_dir = Path(out_dir)
    ocdfg = discover_ocdfg(logs)
    sem = discover_semantic_process(logs)
    n_events = sum(ocdfg["activities"].values())
    n_objects = sum(ocdfg["objects_per_type"].values())
    runs = f"{len(logs)} run{'s' if len(logs) != 1 else ''}"
    scope = f"{label} · {runs}" + (f" · {task}" if task else "")
    if len(scope) > 150:
        scope = scope[:147] + "…"

    oc_scene = ocdfg_scene(
        ocdfg,
        title="Agent-loop OC-DFG",
        subtitle=f"{scope} · {n_events:,} events · {n_objects:,} objects",
        notes=notes,
    )
    n_actions = sum(a["count"] for a in sem["activities"].values() if a["kind"] not in ("start", "end"))
    sem_scene = semantic_scene(
        sem,
        title="Semantic process graph",
        subtitle=f"{scope} · {n_actions} tool actions · {sum(s['objects'] for s in sem['objects'].values())} business objects",
        notes=notes,
    )
    result = {
        "ocdfg": ocdfg,
        "semantic_process": sem,
        "outputs": {
            "ocdfg": _write(oc_scene, out_dir, "ocdfg_process_graph", formats),
            "semantic": _write(sem_scene, out_dir, "semantic_process_graph", formats),
        },
    }
    (out_dir / "process_graphs.json").write_text(
        json.dumps({k: result[k] for k in ("ocdfg", "semantic_process")}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def _load_session(session_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    ocel_path = session_dir / "loopocel.json"
    if ocel_path.is_file():
        log = json.loads(ocel_path.read_text(encoding="utf-8"))
    else:
        from .convert import convert

        log = convert(session_dir)
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    return log, meta


def render_session_process_graphs(
    session_dir: Path | str,
    *,
    include_dirs: list[Path | str] | None = None,
    formats: tuple[str, ...] = ("svg", "png"),
) -> dict[str, Any]:
    """Render both graphs for a session, optionally aggregating further sessions into it."""
    session_dir = Path(session_dir)
    log, meta = _load_session(session_dir)
    logs = [log]
    labels = [meta.get("session_id", session_dir.name)]
    for other in include_dirs or ():
        other_log, other_meta = _load_session(Path(other))
        logs.append(other_log)
        labels.append(other_meta.get("session_id", Path(other).name))
    label = labels[0] if len(labels) == 1 else f"{labels[0]} + {len(labels) - 1} more"
    task = meta.get("task") if len(labels) == 1 else None
    metas = [meta] + [_load_session(Path(o))[1] for o in include_dirs or ()]
    notes = []
    for lbl, m in zip(labels, metas):
        for w in (m.get("provenance") or {}).get("warnings", []):
            notes.append(w if len(labels) == 1 else f"{lbl}: {w}")
    res = render_process_graphs(logs, session_dir, label=label, task=task, notes=notes, formats=formats)
    res["sessions"] = labels
    return res
