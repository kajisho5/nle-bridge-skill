<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/app-icon-dark.png">
    <img src="brand/app-icon.png" width="128" alt="nle-bridge-skill app icon">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/logo/nle-bridge-skill-wordmark-white.svg">
    <img src="brand/logo/nle-bridge-skill-wordmark-black.svg" height="48" alt="nle-bridge-skill">
  </picture>
</p>

<p align="center"><b>Timelines to Premiere and Resolve, and back</b></p>

Timeline interchange with Premiere Pro and DaVinci Resolve, for the [Velisa](https://github.com/kajisho5/Velisa) ecosystem (department 納品 / Delivery). An agent's edit plan becomes editor timelines (OTIO, FCP7 XML, CMX 3600 EDL) with captions as an SRT sidecar; a conform package carries the timelines and a relink manifest of every media file; and the timeline the editor sends back is compared with the plan, cut by cut.

Typical use: [video-production-agent](https://github.com/kajisho5/video-production-agent) plans a rough cut. `export` hands it to an editor as a timeline Premiere or Resolve imports. `conform` packs it with what is needed to relink the media on another machine. When the editor is done, the NLE exports OTIO (or FCP7 XML / EDL) and `diff` says what changed: which cuts moved and by how many frames, which clips were trimmed, added or removed, which markers changed. With a beat grid (the music bed's beats, from ffmpeg-skill `scenes --beats` or stated by whatever generated the music), the timelines carry bar and beat markers, and `diff` says how many frames each cut sits off the nearest beat.

This skill writes and reads files. It does not drive a running NLE: live control is left to the community MCP servers (Velisa ROADMAP, "Premiere Pro and DaVinci Resolve"), and none of their code is used here. [docs/live-control.md](docs/live-control.md) says which two to run beside it, at which pinned versions, with their script tools and telemetry off.

```bash
pip install -e '.[otio]'                         # the extra re-reads every .otio with opentimelineio as a check
nle-bridge doctor
nle-bridge export plan.json -o out/              # out/<name>.otio  .xml  .edl  (.srt when there are captions)
nle-bridge export project.json --subject programme -o out/ --beats beats.json   # the agent's project IR
nle-bridge conform plan.json -o pkg/ --zip       # pkg/timeline/*, pkg/relink.json, pkg/manifest.json, pkg.zip
nle-bridge diff plan.json from_editor.otio -o diff.json --beats beats.json --timeline-out plan_v2.json
nle-bridge import from_editor.xml -o plan_v2.json
```

## Inputs

| Input | How it is read |
|---|---|
| `nle-bridge/timeline@1` (JSON) | Assets (path, fps, size, length, start timecode), video and audio tracks of clips, markers, captions. Times in frames, or in seconds (`in` / `out` / `at`); clips without a position follow each other. See `src/nle_bridge/model.py` |
| video-production-agent project IR | `delivery.timelines[]` (agent ADR-052), picked with `--subject` when there are several: the kept source ranges in programme order, the assets' paths and probe facts, the subject's caption cues; what the agent's `timeline.export` decision lists as `not_exported` becomes a warning |
| ffmpeg-skill render project | The document `render --export-timeline` reads (`clips[{src, in, out, speed}]`, `frame`, `chapters`, `audio.music`, `captions` as an .srt): chapters become markers, the music bed an audio track; project keys a timeline cannot carry are warnings, worded as ffmpeg-skill words them |
| `.otio`, FCP7 `.xml`, `.edl` | An editor's timeline (`import`, and the returned side of `diff`). An EDL states no frame rate: `--fps`, or the plan's rate in `diff` |

## Outputs

| Format | Carries | Does not carry |
|---|---|---|
| OTIO (`.otio`) | Cuts, video and audio tracks, gaps, markers with colour, the media's start timecode, plan ids in metadata | Audio levels (kept as metadata only) |
| FCP7 XML (`.xml`, xmeml 4, for Premiere) | Cuts, video and audio tracks, markers, audio levels (`audiolevels`), DF / NDF, file paths as `file://localhost/...` | Marker colours (FCP7 has none; bars and beats are told apart by name) |
| CMX 3600 EDL (`.edl`) | Cuts on one picture track and up to 4 audio tracks, markers with colour (`* LOC:`), DF / NDF, files in `* FROM CLIP NAME` / `* SOURCE FILE` | Other video tracks, levels, marker durations |
| SRT (`.srt`) | Captions in programme time | |

**Never dropped silently.** Effects, speed changes, titles / generators and transitions do not survive interchange. Each one is a warning naming the item (`SPEED_NOT_EXPORTED`, `EFFECT_NOT_EXPORTED`, `TITLE_NOT_EXPORTED`, `TRANSITION_NOT_EXPORTED`), and so is every format-specific loss (`TRACK_NOT_EXPORTED`, `LEVEL_NOT_EXPORTED`, `MARKER_COLOR_NOT_EXPORTED`, ...). `--strict` exits 5 instead of writing. A clip with a speed change is written at 100 % from its in point for its planned length, so the cut points stay where the plan put them.

**Time.** Every time is a whole frame at an exact rate: 23.976 is 24000/1001, 29.97 is 30000/1001, 59.94 is 60000/1001. Drop-frame timecode exists at 29.97 and 59.94 only (`;` before the frames, `FCM: DROP FRAME`). Seconds become frames by rounding to the nearest frame, ties to even, the rule ffmpeg-skill's own timeline export uses. The record side starts at 01:00:00:00 unless the plan says otherwise; source timecodes are the media's own start timecode plus the in point. Why these choices: [docs/decisions.md](docs/decisions.md) ADR-001.

## Beat grid

`--beats grid.json` reads ffmpeg-skill `scenes --beats --json` output (`beats`, `beat_grid`; the measurement the agent's take assembly uses), a `beat_grid` object, or `nle-bridge/beatgrid@1` (`{"bpm": 120, "beats": [...], "bars": [...]}`, or `{"bpm", "beats_per_bar", "first_downbeat", "duration"}` for a regular grid). Bar heads become red `Bar N` markers and the other beats cyan `Beat N.b` (`--beat-markers bars` for bar heads only). A grid that states no bars gets them every `--beats-per-bar` beats (default 4) from its first beat, and the result says the bars are ASSUMED. `--beat-offset` is where the music starts on the timeline (default 0). Generating or analysing the music is not this skill's job ([ADR-002](docs/decisions.md)).

## Conform package

```
pkg/
  timeline/<name>.otio .xml .edl .srt .json   the cut, and the plan as nle-bridge/timeline@1
  media/                                       only with --copy-media (the timelines then point here)
  relink.json                                  every media file: path, exists, size, sha256, start timecode,
                                               length, fps, picture size, where each fact came from, ranges used
  manifest.json                                every file of the package with its sha256, and the warnings
pkg.zip                                        with --zip: fixed entry dates, sorted, so the same input gives the same bytes
```

Picture size, rate and length come from ffmpeg-skill `probe` when ffmpeg-skill is found (`--ffmpeg-skill`, `$FFMPEG_SKILL_DIR`, `~/.claude/skills/ffmpeg-skill`, `../ffmpeg-skill`), otherwise from the plan; `facts_from` says which. A media file that is not there is a `MEDIA_MISSING` warning, not an error. ffmpeg-skill's probe does not report a media start timecode, so that comes from the plan or is marked unknown (this skill does not run ffprobe itself; [the hand-over](docs/handover-media-start-timecode.md)).

## Round trip

`diff plan returned` reads the editor's timeline against the plan:

- **Media** match by path, then file name, then file name without extension (a relink to a transcode).
- **Clips** pair track by track (V1 with V1, A1 with A1) by the plan id the file still carries, else by the same media with overlapping source ranges. Each pair says how its ends moved (`moved`, `trimmed`, `slipped`, `retimed`, `level`, `enabled` / `disabled`). Leftovers are `removed` and `added`; an added clip that overlaps a plan clip of the same media is marked `split_from` (a razor cut).
- **Cut points** of the first video track: each plan cut with its returned position and the move in frames, and the cuts added or removed.
- **Markers** added, removed, moved, renamed, re-commented or re-coloured. Beat-grid markers are counted, not diffed.
- **Beats** (with `--beats`): for each cut, the nearest beat (`bar.beat`), the signed offset in frames (positive: after the beat), `on_beat` within `--beat-tolerance` (default 0.5 frame), for the plan's position and the returned one; the summary counts the cuts that moved off the beat.
- **Not imported**: effects, speed changes, transitions and titles the editor added are listed under `not_imported`; titles in the plan are under `not_compared` (they never left).

`--timeline-out` writes the returned timeline as a new plan (`nle-bridge/timeline@1`, the plan's asset ids, `provenance: EDITOR`). Method and limits: [ADR-003](docs/decisions.md).

## Commands

| Command | What it does |
|---|---|
| `export <plan> -o <dir> [--formats otio,fcp7,edl,srt] [--name stem] [--subject s] [--fps r] [--drop-frame] [--beats grid] [--beat-markers all\|bars\|none] [--strict]` | The timelines and the SRT; `files` with sha256 and the independent re-read (`verification`), `warnings`, `timeline` summary |
| `conform <plan> -o <dir> [--copy-media] [--zip] [--no-hash] [--no-probe] [--ffmpeg-skill dir] ...` | The conform package above |
| `diff <plan> <returned> [-o diff.json] [--timeline-out plan.json] [--beats grid] [--beat-tolerance 0.5]` | `nle-bridge/diff@1` |
| `import <file> [-o timeline.json] [--fps r] [--subject s]` | Any input as `nle-bridge/timeline@1` |
| `contract --json` | Machine-readable contract in Velisa's registry shape |
| `doctor --json` | Python, ffmpeg-skill (optional), opentimelineio (optional) |

Every command prints one JSON document with `--json`. Existing outputs are refused without `--overwrite`, and an output equal to an input is refused. Exit codes: 0 ok, 2 invalid input or arguments, 3 ffmpeg-skill lacks `probe`, 4 ffmpeg-skill failed, 5 `--strict` and something would be dropped.

## Limits

- Neither NLE was available to test against: the files are checked with opentimelineio and its FCP7 XML and CMX 3600 adapters (and ffmpeg-skill's own exports), not by importing them into Premiere or Resolve yet.
- Hard cuts only. Dissolves, speed ramps, effects and titles are named, not written; they stay in the agent's ffmpeg path or are redone in the NLE.
- Clips are counted at the timeline rate; a source at another rate is a `RATE_MISMATCH` warning and the NLE conforms it.
- FCP7 XML markers have no colour. Premiere writes one audio track per channel, so a stereo clip comes back as two linked clips on A1 and A2.
- FCPXML (Final Cut Pro X) and AAF are not read or written here.

## Tests

```bash
pip install -e '.[test]'
python -m unittest discover -s tests -v
```

Timecode at 23.976, 25, 29.97 DF / NDF and 59.94 DF / NDF (anchors, every frame of the first 40 000 round trip, labels drop-frame skips are refused); every format written and read back identical at every rate; the same files read by opentimelineio and its FCP7 / CMX 3600 adapters frame for frame, and timelines written by those adapters read back; editor changes (trims, a razor cut, a removed clip, moved and added markers) on OTIO, FCP7 XML and EDL; beat markers and beat offsets; the agent's project IR and an ffmpeg-skill render project as input; conform packages (relink facts, missing media, deterministic zip). With ffmpeg and an ffmpeg-skill checkout, media facts come from the real `probe`, and ffmpeg-skill's own `render --export-timeline` output is read and compared with this skill's; with a Velisa checkout, the registry conformance checks and the contract schema run.

## License

MIT
