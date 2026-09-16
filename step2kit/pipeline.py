"""Orchestration: STEP file in, kit files out. Every step is deterministic geometry."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict, replace

from .slabs import load_step, extract_plates
from .joints import resolve_overlaps, find_joints, apply_ops
from .flatten import flatten_all
from .nest import nest
from . import export
from .geometry import inter, volume, axis_name
from .prep import prepare_solids

# A geometry pipeline built on chained OpenCascade booleans can, for the wrong input, grow one stage's
# memory usage without bound (this has crashed the host machine twice with a single job -- see
# server.py's own JOB_MEMORY_LIMIT_MB comment for the incident). A web server wrapping this pipeline can
# watch and kill the job process from outside, but `python -m step2kit` run directly from the command
# line has no such external guard, so this stage-boundary check is the CLI's own safety net: abort with
# a clear error the moment a stage finishes over budget, rather than let the OS run out of memory later.
MEMORY_ABORT_RSS_MB = 6144            # this process's own usage; a real job stays well under 1 GB
MEMORY_ABORT_FREE_MB = 1024           # or system-wide free memory this low, whichever trips first


def _check_memory():
    try:
        import psutil
        p = psutil.Process()
        rss_mb = p.memory_info().rss / (1024 * 1024)
        free_mb = psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return   # psutil unavailable or a transient OS error: don't block the pipeline on the check itself
    if rss_mb > MEMORY_ABORT_RSS_MB:
        raise MemoryError(f"aborting: this process is using {rss_mb:.0f} MB, over the "
                          f"{MEMORY_ABORT_RSS_MB} MB safety limit -- a geometry stage ran away instead of "
                          f"finishing normally; this input needs a bug report, not a bigger limit")
    if free_mb < MEMORY_ABORT_FREE_MB:
        raise MemoryError(f"aborting: system free memory is down to {free_mb:.0f} MB "
                          f"(floor {MEMORY_ABORT_FREE_MB} MB) -- stopping before the OS runs out")


@dataclass
class Config:
    kerf: float = 0.2            # laser beam width (kerf); each outline is offset by kerf/2
    thickness: float = None      # override sheet thickness (default: detected)
    finger: float = None         # finger/tab width (default: 2 x thickness)
    finger_min: float = None     # minimum finger width (default: thickness)
    sheet_w: float = 600.0
    sheet_h: float = 900.0
    gap: float = 5.0
    margin: float = 10.0
    min_feature: float = 1.0     # flag features narrower than this
    labels: bool = True          # engrave part names (ETCH layer)
    assembly_check: bool = True
    wall: float = None           # wall thickness for auto-shelling filled bodies (default: thickness or 3.0)
    facet_chord: float = 0.5     # mm, chord tolerance for approximating curved faces as flat facets
    facet_angle: float = 30.0    # degrees, angular tolerance for the same (lower = more facets)
    auto_shell: bool = True      # hollow out filled (non-shelled) input solids automatically
    auto_facet: bool = True      # approximate curved faces (cylinders, fillets, ...) as flat facets
    play: float = 0.0            # finger/tab fit slack in mm, narrows tabs only, independent of kerf
    waste_tabs: int = 0          # small uncut bridges per part outline, holding it to the scrap sheet
    waste_tab_len: float = 2.0   # length of each waste tab in mm


def run(step_path: str, out_dir: str, cfg: Config, log=print):
    t0 = time.time()

    def stamp(msg):
        log(f"[{time.time()-t0:6.1f}s] {msg}")
        _check_memory()

    stamp("loading STEP")
    solids = load_step(step_path)
    stamp(f"{len(solids)} solid(s), total volume {sum(s.volume for s in solids):.0f} mm3")
    return _run_pipeline(solids, os.path.basename(step_path), out_dir, cfg, log, t0, stamp)


def run_box(w: float, d: float, h: float, wall: float, out_dir: str, cfg: Config, lid: str = "closed",
           div_x: int = 0, div_y: int = 0, handle: bool = False, vent_face: str = None, vent_rows: int = 0,
           vent_cols: int = 0, vent_hole_d: float = 8.0, engrave_text: str = None, engrave_logo: str = None,
           pattern: str = None, pattern_spacing: float = 8.0, log=print):
    """Same pipeline as `run`, but the input solids are synthesized directly rather than loaded from a
    STEP file -- see boxgen.synth_box. Already a clean single-thickness shell (plus dividers and an
    optional independent lid), so auto-shell/facet preprocessing is skipped and the sheet thickness is
    fixed to the wall thickness used to build it, rather than re-detected. If any of engrave_text /
    engrave_logo / pattern is given, it is etched onto the box's top (lid, if lid is "slip" or "slide")
    once the real plate outline for that part is known."""
    t0 = time.time()

    def stamp(msg):
        log(f"[{time.time()-t0:6.1f}s] {msg}")
        _check_memory()

    stamp("generating box geometry")
    from .boxgen import synth_box
    solids, lid_idx = synth_box(w, d, h, wall, lid=lid, div_x=div_x, div_y=div_y, handle=handle,
                                vent_face=vent_face, vent_rows=vent_rows, vent_cols=vent_cols,
                                vent_hole_d=vent_hole_d, log=log)
    stamp(f"box geometry ready, {len(solids)} part(s), total volume {sum(s.volume for s in solids):.0f} mm3")
    cfg = replace(cfg, auto_shell=False, auto_facet=False, thickness=wall)
    name = f"box_{w:g}x{d:g}x{h:g}_{lid}"
    decorate = None
    if engrave_text or engrave_logo or pattern:
        decorate = {"lid_solid_idx": lid_idx, "text": engrave_text, "logo_path": engrave_logo,
                    "pattern": pattern, "pattern_spacing": pattern_spacing}
    return _run_pipeline(solids, name, out_dir, cfg, log, t0, stamp, decorate=decorate)


def _build_decorations(parts, decorate, log):
    lid_idx = decorate.get("lid_solid_idx")
    target = None
    if lid_idx is not None:
        for fp in parts:
            if fp.plate.source.startswith(f"solid {lid_idx} "):
                target = fp
                break
    else:
        candidates = [fp for fp in parts if fp.plate.n.Z > 0.99]
        if candidates:
            target = max(candidates, key=lambda fp: fp.plate.d_lo)
    if target is None:
        log("  decoration requested but no top/lid plate was found on this box; skipped")
        return None
    b = target.poly.bounds
    w, h = b[2] - b[0], b[3] - b[1]
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    margin = 6.0
    lines = []
    pattern = decorate.get("pattern")
    if pattern:
        from .decorate import pattern_lines
        try:
            pl_lines = pattern_lines(pattern, w - 2 * margin, h - 2 * margin, decorate.get("pattern_spacing", 8.0))
            lines += [[(x + cx, y + cy) for x, y in ln] for ln in pl_lines]
        except Exception as ex:
            log(f"  WARNING: decorative pattern '{pattern}' could not be generated ({ex}); skipped")
    logo_path = decorate.get("logo_path")
    if logo_path:
        from .decorate import svg_lines
        try:
            lg_lines = svg_lines(logo_path, w - 2 * margin, h * 0.5)
            lines += [[(x + cx, y + cy + h * 0.15) for x, y in ln] for ln in lg_lines]
        except Exception as ex:
            log(f"  WARNING: logo SVG could not be imported ({ex}); skipped")
    text = decorate.get("text")
    text_pos = (cx, cy - h * 0.3) if (pattern or logo_path) else (cx, cy)
    log(f"  decorating {target.name}: " + (", ".join(filter(None, [
        f"pattern={pattern}" if pattern else None, "logo" if logo_path else None,
        f"text='{text}'" if text else None])) or "nothing to add"))
    return {target.name: {"lines": lines, "text": text, "text_pos": text_pos, "text_size": min(8.0, h * 0.12)}}


def _run_pipeline(solids, input_name, out_dir, cfg: Config, log, t0, stamp, decorate=None):
    os.makedirs(out_dir, exist_ok=True)
    report = {"input": input_name, "config": asdict(cfg), "plates": [], "joints": [],
              "warnings": [], "checks": {}, "files": {}}

    if cfg.auto_shell or cfg.auto_facet:
        stamp("preparing solids (auto-shell filled bodies, facet curved surfaces)")
        wall = cfg.wall or cfg.thickness or 3.0
        try:
            solids, prep_notes = prepare_solids(solids, wall, facet_chord=cfg.facet_chord,
                                                facet_angle=cfg.facet_angle, do_facet=cfg.auto_facet,
                                                do_shell=cfg.auto_shell, log=log)
        except Exception as ex:
            # prepare_solids already catches per-solid failures; this is a last-resort net so an
            # unexpected error here degrades to "try the model as given" rather than failing the job.
            log(f"  preprocessing failed unexpectedly ({ex}); continuing with the model as uploaded")
            prep_notes = [f"WARNING: solid preparation (auto-shell/facet) failed unexpectedly ({ex}); "
                          f"proceeding with the model as uploaded"]
        report["warnings"].extend(prep_notes)
        report["prep"] = prep_notes
        stamp(f"prepared {len(solids)} solid(s), total volume {sum(s.volume for s in solids):.0f} mm3")

    stamp("extracting plates")
    plates, leftover = extract_plates(solids, thickness_override=cfg.thickness, log=log)
    if leftover:
        report["warnings"].append(f"{sum(v for v, _, _ in leftover):.0f} mm3 of the model is not plate-like and was ignored")
    stamp(f"{len(plates)} plates")

    stamp("resolving slab overlaps")
    plates = resolve_overlaps(plates, log=log)
    stamp(f"{len(plates)} plates after overlap resolution")

    stamp("finding joints")
    joints, ops = find_joints(plates, finger=cfg.finger, finger_min=cfg.finger_min, play=cfg.play, log=log)
    for j in joints:
        log("  " + j.describe())
        report["joints"].append({"a": j.i.name, "b": j.j.name, "kind": j.kind, "angle_deg": round(j.theta_deg, 1),
                                 "piercer": j.piercer.name if j.piercer else None,
                                 "segments": len([s for s in j.segments if j.piercer and s[2] == j.piercer.idx]),
                                 "note": j.note.strip()})
    n_joints = sum(1 for j in joints if j.kind != "NONE")
    stamp(f"{n_joints} joints, {len(joints) - n_joints} interfaces without joint")

    stamp("applying cuts")
    apply_ops(plates, ops, log=log)

    stamp("prismatic check and clean rebuild")
    from .flatten import rebuild_prismatic
    report["checks"]["prismatic"] = rebuild_prismatic(plates, log=log)
    for name, r in report["checks"]["prismatic"].items():
        if not r.get("ok"):
            report["warnings"].append(f"{name}: not a clean prism ({r})")

    stamp("checking collisions")
    worst = 0.0
    for a in range(len(plates)):
        for b in range(a + 1, len(plates)):
            v = volume(inter(plates[a].solid, plates[b].solid))
            worst = max(worst, v)
            if v > 0.01:
                report["warnings"].append(f"plates {plates[a].name} and {plates[b].name} overlap by {v:.2f} mm3")
    report["checks"]["max_overlap_mm3"] = round(worst, 4)
    stamp(f"max plate overlap {worst:.4f} mm3")

    stamp("flattening")
    parts = flatten_all(plates, kerf=cfg.kerf, min_feature=cfg.min_feature, log=log)
    for fp in parts:
        b = fp.poly.bounds
        report["plates"].append({"name": fp.name, "thickness": fp.T, "size_mm": [round(b[2]-b[0], 2), round(b[3]-b[1], 2)],
                                 "holes": len(fp.poly.interiors), "thin_spots": fp.thin,
                                 "normal": axis_name(fp.plate.n), "notes": fp.plate.notes + fp.notes})
        if fp.thin:
            report["warnings"].append(f"{fp.name}: {len(fp.thin)} feature(s) narrower than {cfg.min_feature} mm")
        for n in fp.plate.notes + fp.notes:
            if "WARNING" in n or "PIECES" in n.upper():
                report["warnings"].append(f"{fp.name}: {n}")

    stamp("reconstruction check")
    from .verify import reconstruction_check
    report["checks"]["reconstruction"] = reconstruction_check(parts, log=log)

    if cfg.assembly_check:
        stamp("assembly order")
        from .verify import assembly_order
        report["checks"]["assembly"] = assembly_order(plates, joints, log=log)

    stamp("nesting")
    placements, n_sheets = nest(parts, cfg.sheet_w, cfg.sheet_h, cfg.gap, cfg.margin, log=log)
    report["sheets"] = n_sheets

    stamp("writing files")
    base = os.path.join(out_dir, "kit")
    decorations = _build_decorations(parts, decorate, log) if decorate else None
    report["files"]["dxf"] = export.write_dxf(placements, cfg.sheet_w, cfg.sheet_h, n_sheets, base + ".dxf", label=cfg.labels,
                                              waste_tabs=cfg.waste_tabs, waste_tab_len=cfg.waste_tab_len,
                                              decorations=decorations)
    for k in range(n_sheets):
        report["files"][f"svg_sheet_{k+1}"] = export.write_svg(placements, cfg.sheet_w, cfg.sheet_h, k, f"{base}_sheet{k+1}.svg")
    report["files"]["plates_png"] = export.render_plates_png(parts, base + "_plates.png")
    report["files"]["assembly_png"] = export.render_assembly_png(plates, base + "_assembly.png", title="assembled")
    report["files"]["exploded_png"] = export.render_assembly_png(plates, base + "_exploded.png", explode=0.6, title="exploded")
    report["files"]["step"] = export.write_step(plates, base + ".step")
    report["seconds"] = round(time.time() - t0, 1)
    report["files"]["report"] = export.write_report(report, base + "_report.json")
    stamp("done")
    return report, plates, parts, placements
