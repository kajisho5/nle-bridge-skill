"""Every input this skill accepts, as one normalised `nle-bridge/timeline@1`.

- `nle-bridge/timeline@1` (frames, or the seconds form; see model.py): what an agent or a person writes.
- video-production-agent's project IR (`project.json` with `schema_version`, `assets`, `delivery.timelines[]`,
  agent ADR-052): one `timeline.export` record (pick it with `subject` when there are several). Its clips are the
  kept source ranges in programme order; the assets' paths and probe facts come from `assets`; captions from the
  subject's `captions.generate` cues; what the agent's `timeline.export` decision says is not in the timeline
  (`params.not_exported`) becomes a warning.
- ffmpeg-skill's render project (`clips[{src, in, out, speed}]`, `frame`, `chapters`, `audio.music`, ...), the
  same document `render --export-timeline` reads. Chapters become markers, the music bed an audio track; the
  project keys a timeline cannot carry are warnings (the list follows ffmpeg-skill's own `not_exported`).
- An editor timeline: `.otio`, FCP7 `.xml`, CMX 3600 `.edl` (the rate comes from `rate` / the plan).
"""
from __future__ import annotations

import json
import os
import re
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional

from . import edl, fcp7, otio_io, srt
from .model import NleBridgeError, asset_start_frames, base_name, normalise, rate_of, resolve_path, warning
from .timecode import TimecodeError, parse_rate, rate_str

Prober = Callable[[str], Optional[Dict[str, Any]]]

# ffmpeg-skill render project keys an editor timeline cannot carry (its own _NOT_EXPORTED list, same wording)
_RENDER_NOT_EXPORTED = {
    "silence": "the silence cut (render.py measures it at render time; the timeline keeps the clips whole)",
    "graphics": "graphics (titles, lower thirds, social templates)",
    "overlays": "overlays (logos, images, text and video layers)",
    "fit": "fit / reframe to the delivery aspect (set the sequence frame in the editor)",
    "audiogram": "the audiogram picture",
    "loudness": "loudness normalisation",
    "export": "the export preset (the editor's own export decides the delivery)",
    "check": "the platform check",
    "brand": "brand styles",
    "snap": "beat snapping (the timeline uses the project's in/out as written, before any snap)",
}
_RENDER_AUDIO_NOT_EXPORTED = ("replace", "voice", "denoise", "duck", "gain", "stereo", "mono", "downmix", "stereo_widen",
                              "effects", "stems", "music_volume", "fade_in", "fade_out", "music_fade_out", "music_loop")


def _seconds(value: Any, what: str) -> float:
    """seconds, mm:ss or hh:mm:ss(.ms) (the forms a render project uses; hh:mm:ss:ff needs the plan form)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    m = re.match(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$", s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    try:
        return float(s)
    except ValueError:
        raise NleBridgeError("INVALID_PLAN", f"{what}: {value!r} is not seconds, mm:ss or hh:mm:ss.ms")


def _probe_asset(path: str, prober: Optional[Prober]) -> Dict[str, Any]:
    if prober is None:
        return {}
    p = prober(path) or {}
    v = p.get("video") or {}
    out: Dict[str, Any] = {}
    if p.get("duration"):
        out["duration"] = float(p["duration"])
    if v.get("fps"):
        out["fps"] = float(v["fps"])
    if v.get("width") and v.get("height"):
        out["width"], out["height"] = int(v["width"]), int(v["height"])
    out["has_video"] = bool(v.get("width"))
    out["has_audio"] = bool((p.get("audio") or {}).get("channels"))
    return out


def detect_kind(path: str, doc: Any = None) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".otio":
        return "otio"
    if ext == ".xml":
        return "fcp7"
    if ext == ".edl":
        return "edl"
    if ext == ".fcpxml":
        raise NleBridgeError("UNSUPPORTED_FORMAT", "FCPXML (Final Cut Pro X) is not read: export OTIO or FCP7 XML from "
                                                   "the NLE (Premiere has no FCPXML; Resolve writes both)")
    if isinstance(doc, dict):
        if doc.get("OTIO_SCHEMA"):
            return "otio"
        if doc.get("schema") == "nle-bridge/timeline@1" or "tracks" in doc:
            return "native"
        if "schema_version" in doc and ("delivery" in doc or "assets" in doc):
            return "agent"
        if isinstance(doc.get("clips"), list):
            return "render"
    raise NleBridgeError("INVALID_PLAN", f"{path}: not a timeline this skill reads (nle-bridge/timeline@1, the agent's "
                                         "project IR, an ffmpeg-skill render project, .otio, FCP7 .xml or .edl)")


def from_agent(doc: Dict[str, Any], *, subject: Optional[str], rate: Any, base_dir: Optional[str]) -> Dict[str, Any]:
    timelines = ((doc.get("delivery") or {}).get("timelines")) or []
    if not timelines:
        raise NleBridgeError("INVALID_PLAN", "the project IR has no delivery.timelines (the agent plans one when "
                                             "delivery.timeline is otio / fcpxml / edl)")
    if subject:
        rec = next((t for t in timelines if t.get("asset") == subject), None)
        if rec is None:
            raise NleBridgeError("INVALID_PLAN", f"no timeline for subject {subject!r} (have: "
                                                 f"{', '.join(str(t.get('asset')) for t in timelines)})")
    elif len(timelines) == 1:
        rec = timelines[0]
    else:
        raise NleBridgeError("INVALID_ARGUMENT", "the project IR has several timelines: pass --subject "
                                                 + " | ".join(str(t.get("asset")) for t in timelines))
    ir_assets = doc.get("assets") or {}
    assets: Dict[str, Dict[str, Any]] = {}
    for c in rec.get("clips") or []:
        aid = str(c.get("asset"))
        if aid in assets:
            continue
        a = ir_assets.get(aid)
        if not isinstance(a, dict) or not a.get("path"):
            raise NleBridgeError("INVALID_PLAN", f"timeline clip asset {aid!r} is not in the project's assets")
        tech = a.get("technical") or {}
        v, au = tech.get("video") or {}, tech.get("audio") or {}
        assets[aid] = {"path": a["path"], "fps": v.get("fps"), "width": v.get("width"), "height": v.get("height"),
                       "duration": tech.get("duration"), "has_video": bool(v), "has_audio": bool(au),
                       **({"sha256": a["hash"]} if a.get("hash") else {})}
    first = assets[str(rec["clips"][0]["asset"])]
    r = rate if rate is not None else first.get("fps")
    if not r:
        raise NleBridgeError("INVALID_PLAN", "no frame rate: the first clip's asset has no technical.video.fps; pass --fps")
    v_items, a_items = [], []
    for i, c in enumerate(rec.get("clips") or []):
        aid = str(c["asset"])
        v_items.append({"id": f"c{i + 1}", "asset": aid, "in": c["in"], "out": c["out"]})
        if assets[aid]["has_audio"]:
            a_items.append({"id": f"c{i + 1}a", "asset": aid, "in": c["in"], "out": c["out"]})
    tracks = [{"kind": "video", "name": "V1", "items": v_items}]
    if a_items:
        tracks.append({"kind": "audio", "name": "A1", "items": a_items})
    # an audio clip follows its picture clip: same record position even when frames round differently
    warns: List[Dict[str, Any]] = []
    for d in doc.get("decisions") or []:
        if d.get("subject") == "timeline.export" and (d.get("params") or {}).get("asset_id") == rec.get("asset"):
            for what in (d.get("params") or {}).get("not_exported") or []:
                warns.append(warning("NOT_IN_TIMELINE", f"the agent's {what} is not in the timeline (decision {d.get('id')})"))
    caps = []
    for op in ((doc.get("captions") or {}).get("operations") or []):
        if op.get("type") == "captions.generate" and op.get("asset") == rec.get("asset"):
            caps += [{"start": q["start"], "end": q["end"], "text": q.get("text", "")} for q in op.get("cues") or []]
    plan = {"name": f"{base_name(str(doc.get('project', {}).get('id') if isinstance(doc.get('project'), dict) else doc.get('project') or 'timeline'))}_{rec.get('asset')}",
            "rate": r, "assets": assets, "tracks": tracks, "captions": caps, "warnings": warns,
            "source": {"format": "video-production-agent project IR", "subject": rec.get("asset"),
                       "decision_ids": rec.get("decision_ids") or [], "timeline_duration": rec.get("timeline_duration")}}
    tl = normalise(plan, base_dir=base_dir)
    _align_audio(tl)
    return tl


def _align_audio(tl: Dict[str, Any]) -> None:
    """A1 items written as `<id>a` take the record range of their picture item `<id>`."""
    vid = {it["id"]: it for t in tl["tracks"] if t["kind"] == "video" for it in t["items"]}
    for t in tl["tracks"]:
        if t["kind"] != "audio":
            continue
        for it in t["items"]:
            v = vid.get(it["id"][:-1]) if it["id"].endswith("a") else None
            if v:
                it.update(record_in=v["record_in"], record_out=v["record_out"], source_in=v["source_in"],
                          source_out=v["source_out"])


def from_render(doc: Dict[str, Any], *, rate: Any, base_dir: Optional[str], prober: Optional[Prober]) -> Dict[str, Any]:
    clips = doc.get("clips") or []
    if not clips:
        raise NleBridgeError("INVALID_PLAN", "project.clips is empty")
    assets: Dict[str, Dict[str, Any]] = {}
    ids: Dict[str, str] = {}
    warns: List[Dict[str, Any]] = []
    for i, c in enumerate(clips):
        if not isinstance(c, dict) or not c.get("src"):
            raise NleBridgeError("INVALID_PLAN", f"clips[{i}]: no src")
        path = resolve_path(str(c["src"]), base_dir)
        if path not in ids:
            ids[path] = f"a{len(ids) + 1}"
            assets[ids[path]] = dict({"path": path}, **_probe_asset(path, prober))
    frame = doc.get("frame") or {}
    first = assets[ids[resolve_path(str(clips[0]["src"]), base_dir)]]
    r = rate if rate is not None else (frame.get("fps") or first.get("fps"))
    if not r:
        raise NleBridgeError("INVALID_PLAN", "no frame rate: set frame.fps in the project, pass --fps, or let the skill "
                                             "probe the media through ffmpeg-skill")
    v_items, a_items = [], []
    for i, c in enumerate(clips):
        aid = ids[resolve_path(str(c["src"]), base_dir)]
        it: Dict[str, Any] = {"id": f"c{i + 1}", "asset": aid, "in": _seconds(c.get("in", 0), f"clips[{i}].in")}
        if c.get("out") is not None:
            it["out"] = _seconds(c["out"], f"clips[{i}].out")
        elif assets[aid].get("duration") is None:
            raise NleBridgeError("INVALID_PLAN", f"clips[{i}] has no out and its length is unknown (no ffmpeg-skill to "
                                                 "probe it): give out")
        if c.get("speed") not in (None, 1, 1.0):
            it["speed"] = float(c["speed"])
        v_items.append(it)
        if assets[aid].get("has_audio", True):
            a_items.append(dict(it, id=f"c{i + 1}a"))
    tracks = [{"kind": "video", "name": "V1", "items": v_items}]
    if a_items:
        tracks.append({"kind": "audio", "name": "A1", "items": a_items})
    audio = doc.get("audio") or {}
    if audio.get("music"):
        mpath = resolve_path(str(audio["music"]), base_dir)
        info = _probe_asset(mpath, prober)
        if info.get("duration") is None:
            warns.append(warning("NOT_IN_TIMELINE", f"the music bed {base_name(mpath)} is not in the timeline: its length "
                                 "is unknown (no ffmpeg-skill to probe it)"))
        else:
            assets["music"] = dict(info, path=mpath, has_video=False, has_audio=True)
            tracks.append({"kind": "audio", "name": "A2", "items": [{"id": "music", "asset": "music", "in": 0, "at": 0}]})
    trans = doc.get("transition") or {}
    if len(clips) > 1 and str(trans.get("type", "fade")) != "none":
        warns.append(warning("TRANSITION_NOT_EXPORTED", f"the project's {trans.get('type', 'fade')} transition "
                             f"({trans.get('duration', 0.5)} s; render.py's default when none is set) is exported as a "
                             "hard cut, so this timeline is longer than the render"))
    for key, text in _RENDER_NOT_EXPORTED.items():
        if doc.get(key):
            warns.append(warning("NOT_IN_TIMELINE", text))
    dropped = sorted(k for k in _RENDER_AUDIO_NOT_EXPORTED if audio.get(k) not in (None, False, "", 0))
    if dropped:
        warns.append(warning("NOT_IN_TIMELINE", "audio processing: " + ", ".join(dropped)))
    markers = []
    chapters = doc.get("chapters")
    if isinstance(chapters, list):
        for j, ch in enumerate(chapters):
            markers.append({"at": _seconds(ch.get("at", 0), f"chapters[{j}].at"), "name": str(ch.get("title") or ""),
                            "color": "BLUE"})
    caps = []
    if isinstance(doc.get("captions"), str) and doc["captions"].lower().endswith(".srt"):
        cpath = resolve_path(doc["captions"], base_dir)
        try:
            with open(cpath, encoding="utf-8-sig") as fh:
                caps = srt.read(fh.read())
        except OSError as e:
            raise NleBridgeError("INPUT_NOT_FOUND", f"captions {cpath}: {e}")
    elif doc.get("captions"):
        warns.append(warning("NOT_IN_TIMELINE", "captions given in a form other than an .srt path are not carried"))
    plan = {"name": os.path.splitext(base_name(str(doc.get("output") or "timeline")))[0] or "timeline", "rate": r,
            "width": frame.get("width"), "height": frame.get("height"), "assets": assets, "tracks": tracks,
            "markers": markers, "captions": caps, "warnings": warns, "source": {"format": "ffmpeg-skill render project"}}
    tl = normalise(plan, base_dir=base_dir)
    _align_audio(tl)
    music = next((t for t in tl["tracks"] if t["name"] == "A2" and t["kind"] == "audio"), None)
    if music and music["items"]:  # the bed plays once, up to the programme's length (as render.py lays it)
        end = max(it["record_out"] for t in tl["tracks"] if t is not music for it in t["items"])
        m = music["items"][0]
        if m["record_out"] > end:
            m["source_out"] -= m["record_out"] - end
            m["record_out"] = end
    return tl


def load_doc(path: str) -> Any:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as e:
        raise NleBridgeError("INPUT_NOT_FOUND", f"cannot read {path}: {e}")
    return text


def load(path: str, *, rate: Any = None, subject: Optional[str] = None, prober: Optional[Prober] = None,
         start_timecode: Optional[str] = None, asset_starts: Optional[Dict[str, int]] = None,
         drop_frame: Optional[bool] = None) -> Dict[str, Any]:
    """Any accepted input -> normalised timeline. `rate` overrides the input's rate (needed for an EDL)."""
    if not os.path.isfile(path):
        raise NleBridgeError("INPUT_NOT_FOUND", f"no such file: {path}")
    text = load_doc(path)
    base_dir = os.path.dirname(os.path.abspath(path))
    doc: Any = None
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".otio", ".xml", ".edl", ".fcpxml"):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise NleBridgeError("INVALID_PLAN", f"{path} is not JSON: {e}")
    kind = detect_kind(path, doc)
    name = os.path.splitext(base_name(path))[0]
    if rate is not None:
        try:
            rate = rate_str(parse_rate(rate))
        except TimecodeError as e:
            raise NleBridgeError("INVALID_ARGUMENT", f"--fps: {e}")
    if kind == "native":
        if drop_frame is not None:
            doc = dict(doc, drop_frame=drop_frame)
        return normalise(doc, base_dir=base_dir, rate=rate)
    if kind == "agent":
        return _df(from_agent(doc, subject=subject, rate=rate, base_dir=base_dir), drop_frame)
    if kind == "render":
        return _df(from_render(doc, rate=rate, base_dir=base_dir, prober=prober), drop_frame)
    if kind == "otio":
        raw = otio_io.read(text, name_hint=name, rate=rate)
    elif kind == "fcp7":
        raw = fcp7.read(text, name_hint=name)
        if rate is not None and Fraction(parse_rate(rate)) != parse_rate(raw["rate"]):
            raise NleBridgeError("INVALID_TIMELINE", f"the FCP7 XML is {raw['rate']} fps, not {rate}")
    else:
        raw = edl.read(text, rate=rate, start_timecode=start_timecode, name_hint=name, asset_starts=asset_starts)
    if drop_frame is not None and kind != "edl":
        raw["drop_frame"] = drop_frame
    return normalise(raw, base_dir=base_dir)


def _df(tl: Dict[str, Any], drop_frame: Optional[bool]) -> Dict[str, Any]:
    if drop_frame is None or drop_frame == tl["drop_frame"]:
        return tl
    return normalise(dict(tl, drop_frame=drop_frame))


def load_returned(orig: Dict[str, Any], path: str) -> Dict[str, Any]:
    """A timeline that came back from an NLE, read against the plan it started from: the plan's rate (an EDL states
    none), its start timecode when the file gives none, and its media start timecodes (EDL source timecodes are
    absolute)."""
    starts: Dict[str, int] = {}
    for a in orig["assets"].values():
        f = asset_start_frames(a, rate_of(orig))
        if f:
            starts[a["path"]] = starts[a["name"]] = f
    is_edl = os.path.splitext(path)[1].lower() == ".edl"
    return load(path, rate=orig["rate"] if is_edl else None, start_timecode=orig["start_timecode"], asset_starts=starts)
