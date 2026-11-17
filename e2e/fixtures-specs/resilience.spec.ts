/**
 * #974 step 23 — resilience IN THE BROWSER. One source down must cost one
 * warning, never the page. Each scenario starts its OWN fixture server on
 * 127.0.0.1:9028 (dashboard's block; the main run holds 9029) with
 * DASHBOARD_FIXTURES_FAIL=<source>, sequentially, and stops it afterwards.
 */
import { test, expect } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import net from 'node:net';
import path from 'node:path';
import { T2D, view, rows } from './helpers';

const ROOT = path.resolve(__dirname, '../..');
const PORT = 9028;
const BASE = `http://127.0.0.1:${PORT}`;

test.describe.configure({ mode: 'serial' });

function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const s = net.createServer().once('error', () => resolve(false))
      .once('listening', () => s.close(() => resolve(true))).listen(port, '127.0.0.1');
  });
}

async function startFailing(source: string): Promise<ChildProcess> {
  expect(await portFree(PORT), `${PORT} must be free for the failing-source server`).toBe(true);
  const child = spawn(path.join(ROOT, 'scripts/run_fixtures.sh'), [], {
    cwd: ROOT, stdio: 'ignore',
    env: { ...process.env, FIXTURES_PORT: String(PORT), DASHBOARD_FIXTURES_FAIL: source,
           DASHBOARD_FIXTURES_ANCHOR: process.env.DASHBOARD_FIXTURES_ANCHOR ?? '' },
  });
  const until = Date.now() + 45_000;
  for (;;) {
    try { if ((await fetch(`${BASE}/healthz`)).ok) { return child; } } catch { /* not up yet */ }
    if (child.exitCode !== null) { throw new Error(`fixture server (${source}) exited ${child.exitCode}`); }
    if (Date.now() > until) { child.kill('SIGTERM'); throw new Error('fixture server did not come up'); }
    await new Promise((r) => setTimeout(r, 250));
  }
}

async function stop(child: ChildProcess | null) {
  if (!child || child.exitCode !== null) { return; }
  const gone = new Promise((r) => child.once('exit', r));
  child.kill('SIGTERM');
  await Promise.race([gone, new Promise((r) => setTimeout(r, 5000))]);
  if (child.exitCode === null) { child.kill('SIGKILL'); }
}

/** The rest of the page still rendered. */
async function restRenders(page: import('@playwright/test').Page) {
  await expect(page.locator('[data-field="name"]')).toHaveText('Fixtur Typtvå Testsson');
  await expect(page.locator('#panel-plan .plan-group.is-overdue h3')).toHaveText('Försenade (2)');
  await expect(rows(page).first()).toBeVisible();
  await rows(page).first().click();
  await expect(page.locator('[data-role="ev-detail"] h3')).toBeVisible();
  await expect(page.locator('#panel-series [data-role="body"]')).toBeVisible();
}

test.describe('one failing source', () => {
  let child: ChildProcess | null = null;
  test.afterEach(async () => { await stop(child); child = null; });

  test('plan.pdhc down: the event list says names are missing; card, plan and list still render', async ({ page }) => {
    child = await startFailing('plan');
    await page.goto(BASE + view(T2D));
    const evBanners = page.locator('#panel-events [data-role="ev-banners"] .dash-banner');
    await expect(evBanners).toHaveText(['Namn kunde inte hämtas — koder visas i stället.']);
    // the consequence is visible: a code instead of a name
    await expect(rows(page).first()).toContainText('urn:pdhc:concept/');
    // and the card has no warning it should not have
    await expect(page.locator('#panel-card [data-role="banners"] .dash-banner')).toHaveCount(0);
    await expect(page.locator('[data-role="chips"] .dash-chip')).toHaveCount(2);
    await restRenders(page);
  });

  test('ips.pdhc down: spärr cannot be checked, so every patient read is REFUSED, per panel, in words', async ({ page }) => {
    // ips holds both the EPS and the spärr blocks. Since the strict spärr read
    // (fetch_active_blocks(strict=True)) the dashboard fails CLOSED: 503
    // `undetermined` on every patient route. The page must say so in each
    // panel — "could not be checked", not "you may not" and not "empty" —
    // and must not show any patient data.
    child = await startFailing('ips');
    await page.goto(BASE + view(T2D));
    const refused = /^Åtkomsten kunde inte kontrolleras just nu: spärr could not be verified/;
    for (const id of ['#panel-card', '#panel-plan', '#panel-events']) {
      const st = page.locator(`${id} [data-role="status"]`);
      await expect(st, id).toHaveText(refused);
      await expect(st, id).toHaveClass(/is-error/);
    }
    // card and plan never got data; the event list keeps its (empty) filter
    // form visible above the error, with no rows
    await expect(page.locator('#panel-card [data-role="body"]')).toBeHidden();
    await expect(page.locator('#panel-plan [data-role="body"]')).toBeHidden();
    // the shell is intact (headings, nav), nothing patient-specific leaked
    await expect(page.locator('h1')).toHaveText('Patientöversikt');
    await expect(page.locator('#plan-heading')).toHaveText('Plan');
    await expect(page.locator('[data-field="name"]')).toHaveText('—');
    await expect(rows(page)).toHaveCount(0);
    await expect(page.locator('#panel-series [data-role="status"]')).toHaveText('Välj en händelse för att visa dess tidsserie.');
  });
});
