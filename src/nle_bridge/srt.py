"""SubRip (.srt) sidecar: captions travel next to the timeline, never inside it (no interchange format carries them
in a way both NLEs import). Times are programme time from the timeline's first frame; both NLEs import an SRT onto
a subtitle track from the start of the timeline."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from .model import NleBridgeError

_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write(captions: List[Dict[str, Any]]) -> str:
    blocks = []
    for i, c in enumerate(sorted(captions, key=lambda c: (c["start"], c["end"])), 1):
        text = re.sub(r"\n{2,}", "\n", str(c["text"]).replace("\r\n", "\n").replace("\r", "\n")).strip("\n")
        blocks.append(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{text}\n")
    return "\n".join(blocks)


def read(text: str) -> List[Dict[str, Any]]:
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("﻿", "").strip()):
        lines = block.split("\n")
        idx = next((i for i, ln in enumerate(lines) if _TIME.search(ln)), None)
        if idx is None:
            continue
        m = _TIME.search(lines[idx])
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + int(m.group(4).ljust(3, "0")) / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + int(m.group(8).ljust(3, "0")) / 1000.0
        if end <= start:
            raise NleBridgeError("INVALID_CAPTIONS", f"SRT cue {lines[0]!r}: end is not after start")
        out.append({"start": round(start, 3), "end": round(end, 3), "text": "\n".join(lines[idx + 1:])})
    return out

