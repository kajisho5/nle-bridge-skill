"""Conform package: everything an editor needs to open the cut somewhere else, in one folder (and optionally a zip).

    <out>/
      timeline/<name>.otio  .xml  .edl  .srt      the cut (export.py), plus <name>.json (nle-bridge/timeline@1)
      media/<file>                                 only with --copy-media
      relink.json                                  nle-bridge/relink@1: one entry per media file
      manifest.json                                nle-bridge/conform@1: every file of the package with its sha256

relink.json says, for each media file: where the plan found it, where it is in the package (when copied), whether
it exists, its size and sha256, its start timecode, length, frame rate and picture size, and where each of those
facts came from (`ffmpeg-skill probe` when ffmpeg-skill is available, else the plan, else null with the reason),
plus every range of it the timeline uses (in / out as source timecode, the record timecode it lands on).

Nothing in the package records a clock time, so the same plan and media give the same bytes (the zip too: fixed
entry dates and order).
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .export import sha256_file, write_files
from .model import NleBridgeError, asset_start_frames, base_name, rate_of, start_frames, summary, warning
from .timecode import frames_to_timecode, seconds_to_frames

RELINK_SCHEMA = "nle-bridge/relink@1"
CONFORM_SCHEMA = "nle-bridge/conform@1"
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _hex(value: Any) -> str:
    v = str(value).strip().lower()
    return v[7:] if v.startswith("sha256:") else v


def _write_json(path: str, doc: Any) -> None:
    if os.path.lexists(path):
        os.unlink(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")


def relink_entries(tl: Dict[str, Any], probe: Optional[Callable[[str], Optional[Dict[str, Any]]]],
                   hash_media: bool = True) -> Dict[str, Any]:
    rate = rate_of(tl)
    drop = tl["drop_frame"]
    start = start_frames(tl)
    warns: List[Dict[str, Any]] = []
    out = []
    for aid, a in tl["assets"].items():
        path = a["path"]
        exists = os.path.isfile(path)
        e: Dict[str, Any] = {"asset": aid, "name": a["name"], "original_path": path, "package_path": None,
                             "exists": exists, "size_bytes": os.path.getsize(path) if exists else a.get("size_bytes"),
                             "sha256": None, "start_timecode": a.get("start_timecode"),
                             "duration_frames": a.get("duration_frames"), "duration": None,
                             "fps": a.get("fps"), "width": a.get("width"), "height": a.get("height"),
                             "has_video": a.get("has_video", True), "has_audio": a.get("has_audio", True),
                             "facts_from": {}}
        if exists and hash_media:
            e["sha256"] = sha256_file(path)
            e["facts_from"]["sha256"] = "computed"
            if a.get("sha256") and _hex(a["sha256"]) != e["sha256"]:
                warns.append(warning("MEDIA_CHANGED", f"{a['name']}: sha256 differs from the plan's ({a['sha256']}): the "
                                     "file is not the one the plan was made from", item=aid))
        elif a.get("sha256"):
            e["sha256"] = _hex(a["sha256"])
            e["facts_from"]["sha256"] = "plan"
        if not exists:
            warns.append(warning("MEDIA_MISSING", f"{a['name']}: {path} does not exist here; the manifest carries the "
                                 "plan's facts only and the editor must relink it", item=aid))
        p = probe(path) if (probe and exists) else None
        for k in ("fps", "width", "height"):
            e["facts_from"][k] = "plan" if e[k] is not None else None
        if p:
            v = p.get("video") or {}
            if v.get("fps"):
                e["fps"] = float(v["fps"])
                e["facts_from"]["fps"] = "ffmpeg-skill probe"
            if v.get("width") and v.get("height"):
                e["width"], e["height"] = int(v["width"]), int(v["height"])
                e["facts_from"]["width"] = e["facts_from"]["height"] = "ffmpeg-skill probe"
            if p.get("duration") is not None:
                e["duration"] = round(float(p["duration"]), 3)
                e["duration_frames"] = seconds_to_frames(Fraction(str(p["duration"])), rate)
                e["facts_from"]["duration"] = "ffmpeg-skill probe"
            e["has_video"] = bool(v.get("width"))
            e["has_audio"] = bool((p.get("audio") or {}).get("channels"))
        if e["duration"] is None and e["duration_frames"]:
            e["duration"] = round(float(Fraction(e["duration_frames"]) / rate), 3)
            e["facts_from"]["duration"] = "plan"
        e["facts_from"].setdefault("duration", None)
        e["facts_from"]["start_timecode"] = "plan" if e["start_timecode"] else None
        if not e["start_timecode"]:
            e["start_timecode_note"] = ("not known: ffmpeg-skill probe does not report a media start timecode and the plan "
                                        "gives none; 00:00:00:00 is assumed in the EDL and the XML")
        base = asset_start_frames(a, rate)
        uses = []
        for t in tl["tracks"]:
            for it in t["items"]:
                if it.get("asset") != aid:
                    continue
                n = it["record_out"] - it["record_in"]
                uses.append({"item": it["id"], "track": t["name"],
                             "source_in": frames_to_timecode(base + it["source_in"], rate, drop),
                             "source_out": frames_to_timecode(base + it["source_in"] + n, rate, drop),
                             "record_in": frames_to_timecode(start + it["record_in"], rate, drop),
                             "record_out": frames_to_timecode(start + it["record_out"], rate, drop)})
        e["used"] = uses
        out.append(e)
    return {"schema": RELINK_SCHEMA, "rate": tl["rate"], "drop_frame": drop, "media": out, "warnings": warns}


def package(tl: Dict[str, Any], out_dir: str, formats: List[str], *, name: Optional[str] = None,
            copy_media: bool = False, make_zip: bool = False, overwrite: bool = False,
            probe: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None, hash_media: bool = True,
            inputs: Optional[List[str]] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out_dir = os.path.abspath(out_dir)
    if os.path.exists(out_dir) and os.listdir(out_dir) and not overwrite:
        raise NleBridgeError("OUTPUT_EXISTS", f"{out_dir} is not empty: pass --overwrite (only the package's own "
                                              "files are replaced)")
    zip_path = out_dir.rstrip("/\\") + ".zip"
    if make_zip and os.path.lexists(zip_path) and not overwrite:
        raise NleBridgeError("OUTPUT_EXISTS", f"refusing to overwrite {zip_path}: pass --overwrite")
    for p in inputs or []:
        rp = os.path.realpath(p)
        if rp == os.path.realpath(out_dir) or rp.startswith(os.path.realpath(out_dir) + os.sep):
            raise NleBridgeError("OUTPUT_IS_INPUT", f"the package folder {out_dir} holds an input ({p})")
    os.makedirs(os.path.join(out_dir, "timeline"), exist_ok=True)
    relink = relink_entries(tl, probe, hash_media)
    warns = list(relink["warnings"])
    tl_out = tl
    if copy_media:
        media_dir = os.path.join(out_dir, "media")
        os.makedirs(media_dir, exist_ok=True)
        used_names: Dict[str, str] = {}
        tl_out = json.loads(json.dumps(tl))
        for e in relink["media"]:
            if not e["exists"]:
                continue
            fname = e["name"] if e["name"] not in used_names else f"{e['asset']}_{e['name']}"
            used_names[fname] = e["asset"]
            dest = os.path.join(media_dir, fname)
            if os.path.lexists(dest):
                os.unlink(dest)
            shutil.copyfile(e["original_path"], dest)
            e["package_path"] = f"media/{fname}"
            tl_out["assets"][e["asset"]]["path"] = dest
    stem = name or tl["name"]
    written = write_files(tl_out, os.path.join(out_dir, "timeline"), formats, name=stem, overwrite=True, inputs=inputs)
    warns += written["warnings"]
    tl_json = os.path.join(out_dir, "timeline", stem + ".json")
    _write_json(tl_json, tl_out)
    relink_doc = {k: v for k, v in relink.items() if k != "warnings"}
    _write_json(os.path.join(out_dir, "relink.json"), relink_doc)
    files = []
    for root, _dirs, fnames in os.walk(out_dir):
        for f in fnames:
            if f == "manifest.json" and root == out_dir:
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, out_dir).replace(os.sep, "/")
            files.append({"path": rel, "size_bytes": os.path.getsize(p), "sha256": sha256_file(p)})
    files.sort(key=lambda x: x["path"])
    manifest = {"schema": CONFORM_SCHEMA, "skill": "nle-bridge-skill", "skill_version": __version__,
                "timeline": summary(tl_out), "formats": [f["format"] for f in written["files"]],
                "media_copied": copy_media, "files": files, "warnings": warns, **(extra or {})}
    _write_json(os.path.join(out_dir, "manifest.json"), manifest)
    zipped = None
    if make_zip:
        if os.path.lexists(zip_path):
            os.unlink(zip_path)
        top = base_name(out_dir.rstrip("/\\"))
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for rel in sorted([f["path"] for f in files] + ["manifest.json"]):
                info = zipfile.ZipInfo(f"{top}/{rel}", date_time=_ZIP_DATE)
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                src_path = os.path.join(out_dir, rel)
                big = os.path.getsize(src_path) > 0x7FFF0000
                with open(src_path, "rb") as src, z.open(info, "w", force_zip64=big) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        zipped = {"path": zip_path, "sha256": sha256_file(zip_path)}
    return {"package": out_dir, "zip": zipped, "files": written["files"], "relink": relink_doc,
            "manifest": os.path.join(out_dir, "manifest.json"), "warnings": warns, "timeline": summary(tl_out)}

