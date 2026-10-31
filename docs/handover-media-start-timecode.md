# Hand-over: media start timecode (roadmap step 12)

**To:** ffmpeg-skill (owner of FFmpeg execution, `probe`), for whenever it is next opened. **From:** nle-bridge-skill. **Status:** done (完了). ffmpeg-skill 3.0.0 (PR kajisho5/ffmpeg-skill#387, `1dc2f52`) added `probe`'s `timecode: {value, drop_frame, source}` as asked below, with its tests; nle-bridge reads it since [#5](https://github.com/kajisho5/nle-bridge-skill/pull/5). Nothing in ffmpeg-skill was changed by this repository. Decisions and reasons: [decisions.md ADR-005](decisions.md) (the hand-over) and ADR-008 (how it is read).

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

## What nle-bridge does then (one small pull request here) — done in [#5](https://github.com/kajisho5/nle-bridge-skill/pull/5)

- `engine.py` / `conform.py`: take `timecode.value` into `relink.json` with `facts_from.start_timecode: "ffmpeg-skill probe"`, and set `drop_frame` from it; the plan's value still wins when both exist and differ (`START_TIMECODE_MISMATCH` warning).
- `plan.py` `_probe_asset`: render projects and agent IRs get `start_timecode` from the probe.
- Raise the ffmpeg-skill dependency's `version_range` floor to the release that adds the field, keeping `<3.0.0`.
  - As done: the field came in a major release (3.0.0, which also dropped Python 3.9), so the range is `>=2.0.0 <4.0.0` and the field is read when the probe has it; with 2.x the start timecode is not known, as before (ADR-008).
- Tests: the existing `RealProbeTests` with a file written by `ffmpeg -timecode`.

## Until then (still how it works without ffmpeg-skill 3.x)

- The plan states it: `assets.<id>.start_timecode` (and `drop_frame`), which every writer and the relink manifest already use.
- video-production-agent can fill that field from **ingest-skill**'s clip inventory (`ingest inventory --json`), which already records each clip's `timecode` when ffprobe is installed. ingest-skill owns the inventory (素材台帳), so reading its output is reuse, not a second measurement. That is the agent's side (roadmap step 7), not a change in this repository.
- Without either, the media is taken to start at `00:00:00:00` and the manifest says the start timecode is not known.
