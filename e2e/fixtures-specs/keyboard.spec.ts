/**
 * #974 step 23 — the same flow with the KEYBOARD only: Tab / Enter / arrows /
 * Esc. No mouse event is sent in this file.
 */
import { test, expect } from '@playwright/test';
import { T2D, openDashboard, rows, detail, expectSeries, expectHba1cLimitFirst, tabTo } from './helpers';

test('T2D by keyboard: disclosures open with Enter and close with Esc; rows select with arrows and Enter', async ({ page }) => {
  await openDashboard(page, T2D);
  const focused = () => page.evaluate(() => document.activeElement?.id || document.activeElement?.className || '');

  // ---- diagnosis chip: Tab to it, Enter opens, Esc closes and returns focus
  const chip = page.locator('[data-role="chips"] .dash-chip').first();
  await tabTo(page, chip);
  await expect(chip).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(chip).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#dx-detail')).toBeVisible();
  await expect(page.locator('#dx-detail')).toContainText('5A11 Diabetes mellitus typ 2');
  await page.keyboard.press('Escape');
  await expect(page.locator('#dx-detail')).toBeHidden();
  await expect(chip).toHaveAttribute('aria-expanded', 'false');
  await expect(chip).toBeFocused();

  // ---- EPS: Tab on to the toggle, Enter opens, Space works too, Esc closes
  const toggle = page.locator('#eps-toggle');
  await tabTo(page, toggle);
  await page.keyboard.press('Enter');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#eps-detail')).toBeVisible();
  await expect(page.locator('#eps-detail')).toContainText('Inga kända allergier');
  // the close button inside the region is reachable by Tab
  await page.keyboard.press('Tab');
  await expect(page.locator('[data-role="eps-close"]')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#eps-detail')).toBeHidden();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(toggle).toBeFocused();
  await page.keyboard.press('Space');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('Escape');
  await expect(page.locator('#eps-detail')).toBeHidden();

  // ---- filter by typing in the search box (debounced), then Tab to the list
  const q = page.locator('input[name="q"]');
  await tabTo(page, q);
  await page.keyboard.type('HbA1c');
  await expect(page.locator('[data-role="ev-filter-note"]')).toHaveText('Söker i 1 begrepp.');
  await expect(rows(page)).toHaveCount(8);
  await expect(page).toHaveURL(/[?&]q=HbA1c/);

  const list = page.locator('[data-role="ev-list"]');
  await tabTo(page, list);
  await expect(list).toBeFocused();
  expect(await focused()).toContain('ev-list');

  // ArrowDown from no selection selects the first row; detail + series follow
  await page.keyboard.press('ArrowDown');
  await expect(rows(page).nth(0)).toHaveAttribute('aria-selected', 'true');
  await expect(list).toHaveAttribute('aria-activedescendant', 'ev-row-0');
  await expectHba1cLimitFirst(page);
  await expectSeries(page, 'HbA1c');

  // arrows move the selection; End/Home jump
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(rows(page).nth(3)).toHaveAttribute('aria-selected', 'true');
  await expect(rows(page).nth(0)).toHaveAttribute('aria-selected', 'false');
  await expect(detail(page).locator('dt:text-is("Transaktion") + dd')).toHaveText('ej registrerat');
  await page.keyboard.press('End');
  await expect(rows(page).nth(7)).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Home');
  await expect(rows(page).nth(0)).toHaveAttribute('aria-selected', 'true');

  // Enter re-shows the selected row's detail (it was already there: stays)
  await page.keyboard.press('Enter');
  await expectHba1cLimitFirst(page);

  // Esc in the list clears the selection and does not leave focus
  await page.keyboard.press('Escape');
  await expect(list).not.toHaveAttribute('aria-activedescendant', /.+/);
  await expect(detail(page)).toContainText('Välj en rad för att se dess proveniens.');
  await expect(list).toBeFocused();
});

test('every interactive control on the card and the filters has a visible focus ring', async ({ page }) => {
  await openDashboard(page, T2D);
  for (const sel of ['[data-role="chips"] .dash-chip >> nth=0', '#eps-toggle', '[data-role="ev-list"]']) {
    const n = page.locator(sel);
    await tabTo(page, n);
    const outline = await n.evaluate((e) => getComputedStyle(e).outlineStyle + ' ' + getComputedStyle(e).outlineWidth);
    expect(outline, sel).toBe('solid 3px');
  }
});
