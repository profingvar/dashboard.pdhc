/**
 * #974 step 23 — delivery-record screenshot of the T2D page on desktop
 * (the iPad ones are taken in touch.spec.ts, per project).
 */
import { test, expect } from '@playwright/test';
import path from 'node:path';
import { T2D, HBA1C_CODE, openDashboard, rows, expectSeries } from './helpers';

test('screenshot: T2D desktop, HbA1c row selected', async ({ page }) => {
  await openDashboard(page, T2D);
  await page.locator('[data-role="ev-code"]').selectOption(HBA1C_CODE);
  await expect(rows(page)).toHaveCount(8);
  await rows(page).nth(0).click();
  await expectSeries(page, 'HbA1c');
  await expect(page.locator('[data-role="ev-detail"] .lim-block')).toBeVisible();
  await page.mouse.move(0, 0);
  await page.screenshot({ path: path.resolve(__dirname, '../../results/phase6/t2d-desktop.png'), fullPage: true });
});
