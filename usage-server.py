#!/usr/bin/env python3
"""
Local server for The Dev Dispatch.
- Serves dev-dispatch.html and assets from this directory.
- GET /api/usage  -> live Claude usage (same endpoint Claude Code's /usage uses),
  pulling the OAuth token from the macOS Keychain. Cached 60s to respect the
  endpoint's aggressive rate limiting.

Run:  python3 usage-server.py   then open  http://localhost:8787/dev-dispatch.html
"""
import datetime
import glob
import http.server
import json
import os
import re
import shlex
import socketserver
import subprocess
import time
import urllib.request

PORT = 8787
DIR = os.path.dirname(os.path.abspath(__file__))
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Root folder whose Claude Code sessions this dashboard surfaces.
# Override with DISPATCH_ROOT; defaults to ~/MyProjects.
MP = os.environ.get("DISPATCH_ROOT", os.path.expanduser("~/MyProjects")).rstrip("/")
# Claude Code stores each project's logs under ~/.claude/projects/<path-with-slashes-as-dashes>.
SESS_DIR = os.path.join(os.path.expanduser("~/.claude/projects"), MP.replace("/", "-"))
_cache = {"at": 0, "data": None}
_ctx_cache = {"at": 0, "data": None}
CACHE_TTL = 60  # seconds

# ---------- "read" markers: sessionId -> last-activity ts acknowledged ----------
READ_FILE = os.path.expanduser("~/.claude/.dev-dispatch-read.json")


def _load_read():
    try:
        with open(READ_FILE) as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_read(d):
    tmp = READ_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2, sort_keys=True)
    os.replace(tmp, READ_FILE)


def _session_state(s, marks):
    """new = never acknowledged, updated = grew since last read, read = current."""
    mk = marks.get(s["id"])
    if mk is None:
        return "new"
    return "updated" if s["end"] > mk else "read"


def _apply_read_state(ctx):
    """Overlay read/unread state fresh on each request (outside the context cache,
    so 'mark read' reflects immediately)."""
    marks = _load_read()
    for s in ctx.get("sessions", []):
        s["state"] = _session_state(s, marks)
    for tp in ctx.get("topics", []):
        unread = 0
        for s in tp["sessions"]:
            s["state"] = _session_state(s, marks)
            if s["state"] != "read":
                unread += 1
        tp["unreadCount"] = unread
    return ctx


# ---------- real context from Claude Code session logs ----------
def _msg_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for p in c:
            if isinstance(p, dict) and p.get("type") == "text":
                return p["text"]
    return ""


def _proj_of(cwd):
    """(name, label, is_root) from a session cwd."""
    if not cwd or not cwd.startswith(MP):
        return None
    rest = cwd[len(MP):].strip("/")
    if rest == "":
        return ("MyProjects", "root", True)
    parts = rest.split("/")
    label = parts[-1] if "worktrees" in parts else parts[0]
    return (parts[0], label, False)


_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _scan_session(path):
    first_user = last_user = summary = cwd = branch = ts0 = ts1 = None
    n = 0
    edited = {}  # file_path -> edit count (signal for grouping repo-less sessions)
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") == "summary":
                summary = r.get("summary")
            if r.get("cwd"):
                cwd = r["cwd"]
            if r.get("gitBranch"):
                branch = r["gitBranch"]
            if r.get("timestamp"):
                ts0 = ts0 or r["timestamp"]
                ts1 = r["timestamp"]
            if r.get("type") == "user" and not r.get("isMeta"):
                t = _msg_text(r.get("message", {})).strip().replace("\n", " ")
                if t and not t.startswith("<") and not t.startswith("Caveat"):
                    if first_user is None:
                        first_user = t
                    last_user = t
                    n += 1
            elif r.get("type") == "assistant":
                for part in (r.get("message", {}).get("content") or []):
                    if isinstance(part, dict) and part.get("type") == "tool_use" \
                            and part.get("name") in _EDIT_TOOLS:
                        inp = part.get("input") or {}
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp:
                            edited[fp] = edited.get(fp, 0) + 1
    return dict(cwd=cwd, branch=branch, ts0=ts0, ts1=ts1, n=n,
                summary=summary, first=first_user, last=last_user, edited=edited)


_STOP = set((
    "the a an and or of to for in on at with my our your you i we is are be this "
    "that it im just need help want make please can get got use using feature work "
    "working file files code change changes add added like would could should do done "
    "from into out about all some more last first next new old how what why when"
).split())


def _norm_feature(feat):
    """Normalize a worktree/branch name so variants collapse to one topic."""
    f = feat.lower()
    f = re.sub(r"\d{4}[-_]?\d{2}[-_]?\d{2}", "", f)               # dates
    f = re.sub(r"[-_](squashed|copy|v\d+|final|new|old|wip|tmp|temp|2|3)\b", "", f)
    f = re.sub(r"^(feature|feat|fix|chore|wip)[-_/]?", "", f)     # branch prefixes
    f = re.sub(r"[-_/]+", " ", f).strip()
    return f or feat.lower()


def _keywords(text):
    toks = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", (text or "").lower())
    return {t for t in toks if t not in _STOP}


def _rel_parts(cwd):
    if cwd and cwd.startswith(MP):
        rel = cwd[len(MP):].strip("/")
        return [p for p in rel.split("/") if p]
    return []


_MP_PATH_RE = re.compile(re.escape(MP) + r"/([A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-]+)*)")


def _repo_feat_from_parts(parts):
    """(repo, normalized-feature|None) from MyProjects-relative path parts."""
    repo = parts[0]
    feat = None
    if "worktrees" in parts:
        i = parts.index("worktrees")
        feat = parts[i + 1] if len(parts) > i + 1 else None
    elif len(parts) > 1:
        feat = parts[-1]
    return repo, (_norm_feature(feat) if feat else None)


def _pick_repo_feat(parts_iter):
    """Most-frequent (repo, feature|None) over an iterable of (path-parts, weight).
    Only worktree paths contribute a feature, so stray file refs collapse to the repo."""
    counts = {}
    feat_of = {}
    for parts, w in parts_iter:
        if not parts:
            continue
        repo = parts[0]
        counts[repo] = counts.get(repo, 0) + w
        if "worktrees" in parts and repo not in feat_of:
            i = parts.index("worktrees")
            if len(parts) > i + 1:
                feat_of[repo] = _norm_feature(parts[i + 1])
    if not counts:
        return None
    repo = max(counts, key=lambda r: counts[r])
    return repo, feat_of.get(repo)


def _referenced_repo_feat(text):
    """(repo, feature|None) from /MyProjects/... paths mentioned in free text."""
    return _pick_repo_feat(
        ([p for p in m.group(1).split("/") if p], 1)
        for m in _MP_PATH_RE.finditer(text or "")
    )


def _edited_repo_feat(edited):
    """(repo, feature|None) from the paths of files actually edited — ground truth of
    which worktree the work happened in. Bare root-level files are ignored."""
    def gen():
        for fp, c in (edited or {}).items():
            if not fp.startswith(MP + "/"):
                continue
            parts = [p for p in fp[len(MP) + 1:].split("/") if p]
            if len(parts) >= 2:  # file inside a repo, not a bare root-level file
                yield parts, c
    return _pick_repo_feat(gen())


def _set_repo_topic(s, repo, featn, src):
    s["_key"] = "repo:%s/%s" % (repo, featn or "")
    s["_title"] = repo + (" · " + featn if featn else "")
    s["_src"] = src


def _assign_topics(sessions):
    """Resolve each session to a topic via the fallback chain:
       cwd repo+feature  ->  repo/path referenced in prompt  ->
       shared edited files  ->  misc."""
    repoless = []
    for s in sessions:
        # --- tier 1: cwd is inside a project dir/worktree ---
        parts = _rel_parts(s["cwd"])
        if parts:
            repo, featn = _repo_feat_from_parts(parts)
            _set_repo_topic(s, repo, featn, "repo")
            continue
        # --- tier 2: work outranks talk -> the worktree you actually edited in ---
        ref = _edited_repo_feat(s["edited"])
        if ref:
            _set_repo_topic(s, ref[0], ref[1], "edits")
            continue
        # root-level edits only -> let the shared-files pass cluster them
        if s["edited"]:
            repoless.append(s)
            continue
        # --- tier 3: no edits (pure read/explore) -> topic by referenced path ---
        text = " ".join(t for t in (s.get("prompt"), s.get("lastPrompt"),
                                    s.get("summary")) if t)
        ref = _referenced_repo_feat(text)
        if ref:
            _set_repo_topic(s, ref[0], ref[1], "path")
        else:
            repoless.append(s)

    # --- tier 3: union-find over remaining root sessions, SHARED FILES ONLY ---
    # (no keyword edges — they transitively chained unrelated sessions into one blob)
    parent = list(range(len(repoless)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    files_sig = [set(s["edited"].keys()) for s in repoless]
    for i in range(len(repoless)):
        for j in range(i + 1, len(repoless)):
            if files_sig[i] & files_sig[j]:
                parent[find(i)] = find(j)

    comps = {}
    for i in range(len(repoless)):
        comps.setdefault(find(i), []).append(i)

    for members in comps.values():
        files = {}
        for i in members:
            for fp, c in repoless[i]["edited"].items():
                b = os.path.basename(fp)
                files[b] = files.get(b, 0) + c
        if files:
            top = sorted(files, key=lambda x: -files[x])[:2]
            title = " + ".join(top)
            key = "files:" + "|".join(sorted(top))
            src = "files"
        else:
            title, key, src = "Misc / one-offs", "misc", "misc"
        for i in members:
            repoless[i]["_key"] = key
            repoless[i]["_title"] = title
            repoless[i]["_src"] = src
    return sessions


def _dur_min(a, b):
    if not a or not b:
        return 0
    d = datetime.datetime.fromisoformat(b.replace("Z", "+00:00")) - \
        datetime.datetime.fromisoformat(a.replace("Z", "+00:00"))
    return max(0, round(d.total_seconds() / 60))


def _build_context():
    now = time.time()
    if _ctx_cache["data"] and now - _ctx_cache["at"] < CACHE_TTL:
        return _ctx_cache["data"]

    files = sorted(glob.glob(os.path.join(SESS_DIR, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:40]
    sessions = []
    for f in files:
        s = _scan_session(f)
        pj = _proj_of(s["cwd"])
        if not pj or not s["ts0"] or s["n"] == 0:
            continue
        sessions.append(dict(
            id=os.path.basename(f)[:-6],  # filename stem = sessionId
            cwd=s["cwd"], edited=s["edited"],
            project=pj[0], label=pj[1], isRoot=pj[2],
            branch=s["branch"], start=s["ts0"], end=s["ts1"],
            durationMin=_dur_min(s["ts0"], s["ts1"]),
            prompt=(s["first"] or "")[:200],
            lastPrompt=(s["last"] or "")[:200],
            summary=s["summary"], msgs=s["n"],
        ))
    sessions.sort(key=lambda x: x["end"], reverse=True)

    # resolve topics (hybrid fallback chain) across ALL sessions
    _assign_topics(sessions)
    topics = {}
    for s in sessions:
        tp = topics.setdefault(s["_key"], dict(
            id=s["_key"], title=s["_title"], source=s["_src"],
            sessionCount=0, lastActivity=s["end"], sessions=[]))
        tp["sessionCount"] += 1
        if s["end"] > tp["lastActivity"]:
            tp["lastActivity"] = s["end"]
        tp["sessions"].append({k: s[k] for k in (
            "id", "project", "label", "branch", "start", "end", "prompt",
            "lastPrompt", "summary", "msgs", "durationMin")})
    topic_list = sorted(topics.values(), key=lambda x: x["lastActivity"], reverse=True)

    # aggregate projects (skip the bare MyProjects root — those are meta chats)
    week_ago = (datetime.datetime.now(datetime.timezone.utc) -
                datetime.timedelta(days=7)).isoformat()
    projects = {}
    for s in sessions:
        if s["isRoot"]:
            continue
        p = projects.setdefault(s["project"], dict(
            name=s["project"], label=s["label"], lastActivity=s["end"],
            firstSeen=s["start"], sessionCount=0, minutes=0, path=s["cwd"],
            lastSessionId=s["id"],
            lastPrompt=s["lastPrompt"] or s["prompt"], bigPicture=s["prompt"]))
        p["sessionCount"] += 1
        p["minutes"] += s["durationMin"]
        if s["end"] > p["lastActivity"]:
            p["lastActivity"] = s["end"]
            p["path"] = s["cwd"]
            p["lastSessionId"] = s["id"]
            p["lastPrompt"] = s["prompt"]  # opening prompt of most-recent session
        if s["start"] < p["firstSeen"]:
            p["firstSeen"] = s["start"]
            p["bigPicture"] = s["prompt"]
    proj_list = sorted(projects.values(), key=lambda x: x["lastActivity"], reverse=True)

    data = dict(
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        resume=proj_list[0] if proj_list else None,
        projects=proj_list[:4],
        topics=topic_list,
        sessions=sessions[:8],
        sessionsThisWeek=sum(1 for s in sessions if s["end"] >= week_ago),
    )
    _ctx_cache.update(at=now, data=data)
    return data


def _token():
    out = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]
    )
    return json.loads(out).get("claudeAiOauth", {}).get("accessToken", "")


def _fetch_usage():
    now = time.time()
    if _cache["data"] and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {_token()}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    # nosec B310 — URL is the hardcoded https USAGE_URL constant, never user input
    with urllib.request.urlopen(req, timeout=15) as r:  # nosec B310
        data = json.loads(r.read())
    _cache.update(at=now, data=data)
    return data


REG_PATH = os.path.expanduser("~/.claude/.dispatch-iterm.json")


def _reg_load():
    try:
        with open(REG_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _reg_save(reg):
    try:
        with open(REG_PATH, "w") as fh:
            json.dump(reg, fh)
    except Exception:
        pass


def _osa(script):
    return subprocess.run(["osascript", "-e", script],
                          check=True, capture_output=True, text=True).stdout.strip()


def _open_in_iterm(sid, cwd):
    """Focus the iTerm tab already running this session, or open a new one.

    We remember the iTerm session id we created for each Claude session id in a
    small on-disk registry, so we never open a duplicate tab — and it survives a
    restart of this server. Returns "focused" or "opened".
    """
    reg = _reg_load()
    iterm_id = reg.get(sid)

    # 1) if we've opened this session before, try to focus that exact tab
    if iterm_id:
        found = _osa(f'''
        tell application "iTerm2"
          repeat with w in windows
            repeat with t in tabs of w
              repeat with s in sessions of t
                if (id of s) is "{iterm_id}" then
                  select w
                  tell t to select
                  tell s to select
                  activate
                  return "focused"
                end if
              end repeat
            end repeat
          end repeat
          return "notfound"
        end tell''')
        if found == "focused":
            return "focused"

    # 2) otherwise open a fresh tab (or window) and remember its iTerm id
    cmd = f"cd {shlex.quote(cwd)} && claude --resume {sid} --permission-mode auto"
    new_id = _osa(f'''
    tell application "iTerm2"
      activate
      if (count of windows) = 0 then
        set theWin to (create window with default profile)
      else
        set theWin to current window
        tell theWin to create tab with default profile
      end if
      tell current session of theWin to write text {json.dumps(cmd)}
      return id of current session of theWin
    end tell''')
    if new_id:
        reg[sid] = new_id
        _reg_save(reg)
    return "opened"


def _close_iterm(sid):
    """Close the iTerm tab we opened for this session. We can only close ones we
    launched (tracked in the registry). Returns closed / notfound / unknown."""
    reg = _reg_load()
    iterm_id = reg.get(sid)
    if not iterm_id:
        return "unknown"
    res = _osa(f'''
    tell application "iTerm2"
      repeat with w in windows
        repeat with t in tabs of w
          repeat with s in sessions of t
            if (id of s) is "{iterm_id}" then
              tell s to close
              return "closed"
            end if
          end repeat
        end repeat
      end repeat
      return "notfound"
    end tell''')
    reg.pop(sid, None)
    _reg_save(reg)
    return res or "notfound"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIR, **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        is_open = self.path.startswith("/api/open")
        is_close = self.path.startswith("/api/close")
        if self.path.startswith("/api/read"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                marks = _load_read()
                items = (body["marks"].items() if isinstance(body.get("marks"), dict)
                         else [(body.get("session", ""), body.get("end", ""))])
                n = 0
                for sid, end in items:
                    if re.fullmatch(r"[0-9a-fA-F-]{36}", sid or "") and end:
                        marks[sid] = end
                        n += 1
                _save_read(marks)
                return self._json({"ok": True, "marked": n})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if is_open or is_close:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                sid = body.get("session", "")
                # strict allow-list: a session id is a UUID
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", sid):
                    return self._json({"error": "invalid session id"}, 400)
                if is_close:
                    action = _close_iterm(sid)
                    return self._json({"ok": True, "action": action, "session": sid})
                # open: recover the session's working directory from its log
                cwd = MP
                path = os.path.join(SESS_DIR, sid + ".jsonl")
                if os.path.exists(path):
                    with open(path, errors="ignore") as fh:
                        for line in fh:
                            try:
                                r = json.loads(line)
                            except Exception:
                                continue
                            if r.get("cwd"):
                                cwd = r["cwd"]
                                break
                action = _open_in_iterm(sid, cwd)
                return self._json({"ok": True, "action": action, "cwd": cwd, "session": sid})
            except subprocess.CalledProcessError as e:
                return self._json({"error": "osascript failed — grant Automation "
                                            "permission to control iTerm", "detail": str(e)}, 500)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        if self.path.startswith("/api/context"):
            try:
                self._json(_apply_read_state(_build_context()))
            except Exception as e:
                self._json({"error": str(e)}, 502)
            return
        if self.path.startswith("/api/usage"):
            try:
                body = json.dumps(_fetch_usage()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Dev Dispatch → http://localhost:{PORT}/dev-dispatch.html")
        httpd.serve_forever()
