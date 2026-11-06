"""Graphics layers on upper video tracks (`nle-bridge/layers@1`, roadmap step 14, ADR-010).

A plan (`nle-bridge/timeline@1`) may list graphics layers rendered on their own with an alpha channel (a title, a
lower third, a CG element) under `layers`: manifest paths, manifests, or layer entries, mixed. Each layer goes on its
own video track above the plan's picture tracks, in the order given (the first is the lowest), so an editor can
retime, move or replace it in the NLE:

    {"schema": "nle-bridge/layers@1", "producer": "motion-graphics-skill",
     "layers": [{"id": "lt1", "type": "lower_third", "file": "layers/lt1.mov", "start": 2.0, "end": 6.5,
                 "box": {"x": 96, "y": 820, "width": 900, "height": 160}, "width": 1920, "height": 1080,
                 "alpha": "straight"}]}

- `id` (or `element_id`), `file` (or `path`; relative to the manifest), `start` / `end` in programme seconds (or
  `start_frame` / `end_frame`), optional `source_in` (seconds into the file, default 0), `type`, `name`.
- `box` ({x, y, width | w, height | h}: where the graphic sits in the frame, pixels from the top left) or `position`
  ({x, y}); the file's own `width` / `height`, `fps`, `duration`; `alpha` (straight | premultiplied), recorded only.
- A file the size of the frame (the usual shape: "rendered alone on transparent with the same timing") needs no
  placement. A smaller file would need one, and neither OTIO nor this skill's FCP7 XML carries a position: it is
  placed at the centre by the NLE, and LAYER_POSITION_NOT_EXPORTED says so (the box is kept in OTIO metadata and in
  relink.json).
"""
from __future__ import annotations

import json
import os
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .model import NleBridgeError, base_name, rate_of, resolve_path, warning
from .timecode import TimecodeError, seconds_to_frames

LAYERS_SCHEMA = "nle-bridge/layers@1"


def _entries(spec: Any, base_dir: Optional[str], where: str) -> List[Dict[str, Any]]:
    """`layers` as written in a plan -> flat layer entries, each with `_base` (the directory its file is relative to)
    and `_manifest` / `_producer`."""
    if isinstance(spec, (str, dict)):
        spec = [spec]
    if not isinstance(spec, list):
        raise NleBridgeError("INVALID_PLAN", f"{where} must be a list of layer manifests or layers")
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(spec):
        w = f"{where}[{i}]"
        if isinstance(s, str):
            path = resolve_path(s, base_dir)
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
            except OSError as e:
                raise NleBridgeError("INPUT_NOT_FOUND", f"{w}: cannot read the layer manifest {path}: {e}")
            except ValueError as e:
                raise NleBridgeError("INVALID_PLAN", f"{w}: the layer manifest {path} is not JSON: {e}")
            out += _from_doc(doc, os.path.dirname(path), base_name(path), w)
        elif isinstance(s, dict):
            out += _from_doc(s, base_dir, None, w)
        else:
            raise NleBridgeError("INVALID_PLAN", f"{w} must be a manifest path, a manifest or a layer")
    return out


def _from_doc(doc: Any, base_dir: Optional[str], manifest: Optional[str], where: str) -> List[Dict[str, Any]]:
    if isinstance(doc, list):
        items, producer = doc, None
    elif isinstance(doc, dict) and isinstance(doc.get("layers"), list):
        if doc.get("schema") not in (None, LAYERS_SCHEMA):
            raise NleBridgeError("INVALID_PLAN", f"{where}: schema {doc.get('schema')!r} is not {LAYERS_SCHEMA}")
        items, producer = doc["layers"], doc.get("producer")
    elif isinstance(doc, dict):  # one layer written inline
        items, producer = [doc], None
    else:
        raise NleBridgeError("INVALID_PLAN", f"{where}: not a layer manifest")
    out = []
    for j, it in enumerate(items):
        if not isinstance(it, dict):
            raise NleBridgeError("INVALID_PLAN", f"{where}.layers[{j}] must be an object")
        out.append(dict(it, _base=base_dir, _manifest=manifest, _producer=it.get("producer") or producer,
                        _where=f"{where}.layers[{j}]" if items is not doc else where))
    return out


def _frames(e: Dict[str, Any], key: str, rate: Fraction, where: str) -> Optional[int]:
    fk = f"{key}_frame"
    try:
        if e.get(fk) is not None:
            v = e[fk]
            if isinstance(v, bool) or not isinstance(v, int):
                raise NleBridgeError("INVALID_PLAN", f"{where}.{fk} must be a whole number of frames")
            return v
        if e.get(key) is not None:
            return seconds_to_frames(e[key], rate)
    except TimecodeError as err:
        raise NleBridgeError("INVALID_PLAN", f"{where}.{key}: {err}")
    return None


def _box(e: Dict[str, Any], where: str) -> Optional[Dict[str, float]]:
    b = e.get("box")
    if b is None and isinstance(e.get("position"), dict):
        b = e["position"]
    if b is None:
        return None
    if not isinstance(b, dict):
        raise NleBridgeError("INVALID_PLAN", f"{where}.box must be an object {{x, y, width, height}}")
    out: Dict[str, float] = {}
    for k, alias in (("x", None), ("y", None), ("width", "w"), ("height", "h")):
        v = b.get(k, b.get(alias) if alias else None)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise NleBridgeError("INVALID_PLAN", f"{where}.box.{k} must be a number of pixels")
        out[k] = v
    if "x" not in out or "y" not in out:
        raise NleBridgeError("INVALID_PLAN", f"{where}.box needs x and y")
    return out


def add_layers(tl: Dict[str, Any], spec: Any, base_dir: Optional[str]) -> Dict[str, Any]:
    """The normalised timeline with each layer on a new video track above its picture tracks (in place; returns it).
    Layer items carry `layer` ({id, type, box, alpha, producer, manifest}); layer files become assets."""
    rate = rate_of(tl)
    entries = _entries(spec, base_dir, "layers")
    if not entries:
        return tl
    seen = {it["id"] for t in tl["tracks"] for it in t["items"]}
    n_video = sum(1 for t in tl["tracks"] if t["kind"] == "video")
    last_video = max((i for i, t in enumerate(tl["tracks"]) if t["kind"] == "video"), default=-1)
    new_tracks = []
    for e in entries:
        where = e["_where"]
        lid = str(e.get("id") or e.get("element_id") or "")
        if not lid:
            raise NleBridgeError("INVALID_PLAN", f"{where}: a layer needs an id")
        iid = f"layer-{lid}"
        if iid in seen:
            raise NleBridgeError("INVALID_PLAN", f"{where}: duplicate layer id {lid!r}")
        seen.add(iid)
        f = e.get("file") or e.get("path")
        if not f:
            raise NleBridgeError("INVALID_PLAN", f"{where}: a layer needs its file")
        path = resolve_path(str(f), e["_base"])
        start, end = _frames(e, "start", rate, where), _frames(e, "end", rate, where)
        if start is None or end is None:
            raise NleBridgeError("INVALID_PLAN", f"{where}: a layer needs start and end (programme seconds) or "
                                                 "start_frame and end_frame")
        if start < 0 or end <= start:
            raise NleBridgeError("INVALID_PLAN", f"{where}: [{start}, {end}) frames is empty or negative")
        s_in = _frames(e, "source_in", rate, where) or 0
        aid = f"layer-{lid}"
        asset: Dict[str, Any] = {"path": path, "name": base_name(path), "fps": float(e["fps"]) if e.get("fps") else None,
                                 "width": int(e["width"]) if e.get("width") else None,
                                 "height": int(e["height"]) if e.get("height") else None, "duration_frames": None,
                                 "start_timecode": None, "has_video": True, "has_audio": False}
        if e.get("duration") is not None:
            try:
                asset["duration_frames"] = seconds_to_frames(e["duration"], rate)
            except TimecodeError as err:
                raise NleBridgeError("INVALID_PLAN", f"{where}.duration: {err}")
            if s_in + end - start > asset["duration_frames"]:
                raise NleBridgeError("INVALID_PLAN", f"{where}: the layer plays {end - start} frames from {s_in}, but "
                                                     f"{asset['name']} is {asset['duration_frames']} frames long")
        if aid in tl["assets"]:
            raise NleBridgeError("INVALID_PLAN", f"{where}: asset id {aid} is taken")
        tl["assets"][aid] = asset
        box = _box(e, where)
        layer = {"id": lid, "type": str(e.get("type") or "graphic"), "box": box,
                 "alpha": e.get("alpha"), "producer": e.get("_producer"), "manifest": e.get("_manifest")}
        n_video += 1
        name = f"V{n_video}"
        new_tracks.append({"kind": "video", "name": name, "items": [
            {"id": iid, "kind": "clip", "asset": aid, "name": str(e.get("name") or lid), "source_in": s_in,
             "source_out": s_in + end - start, "record_in": start, "record_out": end, "enabled": True,
             "layer": {k: v for k, v in layer.items() if v is not None}}]})
        full = (asset["width"], asset["height"]) == (tl.get("width"), tl.get("height"))
        if box and asset["width"] and asset["height"] and tl.get("width") and not full:
            tl["warnings"].append(warning(
                "LAYER_POSITION_NOT_EXPORTED", f"{iid}: {asset['name']} is {asset['width']}x{asset['height']} on a "
                f"{tl['width']}x{tl['height']} frame; its position (x {box['x']:g}, y {box['y']:g}) is not written "
                "(the NLE centres it) and is kept in OTIO metadata and relink.json only", item=iid))
    tl["tracks"][last_video + 1:last_video + 1] = new_tracks
    return tl


def layer_list(tl: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every layer of a timeline: its track, item, file and record range in frames (for manifests and diff)."""
    out = []
    for t in tl["tracks"]:
        for it in t["items"]:
            if it.get("layer"):
                a = tl["assets"].get(it.get("asset") or "") or {}
                out.append(dict(it["layer"], track=t["name"], item=it["id"], file=a.get("path"),
                                record_in=it["record_in"], record_out=it["record_out"], source_in=it["source_in"]))
    return out
