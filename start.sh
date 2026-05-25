#!/usr/bin/env bash
# ============================================================
# dashboard.pdhc — start.sh  (containerised, ticket #170 / Option C #157)
# App + DB both run as Docker containers (compose project dashboard_pdhc,
# pinned via .env COMPOSE_PROJECT_NAME; #113 renamed it from the old
# "current"). Container names are fixed dashboard_pdhc_app / _db.
#   - restart: unless-stopped -> survives reboots (#154)
#   - image carries its own Python -> immune to host brew upgrades (#153)
# Replaces the prior bare-metal gunicorn model.
# Health path is /healthz. NEVER kill -9 the Colima DB port (9026).
# ============================================================
set -uo pipefail
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_PORT=9027

info() { echo -e "\033[0;32m[dashboard]\033[0m $*"; }
err()  { echo -e "\033[0;31m[dashboard]\033[0m $*"; }
dc()   { docker-compose "$@"; }

info "=== dashboard.pdhc starting (containerised) ==="
unset DOCKER_HOST || true
docker context use colima >/dev/null 2>&1 || true
if ! docker info >/dev/null 2>&1; then
    err "Docker not running — run restart_all.sh first."
    exit 1
fi

cd "$SCRIPT_DIR"

# Legacy cleanup: stop any leftover bare-metal gunicorn holding :9027.
# (bare-metal pid file lived under ../shared/gunicorn.pid in the release layout)
for pf in "$SCRIPT_DIR/gunicorn.pid" "$SCRIPT_DIR/../../shared/gunicorn.pid" "$SCRIPT_DIR/../shared/gunicorn.pid"; do
    if [ -f "$pf" ]; then
        kill "$(cat "$pf")" 2>/dev/null || true
        rm -f "$pf"
        info "stopped legacy bare-metal gunicorn ($pf)"
    fi
done
# Belt-and-braces: a non-docker listener on 9027 only (never touch 9026).
for lp in $(lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN -t 2>/dev/null); do
    if ! ps -p "$lp" -o command= 2>/dev/null | grep -qi docker; then
        kill "$lp" 2>/dev/null || true
        info "freed stray non-docker listener on :$APP_PORT (pid $lp)"
    fi
done

info "bringing up db + app containers..."
dc up -d 2>&1 | tail -6

info "waiting for /healthz on 127.0.0.1:$APP_PORT ..."
ok=0
for i in $(seq 1 30); do
    if curl -sf -m 4 "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
done
if [ "$ok" = "1" ]; then
    info "=== dashboard.pdhc up (containerised) on 127.0.0.1:$APP_PORT ==="
    info "  app: docker container dashboard_pdhc_app (restart=unless-stopped)"
    info "  db:  docker container dashboard_pdhc_db  (port 9026, loopback)"
else
    err "NOT healthy after 60s. Last app logs:"
    dc logs --tail 30 app
    exit 1
fi
