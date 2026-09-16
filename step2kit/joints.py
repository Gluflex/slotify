"""Pairwise plate interfaces and joint generation.

For every pair of non-parallel plates that touch or overlap, the tool finds the interface bar along the
intersection line L of the two mid-planes, decides who pierces whom from where each plate's material
continues, segments the bar into fingers/tabs, and applies analytic box cuts in each plate's frame.
All cuts are prisms perpendicular to the plate, so every result is laser-cuttable by construction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from build123d import Location, Plane, Vector

from .geometry import (BIG, TOL, VOL_TOL, axis_name, box_local, cut, extent_along, face_normal, fuse, inter,
                       merge_intervals, norm_shape, planar_faces, segment, solids_of, unit, volume)
from .slabs import Plate


@dataclass
class PairGeom:
    i: Plate
    j: Plate
    e: Vector           # unit direction of L
    c: Vector           # a point on L
    theta: float        # dihedral angle (rad), in (0, pi/2]
    u: dict             # plate idx -> in-plane unit vector pointing from that plate's body toward L
    a: dict             # plate idx -> half extent of the bar along u in that plate (tab length / slot half width)
    frame: dict         # plate idx -> bar Plane (x=e, z=n_p)
    sgn: dict           # plate idx -> +1 if frame y axis == u_p else -1

    def flip(self, p: Plate):
        self.u[p.idx] = self.u[p.idx] * -1
        self.sgn[p.idx] = -self.sgn[p.idx]

    def box(self, p: Plate, x0, x1, y_lo, y_hi):
        """Box in plate p's bar frame; y given along u_p (positive = away from p's body)."""
        s = self.sgn[p.idx]
        if s > 0:
            return box_local(x0, x1, y_lo, y_hi, 0.0, p.T, self.frame[p.idx])
        return box_local(x0, x1, -y_hi, -y_lo, 0.0, p.T, self.frame[p.idx])


@dataclass
class Joint:
    i: Plate
    j: Plate
    kind: str                       # FINGER | TAB | CROSSLAP | NONE
    piercer: object                 # Plate that gets tabs (FINGER: the non-owner)
    owner: object                   # Plate that owns margins (FINGER) / is pierced (TAB)
    intervals: list                 # [(t0, t1)] along e
    segments: list = field(default_factory=list)   # [(t0, t1, owner_idx)]
    theta_deg: float = 90.0
    note: str = ""
    geom: object = None

    def describe(self):
        return f"{self.i.name} x {self.j.name}: {self.kind} ({self.theta_deg:.0f} deg) {self.note}".strip()


@dataclass
class PlateOps:
    cuts: list = field(default_factory=list)
    tabs: list = field(default_factory=list)
    slots: list = field(default_factory=list)


def pair_geometry(i: Plate, j: Plate):
    ni, nj = i.n, j.n
    cosang = ni.dot(nj)
    if abs(abs(cosang) - 1) < 1e-4:
        return None
    e = unit(ni.cross(nj))
    theta = math.acos(min(1.0, abs(cosang)))        # (0, pi/2]
    # point c on both mid-planes, nearest to the midpoint of the two slab centres
    di, dj = i.d_lo + i.T / 2, j.d_lo + j.T / 2
    ci, cj = i.slab.center(), j.slab.center()
    r = (Vector(ci.X, ci.Y, ci.Z) + Vector(cj.X, cj.Y, cj.Z)) / 2
    # c = r + alpha*ni + beta*nj with ni.c = di, nj.c = dj
    b1, b2 = di - ni.dot(r), dj - nj.dot(r)
    det = 1 - cosang * cosang
    alpha = (b1 - cosang * b2) / det
    beta = (b2 - cosang * b1) / det
    c = r + ni * alpha + nj * beta
    s, cc = math.sin(theta), abs(math.cos(theta))
    u, a, frame, sgn = {}, {}, {}, {}
    for p, q in ((i, j), (j, i)):
        up = q.n - p.n * q.n.dot(p.n)
        up = unit(up)
        body = p.slab.center()
        if up.dot(c - Vector(body.X, body.Y, body.Z)) < 0:
            up = up * -1
        u[p.idx] = up
        a[p.idx] = (q.T + p.T * cc) / (2 * s)
        fr = Plane(origin=c - p.n * (p.T / 2), x_dir=e, z_dir=p.n)
        frame[p.idx] = fr
        ydir = p.n.cross(e)
        sgn[p.idx] = 1 if ydir.dot(up) > 0 else -1
    return PairGeom(i, j, e, c, theta, u, a, frame, sgn)


def _e_intervals(shape, g: PairGeom, gap=0.2):
    ivs = []
    for comp in solids_of(shape):
        lo, hi = extent_along(comp, g.c, g.e)
        if hi - lo > 0.05:
            ivs.append((lo, hi))
    return merge_intervals(ivs, gap)


def _fill(p: Plate, g: PairGeom, x0, x1, y_lo, y_hi):
    """Fraction of the given bar-frame box that is filled with p's material."""
    if x1 - x0 < 0.05:
        return 0.0
    b = g.box(p, x0, x1, y_lo, y_hi)
    v = volume(inter(p.slab, b))
    return v / volume(b) if v > VOL_TOL else 0.0


def _has_material(p: Plate, g: PairGeom, x0, x1, y_lo, y_hi, frac=0.15):
    return _fill(p, g, x0, x1, y_lo, y_hi) > frac


def resolve_overlaps(plates, log=print):
    """Overlapping slab regions go to the plate that continues on both sides of them (the through plate).
    Regions where neither or both continue stay shared: they are corner / cross-lap bars."""
    removals = {p.idx: [] for p in plates}
    for ii in range(len(plates)):
        for jj in range(ii + 1, len(plates)):
            i, j = plates[ii], plates[jj]
            g = pair_geometry(i, j)
            if g is None:
                continue
            bi, bj = i.slab.bounding_box(), j.slab.bounding_box()
            if (bi.min.X > bj.max.X + 1 or bj.min.X > bi.max.X + 1 or bi.min.Y > bj.max.Y + 1 or
                    bj.min.Y > bi.max.Y + 1 or bi.min.Z > bj.max.Z + 1 or bj.min.Z > bi.max.Z + 1):
                continue
            ov = inter(i.slab, j.slab)
            for m in solids_of(ov):
                two = {}
                for p in (i, j):
                    d = 2 * g.a[p.idx] + 0.3
                    up = g.u[p.idx]
                    plus = volume(inter(p.slab, m.moved(Location(up * d))))
                    minus = volume(inter(p.slab, m.moved(Location(up * -d))))
                    two[p.idx] = plus > 0.2 * m.volume and minus > 0.2 * m.volume
                if two[i.idx] and not two[j.idx]:
                    removals[j.idx].append(m)
                elif two[j.idx] and not two[i.idx]:
                    removals[i.idx].append(m)
    import copy
    out = []
    for p in plates:
        for m in removals[p.idx]:
            p.slab = cut(p.slab, m)
        comps = sorted(solids_of(p.slab), key=lambda s: -s.volume)
        if not comps:
            log(f"  plate {p.name} was fully consumed by overlap resolution (nothing left of it), dropped")
            continue
        if len(comps) <= 1:
            p.slab = comps[0]
            out.append(p)
            continue
        # several disconnected pieces in one plane: real ones become plates of their own, crumbs are dropped
        real = [c for c in comps if not _is_crumb(c, p.T)]
        crumbs = sum(c.volume for c in comps) - sum(c.volume for c in real)
        if crumbs > 5.0:
            p.notes.append(f"dropped {crumbs:.0f} mm3 of fragments after overlap resolution")
        for k, c in enumerate(real):
            q = p if k == 0 else copy.copy(p)
            q.slab = c
            if len(real) > 1:
                q.name = f"{p.name}{'abcdefgh'[k]}"
                q.notes = list(p.notes)
            out.append(q)
    for k, p in enumerate(out):
        p.idx = k
    return out


def _leading_extension(p: Plate, q: Plate, g: PairGeom):
    """Extrude p's side faces that touch q along u_p far enough to pass through q. None if no such face."""
    from build123d import extrude
    up = g.u[p.idx]
    length = 2 * g.a[p.idx] + 1.0
    parts = None
    for f in planar_faces(p.slab):
        n = face_normal(f)
        if abs(n.dot(p.n)) > 0.7 or n.dot(up) < 0.3:
            continue
        if f.distance_to(q.slab) > TOL:
            continue
        ext = extrude(f, amount=length, dir=up)
        parts = fuse(parts, ext)
    return parts


def _without_third_plates(B, i, j, plates):
    """A bar between i and j never includes material inside a third plate's slab (three-plate corners,
    tab regions of other joints). Clip against a box no bigger than B's own bbox (padded), not an
    "infinite" 5000 mm slab, so the boolean stays local and cheap."""
    if B is None:
        return None
    bb = B.bounding_box()
    pad = 2.0
    for k in plates:
        if k is i or k is j:
            continue
        kb = k.slab.bounding_box()
        if (bb.min.X > kb.max.X + 0.1 or kb.min.X > bb.max.X + 0.1 or bb.min.Y > kb.max.Y + 0.1 or
                kb.min.Y > bb.max.Y + 0.1 or bb.min.Z > kb.max.Z + 0.1 or kb.min.Z > bb.max.Z + 0.1):
            continue
        # clip k's slab to B's own bbox (in k's local frame) before cutting -> the boolean sees a small solid
        corners = [Vector(x, y, z) for x in (bb.min.X - pad, bb.max.X + pad)
                   for y in (bb.min.Y - pad, bb.max.Y + pad) for z in (bb.min.Z - pad, bb.max.Z + pad)]
        local_pts = [k.plane.to_local_coords(c) for c in corners]
        x0, x1 = min(p.X for p in local_pts), max(p.X for p in local_pts)
        y0, y1 = min(p.Y for p in local_pts), max(p.Y for p in local_pts)
        clip = box_local(x0, x1, y0, y1, 0.0, k.T, k.plane)
        B = cut(B, clip)
        if B is None or volume(B) < VOL_TOL:
            return None
    return B


def _is_crumb(comp, T):
    """A fragment too small or too thin to be a part: less than 8 T^3, or nowhere wider than T."""
    if comp.volume < 8 * T ** 3:
        return True
    bb = comp.bounding_box()
    dims = sorted([bb.size.X, bb.size.Y, bb.size.Z])
    return dims[1] < T * 1.5


def _shrink(s0, s1, play):
    """Narrow a tab (protruding finger) by play/2 on each side, independent of kerf: kerf is a uniform
    outline offset applied later at flatten time, but a snug-vs-loose friction fit is a design choice
    about the joint itself, not a laser-beam-width correction. Left alone (play=0) or if the segment is
    too short to shrink safely without disappearing."""
    if play <= 0:
        return s0, s1
    half = play / 2
    if s1 - s0 <= 2 * half + 0.2:
        return s0, s1
    return s0 + half, s1 - half


def find_joints(plates, finger=None, finger_min=None, play=0.0, log=print):
    """Return (joints, ops_by_plate)."""
    bad = [p.name for p in plates if p.slab is None or volume(p.slab) < VOL_TOL]
    if bad:
        log(f"  dropping {len(bad)} plate(s) with no material left ({', '.join(bad)})")
        plates = [p for p in plates if p.slab is not None and volume(p.slab) >= VOL_TOL]
    joints = []
    ops = {p.idx: PlateOps() for p in plates}
    # priority: bigger plates own corner margins
    for p in plates:
        p.priority = p.slab.volume / p.T
    n = len(plates)
    for ii in range(n):
        for jj in range(ii + 1, n):
            i, j = plates[ii], plates[jj]
            g = pair_geometry(i, j)
            if g is None:
                continue
            bi, bj = i.slab.bounding_box(), j.slab.bounding_box()
            if (bi.min.X > bj.max.X + 1 or bj.min.X > bi.max.X + 1 or bi.min.Y > bj.max.Y + 1 or
                    bj.min.Y > bi.max.Y + 1 or bi.min.Z > bj.max.Z + 1 or bj.min.Z > bi.max.Z + 1):
                continue
            dist = i.slab.distance_to(j.slab)
            if dist > 0.5:
                continue
            ov = inter(i.slab, j.slab)
            ov_vol = volume(ov)
            if ov_vol < VOL_TOL and dist > TOL:
                joints.append(Joint(i, j, "NONE", None, None, [], note=f"near miss, gap {dist:.2f} mm: close it in CAD",
                                    theta_deg=math.degrees(g.theta), geom=g))
                continue
            Si, Sj = i.slab, j.slab
            if ov_vol < VOL_TOL:
                # contact: is the pair already interlocked (existing joint)?
                R = inter(i.inf_slab(), j.inf_slab())
                if volume(inter(Si, R)) > VOL_TOL and volume(inter(Sj, R)) > VOL_TOL:
                    joints.append(Joint(i, j, "NONE", None, None, [], note="already interlocked, left as is",
                                        theta_deg=math.degrees(g.theta), geom=g))
                    continue
                Xi = _leading_extension(i, j, g)
                Xj = _leading_extension(j, i, g)
                if Xi is None and Xj is None:
                    joints.append(Joint(i, j, "NONE", None, None, [], note="touching without an edge landing on a face",
                                        theta_deg=math.degrees(g.theta), geom=g))
                    continue
                Ai = fuse(Si, Xi)
                Aj = fuse(Sj, Xj)
                B = inter(Ai, Aj)
            else:
                B = ov
            B = _without_third_plates(B, i, j, plates)
            ivs = _e_intervals(B, g)
            if not ivs:
                joints.append(Joint(i, j, "NONE", None, None, [], note="no usable bar", theta_deg=math.degrees(g.theta), geom=g))
                continue
            Tmax = max(i.T, j.T)
            w = finger if finger else 2 * Tmax
            wmin = finger_min if finger_min else Tmax
            ivs = merge_intervals(ivs, wmin)
            ivs = [(t0, t1) for t0, t1 in ivs if t1 - t0 >= 2 * wmin]
            if not ivs:
                joints.append(Joint(i, j, "NONE", None, None, [], note="bar shorter than two finger widths",
                                    theta_deg=math.degrees(g.theta), geom=g))
                continue
            hull = (min(t0 for t0, _ in ivs), max(t1 for _, t1 in ivs))
            has_pos, has_neg = {}, {}
            for p in (i, j):
                ap = g.a[p.idx]
                fill_plus = _fill(p, g, hull[0], hull[1], ap + 0.05, ap + 2 * p.T + 0.05)
                fill_minus = _fill(p, g, hull[0], hull[1], -ap - 2 * p.T - 0.05, -ap - 0.05)
                if fill_minus < 0.05 <= fill_plus:
                    g.flip(p)                       # the body is on the other side of the bar
                    fill_plus, fill_minus = fill_minus, fill_plus
                has_pos[p.idx] = fill_plus >= 0.15
                has_neg[p.idx] = fill_minus >= 0.05
            if not (has_neg[i.idx] and has_neg[j.idx]):
                joints.append(Joint(i, j, "NONE", None, None, ivs, note="bar without adjacent body", theta_deg=math.degrees(g.theta), geom=g))
                continue
            hp_i, hp_j = has_pos[i.idx], has_pos[j.idx]
            if hp_i and hp_j:
                jt = _crosslap(i, j, g, ivs, ops, log)
                joints.append(jt)
                continue
            if not hp_i and not hp_j:
                kind = "FINGER"
                owner, piercer = (i, j) if i.priority >= j.priority else (j, i)
            elif hp_j:
                kind, piercer, owner = "TAB", i, j
            else:
                kind, piercer, owner = "TAB", j, i
            jt = Joint(i, j, kind, piercer, owner, ivs, theta_deg=math.degrees(g.theta), geom=g)
            segs = []
            for t0, t1 in ivs:
                A, Bseg = segment(t0, t1, w, wmin)
                for s0, s1 in A:
                    segs.append((s0, s1, piercer.idx))
                for s0, s1 in Bseg:
                    segs.append((s0, s1, owner.idx))
                if not A:
                    jt.note += f" [bar {t1-t0:.1f} mm too short for a tab]"
            jt.segments = sorted(segs)
            # ops
            ap = g.a[piercer.idx]
            beyond = 2 * Tmax + 1.0          # an ending plate has nothing past the bar except overshoot / bevel
            ops[piercer.idx].cuts.append(g.box(piercer, hull[0] - 0.01, hull[1] + 0.01, -ap, ap + beyond))
            for s0, s1, o in jt.segments:
                if o == piercer.idx:
                    ps0, ps1 = _shrink(s0, s1, play)
                    ops[piercer.idx].tabs.append(g.box(piercer, ps0, ps1, -ap, ap))
            ao = g.a[owner.idx]
            if kind == "FINGER":
                for t0, t1 in ivs:
                    # the owner keeps whatever little it has past the bar; only the bar itself is re-partitioned
                    ops[owner.idx].cuts.append(g.box(owner, t0, t1, -ao, ao))
                for s0, s1, o in jt.segments:
                    if o == owner.idx:
                        os0, os1 = _shrink(s0, s1, play)
                        ops[owner.idx].tabs.append(g.box(owner, os0, os1, -ao, ao))
            else:
                for s0, s1, o in jt.segments:
                    if o == piercer.idx:
                        ops[owner.idx].slots.append(g.box(owner, s0, s1, -ao, ao))
            joints.append(jt)
    return joints, ops


def _crosslap(i, j, g, ivs, ops, log):
    jt = Joint(i, j, "CROSSLAP", None, None, ivs, theta_deg=math.degrees(g.theta), geom=g)
    for t0, t1 in ivs:
        tm = (t0 + t1) / 2
        ext = {}
        for p in (i, j):
            ap = g.a[p.idx]
            ext[p.idx] = (_has_material(p, g, t0 - 2 * p.T, t0 - 0.05, -ap, ap, frac=0.05),
                          _has_material(p, g, t1 + 0.05, t1 + 2 * p.T, -ap, ap, frac=0.05))
        # plate whose slot opens at the t1 end must not extend beyond t1, the other not beyond t0
        if not ext[i.idx][1] and not ext[j.idx][0]:
            hi_p, lo_p = i, j
        elif not ext[j.idx][1] and not ext[i.idx][0]:
            hi_p, lo_p = j, i
        else:
            jt.note += " [cross-lap has no open edge: not assemblable, skipped]"
            continue
        ops[hi_p.idx].slots.append(g.box(hi_p, tm, t1 + 0.01, -g.a[hi_p.idx], g.a[hi_p.idx]))
        ops[lo_p.idx].slots.append(g.box(lo_p, t0 - 0.01, tm, -g.a[lo_p.idx], g.a[lo_p.idx]))
        jt.segments += [(t0, tm, lo_p.idx), (tm, t1, hi_p.idx)]
    return jt


def apply_ops(plates, ops, log=print):
    """Build the final solids: slab - cuts, + tabs, - slots, then higher-priority plates win overlaps."""
    for p in plates:
        s = p.slab
        o = ops[p.idx]
        if o.cuts:
            s = norm_shape(s.cut(*o.cuts))
        if o.tabs:
            s = norm_shape(s.fuse(*o.tabs))
        if o.slots:
            s = norm_shape(s.cut(*o.slots))
        p.solid = s
    order = sorted(plates, key=lambda p: -p.priority)
    for k, hi in enumerate(order):
        for lo in order[k + 1:]:
            bi, bj = hi.solid.bounding_box(), lo.solid.bounding_box()
            if (bi.min.X > bj.max.X + 0.1 or bj.min.X > bi.max.X + 0.1 or bi.min.Y > bj.max.Y + 0.1 or
                    bj.min.Y > bi.max.Y + 0.1 or bi.min.Z > bj.max.Z + 0.1 or bj.min.Z > bi.max.Z + 0.1):
                continue
            ov = inter(lo.solid, hi.solid)
            if volume(ov) > 1e-3:
                lo.solid = cut(lo.solid, hi.solid)
                lo.notes.append(f"trimmed {volume(ov):.1f} mm3 where it overlapped {hi.name}")
    for p in plates:
        sols = solids_of(p.solid)
        if len(sols) > 1:
            big = max(sols, key=lambda s: s.volume)
            rest = sum(s.volume for s in sols) - big.volume
            if rest > 2.0:
                p.notes.append(f"WARNING: plate splits into {len(sols)} pieces ({rest:.0f} mm3 dropped)")
            p.solid = big
        elif sols:
            p.solid = sols[0]
    return plates
