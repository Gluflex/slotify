"""Simple deterministic sheet nesting: bounding-box rows with optional 90-degree rotation, several sheets."""
from __future__ import annotations

from dataclasses import dataclass

from shapely.affinity import rotate, translate


@dataclass
class Placement:
    part: object
    sheet: int
    rot: int            # 0 or 90
    dx: float
    dy: float
    geom: object        # placed kerf outline (shapely)
    geom_nominal: object


def nest(parts, sheet_w=600.0, sheet_h=900.0, gap=5.0, margin=10.0, log=print):
    """Shelf packing, tallest-first, choose the rotation that keeps rows flatter. Groups by thickness."""
    placements = []
    by_T = {}
    for p in parts:
        by_T.setdefault(round(p.T, 2), []).append(p)
    sheet_no = 0
    for T, group in sorted(by_T.items()):
        items = []
        for p in group:
            b = p.cut.bounds
            w, h = b[2] - b[0], b[3] - b[1]
            # prefer the orientation whose width fits and whose height is smaller
            if h > w and w <= sheet_w - 2 * margin:
                rot = 90 if (w <= sheet_h - 2 * margin and h <= sheet_w - 2 * margin) else 0
            else:
                rot = 0
            if rot:
                w, h = h, w
            if w > sheet_w - 2 * margin and h <= sheet_w - 2 * margin:
                rot, w, h = (90 if not rot else 0), h, w
            items.append((h, w, rot, p))
        items.sort(key=lambda x: (-x[0], -x[1]))
        x, y, row_h = margin, margin, 0.0
        for h, w, rot, p in items:
            if w > sheet_w - 2 * margin or h > sheet_h - 2 * margin:
                p.notes.append(f"part {w:.0f}x{h:.0f} exceeds the sheet {sheet_w:.0f}x{sheet_h:.0f}")
            if x + w > sheet_w - margin:
                x, y, row_h = margin, y + row_h + gap, 0.0
            if y + h > sheet_h - margin:
                sheet_no += 1
                x, y, row_h = margin, margin, 0.0
            rc = rotate(p.cut, 90, origin=(0, 0)) if rot else p.cut
            rn = rotate(p.poly, 90, origin=(0, 0)) if rot else p.poly
            ox, oy = x - rc.bounds[0], y - rc.bounds[1]
            placements.append(Placement(p, sheet_no, rot, ox, oy, translate(rc, ox, oy), translate(rn, ox, oy)))
            x += w + gap
            row_h = max(row_h, h)
        sheet_no += 1
    used = {}
    for pl in placements:
        b = pl.geom.bounds
        u = used.setdefault(pl.sheet, [0, 0])
        u[0], u[1] = max(u[0], b[2]), max(u[1], b[3])
    net = sum(pl.part.cut.area for pl in placements)
    log(f"nested {len(placements)} parts on {sheet_no} sheet(s) of {sheet_w:.0f} x {sheet_h:.0f} mm; "
        f"net part area {net/100:.0f} cm2 = {100*net/(sheet_no*sheet_w*sheet_h):.0f}% of the sheets")
    return placements, sheet_no
