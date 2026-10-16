'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, converseReply, SAMPLE_PENDING } = require('./helpers');

const SAID = 'Delegate the vendor review to Rahul, due Monday';
const REPLY = 'I will email Rahul the vendor review, due Monday. Shall I go ahead?';

test('a spoken transcript is sent to /converse and the reply is spoken', async ({ page }) => {
  const { calls } = await setup(page, {
    overrides: {
      'POST /converse': () => ({ json: converseReply(REPLY, { pending_actions: [SAMPLE_PENDING[0]], needs_confirmation: true }) }),
    },
  });
  await page.goto('/');
  const orb = page.getByRole('button', { name: 'Start talking' });
  await orb.click();
  await expect(page.locator('#orb')).toHaveAttribute('data-state', 'listening');
  await expect(page.locator('#orb-label')).toHaveText('Listening…');
  await expect.poll(() => page.evaluate(() => window.__recognitions.length && window.__recognitions[0].started)).toBe(true);

  await page.evaluate((text) => window.__recognitions[0].__say(text), SAID);

  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body).toEqual({ text: SAID, conversation_id: null });
  await expect.poll(() => page.evaluate(() => window.__spoken)).toContain(REPLY);

  await expect(page.locator('#transcript .turn-user')).toHaveText(new RegExp(SAID));
  await expect(page.locator('#transcript .turn-assistant')).toHaveText(new RegExp(REPLY));
  await expect(page.locator('#pending-list .pending-item')).toHaveCount(1);
  await expect(page.locator('#orb')).toHaveAttribute('data-state', 'idle');
});

test('the conversation id is reused on the next turn', async ({ page }) => {
  const { calls } = await setup(page, { overrides: { 'POST /converse': () => ({ json: converseReply('Noted.') }) } });
  await page.goto('/');
  const input = page.getByLabel('Type a message');
  await input.fill('What is next?');
  await input.press('Enter');
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  await expect(page.locator('#transcript .turn-assistant')).toHaveCount(1);
  await input.fill('And after that?');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(2);
  const [first, second] = findCalls(calls, 'POST', '/converse');
  expect(first.body.conversation_id).toBeNull();
  expect(second.body).toEqual({ text: 'And after that?', conversation_id: 'conv-1' });
  await expect.poll(() => page.evaluate(() => window.__spoken.length)).toBe(2);
});

test('without SpeechRecognition, audio is recorded and uploaded to /stt', async ({ page }) => {
  const { calls } = await setup(page, {
    speech: { recognition: false, mediaRecorder: true },
    overrides: {
      'POST /stt': () => ({ json: { text: SAID } }),
      'POST /converse': () => ({ json: converseReply(REPLY) }),
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start talking' }).click();
  await expect(page.locator('#orb')).toHaveAttribute('data-state', 'listening');
  await page.getByRole('button', { name: 'Stop listening' }).click();

  await expect.poll(() => findCalls(calls, 'POST', '/stt').length).toBe(1);
  const stt = findCalls(calls, 'POST', '/stt')[0];
  expect(stt.headers['content-type']).toMatch(/^multipart\/form-data/);
  expect(stt.raw.toString('utf8')).toMatch(/name="audio"; filename="speech\.webm"/);

  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe(SAID);
  await expect.poll(() => page.evaluate(() => window.__spoken)).toContain(REPLY);
});

test('pushed WebSocket events show a toast and refresh the brief', async ({ page }) => {
  const { socket } = await setup(page);
  await page.goto('/');
  await expect(page.locator('#conn-status')).toHaveAttribute('data-state', 'live');
  expect(socket.url).toMatch(/\/ws\?token=test-token-123$/);

  const alert = {
    kind: 'morning', for_date: '2026-10-16', text: 'Heads up: the CFO needs you now.', details: [],
    items: [{ id: 'al-1', title: 'CFO escalation', kind: 'email_action', band: 'P1_do_myself', score: 99, reason: 'Board-level risk.', start: null, due: null, url: null, suggested: 'do', delegate_id: null, delegate_name: null }],
    delegations: [], word_count: 8, estimated_seconds: 4, generated_at: '2026-10-16T09:00:00Z',
  };
  socket.socket.send(JSON.stringify({ type: 'event', kind: 'alert', payload: alert }));
  await expect(page.locator('.toast-alert')).toContainText('CFO escalation');
  await expect(page.locator('#brief-cards .card')).toHaveCount(1);
  await expect(page.locator('#brief-cards .card').first()).toHaveClass(/band-p1/);

  socket.socket.send(JSON.stringify({ type: 'event', kind: 'notice', payload: { text: 'Rahul acknowledged the vendor review.' } }));
  await expect(page.locator('.toast').last()).toContainText('Rahul acknowledged');
});
