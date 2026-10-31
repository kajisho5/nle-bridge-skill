# Hand-over: media start timecode (roadmap step 12)

**To:** ffmpeg-skill (owner of FFmpeg execution, `probe`), for whenever it is next opened. **From:** nle-bridge-skill. **Status:** waiting; nothing in ffmpeg-skill is changed by this repository (ffmpeg-skill is treated as complete). Decision and reasons: [decisions.md ADR-005](decisions.md).

## What is missing

A camera file's start timecode (the `timecode` tag ffprobe reports on the format, on the video stream, or on a QuickTime `tmcd` data stream, e.g. `10:00:00:00` or `01:00:00;00`) is what an NLE shows as the clip's source timecode. nle-bridge needs it for:

- OTIO `source_range` / `available_range` and EDL source timecodes, which are absolute (start timecode + in point), and the FCP7 `<file><timecode>`;
- the relink manifest's `start_timecode`, which today says `not known` unless the plan states it;
- reading an EDL back, whose source timecodes only make sense against the media's start.

ffmpeg-skill 2.24 `probe` keeps only `title`, `artist`, `album`, `comment`, `date` and `genre` from the format tags (`scripts/_common/probe.py`), so the timecode never reaches its JSON.

## What would close it

In ffmpeg-skill `probe --json` (a minor, additive change under its 2.x stability promise):

```json
"timecode": {"value": "10:00:00;00", "drop_frame": true, "source": "stream:0" }
```

- `value`: the first of: the video stream's `tags.timecode`, a `tmcd` data stream's `tags.timecode`, the format's `tags.timecode`; `null` when none.
- `drop_frame`: `true` when the label uses `;` (or `.`) before the frames.
- `source`: where it came from (`format`, `stream:<index>`), so a reader can say so.
- A contract entry for the field, and a test with `ffmpeg ... -timecode 01:00:00:00` (MOV and MP4) and with `01:00:00;00` at 30000/1001.

## What nle-bridge does then (one small pull request here)

- `engine.py` / `conform.py`: take `timecode.value` into `relink.json` with `facts_from.start_timecode: "ffmpeg-skill probe"`, and set `drop_frame` from it; the plan's value still wins when both exist and differ (`START_TIMECODE_MISMATCH` warning).
- `plan.py` `_probe_asset`: render projects and agent IRs get `start_timecode` from the probe.
- Raise the ffmpeg-skill dependency's `version_range` floor to the release that adds the field, keeping `<3.0.0`.
- Tests: the existing `RealProbeTests` with a file written by `ffmpeg -timecode`.

## Until then

- The plan states it: `assets.<id>.start_timecode` (and `drop_frame`), which every writer and the relink manifest already use.
- video-production-agent can fill that field from **ingest-skill**'s clip inventory (`ingest inventory --json`), which already records each clip's `timecode` when ffprobe is installed. ingest-skill owns the inventory (素材台帳), so reading its output is reuse, not a second measurement. That is the agent's side (roadmap step 7), not a change in this repository.
- Without either, the media is taken to start at `00:00:00:00` and the manifest says the start timecode is not known.
