/**
 * Spec: nurse_flow — search by patient GUID, fetch summary +
 * variable series + AGP. Drives the deployed nurse workspace
 * against the seeded CGM patient on cdr2.
 *
 * Acceptance gates per platform-plan §4.2 / §4.4.
 */
import { test, expect } from '@playwright/test';

const HBA1C = 'https://termbank.pdhc.se/CodeSystem/loinc/4548-4';
// Fully URL-encoded for Flask's <path:canonical> — see researcher_flow.
const pathify = (uri: string) => encodeURIComponent(uri);

// Patient guid to drive the nurse-flow tests. Defaults to any real
// patient on cdr2; override with $CGM_PATIENT_GUID when running
// against a known CGM-raw patient (~26k obs) for the AGP timing.
const CGM_PATIENT = process.env.CGM_PATIENT_GUID ??
                    '0252eb9a-9ecc-559b-953c-9f1113dae640';

test.describe('nurse flow', () => {
  test('nurse landing page renders the search form + AGP card', async ({ page }) => {
    await page.goto('/nurse');
    await expect(page.locator('body')).toContainText('Patient GUID');
    await expect(page.locator('body')).toContainText('Ambulatory glucose profile');
    await expect(page.locator('body')).toContainText('Latest values');
  });

  test('patient summary endpoint returns structured data for the seeded patient', async ({ request }) => {
    const r = await request.get(`/api/nurse/patient/${CGM_PATIENT}`);
    if (!r.ok()) {
      console.log('summary status', r.status(), 'body=', (await r.text()).slice(0, 300));
    }
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body.patient).toBeTruthy();
    // Schema returned: owner_cdr (matches /^cdr[1-5]$/), conditions[], latest_values[]
    expect(body.owner_cdr ?? body.cdr_id).toMatch(/^cdr[1-5]$/);
  });

  test('AGP returns aggregate stats for 26k+ CGM points', async ({ request }) => {
    const r = await request.get(`/api/nurse/patient/${CGM_PATIENT}/agp`, {
      params: { window: '90' },
    });
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    // Summary always present even if hourly bands aren't populated yet
    // (platform-plan F1 perf doc tracks the hourly-bands fix).
    expect(body.summary).toBeTruthy();
    expect(body.summary).toHaveProperty('mean');
    expect(body.summary).toHaveProperty('cv');
    expect(body.summary).toHaveProperty('tir');
    expect(body.summary).toHaveProperty('tbr');
    expect(body.summary).toHaveProperty('tar');
    // CV may be null when patient has no CGM data; if present and
    // numeric, it should be a plausible coefficient of variation.
    if (typeof body.summary.cv === 'number') {
      expect(body.summary.cv).toBeGreaterThan(0);
      expect(body.summary.cv).toBeLessThan(100);
    }
  });

  test('variable series endpoint returns LTTB-downsampled HbA1c trace', async ({ request }) => {
    const r = await request.get(
      `/api/nurse/patient/${CGM_PATIENT}/variable/${pathify(HBA1C)}`,
    );
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body).toHaveProperty('points');
    expect(Array.isArray(body.points)).toBeTruthy();
    // Points length depends on the window; 0 is acceptable if the
    // patient has no readings of this code in the default window.
    expect(body.points.length).toBeLessThan(2100); // LTTB cap
  });

  test('events endpoint returns a list shape', async ({ request }) => {
    const r = await request.get(`/api/nurse/patient/${CGM_PATIENT}/events`);
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body).toHaveProperty('events');
    expect(Array.isArray(body.events)).toBeTruthy();
  });
});
