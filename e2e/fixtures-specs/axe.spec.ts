/**
 * #974 step 23 — axe-core (pinned @axe-core/playwright 4.10.2). No rule is
 * disabled. A serious or critical violation fails the run.
 */
import { test, expect, type Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import AxeBuilder from '@axe-core/playwright';
import { T2D, HF, CGM, HBA1C_CODE, openDashboard, rows, expectHba1cLimitFirst, expectSeries } from './helpers';

async function scan(page: Page, label: string) {
  const res = await new AxeBuilder({ page }).analyze();
  // the full result per state, for the delivery record (minor/moderate included)
  const out = path.resolve(__dirname, '../../results/phase6', `axe-${label.replace(/\W+/g, '-')}.json`);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(res.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.map((n) => ({ target: n.target, summary: n.failureSummary })) })), null, 2));
  const bad = res.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  const all = res.violations.map((v) => `${v.impact} ${v.id} (${v.nodes.length})`);
  test.info().annotations.push({ type: `axe ${label}`, description: all.join('; ') || 'no violations' });
  const report = bad.map((v) => `${v.impact} ${v.id}: ${v.help}\n` +
    v.nodes.slice(0, 5).map((n) => `   ${n.target.join(' ')} — ${n.failureSummary?.split('\n').join(' ')}`).join('\n')).join('\n');
  expect(bad, `${label}:\n${report}`).toEqual([]);
}

test('axe: initial state', async ({ page }) => {
  await openDashboard(page, T2D);
  await scan(page, 'initial');
});

test('axe: disclosures open, a row selected (limit + missing context shown), series drawn', async ({ page }) => {
  await openDashboard(page, T2D);
  await page.locator('#eps-toggle').click();
  await expect(page.locator('#eps-detail')).toBeVisible();
  await page.locator('[data-role="chips"] .dash-chip').first().click();
  await expect(page.locator('#dx-detail')).toBeVisible();
  await page.locator('[data-role="ev-code"]').selectOption(HBA1C_CODE);
  await expect(rows(page)).toHaveCount(8);
  await rows(page).nth(0).click();
  await expectHba1cLimitFirst(page);
  await expectSeries(page, 'HbA1c');
  await scan(page, 'selected row with limit');
  // a row without context: the "ej registrerat" cells are on screen
  await rows(page).nth(3).click();
  await expect(page.locator('[data-role="ev-detail"] .is-missing').first()).toBeVisible();
  await scan(page, 'selected row without context');
});

// The other two fixture patients exercise other markup: the spärr banner and an
// EPS-only ("ej förankrad") chip (HF), and the LTTB note on a dense series (CGM).
for (const [name, guid] of [['heart failure', HF], ['CGM', CGM]] as const) {
  test(`axe: ${name} patient, first row selected`, async ({ page }) => {
    await openDashboard(page, guid);
    await page.locator('#eps-toggle').click();
    await rows(page).nth(0).click();
    await expect(page.locator('#panel-series [data-role="body"]')).toBeVisible();
    await expect(page.locator('[data-role="ev-detail"] h3')).toBeVisible();
    await scan(page, `${name} selected`);
  });
}
