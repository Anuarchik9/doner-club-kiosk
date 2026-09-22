// Run with Playwright available: node tests/browser.cjs
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const page = await browser.newPage({viewport: {width: 1080, height: 1920}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const menu = {success: true, categories: [{id: 'food', name: 'Донеры'}, {id: 'extra', name: 'Дополнительно'}], products: [
    {id: 'doner', itemId: 'doner', categoryId: 'food', name: 'Донер', price: 1500, modifierGroups: []},
    {id: 'custom', itemId: 'custom', categoryId: 'food', name: 'Донер с добавками', price: 1600,
      modifierGroups: [{id: 'sauce', name: 'Соус', minQuantity: 1, maxQuantity: 1, items: [
        {id: 'm1', name: 'Чесночный', price: 100}, {id: 'm2', name: 'Острый', price: 100}]},
        {id: 'extras', name: 'Добавки', minQuantity: 0, maxQuantity: 1, items: [
          {id: 'm3', name: 'Сыр', price: 200}, {id: 'm4', name: 'Халапеньо', price: 100}]}]},
    {id: 'fries', itemId: 'fries', categoryId: 'extra', name: 'Фри', price: 500, modifierGroups: []}
  ]};
  await page.route('**/kiosk-menu?*', route => route.fulfill({json: menu}));
  await page.goto(process.env.KIOSK_URL || 'http://127.0.0.1:10000');
  await page.locator('[data-card-product="doner"]').click();
  await page.locator('#cartHead').click();
  await page.locator('[data-rec-card="fries"]').click();
  assert.match(await page.locator('.cartGrandTotal').innerText(), /2\s*000/);
  assert.equal(await page.locator('.cartRichLine').count(), 2);
  await page.locator('#cartClose').click();
  await page.locator('[data-card-product="custom"]').click();
  assert.equal(await page.locator('#confirmProduct').isDisabled(), true);
  await page.locator('[data-mid="m1"]').check();
  assert.equal(await page.locator('#confirmProduct').isEnabled(), true);
  await page.locator('[data-mid="m3"]').check();
  assert.equal(await page.locator('[data-mid="m4"]').isDisabled(), true);
  await page.locator('[data-mid="m3"]').uncheck();
  assert.equal(await page.locator('[data-mid="m4"]').isEnabled(), true);
  await page.locator('#confirmProduct').click();
  await page.reload();
  await page.locator('#cartHead').click();
  assert.equal(await page.locator('.cartRichLine').count(), 3);
  await page.locator('#goPay').click();
  for (const digit of '7771234567') await page.locator(`[data-phone-key="${digit}"]`).click();
  assert.equal(await page.locator('#phoneDisplay').innerText(), '+7 (777) 123 45 67');
  assert.equal(await page.locator('#phoneContinue').isEnabled(), true);
  await page.locator('#phoneContinue').click();
  await page.locator('#backCart').click();
  assert.equal(await page.locator('#phoneDisplay').innerText(), '+7 (777) 123 45 67');
  for (let i = 0; i < 10; i++) await page.locator('[data-phone-key="⌫"]').click();
  for (const digit of '8701234567') await page.locator(`[data-phone-key="${digit}"]`).click();
  assert.equal(await page.locator('#phoneDisplay').innerText(), '+7 (870) 123 45 67');
  await page.locator('#phoneContinue').click();
  await page.locator('.payChoice').first().click();
  assert.match(await page.locator('.modal').innerText(), /Заказ в iiko не отправлен/);
  await page.locator('#finish').click();
  assert.equal(await page.locator('#checkout').isDisabled(), true);
  await page.locator('#kzBtn').click();
  assert.equal(await page.locator('html').getAttribute('lang'), 'kk');
  await page.locator('#ruBtn').click();
  // A delayed configuration response must not modify a closed dialog.
  await page.route('**/kiosk-iiko-config?*', async route => {
    await new Promise(resolve => setTimeout(resolve, 300));
    await route.fulfill({json: {success: true, terminalGroups: [{id: 't', name: 'Test'}],
      tables: [{id: 'table', terminalGroupId: 't'}]}});
  });
  await page.locator('[data-card-product="doner"]').click();
  await page.locator('#cartHead').click();
  await page.locator('#testIikoBtn').click();
  await page.locator('#testIikoBack').click();
  await page.waitForTimeout(500);
  assert.equal(await page.locator('.cartModal').count(), 1);
  await page.locator('#cartClose').click();
  if (process.env.KIOSK_SCREENSHOT) await page.screenshot({path: process.env.KIOSK_SCREENSHOT});
  const offline = await browser.newPage();
  offline.on('pageerror', error => errors.push(error.message));
  await offline.route('**/kiosk-menu?*', route => route.fulfill({status: 503,
    json: {success: false, code: 'KIOSK_API_NOT_CONFIGURED', message: 'iikoCloud API key is not configured'}}));
  await offline.goto(process.env.KIOSK_URL || 'http://127.0.0.1:10000');
  await offline.getByText('Не удалось загрузить меню. Повторяем подключение…').waitFor();
  assert.equal(await offline.locator('#checkout').isDisabled(), true);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Browser checks passed: cart, recommendations, modifiers, persistence, phone, demo, languages, async dialog.');
})().catch(error => {console.error(error); process.exitCode = 1;});
