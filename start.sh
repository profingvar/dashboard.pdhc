#!/usr/bin/env bash
# Rule 16: single entry point.
#
# Robustness goals (2026-04-11):
#   - No interference with other services on this box.
#   - Survives SSH disconnect / terminal close (gunicorn runs as daemon,
#     not a foreground `flask run` waited on by the script).
#   - Bounded blast radius: touches ONLY dashboard.pdhc artifacts
#     (its own compose project, its own port 9027, its own pid file).
#   - Idempotent: safe to re-run to redeploy; gracefully stops the old
#     gunicorn first via its pid file before starting a new one.
#
# What it does, in order:
#   1. Load .env (DB creds, APP_PORT, etc.).
#   2. Ensure Colima/Docker is running (never touches other services' containers).
#   3. `docker compose up -d db` — brings up *only* dashboard_pdhc_db.
#   4. Gracefully stops any previous gunicorn for this service (pid file).
#   5. Kills any leftover listener on APP_PORT as a belt-and-braces fallback.
#   6. Activates venv, runs `flask db upgrade`.
#   7. Starts gunicorn as a daemon bound to 127.0.0.1:APP_PORT.
#   8. Polls /healthz until 200 (or fails loudly with log tail).
#
# NOTE: binds to 127.0.0.1 on purpose — external traffic arrives via the
# reverse proxy. Other pdhc services (gateway, sso, ips, cgm, cdr, ...)
# follow the same convention.

set -euo pipefail

# macOS ObjC fork-safety: CoreFoundation in parent poisons fork()s; setting
# this env var before gunicorn prevents the SIGKILL spiral after worker recycles.
# See feedback memory "gunicorn SIGKILL spiral on macOS = fork-safety, not OOM".
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

cd "$(dirname "$0")"
ROOT="$(pwd -P)"  # physical path: resolves current/ symlink to its target release

# --- paths for cross-release persistent state --------------------------------
# `current` is a symlink; keep pid/logs in dashboard.pdhc/shared/ so they
# survive release swaps. We prefer an absolute shared dir next to releases/
# rather than $ROOT/../shared because $ROOT is the physical release path,
# so ".." points inside releases/, not beside it.
_deploy_root="$(cd "$ROOT/../.." && pwd -P 2>/dev/null || echo "")"
if [ -n "$_deploy_root" ] && [ -d "$_deploy_root/releases" ] && [ -L "$_deploy_root/current" ]; then
  mkdir -p "$_deploy_root/shared/logs"
  SHARED="$_deploy_root/shared"
else
  mkdir -p "$ROOT/results/logs"
  SHARED="$ROOT/results"
fi
PID_FILE="$SHARED/gunicorn.pid"
ACCESS_LOG="$SHARED/logs/gunicorn.access.log"
ERROR_LOG="$SHARED/logs/gunicorn.error.log"

# Pin the compose project name so release swaps (which change the cwd dir
# name) can't accidentally create a parallel compose project with a second
# dashboard_pdhc_db container. Value is the name the live resources are
# already tagged with on the macmini (see `docker inspect dashboard_pdhc_db`);
# changing this would orphan the running volume. TODO: migrate to a cleaner
# name like `dashboard_pdhc` in a planned maintenance window (dump → rename
# volume → restore → redeploy).
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-current}"

# --- 1. env ------------------------------------------------------------------
if [ ! -f .env ]; then
  echo "ERROR: .env not found in $ROOT" >&2
  exit 1
fi
echo "==> loading .env"
set -a; . ./.env; set +a
APP_PORT="${APP_PORT:-9027}"

# --- 2. docker/colima --------------------------------------------------------
echo "==> ensuring Docker is running (via Colima)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
unset DOCKER_HOST || true

ensure_docker() {
  docker context use colima >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then
    echo "  Docker already running (context=colima)"
    return 0
  fi
  # Never stop/delete the VM — it hosts every other service's containers.
  if ! colima status >/dev/null 2>&1; then
    echo "  Colima not running. Starting (this may take a minute)..."
    colima start --cpu 4 --memory 8 >/dev/null 2>&1 || true
    docker context use colima >/dev/null 2>&1 || true
    for i in $(seq 1 20); do
      if docker info >/dev/null 2>&1; then
        echo "  Docker up after Colima start (attempt $i)"
        return 0
      fi
      sleep 2
    done
  fi
  echo "ERROR: Docker not responding. Run 'colima status' / 'docker context use colima' manually." >&2
  return 1
}
ensure_docker

DC="docker compose"
if command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
fi

# --- 3. bring up only our DB (compose project is isolated) -------------------
echo "==> bringing up Postgres (9026) — compose project dashboard.pdhc only"
$DC up -d db
# pg_isready loops with a hard ceiling so we don't spin forever
for i in $(seq 1 30); do
  if docker exec dashboard_pdhc_db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    echo "  Postgres ready (attempt $i)"
    break
  fi
  sleep 1
  if [ "$i" = "30" ]; then
    echo "ERROR: Postgres did not become ready after 30s" >&2
    exit 1
  fi
done

# --- 4. graceful stop of previous gunicorn -----------------------------------
if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "==> stopping previous gunicorn (pid $OLD_PID)"
    kill -TERM "$OLD_PID" 2>/dev/null || true
    for i in $(seq 1 10); do
      kill -0 "$OLD_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "  still running after 10s, sending KILL"
      kill -KILL "$OLD_PID" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
fi

# --- 5. belt-and-braces: free APP_PORT if anything else still holds it -------
# Only kills processes on 9027 (our port). Other services use 9026/9028/9029
# for DB/reserved; we do NOT touch those.
LEFTOVER_PIDS="$(lsof -ti tcp:"$APP_PORT" 2>/dev/null || true)"
if [ -n "$LEFTOVER_PIDS" ]; then
  echo "==> killing leftover listeners on :$APP_PORT → $LEFTOVER_PIDS"
  kill -9 $LEFTOVER_PIDS 2>/dev/null || true
fi

# --- 6. venv + migrations ----------------------------------------------------
if [ ! -f "app/.venv/bin/activate" ]; then
  echo "ERROR: app/.venv missing. Run: python3 -m venv app/.venv && app/.venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
echo "==> activating venv"
# shellcheck disable=SC1091
. app/.venv/bin/activate

echo "==> running migrations"
FLASK_APP=app:create_app flask db upgrade

# --- 7. start gunicorn (daemon) ---------------------------------------------
echo "==> starting gunicorn on 127.0.0.1:${APP_PORT} (daemon)"
FLASK_APP=app:create_app gunicorn \
  --bind "127.0.0.1:${APP_PORT}" \
  --workers 2 \
  --timeout 120 \
  --graceful-timeout 30 \
  --max-requests 500 \
  --max-requests-jitter 50 \
  --daemon \
  --pid "$PID_FILE" \
  --access-logfile "$ACCESS_LOG" \
  --error-logfile "$ERROR_LOG" \
  "app:create_app()"

# --- 8. smoke test -----------------------------------------------------------
echo "==> polling /healthz"
for i in $(seq 1 20); do
  code="$(curl -sS -o /tmp/dashboard_healthz.out -w '%{http_code}' "http://127.0.0.1:${APP_PORT}/healthz" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then
    echo "  /healthz → 200 $(cat /tmp/dashboard_healthz.out)"
    break
  fi
  sleep 1
  if [ "$i" = "20" ]; then
    echo "ERROR: /healthz never came up (last=$code). Last error log:" >&2
    tail -40 "$ERROR_LOG" >&2 || true
    exit 1
  fi
done

echo "==> dashboard.pdhc up. pid=$(cat "$PID_FILE" 2>/dev/null || echo ?), logs=$SHARED/logs/"
