"""Cross dissolves centred on the cut (`export --dissolves`, docs/decisions.md ADR-006).

A plan item's `transition_in` (model.normalise_transition: type, frames, before) is a transition from the clip that
ends where this one starts. The cut stays where the plan puts it: the clips' record ranges are unchanged, and the
dissolve borrows media beyond each clip's visible range ("handles"), `before` frames of the incoming clip's head
before the cut and `after` = frames - before of the outgoing clip's tail after it. That is how ffmpeg-skill
`render --export-timeline` writes its dissolves (centred, the odd frame before the cut), and how the three formats
carry one: OTIO a Transition between the clips (in_offset = before, out_offset = after), FCP7 XML a transitionitem
with the adjacent clipitems' start / end at -1, CMX 3600 a zero-length cut on the outgoing source and a D event.

`resolve()` decides which transitions can be written. One is written as a hard cut, with a TRANSITION_NOT_EXPORTED
warning naming why, when no clip ends where it starts (a fade from or to black), when the two clips are too short
for it, or when a clip's media has too few frames beyond its in or out point (checked when the length is known).
Any type other than a dissolve is written as a cross dissolve (TRANSITION_AS_DISSOLVE): editors share no other
transition across these formats, as ffmpeg-skill also says.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .model import warning

DISSOLVE_TYPES = ("dissolve", "cross_dissolve", "crossdissolve", "smpte_dissolve", "fade", "crossfade", "xfade", "mix")
VIDEO_NAME = "Cross Dissolve"
AUDIO_NAME = "Cross Fade (+3dB)"


def is_dissolve(kind: Any) -> bool:
    return str(kind or "").strip().lower().replace("-", "_").replace(" ", "_") in DISSOLVE_TYPES


def resolve(tl: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """-> ({incoming item id: {"frames", "before", "after", "from", "track_kind"}}, warnings)."""
    out: Dict[str, Dict[str, Any]] = {}
    warns: List[Dict[str, Any]] = []
    for t in tl["tracks"]:
        items = t["items"]
        used_head: Dict[str, int] = {}
        for i, it in enumerate(items):
            tr = it.get("transition_in")
            if not tr:
                continue
            iid = it["id"]

            def skip(reason: str) -> None:
                warns.append(warning("TRANSITION_NOT_EXPORTED", f"{iid}: {tr['type']} into this clip is written as a "
                                     f"hard cut: {reason}", item=iid))

            prev = items[i - 1] if i > 0 else None
            if it["kind"] != "clip":
                skip(f"it is a {it['kind']}, not a clip")
                continue
            if prev is None or prev["kind"] != "clip" or prev["record_out"] != it["record_in"]:
                skip("no clip ends where it starts (a dissolve from or to black is not written)")
                continue
            d, before = int(tr["frames"]), int(tr["before"])
            after = d - before
            prev_len = prev["record_out"] - prev["record_in"]
            it_len = it["record_out"] - it["record_in"]
            if used_head.get(prev["id"], 0) + before > prev_len or after > it_len:
                skip(f"{d} frames do not fit in {prev['id']} ({prev_len} frames) and {iid} ({it_len} frames)")
                continue
            if it["source_in"] < before:
                skip(f"it needs {before} frame(s) of media before its in point and has {it['source_in']}")
                continue
            dur = (tl["assets"].get(prev.get("asset") or "") or {}).get("duration_frames")
            prev_out = prev["source_in"] + prev_len  # as exported: 100 % from the in point
            if dur and prev_out + after > dur:
                skip(f"{prev['id']} needs {after} frame(s) of media after its out point and has {max(0, dur - prev_out)}")
                continue
            used_head[iid] = after
            out[iid] = {"frames": d, "before": before, "after": after, "from": prev["id"], "track_kind": t["kind"]}
            if not is_dissolve(tr["type"]):
                warns.append(warning("TRANSITION_AS_DISSOLVE", f"{iid}: {tr['type']} is written as a cross dissolve "
                                     "(editors share no other transition across OTIO, FCP7 XML and EDL)", item=iid))
    return out, warns


def tails(dissolves: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """outgoing item id -> the dissolve that leaves it."""
    return {d["from"]: d for d in dissolves.values()}
