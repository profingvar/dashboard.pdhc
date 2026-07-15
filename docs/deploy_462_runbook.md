# Deploy runbook — #462 clinical dashboard (dashboard.pdhc + CDR1)

Prepared 2026-07-15. **Reviewed state, NOT executed.** Deploys the merged #462
work to prod. Two services: **dashboard.pdhc** (app on 9027) and **cdr.pdhc =
CDR1** (app on 9046). Both containerised on Colima.

## Verified preconditions (2026-07-15, read-only)
- Merged to main: dashboard `3281af3` (PR #1), cdr.pdhc `a920c14` (PR #1).
- Running: `dashboard_pdhc_app` (9027), `cdr_pdhc_app` = CDR1 (9046); both up,
  DBs `dashboard_pdhc_db` / `cdr_pdhc_db` healthy. `docker compose` v2 (5.1.4).
- **`DASHBOARD_PDHC_SERVICE_KEY` MATCHES** in dashboard `.env` and cdr1 `.env`
  (len 32) → no service-key work.
- **cdr.pdhc prod** = git checkout on `main`, HEAD `5350a73`, **5 behind / 0
  ahead of origin/main, no modified tracked files** (only untracked `*.bak`
  clutter) → a fast-forward `git pull` is clean.
- **dashboard prod** = release-symlink layout, `current ->
  releases/2026-07-10T06-54-37Z-x1`, `.env` at `current/.env`. NOT git.
- **Dockerfiles `COPY . .`** → `--build` is MANDATORY on every deploy (a plain
  `up -d` reuses the old baked image and silently runs old code).

## STEP 0 — env changes (operator; edit `.env`, do not commit)
1. **dashboard** `/usr/local/www/dashboard.pdhc/current/.env` — ADD:
   ```
   CDR1_BASE_URL=http://host.docker.internal:9046
   ```
   NOT `https://cdr.pdhc.se` — from inside the box that hairpins through
   Cloudflare (caused the 2026-05-09 502 storm). Use the loopback host, exactly
   like the gateway→cdr1 forwarder. If `host.docker.internal` doesn't resolve in
   the container, add to the app service in `current/docker-compose.yml`:
   `extra_hosts: ["host.docker.internal:host-gateway"]`.
2. **cdr1** `/usr/local/www/cdr.pdhc/cdr_app/.env` — ADD (OPTIONAL, for concept
   display names / #471.5):
   ```
   PLAN_BASE_URL=http://host.docker.internal:9030
   ```
   Omit and charts show the raw `code_canonical` instead of names (fail-open,
   safe). Not required for the first smoke.

## STEP 1 — deploy CDR1 (git pull + rebuild)
```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
docker context use colima
cd /usr/local/www/cdr.pdhc
git status --short          # MUST show no ' M' tracked edits; '?? *.bak' is fine
git pull --ff-only origin main      # 5350a73 -> a920c14
cd cdr_app
docker compose up -d --build app    # --build mandatory (COPY . .)
# verify new code is in the running image:
docker exec cdr_pdhc_app python -c "import app.api.clinical_read; print('clinical_read OK')"
curl -fsS http://127.0.0.1:9046/api/v1/health
# smoke the care-delivery endpoint (needs the cdr1 DASHBOARD key value):
KEY=$(grep '^DASHBOARD_PDHC_SERVICE_KEY=' /usr/local/www/cdr.pdhc/cdr_app/.env | cut -d= -f2-)
curl -s -H 'X-Source-Service: dashboard.pdhc' -H "X-Service-Key: $KEY" \
     -H 'X-Access-Purpose: care-delivery' -H 'X-Is-Admin: 1' \
     http://127.0.0.1:9046/api/v1/clinical/patients | head -c 300; echo
```
cdr.pdhc has **no schema change** — no migration needed.

## STEP 2 — deploy dashboard (new release + rebuild + MIGRATE)
Staged source tarball (merged main, no `.env`/venv/.git):
`dashboard-pdhc-3281af3.tgz` (build it fresh if preferred:
`git archive --format=tar.gz -o dashboard-3281af3.tgz HEAD` from local main).
```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd /usr/local/www/dashboard.pdhc
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
mkdir -p releases/$TS && tar xzf ~/dashboard-pdhc-3281af3.tgz -C releases/$TS
cp current/.env releases/$TS/.env            # carry the live .env forward (incl. STEP 0 edit)
# (if compose had a manual extra_hosts edit, re-apply it to releases/$TS/docker-compose.yml)
OLD=$(readlink current)                       # = releases/2026-07-10T06-54-37Z-x1 (rollback target)
ln -snf releases/$TS current
cd current
docker compose up -d --build app             # --build mandatory
# MIGRATION — saved_design (rev sd071322aa01):
docker exec -e FLASK_APP=app:create_app dashboard_pdhc_app flask db upgrade
docker exec -e FLASK_APP=app:create_app dashboard_pdhc_app flask db heads   # expect: sd071322aa01 (head)
curl -fsS http://127.0.0.1:9027/healthz
```

## STEP 3 — live smoke (the real test — chart JS runs in a browser for the 1st time)
Log in via SSO as a **professional with an affiliation** (care-delivery auth now
requires a care-unit affiliation, NOT the analysis phase):
1. `https://dashboard.pdhc.se/select` lists real patients (org-scoped).
2. Open one → `https://dashboard.pdhc.se/patient/<guid>/charts` renders CDR1
   data. Check: parameter dropdown sorted by count; add up to 3 diagrams; mirror
   a 2nd parameter (dual axis); time slider (1d–5y); zero/y-max; **save a design
   then load it**.
3. If `PLAN_BASE_URL` was set (STEP 0.2): dropdown/legend show display names;
   otherwise raw `urn:pdhc:concept/...` codes.

## Rollback (operator)
- **dashboard**: `ln -snf "$OLD" current && cd current && docker compose up -d
  --build app`. The `saved_design` table is additive — safe to leave in place.
- **CDR1**: `cd /usr/local/www/cdr.pdhc && git checkout 5350a73 && cd cdr_app &&
  docker compose up -d --build app`. (`git checkout` of the pre-deploy commit;
  avoid `git reset --hard` per platform rule §14.)

## Notes / risks
- **Both paths stay live**: legacy `/patient` + ObservationCache are untouched;
  the new view is additive. Retire the legacy surface only after this smoke
  passes (#471 item 1, Q6 = CDR1-only).
- **Auth change is the biggest behavioural shift**: the dashboard front door is
  now care-delivery (affiliation), not analysis-phase. A previously-working
  analysis operator with no affiliation would now be 403 on `/select`. Confirm
  the smoke operator has an affiliation.
- **Do NOT touch CDR2–5** — only CDR1 needs the clinical read surface. The code
  is harmless on the others (CDR_READ_LOCKDOWN already admits dashboard on
  `/api/v1/clinical`) but there's no reason to redeploy them.
- Never `colima stop/restart`, never `docker compose down -v`. Only the two
  `app` services are rebuilt; DBs and volumes are untouched.
