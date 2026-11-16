import { chromium } from '@playwright/test';

const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.stack || String(e)));
page.on('console', (m) => {
  if (m.type() === 'error') errors.push('console: ' + m.text());
});
page.on('response', (r) => {
  if (r.url().includes('/api/')) console.log('API', r.status(), r.url());
});
await page.goto(process.env.URL || 'http://127.0.0.1:5173/');
await page.locator('textarea').fill('Стол письменный 120х60, ЛДСП, 4 шт');
await page.getByRole('button', { name: 'Подобрать', exact: true }).click();
await page.waitForTimeout(5000);
console.log('--- errors ---');
for (const e of errors) console.log(e);
console.log('--- end ---');
await browser.close();
