import { expect, test, type Page, type Route } from '@playwright/test'
import type { CardState, Diagnostic, Fix } from '../src/api/ktru'

const ORIGINAL = '🪑 стул синий, стул синий'
const CHANGED = '🪑 стул синий, стул красный'
const SNAPSHOT = 'contract-snapshot'
const STALE_NOTICE = 'Исправление устарело. Выберите исправление из актуальной карточки.'

function spanFix(): Fix {
  return {
    id: 'fix-second-color', label_ru: 'Исправить второй цвет', snapshot_id: SNAPSHOT, base_revision: 1,
    patch: {
      type: 'replace_span', span: { start: 19, end: 24, text: 'синий', text_revision: 1 },
      replacement: 'красный', snapshot_id: SNAPSHOT, base_revision: 1,
    },
  }
}

function diagnostic(fix: Fix): Diagnostic {
  return {
    id: 'second-color', kind: 'typo', severity: 'warning', assertion_ids: [], char_names: [],
    subject: null, characteristic_refs: [], evidence: [],
    message_ru: 'Проверьте второй цвет', reason_ru: 'Исправление относится к одному фрагменту.',
    fixes: [fix], auto_applied: false,
  }
}

function card(overrides: Partial<CardState> = {}): CardState {
  return {
    contract_version: 's0-4', session_id: 'contract-session', revision: 1, request_id: 'parse-contract',
    snapshot_id: SNAPSHOT, mode: 'real', status: 'provisional', text: ORIGINAL,
    normalized_text: null, search_text: null, correction_map: [], corrections: [], domain: null,
    assertions: [], subjects: [], bindings: [], conflicts: [], family: null, families_top: [],
    search_scope: null, funnel: [], funnel_scopes: [], candidates: [], suggestion: null, final: null,
    fields: [], diagnostics: [diagnostic(spanFix())], rejected_outputs: [], model_manifests: [],
    clarification: null, readiness: 'needs_answers', readiness_reasons_ru: [], answers_count: 0,
    can_undo: false, timings_ms: { parse: 0, retrieve: 0, extract: 0, resolve: 0, total: 0 },
    ...overrides,
  }
}

async function mockApi(page: Page, edit: (route: Route) => Promise<void>, refreshed?: CardState) {
  await page.route('**/api/ktru/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/ktru', '')
    if (path === '/health') {
      await route.fulfill({ json: {
        contract_version: 's0-4', snapshot_id: SNAPSHOT, snapshot_date: '2026-10-10',
        counts: {}, ready: true, llm: 'disabled',
      } })
    } else if (path === '/examples') {
      await route.fulfill({ json: [] })
    } else if (path === '/split') {
      await route.fulfill({ json: [route.request().postDataJSON().text] })
    } else if (path === '/parse') {
      await route.fulfill({ json: card() })
    } else if (path === '/edit') {
      await edit(route)
    } else if (path === '/sessions/contract-session' && refreshed) {
      await route.fulfill({ json: refreshed })
    } else {
      await route.fulfill({ status: 404, json: { error: {
        code: 'not_found', message_ru: `Unexpected test request: ${path}`, retryable: false,
      } } })
    }
  })
}

async function submit(page: Page) {
  await page.goto('/')
  await page.getByLabel('Описание товара').fill(ORIGINAL)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Исправить второй цвет', exact: true })).toBeVisible()
}

test('span fix sends exact server patch and replaces only the selected Unicode span', async ({ page }) => {
  const requests: Record<string, unknown>[] = []
  await mockApi(page, async (route) => {
    requests.push(route.request().postDataJSON())
    await route.fulfill({ json: card({ revision: 2, text: CHANGED, diagnostics: [] }) })
  })
  await submit(page)
  await page.getByRole('button', { name: 'Исправить второй цвет', exact: true }).click()
  await expect(page.getByLabel('Описание товара')).toHaveValue(CHANGED)
  await expect.poll(() => requests.length).toBe(1)
  expect(requests[0]).toMatchObject({ session_id: 'contract-session', expected_revision: 1, patch: spanFix().patch })
  await expect(page.getByRole('button', { name: 'Исправить второй цвет', exact: true })).toHaveCount(0)
})

test('span fix preserves an unsent description draft', async ({ page }) => {
  const requests: Record<string, unknown>[] = []
  await mockApi(page, async (route) => {
    requests.push(route.request().postDataJSON())
    await route.fulfill({ json: card({ revision: 2, text: CHANGED, diagnostics: [] }) })
  })
  await submit(page)
  const draft = `${ORIGINAL}, доставка завтра`
  await page.getByLabel('Описание товара').fill(draft)
  await page.getByRole('button', { name: 'Исправить второй цвет', exact: true }).click()
  await expect.poll(() => requests.length).toBe(1)
  await expect(page.getByRole('button', { name: 'Исправить второй цвет', exact: true })).toHaveCount(0)
  await expect(page.getByLabel('Описание товара')).toHaveValue(draft)
  expect(requests[0]).toMatchObject({ patch: spanFix().patch })
})

test('rejected stale span fix refreshes the card and cannot be reused', async ({ page }) => {
  const requests: Record<string, unknown>[] = []
  await mockApi(page, async (route) => {
    requests.push(route.request().postDataJSON())
    await route.fulfill({ status: 409, json: { error: {
      code: 'stale_revision', message_ru: 'Карточка изменилась', retryable: true,
      current_revision: 2, request_id: null,
    } } })
  }, card({ revision: 2 }))
  await submit(page)
  await page.getByRole('button', { name: 'Исправить второй цвет', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('показано актуальное состояние')
  await expect(page.getByLabel('Описание товара')).toHaveValue(CHANGED)
  await page.getByRole('button', { name: 'Исправить второй цвет', exact: true }).click()
  await expect(page.getByRole('status')).toHaveText(STALE_NOTICE)
  expect(requests).toHaveLength(1)
})

test('pending span fix blocks another submission and preserves text entered while saving', async ({ page }) => {
  let release!: () => void
  const saving = new Promise<void>((resolve) => { release = resolve })
  const requests: Record<string, unknown>[] = []
  await mockApi(page, async (route) => {
    requests.push(route.request().postDataJSON())
    await saving
    await route.fulfill({ json: card({ revision: 2, text: CHANGED, diagnostics: [] }) })
  })
  await submit(page)
  const fix = page.getByRole('button', { name: 'Исправить второй цвет', exact: true })
  await fix.click()
  await expect.poll(() => requests.length).toBe(1)
  await expect(fix).toBeDisabled()
  const draft = `${CHANGED}, доставка завтра`
  const input = page.getByLabel('Описание товара')
  await input.fill(draft)
  await input.press('Control+Enter')
  await expect(input).toHaveValue(draft)
  expect(requests).toHaveLength(1)
  release()
  await expect(fix).toHaveCount(0)
  await expect(input).toHaveValue(draft)
  expect(requests).toHaveLength(1)
})

for (const draftTiming of ['clean', 'before', 'during'] as const) {
  test(`undo span fix restores accepted text and preserves ${draftTiming} description draft`, async ({ page }) => {
    let release!: () => void
    const undoReply = new Promise<void>((resolve) => { release = resolve })
    const undos: Record<string, unknown>[] = []
    await mockApi(page, async (route) => {
      await route.fulfill({ json: card({ revision: 2, text: CHANGED, diagnostics: [], can_undo: true }) })
    })
    await page.route('**/api/ktru/undo', async (route) => {
      undos.push(route.request().postDataJSON())
      if (draftTiming === 'during') await undoReply
      await route.fulfill({ json: card({ revision: 3, text: ORIGINAL, diagnostics: [], can_undo: false }) })
    })
    await submit(page)
    await page.getByRole('button', { name: 'Исправить второй цвет', exact: true }).click()
    const input = page.getByLabel('Описание товара')
    const undo = page.getByRole('button', { name: 'Отменить', exact: true })
    await expect(input).toHaveValue(CHANGED)
    await expect(undo).toBeEnabled()
    const draft = `${CHANGED}, доставка завтра`
    if (draftTiming === 'before') await input.fill(draft)
    await undo.click()
    await expect.poll(() => undos.length).toBe(1)
    if (draftTiming === 'during') {
      await expect(undo).toBeDisabled()
      await input.fill(draft)
      release()
    }
    await expect(page.getByRole('button', { name: 'Подобрать', exact: true })).toBeEnabled()
    await expect(undo).toBeDisabled()
    await expect(input).toHaveValue(draftTiming === 'clean' ? ORIGINAL : draft)
    expect(undos[0]).toMatchObject({ session_id: 'contract-session', expected_revision: 2 })
  })
}

for (const invalid of ['snapshot', 'revision', 'span_revision', 'quote', 'bounds', 'empty'] as const) {
  test(`span fix rejects a stale or invalid ${invalid} before submission`, async ({ page }) => {
    let edits = 0
    const fix = spanFix()
    const patch = fix.patch
    if (patch.type !== 'replace_span') throw new Error('Expected a span fix')
    const invalidPatch = {
      ...patch,
      ...(invalid === 'snapshot' ? { snapshot_id: 'other-snapshot' } : {}),
      ...(invalid === 'revision' ? { base_revision: 0 } : {}),
      span: {
        ...patch.span,
        ...(invalid === 'span_revision' ? { text_revision: 0 } : {}),
        ...(invalid === 'quote' ? { text: 'чёрный' } : {}),
        ...(invalid === 'bounds' ? { start: -1, end: 4 } : {}),
        ...(invalid === 'empty' ? { start: 19, end: 19, text: '' } : {}),
      },
    }
    await mockApi(page, async (route) => {
      edits += 1
      await route.fulfill({ json: card({ revision: 2 }) })
    })
    await page.route('**/api/ktru/parse', async (route) => {
      await route.fulfill({ json: card({ diagnostics: [diagnostic({ ...fix, patch: invalidPatch })] }) })
    })
    await submit(page)
    await page.getByRole('button', { name: 'Исправить второй цвет', exact: true }).click()
    await expect(page.getByRole('status')).toHaveText(STALE_NOTICE)
    await expect(page.getByLabel('Описание товара')).toHaveValue(ORIGINAL)
    expect(edits).toBe(0)
  })
}
