'use strict';

const { test, expect } = require('@playwright/test');
const { mockApi } = require('./helpers');

function pngSize(buffer) {
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  expect(buffer.subarray(0, 8).equals(signature)).toBe(true);
  expect(buffer.subarray(12, 16).toString('ascii')).toBe('IHDR');
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

test('install manifest is linked and valid', async ({ page, request }) => {
  await mockApi(page);
  await page.goto('/');
  const href = await page.locator('link[rel="manifest"]').getAttribute('href');
  expect(href).toBe('manifest.webmanifest');

  const res = await request.get(`/${href}`);
  expect(res.ok()).toBe(true);
  expect(res.headers()['content-type']).toContain('application/manifest+json');
  const manifest = await res.json();

  expect(manifest.name).toBeTruthy();
  expect(manifest.short_name).toBeTruthy();
  expect(manifest.short_name.length).toBeLessThanOrEqual(15);
  expect(manifest.start_url).toBe('/');
  expect(manifest.display).toBe('standalone');
  expect(manifest.theme_color).toMatch(/^#[0-9a-f]{6}$/i);
  expect(manifest.background_color).toMatch(/^#[0-9a-f]{6}$/i);
  expect(await page.locator('meta[name="theme-color"]').getAttribute('content')).toBe(manifest.theme_color);

  const icons = manifest.icons;
  expect(Array.isArray(icons)).toBe(true);
  const sizes = icons.map((i) => i.sizes);
  expect(sizes).toContain('192x192');
  expect(sizes).toContain('512x512');
  expect(icons.some((i) => (i.purpose || '').split(' ').includes('maskable'))).toBe(true);

  for (const icon of icons) {
    expect(icon.type).toBe('image/png');
    const iconRes = await request.get(`/${icon.src}`);
    expect(iconRes.ok(), `${icon.src} should load`).toBe(true);
    expect(iconRes.headers()['content-type']).toBe('image/png');
    const { width, height } = pngSize(await iconRes.body());
    expect(`${width}x${height}`).toBe(icon.sizes);
  }
});

test('apple touch icon and favicon load', async ({ page, request }) => {
  await mockApi(page);
  await page.goto('/');
  for (const selector of ['link[rel="apple-touch-icon"]', 'link[rel="icon"]']) {
    const href = await page.locator(selector).getAttribute('href');
    const res = await request.get(`/${href}`);
    expect(res.ok(), `${href} should load`).toBe(true);
    expect(res.headers()['content-type']).toBe('image/png');
  }
});
