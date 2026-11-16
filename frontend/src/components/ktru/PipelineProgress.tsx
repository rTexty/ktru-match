import type { CardState } from '@/api/ktru'
import { cn } from '@/lib/cn'
import { Icon } from './Icon'
import { Badge, Section } from './ui'

type StageState = 'done' | 'running' | 'partial' | 'skipped'

const MARK: Record<StageState, string> = {
  done: 'bg-emerald-600 text-white',
  running: 'bg-brand text-white',
  partial: 'bg-amber-500 text-white',
  skipped: 'bg-slate-200 text-slate-500',
}

function formatMs(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} с` : `${Math.max(1, Math.round(ms))} мс`
}

/** What ran for this card: rules, retrieval, the open model and resolution. The model runs after the rest, so it is the stage that is pending or skipped. */
export function PipelineProgress({ card, modelPending, modelOff }: { card: CardState; modelPending: boolean; modelOff: boolean }) {
  const t = card.timings_ms
  const model: [StageState, string] =
    card.status === 'analyzed' ? ['done', formatMs(t.extract)] : card.status === 'partial' ? ['partial', t.extract ? formatMs(t.extract) : 'частично'] : modelPending && !modelOff ? ['running', 'идёт…'] : ['skipped', modelOff ? 'не подключена' : 'не запускалась']
  const stages: [string, string, StageState, string][] = [
    ['Разбор текста', 'правила, исправление опечаток и раскладки', 'done', formatMs(t.parse)],
    ['Поиск по КТРУ', 'семейства и позиции каталога', 'done', formatMs(t.retrieve)],
    ['Проверка моделью', card.status === 'partial' ? 'часть ответа модели не принята' : 'открытая модель, факты подтверждены цитатами', ...model],
    ['Подбор кода', 'характеристики, вопросы, проверки', 'done', formatMs(t.resolve)],
  ]
  const finished = stages.filter(([, , state]) => state === 'done' || state === 'partial').length
  return (
    <Section title="Этапы обработки" right={<Badge tone={finished === stages.length ? 'green' : 'gray'}>{`${finished} из ${stages.length}`}</Badge>}>
      <ol className="flex flex-col gap-2.5">
        {stages.map(([label, hint, state, time], index) => (
          <li key={label} className="flex items-start gap-3">
            <span aria-hidden="true" className={cn('mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs font-bold', MARK[state])}>
              {state === 'done' ? <Icon name="check" className="h-3.5 w-3.5" /> : state === 'running' ? <Icon name="refresh" className="h-3.5 w-3.5 motion-safe:animate-spin" /> : state === 'partial' ? '!' : index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className={cn('text-sm leading-6 font-semibold', state === 'skipped' ? 'text-slate-500' : 'text-ink')}>{label}</p>
              <p className="text-xs leading-5 text-slate-600">{hint}</p>
            </div>
            <span className={cn('shrink-0 text-xs leading-6 tabular-nums', state === 'running' ? 'text-brand font-semibold' : 'text-slate-600')}>{time}</span>
          </li>
        ))}
      </ol>
      <p className="border-line mt-3 border-t pt-2 text-xs leading-5 text-slate-600">Всего {formatMs(t.total)}</p>
    </Section>
  )
}
