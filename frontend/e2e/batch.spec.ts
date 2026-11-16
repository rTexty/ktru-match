import { expect, test } from '@playwright/test'
import type { CardState } from '../src/api/ktru'

const ITEMS = ['Молоток, масса 500 г', 'Гвозди, длина 50 мм', 'Стул металлический']

function card(text: string, revision = 1, color = 'Чёрный'): CardState {
  return {
    contract_version: 's0-4',
    session_id: `session-${ITEMS.indexOf(text)}`,
    revision,
    request_id: `request-${revision}`,
    snapshot_id: 'batch-snapshot',
    mode: 'real',
    status: revision === 1 ? 'provisional' : 'analyzed',
    text,
    normalized_text: null,
    search_text: null,
    correction_map: [],
    corrections: [],
    domain: null,
    assertions: [],
    subjects: [],
    bindings: [],
    conflicts: [],
    family: null,
    families_top: [],
    search_scope: null,
    funnel: [],
    funnel_scopes: [],
    candidates: [],
    suggestion: null,
    final: { position_ref: { snapshot_id: 'batch-snapshot', code: `code-${ITEMS.indexOf(text)}-${revision}`, version: 1 }, name: text, basis_ru: [], conditional_on_family: false, scope: null },
    fields: [
      {
        id: 'color',
        label_ru: 'Цвет',
        source: 'new',
        characteristic_ref: null,
        value: { type: 'text', s: color },
        display_ru: color,
        assertion_ids: [],
        evidence_ru: null,
        status: 'from_text',
        required: false,
        portal_category: null,
        subject: null,
        evidence: [],
      },
    ],
    diagnostics: [],
    rejected_outputs: [],
    model_manifests: [],
    clarification: null,
    readiness: 'ready',
    readiness_reasons_ru: [],
    answers_count: 0,
    can_undo: false,
    timings_ms: { parse: 0, retrieve: 0, extract: 0, resolve: 0, total: 0 },
  }
}

test('resubmitting one product replaces an old split even after late batch responses', async ({ page }) => {
  const text = 'Диван прямой, виды диванов, секций диванных Диваны'
  const oldItems = ['Диван прямой', 'виды диванов', 'секций диванных Диваны', 'секции диванные']
  let splitCalls = 0
  let release!: () => void
  const pending = new Promise<void>((resolve) => { release = resolve })
  const parsed: string[] = []
  let lateResponses = 0
  let oldRequests = 0
  await page.route('**/api/ktru/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/ktru', '')
    if (path === '/health') await route.fulfill({ json: { ready: true, llm: 'available', counts: {} } })
    else if (path === '/split') {
      expect(route.request().postDataJSON().text).toBe(text)
      await route.fulfill({ json: ++splitCalls === 1 ? oldItems : [text] })
    } else if (path === '/parse') {
      const submitted = route.request().postDataJSON().text as string
      parsed.push(submitted)
      await route.fulfill({ json: { ...card(submitted), session_id: submitted === text ? 'single' : `old-${oldItems.indexOf(submitted)}` } })
    } else if (path === '/analyze') {
      const { session_id } = route.request().postDataJSON()
      if (session_id.startsWith('old-')) {
        oldRequests++
        await pending
        await route.fulfill({ json: { ...card(oldItems[Number(session_id.slice(4))], 2), session_id } })
        lateResponses++
      } else await route.fulfill({ json: { ...card(text, 2), session_id: 'single' } })
    } else await route.fulfill({ json: [] })
  })
  await page.goto('/')
  await page.getByRole('textbox', { name: 'Описание товара', exact: true }).fill(text)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  await expect.poll(() => parsed.length).toBe(4)
  await expect(page.getByRole('navigation', { name: 'Список товаров', exact: true }).getByRole('button')).toHaveCount(4)
  await expect.poll(() => oldRequests).toBeGreaterThan(0)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  await expect.poll(() => parsed.length).toBe(5)
  const pendingCount = oldRequests
  release()
  await expect.poll(() => lateResponses).toBe(pendingCount)
  await expect(page.getByRole('navigation', { name: 'Список товаров', exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Карточка товара', exact: true })).toHaveCount(1)
  await expect(page.getByRole('textbox', { name: 'Описание товара', exact: true })).toHaveValue(text)
})

test('large descriptions reach splitting intact and oversized single cards keep their draft', async ({ page }) => {
  const descriptions = ITEMS.map((text) => `${text}; ${'индивидуальная гравировка логотипа; '.repeat(50)}`)
  const text = descriptions.join('\n')
  const oversized = 'Молоток; ' + 'индивидуальная гравировка логотипа; '.repeat(130)
  const parsed: string[] = []
  await page.route('**/api/ktru/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/ktru', '')
    if (path === '/health') await route.fulfill({ json: { ready: true, llm: 'disabled', counts: {} } })
    else if (path === '/split') {
      const submitted = route.request().postDataJSON().text
      expect(submitted).toBe(submitted === oversized.trim() ? oversized.trim() : text.trim())
      await route.fulfill({ json: submitted === oversized.trim() ? [submitted] : descriptions })
    } else if (path === '/parse') {
      const submitted = route.request().postDataJSON().text
      parsed.push(submitted)
      await route.fulfill({ json: { ...card(submitted), session_id: `large-${descriptions.indexOf(submitted)}` } })
    } else await route.fulfill({ json: [] })
  })
  await page.goto('/')
  const input = page.getByLabel('Описание товара', { exact: true })
  await input.fill(oversized)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Описание одного товара превышает' })).toBeVisible()
  await expect(input).toHaveValue(oversized)
  expect(parsed).toHaveLength(0)
  await input.fill(text)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Обработано 3 из 3' })).toBeVisible()
  expect(parsed.sort()).toEqual([...descriptions].sort())
  const first = page.getByRole('region', { name: 'Товар 1', exact: true })
  await first.getByRole('button', { name: 'Изменить', exact: true }).click()
  const unsaved = first.getByRole('textbox', { name: 'Цвет', exact: true })
  await unsaved.fill('Несохранённый цвет')
  await input.fill(oversized)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Описание одного товара превышает' })).toBeVisible()
  await expect(unsaved).toHaveValue('Несохранённый цвет')
  await expect(page.getByRole('status').filter({ hasText: 'Обработано 3 из 3' })).toBeVisible()
  expect(parsed).toHaveLength(descriptions.length)
})

test('CSV cards stay editable while other rows load and retain drafts when collapsed', async ({ page }) => {
  let release!: () => void
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  const parsed: string[] = []
  await page.route('**/api/ktru/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/ktru', '')
    if (path === '/health') await route.fulfill({ json: { ready: true, llm: 'available', counts: {} } })
    else if (path === '/parse') {
      const { text } = route.request().postDataJSON()
      parsed.push(text)
      await route.fulfill({ json: card(text) })
    } else if (path === '/analyze') {
      const { session_id } = route.request().postDataJSON()
      const index = Number(session_id.split('-').at(-1))
      if (index > 0) await pending
      await route.fulfill({ json: card(ITEMS[index], 2) })
    } else if (path === '/edit') {
      expect(route.request().postDataJSON().session_id).toBe('session-0')
      expect(route.request().postDataJSON().patch.value).toBe('Синий')
      await route.fulfill({ json: card(ITEMS[0], 3, 'Синий') })
    } else await route.fulfill({ json: [] })
  })
  await page.goto('/batch')
  await page.getByLabel('Выбрать CSV', { exact: true }).setInputFiles({
    name: 'products.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(`\uFEFFнаименование товара\n"${ITEMS[0]}"\n"${ITEMS[1]}"\n${ITEMS[2]}`),
  })
  const list = page.getByRole('region', { name: 'Карточки товаров' })
  const progress = list.getByRole('status').filter({ hasText: 'Обработано' })
  await expect(progress).toContainText('Обработано 1 из 3')
  await expect.poll(() => parsed).toEqual(ITEMS)
  const items = list.getByRole('navigation', { name: 'Список товаров' }).getByRole('button')
  const first = list.getByRole('region', { name: 'Товар 1', exact: true })
  await expect(first.getByRole('heading', { name: 'Карточка товара', exact: true })).toBeVisible()
  await expect(page).toHaveURL(/\/batch$/)
  await first.getByRole('button', { name: 'Изменить', exact: true }).click()
  await first.getByLabel('Цвет', { exact: true }).fill('Синий')
  // Switching to another product and back keeps the unsaved value.
  await items.nth(1).click()
  await expect(first).toBeHidden()
  await items.nth(0).click()
  await expect(first.getByLabel('Цвет', { exact: true })).toHaveValue('Синий')
  await first.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(items.nth(0)).toContainText('code-0-3')
  await expect(progress).toContainText('Обработано 1 из 3')
  release()
  await expect(progress).toContainText('Обработано 3 из 3')
  expect(parsed).toHaveLength(3)
  await items.nth(1).click()
  await expect(list.getByRole('heading', { name: 'Карточка товара', exact: true, includeHidden: true })).toHaveCount(2)
  await expect(list.getByRole('heading', { name: 'Карточка товара', exact: true })).toHaveCount(1)
  await expect(list.getByRole('region', { name: 'Товар 2', exact: true })).toBeVisible()
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  const download = page.waitForEvent('download')
  await list.getByRole('button', { name: 'Скачать результаты CSV' }).click()
  expect((await download).suggestedFilename()).toBe('ktru-results.csv')
})

test('several products in one request get a product list with separate sessions', async ({ page }) => {
  const parsed: string[] = []
  await page.route('**/api/ktru/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/ktru', '')
    if (path === '/health') await route.fulfill({ json: { ready: true, llm: 'disabled', counts: {} } })
    else if (path === '/split') await route.fulfill({ json: ITEMS.slice(0, 2) })
    else if (path === '/parse') {
      const { text } = route.request().postDataJSON()
      parsed.push(text)
      await route.fulfill({ json: card(text) })
    } else await route.fulfill({ json: [] })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Молоток массой 500 г и гвозди длиной 50 мм')
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  const list = page.getByRole('region', { name: 'Карточки товаров' })
  await expect(list.getByRole('status').filter({ hasText: 'Обработано' })).toContainText('Обработано 2 из 2')
  expect(parsed).toEqual(ITEMS.slice(0, 2))
  await list.getByRole('navigation', { name: 'Список товаров' }).getByRole('button').nth(1).click()
  const second = list.getByRole('region', { name: 'Товар 2', exact: true })
  await expect(second.getByRole('region', { name: 'Описание этого товара' })).toContainText(ITEMS[1])
  await expect(second.getByTestId('ktru-code')).toHaveText('code-1-1')
  await second.getByRole('button', { name: 'Изменить описание', exact: true }).click()
  await expect(second.getByLabel('Описание товара', { exact: true })).toHaveValue(ITEMS[1])
  await page.getByRole('button', { name: 'Новый товар', exact: true }).click()
  await expect(list).toHaveCount(0)
  await expect(page.getByLabel('Описание товара', { exact: true })).toHaveValue('')
})
