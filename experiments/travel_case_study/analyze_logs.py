from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from loopocel_mcp.convert import (  # noqa: E402
    BASELINE_EVENTS_BY_FAMILY,
    BASELINE_EVENT_SET,
    OBJECT_TYPES,
    RELATIONS,
    summarize_log,
)
from loopocel_mcp.figure import (  # noqa: E402
    CohortData,
    cohort_from_log,
    render_figure,
)
from loopocel_mcp.validate import pm4py_check, validate_log  # noqa: E402

TASKS_FILE = HERE / "tasks.json"
SESSIONS_DIR = REPO_ROOT / "sessions"
RESULTS_DIR = HERE / "results"
FIGURES_DIR = HERE / "figures"


FONT_5X7: dict[str, tuple[int, ...]] = {
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x06, 0x08, 0x10, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x06, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C),
    ",": (0x00, 0x00, 0x00, 0x00, 0x04, 0x04, 0x08),
    ":": (0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x0C, 0x00),
    ";": (0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x04, 0x08),
    "|": (0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "/": (0x01, 0x02, 0x02, 0x04, 0x08, 0x08, 0x10),
    "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    "_": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F),
    "+": (0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00),
    "*": (0x00, 0x15, 0x0E, 0x1F, 0x0E, 0x15, 0x00),
    "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
    ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
    "[": (0x0E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0E),
    "]": (0x0E, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0E),
    "=": (0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00),
    ">": (0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08),
    "<": (0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02),
    "%": (0x19, 0x19, 0x02, 0x04, 0x08, 0x13, 0x13),
    " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
}


def rasterize_svg_to_png(svg_path: Path, png_path: Path) -> None:
    """Rasterize SVG to high-DPI anti-aliased PNG via Chrome headless, or pure-Python fallback."""
    png_path.parent.mkdir(parents=True, exist_ok=True)
    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(chrome_path).is_file():
        try:
            tree = ET.parse(svg_path)
            root = tree.getroot()
            w = int(float(root.attrib.get("width", "1360")))
            h = int(float(root.attrib.get("height", "325")))
            html_content = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>*{{margin:0;padding:0;box-sizing:border-box;}}html,body{{width:{w}px;height:{h}px;margin:0;padding:0;overflow:hidden;background:#ffffff;}}svg{{width:{w}px;height:{h}px;display:block;}}</style></head><body>{svg_path.read_text(encoding='utf-8')}</body></html>"
            tmp_html = svg_path.with_suffix(".render_tmp.html")
            tmp_html.write_text(html_content, encoding="utf-8")
            res = subprocess.run(
                [
                    chrome_path,
                    "--headless=new",
                    "--no-sandbox",
                    "--hide-scrollbars",
                    "--force-device-scale-factor=2",
                    f"--window-size={w},{h}",
                    f"--screenshot={png_path}",
                    f"file://{tmp_html.resolve()}",
                ],
                capture_output=True,
                timeout=30,
            )
            tmp_html.unlink(missing_ok=True)
            if res.returncode == 0 and png_path.is_file() and png_path.stat().st_size > 20000:
                return
        except Exception:
            pass

    tree = ET.parse(svg_path)
    root = tree.getroot()
    width = int(float(root.attrib.get("width", "1320")))
    height = int(float(root.attrib.get("height", "620")))
    buf = bytearray(b"\xff\xff\xff" * (width * height))

    def parse_hex(c: str) -> tuple[int, int, int]:
        if not c or c == "none":
            return (255, 255, 255)
        h = c.lstrip("#")
        if len(h) == 6:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (30, 35, 45)

    def set_px(px: int, py: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= px < width and 0 <= py < height:
            idx = (py * width + px) * 3
            buf[idx : idx + 3] = bytes(rgb)

    def draw_line(
        x0: float, y0: float, x1: float, y1: float, rgb: tuple[int, int, int], dashed: bool = False
    ) -> None:
        px0, py0, px1, py1 = int(x0), int(y0), int(x1), int(y1)
        steps = max(abs(px1 - px0), abs(py1 - py0), 1)
        for s in range(steps + 1):
            if dashed and (s // 6) % 2 == 1:
                continue
            px = int(px0 + (px1 - px0) * s / steps)
            py = int(py0 + (py1 - py0) * s / steps)
            set_px(px, py, rgb)
            set_px(px + 1, py, rgb)
            set_px(px, py + 1, rgb)

    def draw_text(x: float, y: float, text: str, rgb: tuple[int, int, int], anchor: str, fsize: float) -> None:
        scale = 2 if fsize >= 14.5 else 1
        clean = (
            text.replace("→", "->")
            .replace("≤", "<=")
            .replace("≥", ">=")
            .replace("±", "+/-")
            .replace("€", "EUR ")
            .replace("•", "*")
            .replace("↺", "<-")
            .replace("—", "-")
            .replace("–", "-")
            .replace("ä", "a")
            .replace("ö", "o")
            .replace("ü", "u")
            .replace("ó", "o")
            .replace("ń", "n")
        )
        char_w = 6 * scale
        total_w = len(clean) * char_w
        px, py = int(x), int(y)
        if anchor == "middle":
            sx0 = px - total_w // 2
        elif anchor == "end":
            sx0 = px - total_w
        else:
            sx0 = px
        sy0 = py - (7 * scale)
        for ci, ch in enumerate(clean):
            glyph = FONT_5X7.get(ch.upper(), FONT_5X7[" "])
            gx = sx0 + ci * char_w
            for ri, rbits in enumerate(glyph):
                for coli in range(5):
                    if rbits & (1 << (4 - coli)):
                        for dy in range(scale):
                            for dx in range(scale):
                                set_px(gx + coli * scale + dx, sy0 + ri * scale + dy, rgb)

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "rect":
            x = int(float(elem.attrib.get("x", "0")))
            y = int(float(elem.attrib.get("y", "0")))
            w = int(float(elem.attrib.get("width", "0")))
            h = int(float(elem.attrib.get("height", "0")))
            fill = elem.attrib.get("fill", "#FFFFFF")
            stroke = elem.attrib.get("stroke", "none")
            frgb = parse_hex(fill)
            srgb = parse_hex(stroke) if stroke != "none" else frgb
            for py in range(max(0, y), min(height, y + h + 1)):
                for px in range(max(0, x), min(width, x + w + 1)):
                    if stroke != "none" and (
                        py in (y, y + 1, y + h - 1, y + h)
                        or px in (x, x + 1, x + w - 1, x + w)
                    ):
                        set_px(px, py, srgb)
                    elif fill != "none":
                        set_px(px, py, frgb)
        elif tag == "line":
            x1 = float(elem.attrib.get("x1", "0"))
            y1 = float(elem.attrib.get("y1", "0"))
            x2 = float(elem.attrib.get("x2", "0"))
            y2 = float(elem.attrib.get("y2", "0"))
            srgb = parse_hex(elem.attrib.get("stroke", "#333333"))
            dashed = "stroke-dasharray" in elem.attrib
            draw_line(x1, y1, x2, y2, srgb, dashed=dashed)
        elif tag == "path":
            d = elem.attrib.get("d", "")
            if "M 0 1" in d:
                continue
            pts = [
                (float(m[0]), float(m[1]))
                for m in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", d)
            ]
            srgb = parse_hex(elem.attrib.get("stroke", "#333333"))
            dashed = "stroke-dasharray" in elem.attrib
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                draw_line(x0, y0, x1, y1, srgb, dashed=dashed)
        elif tag == "text":
            x = float(elem.attrib.get("x", "0"))
            y = float(elem.attrib.get("y", "0"))
            anchor = elem.attrib.get("text-anchor", "start")
            fsize = float(elem.attrib.get("font-size", "11"))
            rgb = parse_hex(elem.attrib.get("fill", "#1B2430"))
            txt = "".join(elem.itertext()).strip()
            if txt:
                draw_text(x, y, txt, rgb, anchor, fsize)

    raw_rows = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw_rows.append(0)
        raw_rows.extend(buf[y * row_bytes : (y + 1) * row_bytes])

    def chunk(ctag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctag
            + data
            + struct.pack(">I", zlib.crc32(ctag + data) & 0xFFFFFFFF)
        )

    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw_rows), 6))
        + chunk(b"IEND", b"")
    )
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(png_bytes)


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return float(values[0]), 0.0
    return float(statistics.mean(values)), float(statistics.stdev(values))


def _fmt_ms(values: list[float], decimals: int = 2) -> str:
    m, s = _mean_std(values)
    return f"{m:.{decimals}f} ± {s:.{decimals}f}"


def merge_cohorts(
    label: str,
    title: str,
    subtitle: str,
    cohorts: list[CohortData],
    color: str = "#2B5C8F",
) -> CohortData:
    classes = {k: 0 for k in OBJECT_TYPES}
    relations = {k: 0 for k in RELATIONS}
    events: Counter[str] = Counter()
    baseline_events = {
        etype: 0
        for family in OBJECT_TYPES
        for etype in BASELINE_EVENTS_BY_FAMILY.get(family, ())
    }
    extension_events: Counter[str] = Counter()
    e2o_roles: Counter[str] = Counter()
    total_objects = 0
    total_events = 0
    total_o2o = 0
    total_e2o = 0
    partial_prompts = False

    for c in cohorts:
        s = c.summary
        total_objects += int(s.get("total_objects", 0))
        total_events += int(s.get("total_events", 0))
        total_o2o += int(s.get("total_o2o", 0))
        total_e2o += int(s.get("total_e2o", 0))
        partial_prompts = partial_prompts or bool(s.get("partial_prompts", False))
        for k, v in s.get("classes", {}).items():
            classes[k] = classes.get(k, 0) + int(v)
        for k, v in s.get("relations", {}).items():
            relations[k] = relations.get(k, 0) + int(v)
        for k, v in s.get("events", {}).items():
            events[k] += int(v)
        for k, v in s.get("baseline_events", {}).items():
            baseline_events[k] = baseline_events.get(k, 0) + int(v)
        for k, v in s.get("extension_events", {}).items():
            extension_events[k] += int(v)
        for k, v in s.get("e2o_roles", {}).items():
            e2o_roles[k] += int(v)

    summary = {
        "total_objects": total_objects,
        "total_events": total_events,
        "total_o2o": total_o2o,
        "total_e2o": total_e2o,
        "classes": classes,
        "relations": relations,
        "events": dict(sorted(events.items())),
        "baseline_events": baseline_events,
        "extension_events": dict(sorted(extension_events.items())),
        "e2o_roles": dict(sorted(e2o_roles.items())),
        "coverage": {
            "classes_covered": sum(1 for v in classes.values() if v > 0),
            "classes_total": len(OBJECT_TYPES),
            "relations_covered": sum(1 for v in relations.values() if v > 0),
            "relations_total": len(RELATIONS),
            "baseline_events_covered": sum(
                1 for v in baseline_events.values() if v > 0
            ),
            "baseline_events_total": len(BASELINE_EVENT_SET),
            "extension_event_types": len(extension_events),
        },
        "capture_completeness": "partial" if partial_prompts else "complete",
        "partial_prompts": partial_prompts,
    }
    totals_line = (
        f"{label}: {total_objects:,} objects / {total_events:,} events · "
        f"{total_o2o:,} O2O links"
    )
    markers = {"Prompt": "*"} if partial_prompts else {}
    return CohortData(
        label=label,
        title=title,
        subtitle=subtitle,
        totals_line=totals_line,
        summary=summary,
        color=color,
        markers=markers,
    )


def extract_execution_outcomes(doc: dict[str, Any]) -> tuple[int, int, int]:
    """Return (succeeded_executions, failed_or_rejected_executions, rejected_requests)."""
    succ = 0
    fail = 0
    rejected_req = 0
    for obj in doc.get("objects", []):
        if obj.get("type") == "Execution":
            outcome = "success"
            for attr in obj.get("attributes", []):
                if attr.get("name") == "payload":
                    try:
                        p = json.loads(attr["value"])
                        outcome = str(p.get("outcome", "success")).lower()
                    except Exception:
                        pass
            if outcome in ("success", "succeeded", "ok", "completed"):
                succ += 1
            else:
                fail += 1
        elif obj.get("type") == "ActionRequest":
            for attr in obj.get("attributes", []):
                if attr.get("name") == "payload":
                    try:
                        p = json.loads(attr["value"])
                        if p.get("rejected"):
                            rejected_req += 1
                    except Exception:
                        pass
    return succ, fail, rejected_req


def discover_ocdfg_from_logs(logs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute Object-Centric Directly-Follows Graph (OC-DFG) from OCEL 2.0 logs."""
    event_freq: Counter[str] = Counter()
    dfg_by_otype: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    total_dfg: Counter[tuple[str, str]] = Counter()
    objects_per_type: Counter[str] = Counter()

    for doc in logs:
        obj_type_map: dict[str, str] = {}
        for obj in doc.get("objects", []):
            oid = obj["id"]
            otype = obj["type"]
            obj_type_map[oid] = otype
            objects_per_type[otype] += 1

        obj_timelines: dict[str, list[str]] = defaultdict(list)
        for ev in doc.get("events", []):
            etype = ev["type"]
            event_freq[etype] += 1
            seen_oids: set[str] = set()
            for rel in ev.get("relationships", []):
                oid = rel["objectId"]
                if oid not in seen_oids:
                    seen_oids.add(oid)
                    obj_timelines[oid].append(etype)

        for oid, seq in obj_timelines.items():
            otype = obj_type_map.get(oid, "Unknown")
            for i in range(len(seq) - 1):
                pair = (seq[i], seq[i + 1])
                dfg_by_otype[otype][pair] += 1
                total_dfg[pair] += 1

    return {
        "n_logs": len(logs),
        "event_frequencies": dict(event_freq),
        "objects_per_type": dict(objects_per_type),
        "dfg_by_object_type": {
            ot: {f"{a} -> {b}": cnt for (a, b), cnt in counter.most_common()}
            for ot, counter in dfg_by_otype.items()
        },
        "total_dfg": {
            f"{a} -> {b}": cnt for (a, b), cnt in total_dfg.most_common()
        },
    }


def extract_semantic_process(
    logs_by_task: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Project LoopOCEL logs onto semantic domain steps, results obtained, failures, and commits."""
    step_counts: Counter[str] = Counter()
    edge_counts: Counter[tuple[str, str]] = Counter()
    discovered_by_kind: Counter[str] = Counter()
    committed_by_kind: Counter[str] = Counter()
    trace_summaries: dict[str, dict[str, Any]] = {}

    for tid, doc in logs_by_task.items():
        objs = {o["id"]: o for o in doc.get("objects", [])}
        obj_payloads: dict[str, Any] = {}
        for oid, o in objs.items():
            for attr in o.get("attributes", []):
                if attr.get("name") in ("payload", "state"):
                    try:
                        obj_payloads[oid] = json.loads(attr["value"])
                    except Exception:
                        pass

        # Map request -> execution and request -> result via O2O relationships
        req_to_exec: dict[str, str] = {}
        exec_to_res: dict[str, str] = {}
        req_targets: dict[str, list[str]] = defaultdict(list)
        for oid, o in objs.items():
            for rel in o.get("relationships", []):
                q = rel.get("qualifier")
                tgt = rel.get("objectId")
                if q == "hasExecution":
                    req_to_exec[oid] = tgt
                elif q == "producesResult":
                    exec_to_res[oid] = tgt
                elif q == "targetsObject":
                    req_targets[oid].append(tgt)

        # Track ProcessObjects registered & mutated (final committed state)
        for oid, o in objs.items():
            if o.get("type") == "ProcessObject" and ":" in oid:
                prefix = oid.split(":", 1)[0]
                discovered_by_kind[prefix] += 1
                st = obj_payloads.get(oid, {})
                if isinstance(st, dict) and (
                    st.get("booking_state") == "selected"
                    or st.get("status") == "confirmed"
                ):
                    committed_by_kind[prefix] += 1

        step_counts["trip_registered"] += 1
        prev_step = "trip_registered"

        # Sort ActionRequests in numerical order
        req_ids = sorted(
            (oid for oid, o in objs.items() if o.get("type") == "ActionRequest"),
            key=lambda x: int(x.split(":")[1]) if ":" in x and x.split(":")[1].isdigit() else 0,
        )

        trace_steps: list[dict[str, Any]] = []
        for rid in req_ids:
            rp = obj_payloads.get(rid, {})
            tname = str(rp.get("name", ""))
            targs = rp.get("arguments") or {}
            eid = req_to_exec.get(rid, "")
            ep = obj_payloads.get(eid, {})
            outcome = str(ep.get("outcome", "success")).lower()
            res_id = exec_to_res.get(eid, "")
            resp = obj_payloads.get(res_id, {})
            rdata = resp.get("data") if isinstance(resp, dict) else {}
            if not isinstance(rdata, dict):
                rdata = {}

            targets = req_targets.get(rid, [])
            target_prefixes = {t.split(":", 1)[0] for t in targets if ":" in t}

            if tname == "search_web":
                q_str = str(targs.get("query", ""))
                if "train" in target_prefixes or "trains" in rdata or "ICE" in q_str:
                    step_key = "search_rail"
                    items = [
                        f"{t.get('train_id')} ({t.get('dep')}->{t.get('arr')}, {int(t.get('price_eur', 0))}EUR)"
                        for t in rdata.get("trains", [])
                        if isinstance(t, dict)
                    ]
                    label = f"Search Rail: {', '.join(items) if items else q_str[:32]}"
                elif "flight" in target_prefixes or "flights" in rdata:
                    step_key = "search_flights"
                    items = [
                        f"{f.get('flight_id')} ({f.get('dep')}->{f.get('arr')}, {int(f.get('price_eur', 0))}EUR)"
                        for f in rdata.get("flights", [])
                        if isinstance(f, dict)
                    ]
                    label = f"Search Flights: {', '.join(items) if items else q_str[:32]}"
                elif "hotel" in target_prefixes or "hotels" in rdata:
                    step_key = "search_hotels"
                    items = [
                        f"{h.get('hotel_id')} ({int(h.get('rate_per_night_eur', 0))}EUR/n)"
                        for h in rdata.get("hotels", [])
                        if isinstance(h, dict)
                    ]
                    label = f"Search Hotels: {', '.join(items) if items else q_str[:32]}"
                else:
                    step_key = "search_flights"
                    label = f"Search Web: {q_str[:36]}"

                step_counts[step_key] += 1
                edge_counts[(prev_step, step_key)] += 1
                res_key = f"{step_key}_results"
                step_counts[res_key] += 1
                edge_counts[(step_key, res_key)] += 1
                prev_step = res_key
                trace_steps.append({"kind": step_key, "outcome": "success", "summary": label})

            elif tname in ("verify_constraints", "check_itinerary"):
                step_counts["verify_constraints"] += 1
                edge_counts[(prev_step, "verify_constraints")] += 1
                if outcome in ("failure", "failed", "error"):
                    step_counts["constraint_failed_rework"] += 1
                    edge_counts[("verify_constraints", "constraint_failed_rework")] += 1
                    prev_step = "constraint_failed_rework"
                    errs = rdata.get("errors", [])
                    short_err = errs[0][:48] if errs else "Constraint violation"
                    trace_steps.append(
                        {
                            "kind": "verify_failed",
                            "outcome": "failure",
                            "summary": f"Verify FAILED: {short_err}",
                        }
                    )
                else:
                    step_counts["constraints_verified"] += 1
                    edge_counts[("verify_constraints", "constraints_verified")] += 1
                    prev_step = "constraints_verified"
                    cost = targs.get("total_cost_eur", "")
                    trace_steps.append(
                        {
                            "kind": "verify_passed",
                            "outcome": "success",
                            "summary": f"Verify PASSED (total {cost} EUR)",
                        }
                    )

            elif tname in ("commit_itinerary", "write_itinerary"):
                step_counts["commit_itinerary"] += 1
                edge_counts[(prev_step, "commit_itinerary")] += 1
                prev_step = "commit_itinerary"
                sel_parts = []
                for k in ("selected_train", "selected_flight", "selected_hotel"):
                    if targs.get(k):
                        sel_parts.append(str(targs[k]))
                cost = targs.get("total_cost_eur", "")
                trace_steps.append(
                    {
                        "kind": "commit",
                        "outcome": "success",
                        "summary": f"Committed: {' + '.join(sel_parts)} ({cost} EUR)",
                    }
                )

        trip_obj = obj_payloads.get(f"trip:{tid}", {})
        trace_summaries[tid] = {
            "task_id": tid,
            "origin": trip_obj.get("origin", ""),
            "destination": trip_obj.get("destination", ""),
            "steps": trace_steps,
        }

    return {
        "n_runs": len(logs_by_task),
        "step_counts": dict(step_counts),
        "edge_counts": {f"{a} -> {b}": c for (a, b), c in edge_counts.items()},
        "discovered_by_kind": dict(discovered_by_kind),
        "committed_by_kind": dict(committed_by_kind),
        "traces": trace_summaries,
    }


def _draw_svg_arrow(
    lines: list[str],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: str,
    stroke_width: float = 2.2,
    dashed: bool = False,
) -> None:
    """Draw a line segment with an explicit polygon arrowhead at (x2, y2)."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    head_len, half_w = 10.0, 4.2
    bx, by = x2 - head_len * ux, y2 - head_len * uy
    dash_attr = ' stroke-dasharray="5,4"' if dashed else ""
    lines.append(
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
        f'stroke="{color}" stroke-width="{stroke_width}"{dash_attr}/>'
    )
    p1 = f"{x2:.1f},{y2:.1f}"
    p2 = f"{bx - half_w * uy:.1f},{by + half_w * ux:.1f}"
    p3 = f"{bx + half_w * uy:.1f},{by - half_w * ux:.1f}"
    lines.append(f'<polygon points="{p1} {p2} {p3}" fill="{color}"/>')


def _draw_svg_polyline_arrow(
    lines: list[str],
    pts: list[tuple[float, float]],
    color: str,
    stroke_width: float = 2.2,
    dashed: bool = False,
) -> None:
    """Draw a multi-point polyline with an explicit polygon arrowhead at the last vertex."""
    if len(pts) < 2:
        return
    tip, prev = pts[-1], pts[-2]
    dx, dy = tip[0] - prev[0], tip[1] - prev[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    head_len, half_w = 10.0, 4.2
    bx, by = tip[0] - head_len * ux, tip[1] - head_len * uy
    path_pts = pts[:-1] + [(bx, by)]
    d = "M " + " L ".join(f"{px:.1f} {py:.1f}" for px, py in path_pts)
    dash_attr = ' stroke-dasharray="6,4"' if dashed else ""
    lines.append(
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{stroke_width}" '
        f'stroke-linejoin="round"{dash_attr}/>'
    )
    p1 = f"{tip[0]:.1f},{tip[1]:.1f}"
    p2 = f"{bx - half_w * uy:.1f},{by + half_w * ux:.1f}"
    p3 = f"{bx + half_w * uy:.1f},{by - half_w * ux:.1f}"
    lines.append(f'<polygon points="{p1} {p2} {p3}" fill="{color}"/>')


def render_ocdfg_svg(
    ocdfg: dict[str, Any],
    tier_metrics: dict[str, Any],
    out_svg: Path,
    out_png: Path,
) -> None:
    """Render View 1: Agent-Loop Control-Flow & Object-Type OC-DFG."""
    ev_freq = ocdfg["event_frequencies"]

    W, H = 1360, 325
    lines: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="#FFFFFF"/>',
    ]

    act_nodes = [
        ("Instruction.Registered", "Instruction.Registered", "", 12, 34, 185, 62, "#2A5B84", "#F0F6FC"),
        ("Turn.Started", "Turn.Started", "", 232, 34, 150, 62, "#2A5B84", "#F0F6FC"),
        ("Prompt.Submitted", "Prompt.Submitted", "", 420, 34, 175, 62, "#2A5B84", "#F0F6FC"),
        ("Response.Received", "Response.Received", "", 632, 34, 178, 62, "#2A5B84", "#F0F6FC"),
        ("ActionRequest.Dispatched", "ActionRequest.Dispatched", "", 850, 34, 215, 62, "#7A4D8B", "#F8F2FC"),
        ("Execution.Started", "Execution.Started", "", 1110, 34, 235, 62, "#7A4D8B", "#F8F2FC"),
        ("ProcessObject.Registered", "ProcessObject.Registered", "Trip, Flight, Hotel", 835, 144, 245, 62, "#B85D19", "#FFF7F0"),
        ("ProcessObject.StateChanged", "ProcessObject.StateChanged", "Trip, Flight, Hotel Deltas", 1105, 144, 245, 62, "#B85D19", "#FFF7F0"),
        ("Execution.Finished", "Execution.Finished", "", 1110, 254, 235, 62, "#7A4D8B", "#F8F2FC"),
        ("ActionResult.Received", "ActionResult.Received", "", 840, 254, 215, 62, "#7A4D8B", "#F8F2FC"),
        ("History.Assembled", "History.Assembled", "", 525, 254, 185, 62, "#2E7D5B", "#EFFAF5"),
        ("Turn.Finished", "Turn.Finished", "", 232, 254, 160, 62, "#2A5B84", "#F0F6FC"),
    ]

    edges = [
        (197, 65, 232, 65, "#2A5B84", f"Instr ({ev_freq.get('Instruction.Registered', 0)})", 214, 22),
        (382, 65, 420, 65, "#2A5B84", f"Turn ({ev_freq.get('Turn.Started', 0)})", 401, 22),
        (595, 65, 632, 65, "#2A5B84", f"Prompt ({ev_freq.get('Prompt.Submitted', 0)})", 613, 22),
        (810, 65, 850, 65, "#7A4D8B", f"Resp ({ev_freq.get('Response.Received', 0)})", 830, 22),
        (1065, 65, 1110, 65, "#7A4D8B", f"ActReq ({ev_freq.get('ActionRequest.Dispatched', 0)})", 1087, 22),
        (957, 96, 957, 144, "#B85D19", f"registers ({ev_freq.get('ProcessObject.Registered', 0)})", 882, 126),
        (1227, 96, 1227, 144, "#B85D19", f"mutates ({ev_freq.get('ProcessObject.StateChanged', 0)})", 1296, 126),
        (1227, 206, 1227, 254, "#7A4D8B", f"Exec ({ev_freq.get('Execution.Finished', 0)})", 1280, 235),
        (1110, 285, 1055, 285, "#7A4D8B", f"Result ({ev_freq.get('ActionResult.Received', 0)})", 1082, 242),
        (840, 285, 710, 285, "#2E7D5B", f"History ({ev_freq.get('History.Assembled', 0)})", 775, 242),
        (525, 285, 392, 285, "#2A5B84", f"Turn ({ev_freq.get('Turn.Finished', 0)})", 458, 242),
    ]
    for x1, y1, x2, y2, col, lbl, lx, ly in edges:
        _draw_svg_arrow(lines, x1, y1, x2, y2, col, stroke_width=2.8)
        lines.append(
            f'<text x="{lx}" y="{ly}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13.5" font-weight="bold" fill="{col}">{escape(lbl)}</text>'
        )

    _draw_svg_polyline_arrow(
        lines,
        [(312, 254), (312, 175), (508, 175), (508, 96)],
        "#2E7D5B",
        stroke_width=2.8,
        dashed=True,
    )
    lines.append(
        '<rect x="235" y="142" width="350" height="26" rx="4" fill="#FFFFFF" fill-opacity="0.95"/>'
    )
    lines.append(
        f'<text x="410" y="160" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13.5" font-weight="bold" fill="#2E7D5B">'
        f"Next-Turn &amp; History Carryover Loop ({ev_freq.get('History.Assembled', 0)} transitions)</text>"
    )

    for nid, label, sublabel, x, y, w, h, stroke, fill in act_nodes:
        cnt = ev_freq.get(nid, 0)
        lines.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" ry="8" fill="{fill}" stroke="{stroke}" stroke-width="2.5"/>'
        )
        if sublabel:
            lines.append(
                f'<text x="{x + w // 2}" y="{y + 20}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13" font-weight="bold" fill="#1B2430">{escape(label)}</text>'
            )
            lines.append(
                f'<text x="{x + w // 2}" y="{y + 36}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="11.5" font-weight="bold" fill="#B85D19">{escape(sublabel)}</text>'
            )
            lines.append(
                f'<text x="{x + w // 2}" y="{y + 53}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="12.5" fill="{stroke}" font-weight="bold">freq = {cnt}</text>'
            )
        else:
            lines.append(
                f'<text x="{x + w // 2}" y="{y + 26}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="14" font-weight="bold" fill="#1B2430">{escape(label)}</text>'
            )
            lines.append(
                f'<text x="{x + w // 2}" y="{y + 48}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13.5" fill="{stroke}" font-weight="bold">freq = {cnt}</text>'
            )

    lines.append("</svg>")
    svg_text = "\n".join(lines)
    out_svg.write_text(svg_text, encoding="utf-8")
    rasterize_svg_to_png(out_svg, out_png)


def render_semantic_process_svg(
    sem: dict[str, Any],
    out_svg: Path,
    out_png: Path,
) -> None:
    """Render View 2: Actual Task / Domain Process Graph & Concrete Agent Execution Traces."""
    sc = sem["step_counts"]
    disc = sem["discovered_by_kind"]
    comm = sem["committed_by_kind"]

    W, H = 1360, 305
    lines: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="#FFFFFF"/>',
    ]

    # Nodes: (x, y, w, h, title, subtitle, count_str, stroke, fill)
    panel_a_nodes = [
        (
            12, 108, 182, 74,
            "1. Trip Specification",
            "Entity: Trip (Budget, Dates)",
            f"trips = {sc.get('trip_registered', 0)}",
            "#2A5B84", "#F0F6FC",
        ),
        (
            234, 14, 212, 72,
            "2a. Search Web: Rail",
            "DB ICE / Intercity queries",
            f"queries = {sc.get('search_rail', 0)}",
            "#2A5B84", "#F0F6FC",
        ),
        (
            234, 109, 212, 72,
            "2b. Search Web: Flights",
            "Direct &amp; hub air queries",
            f"queries = {sc.get('search_flights', 0)}",
            "#2A5B84", "#F0F6FC",
        ),
        (
            234, 204, 212, 72,
            "2c. Search Web: Hotels",
            "Venue proximity queries",
            f"queries = {sc.get('search_hotels', 0)}",
            "#2A5B84", "#F0F6FC",
        ),
        (
            486, 14, 222, 72,
            "3a. Rail Options Obtained",
            "Entity: Train (Times &amp; Fares)",
            f"selected {comm.get('train', 0)} / {disc.get('train', 0)} trains",
            "#7A4D8B", "#F8F2FC",
        ),
        (
            486, 109, 222, 72,
            "3b. Flight Options Obtained",
            "Entity: Flight (Times &amp; Bags)",
            f"selected {comm.get('flight', 0)} / {disc.get('flight', 0)} flights",
            "#7A4D8B", "#F8F2FC",
        ),
        (
            486, 204, 222, 72,
            "3c. Hotel Options Obtained",
            "Entity: Hotel (Rates &amp; Policies)",
            f"selected {comm.get('hotel', 0)} / {disc.get('hotel', 0)} hotels",
            "#7A4D8B", "#F8F2FC",
        ),
        (
            750, 104, 235, 76,
            "4. Verify Trip Constraints",
            "Evaluates [Trip × Flight × Hotel]",
            f"checks = {sc.get('verify_constraints', 0)} (Joint Validation)",
            "#2A5B84", "#F0F6FC",
        ),
        (
            750, 214, 235, 76,
            "4a. Constraint Violation!",
            "Cap / Buffer / Baggage Failed",
            f"failures = {sc.get('constraint_failed_rework', 0)} (Rework)",
            "#B03030", "#FDF2F2",
        ),
        (
            1010, 104, 135, 76,
            "5. Verified OK",
            "All Rules Satisfied",
            f"passed = {sc.get('constraints_verified', 0)}",
            "#2E7D5B", "#EFFAF5",
        ),
        (
            1170, 104, 178, 76,
            "6. Commit Plan",
            "Mutates Trip, Flight, Hotel",
            f"committed = {sc.get('commit_itinerary', 0)}",
            "#B85D19", "#FFF7F0",
        ),
    ]

    pa_edges = [
        (194, 130, 234, 50, "#2A5B84"),
        (194, 145, 234, 145, "#2A5B84"),
        (194, 160, 234, 240, "#2A5B84"),
        (446, 50, 486, 50, "#7A4D8B"),
        (446, 145, 486, 145, "#7A4D8B"),
        (446, 240, 486, 240, "#7A4D8B"),
        (708, 50, 750, 128, "#2A5B84"),
        (708, 145, 750, 145, "#2A5B84"),
        (708, 240, 750, 162, "#2A5B84"),
        (867, 180, 867, 214, "#B03030"),
        (985, 142, 1010, 142, "#2E7D5B"),
        (1145, 142, 1170, 142, "#B85D19"),
    ]
    for x1, y1, x2, y2, col in pa_edges:
        _draw_svg_arrow(lines, x1, y1, x2, y2, col, stroke_width=2.8)

    # Multi-object convergence indicator above node 4
    lines.append(
        '<text x="867" y="97" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="12" font-weight="bold" fill="#2A5B84">Multi-Object Convergence</text>'
    )

    # Rework loop arrow from 4a back to 4
    _draw_svg_polyline_arrow(
        lines,
        [(985, 252), (1014, 252), (1014, 162), (985, 162)],
        "#B03030",
        stroke_width=2.8,
        dashed=True,
    )
    lines.append(
        '<text x="1115" y="244" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13.5" font-weight="bold" fill="#B03030">Rework Loop (10x):</text>'
    )
    lines.append(
        '<text x="1115" y="264" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13" font-weight="bold" fill="#B03030">Select compliant candidate</text>'
    )

    for x, y, w, h, t1, t2, t3, stroke, fill in panel_a_nodes:
        lines.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" ry="8" fill="{fill}" stroke="{stroke}" stroke-width="2.5"/>'
        )
        lines.append(
            f'<text x="{x + w // 2}" y="{y + 23}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="14" font-weight="bold" fill="#1B2430">{t1}</text>'
        )
        sub_sz = "11.5" if len(t2) > 22 else "12.5"
        lines.append(
            f'<text x="{x + w // 2}" y="{y + 44}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="{sub_sz}" fill="#4A5568">{t2}</text>'
        )
        lines.append(
            f'<text x="{x + w // 2}" y="{y + 64}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13" font-weight="bold" fill="{stroke}">{escape(t3)}</text>'
        )

    lines.append("</svg>")
    svg_text = "\n".join(lines)
    out_svg.write_text(svg_text, encoding="utf-8")
    rasterize_svg_to_png(out_svg, out_png)


def run_analysis(sessions_dir: Path | None = None) -> dict[str, Any]:
    sdir_root = sessions_dir or SESSIONS_DIR
    tasks_meta = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    tasks = tasks_meta["tasks"]
    tiers_meta = {t["tier_id"]: t for t in tasks_meta["tiers"]}

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    all_logs: list[dict[str, Any]] = []
    logs_by_task: dict[str, dict[str, Any]] = {}
    all_cohorts: list[CohortData] = []
    cohorts_by_tier: dict[str, list[CohortData]] = defaultdict(list)
    per_task_rows: list[dict[str, Any]] = []

    for t in tasks:
        tid = t["task_id"]
        tier_id = t["tier_id"]
        ocel_path = sdir_root / tid / "loopocel.json"
        if not ocel_path.exists():
            continue

        doc = json.loads(ocel_path.read_text(encoding="utf-8"))
        val = validate_log(doc, ocel_path=ocel_path)
        pm4py_res = pm4py_check(ocel_path, doc)
        summary = summarize_log(doc)
        cohort = cohort_from_log(doc, label=tid)

        all_logs.append(doc)
        logs_by_task[tid] = doc
        all_cohorts.append(cohort)
        cohorts_by_tier[tier_id].append(cohort)

        pobj_ids = {
            o["id"]
            for o in doc.get("objects", [])
            if o.get("type") == "ProcessObject"
        }
        targeted_pobjs: set[str] = set()
        for o in doc.get("objects", []):
            for rel in o.get("relationships", []):
                q = rel.get("qualifier")
                target_id = rel.get("objectId")
                if target_id in pobj_ids and q in (
                    "targetsObject",
                    "actOn",
                    "describesObject",
                ):
                    targeted_pobjs.add(target_id)

        mutated_pobjs: set[str] = set()
        for ev in doc.get("events", []):
            if ev.get("type") == "ProcessObject.StateChanged":
                for rel in ev.get("relationships", []):
                    if rel.get("objectId") in pobj_ids:
                        mutated_pobjs.add(rel["objectId"])

        n_turns = summary["classes"].get("Turn", 0)
        n_exec_succ, n_exec_fail, n_req_rej = extract_execution_outcomes(doc)
        n_changes = summary["events"].get("ProcessObject.StateChanged", 0)
        schema_ok = bool(val["schema"]["passed"])
        reconstruct_ok = bool(val["ontology"]["passed"])

        per_task_rows.append(
            {
                "task_id": tid,
                "tier_id": tier_id,
                "tier_number": t["tier_number"],
                "turns": n_turns,
                "events": summary["total_events"],
                "objects": summary["total_objects"],
                "o2o_links": summary["total_o2o"],
                "exec_succeeded": n_exec_succ,
                "exec_failed": n_exec_fail + n_req_rej,
                "pobj_total": len(pobj_ids),
                "pobj_targeted": len(targeted_pobjs),
                "pobj_mutated": len(mutated_pobjs),
                "change_events": n_changes,
                "rework_loops": n_exec_fail + n_req_rej,
                "schema_ok": schema_ok,
                "reconstruct_ok": reconstruct_ok,
                "pm4py_ok": pm4py_res.get("ocdfg_discovery_passed", False),
            }
        )

    tier_summary: dict[str, Any] = {}
    for tier_id, tmeta in tiers_meta.items():
        rows = [r for r in per_task_rows if r["tier_id"] == tier_id]
        tier_summary[tier_id] = {
            "tier_number": tmeta["tier_number"],
            "label": tmeta["tier_label"],
            "n_runs": len(rows),
            "turns_ms": _fmt_ms([r["turns"] for r in rows]),
            "events_ms": _fmt_ms([r["events"] for r in rows]),
            "exec_succ_ms": _fmt_ms([r["exec_succeeded"] for r in rows]),
            "exec_fail_ms": _fmt_ms([r["exec_failed"] for r in rows]),
            "pobj_targeted_ms": _fmt_ms([r["pobj_targeted"] for r in rows]),
            "pobj_mutated_ms": _fmt_ms([r["pobj_mutated"] for r in rows]),
            "rework_ms": _fmt_ms([r["rework_loops"] for r in rows]),
            "valid_pct": 100.0
            * sum(1 for r in rows if r["schema_ok"] and r["reconstruct_ok"])
            / max(1, len(rows)),
        }

    merged_all = merge_cohorts(
        label=f"Travel (n={len(all_cohorts)})",
        title=f"LoopOCEL — Live Web Travel Planning Cohort (n={len(all_cohorts)} runs)",
        subtitle="Recorded via loopocel-mcp across Tier 1 (Direct), Tier 2 (Constrained), and Tier 3 (Multi-Modal)",
        cohorts=all_cohorts,
        color="#2B5C8F",
    )
    render_figure([merged_all], FIGURES_DIR, stem="fig_loopocel_all15")

    if cohorts_by_tier.get("tier1_direct") and cohorts_by_tier.get(
        "tier3_multimodal"
    ):
        c_t1 = merge_cohorts(
            label="Tier 1 Direct",
            title="Tier 1 · Direct Round-Trip",
            subtitle=f"{len(cohorts_by_tier['tier1_direct'])} live web runs · unconstrained point-to-point flights",
            cohorts=cohorts_by_tier["tier1_direct"],
            color="#2B5C8F",
        )
        c_t3 = merge_cohorts(
            label="Tier 3 Multi-Modal",
            title="Tier 3 · Multi-Modal & Multi-Stop",
            subtitle=f"{len(cohorts_by_tier['tier3_multimodal'])} live web runs · rail+air transfer buffer & multi-hotel",
            cohorts=cohorts_by_tier["tier3_multimodal"],
            color="#2E6B4E",
        )
        render_figure([c_t1, c_t3], FIGURES_DIR, stem="fig_loopocel_tier1_vs_tier3")

    # View 1: Architectural Agent-Loop OC-DFG
    ocdfg = discover_ocdfg_from_logs(all_logs)
    render_ocdfg_svg(
        ocdfg,
        tier_summary,
        out_svg=FIGURES_DIR / "fig_ocdfg_process_graph.svg",
        out_png=FIGURES_DIR / "fig_ocdfg_process_graph.png",
    )

    # View 2: Semantic Task / Actual Process Graph & Concrete Traces
    semantic_proc = extract_semantic_process(logs_by_task)
    render_semantic_process_svg(
        semantic_proc,
        out_svg=FIGURES_DIR / "fig_semantic_process_graph.svg",
        out_png=FIGURES_DIR / "fig_semantic_process_graph.png",
    )

    md_lines = [
        "# Table 1: LoopOCEL Metrics Across Travel Planning Complexity Tiers",
        "",
        "| Complexity Tier | Runs | Turns (Mean ± Std) | Events (Mean ± Std) | Tool Executions Succ (Mean ± Std) | Tool Executions Fail / Rework (Mean ± Std) | `ProcessObject` Targeted (Mean ± Std) | `ProcessObject` Mutated (Mean ± Std) | OCEL 2.0 Valid |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for tid in ("tier1_direct", "tier2_constrained", "tier3_multimodal"):
        ts = tier_summary[tid]
        md_lines.append(
            f"| {ts['label']} | {ts['n_runs']} | {ts['turns_ms']} | {ts['events_ms']} | {ts['exec_succ_ms']} | {ts['exec_fail_ms']} | {ts['pobj_targeted_ms']} | {ts['pobj_mutated_ms']} | {ts['valid_pct']:.0f}% |"
        )

    (RESULTS_DIR / "table1_metrics.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )

    report = {
        "n_tasks": len(per_task_rows),
        "all_valid": all(
            r["schema_ok"] and r["reconstruct_ok"] for r in per_task_rows
        ),
        "tier_summary": tier_summary,
        "cohort_totals": merged_all.summary,
        "ocdfg": ocdfg,
        "semantic_process": semantic_proc,
        "per_task": per_task_rows,
    }
    (RESULTS_DIR / "analysis_summary.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


if __name__ == "__main__":
    res = run_analysis()
    print(
        json.dumps(
            {
                "n_tasks": res["n_tasks"],
                "all_valid": res["all_valid"],
                "tier_summary": res["tier_summary"],
                "semantic_step_counts": res["semantic_process"]["step_counts"],
                "discovered_objects_by_kind": res["semantic_process"]["discovered_by_kind"],
                "committed_objects_by_kind": res["semantic_process"]["committed_by_kind"],
            },
            indent=2,
        )
    )
