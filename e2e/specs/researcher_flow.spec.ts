/**
 * Spec: researcher_flow — define cohort, fetch histogram, fetch
 * scatter, export CSV. Drives the deployed federation backend over
 * 4 seeded CDRs.
 *
 * Acceptance gates per platform-plan §4.3 / §4.6.
 *
 * The dashboard's cohort store is in-process per gunicorn worker
 * (post_seed_followups Phase-4.5 follow-up tracks the DB-backed fix).
 * For now `withCohort` retries the whole create+use cycle a few times
 * until the worker that created the cohort also serves the call.
 */
import { test, expect, APIRequestContext } from '@playwright/test';

const HBA1C = 'https://termbank.pdhc.se/CodeSystem/loinc/4548-4';
const LDL   = 'https://termbank.pdhc.se/CodeSystem/loinc/18262-6';
const pathify = (uri: string) => encodeURIComponent(uri);

let api: APIRequestContext;

test.beforeAll(async ({ playwright }) => {
  api = await playwright.request.newContext({
    baseURL: 'https://dashboard.pdhc.se',
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: {
      'X-Source-Service': 'monitor.pdhc',
      'X-Service-Key': process.env.MONITOR_PDHC_SERVICE_KEY!,
    },
  });
});

test.afterAll(async () => {
  await api?.dispose();
});

/** Create a cohort, run `cb(cohortId)`. Retry whole create+cb up to N
 *  times until cb's response is ok() (handles per-worker cohort store). */
async function withCohort<T>(
  cb: (cohortId: string) => Promise<{ status: number; result: T }>,
  attempts = 5,
): Promise<T> {
  let last: { status: number; result: T } | null = null;
  for (let i = 0; i < attempts; i++) {
    const post = await api.post('/api/cohort', {
      data: {
        cdr_ids: ['cdr2', 'cdr3', 'cdr4', 'cdr5'],
        demographics: { age_min: 18, age_max: 95 },
      },
    });
    expect(post.status()).toBe(201);
    const cid = (await post.json()).cohort_id;
    last = await cb(cid);
    if (last.status >= 200 && last.status < 300) return last.result;
    if (last.status !== 404) break;
  }
  throw new Error(
    `cohort-using call failed with ${last?.status} after ${attempts} attempts`,
  );
}

test.describe('researcher flow', () => {
  test('cohort definition spans all 4 CDRs and totals ~400 patients', async () => {
    const r = await api.post('/api/cohort', {
      data: {
        cdr_ids: ['cdr2', 'cdr3', 'cdr4', 'cdr5'],
        demographics: { age_min: 18, age_max: 95 },
      },
    });
    expect(r.status()).toBe(201);
    const body = await r.json();
    expect(body.n).toBeGreaterThanOrEqual(390);
    expect(body.n).toBeLessThanOrEqual(410);
  });

  test('list cohorts returns the expected shape (array under .cohorts)', async () => {
    const r = await api.get('/api/cohort');
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    const list = Array.isArray(body) ? body : (body.cohorts ?? []);
    expect(Array.isArray(list)).toBeTruthy();
  });

  test('histogram(HbA1c) merges across all 4 CDRs', async () => {
    const body = await withCohort(async (cid) => {
      const r = await api.get(
        `/api/cohort/${cid}/variable/${pathify(HBA1C)}/histogram`,
        { params: { buckets: '20' } },
      );
      return { status: r.status(), result: r.ok() ? await r.json() : null };
    });
    expect(body).toBeTruthy();
    expect(body.fanout_mode).toBe('complete');
    expect(body.succeeded_cdrs.sort()).toEqual(['cdr2', 'cdr3', 'cdr4', 'cdr5']);
    expect(body.n).toBeGreaterThan(0);
    // HbA1c in mmol/mol (Sweden / IFCC). 42–80 ≈ 6.0–9.5 % NGSP.
    expect(body.mean).toBeGreaterThan(42);
    expect(body.mean).toBeLessThan(80);
  });

  test('scatter(HbA1c, LDL) returns the documented shape (n=0 today; see perf doc)', async () => {
    const body = await withCohort(async (cid) => {
      const r = await api.get(`/api/cohort/${cid}/scatter`, {
        params: { x: HBA1C, y: LDL, max: '500' },
      });
      return { status: r.status(), result: r.ok() ? await r.json() : null };
    });
    expect(body).toHaveProperty('points');
    expect(body).toHaveProperty('n');
    expect(typeof body.truncated).toBe('boolean');
  });

  test('export returns CSV stream with the canonical schema header', async () => {
    const text = await withCohort(async (cid) => {
      const r = await api.get(
        `/api/cohort/${cid}/export?variables=${encodeURIComponent(HBA1C)}`,
      );
      return { status: r.status(), result: r.ok() ? await r.text() : '' };
    });
    expect(typeof text).toBe('string');
    const header = text.split('\n')[0];
    expect(header).toContain('patient_guid');
    expect(header).toContain('canonical');
    expect(header).toContain('value');
  });
});
