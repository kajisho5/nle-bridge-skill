"""nle-bridge CLI. Every command prints one JSON document with --json.

  nle-bridge export plan.json -o out/ --json                     # out/<name>.otio .xml .edl (+ .srt)
  nle-bridge export project.json --subject programme -o out/ --beats beats.json
  nle-bridge conform plan.json -o pkg/ --zip --json              # timelines + relink.json + manifest.json
  nle-bridge diff plan.json from_editor.otio -o diff.json --beats beats.json --json
  nle-bridge import from_editor.xml -o plan_v2.json              # any timeline as nle-bridge/timeline@1
  nle-bridge contract --json
  nle-bridge doctor --json

Exit codes: 0 done, 2 invalid input or arguments, 3 ffmpeg-skill lacks a tool, 4 ffmpeg-skill failed,
5 --strict and something the plan holds would not be in the files.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .beats import BEAT_SETS, read_grid
from .conform import package
from .contract import ERRORS, LOSS_CODES, contract
from .diff import as_plan, diff
from .engine import EngineError, engine_status, locate, prober
from .export import add_beats, parse_formats, render, write_files
from .model import NleBridgeError, normalise, summary
from .plan import load, load_returned


def _emit(doc: Dict[str, Any], as_json: bool, human: str = "") -> None:
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    elif human:
        print(human)


def _fail(code: str, message: str, as_json: bool, detail: Any = None) -> int:
    doc = {"ok": False, "error": {"code": code, "message": message, "detail": detail}}
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(f"error: {message}" + (f" ({detail})" if detail else ""), file=sys.stderr)
    return ERRORS.get(code, {}).get("exit_code", 1)


def _write_json(path: str, doc: Dict[str, Any], overwrite: bool, inputs: List[str]) -> None:
    if os.path.realpath(path) in {os.path.realpath(p) for p in inputs}:
        raise NleBridgeError("OUTPUT_IS_INPUT", f"{path} is an input of this run")
    if os.path.lexists(path) and not overwrite:
        raise NleBridgeError("OUTPUT_EXISTS", f"refusing to overwrite {path}: pass --overwrite")
    if os.path.lexists(path):
        os.unlink(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _drop(a: argparse.Namespace) -> Optional[bool]:
    return True if getattr(a, "drop_frame", False) else (False if getattr(a, "non_drop_frame", False) else None)


def _prober(a: argparse.Namespace):
    if getattr(a, "no_probe", False):
        return None
    skill = locate(getattr(a, "ffmpeg_skill", None))
    if skill is None:
        if getattr(a, "ffmpeg_skill", None):
            raise NleBridgeError("INVALID_ARGUMENT", f"--ffmpeg-skill {a.ffmpeg_skill}: no ffmpeg-skill there")
        return None
    if "probe" not in skill.tools:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} has no probe")
    return prober(skill, timeout=a.timeout)


def _plan(a: argparse.Namespace, path: str) -> Dict[str, Any]:
    return load(path, rate=a.fps, subject=getattr(a, "subject", None), prober=_prober(a), drop_frame=_drop(a),
                dissolves=getattr(a, "dissolves", False))


def _grid(a: argparse.Namespace) -> Optional[Dict[str, Any]]:
    if not getattr(a, "beats", None):
        return None
    return read_grid(a.beats, beats_per_bar=a.beats_per_bar, beat_set=a.beat_set, offset=a.beat_offset)


def _strict(warns: List[Dict[str, Any]], a: argparse.Namespace) -> None:
    lost = [w for w in warns if w["code"] in LOSS_CODES]
    if getattr(a, "strict", False) and lost:
        raise NleBridgeError("CONTENT_NOT_EXPORTED", f"{len(lost)} thing(s) in the plan would not be in the files "
                             "(--strict): " + "; ".join(w["message"] for w in lost[:5]) + (" ..." if len(lost) > 5 else ""))


def _human_warnings(warns: List[Dict[str, Any]]) -> List[str]:
    return [f"warning [{w['format']}] {w['code']}: {w['message']}" for w in warns]


def cmd_export(a: argparse.Namespace) -> int:
    formats = parse_formats(a.formats)
    tl = _plan(a, a.plan)
    grid = _grid(a)
    tl = add_beats(tl, grid, a.beat_markers)
    _strict(render(tl, formats, a.dissolves)["warnings"], a)
    inputs = [a.plan] + ([a.beats] if a.beats else [])
    res = write_files(tl, a.output, formats, name=a.name, overwrite=a.overwrite, inputs=inputs, dissolves=a.dissolves)
    doc = dict(res, ok=True, plan=os.path.abspath(a.plan),
               beats=_grid_summary(grid) if grid else None)
    lines = [f"{f['format']:5} {f['path']}" for f in res["files"]] + _human_warnings(res["warnings"])
    _emit(doc, a.json, "\n".join(lines))
    return 0


def _grid_summary(grid: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: grid[k] for k in ("source", "bpm", "interval", "beats_per_bar", "offset", "bars_provenance", "beat_set")}
    out.update(beats=len(grid["beats"]), bars=len(grid["bars"]), warnings=grid["warnings"])
    return out


def cmd_conform(a: argparse.Namespace) -> int:
    formats = parse_formats(a.formats)
    pr = _prober(a)
    tl = load(a.plan, rate=a.fps, subject=a.subject, prober=pr, drop_frame=_drop(a), dissolves=a.dissolves)
    grid = _grid(a)
    tl = add_beats(tl, grid, a.beat_markers)
    _strict(render(tl, formats, a.dissolves)["warnings"], a)
    inputs = [a.plan] + ([a.beats] if a.beats else []) + [x["path"] for x in tl["assets"].values()]
    res = package(tl, a.output, formats, name=a.name, copy_media=a.copy_media, make_zip=a.zip, overwrite=a.overwrite,
                  probe=pr, hash_media=not a.no_hash, inputs=inputs,
                  extra={"beats": _grid_summary(grid)} if grid else None, dissolves=a.dissolves)
    doc = dict(res, ok=True, plan=os.path.abspath(a.plan))
    lines = [f"package {res['package']}"] + ([f"zip     {res['zip']['path']}"] if res["zip"] else []) + \
        [f"media   {m['name']}: {'ok' if m['exists'] else 'MISSING'}" for m in res["relink"]["media"]] + \
        _human_warnings(res["warnings"])
    _emit(doc, a.json, "\n".join(lines))
    return 0


def cmd_diff(a: argparse.Namespace) -> int:
    orig = _plan(a, a.plan)
    ret = load_returned(orig, a.returned)
    grid = _grid(a)
    res = diff(orig, ret, grid=grid, beat_tolerance=a.beat_tolerance, returned_path=os.path.abspath(a.returned),
               dissolves=a.dissolves)
    inputs = [a.plan, a.returned] + ([a.beats] if a.beats else [])
    if a.output:
        _write_json(a.output, res, a.overwrite, inputs)
    if a.timeline_out:
        _write_json(a.timeline_out, normalise(as_plan(orig, ret, os.path.abspath(a.returned))), a.overwrite, inputs)
    s = res["summary"]
    lines = [f"clips: {s['clips_unchanged']} unchanged, {s['clips_changed']} changed, {s['clips_added']} added, "
             f"{s['clips_removed']} removed; cuts: {s['cuts_moved']} moved, {s['cuts_added']} added, "
             f"{s['cuts_removed']} removed; markers: +{s['markers_added']} -{s['markers_removed']} ~{s['markers_changed']}"]
    for c in res["cuts"]:
        if c["status"] != "unchanged":
            lines.append(f"  cut {c['status']:8} {c['plan_tc'] or '-':>12} -> {c['returned_tc'] or '-':>12}"
                         + (f"  {c['delta_frames']:+d} f" if c["delta_frames"] is not None else "")
                         + (f"  beat {c['beat']['beat']} {c['beat']['offset_frames']:+g} f" if c.get("beat") else ""))
    if res["beats"]:
        b = res["beats"]
        lines.append(f"beats ({b['bpm']:g} BPM, bars {b['bars_provenance']}): {b['returned_on_beat']} of {b['returned_cuts']} "
                     f"returned cuts on the beat (plan: {b['plan_on_beat']} of {b['plan_cuts']}), "
                     f"{b['moved_off_beat']} moved off it, max {b['max_abs_offset_frames']:g} frames")
    lines += _human_warnings(res["not_imported"])
    _emit(dict(res, ok=True, diff_file=a.output, timeline_file=a.timeline_out), a.json, "\n".join(lines))
    return 0


def cmd_import(a: argparse.Namespace) -> int:
    tl = load(a.file, rate=a.fps, subject=a.subject, drop_frame=_drop(a), dissolves=a.dissolves)
    if a.output:
        _write_json(a.output, tl, a.overwrite, [a.file])
    s = summary(tl)
    _emit(dict(tl, ok=True, summary=s, timeline_file=a.output), a.json,
          "\n".join([f"{s['name']}: {s['clips']} clip(s) on {len(s['tracks'])} track(s), {s['duration']}s at {s['fps']:g} fps, "
                     f"{s['markers']} marker(s)"] + _human_warnings(tl.get("warnings") or [])))
    return 0


def cmd_contract(a: argparse.Namespace) -> int:
    _emit(contract(), True)
    return 0


def otio_status() -> Dict[str, Any]:
    try:
        import opentimelineio as otio  # type: ignore
        return {"found": True, "version": otio.__version__, "licence": "Apache-2.0"}
    except ImportError:
        return {"found": False, "version": None, "install": "pip install 'nle-bridge-skill[otio]'"}


def doctor(explicit: Optional[str] = None) -> Dict[str, Any]:
    eng = engine_status(explicit)
    ot = otio_status()
    notes = []
    if not eng["found"]:
        notes.append("ffmpeg-skill not found: conform takes media facts from the plan only (sha256 and size are still "
                     "measured); pass --ffmpeg-skill DIR or set FFMPEG_SKILL_DIR")
    elif eng["missing_tools"]:
        notes.append("ffmpeg-skill lacks: " + ", ".join(eng["missing_tools"]))
    if not ot["found"]:
        notes.append("opentimelineio not installed: .otio files are written and read without it, but not re-read by "
                     "it as a check; " + ot["install"])
    return {"ok": True, "skill": "nle-bridge-skill", "version": __version__, "python": platform.python_version(),
            "engine": eng, "opentimelineio": ot, "problems": [], "notes": notes}


def cmd_doctor(a: argparse.Namespace) -> int:
    d = doctor(a.ffmpeg_skill)
    _emit(d, a.json, "\n".join(["OK"] + [f"note: {n}" for n in d["notes"]]))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nle-bridge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"nle-bridge-skill {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def plan_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--subject", help="which timeline of an agent project IR (its delivery.timelines[].asset)")
        sp.add_argument("--fps", help="timeline frame rate (25, 29.97, 30000/1001 ...); required for an EDL input")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--drop-frame", action="store_true", help="drop-frame timecode (29.97 / 59.94 only)")
        g.add_argument("--non-drop-frame", action="store_true", help="non-drop-frame timecode")
        sp.add_argument("--beats", help="beat grid JSON (nle-bridge/beatgrid@1, or ffmpeg-skill scenes --beats output)")
        sp.add_argument("--beats-per-bar", type=int, default=None, help="beats per bar when the grid states no bars (default 4)")
        sp.add_argument("--beat-set", choices=BEAT_SETS, default="supported",
                        help="ffmpeg-skill grids: supported beats only (default) or the whole regular grid")
        sp.add_argument("--beat-offset", type=float, default=0.0, help="timeline seconds where the music starts (default 0)")
        sp.add_argument("--dissolves", action="store_true",
                        help="write the plan's transitions as cross dissolves centred on the cut (default: hard cuts, "
                             "each named); diff: the plan was exported with --dissolves")
        sp.add_argument("--json", action="store_true", help="print one JSON document")

    def out_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--formats", help="comma list of otio, fcp7, edl, srt (default: all)")
        sp.add_argument("--name", help="file stem (default: the plan's name)")
        sp.add_argument("--beat-markers", choices=("all", "bars", "none"), default="all",
                        help="with --beats: markers for every beat (default), bar heads only, or none")
        sp.add_argument("--strict", action="store_true", help="exit 5 instead of writing when anything would be dropped")
        sp.add_argument("--overwrite", action="store_true")
        sp.add_argument("--ffmpeg-skill", help="ffmpeg-skill directory (default: see README)")
        sp.add_argument("--no-probe", action="store_true", help="do not ask ffmpeg-skill about the media")
        sp.add_argument("--timeout", type=float, default=600.0, help="seconds per ffmpeg-skill call (default 600)")

    e = sub.add_parser("export", help="plan -> OTIO / FCP7 XML / EDL (+ SRT)")
    e.add_argument("plan")
    e.add_argument("-o", "--output", required=True, help="directory for <name>.otio / .xml / .edl / .srt")
    plan_args(e)
    out_args(e)
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("conform", help="plan -> conform package (timelines, relink manifest, optional media and zip)")
    c.add_argument("plan")
    c.add_argument("-o", "--output", required=True, help="package directory (empty or new)")
    c.add_argument("--copy-media", action="store_true", help="copy the media into <dir>/media and point the timelines there")
    c.add_argument("--zip", action="store_true", help="also write <dir>.zip")
    c.add_argument("--no-hash", action="store_true", help="skip the media sha256 (large media, quick look)")
    plan_args(c)
    out_args(c)
    c.set_defaults(func=cmd_conform)

    d = sub.add_parser("diff", help="the editor's timeline against the plan: clips, cuts, markers, beat offsets")
    d.add_argument("plan")
    d.add_argument("returned", help="the timeline the NLE exported (.otio, FCP7 .xml or .edl)")
    d.add_argument("-o", "--output", help="also write the diff JSON here")
    d.add_argument("--timeline-out", help="write the returned timeline as a new plan (nle-bridge/timeline@1)")
    d.add_argument("--beat-tolerance", type=float, default=0.5, help="frames from a beat that still count as on it (default 0.5)")
    d.add_argument("--overwrite", action="store_true")
    d.add_argument("--ffmpeg-skill", help=argparse.SUPPRESS)
    d.add_argument("--no-probe", action="store_true", help=argparse.SUPPRESS)
    d.add_argument("--timeout", type=float, default=600.0, help=argparse.SUPPRESS)
    plan_args(d)
    d.set_defaults(func=cmd_diff)

    i = sub.add_parser("import", help="any timeline -> nle-bridge/timeline@1")
    i.add_argument("file")
    i.add_argument("-o", "--output", help="also write the timeline JSON here")
    i.add_argument("--subject", help="which timeline of an agent project IR")
    i.add_argument("--fps", help="frame rate (required for an EDL)")
    g = i.add_mutually_exclusive_group()
    g.add_argument("--drop-frame", action="store_true")
    g.add_argument("--non-drop-frame", action="store_true")
    i.add_argument("--dissolves", action="store_true", help="a render project's transition as cross dissolves")
    i.add_argument("--overwrite", action="store_true")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_import)

    k = sub.add_parser("contract", help="machine-readable contract")
    k.add_argument("--json", action="store_true")
    k.set_defaults(func=cmd_contract)

    dr = sub.add_parser("doctor", help="environment check")
    dr.add_argument("--ffmpeg-skill")
    dr.add_argument("--json", action="store_true")
    dr.set_defaults(func=cmd_doctor)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    a = parser().parse_args(argv)
    try:
        return a.func(a)
    except NleBridgeError as e:
        return _fail(e.code, str(e), getattr(a, "json", False), e.detail)
    except EngineError as e:
        return _fail(e.code, str(e), getattr(a, "json", False), e.detail)
    except ValueError as e:  # a rate mismatch between plan and returned timeline, a bad timecode
        return _fail("INVALID_ARGUMENT", str(e), getattr(a, "json", False))


if __name__ == "__main__":
    sys.exit(main())
