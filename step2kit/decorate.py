"""Decoration for a box's top/lid plate: an imported logo (straight-line SVG geometry), a repeating
decorative pattern fill, and/or engraved text, all emitted on the ETCH layer alongside the cut lines so
the part is still cut exactly as before, just marked. Everything here is generated in a local frame
centred on the target part; export.write_dxf places it with the same rotate/translate it already
applies to the part's own outline, so it lands correctly once nested.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from shapely.geometry import LineString
from shapely.geometry import box as shapely_box

PATTERNS = ("diagonal", "crosshatch", "hex")


def _clip(pts, w, h):
    rect = shapely_box(-w / 2, -h / 2, w / 2, h / 2)
    inter = LineString(pts).intersection(rect)
    geoms = inter.geoms if hasattr(inter, "geoms") else [inter]
    return [list(g.coords) for g in geoms if not g.is_empty and g.length > 1e-6]


def _diagonal_lines(w, h, spacing, angle_deg):
    ang = math.radians(angle_deg)
    dx, dy = math.cos(ang), math.sin(ang)
    px, py = -dy, dx
    diag = math.hypot(w, h)
    n = int(diag / spacing) + 2
    lines = []
    for k in range(-n, n + 1):
        cx, cy = px * spacing * k, py * spacing * k
        lines += _clip([(cx - dx * diag, cy - dy * diag), (cx + dx * diag, cy + dy * diag)], w, h)
    return lines


def _hex_grid(w, h, spacing):
    r = spacing / 2
    dx, dy = r * 1.5, r * math.sqrt(3)
    cols, rows = int(w / dx) + 3, int(h / dy) + 3
    lines = []
    for col in range(-cols, cols):
        cx = col * dx
        if abs(cx) > w / 2 + r:
            continue
        for row in range(-rows, rows):
            cy = row * dy + (dy / 2 if col % 2 else 0)
            if abs(cy) > h / 2 + r:
                continue
            pts = [(cx + r * math.cos(math.radians(60 * i)), cy + r * math.sin(math.radians(60 * i))) for i in range(7)]
            lines += _clip(pts, w, h)
    return lines


def pattern_lines(kind, w, h, spacing=8.0):
    """Decorative line-art clipped to a w x h rectangle centred at the origin."""
    if kind == "diagonal":
        return _diagonal_lines(w, h, spacing, 45)
    if kind == "crosshatch":
        return _diagonal_lines(w, h, spacing, 45) + _diagonal_lines(w, h, spacing, -45)
    if kind == "hex":
        return _hex_grid(w, h, spacing)
    raise ValueError(f"unknown pattern '{kind}', choose one of {PATTERNS}")


def _parse_points(s):
    nums = [float(v) for v in re.split(r"[,\s]+", s.strip()) if v]
    return list(zip(nums[0::2], nums[1::2]))


def _parse_path(d):
    """M/L/H/V/Z only, absolute and relative. C/S/Q/T/A curves are linearised straight to their final
    endpoint rather than approximated -- an honest, deterministic simplification (this is a no-AI, no-
    guessing tool) rather than a silently-wrong curve fit; complex logos should be simplified to straight
    segments before import."""
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+(?:[eE]-?\d+)?", d)
    paths, cur = [], []
    x = y = 0.0
    start = (0.0, 0.0)
    i, cmd = 0, None

    def nums(k):
        nonlocal i
        vals = [float(tokens[i + j]) for j in range(k)]
        i += k
        return vals

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd == "m":
                cmd = "l"
            elif cmd == "M":
                cmd = "L"
        if cmd in ("L", "l"):
            dx, dy = nums(2)
            x, y = (x + dx, y + dy) if cmd == "l" else (dx, dy)
            if not cur:
                start = (x, y)
            cur.append((x, y))
        elif cmd in ("H", "h"):
            (dx,) = nums(1)
            x = x + dx if cmd == "h" else dx
            cur.append((x, y))
        elif cmd in ("V", "v"):
            (dy,) = nums(1)
            y = y + dy if cmd == "v" else dy
            cur.append((x, y))
        elif cmd in ("C", "c"):
            vals = nums(6)
            x, y = (x + vals[4], y + vals[5]) if cmd == "c" else (vals[4], vals[5])
            cur.append((x, y))
        elif cmd in ("S", "s", "Q", "q"):
            vals = nums(4)
            x, y = (x + vals[2], y + vals[3]) if cmd.islower() else (vals[2], vals[3])
            cur.append((x, y))
        elif cmd in ("T", "t"):
            vals = nums(2)
            x, y = (x + vals[0], y + vals[1]) if cmd == "t" else (vals[0], vals[1])
            cur.append((x, y))
        elif cmd in ("A", "a"):
            vals = nums(7)
            x, y = (x + vals[5], y + vals[6]) if cmd == "a" else (vals[5], vals[6])
            cur.append((x, y))
        elif cmd in ("Z", "z"):
            if cur:
                cur.append(start)
                paths.append(cur)
            cur = []
        else:
            i += 1
    if cur:
        paths.append(cur)
    return paths


def _fit(raw, target_w, target_h, margin=0.9):
    xs = [p[0] for poly in raw for p in poly]
    ys = [p[1] for poly in raw for p in poly]
    if not xs:
        raise ValueError("no visible content")
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sw, sh = x1 - x0, y1 - y0
    if sw < 1e-6 or sh < 1e-6:
        raise ValueError("SVG content has no visible extent")
    scale = margin * min(target_w / sw, target_h / sh)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # SVG y grows downward; flip to the box's y-up convention while centring on the origin
    return [[((p[0] - cx) * scale, -(p[1] - cy) * scale) for p in poly] for poly in raw]


def svg_lines(path, target_w, target_h):
    """Parse straight-line SVG primitives (line, polyline, polygon, rect, circle, and path M/L/H/V/Z/
    curve-endpoint) and scale/centre them into a target_w x target_h box, y-up, centred at the origin."""
    ns = "{http://www.w3.org/2000/svg}"
    root = ET.parse(path).getroot()
    raw = []
    for el in root.iter():
        tag = el.tag.replace(ns, "")
        if tag == "line":
            raw.append([(float(el.get("x1", 0)), float(el.get("y1", 0))),
                       (float(el.get("x2", 0)), float(el.get("y2", 0)))])
        elif tag in ("polyline", "polygon"):
            pts = _parse_points(el.get("points", ""))
            if tag == "polygon" and pts:
                pts = pts + [pts[0]]
            if pts:
                raw.append(pts)
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            rw, rh = float(el.get("width", 0)), float(el.get("height", 0))
            raw.append([(x, y), (x + rw, y), (x + rw, y + rh), (x, y + rh), (x, y)])
        elif tag == "circle":
            cx, cy, r = float(el.get("cx", 0)), float(el.get("cy", 0)), float(el.get("r", 0))
            raw.append([(cx + r * math.cos(2 * math.pi * k / 32), cy + r * math.sin(2 * math.pi * k / 32))
                       for k in range(33)])
        elif tag == "path":
            raw += _parse_path(el.get("d", ""))
    if not raw:
        raise ValueError("no line-based shapes found in this SVG (only line/polyline/polygon/rect/circle/"
                         "path are read; styling-only or curve-only content is not imported)")
    return _fit(raw, target_w, target_h)
