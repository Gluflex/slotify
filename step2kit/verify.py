"""Independent checks: rebuild every plate from its flat outline, and find an assembly order by
disassembling the kit one plate at a time with exact swept-volume collision tests."""
from __future__ import annotations

import math

import numpy as np
from build123d import Compound, Face, Location, Plane, Vector, Wire, extrude
from shapely.affinity import translate
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .geometry import axis_name, cut, fuse, inter, norm_shape, solids_of, unit, volume


from .flatten import face_from_polygon  # noqa: E402


def reconstruction_check(parts, log=print, tol=0.005):
    """Extrude the nominal flat outline back into 3D and compare with the jointed plate solid."""
    results = []
    ok = True
    for fp in parts:
        try:
            f = face_from_polygon(fp.poly)
            solid = extrude(f, amount=fp.T, dir=Vector(0, 0, 1)).moved(fp.plate.plane.location)
            d1 = volume(cut(solid, fp.plate.solid))
            d2 = volume(cut(fp.plate.solid, solid))
            rel = (d1 + d2) / max(fp.plate.solid.volume, 1e-9)
            good = rel < tol
            ok &= good
            results.append({"plate": fp.name, "symdiff_mm3": round(d1 + d2, 2), "rel": round(rel, 5), "ok": good})
            if not good:
                log(f"  RECONSTRUCTION MISMATCH {fp.name}: {100*rel:.2f}% ({d1+d2:.1f} mm3)")
        except Exception as ex:
            ok = False
            results.append({"plate": fp.name, "error": str(ex), "ok": False})
            log(f"  reconstruction failed for {fp.name}: {ex}")
    log(f"  reconstruction: {'all plates match their outlines' if ok else 'MISMATCH, see report'}")
    return {"ok": ok, "plates": results}


# ---------------------------------------------------------------- assembly order

def _same_dir(a: Vector, b: Vector, tol=0.02):
    return (a - b).length < tol


def _dedupe_dirs(dirs):
    out = []
    for d in dirs:
        if not any(_same_dir(d, o) for o in out):
            out.append(d)
    return out


def _joint_directions(joints, plates):
    """For every plate: list of (other_idx, [allowed world directions]) from each joint it takes part in."""
    cons = {p.idx: [] for p in plates}
    for j in joints:
        if j.kind == "NONE" or j.geom is None:
            continue
        g = j.geom
        ortho = abs(j.theta_deg - 90) < 0.5
        if j.kind == "FINGER":
            for p, q in ((j.i, j.j), (j.j, j.i)):
                dirs = [g.u[p.idx] * -1]
                if ortho:
                    cq = q.solid.center()
                    side = (Vector(cq.X, cq.Y, cq.Z) - g.c).dot(p.n)
                    dirs.append(p.n * (-1 if side > 0 else 1))
                cons[p.idx].append((q.idx, dirs))
        elif j.kind == "TAB":
            P, O = j.piercer, j.owner
            cons[P.idx].append((O.idx, [g.u[P.idx] * -1]))
            cons[O.idx].append((P.idx, [g.u[P.idx]] if ortho else []))
        elif j.kind == "CROSSLAP":
            for t0, t1, owner in j.segments:
                # segment (t0..tm, lo) : lo plate's slot opens toward -e -> lo moves +e ; hi moves -e
                other = j.j.idx if owner == j.i.idx else j.i.idx
                is_lo = (t0 < (j.intervals[0][0] + j.intervals[0][1]) / 2 - 1e-6) if j.intervals else True
                cons[owner].append((other, [g.e if is_lo else g.e * -1]))
    return cons


def _swept_solid(p, d: Vector, D: float):
    """Exact swept volume of plate p translated by D along unit direction d (d parallel or perpendicular to n)."""
    local = p.local(p.solid)
    from .flatten import flatten_plate
    polys = flatten_plate(p)
    if not polys:
        return None
    poly = unary_union(polys)
    dl = Vector(d.dot(p.plane.x_dir), d.dot(p.plane.y_dir), d.dot(p.plane.z_dir))   # direction, not a point
    if abs(dl.Z) > 0.999:
        f = face_from_polygon(poly if isinstance(poly, Polygon) else max(poly.geoms, key=lambda g: g.area))
        if dl.Z > 0:
            prism = extrude(f.moved(Location((0, 0, p.T))), amount=D, dir=Vector(0, 0, 1))
        else:
            prism = extrude(f, amount=D, dir=Vector(0, 0, -1))
        sweep = prism
    else:
        dx, dy = dl.X * D, dl.Y * D
        pieces = [poly, translate(poly, dx, dy)]
        for g in (poly.geoms if hasattr(poly, "geoms") else [poly]):
            for ring in [g.exterior] + list(g.interiors):
                c = list(ring.coords)
                for k in range(len(c) - 1):
                    (x0, y0), (x1, y1) = c[k], c[k + 1]
                    quad = Polygon([(x0, y0), (x1, y1), (x1 + dx, y1 + dy), (x0 + dx, y0 + dy)])
                    if quad.area > 1e-9:
                        pieces.append(quad.buffer(0))
        sw = unary_union(pieces).buffer(0)
        sweep = None
        for g in (sw.geoms if hasattr(sw, "geoms") else [sw]):
            if g.area < 1e-6:
                continue
            pr = extrude(face_from_polygon(g), amount=p.T, dir=Vector(0, 0, 1))
            sweep = fuse(sweep, pr)
    return sweep.moved(p.plane.location) if sweep is not None else None


def assembly_order(plates, joints, log=print, max_steps=200, verbose=False):
    """Disassemble one plate at a time. Returns {'ok', 'steps': [(name, direction)], 'stuck': [names]}."""
    cons = _joint_directions(joints, plates)
    remaining = {p.idx: p for p in plates}
    pts = np.array([[v.X, v.Y, v.Z] for p in plates for v in p.solid.vertices()])
    D = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) + 10.0
    steps = []
    while len(remaining) > 1 and len(steps) < max_steps:
        moved = False
        for idx in sorted(remaining, key=lambda k: -len(cons[k])):
            p = remaining[idx]
            active = [(q, dirs) for q, dirs in cons[idx] if q in remaining]
            if active:
                cands = None
                for q, dirs in active:
                    if cands is None:
                        cands = _dedupe_dirs(dirs)
                    else:
                        cands = [c for c in cands if any(_same_dir(c, d) for d in dirs)]
                    if not cands:
                        break
            else:
                cands = [p.n, p.n * -1, p.plane.x_dir, p.plane.x_dir * -1, p.plane.y_dir, p.plane.y_dir * -1]
            if not cands:
                if verbose:
                    log(f"    {p.name}: no direction allowed by its joints")
                continue
            others = Compound([remaining[k].solid for k in remaining if k != idx])
            for d in cands:
                try:
                    sw = _swept_solid(p, unit(d), D)
                except Exception as ex:
                    log(f"  sweep failed for {p.name} along {axis_name(d)}: {ex}")
                    continue
                if sw is None:
                    continue
                hit = volume(inter(sw, others))
                if verbose:
                    log(f"    {p.name} along {axis_name(unit(d))}: swept {volume(sw):.0f} mm3, hits {hit:.2f} mm3")
                if hit < 1.0:
                    steps.append((p.name, axis_name(unit(d))))
                    del remaining[idx]
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
    stuck = [remaining[k].name for k in remaining] if len(remaining) > 1 else []
    order = list(reversed(steps))
    if stuck:
        log(f"  assembly: no single plate can be removed from {stuck}; the kit may need flexing or a design change")
    else:
        log("  assembly order: " + " -> ".join(f"{n} ({d})" for n, d in order))
    return {"ok": not stuck, "order": order, "stuck": stuck}
