/**
 * #1014: patient-generated data is marked for the professional — list badge,
 * detail note + Ursprung row, hollow chart points with a legend line — and the
 * page stays axe-clean (wcag2a/aa) with the marked row selected.
 */
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import path from 'node:path';
import { T2D, openDashboard, rows } from './helpers';

test('#1014 patient-generated EQ VAS: badge, detail, axe', async ({ page }) => {
  await openDashboard(page, T2D);
  await page.locator('[data-role="ev-code"]').selectOption('urn:pdhc:concept/fa4e0974-000c-4000-8000-000000000005');
  await expect(rows(page).first().locator('.origin-badge')).toBeVisible();
  await rows(page).nth(0).click();
  await expect(page.locator('[data-role="ev-detail"] .origin-note')).toBeVisible();
  await expect(page.locator('[data-role="ser-legend"]')).toContainText('Ofylld punkt = patientgenererad');
  await page.mouse.move(0, 0);
  await page.screenshot({ path: path.resolve(__dirname, '../../results/phase6/origin-1014.png'), fullPage: true });
  const r = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(r.violations.map(v => v.id + ':' + v.nodes.length)).toEqual([]);
});
