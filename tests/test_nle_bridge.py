"""nle-bridge-skill tests.

Unit tests need nothing but the standard library. Three groups need more and say so when they skip:
- `OtioCrossReadTests`: opentimelineio with its FCP7 XML and CMX 3600 adapters (`pip install -e '.[test]'`), the
  reference implementation reading this skill's .otio / .xml / .edl and writing timelines this skill reads back.
- `ConformanceTests`: a Velisa checkout (VELISA_DIR, AI_VIDEO_PRODUCTION_OS_DIR, ../Velisa or ../AI-video-production-OS)
  for the registry conformance checks, and jsonschema for the contract schema.
- `RealProbeTests`: ffmpeg on PATH and an ffmpeg-skill checkout (FFMPEG_SKILL_DIR or ../ffmpeg-skill).

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nle_bridge import __version__, cli, engine, srt  # noqa: E402
from nle_bridge.beats import load_grid, nearest  # noqa: E402
from nle_bridge.conform import package  # noqa: E402
from nle_bridge.contract import ERRORS, LOSS_CODES, contract  # noqa: E402
from nle_bridge.diff import diff  # noqa: E402
from nle_bridge.export import add_beats, content_warnings, render  # noqa: E402
from nle_bridge.model import NleBridgeError, normalise, path_to_url, url_to_path  # noqa: E402
from nle_bridge.plan import load, load_returned  # noqa: E402
from nle_bridge.timecode import (TimecodeError, frames_to_timecode, parse_rate, seconds_to_frames,  # noqa: E402
                                 timecode_to_frames)

HAVE_OTIO = importlib.util.find_spec("opentimelineio") is not None
HAVE_ADAPTERS = HAVE_OTIO and all(importlib.util.find_spec(m) is not None for m in ("otio_fcp_adapter", "otio_cmx3600_adapter"))
OTIO_WHY = "needs opentimelineio with otio-fcp-adapter and otio-cmx3600-adapter (pip install -e '.[test]')"


def _sibling(envs, names):
    for cand in [os.environ.get(e) for e in envs] + [str(ROOT.parent / n) for n in names]:
        if cand and os.path.isdir(cand):
            return Path(cand)
    return None


VELISA_DIR = _sibling(("VELISA_DIR", "AI_VIDEO_PRODUCTION_OS_DIR"), ("Velisa", "AI-video-production-OS"))
FFMPEG_SKILL_DIR = _sibling(("FFMPEG_SKILL_DIR",), ("ffmpeg-skill",))
if FFMPEG_SKILL_DIR and not (FFMPEG_SKILL_DIR / "scripts" / "probe.py").is_file():
    FFMPEG_SKILL_DIR = None
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

RATES = {  # name -> (rate, drop_frame)
    "23.976": ("24000/1001", False), "25": ("25", False), "29.97 NDF": ("30000/1001", False),
    "29.97 DF": ("30000/1001", True), "59.94 NDF": ("60000/1001", False), "59.94 DF": ("60000/1001", True),
}


def run_cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main([str(a) for a in argv])
    out = buf.getvalue()
    try:
        doc = json.loads(out) if out.strip() else None
    except ValueError:
        doc = None
    return code, doc, out


def sample_plan(rate="25", drop=False, media_dir="/media"):
    """Two cameras and a music bed; a level, three markers, captions; all in seconds."""
    return {"schema": "nle-bridge/timeline@1", "name": "demo", "rate": rate, "drop_frame": drop, "width": 1920,
            "height": 1080,
            "assets": {"a": {"path": f"{media_dir}/cam a.mov", "duration": 120, "start_timecode": "10:00:00:00"},
                       "b": {"path": f"{media_dir}/cam_b.mov", "duration": 60},
                       "m": {"path": f"{media_dir}/music.wav", "duration": 90, "has_video": False}},
            "tracks": [{"kind": "video", "items": [{"id": "c1", "asset": "a", "in": 1, "out": 5},
                                                   {"id": "c2", "asset": "b", "in": 2, "out": 4.5},
                                                   {"id": "c3", "asset": "a", "in": 30, "out": 33, "at": 8}]},
                       {"kind": "audio", "items": [{"id": "c1a", "asset": "a", "in": 1, "out": 5, "gain_db": -6},
                                                   {"id": "c2a", "asset": "b", "in": 2, "out": 4.5}]},
                       {"kind": "audio", "name": "Music", "items": [{"id": "mus", "asset": "m", "in": 0, "out": 11}]}],
            "markers": [{"at": 1, "name": "Intro", "comment": "open"}, {"at": 4, "name": "Cut to B", "color": "YELLOW"},
                        {"at": 9.5, "name": "Outro", "color": "BLUE"}],
            "captions": [{"start": 0.5, "end": 2.0, "text": "こんにちは"}, {"start": 2.5, "end": 4.0, "text": "two\nlines"}]}


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nle-bridge-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, doc):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False))
        return p

    def export(self, plan, formats="otio,fcp7,edl,srt", *extra, name="demo"):
        p = plan if isinstance(plan, str) else self.write("plan.json", plan)
        out = os.path.join(self.tmp, "out")
        code, doc, raw = run_cli("export", p, "-o", out, "--formats", formats, "--overwrite", "--json", *extra)
        self.assertEqual(code, 0, raw)
        return p, {f["format"]: f["path"] for f in doc["files"]}, doc


# ------------------------------------------------------------------------------------------------ timecode
class TimecodeTests(unittest.TestCase):
    def test_rates_are_exact(self):
        self.assertEqual(parse_rate(23.976), Fraction(24000, 1001))
        self.assertEqual(parse_rate("29.97"), Fraction(30000, 1001))
        self.assertEqual(parse_rate(59.94), Fraction(60000, 1001))
        self.assertEqual(parse_rate("30000/1001"), Fraction(30000, 1001))
        self.assertEqual(parse_rate(29.97002997), Fraction(30000, 1001))
        self.assertEqual(parse_rate(25), Fraction(25))
        for bad in (0, -1, "x", "1/0", None, float("nan")):
            with self.assertRaises(TimecodeError):
                parse_rate(bad)

    def test_ndf_anchors(self):
        r = Fraction(24000, 1001)
        self.assertEqual(frames_to_timecode(86400, r), "01:00:00:00")  # 24 frames per timecode second
        self.assertEqual(timecode_to_frames("00:00:01:23", r), 47)
        self.assertEqual(frames_to_timecode(90000, Fraction(25)), "01:00:00:00")
        self.assertEqual(frames_to_timecode(108000, Fraction(30000, 1001)), "01:00:00:00")
        self.assertEqual(frames_to_timecode(216000, Fraction(60000, 1001)), "01:00:00:00")
        with self.assertRaises(TimecodeError):
            timecode_to_frames("00:00:00:25", Fraction(25))

    def test_drop_frame_anchors_2997(self):
        r = Fraction(30000, 1001)
        self.assertEqual(frames_to_timecode(1799, r, True), "00:00:59;29")
        self.assertEqual(frames_to_timecode(1800, r, True), "00:01:00;02")
        self.assertEqual(frames_to_timecode(17982, r, True), "00:10:00;00")
        self.assertEqual(frames_to_timecode(107892, r, True), "01:00:00;00")  # one hour of clock
        self.assertEqual(timecode_to_frames("01:00:00;00", r, True), 107892)
        self.assertEqual(timecode_to_frames("00:01:00;02", r, True), 1800)
        with self.assertRaises(TimecodeError):
            timecode_to_frames("00:01:00;00", r, True)  # skipped label
        self.assertEqual(timecode_to_frames("00:10:00;00", r, True), 17982)  # tenth minute keeps its frames

    def test_drop_frame_anchors_5994(self):
        r = Fraction(60000, 1001)
        self.assertEqual(frames_to_timecode(3600, r, True), "00:01:00;04")
        self.assertEqual(frames_to_timecode(215784, r, True), "01:00:00;00")
        with self.assertRaises(TimecodeError):
            timecode_to_frames("00:01:00;03", r, True)

    def test_drop_frame_round_trips_every_frame(self):
        for r in (Fraction(30000, 1001), Fraction(60000, 1001)):
            for f in list(range(0, 40000)) + list(range(2155000, 2160000)):
                self.assertEqual(timecode_to_frames(frames_to_timecode(f, r, True), r, True), f)

    def test_drop_frame_only_at_ntsc_30_and_60(self):
        for r in (Fraction(25), Fraction(24000, 1001), Fraction(24)):
            with self.assertRaises(TimecodeError):
                frames_to_timecode(10, r, True)

    def test_drop_frame_tracks_the_clock(self):
        r = Fraction(30000, 1001)
        f = seconds_to_frames(3600, r)  # one hour of clock
        self.assertEqual(frames_to_timecode(f, r, True), "01:00:00;00")
        self.assertEqual(frames_to_timecode(f, r, False), "00:59:56:12")  # NDF runs 3.6 s slow per hour

    def test_seconds_to_frames_has_no_float_drift(self):
        r = Fraction(30000, 1001)
        self.assertEqual(seconds_to_frames(Fraction(1001, 30000), r), 1)
        self.assertEqual(seconds_to_frames("0.0333667", r), 1)
        self.assertEqual(seconds_to_frames(1.001, r), 30)
        self.assertEqual(seconds_to_frames(0.02, Fraction(25)), 0)  # half a frame: ties to even, as ffmpeg-skill
        self.assertEqual(seconds_to_frames(0.06, Fraction(25)), 2)
        self.assertEqual(seconds_to_frames(0.021, Fraction(25)), 1)


# ------------------------------------------------------------------------------------------------ model
class ModelTests(TmpCase):
    def test_seconds_form_becomes_frames(self):
        tl = normalise(sample_plan("25"))
        v = tl["tracks"][0]["items"]
        self.assertEqual([(i["source_in"], i["source_out"], i["record_in"], i["record_out"]) for i in v],
                         [(25, 125, 0, 100), (50, 112, 100, 162), (750, 825, 200, 275)])
        self.assertEqual(tl["markers"][0]["at_frame"], 25)
        self.assertEqual(tl["start_timecode"], "01:00:00:00")
        self.assertEqual(normalise(tl), tl)  # idempotent

    def test_speed_sets_record_length(self):
        p = sample_plan("25")
        p["tracks"][0]["items"][1]["speed"] = 2
        it = normalise(p)["tracks"][0]["items"][1]
        self.assertEqual((it["source_out"] - it["source_in"], it["record_out"] - it["record_in"], it["speed"]), (62, 31, 2.0))

    def test_refusals(self):
        cases = [
            (lambda p: p["tracks"][0]["items"][0].update(asset="nope"), "not in assets"),
            (lambda p: p["tracks"][0]["items"][1].update(at=2), "overlap"),
            (lambda p: p["tracks"][0]["items"][0].update(out=0.5), "empty"),
            (lambda p: p["markers"][0].update(color="TEAL"), "color"),
            (lambda p: p.update(rate="abc"), "rate"),
            (lambda p: p.update(drop_frame=True), "drop-frame"),
            (lambda p: p.update(tracks=[]), "no clips"),
            (lambda p: p["assets"]["a"].pop("path"), "path"),
            (lambda p: p["tracks"][0]["items"].append({"id": "c1", "asset": "a", "in": 0, "out": 1, "at": 50}), "duplicate"),
        ]
        for mutate, msg in cases:
            p = sample_plan("25")
            mutate(p)
            with self.assertRaises(NleBridgeError) as cm:
                normalise(p)
            self.assertEqual(cm.exception.code, "INVALID_PLAN")
            self.assertIn(msg, str(cm.exception))

    def test_urls_round_trip_posix_windows_unc_unicode(self):
        for p in ("/Volumes/Media/日本語 clip #1%.mov", "C:\\Media\\cam a.mov", "/a/b.mxf", "//server/share/x y.mov"):
            u = path_to_url(p)
            self.assertTrue(u.startswith("file:"))
            self.assertNotIn(" ", u)
            self.assertEqual(url_to_path(u), p.replace("\\", "/") if p.startswith("C:") else p)
        self.assertEqual(url_to_path("file:///Users/x/a%20b.mov"), "/Users/x/a b.mov")
        self.assertEqual(url_to_path("file:///C:/x/a.mov"), "C:/x/a.mov")

    def test_relative_paths_resolve_from_the_plan(self):
        p = sample_plan("25")
        p["assets"]["a"]["path"] = "media/a.mov"
        path = self.write("plan.json", p)
        tl = load(path)
        self.assertEqual(tl["assets"]["a"]["path"], os.path.join(self.tmp, "media", "a.mov"))


# ------------------------------------------------------------------------------------------------ export round trip
class RoundTripTests(TmpCase):
    def test_every_format_reads_back_identical_at_every_rate(self):
        for label, (rate, drop) in RATES.items():
            with self.subTest(rate=label):
                plan_path, files, doc = self.export(sample_plan(rate, drop))
                orig = load(plan_path)
                for fmt in ("otio", "fcp7", "edl"):
                    ret = load_returned(orig, files[fmt])
                    d = diff(orig, ret)
                    self.assertTrue(d["summary"]["identical"], (label, fmt, d["summary"], d["clips"], d["markers"]))
                    self.assertEqual(ret["rate"], orig["rate"])
                    self.assertEqual(ret["drop_frame"], drop, (label, fmt))
                    self.assertEqual(ret["start_timecode"], orig["start_timecode"], (label, fmt))

    def test_source_timecode_and_record_timecode_in_the_edl(self):
        _, files, _ = self.export(sample_plan("30000/1001", True), "edl")
        text = Path(files["edl"]).read_text(encoding="utf-8")
        self.assertIn("FCM: DROP FRAME", text)
        # c1: media starts 10:00:00;00, in 1 s = 30 frames; record starts at the hour
        self.assertIn("001  AX       V     C        10:00:01;00 10:00:05;00 01:00:00;00 01:00:04;00", text)
        self.assertIn("* LOC: 01:00:04;00 YELLOW  Cut to B", text)
        self.assertIn("* SOURCE FILE: /media/cam a.mov", text)
        _, files, _ = self.export(sample_plan("24000/1001"), "edl")
        text = Path(files["edl"]).read_text(encoding="utf-8")
        self.assertIn("FCM: NON-DROP FRAME", text)
        self.assertIn("10:00:01:00 10:00:05:00 01:00:00:00 01:00:04:00", text)

    def test_fcp7_rate_timecode_and_levels(self):
        _, files, _ = self.export(sample_plan("60000/1001", True), "fcp7")
        text = Path(files["fcp7"]).read_text(encoding="utf-8")
        self.assertIn("<timebase>60</timebase>", text)
        self.assertIn("<ntsc>TRUE</ntsc>", text)
        self.assertIn("<displayformat>DF</displayformat>", text)
        self.assertIn("<string>01:00:00;00</string>", text)
        self.assertIn("<frame>215784</frame>", text)
        self.assertIn("<pathurl>file://localhost/media/cam%20a.mov</pathurl>", text)
        self.assertIn("<value>0.501187</value>", text)  # -6 dB
        ret = load(files["fcp7"])
        a1 = next(t for t in ret["tracks"] if t["kind"] == "audio")
        self.assertAlmostEqual(a1["items"][0]["gain_db"], -6.0, places=3)

    def test_otio_rates_and_media_start(self):
        _, files, _ = self.export(sample_plan("24000/1001"), "otio")
        doc = json.loads(Path(files["otio"]).read_text(encoding="utf-8"))
        self.assertEqual(doc["OTIO_SCHEMA"], "Timeline.1")
        self.assertAlmostEqual(doc["global_start_time"]["rate"], 24000 / 1001)
        self.assertEqual(doc["global_start_time"]["value"], 86400.0)
        clip = doc["tracks"]["children"][0]["children"][0]
        self.assertEqual(clip["source_range"]["start_time"]["value"], 864000 + 24)  # 10:00:00:00 + 1 s at 24 tc fps

    def test_srt_sidecar(self):
        _, files, _ = self.export(sample_plan("25"))
        text = Path(files["srt"]).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("1\n00:00:00,500 --> 00:00:02,000\nこんにちは\n"))
        self.assertEqual(srt.read(text), [{"start": 0.5, "end": 2.0, "text": "こんにちは"},
                                          {"start": 2.5, "end": 4.0, "text": "two\nlines"}])

    def test_no_srt_without_captions_and_a_warning_when_it_is_left_out(self):
        p = sample_plan("25")
        p["captions"] = []
        _, files, _ = self.export(p)
        self.assertNotIn("srt", files)
        _, files, doc = self.export(sample_plan("25"), "otio")
        self.assertIn("CAPTIONS_NOT_EXPORTED", [w["code"] for w in doc["warnings"]])

    def test_deterministic_bytes(self):
        _, f1, _ = self.export(sample_plan("29.97", True))
        first = {k: Path(v).read_bytes() for k, v in f1.items()}
        _, f2, _ = self.export(sample_plan("29.97", True))
        self.assertEqual(first, {k: Path(v).read_bytes() for k, v in f2.items()})

    def test_gaps_and_a_clip_at_zero_survive(self):
        p = sample_plan("25")
        p["tracks"][0]["items"][2]["at"] = 30  # a 20 s gap before c3
        plan_path, files, _ = self.export(p)
        orig = load(plan_path)
        for fmt in ("otio", "fcp7", "edl"):
            ret = load_returned(orig, files[fmt])
            c3 = next(i for t in ret["tracks"] for i in t["items"] if t["kind"] == "video" and i["record_in"] > 200)
            self.assertEqual(c3["record_in"], 750, fmt)
            self.assertTrue(diff(orig, ret)["summary"]["identical"], fmt)


# ------------------------------------------------------------------------------------------------ warnings
class WarningTests(TmpCase):
    def test_what_cannot_be_exported_is_named_never_dropped(self):
        p = sample_plan("25")
        v = p["tracks"][0]["items"]
        v[1]["speed"] = 2
        v[2]["effects"] = [{"name": "gaussian blur"}]
        v[2]["transition_in"] = {"type": "dissolve", "duration": 1}
        v.append({"id": "t1", "kind": "title", "text": "Hello", "at": 12, "duration": 2})
        p["tracks"].append({"kind": "video", "name": "V2", "items": [{"id": "o1", "asset": "b", "in": 0, "out": 1}]})
        _, files, doc = self.export(p)
        codes = {(w["code"], w["format"], w["item"]) for w in doc["warnings"]}
        for want in [("SPEED_NOT_EXPORTED", "all", "c2"), ("EFFECT_NOT_EXPORTED", "all", "c3"),
                     ("TRANSITION_NOT_EXPORTED", "all", "c3"), ("TITLE_NOT_EXPORTED", "all", "t1"),
                     ("LEVEL_METADATA_ONLY", "otio", "c1a"), ("LEVEL_NOT_EXPORTED", "edl", "c1a"),
                     ("TRACK_NOT_EXPORTED", "edl", None), ("MARKER_COLOR_NOT_EXPORTED", "fcp7", None)]:
            self.assertIn(want, codes)
        for w in doc["warnings"]:
            self.assertTrue(w["message"])
        # the second video track is in OTIO and FCP7, not in the EDL
        self.assertEqual(len([t for t in load(files["otio"])["tracks"] if t["kind"] == "video"]), 2)
        self.assertEqual(len([t for t in load(files["fcp7"])["tracks"] if t["kind"] == "video"]), 2)
        self.assertEqual(len([t for t in load(files["edl"], rate=25)["tracks"] if t["kind"] == "video"]), 1)
        # a speed change keeps the cut points: c2 is 31 frames on the timeline (62 / 2), written at 100 %
        c2 = next(i for i in load(files["otio"])["tracks"][0]["items"] if i["id"] == "c2")
        self.assertEqual((c2["record_in"], c2["record_out"], c2["source_in"], c2["source_out"]), (100, 131, 50, 81))

    def test_strict_refuses_before_writing(self):
        p = sample_plan("25")
        p["tracks"][0]["items"][1]["speed"] = 2
        path = self.write("plan.json", p)
        out = os.path.join(self.tmp, "strict")
        code, doc, _ = run_cli("export", path, "-o", out, "--strict", "--json")
        self.assertEqual(code, 5)
        self.assertEqual(doc["error"]["code"], "CONTENT_NOT_EXPORTED")
        self.assertFalse(os.path.exists(out))
        p = sample_plan("25")
        for t in p["tracks"]:
            for it in t["items"]:
                it.pop("gain_db", None)
        p["markers"] = [{"at": 1, "name": "Intro"}]
        code, doc, raw = run_cli("export", self.write("clean.json", p), "-o", out, "--strict", "--json")
        self.assertEqual(code, 0, raw)

    def test_loss_codes_cover_every_writer_warning(self):
        p = sample_plan("25")
        p["tracks"][0]["items"][1]["speed"] = 2
        p["tracks"].append({"kind": "video", "name": "V2", "items": [{"id": "o1", "asset": "b", "in": 0, "out": 1}]})
        res = render(normalise(p), ["otio", "fcp7", "edl", "srt"])
        self.assertTrue(res["warnings"])
        for w in res["warnings"]:
            self.assertIn(w["code"], LOSS_CODES)

    def test_rate_mismatch_is_named(self):
        p = sample_plan("25")
        p["assets"]["b"]["fps"] = 29.97
        self.assertIn("RATE_MISMATCH", [w["code"] for w in content_warnings(normalise(p))])


# ------------------------------------------------------------------------------------------------ beats
FFMPEG_SKILL_BEATS = {  # the shape of ffmpeg-skill scenes --beats --json (120 BPM, one weak beat unsupported)
    "duration": 12.0, "beats": [round(0.5 * i, 3) for i in range(24)],
    "beat_grid": {"supported_beats": [round(0.5 * i, 3) for i in range(24) if i != 5], "tempo_bpm": 120.0, "interval": 0.5,
                  "confidence": 0.97, "phase": 0.0, "onsets": 23, "supported": 23, "unsupported": 1, "method": "autocorr",
                  "step_s": 0.01, "range_bpm": [60, 200], "usable": True}}


class BeatTests(TmpCase):
    def test_ffmpeg_skill_grid_assumes_bars(self):
        g = load_grid(FFMPEG_SKILL_BEATS)
        self.assertEqual(g["source"], "ffmpeg-skill/scenes --beats")
        self.assertEqual(g["bars_provenance"], "ASSUMED")
        self.assertIn("BARS_ASSUMED", [w["code"] for w in g["warnings"]])
        self.assertEqual(len(g["beats"]), 23)  # supported only, by default
        self.assertNotIn(2.5, [b["t"] for b in g["beats"]])
        self.assertEqual(g["bars"][:3], [0.0, 2.0, 4.0])
        b = next(x for x in g["beats"] if x["t"] == 3.0)  # beat index 6 -> bar 2 beat 3, even with a gap before it
        self.assertEqual((b["bar"], b["beat"]), (2, 3))
        self.assertEqual(len(load_grid(FFMPEG_SKILL_BEATS, beat_set="grid")["beats"]), 24)
        self.assertEqual(load_grid(FFMPEG_SKILL_BEATS["beat_grid"])["source"], "beat_grid")

    def test_native_grid_with_stated_bars_and_three_four(self):
        g = load_grid({"schema": "nle-bridge/beatgrid@1", "bpm": 90, "beats": [0.5 + i * 2 / 3 for i in range(12)],
                       "bars": [0.5, 2.5, 4.5, 6.5], "beats_per_bar": 3})
        self.assertEqual(g["bars_provenance"], "STATED")
        self.assertFalse(g["warnings"])
        self.assertEqual([x["beat"] for x in g["beats"][:4]], [1, 2, 3, 1])
        gen = load_grid({"bpm": 120, "duration": 4, "first_downbeat": 0.25})
        self.assertEqual(gen["bars_provenance"], "STATED")
        self.assertEqual(gen["bars"], [0.25, 2.25])

    def test_unusable_and_invalid_grids(self):
        bad = copy.deepcopy(FFMPEG_SKILL_BEATS)
        bad["beat_grid"]["usable"] = False
        self.assertIn("BEAT_GRID_UNUSABLE", [w["code"] for w in load_grid(bad)["warnings"]])
        for doc in ({}, {"beats": []}, {"bpm": 120}, {"beats": ["x"]}, [1, 2]):
            with self.assertRaises(NleBridgeError):
                load_grid(doc)

    def test_beat_markers_in_every_format_with_bar_colours(self):
        p = sample_plan("25")
        grid = self.write("beats.json", FFMPEG_SKILL_BEATS)
        _, files, doc = self.export(p, "otio,fcp7,edl", "--beats", grid)
        self.assertEqual(doc["beats"]["bars_provenance"], "ASSUMED")
        otio_doc = json.loads(Path(files["otio"]).read_text(encoding="utf-8"))
        marks = otio_doc["tracks"]["markers"]
        bars = [m for m in marks if m["name"].startswith("Bar ")]
        beats = [m for m in marks if m["name"].startswith("Beat ")]
        self.assertEqual({m["color"] for m in bars}, {"RED"})
        self.assertEqual({m["color"] for m in beats}, {"CYAN"})
        self.assertEqual(len(bars) + len(beats), 21)  # grid beats inside the 11 s timeline (22) less the unsupported one
        self.assertEqual([m["marked_range"]["start_time"]["value"] for m in bars[:3]], [0.0, 50.0, 100.0])
        edl_text = Path(files["edl"]).read_text(encoding="utf-8")
        self.assertIn("* LOC: 01:00:02:00 RED     Bar 2", edl_text)
        self.assertIn("* LOC: 01:00:00:12 CYAN    Beat 1.2", edl_text)  # 0.5 s at 25 fps = 12.5 frames, ties to even
        xml = Path(files["fcp7"]).read_text(encoding="utf-8")
        self.assertIn("<name>Bar 2</name>", xml)
        # read back: kinds and colours survive OTIO and EDL; FCP7 recovers the kind from the name
        for fmt in ("otio", "fcp7", "edl"):
            ret = load(files[fmt], rate=25 if fmt == "edl" else None)
            kinds = [m["kind"] for m in ret["markers"]]
            self.assertEqual(kinds.count("bar"), len(bars), fmt)
            self.assertEqual(kinds.count("beat"), len(beats), fmt)
        _, files, _ = self.export(p, "otio", "--beats", grid, "--beat-markers", "bars")
        names = [m["name"] for m in json.loads(Path(files["otio"]).read_text())["tracks"]["markers"]]
        self.assertFalse([n for n in names if n.startswith("Beat ")])

    def test_offset_moves_the_grid(self):
        g = load_grid(FFMPEG_SKILL_BEATS, offset=1.0)
        self.assertEqual(g["beats"][0]["t"], 1.0)
        tl = add_beats(normalise(sample_plan("25")), g, "all")
        self.assertEqual(min(m["at_frame"] for m in tl["markers"] if m["kind"] == "bar"), 25)

    def test_nearest_beat_offsets_in_frames(self):
        g = load_grid(FFMPEG_SKILL_BEATS, beat_set="grid")
        r = Fraction(25)
        self.assertEqual(nearest(g, 50, r, 0.5)["offset_frames"], 0.0)  # 2.0 s = bar 2
        n = nearest(g, 53, r, 0.5)
        self.assertEqual((n["beat"], n["offset_frames"], n["on_beat"]), ("2.1", 3.0, False))
        n = nearest(g, 61, r, 0.5)  # 2.44 s: 1.5 frames before beat 2.2 at 2.5 s (62.5 frames)
        self.assertEqual((n["beat"], n["offset_frames"]), ("2.2", -1.5))
        r = Fraction(30000, 1001)  # beats are not on frame boundaries at 29.97
        n = nearest(g, seconds_to_frames(2.0, r), r, 0.5)
        self.assertAlmostEqual(n["offset_frames"], 60 - 2.0 * 30000 / 1001, places=3)
        self.assertTrue(n["on_beat"])


# ------------------------------------------------------------------------------------------------ round trip diff
class DiffTests(TmpCase):
    def _edit_otio(self, path, edit):
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        edit(doc)
        out = os.path.join(self.tmp, "returned.otio")
        Path(out).write_text(json.dumps(doc), encoding="utf-8")
        return out

    def test_editor_changes_are_reported(self):
        plan_path, files, _ = self.export(sample_plan("25"), "otio")

        def edit(doc):
            v = doc["tracks"]["children"][0]["children"]
            # c1 trimmed: 10 frames off its tail (ripple: everything after moves 10 frames earlier)
            v[0]["source_range"]["duration"]["value"] -= 10
            # c2 removed, a gap keeps the time; c3 is split in two by a razor cut
            gap = {"OTIO_SCHEMA": "Gap.1", "name": "", "metadata": {}, "effects": [], "markers": [], "enabled": True,
                   "source_range": {"OTIO_SCHEMA": "TimeRange.1",
                                    "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": 25.0, "value": 0.0},
                                    "duration": copy.deepcopy(v[1]["source_range"]["duration"])}}
            c3 = v[3]
            c3a, c3b = copy.deepcopy(c3), copy.deepcopy(c3)
            c3a["source_range"]["duration"]["value"] = 30.0
            c3b["source_range"]["start_time"]["value"] += 30
            c3b["source_range"]["duration"]["value"] = 45.0
            c3b["metadata"] = {}
            v[1:] = [gap, v[2], c3a, c3b]
            # an editor marker, and Outro moved 5 frames
            doc["tracks"]["markers"].append({"OTIO_SCHEMA": "Marker.2", "name": "Fix colour", "color": "RED", "metadata": {},
                                             "comment": "", "marked_range": {"OTIO_SCHEMA": "TimeRange.1",
                                             "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": 25.0, "value": 40.0},
                                             "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": 25.0, "value": 0.0}}})
            for m in doc["tracks"]["markers"]:
                if m["name"] == "Outro":
                    m["marked_range"]["start_time"]["value"] += 5
        ret_path = self._edit_otio(files["otio"], edit)
        code, d, raw = run_cli("diff", plan_path, ret_path, "--json", "--timeline-out", os.path.join(self.tmp, "v2.json"))
        self.assertEqual(code, 0, raw)
        s = d["summary"]
        self.assertFalse(s["identical"])
        c1 = next(c for c in d["clips"] if c["plan_id"] == "c1")
        self.assertEqual(c1["changes"], ["trimmed"])
        self.assertEqual(c1["delta_frames"], {"record_in": 0, "record_out": -10, "source_in": 0, "source_out": -10})
        self.assertIn("c2", [r["plan_id"] for r in d["removed"] if r["track"] == "V1"])
        c3 = next(c for c in d["clips"] if c["plan_id"] == "c3")
        self.assertEqual(c3["delta_frames"]["record_in"], -10)
        self.assertEqual([a["split_from"] for a in d["added"] if a["track"] == "V1"], ["c3"])
        moved = [(c["plan_tc"], c["returned_tc"], c["delta_frames"]) for c in d["cuts"] if c["status"] == "moved"]
        self.assertIn(("01:00:04:00", "01:00:03:15", -10), moved)
        self.assertIn(("01:00:08:00", "01:00:07:15", -10), moved)
        self.assertIn("01:00:08:20", [c["returned_tc"] for c in d["cuts"] if c["status"] == "added"])  # the razor cut
        self.assertEqual([m["name"] for m in d["markers"]["added"]], ["Fix colour"])
        self.assertEqual([(m["name"], m["changes"], m["delta_frames"]) for m in d["markers"]["changed"]],
                         [("Outro", ["moved"], 5)])
        v2 = load(os.path.join(self.tmp, "v2.json"))
        self.assertEqual(v2["assets"]["a"]["path"], "/media/cam a.mov")  # mapped back to the plan's ids
        self.assertEqual(v2["source"]["provenance"], "EDITOR")

    def test_cut_offsets_against_beats(self):
        plan = sample_plan("25")
        plan["tracks"] = [{"kind": "video", "items": [{"id": "c1", "asset": "a", "in": 0, "out": 2},  # cut at 2.0 s (bar 2)
                                                      {"id": "c2", "asset": "b", "in": 0, "out": 2.5},  # cut at 4.5 s
                                                      {"id": "c3", "asset": "a", "in": 10, "out": 12}]}]
        plan["markers"] = []
        grid = self.write("beats.json", FFMPEG_SKILL_BEATS)
        plan_path, files, _ = self.export(plan, "otio", "--beats", grid)

        def edit(doc):  # the editor rolls the first cut 3 frames later (c1 +3, c2 -3 at the head)
            v = doc["tracks"]["children"][0]["children"]
            v[0]["source_range"]["duration"]["value"] += 3
            v[1]["source_range"]["start_time"]["value"] += 3
            v[1]["source_range"]["duration"]["value"] -= 3
        ret = self._edit_otio(files["otio"], edit)
        code, d, raw = run_cli("diff", plan_path, ret, "--beats", grid, "--beat-set", "grid", "--json")
        self.assertEqual(code, 0, raw)
        cuts = [c for c in d["cuts"] if c["plan_frame"] is not None]
        first, second = cuts[0], cuts[1]
        self.assertEqual((first["plan_frame"], first["returned_frame"], first["delta_frames"]), (50, 53, 3))
        self.assertEqual((first["plan_beat"]["offset_frames"], first["plan_beat"]["on_beat"]), (0.0, True))
        self.assertEqual((first["beat"]["beat"], first["beat"]["offset_frames"], first["beat"]["on_beat"]), ("2.1", 3.0, False))
        self.assertTrue(first["beat"]["is_bar"])
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual((second["beat"]["offset_frames"], second["beat"]["on_beat"]), (-0.5, True))  # 4.5 s = 112.5 f, cut at 112
        b = d["beats"]
        self.assertEqual((b["plan_on_beat"], b["returned_on_beat"], b["moved_off_beat"]), (2, 1, 1))
        self.assertEqual(b["max_abs_offset_frames"], 3.0)
        # beat-grid markers that came back are counted, not reported as editor markers
        self.assertFalse(d["markers"]["added"] or d["markers"]["removed"])
        self.assertGreater(d["markers"]["grid_markers"]["returned"], 0)

    def test_fcp7_and_edl_returns(self):
        plan_path, files, _ = self.export(sample_plan("30000/1001", True), "fcp7,edl")
        xml = Path(files["fcp7"]).read_text(encoding="utf-8")
        # the editor drops the plan ids (another NLE), moves c3 one second later and adds a dissolve and a title
        xml = xml.replace("<mastercomment1>nle-bridge:c3</mastercomment1>", "")
        xml = xml.replace("<start>240</start>\n            <end>330</end>", "<start>270</start>\n            <end>360</end>", 1)
        xml = xml.replace("<enabled>TRUE</enabled>\n          <locked>FALSE</locked>",
                          "<transitionitem><effect><name>Cross Dissolve</name></effect></transitionitem>"
                          "<generatoritem><name>Title</name></generatoritem>"
                          "<enabled>TRUE</enabled>\n          <locked>FALSE</locked>", 1)
        ret_xml = self.write("returned.xml", xml)
        code, d, raw = run_cli("diff", plan_path, ret_xml, "--json")
        self.assertEqual(code, 0, raw)
        c3 = next(c for c in d["clips"] if c["plan_id"] == "c3" and c["track"] == "V1")
        self.assertEqual((c3["changes"], c3["delta_frames"]["record_in"]), (["moved"], 30))
        codes = [w["code"] for w in d["not_imported"]]
        self.assertIn("TRANSITION_NOT_IMPORTED", codes)
        self.assertIn("TITLE_NOT_IMPORTED", codes)
        edl_text = Path(files["edl"]).read_text(encoding="utf-8")
        edl_text = edl_text.replace("* NLE-BRIDGE ID: c2a\n", "")  # an NLE writes its own comments
        edl_text = edl_text.replace("* LOC: 01:00:09;15 BLUE    Outro", "* LOC: 01:00:09;20 BLUE    Outro")
        code, d, raw = run_cli("diff", plan_path, self.write("returned.edl", edl_text), "--json")
        self.assertEqual(code, 0, raw)
        self.assertEqual(d["summary"]["clips_changed"], 0)
        self.assertEqual(d["markers"]["changed"][0]["delta_frames"], 5)

    def test_rate_mismatch_is_refused(self):
        plan_path, files, _ = self.export(sample_plan("25"), "otio")
        other = self.write("other.json", sample_plan("30000/1001"))
        code, doc, _ = run_cli("diff", other, files["otio"], "--json")
        self.assertEqual(code, 2)
        self.assertEqual(doc["error"]["code"], "INVALID_ARGUMENT")


# ------------------------------------------------------------------------------------------------ inputs
def agent_ir(media_dir):
    return {"schema_version": "1.0", "project": {"id": "talk"},
            "assets": {"cam": {"id": "cam", "path": f"{media_dir}/cam.mp4", "type": "CAMERA", "hash": None,
                               "technical": {"duration": 60.0, "video": {"fps": 29.97, "width": 1920, "height": 1080},
                                             "audio": {"channels": 2}}},
                       "slides": {"id": "slides", "path": f"{media_dir}/slides.mp4", "type": "SCREEN_CAPTURE",
                                  "technical": {"duration": 60.0, "video": {"fps": 29.97, "width": 1920, "height": 1080},
                                                "audio": None}}},
            "decisions": [{"id": "D7", "subject": "timeline.export", "type": "DELIVER",
                           "params": {"asset_id": "programme", "format": "otio", "not_exported": ["video.speed", "audio.loudness"]}}],
            "captions": {"operations": [{"type": "captions.generate", "asset": "programme", "output": "x.srt", "format": "srt",
                                         "cues": [{"id": "q1", "start": 0.2, "end": 1.5, "text": "Hello"}]}]},
            "delivery": {"targets": [], "timelines": [
                {"type": "timeline.export", "asset": "programme", "format": "otio", "output": "artifacts/p_timeline.otio",
                 "clips": [{"asset": "cam", "in": 1.0, "out": 4.0}, {"asset": "slides", "in": 10.0, "out": 12.5},
                           {"asset": "cam", "in": 20.0, "out": 21.0}], "timeline_duration": 6.5, "decision_ids": ["D7"]},
                {"type": "timeline.export", "asset": "cam", "format": "edl", "output": "artifacts/cam.edl",
                 "clips": [{"asset": "cam", "in": 0.0, "out": 60.0}], "timeline_duration": 60.0, "decision_ids": []}]}}


class InputTests(TmpCase):
    def test_agent_project_ir(self):
        path = self.write("project.json", agent_ir("/m"))
        code, doc, _ = run_cli("export", path, "-o", os.path.join(self.tmp, "o"), "--json")
        self.assertEqual(code, 2)
        self.assertIn("--subject", doc["error"]["message"])
        tl = load(path, subject="programme")
        self.assertEqual(tl["rate"], "30000/1001")
        v = tl["tracks"][0]["items"]
        self.assertEqual([(i["asset"], i["source_in"], i["record_in"]) for i in v], [("cam", 30, 0), ("slides", 300, 90), ("cam", 599, 165)])
        self.assertEqual(sum(i["record_out"] - i["record_in"] for i in v), seconds_to_frames(6.5, parse_rate("29.97")))
        a1 = tl["tracks"][1]["items"]  # slides has no audio: A1 has the two camera clips, aligned with their pictures
        self.assertEqual([(i["record_in"], i["record_out"]) for i in a1], [(v[0]["record_in"], v[0]["record_out"]),
                                                                          (v[2]["record_in"], v[2]["record_out"])])
        self.assertEqual(tl["captions"], [{"start": 0.2, "end": 1.5, "text": "Hello"}])
        self.assertEqual([w["code"] for w in tl["warnings"]], ["NOT_IN_TIMELINE", "NOT_IN_TIMELINE"])
        self.assertEqual(tl["source"]["decision_ids"], ["D7"])
        self.assertEqual(len(load(path, subject="cam")["tracks"][0]["items"]), 1)
        with self.assertRaises(NleBridgeError):
            load(path, subject="nope")

    def test_ffmpeg_skill_render_project(self):
        proj = {"clips": [{"src": "a.mp4", "in": 1, "out": "0:05"}, {"src": "b.mp4", "in": "00:00:02.5", "out": 6, "speed": 1.5}],
                "frame": {"fps": 25, "width": 1280, "height": 720}, "chapters": [{"at": 0, "title": "Start"}, {"at": 3, "title": "B"}],
                "captions": "subs.srt", "graphics": [{"template": "lower_third"}], "loudness": {"target": -14},
                "audio": {"duck": True}, "output": "out/final.mp4"}
        self.write("subs.srt", "1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        tl = load(self.write("project.json", proj))
        self.assertEqual(tl["name"], "final")
        self.assertEqual([m["name"] for m in tl["markers"]], ["Start", "B"])
        self.assertEqual(tl["captions"][0]["text"], "hi")
        msgs = " | ".join(w["message"] for w in tl["warnings"])
        for want in ("transition", "graphics", "loudness", "duck"):
            self.assertIn(want, msgs)
        b = tl["tracks"][0]["items"][1]
        self.assertEqual((b["source_in"], b["source_out"], b["record_out"] - b["record_in"], b["speed"]), (62, 150, 59, 1.5))
        proj["clips"][0].pop("out")
        with self.assertRaises(NleBridgeError):
            load(self.write("project2.json", proj))

    def test_otio_written_by_the_reference_library_with_nle_extras(self):
        if not HAVE_OTIO:
            self.skipTest(OTIO_WHY)
        import opentimelineio as otio
        r = 24000 / 1001
        tl = otio.schema.Timeline(name="from resolve")
        tl.global_start_time = otio.opentime.RationalTime(86400, r)
        tr = otio.schema.Track(name="Video 1", kind=otio.schema.TrackKind.Video)
        tl.tracks.append(tr)
        ref = otio.schema.ExternalReference(target_url="file:///Volumes/A/cam%20a.mov",
                                            available_range=otio.opentime.TimeRange(otio.opentime.RationalTime(864000, r),
                                                                                    otio.opentime.RationalTime(2400, r)))
        c = otio.schema.Clip(name="cam a.mov", media_reference=ref,
                             source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(864024, r), otio.opentime.RationalTime(96, r)))
        c.markers.append(otio.schema.Marker(name="on clip", marked_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime(864048, r), otio.opentime.RationalTime(0, r)), color="PURPLE"))
        tr.append(c)
        tr.append(otio.schema.Transition(transition_type="SMPTE_Dissolve", in_offset=otio.opentime.RationalTime(6, r),
                                         out_offset=otio.opentime.RationalTime(6, r)))
        c2 = otio.schema.Clip(name="fast", media_reference=otio.schema.ExternalReference(target_url="file:///Volumes/A/b.mov"),
                              source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(0, r), otio.opentime.RationalTime(48, r)))
        c2.effects.append(otio.schema.LinearTimeWarp(time_scalar=2.0))
        tr.append(c2)
        tr.append(otio.schema.Clip(name="Title", media_reference=otio.schema.GeneratorReference(generator_kind="Text"),
                                   source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(0, r), otio.opentime.RationalTime(24, r))))
        path = os.path.join(self.tmp, "resolve.otio")
        otio.adapters.write_to_file(tl, path)
        got = load(path)
        self.assertEqual(got["rate"], "24000/1001")
        self.assertEqual(got["start_timecode"], "01:00:00:00")
        items = got["tracks"][0]["items"]
        self.assertEqual([(i["name"], i["source_in"], i["record_in"], i["record_out"]) for i in items],
                         [("cam a.mov", 24, 0, 96), ("fast", 0, 96, 144)])
        self.assertEqual(got["assets"][items[0]["asset"]]["path"], "/Volumes/A/cam a.mov")
        self.assertEqual(got["assets"][items[0]["asset"]]["start_timecode"], "10:00:00:00")
        self.assertEqual([(m["name"], m["at_frame"], m["color"]) for m in got["markers"]], [("on clip", 24, "PURPLE")])
        codes = sorted({w["code"] for w in got["warnings"]})
        self.assertEqual(codes, ["SPEED_NOT_IMPORTED", "TITLE_NOT_IMPORTED", "TRANSITION_NOT_IMPORTED"])

    def test_refusals(self):
        self.assertEqual(run_cli("import", os.path.join(self.tmp, "none.json"), "--json")[0], 2)
        p = self.write("x.fcpxml", "<fcpxml/>")
        code, doc, _ = run_cli("import", p, "--json")
        self.assertEqual(doc["error"]["code"], "UNSUPPORTED_FORMAT")
        p = self.write("x.edl", "TITLE: x\nFCM: NON-DROP FRAME\n001  AX V C 00:00:00:00 00:00:01:00 01:00:00:00 01:00:01:00\n")
        code, doc, _ = run_cli("import", p, "--json")
        self.assertEqual((code, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        code, doc, raw = run_cli("import", p, "--fps", "25", "--json")
        self.assertEqual(code, 0, raw)
        evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xmeml version="4"><sequence/></xmeml>'
        code, doc, _ = run_cli("import", self.write("evil.xml", evil), "--json")
        self.assertEqual(doc["error"]["code"], "INVALID_TIMELINE")
        code, doc, _ = run_cli("import", self.write("bad.json", {"hello": 1}), "--json")
        self.assertEqual(doc["error"]["code"], "INVALID_PLAN")
        p = self.write("df.edl", "FCM: DROP FRAME\n001  AX V C 00:00:00;00 00:00:01;00 01:00:00;00 01:00:01;00\n")
        code, doc, _ = run_cli("import", p, "--fps", "25", "--json")
        self.assertEqual(doc["error"]["code"], "INVALID_TIMELINE")

    def test_output_safety(self):
        path = self.write("plan.json", sample_plan("25"))
        out = os.path.join(self.tmp, "o")
        self.assertEqual(run_cli("export", path, "-o", out, "--json")[0], 0)
        code, doc, _ = run_cli("export", path, "-o", out, "--json")
        self.assertEqual((code, doc["error"]["code"]), (2, "OUTPUT_EXISTS"))
        code, doc, _ = run_cli("import", path, "-o", path, "--overwrite", "--json")
        self.assertEqual(doc["error"]["code"], "OUTPUT_IS_INPUT")
        code, doc, _ = run_cli("export", path, "-o", self.tmp, "--name", "plan", "--formats", "otio", "--json")
        self.assertEqual(code, 0)  # plan.otio next to plan.json is fine; plan.json itself is never written
        code, doc, _ = run_cli("export", path, "-o", out, "--name", "../x", "--overwrite", "--json")
        self.assertEqual(doc["error"]["code"], "INVALID_ARGUMENT")


# ------------------------------------------------------------------------------------------------ conform
class ConformTests(TmpCase):
    def _media(self):
        media = os.path.join(self.tmp, "media")
        os.makedirs(media)
        for name, size in (("cam a.mov", 3000), ("cam_b.mov", 2000), ("music.wav", 1000)):
            with open(os.path.join(media, name), "wb") as fh:
                fh.write(hashlib.sha256(name.encode()).digest() * (size // 32))
        return media

    def test_package_with_relink_manifest_and_deterministic_zip(self):
        media = self._media()
        plan = sample_plan("29.97", True, media_dir=media)
        plan["assets"]["b"]["path"] = os.path.join(media, "missing.mov")
        path = self.write("plan.json", plan)
        pkg = os.path.join(self.tmp, "pkg")
        code, doc, raw = run_cli("conform", path, "-o", pkg, "--zip", "--no-probe", "--json")
        self.assertEqual(code, 0, raw)
        for rel in ("timeline/demo.otio", "timeline/demo.xml", "timeline/demo.edl", "timeline/demo.srt",
                    "timeline/demo.json", "relink.json", "manifest.json"):
            self.assertTrue(os.path.isfile(os.path.join(pkg, rel)), rel)
        relink = json.loads(Path(pkg, "relink.json").read_text(encoding="utf-8"))
        a = next(m for m in relink["media"] if m["asset"] == "a")
        self.assertEqual(a["sha256"], hashlib.sha256(Path(media, "cam a.mov").read_bytes()).hexdigest())
        self.assertEqual(a["size_bytes"], os.path.getsize(os.path.join(media, "cam a.mov")))
        self.assertEqual(a["start_timecode"], "10:00:00:00")
        self.assertEqual(a["used"][0], {"item": "c1", "track": "V1", "source_in": "10:00:01;00", "source_out": "10:00:05;00",
                                        "record_in": "01:00:00;00", "record_out": "01:00:04;00"})
        self.assertEqual(a["facts_from"]["sha256"], "computed")
        b = next(m for m in relink["media"] if m["asset"] == "b")
        self.assertFalse(b["exists"])
        self.assertIn("MEDIA_MISSING", [w["code"] for w in doc["warnings"]])
        self.assertIn("start_timecode_note", b)
        manifest = json.loads(Path(pkg, "manifest.json").read_text(encoding="utf-8"))
        for f in manifest["files"]:
            self.assertEqual(f["sha256"], hashlib.sha256(Path(pkg, f["path"]).read_bytes()).hexdigest())
        z1 = Path(doc["zip"]["path"]).read_bytes()
        with zipfile.ZipFile(doc["zip"]["path"]) as z:
            self.assertIn("pkg/relink.json", z.namelist())
            self.assertEqual({i.date_time for i in z.infolist()}, {(1980, 1, 1, 0, 0, 0)})
        code, doc2, _ = run_cli("conform", path, "-o", pkg, "--zip", "--no-probe", "--overwrite", "--json")
        self.assertEqual(Path(doc2["zip"]["path"]).read_bytes(), z1)
        code, doc3, _ = run_cli("conform", path, "-o", pkg, "--no-probe", "--json")
        self.assertEqual(doc3["error"]["code"], "OUTPUT_EXISTS")

    def test_copy_media_points_the_timelines_into_the_package(self):
        media = self._media()
        path = self.write("plan.json", sample_plan("25", media_dir=media))
        pkg = os.path.join(self.tmp, "pkg2")
        code, doc, raw = run_cli("conform", path, "-o", pkg, "--copy-media", "--no-probe", "--json")
        self.assertEqual(code, 0, raw)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "media", "cam a.mov")))
        relink = json.loads(Path(pkg, "relink.json").read_text(encoding="utf-8"))
        self.assertEqual({m["package_path"] for m in relink["media"]}, {"media/cam a.mov", "media/cam_b.mov", "media/music.wav"})
        tl = load(os.path.join(pkg, "timeline", "demo.otio"))
        norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731 -- C:/x and C:\\x on Windows
        self.assertTrue(all(norm(a["path"]).startswith(norm(os.path.join(pkg, "media"))) for a in tl["assets"].values()))

    def test_plan_hash_mismatch_is_named(self):
        media = self._media()
        plan = sample_plan("25", media_dir=media)
        plan["assets"]["a"]["sha256"] = "sha256:" + "0" * 64
        path = self.write("plan.json", plan)
        code, doc, _ = run_cli("conform", path, "-o", os.path.join(self.tmp, "p3"), "--no-probe", "--json")
        self.assertIn("MEDIA_CHANGED", [w["code"] for w in doc["warnings"]])

    def test_package_api_with_a_fake_probe(self):
        media = self._media()
        tl = normalise(sample_plan("25", media_dir=media))
        res = package(tl, os.path.join(self.tmp, "p4"), ["otio"], hash_media=False,
                      probe=lambda p: {"duration": 12.0, "video": {"fps": 25.0, "width": 3840, "height": 2160},
                                       "audio": {"channels": 2}})
        a = next(m for m in res["relink"]["media"] if m["asset"] == "a")
        self.assertEqual((a["width"], a["height"], a["fps"], a["duration_frames"]), (3840, 2160, 25.0, 300))
        self.assertEqual(a["facts_from"]["width"], "ffmpeg-skill probe")
        self.assertIsNone(a["sha256"])


# ------------------------------------------------------------------------------------------------ contract / CLI
class ContractTests(unittest.TestCase):
    def test_contract_shape(self):
        c = contract()
        self.assertEqual((c["skill_id"], c["version"]), ("nle-bridge", __version__))
        self.assertEqual({p["tool_id"] for p in c["provides"]}, {t["tool_id"] for t in c["tools"]})
        for p in c["provides"]:
            self.assertEqual(p["lifecycle"], "EXPERIMENTAL")
        for d in c["dependencies"]:
            self.assertFalse(d["version_range"].replace(".", "").isdigit())
            self.assertIn("skill_id", d)
        self.assertIn("generating or analysing music", " ".join(c["not_provided"]))
        self.assertIn("live control", " ".join(c["not_provided"]))
        code, doc, _ = run_cli("contract", "--json")
        self.assertEqual((code, doc), (0, c))
        for e in ERRORS.values():
            self.assertIn(e["exit_code"], (2, 3, 4, 5))

    def test_doctor(self):
        code, doc, _ = run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["opentimelineio"]["found"], HAVE_OTIO)

    def test_version_matches_pyproject(self):
        import re
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(__version__, re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1))


# ------------------------------------------------------------------------------------------------ reference reader
@unittest.skipUnless(HAVE_ADAPTERS, OTIO_WHY)
class OtioCrossReadTests(TmpCase):
    """opentimelineio and its FCP7 / CMX 3600 adapters read what this skill writes, frame for frame."""

    def test_reference_reader_sees_the_same_cut(self):
        import opentimelineio as otio
        for label, (rate, drop) in RATES.items():
            with self.subTest(rate=label):
                plan_path, files, doc = self.export(sample_plan(rate, drop))
                self.assertTrue(next(f for f in doc["files"] if f["format"] == "otio")["verification"]["ok"])
                orig = load(plan_path)
                r = float(parse_rate(rate))
                want_v = [(i["record_in"], i["record_out"] - i["record_in"]) for i in orig["tracks"][0]["items"]]
                for fmt in ("otio", "fcp7", "edl"):
                    kw = {"rate": r} if fmt == "edl" else {}
                    t = otio.adapters.read_from_file(files[fmt], **kw)
                    vtracks = [tr for tr in t.tracks if tr.kind == otio.schema.TrackKind.Video]
                    clips = [c for c in vtracks[0] if isinstance(c, otio.schema.Clip)]
                    got = [(int(round(vtracks[0].range_of_child(c).start_time.to_frames())),
                            int(round(c.duration().to_frames()))) for c in clips]
                    self.assertEqual(got, want_v, (label, fmt))
                    names = [c.name for c in clips]
                    self.assertEqual(names, ["cam a.mov", "cam_b.mov", "cam a.mov"], (label, fmt))
                    if fmt != "edl":
                        self.assertEqual([m.name for m in t.tracks.markers], ["Intro", "Cut to B", "Outro"], (label, fmt))
                    self.assertAlmostEqual(clips[0].duration().rate, r, places=3)

    def test_reference_writes_fcp7_and_edl_that_this_skill_reads(self):
        import opentimelineio as otio
        plan_path, files, _ = self.export(sample_plan("25"), "otio")
        t = otio.adapters.read_from_file(files["otio"])
        xml = os.path.join(self.tmp, "ref.xml")
        otio.adapters.write_to_file(t, xml, adapter_name="fcp_xml")
        orig = load(plan_path)
        d = diff(orig, load(xml))
        self.assertEqual(d["summary"]["clips_added"] + d["summary"]["clips_removed"], 0, (d["added"], d["removed"]))
        # the adapter writes no audiolevels filter, which in FCP7 means unity gain: the diff says the level changed
        self.assertEqual([(c["plan_id"], c["changes"]) for c in d["clips"] if c["changes"]], [("c1a", ["level"])])
        self.assertEqual(d["summary"]["cuts_moved"], 0)
        v = otio.schema.Timeline(name="v1only", tracks=[t.tracks[0].clone()])
        edl_path = os.path.join(self.tmp, "ref.edl")
        otio.adapters.write_to_file(v, edl_path, adapter_name="cmx_3600")
        got = load(edl_path, rate=25)
        self.assertEqual([(i["record_in"], i["record_out"]) for i in got["tracks"][0]["items"]],
                         [(i["record_in"], i["record_out"]) for i in orig["tracks"][0]["items"]])


# ------------------------------------------------------------------------------------------------ registry
@unittest.skipUnless(VELISA_DIR and (VELISA_DIR / "registry" / "conformance.py").is_file(),
                     "needs a Velisa checkout (VELISA_DIR, AI_VIDEO_PRODUCTION_OS_DIR or ../Velisa)")
class ConformanceTests(unittest.TestCase):
    def test_registry_checks_pass(self):
        sys.path.insert(0, str(VELISA_DIR))
        try:
            from registry import conformance as conf
        finally:
            sys.path.remove(str(VELISA_DIR))
        results = conf.run_static_checks(contract())
        results.append(conf.check_no_unsafe_shell_out(str(ROOT / "src" / "nle_bridge")))
        results.append(conf.check_doctor_status(lambda: cli.doctor()))
        for r in results:
            print(f"\n  conformance {r.check}: {r.status} ({r.detail})", end="")
            self.assertEqual(r.status, "PASS", f"{r.check}: {r.detail}")

    def test_contract_validates_against_the_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("needs jsonschema")
        schema = json.loads((VELISA_DIR / "registry" / "capability_contract.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(contract(), schema)


# ------------------------------------------------------------------------------------------------ real engine
@unittest.skipUnless(HAVE_FFMPEG and FFMPEG_SKILL_DIR, "needs ffmpeg on PATH and an ffmpeg-skill checkout (FFMPEG_SKILL_DIR)")
class RealProbeTests(TmpCase):
    def test_conform_takes_media_facts_from_ffmpeg_skill(self):
        media = os.path.join(self.tmp, "m")
        os.makedirs(media)
        clip = os.path.join(media, "cam a.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30000/1001:duration=4",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", clip], check=True)
        plan = {"name": "real", "rate": "30000/1001", "assets": {"a": {"path": clip}},
                "tracks": [{"kind": "video", "items": [{"id": "c1", "asset": "a", "in": 0.5, "out": 2.0}]}]}
        path = self.write("plan.json", plan)
        pkg = os.path.join(self.tmp, "pkg")
        code, doc, raw = run_cli("conform", path, "-o", pkg, "--ffmpeg-skill", str(FFMPEG_SKILL_DIR), "--json")
        self.assertEqual(code, 0, raw)
        a = doc["relink"]["media"][0]
        self.assertEqual((a["width"], a["height"]), (640, 360))
        self.assertAlmostEqual(a["fps"], 29.97, places=2)
        self.assertAlmostEqual(a["duration"], 4.0, delta=0.1)
        self.assertEqual(a["facts_from"]["fps"], "ffmpeg-skill probe")
        self.assertTrue(a["has_audio"])
        # a render project without out points gets its lengths from the probe
        proj = self.write("project.json", {"clips": [{"src": clip, "in": 1}], "transition": {"type": "none"}})
        code, doc, raw = run_cli("export", proj, "-o", os.path.join(self.tmp, "e"), "--ffmpeg-skill", str(FFMPEG_SKILL_DIR),
                                 "--formats", "otio", "--json")
        self.assertEqual(code, 0, raw)
        self.assertAlmostEqual(doc["timeline"]["duration"], 3.0, delta=0.1)

    def test_reads_what_ffmpeg_skill_render_export_timeline_writes(self):
        """The same render project through ffmpeg-skill `render --export-timeline` (.otio / .edl) and through this
        skill gives the same cut, frame for frame."""
        media = os.path.join(self.tmp, "m")
        os.makedirs(media)
        clips = []
        for n in ("a", "b"):
            p = os.path.join(media, f"{n}.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=6",
                            "-f", "lavfi", "-i", "sine=frequency=330:duration=6", "-shortest", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", p], check=True)
            clips.append(p)
        proj = {"clips": [{"src": clips[0], "in": 1, "out": 3.5}, {"src": clips[1], "in": 0.4, "out": 2}],
                "transition": {"type": "none"}, "chapters": [{"at": 0, "title": "One"}, {"at": 2.5, "title": "Two"}],
                "output": os.path.join(self.tmp, "final.mp4")}
        proj_path = self.write("project.json", proj)
        ours = load(proj_path, prober=engine.prober(engine.locate(str(FFMPEG_SKILL_DIR))))
        for ext in ("otio", "edl"):
            out = os.path.join(self.tmp, f"ffs.{ext}")
            r = subprocess.run([sys.executable, str(FFMPEG_SKILL_DIR / "scripts" / "render.py"), proj_path,
                                "--export-timeline", out, "--json"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            theirs = load_returned(ours, out)
            d = diff(ours, theirs)
            v = [c for c in d["clips"] if c["track"] == "V1"]
            self.assertEqual([c["status"] for c in v], ["unchanged", "unchanged"], (ext, v, d["added"], d["removed"]))
            self.assertEqual(d["summary"]["cuts_moved"], 0, ext)
            if ext == "otio":
                self.assertEqual([m["name"] for m in theirs["markers"]], ["One", "Two"])
                # ffmpeg-skill colours chapter markers RED; time and name are the same
                self.assertEqual([c["changes"] for c in d["markers"]["changed"]], [["color"], ["color"]])


if __name__ == "__main__":
    unittest.main()
