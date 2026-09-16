"""Command line: python -m step2kit model.step --out outdir --kerf 0.2
Or, with no STEP file at all: python -m step2kit --box 200x150x80 --out outdir
Or, to find the right --kerf for a material/laser: python -m step2kit --kerf-test out/test.dxf"""
from __future__ import annotations

import argparse
import sys

from .pipeline import Config, run, run_box


def main(argv=None):
    ap = argparse.ArgumentParser(prog="step2kit", description="Slotify (step2kit): STEP -> laser-cut kit (DXF). Deterministic, no AI.")
    ap.add_argument("step", nargs="?", default=None, help="STEP file (omit when using --box or --kerf-test)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--kerf", type=float, default=0.2, help="laser beam width in mm (outline offset = kerf/2)")
    ap.add_argument("--thickness", type=float, default=None, help="sheet thickness override in mm")
    ap.add_argument("--finger", type=float, default=None, help="finger / tab width (default 2 x thickness)")
    ap.add_argument("--finger-min", type=float, default=None, help="minimum finger width (default thickness)")
    ap.add_argument("--play", type=float, default=0.0,
                    help="finger/tab fit slack in mm: narrows tabs only, independent of --kerf, for a looser "
                         "friction fit (0 = as tight as the kerf setting allows)")
    ap.add_argument("--sheet", default="600x900", help="sheet size WxH in mm")
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--margin", type=float, default=10.0)
    ap.add_argument("--min-feature", type=float, default=1.0)
    ap.add_argument("--waste-tabs", type=int, default=0,
                    help="small uncut bridges per part outline holding it to the scrap sheet (0 = disabled)")
    ap.add_argument("--waste-tab-len", type=float, default=2.0, help="length of each waste tab in mm")
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument("--no-assembly-check", action="store_true")
    ap.add_argument("--wall", type=float, default=None,
                    help="wall thickness for auto-shelling filled (solid) bodies in mm (default: --thickness or 3.0)")
    ap.add_argument("--facet-chord", type=float, default=0.5,
                    help="chord tolerance in mm for approximating curved surfaces as flat facets")
    ap.add_argument("--facet-angle", type=float, default=30.0,
                    help="angular tolerance in degrees for the same; higher = fewer, bigger facets")
    ap.add_argument("--no-auto-shell", action="store_true", help="do not hollow out filled input solids")
    ap.add_argument("--no-auto-facet", action="store_true", help="do not facet curved surfaces (cylinders etc.)")
    ap.add_argument("--box", metavar="WxDxH", default=None,
                    help="generate a parametric box of these outer dimensions in mm instead of reading a STEP "
                         "file, e.g. --box 200x150x80 (uses --wall or --thickness for the wall thickness)")
    ap.add_argument("--lid", choices=("closed", "open", "slip", "slide"), default="closed",
                    help="closed (default), open (no top), slip (open top + a friction-fit flat lid), "
                         "slide (open top + a lid that slides into grooves, pencil-box style)")
    ap.add_argument("--div-x", type=int, default=0, help="number of internal dividers splitting the box along X")
    ap.add_argument("--div-y", type=int, default=0, help="number of internal dividers splitting the box along Y")
    ap.add_argument("--handle", action="store_true", help="cut a hand-hole through the two end walls")
    ap.add_argument("--vent-face", choices=("front", "back", "left", "right", "top", "bottom"), default=None,
                    help="wall to punch a grid of round vent holes into")
    ap.add_argument("--vent-rows", type=int, default=0)
    ap.add_argument("--vent-cols", type=int, default=0)
    ap.add_argument("--vent-hole-d", type=float, default=8.0, help="vent hole diameter in mm")
    ap.add_argument("--engrave-text", default=None, help="text etched onto the box's top/lid")
    ap.add_argument("--engrave-logo", metavar="LOGO.svg", default=None,
                    help="SVG file (straight-line shapes: line/polyline/polygon/rect/circle/path) etched "
                         "onto the box's top/lid")
    ap.add_argument("--pattern", choices=("diagonal", "crosshatch", "hex"), default=None,
                    help="decorative etched pattern fill on the box's top/lid")
    ap.add_argument("--pattern-spacing", type=float, default=8.0, help="spacing of the decorative pattern in mm")
    ap.add_argument("--kerf-test", metavar="OUT.dxf", default=None,
                    help="write a kerf calibration test piece (three square holes at exact nominal sizes, "
                         "no kerf compensation -- measure each with calipers, kerf = measured - nominal) "
                         "and exit, ignoring every other option")
    a = ap.parse_args(argv)

    if a.kerf_test:
        from .calib import kerf_test_dxf
        path = kerf_test_dxf(a.kerf_test)
        print(f"wrote {path}")
        return 0

    if not a.step and not a.box:
        ap.error("either a STEP file, --box, or --kerf-test is required")

    w, h = (float(x) for x in a.sheet.lower().split("x"))
    cfg = Config(kerf=a.kerf, thickness=a.thickness, finger=a.finger, finger_min=a.finger_min, sheet_w=w, sheet_h=h,
                 gap=a.gap, margin=a.margin, min_feature=a.min_feature, labels=not a.no_labels,
                 assembly_check=not a.no_assembly_check, wall=a.wall, facet_chord=a.facet_chord,
                 facet_angle=a.facet_angle, auto_shell=not a.no_auto_shell, auto_facet=not a.no_auto_facet,
                 play=a.play, waste_tabs=a.waste_tabs, waste_tab_len=a.waste_tab_len)

    if a.box:
        bw, bd, bh = (float(x) for x in a.box.lower().split("x"))
        report, *_ = run_box(bw, bd, bh, a.wall or a.thickness or 3.0, a.out, cfg, lid=a.lid,
                             div_x=a.div_x, div_y=a.div_y, handle=a.handle, vent_face=a.vent_face,
                             vent_rows=a.vent_rows, vent_cols=a.vent_cols, vent_hole_d=a.vent_hole_d,
                             engrave_text=a.engrave_text, engrave_logo=a.engrave_logo, pattern=a.pattern,
                             pattern_spacing=a.pattern_spacing)
    else:
        report, *_ = run(a.step, a.out, cfg)
    print("\nWARNINGS:" if report["warnings"] else "\nno warnings")
    for w_ in report["warnings"]:
        print("  -", w_)
    print("files:")
    for k, v in report["files"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
