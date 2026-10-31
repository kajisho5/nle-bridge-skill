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
- **Dissolves.** OTIO, FCP7 XML and EDL can all carry a cross dissolve; written as an option later, centred on the cut as ffmpeg-skill does. Done: `--dissolves`, ADR-006.
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
- **Not imported.** Transitions other than a cross dissolve (ADR-006 reads those), speed changes, effects, titles and nested sequences in the returned file are listed in `not_imported`. They are not merged into the plan: they are the editor's, and the plan cannot express them.

### Follow-ups

- The agent side: read `diff` and `--timeline-out` as a new plan version (video-production-agent, its adapter step).
- Premiere's per-channel audio tracks (a stereo clip as two linked clips on A1 and A2) pair track by track and show A2 as added. Folding linked channel clips back into one is a later refinement.

## ADR-004 Live control: reuse two pinned community MCP servers, eval tools off, no code taken

Checked 2026-10-31 against Velisa `docs/ecosystem/ROADMAP.md` ("Premiere Pro and DaVinci Resolve") and the servers' own repositories at their release tags and npm entries (read only; nothing cloned into or copied to this repository).

### Requirements

Velisa's roadmap: live NLE control is reuse. Do not fork the community servers; pin a version; keep eval and script tools off; opt out of telemetry where it exists. Its risks: Resolve automation needs Studio, CEP-based Premiere servers break when CEP is retired, arbitrary-script tools (`execute_extendscript`, `execute_lua`) and shared temp-directory IPC, and one Resolve server without a licence and another under GPL-3.0, from which no code may be copied.

### Candidates

| Server | Licence | Maintenance | Code-execution tools | Telemetry | Verdict |
|---|---|---|---|---|---|
| samuelgursky/davinci-resolve-mcp 4.17.0 | MIT | releases every few days, tests, SECURITY.md | none since v3.0.0 (removed; an AST test keeps `exec` / `eval` / `RunScript` out of `src/`) | none; a GitHub release check on start, can be turned off | **Used for Resolve** |
| leancoderkavy/premiere-pro-mcp 1.27.0 | MIT | releases every few days, tests, SECURITY.md | `execute_extendscript`, `evaluate_expression`, refused unless the `unsafe-script` capability is granted | PostHog, only when `POSTHOG_API_KEY` is set | **Used for Premiere** |
| Tooflex/davinci-resolve-mcp | none (no licence file) | last commit 2026-07-26 | `execute_lua` | - | Rejected |
| apvlv/davinci-resolve-mcp | `LICENSE` MIT, a directory says GPL-3.0 | last commit 2026-10-22 | `execute_python`, `execute_lua` | - | Rejected |
| nepfaff/premiere-pro-mcp | none (no licence file) | last commit 2026-02-09 | one tool that `eval()`s UXP JavaScript | - | Rejected |
| tro2789/scout-premiere-mcp | proprietary | last commit 2026-09-01 | `execute_extendscript` | - | Rejected |

### Decision

- **Documented, not built.** [docs/live-control.md](live-control.md) gives the pins (`davinci-resolve-mcp@4.17.0`, `premiere-pro-mcp@1.27.0`, with their tag commits), the install commands of the servers themselves, a chat-app config, and the switches: `PREMIERE_MCP_CAPABILITIES=inspect,edit,export,filesystem` (never `unsafe-script`), no `POSTHOG_API_KEY`, `DAVINCI_RESOLVE_MCP_UPDATE_CHECK=0` / `UPDATE_MODE=never`, stdio only, Resolve safe mode, Premiere's project memory in RAM. This repository contains no server code, no wrapper and no fork.
- **Which file each NLE gets through its server.** Resolve: the OTIO through `import_timeline_checked` / `import_timeline` (Resolve `ImportTimelineFromFile`). Premiere: the FCP7 XML through `import_fcp_xml`, because premiere-pro-mcp 1.27.0 has no OTIO import tool (its source exports OTIO only, and refuses EDL import by design); the OTIO is imported by hand when wanted. This keeps FCP7 XML a first-class output.
- **A pin moves only after a re-check** of licence, code-execution tools, telemetry and the import tool at the new tag, recorded on that page.
- `contract.not_provided` keeps "live control of Premiere Pro or DaVinci Resolve".

### Not decided here

What each NLE keeps on import (roadmap step 8, with the real software), and the agent calling these servers (video-production-agent).

## ADR-005 Media start timecode stays a hand-over to ffmpeg-skill; this skill does not run ffprobe

Checked 2026-10-31 against ffmpeg-skill 2.24 `scripts/_common/probe.py`, Velisa `ecosystem.json`, `docs/ecosystem/ROADMAP.md`, `docs/DEPENDENCY_GRAPH.md` and `docs/CAPABILITY_MATRIX.md`, and ingest-skill `src/ingest_skill/core.py` (all read only).

### Question

ffmpeg-skill `probe` does not report a media file's start timecode (it keeps six format tags, not `timecode`). Could nle-bridge call ffprobe itself to read the `timecode` tag, the way qc-skill and media-analysis-skill call ffprobe directly?

### Findings

- **This skill's own contract and ADR-003** say ffmpeg-skill `probe` is the only media boundary: `not_provided` lists "running ffmpeg or ffprobe directly (only through ffmpeg-skill)".
- **Ownership.** `ecosystem.json` gives "FFmpeg実行" (FFmpeg execution) to ffmpeg-skill, "メディア計測" (media measurement) to media-analysis-skill and "素材台帳" (the clip inventory) to ingest-skill; nle-bridge owns only "NLE連携". This roadmap's rule is that work outside the owned list is handed over, not re-implemented.
- **The precedent is a known wart, not a licence.** qc-skill and media-analysis-skill do call the binaries directly, but Velisa's `CAPABILITY_MATRIX.md` and `DEPENDENCY_GRAPH.md` record that as a capability collision and silent duplication to be made visible, not as the pattern for new skills.
- **ffmpeg-skill is treated as complete** (Velisa ROADMAP): no change there from this repository.
- **The fact is already measured elsewhere.** ingest-skill's inventory records `timecode` per clip when ffprobe is installed.

### Decision

- **Not allowed: no ffprobe call here.** Step 12 stays open.
- **Hand-over written:** [docs/handover-media-start-timecode.md](handover-media-start-timecode.md) states the field ffmpeg-skill `probe` would add, its tests, and the small change here once it exists.
- **Meanwhile** the plan carries it (`assets.*.start_timecode`, `drop_frame`); the agent can copy it from ingest-skill's inventory. Without it the media starts at 00:00:00:00 and the relink manifest says the start timecode is not known (unchanged from ADR-001).

## ADR-006 Cross dissolves centred on the cut, as an option

Checked 2026-10-31 against ffmpeg-skill 2.24 `scripts/_common/timeline.py` (read only; its `to_otio` / `to_edl` and the layout of `transition`), opentimelineio 0.18.1 with otio-fcp-adapter 1.0.0 and otio-cmx3600-adapter 1.0.0 (how the reference reads and writes transitions), and ADR-001's follow-up.

### Requirements

- Roadmap step 10: cross dissolves in OTIO, FCP7 XML and EDL, centred on the cut, as ffmpeg-skill does.
- ffmpeg-skill's own export and this skill's must agree frame for frame on the same render project, now with its default 0.5 s fade too.
- The cut points must not move: captions, markers and the beat analysis hang on them (the reason speed changes are written at 100 %).
- Opt-in: nothing changes for a plan exported as before, and real-NLE behaviour is still unverified (step 8).

### Decision

- **`--dissolves`** on `export`, `conform`, `diff` and `import`. Without it every transition is a hard cut named by `TRANSITION_NOT_EXPORTED`, as in 0.1.0.
- **Model.** An item's `transition_in` is normalised to `{type, frames, before}`: a type name or an object with `duration` (seconds, default 0.5 like render.py), `frames` or `duration_frames`; `before` is how many frames lie before the cut, by default `d - d // 2`. That is ffmpeg-skill's split (the incoming clip gives `d - d // 2` frames of head, the outgoing `d // 2` of tail), so an odd dissolve has its extra frame before the cut. A transition of type `none` or 0 frames is no transition.
- **The cut stays.** Record ranges and visible source ranges are the plan's; a dissolve uses `before` frames of the incoming clip's media before its in point and `after = d - before` frames of the outgoing clip's media after its out point. `transitions.resolve()` writes one only when a clip ends exactly where the incoming one starts, both are long enough (also against a neighbouring dissolve), and the media has the frames (the outgoing clip's checked when its length is known). Otherwise a hard cut with `TRANSITION_NOT_EXPORTED` naming the reason. A type that is not a dissolve (`wipe`, an xfade name) is written as a cross dissolve with `TRANSITION_AS_DISSOLVE`, as ffmpeg-skill notes for its non-fade xfades.
- **OTIO.** `Transition.1`, `SMPTE_Dissolve`, `in_offset` = before, `out_offset` = after, between the two clips, whose source ranges stay their visible parts: ffmpeg-skill's shape.
- **FCP7 XML.** A `transitionitem` (start / end on the sequence, `alignment` center, or start / end when one side is 0; "Cross Dissolve" / effectid `Cross Dissolve` on video, "Cross Fade (+3dB)" / `KGAudioTransCrossFade3dB` on audio). The adjacent clipitems have `start` / `end` -1 and their `in` / `out` include the dissolve's media, as FCP7 and OTIO's FCP7 adapter write it. `center` cannot say where an odd dissolve's cut is: the reader takes `end - (end - start) // 2` (the extra frame before the cut, matching what is written); OTIO's adapter truncates `(start + end) / 2`, one frame earlier, so the reference cross-check uses an even length. A dissolve neither centred nor at an edge (only from an imported timeline) is written centred (`TRANSITION_OFF_CENTRE`).
- **EDL.** The standard pair ffmpeg-skill writes: a zero-length `C` line on the outgoing source where its event ends, then `D nnn` bringing the incoming clip in from where the dissolve starts, with `* FROM CLIP NAME` / `* TO CLIP NAME`, `* SOURCE FILE` and the plan id. Each event runs from where its dissolve starts to where the next one starts. CMX 3600 has no cut point inside a dissolve, so the reader puts the cut at the centre (`before = d - d // 2`); OTIO's CMX reader puts it at the start of the D event instead (in_offset 0), which the cross-check states.
- **Reading back.** All three readers take a dissolve between two clips into the incoming clip's `transition_in` (a wipe or a dissolve from black still is `TRANSITION_NOT_IMPORTED`, with the cut where the transition says). `diff` compares `{frames, before}` per clip pair as a `transition` change and counts `transitions_changed`; the plan's dissolves count as exported only with `--dissolves`.
- **Render projects.** With `--dissolves` the project's transition is laid out exactly as ffmpeg-skill's export lays it: seconds to frames with the same rounding, each clip trimmed by its dissolves' head / tail, so the timeline is as long as the render. The integration test compares ffmpeg-skill's own .otio and .edl of a three-clip project with its default fade against this skill's, clip for clip and cut for cut.
- **Conform.** Each used range in `relink.json` names `handle_before` / `handle_after` when a dissolve needs media beyond it, so the media is not trimmed too tight.

### Not decided here

What Premiere and Resolve make of each form on import, odd-length centring included (step 8).
