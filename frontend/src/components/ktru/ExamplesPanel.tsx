import { ktruApi, type ExampleDomain } from '@/api/ktru'
import { cn } from '@/lib/cn'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'
import { Icon } from './Icon'
import { btn } from './styles'

const DOMAINS: [ExampleDomain, string][] = [
  ['clothing', 'Одежда'],
  ['furniture', 'Мебель'],
  ['tools', 'Инструменты'],
]

/** Real Portal listings (СТЕ) as product cards; picking one runs it through KTRU matching. */
export function ExamplesPanel({ disabled, onPick, count = 6, title = 'Примеры с Портала поставщиков', className }: { disabled: boolean; onPick: (text: string) => void; count?: number; title?: string; className?: string }) {
  const [domain, setDomain] = useState<ExampleDomain>('furniture')
  const [round, setRound] = useState(0)
  const panelId = useId()
  const tabRefs = useRef<Partial<Record<ExampleDomain, HTMLButtonElement>>>({})
  const { data, isError, isFetching, refetch } = useQuery({
    queryKey: ['ktru', 'examples', domain, round, count],
    queryFn: () => ktruApi.examples(domain, count),
    retry: false,
    staleTime: Infinity,
    placeholderData: keepPreviousData,
  })
  const domainLabel = DOMAINS.find(([id]) => id === domain)?.[1]

  return (
    <section className={cn('min-w-0', className)}>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
        <h2 className="text-ink text-xl font-bold sm:text-2xl">{title}</h2>
        <button type="button" aria-label="Показать другие примеры" className={btn.small} disabled={isFetching} onClick={() => setRound((r) => r + 1)}>
          <Icon name="refresh" />
          Другие товары
        </button>
      </div>
      <div role="tablist" aria-label="Категория примеров" className="border-line mb-5 flex gap-8 overflow-x-auto border-b">
        {DOMAINS.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            type="button"
            aria-selected={domain === id}
            id={`${panelId}-${id}`}
            aria-controls={`${panelId}-panel`}
            tabIndex={domain === id ? 0 : -1}
            ref={(node) => {
              if (node) tabRefs.current = { ...tabRefs.current, [id]: node }
            }}
            onKeyDown={(event) => {
              const index = DOMAINS.findIndex(([key]) => key === id)
              const next = event.key === 'ArrowRight' ? (index + 1) % DOMAINS.length : event.key === 'ArrowLeft' ? (index + DOMAINS.length - 1) % DOMAINS.length : event.key === 'Home' ? 0 : event.key === 'End' ? DOMAINS.length - 1 : null
              if (next !== null) {
                event.preventDefault()
                setDomain(DOMAINS[next][0])
                tabRefs.current[DOMAINS[next][0]]?.focus()
              }
            }}
            onClick={() => setDomain(id)}
            className={cn('-mb-px min-h-12 shrink-0 border-b-[3px] text-sm font-semibold tracking-wider uppercase', domain === id ? 'border-brand text-brand' : 'hover:text-brand border-transparent text-slate-500')}
          >
            {label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`${panelId}-panel`} aria-labelledby={`${panelId}-${domain}`} aria-busy={isFetching}>
        {isError ? (
          <div className="bg-white p-5">
            <p className="text-sm leading-6 text-slate-600">Примеры временно недоступны. Введите описание вручную.</p>
            <button type="button" className={cn(btn.secondary, 'mt-3')} onClick={() => void refetch()}>
              Повторить загрузку
            </button>
          </div>
        ) : !data ? (
          <p className="bg-white p-5 text-sm text-slate-600">Загружаем примеры…</p>
        ) : data.length === 0 ? (
          <p className="bg-white p-5 text-sm text-slate-600">В этой категории пока нет товаров.</p>
        ) : (
          <ul className={cn('grid gap-4 sm:grid-cols-2 lg:grid-cols-3', count > 6 && '2xl:grid-cols-4', isFetching && 'opacity-60')}>
            {data.map((ex) => (
              <li key={ex.ste_id} className="min-w-0">
                <button
                  type="button"
                  disabled={disabled || isFetching}
                  onClick={() => onPick(ex.text)}
                  className="group hover:border-brand flex h-full w-full flex-col border border-transparent bg-white p-5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className="mb-3 flex justify-between gap-3 text-xs text-slate-600">
                    <span>{domainLabel}</span>
                    <span className="tabular-nums">СТЕ {ex.ste_id}</span>
                  </span>
                  <span className="text-ink line-clamp-2 text-base leading-6 font-semibold">{ex.name}</span>
                  <span className="mt-2 mb-5 line-clamp-3 text-sm leading-6 text-slate-600">{ex.text}</span>
                  <span className="text-brand mt-auto inline-flex items-center gap-2 text-sm font-semibold group-hover:underline group-hover:underline-offset-4">
                    <Icon name="search" />
                    Подобрать КТРУ
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {isFetching && data && (
          <p aria-live="polite" className="mt-3 text-xs text-slate-600">
            Обновляем примеры…
          </p>
        )}
      </div>
    </section>
  )
}
