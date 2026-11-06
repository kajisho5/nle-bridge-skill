"""Multicam sources for the editor (roadmap step 15, ADR-011).

Input: the JSON ffmpeg-skill `multicam.py --json` prints (read only; scripts/multicam.py):

    {"status": "completed", "output": "edit.mp4", "inputs": ["camA.mp4", "camB.mp4"],
     "offsets_seconds": [0.0, 1.0], "confidence": [1.0, 0.93], "cuts": [[0.0, 2.0, 0], [2.0, 4.0, 1], ...],
     "drift_ppm": [...] (with --fix-drift)}

`offsets_seconds[c]` is where camera c starts on the reference (camera 0) timeline: reference time t is source
time t - offset_c. `cuts` are the ranges rendered, on the reference timeline, naming the camera on screen (after
ffmpeg-skill's own fall-back to camera 0 where a camera had not started). An `--offsets-only` result has no cuts:
camera 0 plays throughout.

The timeline: the programme is the reference timeline. V1 holds the switch list, each item a cut of the camera on
screen, marked `multicam: {"group", "angle"}`; A1 holds camera 0's sound for the whole programme (multicam.py's
default `--audio 0`; its result does not say which source the sound came from). The timeline's `multicam` names
the angles with their offsets in frames. Writers:

- FCP7 XML: each V1 item is a clipitem holding the multiclip (`<multiclip id>` with one `<angle>` per camera, each a
  `<clip>` whose own track places the camera's file at its offset: a camera that starts later begins after a gap,
  one that started earlier begins at its in point); the angle on screen has `<activevideoangle>TRUE`. The
  clipitem's in / out are on the multiclip's (the reference) timebase.
- OTIO: each V1 item is a Stack holding one track per angle (a gap of the offset, then the camera's clip), its
  source_range the part of the multicam played, `metadata.nle_bridge.multicam` the angle on screen.
- EDL: the switched cut only (`MULTICAM_NOT_EXPORTED`).

The readers take either back: the angle on screen becomes a plain clip of its file (so `diff` compares cuts as
always), with `multicam` on the item and the angles with their offsets on the timeline.
"""
from __future__ import annotations

import os
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional

from .model import NleBridgeError, base_name, normalise, resolve_path, warning
from .timecode import TimecodeError, parse_rate, seconds_to_frames

Prober = Callable[[str], Optional[Dict[str, Any]]]
GROUP = "mc1"


def is_multicam_result(doc: Any) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get("offsets_seconds"), list) and isinstance(doc.get("inputs"), list)


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise NleBridgeError("INVALID_PLAN", f"{what} must be a number, got {v!r}")
    return float(v)


def from_result(doc: Dict[str, Any], *, rate: Any, base_dir: Optional[str], prober: Optional[Prober]) -> Dict[str, Any]:
    """An ffmpeg-skill multicam result -> a normalised timeline with `multicam` (see the module docstring)."""
    inputs, offsets = doc["inputs"], doc["offsets_seconds"]
    if len(inputs) < 2 or len(offsets) != len(inputs):
        raise NleBridgeError("INVALID_PLAN", "a multicam result needs two or more inputs and one offset per input")
    if doc.get("status") not in (None, "completed"):
        raise NleBridgeError("INVALID_PLAN", f"the multicam result's status is {doc.get('status')!r}, not completed")
    conf = doc.get("confidence") or [None] * len(inputs)
    warns: List[Dict[str, Any]] = []
    assets: Dict[str, Dict[str, Any]] = {}
    facts: List[Dict[str, Any]] = []
    for i, p in enumerate(inputs):
        path = resolve_path(str(p), base_dir)
        info = (prober(path) or {}) if (prober and os.path.isfile(path)) else {}
        v = info.get("video") or {}
        a = {"path": path, "has_video": bool(v.get("width")) if info else True,
             "has_audio": bool((info.get("audio") or {}).get("channels")) if info else True}
        if v.get("fps"):
            a["fps"] = float(v["fps"])
        if v.get("width") and v.get("height"):
            a["width"], a["height"] = int(v["width"]), int(v["height"])
        if info.get("duration"):
            a["duration"] = float(info["duration"])
        assets[f"cam{i}"] = a
        facts.append(info)
    r = rate if rate is not None else (assets["cam0"].get("fps") or ((doc.get("probe") or {}).get("video") or {}).get("fps"))
    if not r:
        raise NleBridgeError("INVALID_PLAN", "no frame rate: pass --fps, or let the skill probe the cameras through "
                                             "ffmpeg-skill")
    try:
        rr = parse_rate(r)
    except TimecodeError as e:
        raise NleBridgeError("INVALID_PLAN", f"rate: {e}")
    cuts = doc.get("cuts")
    if cuts is None:  # --offsets-only: camera 0 throughout
        if not assets["cam0"].get("duration"):
            raise NleBridgeError("INVALID_PLAN", "an --offsets-only result has no cuts, and the reference's length is "
                                                 "unknown (no ffmpeg-skill to probe it)")
        cuts = [[0.0, assets["cam0"]["duration"], 0]]
    if not isinstance(cuts, list) or not cuts:
        raise NleBridgeError("INVALID_PLAN", "the multicam result's cuts must be a list of [start, end, camera]")
    off_frames = []
    for i, o in enumerate(offsets):
        off_frames.append(seconds_to_frames(Fraction(str(_num(o, f"offsets_seconds[{i}]"))), rr))
    v_items: List[Dict[str, Any]] = []
    end = 0
    for k, c in enumerate(cuts):
        if not isinstance(c, list) or len(c) != 3 or not isinstance(c[2], int) or not 0 <= c[2] < len(inputs):
            raise NleBridgeError("INVALID_PLAN", f"cuts[{k}] must be [start, end, camera] with a camera of the inputs")
        s = seconds_to_frames(Fraction(str(_num(c[0], f"cuts[{k}][0]"))), rr)
        e = seconds_to_frames(Fraction(str(_num(c[1], f"cuts[{k}][1]"))), rr)
        if e <= s:
            continue  # a range shorter than a frame
        cam = c[2]
        src = s - off_frames[cam]
        if src < 0:
            raise NleBridgeError("INVALID_PLAN", f"cuts[{k}]: camera {cam} has not started at {c[0]} s (offset "
                                                 f"{offsets[cam]} s)")
        v_items.append({"id": f"c{k + 1}", "asset": f"cam{cam}", "source_in": src, "source_out": src + e - s,
                        "record_in": s, "record_out": e, "multicam": {"group": GROUP, "angle": cam}})
        end = max(end, e)
    tracks = [{"kind": "video", "name": "V1", "items": v_items}]
    if assets["cam0"]["has_audio"]:
        tracks.append({"kind": "audio", "name": "A1", "items": [
            {"id": "sound", "asset": "cam0", "source_in": 0, "source_out": end, "record_in": 0, "record_out": end}]})
        warns.append(warning("MULTICAM_AUDIO_ASSUMED", "the sound is camera 0's for the whole programme (multicam.py's "
                             "default --audio 0; its result does not say which source the sound came from)"))
    drift = [d for d in (doc.get("drift_ppm") or []) if d]
    if drift:
        warns.append(warning("MULTICAM_DRIFT_NOT_EXPORTED", f"clock drift correction ({', '.join(f'{d:+g}' for d in drift)} "
                             "ppm) is not carried: the angles line up at the start only"))
    stem = os.path.splitext(base_name(str(inputs[0])))[0]
    plan = {"name": f"{stem}_multicam", "rate": r, "assets": assets, "tracks": tracks, "warnings": warns,
            "source": {"format": "ffmpeg-skill multicam result", "output": doc.get("output")}}
    tl = normalise(plan, base_dir=base_dir)
    tl["multicam"] = {"id": GROUP, "name": f"{stem} multicam",
                      "angles": [{"angle": i, "asset": f"cam{i}", "name": tl["assets"][f"cam{i}"]["name"],
                                  "offset_frames": off_frames[i], "offset_seconds": _num(offsets[i], "offset"),
                                  "confidence": conf[i] if i < len(conf) else None} for i in range(len(inputs))]}
    return tl


def check(tl: Dict[str, Any]) -> None:
    """A timeline's `multicam` (from a plan file): angles with an asset of the timeline and a whole-frame offset."""
    mc = tl.get("multicam")
    if mc is None:
        return
    if not isinstance(mc, dict) or not isinstance(mc.get("angles"), list) or len(mc["angles"]) < 2:
        raise NleBridgeError("INVALID_PLAN", "multicam must name two or more angles")
    for i, a in enumerate(mc["angles"]):
        if not isinstance(a, dict) or a.get("asset") not in tl["assets"] or not isinstance(a.get("offset_frames"), int):
            raise NleBridgeError("INVALID_PLAN", f"multicam.angles[{i}] needs an asset of the timeline and offset_frames")


def angle_span(angle: Dict[str, Any], asset: Dict[str, Any], length: int) -> Dict[str, int]:
    """Where an angle's file sits on the multicam timeline [0, length): start (record), in (source frame at start),
    end. A camera that starts later begins after a gap; one that started earlier begins at its in point."""
    off = angle["offset_frames"]
    start = max(0, off)
    s_in = start - off
    dur = asset.get("duration_frames")
    end = min(length, off + dur) if dur else length
    return {"start": start, "in": s_in, "end": max(end, start + 1)}


def multicam_length(tl: Dict[str, Any]) -> int:
    """The multicam's length: the programme's, at least as long as every angle item reaches."""
    mc = tl.get("multicam") or {}
    angles = {a["angle"]: a for a in mc.get("angles") or []}
    n = 0
    for t in tl["tracks"]:
        for it in t["items"]:
            m = it.get("multicam")
            if m and m.get("angle") in angles:
                n = max(n, it["source_in"] + angles[m["angle"]]["offset_frames"] + it["record_out"] - it["record_in"])
    return n
