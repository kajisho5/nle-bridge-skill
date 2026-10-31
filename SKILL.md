---
name: nle-bridge-skill
description: Hand an edit to Premiere Pro or DaVinci Resolve and read the editor's changes back. Writes an edit plan (nle-bridge timeline JSON, video-production-agent's project IR, or an ffmpeg-skill render project) as OpenTimelineIO, FCP7 XML (Premiere) and CMX 3600 EDL with captions as an SRT sidecar, at exact frame rates (23.976, 25, 29.97 / 59.94 drop-frame and non-drop); adds bar and beat markers from a music beat grid; builds a conform package (timelines, relink manifest with paths, sha256, timecode, size and fps, optional media copy, zip); and compares the timeline the NLE exports back with the plan - cuts moved by how many frames, clips trimmed / added / removed, markers changed, and how far each cut is off the beat. Says, item by item, what a format cannot carry (effects, speed changes, titles, transitions) instead of dropping it. Use when asked to export a timeline or XML / EDL / OTIO for Premiere or Resolve, send a cut to an editor, make a conform or relink package, put beat markers on a timeline, or find what the editor changed. Files only, no live NLE control; local, no network.
---

# nle-bridge-skill

Run `nle-bridge <command> ... --json` (installed by `pip install -e .`; `pip install -e '.[otio]'` adds an independent re-read of each .otio). Every command prints one JSON document. `nle-bridge doctor --json` says what is available; nothing is required beyond Python.

| Task | Command |
|---|---|
| Timelines for Premiere / Resolve from a plan | `nle-bridge export plan.json -o out/ --json` (OTIO, FCP7 .xml, .edl, and .srt when the plan has captions) |
| From the agent's project | `nle-bridge export project.json --subject programme -o out/ --json` |
| With the music's bars and beats as markers | add `--beats beats.json` (ffmpeg-skill `scenes --beats --json` output, or `{"bpm":120,"beats":[...],"bars":[...]}`); `--beat-markers bars` for bar heads only |
| With the plan's dissolves as cross dissolves | add `--dissolves` (centred on the cut, in all three formats; a render project's default fade too) |
| Only some formats | `--formats otio,fcp7` (Premiere: OTIO or FCP7 XML; Resolve: any) |
| A package for another machine | `nle-bridge conform plan.json -o pkg/ --zip --json` (`--copy-media` to include the media) |
| What the editor changed | `nle-bridge diff plan.json returned.otio -o diff.json --json` (`.xml` / `.edl` too; an EDL uses the plan's rate) |
| ...and how far the cuts are off the beat | add `--beats beats.json` |
| The editor's cut as the next plan | add `--timeline-out plan_v2.json` |
| Any timeline as JSON | `nle-bridge import file.otio -o timeline.json --json` (`--fps` for an EDL) |
| What this skill declares | `nle-bridge contract --json` |

## How to use it well

1. Prefer OTIO for both NLEs; add FCP7 XML for Premiere versions without OTIO import, and the EDL for anything else. The SRT always travels next to them.
2. Read `warnings` and pass every one on: each names an item the files do not carry (a speed change, an effect, a title, a transition, a second picture track in the EDL, levels in OTIO / EDL, marker colours in FCP7). Use `--strict` when nothing may be lost.
3. Give the beat grid that matches the music actually on the timeline. When it states no bars, the bar heads are counted every 4 beats from its first beat (`bars_provenance: ASSUMED`); say so.
4. For `diff`, compare against the same plan that was exported. A clip that moved shows in `clips`; the cut points and their frame moves are in `cuts`; `beats` counts the cuts that left the beat.

## Report it honestly

- Interchange is cuts, tracks, markers, (FCP7) levels and, only with `--dissolves`, cross dissolves. Do not say the effects, speed changes, titles or other transitions were exported: they are in `warnings`, and a sped-up clip is written at 100 % for its planned length. A dissolve that could not be written is in `warnings` too.
- The files were checked with opentimelineio and its adapters, not imported into a real Premiere or Resolve by this skill. Say that if asked whether it "works in Premiere".
- `diff` is what the returned file says. Cross dissolves are compared (`transition`); effects, other transitions and titles the editor added are listed in `not_imported`, not merged into the plan. A clip the editor replaced with different media shows as removed + added.
- A beat offset is in frames from the nearest beat of the grid given; with an ASSUMED bar the bar number is a count, not a fact about the music.
- This skill does not control a running NLE, generate music, burn or translate captions, or render.

Exit codes: `0` ok, `2` invalid input or arguments (including an existing output without `--overwrite`), `3` ffmpeg-skill lacks `probe`, `4` ffmpeg-skill failed, `5` `--strict` and something would be dropped.

## Not this skill's job

Live NLE control (the community Resolve / Premiere MCP servers, pinned and with script tools off: docs/live-control.md), rendering and transcoding (ffmpeg-skill), FCPXML for Final Cut (ffmpeg-skill `render --export-timeline`), captions themselves (subtitle-skill), music and its beat measurement (sound-design-skill, ffmpeg-skill `scenes --beats`), planning the edit (video-production-agent).
