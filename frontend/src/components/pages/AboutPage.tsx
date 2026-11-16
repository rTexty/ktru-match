import { btn } from '@/components/ktru/styles'
import { PageHeader } from '@/components/layout/AppShell'
import { useHealth } from '@/components/layout/useHealth'
import { Link } from '@tanstack/react-router'

const STAGES = [
  ['Разбор описания', 'Правила и открытая языковая модель выделяют из текста свойства, значения и единицы измерения. Каждый факт привязан к цитате. Если в описании несколько товаров, у каждого свой набор фактов.'],
  ['Поиск в КТРУ', 'Поиск по снимку каталога находит подходящие семейства позиций. Каждое найденное семейство рассматривается целиком, без случайных обрезок.'],
  ['Сверка с каталогом', 'Единицы переводятся, значения проверяются по справочнику КТРУ. Позиции, которые противоречат описанию, исключаются, и для каждой видна причина.'],
  ['Уточнение', 'Если подходят несколько позиций, сервис спрашивает о характеристике, которая лучше всего их различает. Ответ «не знаю» тоже принимается.'],
  ['Карточка товара', 'Характеристики заполняются с указанием источника: КТРУ, Портал или новое поле. Карточка готова, когда выбран код, заполнены обязательные поля и нет блокирующих проверок.'],
]

const PRINCIPLES = [
  ['Характеристики из КТРУ', 'Названия и допустимые значения берутся из каталога. Модель предлагает толкование, а проверяет его код.'],
  ['Открытые модели', 'Описание обрабатывается открытыми моделями на сервере сервиса и не передаётся во внешние API.'],
  ['Ничего не теряется', 'Свойства, которых нет в КТРУ, остаются в карточке как поля Портала или новые поля. Противоречия показываются, а не удаляются.'],
]

const LLM = { available: 'Подключена', disabled: 'Не подключена, работает разбор по правилам', unavailable: 'Временно недоступна' }

export function AboutPage() {
  const { data, isError } = useHealth()
  const active = data ? (data.counts.positions_active ?? data.counts.active_positions) : undefined
  const rows: [string, string][] = data
    ? [
        ['Снимок КТРУ', data.snapshot_date ?? 'Нет данных'],
        ['Действующих позиций', active !== undefined ? active.toLocaleString('ru-RU') : 'Нет данных'],
        ['Языковая модель', data.model_manifest?.model ? `${LLM[data.llm]}: ${data.model_manifest.model}` : LLM[data.llm]],
        ['Готовность сервиса', data.ready ? 'Работает' : 'Каталог загружается'],
      ]
    : []

  return (
    <>
      <PageHeader crumbs={['Каталог продукции', 'Как это работает']} title="Как сервис подбирает код КТРУ">
        <p className="mt-3 max-w-2xl text-base leading-7 text-slate-600">От свободного описания товара до карточки с кодом и характеристиками из каталога. Каждое решение можно проверить и исправить.</p>
      </PageHeader>
      <div className="mx-auto mt-10 grid max-w-[1440px] items-start gap-5 px-4 sm:px-6 lg:grid-cols-[minmax(0,1fr)_400px]">
        <section aria-labelledby="stages-title" className="bg-white p-5 sm:p-10">
          <h2 id="stages-title" className="text-ink mb-8 text-2xl font-bold">
            Пять шагов подбора
          </h2>
          <ol className="max-w-2xl">
            {STAGES.map(([title, text], index) => (
              <li key={title} className="relative flex gap-5 pb-8 last:pb-0">
                {index < STAGES.length - 1 && <span aria-hidden="true" className="bg-line absolute top-10 bottom-0 left-[19px] w-0.5" />}
                <span aria-hidden="true" className="bg-brand relative grid h-10 w-10 shrink-0 place-items-center rounded-full text-base font-bold text-white">
                  {index + 1}
                </span>
                <div className="pt-1.5">
                  <h3 className="text-ink text-lg font-bold">{title}</h3>
                  <p className="mt-2 text-base leading-7 text-slate-700">{text}</p>
                </div>
              </li>
            ))}
          </ol>
          <Link to="/" className={`${btn.primary} mt-10`}>
            Подобрать КТРУ
          </Link>
        </section>
        <div className="space-y-5 lg:sticky lg:top-24">
          <section aria-labelledby="data-title" className="bg-white p-5 sm:p-8">
            <h2 id="data-title" className="text-ink mb-4 text-lg font-bold">
              Данные сервиса
            </h2>
            {isError ? (
              <p className="text-sm leading-6 text-red-800">Сервер не отвечает. Данные появятся, когда он снова будет доступен.</p>
            ) : !data ? (
              <p className="text-sm text-slate-600">Загружаем…</p>
            ) : (
              <dl className="divide-line divide-y">
                {rows.map(([term, value]) => (
                  <div key={term} className="grid gap-1 py-3 first:pt-0 last:pb-0">
                    <dt className="text-xs text-slate-600">{term}</dt>
                    <dd className="text-ink text-sm font-semibold break-words">{value}</dd>
                  </div>
                ))}
              </dl>
            )}
          </section>
          <section aria-labelledby="principles-title" className="bg-white p-5 sm:p-8">
            <h2 id="principles-title" className="text-ink mb-4 text-lg font-bold">
              Принципы
            </h2>
            <ul className="space-y-5">
              {PRINCIPLES.map(([title, text]) => (
                <li key={title} className="border-brand border-l-[3px] pl-4">
                  <h3 className="text-ink text-sm font-bold">{title}</h3>
                  <p className="mt-1 text-sm leading-6 text-slate-600">{text}</p>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </>
  )
}
