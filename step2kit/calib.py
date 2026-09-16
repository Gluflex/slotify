"""Kerf calibration test piece.

The kerf number is the single hardest field in the whole tool to fill in honestly: it depends on the
laser, the power/speed settings and the material, and guessing it wrong makes every finger joint in a
real job either too tight to assemble or too loose to hold.

One shared "KERF TEST" reference plate holds a row of labeled slots, each pre-compensated for a
different candidate kerf value. Separately, one small loose tab is cut per candidate, also labeled and
compensated the same way. Test each tab in turn against the plate's slots by hand -- because the tabs are
independent pieces (not fingers on one rigid comb), a too-tight one never blocks the others from being
tried. Whichever tab seats snugly (not forced, not loose) in its own slot: read its label, that is the
real kerf for --kerf.
"""
from __future__ import annotations

import ezdxf

LAYERS = {"CUT_OUTER": 7, "CUT_INNER": 5, "ETCH": 3}


def kerf_test_dxf(path, tooth_w=6.0, kerf_values=None, log=print):
    kerf_values = kerf_values or [round(0.05 * k, 2) for k in range(0, 7)]   # 0.00 .. 0.30 mm

    pitch = tooth_w * 2.2
    margin = pitch / 2
    slot_h = tooth_w * 1.6
    title_h = 12.0
    label_h = 4.0
    label_gap = 2.0        # clearance between the plate's bottom border and the label text
    label_slot_gap = 1.5   # clearance between the label text and the slot above it
    slot_y0 = label_gap + label_h + label_slot_gap
    plate_w = margin * 2 + pitch * (len(kerf_values) - 1) + tooth_w
    plate_h = title_h + margin + slot_h + slot_y0

    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for name, color in LAYERS.items():
        doc.layers.add(name, color=color)

    # the shared reference plate: one labeled, kerf-compensated slot per candidate value
    msp.add_lwpolyline([(0, 0), (plate_w, 0), (plate_w, plate_h), (0, plate_h)], close=True,
                       dxfattribs={"layer": "CUT_OUTER"})
    msp.add_text("KERF TEST", height=7.0, dxfattribs={"layer": "ETCH"}).set_placement(
        (plate_w / 2 - 22, plate_h - title_h + 2))
    label_y = label_gap + (label_h - 3.2) / 2   # vertically centered within the label band
    for i, k in enumerate(kerf_values):
        cx = margin + i * pitch
        sw = tooth_w - k   # slot drawn narrower by k: the beam grows it back by k when actually cut
        msp.add_lwpolyline([(cx - sw / 2, slot_y0), (cx + sw / 2, slot_y0), (cx + sw / 2, slot_y0 + slot_h),
                           (cx - sw / 2, slot_y0 + slot_h)], close=True, dxfattribs={"layer": "CUT_INNER"})
        msp.add_text(f"{k:.2f}", height=3.2, dxfattribs={"layer": "ETCH"}).set_placement(
            (cx - 5, label_y))

    # separate loose tabs, one per candidate, each its own small independent piece -- so testing one
    # never blocks testing the others, unlike teeth ganged onto a single rigid comb
    tab_w = tooth_w * 1.8
    tab_h = tooth_w * 0.9
    tooth_h = tooth_w * 1.1
    tab_pitch = tab_w + tooth_w
    tab_y0 = -(tab_h + tooth_h + margin)
    for i, k in enumerate(kerf_values):
        ox = i * tab_pitch
        tw = tooth_w + k   # tab drawn wider by k: the beam shrinks it back by k when actually cut
        x0, x1 = ox + (tab_w - tw) / 2, ox + (tab_w + tw) / 2
        pts = [(ox, tab_y0), (ox + tab_w, tab_y0), (ox + tab_w, tab_y0 + tab_h), (x1, tab_y0 + tab_h),
               (x1, tab_y0 + tab_h + tooth_h), (x0, tab_y0 + tab_h + tooth_h), (x0, tab_y0 + tab_h),
               (ox, tab_y0 + tab_h)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "CUT_OUTER"})
        msp.add_text(f"{k:.2f}", height=3.2, dxfattribs={"layer": "ETCH"}).set_placement(
            (ox + tab_w / 2 - 6, tab_y0 + tab_h / 2 - 1.5))

    doc.saveas(path)
    log(f"kerf test piece: 1 reference plate ({len(kerf_values)} labeled slots) + {len(kerf_values)} separate "
        f"loose tabs, testing kerf {kerf_values[0]:.2f}-{kerf_values[-1]:.2f} mm -> {path}")
    return path
