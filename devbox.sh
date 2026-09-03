#!/usr/bin/env bash
# Run a command in the migration converter on the devbox.
#
# SME_COOKIE is read from .env here and piped to the far side, so the auth token
# never appears in a command line, a process list or a shell history.
#
#   ./devbox.sh python3 tools/regroup_ledgers.py
#   ./devbox.sh python3 tools/regroup_ledgers.py --only 1543849946830110720 --apply
#   ./devbox.sh python3 -m emit.bills_apdirect --report
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
COOKIE="$(sed -n 's/^SME_COOKIE=//p' "$HERE/.env" | head -1)"
[ -n "$COOKIE" ] || { echo "SME_COOKIE not found in $HERE/.env" >&2; exit 2; }
# Host from .env too, for the same reason the cookie is: it does not belong in
# a tracked file. Same value the address used to be hardcoded to.
DBHOST="$(sed -n 's/^SME_DB_HOST=//p' "$HERE/.env" | head -1)"
[ -n "$DBHOST" ] || { echo "SME_DB_HOST not found in $HERE/.env" >&2; exit 2; }
exec ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "root@$DBHOST" \
  "cd /root/indiandesign/converter && export SME_COOKIE=\"\$(cat)\" && $*" <<<"$COOKIE"
