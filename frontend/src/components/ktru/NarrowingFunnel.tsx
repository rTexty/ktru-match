import type { CardState, FunnelScope } from '@/api/ktru'
import { Badge, Section } from './ui'

const SCOPES: Record<FunnelScope['kind'], string> = {
  catalog: 'Каталог в снимке',
  retrieved_families: 'Найденные семейства',
  family: 'Выбранное семейство',
}
const TRIGGERS = { retrieval: 'Поиск', assertion: 'Характеристики', answer: 'Ответ пользователя', edit: 'Правка' }

export function NarrowingFunnel({ card }: { card: CardState }) {
  if (card.funnel.length === 0) return null
  return (
    <Section
      title="Подбор кода"
      right={
        card.search_scope && (
          <Badge tone={card.search_scope.retrieval_truncated ? 'amber' : 'gray'}>
            {card.search_scope.retrieval_truncated
              ? 'Поиск ограничен'
              : card.search_scope.coverage_status === 'unconfirmed'
                ? 'Область не подтверждена'
                : card.search_scope.coverage_status === 'family_confirmed'
                  ? 'Семейство подтверждено'
                  : 'Область поиска проверена полностью'}
          </Badge>
        )
      }
    >
      <ol className="grid gap-4 @lg:grid-cols-2 @3xl:grid-cols-3">
        {card.funnel.map((stage, index) => {
          const scope = (card.funnel_scopes ?? []).find((item) => item.id === stage.scope_id)
          return (
            <li key={`${stage.scope_id}:${index}`} className="border-brand min-w-0 border-l-[3px] pl-4">
              <p className="text-xs leading-5 break-words text-slate-600">{stage.label_ru}</p>
              <p className="text-ink my-1 text-[28px] leading-9 font-bold tabular-nums">{stage.count.toLocaleString('ru-RU')}</p>
              <p className="text-xs leading-5 text-slate-600">
                {scope ? SCOPES[scope.kind] : 'Область не указана'}, {TRIGGERS[stage.trigger].toLocaleLowerCase('ru')}
              </p>
              {scope && !scope.membership_complete && <p className="text-xs leading-5 text-amber-800">Состав загружен не полностью</p>}
              {stage.evidence.length > 0 && (
                <details className="mt-1">
                  <summary className="text-brand min-h-11 py-2 text-xs">Основания: {stage.evidence.length}</summary>
                  <ul className="space-y-1 text-xs leading-5 text-slate-600">
                    {stage.evidence.map((evidence, i) => (
                      <li key={i}>{evidence.type === 'text' ? `«${evidence.span.text}»` : evidence.type === 'user_answer' ? 'Ответ пользователя' : evidence.type === 'user_edit' ? 'Правка пользователя' : 'Данные каталога КТРУ'}</li>
                    ))}
                  </ul>
                </details>
              )}
            </li>
          )
        })}
      </ol>
      {card.search_scope && <p className="border-line mt-4 border-t pt-3 text-xs leading-5 text-slate-700">{card.search_scope.reason_ru}</p>}
    </Section>
  )
}
