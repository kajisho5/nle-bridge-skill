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

Reads video-production-agent's project IR (`delivery.timelines[]`, captions, the `timeline.export` decision's `not_exported`) and ffmpeg-skill render projects; reads ffmpeg-skill `scenes --beats` grids; uses ffmpeg-skill `probe` (optional, `>=2.0.0 <4.0.0`) for media facts, and from 3.0.0 for each file's start timecode. Its OTIO and EDL follow the shape ffmpeg-skill's `render --export-timeline` writes, and the integration test reads those exports back cut for cut. The agent hands it the executed IR for `export` / `conform` (video-production-agent#101, its ADR-089); reading `diff` back is still open (step 7, video-production-agent step 12).

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
- [x] **7. (agent side) adapter** — video-production-agent hands `delivery.timeline` to `nle-bridge export` / `conform` and reads `diff` + `--timeline-out` as a new plan version (belongs to video-production-agent). First half in [video-production-agent#101](https://github.com/kajisho5/video-production-agent/pull/101) (its ADR-089: `delivery.nle=export|conform` hands the executed IR to `nle-bridge export` / `conform`); second half in [video-production-agent#117](https://github.com/kajisho5/video-production-agent/pull/117) (its ADR-098, step 12: `revise --editor-timeline` runs `diff --timeline-out` against the IR, and each source whose cut the editor changed becomes a CONFIRM `timeline.editor_cut` the next render cuts exactly; a real FCP7 XML with a trim and a removed clip is read back in its integration test)
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
- [x] **13. Timelines written by auto-editor** ([#10](https://github.com/kajisho5/nle-bridge-skill/pull/10); ADR-009) — `import` and `diff` read the FCP7 XML that auto-editor (Unlicense) exports for Premiere. Its FCPXML (ffmpeg-skill's format here) and its own timeline JSON are out of scope. The fixtures are generated by a pinned auto-editor release on a test clip and committed with the command that made them; no auto-editor code is used or required at run time. Every element it writes that this skill does not keep is a named warning, as for the other formats
  - Done when: an auto-editor FCP7 XML of a clip with silences removed imports with the same cut list auto-editor reports (frame for frame at 29.97 NDF and 25), and `diff` against a plan built from those cuts reports no change
  - Done (ADR-009): auto-editor 29.3.1's `--export premiere` of a clip with two silences, at 29.97 NDF and 25 (`tests/fixtures/auto-editor/`, made by `make_fixtures.sh`; CI regenerates them with the sha256-pinned release binary and compares the bytes), imports with exactly the cut list of its own v1 export on V1, A1 and A2, and `diff` against a plan built from those cuts is `identical` (the per-channel copies folded). Labels, links, the audio format and the per-channel tracks are named (`*_NOT_IMPORTED`)
- [x] **14. Graphics layers on upper tracks** ([#11](https://github.com/kajisho5/nle-bridge-skill/pull/11); ADR-010) — the layer manifests from motion-graphics-skill, hyperframes-skill and remotion-ffmpeg-skill (alpha files with times and positions) placed on upper video tracks in OTIO and FCP7 XML, with their sources in the conform package. Field review 2026-11-05 (Velisa ROADMAP, third round)
  - なぜ要るか：テロップや CG を NLE で後から直せる形で渡せる
  - Done when: a fixture with three graphic layers exports to three upper tracks and reads back with the same times in `diff`
  - Done (ADR-010): a plan's `layers` (`nle-bridge/layers@1`; no new flag) puts each layer on its own video track above the picture in OTIO and FCP7 XML; the fixture's three layers (`tests/fixtures/layers/`, one manifest per producer) export to V2-V4 and read back with the same times in `diff` (identical, each layer `unchanged`; also read by opentimelineio and its FCP7 adapter); the layer files are in `relink.json` / `manifest.json` and copied with `--copy-media`. The producers' manifests are not built yet (motion-graphics 8, hyperframes 13, remotion 11): the shape is handed over in [docs/handover-layer-manifest.md](docs/handover-layer-manifest.md). A file smaller than the frame is centred by the NLE (`LAYER_POSITION_NOT_EXPORTED`)
- [ ] **15. Multicam sources for the editor** — the ffmpeg-skill multicam sync result (offsets per angle) and the switch list exported as a multicam clip the NLE can re-switch (FCP7 XML multiclip; OTIO as a stack with sync offsets), the agent's switches as the initial cuts
  - なぜ要るか：自動切り替えの結果を、編集者が NLE のマルチカム編集で手直しできる
  - Done when: a two-angle fixture opens as one multiclip whose angles line up at the sync offsets (checked by reading it back)
- [ ] **16. Stems and loudness versions in the package** — the conform package carries audio-production-skill's stems on their own tracks and lists the loudness versions with their measurements
  - なぜ要るか：MA に回す素材を、タイムラインと一緒に一式で渡せる
  - Done when: a fixture package lists three stems on three tracks of the timeline

## Boundaries

Rendering and FCPXML are ffmpeg-skill's; captions are subtitle-skill's; music and its beat measurement are sound-design-skill's and ffmpeg-skill's; planning is video-production-agent's.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (3.x, 42 tools, contract_version 2.0; treated as complete, proposals go to Velisa's ROADMAP). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
