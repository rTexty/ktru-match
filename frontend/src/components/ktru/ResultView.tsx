import type { CardField, CardState, Source, Fix, Value, Clarification } from '@/api/ktru'
import { cn } from '@/lib/cn'
import { useEffect, useRef, useState } from 'react'
import { SourceText, TextCorrections } from './SourceText'
import { btn, field, scrollBehavior } from './styles'
import { CodeSummary } from './CodeSummary'
import { CopyButton } from './CopyButton'
import { FieldRow } from './FieldRow'
import { NumericAnswer } from './NumericAnswer'
import { NarrowingFunnel } from './NarrowingFunnel'
import { PipelineProgress } from './PipelineProgress'
import { ProductPhotos } from './ProductPhotos'
import { Icon } from './Icon'
import { Badge, Collapsible, Section, card as cardCls, type Tone } from './ui'

const GROUPS: [Source, string, Tone, string][] = [
  ['ktru', 'Характеристики КТРУ', 'blue', 'КТРУ'],
  ['portal', 'Поля Портала', 'purple', 'Портал'],
  ['new', 'Новые поля', 'gray', 'Новое'],
]

const SEVERITY: Record<'blocker' | 'warning' | 'info', [string, Tone, string]> = {
  blocker: ['Блокирует', 'red', 'border-l-red-500'],
  warning: ['Внимание', 'amber', 'border-l-amber-500'],
  info: ['Инфо', 'gray', 'border-l-slate-300'],
}

export interface Actions {
  busy: boolean
  answer: (questionId: string, optionId: string) => void
  answerValue: (questionId: string, value: Value) => Promise<boolean>
  setField: (label: string, value: string, fieldId: string) => Promise<boolean>
  applyFix: (fix: Fix) => void
}

function fieldKey(field: CardField) {
  return `${field.subject?.id ?? 'item'}:${field.label_ru.toLocaleLowerCase('ru').replaceAll('ё', 'е').trim()}`
}

function questionKey(question: Clarification) {
  return `${question.subject?.id ?? 'item'}:${question.char_name ?? question.question_ru}:${question.value_unit_okei ?? question.value_unit_name ?? ''}`
}

type Draft = { label: string; value: string }
export type RecoveryDraft = Draft & { id: string }

export function ResultView({
  card,
  actions,
  onDraftsChange,
  photosPending = false,
  modelPending = false,
  modelOff = false,
}: {
  card: CardState
  actions: Actions
  onDraftsChange: (drafts: RecoveryDraft[]) => void
  photosPending?: boolean
  modelPending?: boolean
  modelOff?: boolean
}) {
  const [fieldDrafts, setFieldDrafts] = useState<Record<string, Draft>>({})
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, Draft>>({})
  useEffect(() => {
    onDraftsChange([...Object.entries(fieldDrafts).map(([id, draft]) => ({ ...draft, id: `field:${id}` })), ...Object.entries(answerDrafts).map(([id, draft]) => ({ ...draft, id: `answer:${id}` }))])
  }, [fieldDrafts, answerDrafts, onDraftsChange])
  const propertyCounts = card.fields.reduce<Record<string, number>>((counts, field) => {
    const key = fieldKey(field)
    return { ...counts, [key]: (counts[key] ?? 0) + 1 }
  }, {})
  const observationKey = (field: CardField) => (propertyCounts[fieldKey(field)] === 1 ? fieldKey(field) : `${fieldKey(field)}:${field.assertion_ids.join('|') || field.id}`)
  const updateDraft = (kind: 'field' | 'answer', key: string, label: string, value: string | null, savedValue?: string) => {
    const update = kind === 'field' ? setFieldDrafts : setAnswerDrafts
    update((drafts) => {
      if (value !== null) return { ...drafts, [key]: { label, value } }
      if (savedValue !== undefined && drafts[key]?.value !== savedValue) return drafts
      return Object.fromEntries(Object.entries(drafts).filter(([id]) => id !== key))
    })
  }
  const fieldKeys = new Set(card.fields.map(observationKey))
  const numericKey = card.clarification?.accepts_value ? questionKey(card.clarification) : null
  const orphanDrafts = [
    ...Object.entries(fieldDrafts)
      .filter(([key]) => !fieldKeys.has(key))
      .map(([key, draft]) => ({ kind: 'field' as const, key, ...draft })),
    ...Object.entries(answerDrafts)
      .filter(([key]) => key !== numericKey)
      .map(([key, draft]) => ({ kind: 'answer' as const, key, ...draft })),
  ]
  // Selection belongs to one revision; a new card state clears it.
  const [picked, setPicked] = useState<{ rev: number; ids: Set<string> }>({ rev: -1, ids: new Set() })
  const active = picked.rev === card.revision ? picked.ids : new Set<string>()
  const textRef = useRef<HTMLDivElement>(null)
  const [sourceOpen, setSourceOpen] = useState(false)
  const fixedCount = card.corrections.filter((c) => c.target === 'text').length
  const alive = card.candidates.filter((c) => c.status === 'alive')
  const excluded = card.candidates.filter((c) => c.status === 'excluded')

  const select = (ids: string[], scroll = false) => {
    const same = !scroll && ids.length === active.size && ids.every((id) => active.has(id))
    setPicked({ rev: card.revision, ids: same ? new Set() : new Set(ids) })
    if (scroll && ids.length) {
      setSourceOpen(true)
      requestAnimationFrame(() => textRef.current?.scrollIntoView({ block: 'nearest', behavior: scrollBehavior() }))
    }
  }

  const decision = card.final ?? card.suggestion
  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(card, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `ktru-${card.session_id}-r${card.revision}.json`
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  return (
    <div className="@container">
      <div className="grid items-start gap-5 @5xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex min-w-0 flex-col gap-5">
          <CodeSummary card={card} busy={actions.busy} onDownload={download} />

          {/* Problems first: the user sees at once what went wrong and where, before answering or reading the card. */}
          <Diagnostics card={card} actions={actions} select={select} />

          {card.clarification && (
            <section className="border-brand min-w-0 border-l-4 bg-white p-5 sm:p-7">
              <p className="text-brand flex items-center gap-2 text-sm font-semibold">
                <Icon name="info" />
                Ответьте, чтобы выбрать код
              </p>
              <h2 className="text-ink mt-2 text-xl leading-8 font-bold break-words">{card.clarification.question_ru}</h2>
              <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-600">{card.clarification.why_ru}</p>
              <div className="mt-5 grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
                {card.clarification.options.map((o) => (
                  <button
                    key={o.id}
                    type="button"
                    disabled={actions.busy}
                    onClick={() => actions.answer(card.clarification!.id, o.id)}
                    className="border-line hover:border-brand hover:bg-brand-soft/40 flex min-h-14 max-w-full flex-col items-start border bg-white px-4 py-3 text-left text-sm break-words transition-colors disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <span className="text-ink font-semibold">{o.label_ru}</span>
                    {!o.is_dont_know && <span className="text-xs text-slate-500">останется вариантов: {o.remaining_count}</span>}
                  </button>
                ))}
              </div>
              {card.clarification.accepts_value && (
                <NumericAnswer
                  question={card.clarification}
                  actions={actions}
                  value={answerDrafts[questionKey(card.clarification)]?.value ?? ''}
                  onChange={(value, savedValue) =>
                    updateDraft(
                      'answer',
                      questionKey(card.clarification!),
                      `${card.clarification!.char_name ?? card.clarification!.question_ru}${card.clarification!.value_unit_name ? `, ${card.clarification!.value_unit_name}` : ''}`,
                      value,
                      savedValue,
                    )
                  }
                />
              )}
            </section>
          )}

          {orphanDrafts.length > 0 && (
            <section className={`${cardCls} p-5`}>
              <p className="text-sm text-slate-600">Поле или вопрос изменились. Ваш ввод сохранён.</p>
              {orphanDrafts.map((draft) => (
                <div key={`${draft.kind}:${draft.key}`} className="mt-2 flex flex-wrap items-center gap-2">
                  <label className="text-sm" htmlFor={`draft-${draft.kind}-${draft.key}`}>
                    Несохранённая правка: {draft.label}
                  </label>
                  <input id={`draft-${draft.kind}-${draft.key}`} className={`${field} min-h-12 w-full min-w-0 px-3 sm:w-auto`} value={draft.value} onChange={(e) => updateDraft(draft.kind, draft.key, draft.label, e.target.value)} />
                  <CopyButton value={draft.value} label="Копировать правку" />
                  <button type="button" className={btn.small} onClick={() => updateDraft(draft.kind, draft.key, draft.label, null)}>
                    Отмена
                  </button>
                </div>
              ))}
            </section>
          )}

          <Section
            title="Карточка товара"
            right={
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-slate-500">
                  <span className="text-red-600">*</span> — обязательное поле
                </span>
                <button type="button" className={btn.small} onClick={download}>
                  Скачать JSON
                </button>
              </div>
            }
          >
            {card.fields.length === 0 ? (
              <p className="text-sm text-slate-500">Характеристики пока не заполнены.</p>
            ) : (
              <div className="flex flex-col gap-3">
                {GROUPS.map(([source, title, tone, short]) => {
                  const fields = card.fields.filter((f) => f.source === source)
                  if (!fields.length) return null
                  return (
                    <div key={source}>
                      <h3 className="border-line text-ink flex items-center gap-2 border-b-2 pb-1.5 text-sm font-bold">
                        {title} <span className="text-xs font-normal text-slate-400">{fields.length}</span>
                      </h3>
                      <div className="divide-line divide-y">
                        {fields.map((f) => (
                          <FieldRow
                            key={observationKey(f)}
                            field={f}
                            draft={fieldDrafts[observationKey(f)]?.value}
                            onDraft={(value, savedValue) => updateDraft('field', observationKey(f), f.label_ru, value, savedValue)}
                            badge={[short, tone]}
                            actions={actions}
                            selected={f.assertion_ids.some((id) => active.has(id))}
                            onSelect={() => select(f.assertion_ids, true)}
                          />
                        ))}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </Section>

          {decision && <ProductPhotos key={decision.position_ref.code} position={decision.position_ref} text={card.text} waiting={photosPending || actions.busy} />}

          {card.rejected_outputs.length > 0 && (
            <Collapsible title={`Неподтверждённые предложения модели: ${card.rejected_outputs.length}`}>
              <ul className="space-y-2 text-sm">
                {card.rejected_outputs.map((output, index) => (
                  <RejectedProposal key={index} output={output} />
                ))}
              </ul>
            </Collapsible>
          )}

          <div ref={textRef} className="min-w-0 scroll-mt-28">
            <TextCorrections card={card} onFix={actions.applyFix} busy={actions.busy} />
            <Collapsible title={`Разбор описания · фактов: ${card.assertions.length}${fixedCount ? ` · исправлено: ${fixedCount}` : ''}`} open={sourceOpen} onOpenChange={setSourceOpen}>
              <SourceText card={card} active={active} onSelect={(ids) => select(ids)} />
            </Collapsible>
          </div>
        </div>

        <aside aria-label="Подбор кода" className="@container flex min-w-0 flex-col gap-5">
          <PipelineProgress card={card} modelPending={modelPending} modelOff={modelOff} />
          <NarrowingFunnel card={card} />

          <Collapsible title={`Кандидаты: ${alive.length} подходят, ${excluded.length} исключены`}>
            {card.search_scope && <p className="mb-2 text-sm text-slate-600">{card.search_scope.reason_ru}</p>}
            {card.families_top.length > 0 && (
              <div className="mb-3 flex flex-wrap gap-1">
                {card.families_top.map((f) => (
                  <Badge key={f.id} tone={card.family?.id === f.id ? 'blue' : 'gray'}>
                    {f.name} · {f.member_count}
                  </Badge>
                ))}
              </div>
            )}
            <ul className="space-y-1 text-sm">
              {alive.map((c) => (
                <li key={`${c.ref.code}-${c.ref.version}`}>
                  <span className="font-mono">{c.ref.code}</span> {c.name}
                  {c.is_template && (
                    <Badge className="ml-1" tone="gray">
                      шаблон
                    </Badge>
                  )}
                </li>
              ))}
            </ul>
            {excluded.length > 0 && (
              <div className="mt-3">
                <Collapsible title={`Исключённые (${excluded.length})`}>
                  <ul className="space-y-2 text-sm">
                    {excluded.map((c) => (
                      <li key={`${c.ref.code}-${c.ref.version}`} className="text-slate-500">
                        <span className="font-mono">{c.ref.code}</span> {c.name}
                        {c.excluded_by.map((e, i) => (
                          <div key={i} className="pl-4 text-xs text-red-700">
                            — {e.reason_ru}
                          </div>
                        ))}
                      </li>
                    ))}
                  </ul>
                </Collapsible>
              </div>
            )}
          </Collapsible>

          <details className="px-1 text-xs leading-5 break-all text-slate-600">
            <summary className="text-brand min-h-11 py-3 font-semibold">Данные обработки</summary>
            <div>
              {card.timings_ms.total.toFixed(0)} мс · ревизия {card.revision} · сессия {card.session_id.slice(0, 8)} · {card.snapshot_id}
              {card.mode === 'fake' && ' · демонстрационный режим'}
            </div>
          </details>
        </aside>
      </div>
    </div>
  )
}

const SUBJECTS: Record<string, string> = { item: 'товар', package: 'упаковка', set: 'набор', part: 'часть товара', unknown: 'объект не определён' }

type Proposal = { property?: unknown; value?: unknown; unit?: unknown; subject?: unknown; part?: unknown; negated?: unknown; uncertain?: unknown; quote?: unknown }

function parseProposal(raw: string): Proposal | null {
  try {
    const data: unknown = JSON.parse(raw)
    return data && typeof data === 'object' && !Array.isArray(data) && 'property' in data ? data : null
  } catch {
    return null // Cut off at the server's length limit or not JSON: show it as it came.
  }
}

const text = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : typeof v === 'number' ? String(v) : null)

function RejectedProposal({ output }: { output: CardState['rejected_outputs'][number] }) {
  const proposal = parseProposal(output.raw)
  const property = proposal && text(proposal.property)
  const value = proposal && text(proposal.value)
  const unit = proposal && text(proposal.unit)
  const quote = proposal && text(proposal.quote)
  const subject = proposal && text(proposal.subject)
  const part = proposal && text(proposal.part)
  const notes = [
    subject && subject !== 'item' ? `объект: ${SUBJECTS[subject] ?? subject}` : null,
    part ? `часть: ${part}` : null,
    proposal?.negated === true ? 'с отрицанием' : null,
    proposal?.uncertain === true ? 'модель не уверена' : null,
  ].filter((note): note is string => note !== null)
  return (
    <li className="border-l-4 border-l-amber-500 bg-orange-50/50 p-3">
      <p className="text-xs leading-5 text-orange-900">{output.reason_ru}</p>
      {proposal ? (
        <>
          <p className="text-ink mt-1 text-sm leading-6 break-words">
            <span className="font-semibold">{property ?? 'Свойство не указано'}</span>
            {value && <>: {[value, unit].filter(Boolean).join(' ')}</>}
          </p>
          {quote && <blockquote className="mt-1 border-l-2 border-slate-300 pl-3 text-xs leading-5 break-words text-slate-600">Цитата модели: «{quote}»</blockquote>}
          {notes.length > 0 && (
            <p className="mt-1 flex flex-wrap gap-1">
              {notes.map((note) => (
                <Badge key={note}>{note}</Badge>
              ))}
            </p>
          )}
        </>
      ) : (
        <pre className="mt-1 overflow-auto text-xs whitespace-pre-wrap text-slate-600">{output.raw}</pre>
      )}
    </li>
  )
}

function Diagnostics({ card, actions, select }: { card: CardState; actions: Actions; select: (ids: string[], scroll?: boolean) => void }) {
  const searchCorrections = card.corrections.filter((c) => c.target !== 'text') // Text fixes are shown above the description.
  // Catalog defects describe KTRU data, not the supplier's text: keep them out of the way.
  const catalog = card.diagnostics.filter((d) => d.kind === 'schema_defect' && d.severity !== 'blocker')
  const own = card.diagnostics.filter((d) => !catalog.includes(d))
  return (
    <>
      {(own.length > 0 || searchCorrections.length > 0) && (
        <Section title="Проверки" right={<Badge tone={own.some((d) => d.severity === 'blocker') ? 'red' : 'gray'}>{own.length}</Badge>}>
          {searchCorrections.length > 0 && (
            <ul className="mb-3 space-y-1 text-sm">
              {searchCorrections.map((c, i) => (
                <li key={i} className="flex flex-wrap items-center gap-2">
                  <Badge tone="blue">исправлено</Badge>
                  <span className="text-slate-500 line-through">{c.original}</span>→<span className="font-medium">{c.replacement}</span>
                  <span className="text-xs text-slate-500">{c.reason_ru}</span>
                </li>
              ))}
            </ul>
          )}
          <ul className="space-y-2">
            {own.map((d) => {
              const [label, tone, border] = SEVERITY[d.severity]
              return (
                <li key={d.id} className={cn('bg-brand-soft/60 border-l-4 p-3 text-sm', border)}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={tone}>{label}</Badge>
                    <span className="font-medium">{d.message_ru}</span>
                    {d.auto_applied && <Badge tone="green">исправлено автоматически</Badge>}
                    {d.assertion_ids.length > 0 && (
                      <button type="button" className={btn.small} onClick={() => select(d.assertion_ids, true)}>
                        показать в тексте
                      </button>
                    )}
                  </div>
                  {d.reason_ru && <div className="mt-1 text-xs text-slate-500">{d.reason_ru}</div>}
                  {d.fixes.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {d.fixes.map((f) => (
                        <button key={f.id} type="button" disabled={actions.busy} onClick={() => actions.applyFix(f)} className={btn.secondary}>
                          {f.label_ru}
                        </button>
                      ))}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </Section>
      )}
      {catalog.length > 0 && (
        <Collapsible title={`Замечания к данным КТРУ: ${catalog.length}`}>
          <p className="mb-3 text-xs leading-5 text-slate-600">В каталоге у этих характеристик неполные данные. Они не мешают выбрать код, но по ним позиции не исключаются.</p>
          <ul className="space-y-2 text-sm leading-6">
            {catalog.map((d) => (
              <li key={d.id}>{d.message_ru}</li>
            ))}
          </ul>
        </Collapsible>
      )}
    </>
  )
}
