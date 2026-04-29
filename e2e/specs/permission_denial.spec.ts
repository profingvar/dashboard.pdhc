/**
 * Spec: permission_denial — role guards reject the wrong workspace.
 *
 * Drives the deployed dashboard via the monitor.pdhc service-key with
 * X-Test-Roles set to project the synthetic SU blob down to a single
 * role. Verifies that role guards on /nurse and /researcher reject
 * cross-role access with 403.
 *
 * Acceptance gate per platform-plan §4.7.
 */
import { test, expect } from '@playwright/test';

test.describe('role-based access denial', () => {
  test('nurse-only role gets 403 on /researcher', async ({ request }) => {
    const r = await request.get('/researcher', {
      headers: { 'X-Test-Roles': 'nurse' },
    });
    expect(r.status()).toBe(403);
  });

  test('researcher-only role gets 403 on /nurse', async ({ request }) => {
    const r = await request.get('/nurse', {
      headers: { 'X-Test-Roles': 'researcher' },
    });
    expect(r.status()).toBe(403);
  });

  test('user with no clinical roles gets 403 on /workspace', async ({ request }) => {
    const r = await request.get('/workspace', {
      headers: { 'X-Test-Roles': 'other' },
    });
    expect(r.status()).toBe(403);
  });

  test('researcher-only role cannot POST /api/cohort? wait it can — '
    + 'researcher_required guards POST /api/cohort, so researcher-only must succeed',
    async ({ request }) => {
      const r = await request.post('/api/cohort', {
        headers: { 'X-Test-Roles': 'researcher' },
        data: { cdr_ids: ['cdr2'], demographics: { age_min: 18 } },
      });
      expect(r.status()).toBe(201);
    });

  test('nurse-only role gets 403 on POST /api/cohort', async ({ request }) => {
    const r = await request.post('/api/cohort', {
      headers: { 'X-Test-Roles': 'nurse' },
      data: { cdr_ids: ['cdr2'], demographics: { age_min: 18 } },
    });
    expect(r.status()).toBe(403);
  });
});
