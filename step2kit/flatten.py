"""Flatten jointed plates to 2D outlines, apply kerf compensation, check minimum feature width."""
from __future__ import annotations

from dataclasses import dataclass, field

from build123d import GeomType, Plane, Vector
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

from .geometry import BIG, box_local, inter, solids_of, volume


@dataclass
class FlatPart:
    name: str
    plate: object
    poly: Polygon                 # nominal outline (mm, plate frame), holes included
    cut: Polygon = None           # kerf-compensated outline
    thin: list = field(default_factory=list)   # (area, (cx, cy)) of regions narrower than min feature
    pieces: int = 1
    notes: list = field(default_factory=list)

    @property
    def T(self):
        return self.plate.T

    def bbox(self):
        return self.cut.bounds if self.cut is not None else self.poly.bounds


def _edge_segments(wire, chord=0.05):
    import math
    segs = []
    for e in wire.edges():
        if e.geom_type == GeomType.LINE:
            segs.append([e.start_point(), e.end_point()])
        else:
            r = None
            try:
                r = e.radius
            except Exception:
                pass
            if r:
                step = 2 * math.sqrt(max(2 * r * chord, 1e-6))
                nseg = max(8, int(e.length / step) + 1)
            else:
                nseg = max(8, int(e.length / 0.5))
            segs.append([e.position_at(k / nseg) for k in range(nseg + 1)])
    return segs


def _stitch(segs, tol):
    pts = list(segs[0])
    used = {0}
    while len(used) < len(segs):
        last = pts[-1]
        best = None
        for k, sp in enumerate(segs):
            if k in used:
                continue
            if (sp[0] - last).length < tol:
                best = (k, sp[1:])
                break
            if (sp[-1] - last).length < tol:
                best = (k, sp[::-1][1:])
                break
        if best is None:
            return None
        used.add(best[0])
        pts += best[1]
    if (pts[-1] - pts[0]).length < tol:
        pts.pop()
    return pts


def _wire_points(wire, chord=0.05):
    """Ordered (x, y) points of a closed planar wire; curves are sampled to a chord tolerance.
    Booleans occasionally leave a wire with a sub-micron-to-a-few-micron gap between two edges that
    should meet (most often on the many small, near-tangent joint cuts a low-poly faceted curve
    produces); the endpoint-matching tolerance is widened in steps before giving up, so a real
    topology problem still raises rather than silently producing a wrong outline."""
    segs = _edge_segments(wire, chord)
    if not segs:
        raise RuntimeError("open wire while flattening (no edges)")
    for tol in (1e-3, 0.01, 0.05):
        pts = _stitch(segs, tol)
        if pts is not None:
            return [(round(p.X, 4), round(p.Y, 4)) for p in pts]
    raise RuntimeError("open wire while flattening")


def face_from_polygon(poly: Polygon):
    """build123d Face from a shapely polygon (CCW exterior, CW holes -> normal +Z)."""
    from shapely.geometry.polygon import orient
    from build123d import Face, Wire
    poly = orient(poly, sign=1.0)
    outer = Wire.make_polygon([Vector(x, y, 0) for x, y in list(poly.exterior.coords)[:-1]], close=True)
    holes = [Wire.make_polygon([Vector(x, y, 0) for x, y in list(h.coords)[:-1]], close=True) for h in poly.interiors]
    return Face(outer, holes)


def rebuild_prismatic(plates, log=print, tol=0.005):
    """Every plate must be a prism perpendicular to its surface. Check sections near both faces against the
    mid-plane section, then replace the boolean-built solid by a clean extrusion of the mid-plane outline
    (this also removes any topology damage left by the many box booleans)."""
    from build123d import extrude
    results = {}
    for p in plates:
        secs = {}
        try:
            for name, z in (("lo", 0.3), ("mid", p.T / 2), ("hi", p.T - 0.3)):
                polys = flatten_plate(p, z=z)
                secs[name] = unary_union(polys) if polys else None
        except Exception as ex:
            results[p.name] = {"ok": False, "error": f"could not section this plate to check it: {ex}"}
            log(f"  {p.name}: skipped prismatic check/rebuild ({ex})")
            continue
        mid = secs["mid"]
        if mid is None or mid.is_empty:
            results[p.name] = {"ok": False, "error": "no mid-plane section"}
            continue
        worst = 0.0
        for name in ("lo", "hi"):
            g = secs[name]
            d = mid.symmetric_difference(g).area if g is not None else mid.area
            worst = max(worst, d / mid.area)
        ok = worst < tol
        results[p.name] = {"ok": ok, "section_mismatch": round(worst, 5)}
        if not ok:
            log(f"  {p.name}: NOT PRISMATIC, faces differ from the mid-plane by {100*worst:.2f}% of the area")
        polys = [g for g in (mid.geoms if hasattr(mid, "geoms") else [mid]) if g.area > 1.0]
        polys.sort(key=lambda g: -g.area)
        p.raw_solid = p.solid
        solid = None
        for g in polys[:1]:
            prism = extrude(face_from_polygon(g), amount=p.T, dir=Vector(0, 0, 1))
            solid = prism if solid is None else solid.fuse(prism)
        if solid is not None:
            p.solid = solid.moved(p.plane.location)
    return results


def flatten_plate(plate, solid=None, z=None):
    """Section of the plate solid in its own frame at height z (default mid-plane) -> shapely Polygon(s)."""
    solid = solid if solid is not None else plate.solid
    local = plate.local(solid)
    z = plate.T / 2 if z is None else z
    slab = box_local(-BIG, BIG, -BIG, BIG, z - 0.005, z + 0.005, Plane.XY)
    sec = inter(local, slab)
    faces = [f for f in sec.faces() if abs(f.normal_at().Z - 1) < 1e-6] if sec is not None else []
    faces.sort(key=lambda f: -f.area)
    polys = []
    for f in faces:
        outer = _wire_points(f.outer_wire())
        holes = [_wire_points(w) for w in f.inner_wires()]
        poly = Polygon(outer, holes)
        if not poly.is_valid:
            poly = poly.buffer(0)
        polys.append(poly)
    return polys


def flatten_all(plates, kerf=0.0, min_feature=1.0, log=print):
    parts = []
    for p in plates:
        try:
            polys = flatten_plate(p)
        except Exception as ex:
            log(f"  {p.name}: SKIPPED, could not flatten ({ex})")
            p.notes.append(f"WARNING: could not flatten this plate to a 2D outline ({ex}); it is missing from the kit")
            continue
        if not polys:
            log(f"  {p.name}: nothing to flatten")
            continue
        poly = polys[0]
        fp = FlatPart(p.name, p, poly)
        if len(polys) > 1:
            fp.pieces = len(polys)
            fp.notes.append(f"plate has {len(polys)} separate pieces; only the largest is exported")
        # kerf: the beam removes kerf/2 on each side of the path -> grow outer, shrink holes
        if kerf > 0:
            fp.cut = poly.buffer(kerf / 2, join_style="mitre", mitre_limit=5.0)
            if isinstance(fp.cut, MultiPolygon):
                fp.cut = max(fp.cut.geoms, key=lambda g: g.area)
        else:
            fp.cut = poly
        # thin features: opening by min_feature removes anything narrower than it
        opened = poly.buffer(-min_feature / 2, join_style="mitre").buffer(min_feature / 2, join_style="mitre")
        diff = poly.difference(opened)
        for g in getattr(diff, "geoms", [diff]):
            if g.area > 0.2:
                c = g.centroid
                fp.thin.append((round(g.area, 2), (round(c.x, 1), round(c.y, 1))))
        parts.append(fp)
        log(f"  {p.name}: {poly.bounds[2]-poly.bounds[0]:.1f} x {poly.bounds[3]-poly.bounds[1]:.1f} mm, "
            f"{len(poly.exterior.coords)-1} pts, {len(poly.interiors)} holes"
            + (f", {len(fp.thin)} thin spots" if fp.thin else "")
            + (f", {fp.pieces} PIECES" if fp.pieces > 1 else ""))
    return parts
