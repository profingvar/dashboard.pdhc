/**
 * #974 step 23 — the dashboard flow on the T2D fixture patient, by pointer.
 *
 * Gate 0: SELECTION is the mechanism (no pin). The old "hover card -> hover
 * row -> series -> pin -> zoom" is: open dashboard -> open the EPS and a
 * diagnosis disclosure -> select a row -> detail persists and the series
 * shows that concept -> zoom -> URL filters survive a reload.
 */
import { test, expect } from '@playwright/test';
import { T2D, HBA1C_CODE, openDashboard, rows, detail, expectSeries, xRange, expectHba1cLimitFirst } from './helpers';

test('T2D: card, plan, select a row, detail persists, series, zoom, filter survives reload', async ({ page }) => {
  await openDashboard(page, T2D);

  // ---- card: synthetic identity, diagnosis chips with their ICD-11 labels
  await expect(page.locator('[data-field="name"]')).toHaveText('Fixtur Typtvå Testsson');
  const chips = page.locator('[data-role="chips"] .dash-chip');
  await expect(chips).toHaveCount(2);
  await expect(chips.nth(0)).toHaveText(/^5A11\s*Diabetes mellitus typ 2$/);
  await expect(chips.nth(1)).toHaveText(/^BA00\s*Essentiell hypertoni$/);

  // ---- plan: the overdue order is shown as overdue, in words
  const overdue = page.locator('.plan-group.is-overdue');
  await expect(overdue.locator('h3')).toHaveText('Försenade (2)');
  await expect(overdue).toContainText('Systoliskt blodtryck');
  await expect(overdue).toContainText('Diastoliskt blodtryck');
  await expect(page.locator('.plan-group.is-done h3')).toHaveText('Klara (2)');
  await expect(page.locator('.plan-group.is-planned')).toContainText('Självskattad hälsa (EQ VAS)');

  // ---- EPS disclosure: opens on click and STAYS open
  const toggle = page.locator('#eps-toggle');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#eps-detail')).toBeVisible();
  await expect(page.locator('#eps-detail')).toContainText('Inga kända allergier');
  await page.mouse.move(5, 5);
  await expect(page.locator('#eps-detail')).toBeVisible();      // no close on mouseleave

  // ---- diagnosis disclosure
  await chips.nth(0).click();
  await expect(chips.nth(0)).toHaveAttribute('aria-expanded', 'true');
  const dx = page.locator('#dx-detail');
  await expect(dx).toBeVisible();
  await expect(dx).toContainText('5A11 Diabetes mellitus typ 2');
  await expect(dx).toContainText('ICD-11 MMS');
  await expect(dx).toContainText('Patientsammanfattningen, härledd ur cdr2');

  // ---- filter to HbA1c via the concept select; it goes into the URL
  await page.locator('[data-role="ev-code"]').selectOption(HBA1C_CODE);
  await expect(page).toHaveURL(/[?&]code=urn%3Apdhc%3Aconcept%2Ffa4e0974-000c-4000-8000-000000000001/);
  await expect(rows(page)).toHaveCount(8);
  await expect(page.locator('[data-role="ev-count"]')).toHaveText('8 händelser (filtrerat)');

  // the latest HbA1c (53) is above the goal: marked by SYMBOL and WORD, not colour only
  const r0 = rows(page).nth(0);
  await expect(r0.locator('.lim-flag')).toHaveText('▲ över');
  await expect(r0).toContainText('53 mmol/mol');
  await expect(rows(page).locator('.lim-flag')).toHaveCount(5);   // 53 59 55 57 54 above, 50 51 49 inside

  // ---- select by click: detail shows the limit FIRST with its source text
  await r0.click();
  await expect(r0).toHaveAttribute('aria-selected', 'true');
  await expectHba1cLimitFirst(page);
  const ddAfter = (label: string) => detail(page).locator(`dt:text-is("${label}") + dd`);
  await expect(ddAfter('Transaktion')).toHaveText('fa4e0974-000d-4000-8000-000000000001');
  await expectSeries(page, 'HbA1c');

  // ---- a row WITHOUT transaction context: "ej registrerat", named, not dropped
  const r3 = rows(page).nth(3);                // 57, 2025-11, predates the order
  await expect(r3).toContainText('57 mmol/mol');
  await r3.click();
  await expect(r3).toHaveAttribute('aria-selected', 'true');
  await expect(ddAfter('Transaktion')).toHaveText('ej registrerat');
  await expect(ddAfter('Transaktion')).toHaveClass(/is-missing/);
  await expect(ddAfter('Beställning registrerad på mätvärdet')).toHaveText('ej registrerat');
  await expect(detail(page)).toContainText('Ingen kontextrad hittades för mätvärdet i CDR1.');

  // ---- the detail PERSISTS: moving the pointer away / clicking elsewhere does not clear it
  await page.mouse.move(2, 2);
  await page.locator('#events-heading').click();
  await page.waitForTimeout(500);              // past the 300 ms hover-preview delay
  await expect(detail(page)).toContainText('Ingen kontextrad hittades');
  await expect(detail(page)).not.toHaveClass(/is-preview/);
  await expect(r3).toHaveAttribute('aria-selected', 'true');

  // ---- the selected point is the chart's diamond; then ZOOM (ctrl+wheel = the plugin's wheel zoom)
  const [min0, max0] = await xRange(page);
  const canvas = page.locator('[data-role="ser-canvas"]');
  await canvas.scrollIntoViewIfNeeded();          // a wheel off-viewport reaches nothing
  const box = (await canvas.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.keyboard.down('Control');
  for (let i = 0; i < 4; i++) { await page.mouse.wheel(0, -200); }
  await page.keyboard.up('Control');
  await expect.poll(async () => { const [a, b] = await xRange(page); return b - a; })
    .toBeLessThan((max0 - min0) * 0.8);
  expect(await page.evaluate(() =>
    (window as any).Chart.getChart(document.querySelector('[data-role="ser-canvas"]')).isZoomedOrPanned())).toBe(true);
  // programmatic zoom on the instance works as well, and reset restores the overview
  await page.evaluate(() => {
    const ch = (window as any).Chart.getChart(document.querySelector('[data-role="ser-canvas"]'));
    ch.resetZoom('none'); ch.zoom(1.5, 'none');
  });
  const [min1, max1] = await xRange(page);
  expect(max1 - min1).toBeLessThan(max0 - min0);

  // ---- reload: the URL filter is re-applied
  await page.reload();
  await expect(page.locator('[data-role="ev-code"]')).toHaveValue(HBA1C_CODE);
  await expect(rows(page)).toHaveCount(8);
  await expect(page.locator('[data-role="ev-count"]')).toHaveText('8 händelser (filtrerat)');
  await expect(rows(page).nth(0).locator('.lim-flag')).toHaveText('▲ över');
});

test('T2D: a free-text filter persists in the URL across reload', async ({ page }) => {
  await openDashboard(page, T2D, '?q=HbA1c');
  await expect(page.locator('input[name="q"]')).toHaveValue('HbA1c');
  await expect(page.locator('[data-role="ev-filter-note"]')).toHaveText('Söker i 1 begrepp.');
  await expect(rows(page)).toHaveCount(8);
  await page.reload();
  await expect(page.locator('input[name="q"]')).toHaveValue('HbA1c');
  await expect(rows(page)).toHaveCount(8);
});

test('T2D: a plan filter persists in the URL across reload', async ({ page }) => {
  await openDashboard(page, T2D);
  const sel = page.locator('[data-role="ev-plan"]');
  await expect(sel.locator('option')).toHaveCount(3);          // Alla, the T2D order, unordered
  await sel.selectOption({ index: 1 });
  const key = await sel.inputValue();
  expect(key).toMatch(/^fa4e0974-000[be]-/);         // order or care-plan guid
  await expect(page).toHaveURL(new RegExp(`[?&]plan=${key}`));
  await expect(page.locator('[data-role="ev-count"]')).toContainText('(filtrerat)');
  const n = await rows(page).count();
  await page.reload();
  await expect(sel).toHaveValue(key);
  await expect(page.locator('[data-role="ev-count"]')).toContainText('(filtrerat)');
  await expect(rows(page)).toHaveCount(n);
  await expect(page.locator('[data-role="ev-filter-note"]')).not.toContainText('Ogiltigt filter');
});

test('T2D: a URL code that is not one of the patient\'s concepts is refused by name, not dropped', async ({ page }) => {
  await page.goto('/dashboard/view/' + T2D + '?code=urn:pdhc:concept/fa4e0974-000c-4000-8000-0000000000ff');
  await expect(page.locator('[data-role="ev-filter-note"]')).toHaveText(
    /^Ogiltigt filter i adressen \(code=urn:pdhc:concept\/fa4e0974-000c-4000-8000-0000000000ff\)/);
  await expect(rows(page)).toHaveCount(0);
});
