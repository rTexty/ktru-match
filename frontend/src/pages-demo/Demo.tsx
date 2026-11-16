import { useEffect, useRef, useState } from 'react'
import type { CardState } from '@/api/ktru.generated'
import { CodeSummary } from '@/components/ktru/CodeSummary'
import { NarrowingFunnel } from '@/components/ktru/NarrowingFunnel'
import { btn } from '@/components/ktru/styles'
import { cn } from '@/lib/cn'

export type DemoScenario = {
  description: string
  card: CardState
  answers: Record<string, CardState>
}

function downloadCard(card: CardState) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(card, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'ktru-demo-card.json'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1_000)
}

export function Demo({ scenarios }: { scenarios: DemoScenario[] }) {
  const [selected, setSelected] = useState(0)
  const [submitted, setSubmitted] = useState(false)
  const [answerId, setAnswerId] = useState<string | null>(null)
  const resultRef = useRef<HTMLElement>(null)
  const scenario = scenarios[selected]
  const card = submitted ? (answerId ? scenario.answers[answerId] : scenario.card) : null
  const question = answerId === null ? card?.clarification : null
  const answer = scenario.card.clarification?.options.find((option) => option.id === answerId)

  useEffect(() => {
    if (card) resultRef.current?.focus()
  }, [card])

  return (
    <>
      <header className="bg-brand text-white">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-5 py-4 sm:px-8">
          <span className="text-lg font-bold">KTRU Match</span>
          <a className="demo-repo-link text-sm underline underline-offset-4" href="https://github.com/rTexty/ktru-match">
            Исходный код на GitHub
          </a>
        </div>
      </header>

      <div className="mx-auto max-w-5xl space-y-6 px-5 py-8 sm:px-8 sm:py-12">
        <div className="max-w-2xl">
          <h1 className="text-3xl font-bold tracking-tight text-brand-dark sm:text-4xl">Подбор позиции КТРУ</h1>
          <p className="mt-3 leading-7 text-slate-700">
            Выберите описание и посмотрите, как масса влияет на выбор позиции.
            Демо использует ответы движка для готовых примеров из учебного каталога.
          </p>
          <a className="mt-2 inline-block text-sm text-brand underline underline-offset-4" href="https://github.com/rTexty/ktru-match#локальный-запуск">
            Как ввести своё описание
          </a>
        </div>

        <form
          className="space-y-5 border border-line bg-white p-5 sm:p-7"
          onSubmit={(event) => {
            event.preventDefault()
            setAnswerId(null)
            setSubmitted(true)
          }}
        >
          <fieldset>
            <legend className="mb-3 text-sm font-bold">Описание товара</legend>
            <div className="grid gap-2 sm:grid-cols-3">
              {scenarios.map(({ description }, index) => (
                <label key={description} className={cn(
                  'flex min-h-12 cursor-pointer items-center gap-3 border px-3 py-2 text-sm font-semibold text-brand',
                  selected === index ? 'border-brand bg-brand-soft' : 'border-line hover:border-brand',
                )}>
                  <input
                    type="radio"
                    name="description"
                    className="accent-brand"
                    checked={selected === index}
                    onChange={() => {
                      setSelected(index)
                      setSubmitted(false)
                      setAnswerId(null)
                    }}
                  />
                  {description}
                </label>
              ))}
            </div>
          </fieldset>
          <button className={btn.primary} type="submit">Подобрать позицию</button>
        </form>

        {card && (
          <section ref={resultRef} tabIndex={-1} aria-label="Результат подбора" className="@container space-y-4">
            <CodeSummary card={card} busy={false} onDownload={() => downloadCard(card)} />
            {question && (
              <div role="group" aria-label="Уточнение" className="border border-line bg-white p-5 sm:p-7">
                <h2 className="text-xl font-bold">{question.question_ru}</h2>
                <p className="mt-2 text-sm text-slate-700">{question.why_ru}</p>
                <div className="mt-4 flex flex-wrap gap-2">
                  {question.options.filter((option) => scenario.answers[option.id]).map((option) => (
                    <button key={option.id} type="button" className={btn.secondary} onClick={() => setAnswerId(option.id)}>
                      {option.label_ru}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {answer && (
              <div className="flex flex-wrap items-center justify-between gap-3 border border-line bg-white px-5 py-2">
                <p className="text-sm">Ответ: <strong>{answer.label_ru}</strong></p>
                <button type="button" className={btn.small} onClick={() => setAnswerId(null)}>Отменить ответ</button>
              </div>
            )}
            {card.final && (
              <div className="border border-line bg-white p-5 sm:p-7">
                <h2 className="text-xl font-bold">Характеристики</h2>
                <dl className="mt-3 grid gap-2">
                  {card.fields.map((field) => (
                    <div key={field.id} className="flex flex-wrap gap-x-3 text-sm">
                      <dt className="font-semibold">{field.label_ru}</dt>
                      <dd>{field.display_ru}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}
            <NarrowingFunnel card={card} />
          </section>
        )}
      </div>
    </>
  )
}
