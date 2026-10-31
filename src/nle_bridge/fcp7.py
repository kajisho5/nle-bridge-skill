"""Final Cut Pro 7 XML (xmeml version 4), the interchange Premiere Pro imports and exports natively and DaVinci
Resolve reads. Written and read with the standard library.

What it carries: one sequence; video and audio tracks of clipitems (start / end on the sequence, in / out in the
media, both in frames at the sequence rate); the file each clipitem plays (pathurl, its own start timecode when
known); audio levels as the standard `audiolevels` filter (linear gain, 1.0 = 0 dB); sequence markers (name,
comment, in, out). The sequence rate is timebase + ntsc, its timecode states DF / NDF.

With `--dissolves`, a cross dissolve is a transitionitem between the two clipitems (start / end on the sequence,
alignment center, start or end; "Cross Dissolve" on video, "Cross Fade (+3dB)" on audio). As FCP7 writes it, the
clipitems next to it have start / end -1 and their in / out include the media the dissolve uses beyond the cut. The
reader takes the cut from the transition (center: the odd frame before the cut, the rule this skill and ffmpeg-skill
write) and gives the incoming clip its `transition_in`.

What it does not: marker colours (FCP7 had none; xmeml has no colour element), so bar heads and beats are told
apart by name (`Bar N`, `Beat N.b`) and comment, and a `MARKER_COLOR_NOT_EXPORTED` warning says so.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from .model import (DEFAULT_MARKER_COLOR, NleBridgeError, asset_start_frames, base_name, path_to_url, rate_of,
                    start_frames, total_frames, url_to_path, warning)
from .timecode import frames_to_timecode, is_ntsc, nominal, rate_str
from .transitions import AUDIO_NAME, VIDEO_NAME, is_dissolve, tails

_GRID_NAME = re.compile(r"^(Bar \d+|Beat \d+\.\d+)$")


def _q(s: Any) -> str:
    return escape(str(s), {'"': "&quot;"})


def _rate_xml(rate: Fraction, ind: str) -> str:
    return (f"{ind}<rate>\n{ind}  <timebase>{nominal(rate)}</timebase>\n"
            f"{ind}  <ntsc>{'TRUE' if is_ntsc(rate) else 'FALSE'}</ntsc>\n{ind}</rate>")


def _tc_xml(frames: int, rate: Fraction, drop: bool, ind: str) -> str:
    return (f"{ind}<timecode>\n{_rate_xml(rate, ind + '  ')}\n"
            f"{ind}  <string>{frames_to_timecode(frames, rate, drop)}</string>\n"
            f"{ind}  <frame>{frames}</frame>\n{ind}  <displayformat>{'DF' if drop else 'NDF'}</displayformat>\n"
            f"{ind}</timecode>")


def _level_xml(gain_db: float, ind: str) -> str:
    linear = 10 ** (gain_db / 20.0)
    return (f"{ind}<filter>\n{ind}  <effect>\n{ind}    <name>Audio Levels</name>\n"
            f"{ind}    <effectid>audiolevels</effectid>\n{ind}    <effectcategory>audiolevels</effectcategory>\n"
            f"{ind}    <effecttype>audiolevels</effecttype>\n{ind}    <mediatype>audio</mediatype>\n"
            f"{ind}    <parameter>\n{ind}      <parameterid>level</parameterid>\n{ind}      <name>Level</name>\n"
            f"{ind}      <valuemin>0</valuemin>\n{ind}      <valuemax>3.98109</valuemax>\n"
            f"{ind}      <value>{linear:.6f}</value>\n{ind}    </parameter>\n{ind}  </effect>\n{ind}</filter>")


def _transition_xml(it: Dict[str, Any], dz: Dict[str, Any], kind: str, rate: Fraction, ind: str,
                    warns: List[Dict[str, Any]]) -> str:
    d, before, after = dz["frames"], dz["before"], dz["after"]
    if before == 0:
        align = "start"
    elif after == 0:
        align = "end"
    else:
        align = "center"
        if before != d - d // 2:
            warns.append(warning("TRANSITION_OFF_CENTRE", f"{it['id']}: the dissolve has {before} of its {d} frames "
                                 "before the cut; FCP7 XML can only say center, so it is written centred on the cut "
                                 "(the cut itself stays put)", item=it["id"], fmt="fcp7"))
            before = d - d // 2
    name = VIDEO_NAME if kind == "video" else AUDIO_NAME
    effectid = "Cross Dissolve" if kind == "video" else "KGAudioTransCrossFade3dB"
    return "\n".join([
        f"{ind}<transitionitem>", f"{ind}  <start>{it['record_in'] - before}</start>",
        f"{ind}  <end>{it['record_in'] - before + d}</end>", f"{ind}  <alignment>{align}</alignment>",
        _rate_xml(rate, ind + "  "), f"{ind}  <effect>", f"{ind}    <name>{name}</name>",
        f"{ind}    <effectid>{effectid}</effectid>",
        f"{ind}    <effectcategory>{'Dissolve' if kind == 'video' else 'Crossfade'}</effectcategory>",
        f"{ind}    <effecttype>transition</effecttype>", f"{ind}    <mediatype>{kind}</mediatype>", f"{ind}  </effect>",
        f"{ind}</transitionitem>"])


def _cut(start: int, end: int, alignment: str) -> int:
    """The cut inside a transitionitem: center puts the odd frame before the cut (ffmpeg-skill's split)."""
    if alignment.startswith("start"):
        return start
    if alignment.startswith("end"):
        return end
    return end - (end - start) // 2


def write(tl: Dict[str, Any], dissolves: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[str, List[Dict[str, Any]]]:
    rate = rate_of(tl)
    drop = tl["drop_frame"]
    warns: List[Dict[str, Any]] = []
    dissolves = dissolves or {}
    leaving = tails(dissolves)
    files_written: Dict[str, str] = {}
    file_ids: Dict[str, str] = {}
    for aid in tl["assets"]:
        file_ids[aid] = f"file-{len(file_ids) + 1}"

    def file_xml(aid: str, ind: str) -> str:
        fid = file_ids[aid]
        if aid in files_written:
            return f'{ind}<file id="{fid}"/>'
        files_written[aid] = fid
        a = tl["assets"][aid]
        parts = [f'{ind}<file id="{fid}">', f"{ind}  <name>{_q(a['name'])}</name>",
                 f"{ind}  <pathurl>{_q(path_to_url(a['path']))}</pathurl>", _rate_xml(rate, ind + "  ")]
        if a.get("duration_frames"):
            parts.append(f"{ind}  <duration>{a['duration_frames']}</duration>")
        start = asset_start_frames(a, rate)
        parts.append(_tc_xml(start, rate, bool(a.get("drop_frame")) and drop, ind + "  "))
        media = [f"{ind}  <media>"]
        if a.get("has_video", True):
            media.append(f"{ind}    <video>\n{ind}      <samplecharacteristics>\n{_rate_xml(rate, ind + '        ')}")
            if a.get("width") and a.get("height"):
                media.append(f"{ind}        <width>{a['width']}</width>\n{ind}        <height>{a['height']}</height>")
            media.append(f"{ind}      </samplecharacteristics>\n{ind}    </video>")
        if a.get("has_audio", True):
            media.append(f"{ind}    <audio>\n{ind}      <channelcount>2</channelcount>\n{ind}    </audio>")
        media.append(f"{ind}  </media>")
        parts += media + [f"{ind}</file>"]
        return "\n".join(parts)

    def clipitem(it: Dict[str, Any], n: int, kind: str, ind: str, track_index: int) -> str:
        a = tl["assets"][it["asset"]]
        dur = a.get("duration_frames") or it["source_out"]
        head, tail = dissolves.get(it["id"]), leaving.get(it["id"])
        # next to a transition FCP7 writes start / end -1, and in / out take in the media the dissolve uses
        start = -1 if head else it["record_in"]
        end = -1 if tail else it["record_out"]
        s_in = it["source_in"] - (head["before"] if head else 0)
        s_out = it["source_in"] + it["record_out"] - it["record_in"] + (tail["after"] if tail else 0)
        lines = [f'{ind}<clipitem id="clipitem-{n}">', f"{ind}  <masterclipid>masterclip-{file_ids[it['asset']][5:]}</masterclipid>",
                 f"{ind}  <name>{_q(it['name'])}</name>", f"{ind}  <enabled>{'TRUE' if it.get('enabled', True) else 'FALSE'}</enabled>",
                 f"{ind}  <duration>{dur}</duration>", _rate_xml(rate, ind + "  "),
                 f"{ind}  <start>{start}</start>", f"{ind}  <end>{end}</end>",
                 f"{ind}  <in>{s_in}</in>", f"{ind}  <out>{s_out}</out>",
                 file_xml(it["asset"], ind + "  ")]
        if kind == "audio":
            lines.append(f"{ind}  <sourcetrack>\n{ind}    <mediatype>audio</mediatype>\n"
                         f"{ind}    <trackindex>{track_index}</trackindex>\n{ind}  </sourcetrack>")
            if it.get("gain_db") is not None:
                lines.append(_level_xml(float(it["gain_db"]), ind + "  "))
        elif it.get("gain_db") is not None:
            warns.append(warning("LEVEL_NOT_EXPORTED", f"{it['id']}: a level on a video clip is not carried",
                                 item=it["id"], fmt="fcp7"))
        lines.append(f"{ind}  <comments>\n{ind}    <mastercomment1>nle-bridge:{_q(it['id'])}</mastercomment1>\n{ind}  </comments>")
        lines.append(f"{ind}</clipitem>")
        return "\n".join(lines)

    n = 0
    video_tracks, audio_tracks = [], []
    audio_index = 0
    for t in tl["tracks"]:
        ind = "        "
        body = []
        if t["kind"] == "audio":
            audio_index += 1
        for it in t["items"]:
            if it["kind"] != "clip":
                continue
            n += 1
            if it["id"] in dissolves:
                body.append(_transition_xml(it, dissolves[it["id"]], t["kind"], rate, ind + "  ", warns))
            body.append(clipitem(it, n, t["kind"], ind + "  ", min(audio_index, 2) if t["kind"] == "audio" else 1))
        block = f"{ind}<track>\n" + ("\n".join(body) + "\n" if body else "") + \
            f"{ind}  <enabled>TRUE</enabled>\n{ind}  <locked>FALSE</locked>\n{ind}</track>"
        (video_tracks if t["kind"] == "video" else audio_tracks).append(block)
    if not video_tracks:
        video_tracks.append("        <track>\n          <enabled>TRUE</enabled>\n        </track>")
    fmt = ["        <format>", "          <samplecharacteristics>", _rate_xml(rate, "            ")]
    if tl.get("width") and tl.get("height"):
        fmt.append(f"            <width>{tl['width']}</width>\n            <height>{tl['height']}</height>")
    fmt += ["            <pixelaspectratio>square</pixelaspectratio>", "          </samplecharacteristics>", "        </format>"]
    markers = []
    for m in tl["markers"]:
        out = m["at_frame"] + m["duration_frames"] if m.get("duration_frames") else -1
        markers.append(f"    <marker>\n      <comment>{_q(m.get('comment') or '')}</comment>\n"
                       f"      <name>{_q(m['name'])}</name>\n      <in>{m['at_frame']}</in>\n      <out>{out}</out>\n    </marker>")
    if any(m["color"] != DEFAULT_MARKER_COLOR or m["kind"] != "marker" for m in tl["markers"]):
        warns.append(warning("MARKER_COLOR_NOT_EXPORTED", "FCP7 XML has no marker colour: markers keep their name and "
                             "comment (bar heads are 'Bar N', beats 'Beat N.b'); use the OTIO or EDL for colours",
                             fmt="fcp7"))
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE xmeml>", '<xmeml version="4">',
           '  <sequence id="sequence-1">', f"    <name>{_q(tl['name'])}</name>",
           f"    <duration>{total_frames(tl)}</duration>", _rate_xml(rate, "    "),
           _tc_xml(start_frames(tl), rate, drop, "    "), "    <media>", "      <video>", *fmt, *video_tracks,
           "      </video>", "      <audio>", "        <numOutputChannels>2</numOutputChannels>", *audio_tracks,
           "      </audio>", "    </media>", *markers, "  </sequence>", "</xmeml>"]
    return "\n".join(out) + "\n", warns


# ------------------------------------------------------------------------------------------------ read
def _txt(e: Optional[ET.Element], path: str, default: Optional[str] = None) -> Optional[str]:
    if e is None:
        return default
    f = e.find(path)
    return f.text.strip() if f is not None and f.text is not None else default


def _rate(e: Optional[ET.Element]) -> Optional[Fraction]:
    if e is None:
        return None
    tb = _txt(e, "timebase")
    if not tb:
        return None
    base = int(float(tb))
    if (_txt(e, "ntsc") or "FALSE").upper() == "TRUE":
        return Fraction(base * 1000, 1001)
    return Fraction(base)


def read(text: str, *, name_hint: str = "timeline") -> Dict[str, Any]:
    """xmeml (FCP7 / Premiere / Resolve) -> a timeline document in frames (not yet normalised), with warnings for
    transitions, speed (time remap), other filters and generators (titles), which are not taken into a plan."""
    if re.search(r"<!ENTITY", text[:4096]):
        raise NleBridgeError("INVALID_TIMELINE", "FCP7 XML with entity declarations is refused")
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise NleBridgeError("INVALID_TIMELINE", f"not XML: {e}")
    if root.tag != "xmeml":
        raise NleBridgeError("INVALID_TIMELINE", f"not FCP7 XML (root <{root.tag}>, expected <xmeml>)")
    seq = root.find("sequence")
    if seq is None:
        seq = root.find(".//sequence")
    if seq is None:
        raise NleBridgeError("INVALID_TIMELINE", "the FCP7 XML holds no sequence")
    rate = _rate(seq.find("rate")) or Fraction(25)
    tc = seq.find("timecode")
    drop = (_txt(tc, "displayformat") or "NDF").upper() == "DF" and rate in (Fraction(30000, 1001), Fraction(60000, 1001))
    start = int(float(_txt(tc, "frame") or 0)) if tc is not None else 0
    warns: List[Dict[str, Any]] = []
    files: Dict[str, ET.Element] = {}
    for f in seq.iter("file"):
        if f.get("id") and len(list(f)) and f.get("id") not in files:
            files[f.get("id")] = f
    assets: Dict[str, Dict[str, Any]] = {}
    file_asset: Dict[str, str] = {}

    def asset_for(fid: str) -> str:
        if fid in file_asset:
            return file_asset[fid]
        f = files.get(fid)
        path = url_to_path(_txt(f, "pathurl") or "") if f is not None else ""
        name = _txt(f, "name") or base_name(path) or fid
        aid = f"m{len(assets) + 1}"
        frate = _rate(f.find("rate")) if f is not None else None
        ftc = f.find("timecode") if f is not None else None
        sframe = int(float(_txt(ftc, "frame") or 0)) if ftc is not None else 0
        a: Dict[str, Any] = {"path": path or name, "name": name,
                             "duration_frames": int(float(_txt(f, "duration"))) if _txt(f, "duration") else None,
                             "start_timecode": frames_to_timecode(sframe, frate or rate, False) if sframe else None}
        if frate and frate != rate:
            a["fps"] = float(frate)
        w, h = _txt(f, "media/video/samplecharacteristics/width"), _txt(f, "media/video/samplecharacteristics/height")
        if w and h:
            a["width"], a["height"] = int(w), int(h)
        assets[aid] = a
        file_asset[fid] = aid
        return aid

    tracks: List[Dict[str, Any]] = []
    media = seq.find("media")
    for kind in ("video", "audio"):
        for ti, t in enumerate(media.findall(f"{kind}/track") if media is not None else []):
            items = []
            last_end = 0
            els = [el for el in t if el.tag in ("clipitem", "transitionitem", "generatoritem")]
            trans: Dict[int, Dict[str, Any]] = {}  # index in els -> the transition's start, end, cut
            for k, el in enumerate(els):
                if el.tag != "transitionitem":
                    continue
                name = _txt(el, "effect/name") or _txt(el, "effect/effectid") or ""
                try:
                    ts, te = int(float(_txt(el, "start") or "")), int(float(_txt(el, "end") or ""))
                except ValueError:
                    ts = te = -1
                between = 0 < k < len(els) - 1 and els[k - 1].tag == "clipitem" and els[k + 1].tag == "clipitem"
                if 0 <= ts < te and between:
                    cut = _cut(ts, te, (_txt(el, "alignment") or "center").lower())
                    trans[k] = {"start": ts, "end": te, "cut": cut, "dissolve": is_dissolve(name) or
                                any(w in name.lower() for w in ("dissolve", "cross fade", "crossfade")), "name": name}
                    if trans[k]["dissolve"]:
                        continue
                warns.append(warning("TRANSITION_NOT_IMPORTED", f"{kind} track {ti + 1}: transition "
                                     f"{name} is read as a hard cut", fmt="fcp7"))
            for k, el in enumerate(els):
                if el.tag == "transitionitem":
                    continue
                if el.tag == "generatoritem":
                    warns.append(warning("TITLE_NOT_IMPORTED", f"{kind} track {ti + 1}: generator / title "
                                         f"'{_txt(el, 'name') or ''}' is not taken into the plan", fmt="fcp7"))
                    continue
                if el.tag != "clipitem":
                    continue
                fe = el.find("file")
                if fe is None or not fe.get("id"):
                    warns.append(warning("NESTED_NOT_IMPORTED", f"clipitem '{_txt(el, 'name')}' has no file (a nested "
                                         "sequence?) and is not read", fmt="fcp7"))
                    continue
                s, e = int(float(_txt(el, "start") or -1)), int(float(_txt(el, "end") or -1))
                cin, cout = int(float(_txt(el, "in") or 0)), int(float(_txt(el, "out") or 0))
                head, tail = trans.get(k - 1), trans.get(k + 1)
                handles = 0
                if s < 0 and head:  # next to a transition: FCP7 writes -1, in includes the media before the cut
                    s = head["cut"]
                    cin += head["cut"] - head["start"]
                elif s < 0:
                    s = last_end
                if e < 0 and tail:  # and out includes the media after it
                    e = tail["cut"]
                    handles = tail["end"] - tail["cut"]
                elif e < 0:
                    e = s + (cout - cin)
                last_end = e
                comment = _txt(el, "comments/mastercomment1") or ""
                iid = comment[len("nle-bridge:"):] if comment.startswith("nle-bridge:") else f"{'V' if kind == 'video' else 'A'}{ti + 1}-{len(items) + 1}"
                it: Dict[str, Any] = {"id": iid, "kind": "clip", "asset": asset_for(fe.get("id")),
                                      "name": _txt(el, "name") or "", "source_in": cin, "source_out": cin + (e - s),
                                      "record_in": s, "record_out": e,
                                      "enabled": (_txt(el, "enabled") or "TRUE").upper() != "FALSE"}
                if head and head["dissolve"] and head["end"] > head["start"]:
                    it["transition_in"] = {"type": "dissolve", "frames": head["end"] - head["start"],
                                           "before": head["cut"] - head["start"]}
                if e - s != cout - cin - handles:
                    warns.append(warning("SPEED_NOT_IMPORTED", f"{it['name']}: media length {cout - cin} != sequence "
                                         f"length {e - s} frames (a speed change) is not taken into the plan",
                                         item=iid, fmt="fcp7"))
                for eff in el.findall("filter/effect"):
                    eid = _txt(eff, "effectid") or _txt(eff, "name") or ""
                    if eid == "audiolevels":
                        v = _txt(eff, "parameter/value")
                        try:
                            lin = float(v) if v is not None else 1.0
                            it["gain_db"] = round(20 * math.log10(lin), 3) if lin > 0 else -144.0
                        except ValueError:
                            pass
                    elif eid in ("timeremap",):
                        warns.append(warning("SPEED_NOT_IMPORTED", f"{it['name']}: time remap is not taken into the plan",
                                             item=iid, fmt="fcp7"))
                    else:
                        warns.append(warning("EFFECT_NOT_IMPORTED", f"{it['name']}: filter {eid} is not taken into the plan",
                                             item=iid, fmt="fcp7"))
                items.append(it)
            # Premiere writes one track per audio channel: a stereo clip is two linked clipitems on A1 and A2.
            tracks.append({"kind": kind, "name": f"{'V' if kind == 'video' else 'A'}{ti + 1}", "items": items})
    markers = []
    for m in seq.findall("marker"):
        at = int(float(_txt(m, "in") or 0))
        out = int(float(_txt(m, "out") or -1))
        name = _txt(m, "name") or ""
        grid = _GRID_NAME.match(name)
        markers.append({"at_frame": at, "duration_frames": out - at if out > at else 0, "name": name,
                        "comment": _txt(m, "comment") or "",
                        "color": ("RED" if name.startswith("Bar ") else "CYAN") if grid else DEFAULT_MARKER_COLOR,
                        "kind": ("bar" if name.startswith("Bar ") else "beat") if grid else "marker"})
    w = _txt(media, "video/format/samplecharacteristics/width") if media is not None else None
    h = _txt(media, "video/format/samplecharacteristics/height") if media is not None else None
    return {"name": _txt(seq, "name") or name_hint, "rate": rate_str(rate), "drop_frame": drop,
            "start_timecode": frames_to_timecode(start, rate, drop), "width": int(w) if w else None,
            "height": int(h) if h else None, "assets": assets, "tracks": [t for t in tracks if t["items"]] or tracks,
            "markers": markers, "warnings": warns, "source": {"format": "fcp7"}}


def parse_check(text: str) -> bool:
    try:
        return ET.fromstring(text.encode("utf-8")).tag == "xmeml"
    except ET.ParseError:
        return False

