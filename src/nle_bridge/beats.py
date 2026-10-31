"""Beat grid: a music bed's beats (and bar heads) as timeline markers, and how far a cut sits from the nearest beat.

The grid is a measurement or a statement made elsewhere; this skill never generates or analyses music (ADR-002).
It reads three shapes:

1. ffmpeg-skill `scenes --beats --json` (what video-production-agent's take assembly measures its music bed with,
   agent ADR-054): `beats` (the regular grid) and `beat_grid` {tempo_bpm, interval, phase, confidence,
   supported_beats, usable, ...}. That shape has no bars, so bar heads are counted from the first grid beat in
   groups of `beats_per_bar` (default 4) and the result says the bars are ASSUMED.
2. The same `beat_grid` object on its own (as the agent's analysis record keeps it).
3. This skill's own `nle-bridge/beatgrid@1`: {"bpm": 120, "beats": [s, ...], "bars": [s, ...]} or
   {"bpm": 120, "beats_per_bar": 4, "first_downbeat": 0.25, "duration": 30} (a regular grid is generated).
   A host that generated the music (for example a music-generation MCP server in the chat app) knows its bars:
   give them as `bars` and they are STATED, not assumed.

Times are seconds of the music; `offset` (seconds) is where the music starts on the timeline (the agent lays the
bed from programme time 0, so the default is 0).
"""
from __future__ import annotations

import bisect
import json
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .model import NleBridgeError, warning
from .timecode import seconds_to_frames

BEATGRID_SCHEMA = "nle-bridge/beatgrid@1"
DEFAULT_BEATS_PER_BAR = 4
BAR_COLOR = "RED"
BEAT_COLOR = "CYAN"
BEAT_SETS = ("supported", "grid")


def _floats(xs: Any, what: str) -> List[float]:
    if not isinstance(xs, list):
        raise NleBridgeError("INVALID_BEATS", f"{what} must be a list of seconds")
    out = []
    for x in xs:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x or x < 0:
            raise NleBridgeError("INVALID_BEATS", f"{what}: {x!r} is not a time in seconds")
        out.append(float(x))
    return sorted(set(out))


def _median_interval(beats: List[float]) -> Optional[float]:
    d = sorted(b - a for a, b in zip(beats, beats[1:]) if b > a)
    return d[len(d) // 2] if d else None


def load_grid(doc: Any, *, beats_per_bar: Optional[int] = None, beat_set: str = "supported",
              offset: float = 0.0) -> Dict[str, Any]:
    """Any of the three shapes -> {"beats": [s...] (timeline seconds), "bars": [...], "bpm", "interval",
    "bars_provenance": STATED | ASSUMED, "source": shape, "warnings": [...]}."""
    if not isinstance(doc, dict):
        raise NleBridgeError("INVALID_BEATS", "a beat grid must be a JSON object")
    if beat_set not in BEAT_SETS:
        raise NleBridgeError("INVALID_ARGUMENT", f"--beat-set must be one of {', '.join(BEAT_SETS)}")
    warns: List[Dict[str, Any]] = []
    bars: Optional[List[float]] = None
    bpb = beats_per_bar
    if doc.get("schema") == BEATGRID_SCHEMA or ("bpm" in doc and "beat_grid" not in doc):
        shape = BEATGRID_SCHEMA
        bpm = doc.get("bpm")
        bpb = bpb or int(doc.get("beats_per_bar") or DEFAULT_BEATS_PER_BAR)
        if doc.get("beats") is not None:
            beats = _floats(doc["beats"], "beats")
        else:
            if not bpm or not doc.get("duration"):
                raise NleBridgeError("INVALID_BEATS", "nle-bridge/beatgrid@1 needs beats, or bpm and duration")
            step = 60.0 / float(bpm)
            first = float(doc.get("first_downbeat") or 0.0)
            n = int((float(doc["duration"]) - first) / step + 1e-9) + 1
            beats = [round(first + i * step, 6) for i in range(max(0, n))]
            bars = beats[::bpb]
        if doc.get("bars") is not None:
            bars = _floats(doc["bars"], "bars")
        interval = 60.0 / float(bpm) if bpm else _median_interval(beats)
        grid_beats = beats
        provenance = "STATED" if (doc.get("bars") is not None or doc.get("beats") is None) else "ASSUMED"
    else:
        g = doc.get("beat_grid") if isinstance(doc.get("beat_grid"), dict) else doc
        if "supported_beats" not in g and "beats" not in doc:
            raise NleBridgeError("INVALID_BEATS", "not a beat grid: expected nle-bridge/beatgrid@1, ffmpeg-skill "
                                                  "scenes --beats output (beats, beat_grid) or a beat_grid object")
        shape = "ffmpeg-skill/scenes --beats" if "beat_grid" in doc else "beat_grid"
        bpm = g.get("tempo_bpm")
        bpb = bpb or DEFAULT_BEATS_PER_BAR
        grid_beats = _floats(doc.get("beats") or g.get("beats") or g.get("supported_beats") or [], "beats")
        supported = _floats(g.get("supported_beats") or [], "beat_grid.supported_beats")
        if beat_set == "supported" and supported:
            beats = supported
        else:
            beats = grid_beats
            if beat_set == "supported":
                warns.append(warning("BEAT_SET_FALLBACK", "the grid has no supported_beats; the regular grid is used"))
        interval = float(g["interval"]) if g.get("interval") else (60.0 / float(bpm) if bpm else _median_interval(grid_beats))
        if g.get("usable") is False:
            warns.append(warning("BEAT_GRID_UNUSABLE", f"the beat grid is marked usable: false (confidence "
                                                       f"{g.get('confidence')}); markers and offsets are only as good as it"))
        provenance = "ASSUMED"
    if not beats:
        raise NleBridgeError("INVALID_BEATS", "the beat grid has no beats")
    if interval is None or interval <= 0:
        raise NleBridgeError("INVALID_BEATS", "the beat grid has no tempo (bpm / interval) and fewer than two beats")
    if bpb < 1 or bpb > 32:
        raise NleBridgeError("INVALID_ARGUMENT", "--beats-per-bar must be 1-32")
    # Number every beat on the regular grid (so supported beats with gaps keep their bar.beat position).
    t0 = (bars[0] if bars else (grid_beats or beats)[0])
    labelled = []
    for b in beats:
        idx = int(round((b - t0) / interval))
        bar_no, beat_no = divmod(idx, bpb)
        labelled.append({"t": b, "bar": bar_no + 1, "beat": beat_no + 1})
    if bars is None:
        bars = [x["t"] for x in labelled if x["beat"] == 1]
        warns.append(warning("BARS_ASSUMED", f"the grid states no bars; bar heads are every {bpb} beats from the first "
                                             f"beat at {t0:.3f}s (give bars in nle-bridge/beatgrid@1 to state them)"))
    bar_set = set(round(x, 6) for x in bars)
    for x in labelled:
        x["is_bar"] = round(x["t"], 6) in bar_set
    off = float(offset)
    return {"source": shape, "bpm": float(bpm) if bpm else round(60.0 / interval, 3), "interval": interval,
            "beats_per_bar": bpb, "offset": off, "bars_provenance": provenance, "beat_set": beat_set,
            "beats": [dict(x, t=round(x["t"] + off, 6)) for x in labelled],
            "bars": [round(b + off, 6) for b in bars], "warnings": warns}


def read_grid(path: str, **kw: Any) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as e:
        raise NleBridgeError("INPUT_NOT_FOUND", f"cannot read beat grid {path}: {e}")
    except ValueError as e:
        raise NleBridgeError("INVALID_BEATS", f"beat grid {path} is not JSON: {e}")
    return load_grid(doc, **kw)


def grid_markers(grid: Dict[str, Any], rate: Fraction, total_frames: int, which: str = "all") -> List[Dict[str, Any]]:
    """Markers for the beats inside the timeline: bar heads `Bar N` (red), other beats `Beat N.b` (cyan).
    which = all | bars | none."""
    if which == "none":
        return []
    out = []
    bar_ts = set(round(b, 6) for b in grid["bars"])
    seen = set()
    for x in grid["beats"]:
        is_bar = x["is_bar"] or round(x["t"], 6) in bar_ts
        if which == "bars" and not is_bar:
            continue
        at = seconds_to_frames(Fraction(str(x["t"])), rate)
        if at < 0 or at >= total_frames or at in seen:
            continue
        seen.add(at)
        out.append({"at_frame": at, "duration_frames": 0,
                    "name": f"Bar {x['bar']}" if is_bar else f"Beat {x['bar']}.{x['beat']}",
                    "comment": f"{grid['bpm']:g} BPM, beat {x['beat']} of {grid['beats_per_bar']}"
                               + (" (bar assumed)" if grid["bars_provenance"] == "ASSUMED" and is_bar else ""),
                    "color": BAR_COLOR if is_bar else BEAT_COLOR, "kind": "bar" if is_bar else "beat"})
    return out


def nearest(grid: Dict[str, Any], frame: int, rate: Fraction, tolerance: float) -> Dict[str, Any]:
    """Where a cut at `frame` sits against the grid: the nearest beat and the signed offset in frames
    (positive = the cut is after the beat), on_beat when |offset| <= tolerance frames."""
    times = [x["t"] for x in grid["beats"]]
    t = float(Fraction(frame) / rate)
    i = bisect.bisect_left(times, t)
    cands = [j for j in (i - 1, i) if 0 <= j < len(times)]
    j = min(cands, key=lambda k: (abs(times[k] - t), times[k]))
    b = grid["beats"][j]
    off = (Fraction(frame) - Fraction(str(b["t"])) * rate)
    offset = round(float(off), 3)
    return {"beat": f"{b['bar']}.{b['beat']}", "beat_time": b["t"], "is_bar": b["is_bar"], "offset_frames": offset,
            "offset_whole_frames": int(round(offset)), "on_beat": abs(offset) <= tolerance}
