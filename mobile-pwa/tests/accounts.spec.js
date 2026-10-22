'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, accountsBody, converseReply } = require('./helpers');

async function openApps(page) {
  await page.getByRole('navigation', { name: 'Main' }).getByRole('button', { name: 'Apps' }).click();
  return page.locator('#view-apps');
}

test('one-time setup saves the Microsoft app keys, then Connect opens the sign-in page', async ({ page }) => {
  let configured = [];
  const { calls } = await setup(page, {
    overrides: {
      'GET /accounts': () => ({ json: accountsBody({ configured }) }),
      'PUT /accounts/apps/microsoft': () => {
        configured = ['microsoft'];
        return { status: 204, body: '' };
      },
      'POST /accounts/outlook/connect': () => ({ json: { authorization_url: '/signed-in.html' } }),
    },
  });
  await page.route('**/signed-in.html', (route) => route.fulfill({ contentType: 'text/html', body: '<h1>Microsoft sign-in</h1>' }));
  await page.goto('/');
  const dialog = await openApps(page);
  const outlook = dialog.locator('[data-account="outlook"]');
  await expect(outlook).toContainText('Needs one-time setup');

  await outlook.getByRole('button', { name: 'Set up Outlook' }).click();
  await expect(dialog.locator('#app-keys')).toHaveAttribute('open', '');
  await expect(dialog.getByLabel('Redirect URI')).toHaveValue('http://127.0.0.1:4173/oauth/microsoft/callback');
  await dialog.getByLabel('Client ID').fill('ms-client');
  await dialog.getByLabel('Client secret').fill('ms-secret');
  await dialog.getByLabel(/Tenant ID/).fill('contoso.onmicrosoft.com');
  await dialog.getByRole('button', { name: 'Save keys' }).click();

  await expect.poll(() => findCalls(calls, 'PUT', '/accounts/apps/microsoft').length).toBe(1);
  expect(findCalls(calls, 'PUT', '/accounts/apps/microsoft')[0].body).toEqual({
    client_id: 'ms-client', client_secret: 'ms-secret', tenant_id: 'contoso.onmicrosoft.com',
  });
  await expect(outlook).toContainText('Not connected');
  await expect(dialog.getByLabel('Client secret')).toHaveValue('');

  await outlook.getByRole('button', { name: 'Connect Outlook' }).click();
  await expect(page.getByRole('heading', { name: 'Microsoft sign-in' })).toBeVisible();
  expect(findCalls(calls, 'POST', '/accounts/outlook/connect')).toHaveLength(1);
});

test('coming back from sign-in shows the account as connected', async ({ page }) => {
  await setup(page, {
    overrides: { 'GET /accounts': () => ({ json: accountsBody({ configured: ['microsoft'], connected: ['outlook', 'teams'] }) }) },
  });
  await page.goto('/?connected=microsoft');
  await expect(page.locator('.toast')).toContainText('Microsoft is connected');
  const dialog = page.locator('#view-apps');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-account="teams"]')).toHaveAttribute('data-connected', 'true');
  await expect(dialog.locator('[data-account="teams"]')).toContainText('Connected');
  expect(new URL(page.url()).search).toBe('');
});

test('disconnecting asks first and warns that Outlook and Teams go together', async ({ page }) => {
  let connected = ['outlook', 'teams'];
  const { calls } = await setup(page, {
    overrides: {
      'GET /accounts': () => ({ json: accountsBody({ configured: ['microsoft'], connected }) }),
      'DELETE /accounts/teams': () => {
        connected = [];
        return { status: 204, body: '' };
      },
    },
  });
  await page.goto('/');
  const dialog = await openApps(page);
  let message = '';
  page.once('dialog', (d) => {
    message = d.message();
    d.accept();
  });
  await dialog.getByRole('button', { name: 'Disconnect Microsoft Teams' }).click();
  await expect.poll(() => findCalls(calls, 'DELETE', '/accounts/teams').length).toBe(1);
  expect(message).toContain('Outlook and Teams share one Microsoft sign-in');
  await expect(dialog.locator('[data-account="outlook"]')).toHaveAttribute('data-connected', 'false');
});

test('warns when sign-in would return to a different address than this app', async ({ page }) => {
  await setup(page, {
    overrides: { 'GET /accounts': () => ({ json: accountsBody({ configured: ['microsoft'], redirect: 'http://localhost:8000' }) }) },
  });
  await page.goto('/');
  const dialog = await openApps(page);
  await expect(dialog.locator('#accounts-note')).toContainText('Sign-in returns to http://localhost:8000');
});

test('when the assistant needs Teams, one tap asks for access', async ({ page }) => {
  const { calls } = await setup(page, {
    overrides: {
      'POST /converse': () => ({ json: converseReply('Teams isn\'t connected yet.', { needs_account: ['teams'] }) }),
      'GET /accounts': () => ({ json: accountsBody({ configured: ['microsoft'] }) }),
      'POST /accounts/teams/connect': () => ({ json: { authorization_url: '/signed-in.html' } }),
    },
  });
  await page.route('**/signed-in.html', (route) => route.fulfill({ contentType: 'text/html', body: '<h1>Microsoft sign-in</h1>' }));
  await page.goto('/');
  const input = page.getByLabel('Type a message');
  await input.fill('Any new Teams messages?');
  await input.press('Enter');
  const prompt = page.getByRole('group', { name: 'Allow access' });
  await expect(prompt).toBeVisible();
  await prompt.getByRole('button', { name: 'Allow Microsoft Teams' }).click();
  await expect(page.getByRole('heading', { name: 'Microsoft sign-in' })).toBeVisible();
  expect(findCalls(calls, 'POST', '/accounts/teams/connect')).toHaveLength(1);
});

test('an account without its one-time setup opens Apps at the setup form', async ({ page }) => {
  let reply = converseReply('Connect Outlook or Google.', { needs_account: ['outlook', 'google'] });
  await setup(page, { overrides: { 'POST /converse': () => ({ json: reply }) } });
  await page.goto('/');
  const input = page.getByLabel('Type a message');
  await input.fill('Search my mail');
  await input.press('Enter');
  const prompt = page.getByRole('group', { name: 'Allow access' });
  await expect(prompt.getByRole('button')).toHaveText(['Allow Outlook', 'Allow Google']);
  await prompt.getByRole('button', { name: 'Allow Outlook' }).click();
  const dialog = page.locator('#view-apps');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('#app-keys')).toHaveAttribute('open', '');
  await expect(dialog.locator('#app-provider')).toHaveValue('microsoft');
  await expect(page.locator('.toast')).toContainText('needs a one-time setup first');

  await page.getByRole('navigation', { name: 'Main' }).getByRole('button', { name: 'Home' }).click();
  reply = converseReply('Done.');
  await input.fill('thanks');
  await input.press('Enter');
  await expect(prompt).toBeHidden();
});
