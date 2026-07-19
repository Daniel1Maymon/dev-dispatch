# Feature Tracker — Bug Log

Living record of every bug found while building the Feature Tracker (`tracker.html` +
ClickUp/PR auto-detection in `usage-server.py`). Each entry: what broke, root cause, what was
changed, and exactly how to confirm it's actually fixed. Update this file, don't just fix and
move on — that's what caused the confusion today.

---

## 11. A single bad-content fetch wiped real PR entries — reconciliation now requires two misses

**Status:** ✅ FIXED

**Symptom:** `86ey29tvd` (CourtListener tool-runners) showed "No repos added yet" on the
dashboard despite its ClickUp description genuinely having 3 real PR links (verified directly).
`feature-tracker.json` showed `repos: []` with a fresh `updatedAt` — something had actively
wiped it, not just failed to detect it.

**Root cause:** `_gen_task_text(86ey29tvd)` succeeded — no retry/failure logged — but the JSON
it returned had incomplete content missing the real "Pull Requests" section. Reconciliation
(bug #9's fix) correctly requires non-empty text before dropping anything, but this fetch WAS
non-empty, just wrong. A single such incident, with the fetch call itself reporting success,
is invisible to every existing safety check.

**Fix applied:** `_merge_detected_prs` (`usage-server.py:1187`) now tracks a `notFoundStreak`
per auto entry and only drops it after `MISS_THRESHOLD` (2) **consecutive** fetches fail to
find it — not on the first miss. A single bad-content fetch can no longer destroy real data;
it would take two independent fetches agreeing something is gone.

**How to verify:** `grep -n "MISS_THRESHOLD" usage-server.py`. Functionally: after a refresh
correctly re-detects a task's PRs, check `feature-tracker.json` — auto entries should carry
`"notFoundStreak": 0`. To confirm the counter increments rather than instant-dropping, you'd
need to catch a real single-miss event (hard to force on demand) and check the entry survives
with `notFoundStreak: 1` rather than disappearing.

---

## 10. Dashboard went completely empty — model returned an empty task list, treated as truth

**Status:** ✅ FIXED

**Symptom:** `tracker.html` showed "Nothing in progress/review/testing/waiting-for-deployment
right now" despite 8 real in-flight tasks existing. `clickup-tasks.json` had `error: null` and
`tasks: {}` — looked like a clean, successful refresh that just happened to find zero tasks.

**Root cause:** Reproduced directly — the same exact fetch prompt, run 3 times in a row,
produced 3 different outcomes: one correct tool-calling response, one that called nothing and
appended prose claiming it "requires approval," one that refused outright and asked for
permission. This is `claude -p` with the haiku model inconsistently second-guessing whether
it's allowed to call the (correctly pre-approved via `--allowedTools`) ClickUp MCP tools — not
a real permission gate (confirmed via `claude mcp list`: the ClickUp server is connected/
approved at the user-config level). For the task-*list* call specifically, this hesitation
manifested as emitting a valid empty JSON array `[]` instead of refusing with prose — which
`_clickup_refresh()` then trusted as "0 real tasks" and used to overwrite a snapshot that
previously had 8.

**Fix applied:** Three changes in `usage-server.py`:
1. `CLICKUP_MODEL` switched from haiku to `claude-sonnet-5` — far more reliable at following
   "you're authorized, proceed" instructions.
2. Both fetch prompts (`_gen_task_list`, `_gen_task_text`) now open with an explicit "you are
   already authorized... do not ask for permission" line.
3. `_clickup_refresh()`: if the task list comes back as an empty list AND we already have tasks
   from a previous fetch, treat it as suspicious rather than truth — set an error, keep the
   prior `tasks` data untouched, instead of overwriting good data with nothing.

**How to verify:** `grep -n "claude-sonnet-5" usage-server.py` and `grep -n "treated as
suspicious" usage-server.py` should both show the fix. Functionally: this can't be easily
forced to reproduce on demand (it was intermittent even under haiku), but the empty-list guard
means even if it recurs, the dashboard keeps showing the last known-good data instead of going
blank — check `clickup-tasks.json`'s `error` field for `"empty task list"` wording if the
dashboard ever looks stale again.

---

## 9. A PR from one task got cross-attributed to a different task

**Status:** ✅ FIXED — architecture changed to isolated per-task fetches

**Symptom:** The report-sections task (`86exuu1x0`) showed `web-getters-azure#51` as one of its
PRs. That PR is real, but it belongs to an unrelated task ("Find Case Law search endpoint") —
confirmed absent from report-sections' actual description and all 8 of its comments, and
confirmed (via `gh pr view`) to have a completely different title.

**Root cause:** The single combined `claude -p` call fetched every tracked task's description
+ comments and returned them all in one JSON array. Somewhere in generating that combined
output, the model attached a PR mention from a different task's context onto the wrong task's
entry — a form of cross-task contamination inherent to asking one model call to keep 8+ tasks'
worth of text straight at once.

**Fix applied:** Split the single combined call into `_gen_task_list()` (cheap, no
description/comments) + `_gen_task_text(task_id)` (one isolated `claude -p` call per tracked
task, run sequentially). A call that only ever sees ONE task's ClickUp text has no other
task's data available to mix in. `usage-server.py`, `_gen_task_list`/`_gen_task_text`/
`_run_claude_p_json`/`_clickup_refresh`.

**How to verify:** After a refresh, cross-check every repo entry's task assignment makes sense
— for a given task, its stored PR URLs should all trace back to that task's actual ClickUp
description/comments (spot-check with `clickup_get_task`/`clickup_get_task_comments` directly).
Since this is now architecturally impossible (each fetch only sees one task), a recurrence here
would mean the isolation itself broke — check `_gen_task_text` is being called per-`task_id`
and its prompt embeds that specific ID.

---

## 8. Refresh crashed entirely on malformed model output, silently losing the whole cycle

**Status:** ✅ FIXED

**Symptom:** Server log showed `[clickup] worker error: 'str' object has no attribute 'get'`.
A user-edited task (`orchestrator#58` removed from `86exzet45`'s description) still showed the
stale entry after a refresh — not because reconciliation is broken, but because the refresh
that would have reconciled it crashed partway through and never reached the save step at all
(neither `feature-tracker.json` nor `clickup-tasks.json` get written if the per-task loop
raises, since both saves happen only after the loop completes cleanly).

**Root cause:** `_clickup_refresh()`'s loop did `t.get("id")` assuming every item in the parsed
JSON array is a dict. The model occasionally returns a malformed array containing a plain
string instead of an object, and that one bad element crashed the entire refresh — losing
every task's update for that cycle, not just the malformed one.

**Fix applied:** `usage-server.py`, in `_clickup_refresh()`'s loop: `if not isinstance(t, dict):
continue` before touching `t.get(...)` — skips the malformed entry, keeps processing the rest.

**How to verify:**
```bash
grep -n "malformed model output" usage-server.py
```
Should show the guard. Functionally: after a refresh completes, check the log has no
`worker error` line, and `clickup-tasks.json`'s `fetchedAt` actually advances.

---

## 1. Some tasks show 0 PR entries despite the ClickUp description genuinely containing links

**Status:** ✅ FIXED — verified, was actually caused by bug #3 (concurrent fetches)

**Symptom:** `86ey28jg7` (Companies House) had 3 real PR links in its ClickUp description
(confirmed via direct `clickup_get_task` call: db-updates #28, shared-classes #57,
tool-runners #137) but `feature-tracker.json` showed 0 repos for it. Same for several other
tracked tasks at the time.

**Root cause:** Turned out to be bug #3 (multiple concurrent `claude -p` fetches hammering the
same MCP server) corrupting/dropping per-task text in that specific run — not a separate bug.
Once #3 (the concurrency lock) and #2 (revert to additive-only) were fixed, a clean single
refresh correctly picked up all 3 Companies House PRs, all 3 delete-investigation PRs, and every
other task with a real link.

**How it was verified:** After a clean refresh, cross-checked EVERY task still showing 0 repos
directly via `clickup_get_task` + `clickup_get_task_comments` — all 3 (`86ey29tvd`, `86ey29tqb`,
`86ey29tqv`) genuinely have zero PR links anywhere (description or comments), confirming 0 is
the correct state for them, not a bug.

**How to re-verify if this recurs:**
```bash
python3 -c "
import json
d = json.load(open('state/feature-tracker.json'))
print(d.get('86ey28jg7'))
"
```
Expect 3 repo entries (db-updates #28, shared-classes #57, tool-runners #137). If missing again
after a refresh that completed with `error: None`, check bug #3 hasn't regressed (`ps aux | grep
claude -p` should never show more than one ClickUp fetch running at once).

---

## 2. Reconciliation (auto-removing stale PR entries) caused real data loss — twice, then restored

**Status:** ✅ FIXED — re-enabled after fixing the actual root cause (bug #3), verified against
two real ClickUp edits

**Symptom:** `delete-investigation`'s 3-4 legitimately-detected repo entries vanished entirely
after a refresh, even though the PRs were still genuinely in the ClickUp description.

**Root cause:** Turned out to be bug #3 (concurrent `claude -p` fetches corrupting per-task
text under contention) — reconciliation's logic itself was correct, it just trusted corrupted
input. First response was to revert reconciliation entirely (additive-only) rather than fix the
real cause — but the user explicitly wants removed/edited PR mentions to disappear from the
dashboard automatically (no manual cleanup), so once bug #3's lock was in place, reconciliation
was restored rather than left off.

**Fix applied:** `_merge_detected_prs` (`usage-server.py:1156`) reconciles again: drops any
`source: "auto"` entry whose PR link no longer appears in the current description+comments
text, then adds new links found. One guard remains: skip the drop side entirely when fetched
text is completely empty (still a signal that this task's fetch may have failed, not that its
ClickUp text was wiped) — but with concurrent fetches now impossible, partial/corrupted text
per-task shouldn't recur.

**How to verify:** Edit a tracked task's ClickUp description to remove a PR link it previously
had, trigger a refresh, and confirm that specific `source: "auto"` entry disappears from
`feature-tracker.json` while unrelated entries for the same task stay. Verified live on
2026-07-08 against `86exzet45` (stop-investigation, `orchestrator#58` removed from description)
and `86exuu1x0` (report-sections, `report-generator#4` removed).

---

## 3. Multiple concurrent `claude -p` ClickUp fetches piling up

**Status:** ✅ FIXED

**Symptom:** Found 3 separate `claude -p` processes fetching ClickUp simultaneously. Likely
contributed to bug #1 and to the recurring "I can't invoke MCP tools" failures (resource/API
contention).

**Root cause:** `POST /api/tracker/refresh` always spawned a new fetch thread with no check for
one already running. Restarting the server (done many times while debugging) also orphans any
in-flight `claude -p` subprocess instead of killing it, since killing the parent Python process
doesn't kill its already-spawned children.

**Fix applied:** `_CLICKUP_REFRESH_LOCK` (`usage-server.py:1022`, non-blocking acquire) guards
both the periodic worker and the manual refresh endpoint — a second trigger while one is running
is skipped/reported as already-running instead of starting a duplicate.

**How to verify:**
```bash
curl -s -X POST http://localhost:8787/api/tracker/refresh   # first call
curl -s -X POST http://localhost:8787/api/tracker/refresh   # immediately after
```
Second response should include `"alreadyRunning": true`. `ps aux | grep '[c]laude -p'` should
never show more than one ClickUp fetch process at a time.

---

## 4. Refresh button gave a false "done" signal after 5 seconds

**Status:** ✅ FIXED (code fixed; not yet verified in an actual browser — extension wasn't
connected to test with)

**Symptom:** User changed a ClickUp task's status, clicked refresh, saw no change and reasonably
assumed the button was broken. Real cause: the fetch takes 3–13 minutes, but the button reset
itself after a hardcoded 5-second timeout and re-polled — showing stale data every time.

**Fix applied:** `tracker.html`'s `refreshNow()` now polls `/api/tracker` every 10s (up to a 15
min ceiling) until the snapshot actually changes, showing elapsed time, and disables itself
while a refresh is in flight instead of allowing repeat clicks (which was feeding bug #3).

**How to verify:** Open `http://localhost:8787/tracker.html`, click "↻ refresh". Button text
should show `↻ refreshing… (Ns, usually 3-10 min)` and count up, then return to normal and show
new data once the backend fetch completes — NOT reset after 5 seconds.

---

## 5. `--allowedTools` argument swallowed the prompt text

**Status:** ✅ FIXED, verified

**Symptom:** Headless fetch failed instantly with `Error: Input must be provided either through
stdin or as a prompt argument when using --print`.

**Root cause:** `--allowedTools <value> <prompt>` as three separate argv tokens let the CLI's
variadic tool-list parser keep consuming tokens and swallow the prompt string as a fake tool
name.

**Fix applied:** Bind with `=`: `--allowedTools=<value>` as one token (`usage-server.py`, in
`_gen_clickup_snapshot`).

**How to verify:** Any successful refresh confirms this — check `state/clickup-tasks.json`'s
`fetchedAt` advances and `error` is `null` after a refresh.

---

## 6. Fetch timeout too short for description+comments per task

**Status:** ✅ FIXED, verified (measured 183–280s for 9 tasks; timeout now 600s with margin)

**Symptom:** Fetch timed out at 240s once comments-fetching was added (roughly doubles the
per-task round-trips vs. description-only).

**Fix applied:** Timeout raised to 600s (`usage-server.py`, `_gen_clickup_snapshot`), plus a
one-retry on failure since the headless run occasionally (and unpredictably) claims it can't
call MCP tools at all.

**How to verify:** A full refresh should complete (successfully or with a clear error) well
under 600s. Check server log for `[clickup] refreshed, N tracked task(s)`.

---

## 7. `--permission-mode bypassPermissions` was overly broad

**Status:** ✅ FIXED, verified

**Symptom:** N/A — caught by the auto-mode safety classifier before it ran, not by the user.

**Root cause:** Disables ALL permission checks for the headless process, not just the 3 ClickUp
read tools it actually needs.

**Fix applied:** Removed the flag entirely; `--allowedTools` alone pre-approves only the named
tools, everything else is denied cleanly in headless mode (no prompt possible, so it just fails
closed).

**How to verify:**
```bash
grep -n "bypassPermissions" usage-server.py
```
Should return nothing in `_gen_clickup_snapshot`.

---

## Not bugs (deliberately deferred / resolved by user action)

- **"Removed via UI, does it come back?"** — open design question, not yet decided. The one
  concrete case that looked like this was actually the user editing the ClickUp description
  directly, not a UI removal reappearing.
- **Time-to-implement tracking** — discussed (ClickUp's "Time in Status" isn't available on the
  current plan; recommended tracking our own timestamps in `feature-tracker.json` instead) —
  not implemented, waiting on a decision.
