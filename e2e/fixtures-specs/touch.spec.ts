/**
 * #974 step 23 — touch, on an EMULATED iPad (Playwright WebKit with
 * devices['iPad (gen 7)'] / '... landscape': 810x1080 / 1080x810, hasTouch,
 * isMobile). This is NOT the operator's real-iPad pass: no real Safari, no
 * finger, no pinch, no synthesised-hover quirk, no Magic Keyboard hot-plug.
 *
 * Layout: one column below 1000px (portrait), two from 1000px (landscape) —
 * the breakpoint in app/static/css/dashboard.css.
 */
import { test, expect } from '@playwright/test';
import path from 'node:path';
import { T2D, HBA1C_CODE, openDashboard, rows, detail, expectSeries, expectHba1cLimitFirst } from './helpers';

const SHOTS = path.resolve(__dirname, '../../results/phase6');

test('iPad: tap selects, detail persists, layout follows the 1000px breakpoint', async ({ page }, info) => {
  const landscape = info.project.name === 'ipad-landscape';
  await openDashboard(page, T2D);

  // ---- layout
  const vw = page.viewportSize()!.width;
  expect(landscape ? vw >= 1000 : vw < 1000).toBe(true);
  const cols = await page.locator('.dash-grid').evaluate((g) =>
    getComputedStyle(g).gridTemplateColumns.trim().split(/\s+/).length);
  expect(cols).toBe(landscape ? 2 : 1);
  const plan = (await page.locator('#panel-plan').boundingBox())!;
  const ev = (await page.locator('#panel-events').boundingBox())!;
  if (landscape) {
    expect(Math.abs(plan.y - ev.y)).toBeLessThan(2);          // side by side
    expect(ev.x).toBeGreaterThan(plan.x + plan.width - 1);
  } else {
    expect(Math.abs(plan.x - ev.x)).toBeLessThan(2);          // stacked
    expect(ev.y).toBeGreaterThan(plan.y + plan.height - 1);
  }
  // no horizontal page scroll
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  // ---- touch row height is the 44px target, not the 30px pointer density
  const h = (await rows(page).first().boundingBox())!.height;
  expect(h).toBeGreaterThanOrEqual(44);

  // ---- tap a disclosure: it opens and stays
  await page.locator('#eps-toggle').tap();
  await expect(page.locator('#eps-toggle')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#eps-detail')).toBeVisible();
  await page.locator('[data-role="chips"] .dash-chip').first().tap();
  await expect(page.locator('#dx-detail')).toContainText('5A11 Diabetes mellitus typ 2');

  // ---- filter, then TAP a row: one tap selects (no reveal-then-activate)
  await page.locator('[data-role="ev-code"]').selectOption(HBA1C_CODE);
  await expect(rows(page)).toHaveCount(8);
  const r0 = rows(page).nth(0);
  await r0.tap();
  await expect(r0).toHaveAttribute('aria-selected', 'true');
  await expectHba1cLimitFirst(page);
  await expectSeries(page, 'HbA1c');
  // the canvas lets a vertical finger-drag scroll the page
  await expect(page.locator('[data-role="ser-canvas"]')).toHaveCSS('touch-action', 'pan-y');

  // tapping elsewhere does not clear the selection
  await page.locator('#events-heading').tap();
  await expect(r0).toHaveAttribute('aria-selected', 'true');
  await expect(detail(page).locator('.lim-block')).toBeVisible();

  // ---- delivery-record screenshot, data loaded and a row selected
  await page.locator('#eps-detail [data-role="eps-close"]').tap();
  await page.locator('#dx-detail .dash-close').tap();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(SHOTS, `t2d-${info.project.name}.png`), fullPage: true });
});
