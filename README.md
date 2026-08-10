# LLM HUD

LLM HUD adds a compact usage display to each supported coding agent's own
terminal interface. It does not create an iTerm2 status bar, macOS menu bar
item, shell prompt, or background process.

## Supported providers

- **Claude Code**: a graphical two-line HUD with model, project, 5-hour usage,
  7-day usage, and reset times.
- **Codex CLI**: a curated native footer with model, project, weekly usage, and
  remaining context.
- **Kimi CLI**: detects and preserves Kimi's built-in toolbar. Model, project,
  and context stay visible there; quota remains available through `/usage`.

Each integration is owned by its provider. Claude sessions only display Claude
data, Codex sessions only display Codex data, and Kimi keeps using its own
toolbar. No process detector is needed.

## Preview

Claude Code renders a full HUD because its `statusLine` API accepts an external
command:

```text
Claude · Opus · ~/projects/example
5h  ████████░░  76%  ↻ 14:30    7d  ██████░░░░  59%  ↻ Fri 09:00
```

Before Claude returns the first response, rate-limit fields are not available:

```text
Claude · Opus · ~/projects/example
5h  ░░░░░░░░░░  --    7d  ░░░░░░░░░░  --   waiting for first response
```

Codex owns its renderer, so LLM HUD selects and orders native fields instead of
drawing a custom progress bar:

```text
gpt-5.6 xhigh · ~/projects/example · weekly 63% left · Context 98% left
```

## Install

From a checkout:

```bash
./install.sh
```

The installer copies the runtime to `~/.local/share/llm-hud`, creates
`~/.local/bin/llm-hud`, detects installed coding-agent CLIs, and configures only
the detected providers. Restoration metadata is stored under
`~/.config/llm-hud`.

The source installer requires Python 3.11 or newer. A release installer can
bundle the runtime after the project is published.

## Commands

```bash
llm-hud providers
llm-hud install
llm-hud install --provider claude
llm-hud install --provider codex
llm-hud doctor
llm-hud uninstall
```

Installation is idempotent. Uninstall restores the previous provider settings
and refuses to overwrite a status configuration that the user changed later.

## Provider behavior

### Claude Code

Claude passes status-line JSON to `llm-hud render claude`. LLM HUD reads the
model, workspace, and current `rate_limits` windows from that payload. If a
custom status line already exists, its output is retained above the HUD and its
configuration is restored on uninstall.

### Codex CLI

Codex does not accept an external status-line renderer. LLM HUD configures these
native `[tui].status_line` fields:

```toml
[tui]
status_line = [
  "model-with-reasoning",
  "current-dir",
  "weekly-limit",
  "context-remaining",
]
```

Codex controls rendering and refresh timing. Different active sessions can
temporarily show different cached values until each session refreshes.

### Kimi CLI

Kimi already renders model, workspace, Git state, background activity, and
context usage in its bottom toolbar. It does not currently expose a supported
external toolbar renderer, so LLM HUD does not patch Kimi's installation or
read its credentials. Use `/usage` inside Kimi for quota progress and reset
information.

`llm-hud install` reports this provider as `builtin`; uninstall has nothing to
restore because no Kimi settings are changed.

## Integration levels

```text
Provider  Integration  Persistent metrics             On demand
Claude    command      model, cwd, quota               -
Codex     native       model, cwd, weekly, context     -
Kimi      builtin      model, cwd, context             quota (/usage)
```

## Project structure

```text
src/llm_hud/
├── cli.py            command-line interface
├── hud.py            provider-neutral HUD model and renderer
├── paths.py          config and state locations
├── storage.py        atomic file and JSON operations
├── toml_edit.py      conservative Codex TOML editing
└── providers/
    ├── base.py       provider contract
    ├── claude.py     Claude adapter and installer
    ├── codex.py      Codex native-footer adapter
    └── kimi.py       Kimi built-in-toolbar adapter
```

A future provider such as Kimi implements the same `Provider` contract and maps
its available data into the shared HUD model when custom rendering is supported.

## Development

```bash
python3 -m unittest discover -s tests -t . -v
python3 bin/llm-hud providers
```

Tests use temporary HOME, settings, and state paths. They do not modify real
Claude or Codex configuration.
