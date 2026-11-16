import { readFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'

async function openDemo(page: Page) {
  const problems: string[] = []
  page.on('pageerror', (error) => problems.push(`pageerror: ${error.message}`))
  page.on('request', (request) => {
    if (/\/(?:api)(?:\/|$)/.test(new URL(request.url()).pathname)) {
      problems.push(`API request: ${request.url()}`)
    }
  })
  page.on('requestfailed', (request) => {
    if (new URL(request.url()).origin === new URL(page.url()).origin) {
      problems.push(`failed request: ${request.url()} ${request.failure()?.errorText}`)
    }
  })
  page.on('response', (response) => {
    if (response.status() >= 400 && new URL(response.url()).origin === new URL(page.url()).origin) {
      problems.push(`HTTP ${response.status()}: ${response.url()}`)
    }
  })
  await page.goto('/ktru-match/')
  return () => expect(problems).toEqual([])
}

test('clarifies the sample, matches either answer, and undoes it', async ({ page }) => {
  const checkRequests = await openDemo(page)
  const sample = page.getByRole('radio', { name: 'Молоток', exact: true })
  await expect(sample).toBeChecked()
  await page.getByRole('button', { name: 'Подобрать позицию' }).click()

  const clarification = page.getByRole('group', { name: 'Уточнение' })
  await expect(clarification).toContainText(/масс/i)
  const answer500 = clarification.getByRole('button', { name: /500/ })
  const answer700 = clarification.getByRole('button', { name: /700/ })
  await expect(answer500).toBeVisible()
  await expect(answer700).toBeVisible()
  const result = page.getByRole('region', { name: 'Результат подбора' })
  await expect(result).toBeFocused()
  await answer500.press('Enter')
  await expect(result).toContainText('DEMO-HAMMER-500')
  await expect(result).toBeFocused()
  await page.getByRole('button', { name: 'Отменить ответ' }).click()
  await expect(clarification).toContainText(/масс/i)
  await answer700.click()
  await expect(result).toContainText('DEMO-HAMMER-700')
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Скачать карточку' }).click()
  const download = await downloadPromise
  const card = JSON.parse(await readFile((await download.path())!, 'utf8'))
  expect(card.final.position_ref.code).toBe('DEMO-HAMMER-700')
  await page.reload()
  await expect(sample).toBeChecked()
  await expect(result).toHaveCount(0)
  checkRequests()
})

test('unknown answer does not produce a final card', async ({ page }) => {
  const checkRequests = await openDemo(page)
  await page.getByRole('button', { name: 'Подобрать позицию' }).click()
  await page.getByRole('group', { name: 'Уточнение' }).getByRole('button', { name: /не знаю/i }).click()
  const result = page.getByRole('region', { name: 'Результат подбора' })
  await expect(result.getByText('Код КТРУ', { exact: true })).toHaveCount(0)
  await expect(result.getByRole('button', { name: 'Скачать карточку' })).toHaveCount(0)
  checkRequests()
})

for (const mass of [500, 700]) {
  test(`quick example ${mass} g matches directly`, async ({ page }) => {
    const checkRequests = await openDemo(page)
    await page.getByRole('radio', { name: `Молоток, масса ${mass} г`, exact: true }).check()
    await page.getByRole('button', { name: 'Подобрать позицию' }).click()
    await expect(page.getByRole('region', { name: 'Результат подбора' }))
      .toContainText(`DEMO-HAMMER-${mass}`)
    checkRequests()
  })
}

test('mobile page loads styles and fits the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const checkRequests = await openDemo(page)
  const title = page.getByRole('heading', { name: 'Подбор позиции КТРУ' })
  await expect(title).toBeVisible()
  await expect(title).toHaveCSS('color', 'rgb(27, 57, 102)')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
  await page.getByRole('button', { name: 'Подобрать позицию' }).click()
  await expect(page.getByRole('group', { name: 'Уточнение' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
  checkRequests()
})
