"""The only media boundary: ffmpeg-skill's own `probe`, run as `python3 <ffmpeg-skill>/scripts/probe.py <file> --json`.
This skill never runs ffmpeg or ffprobe itself. It reads media only to describe it (size, rate, length) for the
relink manifest, and since ffmpeg-skill 3.0.0 for each file's start timecode (`probe`'s `timecode`); nothing is
decoded or rendered here, and every timeline is written without it.

ffmpeg-skill is found, in order: --ffmpeg-skill, $NLE_BRIDGE_FFMPEG_SKILL_DIR, $FFMPEG_SKILL_DIR,
~/.claude/skills/ffmpeg-skill, ./vendor/ffmpeg-skill, ../ffmpeg-skill. It is optional: without it the facts come
from the plan, and the manifest says where each fact came from.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

ENV_DIRS = ("NLE_BRIDGE_FFMPEG_SKILL_DIR", "FFMPEG_SKILL_DIR")
REQUIRED_TOOLS = ("probe",)

Runner = Callable[[List[str], float], Tuple[int, str, str]]


@dataclass
class FfmpegSkill:
    root: str
    version: str
    tools: List[str]

    def script(self, tool: str) -> str:
        return os.path.join(self.root, "scripts", f"{tool}.py")


def _candidate(path: str) -> Optional[FfmpegSkill]:
    path = os.path.expanduser(path)
    scripts = os.path.join(path, "scripts")
    if not os.path.isfile(os.path.join(scripts, "probe.py")):
        return None
    version = "unknown"
    try:
        with open(os.path.join(path, "package.json"), encoding="utf-8") as fh:
            version = str(json.load(fh).get("version", "unknown"))
    except (OSError, ValueError):
        pass
    tools = sorted(f[:-3] for f in os.listdir(scripts) if f.endswith(".py") and not f.startswith("_"))
    return FfmpegSkill(os.path.realpath(path), version, tools)


def candidates(explicit: Optional[str] = None) -> List[str]:
    out = [explicit] if explicit else []
    out += [os.environ[e] for e in ENV_DIRS if os.environ.get(e)]
    out += [os.path.expanduser("~/.claude/skills/ffmpeg-skill"), os.path.join(os.getcwd(), "vendor", "ffmpeg-skill"),
            os.path.join(os.getcwd(), "..", "ffmpeg-skill")]
    return out


def locate(explicit: Optional[str] = None) -> Optional[FfmpegSkill]:
    for c in candidates(explicit):
        found = _candidate(c)
        if found:
            return found
    return None


def subprocess_runner(argv: List[str], timeout: float) -> Tuple[int, str, str]:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


class EngineError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def run_tool(skill: FfmpegSkill, tool: str, args: List[str], *, timeout: float = 600.0,
             runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """Run one ffmpeg-skill tool with --json and return its document. A non-zero exit or output that is not a
    JSON object is an ENGINE_FAILED error carrying the tool's last stderr line."""
    if tool not in skill.tools:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} has no '{tool}' tool")
    argv = [sys.executable, skill.script(tool)] + list(args) + ["--json"]
    try:
        rc, out, err = runner(argv, timeout)
    except subprocess.TimeoutExpired:
        raise EngineError("ENGINE_TIMEOUT", f"ffmpeg-skill {tool} did not finish within {timeout:.0f}s")
    last = (err.strip().splitlines() or [""])[-1]
    if rc != 0:
        raise EngineError("ENGINE_FAILED", f"ffmpeg-skill {tool} exited {rc}", last)
    try:
        doc = json.loads(out)
    except ValueError:
        raise EngineError("ENGINE_FAILED", f"ffmpeg-skill {tool} did not print a JSON document", last)
    if not isinstance(doc, dict):
        raise EngineError("ENGINE_FAILED", f"ffmpeg-skill {tool} printed JSON that is not an object", last)
    return doc


def prober(skill: FfmpegSkill, *, timeout: float = 600.0, runner: Runner = subprocess_runner) -> Callable[[str], Optional[Dict[str, Any]]]:
    """path -> ffmpeg-skill probe document, or None for a file that is not there (a missing file is the
    manifest's finding, not an engine failure). Each file is probed once per prober: reading a plan and writing its
    relink manifest ask about the same media."""
    seen: Dict[str, Dict[str, Any]] = {}

    def probe(path: str) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(path):
            return None
        key = os.path.realpath(path)
        if key not in seen:
            seen[key] = run_tool(skill, "probe", [path], timeout=timeout, runner=runner)
        return seen[key]
    return probe


def engine_status(explicit: Optional[str]) -> Dict[str, Any]:
    skill = locate(explicit)
    if skill is None:
        return {"found": False, "root": None, "version": None, "missing_tools": list(REQUIRED_TOOLS),
                "looked_in": candidates(explicit)}
    return {"found": True, "root": skill.root, "version": skill.version,
            "missing_tools": [t for t in REQUIRED_TOOLS if t not in skill.tools],
            "probe_timecode": probe_reports_timecode(skill)}


def probe_reports_timecode(skill: FfmpegSkill) -> Optional[bool]:
    """Whether this ffmpeg-skill's probe has the `timecode` field (added in 3.0.0), from its version; None when the
    version cannot be read. Only for doctor's note: conform and export read the field whenever a probe has it."""
    try:
        return int(skill.version.split(".")[0]) >= 3
    except ValueError:
        return None
