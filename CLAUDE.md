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

- **`usage-server.py`** — the entire backend. A stdlib-only `ThreadingHTTPServer` that also serves the static HTML files.
- **`dev-dispatch.html`** / **`focus.html`** / **`tracker.html`** — single-file dashboards with embedded CSS and JS. No bundler; edit directly.
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
GET /api/tracker             feature tracker board (see below)
```

### Feature tracker — `tracker.html`

One card per ClickUp task assigned to the user (status In Progress / In Review / In Testing /
Waiting for Deployment / Complete on the Backend Backlog list), showing its repos/PRs, live merge
status, and manual deployed/e2e-tested toggles.

```
ClickUp REST API (assignee=me, list=Backend Backlog)
        │  direct HTTPS calls via CLICKUP_API_TOKEN (see _clickup_api) — no MCP/claude -p
        ▼
_clickup_worker()            every CLICKUP_INTERVAL(s), writes state/clickup-tasks.json
        │
_build_tracker()             joins clickup-tasks.json + state/feature-tracker.json (manual
        │                    repo/PR entries + toggles), attaches live `gh pr view` merge status
        ▼
GET /api/tracker             POST /api/tracker/repo (add/edit/remove)
                              POST /api/tracker/toggle (deployed/e2eTested)
                              POST /api/tracker/refresh (force re-pull)
```

Repo/branch/PR links are mostly manual (checked against the `_work/` folder naming convention;
too inconsistent to parse reliably). One partial exception: `_clickup_refresh()` scans each
task's ClickUp description for GitHub PR links (`_extract_pr_urls`) and auto-adds any found to
`feature-tracker.json`, tagged `"source": "auto"` (shown with an "auto" pill on the card) and
deduped against what's already there — still editable/removable like a manual entry. Deployed/
e2e-tested have no automated signal, so they're click-to-toggle pills.

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
| `clickup-tasks.json` | Raw snapshot of assigned ClickUp tasks, refreshed by `_clickup_worker()` |
| `feature-tracker.json` | Manual repo/branch/PR entries + deployed/e2eTested toggles, keyed by ClickUp task ID |

### Background worker

`_recap_worker()` runs on a daemon thread, waking every `RECAP_INTERVAL` seconds (default 300). It calls `claude -p` with `RECAP_MODEL` (default `claude-haiku-4-5`) to generate/refresh session recaps. Only sessions whose content changed since the last recap are regenerated — finished sessions are summarized once.

`_clickup_worker()` runs on a daemon thread, waking every `CLICKUP_INTERVAL` seconds (default 600). Each cycle makes **N+1 direct ClickUp REST API calls** (via `_clickup_api`, using `CLICKUP_API_TOKEN`) — no `claude -p` / MCP round-trip, since this step is plain data-fetching + regex matching, not something an LLM needs to be in the loop for:

1. `_gen_task_list()` — `GET /user` then `GET /list/{CLICKUP_LIST_ID}/task` (assignees=me, include_closed=false) — fetches tasks assigned to the user on the Backend Backlog list (id/name/url/status/listName/dateUpdated only, no text), then filters to `TRACKED_STATUSES` (near-miss statuses surfaced in `unmatchedStatuses` instead of silently dropped).
2. `_gen_task_text(task_id)` — `GET /task/{id}` + `GET /task/{id}/comment` — one call pair **per tracked task** (the API only offers per-task endpoints anyway), fetched concurrently via a small thread pool since these are independent HTTP calls with no shared state to corrupt.

Each helper retries once on failure (network blip, timeout, non-2xx) before giving up for that cycle. This replaced an earlier design that shelled out to `claude -p` with the ClickUp MCP tools pre-approved, isolating each task into its own call specifically to avoid the LLM cross-attributing a PR mention from one task onto a different task's entry (see BUGS.md) — since there's no LLM in this path anymore, that failure mode can't happen at all, and a full refresh cycle dropped from 3-13 minutes to a few seconds.

It also **reconciles** each tracked task's auto-detected repos in `feature-tracker.json` against that current description+comments text (`_merge_detected_prs`): any `source: "auto"` entry whose PR link no longer appears anywhere in the text is dropped (e.g. the ClickUp description was edited/cleaned up), and any new PR link found gets added (deduped). Manually-added entries (any other `source`) are never touched by this — reconciliation only applies to what the worker itself derived from ClickUp text. `_TRACKER_LOCK` (a `threading.Lock`) guards every load-modify-save of `feature-tracker.json` — this worker and the `/api/tracker/repo` / `/api/tracker/toggle` HTTP handlers all take it, so a manual edit and the periodic auto-merge can't race and clobber each other.

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
| `CLICKUP_API_TOKEN` | unset | Personal ClickUp API token — required for the feature tracker (`_clickup_api`) to fetch tasks/comments directly; without it, ClickUp refreshes fail |
| `RECAP_INTERVAL` | `300` | Seconds between recap worker runs |
| `RECAP_CONCURRENCY` | `3` | Parallel `claude -p` calls in recap worker |
| `FOCUS_TOPN` | `3` | Topics shown on the focus board |
| `STALE_DAYS` | `7` | Sessions older than this are dropped |
| `CTX_RESERVE` | `20000` | Tokens subtracted from context window for output headroom |
| `CLICKUP_INTERVAL` | `600` | Seconds between feature-tracker ClickUp refreshes |
| `DISABLE_WORKERS` | unset | Skip both background workers entirely — run the server just to serve the dashboard and answer on-demand refresh clicks (`tracker.html`'s `↻ refresh` → `/api/tracker/refresh`, `focus.html`'s → `/api/refresh`), without the 5-min auto-polling `claude -p` recap calls or the 10-min auto-polling ClickUp API calls |
