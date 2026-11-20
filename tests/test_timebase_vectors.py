"""The ecosystem timebase spec's conformance vectors (velisa/timebase@1) against nle_bridge.timecode.

tests/fixtures/timebase/timebase-vectors.json is a byte-for-byte copy of Velisa's docs/specs/timebase-vectors.json
at Velisa commit VELISA_COMMIT (docs/decisions.md ADR-015). This skill offers parse_rate, rate_str, nominal,
same_rate (rate equality), frames_at in `nearest` mode (seconds_to_frames), seconds_of, timecode_from_frames
(frames_to_timecode), frames_from_timecode (timecode_to_frames), timecode_to_seconds and seconds_to_timecode.
Every vector of those functions must pass; the vectors of functions this skill does not have (format_seconds,
seek_seconds, transition_frames, join_positions, frames_at `down`, parse_seconds) and the optional p / i suffix
are listed as not offered, and their number is checked so a new vector cannot go unnoticed.
"""
import hashlib
import json
import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nle_bridge.timecode import (TimecodeError, frames_to_timecode, nominal, parse_rate, parse_timecode,  # noqa: E402
                                 rate_str, seconds_to_frames, timecode_to_frames)

VECTORS = ROOT / "tests" / "fixtures" / "timebase" / "timebase-vectors.json"
VELISA_COMMIT = "b7df1d82586f0b7c00fed4074aecac75cbadf893"
VECTORS_SHA256 = "fecd9730470fd79fb0b8dce6a55ef1ac8fd743ea044a1f1541474ebd249e5aa1"
OFFERED = 100  # of the 134 vectors: the functions this skill has; the rest are counted as not offered


def _out(v):
    if isinstance(v, Fraction):
        return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"
    return v


def _frames_from_timecode(label, rate, drop_frame=None):
    r = parse_rate(rate)
    if drop_frame is None:  # the separator decides (';', ',' or '.' is drop-frame)
        drop_frame = parse_timecode(label)[4]
    return timecode_to_frames(label, r, drop_frame)


def _frames_at(seconds, rate, mode="nearest"):
    if mode != "nearest":
        raise TimecodeError(f"unknown rounding mode {mode!r}", code="INVALID_TIME")
    return seconds_to_frames(seconds, parse_rate(rate))


FUNCTIONS = {
    "parse_rate": lambda value: parse_rate(value),
    "rate_str": lambda rate: rate_str(parse_rate(rate)),
    "nominal": lambda rate: nominal(parse_rate(rate)),
    "same_rate": lambda a, b: parse_rate(a) == parse_rate(b),
    "frames_at": _frames_at,
    "seconds_of": lambda frames, rate: Fraction(frames) / parse_rate(rate),
    "timecode_from_frames": lambda frames, rate, drop_frame: frames_to_timecode(frames, parse_rate(rate), drop_frame),
    "frames_from_timecode": _frames_from_timecode,
    "timecode_to_seconds": lambda label, rate, drop_frame=None: Fraction(_frames_from_timecode(label, rate, drop_frame))
    / parse_rate(rate),
    "seconds_to_timecode": lambda seconds, rate, drop_frame: frames_to_timecode(
        seconds_to_frames(seconds, parse_rate(rate)), parse_rate(rate), drop_frame),
}
NOT_OFFERED_FNS = {"format_seconds", "seek_seconds", "transition_frames", "join_positions", "parse_seconds"}


def _offered(vec):
    if vec.get("optional") or vec["fn"] in NOT_OFFERED_FNS:
        return False
    if vec["fn"] == "frames_at" and vec["args"].get("mode") == "down":
        return False
    return True


class TimebaseVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = VECTORS.read_bytes()
        cls.doc = json.loads(cls.raw.decode("utf-8"))

    def test_the_copy_is_velisas_file(self):
        self.assertEqual(self.doc["spec"], "velisa/timebase@1")
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), VECTORS_SHA256,
                         f"the vectors are a copy of Velisa {VELISA_COMMIT}; re-copy them with that commit recorded")

    def test_every_offered_vector_passes(self):
        ran = 0
        for vec in self.doc["vectors"]:
            if not _offered(vec):
                continue
            ran += 1
            with self.subTest(vec["id"]):
                try:
                    got = _out(FUNCTIONS[vec["fn"]](**vec["args"]))
                except TimecodeError as e:
                    self.assertIn("error", vec, f"{vec['id']}: raised {e.code}: {e}")
                    self.assertEqual(e.code, vec["error"], f"{vec['id']}: {e}")
                    continue
                self.assertNotIn("error", vec, f"{vec['id']}: gave {got!r}, expected {vec.get('error')}")
                self.assertEqual(got, vec["expect"], vec["id"])
        self.assertEqual(ran, OFFERED)

    def test_not_offered_vectors_are_counted(self):
        skipped = [v["id"] for v in self.doc["vectors"] if not _offered(v)]
        self.assertEqual(len(skipped), len(self.doc["vectors"]) - OFFERED, skipped)

    def test_the_issues_cases(self):
        self.assertEqual(rate_str(parse_rate("23.98")), "24000/1001")  # was 1199/50
        self.assertEqual(rate_str(parse_rate(47.95)), "48000/1001")  # was 959/20
        self.assertEqual(seconds_to_frames("0.35", Fraction(30)), 11)  # a half up, was 10 (to even)
        with self.assertRaises(TimecodeError) as cm:
            frames_to_timecode(24 * 3600 * 25, Fraction(25))
        self.assertEqual(cm.exception.code, "OUT_OF_RANGE")
        with self.assertRaises(TimecodeError) as cm:
            timecode_to_frames("24:00:00:00", Fraction(25))
        self.assertEqual(cm.exception.code, "INVALID_TIMECODE")


if __name__ == "__main__":
    unittest.main()
