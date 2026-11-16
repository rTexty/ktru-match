import type { Clarification } from '@/api/ktru'
import type { Actions } from './ResultView'
import { btn, field } from './styles'

export function NumericAnswer({ question, actions, value, onChange }: { question: Clarification; actions: Actions; value: string; onChange: (value: string | null, savedValue?: string) => void }) {
  const valid = /^[+-]?\d+(?:[.,]\d+)?$/.test(value.trim())
  const invalid = value.length > 0 && !valid
  const id = `answer-${question.id}`
  return (
    <form
      className="border-brand/20 mt-4 border-t pt-4"
      onSubmit={async (event) => {
        event.preventDefault()
        if (!valid || actions.busy) return
        if (
          await actions.answerValue(question.id, {
            type: 'number',
            x: value.trim().replace(',', '.'),
            unit_okei: question.value_unit_okei,
            unit_name: question.value_unit_name,
          })
        )
          onChange(null, value)
      }}
    >
      <label className="mb-2 block text-sm font-medium text-slate-800" htmlFor={id}>
        Ваше значение{question.value_unit_name ? `, ${question.value_unit_name}` : ''}
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <input id={id} inputMode="decimal" maxLength={100} value={value} aria-invalid={invalid} aria-describedby={`${id}-hint`} onChange={(event) => onChange(event.target.value)} className={`${field} min-h-12 w-36 px-3 py-2`} />
        <button type="submit" className={btn.secondary} disabled={actions.busy || !valid}>
          Ответить
        </button>
      </div>
      <p id={`${id}-hint`} className={`mt-2 text-xs leading-5 ${invalid ? 'text-red-800' : 'text-slate-600'}`}>
        {invalid ? 'Введите число, например 123,5.' : 'Можно использовать запятую или точку. Единица указана над полем.'}
      </p>
    </form>
  )
}
