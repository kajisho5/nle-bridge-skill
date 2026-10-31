"""Round trip: what the editor changed, from the timeline the NLE gives back, against the plan it started from.

The returned file (OTIO, FCP7 XML or EDL) is read into the same frame-based model as the plan. Then:

1. Media: each returned media file is matched to a plan asset by path, else by file name (case-insensitive),
   else by file name without extension (an NLE that relinked to a transcode). Unmatched ones are new media.
2. Stereo pairs: Premiere writes one audio track per channel, so a stereo clip comes back as two linked clips on
   A1 and A2 (an EDL as an `AA` event). A returned audio clip that repeats one on an earlier audio track exactly
   (same media, same source and record ranges) is folded into it, unless the plan itself has that clip on that
   track; the folded clips are listed under `channels_folded`, and the kept clip names its `channel_tracks`. A
   channel track with clips left over (a copy the editor changed alone) does not take part in the track pairing;
   its leftovers are reported as added (`channel_of` the track they repeat).
   Clips: tracks pair by kind and order (V1 with V1, A1 with A1), after the fold. Within a pair, a returned clip matches a plan
   clip when it plays the same media and their source ranges overlap; the plan id carried in the file (OTIO
   metadata, the FCP7 master comment, the EDL `* NLE-BRIDGE ID` comment) is preferred when an NLE kept it, then
   the largest overlap. Each pair says how its record and source ends moved (moved / trimmed / slipped);
   leftover plan clips are removed, leftover returned clips added (with `split_from` when they overlap a plan
   clip of the same media, as a razor cut does).
3. Cut points: the edit points of the first video track (every clip start and end except the timeline's first
   and last frame). A plan cut point follows its clip pair to the returned one; delta_frames is the move.
4. Markers: the plan's markers against the returned ones by name, then by position. Beat-grid markers
   (`Bar N` / `Beat N.b`, kind bar / beat) are counted, not diffed.
5. Beats (when a beat grid is given): for every returned cut point (and the plan's, for comparison) the nearest
   beat and the signed offset in frames (positive: the cut is after the beat), on_beat within the tolerance.

Cross dissolves are compared (a `transition` change: added, removed, or another length or position); the plan's count
as exported only with `dissolves` (export --dissolves), otherwise they were hard cuts in the files. Effects, speed
changes, other transitions and titles the editor added are listed under `not_imported`: they are in the editor's
file, but not in the returned plan.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .beats import nearest
from .model import base_name, rate_of, start_frames, summary, total_frames
from .timecode import frames_to_timecode
from .transitions import resolve as resolve_transitions

DIFF_SCHEMA = "nle-bridge/diff@1"
_GRID_NAME = re.compile(r"^(Bar \d+|Beat \d+\.\d+)$")


def _norm(p: str) -> str:
    return base_name(p).lower()


def _stem(p: str) -> str:
    return os.path.splitext(_norm(p))[0]


def match_assets(orig: Dict[str, Any], ret: Dict[str, Any]) -> Tuple[Dict[str, Optional[str]], List[str]]:
    """returned asset id -> plan asset id (or None), and the returned ids that are new media."""
    by_path = {a["path"]: aid for aid, a in orig["assets"].items()}
    by_name: Dict[str, str] = {}
    by_stem: Dict[str, str] = {}
    for aid, a in orig["assets"].items():
        by_name.setdefault(_norm(a["path"]), aid)
        by_name.setdefault(a["name"].lower(), aid)
        by_stem.setdefault(_stem(a["path"]), aid)
    out: Dict[str, Optional[str]] = {}
    new = []
    for rid, a in ret["assets"].items():
        m = by_path.get(a["path"]) or by_name.get(_norm(a["path"])) or by_name.get(a["name"].lower()) \
            or by_stem.get(_stem(a["path"])) or by_stem.get(_stem(a["name"]))
        out[rid] = m
        if m is None:
            new.append(rid)
    return out, new


def _overlap(a: Dict[str, Any], b: Dict[str, Any]) -> int:
    return max(0, min(a["source_out"], b["source_out"]) - max(a["source_in"], b["source_in"]))


def _pair_track(o_items: List[Dict[str, Any]], r_items: List[Dict[str, Any]], amap: Dict[str, Optional[str]]):
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    used_o, used_r = set(), set()
    o_by_id = {it["id"]: it for it in o_items}
    for r in r_items:  # 1. the plan id the NLE kept, same media
        o = o_by_id.get(r["id"])
        if o is not None and amap.get(r.get("asset")) == o.get("asset") and o["id"] not in used_o:
            pairs.append((o, r))
            used_o.add(o["id"])
            used_r.add(id(r))
    cands = []
    for oi, o in enumerate(o_items):
        for ri, r in enumerate(r_items):
            if o["id"] in used_o or id(r) in used_r or amap.get(r.get("asset")) != o.get("asset"):
                continue
            ov = _overlap(o, r)
            if ov > 0:
                cands.append((-ov, abs(o["record_in"] - r["record_in"]), oi, ri))
    for _, _, oi, ri in sorted(cands):
        o, r = o_items[oi], r_items[ri]
        if o["id"] in used_o or id(r) in used_r:
            continue
        pairs.append((o, r))
        used_o.add(o["id"])
        used_r.add(id(r))
    removed = [o for o in o_items if o["id"] not in used_o]
    added = [r for r in r_items if id(r) not in used_r]
    pairs.sort(key=lambda p: p[0]["record_in"])
    return pairs, removed, added


_RANGE = ("source_in", "source_out", "record_in", "record_out")


def fold_channel_pairs(orig: Dict[str, Any], ret: Dict[str, Any], amap: Dict[str, Optional[str]],
                       keep_leftovers: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The returned timeline with per-channel copies of a clip folded into one (see 2. above), what was folded, and
    the channel tracks with clips left over. A channel track is taken out of the timeline (so the next audio track
    pairs with the plan's next one) unless `keep_leftovers` keeps the leftovers in place."""
    o_audio = [t for t in orig["tracks"] if t["kind"] == "audio"]
    kept_audio: List[Tuple[str, List[Dict[str, Any]]]] = []
    tracks: List[Dict[str, Any]] = []
    folded: List[Dict[str, Any]] = []
    leftovers: List[Dict[str, Any]] = []
    ai = -1
    for t in ret["tracks"]:
        if t["kind"] != "audio":
            tracks.append(t)
            continue
        ai += 1
        plan_items = o_audio[ai]["items"] if ai < len(o_audio) else []
        keep: List[Dict[str, Any]] = []
        into: Optional[str] = None
        for r in t["items"]:
            twin, twin_track = None, None
            for name, items in kept_audio:
                twin = next((x for x in items if x.get("asset") == r.get("asset")
                             and all(x[k] == r[k] for k in _RANGE)), None)
                if twin is not None:
                    twin_track = name
                    break
            planned = any(o.get("asset") == amap.get(r.get("asset"))
                          and all(o[k] == r[k] for k in ("source_in", "record_in", "record_out")) for o in plan_items)
            if twin is None or planned:
                keep.append(dict(r))
                continue
            twin.setdefault("channel_tracks", [twin_track]).append(t["name"])
            into = into or twin_track
            folded.append({"track": t["name"], "returned_id": r["id"], "into_track": twin_track,
                           "into_returned_id": twin["id"], "name": r["name"], "record_in": r["record_in"],
                           "record_out": r["record_out"]})
        if into and keep:
            leftovers.append({"track": t["name"], "channel_of": into, "items": keep})
            if not keep_leftovers:
                continue
        if keep or not t["items"]:
            tracks.append(dict(t, items=keep))
            kept_audio.append((t["name"], keep))
    return dict(ret, tracks=tracks), folded, leftovers


def _edit_points(items: List[Dict[str, Any]], end: int) -> List[int]:
    pts = set()
    for it in items:
        pts.add(it["record_in"])
        pts.add(it["record_out"])
    return sorted(p for p in pts if 0 < p < end)


def _as_exported(tl: Dict[str, Any], dissolves: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """The plan as the files carry it: clips only (titles / generators are never written), each at 100 % from
    its in point for its record length (a speed change is not carried, export.py), with the cross dissolves that
    export --dissolves writes (none without it)."""
    written = resolve_transitions(tl)[0] if dissolves else {}
    out = dict(tl)
    skipped = []
    tracks = []
    for t in tl["tracks"]:
        items = []
        for it in t["items"]:
            if it["kind"] != "clip":
                skipped.append({"track": t["name"], "plan_id": it["id"], "kind": it["kind"],
                                "reason": f"a {it['kind']} is not in any interchange file, so it cannot come back"})
                continue
            item = dict(it, source_out=it["source_in"] + it["record_out"] - it["record_in"])
            item.pop("transition_in", None)
            if it["id"] in written:
                w = written[it["id"]]
                item["transition_in"] = {"type": "dissolve", "frames": w["frames"], "before": w["before"]}
            items.append(item)
        tracks.append(dict(t, items=items))
    out["tracks"] = tracks
    return out, skipped


def _transition(it: Dict[str, Any]) -> Optional[Dict[str, int]]:
    tr = it.get("transition_in")
    return {"frames": int(tr["frames"]), "before": int(tr["before"])} if tr else None


def diff(orig: Dict[str, Any], ret: Dict[str, Any], *, grid: Optional[Dict[str, Any]] = None,
         beat_tolerance: float = 0.5, returned_path: Optional[str] = None, dissolves: bool = False) -> Dict[str, Any]:
    rate = rate_of(orig)
    if rate_of(ret) != rate:
        raise ValueError(f"the returned timeline is {ret['rate']} fps, the plan {orig['rate']} fps")
    orig, not_compared = _as_exported(orig, dissolves)
    fmt = (ret.get("source") or {}).get("format")
    carries_level = fmt not in ("edl", "otio")  # FCP7 has audiolevels; OTIO only our own metadata, EDL nothing
    carries_color = fmt != "fcp7"
    drop = orig["drop_frame"]
    o_start, r_start = start_frames(orig), start_frames(ret)

    def otc(f: int) -> str:
        return frames_to_timecode(o_start + f, rate, drop)

    def rtc(f: int) -> str:
        return frames_to_timecode(r_start + f, rate, drop)

    amap, new_media = match_assets(orig, ret)
    ret, folded, leftovers = fold_channel_pairs(orig, ret, amap)
    o_tracks = {k: [t for t in orig["tracks"] if t["kind"] == k] for k in ("video", "audio")}
    r_tracks = {k: [t for t in ret["tracks"] if t["kind"] == k] for k in ("video", "audio")}
    clips_out: List[Dict[str, Any]] = []
    removed_out: List[Dict[str, Any]] = []
    added_out: List[Dict[str, Any]] = []
    v1_pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for kind in ("video", "audio"):
        n = max(len(o_tracks[kind]), len(r_tracks[kind]))
        for i in range(n):
            ot = o_tracks[kind][i] if i < len(o_tracks[kind]) else {"name": None, "items": []}
            rt = r_tracks[kind][i] if i < len(r_tracks[kind]) else {"name": None, "items": []}
            pairs, removed, added = _pair_track(ot["items"], rt["items"], amap)
            if kind == "video" and i == 0:
                v1_pairs = pairs
            track = ot["name"] or rt["name"]
            for o, r in pairs:
                d = {"record_in": r["record_in"] - o["record_in"], "record_out": r["record_out"] - o["record_out"],
                     "source_in": r["source_in"] - o["source_in"], "source_out": r["source_out"] - o["source_out"]}
                changes = []
                if d["source_in"] or d["source_out"]:
                    if d["source_in"] == d["source_out"] and d["record_in"] == 0 and d["record_out"] == 0:
                        changes.append("slipped")
                    else:
                        changes.append("trimmed")
                if d["record_in"] and d["record_in"] == d["record_out"] and not (d["source_in"] or d["source_out"]):
                    changes.append("moved")
                elif (d["record_in"] or d["record_out"]) and "trimmed" not in changes:
                    changes.append("retimed")
                if bool(r.get("enabled", True)) != bool(o.get("enabled", True)):
                    changes.append("disabled" if not r.get("enabled", True) else "enabled")
                if (r.get("gain_db") is not None or (o.get("gain_db") is not None and carries_level)) and \
                        round(float(r.get("gain_db") or 0.0), 2) != round(float(o.get("gain_db") or 0.0), 2):
                    changes.append("level")
                if _transition(o) != _transition(r):
                    changes.append("transition")
                clips_out.append({"track": track, "plan_id": o["id"], "asset": o.get("asset"), "name": o["name"],
                                  "status": "changed" if changes else "unchanged", "changes": changes,
                                  "delta_frames": d,
                                  "plan": {"record_in": otc(o["record_in"]), "record_out": otc(o["record_out"]),
                                           "source_in": o["source_in"], "source_out": o["source_out"]},
                                  "returned": {"record_in": rtc(r["record_in"]), "record_out": rtc(r["record_out"]),
                                               "source_in": r["source_in"], "source_out": r["source_out"]},
                                  **({"gain_db": {"plan": o.get("gain_db"), "returned": r.get("gain_db")}}
                                     if "level" in changes else {}),
                                  **({"transition_in": {"plan": _transition(o), "returned": _transition(r)}}
                                     if "transition" in changes else {}),
                                  **({"channel_tracks": r["channel_tracks"]} if r.get("channel_tracks") else {})})
            for o in removed:
                removed_out.append({"track": track, "plan_id": o["id"], "asset": o.get("asset"), "name": o["name"],
                                    "record_in": otc(o["record_in"]), "record_out": otc(o["record_out"]),
                                    "frames": o["record_out"] - o["record_in"]})
            for r in added:
                aid = amap.get(r.get("asset"))
                split = next((o["id"] for o in ot["items"] if aid and o.get("asset") == aid and _overlap(o, r) > 0), None)
                added_out.append({"track": track, "returned_id": r["id"], "asset": aid,
                                  "media": ret["assets"].get(r.get("asset"), {}).get("path"), "name": r["name"],
                                  "new_media": aid is None, "record_in": rtc(r["record_in"]),
                                  "record_out": rtc(r["record_out"]), "source_in": r["source_in"],
                                  "source_out": r["source_out"], "frames": r["record_out"] - r["record_in"],
                                  **({"split_from": split} if split else {})})

    for lo in leftovers:  # a channel copy the editor changed on its own
        for r in lo["items"]:
            added_out.append({"track": lo["track"], "returned_id": r["id"], "asset": amap.get(r.get("asset")),
                              "media": ret["assets"].get(r.get("asset"), {}).get("path"), "name": r["name"],
                              "new_media": amap.get(r.get("asset")) is None, "record_in": rtc(r["record_in"]),
                              "record_out": rtc(r["record_out"]), "source_in": r["source_in"],
                              "source_out": r["source_out"], "frames": r["record_out"] - r["record_in"],
                              "channel_of": lo["channel_of"]})

    # cut points on the first video track
    o_v1 = o_tracks["video"][0]["items"] if o_tracks["video"] else []
    r_v1 = r_tracks["video"][0]["items"] if r_tracks["video"] else []
    o_end, r_end = total_frames(orig), total_frames(ret)
    o_pts, r_pts = _edit_points(o_v1, o_end), _edit_points(r_v1, r_end)
    follow: Dict[int, int] = {}
    for o, r in v1_pairs:
        follow.setdefault(o["record_in"], r["record_in"])
        follow.setdefault(o["record_out"], r["record_out"])
    cuts: List[Dict[str, Any]] = []
    reached = set()
    for f in o_pts:
        g = follow.get(f)
        if g is not None and 0 < g < r_end:
            reached.add(g)
            cuts.append({"status": "moved" if g != f else "unchanged", "plan_frame": f, "plan_tc": otc(f),
                         "returned_frame": g, "returned_tc": rtc(g), "delta_frames": g - f})
        else:
            cuts.append({"status": "removed", "plan_frame": f, "plan_tc": otc(f), "returned_frame": None,
                         "returned_tc": None, "delta_frames": None})
    for g in r_pts:
        if g not in reached:
            cuts.append({"status": "added", "plan_frame": None, "plan_tc": None, "returned_frame": g,
                         "returned_tc": rtc(g), "delta_frames": None})
    cuts.sort(key=lambda c: (c["returned_frame"] if c["returned_frame"] is not None else c["plan_frame"]))

    beats_summary = None
    if grid:
        offs = []
        for c in cuts:
            if c["returned_frame"] is not None:
                c["beat"] = nearest(grid, c["returned_frame"], rate, beat_tolerance)
                offs.append(c["beat"]["offset_frames"])
            if c["plan_frame"] is not None:
                c["plan_beat"] = nearest(grid, c["plan_frame"], rate, beat_tolerance)
        on = sum(1 for c in cuts if c.get("beat", {}).get("on_beat"))
        plan_on = sum(1 for c in cuts if c.get("plan_beat", {}).get("on_beat"))
        beats_summary = {"bpm": grid["bpm"], "beats_per_bar": grid["beats_per_bar"], "offset": grid["offset"],
                         "beat_set": grid["beat_set"], "bars_provenance": grid["bars_provenance"],
                         "tolerance_frames": beat_tolerance, "returned_cuts": len(offs), "returned_on_beat": on,
                         "plan_cuts": sum(1 for c in cuts if c["plan_frame"] is not None), "plan_on_beat": plan_on,
                         "max_abs_offset_frames": round(max((abs(x) for x in offs), default=0.0), 3),
                         "mean_abs_offset_frames": round(sum(abs(x) for x in offs) / len(offs), 3) if offs else 0.0,
                         "moved_off_beat": sum(1 for c in cuts if c.get("plan_beat", {}).get("on_beat")
                                               and not c.get("beat", {}).get("on_beat", True))}

    markers = _diff_markers(orig["markers"], ret["markers"], otc, rtc, carries_color)
    changed = sum(1 for c in clips_out if c["status"] == "changed")
    return {"schema": DIFF_SCHEMA, "plan": summary(orig), "returned": dict(summary(ret), file=returned_path),
            "media": {"matched": {rid: aid for rid, aid in amap.items() if aid},
                      "new": [{"returned_asset": rid, "path": ret["assets"][rid]["path"]} for rid in new_media]},
            "summary": {"clips_unchanged": sum(1 for c in clips_out if c["status"] == "unchanged"),
                        "clips_changed": changed, "clips_added": len(added_out), "clips_removed": len(removed_out),
                        "cuts_moved": sum(1 for c in cuts if c["status"] == "moved"),
                        "cuts_added": sum(1 for c in cuts if c["status"] == "added"),
                        "cuts_removed": sum(1 for c in cuts if c["status"] == "removed"),
                        "transitions_changed": sum(1 for c in clips_out if "transition" in c["changes"]),
                        "channel_clips_folded": len(folded),
                        "markers_added": len(markers["added"]), "markers_removed": len(markers["removed"]),
                        "markers_changed": len(markers["changed"]),
                        "duration_delta_frames": r_end - o_end,
                        "identical": not (changed or added_out or removed_out or markers["added"] or markers["removed"]
                                          or markers["changed"] or r_end != o_end)},
            "clips": clips_out, "added": added_out, "removed": removed_out, "cuts": cuts, "markers": markers,
            "channels_folded": [dict(f, record_in=rtc(f["record_in"]), record_out=rtc(f["record_out"])) for f in folded],
            "beats": beats_summary, "not_compared": not_compared, "not_imported": ret.get("warnings") or []}


def _is_grid(m: Dict[str, Any]) -> bool:
    return m.get("kind") in ("bar", "beat") or bool(_GRID_NAME.match(m.get("name") or ""))


def _diff_markers(om: List[Dict[str, Any]], rm: List[Dict[str, Any]], otc, rtc, carries_color: bool = True) -> Dict[str, Any]:
    o = [m for m in om if not _is_grid(m)]
    r = [m for m in rm if not _is_grid(m)]
    used_r = set()
    pairs = []
    for i, m in enumerate(o):  # same name, nearest position
        cands = [(abs(x["at_frame"] - m["at_frame"]), j) for j, x in enumerate(r) if j not in used_r and x["name"] == m["name"]]
        if cands:
            j = min(cands)[1]
            used_r.add(j)
            pairs.append((i, j))
    paired_o = {i for i, _ in pairs}
    for i, m in enumerate(o):  # same position, renamed
        if i in paired_o:
            continue
        j = next((j for j, x in enumerate(r) if j not in used_r and x["at_frame"] == m["at_frame"]), None)
        if j is not None:
            used_r.add(j)
            paired_o.add(i)
            pairs.append((i, j))
    changed = []
    for i, j in pairs:
        a, b = o[i], r[j]
        what = [k for k, ka in (("moved", "at_frame"), ("renamed", "name"), ("comment", "comment"), ("color", "color"),
                                ("duration", "duration_frames")) if a.get(ka) != b.get(ka) and (carries_color or k != "color")]
        if what:
            changed.append({"name": a["name"], "changes": what, "plan_tc": otc(a["at_frame"]), "returned_tc": rtc(b["at_frame"]),
                            "delta_frames": b["at_frame"] - a["at_frame"], "returned_name": b["name"],
                            "plan_comment": a.get("comment"), "returned_comment": b.get("comment")})
    removed = [{"name": m["name"], "plan_tc": otc(m["at_frame"]), "comment": m.get("comment")}
               for i, m in enumerate(o) if i not in paired_o]
    added = [{"name": m["name"], "returned_tc": rtc(m["at_frame"]), "comment": m.get("comment"), "color": m.get("color")}
             for j, m in enumerate(r) if j not in used_r]
    return {"added": added, "removed": removed, "changed": changed,
            "grid_markers": {"plan": len(om) - len(o), "returned": len(rm) - len(r)}}


def as_plan(orig: Dict[str, Any], ret: Dict[str, Any], returned_path: Optional[str]) -> Dict[str, Any]:
    """The returned timeline as a new plan version: media mapped back to the plan's asset ids and paths
    (new media keep their own), provenance pointing at the plan and the returned file."""
    amap, _ = match_assets(orig, ret)
    ret, _, _ = fold_channel_pairs(orig, ret, amap, keep_leftovers=True)  # one clip per stereo source, as the plan has it
    assets = {}
    rename: Dict[str, str] = {}
    for rid, a in ret["assets"].items():
        aid = amap.get(rid)
        if aid:
            assets[aid] = dict(orig["assets"][aid])
            rename[rid] = aid
        else:
            nid = rid if rid not in orig["assets"] else f"new_{rid}"
            assets[nid] = dict(a)
            rename[rid] = nid
    tracks = [{"kind": t["kind"], "name": t["name"],
               "items": [dict(it, asset=rename.get(it["asset"], it["asset"])) for it in t["items"]]} for t in ret["tracks"]]
    out = dict(ret, assets=assets, tracks=tracks, name=orig["name"],
               source={"format": (ret.get("source") or {}).get("format"), "derived_from": orig["name"],
                       "returned_file": returned_path, "provenance": "EDITOR"})
    out.pop("warnings", None)
    out["warnings"] = []
    return out

