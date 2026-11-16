import { KtruApiError, ktruApi, MAX_SPLIT_TEXT, MAX_TEXT, type CardState, type Fix, type Value } from '@/api/ktru'
import { PageHeader } from '@/components/layout/AppShell'
import { useHealth } from '@/components/layout/useHealth'
import { cn } from '@/lib/cn'
import { useCallback, useEffect, useEffectEvent, useId, useRef, useState } from 'react'
import { ExamplesPanel } from './ExamplesPanel'
import { ResultView, type RecoveryDraft } from './ResultView'
import { btn, field, scrollBehavior } from './styles'
import { Icon } from './Icon'
import { CopyButton } from './CopyButton'
import { card as cardCls } from './ui'
import { ProductCards } from './ProductCards'

const QUICK: [string, string][] = [
  ['Стул', 'Стул на металлическом каркасе, мягкое сиденье, жёсткая спинка'],
  ['Молоток', 'Молоток типа кулачок, масса самого молотка 500 г'],
  ['Неверная высота', 'Стол письменный на металлическом каркасе, высота 5 м'],
  ['Неверная раскладка', 'Cnek yf vtnfkkbxtcrjv rfhrfct'],
]
const STEPS = ['Описание', 'Уточнение', 'Проверка']
const HOW = [
  ['Опишите товар', 'Название, материалы, размеры с единицами измерения. Можно вставить текст из своей карточки.'],
  ['Ответьте на вопросы', 'Если подходят несколько позиций КТРУ, сервис спросит о свойстве, которое лучше всего их различает.'],
  ['Проверьте карточку', 'Характеристики заполнены по КТРУ. Ошибки в описании показаны рядом с вариантом исправления.'],
]

export function KtruPage({
  initialText,
  onInitialTextUsed,
  initialCard,
  onCardChange,
  embedded = false,
  backgroundPending = false,
}: {
  initialText?: string
  onInitialTextUsed?: () => void
  initialCard?: CardState
  onCardChange?: (card: CardState) => void
  embedded?: boolean
  backgroundPending?: boolean
}) {
  const inputId = useId()
  const [draft, setDraft] = useState(initialCard?.text ?? '')
  const [card, setCard] = useState<CardState | null>(initialCard ?? null)
  const [products, setProducts] = useState<string[]>([])
  const [splitText, setSplitText] = useState('')
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [recoveryDrafts, setRecoveryDrafts] = useState<RecoveryDraft[]>([])
  const resultDrafts = useRef<RecoveryDraft[]>([])
  const retainDrafts = useCallback((drafts: RecoveryDraft[]) => {
    resultDrafts.current = drafts
  }, [])
  const generation = useRef(0)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const health = useHealth()
  const [notice, setNotice] = useState<{ tone: 'error' | 'info'; text: string } | null>(null)
  const cardRef = useRef<CardState | null>(initialCard ?? null)

  const apply = (next: CardState) => {
    const cur = cardRef.current
    if (cur && cur.session_id === next.session_id && next.revision < cur.revision) return
    cardRef.current = next
    setCard(next)
    onCardChange?.(next)
  }

  const receiveCard = useEffectEvent((next: CardState) => {
    const cur = cardRef.current
    if (cur === next || (cur && cur.session_id === next.session_id && next.revision <= cur.revision)) return
    setDraft((text) => (text === cur?.text ? next.text : text))
    cardRef.current = next
    setCard(next)
  })
  useEffect(() => {
    if (initialCard) receiveCard(initialCard)
  }, [initialCard])

  const reset = () => {
    generation.current += 1
    setAnalyzing(false)
    resultDrafts.current = []
    cardRef.current = null
    setCard(null)
    setProducts([])
  }

  const handleError = async (e: unknown, token: number, sessionId?: string) => {
    if (token !== generation.current) return
    const err = e instanceof KtruApiError ? e : null
    const cur = cardRef.current
    if (err?.status === 409 && cur && cur.session_id === sessionId) {
      try {
        const refreshed = await ktruApi.session(cur.session_id)
        if (token !== generation.current) return
        apply(refreshed)
        setNotice({ tone: 'info', text: 'Карточка изменилась, показано актуальное состояние. Повторите действие.' })
      } catch {
        if (token === generation.current) setNotice({ tone: 'error', text: err.body.message_ru })
      }
    } else if (err?.status === 404 && cur && cur.session_id === sessionId) {
      const expiredDrafts = [...resultDrafts.current]
      setRecoveryDrafts((drafts) => [...drafts, ...expiredDrafts])
      reset()
      setBusy(false)
      setNotice({ tone: 'info', text: 'Сессия истекла. Текст сохранён — нажмите «Подобрать» ещё раз.' })
    } else {
      setNotice({ tone: 'error', text: err?.body.message_ru ?? 'Не удалось выполнить запрос. Повторите действие.' })
    }
  }

  const analyze = async (preview: CardState, token: number) => {
    if (health.data?.llm === 'disabled') return
    setAnalyzing(true)
    try {
      let next: CardState | null = null
      for (let attempt = 0; attempt <= 6; attempt++) {
        if (token !== generation.current) return
        try {
          next = await ktruApi.analyze(preview)
          break
        } catch (e) {
          if (!(e instanceof KtruApiError) || e.body.code !== 'analysis_busy' || attempt === 6) throw e
          await new Promise((resolve) => setTimeout(resolve, Math.min(500 * 2 ** attempt, 8000)))
        }
      }
      const current = cardRef.current
      if (next && token === generation.current && current?.session_id === preview.session_id && current.revision === preview.revision) apply(next)
    } catch (e) {
      await handleError(e, token, preview.session_id)
    } finally {
      if (token === generation.current) setAnalyzing(false)
    }
  }

  const run = async (fn: () => Promise<CardState>, needsAnalysis = true, syncText = false): Promise<boolean> => {
    const token = ++generation.current
    const sessionId = cardRef.current?.session_id
    const acceptedText = cardRef.current?.text
    setBusy(true)
    setAnalyzing(false)
    setNotice(null)
    try {
      const next = await fn()
      if (token !== generation.current) return false
      apply(next)
      if (syncText) setDraft((currentDraft) => (currentDraft === acceptedText ? next.text : currentDraft))
      if (needsAnalysis && next.status === 'provisional') void analyze(next, token)
      return true
    } catch (e) {
      await handleError(e, token, sessionId)
      return false
    } finally {
      if (token === generation.current) setBusy(false)
    }
  }

  const submit = async (raw = draft) => {
    const text = raw.trim()
    if (!text || busy) return
    if (!embedded) {
      const token = ++generation.current
      setBusy(true)
      setAnalyzing(false)
      setNotice(null)
      try {
        const items = await ktruApi.split(text)
        if (token !== generation.current) return
        if (items.length > 1) {
          reset()
          setProducts(items)
          setSplitText(text)
          setBusy(false)
          return
        }
      } catch (e) {
        await handleError(e, token)
        return
      } finally {
        if (token === generation.current) setBusy(false)
      }
    }
    if (Array.from(text).length > MAX_TEXT) {
      setNotice({ tone: 'error', text: `Описание одного товара превышает ${MAX_TEXT.toLocaleString('ru-RU')} символов. Разделите товары строками или сократите описание. Исходный текст сохранён.` })
      return
    }
    if (!embedded) setProducts([])
    const cur = cardRef.current
    // The server corrects layout and misspellings first; show the corrected text in the box unless the user kept typing.
    void run(
      () =>
        ktruApi.parse(text, cur ?? undefined).then((next) => {
          setDraft((currentDraft) => (currentDraft.trim() === text ? next.text : currentDraft))
          return next
        }),
      true,
    )
  }

  const actions = {
    busy,
    answer: (q: string, o: string) => {
      const cur = cardRef.current
      if (cur) void run(() => ktruApi.answer(cur, q, o))
    },
    answerValue: (questionId: string, value: Value) => {
      const cur = cardRef.current
      return cur ? run(() => ktruApi.answerValue(cur, questionId, value)) : Promise.resolve(false)
    },
    setField: async (label: string, value: string, fieldId: string) => {
      const cur = cardRef.current
      if (!cur) return false
      const success = await run(() => ktruApi.edit(cur, { type: 'set_field', label_ru: label, value, field_id: fieldId }))
      // ResultView retains drafts in a live card. Only keep a separate recovery draft after expiry.
      if (!cardRef.current) setRecoveryDrafts((drafts) => (drafts.length ? drafts : [{ id: `field:${fieldId}`, label, value }]))
      return success
    },
    applyFix: (fix: Fix) => {
      const cur = cardRef.current
      if (!cur || busy) return
      const stale = () => setNotice({ tone: 'info', text: 'Исправление устарело. Выберите исправление из актуальной карточки.' })
      if (fix.base_revision !== cur.revision || fix.snapshot_id !== cur.snapshot_id) {
        stale()
        return
      }
      const patch = fix.patch
      let text: string | null = null
      if (patch.type === 'replace_span') {
        const { span } = patch
        const points = Array.from(cur.text)
        if (
          patch.base_revision !== cur.revision ||
          patch.snapshot_id !== cur.snapshot_id ||
          span.text_revision !== cur.revision ||
          !Number.isInteger(span.start) ||
          !Number.isInteger(span.end) ||
          span.start < 0 ||
          span.end <= span.start ||
          span.end > points.length ||
          points.slice(span.start, span.end).join('') !== span.text
        ) {
          stale()
          return
        }
        text = [...points.slice(0, span.start), patch.replacement, ...points.slice(span.end)].join('')
      } else if (patch.type === 'replace_text') {
        text = patch.text
      }
      if (text !== null) {
        const nextText = text
        setDraft((currentDraft) => (currentDraft === cur.text ? nextText : currentDraft))
      }
      void run(() => ktruApi.edit(cur, patch), true)
    },
  }

  const dirty = card ? draft !== card.text : products.length > 0 && draft.trim() !== splitText
  const step = !card ? 0 : card.readiness === 'ready' ? 2 : 1

  // A product picked on another page arrives as ?text= and runs once.
  const initialUsed = useRef(false)
  const runInitial = useEffectEvent((text: string) => {
    setDraft(text)
    submit(text)
    onInitialTextUsed?.()
  })
  useEffect(() => {
    if (!initialText || initialUsed.current) return
    initialUsed.current = true
    runInitial(initialText)
  }, [initialText])

  // Example buttons say «Подобрать», so they run at once; the text stays in the box for editing.
  const pickExample = (text: string) => {
    setDraft(text)
    void submit(text)
    window.scrollTo({ top: 0, behavior: scrollBehavior() })
  }

  const hero = !card && products.length === 0 && !embedded
  // The red button marks the next thing to do: run a new or edited description. Re-running an unchanged one is secondary.
  const runIsNext = (!card && products.length === 0) || dirty
  const form = (
    <form
      className={cn(cardCls, hero ? 'p-5 sm:p-8' : 'p-5')}
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      <label htmlFor={inputId} className={cn('text-ink mb-1 block font-bold', hero ? 'text-xl' : 'text-lg')}>
        Описание товара
      </label>
      <p id={`${inputId}-hint`} className={cn('text-sm leading-6 text-slate-600', hero ? 'mb-4' : 'mb-3')}>
        {embedded
          ? 'Исправьте описание этого товара и подберите код заново.'
          : hero
            ? 'Укажите товар, его свойства, размеры и единицы измерения. Можно перечислить несколько товаров.'
            : 'Укажите товар, его свойства, размеры и единицы измерения.'}
      </p>
      <textarea
        ref={inputRef}
        id={inputId}
        value={draft}
        maxLength={embedded ? MAX_TEXT : MAX_SPLIT_TEXT}
        rows={hero ? 6 : embedded ? 2 : 3}
        aria-describedby={`${inputId}-hint ${inputId}-count`}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault()
            submit()
          }
        }}
        placeholder="Например: молоток типа кулачок, масса самого молотка 500 г"
        className={cn(field, 'w-full resize-y leading-6', hero ? 'min-h-44 p-4 text-base' : embedded ? 'min-h-16 p-3' : 'min-h-24 p-4')}
      />
      <div className="mt-1 flex justify-between gap-2 text-xs text-slate-600">
        <span>Ctrl / ⌘ + Enter</span>
        <span id={`${inputId}-count`}>{draft.length.toLocaleString('ru-RU')} / {(embedded ? MAX_TEXT : MAX_SPLIT_TEXT).toLocaleString('ru-RU')}</span>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-x-2 gap-y-1 sm:flex-row-reverse sm:justify-between">
        <button type="submit" className={cn(runIsNext ? btn.primary : btn.secondary, 'w-full sm:w-auto sm:px-10')} disabled={busy || !draft.trim()}>
          <Icon name="search" />
          {busy ? 'Подбираем…' : 'Подобрать'}
        </button>
        <div className="flex flex-wrap gap-1">
          {card && (
            <button type="button" className={btn.small} disabled={busy || !card?.can_undo} onClick={() => card && void run(() => ktruApi.undo(card), true, true)}>
              <Icon name="undo" />
              Отменить
            </button>
          )}
          {(card || products.length > 0) && !embedded && (
            <button
              type="button"
              className={btn.small}
              disabled={busy}
              onClick={() => {
                reset()
                setRecoveryDrafts([])
                setDraft('')
                setNotice(null)
                inputRef.current?.focus()
              }}
            >
              <Icon name="plus" />
              Новый товар
            </button>
          )}
        </div>
      </div>
      {dirty && <p className="mt-3 border-l-4 border-amber-500 bg-orange-50 p-3 text-xs leading-5 text-orange-900">Описание изменено. Нажмите «Подобрать», чтобы обновить результат.</p>}
      {notice && (
        <div
          role="status"
          aria-live={notice.tone === 'error' ? 'assertive' : 'polite'}
          className={`mt-3 border-l-4 p-3 text-sm leading-6 ${notice.tone === 'error' ? 'border-red-600 bg-red-50 text-red-800' : 'border-amber-500 bg-orange-50 text-orange-900'}`}
        >
          {notice.text}
        </div>
      )}
      {hero && (
        <div className="border-line mt-6 border-t pt-5">
          <p className="mb-3 text-sm font-semibold text-slate-700">Быстрые примеры</p>
          <ul className="flex flex-wrap gap-2">
            {QUICK.map(([name, text]) => (
              <li key={name}>
                <button type="button" disabled={busy} title={text} onClick={() => pickExample(text)} className={cn(btn.secondary, 'min-h-10 px-4 font-medium')}>
                  {name}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </form>
  )

  // In a product list the description is context, not the next step: show it read-only until the user edits it.
  const description =
    embedded && !editing && !dirty && !notice ? (
      <section className={cn(cardCls, 'flex flex-wrap items-start justify-between gap-x-6 gap-y-2 p-5')} aria-label="Описание этого товара">
        <div className="min-w-0 flex-1">
          <p className="text-xs text-slate-600">Описание товара</p>
          <p className="text-ink mt-1 text-sm leading-6 break-words">{card?.text ?? draft}</p>
        </div>
        <button
          type="button"
          className={btn.small}
          onClick={() => {
            setEditing(true)
            requestAnimationFrame(() => inputRef.current?.focus())
          }}
        >
          <Icon name="edit" />
          Изменить описание
        </button>
      </section>
    ) : (
      form
    )

  const recovery = recoveryDrafts.length > 0 && (
    <section className={`${cardCls} p-5`}>
      <h2 className="text-ink text-sm font-bold">Сохранённые правки</h2>
      <p className="mt-2 text-xs leading-5 text-slate-600">Сессия завершена. Скопируйте правки и внесите их в новую карточку.</p>
      {recoveryDrafts.map((draft, index) => (
        <div key={`${draft.id}:${index}`} className="mt-4">
          <label htmlFor={`recovery-draft-${index}`} className="block text-sm font-medium">
            Несохранённая правка: {draft.label}
          </label>
          <input
            id={`recovery-draft-${index}`}
            value={draft.value}
            onChange={(event) => setRecoveryDrafts((drafts) => drafts.map((item, i) => (i === index ? { ...item, value: event.target.value } : item)))}
            className={`${field} mt-2 min-h-12 w-full min-w-0 px-3`}
          />
          <div className="mt-1 flex flex-wrap gap-2">
            <CopyButton value={draft.value} label="Копировать правку" />
            <button type="button" className={btn.small} onClick={() => setRecoveryDrafts((drafts) => drafts.filter((_, i) => i !== index))}>
              Закрыть
            </button>
          </div>
        </div>
      ))}
    </section>
  )

  const examples = <ExamplesPanel disabled={busy} onPick={pickExample} title="Или выберите товар с Портала поставщиков" className="mx-auto mt-14 max-w-[1440px] px-4 sm:px-6" />
  const result = card && (
    <div className="min-w-0 space-y-4" aria-label="Результат подбора" aria-busy={busy}>
      {(busy || analyzing || backgroundPending) && (
        <div className="border-brand text-brand-dark flex items-start gap-3 border-l-4 bg-white p-4 text-sm" aria-live="polite">
          <Icon name="refresh" className="mt-0.5 shrink-0 motion-safe:animate-spin" />
          <div>
            <p className="font-semibold">{busy ? 'Обновляем карточку' : 'Проверяем описание моделью'}</p>
            <p className="mt-1 text-xs leading-5">{busy ? 'До завершения показан предыдущий результат.' : 'Модель проверяет описание. Можно уточнить карточку или изменить текст.'}</p>
          </div>
        </div>
      )}
      {health.data?.llm === 'disabled' && (
        <div className="flex items-center gap-2 text-xs leading-5 text-slate-600">
          <Icon name="info" className="shrink-0" />
          <p>Модель не подключена. Показан разбор по правилам.</p>
        </div>
      )}
      <ResultView key={card.session_id} card={card} actions={actions} onDraftsChange={retainDrafts} photosPending={busy || analyzing || backgroundPending} modelPending={analyzing || backgroundPending} modelOff={health.data?.llm === 'disabled'} />
    </div>
  )

  if (embedded)
    return (
      <div className="grid gap-4">
        {description}
        {recovery}
        {result}
      </div>
    )

  if (hero) {
    return (
      <>
        <PageHeader crumbs={['Каталог продукции', 'Подбор КТРУ']} title="Подбор кода КТРУ по описанию товара" />
        <div className="mx-auto mt-6 grid max-w-[1440px] items-start gap-5 px-4 sm:px-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          {form}
          <div className="space-y-5">
            <section aria-labelledby="how-title" className="bg-white p-5 sm:p-8">
              <h2 id="how-title" className="text-ink mb-5 text-lg font-bold">
                Как получить карточку
              </h2>
              <ol className="space-y-5">
                {HOW.map(([title, text], index) => (
                  <li key={title} className="flex gap-4">
                    <span aria-hidden="true" className="bg-brand grid h-8 w-8 shrink-0 place-items-center rounded-full text-sm font-bold text-white">
                      {index + 1}
                    </span>
                    <div>
                      <p className="text-ink text-sm font-semibold">{title}</p>
                      <p className="mt-1 text-sm leading-6 text-slate-600">{text}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </section>
            {recovery}
          </div>
        </div>
        {examples}
      </>
    )
  }

  return (
    <>
      <PageHeader
        crumbs={['Каталог продукции', products.length > 0 ? 'Новые товары' : 'Новый товар']}
        title={products.length > 0 ? 'Создание карточек товаров' : 'Создание карточки товара'}
        aside={
          products.length === 0 && (
            <ol aria-label="Этапы подбора" className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
              {STEPS.map((label, index) => (
                <li key={label} aria-current={index === step ? 'step' : undefined} className={`flex items-center gap-2 font-semibold ${index === step ? 'text-brand' : 'text-slate-600'}`}>
                  <span aria-hidden="true" className={`grid h-8 w-8 place-items-center rounded-full ${index === step ? 'bg-brand text-white' : index < step ? 'bg-emerald-600 text-white' : 'bg-white text-slate-600'}`}>
                    {index < step ? <Icon name="check" /> : index + 1}
                  </span>
                  {label}
                </li>
              ))}
            </ol>
          )
        }
      />
      <div className="mx-auto mt-6 grid max-w-[1440px] gap-5 px-4 sm:px-6">
        <section aria-label="Исходное описание" className="min-w-0 space-y-4">
          {form}
          {recovery}
        </section>
        {products.length > 0 && (
          <p className="border-brand text-ink flex items-start gap-3 border-l-4 bg-white p-4 text-sm leading-6">
            <Icon name="info" className="text-brand mt-1 shrink-0" />
            <span>
              В описании {products.length} {plural(products.length, 'товар', 'товара', 'товаров')}. Для каждого подбирается свой код КТРУ. Выберите товар в списке, чтобы проверить и дополнить его карточку.
            </span>
          </p>
        )}
        {products.length > 0 ? <ProductCards texts={products} renderCard={(card, pending, onChange) => <KtruPage embedded initialCard={card} backgroundPending={pending} onCardChange={onChange} />} /> : result}
      </div>
    </>
  )
}

function plural(n: number, one: string, few: string, many: string) {
  const mod10 = n % 10
  const mod100 = n % 100
  return mod10 === 1 && mod100 !== 11 ? one : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? few : many
}
