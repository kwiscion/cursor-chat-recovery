# cursor-chat-recovery

[![CI](https://github.com/kwiscion/cursor-chat-recovery/actions/workflows/ci.yml/badge.svg)](https://github.com/kwiscion/cursor-chat-recovery/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cursor-chat-recovery.svg)](https://pypi.org/project/cursor-chat-recovery/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Did your Cursor chats disappear after you moved, renamed, or re-opened a project? They're not gone. This is an interactive terminal UI to **recover, browse, move, and export Cursor AI chat history** — across all your projects.

![Demo: recovering orphaned chats](demo.gif)

## Why chats "disappear"

Cursor identifies each workspace by a hash of its identifier URI. Reopening a project from a different path, or saving an Untitled multi-folder workspace as a `.code-workspace` file, mints a new workspace identity and detaches all prior chats from the sidebar. The conversations are still in `globalStorage/state.vscdb` keyed by `composerId` — they just need their `workspaceIdentifier` rewritten to point at the new workspace. That's what this tool does, with backups at every step.

Newer Cursor builds (behind the server-distributed `composer_header_typed_table` feature gate) store chat headers in a typed `composerHeaders` SQLite table instead of the legacy `composer.composerHeaders` blob, and stop reading the blob entirely. The tool detects which store is authoritative on your install (via the `composer.composerHeaders.tableGateEnabled` key) and reads/writes the right one — on gated installs it updates both the table's `workspaceId` column and the header JSON together, and clears stale agent-project membership entries so Cursor's launch reconciliation doesn't regroup moved chats under the old workspace.

It's also useful when nothing is broken: browse every chat you've ever had in any project, move chats between workspaces, and export conversations to Markdown.

## Install

Requires Python 3.10+. With [uv](https://docs.astral.sh/uv/):

    uv tool install cursor-chat-recovery

or with pipx:

    pipx install cursor-chat-recovery

Both install the `cursor-chat-recovery` executable (and a short alias, `ccr`).
After `uv tool install`, ensure `~/.local/bin` is on your PATH (run `uv tool update-shell` once if needed).

To install the latest development version instead:
`uv tool install git+https://github.com/kwiscion/cursor-chat-recovery`.
To run from a clone without installing: `uv run cursor-chat-recovery`.

## Use

    cursor-chat-recovery                # interactive TUI  (or: ccr)
    cursor-chat-recovery --readonly     # TUI with mutations disabled
    cursor-chat-recovery --list         # workspaces table to stdout
    cursor-chat-recovery --list --json
    cursor-chat-recovery --export <COMPOSER_ID> --format markdown -o chat.md
    cursor-chat-recovery --reassign <COMPOSER_IDS,COMMA,SEP> <TARGET_WS_ID> --yes
    cursor-chat-recovery --merge <SRC_WS_ID> <TARGET_WS_ID> --yes

Close Cursor before any mutation. The tool refuses to write while Cursor is running.

## Keys

### Global

| Key | Action |
|-----|--------|
| `?` | Open help screen |
| `q` | Quit |
| `esc` | Back / cancel (clears an active filter first) |
| `ctrl-c` | Force quit |

### Workspaces (project list)

| Key | Action |
|-----|--------|
| `up` / `down` | Move selection |
| `pgup` / `pgdn` | Page up / down |
| `home` / `end` | Jump to first / last |
| `enter` | Open workspace's chats |
| `s` | Cycle sort order (last activity → chat count → name → health) |
| `/` | Filter by name or path (`enter` keeps filter, `esc` clears) |

### Chats (within a workspace)

| Key | Action |
|-----|--------|
| `up` / `down` | Move selection |
| `pgup` / `pgdn` | Page up / down |
| `enter` | View messages |
| `space` | Select / deselect chat for bulk actions |
| `a` | Select all chats |
| `A` | Deselect all chats |
| `m` | Move selected (or highlighted) chats to another workspace (`r` is a legacy alias) |
| `e` | Export selected (or highlighted) chats to Markdown files |

### Messages (within a chat)

| Key | Action |
|-----|--------|
| `up` / `down`, `pgup` / `pgdn`, `home` / `end` | Scroll |
| `e` | Export this chat |

### Moving chats

Press `m` on the chats screen to open the move-target picker. Workspaces are
identified by name, path, chat count, and last activity so ambiguous short
names are distinguishable. Type `/` to filter the list; the current workspace
is marked and cannot be selected as a target. After you confirm, a timestamped
backup is written to `~/.cursor-chat-recovery/backups/` before any data is
touched. Cursor must be closed before the move is attempted; the tool refuses
to write while Cursor is running.

## Safety

- Per-session full DB backup written next to `state.vscdb` on first mutation.
- Per-op headers backup under `~/.cursor-chat-recovery/backups/`.
- Read-only mode auto-engages on schema mismatch or detected Cursor-running.
- Reassign is reversible: the per-op backup restores the prior `composer.composerHeaders`.

## Schema drift

When Cursor changes its schema, the tool detects the mismatch on startup and shows a copy-pastable prompt you can paste into a coding agent to adapt the tool. The agent prompt names the affected files and what to change.

## Development

    uv sync --dev
    uv run pytest
    uv run ruff check
    uv run mypy src/

## License

[MIT](LICENSE)
