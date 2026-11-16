import type { CardState, Readiness } from '@/api/ktru'
import { cn } from '@/lib/cn'
import { CopyButton } from './CopyButton'
import { Icon } from './Icon'
import { btn } from './styles'
import { Status, card as cardCls, type Tone } from './ui'

const READINESS: Record<Readiness, [string, Tone]> = {
  ready: ['Карточка готова', 'green'],
  needs_answers: ['Нужны ответы', 'amber'],
  has_blockers: ['Есть блокирующие проблемы', 'red'],
  no_code: ['Код не найден', 'gray'],
}

export function CodeSummary({ card, busy, onDownload }: { card: CardState; busy: boolean; onDownload?: () => void }) {
  const decision = card.final ?? card.suggestion
  const isFinal = !!card.final
  // The code is found and only characteristics are missing: highlight them, never hold the code back (jury: «подсветить ≠ заставить»).
  const [label, tone]: [string, Tone] = isFinal && card.readiness === 'needs_answers' ? ['Код определён', 'green'] : READINESS[card.readiness]
  const ready = card.readiness === 'ready'
  const blocked = card.readiness === 'has_blockers'
  const next = ready
    ? 'Проверьте характеристики и скачайте карточку.'
    : isFinal && blocked
      ? 'Код выбран, но карточка требует исправлений.'
      : isFinal
        ? 'Код определён. Можно дополнить характеристики ниже или скачать карточку как есть.'
        : card.clarification
          ? 'Ответьте на вопрос ниже, чтобы уточнить позицию.'
          : blocked
            ? 'Исправьте блокирующие проблемы ниже.'
            : 'Уточните описание товара или проверьте распознанные характеристики.'

  return (
    <section className={cardCls}>
      <div className="flex flex-col gap-4 p-5 sm:p-7">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Status tone={tone}>{label}</Status>
          <span className="text-xs text-slate-600">{isFinal ? 'Код КТРУ' : decision ? 'Предварительный код КТРУ' : 'Результат подбора'}</span>
        </div>
        {decision ? (
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span data-testid="ktru-code" className="text-ink text-xl leading-tight font-bold tracking-tight break-all tabular-nums sm:text-[32px]">
                {decision.position_ref.code}
              </span>
              <CopyButton value={decision.position_ref.code} disabled={busy} />
            </div>
            <p className="text-ink mt-2 max-w-3xl text-base leading-6 font-semibold break-words sm:text-lg sm:leading-7">{decision.name}</p>
            <p className="mt-1 text-sm text-slate-600">
              {decision.basis_ru.find((line) => line.startsWith('Позиция ')) ?? `Версия ${decision.position_ref.version}`}
              {!isFinal && ', требуется уточнение'}
            </p>
          </div>
        ) : (
          <p className="text-ink text-xl font-bold">Код не определён</p>
        )}
      </div>
      <div
        className={cn(
          'flex flex-wrap items-start gap-3 border-t px-5 py-4 text-sm leading-6 font-semibold sm:px-7',
          blocked ? 'border-red-100 bg-red-50 text-red-900' : ready ? 'border-emerald-100 bg-emerald-50 text-emerald-900' : 'border-line text-brand bg-white',
        )}
      >
        <Icon name={ready ? 'check' : blocked ? 'alert' : 'arrow'} className="mt-1 shrink-0" />
        <p className="flex-1">{next}</p>
        {(ready || (isFinal && !blocked)) && onDownload && (
          <button type="button" className={cn(btn.primary, 'min-h-10 px-4')} onClick={onDownload}>
            <Icon name="download" />
            Скачать карточку
          </button>
        )}
      </div>
      {card.readiness_reasons_ru.length > 0 && (
        <ul className="list-disc space-y-1 py-4 pr-5 pl-10 text-sm leading-6 text-slate-700 sm:pr-7 sm:pl-12">
          {card.readiness_reasons_ru.map((reason, index) => (
            <li key={index}>{reason}</li>
          ))}
        </ul>
      )}
      {decision && decision.basis_ru.length > 0 && (
        <details className="border-line border-t px-5 sm:px-7">
          <summary className="text-brand min-h-12 py-3 text-sm font-semibold">Почему предложен этот код</summary>
          <ul className="mb-5 list-disc space-y-1 pl-5 text-sm leading-6 text-slate-700">
            {decision.basis_ru.map((reason, index) => (
              <li key={index}>{reason}</li>
            ))}
          </ul>
        </details>
      )}
      <p aria-live="polite" aria-atomic="true" className="sr-only">
        {busy ? 'Карточка обновляется.' : `${label}. ${next}`}
      </p>
    </section>
  )
}
