#!/usr/bin/env python3
"""Rasterise the SignalGate mark into the bitmap the console draws.

Dev-only and stdlib-only. Give it the path to the mark SVG (a filled shape,
two stroked polylines and a dot, all in one accent colour) and it writes
``playground/tui/logo_art.py`` with a half-block bitmap and an ASCII
fallback. The SVG path is a command-line argument; neither it nor any other
filesystem path is written into the generated module.

    python scripts/render_logo.py /path/to/mark.svg
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

# Bitmap width in terminal columns; height follows the SVG's own aspect
# ratio. Kept inside the 18-22 column range the console expects.
TARGET_COLS = 20

BEZIER_STEPS = 24  # line segments per cubic curve when flattening

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "playground" / "tui" / "logo_art.py"

NUMBER_RE = re.compile(r"[-+]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?")

Point = tuple[float, float]
# a, b, c, d, e, f in SVG's matrix(a b c d e f) order: x' = a*x + c*y + e
Matrix = tuple[float, float, float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass
class Shape:
    """One drawable piece of the mark: a filled region or a stroked line."""

    filled: bool
    stroke_width: float
    subpaths: list[list[Point]]


def matmul(m1: Matrix, m2: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply_matrix(m: Matrix, point: Point) -> Point:
    a, b, c, d, e, f = m
    x, y = point
    return (a * x + c * y + e, b * x + d * y + f)


def parse_transform(value: str | None) -> Matrix:
    """Compose ``translate()``/``scale()``/``matrix()`` functions, left to right."""
    if not value:
        return IDENTITY
    total = IDENTITY
    for name, args in re.findall(r"(\w+)\(([^)]*)\)", value):
        nums = [float(n) for n in NUMBER_RE.findall(args)]
        if name == "translate":
            tx = nums[0]
            ty = nums[1] if len(nums) > 1 else 0.0
            step: Matrix = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif name == "scale":
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "matrix":
            step = tuple(nums)  # type: ignore[assignment]
        else:
            raise ValueError(f"unsupported transform function: {name!r}")
        total = matmul(total, step)
    return total


def tokenize_path(d: str) -> list[tuple[str, float | str]]:
    tokens: list[tuple[str, float | str]] = []
    i, n = 0, len(d)
    while i < n:
        ch = d[i]
        if ch.isalpha():
            tokens.append(("cmd", ch))
            i += 1
        elif ch in " \t\r\n,":
            i += 1
        else:
            match = NUMBER_RE.match(d, i)
            if not match:
                raise ValueError(f"unreadable path data near {d[i : i + 12]!r}")
            tokens.append(("num", float(match.group())))
            i = match.end()
    return tokens


def cubic_bezier(
    p0: Point, p1: Point, p2: Point, p3: Point, steps: int = BEZIER_STEPS
) -> list[Point]:
    points = []
    for step in range(1, steps + 1):
        t = step / steps
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def parse_path_d(d: str) -> list[list[Point]]:
    """Flatten M/L/C/Z path data (absolute or relative) into polylines."""
    tokens = tokenize_path(d)
    subpaths: list[list[Point]] = []
    current: list[Point] = []
    pos = start = (0.0, 0.0)
    cmd = ""
    i = 0

    def nums(count: int) -> list[float]:
        nonlocal i
        values = []
        for _ in range(count):
            kind, value = tokens[i]
            if kind != "num":
                raise ValueError(f"expected a number, found command {value!r}")
            values.append(float(value))
            i += 1
        return values

    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "cmd":
            cmd = str(value)
            i += 1
        letter = cmd.upper()
        relative = cmd.islower()
        if letter == "M":
            x, y = nums(2)
            if relative:
                x, y = pos[0] + x, pos[1] + y
            if current:
                subpaths.append(current)
            current = [(x, y)]
            pos = start = (x, y)
            cmd = "l" if relative else "L"  # a bare pair after M is a lineto
        elif letter == "L":
            x, y = nums(2)
            if relative:
                x, y = pos[0] + x, pos[1] + y
            current.append((x, y))
            pos = (x, y)
        elif letter == "C":
            x1, y1, x2, y2, x, y = nums(6)
            if relative:
                x1, y1 = pos[0] + x1, pos[1] + y1
                x2, y2 = pos[0] + x2, pos[1] + y2
                x, y = pos[0] + x, pos[1] + y
            current.extend(cubic_bezier(pos, (x1, y1), (x2, y2), (x, y)))
            pos = (x, y)
        elif letter == "Z":
            if current and current[0] != pos:
                current.append(current[0])
            pos = start
        else:
            raise ValueError(f"unsupported path command: {cmd!r}")
    if current:
        subpaths.append(current)
    return subpaths


def load_shapes(svg_path: Path) -> list[Shape]:
    root = ET.parse(svg_path).getroot()
    shapes: list[Shape] = []
    for elem in root.iter():
        if not elem.tag.endswith("path"):
            continue
        d = elem.get("d")
        if not d:
            continue
        matrix = parse_transform(elem.get("transform"))
        subpaths = [[apply_matrix(matrix, p) for p in sub] for sub in parse_path_d(d)]
        stroke = elem.get("stroke", "")
        fill = elem.get("fill", "")
        if stroke and stroke != "none":
            width = float(elem.get("stroke-width", "1"))
            shapes.append(Shape(filled=False, stroke_width=width, subpaths=subpaths))
        elif fill and fill != "none":
            shapes.append(Shape(filled=True, stroke_width=0.0, subpaths=subpaths))
    return shapes


def read_view_box(svg_path: Path) -> tuple[float, float, float, float]:
    root = ET.parse(svg_path).getroot()
    box = root.get("viewBox")
    if not box:
        raise ValueError("the SVG has no viewBox")
    minx, miny, width, height = (float(v) for v in box.split())
    return minx, miny, width, height


def point_in_polygon(x: float, y: float, polygon: list[Point]) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_at_y = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_at_y:
                inside = not inside
    return inside


def point_near_polyline(x: float, y: float, polyline: list[Point], radius: float) -> bool:
    for (x1, y1), (x2, y2) in pairwise(polyline):
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        t = (
            0.0
            if length_sq == 0
            else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length_sq))
        )
        px, py = x1 + t * dx, y1 + t * dy
        if (px - x) ** 2 + (py - y) ** 2 <= radius * radius:
            return True
    return False


def rasterise(
    shapes: list[Shape], view_box: tuple[float, float, float, float], cols: int
) -> list[list[bool]]:
    minx, miny, width, height = view_box
    rows = max(2, round(cols * height / width))
    rows += rows % 2  # an even count pairs up cleanly into half-block rows
    grid = [[False] * cols for _ in range(rows)]
    for row in range(rows):
        y = miny + (row + 0.5) / rows * height
        for col in range(cols):
            x = minx + (col + 0.5) / cols * width
            on = False
            for shape in shapes:
                if shape.filled:
                    on = any(point_in_polygon(x, y, sub) for sub in shape.subpaths)
                else:
                    radius = shape.stroke_width / 2
                    on = any(point_near_polyline(x, y, sub, radius) for sub in shape.subpaths)
                if on:
                    break
            grid[row][col] = on
    return grid


HALF_BLOCKS = {
    (False, False): " ",
    (True, False): "▀",  # upper half block
    (False, True): "▄",  # lower half block
    (True, True): "█",  # full block
}


def to_halfblock_rows(grid: list[list[bool]]) -> tuple[str, ...]:
    return tuple(
        "".join(HALF_BLOCKS[(t, b)] for t, b in zip(top, bottom))
        for top, bottom in zip(grid[0::2], grid[1::2])
    )


def to_ascii_rows(grid: list[list[bool]]) -> tuple[str, ...]:
    return tuple(
        "".join("#" if (t or b) else " " for t, b in zip(top, bottom))
        for top, bottom in zip(grid[0::2], grid[1::2])
    )


def render_module(halfblock: tuple[str, ...], ascii_rows: tuple[str, ...]) -> str:
    def block(rows: tuple[str, ...]) -> str:
        lines = "\n".join(f"    {row!r}," for row in rows)
        return f"(\n{lines}\n)"

    return (
        '"""The SignalGate mark, generated by scripts/render_logo.py.\n\n'
        "Do not edit by hand: re-run the script against the mark SVG to refresh it.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "# Half-block bitmap: one string per terminal row, two pixel rows each.\n"
        f"BITMAP: tuple[str, ...] = {block(halfblock)}\n\n"
        "# ASCII fallback for non-UTF-8 locales.\n"
        f"BITMAP_ASCII: tuple[str, ...] = {block(ascii_rows)}\n\n"
        '__all__ = ["BITMAP", "BITMAP_ASCII"]\n'
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: render_logo.py <mark.svg>", file=sys.stderr)
        return 2
    svg_path = Path(argv[1])
    view_box = read_view_box(svg_path)
    shapes = load_shapes(svg_path)
    grid = rasterise(shapes, view_box, TARGET_COLS)
    halfblock = to_halfblock_rows(grid)
    ascii_rows = to_ascii_rows(grid)
    OUTPUT_PATH.write_text(render_module(halfblock, ascii_rows))
    print(f"wrote {OUTPUT_PATH} ({len(halfblock)} rows x {TARGET_COLS} cols)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
