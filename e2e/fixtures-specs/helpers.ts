/** Shared constants and helpers for the #974 fixture e2e run (synthetic data only). */
import { expect, type Page, type Locator } from '@playwright/test';

export const T2D = 'fa4e0974-000a-4000-8000-000000000001';
export const HF = 'fa4e0974-000a-4000-8000-000000000002';
export const CGM = 'fa4e0974-000a-4000-8000-000000000003';
export const HBA1C_CODE = 'urn:pdhc:concept/fa4e0974-000c-4000-8000-000000000001';
export const CP_T2D = 'fa4e0974-000e-4000-8000-000000000001';

export const view = (guid: string, qs = '') => `/dashboard/view/${guid}${qs}`;

/** Open the page and wait until the card, plan and event list have rendered. */
export async function openDashboard(page: Page, guid: string, qs = '') {
  await page.goto(view(guid, qs));
  await expect(page.locator('#panel-card')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('#panel-plan [data-role="body"]')).toBeVisible();
  await expect(rows(page).first()).toBeVisible();
}

export const rows = (page: Page): Locator => page.locator('[data-role="ev-list"] [role="option"]');
export const detail = (page: Page): Locator => page.locator('[data-role="ev-detail"]');

/** The series panel has drawn a chart for the given concept name. */
export async function expectSeries(page: Page, name: string) {
  const panel = page.locator('#panel-series');
  await expect(panel.locator('[data-role="ser-name"]')).toHaveText(name);
  await expect(panel.locator('canvas[data-role="ser-canvas"]')).toHaveAttribute('aria-label', new RegExp('^' + name + ': \\d+ punkter'));
  await expect.poll(() => page.evaluate(() => {
    const c = document.querySelector('[data-role="ser-canvas"]') as HTMLCanvasElement;
    return !!(window as any).Chart?.getChart(c);
  })).toBe(true);
}

/** The x-range of the series chart, [min, max] in ms. */
export function xRange(page: Page): Promise<[number, number]> {
  return page.evaluate(() => {
    const c = document.querySelector('[data-role="ser-canvas"]') as HTMLCanvasElement;
    const ch = (window as any).Chart.getChart(c);
    return [ch.scales.x.min, ch.scales.x.max] as [number, number];
  });
}

/** The limit block is the FIRST thing after the heading, and names its source. */
export async function expectHba1cLimitFirst(page: Page) {
  const d = detail(page);
  await expect(d.locator('h3')).toHaveText('HbA1c');
  const first = d.locator(':scope > :nth-child(2)');
  await expect(first).toHaveClass(/lim-block/);
  await expect(first).toHaveClass(/is-outside/);
  await expect(first.locator('.lim-status')).toHaveText('▲ Gräns: över gräns');
  await expect(first).toContainText('Gräns ≤ 52 mmol/mol');
  await expect(first).toContainText(
    `Gränsen kommer från: Åtgärdsgräns i patientens vårdplan ${CP_T2D} (satt av beställaren) — HbA1c högst 52`);
}

/** Press Tab until `target` has focus (bounded, so a broken order fails rather than hangs). */
export async function tabTo(page: Page, target: Locator, max = 60) {
  for (let i = 0; i < max; i++) {
    if (await target.evaluate((n) => n === document.activeElement)) { return; }
    await page.keyboard.press('Tab');
  }
  throw new Error('focus never reached the target by Tab');
}
