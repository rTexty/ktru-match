import { KtruPage } from '@/components/ktru/KtruPage'
import { ProductCards } from '@/components/ktru/ProductCards'
import { card } from '@/components/ktru/ui'
import { btn, field } from '@/components/ktru/styles'
import { PageHeader } from '@/components/layout/AppShell'
import { useHealth } from '@/components/layout/useHealth'
import { cn } from '@/lib/cn'
import { useState } from 'react'
import { Icon } from '@/components/ktru/Icon'

// Jury format: one column, header «наименование товара», one product description per line.
const HEADER = /^\s*"?\s*наименование(\s+товара)?\s*"?\s*$/i
/** One-column CSV: quoted cells may hold commas, quotes («""») and line breaks. */
function parseCsv(raw: string): string[] {
  const cells: string[] = []
  let cell = ''
  let quoted = false
  const text = raw.replace(/^\uFEFF/, '')
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        cell += '"'
        i++
      } else if (ch === '"') quoted = false
      else cell += ch
    } else if (ch === '"' && !cell.trim()) {
      quoted = true
      cell = ''
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      cells.push(cell)
      cell = ''
    } else cell += ch
  }
  cells.push(cell)
  const rows = cells.map((c) => c.trim()).filter(Boolean)
  return rows.length && HEADER.test(rows[0]) ? rows.slice(1) : rows
}

const TEMPLATE = 'наименование товара\nСтол письменный 1200х600 мм, ЛДСП, бук\nХалат детский хлопковый, размер 122\nМолоток слесарный, масса 500 г\n'

function downloadTemplate() {
  const url = URL.createObjectURL(new Blob(['\uFEFF' + TEMPLATE], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = 'ktru-template.csv'
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

export function BatchPage() {
  const health = useHealth()
  const [texts, setTexts] = useState<string[]>([])
  const [source, setSource] = useState('')
  const [pasted, setPasted] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const useModel = health.data?.llm === 'available'

  const start = (raw: string, name: string) => {
    const next = parseCsv(raw)
    if (!next.length) {
      setError('В файле нет строк с описанием товара. Проверьте, что описания идут под заголовком «наименование товара».')
      return
    }
    setError(null)
    setSource(name)
    setTexts(next)
  }

  const onFile = async (file: File | undefined) => {
    if (!file) return
    try {
      start(await file.text(), file.name)
    } catch {
      setError('Не удалось прочитать файл. Выберите его ещё раз.')
    }
  }

  const fileInput = (
    <label className={cn(btn.primary, 'has-focus-visible:outline-brand cursor-pointer has-focus-visible:outline-3 has-focus-visible:outline-offset-3')}>
      <Icon name="upload" />
      Выбрать CSV
      <input
        type="file"
        accept=".csv,text/csv,text/plain"
        className="sr-only"
        onChange={(e) => {
          void onFile(e.target.files?.[0])
          e.target.value = ''
        }}
      />
    </label>
  )

  return (
    <>
      <PageHeader crumbs={['Каталог продукции', 'Загрузка CSV']} title="Подбор КТРУ для списка товаров">
        <p className="mt-3 max-w-3xl text-base leading-7 text-slate-600">Загрузите таблицу с описаниями: сервис подберёт код для каждой строки. Результаты можно проверить по одному и скачать одной таблицей.</p>
      </PageHeader>
      <div className="mx-auto mt-8 grid max-w-[1440px] gap-6 px-4 sm:px-6">
        {texts.length > 0 ? (
          <section className={cn(card, 'flex flex-wrap items-center justify-between gap-4 px-5 py-4 sm:px-7')} aria-label="Загруженный файл">
            <p className="text-ink flex min-w-0 items-center gap-3 text-sm">
              <Icon name="document" className="text-brand h-5 w-5 shrink-0" />
              <span className="min-w-0">
                <span className="block truncate font-semibold">{source}</span>
                <span className="text-slate-600">
                  Строк с товарами: {texts.length}. {useModel ? 'Сначала правила, затем модель уточняет каждую карточку.' : 'Модель не подключена: работает разбор по правилам.'}
                </span>
              </span>
            </p>
            <button
              type="button"
              className={btn.secondary}
              onClick={() => {
                setTexts([])
                setSource('')
              }}
            >
              Загрузить другой файл
            </button>
          </section>
        ) : (
          <section className={cn(card, 'p-5 sm:p-8')} aria-labelledby="upload-title">
            <h2 id="upload-title" className="text-ink text-xl font-bold">
              Загрузите файл
            </h2>
            <div
              onDragOver={(e) => {
                e.preventDefault()
                setDragging(true)
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault()
                setDragging(false)
                void onFile(e.dataTransfer.files[0])
              }}
              className={cn('mt-4 grid place-items-center gap-4 border-2 border-dashed px-5 py-10 text-center transition-colors', dragging ? 'border-brand bg-brand-soft' : 'border-line bg-brand-soft/40')}
            >
              <Icon name="upload" className="text-brand h-8 w-8" />
              <div>
                <p className="text-ink text-base font-semibold">Перетащите CSV сюда или выберите файл</p>
                <p className="mt-1 text-sm leading-6 text-slate-600">Один столбец с заголовком «наименование товара», по одному товару в строке.</p>
              </div>
              {fileInput}
              <button type="button" className={btn.small} onClick={downloadTemplate}>
                <Icon name="download" />
                Скачать пример файла
              </button>
            </div>
            {error && (
              <p role="alert" className="mt-4 border-l-4 border-red-600 bg-red-50 p-3 text-sm leading-6 text-red-800">
                {error}
              </p>
            )}
            <details className="border-line mt-6 border-t pt-2">
              <summary className="text-brand flex min-h-12 items-center text-sm font-semibold">Нет файла? Вставьте строки вручную</summary>
              <div className="mt-2 grid gap-3">
                <textarea
                  aria-label="Строки с товарами"
                  className={cn(field, 'min-h-32 w-full p-3 leading-6')}
                  placeholder={'Стол письменный 1200х600 мм, ЛДСП, бук\nХалат детский хлопковый, размер 122'}
                  value={pasted}
                  onChange={(e) => setPasted(e.target.value)}
                />
                <button type="button" className={cn(btn.secondary, 'justify-self-start')} disabled={!pasted.trim()} onClick={() => start(pasted, 'Вставленные строки')}>
                  Подобрать для этих строк
                </button>
              </div>
            </details>
          </section>
        )}

        {texts.length > 0 && <ProductCards texts={texts} renderCard={(card, pending, onChange) => <KtruPage embedded initialCard={card} backgroundPending={pending} onCardChange={onChange} />} />}
      </div>
    </>
  )
}
