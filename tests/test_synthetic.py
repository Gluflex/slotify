"""Synthetic end-to-end tests: build small plate models with build123d, run the pipeline, check the joints.
Run:  python tests/test_synthetic.py   (writes to tests/out/<case>/)"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from build123d import Align, Box, Compound, Location, export_step, Plane, Rectangle, extrude, Vector  # noqa: E402

from step2kit.pipeline import Config, run  # noqa: E402

T = 3.0


def box(x0, x1, y0, y1, z0, z1):
    return Box(x1 - x0, y1 - y0, z1 - z0, align=(Align.MIN, Align.MIN, Align.MIN)).moved(Location((x0, y0, z0)))


def case_open_box_plates():
    """Separate plate bodies: floor, four walls standing on the floor (butt contacts), one partition."""
    floor = box(0, 100, 0, 80, 0, T)
    w1 = box(0, T, 0, 80, T, 60)          # x- wall stands on floor, full width
    w2 = box(100 - T, 100, 0, 80, T, 60)  # x+ wall
    w3 = box(T, 100 - T, 0, T, T, 60)     # y- wall between the x walls
    w4 = box(T, 100 - T, 80 - T, 80, T, 60)
    part = box(T, 100 - T, 40, 40 + T, T, 45)   # partition, T-junction to floor, tabs into x walls
    return Compound([floor, w1, w2, w3, w4, part])


def case_shell_open_box():
    """One shelled solid without a lid."""
    outer = box(0, 120, 0, 90, 0, 70)
    inner = box(T, 120 - T, T, 90 - T, T, 80)
    return outer - inner


def case_shell_closed_box():
    outer = box(0, 120, 0, 90, 0, 70)
    inner = box(T, 120 - T, T, 90 - T, T, 70 - T)
    return outer - inner


def case_crosslap():
    a = box(0, 100, 50, 50 + T, 0, 60)
    b = box(50, 50 + T, 0, 100, 0, 60)
    return Compound([a, b])


def case_filled_block():
    """A solid block with no plate structure at all: must be auto-shelled before anything else works."""
    from build123d import Box
    return Box(80, 60, 40)


def case_cylinder():
    """A solid cylinder: filled (needs shelling) AND curved (needs faceting) at once."""
    from build123d import Cylinder
    return Cylinder(30, 70)


def case_house():
    """Floor, two gable walls, two 45-degree roof plates meeting at the ridge (shelled-solid style)."""
    floor = box(0, 100, 0, 80, 0, T)
    g1 = box(0, T, 0, 80, T, 50)
    g2 = box(100 - T, 100, 0, 80, T, 50)
    # roof plates: sketch in the y-z plane, extrude along x
    import math
    L = 40 * math.sqrt(2)
    r1 = extrude(Rectangle(100, L, align=(Align.MIN, Align.MIN)), amount=T)     # placeholder, positioned below
    pl1 = Plane(origin=(0, 0, 50), x_dir=(1, 0, 0), z_dir=(0, -math.sqrt(.5), math.sqrt(.5)))
    pl2 = Plane(origin=(0, 80, 50), x_dir=(1, 0, 0), z_dir=(0, math.sqrt(.5), math.sqrt(.5)))
    r1 = extrude(Rectangle(100, L, align=(Align.MIN, Align.MIN)), amount=-T).moved(pl1.location)
    r2 = extrude(Rectangle(100, L, align=(Align.MIN, Align.MIN)), amount=-T)
    # mirror second roof: local y goes toward the ridge from y=80
    pl2 = Plane(origin=(0, 80, 50), x_dir=(1, 0, 0), y_dir=(0, -math.sqrt(.5), math.sqrt(.5)))
    r2 = r2.moved(pl2.location)
    return Compound([floor, g1, g2, r1, r2])


CASES = {
    "open_box_plates": (case_open_box_plates, {"FINGER": 4, "TAB": 3}, True),
    "shell_open_box": (case_shell_open_box, {"FINGER": 8}, True),
    "shell_closed_box": (case_shell_closed_box, {"FINGER": 12}, True),
    "crosslap": (case_crosslap, {"CROSSLAP": 1}, True),
    "filled_block": (case_filled_block, {"FINGER": 12}, True),  # every outer face becomes a wall: 6 plates, 12 edges
}


def run_case(name, build, expect, assemblable):
    out = os.path.join(HERE, "out", name)
    os.makedirs(out, exist_ok=True)
    step = os.path.join(out, "model.step")
    export_step(build(), step)
    lines = []
    report, plates, parts, placements = run(step, out, Config(kerf=0.2), log=lines.append)
    kinds = {}
    for j in report["joints"]:
        kinds[j["kind"]] = kinds.get(j["kind"], 0) + 1
    problems = []
    for k, n in expect.items():
        if kinds.get(k, 0) < n:
            problems.append(f"expected at least {n} {k} joints, got {kinds.get(k, 0)}")
    if report["checks"]["max_overlap_mm3"] > 0.01:
        problems.append(f"overlap {report['checks']['max_overlap_mm3']}")
    if not report["checks"]["reconstruction"]["ok"]:
        problems.append("reconstruction mismatch")
    asm = report["checks"].get("assembly", {})
    if assemblable and not asm.get("ok"):
        problems.append(f"expected an assembly order, stuck: {asm.get('stuck')}")
    for w in report["warnings"]:
        if "WARNING" in w or "narrower" in w:
            problems.append(w)
    status = "OK " if not problems else "FAIL"
    print(f"{status} {name}: joints={kinds} {report['seconds']}s" + ("" if not problems else "\n   - " + "\n   - ".join(problems)))
    if problems:
        open(os.path.join(out, "log.txt"), "w", encoding="utf-8").write("\n".join(lines))
    return not problems


if __name__ == "__main__":
    sel = sys.argv[1:] or list(CASES)
    ok = True
    for name in sel:
        build, expect, assemblable = CASES[name]
        ok &= run_case(name, build, expect, assemblable)
    sys.exit(0 if ok else 1)
