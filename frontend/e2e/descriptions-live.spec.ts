import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'
import { mkdir, writeFile } from 'node:fs/promises'
import type { CardState } from '../src/api/ktru'

test.beforeEach(async () => {
  if (process.env.KTRU_LIVE_E2E) await mkdir(resolve('../.ktru-build/local'), { recursive: true })
})

const WENGER = 'Рюкзак WENGER универсальный, светло-серый, светоотражающие элементы, 22 л, 33х15х45 см. Боковые карманы Да, вентилируемая спинка Да, вентилируемые лямки Да, внутренний органайзер Да, высота 450 мм, кол-во отделений 2 шт, количество карманов 4 шт, масса 660 г, материал полиэстер, наличие ручки для переноса Да, объем 22 Л; ДМ3, отверстие для наушников Нет, перегородка в основном отделении Нет, пол унисекс, регулируемые лямки Да, ремни для утягивания основного отделения Нет, узор Нет, фронтальный карман Да, цвет светло-серый'

test('WENGER stays one product without duplicated additional fields in the live app', async ({ page }) => {
  test.skip(!process.env.KTRU_LIVE_E2E, 'Requires the running KTRU API and local open model')
  test.setTimeout(240_000)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await page.getByRole('textbox', { name: 'Описание товара', exact: true }).fill(WENGER)
  const split = page.waitForResponse((response) => response.url().endsWith('/api/ktru/split'))
  const analyzed = page.waitForResponse((response) => response.url().endsWith('/api/ktru/analyze') && response.ok(), { timeout: 210_000 })
  await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
  expect(await (await split).json()).toEqual([WENGER])
  const response = await analyzed
  expect(response.ok()).toBeTruthy()
  const state = await response.json() as CardState
  await writeFile(resolve('../.ktru-build/local/wenger-state.json'), JSON.stringify(state, null, 2))
  expect(state.status).toBe('analyzed')
  expect(state.text).toBe(WENGER)
  expect(state.assertions.some((assertion) => assertion.extractor === 'llm' || assertion.extractor === 'both')).toBeTruthy()
  const extras = state.fields.filter((field) => field.label_ru === 'Дополнительно')
  expect(new Set(extras.map((field) => field.display_ru)).size).toBe(extras.length)
  expect(extras.filter((field) => field.display_ru.includes('Рюкзак WENGER универсальный')).length).toBeLessThanOrEqual(1)
  expect(extras.some((field) => field.display_ru.includes('Рюкзак WENGER универсальный')
    && field.display_ru.includes('светло-серый'))).toBeFalsy()
  expect(extras.filter((field) => field.display_ru === 'светло-серый').length).toBeLessThanOrEqual(1)
  expect(extras.map((field) => field.display_ru)).not.toContain('цвет светло-серый')
  expect(extras.map((field) => field.display_ru)).not.toContain('высота 450 мм')
  expect(extras.map((field) => field.display_ru)).not.toContain('объем 22 Л')
  await expect(page.getByText('В описании 4 товара', { exact: false })).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: 'Список товаров', exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Создание карточки товара', exact: true })).toBeVisible()
  for (const field of extras) {
    const row = page.getByText('Дополнительно', { exact: true }).locator('..').locator('..')
      .filter({ hasText: field.display_ru })
    await expect(row).toHaveCount(1)
    await expect(row.locator('p').filter({ hasText: `«${field.display_ru}»` })).toHaveCount(0)
  }
  expect(errors).toEqual([])
  await page.screenshot({ path: resolve('../.ktru-build/local/wenger.png'), fullPage: true })
})

const CHAIR = 'Стул офисный, черный, спинка сетчатая, сиденье мягкое, подлокотники Да, колеса Да, механизм качания Да, крестовина пластиковая, высота 900 мм, нагрузка 120 кг. Стул оснащен поясничной поддержкой.'
const DRILL = 'Дрель ударная, мощность 750 Вт, патрон быстрозажимной 13 мм, аккумулятор Нет, подсветка Да, дополнительная рукоятка Да, кейс Да, масса 2 кг.'
const SOFA = 'Диван прямой 2470х900х850 мм, цвет - красный. В составе набора мебели Нет, вид материала ножек/опор Металл, вид материала обивки Ткань мебельная, вид материала подлокотника Дерево, вид подлокотника Жесткий, вид продукции Товары, вид товаров Мебель общего назначения, виды диванов, секций диванных Диваны, виды мебели общего назначения Диваны, секции диванные, высота 850 мм, глубина 900 мм, категория мягкости II, количество посадочных мест 3 шт, наличие механизма раскладывания Нет, наличие подлокотников Да, тип дивана прямой, тип каркаса Металлический, тип напольной опоры Ножки, требования к цветовому решению красный, форма спинки Прямоугольная, ширина 2470 мм'
const DELIVERY = 'К поставке прилагается инструкция по эксплуатации и перечень поставляемых принадлежностей. '
const LONG_ITEMS = ['Молоток, масса 500 г', 'Стул офисный, черный', 'Замок навесной, ширина 50 мм']
  .map((name, index) => `${name}, ${DELIVERY.repeat(15)}маркер-${index + 1}`)
const CASES = [
  { name: 'office chair', text: CHAIR, count: 1 },
  { name: 'impact drill', text: DRILL, count: 1 },
  { name: 'drill with included battery', text: 'Дрель ударная, мощность 750 Вт, аккумулятор в комплекте, патрон быстрозажимной 13 мм, масса 2 кг.', count: 1 },
  { name: 'drill with battery in box', text: 'Дрель ударная, мощность 750 Вт, аккумулятор прилагается в коробке, патрон быстрозажимной 13 мм, масса 2 кг.', count: 1 },
  { name: 'drill with unknown battery in box', text: 'Дрель ударная, мощность 750 Вт, наличие аккумулятора в коробке Нет данных, патрон быстрозажимной 13 мм, масса 2 кг.', count: 1 },
  { name: 'sofa with compound catalog values', text: SOFA, count: 1 },
  { name: 'sofa with unknown mechanism', text: 'Диван прямой, тип каркаса Металлический, вид материала обивки Ткань мебельная, количество посадочных мест 3 шт, наличие механизма раскладывания Нет данных', count: 1 },
  { name: 'three separate products', text: '1. Дрель ударная, мощность 750 Вт, 2 шт\n2. Аккумулятор литий-ионный, 18 В, 2 шт\n3. Кейс пластиковый для инструмента, 2 шт', count: 3 },
  { name: 'large numbered request', text: LONG_ITEMS.map((text, index) => `${index + 1}. ${text}`).join('\n'), count: 3 },
]

for (const example of CASES) {
  test(`${example.name} keeps its product boundaries in the live app`, async ({ page }) => {
    test.skip(!process.env.KTRU_LIVE_E2E, 'Requires the running KTRU API and local open model')
    test.setTimeout(660_000)
    const errors: string[] = []
    const analyzed = new Map<string, CardState>()
    page.on('pageerror', (error) => errors.push(error.message))
    page.on('response', async (response) => {
      if (response.url().endsWith('/api/ktru/analyze') && response.ok()) {
        try {
          const state = await response.json() as CardState
          if (state.status === 'analyzed') analyzed.set(state.session_id, state)
        } catch (error) {
          errors.push(String(error))
        }
      }
    })
    await page.goto('/')
    await page.getByRole('textbox', { name: 'Описание товара', exact: true }).fill(example.text)
    const split = page.waitForResponse((response) => response.url().endsWith('/api/ktru/split'))
    await page.getByRole('button', { name: 'Подобрать', exact: true }).click()
    const descriptions = await (await split).json() as string[]
    expect(descriptions).toHaveLength(example.count)
    await expect.poll(() => analyzed.size, { timeout: 600_000 }).toBe(example.count)
    expect([...analyzed.values()].map((state) => state.text).sort()).toEqual([...descriptions].sort())
    await writeFile(resolve(`../.ktru-build/local/${example.name.replaceAll(' ', '-')}-state.json`),
      JSON.stringify({ split: descriptions, cards: [...analyzed.values()] }, null, 2))
    for (const state of analyzed.values()) {
      expect(state.assertions.some((assertion) => assertion.extractor === 'llm' || assertion.extractor === 'both')).toBeTruthy()
    }
    if (example.count > 1) {
      const list = page.getByRole('navigation', { name: 'Список товаров', exact: true })
      await expect(list.getByRole('button')).toHaveCount(example.count)
      await expect(page.getByRole('status').filter({ hasText: `Обработано ${example.count} из ${example.count}` })).toBeVisible()
      for (const [index, description] of descriptions.entries()) {
        await list.getByRole('button').nth(index).click()
        await expect(page.getByRole('region', { name: 'Описание этого товара', exact: true })).toContainText(description)
      }
    } else {
      expect(descriptions).toEqual([example.text])
      await expect(page.getByRole('navigation', { name: 'Список товаров', exact: true })).toHaveCount(0)
      await expect(page.getByRole('heading', { name: 'Создание карточки товара', exact: true })).toBeVisible()
    }
    if (example.name === 'sofa with compound catalog values') {
      const state = [...analyzed.values()][0]
      const extras = state.fields.filter((field) => field.label_ru.startsWith('Дополнительно'))
      expect(extras.map((field) => field.display_ru)).not.toContain('количество посадочных мест 3 шт')
      expect(extras.map((field) => field.display_ru)).not.toContain('вид материала обивки Ткань мебельная')
      expect(extras.map((field) => field.display_ru)).not.toContain('наличие механизма раскладывания Нет')
      expect(state.fields.find((field) => field.label_ru === 'Наличие механизма раскладывания')?.value)
        .toEqual({ type: 'bool', v: false })
    }
    if (['sofa with unknown mechanism', 'drill with unknown battery in box'].includes(example.name)) {
      const state = [...analyzed.values()][0]
      expect(state.fields.some((field) => field.value?.type === 'bool')).toBeFalsy()
      const unknownFields = state.fields.filter((field) => field.display_ru.includes('Нет данных'))
      expect(unknownFields.length).toBeGreaterThan(0)
      for (const field of unknownFields) {
        const row = page.getByText(field.label_ru, { exact: true }).locator('..').locator('..')
          .filter({ has: page.getByText(field.display_ru, { exact: true }) })
        await expect(row.getByText(field.display_ru, { exact: true })).toBeVisible()
      }
    }
    if (['drill with included battery', 'drill with battery in box'].includes(example.name)) {
      const state = [...analyzed.values()][0]
      const kind = example.name === 'drill with battery in box' ? 'package' : 'part'
      const label = kind === 'package' ? 'коробка' : 'аккумулятор'
      expect(state.assertions.some((assertion) => ['llm', 'both'].includes(assertion.extractor)
        && assertion.value.type === 'bool' && assertion.value.v
        && assertion.subject?.kind === kind && assertion.subject.label === label
        && assertion.span?.text.includes('аккумулятор'))).toBeTruthy()
      const battery = state.fields.find((field) => field.value?.type === 'bool' && field.value.v
        && field.subject?.kind === kind && field.subject.label === label)
      expect(battery).toBeDefined()
      const row = page.getByText(battery!.label_ru, { exact: true }).locator('..').locator('..')
        .filter({ has: page.getByText(battery!.display_ru, { exact: true }) })
      await expect(row.getByText(battery!.display_ru, { exact: true })).toBeVisible()
    }
    if (example.name === 'large numbered request') {
      expect(example.text.length).toBeGreaterThan(4_000)
      expect(descriptions).toEqual(LONG_ITEMS)
    }
    expect(errors).toEqual([])
    await page.screenshot({ path: resolve(`../.ktru-build/local/${example.name.replaceAll(' ', '-')}.png`), fullPage: true })
  })
}
