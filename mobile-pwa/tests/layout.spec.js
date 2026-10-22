'use strict';

const { test, expect } = require('@playwright/test');
const { setup, SAMPLE_BRIEF, SAMPLE_PENDING, SAMPLE_FOLLOWUPS, converseReply } = require('./helpers');

const LONG = 'Supercalifragilisticexpialidocious-vendor-contract-renegotiation-for-the-asia-pacific-region';

const longBrief = {
  ...SAMPLE_BRIEF,
  text: `${SAMPLE_BRIEF.text} ${LONG} https://example.com/a/really/long/url/that/should/wrap/${LONG}`,
  items: [
    ...SAMPLE_BRIEF.items,
    { ...SAMPLE_BRIEF.items[1], id: 'it-long', title: LONG, reason: `${LONG} ${LONG}`, delegate_name: `Dr. ${LONG}` },
  ],
};

test.describe('phone layout at 390px', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('has no horizontal scroll with long content', async ({ page }) => {
    await setup(page, {
      overrides: {
        'GET /brief/today': () => ({ json: longBrief }),
        'GET /actions': () => ({ json: [...SAMPLE_PENDING, { ...SAMPLE_PENDING[0], id: 'act-long', title: LONG, readback: LONG }] }),
        'GET /followups': () => ({ json: [...SAMPLE_FOLLOWUPS, { ...SAMPLE_FOLLOWUPS[0], id: 'fu-long', title: LONG, delegate_name: LONG }] }),
        'POST /converse': () => ({ json: converseReply(`${LONG} ${LONG}`) }),
      },
    });
    await page.goto('/');
    await expect(page.locator('#brief-cards .card')).toHaveCount(longBrief.items.length);
    await expect(page.locator('#pending-list .pending-item')).toHaveCount(3);

    await page.getByLabel('Type a message').fill(LONG);
    await page.getByRole('button', { name: 'Send message' }).click();
    await expect(page.locator('#transcript .turn-assistant')).toHaveCount(1);

    const widths = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      inner: window.innerWidth,
    }));
    expect(widths.inner).toBe(390);
    expect(widths.doc).toBeLessThanOrEqual(390);
    expect(widths.body).toBeLessThanOrEqual(390);

    // The orb is the primary control: big, centred and fully on screen.
    const orb = await page.locator('#orb').boundingBox();
    expect(orb.width).toBeGreaterThanOrEqual(180);
    expect(orb.x).toBeGreaterThanOrEqual(0);
    expect(orb.x + orb.width).toBeLessThanOrEqual(390);
  });

  test('settings sheet fits the screen', async ({ page }) => {
    await setup(page);
    await page.goto('/');
    await page.getByRole('button', { name: 'Open settings' }).click();
    const dialog = page.getByRole('dialog', { name: 'Settings' });
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  });

  test('dark theme is the default and reduced motion stops animations', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
    await setup(page);
    await page.goto('/');
    const bg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--bg').trim());
    expect(bg).toBe('#04060f');
    // The star cluster draws still frames instead of animating.
    await expect(page.locator('#cluster')).toHaveAttribute('data-motion', 'reduced');
    const iterations = await page.locator('.sky-stars.s1').evaluate((el) => getComputedStyle(el).animationIterationCount);
    expect(iterations).toBe('1');
  });
});
