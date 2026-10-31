# Roadmap — nle-bridge-skill

Part of the [Velisa](https://github.com/kajisho5/Velisa/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **納品 (Delivery)**, `planned` in `ecosystem.json` until Velisa updates it.
Written on 2026-10-31 from Velisa's ROADMAP ("Premiere Pro and DaVinci Resolve") and `ecosystem.json`, video-production-agent ADR-052 (timeline export) and ADR-054 (beat-snapped takes), and ffmpeg-skill `render --export-timeline` (all read only). Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

0.1.0: `export` (OTIO, FCP7 XML, CMX 3600 EDL, SRT sidecar; exact NTSC rates, drop-frame; every loss a named warning, `--strict`), beat-grid markers (bar heads distinct), `conform` (relink manifest, file manifest, deterministic zip, optional media copy; media facts, start timecodes included from ffmpeg-skill 3.0.0, through ffmpeg-skill `probe` when available), `diff` (the editor's returned OTIO / FCP7 XML / EDL against the plan: clips, cut points in frames, markers, offsets from the beat) and `import`, with contract and doctor. Standard library only; opentimelineio is an optional check, and in the tests OTIO's FCP7 / CMX 3600 adapters are a second reader. Not yet imported into a real Premiere or Resolve.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| NLE連携(タイムライン受け渡し) | partial | Files both ways ([#1](https://github.com/kajisho5/nle-bridge-skill/pull/1)); live NLE control is reuse of two pinned community MCP servers ([docs/live-control.md](docs/live-control.md)), real-NLE import unverified (step 8) |

## Wiring

Reads video-production-agent's project IR (`delivery.timelines[]`, captions, the `timeline.export` decision's `not_exported`) and ffmpeg-skill render projects; reads ffmpeg-skill `scenes --beats` grids; uses ffmpeg-skill `probe` (optional, `>=2.0.0 <4.0.0`) for media facts, and from 3.0.0 for each file's start timecode. Its OTIO and EDL follow the shape ffmpeg-skill's `render --export-timeline` writes, and the integration test reads those exports back cut for cut. No agent adapter yet (step 7).

## Steps

- [x] **1. Skeleton + contract/doctor + `nle-bridge/timeline@1`** ([#1](https://github.com/kajisho5/nle-bridge-skill/pull/1))
  - Done when: Velisa static checks PASS, contract validates against `registry/capability_contract.schema.json`
- [x] **2. EXPORT op** — OTIO / FCP7 XML / EDL + SRT; effects, speed, titles, transitions as named warnings ([#1](https://github.com/kajisho5/nle-bridge-skill/pull/1); ADR-001)
  - Done when: each format reads back identical at 23.976 / 25 / 29.97 DF+NDF / 59.94 DF+NDF, and opentimelineio's readers see the same cut
- [x] **3. Beat-grid markers** — ffmpeg-skill `scenes --beats` or a stated grid; bar heads in another colour ([#1](https://github.com/kajisho5/nle-bridge-skill/pull/1); ADR-002)
  - Done when: bar / beat markers in OTIO, FCP7 XML and EDL, read back by kind
- [x] **4. CONFORM package** — relink manifest (path, sha256, TC, size, fps, ranges used), zip ([#1](https://github.com/kajisho5/nle-bridge-skill/pull/1); ADR-003)
  - Done when: deterministic zip; facts from the real ffmpeg-skill probe
- [x] **5. ROUND TRIP diff** — returned OTIO / FCP7 XML / EDL vs the plan; cut moves in frames; beat offsets ([#1](https://github.com/kajisho5/nle-bridge-skill/pull/1); ADR-003)
  - Done when: trims, a razor cut, a removed clip and marker edits found on all three formats; ffmpeg-skill's own exports read back unchanged
- [x] **6. SKILL.md, CI** (ubuntu / macOS / Windows x Python 3.9 / 3.12, integration with ffmpeg-skill and Velisa; [#1](https://github.com/kajisho5/nle-bridge-skill/pull/1))
- [ ] **7. (agent side) adapter** — video-production-agent hands `delivery.timeline` to `nle-bridge export` / `conform` and reads `diff` + `--timeline-out` as a new plan version (belongs to video-production-agent)
- [ ] **8. Real-NLE check** — import the files into Premiere Pro 25.x and DaVinci Resolve Studio; record what each keeps (EDL marker colours, FCP7 levels, OTIO metadata, start timecodes)
  - Done when: a fixture per NLE in `tests/fixtures/` exported by the real NLE, read back by `diff`
- [x] **9. Live control by reuse** — document the pinned community MCP servers (Resolve `ImportTimelineFromFile`, Premiere OTIO import) beside this skill; eval / script tools off. No fork, no copied code ([#2](https://github.com/kajisho5/nle-bridge-skill/pull/2); ADR-004, [docs/live-control.md](docs/live-control.md))
  - Result: davinci-resolve-mcp 4.17.0 (MIT, no code-execution tools) and premiere-pro-mcp 1.27.0 (MIT, `unsafe-script` off, telemetry off without a key). Premiere's server imports the FCP7 XML; it has no OTIO import tool at 1.27.0
- [x] **10. Dissolves (optional)** — cross dissolves centred on the cut in all three formats, as ffmpeg-skill does ([#3](https://github.com/kajisho5/nle-bridge-skill/pull/3); ADR-006)
  - `--dissolves`; read back from all three formats and compared in `diff`; ffmpeg-skill's own .otio / .edl of a render project with its default fade read back cut for cut
- [x] **11. Premiere stereo pairs** — fold linked per-channel audio clips (A1 + A2) back into one clip in `diff` ([#4](https://github.com/kajisho5/nle-bridge-skill/pull/4); ADR-007)
  - Exact copies on a later audio track fold (FCP7, OTIO, EDL `AA`); a copy the plan has is compared, an edited copy is reported as added
- [x] **12. Media start timecode** — from ffmpeg-skill `probe`'s `timecode` (added in ffmpeg-skill 3.0.0 on this skill's hand-over) into relink.json and the OTIO / FCP7 XML / EDL source timecodes ([#5](https://github.com/kajisho5/nle-bridge-skill/pull/5); ADR-005, ADR-008)
  - Checked first ([#2](https://github.com/kajisho5/nle-bridge-skill/pull/2); ADR-005): calling ffprobe here is not allowed, so it was handed over: [docs/handover-media-start-timecode.md](docs/handover-media-start-timecode.md) (done)
  - Result: the plan's value wins (`START_TIMECODE_MISMATCH` when the probe differs); plans, render projects and agent IRs alike; `version_range` `>=2.0.0 <4.0.0` with the field read when present (2.x: not known, as before). CI's integration job runs against ffmpeg-skill v3.0.0, v2.24.1 and main; `RealProbeTests` uses `ffmpeg -timecode` files (MOV, MP4, `01:00:00;00` at 30000/1001)

## Boundaries

Rendering and FCPXML are ffmpeg-skill's; captions are subtitle-skill's; music and its beat measurement are sound-design-skill's and ffmpeg-skill's; planning is video-production-agent's.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
