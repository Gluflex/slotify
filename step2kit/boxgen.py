"""Parametric box generator: build a rectangular box's geometry directly, with no STEP file and no CAD
step at all, then hand it to the exact same plate-extraction / joint-finding / nesting pipeline every
imported model goes through. A from-scratch box gets the same real geometric joint analysis (finger and
cross-lap joints resolved from actual plate intersections, not a canned template) as a real part would --
every shape here is one solid built with this codebase's own box_local/cut helpers, sharing no code with
any other box generator, just the idea that a plain box shouldn't require external CAD software.

lid modes:
  closed -- solid top, the box is fully sealed (the original, simplest generator).
  open   -- no top at all.
  slip   -- open top, plus a separate flat lid sized to friction-fit the interior opening. It is
            positioned with deliberate clearance from every wall so the joint-finder sees it as an
            unrelated, unconnected part (no fingers, no slots) -- exactly what a loose friction lid needs.
  slide  -- open top, plus grooves cut into the front/back walls and a matching lid that slides in
            through an open slot in the right wall, pencil-box style, stopping against the left wall.
"""
from __future__ import annotations

from build123d import Align, Cylinder, Location, Plane, Vector

from .geometry import VOL_TOL, box_local, cut, perp_axis, volume

CLEAR_FRACTION = 0.15   # of wall thickness, minimum 0.3 mm: gap used for every loose-fit part


def _clearance(wall):
    return max(0.3, wall * CLEAR_FRACTION)


def _divider_positions(n, span, wall):
    """n evenly spaced divider centres splitting the interior (span - 2*wall) into n+1 compartments."""
    if n <= 0:
        return []
    lo, hi = -span / 2 + wall, span / 2 - wall
    step = (hi - lo) / (n + 1)
    return [lo + step * (k + 1) for k in range(n)]


def _round_hole(center: Vector, normal: Vector, depth, radius):
    """A through-hole starting `center` (on the outer face) and cutting inward along -normal, for
    `depth` mm -- z_dir is -normal (not normal) so increasing local z moves INTO the wall, not away
    from it; getting this backwards cuts a shallow dimple at the outer face instead of a through-hole."""
    plane = Plane(origin=center, x_dir=perp_axis(normal), z_dir=-normal)
    cyl = Cylinder(radius, depth, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return cyl.moved(Location((0, 0, -1.0))).moved(plane.location)


def _grid(lo, hi, n, margin):
    if n <= 0:
        return []
    a, b = lo + margin, hi - margin
    if b <= a:
        return [(lo + hi) / 2]
    if n == 1:
        return [(a + b) / 2]
    step = (b - a) / (n - 1)
    return [a + step * k for k in range(n)]


def _handle_holes(shell, w, d, h, wall, handle_w=60.0, handle_h=25.0, log=print):
    """A rectangular hand-hole through the two end (X-normal) walls, near the top."""
    hw = min(handle_w, d * 0.6)
    hh = min(handle_h, h * 0.3)
    z0 = h * 0.6
    left = box_local(-w / 2 - 1, -w / 2 + wall + 1, -hw / 2, hw / 2, z0, z0 + hh, Plane.XY)
    right = box_local(w / 2 - wall - 1, w / 2 + 1, -hw / 2, hw / 2, z0, z0 + hh, Plane.XY)
    out = cut(cut(shell, left), right)
    log(f"  handle holes: {hw:.0f} x {hh:.0f} mm through the two end walls")
    return out


_VENT_FACES = ("front", "back", "left", "right", "top", "bottom")


def _vent_holes(shell, w, d, h, wall, face, rows, cols, hole_d, log=print):
    if face not in _VENT_FACES:
        raise ValueError(f"unknown vent face '{face}', must be one of {_VENT_FACES}")
    r = hole_d / 2
    # clear of the plate edge by enough to also clear the finger-joint teeth along that edge (which can
    # intrude several wall-thicknesses deep), not just the hole's own radius -- a hole positioned by
    # radius alone can break through into a joint gap and merge with it instead of staying an island
    mg = max(hole_d * 0.75, wall * 3.0)
    if face in ("front", "back"):
        y, normal = (-d / 2, Vector(0, -1, 0)) if face == "front" else (d / 2, Vector(0, 1, 0))
        us, vs = _grid(-w / 2, w / 2, cols, mg), _grid(0, h, rows, mg)
        centers = [Vector(u, y, v) for v in vs for u in us]
    elif face in ("left", "right"):
        x, normal = (-w / 2, Vector(-1, 0, 0)) if face == "left" else (w / 2, Vector(1, 0, 0))
        us, vs = _grid(-d / 2, d / 2, cols, mg), _grid(0, h, rows, mg)
        centers = [Vector(x, u, v) for v in vs for u in us]
    else:
        z, normal = (h, Vector(0, 0, 1)) if face == "top" else (0, Vector(0, 0, -1))
        us, vs = _grid(-w / 2, w / 2, cols, mg), _grid(-d / 2, d / 2, rows, mg)
        centers = [Vector(u, v, z) for v in vs for u in us]
    out = shell
    for c in centers:
        out = cut(out, _round_hole(c, normal, wall + 2.0, r))
    log(f"  vent holes: {rows} x {cols} holes of {hole_d:.1f} mm diameter on the {face} wall")
    return out


def _slide_rails(w, d, h, wall, log=print):
    """Four flat rail strips (a lower + upper pair against the front wall, another pair against the
    back wall) that, once glued on, capture the lid vertically in the gap between each pair -- the
    laser-cut-safe equivalent of a routed groove. A real groove (partial-depth, as in solid wood) is not
    reproducible from sheet material cut on a laser, which only ever cuts clean through; every rail here
    stays a plain flat, uniform-thickness part like everything else in the kit, glued on after cutting
    rather than machined in. Positioned with a small clearance from every wall so the joint-finder treats
    them as independent parts, not fingered joints -- gluing them accurately needs the channel gap kept
    clean, not fingers eating into a strip this thin."""
    clear = _clearance(wall)
    rail_t = wall
    rail_h = max(wall * 1.5, 4.0)
    gap = 0.5
    z_lo_top = h - wall * 2.5                       # top edge of the lower rail = the channel floor
    z_hi_bot = z_lo_top + wall + clear               # bottom edge of the upper rail = the channel ceiling
    x0, x1 = -w / 2 + wall + gap, w / 2 - wall - gap
    fy0, fy1 = -d / 2 + wall + gap, -d / 2 + wall + gap + rail_t
    by0, by1 = d / 2 - wall - gap - rail_t, d / 2 - wall - gap
    rails = [
        box_local(x0, x1, fy0, fy1, z_lo_top - rail_h, z_lo_top, Plane.XY),
        box_local(x0, x1, fy0, fy1, z_hi_bot, z_hi_bot + rail_h, Plane.XY),
        box_local(x0, x1, by0, by1, z_lo_top - rail_h, z_lo_top, Plane.XY),
        box_local(x0, x1, by0, by1, z_hi_bot, z_hi_bot + rail_h, Plane.XY),
    ]
    log(f"  slide-lid rails: 4 strips to glue against the front/back inner faces, channel at "
        f"z={z_lo_top:.1f}-{z_hi_bot:.1f} mm (see the assembly notes -- this is a glued channel, not cut in)")
    return rails, z_lo_top


def _slide_lid(w, d, h, wall, z_lo_top):
    clear = _clearance(wall)
    lid_w = w - 2 * wall - clear
    lid_d = d - 2 * wall - 2 * clear   # a bit shy of the full interior depth: slides freely in the channel
    float_z = z_lo_top + 500.0         # far clear of the shell and rails: an independent part, glued/slid in later
    return box_local(-lid_w / 2, lid_w / 2, -lid_d / 2, lid_d / 2, float_z, float_z + wall, Plane.XY)


def _slip_lid(w, d, h, wall):
    clear = _clearance(wall)
    float_z = h + 300.0
    return box_local(-w / 2 + wall + clear, w / 2 - wall - clear, -d / 2 + wall + clear, d / 2 - wall - clear,
                     float_z, float_z + wall, Plane.XY)


def synth_box(w, d, h, wall, lid="closed", div_x=0, div_y=0, handle=False,
             vent_face=None, vent_rows=0, vent_cols=0, vent_hole_d=8.0, log=print):
    if lid not in ("closed", "open", "slip", "slide"):
        raise ValueError(f"unknown lid mode '{lid}'")
    if wall * 2 >= w or wall * 2 >= d:
        raise ValueError(f"wall thickness {wall:g} mm is too large for a {w:g} x {d:g} x {h:g} mm box")
    if lid == "closed" and wall * 2 >= h:
        raise ValueError(f"wall thickness {wall:g} mm is too large for a {w:g} x {d:g} x {h:g} mm box")

    outer = box_local(-w / 2, w / 2, -d / 2, d / 2, 0.0, h, Plane.XY)
    cap_z1 = h - wall if lid == "closed" else h + 1.0
    inner = box_local(-w / 2 + wall, w / 2 - wall, -d / 2 + wall, d / 2 - wall, wall, cap_z1, Plane.XY)
    shell = cut(outer, inner)

    if volume(shell) < VOL_TOL:
        raise ValueError(f"wall thickness {wall:g} mm leaves no material for a {w:g} x {d:g} x {h:g} mm box")
    if handle:
        shell = _handle_holes(shell, w, d, h, wall, log=log)
    if vent_face and vent_rows and vent_cols:
        shell = _vent_holes(shell, w, d, h, wall, vent_face, vent_rows, vent_cols, vent_hole_d, log=log)

    solids = [shell]
    div_top = h - wall if lid == "closed" else h
    for x in _divider_positions(div_x, w, wall):
        solids.append(box_local(x - wall / 2, x + wall / 2, -d / 2 + wall, d / 2 - wall, wall, div_top, Plane.XY))
    for y in _divider_positions(div_y, d, wall):
        solids.append(box_local(-w / 2 + wall, w / 2 - wall, y - wall / 2, y + wall / 2, wall, div_top, Plane.XY))

    lid_idx = None
    if lid == "slip":
        solids.append(_slip_lid(w, d, h, wall))
        lid_idx = len(solids) - 1
    elif lid == "slide":
        rails, z_lo_top = _slide_rails(w, d, h, wall, log=log)
        solids += rails
        solids.append(_slide_lid(w, d, h, wall, z_lo_top))
        lid_idx = len(solids) - 1

    log(f"box generator: {w:g} x {d:g} x {h:g} mm outer, {wall:g} mm walls, lid={lid}"
        + (f", {div_x}x{div_y} dividers" if div_x or div_y else "")
        + (", handle holes" if handle else "")
        + (f", {vent_rows}x{vent_cols} vents on {vent_face}" if vent_face and vent_rows and vent_cols else ""))
    return solids, lid_idx
