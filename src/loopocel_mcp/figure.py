"""Render LoopOCEL ontology coverage and instance count figures (SVG and PNG).

Supports single-session and multi-cohort comparisons using the nine-class,
sixteen-relation, nine-event-family layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .convert import BASELINE_EVENTS_BY_FAMILY, BASELINE_EVENT_SET, OBJECT_TYPES, RELATIONS, summarize_log


PALETTE = {
    "ink": "#272727",
    "line": "#30363D",
    "muted": "#8C959F",
    "uncaptured": "#B0B8C1",
    "uncaptured_fill": "#F6F8FA",
    "cohort_0": "#2B5C8F",
    "cohort_1": "#2E6B4E",
    "cohort_2": "#8C4A27",
    "both": "#22252A",
    "node_fill_covered": "#EBF0F5",
    "node_fill_partial": "#F4F6F8",
    "hex_fill": "#F8FAFC",
    "hex_stroke": "#9BA4B0",
    "focal_dot": "#8C959F",
}

NODES: dict[str, tuple[float, float, float]] = {
    "Turn": (100, 640, 150),
    "Prompt": (470, 360, 180),
    "Response": (470, 920, 180),
    "Instruction": (470, 640, 180),
    "History": (470, 80, 160),
    "ActionRequest": (1040, 920, 210),
    "Execution": (1100, 640, 190),
    "ActionResult": (1040, 360, 210),
    "ProcessObject": (1590, 640, 210),
}

ROUTES: dict[str, tuple[str, float, float, str]] = {
    "hasPrompt": ("M175 625 C270 625 280 360 380 360", 275, 535, "center"),
    "hasResponse": ("M175 655 C270 655 280 920 380 920", 275, 815, "center"),
    "hasNextTurn": ("M25 625 C-80 520 -80 750 25 655", -55, 705, "center"),
    "hasInstruction": ("M470 392 V608", 488, 515, "left"),
    "hasHistory": ("M560 350 H650 V80 H550", 665, 180, "left"),
    "hasActionResult": ("M560 360 H935", 748, 342, "center"),
    "hasActionRequest": ("M560 920 H935", 748, 902, "center"),
    "hasExecution": ("M1100 888 V672", 1118, 745, "left"),
    "targetsObject": ("M1145 920 H1590 V672", 1430, 902, "center"),
    "actOn": ("M1195 640 H1485", 1340, 622, "center"),
    "producesResult": ("M1100 608 V392", 1118, 515, "left"),
    "resultOf": ("M965 392 V888", 947, 640, "right"),
    "describesObject": ("M1145 360 H1840 V640 H1695", 1480, 342, "center"),
    "hasSourcePrompt": ("M390 90 H290 V310 H410 V328", 275, 190, "right"),
    "hasSourceResponse": ("M390 70 H-150 V1140 H280 V934 H380", 95, 1125, "center"),
    "hasSourceActionResult": ("M550 70 H840 V300 H990 V328", 855, 180, "left"),
}

# (center_x, attach_y, width, direction: "up" | "down", connector_svg_path)
EVENT_BOX_ANCHORS: dict[str, tuple[float, float, float, str, str]] = {
    "Turn": (100, 478, 290, "up", "M100 478 V608"),
    "Prompt": (470, 277, 300, "up", "M470 277 V328"),
    "Response": (470, 1013, 330, "down", "M470 1013 V952"),
    "Instruction": (470, 723, 340, "down", "M470 723 V672"),
    "History": (470, 7, 310, "up", "M470 7 V48"),
    "ActionRequest": (1040, 1018, 390, "down", "M1040 1018 V952"),
    "Execution": (1400, 778, 310, "down", "M1400 778 V710 H1175 V672"),
    "ActionResult": (1040, 277, 360, "up", "M1040 277 V328"),
    "ProcessObject": (1590, 512, 430, "up", "M1590 512 V608"),
}


@dataclass
class CohortData:
    """Summarized counts and metadata for one trace or cohort."""

    label: str
    title: str
    subtitle: str
    totals_line: str
    summary: dict[str, Any]
    color: str = "#2B5C8F"
    markers: dict[str, str] = field(default_factory=dict)


def cohort_from_log(
    log: dict[str, Any],
    *,
    label: str = "Run",
    title: str | None = None,
    subtitle: str | None = None,
    meta: dict[str, Any] | None = None,
    color: str = "#2B5C8F",
) -> CohortData:
    summary = summarize_log(log, meta=meta)
    session_meta = meta or summary.get("session", {})
    sid = session_meta.get("session_id") or label
    task = session_meta.get("task") or "Agent trace"
    mode = session_meta.get("mode") or "recorded"
    profile = session_meta.get("profile") or "generic"
    completeness = summary.get("capture_completeness", "complete")
    resolved_title = title or f"Session · {sid}"
    resolved_subtitle = subtitle or f"Task: {task} · Profile: {profile} · Mode: {mode} ({completeness} capture)"
    totals_line = f"{label}: {summary['total_objects']:,} objects / {summary['total_events']:,} events · {summary['total_o2o']:,} O2O links"
    markers: dict[str, str] = {}
    if summary.get("partial_prompts"):
        markers["Prompt"] = "*"
    return CohortData(
        label=label,
        title=resolved_title,
        subtitle=resolved_subtitle,
        totals_line=totals_line,
        summary=summary,
        color=color,
        markers=markers,
    )


def _format_coverage_header(summary: dict[str, Any]) -> str:
    cov = summary["coverage"]
    ext = cov["extension_event_types"]
    ext_suffix = f" (+{ext} event extension{'s' if ext != 1 else ''})" if ext else ""
    return (
        f"Classes {cov['classes_covered']}/{cov['classes_total']} · "
        f"O2O {cov['relations_covered']}/{cov['relations_total']} · "
        f"Baseline events {cov['baseline_events_covered']}/{cov['baseline_events_total']}"
        f"{ext_suffix}"
    )


def _parse_svg_vertices(path_str: str) -> list[tuple[float, float]]:
    tokens = re.findall(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", path_str)
    vertices: list[tuple[float, float]] = []
    x = y = 0.0
    i = 0
    while i < len(tokens):
        cmd = tokens[i]
        i += 1
        count = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6}[cmd]
        vals = list(map(float, tokens[i : i + count]))
        i += count
        if cmd == "C":
            vertices.append((vals[0], vals[1]))
            vertices.append((vals[2], vals[3]))
            x, y = vals[4], vals[5]
            vertices.append((x, y))
        elif cmd in {"M", "L"}:
            x, y = vals[0], vals[1]
            vertices.append((x, y))
        elif cmd == "H":
            x = vals[0]
            vertices.append((x, y))
        elif cmd == "V":
            y = vals[0]
            vertices.append((x, y))
    return vertices


def _element_stroke_and_fill(counts: list[int], cohorts: list[CohortData], is_node: bool = False) -> tuple[str, str]:
    nonzero = [idx for idx, c in enumerate(counts) if c > 0]
    if not nonzero:
        return PALETTE["uncaptured"], PALETTE["uncaptured_fill"] if is_node else "#FFFFFF"
    if len(cohorts) == 1:
        return PALETTE["both"], PALETTE["node_fill_covered"] if is_node else "#FFFFFF"
    if len(nonzero) == len(cohorts) or (len(cohorts) >= 2 and nonzero[0] == 0 and nonzero[-1] == len(cohorts) - 1):
        return PALETTE["both"], PALETTE["node_fill_covered"] if is_node else "#FFFFFF"
    first = cohorts[nonzero[0]]
    return first.color, PALETTE["node_fill_covered"] if is_node else "#FFFFFF"


def _counts_tspan_svg(counts: list[int], cohorts: list[CohortData]) -> str:
    parts: list[str] = []
    for idx, val in enumerate(counts):
        if idx > 0:
            parts.append('<tspan fill="#8C959F"> | </tspan>')
        color = cohorts[idx].color if val > 0 else "#8C959F"
        parts.append(f'<tspan fill="{color}" font-weight="600">{val:,}</tspan>')
    return "".join(parts)


def _family_for_event(event_type: str) -> str:
    prefix = event_type.split(".", 1)[0]
    return prefix if prefix in NODES else "ProcessObject"


def _events_for_family(family: str, cohorts: list[CohortData]) -> list[tuple[str, str, list[int]]]:
    baseline = list(BASELINE_EVENTS_BY_FAMILY.get(family, ()))
    seen_extensions: list[str] = []
    for cohort in cohorts:
        for etype in cohort.summary.get("extension_events", {}):
            if _family_for_event(etype) == family and etype not in baseline and etype not in seen_extensions:
                seen_extensions.append(etype)

    rows: list[tuple[str, str, list[int]]] = []
    for etype in baseline + seen_extensions:
        is_ext = etype not in BASELINE_EVENT_SET
        marker = ""
        for cohort in cohorts:
            if etype in cohort.markers:
                marker = f" {cohort.markers[etype]}"
                break
        ext_tag = " [ext]" if is_ext else ""
        display = f"{etype}{marker}{ext_tag}"
        counts = [int(c.summary.get("events", {}).get(etype, 0)) for c in cohorts]
        rows.append((etype, display, counts))
    return rows


def render_svg(cohorts: list[CohortData]) -> str:
    """Build a standalone SVG string for the LoopOCEL ontology layout and count annotations."""
    if not cohorts:
        raise ValueError("At least one cohort is required to render a figure")

    cohort_palette = ["#2B5C8F", "#2E6B4E", "#8C4A27", "#6E40AA"]
    for idx, cohort in enumerate(cohorts):
        if not cohort.color:
            cohort.color = cohort_palette[idx % len(cohort_palette)]

    view_x, view_y, view_w, view_h = -220, -220, 2140, 1520
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_x} {view_y} {view_w} {view_h}" '
        f'width="1605" height="1140" font-family="Arial, Helvetica, DejaVu Sans, sans-serif">',
        f'<rect x="{view_x}" y="{view_y}" width="{view_w}" height="{view_h}" fill="#FFFFFF"/>',
    ]

    # 1. Header block per cohort (up to 3 columns across the top)
    col_x = [-150, 960, 1480] if len(cohorts) <= 2 else [-150, 560, 1270]
    for idx, cohort in enumerate(cohorts[:3]):
        hx = col_x[idx] if idx < len(col_x) else -150 + idx * 650
        lines.append(
            f'<text x="{hx}" y="-172" font-size="16" font-weight="700" fill="#1F2328">'
            f'{escape(cohort.title)}</text>'
        )
        lines.append(
            f'<text x="{hx}" y="-145" font-size="13.5" fill="#1F2328">'
            f'{escape(_format_coverage_header(cohort.summary))}</text>'
        )
        lines.append(
            f'<text x="{hx}" y="-121" font-size="12" fill="#57606A">'
            f'{escape(cohort.subtitle)}</text>'
        )
        lines.append(
            f'<text x="{hx}" y="-97" font-size="13.5" fill="#1F2328">'
            f'{escape(cohort.totals_line)}</text>'
        )

    # 2. O2O relation routes and labels
    for rel_name, (route, lx, ly, align) in ROUTES.items():
        counts = [int(c.summary["relations"].get(rel_name, 0)) for c in cohorts]
        stroke_color, _ = _element_stroke_and_fill(counts, cohorts, is_node=False)
        dashed = rel_name.startswith("hasSource") or rel_name == "hasNextTurn"
        dash_attr = ' stroke-dasharray="6,4.5"' if dashed else ""

        verts = _parse_svg_vertices(route)
        tip, prev = verts[-1], verts[-2]
        dx, dy = tip[0] - prev[0], tip[1] - prev[1]
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        head_len, half_w = 12.0, 4.5
        base_x, base_y = tip[0] - head_len * ux, tip[1] - head_len * uy

        lines.append(
            f'<path d="{route}" fill="none" stroke="{stroke_color}" '
            f'stroke-width="1.65" stroke-linejoin="round"{dash_attr}/>'
        )
        lines.append(
            f'<line x1="{tip[0] - 18 * ux:.1f}" y1="{tip[1] - 18 * uy:.1f}" '
            f'x2="{base_x:.1f}" y2="{base_y:.1f}" stroke="{stroke_color}" stroke-width="1.65"/>'
        )
        p1 = f"{tip[0]:.1f},{tip[1]:.1f}"
        p2 = f"{base_x - half_w * uy:.1f},{base_y + half_w * ux:.1f}"
        p3 = f"{base_x + half_w * uy:.1f},{base_y - half_w * ux:.1f}"
        lines.append(f'<polygon points="{p1} {p2} {p3}" fill="{stroke_color}"/>')

        anchor = {"left": "start", "right": "end", "center": "middle"}[align]
        marker = ""
        for cohort in cohorts:
            if rel_name in cohort.markers:
                marker = f" {cohort.markers[rel_name]}"
                break
        label_title = f"{rel_name}{marker}"
        count_str_plain = " | ".join(f"{c:,}" for c in counts)
        box_w = max(len(label_title), len(count_str_plain)) * 6.8 + 10
        if align == "center":
            bx = lx - box_w / 2
        elif align == "left":
            bx = lx - 4
        else:
            bx = lx - box_w + 4
        lines.append(
            f'<rect x="{bx:.1f}" y="{ly - 15:.1f}" width="{box_w:.1f}" height="32" '
            f'rx="3" fill="#FFFFFF" fill-opacity="0.92"/>'
        )
        lines.append(
            f'<text x="{lx}" y="{ly - 2}" text-anchor="{anchor}" font-size="11.5" '
            f'font-weight="500" fill="{stroke_color}">{escape(label_title)}</text>'
        )
        lines.append(
            f'<text x="{lx}" y="{ly + 13}" text-anchor="{anchor}" font-size="11">'
            f'{_counts_tspan_svg(counts, cohorts)}</text>'
        )

    # 3. Event family hexagons and focal dotted connectors
    for family, (cx, attach_y, w, direction, route) in EVENT_BOX_ANCHORS.items():
        event_rows = _events_for_family(family, cohorts)
        row_h = 33.0
        h = max(56.0, len(event_rows) * row_h + 16.0)
        cy = attach_y - h / 2.0 if direction == "up" else attach_y + h / 2.0

        # Focal dotted line + small arrowhead
        verts = _parse_svg_vertices(route)
        tip, prev = verts[-1], verts[-2]
        dx, dy = tip[0] - prev[0], tip[1] - prev[1]
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        head_len, half_w = 8.5, 3.2
        base_x, base_y = tip[0] - head_len * ux, tip[1] - head_len * uy

        lines.append(
            f'<path d="{route}" fill="none" stroke="{PALETTE["focal_dot"]}" '
            f'stroke-width="1.2" stroke-dasharray="3.5,3"/>'
        )
        p1 = f"{tip[0]:.1f},{tip[1]:.1f}"
        p2 = f"{base_x - half_w * uy:.1f},{base_y + half_w * ux:.1f}"
        p3 = f"{base_x + half_w * uy:.1f},{base_y - half_w * ux:.1f}"
        lines.append(f'<polygon points="{p1} {p2} {p3}" fill="{PALETTE["focal_dot"]}"/>')

        # Hexagon polygon
        pts = [
            (cx - w / 2 + 16, cy - h / 2),
            (cx + w / 2 - 16, cy - h / 2),
            (cx + w / 2, cy),
            (cx + w / 2 - 16, cy + h / 2),
            (cx - w / 2 + 16, cy + h / 2),
            (cx - w / 2, cy),
        ]
        pts_str = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        lines.append(
            f'<polygon points="{pts_str}" fill="{PALETTE["hex_fill"]}" '
            f'stroke="{PALETTE["hex_stroke"]}" stroke-width="1.2"/>'
        )

        top_y = cy - (len(event_rows) * row_h) / 2.0 + 13.0
        for r_idx, (_, display_name, e_counts) in enumerate(event_rows):
            ey = top_y + r_idx * row_h
            e_color, _ = _element_stroke_and_fill(e_counts, cohorts, is_node=False)
            lines.append(
                f'<text x="{cx}" y="{ey:.1f}" text-anchor="middle" font-size="11" '
                f'font-weight="500" fill="{e_color}">{escape(display_name)}</text>'
            )
            lines.append(
                f'<text x="{cx}" y="{ey + 14.0:.1f}" text-anchor="middle" font-size="10.5">'
                f'{_counts_tspan_svg(e_counts, cohorts)}</text>'
            )

    # 4. Nine ontology class rounded boxes
    for cls_name, (cx, cy, w) in NODES.items():
        h = 64.0
        x0, y0 = cx - w / 2.0, cy - h / 2.0
        counts = [int(c.summary["classes"].get(cls_name, 0)) for c in cohorts]
        stroke_color, fill_color = _element_stroke_and_fill(counts, cohorts, is_node=True)

        marker = ""
        for cohort in cohorts:
            if cls_name in cohort.markers:
                marker = cohort.markers[cls_name]
                break
        display_cls = f"{cls_name}{marker}"

        lines.append(
            f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="8" ry="8" fill="{fill_color}" stroke="{stroke_color}" stroke-width="2.1"/>'
        )
        lines.append(
            f'<text x="{cx}" y="{cy - 5:.1f}" text-anchor="middle" font-size="15.5" '
            f'font-weight="700" fill="{stroke_color}">{escape(display_cls)}</text>'
        )
        lines.append(
            f'<text x="{cx}" y="{cy + 16:.1f}" text-anchor="middle" font-size="11.5">'
            f'{_counts_tspan_svg(counts, cohorts)}</text>'
        )

    # 5. Footer legend and count order explanation
    fy = 1185
    if len(cohorts) == 1:
        legend_items = [
            (PALETTE["both"], "Captured in session (> 0 instances)"),
            (PALETTE["uncaptured"], "Uncaptured (0 instances in this session)"),
        ]
    else:
        legend_items = [(PALETTE["both"], "Multiple cohorts")] + [
            (c.color, f"{c.label} only") for c in cohorts[:3]
        ]

    lx_cursor = -150
    for l_color, l_text in legend_items:
        lines.append(
            f'<line x1="{lx_cursor}" y1="{fy}" x2="{lx_cursor + 38}" y2="{fy}" '
            f'stroke="{l_color}" stroke-width="3.2"/>'
        )
        lines.append(
            f'<text x="{lx_cursor + 48}" y="{fy + 4}" font-size="12" font-weight="600" '
            f'fill="{l_color}">{escape(l_text)}</text>'
        )
        lx_cursor += 460

    order_parts: list[str] = []
    for idx, c in enumerate(cohorts):
        if idx > 0:
            order_parts.append('<tspan fill="#8C959F"> | </tspan>')
        order_parts.append(f'<tspan fill="{c.color}" font-weight="600">{escape(c.label)}</tspan>')

    lines.append(
        f'<text x="-150" y="{fy + 32}" font-size="11.5" fill="#272727">'
        f'Count order: {"".join(order_parts)}&#160;&#160;&#160;&#160;'
        f'Exact object / event / O2O instance counts. 0 = no recorded instance.</text>'
    )
    lines.append(
        f'<text x="-150" y="{fy + 54}" font-size="11.5" fill="#272727">'
        f'* Partial Prompt capture. [ext] Outside the 14 baseline event types. '
        f'Uncaptured does not mean nonexistent.</text>'
    )
    lines.append(
        f'<text x="-150" y="{fy + 76}" font-size="11.5" fill="#272727">'
        f'Rounded boxes: object classes. Hexagons: event types. Gray dotted links: focal E2O only. '
        f'Name / edge colors show coverage; count colors show cohorts.</text>'
    )

    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _render_matplotlib(cohorts: list[CohortData], output_path: Path, fmt: str = "png") -> bool:
    """Render using Matplotlib when available."""
    if importlib.util.find_spec("matplotlib") is None:
        return False

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.path import Path as MplPath
    from matplotlib.patches import FancyBboxPatch, PathPatch, Polygon

    def mpl_svg_path(data: str) -> MplPath:
        tokens = re.findall(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", data)
        vertices: list[tuple[float, float]] = []
        codes: list[int] = []
        x = y = 0.0
        i = 0
        while i < len(tokens):
            cmd = tokens[i]
            i += 1
            count = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6}[cmd]
            vals = list(map(float, tokens[i : i + count]))
            i += count
            if cmd == "C":
                vertices.extend(zip(vals[::2], vals[1::2]))
                codes.extend([MplPath.CURVE4] * 3)
                x, y = vals[-2], vals[-1]
            else:
                if cmd in {"M", "L"}:
                    x, y = vals[0], vals[1]
                elif cmd == "H":
                    x = vals[0]
                else:
                    y = vals[0]
                vertices.append((x, y))
                codes.append(MplPath.MOVETO if cmd == "M" else MplPath.LINETO)
        return MplPath(vertices, codes)

    plt.rcParams.update(
        {
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )
    fig, ax = plt.subplots(figsize=(20, 14))
    ax.set(xlim=(-220, 1920), ylim=(1300, -220), aspect="equal")
    ax.set_axis_off()

    col_x = [-150, 960, 1480] if len(cohorts) <= 2 else [-150, 560, 1270]
    for idx, cohort in enumerate(cohorts[:3]):
        hx = col_x[idx] if idx < len(col_x) else -150 + idx * 650
        ax.text(hx, -172, cohort.title, fontsize=13, fontweight="bold", color="#1F2328", va="center")
        ax.text(hx, -145, _format_coverage_header(cohort.summary), fontsize=10.5, color="#1F2328", va="center")
        ax.text(hx, -121, cohort.subtitle, fontsize=9.5, color="#57606A", va="center")
        ax.text(hx, -97, cohort.totals_line, fontsize=10.5, color="#1F2328", va="center")

    def draw_arrow(route_str: str, color: str, dash: tuple[float, float] | None = None, head_scale: float = 1.0) -> None:
        path = mpl_svg_path(route_str)
        ax.add_patch(
            PathPatch(
                path,
                fill=False,
                edgecolor=color,
                linewidth=1.5 * head_scale,
                linestyle=(0, dash) if dash else "solid",
                capstyle="butt",
                joinstyle="round",
                zorder=1,
            )
        )
        tip, previous = path.vertices[-1], path.vertices[-2]
        dx, dy = tip - previous
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        head_length, half_width = 12 * head_scale, 4.5 * head_scale
        base_x, base_y = tip[0] - head_length * ux, tip[1] - head_length * uy
        ax.plot(
            [tip[0] - 18 * head_scale * ux, base_x],
            [tip[1] - 18 * head_scale * uy, base_y],
            color=color,
            linewidth=1.5 * head_scale,
            solid_capstyle="butt",
            zorder=2,
        )
        ax.add_patch(
            Polygon(
                [
                    tip,
                    (base_x - half_width * uy, base_y + half_width * ux),
                    (base_x + half_width * uy, base_y - half_width * ux),
                ],
                closed=True,
                facecolor=color,
                edgecolor=color,
                linewidth=0.4,
                zorder=4,
            )
        )

    for rel_name, (route, lx, ly, align) in ROUTES.items():
        counts = [int(c.summary["relations"].get(rel_name, 0)) for c in cohorts]
        stroke_color, _ = _element_stroke_and_fill(counts, cohorts, is_node=False)
        dashed = rel_name.startswith("hasSource") or rel_name == "hasNextTurn"
        draw_arrow(route, stroke_color, dash=(4, 3) if dashed else None)
        marker = next((c.markers[rel_name] for c in cohorts if rel_name in c.markers), "")
        count_text = " | ".join(f"{c:,}" for c in counts)
        ax.text(
            lx,
            ly,
            f"{rel_name}{' ' + marker if marker else ''}\n{count_text}",
            fontsize=9.5,
            color=stroke_color,
            ha=align,
            va="center",
            linespacing=1.35,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.2, "alpha": 0.92},
            zorder=5,
        )

    for family, (cx, attach_y, w, direction, route) in EVENT_BOX_ANCHORS.items():
        event_rows = _events_for_family(family, cohorts)
        row_h = 33.0
        h = max(56.0, len(event_rows) * row_h + 16.0)
        cy = attach_y - h / 2.0 if direction == "up" else attach_y + h / 2.0
        draw_arrow(route, PALETTE["focal_dot"], dash=(3, 2.5), head_scale=0.75)
        pts = [
            (cx - w / 2 + 16, cy - h / 2),
            (cx + w / 2 - 16, cy - h / 2),
            (cx + w / 2, cy),
            (cx + w / 2 - 16, cy + h / 2),
            (cx - w / 2 + 16, cy + h / 2),
            (cx - w / 2, cy),
        ]
        ax.add_patch(
            Polygon(
                pts,
                closed=True,
                facecolor=PALETTE["hex_fill"],
                edgecolor=PALETTE["hex_stroke"],
                linewidth=1.2,
                zorder=3,
            )
        )
        top_y = cy - (len(event_rows) * row_h) / 2.0 + 11.0
        for r_idx, (_, display_name, e_counts) in enumerate(event_rows):
            ey = top_y + r_idx * row_h
            e_color, _ = _element_stroke_and_fill(e_counts, cohorts, is_node=False)
            ax.text(cx, ey, display_name, fontsize=9, color=e_color, ha="center", va="center", zorder=5)
            ax.text(
                cx,
                ey + 13.5,
                " | ".join(f"{c:,}" for c in e_counts),
                fontsize=8.5,
                color=cohorts[0].color if any(e_counts) else PALETTE["muted"],
                ha="center",
                va="center",
                zorder=5,
            )

    for cls_name, (cx, cy, w) in NODES.items():
        h = 64.0
        counts = [int(c.summary["classes"].get(cls_name, 0)) for c in cohorts]
        stroke_color, fill_color = _element_stroke_and_fill(counts, cohorts, is_node=True)
        marker = next((c.markers[cls_name] for c in cohorts if cls_name in c.markers), "")
        ax.add_patch(
            FancyBboxPatch(
                (cx - w / 2, cy - h / 2),
                w,
                h,
                boxstyle="round,pad=0,rounding_size=8",
                facecolor=fill_color,
                edgecolor=stroke_color,
                linewidth=1.8,
                zorder=3,
            )
        )
        ax.text(
            cx,
            cy - 8,
            f"{cls_name}{marker}",
            fontsize=12.5,
            fontweight="bold",
            color=stroke_color,
            ha="center",
            va="center",
            zorder=5,
        )
        ax.text(
            cx,
            cy + 14,
            " | ".join(f"{c:,}" for c in counts),
            fontsize=9.5,
            color=cohorts[0].color if any(counts) else PALETTE["muted"],
            ha="center",
            va="center",
            zorder=5,
        )

    fy = 1190
    order_str = " | ".join(c.label for c in cohorts)
    ax.text(
        -150,
        fy,
        f"Count order: {order_str}    Exact object / event / O2O instance counts. 0 = no recorded instance.",
        fontsize=9.5,
        color=PALETTE["ink"],
        va="center",
    )
    ax.text(
        -150,
        fy + 24,
        "* Partial Prompt capture. [ext] Outside the 14 baseline event types. Uncaptured does not mean nonexistent.",
        fontsize=9.5,
        color=PALETTE["ink"],
        va="center",
    )
    ax.text(
        -150,
        fy + 48,
        "Rounded boxes: object classes. Hexagons: event types. Gray dotted links: focal E2O only. Name / edge colors show coverage; count colors show cohorts.",
        fontsize=9.5,
        color=PALETTE["ink"],
        va="center",
    )

    fig.tight_layout(pad=0.4)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format=fmt, dpi=220, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)
    return True


def _render_pure_python_png(cohorts: list[CohortData], png_path: Path) -> bool:
    """Standard-library-only PNG rasterizer fallback with built-in bitmap text."""
    import struct
    import zlib

    width, height = 1605, 1140
    bg = (255, 255, 255)
    buf = bytearray(bg * (width * height))

    font_5x7: dict[str, tuple[int, ...]] = {
        "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
        "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
        "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
        "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
        "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
        "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
        "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
        "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
        "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
        "J": (0x07, 0x02, 0x02, 0x02, 0x12, 0x12, 0x0C),
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
        "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
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
        " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
    }

    def to_px(x: float, y: float) -> tuple[int, int]:
        px = int((x - (-220.0)) / 2140.0 * width)
        py = int((y - (-220.0)) / 1520.0 * height)
        return max(0, min(width - 1, px)), max(0, min(height - 1, py))

    def parse_hex(color_hex: str) -> tuple[int, int, int]:
        h = color_hex.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def set_px(px: int, py: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= px < width and 0 <= py < height:
            idx = (py * width + px) * 3
            buf[idx : idx + 3] = bytes(rgb)

    def draw_text(x: float, y: float, text_str: str, color: str, align: str = "center", scale: int = 1) -> None:
        px, py = to_px(x, y)
        rgb = parse_hex(color)
        clean = text_str.replace("·", "-").replace("†", "+")
        char_w = 6 * scale
        total_w = len(clean) * char_w
        if align == "center":
            start_x = px - total_w // 2
        elif align == "right":
            start_x = px - total_w
        else:
            start_x = px
        start_y = py - (7 * scale) // 2
        for c_idx, ch in enumerate(clean):
            glyph = font_5x7.get(ch.upper(), font_5x7[" "])
            gx = start_x + c_idx * char_w
            for row_i, row_bits in enumerate(glyph):
                for col_i in range(5):
                    if row_bits & (1 << (4 - col_i)):
                        for sy in range(scale):
                            for sx in range(scale):
                                set_px(gx + col_i * scale + sx, start_y + row_i * scale + sy, rgb)

    def draw_rect(x0: float, y0: float, w: float, h: float, fill: str, stroke: str) -> None:
        px0, py0 = to_px(x0, y0)
        px1, py1 = to_px(x0 + w, y0 + h)
        frgb, srgb = parse_hex(fill), parse_hex(stroke)
        for py in range(py0, py1 + 1):
            for px in range(px0, px1 + 1):
                if py in (py0, py0 + 1, py1 - 1, py1) or px in (px0, px0 + 1, px1 - 1, px1):
                    set_px(px, py, srgb)
                else:
                    set_px(px, py, frgb)

    def draw_line(x0: float, y0: float, x1: float, y1: float, stroke: str, dashed: bool = False) -> None:
        px0, py0 = to_px(x0, y0)
        px1, py1 = to_px(x1, y1)
        srgb = parse_hex(stroke)
        steps = max(abs(px1 - px0), abs(py1 - py0), 1)
        for s in range(steps + 1):
            if dashed and (s // 5) % 2 == 1:
                continue
            px = int(px0 + (px1 - px0) * s / steps)
            py = int(py0 + (py1 - py0) * s / steps)
            set_px(px, py, srgb)
            set_px(px + 1, py, srgb)

    col_x = [-150, 960, 1480] if len(cohorts) <= 2 else [-150, 560, 1270]
    for idx, cohort in enumerate(cohorts[:3]):
        hx = col_x[idx] if idx < len(col_x) else -150 + idx * 650
        draw_text(hx, -172, cohort.title, "#1F2328", align="left", scale=2)
        draw_text(hx, -145, _format_coverage_header(cohort.summary), "#1F2328", align="left", scale=1)
        draw_text(hx, -121, cohort.subtitle, "#57606A", align="left", scale=1)
        draw_text(hx, -97, cohort.totals_line, "#1F2328", align="left", scale=1)

    for rel_name, (route, lx, ly, align) in ROUTES.items():
        counts = [int(c.summary["relations"].get(rel_name, 0)) for c in cohorts]
        stroke_color, _ = _element_stroke_and_fill(counts, cohorts, is_node=False)
        dashed = rel_name.startswith("hasSource") or rel_name == "hasNextTurn"
        verts = _parse_svg_vertices(route)
        for (x0, y0), (x1, y1) in zip(verts, verts[1:]):
            draw_line(x0, y0, x1, y1, stroke_color, dashed=dashed)
        draw_text(lx, ly - 5, rel_name, stroke_color, align=align, scale=1)
        draw_text(lx, ly + 10, " | ".join(f"{c:,}" for c in counts), cohorts[0].color if any(counts) else "#8C959F", align=align, scale=1)

    for family, (cx, attach_y, w, direction, route) in EVENT_BOX_ANCHORS.items():
        event_rows = _events_for_family(family, cohorts)
        row_h = 33.0
        h = max(56.0, len(event_rows) * row_h + 16.0)
        cy = attach_y - h / 2.0 if direction == "up" else attach_y + h / 2.0
        verts = _parse_svg_vertices(route)
        for (x0, y0), (x1, y1) in zip(verts, verts[1:]):
            draw_line(x0, y0, x1, y1, PALETTE["focal_dot"], dashed=True)
        draw_rect(cx - w / 2, cy - h / 2, w, h, PALETTE["hex_fill"], PALETTE["hex_stroke"])
        top_y = cy - (len(event_rows) * row_h) / 2.0 + 11.0
        for r_idx, (_, display_name, e_counts) in enumerate(event_rows):
            ey = top_y + r_idx * row_h
            e_color, _ = _element_stroke_and_fill(e_counts, cohorts, is_node=False)
            draw_text(cx, ey, display_name, e_color, align="center", scale=1)
            draw_text(cx, ey + 13.5, " | ".join(f"{c:,}" for c in e_counts), cohorts[0].color if any(e_counts) else "#8C959F", align="center", scale=1)

    for cls_name, (cx, cy, w) in NODES.items():
        counts = [int(c.summary["classes"].get(cls_name, 0)) for c in cohorts]
        stroke_color, fill_color = _element_stroke_and_fill(counts, cohorts, is_node=True)
        marker = next((c.markers[cls_name] for c in cohorts if cls_name in c.markers), "")
        draw_rect(cx - w / 2, cy - 32.0, w, 64.0, fill_color, stroke_color)
        draw_text(cx, cy - 8, f"{cls_name}{marker}", stroke_color, align="center", scale=2)
        draw_text(cx, cy + 15, " | ".join(f"{c:,}" for c in counts), cohorts[0].color if any(counts) else "#8C959F", align="center", scale=1)

    draw_text(-150, 1215, "COUNT ORDER: " + " | ".join(c.label for c in cohorts) + "   EXACT OBJECT / EVENT / O2O INSTANCE COUNTS.", "#272727", align="left", scale=1)
    draw_text(-150, 1240, "* PARTIAL PROMPT CAPTURE. [EXT] OUTSIDE 14 BASELINE EVENT TYPES.", "#272727", align="left", scale=1)

    raw_rows = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw_rows.append(0)
        raw_rows.extend(buf[y * row_bytes : (y + 1) * row_bytes])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw_rows), 6))
        + chunk(b"IEND", b"")
    )
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(png_bytes)
    return True


def _svg_to_png_fallback(svg_path: Path, png_path: Path, cohorts: list[CohortData] | None = None) -> bool:
    """Convert an SVG file to PNG using system tools or pure-Python fallback."""
    png_path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("sips"):
        res = subprocess.run(
            ["sips", "-Z", "3200", "-s", "format", "png", str(svg_path), "--out", str(png_path)],
            capture_output=True,
            timeout=25,
        )
        if res.returncode == 0 and png_path.is_file():
            return True

    for binary, cmd_builder in (
        ("rsvg-convert", lambda s, p: ["rsvg-convert", "-o", str(p), str(s)]),
        ("magick", lambda s, p: ["magick", str(s), str(p)]),
    ):
        if shutil.which(binary):
            res = subprocess.run(cmd_builder(svg_path, png_path), capture_output=True, timeout=20)
            if res.returncode == 0 and png_path.is_file():
                return True

    if shutil.which("qlmanage"):
        with tempfile.TemporaryDirectory() as tmp:
            res = subprocess.run(
                ["qlmanage", "-t", "-s", "1600", "-o", tmp, str(svg_path)],
                capture_output=True,
                timeout=20,
            )
            candidate = Path(tmp) / (svg_path.name + ".png")
            if res.returncode == 0 and candidate.is_file():
                shutil.copy2(candidate, png_path)
                return True

    if cohorts is not None:
        return _render_pure_python_png(cohorts, png_path)
    return False


def render_figure(
    cohorts: list[CohortData],
    output_dir: Path | str,
    *,
    stem: str = "figure",
    formats: tuple[str, ...] = ("svg", "png"),
) -> dict[str, Any]:
    """Render LoopOCEL coverage figure to SVG and/or PNG."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_text = render_svg(cohorts)
    svg_path = output_dir / f"{stem}.svg"
    svg_path.write_text(svg_text, encoding="utf-8")

    outputs: dict[str, str] = {"svg": str(svg_path.resolve())}
    if "png" in formats:
        png_path = output_dir / f"{stem}.png"
        if _render_matplotlib(cohorts, png_path, fmt="png") or _svg_to_png_fallback(
            svg_path, png_path, cohorts=cohorts
        ):
            outputs["png"] = str(png_path.resolve())
    return {
        "outputs": outputs,
        "cohorts": [
            {
                "label": c.label,
                "title": c.title,
                "coverage": c.summary["coverage"],
                "total_objects": c.summary["total_objects"],
                "total_events": c.summary["total_events"],
                "total_o2o": c.summary["total_o2o"],
            }
            for c in cohorts
        ],
    }


def render_session_figure(
    session_dir: Path | str,
    *,
    compare_dirs: list[Path | str] | None = None,
    formats: tuple[str, ...] = ("svg", "png"),
) -> dict[str, Any]:
    """Render figure for a session directory, optionally comparing with other sessions."""
    session_dir = Path(session_dir)
    ocel_path = session_dir / "loopocel.json"
    if not ocel_path.is_file():
        from .convert import convert
        log = convert(session_dir)
    else:
        log = json.loads(ocel_path.read_text(encoding="utf-8"))

    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {"session_id": session_dir.name}

    palette = ["#2B5C8F", "#2E6B4E", "#8C4A27"]
    cohorts = [
        cohort_from_log(
            log,
            label=meta.get("session_id", session_dir.name),
            meta=meta,
            color=palette[0],
        )
    ]
    for idx, other in enumerate(compare_dirs or (), 1):
        other_dir = Path(other)
        other_ocel = other_dir / "loopocel.json"
        if not other_ocel.is_file():
            from .convert import convert
            other_log = convert(other_dir)
        else:
            other_log = json.loads(other_ocel.read_text(encoding="utf-8"))
        other_meta_path = other_dir / "session.json"
        other_meta = (
            json.loads(other_meta_path.read_text(encoding="utf-8"))
            if other_meta_path.is_file()
            else {"session_id": other_dir.name}
        )
        cohorts.append(
            cohort_from_log(
                other_log,
                label=other_meta.get("session_id", other_dir.name),
                meta=other_meta,
                color=palette[idx % len(palette)],
            )
        )

    return render_figure(cohorts, session_dir, stem="figure", formats=formats)
