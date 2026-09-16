"""Outputs: nested DXF, SVG preview per sheet, PNG previews (plates grid, 3D assembly), STEP of the kit, JSON report."""
from __future__ import annotations

import json
import math

import ezdxf
import numpy as np
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point
from shapely.ops import substring

LAYERS = {"CUT_OUTER": 7, "CUT_INNER": 5, "ETCH": 3, "SHEET": 1, "ANNOTATION": 1}


def _ring(coords):
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _ring_with_tabs(coords, n_tabs, tab_len):
    """Split a closed exterior ring into `n_tabs` open cut segments, leaving a small uncut bridge (a
    waste tab) at each gap so the part stays attached to the surrounding scrap sheet instead of coming
    fully loose mid-cut -- it can drop through the laser bed's slats or shift out of registration before
    the job finishes. Falls back to one uninterrupted closed loop if the perimeter is too small to fit
    the requested tabs without them nearly touching."""
    pts = _ring(coords)
    if n_tabs <= 0 or len(pts) < 3:
        return [pts + [pts[0]]]
    ring = LineString(pts + [pts[0]])
    total = ring.length
    step = total / n_tabs
    if step < tab_len * 3:
        return [pts + [pts[0]]]
    # rotate the ring to start right after a gap ends, so segments never need to wrap around the seam
    start = max(0.0, step / 2 - tab_len / 2)
    head, tail = substring(ring, start, total), substring(ring, 0, start)
    rering = LineString(list(head.coords) + list(tail.coords)[1:])
    length = rering.length
    segments = []
    for k in range(n_tabs):
        s0 = min(k * step, length)
        s1 = min((k + 1) * step - tab_len, length)
        if s1 <= s0:
            continue
        pc = list(substring(rering, s0, s1).coords)
        if len(pc) >= 2:
            segments.append(pc)
    return segments or [pts + [pts[0]]]


def write_dxf(placements, sheet_w, sheet_h, n_sheets, path, label=True, sheet_gap=50.0, waste_tabs=0,
             waste_tab_len=2.0, decorations=None):
    """decorations: optional {part_name: {"lines": [[(x,y),...],...], "text": str|None,
    "text_pos": (x,y), "text_size": float}} in the part's own local (pre-placement) frame -- placed with
    the same rotate/translate already applied to the part's own outline, so it lands correctly once
    nested. Lines are etched (ETCH layer), never cut."""
    decorations = decorations or {}
    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for name, color in LAYERS.items():
        doc.layers.add(name, color=color)
    for k in range(n_sheets):
        ox = k * (sheet_w + sheet_gap)
        msp.add_lwpolyline([(ox, 0), (ox + sheet_w, 0), (ox + sheet_w, sheet_h), (ox, sheet_h)], close=True,
                           dxfattribs={"layer": "SHEET"})
    for pl in placements:
        ox = pl.sheet * (sheet_w + sheet_gap)
        g = pl.geom
        if waste_tabs > 0:
            for seg in _ring_with_tabs(g.exterior.coords, waste_tabs, waste_tab_len):
                msp.add_lwpolyline([(x + ox, y) for x, y in seg], close=False, dxfattribs={"layer": "CUT_OUTER"})
        else:
            msp.add_lwpolyline([(x + ox, y) for x, y in _ring(g.exterior.coords)], close=True,
                               dxfattribs={"layer": "CUT_OUTER"})
        for hole in g.interiors:
            msp.add_lwpolyline([(x + ox, y) for x, y in _ring(hole.coords)], close=True,
                               dxfattribs={"layer": "CUT_INNER"})
        if label:
            c = g.representative_point()
            h = max(3.0, min(6.0, (g.bounds[3] - g.bounds[1]) / 8))
            msp.add_text(pl.part.name, height=h, dxfattribs={"layer": "ETCH"}).set_placement((c.x + ox - h, c.y))
        deco = decorations.get(pl.part.name)
        if deco:
            def place(geom):
                if pl.rot:
                    geom = rotate(geom, 90, origin=(0, 0))
                return translate(geom, pl.dx, pl.dy)
            for line in deco.get("lines", []):
                pts = list(place(LineString(line)).coords)
                msp.add_lwpolyline([(x + ox, y) for x, y in pts], close=False, dxfattribs={"layer": "ETCH"})
            if deco.get("text"):
                tp = place(Point(deco.get("text_pos", (0, 0))))
                th = deco.get("text_size", 8.0)
                msp.add_text(deco["text"], height=th, dxfattribs={"layer": "ETCH"}).set_placement(
                    (tp.x + ox - th * 0.3 * len(deco["text"]), tp.y))
    doc.saveas(path)
    return path


def write_svg(placements, sheet_w, sheet_h, sheet_no, path, thin_marks=True):
    """One sheet as SVG (mm units), kerf outline in black, nominal in grey, labels, thin spots in red."""
    pad = 5
    W, H = sheet_w + 2 * pad, sheet_h + 2 * pad
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}">',
           f'<rect x="{pad}" y="{pad}" width="{sheet_w}" height="{sheet_h}" fill="#fbf7ef" stroke="#c9b58c" stroke-width="0.4"/>']

    def path_d(poly):
        d = ""
        for ring in [poly.exterior] + list(poly.interiors):
            pts = _ring(ring.coords)
            d += "M" + " L".join(f"{pad + x:.3f},{pad + sheet_h - y:.3f}" for x, y in pts) + " Z "
        return d

    for pl in placements:
        if pl.sheet != sheet_no:
            continue
        out.append(f'<path d="{path_d(pl.geom_nominal)}" fill="#e8dcc3" stroke="#9a8a6a" stroke-width="0.2" fill-rule="evenodd"/>')
        out.append(f'<path d="{path_d(pl.geom)}" fill="none" stroke="#000" stroke-width="0.3" fill-rule="evenodd"/>')
        c = pl.geom.representative_point()
        out.append(f'<text x="{pad + c.x:.2f}" y="{pad + sheet_h - c.y:.2f}" font-size="6" font-family="sans-serif" '
                   f'text-anchor="middle" fill="#333">{pl.part.name}</text>')
        if thin_marks:
            for area, (tx, ty) in pl.part.thin:
                # thin spot coordinates are in the part frame; map through the placement
                from shapely.geometry import Point
                from shapely.affinity import rotate, translate
                pt = Point(tx, ty)
                if pl.rot:
                    pt = rotate(pt, 90, origin=(0, 0))
                pt = translate(pt, pl.dx, pl.dy)
                out.append(f'<circle cx="{pad + pt.x:.2f}" cy="{pad + sheet_h - pt.y:.2f}" r="4" fill="none" stroke="#d00" stroke-width="0.6"/>')
    out.append("</svg>")
    open(path, "w", encoding="utf-8").write("\n".join(out))
    return path


def render_plates_png(parts, path, cols=4):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = len(parts)
    rows = max(1, math.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 4.2 * rows))
    axes = np.atleast_1d(axes).ravel()
    for ax in axes:
        ax.axis("off")
    for ax, fp in zip(axes, parts):
        poly = fp.poly
        x, y = poly.exterior.xy
        ax.fill(x, y, color="#e8dcc3", ec="#4a3a20", lw=0.8)
        for hole in poly.interiors:
            hx, hy = hole.xy
            ax.fill(hx, hy, color="white", ec="#4a3a20", lw=0.8)
        for area, (tx, ty) in fp.thin:
            ax.plot(tx, ty, "o", ms=12, mfc="none", mec="red", mew=1.5)
        b = poly.bounds
        ax.set_title(f"{fp.name}  {b[2]-b[0]:.1f} x {b[3]-b[1]:.1f} mm" + ("  !" if fp.thin else ""), fontsize=9)
        ax.set_aspect("equal")
        ax.axis("on")
        ax.set_xticks([])
        ax.set_yticks([])
    plt.tight_layout()
    plt.savefig(path, dpi=90)
    plt.close(fig)
    return path


def _shaded(V, tris, rgba, light=(0.4, -0.6, 0.7)):
    """Flat per-triangle Lambert shading with a fixed light direction."""
    L = np.array(light, dtype=float)
    L /= np.linalg.norm(L)
    out = []
    base = np.array(rgba[:3])
    for t in tris:
        a, b, c = V[t[0]], V[t[1]], V[t[2]]
        n = np.cross(b - a, c - a)
        ln = np.linalg.norm(n)
        k = abs(np.dot(n / ln, L)) if ln > 1e-12 else 0.5
        out.append((*np.clip(base * (0.55 + 0.45 * k), 0, 1), 1.0))
    return out


def render_assembly_png(plates, path, explode=0.0, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.colors
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    cols = plt.cm.tab20(np.linspace(0, 1, max(2, len(plates))))
    fig = plt.figure(figsize=(16, 8))
    allv = []
    meshes = []
    centre = None
    if explode:
        pts = np.array([[v.X, v.Y, v.Z] for p in plates for v in p.solid.vertices()])
        centre = pts.mean(axis=0)
    for i, p in enumerate(plates):
        verts, tris = p.solid.tessellate(0.5, 0.3)
        if not verts or not tris:
            continue
        V = np.array([[v.X, v.Y, v.Z] for v in verts])
        if explode:
            c = V.mean(axis=0)
            V = V + (c - centre) * explode
        allv.append(V)
        meshes.append((V, tris, cols[i]))
    if not allv:
        return None
    allv = np.vstack(allv)
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    span = (hi - lo).max()
    mid = (hi + lo) / 2
    for k, (el, az, ttl) in enumerate([(28, -55, "front-left"), (28, 125, "back-right")]):
        ax = fig.add_subplot(1, 2, k + 1, projection="3d")
        for V, tris, col in meshes:
            polys = [V[list(t)] for t in tris]
            fc = _shaded(V, tris, matplotlib.colors.to_rgba(col))
            pc = Poly3DCollection(polys, facecolors=fc, edgecolors="none")
            ax.add_collection3d(pc)
        ax.set_xlim(mid[0] - span / 2, mid[0] + span / 2)
        ax.set_ylim(mid[1] - span / 2, mid[1] + span / 2)
        ax.set_zlim(mid[2] - span / 2, mid[2] + span / 2)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev=el, azim=az)
        ax.set_title(f"{title} {ttl}")
    plt.tight_layout()
    plt.savefig(path, dpi=80)
    plt.close(fig)
    return path


def write_step(plates, path):
    from build123d import Compound, export_step
    comp = Compound([p.solid for p in plates])
    export_step(comp, path)
    return path


def write_report(report, path):
    open(path, "w", encoding="utf-8").write(json.dumps(report, indent=2, default=str))
    return path
