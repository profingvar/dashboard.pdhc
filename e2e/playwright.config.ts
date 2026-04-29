import { defineConfig, devices } from '@playwright/test';

const SERVICE_KEY = process.env.MONITOR_PDHC_SERVICE_KEY;
if (!SERVICE_KEY) {
  throw new Error('MONITOR_PDHC_SERVICE_KEY env var required');
}

export default defineConfig({
  testDir: './specs',
  fullyParallel: false, // serialize so the cohort store stays consistent
  reporter: [['list'], ['html', { outputFolder: '../results/playwright-report', open: 'never' }]],
  use: {
    baseURL: process.env.DASHBOARD_BASE_URL ?? 'https://dashboard.pdhc.se',
    extraHTTPHeaders: {
      'X-Source-Service': 'monitor.pdhc',
      'X-Service-Key': SERVICE_KEY,
    },
    ignoreHTTPSErrors: true,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
