'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, SAMPLE_PENDING, SAMPLE_FOLLOWUPS, TOKEN } = require('./helpers');

test('Confirm and Cancel call the right endpoints', async ({ page }) => {
  const { calls } = await setup(page, {
    overrides: {
      'GET /actions': () => ({ json: SAMPLE_PENDING }),
    },
  });
  await page.goto('/');

  const items = page.locator('#pending-list .pending-item');
  await expect(items).toHaveCount(2);
  await expect(page.locator('#pending-count')).toHaveText('2');
  expect(findCalls(calls, 'GET', '/actions')[0].search).toBe('?status=pending');
  await expect(items.first()).toContainText(SAMPLE_PENDING[0].readback);

  await page.getByRole('button', { name: `Confirm: ${SAMPLE_PENDING[0].title}` }).click();
  await expect.poll(() => findCalls(calls, 'POST', '/actions/act-1/confirm').length).toBe(1);
  await expect(page.locator('.toast')).toContainText('Done: email sent to Rahul.');
  await expect(items).toHaveCount(1);

  await page.getByRole('button', { name: `Cancel: ${SAMPLE_PENDING[1].title}` }).click();
  await expect.poll(() => findCalls(calls, 'POST', '/actions/act-2/cancel').length).toBe(1);
  await expect(items).toHaveCount(0);
  await expect(page.locator('#pending-empty')).toBeVisible();

  expect(findCalls(calls, 'POST', '/actions/act-1/cancel')).toHaveLength(0);
  expect(findCalls(calls, 'POST', '/actions/act-2/confirm')).toHaveLength(0);
  for (const c of [...findCalls(calls, 'POST', '/actions/act-1/confirm'), ...findCalls(calls, 'POST', '/actions/act-2/cancel')]) {
    expect(c.headers.authorization).toBe(`Bearer ${TOKEN}`);
  }
});

test('a failed confirm keeps the item and shows the error', async ({ page }) => {
  await setup(page, {
    overrides: {
      'GET /actions': () => ({ json: [SAMPLE_PENDING[0]] }),
      'POST /actions/:id/confirm': () => ({ status: 500, json: { detail: 'Mail server unavailable' } }),
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Confirm:/ }).click();
  await expect(page.locator('.toast-error')).toContainText('Mail server unavailable');
  await expect(page.locator('#pending-list .pending-item')).toHaveCount(1);
  await expect(page.getByRole('button', { name: /^Confirm:/ })).toBeEnabled();
});

test('delegation status list renders follow-ups', async ({ page }) => {
  await setup(page, { overrides: { 'GET /followups': () => ({ json: SAMPLE_FOLLOWUPS }) } });
  await page.goto('/');
  const rows = page.locator('#followups-list .followup-item');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('Q3 hiring plan');
  await expect(rows.nth(0).locator('.state')).toHaveText('acknowledged');
  await expect(rows.nth(1).locator('.state')).toHaveClass(/state-overdue/);
  await expect(rows.nth(1)).toContainText('nudged 2×');
});
