# Architecture Decision Records

## ADR-001 Interchange: OTIO, FCP7 XML and CMX 3600 written with the standard library; files only

Checked 2026-10-31 against PyPI (opentimelineio 0.18.1, otio-fcp-adapter 1.0.0, otio-cmx3600-adapter 1.0.0), Velisa `docs/ecosystem/ROADMAP.md` ("Premiere Pro and DaVinci Resolve") and `ecosystem.json`, video-production-agent ADR-052, and ffmpeg-skill 2.24 `scripts/_common/timeline.py` (read only).

### Requirements

Velisa's roadmap gives this repository the timeline hand-over to Premiere Pro and DaVinci Resolve: OTIO / FCP7 XML / EDL + SRT export, conform packages, and the round trip of the editor's changes. From that:

- **Formats both NLEs import.** Resolve's `ImportTimelineFromFile` takes AAF / EDL / FCP7 XML / FCPXML / OTIO. Premiere takes OTIO (since 2024-10), FCP7 XML, EDL and AAF, and no FCPXML. OTIO is primary; FCP7 XML is the fallback that carries cuts, tracks, markers and basic levels; the EDL is the lowest common denominator. Subtitles go as an SRT sidecar: no interchange format carries them in a form both import.
- **No live control, no borrowed code.** Live NLE control is left to the community MCP servers, reused later and never forked. One Resolve server has no licence and another is GPL-3.0. Nothing here is derived from any of them; the writers and readers follow the formats.
- **Never drop silently.** Effects, speed changes and titles do not survive interchange. Each one is named.
- **Same shape as the existing exports.** video-production-agent already exports OTIO through ffmpeg-skill `render --export-timeline` (agent ADR-052). The two paths have to agree frame for frame on the same cut.
- **Commercial use, offline, optional dependencies**, like every skill in the ecosystem.

### Libraries

| Candidate | Licence | Notes | Verdict |
|---|---|---|---|
| **Standard library writers and readers** (`json`, `xml.etree`, `fractions`) | - | OTIO JSON, xmeml and CMX 3600 are small, documented text formats. Full control over rates, timecode and what is written; Python 3.9 on every OS with nothing to install | **Chosen** for every file written and read |
| **opentimelineio** | Apache-2.0 | The reference implementation. Wheels for CPython 3.9-3.13 on Linux, macOS and Windows. Since 0.17 its core ships only the OTIO-native adapters (otio_json, otioz, otiod) | **Optional extra `otio`**: `export` re-reads each written .otio with it (`verification`). Not needed to write or read |
| **otio-fcp-adapter, otio-cmx3600-adapter** | Apache-2.0 | OTIO's FCP7 XML and CMX 3600 adapters, split out of core | **Test-only (`test` extra)**: an independent second reader of this skill's .xml and .edl, and a second writer of files this skill must read. CI installs them and fails if those tests skip |
| A community Resolve / Premiere MCP server's timeline code | none / GPL-3.0 / MIT | Live control, not interchange | Rejected here (licence, and it is the later "reuse" step) |

### Decision

- **One model, frames at exact rates.** Every reader produces, and every writer consumes, `nle-bridge/timeline@1`: assets, video and audio tracks of clips (`source_in` / `source_out` from the media's first frame, `record_in` / `record_out` from the timeline's first frame, out points exclusive), markers, captions. All times are whole frames at a rational rate: 23.976 = 24000/1001, 29.97 = 30000/1001, 59.94 = 60000/1001, also 47.952 and 119.88.
- **Seconds to frames.** Rounded to the nearest frame on exact rationals, ties to even. That is Python's `round()`, the rule ffmpeg-skill's timeline export uses, so a cut planned at 2.5 s at 25 fps lands on frame 62 in both exports (checked against ffmpeg-skill in the integration test).
- **Timecode.** Non-drop counts nominal frames (24 for 23.976). Drop-frame exists at 29.97 and 59.94 only and skips 2 (4) frame labels every minute except every tenth; labels it skips are refused, never moved. DF is written with `;` before the frames and `FCM: DROP FRAME` / `<displayformat>DF</displayformat>`. The record side starts at 01:00:00:00 unless the plan says otherwise (ffmpeg-skill's EDL does the same).
- **Source timecode.** A media file's start timecode comes from the plan (`assets.*.start_timecode`). It is drop-frame when the plan says so, when the label has `;`, or, unstated, when the media runs at a drop-frame timeline's rate. OTIO `source_range` and EDL source timecodes are absolute (start timecode + in point), as NLEs write them; FCP7 `in` / `out` count from the media's first frame with the start in the file's `<timecode>`. ffmpeg-skill `probe` does not report a start timecode, so without one in the plan it is 00:00:00:00 and the relink manifest says it is unknown.
- **OTIO.** Timeline.1 / Stack.1 / Track.1 / Clip.2 / Gap.1 / Marker.2 / ExternalReference.1, `file:///` URLs, markers on the stack, `global_start_time` at the record start. This is the shape ffmpeg-skill writes. Plan ids and marker kinds go in `metadata.nle_bridge`. OTIO has no audio-level schema: a level is metadata only (`LEVEL_METADATA_ONLY`).
- **FCP7 XML.** xmeml version 4, the version Premiere writes. One sequence; `rate` as timebase + ntsc; a `file` element in full on first use and by id after; `file://localhost/` URLs (Premiere's form) for POSIX and Windows paths alike; audio levels as the `audiolevels` filter (linear gain, 1.0 = 0 dB); sequence markers. FCP7 has no marker colour (`MARKER_COLOR_NOT_EXPORTED`). The plan id rides in `mastercomment1`. Files with `<!ENTITY` are refused on read.
- **EDL.** CMX 3600, cuts only. `V` for the first video track and `A`, `A2`, `A3`, `A4` for audio tracks 1-4; other tracks are `TRACK_NOT_EXPORTED`. Reel `AX` with the file in `* FROM CLIP NAME:` and `* SOURCE FILE:` (as ffmpeg-skill writes them), and the plan id in `* NLE-BRIDGE ID:`. Markers are `* LOC: <tc> <COLOUR> <name>`, the form Resolve writes and reads. The reader takes reel names with spaces (OTIO's adapter writes them) and `* FROM CLIP:` URLs. An EDL states no rate, so reading one needs the plan's rate or `--fps`.
- **What is not carried.** Effects (`EFFECT_NOT_EXPORTED`), speed changes (`SPEED_NOT_EXPORTED`), titles and generators (`TITLE_NOT_EXPORTED`, left as a gap) and transitions (`TRANSITION_NOT_EXPORTED`, written as a hard cut) are warnings with the item id, in `export`'s JSON and on stderr. `--strict` exits 5 before writing anything.
  - A clip with a speed change is written at 100 % from its in point for its planned length on the timeline. The cut points therefore stay where the plan put them, and with them the captions and the beat alignment. The editor redoes the speed.
- **Inputs.** The agent's project IR is read as its ADR-052 defines `delivery.timelines[]`: kept source ranges, assets by id with their probe facts, the subject's caption cues, and the `not_exported` list of its `timeline.export` decision as `NOT_IN_TIMELINE` warnings. ffmpeg-skill's render project is read as `render --export-timeline` reads it, with its own `not_exported` wording. A render project's default 0.5 s dissolve is not written (`TRANSITION_NOT_EXPORTED`); the agent always sets `transition: none`.
- **Outputs are new files.** An existing output needs `--overwrite`; an output equal to an input is refused; a symlink at a target is replaced, not followed. Nothing records a clock time, so the same input gives the same bytes.

### Follow-ups

- **Real NLE import.** Import the files into Premiere 25.x and Resolve Studio once one is available, and record what each keeps (marker colours from the EDL, levels from FCP7, OTIO metadata).
- **Dissolves.** OTIO, FCP7 XML and EDL can all carry a cross dissolve; written as an option later, centred on the cut as ffmpeg-skill does.
- **Media start timecode.** Needs ffmpeg-skill `probe` to report the `tmcd` / `timecode` tag; ffmpeg-skill is treated as complete, so it is noted, not changed.

## ADR-002 Beat grid: markers from a grid that is read, never made

Checked 2026-10-31 against ffmpeg-skill 2.24 `scenes.py --beats --json` and video-production-agent ADR-054 (beat-snapped take assembly).

### Requirements

The cut is to be cut on the music, and the music may come from a host-side generator (for example a music-generation MCP server in the chat app) or be measured. The editor should see the grid in the NLE, bar heads distinct from the other beats, and the round trip should say how far each cut is from a beat after the editor touched it. Generating or analysing music is **outside** this skill.

### Decision

- **Three input shapes, one internal grid.**
  - ffmpeg-skill `scenes --beats --json`: `beats` (the regular grid) and `beat_grid` {`tempo_bpm`, `interval`, `phase`, `confidence`, `supported_beats`, `usable`, ...}. This is what the agent's take assembly measures its music bed with.
  - A bare `beat_grid` object, as the agent's analysis record keeps it.
  - `nle-bridge/beatgrid@1`: `{"bpm", "beats", "bars"}` stated by whoever made the music, or `{"bpm", "beats_per_bar", "first_downbeat", "duration"}` for a regular grid.
- **Which beats.** For ffmpeg-skill's shape the default is `supported_beats`: the beats a measured onset supports, the subset the agent snaps cuts to (ADR-054). `--beat-set grid` takes the whole regular grid. A grid marked `usable: false` is used and named (`BEAT_GRID_UNUSABLE`).
- **Bars.** Stated bars are used as given (`bars_provenance: STATED`). Without them, bar heads are every `--beats-per-bar` beats (default 4) from the grid's first beat, and the result says ASSUMED (`BARS_ASSUMED`). Beats are numbered on the regular grid, so a supported subset with gaps keeps each beat's bar.beat position.
- **Where the music starts.** `--beat-offset` seconds; default 0, because the agent lays the bed from programme time 0.
- **Markers.** Bar heads are `Bar N`, RED; other beats are `Beat N.b`, CYAN (`--beat-markers all | bars | none`). Plan markers keep their own colours (default GREEN). OTIO and EDL carry the colours. FCP7 XML has none, so the kind is read back from the name. Beats are quantised to frames like every time here.
- **Offsets.** For a cut at frame f, the nearest beat b (ties to the earlier beat): offset = f - b x rate, in frames, signed (positive: the cut is after the beat), not rounded to whole frames, because at 29.97 a beat is rarely on a frame boundary. `on_beat` when |offset| <= `--beat-tolerance` (default 0.5 frame, the best a frame-quantised cut can do).

### Not this skill's job

Generating music, measuring beats (ffmpeg-skill `scenes --beats`), and moving cuts onto beats (video-production-agent ADR-054). This skill shows the grid and measures distances to it.

## ADR-003 Conform package and round trip

### Conform package

- **Layout.** `timeline/` (the exports and the plan as `nle-bridge/timeline@1`), `media/` only with `--copy-media` (the timelines then point at the copies), `relink.json` (`nle-bridge/relink@1`) and `manifest.json` (`nle-bridge/conform@1`: every file with its size and sha256, the warnings). Optionally a zip next to the folder.
- **Relink facts.** For each media file: the plan's path, the path in the package, whether it exists, size, sha256 (streamed; `--no-hash` skips it), start timecode, length, fps and picture size, and the ranges the timeline uses (source in / out and record in / out as timecode). `facts_from` says where each fact came from: `ffmpeg-skill probe` when ffmpeg-skill is found (the only media boundary; this skill never runs ffprobe), `plan`, `computed`, or null. A sha256 that differs from the plan's is `MEDIA_CHANGED`; a missing file is `MEDIA_MISSING`, a warning, because a package is often made where the media is not.
- **Determinism.** No clock time anywhere. The zip has sorted entries and the date 1980-01-01, and is written with ZIP64 for large media.
- **Safety.** The package folder must be new or empty (or `--overwrite`), must not contain an input, and only the package's own files are written.

### Round trip

- **Reading.** The returned file goes through the same readers, with the plan's rate (an EDL has none), the plan's record start when the file gives none, and the plan's media start timecodes (EDL source timecodes are absolute).
- **Matching media.** By path, then file name (case-insensitive), then file name without extension. The last covers an NLE that relinked to a transcode.
- **Matching clips.** Tracks pair by kind and order. Within a pair, the plan id the file still carries (OTIO metadata, FCP7 master comment, EDL comment) wins when the media agrees. Otherwise pairs are formed greedily by the largest source overlap of the same media, then the nearest record position. A plan clip is compared as it was exported: clips only, at 100 % for its record length. Titles never left, so they are listed under `not_compared`, not as removed.
- **Changes.** Per pair: `delta_frames` of the four ends; `trimmed` (source ends moved), `slipped` (source moved, record kept), `moved` (record moved, source kept), `retimed`, `level` (FCP7; OTIO / EDL do not carry levels, so a missing level there is not a change), `enabled` / `disabled`. Leftovers are `removed` / `added`; an added clip overlapping a plan clip of the same media is `split_from` it (a razor cut).
- **Cut points.** The edit points of the first video track, except the first and last frame. A plan cut follows its clip pair to the returned position. `delta_frames` is the move; unpaired cuts are `added` / `removed`.
- **Markers.** Paired by name (nearest), then by position (renamed). Colour is not compared for FCP7 (it has none). Beat-grid markers are counted only.
- **Not imported.** Transitions, speed changes, effects, titles and nested sequences in the returned file are listed in `not_imported`. They are not merged into the plan: they are the editor's, and the plan cannot express them.

### Follow-ups

- The agent side: read `diff` and `--timeline-out` as a new plan version (video-production-agent, its adapter step).
- Premiere's per-channel audio tracks (a stereo clip as two linked clips on A1 and A2) pair track by track and show A2 as added. Folding linked channel clips back into one is a later refinement.
