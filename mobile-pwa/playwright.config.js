// Playwright config for the mobile PWA (Chromium only).
'use strict';

const fs = require('fs');
const path = require('path');
const { defineConfig, devices } = require('@playwright/test');

const PORT = Number(process.env.PWA_TEST_PORT || 4173);
const BASE_URL = `http://127.0.0.1:${PORT}`;

// Playwright normally finds its own Chromium. Only when PLAYWRIGHT_BROWSERS_PATH holds a
// Chromium build with a different revision than this Playwright expects (preinstalled
// sandbox images) do we point at that binary explicitly. In CI, where
// `npx playwright install chromium` runs, the revisions match and this returns undefined.
function chromiumExecutable() {
  if (process.env.PW_CHROMIUM_EXECUTABLE) return process.env.PW_CHROMIUM_EXECUTABLE;
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH;
  if (!root || !fs.existsSync(root)) return undefined;
  let expected;
  try {
    const core = path.dirname(require.resolve('playwright-core/package.json'));
    const browsers = JSON.parse(fs.readFileSync(path.join(core, 'browsers.json'), 'utf8')).browsers;
    expected = browsers.find((b) => b.name === 'chromium').revision;
  } catch {
    return undefined;
  }
  if (fs.existsSync(path.join(root, `chromium-${expected}`))) return undefined;
  const candidates = fs.readdirSync(root).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse();
  for (const dir of candidates) {
    for (const sub of ['chrome-linux/chrome', 'chrome-linux64/chrome', 'chrome-mac/Chromium.app/Contents/MacOS/Chromium', 'chrome-win/chrome.exe']) {
      const bin = path.join(root, dir, sub);
      if (fs.existsSync(bin)) return bin;
    }
  }
  return undefined;
}

const executablePath = chromiumExecutable();

module.exports = defineConfig({
  testDir: './tests',
  timeout: 20000,
  expect: { timeout: 5000 },
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    serviceWorkers: 'block',
  },
  projects: [
    {
      name: 'chromium-mobile',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 390, height: 844 },
        deviceScaleFactor: 2,
        isMobile: true,
        hasTouch: true,
        ...(executablePath ? { launchOptions: { executablePath } } : {}),
      },
    },
  ],
  webServer: {
    command: `node scripts/serve.js --port ${PORT}`,
    url: `${BASE_URL}/index.html`,
    reuseExistingServer: !process.env.CI,
    timeout: 15000,
  },
});
