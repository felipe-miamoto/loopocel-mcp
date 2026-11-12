"""SVG to PNG rasterization with real, anti-aliased fonts.

Tries vector renderers in order of fidelity and returns False when none is
available, so callers keep the SVG instead of producing a degraded bitmap.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


def svg_size(svg_text: str) -> tuple[int, int]:
    """Return the (width, height) declared on the root <svg> element."""
    head = svg_text[:2000]
    w = re.search(r'\bwidth="([\d.]+)', head)
    h = re.search(r'\bheight="([\d.]+)', head)
    if w and h:
        return int(float(w.group(1))), int(float(h.group(1)))
    vb = re.search(r'viewBox="[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)"', head)
    if vb:
        return int(float(vb.group(1))), int(float(vb.group(2)))
    return 1600, 1000


def _ok(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _cairosvg(svg_path: Path, png_path: Path, scale: float) -> bool:
    if importlib.util.find_spec("cairosvg") is None:
        return False
    try:
        import cairosvg

        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), scale=scale, background_color="white")
    except Exception:
        return False
    return _ok(png_path)


def _run(cmd: list[str], png_path: Path, timeout: int = 30) -> bool:
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 and _ok(png_path)


def _chrome(svg_path: Path, png_path: Path, scale: float, size: tuple[int, int]) -> bool:
    binary = next(
        (p for p in CHROME_PATHS if (Path(p).is_file() if p.startswith("/") else shutil.which(p))),
        None,
    )
    if binary is None:
        return False
    w, h = size
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        f"html,body{{margin:0;padding:0;width:{w}px;height:{h}px;overflow:hidden;background:#fff}}"
        f"svg{{display:block;width:{w}px;height:{h}px}}</style></head><body>"
        f"{svg_path.read_text(encoding='utf-8')}</body></html>"
    )
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        return _run(
            [
                binary if binary.startswith("/") else shutil.which(binary) or binary,
                "--headless=new",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--force-device-scale-factor={scale}",
                f"--window-size={w},{h}",
                f"--screenshot={png_path}",
                page.resolve().as_uri(),
            ],
            png_path,
            timeout=40,
        )


def _qlmanage(svg_path: Path, png_path: Path, longest: int) -> bool:
    if not shutil.which("qlmanage"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        try:
            res = subprocess.run(
                ["qlmanage", "-t", "-s", str(longest), "-o", tmp, str(svg_path)],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        candidate = Path(tmp) / (svg_path.name + ".png")
        if res.returncode == 0 and candidate.is_file():
            shutil.copy2(candidate, png_path)
            return True
    return False


def svg_to_png(svg_path: Path | str, png_path: Path | str, *, scale: float = 2.0) -> bool:
    """Rasterize an SVG file at `scale`x its declared size using a vector renderer."""
    svg_path, png_path = Path(svg_path), Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    size = svg_size(svg_path.read_text(encoding="utf-8"))
    longest = int(max(size) * scale)

    if _cairosvg(svg_path, png_path, scale):
        return True
    if shutil.which("rsvg-convert") and _run(
        ["rsvg-convert", "-z", str(scale), "-b", "white", "-o", str(png_path), str(svg_path)],
        png_path,
    ):
        return True
    if shutil.which("sips") and _run(
        ["sips", "-Z", str(longest), "-s", "format", "png", str(svg_path), "--out", str(png_path)],
        png_path,
    ):
        return True
    if _chrome(svg_path, png_path, scale, size):
        return True
    if shutil.which("magick") and _run(
        ["magick", "-density", str(int(96 * scale)), str(svg_path), str(png_path)], png_path
    ):
        return True
    return _qlmanage(svg_path, png_path, longest)
