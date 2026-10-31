"""The editor-neutral timeline every reader produces and every writer consumes (`nle-bridge/timeline@1`).

Every time is a whole frame count at the timeline's exact `rate`:

    {"schema": "nle-bridge/timeline@1", "name": "...", "rate": "24000/1001", "drop_frame": false,
     "start_timecode": "01:00:00:00", "width": 1920, "height": 1080,
     "assets": {"a1": {"path": "/abs/a.mov", "name": "a.mov", "fps": 23.976, "width": 1920, "height": 1080,
                       "duration_frames": 2400, "start_timecode": "10:00:00:00", "has_video": true, "has_audio": true}},
     "tracks": [{"kind": "video", "name": "V1", "items": [
                   {"id": "c1", "asset": "a1", "name": "a.mov", "source_in": 0, "source_out": 48,
                    "record_in": 0, "record_out": 48, "enabled": true}]},
                {"kind": "audio", "name": "A1", "items": [... "gain_db": -6.0 ...]}],
     "markers": [{"at_frame": 24, "duration_frames": 0, "name": "Intro", "comment": "", "color": "GREEN", "kind": "marker"}],
     "captions": [{"start": 0.5, "end": 2.0, "text": "..."}],
     "warnings": [...]}

`source_in` / `source_out` count from the first frame of the media (its start timecode is in the asset), and
`record_in` / `record_out` from the timeline's first frame (shown as `start_timecode`). Out points are exclusive.
An item may carry `transition_in` ({"type", "frames", "before"}: a transition from the clip that ends where it starts,
`before` of its frames before the cut; see normalise_transition and transitions.py).

A plan written by hand or by an agent may give seconds instead of frames: `in` / `out` (source seconds) and
optionally `at` (record seconds) on an item, `at` (seconds) instead of `at_frame` on a marker. Items with no record position follow each other on
their track. `normalise()` turns either form into frames and checks it.
"""
from __future__ import annotations

import copy
import os
import re
from fractions import Fraction
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from .timecode import (DROP_FRAME_RATES, TimecodeError, check_drop_frame, frames_to_timecode, parse_rate, rate_str,
                       seconds_to_frames, timecode_to_frames)

TIMELINE_SCHEMA = "nle-bridge/timeline@1"
DEFAULT_START_TC = "01:00:00:00"
MARKER_COLORS = ("RED", "PINK", "ORANGE", "YELLOW", "GREEN", "CYAN", "BLUE", "PURPLE", "MAGENTA", "BLACK", "WHITE")
DEFAULT_MARKER_COLOR = "GREEN"
MARKER_KINDS = ("marker", "bar", "beat")
ITEM_KINDS = ("clip", "title", "generator")


class NleBridgeError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def warning(code: str, message: str, *, item: Optional[str] = None, fmt: Optional[str] = None) -> Dict[str, Any]:
    """One thing that is not carried (or only partly carried) into a file: never dropped without one of these."""
    return {"code": code, "format": fmt or "all", "item": item, "message": message}


# ------------------------------------------------------------------------------------------------ paths and URLs
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def is_abs(path: str) -> bool:
    return path.startswith("/") or path.startswith("\\\\") or bool(_WIN_ABS.match(path))


def resolve_path(path: str, base_dir: Optional[str]) -> str:
    """An asset path as written in a plan: absolute (POSIX or Windows form) stays as written; relative is taken
    from the plan file's directory."""
    if not path:
        return path
    if path.startswith("file:"):
        return url_to_path(path)
    if is_abs(path):
        return path
    return os.path.abspath(os.path.join(base_dir or os.getcwd(), path))


def path_to_url(path: str, localhost: bool = True) -> str:
    """A percent-encoded file URL for POSIX and Windows paths alike, independent of the OS this runs on:
    file://localhost/... (the form Premiere writes in FCP7 XML) or, with localhost=False, file:///... (the form
    OTIO files and ffmpeg-skill's own export use)."""
    p = path.replace("\\", "/")
    if _WIN_ABS.match(path):
        p = "/" + p
    elif p.startswith("//"):  # UNC \\server\share
        return "file:" + quote(p, safe="/:")
    return ("file://localhost" if localhost else "file://") + quote(p, safe="/:")


def url_to_path(url: str) -> str:
    """The inverse of path_to_url; also reads file:///path, file:/path and a bare path."""
    if not url.startswith("file:"):
        return url
    u = urlparse(url)
    path = unquote(u.path)
    if u.netloc and u.netloc != "localhost":
        return "//" + u.netloc + path  # UNC
    if re.match(r"^/[A-Za-z]:/", path):
        return path[1:]
    return path


def unique_id(iid: str, track: str, seen: set) -> str:
    """An item id that is not yet in `seen` (and add it): a returned file can carry one plan id on several clips,
    e.g. Premiere's per-channel copies of a stereo clip on A1 and A2; the later ones become `<id>@<track>`."""
    out = iid
    n = 1
    while out in seen:
        out = f"{iid}@{track}" + (f"-{n}" if n > 1 else "")
        n += 1
    seen.add(out)
    return out


def base_name(path: str) -> str:
    return re.split(r"[\\/]", path)[-1] if path else ""


# ------------------------------------------------------------------------------------------------ normalise
def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise NleBridgeError("INVALID_PLAN", f"{what} must be a number of frames, got {value!r}")
    if float(value) != int(value):
        raise NleBridgeError("INVALID_PLAN", f"{what} must be a whole number of frames, got {value!r}")
    return int(value)


def _secs(value: Any, what: str, rate: Fraction) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise NleBridgeError("INVALID_PLAN", f"{what} must be seconds, got {value!r}")
    try:
        return seconds_to_frames(value, rate)
    except TimecodeError as e:
        raise NleBridgeError("INVALID_PLAN", f"{what}: {e}")


DEFAULT_TRANSITION_SECONDS = 0.5  # ffmpeg-skill render.py's default transition length


def normalise_transition(value: Any, rate: Fraction, where: str) -> Optional[Dict[str, Any]]:
    """A transition into a clip -> {"type", "frames", "before"} (None for type none or zero length).

    Written as a type name ("dissolve") or an object: {"type", "duration" (seconds, default 0.5 as in ffmpeg-skill) |
    "frames" | "duration_frames", "before"}. `before` is how many of the frames lie before the cut; by default the
    transition is centred on the cut with the odd frame before it (ffmpeg-skill: the incoming clip gives
    d - d // 2 frames of head, the outgoing clip d // 2 frames of tail)."""
    t = {"type": value} if isinstance(value, str) else value
    if not isinstance(t, dict):
        raise NleBridgeError("INVALID_PLAN", f"{where} must be a transition type or an object")
    kind = str(t.get("type") or "dissolve")
    if kind.lower() == "none":
        return None
    if t.get("frames") is not None:
        frames = _int(t["frames"], f"{where}.frames")
    elif t.get("duration_frames") is not None:
        frames = _int(t["duration_frames"], f"{where}.duration_frames")
    else:
        frames = _secs(t.get("duration", DEFAULT_TRANSITION_SECONDS), f"{where}.duration", rate)
    if frames < 0:
        raise NleBridgeError("INVALID_PLAN", f"{where}: a transition cannot be negative")
    if frames == 0:
        return None
    before = _int(t["before"], f"{where}.before") if t.get("before") is not None else frames - frames // 2
    if not 0 <= before <= frames:
        raise NleBridgeError("INVALID_PLAN", f"{where}.before must be between 0 and {frames}")
    return {"type": kind, "frames": frames, "before": before}


def normalise(doc: Dict[str, Any], *, base_dir: Optional[str] = None, rate: Any = None) -> Dict[str, Any]:
    """A timeline document (frames, or seconds as described above) -> a checked `nle-bridge/timeline@1` in frames.
    `rate` overrides or supplies the timeline rate. Raises NleBridgeError(INVALID_PLAN) with the offending path."""
    if not isinstance(doc, dict):
        raise NleBridgeError("INVALID_PLAN", "a timeline must be a JSON object")
    d = copy.deepcopy(doc)
    try:
        r = parse_rate(rate if rate is not None else d.get("rate", d.get("fps")))
    except TimecodeError as e:
        raise NleBridgeError("INVALID_PLAN", f"rate: {e}" if (rate is not None or "rate" in d or "fps" in d)
                             else "no frame rate: give \"rate\" in the plan or --fps")
    drop = bool(d.get("drop_frame", False))
    try:
        check_drop_frame(r, drop)
        start_tc = str(d.get("start_timecode") or DEFAULT_START_TC)
        start_frames = timecode_to_frames(start_tc, r, drop)
        start_tc = frames_to_timecode(start_frames, r, drop)
    except TimecodeError as e:
        raise NleBridgeError("INVALID_PLAN", str(e))
    out: Dict[str, Any] = {"schema": TIMELINE_SCHEMA, "name": str(d.get("name") or "timeline"), "rate": rate_str(r),
                           "drop_frame": drop, "start_timecode": start_tc,
                           "width": int(d["width"]) if d.get("width") else None,
                           "height": int(d["height"]) if d.get("height") else None}
    assets: Dict[str, Dict[str, Any]] = {}
    raw_assets = d.get("assets") or {}
    if isinstance(raw_assets, list):
        raw_assets = {str(a.get("id")): a for a in raw_assets if isinstance(a, dict)}
    if not isinstance(raw_assets, dict):
        raise NleBridgeError("INVALID_PLAN", "assets must be an object keyed by asset id")
    for aid, a in raw_assets.items():
        if not isinstance(a, dict) or not a.get("path"):
            raise NleBridgeError("INVALID_PLAN", f"assets.{aid}: needs a path")
        path = resolve_path(str(a["path"]), base_dir)
        asset = {"path": path, "name": str(a.get("name") or base_name(path)),
                 "fps": float(a["fps"]) if a.get("fps") else None,
                 "width": int(a["width"]) if a.get("width") else None,
                 "height": int(a["height"]) if a.get("height") else None,
                 "duration_frames": None, "start_timecode": a.get("start_timecode") or None,
                 "has_video": bool(a.get("has_video", True)), "has_audio": bool(a.get("has_audio", True))}
        if a.get("duration_frames") is not None:
            asset["duration_frames"] = _int(a["duration_frames"], f"assets.{aid}.duration_frames")
        elif a.get("duration") is not None:
            asset["duration_frames"] = _secs(a["duration"], f"assets.{aid}.duration", r)
        if asset["start_timecode"]:
            try:
                src_rate = parse_rate(asset["fps"]) if asset["fps"] else r
                # the media's timecode is drop-frame when the plan says so, when the label is written with ';',
                # or, unstated, when the media runs at the timeline's rate and the timeline is drop-frame
                src_drop = bool(a["drop_frame"]) if a.get("drop_frame") is not None else (
                    ";" in str(asset["start_timecode"]) or (drop and src_rate == r))
                src_drop = src_drop and src_rate in DROP_FRAME_RATES
                timecode_to_frames(asset["start_timecode"], src_rate, src_drop)
            except TimecodeError as e:
                raise NleBridgeError("INVALID_PLAN", f"assets.{aid}.start_timecode: {e}")
            asset["drop_frame"] = src_drop
        for k in ("sha256", "size_bytes"):
            if a.get(k) is not None:
                asset[k] = a[k]
        assets[str(aid)] = asset
    out["assets"] = assets

    tracks: List[Dict[str, Any]] = []
    seen_ids = set()
    counts = {"video": 0, "audio": 0}
    for ti, t in enumerate(d.get("tracks") or []):
        if not isinstance(t, dict):
            raise NleBridgeError("INVALID_PLAN", f"tracks[{ti}] must be an object")
        kind = str(t.get("kind") or "video").lower()
        if kind not in counts:
            raise NleBridgeError("INVALID_PLAN", f"tracks[{ti}].kind must be video or audio, got {t.get('kind')!r}")
        counts[kind] += 1
        name = str(t.get("name") or f"{'V' if kind == 'video' else 'A'}{counts[kind]}")
        pos = 0
        items: List[Dict[str, Any]] = []
        for ii, it in enumerate(t.get("items") or t.get("clips") or []):
            where = f"tracks[{ti}].items[{ii}]"
            if not isinstance(it, dict):
                raise NleBridgeError("INVALID_PLAN", f"{where} must be an object")
            ik = str(it.get("kind") or "clip")
            if ik not in ITEM_KINDS:
                raise NleBridgeError("INVALID_PLAN", f"{where}.kind must be one of {', '.join(ITEM_KINDS)}")
            aid = it.get("asset")
            if ik == "clip":
                if aid is None or str(aid) not in assets:
                    raise NleBridgeError("INVALID_PLAN", f"{where}.asset {aid!r} is not in assets")
                aid = str(aid)
            if "source_in" in it:
                s_in = _int(it["source_in"], f"{where}.source_in")
                s_out = _int(it["source_out"], f"{where}.source_out") if "source_out" in it else None
            else:
                s_in = _secs(it.get("in", 0), f"{where}.in", r)
                s_out = _secs(it["out"], f"{where}.out", r) if it.get("out") is not None else None
            if "record_in" in it:
                rec_in = _int(it["record_in"], f"{where}.record_in")
            elif it.get("at") is not None:
                rec_in = _secs(it["at"], f"{where}.at", r)
            else:
                rec_in = pos
            if s_out is None:
                if "record_out" in it:
                    s_out = s_in + _int(it["record_out"], f"{where}.record_out") - rec_in
                elif it.get("duration") is not None:
                    s_out = s_in + _secs(it["duration"], f"{where}.duration", r)
                elif ik == "clip" and assets[aid]["duration_frames"]:
                    s_out = assets[aid]["duration_frames"]
                else:
                    raise NleBridgeError("INVALID_PLAN", f"{where}: no out point (out, source_out or duration)")
            if s_in < 0 or s_out <= s_in:
                raise NleBridgeError("INVALID_PLAN", f"{where}: source range [{s_in}, {s_out}) is empty or negative")
            if rec_in < 0:
                raise NleBridgeError("INVALID_PLAN", f"{where}: record position is negative")
            length = s_out - s_in
            speed = float(it.get("speed") or 1.0)
            if not speed > 0:
                raise NleBridgeError("INVALID_PLAN", f"{where}: speed must be positive")
            rec_out = _int(it["record_out"], f"{where}.record_out") if "record_out" in it else \
                rec_in + max(1, int(round(Fraction(length) / Fraction(str(speed)))))
            if rec_out <= rec_in:
                raise NleBridgeError("INVALID_PLAN", f"{where}: record range is empty")
            if rec_out - rec_in != length and abs(speed - 1.0) < 1e-9:
                speed = length / float(rec_out - rec_in)
            iid = str(it.get("id") or f"{name}-{ii + 1}")
            if iid in seen_ids:
                raise NleBridgeError("INVALID_PLAN", f"{where}: duplicate item id {iid!r}")
            seen_ids.add(iid)
            item: Dict[str, Any] = {"id": iid, "kind": ik, "asset": aid,
                                    "name": str(it.get("name") or (assets[aid]["name"] if aid in assets else ik)),
                                    "source_in": s_in, "source_out": s_out, "record_in": rec_in, "record_out": rec_out,
                                    "enabled": bool(it.get("enabled", True))}
            if abs(speed - 1.0) > 1e-9:
                item["speed"] = round(speed, 6)
            if it.get("gain_db") is not None:
                item["gain_db"] = float(it["gain_db"])
            for k in ("effects", "text"):
                if it.get(k):
                    item[k] = it[k]
            if it.get("transition_in"):
                tr = normalise_transition(it["transition_in"], r, f"{where}.transition_in")
                if tr:
                    item["transition_in"] = tr
            items.append(item)
            pos = rec_out
        items.sort(key=lambda x: (x["record_in"], x["record_out"]))
        for a, b in zip(items, items[1:]):
            if b["record_in"] < a["record_out"]:
                raise NleBridgeError("INVALID_PLAN", f"track {name}: {a['id']} and {b['id']} overlap on the record side")
        tracks.append({"kind": kind, "name": name, "items": items})
    if not any(t["items"] for t in tracks):
        raise NleBridgeError("INVALID_PLAN", "the timeline has no clips")
    out["tracks"] = tracks

    markers: List[Dict[str, Any]] = []
    for mi, m in enumerate(d.get("markers") or []):
        where = f"markers[{mi}]"
        if not isinstance(m, dict):
            raise NleBridgeError("INVALID_PLAN", f"{where} must be an object")
        at = _int(m["at_frame"], f"{where}.at_frame") if "at_frame" in m else _secs(m.get("at", 0), f"{where}.at", r)
        color = str(m.get("color") or DEFAULT_MARKER_COLOR).upper()
        if color not in MARKER_COLORS:
            raise NleBridgeError("INVALID_PLAN", f"{where}.color must be one of {', '.join(MARKER_COLORS)}")
        kind = str(m.get("kind") or "marker")
        if kind not in MARKER_KINDS:
            raise NleBridgeError("INVALID_PLAN", f"{where}.kind must be one of {', '.join(MARKER_KINDS)}")
        dur = _int(m.get("duration_frames", 0), f"{where}.duration_frames") if "duration_frames" in m else \
            (_secs(m["duration"], f"{where}.duration", r) if m.get("duration") else 0)
        markers.append({"at_frame": at, "duration_frames": dur, "name": str(m.get("name") or m.get("title") or ""),
                        "comment": str(m.get("comment") or ""), "color": color, "kind": kind})
    markers.sort(key=lambda m: (m["at_frame"], m["kind"] != "marker", m["name"]))
    out["markers"] = markers

    caps: List[Dict[str, Any]] = []
    for ci, c in enumerate(d.get("captions") or []):
        if not isinstance(c, dict) or c.get("start") is None or c.get("end") is None:
            raise NleBridgeError("INVALID_PLAN", f"captions[{ci}] needs start, end and text")
        s, e = float(c["start"]), float(c["end"])
        if e <= s or s < 0:
            raise NleBridgeError("INVALID_PLAN", f"captions[{ci}]: end must be after start, both >= 0")
        caps.append({"start": round(s, 3), "end": round(e, 3), "text": str(c.get("text") or "")})
    out["captions"] = caps
    out["warnings"] = list(d.get("warnings") or [])
    if d.get("source"):
        out["source"] = d["source"]
    return out


# ------------------------------------------------------------------------------------------------ helpers
def rate_of(tl: Dict[str, Any]) -> Fraction:
    return parse_rate(tl["rate"])


def start_frames(tl: Dict[str, Any]) -> int:
    return timecode_to_frames(tl["start_timecode"], rate_of(tl), tl["drop_frame"])


def total_frames(tl: Dict[str, Any]) -> int:
    return max((it["record_out"] for t in tl["tracks"] for it in t["items"]), default=0)


def asset_start_frames(asset: Dict[str, Any], rate: Fraction) -> int:
    """The media's start timecode as a frame count at the timeline rate (0 when unknown)."""
    tc = asset.get("start_timecode")
    if not tc:
        return 0
    src_rate = parse_rate(asset["fps"]) if asset.get("fps") else rate
    frames = timecode_to_frames(tc, src_rate, bool(asset.get("drop_frame", False)))
    return frames if src_rate == rate else seconds_to_frames(Fraction(frames) / src_rate, rate)


def clips(tl: Dict[str, Any], kind: Optional[str] = None) -> List[Dict[str, Any]]:
    return [dict(it, track=t["name"], track_kind=t["kind"]) for t in tl["tracks"] if kind in (None, t["kind"])
            for it in t["items"]]


def summary(tl: Dict[str, Any]) -> Dict[str, Any]:
    r = rate_of(tl)
    total = total_frames(tl)
    return {"name": tl["name"], "rate": tl["rate"], "fps": round(float(r), 6), "drop_frame": tl["drop_frame"],
            "start_timecode": tl["start_timecode"], "duration_frames": total, "duration": round(float(Fraction(total) / r), 3),
            "tracks": [{"kind": t["kind"], "name": t["name"], "items": len(t["items"])} for t in tl["tracks"]],
            "clips": sum(len(t["items"]) for t in tl["tracks"]), "markers": len(tl["markers"]),
            "captions": len(tl.get("captions") or [])}
