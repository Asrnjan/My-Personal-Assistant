'use strict';

const { test, expect } = require('@playwright/test');

test.use({ serviceWorkers: 'allow' });

test('service worker registers, controls the page and serves the shell offline', async ({ page, context }) => {
  await context.route(/https:\/\/fonts\.(googleapis|gstatic)\.com\//, (route) => route.abort());
  await page.goto('/');

  const registration = await page.evaluate(async () => {
    const reg = await navigator.serviceWorker.ready;
    return { scope: reg.scope, active: Boolean(reg.active), script: reg.active && reg.active.scriptURL };
  });
  expect(registration.active).toBe(true);
  expect(registration.scope).toMatch(/\/$/);
  expect(registration.script).toMatch(/\/sw\.js$/);

  // The shell gets cached on install.
  await expect.poll(() => page.evaluate(async () => {
    const keys = await caches.keys();
    const shell = keys.find((k) => k.startsWith('cos-shell-'));
    if (!shell) return 0;
    return (await (await caches.open(shell)).keys()).length;
  })).toBeGreaterThanOrEqual(5);

  await page.reload();
  await expect.poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller))).toBe(true);

  await context.setOffline(true);
  await page.goto('/?autostart=none');
  await expect(page.locator('#orb')).toBeVisible();
  await expect(page.locator('.brand-title')).toHaveText(/Chief of Staff/i);
  await context.setOffline(false);

  // Other pages on the same server (API docs, OAuth callback) are not swapped for the app.
  await page.goto('/docs');
  await expect(page.locator('#orb')).toHaveCount(0);
  await expect(page.locator('body')).toContainText('Not found');
});
