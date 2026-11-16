import { expect, test, type Page } from '@playwright/test'
import type { CardState, Clarification } from '../src/api/ktru'

const TEXT = 'Молоток слесарный, масса 500 г'

function card(overrides: Partial<CardState> = {}): CardState {
  return {
    contract_version: 's0-4',
    session_id: 'ux-session',
    revision: 1,
    request_id: 'ux-parse',
    snapshot_id: 'ux-snapshot',
    mode: 'real',
    status: 'analyzed',
    text: TEXT,
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
    final: null,
    fields: [
      {
        id: 'mass',
        label_ru: 'Масса',
        source: 'new',
        characteristic_ref: null,
        value: { type: 'number', x: '500', unit_name: 'г', unit_okei: '163' },
        display_ru: '500 г',
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
    readiness: 'needs_answers',
    readiness_reasons_ru: [],
    answers_count: 0,
    can_undo: false,
    timings_ms: { parse: 10, retrieve: 0, extract: 0, resolve: 0, total: 10 },
    ...overrides,
  }
}

async function setup(page: Page, state = card()) {
  await page.route('**/api/ktru/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/ktru', '')
    await route.fulfill({
      json:
        path === '/health'
          ? { contract_version: 's0-4', snapshot_id: 'ux-snapshot', snapshot_date: '2026-10-10', counts: { positions_active: 60087 }, ready: true, llm: 'disabled' }
          : path === '/examples'
            ? []
            : path === '/split'
              ? [route.request().postDataJSON().text]
              : state,
    })
  })
  await page.goto('/')
}

async function submit(page: Page) {
  await page.getByLabel('Описание товара', { exact: true }).fill(TEXT)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Карточка товара', exact: true })).toBeVisible()
}

test('canceling a field edit returns focus to its edit action', async ({ page }) => {
  await setup(page)
  await submit(page)
  const edit = page.getByRole('button', { name: 'Изменить', exact: true })
  await edit.click()
  await page.getByLabel('Масса', { exact: true }).press('Escape')
  await expect(edit).toBeFocused()
})

test('example categories support arrow keys and one tab stop', async ({ page }) => {
  await setup(page)
  const furniture = page.getByRole('tab', { name: 'Мебель', exact: true })
  await furniture.focus()
  await furniture.press('ArrowRight')
  const tools = page.getByRole('tab', { name: 'Инструменты', exact: true })
  await expect(tools).toBeFocused()
  await expect(tools).toHaveAttribute('aria-selected', 'true')
  await expect(furniture).toHaveAttribute('tabindex', '-1')
})

test('a changed description is clearly separated from the saved result', async ({ page }) => {
  await setup(page)
  await submit(page)
  await page.getByLabel('Описание товара', { exact: true }).fill('Стол письменный')
  await expect(page.getByText('Описание изменено. Нажмите «Подобрать», чтобы обновить результат.', { exact: true })).toBeVisible()
})

test('a selected code with blockers does not imply a ready card', async ({ page }) => {
  await setup(
    page,
    card({
      readiness: 'has_blockers',
      readiness_reasons_ru: ['Уточните массу'],
      final: { position_ref: { snapshot_id: 'ux-snapshot', code: '25.73.30.140-00000001', version: 1 }, name: 'Молоток', basis_ru: [], conditional_on_family: false, scope: null },
    }),
  )
  await submit(page)
  await expect(page.getByText('Код выбран, но карточка требует исправлений.', { exact: true })).toBeVisible()
  await expect(page.getByText('Карточка готова', { exact: true })).toHaveCount(0)
})

test('numeric answers explain invalid input and accept Enter', async ({ page }) => {
  const question: Clarification = {
    subject: null,
    id: 'mass-question',
    kind: 'characteristic',
    base_revision: 1,
    snapshot_id: 'ux-snapshot',
    question_ru: 'Какова масса?',
    why_ru: 'Уточните значение',
    char_name: 'Масса',
    options: [],
    expected_remaining_worst: 1,
    accepts_value: true,
    value_unit_okei: '163',
    value_unit_name: 'г',
  }
  await setup(page, card({ clarification: question }))
  let answers = 0
  await page.route('**/api/ktru/answer', async (route) => {
    answers += 1
    expect(route.request().postDataJSON().value.x).toBe('123.5')
    await route.fulfill({ json: card({ revision: 2 }) })
  })
  await submit(page)
  const input = page.getByLabel('Ваше значение, г', { exact: true })
  await input.fill('очень тяжёлый')
  await expect(input).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByText('Введите число, например 123,5.', { exact: true })).toBeVisible()
  await input.fill('123,5')
  await input.press('Enter')
  await expect.poll(() => answers).toBe(1)
})

test('copy failure is explained instead of silently ignored', async ({ page }) => {
  await page.addInitScript(() =>
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: async () => {
          throw new Error('permission denied')
        },
      },
      configurable: true,
    }),
  )
  await setup(
    page,
    card({
      final: {
        position_ref: { snapshot_id: 'ux-snapshot', code: '25.73.30.140-00000001', version: 1 },
        name: 'Молоток',
        basis_ru: [],
        conditional_on_family: false,
        scope: null,
      },
    }),
  )
  await submit(page)
  await page.getByRole('button', { name: 'Копировать', exact: true }).click()
  await expect(page.getByText('Не удалось скопировать. Выделите код и скопируйте вручную.', { exact: true })).toBeVisible()
})

test('workspace keeps the description, code and next action on the first screen and wraps long labels on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await setup(
    page,
    card({
      readiness: 'ready',
      final: { position_ref: { snapshot_id: 'ux-snapshot', code: '25.73.30.141-00000001', version: 4 }, name: 'Молоток', basis_ru: [], conditional_on_family: false, scope: null },
    }),
  )
  await submit(page)
  const input = await page.getByLabel('Описание товара', { exact: true }).boundingBox()
  const code = await page.getByTestId('ktru-code').boundingBox()
  const download = await page.getByRole('button', { name: 'Скачать карточку', exact: true }).boundingBox()
  expect(input && code && download && input.y < code.y && download.y + download.height < 900).toBeTruthy()
  // Re-running an unchanged description is not the next step; the ready card's download is.
  await expect(page.getByRole('button', { name: 'Подобрать', exact: true })).not.toHaveClass(/bg-action/)
  await page.setViewportSize({ width: 320, height: 568 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  const codeLines = await page.getByTestId('ktru-code').evaluate((element) => element.getBoundingClientRect().height / Number.parseFloat(getComputedStyle(element).lineHeight))
  expect(codeLines).toBeLessThan(1.5)
  // The example catalog is for starting out; it does not compete with a result.
  await expect(page.getByRole('heading', { name: 'Или выберите товар с Портала поставщиков', exact: true })).toHaveCount(0)
})

test('a linked field opens hidden source evidence before highlighting it', async ({ page }) => {
  const state = card()
  const linked = card({
    fields: [{ ...state.fields[0], assertion_ids: ['mass-fact'] }],
    assertions: [
      {
        id: 'mass-fact',
        subject_kind: 'item',
        subject_label: null,
        subject: null,
        property_raw: 'масса',
        value_raw: '500',
        unit_raw: 'г',
        value: { type: 'number', x: '500', unit_name: 'г', unit_okei: '163' },
        operator: 'eq',
        polarity: 'positive',
        origin: 'text',
        extractor: 'rules',
        span: { start: 25, end: 28, text: '500', text_revision: 1 },
        evidence: [],
        status: 'supported',
        note_ru: null,
      },
    ],
  })
  await setup(page, linked)
  await submit(page)
  const toggle = page.getByRole('button', { name: 'Разбор описания · фактов: 1', exact: true })
  if ((await toggle.getAttribute('aria-expanded')) === 'true') await toggle.click()
  await page.getByRole('button', { name: 'Масса', exact: true }).click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.locator('mark[aria-pressed="true"]')).toBeVisible()
  await toggle.click()
  await page.getByRole('button', { name: 'Масса', exact: true }).click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.locator('mark[aria-pressed="true"]')).toBeVisible()
})

test('session expiry preserves the latest field draft typed while saving', async ({ page }) => {
  await setup(page)
  let release!: () => void
  const reply = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/ktru/edit', async (route) => {
    await reply
    await route.fulfill({
      status: 404,
      json: {
        error: {
          code: 'session_expired',
          message_ru: 'Сессия истекла',
          retryable: false,
        },
      },
    })
  })
  await submit(page)
  await page.getByRole('button', { name: 'Изменить', exact: true }).click()
  const input = page.getByLabel('Масса', { exact: true })
  await input.fill('600 г')
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Сохранить', exact: true })).toBeDisabled()
  await input.fill('700 г')
  release()
  await expect(page.getByLabel('Несохранённая правка: Масса', { exact: true })).toHaveValue('700 г')
  await expect(page.getByLabel('Описание товара', { exact: true })).toHaveValue(TEXT)
})
