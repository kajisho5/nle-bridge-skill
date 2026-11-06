#!/usr/bin/env bash
# Regenerates the auto-editor fixtures of roadmap step 13 (docs/decisions.md ADR-009).
#
#   AUTO_EDITOR=/path/to/auto-editor-29.3.1 tests/fixtures/auto-editor/make_fixtures.sh [out_dir]
#
# auto-editor (Unlicense, https://github.com/WyattBlue/auto-editor) is pinned to release 29.3.1: CI downloads that
# release's linux-x86_64 binary and checks its sha256 (.github/workflows/tests.yml). nle-bridge never runs it: the
# files it writes are committed here and read by the tests. The test clip is made with ffmpeg (a 10 s test picture,
# a 440 Hz tone with two silences, PCM sound), at 29.97 fps (30000/1001, non-drop) and at 25 fps. The media itself
# is not committed; auto-editor writes its absolute path, so the clip is made in a fixed folder.
set -euo pipefail
AUTO_EDITOR="${AUTO_EDITOR:-auto-editor}"
OUT="$(cd "${1:-$(dirname "$0")}" && pwd)"
WORK=/tmp/nle-bridge-auto-editor
rm -rf "$WORK" && mkdir -p "$WORK"
cd "$WORK"
"$AUTO_EDITOR" --version
# tone 0-2 s, silence 2-3.5 s, tone 3.5-6 s, silence 6-8 s, tone 8-10 s
TONE='0.5*sin(2*PI*440*t)*(lt(t,2)+between(t,3.5,6)+gte(t,8))'
for spec in "2997 30000/1001" "25 25"; do
  set -- $spec
  name="talk_$1"
  ffmpeg -hide_banner -loglevel error -y -f lavfi -i "testsrc2=size=320x180:rate=$2:duration=10" \
    -f lavfi -i "aevalsrc=${TONE}|${TONE}:s=48000:d=10" -map 0:v -map 1:a -c:v mpeg4 -q:v 8 -c:a pcm_s16le \
    -shortest "$name.mov"
  # the FCP7 XML auto-editor writes for Premiere, and its own cut list (v1: [start, end, speed] in frames)
  "$AUTO_EDITOR" "$name.mov" --export premiere -o "$OUT/$name.xml" --progress none
  "$AUTO_EDITOR" "$name.mov" --export v1 -o "$OUT/$name.v1.json" --progress none
done
rm -rf "$WORK"
