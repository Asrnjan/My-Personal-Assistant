'use strict';

const { test, expect } = require('@playwright/test');
const { setup, findCalls, SAMPLE_BRIEF, TOKEN } = require('./helpers');

test('brief renders cards with band colours, reasons and suggestions', async ({ page }) => {
  const { calls } = await setup(page);
  await page.goto('/');

  const cards = page.locator('#brief-cards .card');
  await expect(cards).toHaveCount(SAMPLE_BRIEF.items.length);
  await expect(page.locator('#brief-text')).toHaveText(SAMPLE_BRIEF.text);

  const expected = [
    ['P1_do_myself', 'band-p1', 'P1'],
    ['P2_delegate', 'band-p2', 'P2'],
    ['P3_decline', 'band-p3', 'P3'],
    ['P4_drop', 'band-p4', 'P4'],
  ];
  for (let i = 0; i < expected.length; i++) {
    const [band, cls, code] = expected[i];
    const card = cards.nth(i);
    await expect(card).toHaveAttribute('data-band', band);
    await expect(card).toHaveClass(new RegExp(cls));
    await expect(card.locator('.band-chip')).toContainText(code);
    await expect(card.locator('.card-title')).toHaveText(SAMPLE_BRIEF.items[i].title);
    await expect(card.locator('.card-reason')).toHaveText(SAMPLE_BRIEF.items[i].reason);
  }

  // Band colours differ: P1 glows magenta, P2 cyan.
  const stripe = async (i) => cards.nth(i).evaluate((el) => getComputedStyle(el, '::before').backgroundColor);
  expect(await stripe(0)).not.toBe(await stripe(1));

  const delegateCard = cards.nth(1);
  await expect(delegateCard.locator('.suggest')).toContainText('Delegate');
  await expect(delegateCard.locator('.delegate')).toHaveText('Rahul Mehta');
  await expect(cards.nth(0).locator('.card-title a')).toHaveAttribute('href', 'https://example.com/deck');

  const briefCalls = findCalls(calls, 'GET', '/brief/today');
  expect(briefCalls.length).toBeGreaterThan(0);
  expect(briefCalls[0].headers.authorization).toBe(`Bearer ${TOKEN}`);
  expect(briefCalls[0].search).toMatch(/kind=(morning|evening)/);
});

test('"Tell me more" reveals and speaks the details', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  const button = page.getByRole('button', { name: 'Tell me more' });
  await expect(button).toBeEnabled();
  await button.click();
  await expect(button).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#brief-details li')).toHaveCount(SAMPLE_BRIEF.details.length);
  await expect.poll(() => page.evaluate(() => window.__spoken)).toContain(SAMPLE_BRIEF.details.join(' '));
});

test('switching to the evening brief requests kind=evening', async ({ page }) => {
  const { calls } = await setup(page);
  await page.goto('/');
  await expect(page.locator('#brief-cards .card')).toHaveCount(4);
  await page.getByRole('button', { name: 'PM' }).click();
  await expect.poll(() => findCalls(calls, 'GET', '/brief/today').some((c) => c.search === '?kind=evening')).toBe(true);
});
