"""Frame rates and SMPTE timecode: every time in an exported timeline is a whole frame count at an exact rate.

- A rate is a Fraction. The NTSC rates editors use are stored as the exact ratio (23.976 -> 24000/1001,
  29.97 -> 30000/1001, 59.94 -> 60000/1001, 47.952 -> 48000/1001, 119.88 -> 120000/1001), never as the decimal.
- Timecode counts frames at the nominal integer rate (24, 30, 60 ...). Non-drop-frame (NDF) simply runs slower
  than the clock at an NTSC rate. Drop-frame (DF, 29.97 and 59.94 only) skips frame numbers 0 and 1 (0-3 at
  59.94) at the start of every minute except every tenth, so that one hour of timecode is one hour of clock.
- Seconds become frames by rounding to the nearest frame on exact rationals (no float drift: 1001/30000 s at
  30000/1001 is exactly 1 frame); a time exactly half-way between two frames goes to the even one, the rule
  ffmpeg-skill's own timeline export uses (Python's round()), so both exports put a cut on the same frame.
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, Tuple

_NTSC = {23.976: Fraction(24000, 1001), 29.97: Fraction(30000, 1001), 59.94: Fraction(60000, 1001),
         47.952: Fraction(48000, 1001), 119.88: Fraction(120000, 1001)}
DROP_FRAME_RATES = (Fraction(30000, 1001), Fraction(60000, 1001))
_TC = re.compile(r"^(\d{1,2})[:;.](\d{2})[:;.](\d{2})([:;.,])(\d{2,3})$")


class TimecodeError(ValueError):
    """A rate, a timecode or a time that cannot be used (an INVALID_ARGUMENT for the caller)."""


def parse_rate(value: Any) -> Fraction:
    """A frame rate as an exact Fraction: 25, 29.97, "30000/1001", "23.976", Fraction(24000, 1001)."""
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, str) and "/" in value:
        num, _, den = value.partition("/")
        try:
            rate = Fraction(int(num.strip()), int(den.strip()))
        except (ValueError, ZeroDivisionError):
            raise TimecodeError(f"frame rate {value!r} is not N/D")
    else:
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise TimecodeError(f"frame rate {value!r} is not a number")
        if f != f or f in (float("inf"), float("-inf")):
            raise TimecodeError(f"frame rate {value!r} is not a number")
        rate = next((exact for approx, exact in _NTSC.items() if abs(f - approx) < 0.0015 or abs(f - float(exact)) < 1e-6), None)
        if rate is None:
            rate = Fraction(f).limit_denominator(1001)
    if rate <= 0 or rate > 1000:
        raise TimecodeError(f"frame rate {value!r} is out of range (0, 1000]")
    return rate


def rate_str(rate: Fraction) -> str:
    """'25' or '30000/1001': how a rate is written in this skill's JSON."""
    return str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"


def nominal(rate: Fraction) -> int:
    """The timecode base: frames per timecode second (24 for 23.976, 30 for 29.97)."""
    return max(1, int(round(float(rate))))


def is_ntsc(rate: Fraction) -> bool:
    return rate.denominator == 1001


def drop_frames_per_minute(rate: Fraction) -> int:
    """2 at 29.97, 4 at 59.94; drop-frame is not defined for any other rate."""
    if rate not in DROP_FRAME_RATES:
        raise TimecodeError(f"drop-frame timecode exists only at 29.97 and 59.94 fps, not {float(rate):.3f}")
    return 2 * nominal(rate) // 30


def check_drop_frame(rate: Fraction, drop_frame: bool) -> None:
    if drop_frame:
        drop_frames_per_minute(rate)


def seconds_to_frames(seconds: Any, rate: Fraction) -> int:
    """Seconds -> the nearest whole frame (ties to even, as ffmpeg-skill), computed on exact rationals."""
    try:
        s = Fraction(str(seconds)) if not isinstance(seconds, Fraction) else seconds
    except (ValueError, ZeroDivisionError):
        raise TimecodeError(f"time {seconds!r} is not a number of seconds")
    return int(round(s * rate))


def frames_to_seconds(frames: int, rate: Fraction) -> float:
    return float(Fraction(int(frames)) / rate)


def frames_to_timecode(frames: int, rate: Fraction, drop_frame: bool = False) -> str:
    """Frame count (from 00:00:00:00) -> 'hh:mm:ss:ff', or 'hh:mm:ss;ff' for drop-frame."""
    frames = int(frames)
    if frames < 0:
        raise TimecodeError(f"negative frame count {frames}")
    fps = nominal(rate)
    if drop_frame:
        drop = drop_frames_per_minute(rate)
        per_10min = fps * 600 - drop * 9
        per_min = fps * 60 - drop
        d, m = divmod(frames, per_10min)
        frames += drop * 9 * d + (drop * ((m - drop) // per_min) if m > drop else 0)
    h, rem = divmod(frames, fps * 3600)
    mi, rem = divmod(rem, fps * 60)
    s, f = divmod(rem, fps)
    return f"{h:02d}:{mi:02d}:{s:02d}{';' if drop_frame else ':'}{f:02d}"


def parse_timecode(tc: str) -> Tuple[int, int, int, int, bool]:
    """'hh:mm:ss:ff' / 'hh:mm:ss;ff' / 'hh;mm;ss;ff' -> (h, m, s, f, written_as_drop_frame)."""
    m = _TC.match(str(tc).strip())
    if not m:
        raise TimecodeError(f"{tc!r} is not hh:mm:ss:ff timecode")
    h, mi, s, sep, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5))
    if mi > 59 or s > 59:
        raise TimecodeError(f"{tc!r}: minutes and seconds must be 00-59")
    return h, mi, s, f, sep in ";,." or ";" in str(tc)


def timecode_to_frames(tc: str, rate: Fraction, drop_frame: bool = False) -> int:
    """'hh:mm:ss:ff' -> frame count from 00:00:00:00. Drop-frame labels that do not exist (00:01:00;00 at 29.97)
    are refused, never silently moved."""
    h, mi, s, f, _ = parse_timecode(tc)
    fps = nominal(rate)
    if f >= fps:
        raise TimecodeError(f"{tc!r}: frame {f} is out of range for {fps} fps timecode (00-{fps - 1:02d})")
    total = ((h * 60 + mi) * 60 + s) * fps + f
    if drop_frame:
        drop = drop_frames_per_minute(rate)
        if s == 0 and f < drop and mi % 10 != 0:
            raise TimecodeError(f"{tc!r} does not exist in drop-frame timecode (frames 00-{drop - 1:02d} are skipped "
                                "at the start of every minute not divisible by 10)")
        minutes = h * 60 + mi
        total -= drop * (minutes - minutes // 10)
    return total
