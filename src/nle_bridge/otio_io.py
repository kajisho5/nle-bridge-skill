"""OpenTimelineIO JSON (.otio), written and read with the standard library.

The file is OTIO's own serialisation (Timeline.1 / Stack.1 / Track.1 / Clip.2 / Gap.1 / Marker.2 /
ExternalReference.1), the shape ffmpeg-skill `render --export-timeline` writes and both Premiere (OTIO import) and
Resolve read. The opentimelineio package is not needed to write or read it; when it is installed (`[otio]` extra),
`export` re-reads every file with it as a check (verify()).

- Times are RationalTime at the timeline rate. A clip's source_range starts at its media's own timecode (the
  available_range starts at the media's start timecode), as NLEs write it; the reader subtracts it again.
- Markers sit on the timeline's stack with OTIO's colour names; their kind (marker / bar / beat) and every item's
  plan id ride in `metadata.nle_bridge`, which round trip uses as a hint when an NLE keeps it.
- Audio levels have no OTIO schema: a clip's gain_db is metadata only (`LEVEL_METADATA_ONLY` warning).
- With `--dissolves`, a cross dissolve is a Transition.1 (SMPTE_Dissolve) between the two clips: in_offset is the
  part before the cut, out_offset the part after, the clips' source ranges stay their visible parts (as ffmpeg-skill
  writes it). The reader takes an SMPTE_Dissolve between two clips back as the incoming clip's `transition_in`.
"""
from __future__ import annotations

import json
import math
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from .model import (DEFAULT_MARKER_COLOR, MARKER_COLORS, NleBridgeError, asset_start_frames, base_name, path_to_url,
                    rate_of, start_frames, url_to_path, warning)
from .timecode import frames_to_timecode, parse_rate, rate_str, seconds_to_frames
from .transitions import AUDIO_NAME, VIDEO_NAME

META = "nle_bridge"


def _rt(value: int, rate: Fraction) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(rate), "value": float(value)}


def _range(start: int, dur: int, rate: Fraction) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "TimeRange.1", "duration": _rt(dur, rate), "start_time": _rt(start, rate)}


def _gap(dur: int, rate: Fraction) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "Gap.1", "metadata": {}, "name": "", "source_range": _range(0, dur, rate),
            "effects": [], "markers": [], "enabled": True}


def write(tl: Dict[str, Any], dissolves: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """-> (OTIO JSON text, warnings). `dissolves`: transitions.resolve()'s map, written as Transition.1 items."""
    rate = rate_of(tl)
    dissolves = dissolves or {}
    warns: List[Dict[str, Any]] = []
    tracks = []
    for t in tl["tracks"]:
        children: List[Dict[str, Any]] = []
        pos = 0
        for it in t["items"]:
            if it["record_in"] > pos:
                children.append(_gap(it["record_in"] - pos, rate))
            length = it["record_out"] - it["record_in"]
            meta: Dict[str, Any] = {"id": it["id"], "kind": it["kind"]}
            if it.get("gain_db") is not None:
                meta["gain_db"] = it["gain_db"]
                warns.append(warning("LEVEL_METADATA_ONLY", f"{it['id']}: level {it['gain_db']:+g} dB is kept as OTIO "
                                     "metadata only (OTIO has no audio-level schema; NLEs import the clip at 0 dB)",
                                     item=it["id"], fmt="otio"))
            if it["kind"] != "clip":
                children.append(_gap(length, rate))
                pos = it["record_out"]
                continue
            dz = dissolves.get(it["id"])
            if dz:
                children.append({"OTIO_SCHEMA": "Transition.1", "metadata": {META: {"item": it["id"]}},
                                 "name": VIDEO_NAME if t["kind"] == "video" else AUDIO_NAME,
                                 "transition_type": "SMPTE_Dissolve",
                                 "in_offset": _rt(dz["before"], rate), "out_offset": _rt(dz["after"], rate)})
            a = tl["assets"][it["asset"]]
            base = asset_start_frames(a, rate)
            media_ref: Dict[str, Any] = {"OTIO_SCHEMA": "ExternalReference.1", "metadata": {META: {"asset": it["asset"]}},
                                         "name": a["name"], "available_range": None,
                                         "available_image_bounds": None, "target_url": path_to_url(a["path"], localhost=False)}
            if a.get("duration_frames"):
                media_ref["available_range"] = _range(base, a["duration_frames"], rate)
            children.append({"OTIO_SCHEMA": "Clip.2", "metadata": {META: meta}, "name": it["name"],
                             "source_range": _range(base + it["source_in"], length, rate),
                             "effects": [], "markers": [], "enabled": bool(it.get("enabled", True)),
                             "media_references": {"DEFAULT_MEDIA": media_ref},
                             "active_media_reference_key": "DEFAULT_MEDIA"})
            pos = it["record_out"]
        tracks.append({"OTIO_SCHEMA": "Track.1", "metadata": {}, "name": t["name"], "source_range": None,
                       "effects": [], "markers": [], "enabled": True, "children": children,
                       "kind": "Video" if t["kind"] == "video" else "Audio"})
    markers = [{"OTIO_SCHEMA": "Marker.2", "metadata": {META: {"kind": m["kind"]}}, "name": m["name"],
                "color": m["color"], "comment": m.get("comment") or "",
                "marked_range": _range(m["at_frame"], m.get("duration_frames") or 0, rate)} for m in tl["markers"]]
    doc = {"OTIO_SCHEMA": "Timeline.1", "metadata": {META: {"schema": tl["schema"], "rate": tl["rate"],
                                                             "drop_frame": tl["drop_frame"],
                                                             "start_timecode": tl["start_timecode"]}},
           "name": tl["name"], "global_start_time": _rt(start_frames(tl), rate),
           "tracks": {"OTIO_SCHEMA": "Stack.1", "metadata": {}, "name": "tracks", "source_range": None,
                      "effects": [], "markers": markers, "enabled": True, "children": tracks}}
    return json.dumps(doc, indent=4, ensure_ascii=False) + "\n", warns


# ------------------------------------------------------------------------------------------------ read
def _frames(rt: Optional[Dict[str, Any]], rate: Fraction) -> int:
    """A RationalTime at any rate -> whole frames at `rate`."""
    if not rt:
        return 0
    v, r = float(rt.get("value", 0.0)), float(rt.get("rate", float(rate)) or float(rate))
    if abs(r - float(rate)) < 1e-6:
        return int(round(v))
    return seconds_to_frames(Fraction(v).limit_denominator(1000000) / parse_rate(r), rate)


def _schema(o: Any) -> str:
    return str(o.get("OTIO_SCHEMA", "")).split(".")[0] if isinstance(o, dict) else ""


def _first_rate(o: Any) -> Optional[float]:
    if isinstance(o, dict):
        if _schema(o) == "RationalTime" and o.get("rate"):
            return float(o["rate"])
        for k in ("source_range", "global_start_time", "marked_range"):
            r = _first_rate(o.get(k))
            if r:
                return r
        for v in o.values():
            r = _first_rate(v) if isinstance(v, (dict, list)) else None
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _first_rate(v)
            if r:
                return r
    return None


def read(text: str, *, name_hint: str = "timeline", rate: Any = None) -> Dict[str, Any]:
    """OTIO JSON (any NLE's) -> a timeline document in frames (not yet normalised) with `warnings` for what it
    carries that this skill does not take into a plan (effects, speed, transitions, generators, nested stacks)."""
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise NleBridgeError("INVALID_TIMELINE", f"not OTIO JSON: {e}")
    if _schema(doc) == "SerializableCollection":
        doc = next((c for c in doc.get("children") or [] if _schema(c) == "Timeline"), None)
    if _schema(doc) != "Timeline":
        raise NleBridgeError("INVALID_TIMELINE", "the OTIO file holds no Timeline")
    meta = ((doc.get("metadata") or {}).get(META) or {})
    stack = doc.get("tracks") or {}
    r = parse_rate(rate if rate is not None else (meta.get("rate") or _first_rate(doc) or 24))
    drop = bool(meta.get("drop_frame", False))
    if not meta and r in (Fraction(30000, 1001), Fraction(60000, 1001)):
        drop = bool(((doc.get("metadata") or {}).get("Resolve_OTIO") or {}).get("Timecode Drop Frame", False))
    gst = _frames(doc.get("global_start_time"), r) if doc.get("global_start_time") else 0
    warns: List[Dict[str, Any]] = []
    assets: Dict[str, Dict[str, Any]] = {}
    by_url: Dict[str, str] = {}
    tracks: List[Dict[str, Any]] = []
    for t in stack.get("children") or []:
        if _schema(t) != "Track":
            warns.append(warning("NESTED_NOT_IMPORTED", f"a {_schema(t)} directly under the stack is not read", fmt="otio"))
            continue
        kind = "audio" if str(t.get("kind", "Video")).lower() == "audio" else "video"
        items = []
        pos = 0
        pending: Optional[Dict[str, Any]] = None  # an SMPTE_Dissolve waiting for the clip after it
        children = t.get("children") or []
        for ci, c in enumerate(children):
            sch = _schema(c)
            if sch == "Transition":
                nxt = children[ci + 1] if ci + 1 < len(children) else None
                prev_clip = bool(items) and items[-1]["record_out"] == pos and ci > 0 and _schema(children[ci - 1]) == "Clip"
                if c.get("transition_type") == "SMPTE_Dissolve" and prev_clip and _schema(nxt) == "Clip":
                    before = _frames(c.get("in_offset"), r)
                    pending = {"type": "dissolve", "frames": before + _frames(c.get("out_offset"), r), "before": before}
                else:
                    warns.append(warning("TRANSITION_NOT_IMPORTED", f"track {t.get('name')}: transition "
                                         f"{c.get('transition_type') or c.get('name')} is read as a hard cut", fmt="otio"))
                continue
            tr_in, pending = pending, None
            dur = _frames((c.get("source_range") or {}).get("duration"), r) if c.get("source_range") else 0
            if sch == "Gap":
                pos += dur
                continue
            if sch != "Clip":
                warns.append(warning("NESTED_NOT_IMPORTED", f"track {t.get('name')}: a {sch} item is read as a gap",
                                     fmt="otio"))
                pos += dur
                continue
            ref = (c.get("media_references") or {}).get(c.get("active_media_reference_key") or "DEFAULT_MEDIA") \
                if c.get("media_references") else c.get("media_reference")
            ref = ref or {}
            cmeta = ((c.get("metadata") or {}).get(META) or {})
            iid = str(cmeta.get("id") or f"{t.get('name') or kind}-{ci + 1}")
            for e in c.get("effects") or []:
                if _schema(e) in ("LinearTimeWarp", "FreezeFrame", "TimeEffect"):
                    warns.append(warning("SPEED_NOT_IMPORTED", f"{c.get('name')}: speed change "
                                         f"{e.get('time_scalar', 0)}x is not taken into the plan", item=iid, fmt="otio"))
                else:
                    warns.append(warning("EFFECT_NOT_IMPORTED", f"{c.get('name')}: effect {e.get('effect_name') or e.get('name')} "
                                         "is not taken into the plan", item=iid, fmt="otio"))
            if _schema(ref) == "GeneratorReference":
                warns.append(warning("TITLE_NOT_IMPORTED", f"{c.get('name')}: generator / title "
                                     f"({ref.get('generator_kind')}) is read as a gap", item=iid, fmt="otio"))
                if tr_in:
                    warns.append(warning("TRANSITION_NOT_IMPORTED", f"track {t.get('name')}: the dissolve into "
                                         f"{c.get('name')} is not taken into the plan (a generator is read as a gap)",
                                         fmt="otio"))
                pos += dur
                continue
            url = str(ref.get("target_url") or "")
            path = url_to_path(url) if url else ""
            key = path or str(c.get("name") or iid)
            if key not in by_url:
                aid = str(((ref.get("metadata") or {}).get(META) or {}).get("asset") or f"m{len(by_url) + 1}")
                while aid in assets:
                    aid += "_"
                by_url[key] = aid
                avail = ref.get("available_range")
                assets[aid] = {"path": path or key, "name": ref.get("name") or base_name(path) or c.get("name"),
                               "start_frame": _frames(avail.get("start_time"), r) if avail else 0,
                               "duration_frames": _frames(avail.get("duration"), r) if avail else None}
            aid = by_url[key]
            start = _frames(c["source_range"]["start_time"], r) - assets[aid]["start_frame"]
            if start < 0:
                start = _frames(c["source_range"]["start_time"], r)
            items.append({"id": iid, "kind": "clip", "asset": aid, "name": str(c.get("name") or ""),
                          "source_in": start, "source_out": start + dur, "record_in": pos, "record_out": pos + dur,
                          "enabled": bool(c.get("enabled", True)),
                          **({"gain_db": cmeta["gain_db"]} if cmeta.get("gain_db") is not None else {}),
                          **({"transition_in": tr_in} if tr_in and tr_in["frames"] > 0 else {})})
            for m in c.get("markers") or []:
                mr = m.get("marked_range") or {}
                at = pos + _frames(mr.get("start_time"), r) - _frames(c["source_range"]["start_time"], r)
                items[-1].setdefault("_markers", []).append(_marker(m, at, r))
            pos += dur
        tracks.append({"kind": kind, "name": str(t.get("name") or ""), "items": items})
    markers = []
    for m in stack.get("markers") or []:
        markers.append(_marker(m, _frames((m.get("marked_range") or {}).get("start_time"), r), r))
    for t in tracks:  # clip markers become timeline markers at their record position
        for it in t["items"]:
            markers += it.pop("_markers", [])
    out_assets = {}
    for aid, a in assets.items():
        out_assets[aid] = {"path": a["path"], "name": a["name"], "duration_frames": a["duration_frames"],
                           "start_timecode": frames_to_timecode(a["start_frame"], r, False) if a["start_frame"] else None}
    start_tc = meta.get("start_timecode") or frames_to_timecode(gst, r, drop)
    return {"name": str(doc.get("name") or name_hint), "rate": rate_str(r), "drop_frame": drop,
            "start_timecode": start_tc, "assets": out_assets, "tracks": tracks, "markers": markers,
            "warnings": warns, "source": {"format": "otio"}}


def _marker(m: Dict[str, Any], at: int, r: Fraction) -> Dict[str, Any]:
    color = str(m.get("color") or DEFAULT_MARKER_COLOR).upper()
    kind = str(((m.get("metadata") or {}).get(META) or {}).get("kind") or "marker")
    return {"at_frame": max(0, at), "duration_frames": _frames((m.get("marked_range") or {}).get("duration"), r),
            "name": str(m.get("name") or ""), "comment": str(m.get("comment") or ""),
            "color": color if color in MARKER_COLORS else DEFAULT_MARKER_COLOR,
            "kind": kind if kind in ("marker", "bar", "beat") else "marker"}


# ------------------------------------------------------------------------------------------------ verify
def verify(path: str) -> Optional[Dict[str, Any]]:
    """Re-read a written .otio with the opentimelineio package when it is installed; None when it is not."""
    try:
        import opentimelineio as otio  # type: ignore
    except ImportError:
        return None
    try:
        t = otio.adapters.read_from_file(path)
        clips = list(t.find_clips()) if hasattr(t, "find_clips") else list(t.each_clip())
        return {"reader": f"opentimelineio {otio.__version__}", "ok": True, "clips": len(clips),
                "tracks": len(t.tracks), "markers": len(t.tracks.markers),
                "duration_frames": int(round(t.duration().value)) if not math.isnan(t.duration().value) else None}
    except Exception as e:  # noqa: BLE001 -- any failure of the independent reader is the finding
        return {"reader": f"opentimelineio {getattr(otio, '__version__', '?')}", "ok": False, "error": str(e)}
