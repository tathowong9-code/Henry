#!/usr/bin/env bash
# Trading 212 API helper. Reads credentials from the environment only:
#   T212_API_KEY, T212_API_SECRET  (set in the cloud environment settings, never in the repo)
# Usage:
#   scripts/t212.sh GET  /equity/account/summary
#   scripts/t212.sh GET  /equity/orders
#   scripts/t212.sh POST /equity/orders/limit '{"ticker":"MDB_US_EQ","quantity":1,"limitPrice":415,"timeValidity":"GOOD_TILL_CANCEL"}'
set -euo pipefail

: "${T212_API_KEY:?T212_API_KEY is not set}"
: "${T212_API_SECRET:?T212_API_SECRET is not set}"

BASE="${T212_BASE_URL:-https://live.trading212.com/api/v0}"
METHOD="${1:?method required}"
PATH_="${2:?path required}"
BODY="${3:-}"

CREDENTIALS=$(printf '%s:%s' "$T212_API_KEY" "$T212_API_SECRET" | base64 | tr -d '\n')

args=(-sS -X "$METHOD" "$BASE$PATH_" -H "Authorization: Basic $CREDENTIALS")
if [ -n "$BODY" ]; then
  args+=(-H "Content-Type: application/json" -d "$BODY")
fi
curl "${args[@]}"
echo
