/**
 * Spec: workspace_switch — dual-role users land on the chooser, can
 * navigate to nurse and researcher, can return.
 */
import { test, expect } from '@playwright/test';

test.describe('workspace switching', () => {
  test('admin (no role projection) sees both cards on /workspace', async ({ page }) => {
    await page.goto('/workspace');
    await expect(page).toHaveURL(/\/workspace$/);
    await expect(page.locator('body')).toContainText('Nurse workspace');
    await expect(page.locator('body')).toContainText('Researcher workspace');
  });

  test('dual-role user sees both cards', async ({ request, page }) => {
    // Browser carries default extraHTTPHeaders (admin); we can simulate
    // dual-role-non-admin by overriding via X-Test-Roles for THIS page.
    await page.setExtraHTTPHeaders({ 'X-Test-Roles': 'nurse,researcher' });
    await page.goto('/workspace');
    await expect(page.locator('body')).toContainText('Nurse workspace');
    await expect(page.locator('body')).toContainText('Researcher workspace');
  });

  test('nurse-only redirects from /workspace to /nurse', async ({ page }) => {
    await page.setExtraHTTPHeaders({ 'X-Test-Roles': 'nurse' });
    const resp = await page.goto('/workspace', { waitUntil: 'commit' });
    // Followed redirect lands on /nurse
    await page.waitForURL(/\/nurse$/);
    await expect(page.locator('body')).toContainText('Patient GUID');
  });

  test('researcher-only redirects from /workspace to /researcher', async ({ page }) => {
    await page.setExtraHTTPHeaders({ 'X-Test-Roles': 'researcher' });
    await page.goto('/workspace');
    await page.waitForURL(/\/researcher$/);
    await expect(page.locator('body')).toContainText('Build cohort');
  });

  test('admin can navigate to /nurse and back to /workspace', async ({ page }) => {
    await page.goto('/workspace');
    await page.goto('/nurse');
    await expect(page.locator('body')).toContainText('Patient GUID');
    await page.goto('/workspace');
    await expect(page.locator('body')).toContainText('Nurse workspace');
  });
});
