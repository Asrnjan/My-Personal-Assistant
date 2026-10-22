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
  // The conversation carries on: Atlas listens for the answer without its name.
  await expect(page.locator('#orb')).toHaveAttribute('data-state', 'listening');
  await expect(page.locator('#orb-label')).toHaveText('Listening… say “thanks” when you’re done');
});

test('a long request with pauses is sent whole, once you stop talking', async ({ page }) => {
  const { calls } = await setup(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Start talking' }).click();
  await expect.poll(() => page.evaluate(() => window.__recognitions.length && window.__recognitions[0].started)).toBe(true);
  await page.evaluate(() => window.__recognitions[0].__continue('Delegate the vendor review'));
  await page.waitForTimeout(1200); // a natural pause mid-sentence
  expect(findCalls(calls, 'POST', '/converse')).toHaveLength(0);
  await page.evaluate(() => window.__recognitions[0].__continue('to Rahul, due Friday'));
  await expect(page.locator('#caption')).toHaveText('Delegate the vendor review to Rahul, due Friday');
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length, { timeout: 6000 }).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe('Delegate the vendor review to Rahul, due Friday');
});

test('a tap sends what was heard straight away', async ({ page }) => {
  const { calls } = await setup(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Start talking' }).click();
  await expect.poll(() => page.evaluate(() => window.__recognitions.length && window.__recognitions[0].started)).toBe(true);
  await page.evaluate(() => window.__recognitions[0].__continue('What is on my calendar'));
  await page.getByRole('button', { name: 'Stop listening' }).click();
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe('What is on my calendar');
});

test('the back-and-forth needs no name, and "thanks" ends it', async ({ page }) => {
  let replies = ['Rahul has the vendor review. Anything else?', 'You are welcome.'];
  const { calls } = await setup(page, { overrides: { 'POST /converse': () => ({ json: converseReply(replies.shift()) }) } });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start talking' }).click();
  await expect.poll(() => page.evaluate(() => window.__recognitions.length && window.__recognitions[0].started)).toBe(true);
  await page.evaluate(() => window.__recognitions[0].__say('Who has the vendor review?'));
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length, { timeout: 6000 }).toBe(1);

  // Atlas listens again by itself after speaking.
  await expect.poll(() => page.evaluate(() => window.__recognitions.filter((r) => r.purpose === 'turn' && r.started && !r.ended).length)).toBe(1);
  await page.evaluate(() => window.__recognitions.filter((r) => r.purpose === 'turn' && !r.ended).pop().__say('thanks'));
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length, { timeout: 6000 }).toBe(2);
  expect(findCalls(calls, 'POST', '/converse')[1].body.text).toBe('thanks');

  // "thanks" ends the conversation: no new listening after the reply.
  await expect.poll(() => page.evaluate(() => window.__spoken)).toContain('You are welcome.');
  await page.waitForTimeout(600);
  await expect(page.locator('#orb')).toHaveAttribute('data-state', 'idle');
});

test('turning off "Keep the conversation going" stops listening after replies', async ({ page }) => {
  const { calls } = await setup(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Open settings' }).click();
  await page.getByRole('switch', { name: 'Keep the conversation going' }).uncheck();
  await page.getByRole('button', { name: 'Close settings' }).click();
  await page.getByRole('button', { name: 'Start talking' }).click();
  await expect.poll(() => page.evaluate(() => window.__recognitions.length && window.__recognitions[0].started)).toBe(true);
  await page.evaluate(() => window.__recognitions[0].__say('brief me'));
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length, { timeout: 6000 }).toBe(1);
  await expect.poll(() => page.evaluate(() => window.__spoken.length)).toBe(1);
  await page.waitForTimeout(600);
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

test('a heard phrase is sent even when recognition never marks it final', async ({ page }) => {
  const { calls } = await setup(page, { overrides: { 'POST /converse': () => ({ json: converseReply(REPLY) }) } });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start talking' }).click();
  await expect.poll(() => page.evaluate(() => window.__recognitions.length && window.__recognitions[0].started)).toBe(true);
  await page.evaluate((text) => window.__recognitions[0].__hear(text), SAID);
  await page.getByRole('button', { name: 'Stop listening' }).click();
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe(SAID);
});

test.describe('on iPhone', () => {
  test.use({ userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1' });

  test('voice is recorded and transcribed by the server even though Safari has SpeechRecognition', async ({ page }) => {
    const { calls } = await setup(page, {
      speech: { recognition: true, mediaRecorder: true },
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
    await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
    expect(await page.evaluate(() => window.__recognitions.length)).toBe(0);
  });
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

test('keeps listening after a reply even when the browser never says speech ended', async ({ page }) => {
  const { calls } = await setup(page, { speech: { synthesis: 'no-end' } });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start talking' }).click();
  await expect.poll(() => page.evaluate(() => window.__recognitions.length && window.__recognitions[0].started)).toBe(true);
  await page.evaluate(() => window.__recognitions[0].__say('brief me'));
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length, { timeout: 6000 }).toBe(1);
  await expect.poll(
    () => page.evaluate(() => window.__recognitions.filter((r) => r.purpose === 'turn' && r.started && !r.ended).length),
    { timeout: 6000 },
  ).toBe(1);
  await expect(page.locator('#orb-label')).toHaveText('Listening… say “thanks” when you’re done');
});

test('a long reply is spoken a few sentences at a time', async ({ page }) => {
  const long = 'First thing today is the board deck, which needs your sign-off by noon. '
    + 'Second, Rahul can take the vendor review because he handled the last one. '
    + 'Third, the agency pitch clashes with the investor call, so I suggest declining it. '
    + 'Shall I go ahead?';
  await setup(page, { overrides: { 'POST /converse': () => ({ json: converseReply(long) }) } });
  await page.goto('/');
  await page.getByLabel('Type a message').fill('brief me');
  await page.getByLabel('Type a message').press('Enter');
  await expect.poll(() => page.evaluate(() => window.__spoken.length)).toBeGreaterThan(1);
  const spoken = await page.evaluate(() => window.__spoken);
  expect(spoken.join(' ')).toBe(long);
  for (const chunk of spoken) expect(chunk.length).toBeLessThanOrEqual(180);
});
