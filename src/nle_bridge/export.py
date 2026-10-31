"""Export: one normalised timeline -> OTIO / FCP7 XML / CMX 3600 EDL files and an SRT sidecar.

Nothing the plan holds is dropped without a warning. Interchange carries cuts, tracks, markers and (FCP7) audio
levels; effects, speed changes, titles / generators and transitions do not survive it (Velisa ROADMAP, "Premiere
Pro and DaVinci Resolve"), so each one is named in `warnings` with its item id. Format-specific losses (a second
picture track in an EDL, marker colours in FCP7, levels in OTIO / EDL) are added by the writers.

Transitions: written as hard cuts (`TRANSITION_NOT_EXPORTED`) unless `--dissolves` asks for cross dissolves centred
on the cut (transitions.py, ADR-006); then only the ones that cannot be written are named.

Speed: the clip is written at 100 % from its source in point for its planned length on the timeline, so the cut
points stay where the plan put them; the warning says the speed change is the editor's to redo.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional

from . import edl, fcp7, otio_io, srt, transitions
from .beats import grid_markers
from .model import NleBridgeError, rate_of, summary, total_frames, warning
from .timecode import parse_rate

FORMATS = {"otio": ".otio", "fcp7": ".xml", "edl": ".edl", "srt": ".srt"}
DEFAULT_FORMATS = ("otio", "fcp7", "edl", "srt")
WRITERS = {"otio": otio_io.write, "fcp7": fcp7.write, "edl": edl.write}


def parse_formats(spec: Optional[str]) -> List[str]:
    if not spec:
        return list(DEFAULT_FORMATS)
    out = []
    for f in (x.strip().lower() for x in spec.split(",") if x.strip()):
        f = {"xml": "fcp7", "fcpxml7": "fcp7", "cmx3600": "edl"}.get(f, f)
        if f not in FORMATS:
            raise NleBridgeError("INVALID_ARGUMENT", f"unknown format {f!r}: use {', '.join(FORMATS)}")
        if f not in out:
            out.append(f)
    return out


def content_warnings(tl: Dict[str, Any], dissolves: bool = False) -> List[Dict[str, Any]]:
    """What no interchange format carries, per item (transitions: only without `dissolves`; with it,
    transitions.resolve() names the ones it cannot write)."""
    out: List[Dict[str, Any]] = []
    r = rate_of(tl)
    for t in tl["tracks"]:
        for it in t["items"]:
            iid = it["id"]
            if it.get("speed") and abs(float(it["speed"]) - 1.0) > 1e-9:
                out.append(warning("SPEED_NOT_EXPORTED", f"{iid}: speed {float(it['speed']):g}x is not carried; the clip "
                                   f"is written at 100 % from its in point for its planned length "
                                   f"({it['record_out'] - it['record_in']} frames), so the cut points stay put",
                                   item=iid))
            for e in it.get("effects") or []:
                name = e.get("name") or e.get("type") if isinstance(e, dict) else str(e)
                out.append(warning("EFFECT_NOT_EXPORTED", f"{iid}: effect {name} is not carried (keep it in the "
                                   "agent's ffmpeg path or redo it in the NLE)", item=iid))
            if it["kind"] in ("title", "generator"):
                text = f" '{it.get('text')}'" if it.get("text") else ""
                out.append(warning("TITLE_NOT_EXPORTED", f"{iid}: {it['kind']}{text} on {t['name']} is left as a gap "
                                   f"({it['record_out'] - it['record_in']} frames); titles do not survive interchange",
                                   item=iid))
            if it.get("transition_in") and not dissolves:
                tr = it["transition_in"]
                out.append(warning("TRANSITION_NOT_EXPORTED", f"{iid}: {tr['type']} ({tr['frames']} frames) into this "
                                   "clip is written as a hard cut (--dissolves writes cross dissolves)", item=iid))
            a = tl["assets"].get(it.get("asset") or "")
            if a and a.get("fps") and a.get("has_video", True) and t["kind"] == "video" and parse_rate(a["fps"]) != r:
                out.append(warning("RATE_MISMATCH", f"{iid}: {a['name']} is {a['fps']:g} fps on a {float(r):.3f} fps "
                                   "timeline; in / out are counted at the timeline rate (the NLE conforms it)", item=iid))
    return out


def add_beats(tl: Dict[str, Any], grid: Optional[Dict[str, Any]], which: str) -> Dict[str, Any]:
    """The timeline with the beat grid's markers added (bar heads RED, beats CYAN)."""
    if not grid:
        return tl
    out = dict(tl)
    out["markers"] = sorted(list(tl["markers"]) + grid_markers(grid, rate_of(tl), total_frames(tl), which),
                            key=lambda m: (m["at_frame"], m["kind"] != "marker", m["name"]))
    out["warnings"] = list(tl.get("warnings") or []) + list(grid.get("warnings") or [])
    return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render(tl: Dict[str, Any], formats: List[str], dissolves: bool = False) -> Dict[str, Any]:
    """-> {"texts": {fmt: text}, "warnings": [...]} without touching the disk. `dissolves` writes the plan's
    transitions as cross dissolves centred on the cut where they can be."""
    texts: Dict[str, str] = {}
    warns = list(tl.get("warnings") or []) + content_warnings(tl, dissolves)
    dz: Dict[str, Dict[str, Any]] = {}
    if dissolves:
        dz, dw = transitions.resolve(tl)
        warns += dw
    for f in formats:
        if f == "srt":
            if tl.get("captions"):
                texts["srt"] = srt.write(tl["captions"])
            continue
        text, w = WRITERS[f](tl, dz)
        texts[f] = text
        warns += w
    if tl.get("captions") and "srt" not in formats:
        warns.append(warning("CAPTIONS_NOT_EXPORTED", f"{len(tl['captions'])} caption(s) are in the plan but srt is "
                             "not among the formats"))
    return {"texts": texts, "warnings": _dedupe(warns)}


def _dedupe(warns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for w in warns:
        key = (w.get("code"), w.get("format"), w.get("item"), w.get("message"))
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


def write_files(tl: Dict[str, Any], out_dir: str, formats: List[str], *, name: Optional[str] = None,
                overwrite: bool = False, inputs: Optional[List[str]] = None, dissolves: bool = False) -> Dict[str, Any]:
    """Write `<out_dir>/<name>.<ext>` for each format; refuses to replace a file without `overwrite` and never
    writes over an input. Returns the files (path, sha256, independent re-read check) and the warnings."""
    stem = name or tl["name"]
    if not stem or any(c in stem for c in "/\\") or stem in (".", ".."):
        raise NleBridgeError("INVALID_ARGUMENT", f"--name {stem!r} must be a plain file name")
    res = render(tl, formats, dissolves)
    paths = {f: os.path.join(out_dir, stem + FORMATS[f]) for f in res["texts"]}
    ins = {os.path.realpath(p) for p in (inputs or [])}
    for f, p in paths.items():
        if os.path.realpath(p) in ins:
            raise NleBridgeError("OUTPUT_IS_INPUT", f"{p} is an input of this run")
        if os.path.lexists(p) and not overwrite:
            raise NleBridgeError("OUTPUT_EXISTS", f"refusing to overwrite {p}: pass --overwrite")
    os.makedirs(out_dir, exist_ok=True)
    files = []
    for f in formats:
        if f not in res["texts"]:
            continue
        p = paths[f]
        if os.path.lexists(p):
            os.unlink(p)  # never follow a symlink planted at the target
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(res["texts"][f])
        entry: Dict[str, Any] = {"format": f, "path": os.path.abspath(p), "sha256": sha256_file(p)}
        if f == "otio":
            entry["verification"] = otio_io.verify(p)
        elif f == "fcp7":
            entry["verification"] = {"reader": "xml.etree", "ok": fcp7.parse_check(res["texts"][f])}
        files.append(entry)
    return {"files": files, "warnings": res["warnings"], "timeline": summary(tl)}
