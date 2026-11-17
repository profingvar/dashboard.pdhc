/**
 * #974 Phase 6 step 23 — Playwright e2e + axe against the SYNTHETIC fixture
 * runner (scripts/run_fixtures.sh), never against a deployed host.
 *
 *   cd e2e && npm run test:fixtures
 *
 * The production config (playwright.config.ts, specs/) is separate and
 * untouched. Everything here runs on 127.0.0.1 with AUTH_MODE=off and
 * DASHBOARD_FIXTURES=1; the runner points every upstream at fixture://.
 *
 * ANCHOR. The fixture dates are relative to DASHBOARD_FIXTURES_ANCHOR. It is
 * pinned ONCE per run (to today 00:00 UTC unless the caller sets it), and the
 * same value goes to every server the run starts, so a run is deterministic.
 * It is deliberately not a literal date: request_client judges planned vs
 * overdue against the REAL clock, so a literal anchor would turn the
 * "planned" order (anchor + 21 d) overdue three weeks later and the suite
 * would rot. The specs assert no calendar dates.
 */
import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.FIXTURES_PORT ?? 9029);
const ANCHOR = process.env.DASHBOARD_FIXTURES_ANCHOR
  ?? new Date().toISOString().slice(0, 10) + 'T00:00:00+00:00';
process.env.DASHBOARD_FIXTURES_ANCHOR = ANCHOR;   // seen by the resilience spec's own servers
const BASE = `http://127.0.0.1:${PORT}`;
// The default browser cache (~/Library/Caches/ms-playwright) is root-owned on
// this Mac and holds builds for an older Playwright; 1.59's builds live in a
// user-writable cache. `npm run test:fixtures` sets this; the line below
// covers a bare `npx playwright test --config ...` (workers inherit it).
process.env.PLAYWRIGHT_BROWSERS_PATH ??= path.join(process.env.HOME ?? '', 'Library/Caches/ms-playwright-user');
if (!/^http:\/\/127\.0\.0\.1:/.test(BASE)) { throw new Error('fixtures config must target 127.0.0.1'); }

export default defineConfig({
  testDir: './fixtures-specs',
  outputDir: '../results/phase6/test-output',
  fullyParallel: false,
  workers: 2,
  retries: 0,                       // a flake must show, not be retried away
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { outputFolder: '../results/phase6/playwright-report', open: 'never' }]],
  use: {
    baseURL: BASE,
    locale: 'sv-SE',
    timezoneId: 'Europe/Stockholm',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `${ROOT}/scripts/run_fixtures.sh`,
    cwd: ROOT,
    url: `${BASE}/healthz`,
    // Never reuse: a developer's server may run another anchor or failing set.
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: 'ignore',
    stderr: 'pipe',
    env: {
      FIXTURES_PORT: String(PORT),
      DASHBOARD_FIXTURES_ANCHOR: ANCHOR,
      DASHBOARD_FIXTURES_FAIL: '',
    },
  },
  projects: [
    {
      name: 'desktop-chromium',
      testIgnore: /touch\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1366, height: 900 } },
    },
    {
      name: 'ipad-portrait',
      testMatch: /touch\.spec\.ts/,
      use: { ...devices['iPad (gen 7)'] },            // WebKit, 810x1080, touch
    },
    {
      name: 'ipad-landscape',
      testMatch: /touch\.spec\.ts/,
      use: { ...devices['iPad (gen 7) landscape'] },  // WebKit, 1080x810, touch
    },
  ],
});
