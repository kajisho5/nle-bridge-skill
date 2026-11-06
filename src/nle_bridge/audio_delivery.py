"""Stems and loudness versions from audio-production-skill (roadmap step 16, ADR-012).

A plan (`nle-bridge/timeline@1`) may name an audio-production-skill response (`audio-production/response@1`, the JSON
its `run --json` prints; read only) under `audio_delivery`: a path (relative to the plan) or the document, or
`{"response": <path or document>, "stems": [output ids]}`. Each completed output is one of:

- a **stem** (dialogue, music, effects, ...): its own audio track of the timeline, from the programme's first frame
  for its length (stems are written the programme's length and aligned to its first sample), named by its
  `output_id`, the item `stem-<output_id>` carrying `stem` {output_id, node_id, type, channels, sample_rate}; or
- a **loudness version**: an output whose last operation is NORMALIZE (one master at several targets). Not on the
  timeline (they are alternatives of one mix); `conform` lists each with its target and its measurement
  (integrated loudness, true peak, loudness range, as audio-production re-measured it) and copies it with
  `--copy-media`.

`stems` lists the outputs that are stems when the default (every output whose last operation is not NORMALIZE)
does not fit; an output neither listed nor a loudness version is then named (`AUDIO_OUTPUT_NOT_PLACED`). An output that did not complete is named (`AUDIO_OUTPUT_NOT_COMPLETED`), never placed. A stem whose
length differs from the programme by more than a frame is named (`STEM_LENGTH_DIFFERS`).
"""
from __future__ import annotations

import json
import os
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .model import NleBridgeError, base_name, rate_of, resolve_path, total_frames, warning
from .timecode import TimecodeError, seconds_to_frames

RESPONSE_SCHEMA = "audio-production/response@1"


def _load(spec: Any, base_dir: Optional[str]):
    """-> (response document, its directory, its file name or None, the stems override or None)."""
    stems = None
    if isinstance(spec, dict) and "response" in spec:
        stems = spec.get("stems")
        if stems is not None and (not isinstance(stems, list) or not all(isinstance(x, str) for x in stems)):
            raise NleBridgeError("INVALID_PLAN", "audio_delivery.stems must be a list of output ids")
        spec = spec["response"]
    if isinstance(spec, str):
        path = resolve_path(spec, base_dir)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except OSError as e:
            raise NleBridgeError("INPUT_NOT_FOUND", f"audio_delivery: cannot read {path}: {e}")
        except ValueError as e:
            raise NleBridgeError("INVALID_PLAN", f"audio_delivery: {path} is not JSON: {e}")
        return doc, os.path.dirname(path), base_name(path), stems
    if isinstance(spec, dict):
        return spec, base_dir, None, stems
    raise NleBridgeError("INVALID_PLAN", "audio_delivery must be a response path, a response, or {response, stems}")


def _last_op(out: Dict[str, Any]) -> Dict[str, Any]:
    prov = out.get("provenance") or {}
    ops = prov.get("operations") or []
    return next((o for o in ops if o.get("node_id") == prov.get("node_id")), ops[0] if ops else {})


def add_audio_delivery(tl: Dict[str, Any], spec: Any, base_dir: Optional[str]) -> Dict[str, Any]:
    """Stems on new audio tracks after the plan's, loudness versions in `tl["audio_delivery"]` (in place)."""
    doc, rdir, rname, override = _load(spec, base_dir)
    if not isinstance(doc, dict) or doc.get("schema") != RESPONSE_SCHEMA:
        raise NleBridgeError("INVALID_PLAN", f"audio_delivery: not an {RESPONSE_SCHEMA} document")
    if doc.get("ok") is not True:
        err = (doc.get("error") or {}).get("message") or doc.get("status")
        raise NleBridgeError("INVALID_PLAN", f"audio_delivery: the audio-production run did not succeed ({err})")
    rate = rate_of(tl)
    programme = total_frames(tl)
    outputs = [o for o in doc.get("outputs") or [] if isinstance(o, dict)]
    ids = [str(o.get("output_id")) for o in outputs]
    if override is not None:
        missing = [x for x in override if x not in ids]
        if missing:
            raise NleBridgeError("INVALID_PLAN", f"audio_delivery.stems: no output {', '.join(missing)} in the response")
    seen = {it["id"] for t in tl["tracks"] for it in t["items"]}
    stems: List[Dict[str, Any]] = []
    versions: List[Dict[str, Any]] = []
    tracks = []
    for o in outputs:
        oid = str(o.get("output_id"))
        if o.get("status") != "completed":
            tl["warnings"].append(warning("AUDIO_OUTPUT_NOT_COMPLETED", f"audio-production output {oid} is "
                                          f"{o.get('status')}; it is not in the timeline or the package", item=oid))
            continue
        art = o.get("artifact") or {}
        path = resolve_path(str(o.get("path") or art.get("path") or ""), rdir)
        if not path:
            raise NleBridgeError("INVALID_PLAN", f"audio_delivery: output {oid} has no path")
        last = _last_op(o)
        entry = {"output_id": oid, "path": path, "format": o.get("format"), "sha256": art.get("sha256"),
                 "size_bytes": art.get("size"), "duration": art.get("duration"), "channels": art.get("channels"),
                 "sample_rate": art.get("sample_rate"), "codec": art.get("codec"), "node_id": last.get("node_id"),
                 "type": last.get("type")}
        is_stem = (oid in override) if override is not None else last.get("type") != "NORMALIZE"
        if not is_stem and last.get("type") != "NORMALIZE":  # left out of `stems` and not a loudness version
            tl["warnings"].append(warning("AUDIO_OUTPUT_NOT_PLACED", f"audio-production output {oid} ({last.get('type')}) "
                                          "is neither in audio_delivery.stems nor a loudness version: it is not in the "
                                          "timeline or the package", item=oid))
            continue
        if not is_stem:
            p = last.get("parameters") or {}
            m = (last.get("measurements") or {}).get("loudness") or {}
            within = None
            if m.get("integrated_lufs") is not None and p.get("target_lufs") is not None:
                within = abs(float(m["integrated_lufs"]) - float(p["target_lufs"])) <= float(p.get("tolerance_lufs") or 1.0) \
                    and (m.get("true_peak_dbtp") is None or p.get("true_peak_db") is None
                         or float(m["true_peak_dbtp"]) <= float(p["true_peak_db"]))
            versions.append(dict(entry, target={k: p.get(k) for k in ("target_lufs", "true_peak_db", "loudness_range_lu",
                                                                       "tolerance_lufs", "profile") if p.get(k) is not None},
                                 measured={k: m.get(k) for k in ("integrated_lufs", "true_peak_dbtp", "loudness_range_lu",
                                                                 "measured_by") if m.get(k) is not None},
                                 within_target=within))
            continue
        if art.get("duration") is None:
            raise NleBridgeError("INVALID_PLAN", f"audio_delivery: stem {oid} has no artifact duration")
        try:
            frames = seconds_to_frames(Fraction(str(art["duration"])), rate)
        except TimecodeError as e:
            raise NleBridgeError("INVALID_PLAN", f"audio_delivery: stem {oid}: {e}")
        iid = f"stem-{oid}"
        if iid in seen:
            raise NleBridgeError("INVALID_PLAN", f"audio_delivery: duplicate item id {iid}")
        seen.add(iid)
        aid = f"stem-{oid}"
        if aid in tl["assets"]:
            raise NleBridgeError("INVALID_PLAN", f"audio_delivery: asset id {aid} is taken")
        tl["assets"][aid] = {"path": path, "name": base_name(path), "fps": None, "width": None, "height": None,
                             "duration_frames": frames, "start_timecode": None, "has_video": False, "has_audio": True,
                             **({"sha256": art["sha256"]} if art.get("sha256") else {}),
                             **({"size_bytes": art["size"]} if art.get("size") is not None else {})}
        if programme and abs(frames - programme) > 1:
            tl["warnings"].append(warning("STEM_LENGTH_DIFFERS", f"stem {oid} is {frames} frames, the programme "
                                          f"{programme}: stems are meant to be the programme's length", item=iid))
        stem = {k: entry[k] for k in ("output_id", "node_id", "type", "channels", "sample_rate") if entry[k] is not None}
        tracks.append({"kind": "audio", "name": oid, "items": [
            {"id": iid, "kind": "clip", "asset": aid, "name": oid, "source_in": 0, "source_out": frames, "record_in": 0,
             "record_out": frames, "enabled": True, "stem": stem}]})
        stems.append(dict(entry, item=iid))
    last_audio = max((i for i, t in enumerate(tl["tracks"]) if t["kind"] == "audio"), default=len(tl["tracks"]) - 1)
    tl["tracks"][last_audio + 1:last_audio + 1] = tracks
    tl["audio_delivery"] = {"response": rname, "skill": doc.get("skill"), "stems": stems, "loudness_versions": versions}
    return tl
