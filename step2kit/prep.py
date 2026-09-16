"""Preprocessing for inputs that are not already a plate-based model.

Two cases, both fully deterministic (an approximation choice, not a guess):

- Curved surfaces (cylinders, cones, fillets, splines, ...): tessellated at a chosen resolution, then
  adjacent coplanar triangles are merged into clean flat polygon faces and re-sewn into a solid. A
  cylinder becomes an N-sided prism; "resolution" is exactly the tessellation tolerance, so "low
  resolution" means a coarse angular tolerance and few facets.
- Filled (solid, not shelled) bodies: detected by comparing volume to surface area (a thin plate has
  2*volume/area ~= its thickness; a filled block does not), then turned into a shell by taking a
  wall-thickness slab from every outer face directly (see `auto_shell` for why this beats a single
  inward boolean offset through one chosen opening).
Both stages log what they did; the caller (pipeline.py) surfaces that in the report.
"""
from __future__ import annotations

import numpy as np
from build123d import Face, GeomType, Solid, Vector, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.ShapeFix import ShapeFix_Shape
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from .geometry import perp_axis, planar_faces


def has_curved_faces(solid) -> bool:
    return any(f.geom_type != GeomType.PLANE for f in solid.faces())


def curved_area_fraction(solid) -> float:
    faces = solid.faces()
    total = sum(f.area for f in faces)
    if total < 1e-9:
        return 0.0
    curved = sum(f.area for f in faces if f.geom_type != GeomType.PLANE)
    return curved / total


def needs_facet(solid, area_threshold=0.05) -> bool:
    """Only worth faceting if curved surfaces are a real part of the shape, not just a stray tiny
    fillet or rounded edge on an otherwise flat model. Faceting an entire solid to chase down a couple
    of small curved faces would needlessly re-tessellate every flat face too (the merge step re-derives
    them from the mesh) and can only make a clean model worse; those few curved faces are better left
    as unrecognised leftover, reported honestly, than corrupting the rest."""
    return curved_area_fraction(solid) >= area_threshold


def facet_solid(solid, chord_tol=0.5, angle_deg=30.0, sew_tol=0.05, log=print, name=""):
    """Replace every face of `solid` by a tessellation at the given tolerance, merge adjacent coplanar
    triangles into single flat polygons, and re-sew into a solid. Raises RuntimeError with a clear
    message if the result cannot be sewn watertight (the caller should retry with looser tolerances)."""
    import math
    verts, tris = solid.tessellate(chord_tol, angular_tolerance=math.radians(angle_deg))
    V = np.array([[v.X, v.Y, v.Z] for v in verts])
    groups = {}
    for t in tris:
        a, b, c = V[t[0]], V[t[1]], V[t[2]]
        n = np.cross(b - a, c - a)
        nn = np.linalg.norm(n)
        if nn < 1e-9:
            continue
        n = n / nn
        d = round(float(np.dot(a, n)), 2)
        key = (round(float(n[0]), 3), round(float(n[1]), 3), round(float(n[2]), 3), d)
        groups.setdefault(key, []).append(t)
    faces = []
    for key, ts in groups.items():
        n = Vector(key[0], key[1], key[2])
        origin = Vector(*V[ts[0][0]])
        u = perp_axis(n)
        v = n.cross(u)
        polys = []
        for t in ts:
            pts2d = [((Vector(*V[idx]) - origin).dot(u), (Vector(*V[idx]) - origin).dot(v)) for idx in t]
            poly = Polygon(pts2d)
            if poly.area < 1e-9:
                continue
            if not poly.exterior.is_ccw:
                poly = Polygon(list(poly.exterior.coords)[::-1])
            polys.append(poly)
        if not polys:
            continue
        merged = unary_union(polys)
        for g in (merged.geoms if hasattr(merged, "geoms") else [merged]):
            if g.area < 1e-6:
                continue
            g = orient(g, sign=1.0)
            outer = [origin + u * x + v * y for x, y in list(g.exterior.coords)[:-1]]
            holes = [[origin + u * x + v * y for x, y in list(h.coords)[:-1]] for h in g.interiors]
            faces.append(Face(Wire.make_polygon(outer, close=True),
                              [Wire.make_polygon(h, close=True) for h in holes]))
    for tol in (sew_tol, sew_tol * 4, sew_tol * 20):
        sew = BRepBuilderAPI_Sewing(tol)
        for f in faces:
            sew.Add(f.wrapped)
        sew.Perform()
        result = sew.SewedShape()
        if result.ShapeType() == TopAbs_ShapeEnum.TopAbs_SHELL:
            break
    else:
        if sew.NbFreeEdges() > 0:
            raise RuntimeError(f"{name or 'solid'}: faceting left {sew.NbFreeEdges()} free edges even at "
                               f"{sew_tol*20:.2f} mm sewing tolerance; the curved geometry is too irregular "
                               f"for the tessellation to close cleanly, try a coarser --facet-chord")
        raise RuntimeError(f"{name or 'solid'}: faceting closed watertight but split into separate, "
                           f"disconnected pieces (a sealed internal cavity, most likely) rather than one "
                           f"shell; a fully closed hollow of a smooth body has no cuttable edges, it needs "
                           f"an opening (see auto_shell)")
    shell = TopoDS.Shell_s(result)
    mk = BRepBuilderAPI_MakeSolid(shell)
    if not mk.IsDone():
        raise RuntimeError(f"{name or 'solid'}: could not build a solid from the faceted shell")
    out = Solid(mk.Solid())
    # BRepBuilderAPI_MakeSolid already produces a correctly-typed, correctly-oriented solid here; a
    # generic ShapeFix_Shape pass on top of it has been observed to silently strip the SOLID wrapper
    # down to a bare SHELL (same geometry, but .solids() becomes empty and booleans against it return
    # nothing) even though .volume and .is_valid still report fine. Only reach for repair if the direct
    # result actually looks wrong.
    if not out.solids() or abs(out.volume - solid.volume) > 0.5 * solid.volume:
        fixer = ShapeFix_Shape(out.wrapped)
        fixer.Perform()
        fixed = Solid(fixer.Shape())
        if fixed.solids() and abs(fixed.volume - solid.volume) < abs(out.volume - solid.volume):
            out = fixed
        if not out.solids():
            raise RuntimeError(f"{name or 'solid'}: the faceted shell sewed watertight but would not "
                               f"become a valid solid (volume {out.volume:.0f} vs {solid.volume:.0f} expected)")
    log(f"  {name or 'solid'}: faceted {len(solid.faces())} faces ({sum(1 for f in solid.faces() if f.geom_type != GeomType.PLANE)} curved) "
        f"-> {len(out.faces())} flat facets, volume {solid.volume:.0f} -> {out.volume:.0f} mm3 "
        f"({100*abs(out.volume-solid.volume)/solid.volume:.1f}% change)")
    return out


def equivalent_thickness(solid) -> float:
    """2*volume/surface_area: for a thin plate this is close to its thickness; for a filled block it is
    much larger (a cube of side L has 2*V/SA = L/3). Used to decide whether a solid needs shelling."""
    sa = sum(f.area for f in solid.faces())
    return 2.0 * solid.volume / sa if sa > 1e-9 else float("inf")


def auto_shell(solid, wall, log=print, name=""):
    """Turn a filled body into a shell by taking a wall-thickness slab from EVERY outer face, rather
    than boolean-offsetting the whole solid inward through one chosen opening.

    The first version of this picked one face to remove (preferring the topmost, like a container's
    lid) and called build123d's `Solid.hollow` (OCCT's BRepOffsetAPI_MakeThickSolid) to offset every
    remaining face inward. That is the wrong operation for a part that already looks like a box: it
    silently discards whichever face was picked, and a single global inward offset distorts or nearly
    erases thin cantilevered features (a diving-board-style overhang collapses toward zero thickness).
    It was also the least reliable call in the whole pipeline -- OCCT's thick-solid offset can fail
    outright depending on exactly which face is opened, on otherwise ordinary geometry.

    Every face of a solid with no pre-existing internal cavity has an outward-pointing normal, by
    definition -- including the walls of a cutout that already goes all the way through. So instead:
    for each outer planar face, cut out just the slab of material within `wall` mm of that face
    (inward along its own normal) directly from the original solid. No face is singled out or removed;
    a cutout's rim faces stay part of whichever large face they are carved into, because they were
    never large enough to seed their own plate. A thin overhang simply yields two thin, truncated,
    likely-overlapping slabs (one from each of its faces) which the ordinary plate-overlap and
    duplicate-plate logic downstream already merges or dedupes -- there is no separate distortion step
    to get wrong. The result is fed straight into the normal facet/extract_plates pipeline exactly like
    a real shelled solid, because it now has genuine antiparallel face pairs at the wall thickness."""
    from .geometry import box_local, inter, volume, VOL_TOL, fuse
    from build123d import Plane, Vector

    faces = [f for f in solid.faces() if f.geom_type == GeomType.PLANE]
    total_area = sum(f.area for f in solid.faces())
    min_area = max(4 * wall * wall, 0.001 * total_area)
    skin = None
    used = 0
    for f in faces:
        if f.area < min_area:
            continue
        n = f.normal_at()
        n = n / n.length
        origin = f.center()
        plane = Plane(origin=origin, x_dir=perp_axis(n), z_dir=n)
        local_pts = [plane.to_local_coords(Vector(v.X, v.Y, v.Z)) for v in f.vertices()]
        x0, x1 = min(p.X for p in local_pts), max(p.X for p in local_pts)
        y0, y1 = min(p.Y for p in local_pts), max(p.Y for p in local_pts)
        pad = max(wall * 2, 1.0)
        box = box_local(x0 - pad, x1 + pad, y0 - pad, y1 + pad, -wall, 0.0, plane)
        plate = inter(solid, box)
        if plate is None or volume(plate) < VOL_TOL:
            continue
        skin = fuse(skin, plate)
        used += 1
    if skin is None or volume(skin) < VOL_TOL:
        raise RuntimeError(f"{name or 'solid'}: found no outer face large enough (>= {min_area:.0f} mm2) to "
                           f"seed a wall plate at {wall:.2f} mm; try a smaller --wall")
    log(f"  {name or 'solid'}: filled body (no plate structure), took a {wall:.2f} mm wall slab from "
        f"{used} outer face(s), every outer face kept as a wall -> volume {solid.volume:.0f} -> {volume(skin):.0f} mm3")
    return skin


def prepare_solids(solids, wall, facet_chord=0.5, facet_angle=30.0, shell_ratio=2.5,
                   do_facet=True, do_shell=True, log=print):
    """Facet curved solids and auto-shell filled ones so extract_plates only ever sees plate-shaped,
    all-planar input. Returns (solids, notes)."""
    out, notes = [], []
    for i, s in enumerate(solids):
        name = f"solid {i}"
        cur = s
        if do_shell and equivalent_thickness(cur) > shell_ratio * wall:
            try:
                cur = auto_shell(cur, wall, log=log, name=name)
                notes.append(f"{name}: filled body, auto-shelled to a {wall:.2f} mm wall by taking a slab from "
                            f"every outer face (no face removed, cutouts stay open)")
            except Exception as ex:
                log(f"  {name}: could not auto-shell ({ex}); left as its original solid geometry")
                notes.append(f"{name}: WARNING: could not auto-shell this filled body ({ex}); it will show up "
                            f"as unrecognised leftover geometry rather than a kit -- try --wall with a "
                            f"different thickness, or shell it by hand in CAD")
        if do_facet and needs_facet(cur):
            n_curved = sum(1 for f in cur.faces() if f.geom_type != GeomType.PLANE)
            frac = curved_area_fraction(cur)
            try:
                faceted = facet_solid(cur, chord_tol=facet_chord, angle_deg=facet_angle, log=log, name=name)
                cur = faceted
                notes.append(f"{name}: {n_curved} curved face(s) ({100*frac:.0f}% of surface area) approximated "
                            f"as flat facets (chord {facet_chord:.2f} mm, angle {facet_angle:.0f} deg)")
            except Exception as ex:
                log(f"  {name}: could not facet ({ex}); left with its original curved faces")
                notes.append(f"{name}: WARNING: could not facet this solid's curved surfaces ({ex}); they will "
                            f"show up as unrecognised leftover geometry rather than plates -- try a coarser "
                            f"--facet-chord")
        elif do_facet and has_curved_faces(cur):
            notes.append(f"{name}: has a small curved feature (< 5% of surface area) that was left as is; "
                        f"it will show up as unrecognised leftover geometry rather than a plate")
        out.append(cur)
    return out, notes
