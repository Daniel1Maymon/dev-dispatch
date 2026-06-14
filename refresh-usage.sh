#!/usr/bin/env bash
# Pulls live Claude usage from the same endpoint Claude Code's /usage uses,
# then writes usage-data.js (window.USAGE) for dev-dispatch.html to read.
# Run it before opening the dashboard, or on a schedule (see bottom).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. OAuth token Claude Code already stored (needs user:profile scope)
TOK=$(security find-generic-password -s "Claude Code-credentials" -w \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('claudeAiOauth',{}).get('accessToken',''))")

if [ -z "$TOK" ]; then echo "No Claude Code OAuth token in Keychain" >&2; exit 1; fi

# 2. Hit the undocumented usage endpoint (one shot — it rate-limits hard)
RESP=$(curl -s https://api.anthropic.com/api/oauth/usage \
  -H "Authorization: Bearer $TOK" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "Content-Type: application/json")

# 3. Reshape into window.USAGE = {...}
echo "$RESP" | python3 -c "
import sys, json, datetime
d = json.load(sys.stdin)
def blk(b):
    return None if not b else {'utilization': b.get('utilization'), 'resets_at': b.get('resets_at')}
out = {
  'fetched_at':       datetime.datetime.now(datetime.timezone.utc).isoformat(),
  'five_hour':        blk(d.get('five_hour')),
  'seven_day':        blk(d.get('seven_day')),
  'seven_day_opus':   blk(d.get('seven_day_opus')),
  'seven_day_sonnet': blk(d.get('seven_day_sonnet')),
}
print('window.USAGE = ' + json.dumps(out, indent=2) + ';')
" > "$DIR/usage-data.js"

echo "Wrote $DIR/usage-data.js"

# Schedule it (optional): refresh every 10 min while you're working —
#   */10 * * * * "$HOME"/dev-dispatch/refresh-usage.sh
