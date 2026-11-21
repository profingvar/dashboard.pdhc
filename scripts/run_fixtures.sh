#!/usr/bin/env bash
# dashboard.pdhc — run the app LOCALLY on SYNTHETIC fixture data (#974 step 22).
#
#   scripts/run_fixtures.sh                         # 127.0.0.1:9029
#   FIXTURES_PORT=9028 scripts/run_fixtures.sh      # another port in OUR block
#   DASHBOARD_FIXTURES_FAIL=ips,plan scripts/run_fixtures.sh   # sources down
#   DASHBOARD_FIXTURES_ANCHOR=2026-11-17 scripts/run_fixtures.sh  # fixed dates
#
# AUTH_MODE=off + DASHBOARD_FIXTURES=1, a throwaway sqlite DB, every upstream
# pointed at fixture:// (no network). Port 9029 is in dashboard's block
# (9026-9029, Rule 16/22); the runner refuses a busy port rather than
# wandering into a sibling's range. Local development ONLY.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
# Nothing from a developer's shell may point the fixture app at a live service.
unset CDR1_BASE_URL EPS_BASE_URL IPS_BASE_URL REQUEST_BASE_URL PLAN_BASE_URL ICD11_BASE_URL \
      CDR_ENDPOINTS GATEWAY_BASE_URL SSO_BASE_URL DATABASE_URL || true
export AUTH_MODE=off DASHBOARD_FIXTURES=1 FIXTURES_PORT="${FIXTURES_PORT:-9029}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$ROOT/app/.venv/bin/python" -m app.dashboard.fixtures
