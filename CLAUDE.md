# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run

```bash
python3 usage-server.py
# then open http://localhost:8787/dev-dispatch.html
```

No build step. No dependencies beyond the Python standard library.

To point at a different projects root or state directory:

```bash
DISPATCH_ROOT=~/code DISPATCH_STATE_DIR=/tmp/state python3 usage-server.py
```

## Tests

```bash
python3 test_topic_overrides.py
```

This is the only test file. It imports `usage-server.py` directly via `importlib`, overrides `DISPATCH_STATE_DIR` to a temp dir, and exercises the topic/archive/session-flag logic. Run it to validate any changes to `_topic_action`, `_sessions_action`, `_apply_topic_overrides`, `set_archived`, or `_archived_topic_ids`.

## Architecture

### Files

- **`usage-server.py`** (~1456 lines) — the entire backend. A stdlib-only `ThreadingHTTPServer` that also serves the static HTML files.
- **`dev-dispatch.html`** / **`focus.html`** — single-file dashboards with embedded CSS and JS. No bundler; edit directly.
- **`state/`** — all persisted state (gitignored). Never written to by the HTML files — only by the server.

### Data flow

```
~/.claude/projects/<root>/*.jsonl   ← Claude Code session logs (read-only)
        │
        ▼
_scan_session(path)         parses one .jsonl into a session dict
        │
_build_context()            scans all sessions, resolves topics, caches 60s
        │                   ↳ _assign_topics() → 4-tier topic grouping
        │                   ↳ _apply_topic_overrides() → manual renames/merges/moves
        ▼
GET /api/context            main dashboard
GET /api/focus              focus/refocus board (issue cards per session)
GET /api/usage              proxied Claude usage from macOS Keychain, cached 60s
```

### Topic grouping — `_assign_topics()`

Sessions are resolved to a topic via a 4-tier fallback (first match wins):

1. **cwd inside a project dir** — `repo:<name>/<worktree-feature>`
2. **files actually edited** — same `repo:` key, using the worktree path of the edits
3. **paths mentioned in prompts** — `repo:` key from `/MyProjects/...` refs in text
4. **union-find over shared edited files** — `files:<a>|<b>` clusters; everything else → `misc`

Manual overrides (rename/merge/move) are stored in `state/topics.json` and applied on top in `_apply_topic_overrides()`.

### Topic key format

Keys must match `^(repo:[A-Za-z0-9 ._+/|-]*|files:[A-Za-z0-9 ._+|-]*|custom:[A-Za-z0-9]+|misc)$`.

- `repo:<name>/` — project root sessions
- `repo:<name>/<feature>` — worktree/feature sessions
- `files:<file1>|<file2>` — clustered by shared edits
- `custom:<id>` — user-created topics
- `misc` — catch-all

### State files (`state/`)

| File | Contents |
|------|----------|
| `topics.json` | Manual overrides: `{renames, merges, moves}` |
| `session-flags.json` | Per-session `archived` / `deleted` flags |
| `recaps.json` | AI-generated one-sentence recaps, keyed by sessionId |
| `issuecards.json` | Structured issue cards for the focus board |
| `archived.json` | Archived topic fingerprints (used for auto-revive detection) |
| `closed.json` | Sessions closed from the dashboard (greys them out) |
| `iterm-registry.json` | sessionId → iTerm session id (for focus/close) |

### Background worker

`_recap_worker()` runs on a daemon thread, waking every `RECAP_INTERVAL` seconds (default 300). It calls `claude -p` with `RECAP_MODEL` (default `claude-haiku-4-5`) to generate/refresh session recaps. Only sessions whose content changed since the last recap are regenerated — finished sessions are summarized once.

### iTerm2 control

`_open_in_iterm` and `_close_iterm` run AppleScript via `subprocess` (`osascript`). They require macOS Automation permission for iTerm. Only sessions opened by the dashboard are tracked in `iterm-registry.json`; manually opened tabs are not.

### Session log format

Each line of a `.jsonl` file is a JSON record with fields including: `type` (`user`/`assistant`/`summary`), `cwd`, `gitBranch`, `timestamp`, `message`, `summary`, `isMeta`. The `usage` field inside assistant messages provides context-token counts used for the context gauge.

### Soft-delete invariant

Deleting a session via the dashboard sets a flag in `session-flags.json` and hides it from both dashboards. The `.jsonl` log on disk is **never touched**.

### Archive auto-revive

A topic's archived state is stored with a fingerprint of its sessions (total message count). If new messages appear in any session, the fingerprint changes and the topic auto-unarchives on the next `_build_context()` call.

## Key env vars

| Var | Default | Effect |
|-----|---------|--------|
| `DISPATCH_ROOT` | `~/MyProjects` | Root folder whose sessions are surfaced |
| `DISPATCH_STATE_DIR` | `./state/` | Where all state JSON files live |
| `RECAP_INTERVAL` | `300` | Seconds between recap worker runs |
| `RECAP_CONCURRENCY` | `3` | Parallel `claude -p` calls in recap worker |
| `FOCUS_TOPN` | `3` | Topics shown on the focus board |
| `STALE_DAYS` | `7` | Sessions older than this are dropped |
| `CTX_RESERVE` | `20000` | Tokens subtracted from context window for output headroom |
