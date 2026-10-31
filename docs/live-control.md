# Live control by reuse: Resolve and Premiere MCP servers beside this skill

This skill writes and reads timeline files. Driving a running NLE (importing those files, adding markers, queueing a render) is left to community MCP servers that run next to it in the same chat app (Velisa ROADMAP, "Premiere Pro and DaVinci Resolve"). They are **reused, never forked, and none of their code is copied here**. This page says which servers, at which pinned version, and with which switches. Why these and not others: [decisions.md ADR-004](decisions.md).

Checked on 2026-10-31 against the servers' own repositories at the pinned tags and their npm registry entries (primary sources; nothing here comes from a directory site). Re-check before changing a pin.

## The two servers

| | DaVinci Resolve | Premiere Pro |
|---|---|---|
| Server | [samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp) | [leancoderkavy/premiere-pro-mcp](https://github.com/leancoderkavy/premiere-pro-mcp) |
| Licence | MIT (`LICENSE`, `package.json`) | MIT (`LICENSE`, `package.json`) |
| Pin | **4.17.0**: tag `v4.17.0` = commit `6dea2f3dd2524a81dff18d8eafc8711f90108964`, npm `davinci-resolve-mcp@4.17.0` (published 2026-10-29) | **1.27.0**: tag `v1.27.0` = commit `c2370d77215fa8acf444a0a95e4b62a5f17347cb`, npm `premiere-pro-mcp@1.27.0` (published 2026-10-31) |
| Maintenance | Active: releases every few days (4.15.3 to 4.17.0 in October 2026), a test suite, a security policy | Active: releases every few days (1.24.3 to 1.27.0 in October 2026), a test suite, a security policy |
| NLE requirement | **Resolve Studio** (external scripting is Studio-only); Preferences > General > External scripting using: **Local** | Premiere Pro with the server's CEP panel (`--install-cep`); the UXP bridge needs Premiere 25.6+ |
| Arbitrary-code tools | **None.** Since v3.0.0 the server does not execute caller-supplied code: `script_plugin run_inline` / `execute` were removed, the probe refuses `execute=true`, and a test AST-scans `src/` for `exec` / `eval` / `RunScript` (CHANGELOG "v3.0.0") | `execute_extendscript` and `evaluate_expression` exist, but are hidden from discovery and refused at call time unless `PREMIERE_MCP_CAPABILITIES` contains `unsafe-script` (default `inspect,edit,export,filesystem`; README "Security", SECURITY.md) |
| Telemetry / network | No telemetry found in `src/`. On start it checks GitHub for a newer release (`src/utils/update_check.py`); `DAVINCI_RESOLVE_MCP_UPDATE_CHECK=0` turns that off | PostHog telemetry exists (`src/telemetry.ts`) but is **off unless `POSTHOG_API_KEY` is set** |
| Importing this skill's files | `media_pool` action `import_timeline(path, options?)`, or `timeline` action `import_timeline_checked(path, ..., dry_run?)`: both call Resolve's `ImportTimelineFromFile`, which takes **OTIO, FCP7 XML, EDL** (and AAF / FCPXML) | `import_fcp_xml` (CEP `app.openFCPXML`, into a new project or the open one) takes the **FCP7 XML**. 1.27.0 has no OTIO import tool (its `src/` exports OTIO through `export_interchange_uxp` only), and `import_edl` refuses by design. Import the `.otio` by hand (File > Import) when OTIO is wanted |

Not used: servers whose licence is missing or unclear, or whose main tool runs arbitrary code. Checked the same day:

| Server | Licence at HEAD | Last commit | Why not |
|---|---|---|---|
| Tooflex/davinci-resolve-mcp | **no licence file** (HEAD `e8b3d92`) | 2026-07-26 | No licence (nothing may be reused); has `execute_lua` |
| apvlv/davinci-resolve-mcp | `LICENSE` says MIT (HEAD `33678a6`); a directory site lists GPL-3.0 | 2026-10-22 | `execute_python` and `execute_lua` tools; licence statements disagree |
| nepfaff/premiere-pro-mcp | **no licence file** (HEAD `63215d3`) | 2026-02-09 | No licence; its one tool `eval()`s arbitrary UXP JavaScript (`allowCodeGenerationFromStrings`) |
| tro2789/scout-premiere-mcp | proprietary | 2026-09-01 | Not open source; `execute_extendscript` |

Velisa's roadmap notes that one Resolve server has no licence and another is GPL-3.0. Whatever the server, the rule here is the same: **do not copy code from any of them**, including the two used. This repository only names them and their switches.

## Setup

1. Install this skill as usual and export the timelines (`nle-bridge export plan.json -o out/ --json`).
2. Install the servers at the pinned versions, through their own installers, never from a fork or a copy:
   - Resolve: `npx -y davinci-resolve-mcp@4.17.0 setup` (it copies the package into a managed folder and writes the client config; `--clients manual` only prints it). Use the **compound** server (the default), not the 390-tool "full" one.
   - Premiere: `npx -y premiere-pro-mcp@1.27.0 --install-cep` (the CEP panel), then the server below.
3. Configure the chat app. A Claude Desktop config with this skill's agent and both servers (paths are placeholders):

```json
{"mcpServers": {
  "video-agent": {"command": "/abs/path/video-agent-mcp", "args": ["--workspace", "/abs/path/video-agent-work"]},
  "davinci-resolve": {
    "command": "npx", "args": ["-y", "davinci-resolve-mcp@4.17.0", "server"],
    "env": {"DAVINCI_RESOLVE_MCP_UPDATE_CHECK": "0", "DAVINCI_RESOLVE_MCP_UPDATE_MODE": "never"}
  },
  "premiere-pro": {
    "command": "npx", "args": ["-y", "premiere-pro-mcp@1.27.0"],
    "env": {"PREMIERE_MCP_CAPABILITIES": "inspect,edit,export,filesystem", "PREMIERE_CONTEXT_BACKEND": "memory"}
  }
}}
```

4. Keep the switches:
   - **Pinned versions.** Always `@<version>` after the package name; never `@latest`, never the servers' self-update (`DAVINCI_RESOLVE_MCP_UPDATE_MODE=never`; for Premiere do not run `--update`). Move a pin only after re-checking this page's rows for the new tag.
   - **Eval / script tools off.** Premiere: `PREMIERE_MCP_CAPABILITIES` lists exactly `inspect,edit,export,filesystem` and never `unsafe-script`. Resolve has no such tool to turn off; do not install scripts, Fuses or DCTLs through it (`script_plugin install`, `fuse_plugin`, `dctl`) for this workflow, and leave Resolve's own script menu alone.
   - **Telemetry off.** Premiere: no `POSTHOG_API_KEY` in its environment (or in the shell it inherits). Resolve: no telemetry; the update check is off by the env above.
   - **Local transport only.** Both run over stdio. Do not start Resolve's `--transport sse|streamable-http` or Premiere's `http-server`; those listen on a port.
   - **Destructive actions.** Let the chat app ask before each one. For Resolve, its safe mode refuses high-risk actions: `{"destructive": {"safe_mode": true}}` in the server's `logs/media-analysis-preferences.json` (or through its setup defaults).
   - **No project memory unless wanted.** `PREMIERE_CONTEXT_BACKEND=memory` keeps Premiere's server from writing project names and notes to disk.

## The workflow

1. `nle-bridge export` (or `conform` for another machine) writes `.otio`, `.xml`, `.edl` and `.srt`.
2. Resolve: ask Claude to import `out/<name>.otio` with `timeline` > `import_timeline_checked` (try `dry_run` first). Premiere: `import_fcp_xml` with `out/<name>.xml`, or import the `.otio` by hand. The SRT is imported as a caption track by hand in either NLE.
3. The editor works in the NLE. When done, export OTIO (Resolve: `timeline` > `export_timeline_checked`, or File > Export; Premiere: `export_interchange_uxp` for OTIO, or `export_as_fcp_xml`).
4. `nle-bridge diff plan.json returned.otio --timeline-out plan_v2.json --json` says what changed.

What each NLE keeps from these files on import (marker colours from the EDL, FCP7 levels, OTIO metadata, start timecodes) is not yet recorded: that is roadmap step 8, done with the real software.
