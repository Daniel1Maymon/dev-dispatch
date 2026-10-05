// Renders PR Reviews cards through the page's real code (ago() through factsBlock()) and
// checks the "review by / opened / code updated / last review" rows inside each card.
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("./reviews.html", import.meta.url), "utf8");
const start = html.indexOf("  function ago(iso)");
const end = html.indexOf("  /* ---------------------------------------------------------------------\n     Status-change alerts.");
if (start < 0 || end < 0) throw new Error("card code not found in reviews.html");
const esc = s => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const renderCard = new Function("esc", "flashing", html.slice(start, end) + "; return renderCard;")(esc, () => false);

let fails = 0;
function check(name, got, want){
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}
const hoursAgo = h => new Date(Date.now() - h * 3600000).toISOString();
const base = { url: "https://github.com/o/r/pull/1", number: 1, title: "T", repo: "r", author: "someone",
  updatedAt: hoursAgo(1), state: "OPEN", myVerdict: "COMMENTED" };

const full = renderCard({ ...base,
  reviewers: [{ name: "Daniel", login: "daniel1maymon", state: "COMMENTED" }, { name: "Barak", login: "baraktenjin", state: "APPROVED" }],
  createdAt: hoursAgo(24 * 12 + 2), lastCommitAt: hoursAgo(5), lastReviewAt: hoursAgo(50) });
const row = lab => (full.match(new RegExp(`<span class="lab">${lab}</span>(<span[^>]*>.*?</span>)`)) || [])[1] || "";
check("review by lists reviewers with marks", /Daniel 💬.* · .*Barak ✅/.test(full), true);
check("opened 12 days ago -> date + '(12 days ago)'", /\(12 days ago\)/.test(row("opened")), true);
check("code updated 5h ago -> '5h ago', no parentheses", />5h ago</.test(row("code updated")), true);
check("last review 50h ago -> '(2 days ago)'", /\(2 days ago\)/.test(row("last review")), true);

const bare = renderCard({ ...base, reviewers: [], createdAt: "", lastCommitAt: "", lastReviewAt: "" });
check("no reviewers -> 'nobody yet'", bare.includes("nobody yet"), true);
check("no review -> 'none yet'", /last review<\/span><span class="none">none yet/.test(bare), true);

console.log(fails ? `\n${fails} check(s) failed` : "\nall checks passed");
process.exit(fails ? 1 : 0);
