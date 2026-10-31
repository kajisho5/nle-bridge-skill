"""Machine-readable contract (`nle-bridge contract --json`), in the shape Velisa's registry reads
(registry/capability_contract.schema.json: skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import __version__
from .beats import BAR_COLOR, BEAT_COLOR, BEAT_SETS, BEATGRID_SCHEMA, DEFAULT_BEATS_PER_BAR
from .conform import CONFORM_SCHEMA, RELINK_SCHEMA
from .diff import DIFF_SCHEMA
from .engine import ENV_DIRS, REQUIRED_TOOLS
from .export import DEFAULT_FORMATS, FORMATS
from .model import DEFAULT_START_TC, MARKER_COLORS, TIMELINE_SCHEMA

SKILL_ID = "nle-bridge"
CONTRACT_VERSION = "1.0"

# Warning codes that mean content of the plan is not (or only partly) in a written file; --strict refuses them.
LOSS_CODES = ("SPEED_NOT_EXPORTED", "EFFECT_NOT_EXPORTED", "TITLE_NOT_EXPORTED", "TRANSITION_NOT_EXPORTED",
              "TRACK_NOT_EXPORTED", "LEVEL_NOT_EXPORTED", "LEVEL_METADATA_ONLY", "MARKER_COLOR_NOT_EXPORTED",
              "MARKER_DURATION_NOT_EXPORTED", "CAPTIONS_NOT_EXPORTED", "NOT_IN_TIMELINE",
              "TRANSITION_AS_DISSOLVE", "TRANSITION_OFF_CENTRE")

_PLAN_ARGS = ["[--subject <asset>]", "[--fps <rate>]", "[--drop-frame | --non-drop-frame]", "[--dissolves]",
              "[--beats <grid.json>]",
              f"[--beats-per-bar {DEFAULT_BEATS_PER_BAR}]", "[--beat-set supported|grid]", "[--beat-offset <s>]"]

TOOLS = [
    {"tool_id": "nle-bridge/export", "role": "execution", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "A plan (nle-bridge/timeline@1, the agent's project IR, an ffmpeg-skill render project, or an OTIO / "
                    "FCP7 XML / EDL) as editor timelines: OTIO, FCP7 XML (Premiere), CMX 3600 EDL, and an SRT sidecar for "
                    "captions. A beat grid adds bar / beat markers; --dissolves writes cross dissolves centred on the cut. What a "
                    "format cannot carry is a warning, never dropped",
     "argv": ["export", "<plan>", "-o <dir>", "[--formats otio,fcp7,edl,srt]", "[--name <stem>]", *_PLAN_ARGS,
              "[--beat-markers all|bars|none]", "[--strict]", "[--overwrite]", "--json"]},
    {"tool_id": "nle-bridge/conform", "role": "execution", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "A conform package: the timelines, the plan as JSON, a relink manifest (path, size, sha256, start "
                    "timecode, length, fps, picture size, and the ranges used of every media file) and a file manifest, "
                    "in a folder and optionally a zip; media copied in only with --copy-media",
     "argv": ["conform", "<plan>", "-o <dir>", "[--formats ...]", "[--name <stem>]", *_PLAN_ARGS, "[--copy-media]",
              "[--zip]", "[--no-hash]", "[--no-probe]", "[--ffmpeg-skill <dir>]", "[--strict]", "[--overwrite]", "--json"]},
    {"tool_id": "nle-bridge/diff", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "Round trip: the timeline an NLE gives back (OTIO, FCP7 XML or EDL) against the plan: clips changed "
                    "(moved, trimmed, slipped, level, transition), added (split_from on a razor cut) and removed, cut points moved in "
                    "frames, markers added / removed / changed, and with a beat grid how many frames each cut sits off "
                    "the nearest beat. Optionally the returned timeline as a new plan version",
     "argv": ["diff", "<plan>", "<returned.otio|.xml|.edl>", "[-o <diff.json>]", "[--timeline-out <plan.json>]",
              *_PLAN_ARGS, "[--beat-tolerance 0.5]", "[--overwrite]", "--json"]},
    {"tool_id": "nle-bridge/import", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "Any accepted input (plan, project IR, render project, OTIO, FCP7 XML, EDL) as nle-bridge/timeline@1",
     "argv": ["import", "<file>", "[-o <timeline.json>]", "[--subject <asset>]", "[--fps <rate>]", "[--dissolves]",
              "[--overwrite]", "--json"]},
]

ERRORS = {
    "INPUT_NOT_FOUND": {"exit_code": 2, "retryable": False},
    "INVALID_ARGUMENT": {"exit_code": 2, "retryable": False},
    "INVALID_PLAN": {"exit_code": 2, "retryable": False},
    "INVALID_TIMELINE": {"exit_code": 2, "retryable": False},
    "INVALID_BEATS": {"exit_code": 2, "retryable": False},
    "INVALID_CAPTIONS": {"exit_code": 2, "retryable": False},
    "UNSUPPORTED_FORMAT": {"exit_code": 2, "retryable": False},
    "OUTPUT_EXISTS": {"exit_code": 2, "retryable": False},
    "OUTPUT_IS_INPUT": {"exit_code": 2, "retryable": False},
    "ENGINE_TOOL_MISSING": {"exit_code": 3, "retryable": False},
    "ENGINE_FAILED": {"exit_code": 4, "retryable": False},
    "ENGINE_TIMEOUT": {"exit_code": 4, "retryable": True},
    "CONTENT_NOT_EXPORTED": {"exit_code": 5, "retryable": False},
}


def contract() -> Dict[str, Any]:
    return {
        "skill_id": SKILL_ID,
        "id": SKILL_ID,
        # an object, as registry/capability_contract.schema.json types `skill` (id + version), with the package name
        "skill": {"id": SKILL_ID, "version": __version__, "package": "nle-bridge-skill"},
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        "provides": [{"id": "nle.timeline_export", "lifecycle": "EXPERIMENTAL", "tool_id": "nle-bridge/export",
                      "output_artifact_types": ["timeline", "subtitle_document"], "mutates_input": False},
                     {"id": "nle.conform_package", "lifecycle": "EXPERIMENTAL", "tool_id": "nle-bridge/conform",
                      "output_artifact_types": ["timeline", "subtitle_document"], "mutates_input": False},
                     {"id": "nle.round_trip_diff", "lifecycle": "EXPERIMENTAL", "tool_id": "nle-bridge/diff",
                      "input_artifact_types": ["timeline"], "output_artifact_types": ["analysis_result", "timeline"],
                      "mutates_input": False},
                     {"id": "nle.timeline_import", "lifecycle": "EXPERIMENTAL", "tool_id": "nle-bridge/import",
                      "input_artifact_types": ["timeline", "project_ir"], "output_artifact_types": ["timeline"],
                      "mutates_input": False}],
        "dependencies": [{"id": "ffmpeg-skill", "skill_id": "ffmpeg-skill", "kind": "skill", "required": False,
                          "version_range": ">=2.0.0 <3.0.0", "tools": list(REQUIRED_TOOLS),
                          "for": ["nle-bridge/conform (media facts for the relink manifest)",
                                  "nle-bridge/export of a render project without out points (clip lengths)"],
                          "located_by": ["--ffmpeg-skill"] + [f"${e}" for e in ENV_DIRS]
                          + ["~/.claude/skills/ffmpeg-skill", "./vendor/ffmpeg-skill", "../ffmpeg-skill"]},
                         {"id": "opentimelineio", "skill_id": "opentimelineio", "kind": "python", "required": False,
                          "extra": "otio", "version_range": ">=0.17", "licence": "Apache-2.0",
                          "for": ["re-reading each written .otio as an independent check (export / conform verification)"]}],
        "not_provided": ["live control of Premiere Pro or DaVinci Resolve (import, render queue, markers in a running NLE: "
                         "reuse the community MCP servers, Velisa ROADMAP)",
                         "effects, speed changes, titles, and transitions other than a cross dissolve, in the interchange files "
                         "(named as warnings); cross dissolves only with --dissolves",
                         "FCPXML (Final Cut Pro X): ffmpeg-skill render --export-timeline writes it",
                         "AAF", "rendering, transcoding or proxies (ffmpeg-skill)",
                         "generating or analysing music (a beat grid is read, never made: ffmpeg-skill scenes --beats "
                         "measures one, a music-generation host states one)",
                         "captions themselves (subtitle-skill); they travel as an SRT sidecar",
                         "running ffmpeg or ffprobe directly (only through ffmpeg-skill)", "network access of any kind"],
        "tools": TOOLS,
        "formats": {"otio": {"extension": FORMATS["otio"], "schema": "OpenTimelineIO JSON (Timeline.1, Clip.2, Marker.2)",
                             "carries": ["cuts", "video and audio tracks", "gaps", "markers with colour", "media start timecode",
                                         "cross dissolves (--dissolves)"],
                             "not_carried": ["audio levels (metadata only)"]},
                    "fcp7": {"extension": FORMATS["fcp7"], "schema": "xmeml version 4",
                             "carries": ["cuts", "video and audio tracks", "markers", "audio levels (audiolevels filter)",
                                         "media start timecode", "DF / NDF", "cross dissolves (--dissolves)"],
                             "not_carried": ["marker colours", "a dissolve off the cut's centre"]},
                    "edl": {"extension": FORMATS["edl"], "schema": "CMX 3600",
                            "carries": ["cuts on one picture track and up to 4 audio tracks", "markers with colour (* LOC)",
                                        "DF / NDF", "cross dissolves (--dissolves; read back centred on the cut)"],
                            "not_carried": ["more than one video track", "audio levels", "marker durations",
                                            "the frame rate (the reader needs it)"]},
                    "srt": {"extension": FORMATS["srt"], "carries": ["captions in programme time"]}},
        "default_formats": list(DEFAULT_FORMATS),
        "inputs": [TIMELINE_SCHEMA, "video-production-agent project IR (delivery.timelines[], ADR-052)",
                   "ffmpeg-skill render project (clips[].src)", "OTIO JSON", "FCP7 XML", "CMX 3600 EDL (with --fps)"],
        "timeline": {"schema": TIMELINE_SCHEMA, "units": "whole frames at an exact rational rate",
                     "default_start_timecode": DEFAULT_START_TC, "marker_colors": list(MARKER_COLORS),
                     "frame_rates": "any; NTSC as exact N/1001; drop-frame at 29.97 and 59.94 only"},
        "beats": {"schema": BEATGRID_SCHEMA, "also_reads": ["ffmpeg-skill scenes --beats (beats, beat_grid)", "beat_grid"],
                  "bar_color": BAR_COLOR, "beat_color": BEAT_COLOR, "beat_sets": list(BEAT_SETS),
                  "default_beats_per_bar": DEFAULT_BEATS_PER_BAR,
                  "bars": "STATED when the grid lists bars (or is generated from first_downbeat), else ASSUMED every N beats",
                  "offset": "seconds from the timeline start to the music's 0 (default 0)"},
        "conform": {"schema": CONFORM_SCHEMA, "relink_schema": RELINK_SCHEMA,
                    "layout": ["timeline/<name>.otio|.xml|.edl|.srt|.json", "media/ (--copy-media)", "relink.json",
                               "manifest.json"], "zip": "<dir>.zip, fixed entry dates, sorted"},
        "diff": {"schema": DIFF_SCHEMA, "provenance": "OBSERVED",
                 "beat_offset": "signed frames from the nearest beat (positive: the cut is after it)"},
        "warnings": {"loss_codes": list(LOSS_CODES), "strict": "--strict exits 5 (CONTENT_NOT_EXPORTED) before writing"},
        "execution": {"canonical_invocation": ["nle-bridge", "<command>", "...", "--json"], "network": False, "shell": False,
                      "engine": "ffmpeg-skill probe only, optional (scripts run with this Python, argv only)",
                      "input_is_read_only": True,
                      "overwrites": "never unless --overwrite; an output equal to an input is refused"},
        "errors": ERRORS,
    }
