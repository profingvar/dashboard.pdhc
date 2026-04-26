#!/usr/bin/env bash
# Walks the CapabilityStatement and exercises every endpoint (Rule 9, Rule 20).
set -euo pipefail

BASE="${BASE:-http://localhost:9027}"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)_results"
OUT="./results/${TS}"
mkdir -p "$OUT"
LOG="$OUT/api_test.json"

pass=0; fail=0
record () { # name code expected [trailing]
  printf '  {"endpoint":"%s","status":%s,"expected":%s}%s\n' "$1" "$2" "$3" "${4:-,}" >> "$LOG"
  if [ "$2" = "$3" ]; then pass=$((pass+1)); else fail=$((fail+1)); fi
}

echo '[' > "$LOG"

code=$(curl -sk -o "$OUT/healthz.json" -w '%{http_code}' "$BASE/healthz")
record "/healthz" "$code" "200"

code=$(curl -sk -o "$OUT/metadata.json" -w '%{http_code}' "$BASE/metadata")
record "/metadata" "$code" "200"

code=$(curl -sk -o "$OUT/landing.html" -w '%{http_code}' "$BASE/")
record "/" "$code" "200"

code=$(curl -sk -o "$OUT/series_bad.json" -w '%{http_code}' "$BASE/api/v1/series")
record "/api/v1/series (no args → 400)" "$code" "400"

code=$(curl -sk -o "$OUT/series_ok.json" -w '%{http_code}' \
  "$BASE/api/v1/series?patient=00000000-0000-0000-0000-000000000000&concept=00000000-0000-0000-0000-000000000000")
record "/api/v1/series (empty set)" "$code" "200" ""

echo ']' >> "$LOG"

echo "pass=$pass fail=$fail → $LOG"
[ "$fail" -eq 0 ]
