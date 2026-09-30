// Behavioral check for the side-rail flash: pulls the REAL renderStatusPanel out of
// tracker.html (no copy of the logic lives here) and renders it with a stubbed DOM, so a
// change to the markup shows up as a failing assertion rather than a silent miss.
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("./tracker.html", import.meta.url), "utf8");
const start = html.indexOf("function renderStatusPanel(");
const end = html.indexOf("function flash(el)");
if (start < 0 || end < 0 || end <= start) throw new Error("couldn't locate renderStatusPanel in tracker.html");
const src = html.slice(start, end);

const sinks = {};
const $ = id => (sinks[id] ??= { textContent: "", innerHTML: "" });
const esc = s => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const prNumber = u => "PR #" + u.split("/").pop();

// only t1:r1 is "live"; t2:rX is not
const LIVE = new Set(["t1:r1"]);
const flashing = k => LIVE.has(k);

const render = new Function("$", "esc", "prNumber", "flashing", src + "; return renderStatusPanel;")($, esc, prNumber, flashing);

const PR_A = "https://github.com/org/repo-a/pull/32";
const PR_B = "https://github.com/org/repo-b/pull/99";
const data = { cards: [
  { taskId: "t1", name: "Task One", url: "u1", repos: [
    { id: "r1", repo: "org/repo-a", prUrl: PR_A, status: "working on it" },
    { id: "r9", repo: "org/repo-b", prUrl: PR_B, status: "working on it" },
  ]},
  // same PR_A linked from a second task -> the rail groups them into ONE entry
  { taskId: "t2", name: "Task Two", url: "u2", repos: [
    { id: "r5", repo: "org/repo-a", prUrl: PR_A, status: "working on it" },
  ]},
]};

render(data, "wpN", "wpL", ["working on it", "blocked"]);
const out = $("wpL").innerHTML;

let fails = 0;
const check = (name, got, want) => {
  const ok = String(got) === String(want);
  if (!ok) fails++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  got=${got} want=${want}`);
};

const items = out.split('<div class="wp-item').slice(1);
check("rail groups the shared PR into one entry", items.length, 2);

const a = items.find(s => s.includes("repo-a"));
const b = items.find(s => s.includes("repo-b"));

check("shared entry carries BOTH task keys", /data-keys="([^"]*)"/.exec(a)[1], "t1:r1|t2:r5");
check("live entry is marked", a.startsWith('" wp-shared status-changed"') || a.includes("status-changed"), true);
check("entry with no live key is NOT marked", b.includes("status-changed"), false);
check("unmarked entry still carries its key", /data-keys="([^"]*)"/.exec(b)[1], "t1:r9");

// a key that is live only via the SECOND task must still light the shared entry
LIVE.clear(); LIVE.add("t2:r5");
render(data, "wpN", "wpL", ["working on it", "blocked"]);
const out2 = $("wpL").innerHTML;
const a2 = out2.split('<div class="wp-item').slice(1).find(s => s.includes("repo-a"));
check("shared entry lights from its SECOND key too", a2.includes("status-changed"), true);

// nothing live -> nothing marked
LIVE.clear();
render(data, "wpN", "wpL", ["working on it", "blocked"]);
check("nothing live -> no entry marked", $("wpL").innerHTML.includes("status-changed"), false);

console.log(fails ? `\n${fails} check(s) failed` : "\nall checks passed");
process.exit(fails ? 1 : 0);
