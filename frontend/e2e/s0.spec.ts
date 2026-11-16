import { expect, test, type Page, type Route } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import type { CardField, CardState, Clarification, Health } from '../src/api/ktru'

const FIRST_TEXT = 'Стул офисный на металлическом каркасе'
const NEXT_TEXT = 'Молоток слесарный с деревянной рукояткой'
const PREVIEW_CODE = '31.01.11.150-00000001'
const ANALYZED_CODE = '31.01.11.150-00000002'
const NEXT_CODE = '25.73.30.140-00000001'

function card(overrides: Partial<CardState> = {}): CardState {
  return {
    contract_version: 's0-4',
    session_id: 'session-first',
    revision: 1,
    request_id: 'parse-first',
    snapshot_id: 'test-snapshot',
    mode: 'real',
    status: 'provisional',
    text: FIRST_TEXT,
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
    suggestion: {
      position_ref: { snapshot_id: 'test-snapshot', code: PREVIEW_CODE, version: 1 },
      name: 'Предварительный стул',
      basis_ru: ['Предварительный результат правил'],
      conditional_on_family: true,
      scope: null,
    },
    final: null,
    fields: [],
    diagnostics: [],
    rejected_outputs: [],
    model_manifests: [],
    clarification: null,
    readiness: 'needs_answers',
    readiness_reasons_ru: ['Нужен анализ описания'],
    answers_count: 0,
    can_undo: false,
    timings_ms: { parse: 10, retrieve: 0, extract: 0, resolve: 0, total: 10 },
    ...overrides,
  }
}

function analyzed(overrides: Partial<CardState> = {}): CardState {
  return card({
    revision: 2,
    request_id: 'analyze-first',
    status: 'analyzed',
    suggestion: null,
    final: {
      position_ref: { snapshot_id: 'test-snapshot', code: ANALYZED_CODE, version: 1 },
      name: 'Стул после анализа',
      basis_ru: ['Описание проверено'],
      conditional_on_family: false,
      scope: null,
    },
    readiness: 'ready',
    readiness_reasons_ru: [],
    ...overrides,
  })
}

function colorField(value = 'Чёрный', id = 'field-color-item'): CardField {
  return {
    id, label_ru: 'Цвет', source: 'new', characteristic_ref: null,
    value: { type: 'text', s: value }, display_ru: value, assertion_ids: [],
    evidence_ru: null, status: 'from_text', required: false, portal_category: null, subject: null, evidence: [],
  }
}

function question(overrides: Partial<Clarification> = {}): Clarification {
  return {
    subject: null,
    id: 'question-first', kind: 'characteristic', base_revision: 1, snapshot_id: 'test-snapshot',
    question_ru: 'Какое сиденье?', why_ru: 'Ответ уточнит позицию', char_name: 'Сиденье',
    options: [{ id: 'option-soft', label_ru: 'Мягкое', remaining_count: 1, is_dont_know: false,
      answer: { type: 'enum', label: 'Мягкое' }, effects: [] }],
    expected_remaining_worst: 1, accepts_value: false, value_unit_okei: null, value_unit_name: null,
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

type ApiHandler = (route: Route) => Promise<void>

async function mockApi(page: Page, handlers: Record<string, ApiHandler>, llm: Health['llm'] = 'available') {
  await page.route('**/api/ktru/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/ktru', '')
    if (path === '/health') {
      await route.fulfill({ json: {
        contract_version: 's0-4', snapshot_id: 'test-snapshot', snapshot_date: '2026-10-10',
        counts: { positions_active: 10 }, ready: true, llm,
      } })
    } else if (path === '/examples') {
      await route.fulfill({ json: [] })
    } else if (path === '/split') {
      await route.fulfill({ json: [route.request().postDataJSON().text] })
    } else if (handlers[path]) {
      await handlers[path](route)
    } else {
      await route.fulfill({ status: 404, json: { error: {
        code: 'not_found', message_ru: `Unexpected test request: ${path}`, retryable: false,
      } } })
    }
  })
}

async function submit(page: Page, text = FIRST_TEXT) {
  await page.getByLabel('Описание товара').fill(text)
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
}

// Model transports can finish after cancellation. The UI must also check request ownership.
async function ignoreAnalyzeCancellation(page: Page) {
  await page.addInitScript(() => {
    const fetch = window.fetch.bind(window)
    window.fetch = (input, init) => fetch(input,
      String(input).endsWith('/api/ktru/analyze') ? { ...init, signal: undefined } : init)
  })
}

async function settleRendering(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
  }))
}

test('shows parse preview, then analyzes the accepted session and revision', async ({ page }) => {
  const reply = deferred<CardState>()
  const requests: Record<string, unknown>[] = []
  await mockApi(page, {
    '/parse': async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({ text: FIRST_TEXT, expected_revision: 0 })
      await route.fulfill({ json: card() })
    },
    '/analyze': async (route) => {
      requests.push(route.request().postDataJSON())
      await route.fulfill({ json: await reply.promise })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect(page.getByTestId('ktru-code')).toHaveText(PREVIEW_CODE)
  await expect.poll(() => requests.length).toBe(1)
  expect(requests[0]).toMatchObject({ session_id: 'session-first', expected_revision: 1 })
  expect(requests[0].request_id).toEqual(expect.any(String))
  expect(requests[0]).not.toHaveProperty('text')
  reply.resolve(analyzed())
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await expect(page.getByText('Карточка готова', { exact: true })).toBeVisible()
})

test('late analysis cannot overwrite a new parse in the same session', async ({ page }) => {
  await ignoreAnalyzeCancellation(page)
  const firstReply = deferred<CardState>()
  const nextReply = deferred<CardState>()
  let analyzes = 0
  await mockApi(page, {
    '/parse': async (route) => {
      const request = route.request().postDataJSON()
      if (request.text === FIRST_TEXT) {
        await route.fulfill({ json: card() })
      } else {
        expect(request).toMatchObject({ session_id: 'session-first', expected_revision: 1, text: NEXT_TEXT })
        await route.fulfill({ json: card({ revision: 2, text: NEXT_TEXT, suggestion: {
          position_ref: { snapshot_id: 'test-snapshot', code: NEXT_CODE, version: 1 },
          name: 'Предварительный молоток', basis_ru: [], conditional_on_family: true, scope: null,
        } }) })
      }
    },
    '/analyze': async (route) => {
      analyzes += 1
      const reply = analyzes === 1 ? firstReply : nextReply
      await route.fulfill({ json: await reply.promise })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect.poll(() => analyzes).toBe(1)
  await submit(page, NEXT_TEXT)
  await expect(page.getByTestId('ktru-code')).toHaveText(NEXT_CODE)
  await expect.poll(() => analyzes).toBe(2)
  const late = page.waitForResponse((response) => response.url().endsWith('/analyze') && response.request().postDataJSON().expected_revision === 1)
  firstReply.resolve(analyzed())
  await (await late).finished()
  await settleRendering(page)
  await expect(page.getByTestId('ktru-code')).toHaveText(NEXT_CODE)
  await expect(page.getByLabel('Описание товара')).toHaveValue(NEXT_TEXT)
  nextReply.resolve(analyzed({ revision: 3, text: NEXT_TEXT, final: {
    position_ref: { snapshot_id: 'test-snapshot', code: NEXT_CODE, version: 1 },
    name: 'Молоток после анализа', basis_ru: [], conditional_on_family: false, scope: null,
  } }))
  await expect(page.getByText('Молоток после анализа', { exact: true })).toBeVisible()
})

test('late analysis cannot restore a card after New product', async ({ page }) => {
  await ignoreAnalyzeCancellation(page)
  const reply = deferred<CardState>()
  let analyzes = 0
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card() }) },
    '/analyze': async (route) => {
      analyzes += 1
      await route.fulfill({ json: await reply.promise })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect.poll(() => analyzes).toBe(1)
  await page.getByRole('button', { name: 'Новый товар', exact: true }).click()
  await expect(page.getByTestId('ktru-code')).toHaveCount(0)
  const late = page.waitForResponse((response) => response.url().endsWith('/analyze'))
  reply.resolve(analyzed())
  await (await late).finished()
  await settleRendering(page)
  await expect(page.getByTestId('ktru-code')).toHaveCount(0)
  await expect(page.getByLabel('Описание товара')).toHaveValue('')
})

test('analysis failure keeps the preview and the current draft', async ({ page }) => {
  const reply = deferred<void>()
  let analyzes = 0
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card() }) },
    '/analyze': async (route) => {
      analyzes += 1
      await reply.promise
      await route.fulfill({ status: 503, json: { error: {
        code: 'unavailable', message_ru: 'Модель временно недоступна', retryable: true,
      } } })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect.poll(() => analyzes).toBe(1)
  await page.getByLabel('Описание товара').fill(NEXT_TEXT)
  reply.resolve()
  await expect(page.getByRole('status')).toContainText('Модель временно недоступна')
  await expect(page.getByTestId('ktru-code')).toHaveText(PREVIEW_CODE)
  await expect(page.getByLabel('Описание товара')).toHaveValue(NEXT_TEXT)
})

test('analysis conflict refreshes the card without replacing the draft', async ({ page }) => {
  const reply = deferred<void>()
  let analyzes = 0
  let refreshes = 0
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card() }) },
    '/analyze': async (route) => {
      analyzes += 1
      await reply.promise
      await route.fulfill({ status: 409, json: { error: {
        code: 'stale_revision', message_ru: 'Данные изменились', retryable: true, current_revision: 3,
      } } })
    },
    '/sessions/session-first': async (route) => {
      refreshes += 1
      await route.fulfill({ json: analyzed({ revision: 3 }) })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect.poll(() => analyzes).toBe(1)
  await page.getByLabel('Описание товара').fill(NEXT_TEXT)
  reply.resolve()
  await expect(page.getByRole('status')).toContainText('показано актуальное состояние')
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await expect(page.getByLabel('Описание товара')).toHaveValue(NEXT_TEXT)
  expect(refreshes).toBe(1)
})

test('parse conflict refreshes the card without replacing the new draft', async ({ page }) => {
  let refreshes = 0
  await mockApi(page, {
    '/parse': async (route) => {
      const request = route.request().postDataJSON()
      if (request.text === FIRST_TEXT) {
        await route.fulfill({ json: card() })
      } else {
        await route.fulfill({ status: 409, json: { error: {
          code: 'stale_revision', message_ru: 'Данные изменились', retryable: true, current_revision: 3,
        } } })
      }
    },
    '/analyze': async (route) => { await route.fulfill({ json: analyzed() }) },
    '/sessions/session-first': async (route) => {
      refreshes += 1
      await route.fulfill({ json: analyzed({ revision: 3 }) })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await submit(page, NEXT_TEXT)
  await expect(page.getByRole('status')).toContainText('показано актуальное состояние')
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await expect(page.getByLabel('Описание товара')).toHaveValue(NEXT_TEXT)
  expect(refreshes).toBe(1)
})

test('disabled model keeps the rules preview without an analysis request', async ({ page }) => {
  let analyzes = 0
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card() }) },
    '/analyze': async (route) => {
      analyzes += 1
      await route.fulfill({ json: analyzed() })
    },
  }, 'disabled')
  await page.goto('/')
  await expect(page.getByText('КТРУ от 2026-10-10 · 10 позиций', { exact: true })).toBeVisible()
  await submit(page)
  await expect(page.getByText('Модель не подключена. Показан разбор по правилам.', { exact: true })).toBeVisible()
  await settleRendering(page)
  await expect(page.getByTestId('ktru-code')).toHaveText(PREVIEW_CODE)
  expect(analyzes).toBe(0)
})

test('field edit sends the current field id with the accepted revision', async ({ page }) => {
  const field = colorField()
  const requests: Record<string, unknown>[] = []
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ fields: [field] }) }) },
    '/edit': async (route) => {
      requests.push(route.request().postDataJSON())
      await route.fulfill({ json: card({ revision: 2, fields: [{
        ...field, value: { type: 'text', s: 'Синий' }, display_ru: 'Синий', status: 'confirmed',
      }] }) })
    },
  }, 'disabled')
  await page.goto('/')
  await expect(page.getByText('КТРУ от 2026-10-10 · 10 позиций', { exact: true })).toBeVisible()
  await submit(page)
  await page.getByRole('button', { name: 'Изменить', exact: true }).click()
  await page.getByLabel('Цвет', { exact: true }).fill('Синий')
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByText('Синий', { exact: true })).toBeVisible()
  expect(requests).toHaveLength(1)
  expect(requests[0]).toMatchObject({
    session_id: 'session-first', expected_revision: 1,
    patch: { type: 'set_field', field_id: 'field-color-item', label_ru: 'Цвет', value: 'Синий' },
  })
})

test('rejected model proposals remain visible with the rejection reason', async ({ page }) => {
  const proposal = '{"property":"Материал","value":"титан","quote":"неизвестная цитата"}'
  const reason = 'Цитата отсутствует в исходном описании'
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card() }) },
    '/analyze': async (route) => {
      await route.fulfill({ json: analyzed({ rejected_outputs: [{ raw: proposal, reason_ru: reason }] }) })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await page.getByRole('button', { name: 'Неподтверждённые предложения модели: 1' }).click()
  await expect(page.getByText(reason, { exact: true })).toBeVisible()
  await expect(page.getByText('Материал: титан', { exact: true })).toBeVisible()
  await expect(page.getByText('Цитата модели: «неизвестная цитата»', { exact: true })).toBeVisible()
})

for (const action of ['answer', 'edit'] as const) {
  test(`${action} during pending analysis starts analysis of the accepted revision`, async ({ page }) => {
    const firstReply = deferred<CardState>()
    const nextReply = deferred<CardState>()
    const requests: Record<string, unknown>[] = []
    const accepted = card({ revision: 2, fields: [colorField('Синий')] })
    await mockApi(page, {
      '/parse': async (route) => {
        await route.fulfill({ json: card({ fields: [colorField()], clarification: question() }) })
      },
      [`/${action}`]: async (route) => {
        const request = route.request().postDataJSON()
        expect(request).toMatchObject({ session_id: 'session-first', expected_revision: 1 })
        if (action === 'answer') expect(request).toMatchObject({ question_id: 'question-first', option_id: 'option-soft' })
        else expect(request.patch).toMatchObject({ field_id: 'field-color-item', value: 'Синий' })
        await route.fulfill({ json: accepted })
      },
      '/analyze': async (route) => {
        requests.push(route.request().postDataJSON())
        await route.fulfill({ json: await (requests.length === 1 ? firstReply.promise : nextReply.promise) })
      },
    })
    await page.goto('/')
    await submit(page)
    await expect.poll(() => requests.length).toBe(1)
    if (action === 'answer') {
      await page.getByRole('button', { name: /^Мягкое(?:\s|$)/ }).click()
    } else {
      await page.getByRole('button', { name: 'Изменить', exact: true }).click()
      await page.getByLabel('Цвет', { exact: true }).fill('Синий')
      await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
    }
    await expect.poll(() => requests.length).toBe(2)
    expect(requests.map((request) => request.expected_revision)).toEqual([1, 2])
    await expect(page.getByText('Синий', { exact: true })).toBeVisible()
    const late = page.waitForResponse((response) => response.url().endsWith('/analyze') && response.request().postDataJSON().expected_revision === 1)
    firstReply.resolve(analyzed())
    await (await late).finished()
    await settleRendering(page)
    await expect(page.getByTestId('ktru-code')).toHaveText(PREVIEW_CODE)
    await expect(page.getByText('Синий', { exact: true })).toBeVisible()
    nextReply.resolve(analyzed({ revision: 3, fields: accepted.fields }))
    await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
    await expect(page.getByText('Синий', { exact: true })).toBeVisible()
  })
}

test('busy analysis retries the same accepted session and revision', async ({ page }) => {
  const requests: Record<string, unknown>[] = []
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card() }) },
    '/analyze': async (route) => {
      requests.push(route.request().postDataJSON())
      if (requests.length === 1) {
        await route.fulfill({ status: 503, json: { error: {
          code: 'analysis_busy', message_ru: 'Анализ занят', retryable: true,
          current_revision: 1, request_id: requests[0].request_id,
        } } })
      } else {
        await route.fulfill({ json: analyzed() })
      }
    },
  })
  await page.goto('/')
  await submit(page)
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  expect(requests).toHaveLength(2)
  for (const request of requests) expect(request).toMatchObject({ session_id: 'session-first', expected_revision: 1 })
  await expect(page.getByRole('status')).toHaveCount(0)
})

for (const action of ['parse', 'reset'] as const) {
  test(`new ${action} stops retries of an earlier busy analysis`, async ({ page }) => {
    await page.clock.install()
    const requests: Record<string, unknown>[] = []
    const busyReply = deferred<void>()
    await mockApi(page, {
      '/parse': async (route) => {
        const request = route.request().postDataJSON()
        await route.fulfill({ json: request.text === FIRST_TEXT ? card() : card({ revision: 2, text: NEXT_TEXT }) })
      },
      '/analyze': async (route) => {
        const request = route.request().postDataJSON()
        requests.push(request)
        if (request.expected_revision === 1) {
          await busyReply.promise
          await route.fulfill({ status: 503, json: { error: {
            code: 'analysis_busy', message_ru: 'Анализ занят', retryable: true,
            current_revision: 1, request_id: request.request_id,
          } } })
        } else {
          await route.fulfill({ json: analyzed({ revision: 3, text: NEXT_TEXT }) })
        }
      },
    })
    await page.goto('/')
    await submit(page)
    await expect.poll(() => requests.length).toBe(1)
    if (action === 'parse') {
      await submit(page, NEXT_TEXT)
      await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
    } else {
      await page.getByRole('button', { name: 'Новый товар', exact: true }).click()
      await expect(page.getByTestId('ktru-code')).toHaveCount(0)
    }
    const late = page.waitForResponse((response) => response.url().endsWith('/analyze') && response.status() === 503)
    busyReply.resolve()
    await (await late).finished()
    await settleRendering(page)
    await page.clock.fastForward(60_000)
    await settleRendering(page)
    expect(requests.filter((request) => request.expected_revision === 1)).toHaveLength(1)
    await expect(page.getByRole('status')).toHaveCount(0)
    if (action === 'reset') await expect(page.getByLabel('Описание товара')).toHaveValue('')
    else await expect(page.getByLabel('Описание товара')).toHaveValue(NEXT_TEXT)
  })
}

test('failed field edit keeps the entered value after refresh changes the field id', async ({ page }) => {
  let edits = 0
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ fields: [colorField()] }) }) },
    '/edit': async (route) => {
      edits += 1
      await route.fulfill({ status: 409, json: { error: {
        code: 'stale_revision', message_ru: 'Карточка изменилась', retryable: true, current_revision: 3,
      } } })
    },
    '/sessions/session-first': async (route) => {
      await route.fulfill({ json: analyzed({ revision: 3, fields: [colorField('Красный', 'field-color-refreshed')] }) })
    },
  }, 'disabled')
  await page.goto('/')
  await expect(page.getByText('КТРУ от 2026-10-10 · 10 позиций', { exact: true })).toBeVisible()
  await submit(page)
  await page.getByRole('button', { name: 'Изменить', exact: true }).click()
  await page.getByLabel('Цвет', { exact: true }).fill('Синий')
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('показано актуальное состояние')
  await expect(page.getByText('Красный', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Несохранённая правка: Цвет', { exact: true })).toHaveValue('Синий')
  expect(edits).toBe(1)
})

test('numeric answer sends a decimal value and the question units', async ({ page }) => {
  const requests: Record<string, unknown>[] = []
  await mockApi(page, {
    '/parse': async (route) => {
      await route.fulfill({ json: card({ clarification: question({
        kind: 'required', question_ru: 'Высота стула?', char_name: 'Высота', options: [],
        accepts_value: true, value_unit_okei: '003', value_unit_name: 'мм',
      }) }) })
    },
    '/answer': async (route) => {
      requests.push(route.request().postDataJSON())
      await route.fulfill({ json: analyzed() })
    },
  }, 'disabled')
  await page.goto('/')
  await expect(page.getByText('КТРУ от 2026-10-10 · 10 позиций', { exact: true })).toBeVisible()
  await submit(page)
  await page.getByLabel('Ваше значение, мм', { exact: true }).fill('123,5')
  await page.getByRole('button', { name: 'Ответить', exact: true }).click()
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  expect(requests).toHaveLength(1)
  expect(requests[0]).toMatchObject({
    session_id: 'session-first', expected_revision: 1, question_id: 'question-first',
    value: { type: 'number', x: '123.5', unit_okei: '003', unit_name: 'мм' },
  })
  expect(requests[0]).not.toHaveProperty('option_id')
})

test('numeric answer draft survives analysis that changes the same question id', async ({ page }) => {
  const reply = deferred<CardState>()
  let analyzes = 0
  const numericQuestion = question({
    kind: 'required', question_ru: 'Высота стула?', char_name: 'Высота', options: [],
    accepts_value: true, value_unit_okei: '003', value_unit_name: 'мм',
  })
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ clarification: numericQuestion }) }) },
    '/analyze': async (route) => {
      analyzes += 1
      await route.fulfill({ json: await reply.promise })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect.poll(() => analyzes).toBe(1)
  await page.getByLabel('Ваше значение, мм', { exact: true }).fill('123,5')
  reply.resolve(analyzed({ clarification: {
    ...numericQuestion, id: 'question-after-analysis', base_revision: 2,
  } }))
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await expect(page.getByLabel('Ваше значение, мм', { exact: true })).toHaveValue('123,5')
})

test('open field edit draft survives analysis that changes its field id', async ({ page }) => {
  const reply = deferred<CardState>()
  let analyzes = 0
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ fields: [colorField()] }) }) },
    '/analyze': async (route) => {
      analyzes += 1
      await route.fulfill({ json: await reply.promise })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect.poll(() => analyzes).toBe(1)
  await page.getByRole('button', { name: 'Изменить', exact: true }).click()
  await page.getByLabel('Цвет', { exact: true }).fill('Синий')
  reply.resolve(analyzed({ fields: [colorField('Чёрный', 'field-color-after-analysis')] }))
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await expect(page.getByLabel('Цвет', { exact: true })).toHaveValue('Синий')
})

test('independent extra fields keep separate edit drafts and target ids', async ({ page }) => {
  const requests: Record<string, unknown>[] = []
  const extras: CardField[] = ['гарантия два года', 'доставка завтра'].map((value, index) => ({
    ...colorField(value, `extra-${index}`), label_ru: 'Дополнительно',
    value: { type: 'unparsed', s: value, reason: 'Свойство ещё не распознано' },
    assertion_ids: [`extra-assertion-${index}`], status: 'unparsed',
    subject: { id: 'item-shared', kind: 'item', label: null, parent_subject_id: null, evidence: [] },
  }))
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ fields: extras }) }) },
    '/edit': async (route) => {
      requests.push(route.request().postDataJSON())
      await route.fulfill({ json: analyzed({ fields: [
        { ...extras[0], display_ru: 'гарантия три года', value: { type: 'text', s: 'гарантия три года' } }, extras[1],
      ] }) })
    },
  }, 'disabled')
  await page.goto('/')
  await expect(page.getByText('КТРУ от 2026-10-10 · 10 позиций', { exact: true })).toBeVisible()
  await submit(page)
  await page.getByRole('button', { name: 'Изменить', exact: true }).nth(0).click()
  const input = page.getByRole('textbox', { name: 'Дополнительно', exact: true })
  await expect(input).toHaveCount(1)
  await expect(input).toHaveValue('гарантия два года')
  await expect(page.getByText('доставка завтра', { exact: true })).toBeVisible()
  await input.fill('гарантия три года')
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  expect(requests).toHaveLength(1)
  expect(requests[0]).toMatchObject({ patch: {
    type: 'set_field', label_ru: 'Дополнительно', field_id: 'extra-0', value: 'гарантия три года',
  } })
  await expect(page.getByText('доставка завтра', { exact: true })).toBeVisible()
})

test('numeric draft stays separate when analysis changes its unit', async ({ page }) => {
  const reply = deferred<CardState>()
  let analyzes = 0
  const numericQuestion = question({
    kind: 'required', question_ru: 'Высота стула?', char_name: 'Высота', options: [],
    accepts_value: true, value_unit_okei: '003', value_unit_name: 'мм',
  })
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ clarification: numericQuestion }) }) },
    '/analyze': async (route) => {
      analyzes += 1
      await route.fulfill({ json: await reply.promise })
    },
  })
  await page.goto('/')
  await submit(page)
  await expect.poll(() => analyzes).toBe(1)
  await page.getByLabel('Ваше значение, мм', { exact: true }).fill('123,5')
  reply.resolve(analyzed({ clarification: {
    ...numericQuestion, id: 'question-after-analysis', base_revision: 2,
    value_unit_okei: '004', value_unit_name: 'см',
  } }))
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await expect(page.getByLabel('Ваше значение, см', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Несохранённая правка: Высота, мм', { exact: true })).toHaveValue('123,5')
})

test('numeric answer response does not clear text entered while saving', async ({ page }) => {
  const reply = deferred<CardState>()
  let answers = 0
  const numericQuestion = question({
    kind: 'required', question_ru: 'Высота стула?', char_name: 'Высота', options: [],
    accepts_value: true, value_unit_okei: '003', value_unit_name: 'мм',
  })
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ clarification: numericQuestion }) }) },
    '/answer': async (route) => {
      answers += 1
      expect(route.request().postDataJSON()).toMatchObject({ value: { type: 'number', x: '123' } })
      await route.fulfill({ json: await reply.promise })
    },
  }, 'disabled')
  await page.goto('/')
  await expect(page.getByText('КТРУ от 2026-10-10 · 10 позиций', { exact: true })).toBeVisible()
  await submit(page)
  const input = page.getByLabel('Ваше значение, мм', { exact: true })
  await input.fill('123')
  await page.getByRole('button', { name: 'Ответить', exact: true }).click()
  await expect.poll(() => answers).toBe(1)
  await input.fill('456')
  reply.resolve(analyzed({ clarification: { ...numericQuestion, id: 'question-next', base_revision: 2 } }))
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  await expect(input).toHaveValue('456')
})

test('Enter cannot submit another field edit while saving', async ({ page }) => {
  const reply = deferred<CardState>()
  let edits = 0
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card({ fields: [colorField()] }) }) },
    '/edit': async (route) => {
      edits += 1
      await route.fulfill({ json: await reply.promise })
    },
  }, 'disabled')
  await page.goto('/')
  await submit(page)
  await page.getByRole('button', { name: 'Изменить', exact: true }).click()
  const input = page.getByLabel('Цвет', { exact: true })
  await input.fill('Синий')
  await input.press('Enter')
  await expect.poll(() => edits).toBe(1)
  await expect(page.getByRole('button', { name: 'Сохранить', exact: true })).toBeDisabled()
  await input.press('Enter')
  await settleRendering(page)
  expect(edits).toBe(1)
  reply.resolve(card({ revision: 2, fields: [colorField('Синий')] }))
  await expect(page.getByText('Синий', { exact: true })).toBeVisible()
})

test('JSON download contains the full current card state', async ({ page }) => {
  const expected = analyzed({ fields: [colorField('Синий')], rejected_outputs: [{ raw: 'invalid proposal', reason_ru: 'Нет цитаты' }] })
  await mockApi(page, {
    '/parse': async (route) => { await route.fulfill({ json: card() }) },
    '/analyze': async (route) => { await route.fulfill({ json: expected }) },
  })
  await page.goto('/')
  await submit(page)
  await expect(page.getByTestId('ktru-code')).toHaveText(ANALYZED_CODE)
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Скачать JSON', exact: true }).click(),
  ])
  expect(download.suggestedFilename()).toBe('ktru-session-first-r2.json')
  const path = await download.path()
  expect(path).not.toBeNull()
  expect(JSON.parse(await readFile(path!, 'utf8'))).toEqual(expected)
})
