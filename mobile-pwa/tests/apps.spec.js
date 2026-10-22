'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, connectionsBody, accountsBody } = require('./helpers');

const nav = (page) => page.getByRole('navigation', { name: 'Main' });

test('the tab bar switches views and the back button goes back a tab', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  await expect(page.locator('#view-home')).toBeVisible();
  await expect(nav(page).getByRole('button', { name: 'Home' })).toHaveAttribute('aria-current', 'page');

  await nav(page).getByRole('button', { name: 'Team' }).click();
  await expect(page.locator('#view-team')).toBeVisible();
  await expect(page.locator('#view-home')).toBeHidden();
  expect(new URL(page.url()).hash).toBe('#team');

  await nav(page).getByRole('button', { name: 'Chat' }).click();
  await expect(page.locator('#view-chat')).toBeVisible();

  await page.goBack();
  await expect(page.locator('#view-team')).toBeVisible();
  await expect(nav(page).getByRole('button', { name: 'Team' })).toHaveAttribute('aria-current', 'page');
});

test('opening /#apps lands on the Apps tab with every app grouped', async ({ page }) => {
  await setup(page, { overrides: { 'GET /connections': () => ({ json: connectionsBody({ connected: ['jira'] }) }) } });
  await page.goto('/#apps');
  const apps = page.locator('#view-apps');
  await expect(apps).toBeVisible();
  await expect(apps.locator('#apps-brain [data-app="claude"]')).toContainText('Not connected');
  await expect(apps.locator('#apps-tasks [data-app="jira"]')).toHaveAttribute('data-connected', 'true');
  await expect(apps.locator('#apps-chat [data-app="telegram"]')).toBeVisible();
  await expect(apps.locator('#apps-progress-label')).toHaveText('1 of 4 connected');
  await expect(nav(page).locator('#apps-badge')).toBeVisible();
});

test('connecting Claude takes one pasted key in a sheet', async ({ page }) => {
  let connected = [];
  const { calls } = await setup(page, {
    overrides: {
      'GET /connections': () => ({ json: connectionsBody({ connected }) }),
      'PUT /connections/claude': () => {
        connected = ['claude'];
        return { json: { message: 'Claude is ready.', app: connectionsBody({ connected }).apps[0] } };
      },
    },
  });
  await page.goto('/#apps');
  await page.getByRole('button', { name: 'Set up Claude' }).click();
  const sheet = page.getByRole('dialog', { name: 'Connect Claude' });
  await expect(sheet).toBeVisible();
  await expect(sheet.locator('#app-sheet-steps li')).toHaveCount(2);
  await expect(sheet.getByRole('link', { name: /Get a key/ })).toHaveAttribute('href', 'https://example.com/claude');
  await sheet.getByLabel('API key').fill('sk-ant-test');
  await sheet.getByRole('button', { name: 'Connect' }).click();

  await expect.poll(() => findCalls(calls, 'PUT', '/connections/claude').length).toBe(1);
  expect(findCalls(calls, 'PUT', '/connections/claude')[0].body).toEqual({ values: { api_key: 'sk-ant-test' } });
  await expect(page.locator('.toast')).toContainText('Claude is ready.');
  await expect(page.locator('#apps-brain [data-app="claude"]')).toHaveAttribute('data-connected', 'true');
  await expect(sheet).toBeHidden();
});

test('a failed check keeps the sheet open and says what to do', async ({ page }) => {
  await setup(page, {
    overrides: {
      'PUT /connections/telegram': () => ({ status: 400, json: { detail: 'Almost there. Open @my_bot in Telegram, press Start, then tap Connect again.' } }),
    },
  });
  await page.goto('/#apps');
  await page.getByRole('button', { name: 'Set up Telegram' }).click();
  const sheet = page.getByRole('dialog', { name: 'Connect Telegram' });
  await sheet.getByLabel('Bot token').fill('1:abc');
  await sheet.getByRole('button', { name: 'Connect' }).click();
  await expect(sheet.locator('#app-sheet-msg')).toContainText('press Start');
  await expect(sheet).toBeVisible();
});

test('a connected app can be disconnected from its sheet', async ({ page }) => {
  let connected = ['jira'];
  const { calls } = await setup(page, {
    overrides: {
      'GET /connections': () => ({ json: connectionsBody({ connected }) }),
      'DELETE /connections/jira': () => {
        connected = [];
        return { status: 204, body: '' };
      },
    },
  });
  await page.goto('/#apps');
  await page.getByRole('button', { name: 'Manage Jira' }).click();
  const sheet = page.getByRole('dialog', { name: 'Jira' });
  // Saved secrets are never shown; leaving the field empty keeps them.
  await expect(sheet.getByLabel('API token')).toHaveAttribute('placeholder', 'Saved. Leave empty to keep it.');
  page.once('dialog', (d) => d.accept());
  await sheet.getByRole('button', { name: 'Disconnect' }).click();
  await expect.poll(() => findCalls(calls, 'DELETE', '/connections/jira').length).toBe(1);
  await expect(page.locator('#apps-tasks [data-app="jira"]')).toHaveAttribute('data-connected', 'false');
});

test('home shows a setup checklist until Claude and email are connected', async ({ page }) => {
  let connected = [];
  await setup(page, {
    overrides: {
      'GET /connections': () => ({ json: connectionsBody({ connected }) }),
      'GET /accounts': () => ({ json: accountsBody({ configured: ['google'], connected: ['google'] }) }),
    },
  });
  await page.goto('/');
  const card = page.locator('#setup-card');
  await expect(card).toBeVisible();
  await expect(card.locator('[data-step="device"]')).toHaveAttribute('data-done', 'true');
  await expect(card.locator('[data-step="email"]')).toHaveAttribute('data-done', 'true');
  await expect(card.locator('[data-step="claude"]')).toHaveAttribute('data-done', 'false');
  await card.getByRole('button', { name: 'Add Claude' }).click();
  await expect(page.getByRole('dialog', { name: 'Connect Claude' })).toBeVisible();

  connected = ['claude'];
  await page.reload();
  await expect(page.locator('#brief-cards .card').first()).toBeVisible();
  await expect(card).toBeHidden();
});

test('without a sign-in the checklist starts with connecting the device', async ({ page }) => {
  await setup(page, { token: false });
  await page.goto('/');
  const card = page.locator('#setup-card');
  await expect(card).toBeVisible();
  await card.getByRole('button', { name: 'Connect this device' }).click();
  await expect(page.getByRole('dialog', { name: 'Settings' })).toBeVisible();
});

test('the star cluster reacts: a sent message flashes it and a reply shows under it', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  const cluster = page.locator('#cluster');
  await expect(cluster).toHaveAttribute('data-state', 'idle');
  const before = Number(await cluster.getAttribute('data-pulses') || 0);
  await page.getByRole('button', { name: 'Brief me' }).click();
  await expect.poll(async () => Number(await cluster.getAttribute('data-pulses') || 0)).toBeGreaterThan(before);
  await expect(page.locator('#reply-bubble')).toHaveText('Okay.');
  await expect(cluster).toHaveAttribute('data-state', /idle|speaking/);
  // The canvas really draws something.
  const lit = await cluster.evaluate((c) => {
    const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 3; i < data.length; i += 4 * 50) if (data[i] > 0) n++;
    return n;
  });
  expect(lit).toBeGreaterThan(10);
});

test('touching the sphere sends ripples from that point', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  const cluster = page.locator('#cluster');
  const box = await page.locator('#orb').boundingBox();
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.4);
  await page.mouse.down();
  await page.mouse.up();
  await expect(cluster).toHaveAttribute('data-touches', '1');
});
