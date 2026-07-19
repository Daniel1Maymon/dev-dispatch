# The Dev Dispatch

A personal morning dashboard for working across many Claude Code sessions. It shows
where you left off, your **live** session + weekly usage, and groups your sessions
into topics you can resume or close straight from the browser (in iTerm).

> Built for macOS + iTerm2 + Claude Code. The live-usage and resume-in-terminal
> features rely on those specifically.

## Run

```bash
cd ~/dev-dispatch            # wherever you cloned it
python3 usage-server.py
```

Then open **http://localhost:8787/dev-dispatch.html**
(must be `http://localhost`, not `file://` — the browser needs the server for usage + session data).

By default it surfaces the Claude Code sessions under `~/MyProjects`. Point it at a
different folder with an env var:

```bash
DISPATCH_ROOT=~/code python3 usage-server.py
```

The feature tracker (`tracker.html`) needs a ClickUp personal API token to fetch your
tasks — get one from ClickUp (avatar, bottom-left → Settings → Apps → API Token →
Generate). Paste it into a `.env` file in this folder (gitignored) and the server picks
it up automatically, no need to export it yourself:

```bash
# .env
CLICKUP_API_TOKEN=pk_your_token_here
```

By default the server also runs two background workers on a timer for as long as it stays
up: recaps every 5 min (calls `claude -p`, so this one does cost tokens continuously) and
the ClickUp feature-tracker every 10 min (plain ClickUp API calls — fast, and costs no
Claude tokens at all, but still needs `CLICKUP_API_TOKEN` set). If you'd rather start the
server only when you're about to look at the dashboard, and update it on demand via the
`↻ refresh` buttons instead:

```bash
DISABLE_WORKERS=1 python3 usage-server.py
```

## Files

| File | Role |
|------|------|
| `dev-dispatch.html` | The dashboard UI (single file, embedded CSS/JS). |
| `usage-server.py` | Local server on `127.0.0.1:8787`. Proxies live usage, parses session logs, opens/closes iTerm tabs. |
| `refresh-usage.sh` | Legacy one-shot usage fetcher. Superseded by the server's `/api/usage` — kept for reference. |

## How it works

```
~/.claude/projects/<root>/*.jsonl        (Claude Code session logs for DISPATCH_ROOT)
        │
        ▼
usage-server.py  _scan_session → _build_context   (picks resume, groups topics)
        │   GET /api/context        GET /api/usage (live, cached 60s)
        ▼                           POST /api/open   POST /api/close  (iTerm control)
dev-dispatch.html   renderContext() / pollUsage()
```

- **Live usage** comes from Claude's undocumented `GET /api/oauth/usage` endpoint, using the
  OAuth token already in your macOS Keychain (`Claude Code-credentials`). Nothing is stored on
  disk; the token is read at request time. Cached ≥60s — the endpoint rate-limits hard.
- **Resume card** = the project whose most recent session ended latest. The button runs
  `claude --resume <id> --permission-mode auto` in iTerm.
- **Topics** = hybrid fallback chain: repo+feature → shared files → summary keywords → misc.

## Local state — `state/` (gitignored, never published)

All persisted dashboard state lives in a single `state/` folder beside the server
(override with `DISPATCH_STATE_DIR`). It's gitignored so your work never leaves the machine:

- `state/iterm-registry.json` — maps Claude sessionId → iTerm session id (for focus/close).
- `state/read-markers.json` — "read" markers per session.
- `state/recaps.json` — per-session AI recaps.
- `state/issuecards.json` — per-session structured issue cards (focus board).
- `state/archived.json` — subjects you've archived/marked done.

The only external input is Claude Code's own logs (read-only):
`~/.claude/projects/<root>/*.jsonl`.

## Notes

- Focusing/closing a session from the dashboard only works for sessions **opened by the dashboard**
  (tracked in the registry). Manually-opened iTerm tabs aren't tracked yet.
- First time you resume/close, macOS will prompt for Automation permission to control iTerm.

## License

MIT — see [LICENSE](LICENSE).
