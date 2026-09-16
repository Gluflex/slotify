"""Small geometric helpers shared by the pipeline. Pure OpenCascade via build123d; no ML anywhere."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from build123d import Align, Box, Compound, Location, Plane, ShapeList, Vector, GeomType

BIG = 5000.0          # "infinite" extent for slab / cut boxes, mm
TOL = 1e-3            # coincidence tolerance, mm
VOL_TOL = 0.5         # ignore solids/overlaps smaller than this, mm3


def vec(x, y, z) -> Vector:
    return Vector(float(x), float(y), float(z))


def unit(v: Vector) -> Vector:
    n = v.length
    if n < 1e-12:
        raise ValueError("zero vector")
    return v / n


def perp_axis(n: Vector) -> Vector:
    """A deterministic unit vector perpendicular to n."""
    cands = [Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1)]
    a = min(cands, key=lambda c: abs(c.dot(n)))
    return unit(n.cross(a))


def box_local(x0, x1, y0, y1, z0, z1, plane: Plane):
    """Axis-aligned box in the local coordinates of `plane`, returned in world coordinates."""
    if x1 <= x0 or y1 <= y0 or z1 <= z0:
        raise ValueError(f"degenerate box {(x0, x1, y0, y1, z0, z1)}")
    b = Box(x1 - x0, y1 - y0, z1 - z0, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((x0, y0, z0)))
    return b.moved(plane.location)


def volume(s) -> float:
    if s is None:
        return 0.0
    try:
        return float(s.volume)
    except Exception:
        return 0.0


def solids_of(s):
    if s is None:
        return []
    try:
        return [x for x in s.solids() if x.volume > VOL_TOL]
    except Exception:
        return []


def biggest_solid(s):
    sols = solids_of(s)
    return max(sols, key=lambda x: x.volume) if sols else None


def extent_along(shape, origin: Vector, direction: Vector):
    """(min, max) of (v - origin)·direction over the shape's vertices."""
    lo, hi = math.inf, -math.inf
    for v in shape.vertices():
        t = (Vector(v.X, v.Y, v.Z) - origin).dot(direction)
        lo, hi = min(lo, t), max(hi, t)
    return lo, hi


def merge_intervals(ivs, gap=0.0):
    ivs = sorted([list(i) for i in ivs if i[1] > i[0]])
    out = []
    for a, b in ivs:
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def intersect_intervals(a, b):
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi > lo:
                out.append((lo, hi))
    return merge_intervals(out)


def segment(lo: float, hi: float, w: float, w_min: float):
    """Split [lo, hi] into an odd number of ~w wide segments, centred, margins merged into the end segments.
    Returns (A, B): A = odd-index segments (piercer tabs), B = even-index segments incl. margins (owner).
    A bar shorter than 3*w_min gets a single centred tab (A) with owner margins if it is at least w_min long,
    and no joint at all below w_min."""
    length = hi - lo
    if length < w_min - 1e-6:
        return [], [(lo, hi)]
    n = int(length // w)
    if n % 2 == 0:
        n -= 1
    if n < 3:
        # single tab: as wide as w if it fits, otherwise the whole bar
        tw = min(w, length)
        m = (length - tw) / 2
        A = [(lo + m, lo + m + tw)]
        B = []
        if m > 1e-6:
            B = [(lo, lo + m), (hi - m, hi)]
        return A, B
    m = (length - n * w) / 2
    A, B = [], []
    for i in range(n):
        a, b = lo + m + i * w, lo + m + (i + 1) * w
        (A if i % 2 else B).append([a, b])
    B[0][0] = lo
    B[-1][1] = hi
    return [tuple(x) for x in A], [tuple(x) for x in B]


def planar_faces(shape):
    return [f for f in shape.faces() if f.geom_type == GeomType.PLANE]


def face_normal(f) -> Vector:
    return unit(f.normal_at())


def face_offset(f, n: Vector) -> float:
    c = f.center()
    return Vector(c.X, c.Y, c.Z).dot(n)


def fmt_vec(v: Vector) -> str:
    return f"({v.X:+.3f}, {v.Y:+.3f}, {v.Z:+.3f})"


def axis_name(v: Vector) -> str:
    """Human name for a direction: +X, -Z, or a rounded vector for oblique ones."""
    for name, ax in (("X", Vector(1, 0, 0)), ("Y", Vector(0, 1, 0)), ("Z", Vector(0, 0, 1))):
        d = v.dot(ax)
        if abs(abs(d) - 1) < 1e-3:
            return ("+" if d > 0 else "-") + name
    return f"({v.X:.2f}, {v.Y:.2f}, {v.Z:.2f})"


def norm_shape(x):
    """build123d booleans may return a Shape, a ShapeList or None; normalise to Shape or None."""
    if x is None:
        return None
    if isinstance(x, ShapeList):
        x = [s for s in x if s is not None]
        if not x:
            return None
        return x[0] if len(x) == 1 else Compound(x)
    return x


def inter(a, b):
    if a is None or b is None:
        return None
    return norm_shape(a.intersect(b))


def fuse(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return norm_shape(a.fuse(b))


def cut(a, b):
    if a is None or b is None:
        return a
    return norm_shape(a.cut(b))
