import { defineConfig, devices } from '@playwright/test'

// E2E runs against the full dev stack (Vite + API + worker + Postgres/Redis +
// the judge image). Bring it up with `scripts/dev-up.sh` first; these tests
// drive the real browser against http://localhost:5173.
const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173'

export default defineConfig({
  testDir: './e2e',
  // Judging is a shared, heavy resource — keep runs serial and deterministic.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'list' : [['list'], ['html', { open: 'never' }]],
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: BASE_URL,
    // Match the app's dark-first default so theme state is deterministic
    // (Chromium otherwise reports prefers-color-scheme: light).
    colorScheme: 'dark',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  // Reuse the Vite server started by dev-up.sh; start one if absent (local only).
  // Vite is launched directly, not via `pnpm dev`: newer pnpm 11 releases don't pass
  // Playwright's shutdown signal on to the child, so the run hangs after the last
  // test instead of exiting.
  webServer: {
    command: './node_modules/.bin/vite',
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
