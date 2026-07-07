#!/usr/bin/env python3
"""Smoke test for the topic edit/delete/move + archive-cascade logic.

Runs against a throwaway STATE_DIR so it never touches your real state/.
Usage:  python3 test_topic_overrides.py
"""
import os
import tempfile

# point the server at a temp state dir BEFORE importing it
_TMP = tempfile.mkdtemp(prefix="dispatch-test-")
os.environ["DISPATCH_STATE_DIR"] = _TMP

import importlib.util

spec = importlib.util.spec_from_file_location(
    "usage_server", os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage-server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

ok = 0
fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name}")


def fresh_sessions():
    """Two repo sessions in different topics + one misc."""
    return [
        {"id": "a" * 36, "_key": "repo:tool-runners/", "_title": "tool-runners",
         "_src": "repo", "msgs": 3, "end": "2026-06-17T10:00:00Z"},
        {"id": "b" * 36, "_key": "repo:tool-runners/google query tool",
         "_title": "tool-runners · google query tool", "_src": "repo",
         "msgs": 5, "end": "2026-06-17T11:00:00Z"},
        {"id": "c" * 36, "_key": "misc", "_title": "Misc / one-offs",
         "_src": "misc", "msgs": 1, "end": "2026-06-17T09:00:00Z"},
    ]


print("rename:")
srv._topic_action("rename", {"key": "repo:tool-runners/", "title": "Tool Runners"})
s = fresh_sessions()
srv._apply_topic_overrides(s)
check("topic title overridden", s[0]["_title"] == "Tool Runners")
check("topic key unchanged by rename", s[0]["_key"] == "repo:tool-runners/")
srv._topic_action("rename", {"key": "repo:tool-runners/", "title": ""})
s = fresh_sessions()
srv._apply_topic_overrides(s)
check("empty rename reverts to auto name", s[0]["_title"] == "tool-runners")

print("move:")
srv._topic_action("move", {"session": "c" * 36, "key": "repo:tool-runners/"})
s = fresh_sessions()
srv._apply_topic_overrides(s)
check("session moved to target key", s[2]["_key"] == "repo:tool-runners/")
check("moved session gets target's title", s[2]["_title"] == "tool-runners")
srv._topic_action("move", {"session": "c" * 36, "key": ""})
s = fresh_sessions()
srv._apply_topic_overrides(s)
check("clearing move reverts to structural topic", s[2]["_key"] == "misc")

print("move to a brand-new custom topic:")
srv._topic_action("move", {"session": "c" * 36, "key": "custom:abc", "title": "My New Topic"})
s = fresh_sessions()
srv._apply_topic_overrides(s)
check("session lands in custom topic", s[2]["_key"] == "custom:abc")
check("custom topic carries its title", s[2]["_title"] == "My New Topic")
srv._topic_action("move", {"session": "c" * 36, "key": ""})

print("delete (merge) + cascade of moved sessions:")
# move c into the google-query topic, then delete that topic into tool-runners
srv._topic_action("move", {"session": "c" * 36, "key": "repo:tool-runners/google query tool"})
srv._topic_action("delete", {"key": "repo:tool-runners/google query tool",
                             "target": "repo:tool-runners/"})
s = fresh_sessions()
srv._apply_topic_overrides(s)
check("deleted topic's own session folds into target", s[1]["_key"] == "repo:tool-runners/")
check("session moved into deleted topic follows to target", s[2]["_key"] == "repo:tool-runners/")

print("delete loop is refused:")
srv._topics_save({})  # reset
srv._topic_action("delete", {"key": "repo:x/", "target": "repo:y/"})
looped = False
try:
    srv._topic_action("delete", {"key": "repo:y/", "target": "repo:x/"})
except ValueError:
    looped = True
check("merge cycle rejected", looped)

print("validation:")
for bad in [{"key": "../etc", "title": "x"}]:
    raised = False
    try:
        srv._topic_action("rename", bad)
    except ValueError:
        raised = True
    check("invalid key rejected", raised)
raised = False
try:
    srv._topic_action("move", {"session": "not-a-uuid", "key": "misc"})
except ValueError:
    raised = True
check("invalid session id rejected", raised)

print("archive cascade (topic -> sessions):")
srv._topics_save({})
topics = [
    {"id": "repo:tool-runners/", "title": "tool-runners",
     "lastActivity": "2026-06-17T11:00:00Z",
     "sessions": [{"id": "a" * 36, "msgs": 3, "end": "2026-06-17T10:00:00Z"},
                  {"id": "b" * 36, "msgs": 5, "end": "2026-06-17T11:00:00Z"}]},
]
srv.set_archived("repo:tool-runners/", False, topics)
ids = srv._archived_topic_ids(topics)
check("archived topic id reported", "repo:tool-runners/" in ids)
# auto-revive: bump a session's activity -> fingerprint changes -> un-archived
topics[0]["sessions"][0]["msgs"] = 99
ids = srv._archived_topic_ids(topics)
check("topic auto-revives on new activity", "repo:tool-runners/" not in ids)

print("merge alias (same as delete):")
srv._topics_save({})
srv._topic_action("merge", {"key": "repo:foo/", "target": "repo:bar/"})
s = [{"id": "d" * 36, "_key": "repo:foo/", "_title": "foo", "_src": "repo"}]
srv._apply_topic_overrides(s)
check("merge folds source into target", s[0]["_key"] == "repo:bar/")

print("batch session flags:")
srv._sflags_save({})
sids = ["e" * 36, "f" * 36]
srv._sessions_action("archive", {"sessions": sids})
fl = srv._sflags_load()
check("both sessions archived", all(fl.get(x, {}).get("archived") for x in sids))
srv._sessions_action("unarchive", {"sessions": ["e" * 36]})
fl = srv._sflags_load()
check("one unarchived, one still archived",
      not fl.get("e" * 36, {}).get("archived") and fl.get("f" * 36, {}).get("archived"))

srv._sessions_action("delete", {"sessions": sids})
fl = srv._sflags_load()
check("both sessions soft-deleted", all(fl.get(x, {}).get("deleted") for x in sids))
trash = srv._trash_list()
check("trash lists deleted sessions", trash["count"] == 2)
srv._sessions_action("undelete", {"sessions": sids})
check("undelete empties trash", srv._trash_list()["count"] == 0)

print("batch move:")
srv._topics_save({})
srv._sessions_action("move", {"sessions": sids, "key": "repo:dest/"})
ov = srv._topics_load()
check("both sessions moved to target", all(ov["moves"].get(x) == "repo:dest/" for x in sids))

print("batch validation:")
raised = False
try:
    srv._sessions_action("archive", {"sessions": ["nope"]})
except ValueError:
    raised = True
check("no valid sessions rejected", raised)
raised = False
try:
    srv._sessions_action("bogus", {"sessions": [sids[0]]})
except ValueError:
    raised = True
check("unknown session action rejected", raised)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
