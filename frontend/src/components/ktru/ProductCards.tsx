import { KtruApiError, ktruApi, type CardState } from '@/api/ktru'
import { useHealth } from '@/components/layout/useHealth'
import { cn } from '@/lib/cn'
import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from 'react'
import { Icon } from './Icon'
import { btn, scrollBehavior } from './styles'
import { Status, card, type Tone } from './ui'

type Filter = 'all' | 'attention' | 'ready' | 'error' | 'pending'
const FILTERS: [Filter, string][] = [
  ['all', 'Все'],
  ['attention', 'Нужно внимание'],
  ['ready', 'Готово'],
  ['pending', 'В работе'],
  ['error', 'Ошибки'],
]
type Row = { index: number; text: string; state: 'queued' | 'running' | 'done' | 'error'; card?: CardState; error?: string }
// Rules take ~0.1 s, so every row gets its card first; the open model then refines rows with as many
// workers as the API admits (/health llm_concurrency, KTRU_LLM_CONCURRENCY).
const RULES_PARALLEL = 4
const MODEL_PARALLEL_DEFAULT = 2
const READY: Record<CardState['readiness'], [string, Tone]> = {
  ready: ['Готово', 'green'],
  needs_answers: ['Нужно уточнение', 'amber'],
  has_blockers: ['Есть проблемы', 'red'],
  no_code: ['Код не найден', 'gray'],
}

function summary(c: CardState) {
  return {
    decision: c.final ?? c.suggestion,
    alive: c.candidates.filter((x) => x.status === 'alive').length,
    suspicious: c.diagnostics.filter((d) => d.kind === 'implausible').length,
    missing: c.fields.filter((f) => f.required && f.status === 'needs_answer').map((f) => f.label_ru),
  }
}

function csvCell(value: string) {
  return /[",\n;]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value
}

export function ProductCards({ texts, renderCard }: { texts: string[]; renderCard: (card: CardState, pending: boolean, onChange: (card: CardState) => void) => ReactNode }) {
  const health = useHealth()
  const [rows, setRows] = useState<Row[]>([])
  const [visited, setVisited] = useState<Set<number>>(new Set([0]))
  const [selected, setSelected] = useState(0)
  const [filter, setFilter] = useState<Filter>('all')
  const detailRef = useRef<HTMLDivElement>(null)
  const run = useRef(0)

  const update = (index: number, patch: Partial<Row>, token: number) => {
    if (token !== run.current) return
    setRows((all) =>
      all.map((row) => {
        if (row.index !== index) return row
        const next = { ...row, ...patch }
        if (row.card && patch.card && row.card.session_id === patch.card.session_id && row.card.revision > patch.card.revision) next.card = row.card
        return next
      }),
    )
  }

  const process = async (row: Row, token: number, useModel: boolean) => {
    update(row.index, { state: 'running', error: undefined }, token)
    try {
      let c = row.card ?? (await ktruApi.parse(row.text))
      if (token !== run.current) return
      update(row.index, { card: c }, token)
      if (useModel && c.status === 'provisional') {
        for (let attempt = 0; token === run.current; attempt++) {
          try {
            c = await ktruApi.analyze(c)
            break
          } catch (e) {
            // A user may already have answered or edited this card while the queue was analyzing it.
            if (e instanceof KtruApiError && e.status === 409 && e.body.code === 'stale_revision') {
              c = await ktruApi.session(c.session_id)
              break
            }
            // Large batches can outlive the API's session cache; parse the row again and continue.
            if (e instanceof KtruApiError && e.body.code === 'session_expired' && attempt < 6) {
              c = await ktruApi.parse(row.text)
              update(row.index, { card: c }, token)
              continue
            }
            if (!(e instanceof KtruApiError) || e.body.code !== 'analysis_busy' || attempt === 6) throw e
            await new Promise((resolve) => setTimeout(resolve, Math.min(500 * 2 ** attempt, 8000)))
          }
        }
      }
      update(row.index, { card: c, state: 'done' }, token)
    } catch (e) {
      update(row.index, { state: 'error', error: e instanceof Error ? e.message : 'Ошибка обработки' }, token)
    }
  }

  const start = useEffectEvent(async (token: number) => {
    await Promise.resolve()
    if (token !== run.current) return
    const queue: Row[] = texts.map((text, index) => ({ index, text, state: 'queued' }))
    setRows(queue)
    setVisited(new Set([0]))
    setSelected(0)
    setFilter('all')
    const status = health.data ?? (await ktruApi.health().catch(() => null))
    const useModel = status?.llm === 'available'
    const pool = async (rows: Row[], limit: number, job: (row: Row) => Promise<void>) => {
      let next = 0
      const worker = async () => {
        while (next < rows.length && token === run.current) await job(rows[next++])
      }
      await Promise.all(Array.from({ length: Math.min(limit, rows.length) }, worker))
    }
    // Phase 1: rules for every row; each card is published as soon as it is parsed.
    const parsed: Row[] = []
    await pool(queue, RULES_PARALLEL, async (row) => {
      if (!useModel) return process(row, token, false)
      update(row.index, { state: 'running', error: undefined }, token)
      try {
        const c = await ktruApi.parse(row.text)
        update(row.index, { card: c }, token)
        parsed.push({ ...row, card: c })
      } catch (e) {
        update(row.index, { state: 'error', error: e instanceof Error ? e.message : 'Ошибка обработки' }, token)
      }
    })
    // Phase 2: the model refines the parsed rows in order, as many at once as the API admits.
    parsed.sort((a, b) => a.index - b.index)
    await pool(parsed, status?.llm_concurrency ?? MODEL_PARALLEL_DEFAULT, (row) => process(row, token, true))
  })
  useEffect(() => {
    const token = ++run.current
    void start(token)
    return () => {
      run.current = token + 1
    }
  }, [texts])

  const download = () => {
    const lines = [['наименование товара', 'код КТРУ', 'версия', 'наименование КТРУ', 'статус', 'код окончательный', 'подходящих позиций', 'подозрительных свойств', 'не указаны обязательные'].join(',')]
    for (const row of rows) {
      const s = row.card ? summary(row.card) : null
      lines.push(
        [
          row.card?.text ?? row.text,
          s?.decision?.position_ref.code ?? '',
          String(s?.decision?.position_ref.version ?? ''),
          s?.decision?.name ?? '',
          row.error ?? (row.card ? READY[row.card.readiness][0] : row.state),
          row.card?.final ? 'да' : 'нет',
          String(s?.alive ?? ''),
          String(s?.suspicious ?? ''),
          s?.missing.join('; ') ?? '',
        ]
          .map(csvCell)
          .join(','),
      )
    }
    const url = URL.createObjectURL(new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'ktru-results.csv'
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  const done = rows.filter((r) => r.state === 'done' || r.state === 'error').length
  const finals = rows.filter((r) => r.card?.final).length
  const group = (r: Row): Filter => (r.state === 'error' ? 'error' : r.state !== 'done' ? 'pending' : r.card?.readiness === 'ready' ? 'ready' : 'attention')
  const counts = rows.reduce<Record<Filter, number>>((acc, r) => ({ ...acc, [group(r)]: acc[group(r)] + 1 }), { all: rows.length, attention: 0, ready: 0, error: 0, pending: 0 })
  const shown = rows.filter((r) => filter === 'all' || group(r) === filter)
  const current = rows.find((r) => r.index === selected)

  const select = (index: number) => {
    setSelected(index)
    setVisited((all) => (all.has(index) ? all : new Set([...all, index])))
    // On narrow screens the card is below the list: bring it into view.
    if (window.matchMedia('(max-width: 1023px)').matches) requestAnimationFrame(() => detailRef.current?.scrollIntoView({ block: 'start', behavior: scrollBehavior() }))
  }

  return (
    <section className={card} aria-label="Карточки товаров">
      <div className="border-line flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b px-5 py-4 sm:px-7">
        <div className="min-w-0 flex-1">
          <p className="text-ink text-sm font-semibold" role="status">
            Обработано {done} из {texts.length}, окончательный код: {finals}
          </p>
          <div className="bg-brand-soft mt-2 h-1.5 max-w-md" aria-hidden="true">
            <div className="bg-brand h-full transition-[width]" style={{ width: `${texts.length ? (done / texts.length) * 100 : 0}%` }} />
          </div>
        </div>
        <button type="button" className={btn.secondary} disabled={done < texts.length} title={done < texts.length ? 'Станет доступно, когда все товары обработаются' : undefined} onClick={download}>
          <Icon name="download" />
          Скачать результаты CSV
        </button>
      </div>
      {rows.length > 3 && (
        <div role="group" aria-label="Показать товары" className="border-line flex gap-2 overflow-x-auto border-b px-5 py-3 sm:px-7">
          {FILTERS.filter(([id]) => id === 'all' || counts[id] > 0 || filter === id).map(([id, label]) => (
            <button
              key={id}
              type="button"
              aria-pressed={filter === id}
              onClick={() => setFilter(id)}
              className={cn('min-h-9 shrink-0 border px-3 text-xs font-semibold whitespace-nowrap', filter === id ? 'border-brand bg-brand text-white' : 'border-line text-brand hover:border-brand bg-white')}
            >
              {label}: {counts[id]}
            </button>
          ))}
        </div>
      )}
      <div className="grid lg:grid-cols-[340px_minmax(0,1fr)]">
        <nav aria-label="Список товаров" className="border-line max-h-[45vh] overflow-y-auto border-b lg:sticky lg:top-24 lg:max-h-[calc(100vh-7rem)] lg:self-start lg:border-r lg:border-b-0">
          {shown.length === 0 ? (
            <p className="p-5 text-sm text-slate-600">В этой группе нет товаров.</p>
          ) : (
            <ol className="divide-line divide-y">
              {shown.map((r) => {
                const s = r.card ? summary(r.card) : null
                const [label, tone] = rowStatus(r)
                const active = r.index === selected
                return (
                  <li key={r.index}>
                    <button
                      type="button"
                      aria-current={active ? 'true' : undefined}
                      onClick={() => select(r.index)}
                      className={cn('block w-full border-l-4 px-4 py-3 text-left transition-colors sm:px-5', active ? 'border-brand bg-brand-soft' : 'hover:bg-brand-soft/50 border-transparent')}
                    >
                      <span className="flex items-start gap-3">
                        <span className="w-6 shrink-0 pt-0.5 text-xs text-slate-500 tabular-nums">{r.index + 1}</span>
                        <span className="min-w-0 flex-1">
                          <span className="text-ink line-clamp-2 text-sm leading-5 font-semibold break-words">{r.card?.text ?? r.text}</span>
                          <span className="mt-1 block truncate text-xs text-slate-600 tabular-nums">
                            {s?.decision ? `${s.decision.position_ref.code}${r.card?.final ? '' : ', предварительно'}` : r.card ? (s && s.alive > 1 ? `Подходят ${s.alive} позиций` : 'Код не определён') : 'Ожидаем результат'}
                          </span>
                          <span className="mt-1.5 block">
                            <Status tone={tone}>{label}</Status>
                          </span>
                        </span>
                      </span>
                    </button>
                  </li>
                )
              })}
            </ol>
          )}
        </nav>
        <div ref={detailRef} className="min-w-0 scroll-mt-4 bg-slate-50/60 p-4 sm:p-6">
          {current && (
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-ink text-lg font-bold">
                Товар {current.index + 1} из {rows.length}
              </h2>
              <div className="flex gap-1">
                <button type="button" className={btn.small} disabled={current.index === 0} onClick={() => select(current.index - 1)}>
                  <Icon name="chevron" className="rotate-90" />
                  Предыдущий
                </button>
                <button type="button" className={btn.small} disabled={current.index === rows.length - 1} onClick={() => select(current.index + 1)}>
                  Следующий
                  <Icon name="chevron" className="-rotate-90" />
                </button>
              </div>
            </div>
          )}
          {rows.map((r) => {
            const token = run.current
            if (r.index !== selected && !visited.has(r.index)) return null
            return (
              <div key={r.index} hidden={r.index !== selected} role="region" aria-label={`Товар ${r.index + 1}`}>
                {r.error && (
                  <div className="mb-4 flex flex-wrap items-center gap-3 border-l-4 border-red-600 bg-red-50 p-4 text-sm text-red-800" role="alert">
                    <p>{r.error}</p>
                    <button type="button" className={btn.secondary} onClick={() => void process(r, run.current, health.data?.llm === 'available')}>
                      Повторить обработку
                    </button>
                  </div>
                )}
                {r.card
                  ? renderCard(r.card, r.state === 'running', (next) => update(r.index, { card: next }, token))
                  : !r.error && (
                      <div className="flex items-center gap-3 bg-white p-6 text-sm text-slate-600">
                        <Icon name="refresh" className="shrink-0 motion-safe:animate-spin" />
                        {r.state === 'queued' ? 'Товар в очереди. Карточка появится после обработки описания.' : 'Обрабатываем описание…'}
                      </div>
                    )}
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}

function rowStatus(r: Row): [string, Tone] {
  if (r.state === 'error') return ['Ошибка', 'red']
  if (r.card && r.state === 'done') return READY[r.card.readiness]
  return r.state === 'queued' ? ['В очереди', 'gray'] : ['Обработка', 'blue']
}
