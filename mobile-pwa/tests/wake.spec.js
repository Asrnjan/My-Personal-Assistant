'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, converseReply } = require('./helpers');

const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1';

function health(name) {
  return { 'GET /health': () => ({ json: { status: 'ok', assistant_name: name } }) };
}

async function turnOnListening(page, name = 'Atlas') {
  await page.getByRole('button', { name: 'Open settings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Settings' });
  const toggle = dialog.getByRole('switch', { name: `Listen for “${name}” while the app is open` });
  await toggle.check();
  await dialog.getByRole('button', { name: 'Close settings' }).click();
  await expect(page.locator('#orb-label')).toHaveText(`Say “${name}” or tap`);
  await expect.poll(() => page.evaluate(() => window.__recognitions.some((r) => r.purpose === 'wake' && r.started && !r.ended))).toBe(true);
}

const wakeRecognizer = (page) => page.evaluateHandle(() => window.__recognitions.filter((r) => r.purpose === 'wake' && !r.ended).pop());

test('saying "Atlas, brief me" sends the request in one go', async ({ page }) => {
  const { calls } = await setup(page, { overrides: { ...health('Atlas'), 'POST /converse': () => ({ json: converseReply('Good morning.') }) } });
  await page.goto('/');
  await turnOnListening(page);
  const rec = await wakeRecognizer(page);
  await rec.evaluate((r) => r.__hear('pass the salt please'));
  await rec.evaluate((r) => r.__hear('hey atlas brief'));
  expect(findCalls(calls, 'POST', '/converse')).toHaveLength(0); // waits for the end of the sentence
  await rec.evaluate((r) => r.__say('Hey Atlas, brief me.'));
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe('brief me');
  await expect(page.locator('#transcript .turn-assistant .turn-who')).toHaveText('Atlas');
  // Then it listens for a follow-up without the name.
  await expect.poll(() => page.evaluate(() => window.__recognitions.filter((r) => r.purpose === 'turn' && r.started && !r.ended).length)).toBe(1);
});

test('"Atlas" followed by a long request keeps every word', async ({ page }) => {
  const { calls } = await setup(page, { overrides: health('Atlas') });
  await page.goto('/');
  await turnOnListening(page);
  const rec = await wakeRecognizer(page);
  await rec.evaluate((r) => r.__continue('Atlas'));
  await rec.evaluate((r) => r.__continue('delegate the vendor review'));
  await page.waitForTimeout(400); // a short natural pause
  await rec.evaluate((r) => r.__continue('to Rahul due Friday'));
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length, { timeout: 6000 }).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe('delegate the vendor review to Rahul due Friday');
});

test('the name alone starts listening for the request', async ({ page }) => {
  const { calls } = await setup(page, { overrides: health('Atlas') });
  await page.goto('/');
  await turnOnListening(page);
  const rec = await wakeRecognizer(page);
  await rec.evaluate((r) => r.__hear('at last'));
  await expect(page.locator('#orb')).toHaveAttribute('data-state', 'listening');
  // After a pause with nothing more, a fresh listen starts for the request.
  await expect.poll(() => page.evaluate(() => window.__recognitions.some((r) => r.purpose === 'turn' && r.started)), { timeout: 6000 }).toBe(true);
  const turn = await page.evaluateHandle(() => window.__recognitions.filter((r) => r.purpose === 'turn').pop());
  await turn.evaluate((r) => r.__say("What's next?"));
  await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
  expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe("What's next?");
});

test('the server decides the name, and turning it off stops listening', async ({ page }) => {
  await setup(page, { overrides: health('Nova') });
  await page.goto('/');
  await turnOnListening(page, 'Nova');
  await page.reload();
  await expect(page.locator('#orb-label')).toHaveText('Say “Nova” or tap');
  await page.getByRole('button', { name: 'Open settings' }).click();
  await page.getByRole('switch', { name: 'Listen for “Nova” while the app is open' }).uncheck();
  await expect(page.locator('#orb-label')).toHaveText('Tap to talk');
  await expect.poll(() => page.evaluate(() => window.__recognitions.filter((r) => r.purpose === 'wake' && !r.ended).length)).toBe(0);
});

test('a blocked microphone turns listening off and says why', async ({ page }) => {
  await setup(page, { overrides: health('Atlas') });
  await page.goto('/');
  await turnOnListening(page);
  const rec = await wakeRecognizer(page);
  await rec.evaluate((r) => r.onerror({ error: 'not-allowed' }));
  await expect(page.locator('.toast')).toContainText('can\'t listen for “Atlas”');
  await expect(page.locator('#orb-label')).toHaveText('Tap to talk');
});

test.describe('on iPhone', () => {
  test.use({ userAgent: IPHONE_UA });

  test('points to the Siri shortcut for a locked phone, and /?autostart=listen starts recording', async ({ page }) => {
    await setup(page, { overrides: health('Atlas'), speech: { mediaRecorder: true } });
    await page.goto('/');
    await page.getByRole('button', { name: 'Open settings' }).click();
    const dialog = page.getByRole('dialog', { name: 'Settings' });
    await expect(dialog.getByRole('switch', { name: /Listen for/ })).toBeEnabled();
    await expect(dialog.locator('#wake-ios')).toContainText('Hey Siri, Atlas');

    await page.goto('/?autostart=listen');
    await expect(page.locator('#orb')).toHaveAttribute('data-state', 'listening');
    expect(new URL(page.url()).search).toBe('');
  });

  test('while the app is open, a phrase starting with the name is sent; others are dropped', async ({ page }) => {
    const heard = ['what time is it', 'Atlas, brief me'];
    const { calls } = await setup(page, {
      overrides: {
        ...health('Atlas'),
        'POST /stt': () => ({ json: { text: heard.shift() || '' } }),
        'POST /converse': () => ({ json: converseReply('Here is your day.') }),
      },
      speech: { mediaRecorder: true, audioLevels: true },
    });
    await page.goto('/');
    await page.getByRole('button', { name: 'Open settings' }).click();
    await page.getByRole('switch', { name: /Listen for/ }).check();
    await page.keyboard.press('Escape');

    // Silence is never uploaded.
    await page.waitForTimeout(1500);
    expect(findCalls(calls, 'POST', '/stt')).toHaveLength(0);

    const speakPhrase = async () => {
      await page.evaluate(() => { window.__micLevel = 0.2; });
      await page.waitForTimeout(300);
      await page.evaluate(() => { window.__micLevel = 0; });
    };
    await speakPhrase(); // "what time is it": not for Atlas
    await expect.poll(() => findCalls(calls, 'POST', '/stt').length).toBe(1);
    await page.waitForTimeout(300);
    expect(findCalls(calls, 'POST', '/converse')).toHaveLength(0);

    await speakPhrase(); // "Atlas, brief me"
    await expect.poll(() => findCalls(calls, 'POST', '/converse').length).toBe(1);
    expect(findCalls(calls, 'POST', '/converse')[0].body.text).toBe('brief me');
  });
});
