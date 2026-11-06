# Hand-over: graphics layer manifests (roadmap step 14)

**To:** motion-graphics-skill (its step 8, layer export), hyperframes-skill (its step 13, alpha deliveries) and remotion-ffmpeg-skill (its step 11, `--layers`), for whenever each is opened. **From:** nle-bridge-skill. **Status:** open on their side; nle-bridge reads the manifest below since [#11](https://github.com/kajisho5/nle-bridge-skill/pull/11). Nothing in those repositories was changed by this one. Decisions and reasons: [decisions.md ADR-010](decisions.md).

## What nle-bridge reads

A plan's `layers` lists manifest paths (relative to the plan), manifests, or single layers. A manifest:

```json
{"schema": "nle-bridge/layers@1", "producer": "motion-graphics-skill",
 "layers": [{"id": "lt1", "type": "lower_third", "file": "media/lt1.mov", "start": 1.0, "end": 6.0,
             "box": {"x": 96, "y": 820, "width": 900, "height": 160},
             "width": 1920, "height": 1080, "fps": 25, "duration": 5.0, "alpha": "straight"}]}
```

| Field | Required | Meaning |
|---|---|---|
| `id` (or `element_id`) | yes | unique in the plan; the timeline item is `layer-<id>` |
| `file` (or `path`) | yes | the alpha file (ProRes 4444, PNG-in-MOV, ...), relative to the manifest |
| `start`, `end` | yes (or `start_frame`, `end_frame`) | where it plays on the programme, seconds (rounded to the nearest frame, ties to even) |
| `source_in` | no (0) | seconds into the file where it starts |
| `type`, `name` | no | what it is (`lower_third`, `title_card`, `captions`, ...) |
| `box` `{x, y, width, height}` (`w` / `h`) or `position` `{x, y}` | no | where the graphic sits in the frame, pixels from the top left |
| `width`, `height`, `fps`, `duration` | no | the file's own; `duration` is checked against `source_in` + length |
| `alpha` | no | `straight` or `premultiplied`, recorded only |

## What would make it smooth

- One file per element, **the size of the programme frame**, transparent around the graphic, with the element's timing (motion-graphics step 8 says "each element rendered alone on transparent with the same timing"). Then no placement is needed in the NLE: neither OTIO nor FCP7 XML here carries a position, so a smaller file is centred by the NLE and nle-bridge says so (`LAYER_POSITION_NOT_EXPORTED`).
- `box` still helps: it travels in OTIO metadata and `relink.json`, so an editor or After Effects can find the graphic.
- The manifest beside the files, with paths relative to it, so a conform package can copy both.
