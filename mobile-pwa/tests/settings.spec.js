'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, TOKEN } = require('./helpers');

test('login with the shared secret stores the token and loads data', async ({ page }) => {
  const { calls } = await setup(page, { token: false });
  await page.goto('/');
  await expect(page.locator('#connect-banner')).toBeVisible();
  expect(findCalls(calls, 'GET', '/brief/today')).toHaveLength(0);

  await page.getByRole('button', { name: 'Open settings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await dialog.getByLabel('Shared secret').fill('open-sesame');
  await dialog.getByRole('button', { name: 'Connect' }).click();

  await expect.poll(() => page.evaluate(() => localStorage.getItem('cos.token'))).toBe(TOKEN);
  const login = findCalls(calls, 'POST', '/auth/token');
  expect(login).toHaveLength(1);
  expect(login[0].body).toEqual({ secret: 'open-sesame', client_name: 'pwa' });
  expect(login[0].headers.authorization).toBeUndefined();

  await expect(dialog).toBeHidden();
  await expect(page.locator('#connect-banner')).toBeHidden();
  await expect.poll(() => findCalls(calls, 'GET', '/brief/today').length).toBeGreaterThan(0);
  expect(findCalls(calls, 'GET', '/brief/today')[0].headers.authorization).toBe(`Bearer ${TOKEN}`);
});

test('a wrong secret shows an error and stores nothing', async ({ page }) => {
  await setup(page, {
    token: false,
    overrides: { 'POST /auth/token': () => ({ status: 401, json: { detail: 'Invalid secret' } }) },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Open settings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await dialog.getByLabel('Shared secret').fill('wrong');
  await dialog.getByRole('button', { name: 'Connect' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('That secret was not accepted.');
  expect(await page.evaluate(() => localStorage.getItem('cos.token'))).toBeNull();
});

test('API base override routes calls to the configured server', async ({ page }) => {
  const { calls } = await setup(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Open settings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await dialog.getByLabel(/API base URL/).fill('https://assistant.example.com/');
  await dialog.getByRole('button', { name: 'Save' }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('cos.apiBase'))).toBe('https://assistant.example.com');
  await expect.poll(() => calls.some((c) => c.host === 'assistant.example.com' && c.path === '/brief/today')).toBe(true);
});

test('an expired token sends the user back to connect', async ({ page }) => {
  await setup(page, { overrides: { 'GET /brief/today': () => ({ status: 401, json: { detail: 'expired' } }) } });
  await page.goto('/');
  await expect(page.locator('#connect-banner')).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('cos.token'))).toBeNull();
});
