# Changelog

## Unreleased

- Live control by reuse (roadmap step 9, ADR-004): `docs/live-control.md` pins davinci-resolve-mcp 4.17.0 and premiere-pro-mcp 1.27.0 (both MIT), with their install commands, a chat-app config and the switches that keep script tools, telemetry and self-update off. No server code is used or copied.
- Media start timecode (roadmap step 12, ADR-005): not read with ffprobe here; a hand-over to ffmpeg-skill `probe` (`docs/handover-media-start-timecode.md`). The step stays open.

## 0.1.0 - 2026-10-31

First release (roadmap steps 1-6).

- `nle-bridge export`: a plan (`nle-bridge/timeline@1`, video-production-agent's project IR, an ffmpeg-skill render project, or an OTIO / FCP7 XML / EDL) as OpenTimelineIO, FCP7 XML (xmeml 4, for Premiere) and CMX 3600 EDL, plus an SRT sidecar for captions. Whole frames at exact rates (23.976 = 24000/1001, 29.97 and 59.94 drop-frame or not); seconds rounded to the nearest frame, ties to even, like ffmpeg-skill. Effects, speed changes, titles, transitions and every format-specific loss are warnings naming the item; `--strict` exits 5 before writing.
- Beat grids (`--beats`): ffmpeg-skill `scenes --beats` output, a `beat_grid`, or `nle-bridge/beatgrid@1`; bar heads RED `Bar N`, other beats CYAN `Beat N.b`; bars ASSUMED every N beats when the grid states none.
- `nle-bridge conform`: timelines, the plan as JSON, `relink.json` (path, exists, size, sha256, start timecode, length, fps, picture size, ranges used, and where each fact came from: ffmpeg-skill `probe` when found) and `manifest.json`; deterministic zip; `--copy-media`.
- `nle-bridge diff`: the editor's returned OTIO / FCP7 XML / EDL against the plan: clips changed / added (`split_from` a razor cut) / removed, cut points moved in frames, markers changed, and with a beat grid each cut's offset from the nearest beat; `--timeline-out` writes the returned cut as the next plan.
- `nle-bridge import`, `contract` (Velisa registry shape, validates against `capability_contract.schema.json`), `doctor`.
- Tested: timecode anchors and every-frame DF round trips; every format read back identical at six rates; opentimelineio and its FCP7 / CMX 3600 adapters read the same cut; ffmpeg-skill's own `render --export-timeline` files read back cut for cut. Not tested: import into a real Premiere or Resolve.
