'use strict';

const { test, expect } = require('@playwright/test');
const { setup } = require('./helpers');

// What Edge on Windows offers: the old desktop voices first, the natural ones later.
const VOICES = [
  { name: 'Microsoft David - English (United States)', lang: 'en-US', default: true },
  { name: 'Microsoft Zira - English (United States)', lang: 'en-US' },
  { name: 'Microsoft Guy Online (Natural) - English (United States)', lang: 'en-US', localService: false },
  { name: 'Microsoft Aria Online (Natural) - English (United States)', lang: 'en-US', localService: false },
  { name: 'Microsoft Denise Online (Natural) - French (France)', lang: 'fr-FR', localService: false },
];

async function testVoice(page) {
  await page.getByRole('button', { name: 'Open settings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await dialog.getByRole('button', { name: 'Test the voice' }).click();
  await expect.poll(() => page.evaluate(() => window.__utterances.length)).toBeGreaterThan(0);
  return { dialog, spoken: await page.evaluate(() => window.__utterances.at(-1)) };
}

test('Atlas speaks with the most natural female voice, a little faster than default', async ({ page }) => {
  await setup(page, { speech: { voices: VOICES } });
  await page.goto('/');
  const { dialog, spoken } = await testVoice(page);
  expect(spoken.voice).toBe('Microsoft Aria Online (Natural) - English (United States)');
  expect(spoken.rate).toBeGreaterThan(1.05);
  await expect(dialog.getByLabel('Atlas speaks with')).toContainText('Best available (Microsoft Aria Online');
  // Only voices for the user's language are offered.
  await expect(dialog.getByLabel('Atlas speaks with').locator('option')).toHaveCount(5);
});

test('a voice and speed picked in Settings are remembered', async ({ page }) => {
  await setup(page, { speech: { voices: VOICES } });
  await page.goto('/');
  await page.getByRole('button', { name: 'Open settings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await dialog.getByLabel('Atlas speaks with').selectOption('Microsoft Zira - English (United States)');
  await dialog.getByLabel('Speed').selectOption('1.25');

  await page.reload();
  await page.evaluate(() => { window.__utterances = []; });
  const { spoken } = await testVoice(page);
  expect(spoken.voice).toBe('Microsoft Zira - English (United States)');
  expect(spoken.rate).toBe(1.25);
});

test('without a voice list the browser default still speaks', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  const { spoken } = await testVoice(page);
  expect(spoken.voice).toBeNull();
});
