import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e-pages',
  outputDir: '../.ktru-build/playwright/pages',
  forbidOnly: !!process.env.CI,
  use: { baseURL: 'http://127.0.0.1:4174', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], channel: 'chromium' } }],
  webServer: {
    command: 'pnpm exec vite preview --config vite.pages.config.ts --host 127.0.0.1 --port 4174 --strictPort',
    url: 'http://127.0.0.1:4174/ktru-match/',
    timeout: 10_000,
  },
})
