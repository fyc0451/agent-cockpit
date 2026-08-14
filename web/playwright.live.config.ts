import { defineConfig } from '@playwright/test'

const baseURL = process.env.PLAYWRIGHT_LIVE_BASE_URL
if (!baseURL) {
  throw new Error(
    'PLAYWRIGHT_LIVE_BASE_URL is required. Start via scripts/terminal_live_e2e.py so the browser only sees the ephemeral loopback URL.',
  )
}
if (baseURL.includes(':8790') || baseURL.includes(':18790')) {
  throw new Error('live e2e must not use reserved ports 8790/18790')
}

const artifactDir = process.env.PLAYWRIGHT_LIVE_ARTIFACT_DIR || '/tmp/term003-live-playwright'

export default defineConfig({
  testDir: './e2e-live',
  timeout: 90_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: `${artifactDir}/html` }]],
  outputDir: `${artifactDir}/test-results`,
  use: {
    baseURL,
    browserName: 'chromium',
    viewport: { width: 1280, height: 800 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
})
