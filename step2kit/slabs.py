"""Plate (slab) extraction from STEP solids.

Every input solid is treated as a union of constant-thickness plates. A single shelled solid yields many
plates; a body that already is a plate yields itself. Plates are found face by face: for each planar face
whose material is exactly one sheet thickness deep, the region of the solid between that face and its
parallel counterpart is a plate candidate. Candidates are de-duplicated and validated.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from build123d import Compound, Location, Plane, Vector, import_step

from .geometry import (BIG, TOL, VOL_TOL, box_local, cut, extent_along, face_normal, face_offset, fuse, inter,
                       perp_axis, planar_faces, solids_of, unit, volume)


@dataclass
class Plate:
    idx: int
    name: str
    n: Vector                   # unit normal of the plate (thickness direction)
    d_lo: float                 # material occupies d_lo <= p·n <= d_lo + T
    T: float
    slab: object                # raw slab solid (may still overlap neighbours at the corners)
    plane: Plane                # frame: origin on the lo plane, z_dir = n
    source: str = ""            # which input solid / face it came from
    body: object = None         # slab minus every other plate's infinite slab (set later)
    solid: object = None        # final jointed solid (set by joints.apply)
    priority: float = 0
    area: float = 0.0
    orig_interval: tuple = (0.0, 0.0)   # slab interval along n as found in the model (for de-duplication)
    notes: list = field(default_factory=list)

    @property
    def d_hi(self):
        return self.d_lo + self.T

    def inf_slab(self):
        """The infinite slab of this plate as a big box (world coordinates)."""
        return box_local(-BIG, BIG, -BIG, BIG, 0.0, self.T, self.plane)

    def local(self, shape):
        return shape.moved(self.plane.location.inverse())


def load_step(path: str):
    cmp_ = import_step(path)
    sols = solids_of(cmp_)
    if not sols:
        raise ValueError("STEP file contains no solids")
    return sols


def detect_thickness_classes(solids, log=print):
    """Area-weighted histogram of 'material depth behind planar faces'. Returns [(t, area_share)]."""
    hist = Counter()
    total = 0.0
    for s in solids:
        info = _faces_info(s)
        for f, n, d, a, bb in info:
            if a < 1.0:
                continue
            total += a
            best = _face_depth(f, n, d, info)
            if best is not None:
                hist[round(best, 2)] += a
    classes = [(t, a / total) for t, a in hist.most_common() if a / total >= 0.04]
    classes.sort(key=lambda x: -x[1])
    log("thickness classes (area share): " + ", ".join(f"{t:.2f} mm ({100*s:.0f}%)" for t, s in classes))
    return classes


def _faces_info(s):
    out = []
    for f in planar_faces(s):
        n = face_normal(f)
        c = f.center()
        out.append((f, n, Vector(c.X, c.Y, c.Z), f.area, f.bounding_box()))
    return out


def _face_depth(f, n, c, faces_info):
    """Distance from face f (outward normal n, centre c) to the nearest anti-parallel face behind it."""
    best = None
    d = c.dot(n)
    bb = f.bounding_box()
    for g, ng, cg, ag, bbg in faces_info:
        if g is f or n.dot(ng) > -0.9999:
            continue
        t = d - cg.dot(n)            # both offsets measured along n
        if t <= 0.05:
            continue
        if (bb.max.X < bbg.min.X - t - 0.1 or bbg.max.X < bb.min.X - t - 0.1 or
                bb.max.Y < bbg.min.Y - t - 0.1 or bbg.max.Y < bb.min.Y - t - 0.1 or
                bb.max.Z < bbg.min.Z - t - 0.1 or bbg.max.Z < bb.min.Z - t - 0.1):
            continue
        if best is None or t < best:
            best = t
    return best


def extract_plates(solids, thickness_override=None, log=print):
    """Return a list of Plate (raw slabs, may overlap at corners) covering the input solids."""
    classes = detect_thickness_classes(solids, log)
    if not classes:
        raise ValueError("no constant-thickness walls found; is this a plate-based model?")
    class_ts = [t for t, _ in classes]
    sheet_T = thickness_override or class_ts[0]
    plates: list[Plate] = []
    for si, s in enumerate(solids):
        info = _faces_info(s)
        # broad faces first: bigger faces make better seeds
        for f, n, c, a, bb in sorted(info, key=lambda r: -r[3]):
            d = c.dot(n)
            t = _face_depth(f, n, c, info)
            if t is None:
                continue
            t = min(class_ts, key=lambda c: abs(c - t)) if min(abs(c - t) for c in class_ts) < 0.05 else None
            if t is None or a < 4 * t * t:
                continue
            # a seed face must be broad. A strip about one sheet thickness wide (area/perimeter ~ t/2)
            # is an edge face -- e.g. the bottom rim of an open box, which sits exactly t below the
            # floors of the vent slots in the walls and would otherwise read as a flat 3 mm "plate".
            perim = f.outer_wire().length + sum(w.length for w in f.inner_wires())
            if perim > 0 and a / perim < 0.6 * t:
                continue
            # same plane, same thickness as an existing plate?  (a single physical wall can be split
            # across several STEP faces, e.g. by a cutout carved with separate boolean operations)
            same_interval_plate = None
            for p in plates:
                if abs(abs(p.n.dot(n)) - 1) < 1e-6:
                    # p.d_lo is measured along p.n; convert this face's interval to p.n
                    if p.n.dot(n) > 0:
                        lo, hi = d - t, d
                    else:
                        lo, hi = -d, -d + t
                    if abs(lo - p.orig_interval[0]) < 0.02 and abs(hi - p.orig_interval[1]) < 0.02:
                        same_interval_plate = p
                        break
            # the slab region behind this face. Bounded to this face's OWN footprint (padded), not an
            # unbounded lateral extent: on a facetted curved surface, adjacent facets are contiguous with
            # this one (they share an edge on the same solid), so an unbounded box pulls their material
            # in too and the seed face ends up "covering" only a sliver of an oversized, wrong region.
            pad = max(t, 1.0) * 3
            plane = Plane(origin=n * (d - t), x_dir=perp_axis(n), z_dir=n)
            local_pts = [plane.to_local_coords(Vector(v.X, v.Y, v.Z)) for v in f.vertices()]
            fx0, fx1 = min(p.X for p in local_pts), max(p.X for p in local_pts)
            fy0, fy1 = min(p.Y for p in local_pts), max(p.Y for p in local_pts)
            region = inter(s, box_local(fx0 - pad, fx1 + pad, fy0 - pad, fy1 + pad, 0.0, t, plane))
            comps = solids_of(region)
            if not comps:
                continue
            # pick the component that actually touches the seed face. Not a probe at the face centre:
            # a face with a cutout (display window, hand hole) can have its centroid inside the hole,
            # and probing there found nothing and silently dropped the whole plate.
            comp = min(comps, key=lambda x: x.distance_to(f))
            if comp.distance_to(f) > 0.05:
                continue
            if same_interval_plate is not None:
                already = volume(inter(comp, same_interval_plate.slab))
                if already < 0.9 * volume(comp):
                    same_interval_plate.slab = fuse(same_interval_plate.slab, comp)
                    same_interval_plate.area += a
                continue
            # broad-face sanity: this face must cover a fair share of its slab's mid-section
            sec = inter(comp, box_local(fx0 - pad, fx1 + pad, fy0 - pad, fy1 + pad, t / 2 - 0.005, t / 2 + 0.005, plane))
            sec_area = volume(sec) / 0.01 if sec is not None else 0.0
            if sec_area > 0 and a < 0.10 * sec_area:
                continue
            note = None
            orig = (d - t, d)
            if abs(t - sheet_T) > 0.05:
                # e.g. an inclined plate that was extruded vertically: keep the seed face, re-thicken to the sheet
                comp, plane, t, note = _rethicken(comp, plane, t, sheet_T, n, d)
            pl = Plate(idx=len(plates), name=f"P{len(plates)+1}", n=n, d_lo=d - t, T=t, slab=comp, plane=plane,
                       source=f"solid {si} face area {a:.0f}", area=a, orig_interval=orig)
            if note:
                pl.notes.append(note)
            plates.append(pl)
            log(f"  plate {pl.name}: n={n.X:+.2f},{n.Y:+.2f},{n.Z:+.2f} T={t:.2f} vol={comp.volume:.0f} seed face {a:.0f} mm2"
                + (f"  [{note}]" if note else ""))
    if not plates:
        raise ValueError("no plates found")
    plates = _dedupe(plates, log)
    # leftover check: material not covered by any plate
    union = None
    for p in plates:
        union = fuse(union, p.slab)
    left = 0.0
    left_bb = []
    for s in solids:
        rest = cut(s, union)
        for r in solids_of(rest):
            if r.volume > max(VOL_TOL, 2.0):
                left += r.volume
                bb = r.bounding_box()
                left_bb.append((r.volume, (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)))
    if left > 0:
        log(f"WARNING: {left:.0f} mm3 of the model is not part of any plate (non-plate geometry): {left_bb[:5]}")
    for p in plates:
        p.priority = p.idx  # refined later by area
    _name_plates(plates)
    return plates, left_bb


def _rethicken(comp, plane, t, sheet_T, n, d):
    """Replace a plate of wrong thickness t by one of thickness sheet_T behind the same seed face."""
    from build123d import extrude
    local = comp.moved(plane.location.inverse())
    thin = box_local(-BIG, BIG, -BIG, BIG, t / 2 - 0.005, t / 2 + 0.005, Plane.XY)
    sec = inter(local, thin)
    faces = [f for f in sec.faces() if abs(f.normal_at().Z - 1) < 1e-6]
    if not faces:
        return comp, plane, t, None
    new_plane = Plane(origin=n * (d - sheet_T), x_dir=plane.x_dir, z_dir=n)
    solid = None
    for f in faces:
        f2 = f.moved(Location((0, 0, sheet_T - t / 2)))     # onto the seed face level in the new frame
        prism = extrude(f2, amount=sheet_T, dir=Vector(0, 0, -1))
        solid = fuse(solid, prism)
    solid = solid.moved(new_plane.location)
    note = f"model thickness was {t:.2f} mm, re-thickened to {sheet_T:.2f} mm behind its outer face"
    return solid, new_plane, sheet_T, note


def _dedupe(plates, log=print):
    """Two seeds of the same physical plate (e.g. its two faces after re-thickening) give overlapping,
    parallel slabs: keep the one with the bigger seed face."""
    keep = []
    for p in sorted(plates, key=lambda x: -x.area):
        dup = False
        for q in keep:
            if abs(abs(p.n.dot(q.n)) - 1) > 1e-4:
                continue
            ov = volume(inter(p.slab, q.slab))
            if ov > 0.3 * min(p.slab.volume, q.slab.volume):
                dup = True
                log(f"  plate {p.name} duplicates {q.name} (overlap {ov:.0f} mm3), dropped")
                break
        if not dup:
            keep.append(p)
    for k, p in enumerate(keep):
        p.idx = k
    return keep


def _name_plates(plates):
    """Short deterministic names: axis + mid-plane position (X-98, Y44, Z2) or S1, S2 for sloped plates."""
    counters = Counter()
    for p in plates:
        n = p.n
        ax = max(("X", "Y", "Z"), key=lambda a: abs(getattr(n, a)))
        val = getattr(n, ax)
        if abs(val) > 0.999:
            mid = (p.d_lo + p.T / 2) * (1 if val > 0 else -1)
            base = f"{ax}{mid:+.0f}".replace("+", "") if mid >= 0 else f"{ax}{mid:.0f}"
        else:
            counters["S"] += 1
            base = f"S{counters['S']}"
        counters[base] += 1
        p.name = base if counters[base] == 1 else f"{base}_{counters[base]}"


def compute_bodies(plates, log=print):
    """body = slab minus every other plate's infinite slab (clipped to this slab's bbox)."""
    for p in plates:
        body = p.slab
        bb = p.slab.bounding_box()
        for q in plates:
            if q is p:
                continue
            clip = q.inf_slab()
            body = cut(body, clip)
        p.body = body
    return plates
