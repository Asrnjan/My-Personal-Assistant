'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, converseReply, SAMPLE_BRIEF } = require('./helpers');

const BRIEF_REPLY = 'Good morning. Your top priority is the board deck.';

test('?autostart=brief runs the brief flow on open', async ({ page }) => {
  const { calls } = await setup(page, {
    overrides: { 'POST /converse': () => ({ json: converseReply(BRIEF_REPLY) }) },
  });
  await page.goto('/?autostart=brief');

  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  const converse = findCalls(calls, 'POST', '/converse')[0];
  expect(converse.body.text.toLowerCase()).toContain('brief');
  expect(converse.body).toHaveProperty('conversation_id');

  await expect(page.locator('#brief-cards .card')).toHaveCount(SAMPLE_BRIEF.items.length);
  await expect.poll(() => page.evaluate(() => window.__spoken)).toContain(BRIEF_REPLY);
  await expect(page.locator('#transcript .turn-assistant')).toContainText(BRIEF_REPLY);
  await expect(page.locator('#tap-to-hear')).toBeHidden();
  // The deep link is consumed so a reload does not replay the brief.
  expect(new URL(page.url()).search).toBe('');
});

test('when speech is blocked without a gesture, a "Tap to hear" prompt appears', async ({ page }) => {
  const { calls } = await setup(page, {
    speech: { synthesis: 'blocked' },
    overrides: { 'POST /converse': () => ({ json: converseReply(BRIEF_REPLY) }) },
  });
  await page.goto('/?autostart=brief');

  const prompt = page.getByRole('button', { name: 'Tap to hear your brief' });
  await expect(prompt).toBeVisible();
  // The server TTS fallback was attempted before showing the prompt.
  expect(findCalls(calls, 'GET', '/tts').length).toBeGreaterThan(0);

  await page.evaluate(() => { window.__allowSpeech = true; });
  await prompt.click();
  await expect(page.locator('#tap-to-hear')).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.__spoken)).toContain(BRIEF_REPLY);
});

test('autostart without a token asks to connect, then runs the brief', async ({ page }) => {
  const { calls } = await setup(page, {
    token: false,
    overrides: { 'POST /converse': () => ({ json: converseReply(BRIEF_REPLY) }) },
  });
  await page.goto('/?autostart=brief');
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await expect(dialog).toBeVisible();
  expect(findCalls(calls, 'POST', '/converse')).toHaveLength(0);

  await dialog.getByLabel('Shared secret').fill('open-sesame');
  await dialog.getByRole('button', { name: 'Connect' }).click();
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe('brief me');
  await expect.poll(() => page.evaluate(() => window.__spoken)).toContain(BRIEF_REPLY);
});
