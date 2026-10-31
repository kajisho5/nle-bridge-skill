"""CMX 3600 EDL, written and read with the standard library.

- `FCM: DROP FRAME` / `FCM: NON-DROP FRAME`; drop-frame timecodes are written with `;` before the frames.
- One event per clip, cuts only. Video track 1 is the picture (channel `V`); audio tracks 1-4 are channels
  `A`, `A2`, `A3`, `A4`. CMX 3600 has a single picture track, so other video tracks are not written
  (`TRACK_NOT_EXPORTED`).
- Reel `AX` (file-based media) with the file in `* FROM CLIP NAME:` and `* SOURCE FILE:` comments, which
  Premiere and Resolve relink from. Source timecodes are the media's own (its start timecode + the in point).
- Markers are `* LOC: <record tc> <COLOUR> <name>` lines (the form Resolve reads and writes), so bar heads (RED)
  and beats (CYAN) keep their colours.
- Audio levels are not part of CMX 3600 (`LEVEL_NOT_EXPORTED`).
- With `--dissolves`, a cross dissolve is the standard pair ffmpeg-skill also writes: a zero-length cut on the
  outgoing source where its event ends, then a `D nnn` event bringing the incoming clip in from where the dissolve
  starts, with `* FROM CLIP NAME` / `* TO CLIP NAME`. An EDL has no cut point inside a dissolve, so the reader puts
  the cut at the centre (the odd frame before it), the rule this skill and ffmpeg-skill write.
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from .model import (DEFAULT_MARKER_COLOR, MARKER_COLORS, NleBridgeError, asset_start_frames, base_name, rate_of,
                    start_frames, url_to_path, warning)
from .timecode import TimecodeError, frames_to_timecode, parse_rate, rate_str, timecode_to_frames
from .transitions import tails

_TCRE = re.compile(r"^\d\d[:;.]\d\d[:;.]\d\d[:;.]\d\d$")
_TRANS = re.compile(r"^(C|D|W\d{3}|K|KB|KO)$")
_LOC = re.compile(r"^\*\s*LOC:\s*(\d\d[:;.]\d\d[:;.]\d\d[:;.]\d\d)\s+(\w+)\s*(.*)$")
_AUDIO_CHANNELS = ("A", "A2", "A3", "A4")
_GRID_NAME = re.compile(r"^(Bar \d+|Beat \d+\.\d+)$")


def _event(line: str) -> Optional[Tuple[str, str, str, str, int, str, str, str, str]]:
    """'NNN  REEL  CHAN  TRANS [DUR]  SRC-IN SRC-OUT REC-IN REC-OUT' -> (num, reel, chan, trans, dur, times...),
    read from both ends so a reel name with spaces (some writers keep the file name) still parses."""
    tok = line.split()
    if len(tok) < 8 or not tok[0].isdigit() or not all(_TCRE.match(t) for t in tok[-4:]):
        return None
    head = tok[1:-4]
    dur = 0
    if head and head[-1].isdigit() and len(head) >= 4:  # a transition duration
        dur = int(head[-1])
        head = head[:-1]
    if len(head) < 3 or not _TRANS.match(head[-1].upper()):
        return None
    return (tok[0], " ".join(head[:-2]), head[-2], head[-1].upper(), dur, *tok[-4:])


def write(tl: Dict[str, Any], dissolves: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[str, List[Dict[str, Any]]]:
    rate = rate_of(tl)
    drop = tl["drop_frame"]
    warns: List[Dict[str, Any]] = []
    start = start_frames(tl)
    dissolves = dissolves or {}
    leaving = tails(dissolves)

    def tc(frames: int) -> str:
        return frames_to_timecode(frames, rate, drop)

    events: List[Tuple[int, str, List[str]]] = []  # (record start, channel, lines after the event number)
    v_seen = a_seen = 0
    for t in tl["tracks"]:
        if t["kind"] == "video":
            v_seen += 1
            if v_seen > 1:
                if t["items"]:
                    warns.append(warning("TRACK_NOT_EXPORTED", f"video track {t['name']}: CMX 3600 has one picture "
                                         f"track; its {len(t['items'])} clip(s) are not in the EDL (use OTIO or FCP7 XML)",
                                         fmt="edl"))
                continue
            chan = "V"
        else:
            a_seen += 1
            if a_seen > len(_AUDIO_CHANNELS):
                if t["items"]:
                    warns.append(warning("TRACK_NOT_EXPORTED", f"audio track {t['name']}: an EDL carries 4 audio "
                                         "tracks; this one is not in the EDL", fmt="edl"))
                continue
            chan = _AUDIO_CHANNELS[a_seen - 1]
        src_end: Dict[str, int] = {}  # item id -> where its event's source ends
        names: Dict[str, str] = {}
        for it in t["items"]:
            if it["kind"] != "clip":
                continue
            if it.get("gain_db") is not None:
                warns.append(warning("LEVEL_NOT_EXPORTED", f"{it['id']}: level {it['gain_db']:+g} dB is not part of "
                                     "CMX 3600", item=it["id"], fmt="edl"))
            a = tl["assets"][it["asset"]]
            head, tail = dissolves.get(it["id"]), leaving.get(it["id"])
            # an event runs from where its dissolve starts to where the next one starts
            rec_in = it["record_in"] - (head["before"] if head else 0)
            rec_out = it["record_out"] - (tail["before"] if tail else 0)
            s_in = asset_start_frames(a, rate) + it["source_in"] - (head["before"] if head else 0)
            s_out = s_in + rec_out - rec_in
            src_end[it["id"]] = s_out
            names[it["id"]] = it["name"]
            body = []
            if head:
                cut = tc(src_end[head["from"]])
                body.append(f"AX       {chan:<5} C        {cut} {cut} {tc(start + rec_in)} {tc(start + rec_in)}")
                body.append(f"AX       {chan:<5} D    {head['frames']:03d} {tc(s_in)} {tc(s_out)} "
                            f"{tc(start + rec_in)} {tc(start + rec_out)}")
                body.append(f"* FROM CLIP NAME: {names[head['from']]}")
                body.append(f"* TO CLIP NAME: {it['name']}")
            else:
                body.append(f"AX       {chan:<5} C        {tc(s_in)} {tc(s_out)} {tc(start + rec_in)} {tc(start + rec_out)}")
                body.append(f"* FROM CLIP NAME: {it['name']}")
            body.append(f"* SOURCE FILE: {a['path']}")
            body.append(f"* NLE-BRIDGE ID: {it['id']}")
            events.append((rec_in, chan, body))
    order = {c: i for i, c in enumerate(("V",) + _AUDIO_CHANNELS)}
    events.sort(key=lambda e: (e[0], order[e[1]]))
    lines = [f"TITLE: {tl['name']}", f"FCM: {'DROP FRAME' if drop else 'NON-DROP FRAME'}", ""]
    for n, (_, chan, body) in enumerate(events, 1):
        lines += [f"{n:03d}  {x}" if x.startswith("AX") else x for x in body]
        lines.append("")
    for m in tl["markers"]:
        name = m["name"] + (f" | {m['comment']}" if m.get("comment") and m["kind"] == "marker" else "")
        lines.append(f"* LOC: {tc(start + m['at_frame'])} {m['color']:<7} {name}")
    if any(m.get("duration_frames") for m in tl["markers"]):
        warns.append(warning("MARKER_DURATION_NOT_EXPORTED", "EDL LOC markers are points; marker durations are dropped",
                             fmt="edl"))
    return "\n".join(lines).rstrip("\n") + "\n", warns


def read(text: str, *, rate: Any = None, start_timecode: Optional[str] = None, name_hint: str = "timeline",
         asset_starts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """CMX 3600 -> a timeline document in frames (not yet normalised). An EDL states no frame rate: `rate` must
    come from the caller (the plan it is compared with, or --fps). Record times are taken from `start_timecode`
    (default: the hour the first event starts in). `asset_starts` maps a media path or name to its start timecode
    in frames (source timecodes are absolute; without it the media is assumed to start at 00:00:00:00)."""
    if rate is None:
        raise NleBridgeError("INVALID_ARGUMENT", "an EDL carries no frame rate: pass --fps (or compare it with a plan)")
    r = parse_rate(rate)
    drop = False
    title = name_hint
    events: List[Dict[str, Any]] = []
    markers_raw: List[Tuple[str, str, str]] = []
    warns: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("TITLE:"):
            title = line[6:].strip() or name_hint
            continue
        if line.startswith("FCM:"):
            drop = "NON" not in line.upper() and "DROP" in line.upper()
            continue
        ev = _event(line.strip())
        if ev:
            num, reel, chan, trans, dur, si, so, ri, ro = ev
            cur = {"num": num, "reel": reel, "chan": chan.upper(), "trans": trans, "dur": dur, "si": si, "so": so,
                   "ri": ri, "ro": ro, "name": None, "file": None, "id": None, "to_name": None, "to_file": None,
                   "from_file": None}
            events.append(cur)
            continue
        lm = _LOC.match(line.strip())
        if lm:
            markers_raw.append(lm.groups())
            continue
        if line.startswith("M2"):
            warns.append(warning("SPEED_NOT_IMPORTED", f"{line.strip()}: speed change is not taken into the plan",
                                 fmt="edl"))
            continue
        if cur is not None and line.startswith("*"):
            body = line[1:].strip()
            for key, field in (("FROM CLIP NAME:", "name"), ("TO CLIP NAME:", "to_name"), ("SOURCE FILE:", "file"),
                               ("FROM CLIP:", "from_file"), ("TO CLIP:", "to_file"), ("NLE-BRIDGE ID:", "id")):
                if body.upper().startswith(key):
                    value = body[len(key):].strip()
                    cur[field] = url_to_path(value) if field.endswith("file") else value
                    break
    if drop and r not in (Fraction(30000, 1001), Fraction(60000, 1001)):
        raise NleBridgeError("INVALID_TIMELINE", f"the EDL says DROP FRAME but the rate is {float(r):.3f} fps")

    def fr(tc: str) -> int:
        try:
            return timecode_to_frames(tc, r, drop)
        except TimecodeError as e:
            raise NleBridgeError("INVALID_TIMELINE", f"EDL timecode: {e}")

    if not events:
        raise NleBridgeError("INVALID_TIMELINE", "no CMX 3600 events in the EDL")
    events = [e for e in events if fr(e["ro"]) > fr(e["ri"])]  # a dissolve's zero-length outgoing line
    if not events:
        raise NleBridgeError("INVALID_TIMELINE", "no CMX 3600 events with a length in the EDL")
    for e in events:
        if e["trans"] == "D":  # the incoming clip: TO CLIP NAME / TO CLIP name it when given
            e["name"] = e["to_name"] or e["name"]
            e["file"] = e["to_file"] or e["file"]
        else:
            e["file"] = e["file"] or e["from_file"]
    first = min(fr(e["ri"]) for e in events)
    start = timecode_to_frames(start_timecode, r, drop) if start_timecode else None
    if start is None or first < start:  # no start given, or the file's record times begin before it
        start = timecode_to_frames(frames_to_timecode(first, r, drop)[:2] + ":00:00:00", r, drop)
    assets: Dict[str, Dict[str, Any]] = {}
    key_asset: Dict[str, str] = {}
    tracks: Dict[str, Dict[str, Any]] = {}
    starts = asset_starts or {}
    for e in events:
        key = e["file"] or e["name"] or e["reel"]
        if key not in key_asset:
            aid = f"m{len(key_asset) + 1}"
            key_asset[key] = aid
            assets[aid] = {"path": e["file"] or key, "name": e["name"] or base_name(e["file"] or "") or key}
        aid = key_asset[key]
        base = starts.get(e["file"] or "", starts.get(e["name"] or "", starts.get(base_name(e["file"] or ""), 0)))
        if base:
            assets[aid]["start_frame"] = base
        si, so, ri, ro = fr(e["si"]), fr(e["so"]), fr(e["ri"]) - start, fr(e["ro"]) - start
        chans = {"V": ["V"], "B": ["V", "A"], "A": ["A"], "AA": ["A", "A2"], "A2": ["A2"], "A3": ["A3"], "A4": ["A4"],
                 "AA/V": ["V", "A", "A2"]}.get(e["chan"], [])
        if not chans:
            warns.append(warning("TRACK_NOT_IMPORTED", f"event {e['num']}: channel {e['chan']} is not read", fmt="edl"))
        for c in chans:
            t = tracks.setdefault(c, {"kind": "video" if c == "V" else "audio",
                                      "name": "V1" if c == "V" else ("A1" if c == "A" else c), "items": []})
            iid = e["id"] or f"{t['name']}-{e['num']}"
            if any(x["id"] == iid for x in t["items"]) or any(iid == x["id"] for tt in tracks.values() for x in tt["items"]):
                iid = f"{iid}@{t['name']}"
            item = {"id": iid, "kind": "clip", "asset": aid, "name": e["name"] or assets[aid]["name"],
                    "source_in": si - base, "source_out": si - base + (ro - ri), "record_in": ri, "record_out": ro}
            prev = t["items"][-1] if t["items"] else None
            if e["trans"] == "D" and e["dur"] > 0 and prev is not None and prev["record_out"] == ri \
                    and e["dur"] <= ro - ri:
                # the dissolve starts at the event's record in; the cut is taken at its centre (ADR-006)
                d = e["dur"]
                before = d - d // 2
                prev["record_out"] += before
                prev["source_out"] += before
                item.update(record_in=ri + before, source_in=si - base + before,
                            transition_in={"type": "dissolve", "frames": d, "before": before})
            elif e["trans"] != "C":
                warns.append(warning("TRANSITION_NOT_IMPORTED", f"event {e['num']}: transition {e['trans']} is read as "
                                     "a cut" + (" (no clip on the track ends where it starts)" if e["trans"] == "D" else ""),
                                     fmt="edl"))
            t["items"].append(item)
            if so - si != ro - ri:
                warns.append(warning("SPEED_NOT_IMPORTED", f"event {e['num']}: source and record lengths differ",
                                     item=iid, fmt="edl"))
    for a in assets.values():
        sf = a.pop("start_frame", 0)
        a["start_timecode"] = frames_to_timecode(sf, r, False) if sf else None
    markers = []
    for tcs, color, name in markers_raw:
        nm = name.strip()
        comment = ""
        if " | " in nm:
            nm, comment = nm.split(" | ", 1)
        col = color.upper()
        grid = _GRID_NAME.match(nm)
        markers.append({"at_frame": max(0, fr(tcs) - start), "duration_frames": 0, "name": nm, "comment": comment,
                        "color": col if col in MARKER_COLORS else DEFAULT_MARKER_COLOR,
                        "kind": ("bar" if nm.startswith("Bar ") else "beat") if grid else "marker"})
    order = ["V", "A", "A2", "A3", "A4"]
    return {"name": title, "rate": rate_str(r), "drop_frame": drop, "start_timecode": frames_to_timecode(start, r, drop),
            "assets": assets, "tracks": [tracks[c] for c in order if c in tracks], "markers": markers,
            "warnings": warns, "source": {"format": "edl"}}
