import type { CardField, FieldStatus } from '@/api/ktru'
import { cn } from '@/lib/cn'
import { useId, useRef, useState } from 'react'
import type { Actions } from './ResultView'
import { btn, field } from './styles'
import { Badge, type Tone } from './ui'

const STATUS: Record<FieldStatus, [string, Tone]> = {
  from_text: ['Из текста', 'blue'],
  from_catalog: ['Из каталога', 'gray'],
  confirmed: ['Подтверждено', 'green'],
  needs_answer: ['Требует ответа', 'amber'],
  conflict: ['Конфликт', 'red'],
  unparsed: ['Не распознано', 'gray'],
}

export function FieldRow({
  field: f,
  draft,
  onDraft,
  badge,
  actions,
  selected,
  onSelect,
}: {
  field: CardField
  draft?: string
  onDraft: (value: string | null, savedValue?: string) => void
  badge: [string, Tone]
  actions: Actions
  selected: boolean
  onSelect: () => void
}) {
  const inputId = useId()
  const rowRef = useRef<HTMLDivElement>(null)
  const [saveFailed, setSaveFailed] = useState(false)
  const restoreFocus = () => requestAnimationFrame(() => rowRef.current?.querySelector<HTMLButtonElement>('[data-edit-field]')?.focus())
  const value = draft ?? ''
  const editing = draft !== undefined
  const [status, tone] = STATUS[f.status]
  const cancel = () => {
    setSaveFailed(false)
    onDraft(null)
    restoreFocus()
  }
  const save = async () => {
    if (actions.busy) return
    if (await actions.setField(f.label_ru, value, f.id)) {
      onDraft(null, value)
      setSaveFailed(false)
      restoreFocus()
    } else setSaveFailed(true)
  }

  return (
    <div
      ref={rowRef}
      className={cn(
        'grid min-w-0 grid-cols-1 gap-1 border-l-2 py-2 pl-3 md:grid-cols-[minmax(0,1.2fr)_minmax(0,1.6fr)_minmax(9rem,1fr)] md:gap-3',
        selected ? 'border-l-brand bg-brand-soft' : f.status === 'conflict' ? 'border-l-red-500 bg-red-50/40' : f.status === 'needs_answer' ? 'border-l-amber-500' : 'border-l-transparent',
      )}
    >
      <div className="min-w-0 text-sm leading-6 break-words text-slate-700">
        {f.assertion_ids.length > 0 ? (
          <button type="button" onClick={onSelect} className="text-brand text-left font-medium break-words hover:underline" title="Показать в тексте">
            {f.label_ru}
          </button>
        ) : (
          <span className="font-medium">{f.label_ru}</span>
        )}
        {f.required && (
          <span className="ml-1 text-red-700" title="Обязательная характеристика">
            *
          </span>
        )}
        {f.subject?.kind && f.subject.kind !== 'item' && (
          <p className="text-xs leading-5 text-slate-600">{f.subject.label ?? { package: 'Упаковка', set: 'Набор', unknown: 'Объект требует уточнения', part: 'Часть товара' }[f.subject.kind]}</p>
        )}
      </div>
      <div className="min-w-0">
        {editing ? (
          <div>
            <label htmlFor={inputId} className={saveFailed ? 'mb-2 block text-xs font-medium text-amber-900' : 'sr-only'}>
              {saveFailed ? `Несохранённая правка: ${f.label_ru}` : f.label_ru}
            </label>
            <p id={`${inputId}-hint`} className="mb-2 text-xs leading-5 text-slate-600">
              Текущее значение: <span>{f.display_ru || 'не заполнено'}</span>. Укажите значение вместе с единицей измерения.
            </p>
            <input
              id={inputId}
              autoFocus
              onFocus={(event) => event.target.select()}
              aria-describedby={`${inputId}-hint`}
              value={value}
              maxLength={500}
              onChange={(event) => onDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  void save()
                }
                if (event.key === 'Escape') {
                  event.preventDefault()
                  cancel()
                }
              }}
              className={`${field} min-h-12 w-full min-w-0 px-3 py-2`}
            />
            <div className="mt-1 flex flex-wrap gap-1">
              <button type="button" className={btn.small} disabled={actions.busy} onClick={() => void save()}>
                Сохранить
              </button>
              <button type="button" className={btn.small} onClick={cancel}>
                Отмена
              </button>
            </div>
          </div>
        ) : (
          <p className="text-sm leading-6 font-semibold break-words text-slate-900">{f.display_ru || <span className="font-normal text-slate-600">Не заполнено</span>}</p>
        )}
        {f.evidence_ru && f.evidence_ru.replace(/^«|»$/g, '').trim() !== f.display_ru.trim()
          && <p className="mt-1 text-xs leading-5 break-words text-slate-600">{f.evidence_ru}</p>}
        {f.portal_category && <p className="mt-1 text-xs leading-5 break-words text-violet-800">Категория Портала: {f.portal_category}</p>}
      </div>
      <div className="flex min-w-0 flex-wrap items-start gap-1 md:justify-end">
        <Badge tone={badge[1]}>{badge[0]}</Badge>
        <Badge tone={tone}>{status}</Badge>
        {!editing && (
          <button data-edit-field type="button" className={btn.small} disabled={actions.busy} onClick={() => onDraft(f.display_ru)}>
            Изменить
          </button>
        )}
      </div>
    </div>
  )
}
